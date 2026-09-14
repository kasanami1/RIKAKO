import json, sqlite3, logging
import re
from datetime import datetime
from pathlib import Path
# 联网检索逻辑集中在 web_tools.WebSearcher；本模块只负责“抽取 + 归档 + 审核”。

CATEGORIES = ('人物基本信息','性格与行为模式','背景故事与经历','核心语录与口头禅','人际关系网','作品世界观设定')

# 导入文档时的抽取提示词模板（可用 config 的 knowledge_import_prompt 覆盖）。
# 关键点：只依据材料本身，不许模型用自己的先验知识补充——否则「导入资料」就失去意义。
DEFAULT_IMPORT_PROMPT = '''你是一个角色资料整理助手。目标角色是「{name}」。

下面是从用户导入的资料《{source}》中截取的一段材料。请**只依据这段材料**，抽取关于「{name}」的客观信息。

分类列表：
- 人物基本信息（姓名、别名、性别、年龄、出生地、职业等）
- 性格与行为模式
- 背景故事与经历
- 核心语录与口头禅
- 人际关系网
- 作品世界观设定

严格要求：
1. 材料里没有写的内容不要补充；不要使用你自己的先验知识，也不要推测。
2. 这段材料与「{name}」无关时，输出空数组 []。
3. content 写成能直接放进设定集的完整陈述句，保留具体细节（人名、地名、时间、台词原文等）。
4. source 一律填「{source}」。
5. confidence 表示这段材料对这条信息的支撑程度，0~1。

只输出JSON数组，不要输出Markdown、解释或代码块：
[{"include": true, "category": "人物基本信息", "content": "……", "source": "{source}", "confidence": 0.9}]
'''


def read_document(path):
    """读入要导入的文档。txt/md 直接读，html 去标签；编码按常见顺序依次尝试。

    中文小说 txt 大量是 GBK/GB18030，只按 UTF-8 读会得到一堆乱码，所以这里必须退码。
    """
    path = Path(path)
    raw = path.read_bytes()
    text = ''
    for encoding in ('utf-8-sig', 'utf-8', 'gb18030', 'utf-16'):
        try:
            text = raw.decode(encoding); break
        except (UnicodeDecodeError, LookupError):
            continue
    if not text: text = raw.decode('utf-8', errors='replace')
    if path.suffix.lower() in ('.html', '.htm'):
        try:
            from web_tools import html_to_text
            text = html_to_text(text)
        except Exception:
            pass
    text = re.sub(r'[ \t\u00a0]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def split_chunks(text, size=6000, overlap=200):
    """把长文切成块送给模型；块间留重叠，避免正好把一条事实切成两半。"""
    text = (text or '').strip()
    if not text: return []
    size = max(500, int(size or 6000)); overlap = max(0, min(int(overlap or 0), size // 2))
    if len(text) <= size: return [text]
    chunks = []; start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            # 只在块的后半段找断点，优先在换行/句号处断开，读起来更完整。
            tail = text[start:end]
            cut = max(tail.rfind('\n'), tail.rfind('。'), tail.rfind('！'), tail.rfind('？'))
            if cut > int(size * 0.5): end = start + cut + 1
        chunks.append(text[start:end])
        if end >= len(text): break
        start = max(end - overlap, start + 1)
    return chunks

class KnowledgeBase:
    def __init__(self, db, root, config=None):
        # config 传 ConfigManager（或普通 dict）时，检索行为可被 config.json 调整。
        self.db, self.root, self.config = db, Path(root), config
        # entries() 用 dict(row) 取值，依赖 Row 工厂；以前是靠 repositories 顺手设的，
        # 这里自己设一次，免得换个调用顺序就报 "cannot convert dictionary update sequence"。
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''CREATE TABLE IF NOT EXISTS knowledge_entries(id INTEGER PRIMARY KEY,category TEXT NOT NULL,content TEXT NOT NULL,source TEXT DEFAULT '',status TEXT DEFAULT '待审核',created_at TEXT,updated_at TEXT); CREATE TABLE IF NOT EXISTS knowledge_history(id INTEGER PRIMARY KEY,entry_id INTEGER,category TEXT,content TEXT,status TEXT,changed_at TEXT); CREATE TABLE IF NOT EXISTS knowledge_meta(key TEXT PRIMARY KEY,value TEXT)''')
    def _setting(self, key, default):
        if self.config is None: return default
        getter=getattr(self.config,'get',None)
        return getter(key, default) if callable(getter) else self.config.get(key, default)
    def entries(self, category=None):
        q='SELECT * FROM knowledge_entries'; args=()
        if category: q+=' WHERE category=?'; args=(category,)
        return [dict(r) for r in self.db.execute(q+' ORDER BY id',args).fetchall()]
    def save(self, category, content, source='', status='待审核', entry_id=None):
        content=str(content or '').strip()
        if not content:
            # 面板里的「新增」可能带着空文本框触发，直接拦掉，避免留下空白条目。
            logging.getLogger('knowledge').warning('知识内容为空，已忽略本次保存')
            return
        now=datetime.now().isoformat(timespec='seconds')
        if entry_id:
            old=self.db.execute('SELECT * FROM knowledge_entries WHERE id=?',(entry_id,)).fetchone(); self.db.execute('INSERT INTO knowledge_history(entry_id,category,content,status,changed_at) VALUES(?,?,?,?,?)',(entry_id,old['category'],old['content'],old['status'],now)); self.db.execute('UPDATE knowledge_entries SET category=?,content=?,source=?,status=?,updated_at=? WHERE id=?',(category,content,source,status,now,entry_id))
        else: self.db.execute('INSERT INTO knowledge_entries(category,content,source,status,created_at,updated_at) VALUES(?,?,?,?,?,?)',(category,content,source,status,now,now))
        self.db.commit()
    def delete(self, entry_id): self.db.execute('DELETE FROM knowledge_entries WHERE id=?',(entry_id,)); self.db.commit()

    # ------------------------------------------------------------------
    # 去重
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize(text):
        return re.sub(r'[\s\W_]+', '', str(text or ''))

    def _similarity(self, a, b):
        """相似度：2-gram 的**包含度**为主、Jaccard 为辅。

        中文短句上用 Jaccard 会严重漏检：「…之谜》中的角色」和「…之谜》中的主要角色」
        只差两个字，Jaccard 却只有 0.77。改成看「短的那条有多少被长的包含」就很准。
        为避免「短条目被长段落吞掉」，两者长度差太多时只认 Jaccard。
        """
        na, nb = self._normalize(a), self._normalize(b)
        if not na or not nb: return 0.0
        if na == nb: return 1.0
        ga, gb = self._ngrams(na, (2,)), self._ngrams(nb, (2,))
        if not ga or not gb: return 0.0
        inter = len(ga & gb)
        union = len(ga | gb)
        jaccard = inter / float(union) if union else 0.0
        ratio = min(len(na), len(nb)) / float(max(len(na), len(nb)))
        if ratio < 0.7: return jaccard          # 长度差太多，不当成同一条
        contain = inter / float(min(len(ga), len(gb)))
        return max(jaccard, contain)

    @staticmethod
    def _order_group(group):
        # 建议保留哪条：已确认优先 → 内容更长（信息更全）→ id 更小（更早采集）
        return sorted(group, key=lambda e: (0 if e['status'] == '已确认' else 1, -len(e['content']), e['id']))

    def find_duplicates(self, threshold=None, category=None):
        """找出重复条目，返回分组；每组第一条是建议保留的。

        只在同一分类内比较：不同分类出现相同内容，往往是用户有意分开归档的。
        """
        if threshold is None: threshold = float(self._setting('knowledge_dedupe_threshold', 0.75) or 0.75)
        entries = [e for e in self.entries(category) if str(e.get('content') or '').strip()]
        groups = []; used = set()
        for i, entry in enumerate(entries):
            if i in used: continue
            group = [entry]; used.add(i)
            for j in range(i + 1, len(entries)):
                if j in used: continue
                other = entries[j]
                if other['category'] != entry['category']: continue
                if any(self._similarity(other['content'], member['content']) >= threshold for member in group):
                    group.append(other); used.add(j)
            if len(group) > 1: groups.append(self._order_group(group))
        return groups

    def delete_many(self, ids):
        ids = [int(i) for i in ids]
        if not ids: return 0
        marks = ','.join('?' for _ in ids)
        self.db.execute(f'DELETE FROM knowledge_entries WHERE id IN ({marks})', tuple(ids)); self.db.commit()
        return len(ids)

    def duplicate_of(self, content, category, threshold=None):
        """入库前查重：返回已存在的相似条目，没有则 None。"""
        if threshold is None: threshold = float(self._setting('knowledge_dedupe_threshold', 0.75) or 0.75)
        if not self._normalize(content): return None
        for entry in self.entries(category):
            if self._similarity(content, entry['content']) >= threshold: return entry
        return None

    DEDUPE_PROMPT = '''你是知识库去重助手。下面同一个分类下的条目中，有些在表达同一件事，只是措辞不同。

判断标准：**说的是同一个事实**才算重复。例如「她是回末家当主」和「她担任回末家现任当家」是重复；
但只是同一主题、信息不同（如「生日是2月29日」和「身高170厘米」）不算重复。

**删除不能丢信息**：被删条目里如果有保留条目没写到的独有细节，必须把这些细节补进 keep_content。
例如保留项写了「与房石阳明有感情线」，被删项还写了「她负责照顾幼女咩子」，那 keep_content 要把咩子这条一并写入。

对每一组：
- keep：信息最完整的那条的编号。
- remove：其余编号。
- keep_content：**合并后的完整内容**（保留项原文 + 被删项里的独有细节）。没有需要补充的细节时可以省略该字段。

只输出JSON数组，不要解释、不要Markdown代码块：
[{"keep": 3, "remove": [7, 12], "keep_content": "合并后的内容"}]
没有重复就输出 []。'''

    async def find_duplicates_ai(self, api, batch_size=120):
        """用模型找**语义**重复。

        纯字面相似度对改写无能为力：实测库里三条「…是《人狼村之谜》的角色」的
        不同措辞，字面相似度最高只有 0.76，按字面查重一条都找不出来。

        只发**一次**请求（把所有分类一起给模型）：早先按分类逐个调用，遇到推理
        模型每次都要几十秒，6 个分类直接跑到超时。
        返回 {'groups': [[保留项, 重复项...]], 'checked': [...], 'failed': [...], 'entries': n}。
        """
        from llm_provider import provider_from_config
        logger = logging.getLogger('knowledge')
        prompt = self._setting('knowledge_dedupe_prompt', '') or self.DEDUPE_PROMPT
        entries = [e for e in self.entries() if str(e.get('content') or '').strip()]
        result = {'groups': [], 'checked': [], 'failed': [], 'entries': len(entries)}
        if len(entries) < 2: return result
        # 按分类整块分批，避免把同一分类拆到两次请求里而漏掉跨批的重复。
        batches = []; current = []; size = 0
        for category in CATEGORIES:
            items = [e for e in entries if e['category'] == category]
            if len(items) < 2: continue
            if current and size + len(items) > batch_size: batches.append(current); current = []; size = 0
            current.extend(items); size += len(items)
        if current: batches.append(current)
        provider = provider_from_config(self._util_provider_config(api))
        for batch in batches:
            listing = ''
            for category in CATEGORIES:
                items = [e for e in batch if e['category'] == category]
                if items: listing += '分类：%s\n%s\n' % (category, '\n'.join('[%d] %s' % (e['id'], e['content']) for e in items))
            try:
                raw = str(await provider.chat([{'role':'system','content':prompt},{'role':'user','content':listing}]) or '').strip()
            except Exception as exc:
                logger.warning('查重调用失败：%s', exc); result['failed'].append('%d 条一批' % len(batch)); continue
            if not raw or raw.startswith('暂时无法连接'):
                logger.warning('查重没有可用返回：%s', raw[:140] or '返回为空'); result['failed'].append('%d 条一批' % len(batch)); continue
            data, how = self._parse_items(raw)
            if how != 'strict': logger.warning('查重返回不是规范 JSON（%s）', how)
            by_id = {e['id']: e for e in batch}
            for item in data:
                if not isinstance(item, dict): continue
                keep = by_id.get(item.get('keep'))
                dups = [by_id[i] for i in (item.get('remove') or []) if i in by_id and i != item.get('keep')]
                if not (keep and dups): continue
                # 模型给的合并内容：删除前先把被删项里的独有细节补进保留项，避免丢信息。
                merged = str(item.get('keep_content') or '').strip()
                if merged and merged != str(keep['content']).strip(): keep['_merged'] = merged
                result['groups'].append([keep] + dups)
            result['checked'].append('%d 条' % len(batch))
            logger.info('查重本批 %d 条：累计发现 %d 组', len(batch), len(result['groups']))
        return result
    @staticmethod
    def _ngrams(text, sizes=(2, 3, 4)):
        """中文没有空格，按 2~4 字滑窗切分，才能跟知识条目做有意义的匹配。"""
        cleaned=re.sub(r'[^\w\u4e00-\u9fff]+', '', str(text or ''))
        grams=set()
        for size in sizes:
            for i in range(len(cleaned)-size+1):
                grams.add(cleaned[i:i+size])
        return grams

    def _background(self, entries, limit):
        """没有任何关键词命中时，兜底塞几条核心设定，保证桌宠开口时有角色依据。"""
        if not self._setting('knowledge_always_inject', True): return []
        order={category: index for index, category in enumerate(CATEGORIES)}
        ranked=sorted(entries, key=lambda e: (e['status'] != '已确认', order.get(e['category'], 99)))
        return ranked[:limit]

    def search(self, text, limit=8):
        """按 n-gram 重合度打分检索。

        注意：待审核与已确认的条目**都会**参与检索（可用 knowledge_use_pending
        关闭）。原来用 SQL LIKE 配空格分词，中文消息整句变成一个词，导致
        采集到的知识几乎永远进不了对话，这里一并修掉。
        """
        use_pending=bool(self._setting('knowledge_use_pending', True))
        # 过滤空白条目：历史遗留的空行不该占注入名额。
        entries=[e for e in self.entries() if str(e.get('content') or '').strip() and (use_pending or e['status'] == '已确认')]
        if not entries: return []
        grams=self._ngrams(text)
        if not grams: return self._background(entries, min(limit, 3))
        scored=[]
        for entry in entries:
            content=entry['content']
            score=sum(len(gram)*content.count(gram) for gram in grams if gram in content)
            if score:
                if entry['status'] == '已确认': score += 1  # 已确认略加权，但不排挤待审核
                scored.append((score, entry))
        if not scored: return self._background(entries, min(limit, 3))
        scored.sort(key=lambda pair: -pair[0])
        return [entry for _, entry in scored[:limit]]
    def _query_plan(self, name, description, api):
        """把搜索词模板展开成一条或多条查询（用 | 分隔可跑多轮检索）。

        默认只用角色名：实测 Bing 这类搜索引擎对「名字 + 一堆限定词」会松散匹配，
        反而把「回末李花子」拆成「回」字词条；只搜名字最准。需要补充时再自己加
        模板，例如 `{name}|{name} 语录|{name} 人设`。
        """
        template = api.get('knowledge_web_query') or '{name}'
        queries = []
        for part in str(template).split('|'):
            text = part.replace('{name}', name or '').replace('{description}', description or '').strip()
            text = re.sub(r'\s+', ' ', text).strip()
            if text and text not in queries:
                queries.append(text)
        if not queries:
            queries = [' '.join(x for x in (name, description) if x).strip()]
        return [q for q in queries[:4] if q]

    def _exists(self, content):
        """按去空白内容去重，避免每次搜集都把同样的条目再存一遍。"""
        key = re.sub(r'\s+', '', str(content))
        if not key:
            return True
        for row in self.db.execute('SELECT content FROM knowledge_entries'):
            if re.sub(r'\s+', '', row['content']) == key:
                return True
        return False

    async def _collect_web(self, name, description, api, max_results, logger):
        """调用本地联网组件，返回 (给模型的检索文本, 来源列表, 后端名)。"""
        from web_tools import WebSearcher
        searcher = WebSearcher.from_config(api)
        if searcher.backend == 'off':
            logger.info('联网组件后端为 off，本次跳过联网检索')
            return '', [], 'off'
        logger.info('联网组件配置：%s', searcher.describe())
        hits, seen = [], set()
        for query in self._query_plan(name, description, api):
            logger.info('检索关键词：%s', query)
            found = await searcher.search(query, limit=max_results)
            for hit in found:
                marker = (hit.url or '') + '|' + hit.title
                if marker in seen:
                    continue
                seen.add(marker)
                hits.append(hit)
        text = searcher.format_for_prompt(hits)
        sources = searcher.sources(hits)
        logger.info('联网检索完成：后端=%s，结果=%d 条，正文入提示词=%d 字', searcher.last_backend or '-', len(hits), len(text))
        return text, sources, searcher.last_backend

    @staticmethod
    def _parse_items(raw):
        """把模型返回解析成条目列表，尽量宽容。

        模型返回 JSON 时经常带瑕疵：外面套 ```json 代码块、前后加解释文字、
        字符串里混进真实换行。逐级降级解析，最后才退回「整段作为一条候选」，
        避免一次坏 JSON 就把整段塞进知识库。返回 (条目列表, 用了哪种方式)。
        """
        cleaned = re.sub(r'^```(?:json)?\s*|\s*```\s*$', '', str(raw).strip(), flags=re.I | re.S).strip()
        match = re.search(r'\[[\s\S]*\]', cleaned)
        candidate = match.group(0) if match else cleaned
        for how, strict in (('strict', True), ('lenient', False)):
            try:
                data = json.loads(candidate, strict=strict)
                if isinstance(data, list):
                    return data, how
            except Exception:
                continue
        salvaged = []
        for block in re.findall(r'\{[^{}]*\}', candidate, re.S):
            for strict in (True, False):
                try:
                    item = json.loads(block, strict=strict)
                except Exception:
                    continue
                if isinstance(item, dict):
                    salvaged.append(item)
                    break
        if salvaged:
            return salvaged, 'salvaged'
        # Keep a non-JSON model answer as one reviewable candidate
        # instead of silently discarding useful research.
        return [{'include': True, 'category': '人物基本信息', 'content': cleaned, 'source': '知识库 API', 'confidence': 0.55}], 'raw'

    @staticmethod
    def _provider_config(api, use_web_search=False):
        return {'provider': api.get('knowledge_api_provider', api.get('provider','echo')), 'model': api.get('knowledge_api_model', api.get('model','gpt-4o-mini')), 'api_key': api.get('knowledge_api_key', api.get('api_key','')), 'url': api.get('knowledge_api_url', api.get('url','')), 'base_url': api.get('knowledge_api_url', api.get('base_url','')), 'max_tokens': api.get('knowledge_api_max_tokens',500), 'use_web_search': use_web_search, 'temperature': api.get('knowledge_api_temperature',0.7), 'top_p': api.get('knowledge_api_top_p',1.0), 'frequency_penalty': api.get('knowledge_api_frequency_penalty',0.0), 'presence_penalty': api.get('knowledge_api_presence_penalty',0.0), 'stream': api.get('knowledge_api_stream',True), 'timeout': api.get('knowledge_api_timeout',180)}

    def _util_provider_config(self, api):
        """工具类调用（查重、文档归纳）专用的 provider 配置。

        这类任务输入短、输出短，但用主模型里的重推理模型会每次先烧几千 token
        思维链：实测 deepseek-v4-pro 查一次重 73 秒还返回空，deepseek-chat 只要
        1 秒。所以给它们单独留一个可选模型（knowledge_util_model），留空则沿用主模型。
        """
        cfg = self._provider_config(api, use_web_search=False)
        cfg['model'] = str(self._setting('knowledge_util_model', '') or cfg['model'])
        cfg['max_tokens'] = int(self._setting('knowledge_util_max_tokens', 8000) or 8000)
        return cfg

    async def ingest_documents(self, name, documents, api, prompt=None, max_chars=500):
        """把导入的文档归纳进知识库。

        documents 可以是文件路径列表，也可以是 (来源名, 文本) 列表。
        长文档会分块、逐块交给模型抽取，并且**只依据材料本身**、不允许模型拿
        自己的先验知识补充——否则导入资料就失去了意义。返回 (新增条数, 实情说明)。
        """
        logger = logging.getLogger('knowledge')
        docs = []
        for item in documents:
            if isinstance(item, (str, Path)):
                path = Path(item); docs.append((path.name, read_document(path)))
            else:
                docs.append((str(item[0]), str(item[1])))
        size = int(self._setting('knowledge_import_chunk_chars', 6000) or 6000)
        overlap = int(self._setting('knowledge_import_overlap', 200) or 200)
        budget = int(self._setting('knowledge_import_max_chunks', 30) or 30)
        chunks = []
        for filename, text in docs:
            for piece in split_chunks(text, size, overlap): chunks.append((filename, piece))
        truncated = max(0, len(chunks) - budget); chunks = chunks[:budget]
        if not chunks: return 0, '导入的文件没有可读文本'
        logger.info('文档归纳开始：%d 个文件、%d 块（单次上限 %d 块，本次截断 %d 块）', len(docs), len(chunks), budget, truncated)
        template = prompt or self._setting('knowledge_import_prompt', '') or DEFAULT_IMPORT_PROMPT
        from llm_provider import provider_from_config
        provider = provider_from_config(self._util_provider_config(api))
        count = 0; skipped = 0; failed = 0
        for index, (filename, piece) in enumerate(chunks, 1):
            system = template.replace('{name}', name or '').replace('{source}', filename)
            before = count
            try:
                raw = str(await provider.chat([{'role':'system','content':system},{'role':'user','content':f'材料（{filename} 第 {index} 段）：\n{piece}'}]) or '').strip()
            except Exception as exc:
                logger.warning('第 %d 块调用失败：%s', index, exc); failed += 1; continue
            if not raw or raw.startswith('暂时无法连接'):
                logger.warning('第 %d 块没有可用返回：%s', index, raw[:160] or '返回为空'); failed += 1; continue
            data, how = self._parse_items(raw)
            if how != 'strict': logger.warning('第 %d 块返回不是规范 JSON（%s），解析出 %d 条', index, how, len(data))
            for item in data:
                if item.get('include') is not True or not item.get('content'): continue
                content = str(item['content'])[:max_chars]
                category = item.get('category') if item.get('category') in CATEGORIES else '人物基本信息'
                # 同一轮里也要查重：相邻块有重叠，很容易抽出同一条事实。
                if self._exists(content) or self.duplicate_of(content, category):
                    skipped += 1; continue
                self.save(category, content, filename, '待审核'); count += 1
            logger.info('文档归纳进度 %d/%d：本块新增 %d 条（累计 %d）', index, len(chunks), count - before, count)
        notes = ['导入 %d 个文件' % len(docs), '处理 %d 块' % len(chunks)]
        if count: notes.append('新增 %d 条' % count)
        if skipped: notes.append('查重跳过 %d 条' % skipped)
        if failed: notes.append('%d 块处理失败（详见 app.log）' % failed)
        if truncated: notes.append('超出单次上限，还有 %d 块未处理' % truncated)
        if not count and not failed: notes.append('没有抽到新条目' + ('（内容可能都已存在，可点“检测重复”清理）' if skipped else ''))
        note = '；'.join(notes)
        now = datetime.now().isoformat(timespec='seconds')
        for key, value in (('last_updated', now), ('last_search_backend', '文档导入'), ('last_note', note)):
            self.db.execute("INSERT OR REPLACE INTO knowledge_meta(key,value) VALUES(?,?)", (key, value))
        self.db.commit()
        logger.info('文档归纳完成：%s', note)
        return count, note

    async def build(self, name, description, api, prompt, max_results=10, max_chars=500):
        # 联网有两条互相独立的路径，可以只开一条也可以同时开：
        #   1. API 自身联网  knowledge_api_web_search —— 走 /v1/responses 的
        #      web_search 工具，模型自己搜，不需要额外 Key；
        #   2. 本地联网组件  knowledge_web_backend —— web_tools 先搜好网页再喂给
        #      模型，任何模型都能用，来源 URL 明确。
        logger = logging.getLogger('knowledge')
        api_web = bool(api.get('knowledge_api_web_search', False))
        local_backend = str(api.get('knowledge_web_backend', 'auto') or 'auto').lower()
        local_web = local_backend != 'off'
        notes = []
        logger.info('开始搜集角色：%s，Provider=%s，API自身联网=%s，本地联网=%s', name, api.get('knowledge_api_provider', api.get('provider','echo')), '开' if api_web else '关', local_backend if local_web else 'off')
        try:
            from llm_provider import provider_from_config
            provider_base=self._provider_config(api)
            provider = provider_from_config({**provider_base, 'use_web_search': api_web})
            search_text, web_sources, web_backend = '', [], ''
            if local_web:
                search_text, web_sources, web_backend = await self._collect_web(name, description, api, max_results, logger)
                logger.info('本地检索摘要长度：%d', len(search_text))
                if search_text:
                    notes.append('本地 %s 搜到 %d 个来源' % (web_backend or local_backend, len(web_sources)))
                else:
                    notes.append('本地联网检索没有结果')
                    hint = ('本地联网检索没有返回结果；请检查网络/代理，或在“知识库 API 设置”里换成 tavily/bocha 等搜索 API（详细原因见 app.log 的 web 日志）。')
                    if api.get('knowledge_web_strict', False) and not api_web:
                        raise RuntimeError(hint + '（已开启“检索失败即中止”，且 API 自身联网也关闭）')
                    if api_web:
                        logger.warning('%s 本次交给 API 自身联网检索。', hint)
                    else:
                        logger.warning('%s 本次改用模型自身知识继续。', hint)
            else:
                notes.append('本地联网已关闭')
            if not api_web and not local_web:
                notes = ['未使用任何联网：仅凭模型自身知识整理，真实性完全取决于该模型']
            material = f'\n本地检索结果：\n{search_text}\n' if search_text else '\n（本次无本地检索结果）\n'
            tail = f'\n即使只有一条可靠事实也请输出，最多{max_results}条。只输出JSON数组，每项含 include(boolean), category, content, source, confidence(0-1)。'

            def build_messages(web_ok):
                if search_text:
                    basis_text = '本地检索结果如下，请结合它们、你联网检索到的信息和你确认的知识作答；不得把我的补充描述当作事实。每条的 source 请填真实来源 URL。'
                elif web_ok:
                    basis_text = '本次没有预置的本地检索结果，请你**自行联网检索**核实后再作答；不得把我的补充描述当作事实。每条的 source 请填你实际检索到的来源 URL。'
                else:
                    basis_text = '公开检索结果为空，请使用你已有且确定的知识，并明确降低 confidence；不要把补充描述当事实；source 请如实写“模型已有知识”而不是编造链接。'
                return [{'role':'system','content':prompt},
                        {'role':'user','content':f'角色“{name}”。补充描述：{description}{material}{basis_text}{tail}'}]

            result = await provider.chat(build_messages(api_web))
            raw = str(result or '').strip()
            # API 自身联网失败不再静默吞掉：回退继续跑，但把实情写进 note 交给界面显示。
            if api_web and (not raw or raw.startswith('暂时无法连接')):
                reason = re.sub(r'^暂时无法连接第三方 API：', '', raw or '返回为空').strip()
                # 归类成短句：原始报错里套着括号，直接塞进界面会是一团乱码般的括号。
                if '没有真正执行检索' in reason: reason = '模型收下了联网工具但没有真的去搜'
                elif '返回为空' in reason: reason = '接口返回为空（推理模型可能 token 不够）'
                else: reason = reason.splitlines()[0][:70]
                notes.append('API 自身联网不可用：' + reason)
                logger.warning('API 自身联网不可用：%s', raw[:200] or '返回为空')
                logger.warning('已改回普通对话接口，只用本地检索结果 + 模型整理。')
                result = await provider_from_config({**provider_base, 'use_web_search': False}).chat(build_messages(False))
                raw = str(result or '').strip()
            elif api_web:
                notes.append('API 自身联网正常')
            logging.getLogger('knowledge').info('API 返回长度=%d，前200字符=%r', len(raw), raw[:200])
            if not raw: raise ValueError('API 返回为空，请检查模型、API 地址或联网权限')
            if raw.startswith('暂时无法连接'):
                raise RuntimeError(raw)
            # Accept strict JSON, fenced ```json blocks, or explanatory text
            # surrounding the first JSON array.
            data, how = self._parse_items(raw)
            if how != 'strict':
                logger.warning('模型返回不是规范 JSON 数组（解析方式=%s），已解析出 %d 条', how, len(data))
            if not isinstance(data, list): raise ValueError('API 返回格式不是 JSON 数组')
            if not data:
                logger.warning('API 返回空数组 []：接口正常，但模型没有给出可确认的知识条目')
                note = '；'.join(notes + ['模型没有给出可入库的条目'])
                self.db.execute("INSERT OR REPLACE INTO knowledge_meta(key,value) VALUES('last_updated',?)",(datetime.now().isoformat(timespec='seconds'),))
                self.db.execute("INSERT OR REPLACE INTO knowledge_meta(key,value) VALUES('source_count',?)",('0',))
                self.db.execute("INSERT OR REPLACE INTO knowledge_meta(key,value) VALUES('last_search_backend',?)",(web_backend or '未联网',))
                self.db.execute("INSERT OR REPLACE INTO knowledge_meta(key,value) VALUES('last_note',?)",(note,))
                self.db.commit(); return 0, note
            count=0; skipped=0; sources=set(web_sources)
            for item in data if isinstance(data,list) else []:
                # `confidence` is advisory for human review. Any candidate
                # explicitly included by the model is persisted as pending
                # review, even when confidence is low.
                if item.get('include') is True and item.get('content'):
                    content=str(item['content'])[:max_chars]
                    category=item.get('category','人物基本信息') if item.get('category') in CATEGORIES else '人物基本信息'
                    # 精确重复和「意思相同但措辞不同」的近似重复都挡掉。
                    if self._exists(content) or self.duplicate_of(content, category):
                        skipped+=1; continue
                    self.save(category, content, item.get('source','联网检索'), '待审核'); count+=1; sources.add(item.get('source','联网检索'))
            if skipped: notes.append('去重跳过 %d 条' % skipped)
            note='；'.join(notes)
            meta={'last_updated':datetime.now().isoformat(timespec='seconds'),'source_count':str(len(sources)),'last_search_backend':web_backend or '未联网','last_search_sources':json.dumps(list(sources)[:20],ensure_ascii=False),'last_note':note}
            for key,value in meta.items():
                self.db.execute("INSERT OR REPLACE INTO knowledge_meta(key,value) VALUES(?,?)",(key,value))
            self.db.commit()
            logger.info('搜集完成：新增 %d 条；%s', count, note)
            return count, note
        except Exception as exc:
            logging.getLogger('knowledge').exception('知识库搜集失败')
            raise RuntimeError(f'知识库 API 返回异常或连接失败：{exc}') from exc

    def summary(self):
        meta={r['key']:r['value'] for r in self.db.execute('SELECT key,value FROM knowledge_meta')}; return meta
