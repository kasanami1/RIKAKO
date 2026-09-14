import math
from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QImage, QPainter, QPen, QAction
from PySide6.QtWidgets import QMenu, QApplication
from PySide6.QtWidgets import QWidget
from sprite_sheet import DIRECTIONS


class PetWindow(QWidget):
    directionChanged = Signal(int)
    moved = Signal()
    bubbleRequested = Signal(str)
    # 桌宠大小被改了（右键菜单 / Ctrl+滚轮 / 工作台滑块都会发），主程序拿它写回 config
    scaleChanged = Signal(float)
    # 视线追踪用独立的高频定时器，不跟动画帧共用。
    # 之前方向只在动画 tick 里更新，而每帧时长最长 1600ms，于是动一下鼠标要等
    # 一秒多才转头，跟手感很差。这个值可由工作台的「视线灵敏度」滑块调整。
    DEFAULT_TRACK_INTERVAL = 50
    # 换向死区（度）：离开当前朝向中心 22.5°+这个值才切换。
    # 不给死区，光标停在分界线上会反复横跳；给太大，转头又会显得迟钝。
    TRACK_HYSTERESIS_DEG = 8
    # 光标离角色中心近于这个距离时不做判定：那点位移对应巨大角度变化，纯属噪声。
    TRACK_MIN_DISTANCE = 24
    # 光标位移超过这么多像素才算「在动」。设一点阈值是为了滤掉鼠标自身的微小抖动。
    TRACK_STILL_THRESHOLD = 3
    # 跨多个方向时，中间每一格停留多久（毫秒）。
    # 鼠标快速划过时角度会一下跨过好几个扇区；直接跳过去就是从「右下」整幅切到
    # 「右上」，视觉上是个突兀的跳变。逐格走过去看起来才是连续转身。
    TRACK_STEP_MS = 60
    # 鼠标停稳多久才恢复眨眼 = 一次完整眨眼动画时长 × 这个比例。
    #   1.0 = 一次眨眼绝不会被打断，但停手后要等满一个周期（很迟钝）
    #   0.5 = 恢复快一倍，偶尔会眨到一半被鼠标打断
    # 想更灵敏就往小调，想更稳就往大调。
    SETTLE_RATIO = 0.5
    # 再快也不低于这个值：否则鼠标稍一停顿就触发眨眼，看起来会很神经质。
    SETTLE_MIN_MS = 300
    # ---- 桌宠大小（缩放）----
    # 1.0 = 历史上的 256px 窗口。上限给到 3 倍（768px）：源素材是 1254px 的图，放大到
    # 3 倍仍然比源图小，所以不会糊；真正会糊的是源帧本身很小的素材。
    BASE_SIZE = 256
    MIN_SCALE = 0.4
    MAX_SCALE = 3.0
    WHEEL_STEP = 0.05        # Ctrl+滚轮每格
    MENU_STEP = 0.1          # 菜单里的「放大/缩小」每步
    def __init__(self, sheet):
        super().__init__()
        self.sheet = sheet
        self.auto_direction = True
        self._bubble = ""
        self._talking = False
        # 表情：手动选的（常态）与接口临时指定的（会自动收回）
        self._manual_expression = ""
        self._auto_expression = ""
        self._expression_timer = None
        self.expression_hold_seconds = 5.0
        self.crisp_rendering = False
        self._base_interval = 125
        self.track_interval = self.DEFAULT_TRACK_INTERVAL
        self._frame_cache = {}
        self._last_cursor = None
        self._settle_key = None
        self._settle_ms = 0
        self._playing = True
        self._warm_queue = []
        self._warm_timer = None
        self.drag_offset = None
        # 桌宠大小：默认 1.0（= BASE_SIZE）。主程序会从 config 里读 pet_scale 覆盖它。
        self.scale = 1.0
        self.setFixedSize(self.BASE_SIZE, self.BASE_SIZE)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        # 用第 0 帧自己的时长起表：否则首帧按默认间隔显示，一开机就提前眨眼。
        self.timer.start(self.sheet.current_duration(self._base_interval))
        self.track_timer = QTimer(self)
        self.track_timer.timeout.connect(self.track_direction)
        self.track_timer.start(self.track_interval)
        # 鼠标停稳计时：只有它超时（超过一次完整眨眼动画的时长）才恢复眨眼。
        self.settle_timer = QTimer(self)
        self.settle_timer.setSingleShot(True)
        self.settle_timer.timeout.connect(self._settled)
        # 转向逐格过渡：跨多格时把中间方向也走一遍，避免整幅硬跳。
        self._target_direction = self.sheet.direction
        self._step_timer = QTimer(self)
        self._step_timer.timeout.connect(self._step_towards_target)
        # ---- 待机动作（无八方向的那类）----
        # 鼠标一直不动就切到「待机动作」轮流播；鼠标一动立刻回到八方向的追踪动作。
        self.tracking_motion = "idle"
        self.idle_motions = []
        self.idle_after_seconds = 20.0
        self.idle_motion_seconds = 10.0
        self._idle_mode = False
        self._last_idle = None
        self._still_timer = QTimer(self)
        self._still_timer.setSingleShot(True)
        self._still_timer.timeout.connect(self._enter_idle_mode)
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._next_idle_motion)
        self.refresh_idle_plan()

    def set_track_interval(self, milliseconds):
        """视线灵敏度：数值越小越跟手（由工作台的滑块调用）。"""
        self.track_interval = max(20, min(2000, int(milliseconds)))
        if self.track_timer.isActive(): self.track_timer.setInterval(self.track_interval)

    # ------------------------------------------------------------------ 桌宠大小
    def set_scale(self, value, persist=True):
        """改桌宠大小。1.0 = 原来的 256px，范围 0.4~3.0。

        缩放的锚点是**底边中心**（脚），不是窗口中心：放大时角色像"从地面长高"，
        而不是从中心往四周鼓——后者在桌面上看起来会整只往上飘。改完还会把窗口夹回
        屏幕工作区内，免得放到 2 倍时半只跑出屏幕。

        `persist=False` 用于「拖滑块时连续预览」：界面实时跟着变，但只在松手时才写配置。
        """
        scale = max(self.MIN_SCALE, min(self.MAX_SCALE, float(value)))
        if abs(scale - self.scale) < 1e-4:
            return False
        before = self.frameGeometry()
        self.scale = scale
        size = max(64, int(round(self.BASE_SIZE * scale)))
        self.setFixedSize(size, size)
        # 底边中心不动
        left = before.center().x() - size // 2
        top = before.bottom() - size + 1
        area = self._available_area()
        if area is not None:
            left = max(area.left(), min(left, area.right() - size + 1))
            top = max(area.top(), min(top, area.bottom() - size + 1))
        self.move(left, top)
        # 缓存是按 (路径, 尺寸) 存的，尺寸一变自然失效，不用手动清。
        self.update()
        import logging
        logging.getLogger('pet').info('桌宠大小改为 %d%%（窗口 %d×%d）', round(scale * 100), size, size)
        if persist: self.scaleChanged.emit(scale)
        return True

    def _available_area(self):
        try:
            screen = self.screen() or QApplication.primaryScreen()
            return screen.availableGeometry() if screen else None
        except Exception:
            return None

    def scale_up(self, step=None):
        return self.set_scale(self.scale + (self.MENU_STEP if step is None else step))

    def scale_down(self, step=None):
        return self.set_scale(self.scale - (self.MENU_STEP if step is None else step))

    def wheelEvent(self, event):
        """Ctrl + 滚轮 = 缩放。**必须按 Ctrl**：桌宠是无边框置顶窗口，
        不设条件的话鼠标停在它身上滚页面就会被它吃掉，很烦。"""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta: self.set_scale(self.scale + (self.WHEEL_STEP if delta > 0 else -self.WHEEL_STEP))
            event.accept()
            return
        super().wheelEvent(event)

    # ------------------------------------------------------------------ 待机动作
    def refresh_idle_plan(self):
        """按素材与实际配置，决定「追踪用哪套八方向」和「待机轮播哪些无方向动作」。

        自动判定规则（都可由配置覆盖）：
        * 追踪动作：配置里指定且确实有八方向 → 用它；否则取第一个有八方向的动作
        * 待机动作：配置列表里确实存在的 → 用它们；否则自动收集**所有无八方向的动作**
        没有无方向素材时，整套待机逻辑自己失效（行为跟以前完全一样）。
        """
        import logging
        log = logging.getLogger('pet')
        directional = self.sheet.directional_motions
        flat = self.sheet.flat_motions
        if self.tracking_motion not in directional:
            # 默认必须是 idle：它在旧版本里就是默认动作（带眨眼的那套）。
            # 直接取 directional[0] 会按字母序选中「1」，等于悄悄把角色的默认样子换了。
            if 'idle' in directional: self.tracking_motion = 'idle'
            elif directional: self.tracking_motion = directional[0]
            else: self.tracking_motion = self.sheet.actions[0] if self.sheet.actions else 'idle'
        wanted = [n for n in (self.idle_motions or []) if n in flat]
        self._idle_pool = wanted or flat
        log.info('动作规划：追踪=%s（八方向 %s）｜待机=%s（无方向素材 %s）',
                 self.tracking_motion, directional or '无', self._idle_pool or '无', flat or '无')
        return self._idle_pool

    def _enter_idle_mode(self):
        """鼠标停久了：切到无方向的待机动作，并开始随机轮播。"""
        import logging
        if not self._playing or not self._idle_pool:
            logging.getLogger('pet').info('该进待机了，但没进：playing=%s 待机池=%s'
                                          % (self._playing, self._idle_pool or '空'))
            return
        self._idle_mode = True
        logging.getLogger('pet').info('鼠标静止够了，进入待机动作（池：%s）', '、'.join(self._idle_pool))
        self._next_idle_motion(first=True)

    def _next_idle_motion(self, first=False):
        import logging, random
        if not self._idle_mode or not self._idle_pool: return
        pool = self._idle_pool
        if len(pool) > 1:
            choices = [n for n in pool if n != self._last_idle] or pool
        else:
            choices = pool
        name = random.choice(choices)
        logging.getLogger('pet').info('待机动作 %s：%s', '开始' if first else '换到', name)
        self._last_idle = name
        self.sheet.set_action(name)
        self.sheet.frame = 0
        self.refresh_interval(); self.update()
        seconds = max(1.0, float(self.idle_motion_seconds or 10))
        self._idle_timer.start(int(seconds * 1000))
        if not first: return
        self.timer.start(self.sheet.current_duration(self._base_interval))

    def preview_idle_motion(self):
        """手动立刻切到待机动作（不用等静止计时），给界面上的「试一下」按钮用。"""
        self._still_timer.stop()
        self._enter_idle_mode()
        return self.sheet.action if self._idle_mode else None

    def _exit_idle_mode(self):
        """鼠标动了：马上回到八方向的追踪动作，朝向用当前目标方向。"""
        if not self._idle_mode: return
        import logging
        logging.getLogger('pet').info('鼠标动了，退出待机动作，回到 %s', self.tracking_motion)
        self._idle_mode = False
        self._idle_timer.stop()
        self.sheet.set_action(self.tracking_motion)
        self.sheet.set_direction(self._target_direction)
        self.sheet.frame = 0
        self.refresh_interval(); self.update()

    @property
    def idle_mode(self):
        return self._idle_mode

    def restart_stillness(self):
        """鼠标在动（或刚动过）：重开「静止计时」，并把待机模式收掉。"""
        self._exit_idle_mode()
        if self._playing:
            self._still_timer.start(max(1000, int(float(self.idle_after_seconds or 20) * 1000)))

    def settle_delay(self):
        """鼠标要停稳多久才开始眨眼。

        取「一次完整动画循环时长 × SETTLE_RATIO」：原来用满一个周期（2820ms）
        太迟钝，改成一半（约 1410ms），停手后很快就恢复眨眼。
        不同动作/方向的帧时长不同，所以按 (动作, 方向) 缓存，免得每次鼠标移动都重算。
        """
        key = (self.sheet.action, self.sheet.direction)
        if self._settle_key != key:
            self._settle_key = key
            cycle = self.sheet.blink_cycle_max(self._base_interval)
            self._settle_ms = max(self.SETTLE_MIN_MS, int(cycle * self.SETTLE_RATIO))
        return self._settle_ms

    def pause_blinking(self):
        """只要鼠标在动就停止眨眼，并退回睁眼帧。

        眨眼途中被叫停会立刻睁眼——这是刻意的：转身过程中眨到一半看起来更怪。
        停稳后由 settle_timer 重新开始。
        """
        if self.timer.isActive(): self.timer.stop()
        if self.sheet.frame != 0:
            self.sheet.frame = 0
            self.update()
        self.settle_timer.start(self.settle_delay())

    def _settled(self):
        """鼠标停稳了：从睁眼帧开始正常眨眼。"""
        if not self._playing: return
        self.sheet.frame = 0
        self.timer.setInterval(self.sheet.current_duration(self._base_interval))
        self.timer.start()
        self.update()

    def aim_at(self, index):
        """把目标朝向设为 index，必要时逐格走过去。

        相邻一格：立刻切，不加任何延迟（保证跟手）。
        跨两格以上：先立刻走第一格（近的那格），剩下的按 TRACK_STEP_MS 逐格走完，
        这样「右下 → 右上」会经过「正右」，不会整幅硬跳。
        过渡途中又来新目标，就从当前位置重新算，不会卡在旧路径上。
        """
        index %= 8
        self._target_direction = index
        self._step_timer.stop()
        self._step_once()                       # 第一步立即执行，避免引入延迟
        if self.sheet.direction != index:
            self._step_timer.start(self.TRACK_STEP_MS)

    def jump_to_direction(self, index):
        """手动指定朝向：立刻切过去，并取消正在进行的逐格过渡。

        工作台点方向按钮属于「我要直接看这个方向」，不该被过渡动画拖住。
        """
        self._step_timer.stop()
        self._target_direction = index % 8
        self.sheet.set_direction(self._target_direction)
        self.directionChanged.emit(self._target_direction)
        self.update()

    def _step_once(self):
        current = self.sheet.direction
        if current == self._target_direction: return
        diff = (self._target_direction - current) % 8
        step = 1 if diff <= 4 else -1           # 走较短的那一边
        self.sheet.set_direction((current + step) % 8)
        self.directionChanged.emit(self.sheet.direction)
        self.update()

    def _step_towards_target(self):
        self._step_once()
        if self.sheet.direction == self._target_direction: self._step_timer.stop()

    def track_direction(self):
        """只负责视线朝向：高频轮询鼠标，判断是否在动、该朝哪边。

        行为约定（用户指定）：
          · 鼠标在动 → 不眨眼，保持睁眼
          · 鼠标停稳超过「一次完整眨眼动画的时长上限」→ 才开始眨眼
        所以这里除了换向，还要负责「叫停眨眼」这件事。

        注意：**静止判定要放在 `auto_direction` 检查之前**。待机动作跟朝向无关，
        以前关掉「跟随鼠标方向」（点一下方向按钮就会关）之后这里直接 return，
        待机计时器永远起不来 —— 表现就是「鼠标不动半天，什么也没发生」。
        """
        center = self.frameGeometry().center(); cursor = QCursor.pos()
        dx, dy = cursor.x() - center.x(), cursor.y() - center.y()
        here = (cursor.x(), cursor.y())
        if self._last_cursor is not None:
            moved = abs(here[0] - self._last_cursor[0]) + abs(here[1] - self._last_cursor[1]) >= self.TRACK_STILL_THRESHOLD
        else:
            moved = False
        self._last_cursor = here
        if moved:
            self.pause_blinking()
            self.restart_stillness()          # 鼠标一动：退出待机动作、重开静止计时
        elif not self._still_timer.isActive() and not self._idle_mode:
            self.restart_stillness()          # 首次进入 / 计时器已走完但没进待机的兜底
        if not self.auto_direction: return
        if self._idle_mode:
            # 待机动作没有方向可言：这段时间不做朝向判定，等鼠标动了再说。
            return
        if math.hypot(dx, dy) < self.TRACK_MIN_DISTANCE:
            # 光标贴在角色身上时角度没有意义（一点点位移就绕好几圈），不做朝向判定。
            return
        angle = math.degrees(math.atan2(dy, dx))
        current_center = self.sheet.direction * 45 - 90      # 当前朝向的中心角
        delta = abs((angle - current_center + 180) % 360 - 180)
        if delta < 22.5 + self.TRACK_HYSTERESIS_DEG: return  # 还在死区内，保持不动
        index = int((angle + 112.5) // 45) % 8
        if index == self.sheet.direction and index == self._target_direction: return
        self.aim_at(index)                                   # 跨格时逐格转身
        self.settle_timer.start(self.settle_delay())         # 换向等于「还在动」，重新等
        self.update()

    def tick(self):
        self.sheet.advance(); self.timer.setInterval(self.sheet.current_duration(self._base_interval)); self.update()

    def set_interval(self, value):
        self._base_interval = max(20, int(value)); self.timer.setInterval(self.sheet.current_duration(self._base_interval))
    def refresh_interval(self):
        """帧被重置后（换动作等）按当前帧的时长重设计时器，避免用上一帧的时长显示。"""
        self.timer.setInterval(self.sheet.current_duration(self._base_interval))
    def set_auto_direction(self, value): self.auto_direction = bool(value)
    def set_playing(self, value):
        # 暂停时连视线追踪一起停，否则「暂停」了角色还会跟着鼠标转头。
        self._playing = bool(value)
        if value:
            self.sheet.frame = 0
            self.timer.start(self.sheet.current_duration(self._base_interval))
            self.track_timer.start(self.track_interval)
            self.restart_stillness()
        else:
            self.timer.stop(); self.track_timer.stop(); self.settle_timer.stop()
            self._still_timer.stop(); self._idle_timer.stop(); self._exit_idle_mode()

    def show_bubble(self, text):
        self._bubble = str(text)
        self.bubbleRequested.emit(self._bubble)
        self.update()

    def set_talk_state(self, thinking):
        self._talking = bool(thinking)
        self.update()

    # ------------------------------------------------------------------ 表情
    def set_expression(self, name, hold_seconds=None):
        """切换表情；过一会儿自动回到常态。

        两类表情要分清楚：
        * **手动**（右键菜单选的）——用户说了算，一直留着；
        * **自动**（接口在回复里指定的）——临时的，`hold_seconds` 后回到手动选的那个。
        素材库里没有的名字一律忽略并记日志，免得模型编一个名字就把角色弄成空白。
        """
        import logging
        log = logging.getLogger('pet')
        name = str(name or '').strip()
        available = self.sheet.expressions
        if name and name not in available:
            log.info('表情「%s」不在素材库里（现有：%s），忽略', name, '、'.join(available) or '无')
            return False
        if not name and not self._auto_expression:
            return False
        self._auto_expression = name
        self.sheet.set_expression(name); self.sheet.frame = 0
        self.refresh_interval(); self.update()
        if self._expression_timer is None:
            self._expression_timer = QTimer(self); self._expression_timer.setSingleShot(True)
            self._expression_timer.timeout.connect(self._expression_expired)
        self._expression_timer.stop()
        if name:
            seconds = float(self.expression_hold_seconds if hold_seconds is None else hold_seconds)
            self._expression_timer.start(max(500, int(seconds * 1000)))
            log.info('换上表情「%s」，%.1f 秒后收回', name, seconds)
        return True

    def _expression_expired(self):
        """自动表情到期：回到手动选的表情（通常是「动作默认」）。"""
        self._auto_expression = ''
        self.sheet.set_expression(self._manual_expression); self.sheet.frame = 0
        self.refresh_interval(); self.update()

    @property
    def current_expression(self):
        return self.sheet.expression

    def scaled_frame(self):
        """取当前帧缩放后的图，带缓存。

        源图是 1254x1254 的 PNG，从头解码一次要 31~39ms；原先 paintEvent 每次都解一遍，
        追踪频率一提上来，UI 线程就被解码占满，表现就是整个桌宠又卡又飘。
        缓存键带上文件的修改时间和大小，所以换了素材会立刻失效，不会显示旧图。
        """
        frames = self.sheet.frames()
        if not frames: return None
        return self._scaled_for(frames[self.sheet.frame % len(frames)])

    def _scaled_for(self, path):
        try:
            info = path.stat(); stamp = (info.st_mtime_ns, info.st_size)
        except OSError:
            stamp = (0, 0)
        key = (str(path), stamp, self.width(), self.height(), self.crisp_rendering)
        cached = self._frame_cache.get(key)
        if cached is not None: return cached
        image = QImage(str(path))
        if image.isNull(): return None
        # Imported art is often a large transparent canvas. Scale it into the
        # desktop pet viewport so the character is not clipped outside the
        # 256px window and keep its aspect ratio intact.
        mode = Qt.TransformationMode.FastTransformation if self.crisp_rendering else Qt.TransformationMode.SmoothTransformation
        scaled = image.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio, mode)
        if len(self._frame_cache) >= 256: self._frame_cache.pop(next(iter(self._frame_cache)), None)
        self._frame_cache[key] = scaled
        return scaled

    def start_warmup(self):
        """后台把当前动作所有方向的帧逐张解码进缓存。

        全量约 40 张、一张 39ms。分摊成每 150ms 一张（约 20% CPU）慢慢预热，
        既不会卡住启动，也不会在你刚好转过视线的那一刻掉帧。
        """
        folder = self.sheet.root / "motions" / self.sheet.action
        self._warm_queue = [p for d in DIRECTIONS for p in sorted((folder / d).glob("*.png"))]
        if not self._warm_queue: return
        self._warm_timer = QTimer(self)
        self._warm_timer.timeout.connect(self._warm_step)
        self._warm_timer.start(150)

    def _warm_step(self):
        while self._warm_queue:
            if self._scaled_for(self._warm_queue.pop(0)) is not None: return   # 每个 tick 只解一张
        self._warm_timer.stop()

    def paintEvent(self, _):
        painter = QPainter(self); scaled = self.scaled_frame()
        if scaled is not None:
            painter.drawImage((self.width() - scaled.width()) // 2, (self.height() - scaled.height()) // 2, scaled)
        else:
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor("#5266a3")); painter.drawEllipse(QRectF(48, 25, 160, 170))
            painter.setBrush(QColor("#ffe0cd")); painter.drawEllipse(QRectF(68, 55, 120, 125))
            painter.setBrush(QColor("#303b6c")); painter.drawPie(QRectF(52, 24, 152, 105), 0, 180 * 16)
            painter.setPen(QPen(QColor("#303b6c"), 7)); painter.drawPoint(108, 120); painter.drawPoint(150, 120)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton: self.drag_offset = event.globalPosition().toPoint() - self.pos()
    def mouseMoveEvent(self, event):
        if self.drag_offset and event.buttons() & Qt.MouseButton.LeftButton: self.move(event.globalPosition().toPoint() - self.drag_offset)
    def mouseReleaseEvent(self, _): self.drag_offset = None
    def moveEvent(self, event):
        super().moveEvent(event)
        self.moved.emit()

    def context_menu(self):
        """构建右键菜单，返回 (菜单, 动作子菜单, 表情子菜单, 大小子菜单, 关闭动作)。

        拆出来是为了能单独验证菜单内容——`menu.exec()` 是模态阻塞的，没法在测试里调用。
        """
        menu = QMenu(self)
        actions = menu.addMenu("切换动作")
        for name in self.sheet.actions:
            action = actions.addAction(name); action.setData(name)
        expression_menu = None
        expressions = self.sheet.expressions
        if expressions:
            expression_menu = menu.addMenu("切换表情")
            clear = expression_menu.addAction("使用动作默认表情"); clear.setData("")
            for name in expressions:
                action = expression_menu.addAction(name); action.setData(name)
        # 大小：菜单给固定档位（好复现），滚轮给了连续微调的自由。
        scale_menu = menu.addMenu('调整大小（当前 %d%%）' % round(self.scale * 100))
        bigger = scale_menu.addAction('放大一点'); bigger.setData(('scale', self.MENU_STEP))
        smaller = scale_menu.addAction('缩小一点'); smaller.setData(('scale', -self.MENU_STEP))
        scale_menu.addSeparator()
        for label, value in (('很小 60%', 0.6), ('小 80%', 0.8), ('标准 100%', 1.0),
                             ('大 130%', 1.3), ('很大 160%', 1.6), ('特大 200%', 2.0)):
            action = scale_menu.addAction(label); action.setData(('scale_to', value))
        scale_menu.addSeparator()
        hint = scale_menu.addAction('提示：按住 Ctrl 滚轮也能缩放')
        hint.setEnabled(False)
        menu.addSeparator()
        # 桌宠模式没有工作台窗口，必须留一个退出口，否则用户只能去任务管理器结束进程。
        quit_action = menu.addAction("关闭桌宠")
        return menu, actions, expression_menu, scale_menu, quit_action

    def apply_menu_choice(self, chosen, actions, expression_menu, scale_menu, quit_action):
        if chosen is quit_action:
            QApplication.instance().quit()
        elif chosen and chosen.parent() is actions:
            self.sheet.set_action(chosen.data()); self.update()
        elif chosen and scale_menu is not None and chosen.parent() is scale_menu:
            data = chosen.data()
            if isinstance(data, tuple):
                kind, value = data
                self.set_scale(value) if kind == 'scale_to' else self.set_scale(self.scale + value)
        elif chosen and expression_menu and chosen.parent() is expression_menu:
            # 手动选的表情是「常态」，自动表情到期后要回到它。
            self._manual_expression = chosen.data() or ''
            if self._expression_timer is not None: self._expression_timer.stop()
            self._auto_expression = ''
            self.sheet.set_expression(self._manual_expression); self.sheet.frame = 0
            self.refresh_interval(); self.update()

    def contextMenuEvent(self, event):
        menu, actions, expression_menu, scale_menu, quit_action = self.context_menu()
        self.apply_menu_choice(menu.exec(event.globalPos()), actions, expression_menu, scale_menu, quit_action)
