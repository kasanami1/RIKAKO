from pathlib import Path
import json
import random
import re
from PySide6.QtGui import QImage


DIRECTIONS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


class SpriteSheet:
    def __init__(self, root: Path):
        self.root = root
        self.action = "idle"
        self.expression = ""
        self.direction = 0
        self.frame = 0
        self.order = "forward"
        self._frames_cache = {}
        self._durations_cache = {}

    @property
    def actions(self):
        folder = self.root / "motions"
        names = sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.exists() else []
        return names or ["idle"]

    @property
    def expressions(self):
        folder = self.root / "expressions"
        return sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.exists() else []

    # ------------------------------------------------------------------ 有方向 / 无方向
    @staticmethod
    def _flat_frames(base):
        """直接放在动作/表情目录下的 PNG（无方向素材）。"""
        return sorted(base.glob("*.png")) if base.exists() else []

    def motion_kind(self, name=None):
        """这个动作是「无方向的整段」还是「按八方向分的」。

        * `flat`  —— 目录下直接是 PNG（待机动作就该这样，本来没有朝向可言）
        * `directional` —— 有 N/E/S… 子目录且有图（鼠标追踪那套）
        * `empty` —— 什么都没有
        """
        base = self.root / "motions" / (name or self.action)
        if self._flat_frames(base): return "flat"
        if self._nearest_populated(base): return "directional"
        return "empty"

    @property
    def flat_motions(self):
        """无方向动作（适合当待机）。"""
        return [n for n in self.actions if self.motion_kind(n) == "flat"]

    @property
    def directional_motions(self):
        """有八方向的动作（适合当鼠标追踪）。"""
        return [n for n in self.actions if self.motion_kind(n) == "directional"]

    def is_flat(self, name=None):
        return self.motion_kind(name) == "flat"

    def is_current_flat(self):
        """当前**实际显示**的这一套帧是不是无方向的。

        表情优先：设了表情就看表情那套（表情是无方向的，那方向选择同样没意义），
        没设表情才看动作。
        """
        if self.expression:
            base = self.root / "expressions" / self.expression
            if self._flat_frames(base): return True
            if self._nearest_populated(base): return False
        return self.is_flat(self.action)

    def set_action(self, name):
        self.action, self.frame = name, 0

    def set_expression(self, name=""):
        self.expression, self.frame = name, 0

    def set_direction(self, index):
        # 转向**不改动动画相位**：朝向和眨眼是两件独立的事。
        # 这里以前会把帧号归零，后果是鼠标一动就跳回睁眼帧；配合 PetWindow 里
        # 「转向后重开计时器」，鼠标持续移动时那台计时器被反复重启，动画永远走不到
        # 下一次触发——表现就是角色冻住、又卡又迟钝（帧时长越长越明显）。
        self.direction = index % 8

    def set_order(self, order):
        self.order, self.frame = order, 0

    def frames(self):
        """当前显示的帧列表（带缓存）。

        这个函数每次都要扫目录；而它被 paintEvent、current_duration()、advance()
        反复调用，视线追踪一快（20ms 一次）就变成每秒几十次目录扫描。改用目录的
        修改时间来判定失效，既便宜又不会在换素材后读到旧列表。

        **缓存键必须包含「实际会扫的那个目录」**：无方向动作的帧在动作根目录，
        以前却拿「动作/方向」子目录的 mtime 当失效依据 —— 那个目录根本不存在，
        mtime 恒为 0，于是缓存永不失效：导入/改名/重排/删除帧之后，仍然返回旧路径。
        表现就是「拖一下顺序，角色和缩略图全变成占位图」（旧路径的文件已经不存在了）。
        """
        direction = DIRECTIONS[self.direction]
        base = (self.root / "expressions" / self.expression) if self.expression \
            else (self.root / "motions" / self.action)
        flat = bool(self._flat_frames(base))
        key = (str(base), direction, bool(self.expression), flat)
        stamp = self._folder_stamp(base, None if flat else base / direction)
        cached = self._frames_cache.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        frames = self._scan_frames()
        self._frames_cache[key] = (stamp, frames)
        return frames

    @staticmethod
    def _folder_stamp(*folders):
        """把若干目录的修改时间拼成缓存戳（不存在的目录记 0）。"""
        stamps = []
        for folder in folders:
            if folder is None:
                stamps.append(0); continue
            try:
                stamps.append(folder.stat().st_mtime_ns)
            except OSError:
                stamps.append(0)
        return tuple(stamps)

    def invalidate_frames(self):
        """外部改动了素材（导入/重排/改名/删除）后主动清缓存。"""
        self._frames_cache.clear()

    def _scan_frames(self):
        """取帧顺序：表情（无方向整段 → 本方向 → 同表情最近方向） > 动作（同左）。

        「无方向」指的是目录下直接放 PNG、不分子目录——待机这类动作本来就没有朝向，
        硬要画八向是浪费。有方向的素材照旧优先用当前方向。
        """
        if self.expression:
            base = self.root / "expressions" / self.expression
            frames = self._flat_frames(base)
            if frames:
                return frames
            folder = base / DIRECTIONS[self.direction]
            frames = sorted(folder.glob("*.png")) if folder.exists() else []
            if frames:
                return frames
            nearest = self._nearest_populated(base)
            if nearest:
                return nearest
        base = self.root / "motions" / self.action
        frames = self._flat_frames(base)
        if frames:
            return frames
        folder = base / DIRECTIONS[self.direction]
        frames = sorted(folder.glob("*.png")) if folder.exists() else []
        if frames:
            return frames
        # Assets are often imported for only a subset of the eight directions.
        # Fall back to the nearest populated direction so auto-tracking never
        # makes the desktop pet disappear when the cursor points at an empty
        # folder.
        return self._nearest_populated(base) or []

    def _nearest_populated(self, parent):
        """在 parent/<方向>/ 里找离当前朝向最近的那一组帧（没有就返回空）。"""
        if not parent.exists():
            return []
        populated = []
        for index, direction in enumerate(DIRECTIONS):
            candidate = parent / direction
            files = sorted(candidate.glob("*.png")) if candidate.exists() else []
            if files:
                distance = min((index - self.direction) % 8, (self.direction - index) % 8)
                populated.append((distance, files))
        return min(populated, key=lambda item: item[0])[1] if populated else []

    def _duration_file(self):
        """时长文件：无方向动作放在动作根目录，有方向的放在方向目录里。"""
        if self.expression:
            base = self.root / "expressions" / self.expression
            if self._flat_frames(base): return base / "durations.json"
            return base / DIRECTIONS[self.direction] / "durations.json"
        base = self.root / "motions" / self.action
        if self._flat_frames(base): return base / "durations.json"
        return base / DIRECTIONS[self.direction] / "durations.json"

    @staticmethod
    def _normalize_duration(value):
        """把 durations.json 里的值规范成 int（固定时长）或 [下限, 上限]（每轮随机取）。"""
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            try:
                low, high = int(value[0]), int(value[1])
            except (TypeError, ValueError):
                return None
            if low > high: low, high = high, low
            low = max(20, min(10000, low)); high = max(20, min(10000, high))
            return [low, high] if high > low else low
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return max(20, min(10000, number)) if number > 0 else None

    def durations(self):
        """{文件名: 时长}。时长是毫秒数，或 [下限, 上限] 区间（每轮循环随机取值）。

        区间让眨眼间隔不再固定：一直以完全相同的节奏眨眼，看久了立刻会觉得是机械循环。
        带缓存：这个文件原先每次取时长都要读一遍盘，而视线追踪每秒会问几十次。
        用「文件修改时间 + 大小」当缓存键，改了时长会立刻生效。
        """
        path = self._duration_file()
        try:
            info = path.stat(); stamp = (info.st_mtime_ns, info.st_size)
        except OSError:
            return {}
        cached = self._durations_cache.get(str(path))
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            return {}
        result = {}
        for name, value in data.items() if isinstance(data, dict) else []:
            normalized = self._normalize_duration(value)
            if normalized is not None: result[str(name)] = normalized
        self._durations_cache[str(path)] = (stamp, result)
        return result

    def duration_text(self, filename):
        """给界面显示/编辑用的文本：固定时长是 '1600'，区间是 '1000-1600'。"""
        value = self.durations().get(filename)
        if value is None: return ""
        return "%d-%d" % (value[0], value[1]) if isinstance(value, list) else str(value)

    def set_frame_duration(self, filename, milliseconds):
        """milliseconds 支持 '1600'、'1000-1600'、'1000~1600'，空字符串表示清除。"""
        path = self._duration_file(); data = self.durations()
        text = "" if milliseconds is None else str(milliseconds).strip()
        if not text:
            data.pop(filename, None)
        else:
            numbers = []
            for part in [p for p in re.split(r"[-~,，\s]+", text) if p]:
                try: numbers.append(int(part))
                except ValueError: numbers = []; break
            if not numbers: return
            value = self._normalize_duration(numbers[:2] if len(numbers) >= 2 else numbers[0])
            if value is None: return
            data[filename] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def blink_cycle_max(self, default=125):
        """一次完整动画循环的时长上限（每帧取各自的上限，再求和）。

        用来决定「鼠标停稳多久之后才开始眨眼」。带区间的帧取上限，所以这个值是
        最坏情况下一次循环要花的时间——用它当等待门槛，就不会出现「刚眨到一半
        鼠标又动了」的打断。
        """
        total = 0
        for path in self.frames():
            value = self.durations().get(path.name)
            if value is None: total += default
            elif isinstance(value, list): total += value[1]
            else: total += value
        return total or default

    def current_duration(self, default):
        frames = self.frames()
        if not frames: return default
        value = self.durations().get(frames[self.frame % len(frames)].name)
        if value is None: return default
        # 区间：每次轮到这一帧时重新抽一次，所以每个循环的停留时长都不同。
        return random.randint(value[0], value[1]) if isinstance(value, list) else value

    def advance(self):
        count = len(self.frames())
        if count < 2:
            return
        if self.order == "random":
            self.frame = random.randrange(count)
        elif self.order == "reverse":
            self.frame = (self.frame - 1) % count
        else:
            self.frame = (self.frame + 1) % count

    def image(self):
        frames = self.frames()
        return QImage(str(frames[self.frame % len(frames)])) if frames else QImage()
