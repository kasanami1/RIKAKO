import asyncio, json, sqlite3, re, logging
from pathlib import Path
from PySide6.QtCore import QObject, Signal
from config_manager import ConfigManager
from llm_provider import is_error, provider_from_config
from memory import MemoryManager, ContextManager
from knowledge_base import KnowledgeBase

class ConversationManager(QObject):
    reply_received=Signal(str); thinking=Signal(bool)
    # 模型/接口出错时的提示（**不是**角色台词：不进气泡当台词、更不会朗读）
    error_received=Signal(str)
    # 模型在回复里指定的表情（名字一定来自素材库白名单，见 parse_expression）
    expression_requested=Signal(str)
    def __init__(self, root=None, config=None, provider=None):
        super().__init__(); root=Path(root or '.'); self.db=sqlite3.connect(root/'memory.db'); self.db.executescript('CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,role TEXT,content TEXT,created_at TEXT); CREATE TABLE IF NOT EXISTS memories(id INTEGER PRIMARY KEY,content TEXT,importance INTEGER,created_at TEXT); CREATE TABLE IF NOT EXISTS conversation_summaries(id INTEGER PRIMARY KEY,content TEXT,created_at TEXT);')
        self.config=config or ConfigManager(root/'config.json'); self.provider=provider or provider_from_config(self.config.data); self.memory=MemoryManager(self.db); self.context=ContextManager(self.db); self.persona=(root/'prompts'/'persona.json'); self.knowledge=(root/'knowledge.json'); self.root=root; self.character='default'
        self.blocked_keywords=self.config.get('blocked_keywords',['AI','模型','程序','角色扮演','JSON','提示词','思维链'])
        self.knowledge_base=KnowledgeBase(self.db, root, self.config)
        self._player=None
        # 逐句合成时用来判断「这批语音是不是已经被打断/切换了」。
        self._speech_generation=0
        # 屏幕理解的优先级判定要用：用户最后一次说话的时间、角色是否正在回复。
        self.last_user_activity=0.0
        self.thinking_now=False
        self.fallback='你说的话好奇怪喵，我听不懂'
    def _knowledge_context(self, user_text):
        """把命中的角色知识拼进系统提示。待审核条目同样参与（可在配置里关闭）。

        桌宠回复很短，所以对注入长度设上限，避免知识库挤掉人设与记忆。
        """
        limit=int(self.config.get('knowledge_max_chars',1200) or 1200)
        related=self.knowledge_base.search(user_text)
        if not related: return ''
        picked=[]; used=0
        for entry in related:
            content=entry['content']
            if used+len(content)>limit and picked: break
            picked.append(content); used+=len(content)
        return '\n相关角色知识：'+'；'.join(picked) if picked else ''
    def _system_prompt(self):
        default=f'你是{self.character}，一个友善、简洁的桌面宠物。只用第一人称回答，每次不超过30个汉字。'
        try:
            data=json.loads(self.persona.read_text(encoding='utf-8')); prompt=f'当前角色名称：{self.character}\n'+data.get('system',default)+'\n禁止：'+ '；'.join(data.get('forbidden',[]))
            return prompt
        except Exception: return default

    # ------------------------------------------------------------------ 语气指令
    # 语气分三层，别混：
    #   ① 这一块（语气**标注**提示词）：教模型「按上下文给每一句标一个语气」，每次都发；
    #   ② persona/config 里的 instructions：那一句「该怎么读」的默认描述（发给合成接口）；
    #   ③ 模型写在回复里的 [语气:说明]：真正逐句生效的东西，由 ① 引导出来。
    DEFAULT_TONE_PROMPT = (
        '回复时请为每一句话单独判断语气，并在那句话前面写一个标记：【语气:说明】。\n'
        '说明要短、要具体，贴合这句话在上下文里的情绪与处境，例如：小声、急促、轻而慢、'
        '带笑意、冷淡、犹豫、故作镇定、温柔却疏离。\n'
        '同一段回复里，不同句子可以用完全不同的语气——请按上下文判断，不要套模板、不要千篇一律。\n'
        '没有特别之处就写【语气:平常】。\n'
        '这些标记用户看不到、也不会被读出来，不要向用户解释它们。'
    )

    def persona_tts(self):
        """角色自己的语气设置（`prompts/persona.json` 里的 `tts` 块）。

        这是「按角色」的那一层：换角色就换语气，和 config.json 里的全局设置叠加，
        persona 里的优先。可写字段：
            {"tts": {"instructions": "语气清冷，语速偏慢", "optimize_instructions": true,
                     "model": "qwen3-tts-instruct-flash", "voice": "Cherry",
                     "tone_prompt": "自定义的语气标注提示词", "tone_prompt_enabled": true}}
        """
        try:
            data = json.loads(self.persona.read_text(encoding='utf-8'))
            block = data.get('tts') or {}
            return {k: v for k, v in block.items() if k in
                    ('instructions', 'optimize_instructions', 'model', 'voice', 'language',
                     'tone_prompt', 'tone_prompt_enabled')}
        except Exception:
            return {}

    def tone_instructions(self):
        """这一轮该用的语气指令：角色设置优先，其次全局配置，都没有就空。"""
        persona = self.persona_tts()
        tone = str(persona.get('instructions') or self.config.get('tts_instructions', '') or '').strip()
        return tone

    def tone_prompt_text(self):
        """语气标注提示词的正文（角色层 → 全局 → 内置默认）。"""
        persona = self.persona_tts()
        text = str(persona.get('tone_prompt') or self.config.get('tone_prompt', '') or '').strip()
        return text or self.DEFAULT_TONE_PROMPT

    def tone_prompt_enabled(self):
        """这一块要不要发。角色层写了就以角色层为准，否则看全局开关（默认开）。"""
        persona = self.persona_tts()
        if 'tone_prompt_enabled' in persona:
            return bool(persona['tone_prompt_enabled'])
        return bool(self.config.get('tone_prompt_enabled', True))

    def tone_prompt_block(self):
        """语气标注提示词：**每次对话都注入**的一段独立区块。

        和以前的写法有个重要区别：以前只在「填了语气」时才教模型写标记，
        现在是独立一块、恒输入，让模型**主动**按上下文逐句判断语气——
        同一句「这样的结局，真是让人遗憾」在得意时是「轻佻」、在失落时是「低声」，
        这个判断是模型做的，不是我们写死的。
        """
        if not self.tone_prompt_enabled(): return ''
        text = self.tone_prompt_text()
        if not text: return ''
        return '\n\n【语气标注（每次都按这个来）】\n' + text

    TONE_TAG = re.compile(r'[\[【（(]\s*语气\s*[:：]\s*([^\]】）)]{1,40})\s*[\]】）)]')

    @classmethod
    def parse_tone(cls, text):
        """取出语气标记，返回 (干净文本, [(语气, 这段文本), ...])。

        语气是**往后生效**的：`[语气:小声] 甲。[语气:大喊] 乙。`
        → `[('小声', '甲。'), ('大喊', '乙。')]`。
        没写标记的那段语气是 `''`（表示「用全局默认」，由 speak 去套）。
        标记本身从正文里删掉——**既不显示、也不朗读**，和表情标记一样。
        """
        raw = str(text or '')
        merged = []          # [[语气, 文本], ...]
        pending = None       # 刚读到、还没挂到文字上的语气
        cursor = 0

        def flush(piece):
            """把一段正文挂上当前语气；空白段直接丢掉（标记周围常带空格）。"""
            nonlocal pending
            body = piece.strip()
            if not body: return
            merged.append([pending or '', body])
            pending = None

        for match in cls.TONE_TAG.finditer(raw):
            flush(raw[cursor:match.start()])
            pending = match.group(1).strip()
            cursor = match.end()
        flush(raw[cursor:])
        if not merged:                              # 全是标记：别把回复弄空
            return raw.strip(), []
        clean = re.sub(r'[ \t]{2,}', ' ', ''.join(body for _, body in merged)).strip()
        return clean, [(tone, body) for tone, body in merged]

    # ------------------------------------------------------------------ 表情标签
    def expression_names(self):
        """素材库里可用的表情名（`assets/characters/<角色>/expressions/` 下的目录）。

        没素材时返回空列表——那就不给模型发标签说明，也没有可解析的东西。
        """
        folder=getattr(self,'character_root',None)
        if folder is None:
            root=getattr(self,'root',None)
            folder=(root/'assets'/'characters'/self.character) if root else None
        if folder is None: return []
        base=folder/'expressions'
        if not base.exists(): return []
        try: return sorted(p.name for p in base.iterdir() if p.is_dir())
        except OSError: return []

    def expression_instruction(self):
        """往系统提示里加一段「你可以指定表情」的说明（闭集，不许自己造词）。"""
        if not self.config.get('expression_enabled', True): return ''
        names=self.expression_names()
        if not names: return ''
        return ('\n如果你想让角色露出某个表情，就在回复最前面写一个标记：[表情:名字]，'
                '名字只能从这个列表里选：%s。没有合适的就不要写，不要自己造名字。'
                '标记不会显示给用户，也不会被读出来。' % '、'.join(names))

    @staticmethod
    def parse_expression(text, available):
        """从回复里取出表情标记，返回 (干净文本, 表情名或空)。

        只认白名单里的名字：模型偶尔会编一个（甚至把整句话塞进方括号），
        那种一律当没写——否则角色会变成空白或者报错。
        """
        raw=str(text or '')
        names={str(n).strip() for n in (available or [])}
        found=''
        pattern=re.compile(r'[\[【（(]\s*表情\s*[:：]\s*([^\]】）)]{1,20})\s*[\]】）)]')
        def take(match):
            nonlocal found
            name=match.group(1).strip()
            if name in names and not found: found=name
            return ''                      # 标记一律从正文里去掉（TTS 也不能念出来）
        clean=pattern.sub(take, raw)
        clean=re.sub(r'^[\s,,。、:：]+','',clean).strip()
        if not clean and not found:
            clean=raw.strip()      # 一个标记都没匹配到：原样返回，别把正常回复弄空
        return clean, found

    def _apply_expression(self, name):
        if name:
            logging.getLogger('conversation').info('回复指定表情：%s', name)
            self.expression_requested.emit(name)

    @staticmethod
    def clean_reply(text):
        text=re.sub(r'[（(].*?[）)]','',str(text),flags=re.S)
        text=re.sub(r'^(首先|其次|最后|综上所述|总的来说)[:：，,。\s]*','',text.strip())
        text=re.sub(r'^(分析|思考|推理)[:：].*?([。！？!?]|$)','',text,flags=re.S).strip()
        return text[:120] or '……喵？你说啥'
    async def send_message(self, user_text):
        """把用户输入发给模型并播报回复。

        每一步都写日志、整体兜异常：以前这里任何一步出错，协程直接死掉，
        asyncio 又不报错（pythonw 下连 stderr 都没有），表现就是「消息发出去了
        但角色毫无反应」，而且日志里什么都没有。现在最坏也会回一句错误提示。
        """
        log = logging.getLogger('conversation')
        if not user_text.strip(): return
        # 用户说话了：屏幕理解要立刻让位（这个时间戳也是它判断「用户空不空闲」的依据）。
        import time as _time
        self.last_user_activity=_time.monotonic()
        if any(word.lower() in user_text.lower() for word in self.blocked_keywords):
            self.reply_received.emit(self.fallback); return
        self.thinking.emit(True); self.thinking_now=True
        try:
            log.info('收到消息，长度=%d', len(user_text))
            system = (self._system_prompt() + self.tone_prompt_block()
                      + self.expression_instruction() + self._knowledge_context(user_text))
            log.info('提示词就绪，长度=%d', len(system))
            from repositories import MessageRepository
            repo = MessageRepository(self.db); repo.add('user', user_text)
            log.info('调用模型 %s …', self.config.get('model', ''))
            raw = await self.provider.chat(self.context.build(system, self.memory.retrieve(user_text)))
            # 接口出错时 provider 是「返回一句提示」而不是抛异常。这一步必须拦住：
            # 以前那句话会被当成回复 —— 直接显示在气泡里、还被语音念出来
            # （实测中继偶发 504，角色就用很长的 TTS 把「HTTP 504 Gateway Time-out」念了一遍）。
            if is_error(raw):
                log.error('模型接口报错：%s', str(raw)[:200])
                self.error_received.emit(str(raw).strip())
                return
            log.info('模型返回，长度=%d', len(str(raw or '')))
            # 先剥语气标记（它带着「哪句用哪种语气」），再剥表情，最后才做 clean_reply
            # —— clean_reply 会把括号内容删掉，顺序反了语气标记就没了。
            detoned, segments = self.parse_tone(raw)
            clean, expression = self.parse_expression(detoned, self.expression_names())
            reply = self.clean_reply(clean)
            repo.add('assistant', reply)
            self.reply_received.emit(reply)
            self._apply_expression(expression)
            if segments and any(tone for tone, _ in segments):
                log.info('回复指定语气：%s',
                         '；'.join('%s→%s' % (tone, body[:12]) for tone, body in segments if tone))
            # 语音只是「把回复读出来」，合成慢或失败都不该拖住对话，所以单独起任务。
            speak = asyncio.create_task(self.speak(reply, segments))
            speak.add_done_callback(lambda t: t.cancelled() or t.exception() and logging.getLogger('voice').error('语音任务异常', exc_info=t.exception()))
            # 记忆整理失败不该影响已经给出的回复，所以放在播报之后单独兜住。
            try:
                await self.memory.extract(user_text, reply, self.provider); await self.context.compress(self.provider)
            except Exception:
                log.exception('记忆整理失败（回复已正常给出）')
        except Exception as exc:
            log.exception('回复失败')
            self.reply_received.emit(f'（出错了：{type(exc).__name__}，详见 app.log）')
        finally:
            self.thinking.emit(False); self.thinking_now=False

    # ------------------------------------------------------------------ 屏幕理解
    def screen_system_prompt(self):
        """给视觉模型的系统提示（按模式分两种）。

        * `direct`：直接把角色人设给它，让它**以角色身份开口**；
        * `describe`：只要客观描述，台词交给角色模型来写（人设更稳，但多一次调用）。
        """
        mode = str(self.config.get('screen_reply_mode', 'direct') or 'direct').lower()
        if mode == 'describe':
            return ('你是屏幕内容识别助手。用一两句话客观描述这块屏幕上正在发生什么：'
                    '谁在做什么、画面里有什么明显的文字或界面。看不清就说看不清，不要编造，也不要评价。')
        extra = str(self.config.get('screen_prompt', '') or '').strip() or (
            '你正在偷偷瞄用户的屏幕。用你的角色口吻，对屏幕上正在发生的事说一句很短的话'
            '（不超过30个字），像在旁边看热闹；不要解说画面、不要说教，'
            '也不要提“截图 / 图片 / 屏幕 / API”这些词。')
        return self._system_prompt() + self.tone_prompt_block() + '\n' + extra

    async def ambient_say(self, text):
        """屏幕上看到的东西 → 让角色主动说一句。

        与用户对话的区别（有意为之）：
        * **不写数据库**：既不污染对话上下文，也不把屏幕内容留在磁盘上；
        * 优先级由 ScreenWatcher 把关，这里再兜一道「正在回复用户就别插嘴」。
        """
        log = logging.getLogger('screen')
        text = str(text or '').strip()
        if not text: return
        if self.thinking_now:
            log.info('角色正在回复用户，忽略这次屏幕观察'); return
        try:
            mode = str(self.config.get('screen_reply_mode', 'direct') or 'direct').lower()
            segments = None
            if mode == 'describe':
                # 两段式：上面拿到的是客观描述，这里让角色模型把它变成一句台词。
                system = (self._system_prompt() + self.tone_prompt_block()
                          + self.expression_instruction() + '\n' + (
                    '用户没有跟你说话，你只是瞄到了他屏幕上的一幕。'
                    '用你的口吻对这件事说一句很短的话（不超过30个字），不要提“截图/图片/屏幕”。'))
                raw = await self.provider.chat(self.context.build(system, self.memory.retrieve(text[:40])))
                if is_error(raw):
                    log.error('屏幕观察转台词时接口报错：%s', str(raw)[:200])
                    self.error_received.emit(str(raw).strip()); return
                detoned, segments = self.parse_tone(raw)
                clean, expression = self.parse_expression(detoned, self.expression_names())
                line = self.clean_reply(clean)
            else:
                detoned, segments = self.parse_tone(text)
                clean, expression = self.parse_expression(detoned, self.expression_names())
                line = self.clean_reply(clean)
            if not line: return
            self.reply_received.emit(line)
            self._apply_expression(expression)
            task = asyncio.create_task(self.speak(line, segments))
            task.add_done_callback(lambda t: t.cancelled() or t.exception() and logging.getLogger('voice').error('语音任务异常', exc_info=t.exception()))
            log.info('屏幕主动开口：%s', line[:60])
        except Exception:
            log.exception('屏幕观察转台词失败')

    async def speak(self, text, segments=None):
        """把角色回复合成语音并播放。任何失败都只记日志，绝不影响对话。

        `segments` 是 parse_tone 给的 `[(语气, 文本)]`。云端 instruct 模型能逐句换语气，
        所以那种情况下每一段单独发一次请求；本地 GPT-SoVITS 没有语气参数，只按句拆。
        """
        log = logging.getLogger('voice')
        try:
            from voice import SpeechPlayer, split_sentences, tts_from_config
            engine = tts_from_config(self.config, self.persona_tts())
            if engine is None: return
            ok, why = engine.availability()
            if not ok:
                log.warning('语音合成不可用：%s', why); return
            # 括号里通常是「（微笑）」这类动作描述，读出来很奇怪，去掉。
            def readable(part):
                return re.sub(r'[（(][^）)]*[）)]', '', str(part)).strip()
            # 引擎自带的全局语气（config.json / persona.json 里那条），没写标记的句子用它。
            fallback = str(getattr(engine, 'instructions', '') or '').strip()
            blocks = []
            if getattr(engine, 'supports_tone', False) and segments:
                for tone, body in segments:
                    for part in split_sentences(readable(body)):
                        blocks.append((tone or fallback, part))
            if not blocks:
                blocks = [(fallback, part) for part in split_sentences(readable(text))]
            if not blocks: return
            if self._player is None: self._player = SpeechPlayer()
            self._player.volume = int(self.config.get('tts_volume', 80) or 80)
            # 本地引擎慢（CPU 上一整段要二十几秒），按句合成、边合成边排队播，
            # 第一句先出声；云端很快，整段一次合成。
            if getattr(engine, 'sentence_pipeline', False):
                self._speech_generation += 1
                generation = self._speech_generation
                log.info('本地语音按句合成：共 %d 段（%s）',
                         len(blocks), '、'.join('%d字' % len(p) for _, p in blocks))
                for index, (_, part) in enumerate(blocks, 1):
                    if generation != self._speech_generation:
                        log.info('语音已被打断，停止后续合成'); return
                    try:
                        audio = await engine.synthesize(part)
                    except Exception:
                        log.exception('第 %d/%d 句合成失败，跳过', index, len(blocks)); continue
                    self._player.enqueue(audio)
                    log.info('已排队角色语音 %d/%d：%d 字节', index, len(blocks), len(audio))
            elif len({tone for tone, _ in blocks}) <= 1:
                audio = await engine.synthesize(''.join(part for _, part in blocks),
                                                instructions=blocks[0][0])
                self._player.enqueue(audio)
                log.info('已播放角色语音：%d 字节（语气：%s）', len(audio), blocks[0][0] or '默认')
            else:
                log.info('云端按语气分段合成：%s',
                         '；'.join('%s→%s' % (tone or '默认', part[:12]) for tone, part in blocks))
                for index, (tone, part) in enumerate(blocks, 1):
                    try:
                        audio = await engine.synthesize(part, instructions=tone)
                    except Exception:
                        log.exception('第 %d/%d 句合成失败，跳过', index, len(blocks)); continue
                    self._player.enqueue(audio)
                    log.info('已排队角色语音 %d/%d：%d 字节（语气：%s）',
                             index, len(blocks), len(audio), tone or '默认')
        except Exception:
            log.exception('语音合成/播放失败')

    def stop_speech(self):
        # 自增代数：正在逐句合成的那个循环看到代数变了就自己收手。
        self._speech_generation += 1
        if self._player is not None: self._player.stop()
    def update_provider(self, values):
        self.config.update(values)
        self.provider = provider_from_config(self.config.data)

    def close(self):
        try: self.db.close()
        except Exception: pass

    def switch_character(self, name):
        """Use an isolated memory database and prompt/knowledge files per role."""
        import re
        safe = re.sub(r'[^\w\-\u4e00-\u9fff]', '_', name) or 'default'
        if self.db: self.db.close()
        self.character = name
        self.db = sqlite3.connect(self.root / f'memory_{safe}.db')
        self.db.executescript('CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,role TEXT,content TEXT,created_at TEXT); CREATE TABLE IF NOT EXISTS memories(id INTEGER PRIMARY KEY,content TEXT,importance INTEGER,created_at TEXT); CREATE TABLE IF NOT EXISTS conversation_summaries(id INTEGER PRIMARY KEY,content TEXT,created_at TEXT);')
        self.memory = MemoryManager(self.db); self.context = ContextManager(self.db); self.knowledge_base = KnowledgeBase(self.db, self.root, self.config)
        role_root = self.root / 'assets' / 'characters' / name
        self.persona = role_root / 'prompts' / 'persona.json' if (role_root / 'prompts' / 'persona.json').exists() else self.root / 'prompts' / 'persona.json'
        self.knowledge = role_root / 'knowledge.json' if (role_root / 'knowledge.json').exists() else self.root / 'knowledge.json'
