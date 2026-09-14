# -*- coding: utf-8 -*-
"""李花子桌宠 · 可插拔联网检索组件（web_tools）。

为什么需要它：原来「联网」这件事被绑在模型服务商身上（OpenAI 的
web_search_preview 工具），而项目实际用的是第三方 compatible 接口，
那个开关等于没生效；本地兜底只有一个 DuckDuckGo 网页抓取，很容易被
限流或被墙，抓不到就直接报错。

这个模块把「联网」从模型里解耦出来，变成桌宠本地的能力：

    1. 本地调用搜索引擎后端 → 拿到标题/链接/摘要（SearchHit）
    2. 可选：并发抓取前 N 个网页正文 → 给模型真正的内容而不只是摘要
    3. 拼成带引用编号 [1] [2] 的纯文本交给知识库 API 做抽取

支持的后端（backend）：
    auto        按下面的顺序自动挑第一个可用的（推荐）
    bing        Bing 的 RSS 输出，免 key、国内可直连、结果是干净 XML（最稳的兜底）
    searxng     自建/公共 SearXNG 实例，免 key（填 base_url 即可）
    tavily      Tavily Search API，专为 LLM 设计，有免费额度
    bocha       博查搜索（中文效果好），有免费额度
    zhipu       智谱 AI 网络搜索工具（中文，有免费额度）
    serper      Serper.dev（Google 结果），有免费额度
    brave       Brave Search API，有免费额度
    duckduckgo  免 key 网页抓取（国内多数网络不可达）
    wikipedia   免 key 的维基百科 API（国内多数网络不可达）
    off         完全关闭联网

用法：
    from web_tools import WebSearcher
    searcher = WebSearcher.from_config(config_dict)
    hits = await searcher.search('李花子 人设', limit=8)
    text = searcher.format_for_prompt(hits)
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from html import unescape
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

LOG = logging.getLogger('web')

DEFAULT_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 LilyPet/1.0'
)

# auto 模式下按此顺序尝试；需要 key 的后端没配 key 会被跳过。
AUTO_KEY_ORDER = ('tavily', 'bocha', 'zhipu', 'serper', 'brave')
# 免 key 兜底：bing 的 RSS 在国内可直连，优先；duckduckgo/wikipedia 仅作最后尝试。
AUTO_KEYLESS_ORDER = ('bing', 'duckduckgo', 'wikipedia')
BACKENDS = ('auto', 'bing', 'searxng', 'tavily', 'bocha', 'zhipu', 'serper', 'brave', 'duckduckgo', 'wikipedia', 'off')
KEYLESS_BACKENDS = ('bing', 'searxng', 'duckduckgo', 'wikipedia')

TAG_RE = re.compile(r'<[^>]+>')
DROP_RE = re.compile(r'<(script|style|noscript|svg|head)[^>]*>[\s\S]*?</\1>', re.I)
BLOCK_RE = re.compile(r'</?(p|div|br|li|tr|h[1-6]|section|article|blockquote|table)[^>]*>', re.I)
WS_RE = re.compile(r'[ \t\u00a0]+')


def html_to_text(html: str) -> str:
    """极简 HTML 正文提取：够用、无第三方依赖、对摘要场景足够稳。"""
    if not html:
        return ''
    text = DROP_RE.sub(' ', html)
    text = BLOCK_RE.sub('\n', text)
    text = TAG_RE.sub(' ', text)
    text = unescape(text)
    text = WS_RE.sub(' ', text)
    text = re.sub(r'\n\s*\n+', '\n', text)
    return text.strip()


def _xml_text(value: str) -> str:
    """RSS/XML 字段里常见的 CDATA 与实体清理。"""
    text = re.sub(r'^\s*<!\[CDATA\[|\]\]>\s*$', '', (value or '').strip())
    return html_to_text(unescape(text))


def _decode(response: httpx.Response) -> str:
    """按 charset 解码；没声明 charset 时先 UTF-8 再退 GBK（中文站点常见）。"""
    raw = response.content
    charset = response.charset_encoding or ''
    for enc in (charset, 'utf-8', 'gb18030'):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode('utf-8', errors='replace')


def _meta_description(html: str) -> str:
    """网页自带的 description 通常是最干净的一句话摘要，优先放进正文最前面。"""
    for pattern in (
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']',
    ):
        match = re.search(pattern, html, re.I | re.S)
        if match:
            text = html_to_text(match.group(1))
            if len(text) > 15:
                return text
    return ''


def _strip_boilerplate(text: str) -> str:
    """去掉导航栏、登录注册、相关推荐之类的模板噪音。

    导航条的特征很明显：一行里塞满「登录 注册 首页 客户端」这种 1~2 字的短词。
    """
    kept = []
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        tokens = line.split()
        if len(tokens) >= 5 and (sum(len(token) for token in tokens) / float(len(tokens))) <= 2.6:
            continue
        if len(line) < 10 and not re.search(r'[。！？!?；;]', line):
            continue
        kept.append(line)
    return '\n'.join(kept)


def _clean_url(url: str) -> str:
    """还原搜索引擎的跳转链接（DuckDuckGo 的 /l/?uddg=... 之类）。"""
    if not url:
        return ''
    url = unescape(url.strip())
    if url.startswith('//'):
        url = 'https:' + url
    if 'duckduckgo.com/l/' in url or '/l/?' in url:
        query = parse_qs(urlparse(url).query)
        if query.get('uddg'):
            return unquote(query['uddg'][0])
    return url


@dataclass
class SearchHit:
    """一条检索结果。text 在抓取正文后被填充。"""

    title: str
    url: str
    snippet: str = ''
    backend: str = ''
    text: str = ''

    def as_source(self) -> str:
        return self.url or self.backend or '联网检索'


class WebSearcher:
    """联网检索器：搜索 + 可选正文抓取 + 提示词格式化。"""

    def __init__(
        self,
        backend: str = 'auto',
        api_key: str = '',
        base_url: str = '',
        language: str = 'zh-CN',
        timeout: float = 15.0,
        pages: int = 3,
        page_chars: int = 1500,
        max_bytes: int = 700_000,
        concurrency: int = 4,
        proxy: str = '',
    ):
        self.backend = (backend or 'auto').strip().lower()
        if self.backend not in BACKENDS:
            LOG.warning('未知检索后端 %s，回退到 auto', self.backend)
            self.backend = 'auto'
        self.api_key = (api_key or '').strip()
        self.base_url = (base_url or '').strip().rstrip('/')
        self.language = language or 'zh-CN'
        self.timeout = float(timeout or 15.0)
        self.pages = max(0, int(pages))
        self.page_chars = max(200, int(page_chars))
        self.max_bytes = int(max_bytes)
        self.concurrency = max(1, int(concurrency))
        self.proxy = proxy or None
        self.last_backend = ''

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]] = None, prefix: str = 'knowledge_web_') -> 'WebSearcher':
        """从 config.json 读取 web 组件设置（默认前缀 knowledge_web_）。"""
        cfg = config or {}

        def pick(name: str, default: Any = None) -> Any:
            return cfg.get(prefix + name, default)

        return cls(
            backend=pick('backend', 'auto'),
            api_key=pick('api_key', ''),
            base_url=pick('base_url', ''),
            language=pick('language', 'zh-CN'),
            timeout=pick('timeout', 15),
            pages=pick('pages', 3),
            page_chars=pick('page_chars', 1500),
            max_bytes=pick('max_bytes', 700_000),
            concurrency=pick('concurrency', 4),
            proxy=pick('proxy', ''),
        )

    def describe(self) -> str:
        """给日志/界面看的一句话描述。"""
        if self.backend == 'off':
            return '联网已关闭'
        if self.backend == 'auto':
            plan = []
            if self.base_url:
                plan.append('searxng')
            plan += [b for b in AUTO_KEY_ORDER if self.api_key]
            plan += list(AUTO_KEYLESS_ORDER)
            return '自动（尝试顺序：' + ' → '.join(plan) + '）'
        if self.backend in KEYLESS_BACKENDS:
            return self.backend + ('（' + self.base_url + '）' if self.backend == 'searxng' and self.base_url else '')
        return self.backend + ('（已配置 key）' if self.api_key else '（缺少 API Key）')

    # ------------------------------------------------------------------ 入口
    async def search(self, query: str, limit: int = 8, expand: Optional[int] = None) -> List[SearchHit]:
        """检索并（可选）抓取正文。任何失败都只记录日志并返回已有结果。"""
        query = (query or '').strip()
        if not query or self.backend == 'off':
            return []
        limit = max(1, int(limit or 8))
        if expand is None:
            expand = self.pages
        headers = {'User-Agent': DEFAULT_UA, 'Accept-Language': f'{self.language},zh;q=0.9,en;q=0.8'}
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
                headers=headers,
                proxy=self.proxy,
            ) as client:
                hits = await self._search_with_fallback(client, query, limit)
                if hits and expand > 0:
                    await self._expand(client, hits, expand)
                return hits
        except Exception as exc:  # 网络层兜底，绝不把异常抛给调用方
            LOG.warning('联网检索异常（%s）：%s', self.backend, exc)
            return []

    async def _search_with_fallback(self, client: httpx.AsyncClient, query: str, limit: int) -> List[SearchHit]:
        tried: List[str] = []
        for name in self._plan():
            try:
                hits = await self._run_backend(client, name, query, limit)
            except Exception as exc:
                LOG.warning('检索后端 %s 失败：%s', name, exc)
                tried.append(f'{name}(失败)')
                continue
            if hits:
                self.last_backend = name
                LOG.info('检索后端 %s 命中 %d 条（尝试过：%s）', name, len(hits), ', '.join(tried) or name)
                return hits
            tried.append(f'{name}(空)')
        LOG.warning('所有检索后端都没有结果：%s', ', '.join(tried) or '无可用后端')
        return []

    def _plan(self) -> List[str]:
        if self.backend != 'auto':
            return [self.backend]
        plan: List[str] = []
        if self.base_url:
            plan.append('searxng')
        if self.api_key:
            plan.extend(AUTO_KEY_ORDER)
        plan.extend(AUTO_KEYLESS_ORDER)
        return plan

    async def _run_backend(self, client: httpx.AsyncClient, name: str, query: str, limit: int) -> List[SearchHit]:
        if name == 'bing':
            return await self._bing(client, query, limit)
        if name == 'searxng':
            return await self._searxng(client, query, limit)
        if name == 'tavily':
            return await self._tavily(client, query, limit)
        if name == 'bocha':
            return await self._bocha(client, query, limit)
        if name == 'zhipu':
            return await self._zhipu(client, query, limit)
        if name == 'serper':
            return await self._serper(client, query, limit)
        if name == 'brave':
            return await self._brave(client, query, limit)
        if name == 'duckduckgo':
            return await self._duckduckgo(client, query, limit)
        if name == 'wikipedia':
            return await self._wikipedia(client, query, limit)
        return []

    # ------------------------------------------------------- 各搜索引擎后端
    async def _bing(self, client: httpx.AsyncClient, query: str, limit: int) -> List[SearchHit]:
        """Bing 的 RSS 输出：免 key、国内可直连、返回干净 XML，最稳的免配置方案。"""
        response = await client.get(
            'https://www.bing.com/search',
            params={'q': query, 'format': 'rss', 'count': min(limit, 50), 'setlang': 'zh-CN'},
        )
        if response.status_code >= 400:
            raise RuntimeError(f'HTTP {response.status_code}')
        payload = _decode(response)
        items = re.findall(
            r'<item>[\s\S]*?<title>([\s\S]*?)</title>[\s\S]*?<link>([\s\S]*?)</link>[\s\S]*?<description>([\s\S]*?)</description>',
            payload,
        )
        return [
            SearchHit(_xml_text(title), _clean_url(_xml_text(url)), _xml_text(description), 'bing')
            for title, url, description in items[:limit]
        ]

    async def _searxng(self, client: httpx.AsyncClient, query: str, limit: int) -> List[SearchHit]:
        if not self.base_url:
            return []
        base = self.base_url if self.base_url.endswith('/search') else self.base_url + '/search'
        response = await client.get(base, params={'q': query, 'format': 'json', 'language': self.language})
        if response.status_code >= 400:
            raise RuntimeError(f'HTTP {response.status_code}（实例可能未开启 json 格式）')
        data = response.json()
        return [
            SearchHit(item.get('title', ''), _clean_url(item.get('url', '')), item.get('content', '') or '', 'searxng')
            for item in (data.get('results') or [])[:limit]
        ]

    async def _tavily(self, client: httpx.AsyncClient, query: str, limit: int) -> List[SearchHit]:
        if not self.api_key:
            return []
        payload = {
            'api_key': self.api_key,
            'query': query,
            'max_results': limit,
            'search_depth': 'advanced',
            'include_answer': True,
        }
        response = await client.post('https://api.tavily.com/search', json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f'HTTP {response.status_code} {response.text[:160]}')
        data = response.json()
        hits = [
            SearchHit(item.get('title', ''), item.get('url', ''), item.get('content', '') or '', 'tavily')
            for item in (data.get('results') or [])[:limit]
        ]
        # Tavily 会额外给一段聚合答案，作为高置信度摘要放在最前面。
        answer = (data.get('answer') or '').strip()
        if answer:
            hits.insert(0, SearchHit('Tavily 聚合摘要', '', answer, 'tavily'))
        return hits

    async def _bocha(self, client: httpx.AsyncClient, query: str, limit: int) -> List[SearchHit]:
        if not self.api_key:
            return []
        # 官方域名是 api.bocha.cn；api.bochaai.com 是旧域名，也保留可用。
        endpoint = self.base_url if 'bocha' in self.base_url else 'https://api.bocha.cn/v1/web-search'
        if not endpoint.endswith('/web-search'):
            endpoint = endpoint.rstrip('/') + '/v1/web-search'
        response = await client.post(
            endpoint,
            headers={'Authorization': 'Bearer ' + self.api_key, 'Content-Type': 'application/json'},
            json={'query': query, 'summary': True, 'count': min(limit, 50), 'freshness': 'noLimit'},
        )
        if response.status_code >= 400:
            raise RuntimeError(f'HTTP {response.status_code} {response.text[:160]}')
        data = response.json()
        pages = (((data.get('data') or {}).get('webPages') or {}).get('value')) or []
        hits = []
        for item in pages[:limit]:
            snippet = item.get('summary') or item.get('snippet') or ''
            hits.append(SearchHit(item.get('name', ''), item.get('url', ''), snippet, 'bocha'))
        return hits

    async def _zhipu(self, client: httpx.AsyncClient, query: str, limit: int) -> List[SearchHit]:
        """智谱 AI 的网络搜索工具 API（中文，国内直连，有免费额度）。"""
        if not self.api_key:
            return []
        response = await client.post(
            'https://open.bigmodel.cn/api/paas/v4/web_search',
            headers={'Authorization': 'Bearer ' + self.api_key, 'Content-Type': 'application/json'},
            json={'search_query': query, 'search_engine': 'search_std', 'count': min(limit, 50)},
        )
        if response.status_code >= 400:
            raise RuntimeError(f'HTTP {response.status_code} {response.text[:160]}')
        data = response.json()
        rows = data.get('search_result') or (data.get('data') or {}).get('search_result') or []
        return [
            SearchHit(
                item.get('title') or item.get('name', ''),
                item.get('link') or item.get('url', ''),
                item.get('content') or item.get('snippet') or item.get('summary', ''),
                'zhipu',
            )
            for item in rows[:limit]
        ]

    async def _serper(self, client: httpx.AsyncClient, query: str, limit: int) -> List[SearchHit]:
        if not self.api_key:
            return []
        response = await client.post(
            'https://google.serper.dev/search',
            headers={'X-API-KEY': self.api_key, 'Content-Type': 'application/json'},
            json={'q': query, 'num': limit, 'gl': 'cn', 'hl': 'zh-cn'},
        )
        if response.status_code >= 400:
            raise RuntimeError(f'HTTP {response.status_code} {response.text[:160]}')
        data = response.json()
        hits = []
        answer = ((data.get('answerBox') or {}).get('answer') or (data.get('answerBox') or {}).get('snippet') or '').strip()
        if answer:
            hits.append(SearchHit('Google 精选摘要', (data.get('answerBox') or {}).get('link', ''), answer, 'serper'))
        for item in (data.get('organic') or [])[:limit]:
            hits.append(SearchHit(item.get('title', ''), item.get('link', ''), item.get('snippet', ''), 'serper'))
        return hits

    async def _brave(self, client: httpx.AsyncClient, query: str, limit: int) -> List[SearchHit]:
        if not self.api_key:
            return []
        response = await client.get(
            'https://api.search.brave.com/res/v1/web/search',
            headers={'X-Subscription-Token': self.api_key, 'Accept': 'application/json'},
            params={'q': query, 'count': min(limit, 20)},
        )
        if response.status_code >= 400:
            raise RuntimeError(f'HTTP {response.status_code} {response.text[:160]}')
        data = response.json()
        results = ((data.get('web') or {}).get('results')) or []
        return [
            SearchHit(html_to_text(item.get('title', '')), item.get('url', ''), html_to_text(item.get('description', '')), 'brave')
            for item in results[:limit]
        ]

    async def _duckduckgo(self, client: httpx.AsyncClient, query: str, limit: int) -> List[SearchHit]:
        """免 key 兜底。lite 版结构简单，html 版作为备选。"""
        hits: List[SearchHit] = []
        try:
            response = await client.post('https://lite.duckduckgo.com/lite/', data={'q': query})
            if response.status_code < 400:
                page = response.text
                links = re.findall(r"<a[^>]+href=\"([^\"]+)\"[^>]*class=['\"]result-link['\"][^>]*>([\s\S]*?)</a>", page, re.I)
                snippets = re.findall(r"class=['\"]result-snippet['\"][^>]*>([\s\S]*?)</td>", page, re.I)
                for index, (url, title) in enumerate(links[:limit]):
                    snippet = snippets[index] if index < len(snippets) else ''
                    hits.append(SearchHit(html_to_text(title), _clean_url(url), html_to_text(snippet), 'duckduckgo'))
        except Exception as exc:
            LOG.info('DuckDuckGo lite 失败：%s', exc)
        if hits:
            return hits
        response = await client.get('https://html.duckduckgo.com/html/', params={'q': query})
        if response.status_code >= 400:
            raise RuntimeError(f'HTTP {response.status_code}')
        page = response.text
        blocks = re.findall(
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>[\s\S]*?class="result__snippet"[^>]*>([\s\S]*?)</a>',
            page,
            re.I,
        )
        return [
            SearchHit(html_to_text(title), _clean_url(url), html_to_text(snippet), 'duckduckgo')
            for url, title, snippet in blocks[:limit]
        ]

    async def _wikipedia(self, client: httpx.AsyncClient, query: str, limit: int) -> List[SearchHit]:
        """最后的兜底：维基百科 API，免 key、结构稳定。"""
        lang = 'zh' if self.language.lower().startswith('zh') else 'en'
        response = await client.get(
            f'https://{lang}.wikipedia.org/w/api.php',
            params={
                'action': 'query',
                'list': 'search',
                'srsearch': query,
                'srlimit': limit,
                'format': 'json',
                'origin': '*',
            },
        )
        if response.status_code >= 400:
            raise RuntimeError(f'HTTP {response.status_code}')
        data = response.json()
        hits = []
        for item in ((data.get('query') or {}).get('search') or [])[:limit]:
            title = item.get('title', '')
            hits.append(
                SearchHit(
                    title,
                    'https://' + lang + '.wikipedia.org/wiki/' + quote_plus(title.replace(' ', '_')),
                    html_to_text(item.get('snippet', '')),
                    'wikipedia',
                )
            )
        return hits

    # ------------------------------------------------------------- 正文抓取
    async def _expand(self, client: httpx.AsyncClient, hits: List[SearchHit], pages: int) -> None:
        """并发抓取前 N 条结果的网页正文，填充 hit.text。"""
        semaphore = asyncio.Semaphore(self.concurrency)
        targets = [hit for hit in hits if hit.url][:pages]

        async def worker(hit: SearchHit) -> None:
            async with semaphore:
                try:
                    hit.text = await self.fetch_text(client, hit.url)
                except Exception as exc:
                    LOG.info('抓取正文失败 %s：%s', hit.url, exc)

        if targets:
            await asyncio.gather(*(worker(hit) for hit in targets))
            LOG.info('正文抓取完成：%d/%d 条成功', sum(1 for hit in targets if hit.text), len(targets))

    async def fetch_text(self, client: httpx.AsyncClient, url: str) -> str:
        """抓单个网页并转成纯文本，带体积上限与类型校验。"""
        response = await client.get(url, headers={'User-Agent': DEFAULT_UA})
        if response.status_code >= 400:
            return ''
        ctype = response.headers.get('content-type', '')
        if ctype and not any(kind in ctype.lower() for kind in ('html', 'text', 'xml')):
            return ''
        if len(response.content) > self.max_bytes:
            return ''
        page = _decode(response)
        body = _strip_boilerplate(html_to_text(page))
        summary = _meta_description(page)
        text = (summary + '\n' + body) if summary else body
        return text[: self.page_chars * 4]

    # --------------------------------------------------------------- 输出层
    def format_for_prompt(self, hits: List[SearchHit], max_chars: int = 12000) -> str:
        """把结果拼成带引用编号的文本，让模型能写出可追溯的 source。"""
        if not hits:
            return ''
        lines: List[str] = []
        used = 0
        for index, hit in enumerate(hits, 1):
            body = hit.text or hit.snippet
            body = re.sub(r'\s+', ' ', body).strip()[: self.page_chars]
            block = f'[{index}] {hit.title}'
            if hit.url:
                block += f'\n来源：{hit.url}'
            if body:
                block += f'\n内容：{body}'
            if used + len(block) > max_chars:
                break
            lines.append(block)
            used += len(block)
        return '\n\n'.join(lines)

    def sources(self, hits: List[SearchHit]) -> List[str]:
        seen: List[str] = []
        for hit in hits:
            url = hit.url or hit.backend
            if url and url not in seen:
                seen.append(url)
        return seen

    async def diagnose(self, query: str = '李花子') -> str:
        """给配置界面的「测试联网检索」用：返回一段人话报告。"""
        if self.backend == 'off':
            return '联网已关闭（后端 = off）。'
        if not query.strip():
            return '请输入测试关键词。'
        hits = await self.search(query, limit=5, expand=0)
        plan = ' → '.join(self._plan())
        if not hits:
            return (
                f'检索失败：{plan} 全部没有返回结果。\n'
                '常见原因：网络/代理不通、后端需要 API Key 但未填写、'
                'SearXNG 地址不提供 JSON 格式、或对方限流。可直接换成 tavily/bocha 这类搜索 API。'
            )
        head = f'检索成功：后端 {self.last_backend}，命中 {len(hits)} 条。\n尝试顺序：{plan}\n\n'
        preview = '\n'.join(f'· {hit.title} — {hit.url}' for hit in hits[:5])
        return head + preview


def needs_key(backend: str) -> bool:
    return (backend or '').lower() in ('tavily', 'bocha', 'zhipu', 'serper', 'brave')
