import asyncio
import json
import logging
from abc import ABC, abstractmethod

# provider 出错时不是抛异常，而是**返回一句以这个前缀开头的话**（聊天链路靠它把原因显示给用户）。
# 谁要把返回值当正文用，都必须先过 is_error()——否则会把「暂时无法连接…」当成角色台词念出来。
ERROR_PREFIX = '暂时无法连接'


def is_error(text) -> bool:
    return str(text or '').strip().startswith(ERROR_PREFIX)


class LLMProvider(ABC):
    def __init__(self, config=None): self.config = config
    @abstractmethod
    async def chat(self, messages): ...

class EchoProvider(LLMProvider):
    async def chat(self, messages):
        await asyncio.sleep(0)
        user = next((m['content'] for m in reversed(messages) if m['role']=='user'), '')
        return f"我收到啦：{user}"

class OpenAIProvider(LLMProvider):
    async def chat(self, messages):
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=self.config.get('api_key'))
            if self.config.get('use_web_search'):
                response = await client.responses.create(model=self.config.get('model','gpt-4o-mini'), input=messages, tools=[{'type':'web_search_preview'}], max_output_tokens=int(self.config.get('max_tokens',120)))
                return response.output_text
            params={'model':self.config.get('model','gpt-4o-mini'),'messages':messages,'max_tokens':int(self.config.get('max_tokens',120)),'temperature':float(self.config.get('temperature',0.7)),'top_p':float(self.config.get('top_p',1.0)),'frequency_penalty':float(self.config.get('frequency_penalty',0.0)),'presence_penalty':float(self.config.get('presence_penalty',0.0))}
            if self.config.get('stop'): params['stop']=self.config['stop']
            r = await client.chat.completions.create(**params)
            return r.choices[0].message.content
        except Exception as e: return f"暂时无法连接 OpenAI：{e}"

class CompatibleProvider(LLMProvider):
    async def chat(self, messages):
        import httpx
        base=(self.config.get('base_url') or self.config.get('url') or '').rstrip('/')
        if not base.endswith('/v1'): base += '/v1'
        headers={'Authorization':'Bearer '+self.config.get('api_key',''),'Content-Type':'application/json'}
        payload={'model':self.config.get('model',''),'messages':messages,'max_tokens':int(self.config.get('max_tokens',120)),'temperature':float(self.config.get('temperature',0.7)),'top_p':float(self.config.get('top_p',1.0)),'frequency_penalty':float(self.config.get('frequency_penalty',0.0)),'presence_penalty':float(self.config.get('presence_penalty',0.0))}
        stop=self.config.get('stop',[])
        if stop: payload['stop']=stop
        stream=bool(self.config.get('stream',False))
        timeout=float(self.config.get('timeout',120) or 120)
        if self.config.get('use_web_search'):
            # 中继只有在 Responses API + tools[type=web_search] 下才会真正联网：
            # 旧工具名 web_search_preview 会被拒绝，chat/completions 也不支持联网。
            return await self._responses_web_search(base, headers, messages, timeout)
        # 中继接口首次请求经常要冷启动（实测 >30s），失败一次就重试，避免白跑一轮联网检索。
        # 5xx / 429 也要重试：中继的 nginx 会偶发 504（实测出现过整条回复变成
        # 「HTTP 504 Gateway Time-out」被当成台词念出来的情况），多试一次通常就过了。
        log=logging.getLogger('llm')
        last=None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    if stream:
                        text=await self._stream_chat(client,base,headers,payload)
                        if text: return text
                        # 推理模型的流式增量大多落在 reasoning_content 上，正文可能整段为空；
                        # 这时退回非流式再取一次完整消息，别直接把这一轮判死。
                        log.info('流式没有返回正文，改用非流式重试')
                    response=await client.post(base+'/chat/completions',headers=headers,json=payload)
                if response.status_code >= 400:
                    if response.status_code in (408, 425, 429) or response.status_code >= 500:
                        last=RuntimeError(f'HTTP {response.status_code} {response.text[:200]}')
                        log.warning('模型接口返回 %s，第 %d 次重试', response.status_code, attempt+1)
                        if attempt < 2: await asyncio.sleep(1.5 * (attempt + 1)); continue
                        return f"暂时无法连接第三方 API：HTTP {response.status_code} {response.text[:500]}（已重试 3 次）"
                    return f"暂时无法连接第三方 API：HTTP {response.status_code} {response.text[:500]}"
                data=response.json(); choices=data.get('choices') or []
                if not choices: return f"暂时无法连接第三方 API：响应缺少 choices，原始返回：{response.text[:500]}"
                message=choices[0].get('message',{}) or {}
                content=message.get('content') or ''
                if not content and message.get('reasoning_content'):
                    # deepseek-flash 这类推理模型会把 max_tokens 全花在思维链上，
                    # 正文就成了空字符串。不点明的话只看到「返回为空」，极难排查。
                    return (f"暂时无法连接第三方 API：模型只输出了思维链、正文为空——"
                            f"思维链已耗尽 max_tokens({payload['max_tokens']})，请把「最大 token」调大（建议 8000）")
                return content
            except Exception as e:
                # 附带异常类型：httpx 的超时异常 str() 常常是空字符串，只打 e 会看不到原因。
                last=e
                log.warning('模型请求异常（第 %d 次）：%s: %r', attempt+1, type(e).__name__, e)
                if attempt < 2: await asyncio.sleep(1.5 * (attempt + 1))
        return f"暂时无法连接第三方 API：{type(last).__name__}: {last!r}（已重试 3 次）"

    async def _stream_chat(self, client, base, headers, payload):
        """流式请求：长提示词 + 大 max_tokens 时，nginx 网关会给非流式请求回 504；
        流式只要持续有数据就不会被判超时，是知识库抽取能稳定跑完的关键。"""
        body=dict(payload); body['stream']=True
        chunks=[]
        async with client.stream('POST', base+'/chat/completions', headers=headers, json=body) as response:
            if response.status_code >= 400:
                detail=(await response.aread()).decode('utf-8','replace')[:500]
                raise RuntimeError(f'HTTP {response.status_code} {detail}')
            async for line in response.aiter_lines():
                if not line.startswith('data:'): continue
                data=line[5:].strip()
                if not data or data=='[DONE]': continue
                try: delta=json.loads(data)['choices'][0].get('delta',{}).get('content')
                except Exception: continue
                if delta: chunks.append(delta)
        return ''.join(chunks)

    # ------------------------------------------------------------------
    # Responses API 联网检索
    # ------------------------------------------------------------------
    async def _responses_web_search(self, base, headers, messages, timeout):
        """用 /v1/responses + tools[type=web_search] 让模型自己联网搜索。

        **接口收下这个工具不等于真的会去搜。** 实测 DeepSeek：
          · deepseek-v4-pro —— 产生 web_search_call，真联网（可用）
          · deepseek-flash   —— 一次搜索都不发，却凭训练数据编出「来源链接」（危险）
        所以这里必须核对搜索调用次数；为 0 就判定「没联网」，宁可退回本地检索，
        也不能把编造的来源写进知识库。
        """
        import httpx
        model=self.config.get('model','')
        payload={'model':model,'input':messages,'tools':[{'type':'web_search'}],'max_output_tokens':int(self.config.get('max_tokens',500))}
        last=None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    text, searches = '', 0
                    if self.config.get('stream',True):
                        try:
                            text, searches = await self._responses_stream(client,base,headers,payload)
                        except Exception as exc:
                            last=exc
                            logging.getLogger('llm').info('Responses 流式不可用，改用非流式：%s', exc)
                    if not text:
                        text, searches = await self._responses_once(client,base,headers,payload)
                    if not text:
                        return (f"暂时无法连接第三方 API：Responses 返回为空（模型 {model} 可能不支持联网接口；"
                                f"推理模型还会被思维链吃光 token，可把「最大 token」调到 8000）")
                    if searches == 0:
                        return (f"暂时无法连接第三方 API：接口接受了联网工具但没有真正执行检索（模型 {model} "
                                f"未产生任何搜索调用，这种回答来自训练数据、来源不可信），已改用本地检索结果")
                    logging.getLogger('llm').info('API 自身联网完成：模型 %s 实际搜索 %d 次', model, searches)
                    return text
            except Exception as e:
                last=e
                if attempt == 0: await asyncio.sleep(1.5)
        return f"暂时无法连接第三方 API：{type(last).__name__}: {last!r}"

    async def _responses_once(self, client, base, headers, payload):
        response=await client.post(base+'/responses', headers=headers, json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f'HTTP {response.status_code} {response.text[:300]}')
        data=response.json()
        searches=len([item for item in (data.get('output') or []) if item.get('type')=='web_search_call'])
        return self._responses_text(data), searches

    @staticmethod
    def _responses_text(data):
        """取最后一条 message 的正文：前面往往还有一句「我先去搜一下」的过场话。"""
        text=data.get('output_text')
        if text: return text
        messages=[item for item in (data.get('output') or []) if item.get('type')=='message']
        if not messages: return ''
        parts=[]
        for part in messages[-1].get('content') or []:
            if part.get('type')=='output_text' and part.get('text'): parts.append(part['text'])
        return ''.join(parts)

    async def _responses_stream(self, client, base, headers, payload):
        """SSE 流式；顺带收集来源链接，并统计真实的搜索调用次数。"""
        body=dict(payload); body['stream']=True
        chunks=[]; sources=[]; searches=0; saw_search=False
        async with client.stream('POST', base+'/responses', headers=headers, json=body) as response:
            if response.status_code >= 400:
                detail=(await response.aread()).decode('utf-8','replace')[:300]
                raise RuntimeError(f'HTTP {response.status_code} {detail}')
            async for line in response.aiter_lines():
                if not line.startswith('data:'): continue
                raw=line[5:].strip()
                if not raw or raw=='[DONE]': continue
                try: event=json.loads(raw)
                except Exception: continue
                kind=event.get('type') or ''
                if kind=='response.output_text.delta':
                    delta=event.get('delta')
                    if delta: chunks.append(delta)
                elif kind=='response.output_text.annotation.added':
                    url=(event.get('annotation') or {}).get('url')
                    if url and url not in sources: sources.append(url)
                elif kind.startswith('response.web_search_call'):
                    saw_search=True
                    if kind.endswith('.completed'): searches+=1
                elif kind in ('response.failed','error'):
                    raise RuntimeError(str(event)[:200])
        text=''.join(chunks)
        if text and sources:
            text += '\n\n检索来源：' + ' '.join(sources[:10])
        return text, (searches or (1 if saw_search else 0))

class OllamaProvider(LLMProvider):
    async def chat(self, messages):
        try:
            import httpx
            async with httpx.AsyncClient() as c:
                options={'num_predict':int(self.config.get('max_tokens',120)), 'stop':self.config.get('stop',['（','(', '【思考','首先','其次'])}
                r = await c.post(self.config.get('url','http://localhost:11434/api/chat'), json={'model':self.config.get('model','llama3'),'messages':messages,'stream':False,'options':options})
                return r.json()['message']['content']
        except Exception as e: return f"暂时无法连接 Ollama：{e}"

def provider_from_config(config):
    name = config.get('provider','echo') if config else 'echo'
    return {'openai':OpenAIProvider,'ollama':OllamaProvider,'compatible':CompatibleProvider,'third_party':CompatibleProvider}.get(name, EchoProvider)(config or {})
