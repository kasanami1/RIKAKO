import json
import os
from pathlib import Path

class ConfigManager:
    def __init__(self, path=None):
        self.path = Path(path or 'config.json'); self.data = {}
        if self.path.exists():
            try: self.data = json.loads(self.path.read_text(encoding='utf-8'))
            except Exception: self.data = {}
        # Environment variables take precedence and are never written back.
        if os.getenv('LILYZ_API_KEY'): self.data['api_key'] = os.getenv('LILYZ_API_KEY')
        if os.getenv('LILYZ_BASE_URL'): self.data['base_url'] = os.getenv('LILYZ_BASE_URL')
    def get(self, key, default=None): return self.data.get(key, default)
    def update(self, values):
        self.data.update(values)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')
