import asyncio
import logging
from PySide6.QtCore import Qt, QPoint, QRectF, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QFont
from PySide6.QtWidgets import QApplication, QWidget, QLineEdit, QPushButton, QToolButton, QHBoxLayout, QVBoxLayout, QLabel, QMessageBox, QTextEdit, QSizePolicy
from speech import MAX_RECORD_SECONDS, Recorder, transcriber_from_config

LOG = logging.getLogger('speech')


class GrowingTextEdit(QTextEdit):
    """会随内容长高的多行输入框。

    单行 QLineEdit 长句子只能左右滚动，看不全也难改。这里换成多行：输入越多，
    输入框越长，整个聊天窗口跟着往下延展，整段内容一屏可见。
    Enter 发送、Shift+Enter 换行。
    """

    submitRequested = Signal()

    def __init__(self, parent=None, min_lines=1, max_lines=8):
        super().__init__(parent)
        self.min_lines, self.max_lines = min_lines, max_lines
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFont(QFont('Segoe UI', 10))
        self.textChanged.connect(self.fit_to_content)
        self.fit_to_content()

    def _content_height(self):
        """文档实际高度（含自动换行）。宽度必须先锁定，否则换行没算、高度不对。"""
        doc = self.document()
        width = self.viewport().width() or (self.width() - 2 * self.frameWidth())
        if width > 0: doc.setTextWidth(width)
        return doc.size().height()

    def line_count(self):
        spacing = max(1, self.fontMetrics().lineSpacing())
        margin = 2 * self.document().documentMargin()
        return max(self.min_lines, min(self.max_lines, int((self._content_height() - margin) / spacing + 0.99)))

    def fit_to_content(self):
        """按内容高度调整自身高度，到 max_lines 为止，再长就交给滚动条。"""
        spacing = max(1, self.fontMetrics().lineSpacing())
        margin = 2 * self.document().documentMargin()
        pad = 2 * self.frameWidth() + 10
        lowest = spacing + margin + pad
        highest = spacing * self.max_lines + margin + pad
        height = int(min(highest, max(lowest, self._content_height() + pad)))
        if height != self.height(): self.setFixedHeight(height)

    def keyPressEvent(self, event):
        enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if enter and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submitRequested.emit(); return
        super().keyPressEvent(event)

class SpeechBubble(QWidget):
    """角色头顶右上方的对话气泡。

    定位不再靠「窗口中心」硬猜，而是从**角色当前帧里真实找出头顶**：
    从上往下扫第一片不透明像素，取那一带的水平中心当锚点（换素材、换方向都跟着走）。
    气泡默认放在锚点的右上方一点点，尾巴朝下指向头顶；上面没地方就翻到下方（尾巴朝上），
    右边不够就整体左移。全部位置都会被夹在当前屏幕的工作区内，不会跑出屏幕。
    """

    OFFSET_Y = 8        # 气泡底边与头顶的垂直间距
    TAIL_RATIO = 0.18   # 尾巴落在气泡宽度 18% 处：于是气泡整体偏向头顶右上方
    PAD = 12            # 气泡内边距
    TAIL_W = 11         # 尾巴半宽
    TAIL_H = 14         # 尾巴高度
    RADIUS = 16

    def __init__(self, pet):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.FramelessWindowHint)
        self.pet, self.text = pet, ""
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.label = QLabel(self); self.label.setWordWrap(True); self.label.setAlignment(Qt.AlignmentFlag.AlignCenter); self.label.setStyleSheet("color:#20242b;background:transparent;font:10pt 'Segoe UI';")
        self._tail = ('down', 0.5)          # ('down'|'up', 尾巴在气泡内的相对位置 0~1)
        pet.moved.connect(self._place); pet.bubbleRequested.connect(self.show_text)

    def head_anchor(self):
        """角色头顶在**窗口坐标**里的位置，返回 (x, y, 头顶左边界, 头顶右边界)。

        取法：把当前帧转成图像，从上往下找第一条不透明像素带，取它的水平中心与左右范围。
        这样气泡永远对着「头」，而不是对着 256×256 画布的中心。
        """
        width, height = self.pet.width(), self.pet.height()
        fallback = (width * 0.55, height * 0.16, width * 0.35, width * 0.75)
        try:
            frame = self.pet.scaled_frame()
        except Exception:
            frame = None
        if frame is None:
            return fallback
        # scaled_frame() 给的是 QImage；这里两种都兼容，省得以后再踩一次。
        image = frame.toImage() if hasattr(frame, 'toImage') else frame
        if image is None or image.isNull():
            return fallback
        ratio = float(image.devicePixelRatio() or 1.0)
        rows, top = [], None
        for y in range(0, image.height(), 2):
            found = [x for x in range(0, image.width(), 2) if image.pixelColor(x, y).alpha() > 24]
            if found:
                top = y; rows.extend(found)
                for y2 in range(y + 2, min(y + 12, image.height()), 2):     # 头顶那几行一起算，抗噪
                    rows.extend(x for x in range(0, image.width(), 2) if image.pixelColor(x, y2).alpha() > 24)
                break
        if not rows or top is None:
            return fallback
        centre = sum(rows) / float(len(rows))
        # 帧是按 KeepAspectRatio 居中画在窗口里的，所以要把图像坐标换算回窗口坐标
        drawn_w, drawn_h = image.width() / ratio, image.height() / ratio
        left = (width - drawn_w) / 2.0
        top_offset = (height - drawn_h) / 2.0
        return (left + centre / ratio, top_offset + top / ratio,
                left + min(rows) / ratio, left + max(rows) / ratio)

    def show_text(self, text):
        self.text = str(text)
        self.label.setText(self.text)
        self.label.setFixedWidth(min(320, max(150, self.label.sizeHint().width() + 24)))
        self.label.adjustSize()
        self.setFixedSize(self.label.width() + self.PAD * 2, self.label.height() + self.PAD * 2 + self.TAIL_H)
        # 必须 force：show_text 是先定位再 show，而 _place 平时为了省开销会跳过隐藏状态，
        # 结果气泡第一次出现时停在 (0,0) 左上角，要等角色移动才跳到位。
        self._place(force=True); self.show(); self.raise_()

    def _place(self, force=False):
        if not force and not self.isVisible(): return
        pet = self.pet.frameGeometry()
        anchor = self.head_anchor()
        head_x, head_y = anchor[0], anchor[1]
        anchor_x, anchor_y = pet.left() + head_x, pet.top() + head_y
        # 定位的是**尾巴**（不是气泡左边缘）：尾巴正对头顶中心，气泡自然朝右上方铺开。
        # 以前把气泡左边缘顶在头顶右侧，尾巴就永远差着二十几像素够不到头。
        tail_ratio = self.TAIL_RATIO
        want_x = anchor_x - tail_ratio * self.width()
        want_y = anchor_y - self.height() - self.OFFSET_Y
        direction = 'down'
        screen = QApplication.screenAt(QPoint(int(anchor_x), int(anchor_y))) or QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else None
        if area is not None and want_y < area.top() + 4:
            # 头顶上面没地方了：翻到下方，尾巴朝上指着头
            direction = 'up'
            want_y = pet.bottom() + 6 - self.TAIL_H
        if area is not None and want_x + self.width() > area.right() - 4:
            # 右边放不下 → 镜像到左边（尾巴仍在头顶）
            tail_ratio = 1.0 - self.TAIL_RATIO
            want_x = anchor_x - tail_ratio * self.width()
        if area is not None:
            want_x = max(area.left() + 4, min(want_x, area.right() - self.width() - 4))
            want_y = max(area.top() + 4, min(want_y, area.bottom() - self.height() - 4))
        self.move(int(want_x), int(want_y))
        self._tail = (direction, tail_ratio)
        self._layout_label(direction)

    def _layout_label(self, direction):
        top = self.TAIL_H + 6 if direction == 'up' else 6
        self.label.setGeometry(self.PAD, top, self.width() - self.PAD * 2,
                               self.height() - self.TAIL_H - self.PAD - 2)

    def paintEvent(self, _):
        painter=QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#7e8799'),1)); painter.setBrush(QColor(255,255,255,248))
        direction, ratio = self._tail
        body_top = self.TAIL_H if direction == 'up' else 1
        body_height = self.height() - self.TAIL_H - 2
        painter.drawRoundedRect(QRectF(1, body_top, self.width()-2, body_height), self.RADIUS, self.RADIUS)
        tail_x = max(self.PAD + self.TAIL_W, min(self.width() - self.PAD - self.TAIL_W, ratio * self.width()))
        if direction == 'up':
            tail = [QPoint(int(tail_x - self.TAIL_W), body_top + 1), QPoint(int(tail_x), 1), QPoint(int(tail_x + self.TAIL_W), body_top + 1)]
        else:
            bottom = body_top + body_height
            tail = [QPoint(int(tail_x - self.TAIL_W), bottom - 1), QPoint(int(tail_x), self.height() - 1), QPoint(int(tail_x + self.TAIL_W), bottom - 1)]
        painter.drawPolygon(tail)

class ChatWindow(QWidget):
    """聊天输入窗：可以拖边框改大小。

    它是无边框窗口（FramelessWindowHint），系统默认不给拖边框的机会，所以这里：
      * 用 `QWindow.startSystemResize()` 调系统的缩放（Windows 上是原生手感，比手写
        mouseMove 改 setGeometry 稳得多）；
      * 鼠标靠近边框时把光标换成对应的缩放箭头，让人知道这儿能拖；
      * 高度默认还是跟着输入框内容自动长（这是原来就有的行为），但**一旦你手动拖过**，
        就记住你的高度，不再自动顶掉（宽度同理）。
    """

    RESIZE_MARGIN = 6      # 边框多少像素内算「拖边框」

    def __init__(self, manager, pet_width=256, pet=None):
        super().__init__(); self.manager, self.pet = manager, pet; self._drag_offset=None; self.setWindowTitle('和李花子聊天'); self.setMinimumWidth(pet_width); self.setMinimumHeight(54); self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint); self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._keep_height=False        # 用户手动拖过之后就不再自动改高度
        self._resize_edges=None
        input_font=QFont('Segoe UI'); input_font.setPointSize(10); pin_font=QFont('Segoe UI Symbol'); pin_font.setPointSize(15)
        self.input=GrowingTextEdit(self, min_lines=1, max_lines=8); self.input.setPlaceholderText('输入消息…（Enter 发送，Shift+Enter 换行）')
        # 按钮统一走一套样式：浅底细边，只有「发送」用主题色，视觉主次分明。
        self.send=QPushButton('➤'); self.send.setFont(input_font); self.send.setToolTip('发送（Enter）'); self.send.setFixedSize(56,30); self.send.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send.setStyleSheet('QPushButton{background:#4968d8;color:white;border:0;border-radius:7px} QPushButton:hover{background:#3d5cc9} QPushButton:disabled{background:#c7d0e6}')
        self.pin=QToolButton(); self.pin.setText('⌖'); self.pin.setFont(pin_font); self.pin.setCheckable(True); self.pin.setChecked(True); self.pin.setToolTip('固定在桌宠下方'); self.pin.setFixedSize(30,30); self.pin.setCursor(Qt.CursorShape.PointingHandCursor)
        # 选中态用浅色高亮而不是实心块：否则它会和「发送」抢视觉重心。
        self.pin.setStyleSheet('QToolButton{color:#8a93a6;border:1px solid #e2e6ef;border-radius:7px;background:#fafbfe} QToolButton:hover{background:#f2f5fd} QToolButton:checked{background:#e8edff;border-color:#aab8ee;color:#4968d8}'); self.pin.toggled.connect(self._place_below)
        # 语音输入：点一下开始录音，再点一下结束并识别。识别结果先填进输入框而不是直接发出，
        # 因为语音识别难免出错，留给你改一眼再按回车更稳妥。
        self.mic=QToolButton(); self.mic.setText('语音'); self.mic.setFont(input_font); self.mic.setCheckable(True); self.mic.setToolTip('语音输入：点一下开始录音，再点一下结束并识别'); self.mic.setFixedHeight(30); self.mic.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mic.setStyleSheet('QToolButton{color:#4968d8;border:1px solid #d5dbea;border-radius:7px;background:#f7f9ff;padding:0 8px} QToolButton:hover{background:#eef2ff} QToolButton:checked{background:#e04f4f;border-color:#e04f4f;color:white} QToolButton:disabled{color:#9aa3b2;background:#f1f3f7}'); self.mic.toggled.connect(self._mic_toggled); self._recorder=None; self._record_timer=None
        # 静音开关：勾上 = 角色不朗读回复，取消勾选 = 角色读出来。
        # 做成「静音」而不是「朗读」，是因为默认状态是关的：空着的勾选框就代表「会读」，
        # 一眼就知道现在会不会出声，不用反着想。
        self.mute=QToolButton(); self.mute.setText('静音'); self.mute.setFont(input_font); self.mute.setCheckable(True); self.mute.setFixedHeight(30); self.mute.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mute.setToolTip('勾上=角色不朗读回复；取消勾选=角色把回复读出来（需在 ⚙ → 语音合成设置 里填好百炼 API Key）')
        self.mute.setStyleSheet('QToolButton{color:#8a93a6;border:1px solid #e2e6ef;border-radius:7px;background:#fafbfe;padding:0 8px} QToolButton:hover{background:#f2f5fd} QToolButton:checked{background:#fdecea;border-color:#e2a49e;color:#c0392b}')
        self.mute.setChecked(not bool(getattr(manager, 'config', None) and manager.config.get('tts_enabled', False)))
        self.mute.toggled.connect(self._mute_toggled)
        self.setStyleSheet('ChatWindow{background:#ffffff;border:1px solid #cfd5e2;border-radius:10px} QTextEdit{background:#ffffff;border:1px solid #d9dee8;border-radius:8px;padding:4px 8px}')
        # 输入框独占一行（与角色同宽），按钮另起一行：挤在一行里输入区只剩一半宽。
        input_row=QHBoxLayout(); input_row.setContentsMargins(8,8,8,0); input_row.setSpacing(0); input_row.addWidget(self.input, 1)
        button_row=QHBoxLayout(); button_row.setContentsMargins(8,6,8,8); button_row.setSpacing(6)
        button_row.addWidget(self.pin); button_row.addWidget(self.mic); button_row.addWidget(self.mute); button_row.addStretch(1); button_row.addWidget(self.send)
        layout=QVBoxLayout(self); layout.setContentsMargins(0,0,0,0); layout.setSpacing(0); layout.addLayout(input_row); layout.addLayout(button_row)
        for child in (self.input, self.send, self.pin, self.mic, self.mute): child.installEventFilter(self)
        self.send.clicked.connect(self.submit); self.input.submitRequested.connect(self.submit); self.input.textChanged.connect(self._fit_height); manager.thinking.connect(lambda v:self.send.setEnabled(not v))
        self._fit_height()
        self.resize(pet_width, max(54, self.layout().sizeHint().height()))
        if pet: pet.moved.connect(self._place_below)
    def showEvent(self,event): super().showEvent(event); self._fit_height(); self._place_below()
    # ---------------------------------------------------------------- 拖边框改大小
    def _edges_at(self, pos):
        """这个点在窗口的哪条边上（无边框窗口要自己判断）。"""
        m = self.RESIZE_MARGIN
        edges = Qt.Edge(0)
        if pos.x() <= m: edges |= Qt.Edge.LeftEdge
        if pos.x() >= self.width() - m: edges |= Qt.Edge.RightEdge
        if pos.y() <= m: edges |= Qt.Edge.TopEdge
        if pos.y() >= self.height() - m: edges |= Qt.Edge.BottomEdge
        return edges

    @staticmethod
    def _cursor_for(edges):
        left = bool(edges & Qt.Edge.LeftEdge); right = bool(edges & Qt.Edge.RightEdge)
        top = bool(edges & Qt.Edge.TopEdge); bottom = bool(edges & Qt.Edge.BottomEdge)
        if (left and top) or (right and bottom): return Qt.CursorShape.SizeFDiagCursor
        if (right and top) or (left and bottom): return Qt.CursorShape.SizeBDiagCursor
        if left or right: return Qt.CursorShape.SizeHorCursor
        if top or bottom: return Qt.CursorShape.SizeVerCursor
        return None

    def _begin_manual_resize(self):
        """用户开始拖边框的那一刻要做的准备。

        关键一步是**放开 setFixedHeight 留下的 min==max**：不放开的话，
        系统缩放会被这个固定尺寸顶住，拖了没反应（这个坑是测试里踩出来的）。
        """
        self._keep_height = True
        self.setMinimumHeight(54)
        self.setMaximumHeight(16777215)

    def _start_resize(self, edges):
        """交给系统去做缩放：比手写 setGeometry 跟手，也不会有拖影。"""
        handle = self.windowHandle()
        if handle is None or not edges: return False
        try:
            handle.startSystemResize(edges)
        except Exception:
            LOG.warning('系统缩放不可用，改用自己算（%s）', edges, exc_info=True)
            return False
        return True

    def _fit_height(self):
        """输入框长高后，整个窗口跟着往下延展（顶部位置不动）。

        用户手动拖过高度之后就不再自动改，免得刚拖好又被顶回去；
        但内容更高时仍然会撑开（否则输入的字会被藏起来）。
        判断「用户想要多高」直接用当前高度，不额外记一份状态——
        隐藏窗口的 resize 事件不一定按预期到达，记状态反而会算错（测试踩过）。
        """
        self.input.fit_to_content()
        self.layout().activate()
        needed = max(54, self.layout().sizeHint().height())
        if self._keep_height:
            # 先放开 setFixedHeight 留下的 min==max，否则拖不动。
            self.setMinimumHeight(54)
            self.setMaximumHeight(16777215)
            want = max(needed, self.height())
            if self.height() != want: self.resize(self.width(), want)
        else:
            self.setFixedHeight(needed)
        self._place_below()
    def _place_below(self):
        if self.pin.isChecked() and self.pet:
            target=self.pet.frameGeometry(); x, y = target.left(), target.bottom()+8
            # 手动拖宽过之后可能顶出屏幕右边，夹一下工作区，别让它跑到看不见的地方。
            try:
                screen = self.screen() or QApplication.primaryScreen()
                area = screen.availableGeometry() if screen else None
                if area is not None:
                    x = max(area.left(), min(x, area.right() - self.width() + 1))
                    y = min(y, area.bottom() - self.height() + 1)
            except Exception:
                pass
            self.move(x, y)
    def mousePressEvent(self,event):
        # 点在边框上 = 想改大小（优先级高于「拖动窗口」）。
        edges = self._edges_at(event.position().toPoint())
        if edges and event.button()==Qt.MouseButton.LeftButton:
            self._begin_manual_resize()
            if self._start_resize(edges): return
        if not self.pin.isChecked() and event.button()==Qt.MouseButton.LeftButton: self._drag_offset=event.globalPosition().toPoint()-self.pos()
    def mouseMoveEvent(self,event):
        edges = self._edges_at(event.position().toPoint())
        cursor = self._cursor_for(edges)
        if cursor is not None: self.setCursor(cursor)
        else: self.unsetCursor()
        if self._drag_offset and event.buttons() & Qt.MouseButton.LeftButton: self.move(event.globalPosition().toPoint()-self._drag_offset)
    def mouseReleaseEvent(self,_):
        self._drag_offset=None
        if self._keep_height: self._fit_height()
    def resizeEvent(self, event):
        """尺寸变了就重排一次（气泡/位置都跟着走）。"""
        super().resizeEvent(event)
        if self._keep_height: self._place_below()
    def eventFilter(self, obj, event):
        # 子控件铺满整个窗口，鼠标落在它们身上时也要能拖边框：
        # 把坐标换算回窗口坐标再判断。
        if event.type() in (event.Type.MouseMove, event.Type.MouseButtonPress):
            try: local = obj.mapTo(self, event.position().toPoint())
            except Exception: local = None
            if local is not None:
                edges = self._edges_at(local)
                if event.type() == event.Type.MouseMove:
                    cursor = self._cursor_for(edges)
                    if cursor is not None: self.setCursor(cursor)
                    else: self.unsetCursor()
                elif edges and event.button() == Qt.MouseButton.LeftButton:
                    self._begin_manual_resize()
                    if self._start_resize(edges): return True
        if not self.pin.isChecked() and event.type() == event.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos(); return False
        if not self.pin.isChecked() and event.type() == event.Type.MouseMove and self._drag_offset and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset); return True
        if event.type() == event.Type.MouseButtonRelease:
            self._drag_offset=None
            if self._keep_height: self._fit_height()
        return super().eventFilter(obj,event)
    def submit(self):
        text=self.input.toPlainText().strip()
        if not text: return
        # 先建任务再清空文本框：万一事件循环不可用（create_task 抛错），
        # 内容还在，不会出现「点了发送、消息没了、也没回复」。
        try:
            task = asyncio.create_task(self.manager.send_message(text))
        except Exception as exc:
            LOG.error('无法发送消息：%s: %r', type(exc).__name__, exc, exc_info=exc)
            QMessageBox.warning(self, '发送失败', '%s: %s\n\n详情见 app.log' % (type(exc).__name__, exc))
            return
        self.input.clear()
        # create_task 的异常默认没人回收，pythonw 下连报错都看不到；
        # 加个回调把失败摆到明面上。
        task.add_done_callback(self._report_task_failure)

    def _report_task_failure(self, task):
        if task.cancelled(): return
        exc = task.exception()
        if exc is None: return
        LOG.error('发送消息的任务异常：%s: %r', type(exc).__name__, exc, exc_info=exc)
        QMessageBox.warning(self, '发送失败', '%s: %s\n\n详情见 app.log' % (type(exc).__name__, exc))

    # ------------------------------------------------------------------ 静音 / 朗读
    def _mute_toggled(self, muted):
        """勾上静音 = 角色不朗读回复，取消勾选 = 角色读出来；状态立刻写回 config.json。"""
        config = getattr(self.manager, 'config', None)
        if config is not None:
            try:
                config.update({'tts_enabled': not muted})
            except Exception:
                LOG.warning('保存静音开关失败', exc_info=True)
        if muted:
            try:
                self.manager.stop_speech()
            except Exception:
                LOG.warning('停止朗读失败', exc_info=True)
            return
        # 取消静音却没配 Key 的话，等角色张口没声最难受，这里提前说清楚。
        try:
            from voice import tts_from_config
            engine = tts_from_config(config)
            ok, why = (False, '语音合成还没配置好') if engine is None else engine.availability()
        except Exception as exc:
            ok, why = False, '%s: %s' % (type(exc).__name__, exc)
        if not ok:
            QMessageBox.information(self, '语音合成', '%s\n\n可在工作台 ⚙ → 语音合成设置 里填好百炼 API Key。' % why)

    # ------------------------------------------------------------------ 语音输入
    def _mic_toggled(self, recording):
        if recording: self._start_recording()
        else: self._stop_recording()

    def _start_recording(self):
        # 先确认识别可用再录：否则用户对着麦克风说完才被告知「已关闭」，白录一遍。
        transcriber, _ = transcriber_from_config(getattr(self.manager, 'config', None))
        if transcriber is None:
            self.mic.setChecked(False)
            QMessageBox.information(self, '语音输入', '语音输入已关闭。\n\n可在工作台 ⚙ → 语音输入设置 里开启。')
            return
        try:
            self._recorder = Recorder(); self._recorder.start()
        except Exception as exc:
            self._recorder = None
            self.mic.setChecked(False); self.mic.setText('语音')
            QMessageBox.warning(self, '语音输入', '无法开始录音：\n%s' % exc)
            return
        self.mic.setText('停止'); self.input.setPlaceholderText('正在录音…再点一次「停止」结束')
        # 忘记按停时的兜底，否则录音会一直涨。
        self._record_timer = QTimer(self); self._record_timer.setSingleShot(True)
        self._record_timer.timeout.connect(lambda: self.mic.setChecked(False))
        self._record_timer.start(MAX_RECORD_SECONDS * 1000)

    def _stop_recording(self):
        if self._record_timer is not None:
            self._record_timer.stop(); self._record_timer = None
        recorder, self._recorder = self._recorder, None
        path = None
        try:
            path = recorder.stop() if recorder else None
        except Exception as exc:
            LOG.warning('停止录音失败：%s', exc)
        self.input.setPlaceholderText('输入消息…')
        if not path:
            self.mic.setText('语音'); self.mic.setEnabled(True)
            QMessageBox.information(self, '语音输入', '没有录到声音，请检查麦克风后重试。')
            return
        self.mic.setText('识别…'); self.mic.setEnabled(False)
        asyncio.create_task(self._recognize(path))

    async def _recognize(self, path):
        transcriber, language = transcriber_from_config(getattr(self.manager, 'config', None))
        try:
            if transcriber is None: raise RuntimeError('语音识别已关闭（可在工作台 ⚙ → 语音输入设置 里开启）')
            text = await transcriber.transcribe_async(path, language)
            if text:
                self.input.setPlainText(text); self.input.setFocus()
            else:
                QMessageBox.information(self, '语音输入', '没有听清，请再说一遍。\n\n（短句、安静环境下识别率更高）')
        except Exception as exc:
            LOG.warning('语音识别失败：%s', exc)
            QMessageBox.warning(self, '语音识别失败',
                                '%s\n\n可在工作台 ⚙ → 语音输入设置 里换用其它识别方式。' % exc)
        finally:
            Recorder.cleanup(path)
            self.mic.setEnabled(True); self.mic.setText('语音'); self.mic.setChecked(False)
