import sqlite3
from datetime import datetime

class BaseRepository:
    def __init__(self, db): self.db=db; self.db.row_factory=sqlite3.Row
    def execute(self, sql, args=()):
        cur=self.db.execute(sql,args); self.db.commit(); return cur

class MessageRepository(BaseRepository):
    def add(self, role, content): self.execute('INSERT INTO messages(role,content,created_at) VALUES(?,?,?)',(role,content,datetime.utcnow().isoformat()))
    def recent(self, n=10): return [dict(r) for r in self.db.execute('SELECT role,content FROM messages ORDER BY id DESC LIMIT ?', (n,)).fetchall()][::-1]
    def delete_before(self, id_): self.execute('DELETE FROM messages WHERE id<?',(id_,))

class MemoryRepository(BaseRepository):
    def add(self, content, importance=1): self.execute('INSERT INTO memories(content,importance,created_at) VALUES(?,?,?)',(content,importance,datetime.utcnow().isoformat()))
    def search(self, text, limit=5): return [dict(r) for r in self.db.execute('SELECT content,importance FROM memories WHERE content LIKE ? ORDER BY importance DESC LIMIT ?',('%'+text[:30]+'%',limit))]
    def all(self): return [dict(r) for r in self.db.execute('SELECT * FROM memories ORDER BY id DESC')]

class SummaryRepository(BaseRepository):
    def add(self, content): self.execute('INSERT INTO conversation_summaries(content,created_at) VALUES(?,?)',(content,datetime.utcnow().isoformat()))
    def latest(self):
        r=self.db.execute('SELECT content FROM conversation_summaries ORDER BY id DESC LIMIT 1').fetchone(); return r['content'] if r else ''
