from pathlib import Path

class AssetRegistry:
    def __init__(self, assets_root):
        self.root = Path(assets_root)
    def get_character_list(self):
        characters = self.root / 'characters'
        if characters.exists():
            names = sorted(p.name for p in characters.iterdir() if p.is_dir())
            if names: return names
        return ['default']
    def character_root(self, name):
        candidate = self.root / 'characters' / name
        return candidate if candidate.exists() else self.root
