import json, sqlite3
from pathlib import Path
from repositories import MessageRepository, MemoryRepository, SummaryRepository

class MemoryManager:
    def __init__(self, db): self.repo=MemoryRepository(db)
    def retrieve(self, text): return self.repo.search(text)
    async def extract(self, user_text, reply, provider):
        try:
            prompt=Path(__file__).parent/'prompts'/'memory_extractor.txt'; result=await provider.chat([{'role':'system','content':prompt.read_text(encoding='utf-8')},{'role':'user','content':user_text+'\n'+reply}])
            data=json.loads(result)
            for item in data if isinstance(data,list) else []:
                if item.get('content') and item.get('importance',0)>=2 and not self.repo.search(item['content']): self.repo.add(item['content'],item['importance'])
        except Exception: pass

class ContextManager:
    def __init__(self, db, max_messages=20): self.messages=MessageRepository(db); self.summaries=SummaryRepository(db); self.max_messages=max_messages
    def build(self, system, memories):
        out=[{'role':'system','content':system}]; summary=self.summaries.latest()
        if summary: out.append({'role':'system','content':'历史摘要：'+summary})
        if memories: out.append({'role':'system','content':'相关记忆：'+'；'.join(x['content'] for x in memories)})
        out += self.messages.recent(10); return out
    async def compress(self, provider):
        rows=self.messages.recent(30)
        if len(rows)<=self.max_messages: return
        prompt=Path(__file__).parent/'prompts'/'summarizer.txt'; text=await provider.chat([{'role':'system','content':prompt.read_text(encoding='utf-8')},{'role':'user','content':str(rows)}]); self.summaries.add(text)
