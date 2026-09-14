import os, shutil, json, subprocess, urllib.parse, urllib.request, asyncio, logging
import uuid
from pathlib import Path
from PySide6.QtCore import Qt, QSize, QTimer, QRect
from PySide6.QtGui import QPixmap, QColor, QPainter, QPen
from PySide6.QtWidgets import QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton, QSlider, QSplitter, QToolButton, QVBoxLayout, QWidget, QComboBox, QCheckBox, QGridLayout, QDialog, QLineEdit, QFormLayout, QDialogButtonBox, QSpinBox, QDoubleSpinBox, QStackedWidget, QTextEdit, QProgressBar, QGroupBox, QScrollArea, QSizeGrip
from knowledge_base import CATEGORIES
from web_tools import BACKENDS, WebSearcher
from asset_registry import AssetRegistry
from sprite_sheet import DIRECTIONS


def screen_area(widget=None):
    """widget 所在屏幕的可用区域（任务栏什么的已经扣掉）。拿不到就返回 None。"""
    try:
        from PySide6.QtGui import QGuiApplication
        screen = None
        if widget is not None:
            handle = widget.windowHandle()
            if handle is not None: screen = handle.screen()
            if screen is None and hasattr(widget, 'screen'): screen = widget.screen()
        if screen is None: screen = QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen else None
    except Exception:
        return None


def fit_dialog(dialog, width, height, min_width=420, min_height=300, ratio=(0.95, 0.9)):
    """把对话框的初始大小夹进屏幕可用区域，并保证能拖边框改大小。

    以前这里是 `d.resize(820, 760)` 这种写死的数字：屏幕不够高时，窗口会**顶到屏幕外**，
    底部的按钮和状态栏就永远看不见了（用户报的就是这个）。现在：
      * 初始大小最多占屏幕的 95% 宽 / 90% 高；
      * 最小尺寸给一个能用的下限，但不阻止用户缩得更小（下限会再夹一次屏幕）；
      * 打开右下角的尺寸手柄，鼠标指向边框时也能拖。
    真正「内容太高」的对话框还会把内容塞进 QScrollArea，
    这样即使窗口被拖得很小，信息也滚得到。
    """
    area = screen_area(dialog)
    if area is not None:
        width = min(int(width), int(area.width() * ratio[0]))
        height = min(int(height), int(area.height() * ratio[1]))
        min_width = min(min_width, int(area.width() * 0.8))
        min_height = min(min_height, int(area.height() * 0.6))
    dialog.setSizeGripEnabled(True)
    dialog.setMinimumSize(max(320, min_width), max(240, min_height))
    dialog.resize(max(int(width), max(320, min_width)), max(int(height), max(240, min_height)))
    return dialog


def scrollable(dialog, width, height, min_width=420, min_height=300):
    """给内容很高的对话框套一层滚动区，返回里面那个用来 addWidget 的布局。

    按钮行要不要滚动由调用方决定：把 QDialogButtonBox 加到 dialog 自己的布局上，
    它就会一直贴在底部（不会跟内容一起滚走）。
    """
    outer = QVBoxLayout(dialog); outer.setContentsMargins(0, 0, 0, 0)
    area = QScrollArea(dialog); area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    inner = QWidget(); layout = QVBoxLayout(inner); layout.setContentsMargins(12, 12, 12, 12)
    area.setWidget(inner); outer.addWidget(area, 1)
    fit_dialog(dialog, width, height, min_width, min_height)
    dialog._outer_layout = outer      # 按钮行加到这个上面，不跟着滚
    return layout


# ---------------------------------------------------------------- 对话 API 的多套配置
# 「保存多个自己填的第三方接口」：一份 config.json 里存一个列表，随时切换。
# 一个档位只包含「接口身份」这五项；拦截关键词、停止词是回复过滤，不属于某个接口，所以不分档。
API_PROFILE_FIELDS = ('provider', 'model', 'api_key', 'url', 'max_tokens')
API_PROFILE_LABELS = {'provider': 'Provider', 'model': '模型', 'api_key': 'API Key',
                      'url': 'API 地址', 'max_tokens': '最大回复 token'}


def _as_text(value):
    return '' if value is None else str(value).strip()


def api_profiles(config):
    """读出已保存的接口档位。config 里没有、或格式不对，都当空列表（不抛异常）。

    读的时候顺手清洗：缺字段补空、名字为空或重复的丢掉，免得脏数据把界面搞崩。
    """
    raw = config.get('api_profiles', []) if config is not None else []
    if not isinstance(raw, list): return []
    seen, out = set(), []
    for item in raw:
        if not isinstance(item, dict): continue
        name = _as_text(item.get('name'))
        if not name or name in seen: continue
        seen.add(name)
        profile = {'name': name}
        for key in API_PROFILE_FIELDS:
            value = item.get(key, 0 if key == 'max_tokens' else '')
            if key == 'max_tokens':
                try: value = int(value or 0)
                except (TypeError, ValueError): value = 0
            profile[key] = value if key == 'max_tokens' else _as_text(value)
        out.append(profile)
    return out


def api_profile_match(config, fields):
    """当前填的这套等于哪个已存档？一样就返回它的名字，不然返回空串。

    用它来决定「保存后显示当前生效的是哪一档」——填的和存的差一个字符就不算同一档，
    这样界面不会谎称「你正在用某某档」。
    """
    for profile in api_profiles(config):
        if all(_as_text(profile.get(key)) == _as_text(fields.get(key))
               for key in API_PROFILE_FIELDS):
            return profile['name']
    return ''


def api_profiles_preview(profile):
    """给档位写一句人话摘要（不含 Key 本身，只说有没有填）。"""
    has_key = '有' if _as_text(profile.get('api_key')) else '无'
    return '%s ｜ %s ｜ %s ｜ Key %s ｜ %s token' % (
        profile.get('provider') or '-', profile.get('model') or '(未填模型)',
        profile.get('url') or '(默认地址)', has_key, profile.get('max_tokens') or '-')

class ControlPanel(QWidget):
    def __init__(self, sheet, pet, manager=None):
        super().__init__(); self.sheet, self.pet, self.manager = sheet, pet, manager; self.setWindowTitle("李花子 · 桌宠工作台"); self.resize(980, 620); self._thumb_cache = {}; self._frames_timer = None; self._shown_frames = None; self._build(); self._refresh_actions()
    def _build(self):
        self.setStyleSheet("QWidget{background:#f5f7fa;color:#20242b;font:10pt 'Segoe UI'} QFrame#card{background:white;border:1px solid #e1e5eb;border-radius:8px} QListWidget{background:white;border:0;outline:0} QListWidget::item{padding:8px;border-radius:5px} QListWidget::item:selected{background:#e8edff;color:#3452c4} QPushButton,QToolButton{border:1px solid #d7dce5;background:white;border-radius:5px;padding:6px 10px} QPushButton:hover,QToolButton:hover{background:#eef2ff} QSlider::groove:horizontal{height:4px;background:#dce1eb} QSlider::handle:horizontal{width:14px;margin:-5px 0;border-radius:7px;background:#4d69e8}")
        title=QLabel("李花子  ·  精灵片工作台"); title.setStyleSheet("font-size:18px;font-weight:600;padding:4px"); self.settings=QToolButton(); self.settings.setText("⚙"); self.settings.clicked.connect(self._settings_menu); header=QHBoxLayout(); header.addWidget(title); header.addStretch(); header.addWidget(self.settings)
        self.actions=QListWidget(); self.actions.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu); self.actions.customContextMenuRequested.connect(self._action_menu); new=QPushButton("＋ 新建动作"); new.clicked.connect(self.create_action); left=self._card("动作",self.actions,new)
        self.frames=QListWidget(); self.frames.setViewMode(QListWidget.ViewMode.ListMode); self.frames.setIconSize(QSize(72,72)); self.frames.setMinimumWidth(430); self.frames.setSpacing(14); self.frames.setUniformItemSizes(False); self.frames.setDragDropMode(QListWidget.DragDropMode.InternalMove); self.frames.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu); self.frames.customContextMenuRequested.connect(self._frame_menu); self.frames.model().rowsMoved.connect(lambda *_: self._save_frame_order()); self.frames.setStyleSheet('QFrame#frameRow{background:#ffffff;border:1px solid #d9dee8;border-radius:7px;} QFrame#frameRow:hover{border:1px solid #7890ee;background:#f8f9ff;}'); self._clipboard_path=None; self._clipboard_cut=False
        self.import_button=QPushButton("＋ 导入 PNG 帧"); self.import_button.clicked.connect(self.import_frames); self.direction=QComboBox()
        # 每一项都带上 data：前八个是方向序号 0~7，最后一项是 -1（无方向）。
        # 以前前八项用 addItems() 加、data 是 None，导致「当前是不是无方向」判断永远不成立。
        for arrow, index in (("↑  上",0),("↗  右上",1),("→  右",2),("↘  右下",3),("↓  下",4),("↙  左下",5),("←  左",6),("↖  左上",7)):
            self.direction.addItem(arrow, index)
        self.direction.addItem("无方向（帧按顺序播）",-1)
        self.direction.setToolTip("有方向的动作按方向取帧；无方向动作（比如待机）所有帧放在一起按顺序播")
        self.direction.currentIndexChanged.connect(self._direction_changed); self.flatten=QPushButton("转为无方向"); self.flatten.setToolTip("把这个动作各方向的帧收进动作根目录，变成无方向动作（待机用）"); self.flatten.clicked.connect(self.flatten_action); top=QHBoxLayout(); top.addWidget(QLabel("帧序列")); top.addStretch(); top.addWidget(self.flatten); top.addWidget(QLabel("方向")); top.addWidget(self.direction); center=QFrame(); center.setObjectName("card"); cl=QVBoxLayout(center); cl.addLayout(top); cl.addWidget(self.frames); cl.addWidget(self.import_button)
        self.preview=QLabel("暂无帧素材"); self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter); self.preview.setMinimumSize(280,300); self.preview.setStyleSheet("background:#202638;color:#aeb8d4;border-radius:8px"); self.play=QPushButton("▶ 播放"); self.play.setCheckable(True); self.play.toggled.connect(self._toggle); self.follow=QCheckBox("跟随鼠标方向"); self.follow.setChecked(True); self.follow.toggled.connect(self.pet.set_auto_direction); self.order=QComboBox(); [self.order.addItem(t,d) for t,d in (("正序","forward"),("倒序","reverse"),("随机","random"),("循环","loop"))]; self.order.currentIndexChanged.connect(lambda i:self.sheet.set_order(self.order.itemData(i))); self.speed=QSlider(Qt.Orientation.Horizontal); self.speed.setRange(20,1000); self.speed.setValue(125); self.speed.valueChanged.connect(self._speed_changed); self.speed_value=QLabel("125 ms"); self.refresh_playback=QPushButton("↻"); self.refresh_playback.setToolTip("应用所有单帧时长并刷新播放"); self.refresh_playback.setFixedWidth(34); self.refresh_playback.clicked.connect(self.apply_frame_durations); row=QHBoxLayout(); row.addWidget(QLabel("帧间隔")); row.addWidget(self.speed); row.addWidget(self.speed_value); row.addWidget(self.refresh_playback); presets=QHBoxLayout(); [self._preset(presets,t,v) for t,v in (("慢",280),("标准",125),("快",60))]; grid=QGridLayout(); [self._direction_button(grid,i) for i in range(8)]; self.track=QSlider(Qt.Orientation.Horizontal); self.track.setRange(20,400); self.track.setValue(int(self.manager.config.get('pet_track_interval',50)) if self.manager else 50); self.track.setToolTip('数值越小越跟手；调大则转头更沉稳，也几乎不占 CPU'); self.track_value=QLabel(f"{self.track.value()} ms"); self.track.valueChanged.connect(self._track_changed); self.track.sliderReleased.connect(self._track_saved); track_row=QHBoxLayout(); track_row.addWidget(QLabel("视线灵敏度")); track_row.addWidget(self.track); track_row.addWidget(self.track_value); self.pet_scale=QSlider(Qt.Orientation.Horizontal); self.pet_scale.setRange(int(pet.MIN_SCALE*100), int(pet.MAX_SCALE*100)); self.pet_scale.setValue(int(round(float(self.manager.config.get('pet_scale',1.0) or 1.0)*100))); self.pet_scale.setToolTip('桌宠大小。也可以直接对桌宠按住 Ctrl 滚轮，或右键 → 调整大小'); self.pet_scale_value=QLabel('%d%%' % self.pet_scale.value()); self.pet_scale.valueChanged.connect(self._scale_changed); self.pet_scale.sliderReleased.connect(self._scale_saved); scale_row=QHBoxLayout(); scale_row.addWidget(QLabel('桌宠大小')); scale_row.addWidget(self.pet_scale); scale_row.addWidget(self.pet_scale_value); right=QFrame(); right.setObjectName("card"); rl=QVBoxLayout(right); rl.addWidget(self.preview); rl.addLayout(grid); rl.addLayout(row); rl.addLayout(track_row); rl.addLayout(scale_row); rl.addLayout(presets); rl.addWidget(self.order); rl.addWidget(self.follow); rl.addWidget(self.play)
        split=QSplitter(); split.addWidget(left); split.addWidget(center); split.addWidget(right); split.setSizes([180,360,360]); layout=QVBoxLayout(self); layout.addLayout(header); layout.addWidget(split); self.actions.currentTextChanged.connect(self._action_changed); self.frames.currentItemChanged.connect(self._frame_selected); self.pet.directionChanged.connect(self._auto_direction_changed); self._sync_direction_buttons(0); self._sync_direction_ui()
    def _card(self,label,widget,button): card=QFrame(); card.setObjectName("card"); l=QVBoxLayout(card); l.addWidget(QLabel(label)); l.addWidget(widget); l.addWidget(button); return card
    def _preset(self,layout,text,value): b=QPushButton(text); b.clicked.connect(lambda:self.speed.setValue(value)); layout.addWidget(b)
    def _direction_button(self,grid,index):
        row,col=((0,1),(0,2),(1,2),(2,2),(2,1),(2,0),(1,0),(0,0))[index]; b=QToolButton(); b.setText(("↑","↗","→","↘","↓","↙","←","↖")[index]); b.setCheckable(True); b.clicked.connect(lambda:self._manual_direction(index)); grid.addWidget(b,row,col); setattr(self, f"dir_button_{index}", b)
    def _refresh_actions(self,select=None): self.actions.clear(); self.actions.addItems(self.sheet.actions); found=self.actions.findItems(select or self.sheet.action,Qt.MatchFlag.MatchExactly); self.actions.setCurrentItem(found[0] if found else self.actions.item(0)); self._refresh_frames()
    def _track_changed(self, v):
        self.track_value.setText(f"{v} ms")
        if self.pet: self.pet.set_track_interval(v)
    def _track_saved(self):
        # 拖动过程中不写文件，松手时才落盘。
        if self.manager: self.manager.config.update({'pet_track_interval': self.track.value()})
    def _scale_changed(self, value):
        """拖滑块时实时预览大小（persist=False：先不写配置，也别让桌宠回写一遍）。"""
        self.pet_scale_value.setText('%d%%' % value)
        if self.pet: self.pet.set_scale(value / 100.0, persist=False)
    def _scale_saved(self):
        # 松手才落盘。这里**直接写**：set_scale 在「值没变」时不会发信号，
        # 只靠信号写配置的话，用户把滑块拖回原值就存不上了。
        if self.manager: self.manager.config.update({'pet_scale': round(self.pet_scale.value() / 100.0, 3)})
    def _action_changed(self,name):
        self.sheet.set_action(name); self._refresh_frames(); self._sync_direction_ui(); self.pet.refresh_interval(); self.pet.update()
    def _direction_changed(self,index):
        data=self.direction.itemData(index)
        if data is None or int(data)<0: return          # 选到「无方向」这一项，不改任何东西
        self.pet.jump_to_direction(int(data)); self._sync_direction_buttons(int(data)); self._refresh_frames(); self.pet.refresh_interval(); self.pet.update()
    def _sync_direction_ui(self):
        """无方向动作：把「方向」显示成「无方向」并禁用方向选择。

        那个下拉和八个方向按钮对无方向动作完全没意义（帧都在一起按顺序播），
        留着只会让人以为「导入的帧按方向藏起来了」。
        """
        flat=self.sheet.is_current_flat()
        index=self.direction.findData(-1)
        if flat:
            self.direction.blockSignals(True); self.direction.setCurrentIndex(index); self.direction.blockSignals(False)
            self.direction.setEnabled(False)
            self.direction.setToolTip('当前动作是无方向的：所有帧放在一起按顺序播，与朝向无关')
        else:
            self.direction.setEnabled(True)
            self.direction.setToolTip('有方向的动作按方向取帧；无方向动作（比如待机）所有帧放在一起按顺序播')
            current=self.direction.currentData()
            if current is None or int(current)<0 or int(current)!=self.sheet.direction:
                self.direction.blockSignals(True); self.direction.setCurrentIndex(self.sheet.direction); self.direction.blockSignals(False)
        for i in range(8):
            button=getattr(self,f"dir_button_{i}",None)
            if button is not None:
                button.setEnabled(not flat)
                if flat: button.setChecked(False)
        can_flatten=(not flat) and self.sheet.motion_kind(self.sheet.action)=="directional"
        self.flatten.setEnabled(can_flatten)
        self.flatten.setToolTip('把这个动作各方向的帧收进动作根目录，变成无方向动作（待机用）'
                                if can_flatten else '当前动作已经是无方向的（或还没有帧）')
    def _auto_direction_changed(self, index):
        self.direction.blockSignals(True); self.direction.setCurrentIndex(index); self.direction.blockSignals(False); self._sync_direction_buttons(index); self._refresh_frames_soon()
    def _refresh_frames_soon(self):
        """方向变化触发的帧列表刷新，做去抖。

        鼠标移动时方向会连续变化（跨格时还有中间过渡步），而重建一次列表要
        12~30ms（idle 5 帧最慢，动作 1 只有 1 帧，这就是两者手感差异的来源）。
        这里合并成「方向停稳 250ms 后刷新一次」，转向过程中一次都不重建。
        """
        if self._frames_timer is None:
            self._frames_timer = QTimer(self); self._frames_timer.setSingleShot(True); self._frames_timer.timeout.connect(self._refresh_frames)
        self._frames_timer.start(250)
    def _thumb(self, path):
        """缩略图带缓存。

        原先每换一次方向都要把该方向所有帧的原图（1254x1254）重新解码一遍：
        idle 有 5 帧就是 127ms，动作 1 只有 1 帧约 46ms。这段时间 UI 线程被堵死，
        桌宠既不能重绘也不能响应鼠标——「切换方向卡顿」的真正原因就在这里，
        和视线追踪代码无关。缓存键含文件修改时间，换了素材会立刻失效。
        """
        try:
            info = path.stat(); key = (str(path), info.st_mtime_ns, info.st_size)
        except OSError:
            key = (str(path), 0, 0)
        cached = self._thumb_cache.get(key)
        if cached is not None: return cached
        pixmap = QPixmap(str(path)).scaled(72,72,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation)
        if len(self._thumb_cache) >= 256: self._thumb_cache.pop(next(iter(self._thumb_cache)), None)
        self._thumb_cache[key] = pixmap
        return pixmap
    def _refresh_frames(self):
        self._sync_direction_ui()                    # 方向显示跟着动作/表情一起更新
        paths=self.sheet.frames()
        # 列表内容没变就整个跳过。动作 1 有一半方向是空的、会回退到同一个方向，
        # 相邻方向拿到的帧列表完全一样，重建纯属白做（一次约 30ms）。
        if [str(p) for p in paths] == self._shown_frames: return
        self._shown_frames = [str(p) for p in paths]
        self.frames.clear()
        durations = self.sheet.durations()
        for p in paths:
            item = QListWidgetItem(); item.setData(Qt.ItemDataRole.UserRole, str(p)); self.frames.addItem(item)
            row = QFrame(); row.setObjectName('frameRow'); rl = QHBoxLayout(row); rl.setContentsMargins(8,3,8,3)
            thumb = QLabel(); thumb.setPixmap(self._thumb(p)); thumb.setFixedSize(82,78)
            order = QLabel(f"{self.frames.count():02d}"); order.setAlignment(Qt.AlignmentFlag.AlignCenter); order.setFixedWidth(30); order.setStyleSheet("font-weight:600;color:#6b7280;background:#eef1f6;border-radius:12px;padding:4px")
            name = QLabel(p.name); name.setMinimumWidth(240); name.setToolTip(str(p)); duration = QLineEdit(self.sheet.duration_text(p.name)); duration.setPlaceholderText("继承全局"); duration.setToolTip("固定时长填 1600；想要随机抖动填区间，如 1000-1600（每轮在此区间内随机）"); duration.setMaximumWidth(110); duration.editingFinished.connect(lambda p=p, w=duration: (self.sheet.set_frame_duration(p.name, w.text()), w.setText(self.sheet.duration_text(p.name))))
            rl.addWidget(order); rl.addWidget(thumb); rl.addWidget(name); rl.addStretch(); rl.addWidget(QLabel("时长:")); rl.addWidget(duration); row.setMinimumHeight(96); row.setMaximumHeight(104); item.setSizeHint(QSize(0, 104)); self.frames.setItemWidget(item, row)
        self._update_preview(paths[0] if paths else None)
    def _frame_selected(self,current,_): self._update_preview(Path(current.data(Qt.ItemDataRole.UserRole)) if current else None)
    def _update_preview(self,path):
        if path and path.exists(): self.preview.setPixmap(QPixmap(str(path)).scaled(260,260,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
        else: self.preview.setText("暂无帧素材"); self.preview.setPixmap(QPixmap())
    def _speed_changed(self,v): self.speed_value.setText(f"{v} ms"); self.pet.set_interval(v)
    def apply_frame_durations(self):
        for i in range(self.frames.count()):
            item = self.frames.item(i); row = self.frames.itemWidget(item)
            if row:
                edits = row.findChildren(QLineEdit)
                if edits:
                    path = Path(item.data(Qt.ItemDataRole.UserRole)); self.sheet.set_frame_duration(path.name, edits[-1].text())
        self.sheet.frame = 0; self.pet.set_interval(self.speed.value()); self._refresh_frames(); self.pet.update()
    def _sync_direction_buttons(self,index):
        # 无方向动作时这八个按钮是禁用的、也不该亮，否则看起来像「帧被藏在某个方向里」。
        flat=self.sheet.is_current_flat()
        for i in range(8): getattr(self, f"dir_button_{i}").setChecked((not flat) and i == index)
    def _manual_direction(self,i):
        self.follow.setChecked(False); self.direction.blockSignals(True); self.direction.setCurrentIndex(i); self.direction.blockSignals(False); self.sheet.set_direction(i); self._sync_direction_buttons(i); self._refresh_frames(); self.pet.refresh_interval(); self.pet.update()
    def _toggle(self,p):
        self.play.setText("▶ 播放" if p else "Ⅱ 暂停"); self.pet.refresh_interval(); self.pet.set_playing(not p)
    def _folder(self):
        """当前动作的帧目录：无方向的落在动作根目录，有方向的落在 `动作/方向/`。

        判断顺序很关键（这里踩过坑）：
        1. 已经有帧 → 按素材现状（flat / directional）走；
        2. **还没有帧** → 看这个动作里有没有 `N/E/S…` 子目录：
           有（「新建八方向动作」会先建好八个）→ 往方向目录放；
           没有（「新建无方向动作」建的）→ 放动作根目录，于是它保持无方向。
        以前没有第 2 条，导致在无方向动作里导入第一帧时凭空建出方向子目录，
        动作立刻被判定成有方向 —— 也就是「建不出无方向多帧」。
        """
        base = self.sheet.root/"motions"/self.sheet.action
        kind = self.sheet.motion_kind(self.sheet.action)
        if kind == "flat": return base
        if kind == "directional": return base/DIRECTIONS[self.sheet.direction]
        has_direction_folders = any((base/d).is_dir() for d in DIRECTIONS)
        return base/DIRECTIONS[self.sheet.direction] if has_direction_folders else base
    def create_action(self):
        """新建动作：用**中文按钮**明确问一次类型。

        以前用 QMessageBox.question(Yes/No/Cancel)：Qt 默认不带中文翻译，
        那三个按钮在中文系统上显示成英文，用户根本看不出那是在问「八方向还是无方向」。
        """
        n,ok=QInputDialog.getText(self,"新建动作","动作名称")
        if not (ok and n.strip()): return None
        name=n.strip()
        d=QDialog(self); d.setWindowTitle('新建动作：%s' % name); v=QVBoxLayout(d); fit_dialog(d, 470, 340)
        tip=QLabel('「%s」要建成哪种？\n\n'
                   '· 八方向（跟随鼠标转头）：每个方向各一套帧，会先建好 N/E/S… 八个目录\n'
                   '· 无方向（待机这类）：所有帧放在一起，不分子目录' % name)
        tip.setWordWrap(True); v.addWidget(tip)
        box=QDialogButtonBox(); eight=box.addButton('八方向（跟随鼠标）',QDialogButtonBox.ButtonRole.AcceptRole)
        flat=box.addButton('无方向（待机用）',QDialogButtonBox.ButtonRole.AcceptRole)
        box.addButton('取消',QDialogButtonBox.ButtonRole.RejectRole); v.addWidget(box)
        choice={'kind':None}
        eight.clicked.connect(lambda: (choice.update(kind='eight'), d.accept()))
        flat.clicked.connect(lambda: (choice.update(kind='flat'), d.accept()))
        d.exec()
        if not choice['kind']: return None
        base=self.sheet.root/"motions"/name
        if choice['kind']=='eight':
            [ (base/direction).mkdir(parents=True,exist_ok=True) for direction in DIRECTIONS ]
        else:
            base.mkdir(parents=True,exist_ok=True)
        self._refresh_actions(name); return name
    def create_flat_action(self):
        """新建「无方向动作」：帧直接放在动作目录下，适合待机这类没有朝向的动画。"""
        n,ok=QInputDialog.getText(self,"新建无方向动作","动作名称（待机用，不需要八方向）")
        if ok and n.strip():
            (self.sheet.root/"motions"/n.strip()).mkdir(parents=True,exist_ok=True)
            self._refresh_actions(n.strip()); return n.strip()
        return None
    def flatten_action(self):
        """把当前动作「转为无方向」：各方向的帧收进动作根目录，删掉方向子目录。

        给已经建错成八方向的动作（比如「发呆」）一条回头路；帧文件名冲突时加方向前缀，
        方向目录里的 durations.json 会按新文件名并到根目录，时长不丢。
        """
        if not self.manager: return
        base=self.sheet.root/"motions"/self.sheet.action
        if self.sheet.motion_kind(self.sheet.action)=="flat":
            QMessageBox.information(self,"转为无方向","这个动作已经就是无方向的了。"); return
        total=sum(len(list((base/direction).glob("*.png"))) for direction in DIRECTIONS if (base/direction).is_dir())
        if total==0:
            # 以前这里会照样往下走，然后弹「已把 0 帧收进根目录，现在是无方向动作」——
            # 听着像成功了，其实这个动作还是空的、待机列表里也不会出现。
            QMessageBox.information(self,"转为无方向",
                "「%s」里还没有帧，没什么可转换的。\n\n请先选中它、点「＋ 导入 PNG 帧」把图导进来；"
                "（如果图已经在别的动作里，先在那个动作里导出/复制过来）" % self.sheet.action); return
        if QMessageBox.question(self,"转为无方向",
                "把「%s」各方向的 %d 帧收进动作根目录、并删掉方向子目录？\n"
                "（帧不会丢；文件名冲突时自动加方向前缀，方向里的时长设置会一并保留）"
                % (self.sheet.action,total))!=QMessageBox.StandardButton.Yes: return
        moved=0; merged_durations={}
        for direction in DIRECTIONS:
            folder=base/direction
            if not folder.is_dir(): continue
            renames={}                                   # 原名 -> 落地名，时长要跟着改
            for source in sorted(folder.glob("*.png")):
                target=base/source.name
                if target.exists(): target=base/("%s_%s" % (direction, source.name))
                renames[source.name]=target.name
                source.replace(target); moved+=1
            duration_file=folder/"durations.json"
            if duration_file.exists():
                try: data=json.loads(duration_file.read_text(encoding="utf-8"))
                except (OSError,ValueError): data={}
                for key,value in (data.items() if isinstance(data,dict) else []):
                    # 按**实际**改名结果映射；以前按「文件在不在」猜，会把没改名的帧也加上方向前缀，
                    # 于是时长对不上文件名、静默失效。
                    merged_durations.setdefault(renames.get(key,key),value)
                duration_file.unlink()
            try: folder.rmdir()
            except OSError: pass
        if merged_durations:
            path=base/"durations.json"; existing={}
            if path.exists():
                try: existing=json.loads(path.read_text(encoding="utf-8"))
                except (OSError,ValueError): existing={}
            existing.update(merged_durations)
            path.write_text(json.dumps(existing,ensure_ascii=False,indent=2),encoding="utf-8")
        self.sheet.frame=0; self._refresh_frames(); self.pet.update()
        QMessageBox.information(self,"转为无方向",
            "已把 %d 帧收进动作根目录，现在「%s」是无方向动作，可以当待机用了。" % (moved,self.sheet.action))
    def import_frames(self):
        files,_=QFileDialog.getOpenFileNames(self,"导入 PNG 帧","","PNG 图片 (*.png)");
        if files:
            folder=self._folder(); folder.mkdir(parents=True,exist_ok=True)
            for f in files: shutil.copy2(f, folder/Path(f).name)
            self.sheet.invalidate_frames(); self._refresh_frames(); self.pet.update()
            # 明确告诉用户导到哪去了：以前导完没任何反馈，「帧不知道去哪了」全靠猜。
            kind='无方向动作（帧都在这一个目录里）' if self.sheet.is_flat(self.sheet.action) else '八方向动作的「%s」方向' % DIRECTIONS[self.sheet.direction]
            QMessageBox.information(self,"导入完成",
                "已导入 %d 帧到：\n%s\\%s\n\n这个动作现在是%s，共 %d 帧。"
                % (len(files), self.sheet.action, folder.name, kind, len(self.sheet.frames())))
    def _selected(self):
        i=self.frames.currentItem(); return Path(i.data(Qt.ItemDataRole.UserRole)) if i else None
    def delete_frame(self):
        p=self._selected();
        if p and QMessageBox.question(self,"删除帧",f"删除 {p.name}？")==QMessageBox.StandardButton.Yes: p.unlink(); self.sheet.invalidate_frames(); self._refresh_frames()
    def rename_frame(self):
        p=self._selected();
        if p:
            n,ok=QInputDialog.getText(self,"重命名帧","文件名",text=p.stem)
            if ok and n.strip(): p.rename(p.with_name(n.strip()+".png")); self.sheet.invalidate_frames(); self._refresh_frames()
    def _frame_menu(self,pos):
        item=self.frames.itemAt(pos)
        if item:
            self.frames.setCurrentItem(item)
        else:
            self.frames.clearSelection(); self.frames.setCurrentRow(-1)
        m=QMenu(self); copy=m.addAction("复制帧"); cut=m.addAction("剪切帧"); paste=m.addAction("粘贴帧"); duplicate=m.addAction("复制为新帧"); m.addSeparator(); d=m.addAction("删除帧"); r=m.addAction("重命名帧"); a=m.exec(self.frames.mapToGlobal(pos));
        if a==copy: self.copy_frame()
        elif a==cut: self.cut_frame()
        elif a==paste: self.paste_frame()
        elif a==duplicate: self.duplicate_frame()
        elif a==d: self.delete_frame()
        elif a==r: self.rename_frame()
    def copy_frame(self):
        path=self._selected()
        if path: self._clipboard_path=path; self._clipboard_cut=False
    def cut_frame(self):
        path=self._selected()
        if path: self._clipboard_path=path; self._clipboard_cut=True
    def paste_frame(self):
        if not self._clipboard_path or not self._clipboard_path.exists(): return
        target=self._folder(); target.mkdir(parents=True,exist_ok=True); source=self._clipboard_path; name=source.stem; ext=source.suffix; destination=target/(name+ext); index=1
        while destination.exists(): destination=target/(f"{name}_copy{index}{ext}"); index+=1
        shutil.move(str(source),str(destination)) if self._clipboard_cut else shutil.copy2(str(source),str(destination))
        source_durations = self.sheet.durations();
        if source.name in source_durations: self.sheet.set_frame_duration(destination.name, source_durations[source.name])
        # Rebuild the list with the pasted file inserted after the selected row.
        existing = [Path(self.frames.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.frames.count()) if Path(self.frames.item(i).data(Qt.ItemDataRole.UserRole)).exists()]
        anchor = self.frames.currentRow(); insert_at = anchor + 1 if anchor >= 0 else len(existing)
        existing = [p for p in existing if p.resolve() != destination.resolve()]
        existing.insert(min(insert_at, len(existing)), destination)
        self._persist_order(existing)
        self._clipboard_cut=False; self._refresh_frames(); self.pet.update()
    def duplicate_frame(self):
        path=self._selected()
        if path: self._clipboard_path=path; self._clipboard_cut=False; self.paste_frame()

    def _persist_order(self, paths):
        """Persist a desired visual order using numeric filenames."""
        folder = self._folder()
        if not paths: return
        temp = []
        for i, path in enumerate(paths):
            t = folder / f".__frame_order_{uuid.uuid4().hex}_{i}.png"; path.rename(t); temp.append(t)
        durations = self.sheet.durations()
        new_durations = {}
        for i, temp_path in enumerate(temp):
            target = folder / f"{i:03d}.png"; temp_path.rename(target)
            # The original duration is looked up by the pre-reorder source name.
            source_name = paths[i].name
            if source_name in durations: new_durations[target.name] = durations[source_name]
        (folder / "durations.json").write_text(__import__('json').dumps(new_durations, ensure_ascii=False, indent=2), encoding='utf-8')
        # 改完文件立刻让帧列表缓存失效：以前靠「动作/方向」目录的 mtime 判断，无方向动作
        # 根本没有那个目录，于是缓存永不失效 —— 重排后列表还指着旧文件名，缩略图和角色
        # 都变成占位图。
        self.sheet.invalidate_frames()
        self._refresh_frames()

    def _save_frame_order(self):
        items = [self.frames.item(i) for i in range(self.frames.count())]
        paths = [Path(item.data(Qt.ItemDataRole.UserRole)) for item in items]
        self._persist_order(paths)
    def _action_menu(self,pos):
        m=QMenu(self); r=m.addAction("重命名动作"); f=m.addAction("转为无方向动作"); d=m.addAction("删除动作"); a=m.exec(self.actions.mapToGlobal(pos));
        if a==r: self.rename_action()
        elif a==f: self.flatten_action()
        elif a==d: self.delete_action()
    def rename_action(self):
        old=self.sheet.action; n,ok=QInputDialog.getText(self,"重命名动作","新名称",text=old)
        if ok and n.strip() and n.strip()!=old: (self.sheet.root/"motions"/old).rename(self.sheet.root/"motions"/n.strip()); self.sheet.set_action(n.strip()); self._refresh_actions(n.strip())
    def delete_action(self):
        if self.sheet.action!="idle" and QMessageBox.question(self,"删除动作","删除当前动作及全部帧？")==QMessageBox.StandardButton.Yes: shutil.rmtree(self.sheet.root/"motions"/self.sheet.action); self._refresh_actions()
    def _settings_menu(self):
        m=QMenu(self); role=m.addAction("角色"); api=m.addAction("API 配置"); voice=m.addAction("语音输入设置"); tts=m.addAction("语音合成设置"); screen=m.addAction("屏幕理解设置"); idle=m.addAction("待机动作设置"); prompt=m.addAction("角色提示词"); knowledge=m.addAction("角色知识库"); logs=m.addAction("运行日志"); folder=m.addAction("打开当前素材目录"); chosen=m.exec(self.settings.mapToGlobal(self.settings.rect().bottomLeft()))
        if chosen==role: self._character_dialog()
        elif chosen==api: self._api_dialog()
        elif chosen==voice: self._voice_dialog()
        elif chosen==tts: self._tts_dialog()
        elif chosen==screen: self._screen_dialog()
        elif chosen==idle: self._idle_motion_dialog()
        elif chosen==prompt: self._prompt_dialog()
        elif chosen==knowledge: self._knowledge_dialog()
        elif chosen==logs: self._log_dialog()
        elif chosen==folder: self._folder().mkdir(parents=True,exist_ok=True); os.startfile(str(self._folder()))

    def _log_dialog(self):
        path=self.manager.root/'app.log'; d=QDialog(self); d.setWindowTitle('运行日志'); l=QVBoxLayout(d); fit_dialog(d, 820, 560); view=QTextEdit(); view.setReadOnly(True); l.addWidget(view); status=QLabel(str(path)); l.addWidget(status); row=QHBoxLayout(); refresh=QPushButton('刷新'); clear=QPushButton('清空'); export=QPushButton('导出日志'); close=QPushButton('关闭'); [row.addWidget(b) for b in (refresh,clear,export,close)]; l.addLayout(row)
        def load():
            try: view.setPlainText(path.read_text(encoding='utf-8') if path.exists() else '暂无日志'); view.moveCursor(view.textCursor().MoveOperation.End)
            except Exception as e: view.setPlainText(str(e))
        def clear_log():
            try: path.write_text('',encoding='utf-8'); load()
            except OSError as e: QMessageBox.warning(d,'清空失败',str(e))
        def export_log():
            target,_=QFileDialog.getSaveFileName(d,'导出日志','app.log','日志文件 (*.log);;文本文件 (*.txt)')
            if target:
                try: shutil.copy2(path,target)
                except OSError as e: QMessageBox.warning(d,'导出失败',str(e))
        refresh.clicked.connect(load); clear.clicked.connect(clear_log); export.clicked.connect(export_log); close.clicked.connect(d.close); timer=QTimer(d); timer.timeout.connect(load); timer.start(1500); load(); d.setModal(False); d.show(); d.raise_()

    def _character_dialog(self):
        registry=AssetRegistry(self.manager.root/'assets'); dialog=QDialog(self); dialog.setWindowTitle('角色管理'); layout=QVBoxLayout(dialog); fit_dialog(dialog, 460, 320)
        search=QLineEdit(); search.setPlaceholderText('搜索角色…'); chooser=QComboBox(); chooser.addItems(registry.get_character_list()); current=getattr(self.manager,'character','default'); chooser.setCurrentText(current); layout.addWidget(QLabel('角色（素材、知识库、长期记忆相互隔离）')); layout.addWidget(search); layout.addWidget(chooser)
        buttons_row=QHBoxLayout(); new=QPushButton('新增'); rename=QPushButton('重命名'); delete=QPushButton('删除'); [buttons_row.addWidget(b) for b in (new,rename,delete)]; layout.addLayout(buttons_row); buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel); layout.addWidget(buttons); buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
        all_names=registry.get_character_list()
        def filter_names(text):
            value=text.strip().lower(); selected=chooser.currentText(); chooser.blockSignals(True); chooser.clear(); chooser.addItems([n for n in all_names if value in n.lower()]); chooser.setCurrentText(selected); chooser.blockSignals(False)
        search.textChanged.connect(filter_names)
        def create():
            name,ok=QInputDialog.getText(dialog,'新增角色','角色名称')
            if ok and name.strip() and name.strip() not in all_names:
                base=self.manager.root/'assets'/'characters'/name.strip(); [ (base/'motions'/'idle'/d).mkdir(parents=True,exist_ok=True) for d in DIRECTIONS ]; all_names.append(name.strip()); filter_names(search.text()); chooser.setCurrentText(name.strip())
        def rename_role():
            old=chooser.currentText(); name,ok=QInputDialog.getText(dialog,'重命名角色','新名称',text=old)
            if ok and name.strip() and name.strip()!=old and name.strip() not in all_names:
                characters_root=self.manager.root/'assets'/'characters'; characters_root.mkdir(parents=True,exist_ok=True); old_path=characters_root/old; new_path=characters_root/name.strip()
                try:
                    if old_path.exists():
                        old_path.rename(new_path)
                    else:
                        # `default` may be a virtual role backed directly by
                        # assets/motions. Materialize it before renaming.
                        new_path.mkdir(parents=True,exist_ok=True)
                        source_root=self.manager.root/'assets'
                        for folder_name in ('motions','expressions'):
                            source=source_root/folder_name
                            if source.exists(): shutil.copytree(source,new_path/folder_name,dirs_exist_ok=True)
                    all_names.remove(old); all_names.append(name.strip()); filter_names(search.text()); chooser.setCurrentText(name.strip())
                except OSError as exc:
                    QMessageBox.warning(dialog,'重命名失败',str(exc))
        def delete_role():
            name=chooser.currentText()
            if len(all_names)<=1 or name=='default': QMessageBox.information(dialog,'无法删除','至少需要保留一个角色，且不能删除 default。'); return
            if QMessageBox.question(dialog,'删除角色',f'删除 {name} 的素材与角色目录？')==QMessageBox.StandardButton.Yes:
                shutil.rmtree(self.manager.root/'assets'/'characters'/name); all_names.remove(name); filter_names(search.text())
        new.clicked.connect(create); rename.clicked.connect(rename_role); delete.clicked.connect(delete_role)
        if dialog.exec()==QDialog.DialogCode.Accepted:
            name=chooser.currentText() or all_names[0]; self.sheet.root=registry.character_root(name); self.sheet.set_action('idle'); self.manager.switch_character(name); self._refresh_actions('idle'); self.pet.update()

    def _prompt_dialog(self):
        role_root = self.sheet.root if (self.sheet.root / 'prompts').exists() else self.manager.root
        path = role_root / "prompts" / "persona.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        try: current = path.read_text(encoding='utf-8') if path.exists() else '{}'
        except OSError: current = '{}'
        dialog=QDialog(self); dialog.setWindowTitle('角色提示词'); layout=QVBoxLayout(dialog); fit_dialog(dialog, 560, 460); editor=QTextEdit(); editor.setPlainText(current); editor.setPlaceholderText('输入 JSON 人设配置'); layout.addWidget(QLabel('直接编辑 prompts/persona.json（保存后下次对话生效）')); layout.addWidget(editor); buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel); layout.addWidget(buttons); buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
        def save_prompt():
            try: data=json.loads(editor.toPlainText()); path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8'); dialog.close()
            except json.JSONDecodeError: QMessageBox.warning(dialog,'格式错误','提示词必须是有效 JSON。')
        buttons.accepted.connect(save_prompt); buttons.rejected.connect(dialog.close); dialog.setModal(False); dialog.show(); dialog.raise_()

    def _knowledge_dialog(self):
        kb=self.manager.knowledge_base; dialog=QDialog(self); dialog.setWindowTitle('角色知识库构建与审核'); root=QVBoxLayout(dialog); fit_dialog(dialog, 800, 600)
        form=QFormLayout(); default_name=self.manager.config.get('knowledge_name') or getattr(self.manager,'character','') or '李花子'; name=QLineEdit(default_name); desc=QLineEdit(); fetch=QPushButton('开始搜集（异步）'); progress=QProgressBar(); progress.setRange(0,0); progress.setVisible(False); status=QLabel('可配置独立 API；采集结果默认待审核。'); form.addRow('角色名称',name); form.addRow('补充描述',desc); form.addRow('采集',fetch); form.addRow('进度',progress); root.addLayout(form)
        api_button=QPushButton('知识库 API 与提示词设置'); root.addWidget(api_button); api_button.clicked.connect(self._knowledge_api_dialog)
        import_button=QPushButton('导入文档归纳（txt / md / html，可多选）'); dedupe_button=QPushButton('检测并清理重复'); tools=QHBoxLayout(); tools.addWidget(import_button); tools.addWidget(dedupe_button); root.addLayout(tools)
        category=QComboBox(); category.addItems(CATEGORIES); entries=QListWidget(); content=QTextEdit(); content.setPlaceholderText('选择条目后编辑内容'); add=QPushButton('新增'); save=QPushButton('保存修改'); delete=QPushButton('删除'); approve=QPushButton('标记已确认'); summary=QLabel(); row=QHBoxLayout(); row.addWidget(category); row.addWidget(entries); right=QVBoxLayout(); right.addWidget(content); right.addWidget(summary); buttons=QHBoxLayout(); [buttons.addWidget(b) for b in (add,save,delete,approve)]; right.addLayout(buttons); row.addLayout(right); root.addLayout(row); close=QDialogButtonBox(QDialogButtonBox.StandardButton.Close); root.addWidget(close); close.rejected.connect(dialog.reject); close.accepted.connect(dialog.accept)
        def load_row(_=None):
            data=kb.entries(category.currentText()); i=entries.currentRow()
            if 0<=i<len(data): content.setPlainText(data[i]['content'])
            else: content.clear()
        def refresh(keep=True):
            current=entries.currentRow()
            # 重建列表必须屏蔽信号：entries.clear() 会把当前行置 -1，进而触发
            # currentRowChanged 把编辑框清空——「保存后内容消失、选中丢失」就是它。
            entries.blockSignals(True); entries.clear()
            for x in kb.entries(category.currentText()):
                entries.addItem(f"[{x['status']}] {x['content'][:50] or '（空条目，可直接删除）'}")
            entries.blockSignals(False)
            if keep and 0<=current<entries.count(): entries.setCurrentRow(current)   # 会触发 load_row，显示保存后的内容
            elif not keep: entries.setCurrentRow(-1); content.clear()
            elif entries.count(): entries.setCurrentRow(0)
            else: content.clear()
            meta=kb.summary(); summary.setWordWrap(True); summary.setText(f"更新时间：{meta.get('last_updated','-')}　来源数：{meta.get('source_count','0')}　后端：{meta.get('last_search_backend','-')}\n最近一次实情：{meta.get('last_note','-')}")
        category.currentTextChanged.connect(lambda _=None: refresh(keep=False)); refresh()
        def selected():
            row=entries.currentRow(); data=kb.entries(category.currentText()); return data[row] if 0<=row<len(data) else None
        entries.currentRowChanged.connect(load_row)
        entries.itemClicked.connect(lambda _item: load_row())   # 再点同一项也能重新载入，便于撤销未保存的改动
        add.clicked.connect(lambda: (kb.save(category.currentText(),content.toPlainText(),'手动','待审核'),refresh()))
        save.clicked.connect(lambda: self._save_kb_entry(kb,selected(),category,content,refresh))
        delete.clicked.connect(lambda: (kb.delete(selected()['id']),refresh()) if selected() else None)
        approve.clicked.connect(lambda: self._approve_kb(kb,selected(),refresh))
        def remember_name():
            # 这个输入框以前只读不写，所以改名后下次打开又变回默认值。
            value=name.text().strip()
            if value and value != self.manager.config.get('knowledge_name'): self.manager.config.update({'knowledge_name':value})
        name.editingFinished.connect(remember_name)
        dialog.finished.connect(lambda _=None: remember_name())
        async def collect():
            remember_name()
            values=self.manager.config.data; fetch.setEnabled(False); progress.setVisible(True)
            local_on=str(values.get('knowledge_web_backend','auto')).lower() != 'off'; api_on=bool(values.get('knowledge_api_web_search'))
            if api_on and local_on: status.setText('正在联网检索（API 自身 + 本地）并归档…')
            elif api_on: status.setText('正在用 API 自身联网检索并归档…')
            elif local_on: status.setText('正在本地检索网页，再交给 API 整理…')
            else: status.setText('未联网：正在让模型凭自身知识整理（真实性取决于模型，建议用最强模型）…')
            try:
                count,note=await kb.build(name.text(),desc.text(),values,values.get('knowledge_prompt',''))
                if count == 0: status.setText(f'完成：API 正常但没有可入库的条目（{note}）')
                else: status.setText(f'完成：新增 {count} 条，等待审核（{note}）')
            except Exception as exc: status.setText(f'搜集失败：{exc}'); QMessageBox.warning(dialog,'知识库搜集失败',str(exc))
            finally: progress.setVisible(False); fetch.setEnabled(True)
            refresh()
        fetch.clicked.connect(lambda: asyncio.create_task(collect()))
        async def run_import(files):
            remember_name()
            import_button.setEnabled(False); fetch.setEnabled(False); progress.setVisible(True)
            status.setText('正在导入 %d 个文件并归纳，长文档需要几分钟…' % len(files))
            try:
                values=self.manager.config.data
                count,note=await kb.ingest_documents(name.text(),files,values,values.get('knowledge_import_prompt',''))
                status.setText(('完成：新增 %d 条，等待审核（%s）' % (count,note)) if count else ('完成：没有新增条目（%s）' % note))
            except Exception as exc:
                status.setText('导入失败：%s' % exc); QMessageBox.warning(dialog,'文档导入失败',str(exc))
            finally:
                progress.setVisible(False); import_button.setEnabled(True); fetch.setEnabled(True)
            refresh()
        def pick_and_import():
            files,_chosen=QFileDialog.getOpenFileNames(dialog,'选择要导入的角色资料','','文本文件 (*.txt *.md *.markdown *.html *.htm);;所有文件 (*.*)')
            if files: asyncio.create_task(run_import(files))
        import_button.clicked.connect(pick_and_import)
        async def run_dedupe():
            dedupe_button.setEnabled(False); progress.setVisible(True); status.setText('正在检测重复条目（调用 API 做语义判断，稍等）…')
            method='API 语义查重'; note=''
            try:
                result=await kb.find_duplicates_ai(self.manager.config.data)
                groups=result['groups']
                if not result['checked']:
                    # 一次都没成功，别假装查过了——降级到字面相似度并如实说明。
                    groups=kb.find_duplicates(); method='字面相似度（API 查重不可用，已降级）'
                elif result['failed']:
                    note='；%d 个分类没查成（%s）' % (len(result['failed']), '、'.join(result['failed']))
            except Exception as exc:
                groups=kb.find_duplicates(); method='字面相似度（API 异常：%s）' % exc
            finally:
                progress.setVisible(False); dedupe_button.setEnabled(True)
            status.setText('重复检测完成（%s）：%d 组%s' % (method,len(groups),note))
            self._show_dedupe_result(kb,groups,method,note,refresh,dialog)
        dedupe_button.clicked.connect(lambda: asyncio.create_task(run_dedupe()))
        dialog.setModal(False); dialog.show(); dialog.raise_()

    def _voice_dialog(self):
        """语音输入设置。默认用 Windows 自带离线识别：零安装零 Key，开箱可用。"""
        if not self.manager: return
        from speech import probe
        c=self.manager.config; d=QDialog(self); d.setWindowTitle('语音输入设置'); root=QVBoxLayout(d); fit_dialog(d, 700, 400); f=QFormLayout()
        provider=QComboBox(); provider.addItem('本地离线识别 vosk（推荐：免 Key、不联网、比系统引擎准）','vosk'); provider.addItem('Windows 自带离线识别（免安装，但准确率较低）','windows'); provider.addItem('云端识别（OpenAI 兼容接口，最准，需要 Key）','openai'); provider.addItem('关闭语音输入','off')
        index=provider.findData(str(c.get('asr_provider','vosk') or 'vosk')); provider.setCurrentIndex(index if index>=0 else 0)
        language=QComboBox(); language.setEditable(True); language.addItems(['zh-CN','en-US','ja-JP']); language.setCurrentText(str(c.get('asr_language','zh-CN') or 'zh-CN'))
        url=QLineEdit(c.get('asr_url','') or ''); url.setPlaceholderText('如 https://api.siliconflow.cn')
        key=QLineEdit(c.get('asr_api_key','') or ''); key.setEchoMode(QLineEdit.EchoMode.Password)
        model=QLineEdit(c.get('asr_model','') or ''); model.setPlaceholderText('如 FunAudioLLM/SenseVoiceSmall')
        for widget, label in ((provider,'识别方式'),(language,'识别语言'),(url,'接口地址'),(key,'API Key'),(model,'模型名')):
            f.addRow(label, widget)
        root.addLayout(f)
        tip=QLabel('离线识别用的是系统自带引擎，中文机器通常已装好；云端识别可用硅基流动(有免费额度)、Groq 等。\n识别结果会先填进聊天输入框，你确认后再按回车发送。')
        tip.setWordWrap(True); tip.setStyleSheet('color:#5b6472'); root.addWidget(tip)
        status=QLabel(''); status.setWordWrap(True); status.setStyleSheet('color:#33415c;background:#f2f5fb;border-radius:6px;padding:8px'); root.addWidget(status)
        test=QPushButton('检测语音输入'); root.addWidget(test)
        box=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel)
        (getattr(d,'_outer_layout',None) or root).addWidget(box)
        def current_config():
            return {'asr_provider':provider.currentData(),'asr_language':language.currentText().strip(),
                    'asr_url':url.text().strip(),'asr_api_key':key.text().strip(),'asr_model':model.text().strip()}
        test.clicked.connect(lambda: status.setText(probe(current_config())))
        def save():
            c.update(current_config()); d.close()
        box.accepted.connect(save); box.rejected.connect(d.close)
        status.setText(probe(current_config()))
        d.setModal(False); d.show(); d.raise_()


    def _screen_dialog(self):
        """屏幕理解设置：独立视觉 API + 可开关 + 划定范围 / 锁定窗口。"""
        if not self.manager: return
        from screen_vision import VisionClient, capture_for_config, list_windows, virtual_desktop
        c=self.manager.config
        d=QDialog(self); d.setWindowTitle('屏幕理解设置（让角色看一眼屏幕）'); root=scrollable(d, 840, 780)

        enabled=QCheckBox('启用：角色会时不时看一眼屏幕，主动说一句话'); enabled.setChecked(bool(c.get('screen_enabled',False)))
        privacy=QLabel('隐私说明：截图只在内存里传给视觉模型，**不落盘**；识别结果也**不写进对话数据库**，'
                       '只用来让角色说一句。默认关闭；可以只截一块区域或锁定某个窗口。'
                       '用户刚说过话、或角色正在回复时，绝不会去看屏幕。')
        privacy.setWordWrap(True); privacy.setStyleSheet('color:#5a6478;background:#fff8e8;border:1px solid #f0dfb8;border-radius:6px;padding:8px')
        root.addWidget(enabled); root.addWidget(privacy)

        # ---------------- 独立视觉 API ----------------
        abox=QGroupBox('视觉 API（独立于聊天模型；能看图的模型通常不是 DeepSeek）'); af=QFormLayout(abox)
        provider=QComboBox(); provider.addItems(['compatible','openai']); provider.setCurrentText(str(c.get('screen_provider','compatible') or 'compatible'))
        base=QLineEdit(str(c.get('screen_base_url','') or '')); base.setPlaceholderText('例如 https://你的中转站域名（会补 /v1）')
        skey=QLineEdit(str(c.get('screen_api_key','') or '')); skey.setEchoMode(QLineEdit.EchoMode.Password); skey.setPlaceholderText('视觉模型的 Key')
        smodel=QComboBox(); smodel.setEditable(True); smodel.addItem(str(c.get('screen_model','') or '')); smodel.setCurrentText(str(c.get('screen_model','') or '')); smodel.setToolTip('视觉模型名；可以点「加载模型」从中继拉一份可用列表')
        stokens=QSpinBox(); stokens.setRange(32,8000); stokens.setValue(int(c.get('screen_max_tokens',200) or 200)); stokens.setToolTip('只要一句台词，200 足够；多模态图片本身会占不少 token')
        stimeout=QSpinBox(); stimeout.setRange(10,600); stimeout.setValue(int(c.get('screen_timeout',90) or 90)); stimeout.setSuffix(' 秒')
        reuse=QPushButton('沿用聊天 API 的地址/Key/模型'); load_models=QPushButton('加载模型')
        load_models.setToolTip('用下面填的地址和 Key 拉一份可用模型列表（也顺便验证 Key 是否有效）')
        arow=QHBoxLayout(); arow.addWidget(reuse); arow.addWidget(load_models); arow.addStretch(1)
        af.addRow('Provider', provider); af.addRow('地址', base); af.addRow('API Key', skey)
        af.addRow('模型', smodel); af.addRow('最大 token', stokens); af.addRow('超时', stimeout); af.addRow('', arow)
        root.addWidget(abox)

        # ---------------- 采集范围 ----------------
        rbox=QGroupBox('看哪一块（越窄越省心，也越不容易看到隐私）'); rf=QFormLayout(rbox)
        mode=QComboBox(); mode.addItem('整屏','full'); mode.addItem('划定区域','region'); mode.addItem('锁定窗口','window')
        window=QComboBox(); window.setEditable(True)
        refresh=QPushButton('刷新窗口列表')
        wrow=QHBoxLayout(); wrow.addWidget(window,1); wrow.addWidget(refresh)
        region_label=QLabel('（还没框选）'); pick=QPushButton('框选区域'); clear=QPushButton('清除区域')
        prow=QHBoxLayout(); prow.addWidget(region_label,1); prow.addWidget(pick); prow.addWidget(clear)
        maxedge=QSpinBox(); maxedge.setRange(320,4096); maxedge.setValue(int(c.get('screen_max_edge',1280) or 1280))
        maxedge.setToolTip('上传前把长边缩到这个尺寸：省 token，也少暴露画面细节')
        rf.addRow('范围', mode); rf.addRow('窗口', wrow); rf.addRow('区域', prow); rf.addRow('上传长边上限', maxedge)
        root.addWidget(rbox)

        # ---------------- 节奏 ----------------
        tbox=QGroupBox('节奏与提示词'); tf=QFormLayout(tbox)
        interval=QSpinBox(); interval.setRange(5,3600); interval.setValue(int(c.get('screen_interval',30) or 30)); interval.setSuffix(' 秒')
        interval.setToolTip('最短间隔：再闲也不会比这更频繁地看屏幕')
        idle=QSpinBox(); idle.setRange(0,3600); idle.setValue(int(c.get('screen_idle_seconds',45) or 45)); idle.setSuffix(' 秒')
        idle.setToolTip('用户多久没说话才允许主动看；用户一开口，屏幕观察立刻让位')
        only_change=QCheckBox('画面没变化就不重复看（省请求）'); only_change.setChecked(bool(c.get('screen_only_on_change',True)))
        replymode=QComboBox(); replymode.addItem('直接让视觉模型以角色口吻说（1 次调用）','direct'); replymode.addItem('先客观描述，再由角色模型说（2 次调用，人设更稳）','describe')
        replymode.setCurrentIndex(0 if str(c.get('screen_reply_mode','direct') or 'direct')=='direct' else 1)
        prompt=QTextEdit(str(c.get('screen_prompt','') or '')); prompt.setFixedHeight(64)
        prompt.setPlaceholderText('留空用默认：用角色口吻对屏幕上的事说一句很短的话，不要解说画面')
        hint=QLineEdit(str(c.get('screen_hint','') or '')); hint.setPlaceholderText('随图一起发过去的提示，可留空')
        tf.addRow('最短间隔', interval); tf.addRow('用户静默', idle); tf.addRow('', only_change)
        tf.addRow('台词来源', replymode); tf.addRow('系统提示', prompt); tf.addRow('附带提示', hint)
        root.addWidget(tbox)

        status=QLabel(); status.setWordWrap(True); status.setStyleSheet('color:#33415c;background:#f2f5fb;border-radius:6px;padding:8px'); root.addWidget(status)
        test=QPushButton('测试连接（真发一张小图）'); peek=QPushButton('现在看一眼并对我说一句')
        brow=QHBoxLayout(); brow.addWidget(test); brow.addWidget(peek); brow.addStretch(1); root.addLayout(brow)
        dbox=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel); root.addWidget(dbox)

        state={'region': list(c.get('screen_region') or []) or None}
        def current_mode():
            if state['region'] and mode.currentData()=='region': return 'region'
            return mode.currentData() or 'full'
        def refresh_mode_label():
            md=current_mode()
            if md=='window': text='锁定窗口：%s' % (window.currentText().strip() or '（还没选）')
            elif md=='region': text='划定区域：%s' % (state['region'] or '（还没框选）')
            else: text='整屏（整块主屏）'
            status.setText('当前范围：%s' % text)
            region_label.setText(str(state['region']) if state['region'] else '（还没框选）')
        def load_windows():
            items=list_windows(); window.clear(); window.addItem('')
            for item in items: window.addItem(item['title'])
            wanted=str(c.get('screen_window_title','') or '')
            if wanted: window.setCurrentText(wanted)
            return items
        def snapshot():
            md=current_mode()
            return {'screen_enabled':enabled.isChecked(),'screen_provider':provider.currentText(),
                    'screen_base_url':base.text().strip(),'screen_api_key':skey.text().strip(),
                    'screen_model':smodel.currentText().strip(),'screen_max_tokens':stokens.value(),
                    'screen_timeout':stimeout.value(),'screen_interval':interval.value(),
                    'screen_idle_seconds':idle.value(),'screen_only_on_change':only_change.isChecked(),
                    'screen_reply_mode':replymode.currentData(),'screen_prompt':prompt.toPlainText().strip(),
                    'screen_hint':hint.text().strip(),'screen_max_edge':maxedge.value(),
                    'screen_region':state['region'] if md=='region' else None,
                    'screen_window_title':window.currentText().strip() if md=='window' else ''}

        def pick_region():
            picker=RegionPicker(); picker.showFullScreen(); picker.raise_(); picker.activateWindow()
            self._picker=picker
            def done():
                if picker.result:
                    state['region']=list(picker.result); mode.setCurrentIndex(1)
                refresh_mode_label()
            picker.destroyed.connect(lambda *_: done())
            original_close=picker.closeEvent
            def close_event(event):
                original_close(event); done()
            picker.closeEvent=close_event

        def clear_region():
            state['region']=None; refresh_mode_label()

        def reuse_chat():
            """把聊天 API 的地址/Key/模型抄过来。

            地址有两个键位：`base_url`（新）和 `url`（实际在用、聊天那边一直写的是它）。
            以前只读 base_url，于是「沿用聊天 API」只填上了 Key 和模型、**地址是空的**。
            """
            chat_url=str(c.get('base_url','') or c.get('url','') or '')
            base.setText(chat_url)
            skey.setText(str(c.get('api_key','') or ''))
            smodel.setCurrentText(str(c.get('model','') or ''))
            want=str(c.get('provider','compatible') or 'compatible')
            if provider.findText(want)>=0: provider.setCurrentText(want)
            status.setText('已沿用聊天 API：\n地址 %s\nKey %s ｜ 模型 %s\n'
                           '（聊天模型不一定支持图片，点「加载模型」验 Key、再点「测试连接」验读图）'
                           % (chat_url or '（聊天配置里也没有地址，请手填）',
                              '已填入' if skey.text().strip() else '聊天里也是空的',
                              smodel.currentText() or '（空）'))

        def do_load_models():
            """拉一份可用模型列表；同时也顺手验证了地址 + Key 通不通。"""
            self._load_models(provider.currentText(), skey.text().strip(),
                              base.text().strip() or str(c.get('base_url','') or c.get('url','') or ''), smodel)

        async def do_test():
            test.setEnabled(False); status.setText('正在测试视觉 API（真发一张小图）…')
            try:
                ok, why = await VisionClient(snapshot()).probe()
                text=('视觉 API 可用：%s' % why) if ok else ('视觉 API 不可用：%s' % why)
                # 失败原因也要落到 app.log：以前只写在状态栏，关掉窗口就查不到了。
                (logging.getLogger('screen').info if ok else logging.getLogger('screen').warning)('屏幕理解测试连接：%s', text)
                status.setText(text)
            finally:
                test.setEnabled(True)

        async def do_peek():
            peek.setEnabled(False); status.setText('正在截屏并调用视觉模型…')
            try:
                client=VisionClient(snapshot())
                png, info, note=capture_for_config(snapshot())
                status.setText('截了 %s（%dx%d，%d 字节），正在问模型…' % (note, info['width'], info['height'], info['bytes']))
                text=await client.look(png, self.manager.screen_system_prompt(), hint.text().strip())
                status.setText('范围：%s\n模型说：%s\n（上面那句会直接进气泡并朗读，不会被写进数据库）' % (note, text))
                await self.manager.ambient_say(text)
            except Exception as exc:
                logging.getLogger('screen').warning('看一眼失败（范围/模型/Key 见配置）：%s', exc)
                status.setText('看一眼失败：%s' % exc)
            finally:
                peek.setEnabled(True)

        def save():
            values=snapshot(); c.update(values); d.close()
        def on_mode_change(*_a):
            if mode.currentData()=='window':
                if not window.count(): load_windows()
            refresh_mode_label()

        mode.currentIndexChanged.connect(on_mode_change)
        window.currentTextChanged.connect(lambda *_: refresh_mode_label())
        refresh.clicked.connect(lambda: (load_windows(), refresh_mode_label()))
        pick.clicked.connect(pick_region); clear.clicked.connect(clear_region)
        reuse.clicked.connect(reuse_chat); load_models.clicked.connect(do_load_models)
        test.clicked.connect(lambda: asyncio.create_task(do_test()))
        peek.clicked.connect(lambda: asyncio.create_task(do_peek()))
        dbox.accepted.connect(save); dbox.rejected.connect(d.close)

        # 初始状态：按配置把范围选到对应那一项
        if str(c.get('screen_window_title','') or '').strip(): mode.setCurrentIndex(2); load_windows()
        elif state['region']: mode.setCurrentIndex(1)
        else: mode.setCurrentIndex(0)
        refresh_mode_label()
        d.setModal(False); d.show(); d.raise_()

    def _idle_motion_dialog(self):
        """待机动作设置：鼠标长时间不动时，在「无八方向」的动作之间随机轮播。"""
        if not self.manager or not self.pet: return
        c=self.manager.config; pet=self.pet
        d=QDialog(self); d.setWindowTitle('待机动作设置'); root=QVBoxLayout(d); fit_dialog(d, 700, 600)
        hint=QLabel('无方向动作 = 帧直接放在 motions\\<动作名>\\ 下，不需要 N/E/S… 子目录。\n'
                    '鼠标一直不动超过「静止秒数」就切进待机动作、每隔「每个待机秒数」随机换一个；'
                    '鼠标一动立刻回到八方向的追踪动作，并保留当前朝向。')
        hint.setWordWrap(True); hint.setStyleSheet('color:#5a6478;background:#fafbfe;border:1px solid #e6eaf2;border-radius:6px;padding:8px')
        root.addWidget(hint)
        f=QFormLayout()
        track=QComboBox(); track.addItems(self.sheet.directional_motions or self.sheet.actions or ['idle'])
        track.setCurrentText(pet.tracking_motion if pet.tracking_motion in self.sheet.directional_motions else (self.sheet.directional_motions[0] if self.sheet.directional_motions else 'idle'))
        track.setToolTip('鼠标在动时用这套（必须是画了八方向的动作）')
        f.addRow('追踪动作（八方向）', track)
        after=QDoubleSpinBox(); after.setRange(3,3600); after.setDecimals(1); after.setValue(float(pet.idle_after_seconds)); after.setSuffix(' 秒')
        f.addRow('静止多久进入待机', after)
        each=QDoubleSpinBox(); each.setRange(1,3600); each.setDecimals(1); each.setValue(float(pet.idle_motion_seconds)); each.setSuffix(' 秒')
        f.addRow('每个待机播多久', each)
        root.addLayout(f)

        box=QGroupBox('待机动作（一个都不勾 = 自动使用全部无方向动作）'); bf=QVBoxLayout(box)
        listing=QListWidget()
        chosen=set(pet.idle_motions or [])
        bf.addWidget(listing)
        row=QHBoxLayout(); new_flat=QPushButton('新建无方向动作'); refresh=QPushButton('刷新列表'); tryit=QPushButton('立刻试一下待机')
        row.addWidget(new_flat); row.addWidget(refresh); row.addWidget(tryit); row.addStretch(1); bf.addLayout(row)
        root.addWidget(box)
        status=QLabel(''); status.setWordWrap(True); status.setStyleSheet('color:#33415c;background:#f2f5fb;border-radius:6px;padding:8px'); root.addWidget(status)
        dbox=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel); root.addWidget(dbox)

        def reload_list():
            """列出**所有**动作，并说明哪些能当待机、哪些为什么不能。

            只列能用的动作会让人一头雾水（「我新建的蝴蝶怎么不见了」）——
            直接把原因摊开：还没有帧 / 是八方向动作。
            """
            listing.clear()
            flat_now=self.sheet.flat_motions
            for name in self.sheet.actions:
                kind=self.sheet.motion_kind(name)
                base=self.sheet.root/"motions"/name
                frames=len(self.sheet._flat_frames(base))
                if kind=='flat':
                    text='%s    （%d 帧，可以当待机）' % (name, frames)
                elif kind=='directional':
                    text='%s    （八方向动作，不能当待机）' % name
                else:
                    text='%s    （还没有帧：先选中它 →「＋ 导入 PNG 帧」）' % name
                item=QListWidgetItem(text); item.setData(Qt.ItemDataRole.UserRole, name)
                if kind=='flat':
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Checked if (not chosen or name in chosen) else Qt.CheckState.Unchecked)
                else:
                    # QListWidgetItem 默认就带 ItemIsUserCheckable，必须显式去掉，
                    # 否则「不能用的动作」也能打勾、还会被当成待机存进配置。
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                    item.setForeground(QColor('#98a2b3'))
                listing.addItem(item)
            status.setText('可当待机的无方向动作：%s ｜ 全部动作：%s'
                           % ('、'.join(flat_now) or '无', '、'.join(self.sheet.actions) or '无'))
        def do_new():
            name=self.create_flat_action()
            if name:
                status.setText('已新建「%s」，右键动作列表切过去再导入帧即可。' % name)
                reload_list()
        def save():
            picked=[listing.item(i).data(Qt.ItemDataRole.UserRole) for i in range(listing.count())
                    if listing.item(i).checkState()==Qt.CheckState.Checked and listing.item(i).data(Qt.ItemDataRole.UserRole)]
            values={'tracking_motion':track.currentText().strip(),'idle_motions':picked,
                    'idle_after_seconds':after.value(),'idle_motion_seconds':each.value()}
            c.update(values)
            pet.tracking_motion=values['tracking_motion']; pet.idle_motions=picked
            pet.idle_after_seconds=values['idle_after_seconds']; pet.idle_motion_seconds=values['idle_motion_seconds']
            pool=pet.refresh_idle_plan()
            status.setText('已保存。待机动作池：%s' % ('、'.join(pool) or '（空——不会进待机模式）'))
            pet.restart_stillness()
            d.close()
        def do_try():
            """立刻切到待机动作看一眼，不用干等静止计时。"""
            if not pet._idle_pool:
                status.setText('还没识别到无方向动作：先「刷新列表」，确认动作里有帧（帧直接放在动作目录下）。'); return
            name=pet.preview_idle_motion()
            status.setText(('已切到待机动作「%s」，看桌面上的角色；鼠标一动就会自动回到 %s。'
                            % (name, pet.tracking_motion)) if name else '没切成，请看 app.log 里 pet 开头的日志。')
        new_flat.clicked.connect(do_new); refresh.clicked.connect(reload_list); tryit.clicked.connect(do_try)
        dbox.accepted.connect(save); dbox.rejected.connect(d.close)
        reload_list()
        d.setModal(False); d.show(); d.raise_()

    def _tts_dialog(self):
        """语音合成设置：云端（百炼 Qwen-TTS）或本地（GPT-SoVITS）二选一。"""
        if not self.manager: return
        from voice import (AUDIO_TTS_INSTRUCTION_LIMIT, AUDIO_TTS_LANGS, AUDIO_TTS_MODELS,
                           AUDIO_TTS_VOICES, GPTSOVITS_LANGS, GPTSOVITS_SPLIT_METHODS, LOCAL_TTS_URL,
                           SYSTEM_VOICES, AudioVoiceCloner, SpeechPlayer, VoiceCloner, VoiceDesigner,
                           gptsovits_start_command, instruction_cost, list_all_voices, probe,
                           probe_clone_audio, split_sentences)
        c=self.manager.config; d=QDialog(self); d.setWindowTitle('语音合成设置（角色说话）'); root=scrollable(d, 800, 820)

        def persona_tts_block():
            """读角色层（prompts/persona.json 的 tts 块）。

            语气有三层（角色 / 全局 / 单句），角色层优先。这个框只管全局层，
            所以角色层写了什么必须显示出来——不然用户会以为「我填的语气怎么没了」。
            """
            try:
                path=getattr(self.manager,'persona',None)
                if path and Path(path).exists():
                    data=json.loads(Path(path).read_text(encoding='utf-8')) or {}
                    block=data.get('tts') or {}
                    return {k:str(v).strip() for k,v in block.items() if str(v).strip()}
            except Exception:
                logging.getLogger('voice').warning('读 persona 的 tts 块失败', exc_info=True)
            return {}
        persona_tone=persona_tts_block()

        def persona_note():
            """角色层语气的说明标签（没有就返回空标签）。"""
            if not persona_tone: return None
            parts=[]
            if persona_tone.get('instructions'):
                parts.append('语气：%s' % persona_tone['instructions'])
            if persona_tone.get('voice'): parts.append('音色：%s' % persona_tone['voice'])
            if persona_tone.get('model'): parts.append('模型：%s' % persona_tone['model'])
            if not parts: return None
            label=QLabel('角色层（prompts/persona.json 的 tts 块）现在写着 —— %s\n'
                         '**角色层优先于这个框**：想改就改那个文件，或者把它清空再用这里。'
                         % '；'.join(parts))
            label.setWordWrap(True)
            label.setStyleSheet('color:#33415c;background:#eef2fb;border:1px solid #d6ddf0;border-radius:6px;padding:6px')
            return label
        enabled=QCheckBox('启用：角色回复时朗读出来'); enabled.setChecked(bool(c.get('tts_enabled',False)))
        provider=QComboBox()
        provider.addItem('云端 · 阿里云百炼 Qwen-TTS（音色多、快，按量计费）','qwen')
        provider.addItem('云端 · Qwen-Audio-TTS（语气最强，可复刻音色；音色不通用）','qwenaudio')
        provider.addItem('本地 · GPT-SoVITS（自己的服务，免费离线，CPU 慢）','gptsovits')
        want=str(c.get('tts_provider','qwen') or 'qwen').lower()
        if want in ('gptsovits','local','gpt-sovits'): provider.setCurrentIndex(2)
        elif want in ('qwenaudio','audio','qwen-audio','qwen_audio'): provider.setCurrentIndex(1)
        else: provider.setCurrentIndex(0)
        volume=QSpinBox(); volume.setRange(0,100); volume.setValue(int(c.get('tts_volume',80)))
        common=QFormLayout()
        for widget, label in ((enabled,'启用'),(provider,'合成方式'),(volume,'音量')):
            common.addRow(label, widget)
        root.addLayout(common)

        # 试听模块：音色（含刚复刻/设计出来的）选好之后，随便打一段字就能听效果。
        # 放在音色表单正下方，就是为了「选音色 → 试听」这一步不用来回翻。
        abox=QGroupBox('试听 · 输入文本，用上面选的音色读出来'); af=QVBoxLayout(abox)
        audition=QTextEdit(str(c.get('tts_audition_text','')) or '你好呀，我是回末李花子，请多指教。')
        audition.setFixedHeight(56); audition.setPlaceholderText('想听什么就打什么，改完点右边的「用当前音色试听」')
        arow=QHBoxLayout(); test=QPushButton('检测'); play=QPushButton('用当前音色试听')
        arow.addWidget(test); arow.addWidget(play); arow.addStretch(1)
        af.addWidget(audition); af.addLayout(arow)
        root.addWidget(abox)

        # 语气标注提示词：这是**独立的一块**，和 pages 里的「怎么读」不是一回事——
        # 它教模型「按上下文给每一句标一个语气」，所以你改它就能改模型的标注习惯。
        # 放在 stack 外面，因为它是给聊天模型看的，跟合成方式无关。
        from conversation_manager import ConversationManager
        builtin_default=ConversationManager.DEFAULT_TONE_PROMPT
        tpbox=QGroupBox('语气标注提示词（每次对话都发给模型 · 和合成方式无关）'); tpf=QVBoxLayout(tpbox)
        tpenabled=QCheckBox('每次对话都注入这一段'); tpenabled.setChecked(bool(c.get('tone_prompt_enabled',True)))
        tpenabled.setToolTip('勾上=每轮都发。它让模型主动按上下文给每一句标语气（同一句话在不同情境下可以标出完全不同的语气）。\n'
                             '关掉=不教它写标记，回复里就不会出现【语气:…】，语音只用下面那条默认语气。')
        tp=QTextEdit(str(c.get('tone_prompt','') or '').strip() or builtin_default)
        tp.setFixedHeight(120)
        tp.setToolTip('这就是那段「每次都发」的提示词，可以随便改。\n'
                      '标出来的语气会用在语音合成上；模型不该把它显示给你，也不会被读出来。\n'
                      '想按角色区分，也可以写进 prompts/persona.json 的 tts.tone_prompt（那里优先）。')
        tprow=QHBoxLayout(); tpreset=QPushButton('恢复内置默认'); tpcost=QLabel()
        tpreset.setToolTip('把上面的内容换回内置的默认提示词')
        tprow.addWidget(tpreset); tprow.addWidget(tpcost); tprow.addStretch(1)
        def sync_tp_count(*_a):
            text=tp.toPlainText().strip()
            tpcost.setText('共 %d 字（约 %d token）' % (len(text), max(1, len(text)//2)))
            tpcost.setStyleSheet('color:#5a6478')
        tp.textChanged.connect(sync_tp_count); sync_tp_count()
        tpreset.clicked.connect(lambda: tp.setPlainText(builtin_default))
        tphint=QLabel('本机实测（用你现在的聊天模型跑的两遍，只换上下文）：同一句「这样的结局，真是让人遗憾」，'
                      '玩家赢了时标成「小声不甘」，玩家说「我又失败了」时标成「低落」——'
                      '合成出来 3.36 秒 vs 4.56 秒。语气由模型按上下文判断，不是写死的。\n'
                      '⚠ 标出来的语气**只有认语气的合成方式才会生效**：Qwen-Audio-TTS / qwen3-tts-instruct-flash 认；'
                      '本地 GPT-SoVITS 不认（标记会从正文里去掉，但读出来没有变化）。')
        tphint.setWordWrap(True)
        tphint.setStyleSheet('color:#1f5130;background:#f2fbf4;border:1px solid #cfe9d6;border-radius:6px;padding:6px')
        tpf.addWidget(tpenabled); tpf.addWidget(tp); tpf.addLayout(tprow); tpf.addWidget(tphint)
        root.addWidget(tpbox)

        status=QLabel(); status.setWordWrap(True); status.setStyleSheet('color:#33415c;background:#f2f5fb;border-radius:6px;padding:8px'); root.addWidget(status)

        # ---------------- 云端页 ----------------
        page_cloud=QWidget(); cloud_layout=QVBoxLayout(page_cloud); cloud_layout.setContentsMargins(0,0,0,0)
        f=QFormLayout()
        key=QLineEdit(c.get('tts_api_key','') or ''); key.setEchoMode(QLineEdit.EchoMode.Password); key.setPlaceholderText('阿里云百炼（DashScope）API Key')
        model=QComboBox(); model.setEditable(True); model.addItems(['qwen3-tts-flash','qwen3-tts-instruct-flash','qwen3-tts-vd-2026-01-26']); model.setCurrentText(str(c.get('tts_model','qwen3-tts-flash')))
        model.setToolTip('普通朗读用 qwen3-tts-flash；用「自建音色」时必须选建音色时的驱动模型（qwen3-tts-vd-2026-01-26）')
        voice=QComboBox(); voice.setEditable(True)
        for name, desc in SYSTEM_VOICES: voice.addItem('%s（%s）' % (name, desc), name)
        current=str(c.get('tts_voice','Cherry')); found=voice.findData(current)
        if found>=0: voice.setCurrentIndex(found)
        else: voice.setCurrentText(current)
        for widget, label in ((key,'百炼 API Key'),(model,'合成模型'),(voice,'音色')):
            f.addRow(label, widget)
        cloud_layout.addLayout(f)

        # 语气：全局默认的那一条。想让角色「按回复内容自己换语气」，模型那边还要会写
        # [语气:xxx] 标记——只有填了这里，系统提示里才会教它写。
        tbox=QGroupBox('语气（怎么说话，不是说什么）'); tf=QVBoxLayout(tbox)
        tone=QLineEdit(str(c.get('tts_instructions','') or ''))
        tone.setPlaceholderText('例：语气温柔偏冷淡，语速稍慢，句尾轻微上扬')
        tone.setToolTip('留空 = 不带语气，跟以前一样。填了之后：① 合成时整体套用这条；'
                        '② 系统提示里会允许模型用 [语气:说明] 逐句改语气（标记不显示、不朗读）')
        optimize=QCheckBox('让模型自己润色这条语气描述（optimize_instructions）')
        optimize.setChecked(bool(c.get('tts_optimize_instructions',False)))
        optimize.setToolTip('打开后服务端会先把你写的语气改写成它更容易执行的版本，稍慢一点点；填得比较口语时有用')
        thint=QLabel('实测结论（同一句话各发两次，按「区间是否重叠」比时长，不是看单次差值）：\n'
                     '· qwen3-tts-instruct-flash：不加语气 3.60~3.76 秒，「缓慢、拖长」5.92~6.48 秒，'
                     '「快、急促」2.64~2.80 秒 —— **认语气**（开不开润色都认）\n'
                     '· qwen3-tts-flash：不加 3.28~3.92 秒，缓慢 3.28~3.36 秒 —— 区间重叠，**不认**\n'
                     '· qwen3-tts-vc-2026-01-22（复刻音色）：不加 3.76~4.16 秒，缓慢 3.68~4.80 秒 —— '
                     '**不认**，接口收下这个字段但不用它\n'
                     '所以想用语气，就得把「合成模型」换成 qwen3-tts-instruct-flash 并配系统音色；'
                     '复刻出来的音色暂时没有语气（要两者兼得只能用 Qwen-Audio-TTS，接口地址不一样）。')
        thint.setWordWrap(True)
        thint.setStyleSheet('color:#8a5a12;background:#fffaf0;border:1px solid #f0e0c0;border-radius:6px;padding:6px')
        tf.addWidget(tone); tf.addWidget(optimize); tf.addWidget(thint)
        cloud_persona_note=persona_note()
        if cloud_persona_note is not None: tf.addWidget(cloud_persona_note)
        cloud_layout.addWidget(tbox)
        box=QGroupBox('自建音色 · 声音设计（用一句话描述生成专属音色，不用录音）'); bf=QFormLayout(box)
        prompt=QLineEdit(str(c.get('tts_design_prompt',''))); prompt.setPlaceholderText('例：年轻女性，语速偏快，语调上扬，清亮活泼，适合动画角色')
        preview=QLineEdit(str(c.get('tts_design_preview',''))); preview.setPlaceholderText('必填；创建音色时用它生成预览音频')
        preview.setToolTip('只在「创建音色」时用来生成预览音频；日常试听请用上面的试听框')
        target=QLineEdit(str(c.get('tts_design_target_model','qwen3-tts-vd-2026-01-26')))
        target.setToolTip('建出来的音色由这个模型驱动，合成时必须用同一个模型名，否则会失败')
        create=QPushButton('创建音色并试听'); listing=QPushButton('列出我的自建音色')
        brow=QHBoxLayout(); brow.addWidget(create); brow.addWidget(listing); brow.addStretch(1)
        bf.addRow('声音描述', prompt); bf.addRow('预览文本', preview); bf.addRow('驱动模型', target); bf.addRow('', brow)
        cloud_layout.addWidget(box)

        # 声音复刻：手里已经有录音时走这条。上传的是本地文件（base64），不需要先传到公网。
        cbox=QGroupBox('自建音色 · 声音复刻（用你手里的录音，10~20 秒最合适）'); cf=QFormLayout(cbox)
        clonefile=QLineEdit(str(c.get('tts_clone_file',''))); clonefile.setPlaceholderText('选一段 WAV / MP3 / M4A；建议 10~20 秒、安静环境、单人朗读')
        pick=QPushButton('选择文件…'); cfrow=QHBoxLayout(); cfrow.addWidget(clonefile,1); cfrow.addWidget(pick)
        clonename=QLineEdit(str(c.get('tts_clone_name','lilyz'))); clonename.setToolTip('音色名前缀，只能数字、字母、下划线，不超过 16 个字符')
        clonetarget=QLineEdit(str(c.get('tts_clone_target_model','qwen3-tts-vc-2026-01-22')))
        clonetarget.setToolTip('复刻出来的音色由这个模型驱动，合成时必须用同一个模型名；不要用 realtime 版（那要 WebSocket）')
        clonetext=QLineEdit(str(c.get('tts_clone_text',''))); clonetext.setPlaceholderText('可选：录音里读的内容，填了相似度更好（必须与音频完全一致）')
        checkclone=QPushButton('体检这段录音'); clone=QPushButton('用这段录音创建音色并试听')
        crow=QHBoxLayout(); crow.addWidget(checkclone); crow.addWidget(clone); crow.addStretch(1)
        cf.addRow('录音文件', cfrow); cf.addRow('音色名', clonename); cf.addRow('驱动模型', clonetarget)
        cf.addRow('录音文本', clonetext); cf.addRow('', crow)
        cloud_layout.addWidget(cbox)

        # ---------------- 本地页（GPT-SoVITS）----------------
        page_local=QWidget(); local_layout=QVBoxLayout(page_local); local_layout.setContentsMargins(0,0,0,0)
        lf=QFormLayout()
        ghome=QLineEdit(str(c.get('gptsovits_home','') or '')); ghome.setPlaceholderText(r'整合包根目录，例如 D:\GPT-SoVITS')
        ghome.setToolTip('用来点「启动本地服务」找 runtime\\python.exe；只填路径，不写入那边任何文件')
        gurl=QLineEdit(str(c.get('gptsovits_url','') or LOCAL_TTS_URL)); gurl.setToolTip('api_v2.py 的地址，默认 http://127.0.0.1:9880')
        gref=QLineEdit(str(c.get('gptsovits_ref_audio','') or '')); gref.setPlaceholderText('参考音频（3~10 秒、干净单人声最好）')
        gref.setToolTip('注意：这是「服务端本机路径」。api_v2 没有开放上传音频的接口，所以文件要放在服务端能读到的地方（同一台机器给绝对路径即可）')
        gpick=QPushButton('选择…'); grow=QHBoxLayout(); grow.addWidget(gref,1); grow.addWidget(gpick)
        gprompt=QLineEdit(str(c.get('gptsovits_prompt_text','') or '')); gprompt.setPlaceholderText('可留空（ref_free）；填了音色更准，但必须和参考音频一字不差')
        gplang=QComboBox(); gplang.setEditable(True)
        for code, name in GPTSOVITS_LANGS: gplang.addItem('%s（%s）' % (code, name), code)
        gplang.setCurrentText(str(c.get('gptsovits_prompt_lang','zh') or 'zh'))
        gplang.setToolTip('参考音频说的语言。游戏原声一般是日文，那就选 ja；留空参考文本时影响不大')
        glang=QComboBox(); glang.setEditable(True)
        for code, name in GPTSOVITS_LANGS: glang.addItem('%s（%s）' % (code, name), code)
        glang.setCurrentText(str(c.get('gptsovits_text_lang','zh') or 'zh')); glang.setToolTip('要合成的语言，读中文台词就选 zh')
        gspeed=QDoubleSpinBox(); gspeed.setRange(0.5,2.0); gspeed.setSingleStep(0.05); gspeed.setDecimals(2); gspeed.setValue(float(c.get('gptsovits_speed',1.0) or 1.0))
        gsplit=QComboBox(); gsplit.addItems(list(GPTSOVITS_SPLIT_METHODS)); gsplit.setCurrentText(str(c.get('gptsovits_split_method','cut5') or 'cut5'))
        gsplit.setToolTip('长文本切句方式，一般 cut5；整段不切用 cut0')
        gseed=QSpinBox(); gseed.setRange(-1,2147483647); gseed.setValue(int(c.get('gptsovits_seed',-1) if c.get('gptsovits_seed',-1) is not None else -1))
        gseed.setToolTip('-1 = 每次随机（语气自然些）；固定值可复现同一段语音')
        gtimeout=QSpinBox(); gtimeout.setRange(30,1800); gtimeout.setValue(int(c.get('gptsovits_timeout',300) or 300)); gtimeout.setSuffix(' 秒')
        gtimeout.setToolTip('CPU 推理很慢，长句要给足时间')
        for widget, label in ((ghome,'整合包目录'),(gurl,'服务地址'),(gref,'参考音频'),(gprompt,'参考文本'),
                              (gplang,'参考语言'),(glang,'合成语言'),(gspeed,'语速'),(gsplit,'切句方式'),
                              (gseed,'随机种子'),(gtimeout,'超时')):
            lf.addRow(label, grow if widget is gref else widget)
        local_layout.addLayout(lf)
        gbtns=QHBoxLayout()
        gstart=QPushButton('启动本地服务'); gstop=QPushButton('停止服务'); gcheck=QPushButton('只要端口检测')
        gbtns.addWidget(gstart); gbtns.addWidget(gstop); gbtns.addWidget(gcheck); gbtns.addStretch(1)
        local_layout.addLayout(gbtns)
        gweights=QHBoxLayout()
        gweights.addWidget(QLabel('切换权重（可选）'))
        gptw=QLineEdit(str(c.get('gptsovits_gpt_weights','') or '')); gptw.setPlaceholderText('GPT 权重，如 GPT_weights_v2Pro/xxx.ckpt')
        svw=QLineEdit(str(c.get('gptsovits_sovits_weights','') or '')); svw.setPlaceholderText('SoVITS 权重，如 SoVITS_weights_v2Pro/xxx.pth')
        gapply=QPushButton('加载模型（应用权重）')
        gapply.setToolTip('让本地服务加载上面填的 GPT / SoVITS 权重（等于调 /set_gpt_weights、/set_sovits_weights）；'
                          '两个框都空就什么也不做')
        gweights.addWidget(gptw,2); gweights.addWidget(svw,2); gweights.addWidget(gapply)
        local_layout.addLayout(gweights)
        ghint=QLabel('本地要点：① 服务要先开着（「启动本地服务」或整合包里的 go-api.bat），加载模型要几十秒、约 3 GB 内存；'
                     '② 参考音频走的是服务端本机路径，不经过上传；③ CPU 推理慢，桌宠会**按句合成、边合成边播**，第一句先出声；'
                     '④ 整合包默认加载 v2，要用自己训的 v2Pro 就填上面的权重再点「应用权重」。')
        ghint.setWordWrap(True); ghint.setStyleSheet('color:#5a6478;background:#fafbfe;border:1px solid #e6eaf2;border-radius:6px;padding:8px')
        local_layout.addWidget(ghint)
        local_layout.addStretch(1)

        # ---------------- Qwen-Audio-TTS 页 ----------------
        # 和上面那页是**两套接口**：参数名、音色、复刻方式都不一样，所以单独一页，
        # 而不是在同一页里塞 if。实测这条路对语气最敏感（3.2 秒 → 8.9 秒）。
        page_audio=QWidget(); audio_layout=QVBoxLayout(page_audio); audio_layout.setContentsMargins(0,0,0,0)
        af=QFormLayout()
        akey=QLineEdit(str(c.get('audio_tts_api_key','') or '')); akey.setEchoMode(QLineEdit.EchoMode.Password)
        akey.setPlaceholderText('留空 = 直接沿用上面 Qwen-TTS 那条 Key（通常就是同一个）')
        areuse=QPushButton('用上面那条 Key'); areuse.setToolTip('把 Qwen-TTS 页里填的 Key 复制过来')
        akeyrow=QHBoxLayout(); akeyrow.addWidget(akey,1); akeyrow.addWidget(areuse)
        aws=QLineEdit(str(c.get('audio_tts_workspace','') or ''))
        aws.setPlaceholderText('可留空')
        aws.setToolTip('业务空间 ID，形如 llm-xxxxxxxx。官方推荐用它组专属域名，'
                       '但官方也说明「现有域名仍可正常使用」——本机实测留空（走通用域名）接口正常，'
                       '所以不填也行')
        amodel=QComboBox()
        for name, desc in AUDIO_TTS_MODELS: amodel.addItem('%s（%s）' % (name, desc), name)
        amodel.setCurrentText(str(c.get('audio_tts_model','qwen-audio-3.0-tts-flash') or 'qwen-audio-3.0-tts-flash'))
        amodel.setToolTip('flash 更便宜；plus 音质与表现力更好。两者的音色表不同，别混用')
        avoice=QComboBox(); avoice.setEditable(True)
        for name, desc in AUDIO_TTS_VOICES: avoice.addItem('%s（%s）' % (name, desc), name)
        acurrent=str(c.get('audio_tts_voice','longanfengyue') or 'longanfengyue')
        afound=avoice.findData(acurrent)
        if afound>=0: avoice.setCurrentIndex(afound)
        else: avoice.setCurrentText(acurrent)
        avoice.setToolTip('这条路的音色和 Qwen-TTS 那批**不通用**（Cherry 之类在这里会报 411）。'
                          '另有 500 多个基础音色，名字形如 qwen-audio-3.0-tts-flash-xxx，'
                          '官方文档给了 Excel 清单和试听包')
        arate=QDoubleSpinBox(); arate.setRange(0.5,2.0); arate.setSingleStep(0.05); arate.setDecimals(2)
        arate.setValue(float(c.get('audio_tts_rate',1.0) or 1.0)); arate.setToolTip('语速倍数，1.0 = 原速')
        apitch=QDoubleSpinBox(); apitch.setRange(0.5,2.0); apitch.setSingleStep(0.05); apitch.setDecimals(2)
        apitch.setValue(float(c.get('audio_tts_pitch',1.0) or 1.0))
        apitch.setToolTip('音调倍数。注意降调会让整段变长、升调会变短（官方说明）')
        avol=QSpinBox(); avol.setRange(0,100); avol.setValue(int(c.get('audio_tts_volume',50) if c.get('audio_tts_volume',50) is not None else 50))
        avol.setToolTip('这一页的音量是「合成时」的音量（和上面那个播放器音量是两回事）')
        asr=QComboBox()
        for value in (16000,22050,24000,44100,48000): asr.addItem('%d Hz' % value, value)
        asr.setCurrentText('%d Hz' % int(c.get('audio_tts_sample_rate',24000) or 24000))
        afmt=QComboBox()
        for value in ('wav','mp3'): afmt.addItem(value, value)
        afmt.setCurrentText(str(c.get('audio_tts_format','wav') or 'wav'))
        alang=QComboBox()
        for code, name in AUDIO_TTS_LANGS: alang.addItem('%s（%s）' % (code, name), code)
        alang.setCurrentText('%s（%s）' % (str(c.get('audio_tts_language_hints','zh') or 'zh'),
                                           dict(AUDIO_TTS_LANGS).get(str(c.get('audio_tts_language_hints','zh') or 'zh'),'')))
        alang.setToolTip('提示模型用哪种语言读，中英混排或数字念错时有用')
        aseed=QSpinBox(); aseed.setRange(0,65535); aseed.setValue(int(c.get('audio_tts_seed',0) or 0))
        arandom=QCheckBox('每次随机（不固定种子）'); arandom.setChecked(bool(c.get('audio_tts_random_seed',False)))
        aseed.setToolTip('固定种子 = 同样的文字每次得到同样的音频；桌宠一般希望有点变化')
        arow2=QHBoxLayout(); arow2.addWidget(aseed); arow2.addWidget(arandom); arow2.addStretch(1)
        for widget, label in ((akeyrow,'百炼 API Key'),(aws,'业务空间 ID'),(amodel,'合成模型'),(avoice,'音色'),
                              (arate,'语速'),(apitch,'音调'),(avol,'合成音量'),(asr,'采样率'),
                              (afmt,'音频格式'),(alang,'语言'),(arow2,'随机种子')):
            af.addRow(label, widget)
        audio_layout.addLayout(af)

        atbox=QGroupBox('语气 · 这条路最值得用的功能'); atf=QVBoxLayout(atbox)
        atone=QLineEdit(str(c.get('tts_instructions','') or ''))
        atone.setPlaceholderText('例：语速偏慢、字句轻柔、尾音轻落，带一点疏离感')
        atone.setToolTip('这一页的「语气」和 Qwen-TTS 页共用同一份设置（tts_instructions），'
                         '切换合成方式不用担心要重填')
        acount=QLabel()
        def sync_count(*_a):
            text=atone.text().strip()
            cost=int(instruction_cost(text))
            over=cost>AUDIO_TTS_INSTRUCTION_LIMIT
            acount.setText('已用 %d / %d 字%s' % (cost, AUDIO_TTS_INSTRUCTION_LIMIT,
                                                 '（超了会被截断，日志里会写）' if over else ''))
            acount.setStyleSheet('color:%s' % ('#b3261e' if over else '#5a6478'))
        atone.textChanged.connect(sync_count); sync_count()
        athint=QLabel('本机实测（同一句话各发两次，比时长区间）：不加语气 3.2/3.2 秒，'
                      '「缓慢、拖长」8.88/9.2 秒，「快、急促」2.24/2.24 秒 —— 语气在这条路上**明显生效**，'
                      '系统音色和复刻音色都认。限制：指令最长 100 字（汉字算 2 字）。')
        athint.setWordWrap(True)
        athint.setStyleSheet('color:#1f5130;background:#f2fbf4;border:1px solid #cfe9d6;border-radius:6px;padding:6px')
        atf.addWidget(atone); atf.addWidget(acount); atf.addWidget(athint)
        audio_persona_note=persona_note()
        if audio_persona_note is not None: atf.addWidget(audio_persona_note)
        audio_layout.addWidget(atbox)

        aclbox=QGroupBox('声音复刻（Qwen-Audio-TTS 专用：这里建的音色只属于这个模型）'); aclf=QFormLayout(aclbox)
        aclfile=QLineEdit(str(c.get('audio_tts_clone_file','') or ''))
        aclfile.setPlaceholderText('选一段 10~20 秒的干净人声（WAV / MP3 / M4A）')
        aclfile.setToolTip('本地文件直接可用：我会整理成 单声道/16bit/24kHz 再用 base64 传上去。\n'
                           '文档只写了「公网 URL」，但**实测 base64 的 data: 地址照样 200 建出音色**，'
                           '所以不用先把录音传到公网')
        aclpick=QPushButton('选择文件…')
        aclfrow=QHBoxLayout(); aclfrow.addWidget(aclfile,1); aclfrow.addWidget(aclpick)
        aclurl=QLineEdit(str(c.get('audio_tts_clone_url','') or ''))
        aclurl.setPlaceholderText('可选：如果录音已经在公网，直接填 http/https 地址')
        aclprefix=QLineEdit(str(c.get('audio_tts_clone_prefix','myvoice')) if c.get('audio_tts_clone_prefix','myvoice') else 'myvoice')
        aclprefix.setToolTip('音色名前缀：只能字母数字，最多 10 个字符；'
                             '建出来的音色形如 qwen-audio-3.0-tts-flash-<前缀>-<随机串>')
        aclhint=QLabel('本机实测（2026-09-13）：本地录音 → base64 → 音色建成功，'
                       '并且**复刻音色同样认语气**——同一句台词，不加语气 2.56 秒，'
                       '加「缓慢、拖长」7.84 秒。\n'
                       '两点注意：① 音色**不跨模型**，这条路上建的音色换到 Qwen-TTS 用不了，反之亦然；'
                       '② 官方文档说 url 必须公网可访问，base64 是我实测出来的做法，官方没写。')
        aclhint.setWordWrap(True)
        aclhint.setStyleSheet('color:#1f5130;background:#f2fbf4;border:1px solid #cfe9d6;border-radius:6px;padding:6px')
        aclbtn=QPushButton('用这段录音建音色'); aclbtn.setToolTip('成功后会把新音色填到上面的「音色」框里')
        aclf.addRow('录音文件', aclfrow); aclf.addRow('或公网地址', aclurl)
        aclf.addRow('音色前缀', aclprefix)
        aclf.addRow('', aclbtn); aclf.addRow('', aclhint)
        audio_layout.addWidget(aclbox)
        audio_layout.addStretch(1)

        stack=QStackedWidget(); stack.addWidget(page_cloud); stack.addWidget(page_audio); stack.addWidget(page_local); root.addWidget(stack)
        def sync_provider(*_a):
            # 只换页，**不要**再 d.resize()：那会把用户刚拖好的窗口大小顶掉。
            data=provider.currentData()
            stack.setCurrentIndex(2 if data=='gptsovits' else (1 if data=='qwenaudio' else 0))
        provider.currentIndexChanged.connect(sync_provider); sync_provider()
        areuse.clicked.connect(lambda: akey.setText(key.text().strip()))

        # 按钮行挂在对话框自己的布局上（不跟着滚动区走），永远贴在底部看得见。
        dbox=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel)
        outer=getattr(d,'_outer_layout',None)
        (outer or root).addWidget(dbox)

        def chosen_voice():
            """当前到底选了哪个音色。

            可编辑下拉框有个坑：用 setCurrentText() 填一个不在列表里的名字（自建/复刻音色）
            时，编辑框显示新名字，但 currentData() 还是旧索引的 data（默认 Cherry）。
            直接用 currentData() 会「界面显示复刻音色、实际拿 Cherry 去合成」，
            保存时还会把配置里的复刻音色悄悄改回 Cherry。
            所以：列表项认 data（文本形如「名字（说明）」），手填/插进来的名字认文本。
            """
            text = voice.currentText().strip()
            data = str(voice.currentData() or '').strip()
            if data and (text == data or text.startswith(data + '（')): return data
            return text or data

        def chosen_audio_voice():
            """Qwen-Audio-TTS 那页的音色，坑和上面那个一模一样，同样的处理。"""
            text = avoice.currentText().strip()
            data = str(avoice.currentData() or '').strip()
            if data and (text == data or text.startswith(data + '（')): return data
            return text or data

        def lang_of(combo):
            """取语言代码（本地接口只认 zh/en/ja/ko/yue 这种短码）。

            下拉项的显示文本是「zh（中文）」、真正的值在 itemData 里。
            直接 currentText() 会把「ja（日文）」发给服务端，api_v2 校验不过就回 400——
            所以：选中的是列表项就用它的 data；手填的再退回去掉括号。
            """
            text=combo.currentText().strip()
            index=combo.findText(text)
            if index>=0: return str(combo.itemData(index) or text).strip()
            return text.split('（')[0].split('(')[0].strip()

        def snapshot():
            # 两页各有自己的「语气」输入框，但存的是同一个配置键（tts_instructions）：
            # 同一种东西只该有一个设置，切换合成方式不该要求重填。存的时候只认当前那页。
            audio_page = provider.currentData()=='qwenaudio'
            return {'tts_enabled':enabled.isChecked(),'tts_provider':provider.currentData() or 'qwen',
                    'tts_api_key':key.text().strip(),
                    'tts_model':model.currentText().strip(),'tts_voice':chosen_voice(),
                    'tts_volume':volume.value(),'tts_design_prompt':prompt.text().strip(),
                    'tts_design_preview':preview.text().strip(),'tts_design_target_model':target.text().strip(),
                    'tts_clone_file':clonefile.text().strip(),'tts_clone_name':clonename.text().strip(),
                    'tts_clone_target_model':clonetarget.text().strip(),'tts_clone_text':clonetext.text().strip(),
                    'tts_audition_text':audition.toPlainText().strip(),
                    'tts_instructions':(atone.text() if audio_page else tone.text()).strip(),
                    'tts_optimize_instructions':optimize.isChecked(),
                    'tone_prompt_enabled':tpenabled.isChecked(),
                    'tone_prompt':tp.toPlainText().strip(),
                    'audio_tts_api_key':akey.text().strip(),
                    'audio_tts_workspace':aws.text().strip(),
                    'audio_tts_model':amodel.currentText().strip(),
                    'audio_tts_voice':chosen_audio_voice(),
                    'audio_tts_format':afmt.currentText().strip(),
                    'audio_tts_sample_rate':int(asr.currentData() or 24000),
                    'audio_tts_rate':arate.value(),'audio_tts_pitch':apitch.value(),
                    'audio_tts_volume':avol.value(),
                    'audio_tts_seed':aseed.value(),
                    'audio_tts_random_seed':arandom.isChecked(),
                    'audio_tts_language_hints':str(alang.currentData() or 'zh'),
                    'audio_tts_clone_file':aclfile.text().strip(),
                    'audio_tts_clone_url':aclurl.text().strip(),
                    'audio_tts_clone_prefix':aclprefix.text().strip(),
                    'gptsovits_home':ghome.text().strip(),'gptsovits_url':gurl.text().strip(),
                    'gptsovits_ref_audio':gref.text().strip(),'gptsovits_prompt_text':gprompt.text().strip(),
                    'gptsovits_prompt_lang':lang_of(gplang),'gptsovits_text_lang':lang_of(glang),
                    'gptsovits_speed':gspeed.value(),'gptsovits_split_method':gsplit.currentText().strip(),
                    'gptsovits_seed':gseed.value(),'gptsovits_timeout':gtimeout.value(),
                    'gptsovits_gpt_weights':gptw.text().strip(),'gptsovits_sovits_weights':svw.text().strip()}
        def engine_from_ui():
            """按界面上当前选的方式造引擎（可能还没保存，所以不能只读配置）。"""
            from voice import GptSoVitsTTS, QwenAudioTTS, QwenTTS
            data=provider.currentData()
            if data=='gptsovits':
                return GptSoVitsTTS(gurl.text().strip() or LOCAL_TTS_URL, gref.text().strip(),
                                    gprompt.text().strip(), lang_of(gplang),
                                    lang_of(glang), gspeed.value(), gtimeout.value(),
                                    gsplit.currentText().strip(), seed=gseed.value())
            if data=='qwenaudio':
                return QwenAudioTTS(akey.text().strip() or key.text().strip(),
                                    amodel.currentText().strip(), chosen_audio_voice(),
                                    str(c.get('audio_tts_base_url','') or 'https://dashscope.aliyuncs.com'),
                                    aws.text().strip(), afmt.currentText().strip(),
                                    int(asr.currentData() or 24000), avol.value(),
                                    arate.value(), apitch.value(),
                                    None if arandom.isChecked() else aseed.value(),
                                    [str(alang.currentData() or 'zh')],
                                    atone.text().strip(), str(c.get('tts_timeout',60)))
            return QwenTTS(key.text().strip(), model.currentText().strip(), chosen_voice(),
                           str(c.get('tts_base_url','')), str(c.get('tts_language','Chinese')),
                           timeout=str(c.get('tts_timeout',60)),
                           instructions=tone.text().strip(),
                           optimize_instructions=optimize.isChecked())
        def play_audio(audio):
            # 复用同一个播放器：每点一次试听就新建一个的话，上一段的临时文件要等到退出才清。
            if getattr(self,'_tts_player',None) is None: self._tts_player=SpeechPlayer()
            self._tts_player.volume=volume.value(); self._tts_player.play(audio)

        def audition_text():
            """试听用哪段文本：优先试听框，其次声音设计的预览文本，最后兜个默认句。"""
            return (audition.toPlainText().strip() or preview.text().strip()
                    or '你好呀，我是回末李花子，请多指教。')

        async def do_check():
            test.setEnabled(False); status.setText('正在检测…')
            try:
                engine=engine_from_ui()
                ok, why=engine.availability()
                lines=[probe(snapshot(), getattr(self.manager,'persona',None))]
                if ok and provider.currentData()=='gptsovits':
                    # 本地再补一条端口探测：配置对不对、服务开没开是两件事。
                    hop, hwhy=await engine.health()
                    lines.append('本地服务：%s' % hwhy)
                status.setText('\n'.join(lines))
            except Exception as exc:
                logging.getLogger('voice').warning('检测失败：%s', exc)
                status.setText('检测失败：%s' % exc)
            finally:
                test.setEnabled(True)
        test.clicked.connect(lambda: asyncio.create_task(do_check()))
        async def do_speak(engine=None, text=None, what='试听'):
            play.setEnabled(False); status.setText('正在合成…')
            try:
                text=text or audition_text()
                engine=engine or engine_from_ui()
                if getattr(engine,'sentence_pipeline',False):
                    # 本地按句合成：第一句先出声，跟正式朗读时的行为一致。
                    parts=split_sentences(text); playing=getattr(self,'_tts_player',None)
                    if playing is None: self._tts_player=playing=SpeechPlayer()
                    playing.volume=volume.value()
                    status.setText('%s：本地逐句合成，共 %d 句（CPU 较慢，第一句先出声）…' % (what, len(parts)))
                    total=0
                    for index,part in enumerate(parts,1):
                        audio=await engine.synthesize(part); total+=len(audio)
                        playing.enqueue(audio)
                        status.setText('%s：已排队 %d/%d 句（累计 %d 字节）…' % (what, index, len(parts), total))
                    status.setText('%s已开始播放（本地 GPT-SoVITS，%d 句 / %d 字节）\n%s'
                                   % (what, len(parts), total, getattr(engine,'ref_note','') or ''))
                else:
                    # 试听框里写 [语气:…] 也能用：就地解析，看看这个模型到底认不认语气。
                    from conversation_manager import ConversationManager
                    clean, segments = ConversationManager.parse_tone(text)
                    tones = [t for t, _ in segments if t]
                    if tones:
                        text = clean
                        if len(set(tones)) > 1:
                            status.setText('%s：试听只按第一段语气（%s）合成，角色正式回复才会逐句换'
                                           % (what, tones[0]))
                    # 没写标记时，真正生效的是引擎自带的那条（两页共用的 tts_instructions，
                    # 所以要从引擎上读，不能只读当前页的输入框）。
                    shown = tones[0] if tones else (str(getattr(engine,'instructions','') or '').strip() or '无')
                    if getattr(engine,'supports_tone',False):
                        note='语气：%s' % shown
                    else:
                        # 实测：只有 qwen3-tts-instruct-flash 认语气，其余模型收下字段但不用。
                        # 这里必须说实话，不然试听听着没变还以为是自己没写好。
                        note='语气：不生效（模型 %s 不认语气指令，只有 qwen3-tts-instruct-flash 支持）' % engine.model
                    status.setText('%s：正在用「%s」合成（%s）…' % (what, engine.voice, note))
                    audio=await engine.synthesize(text, instructions=(tones[0] if tones else None))
                    play_audio(audio)
                    status.setText('%s已播放（音色 %s，%s，%d 字节）'
                                   % (what, engine.voice, note, len(audio)))
            except Exception as exc:
                # 状态栏在对话框里，关掉就看不到了；同时写日志，事后才查得动。
                logging.getLogger('voice').warning('%s失败：%s', what, exc)
                status.setText('%s失败：%s' % (what, exc))
            finally:
                play.setEnabled(True)
        play.clicked.connect(lambda: asyncio.create_task(do_speak()))

        async def do_audio_clone():
            """Qwen-Audio-TTS 的复刻：本地文件会被整理成 base64 再传（实测可用）。"""
            aclbtn.setEnabled(False); status.setText('正在整理录音并建音色…')
            try:
                source=aclfile.text().strip() or aclurl.text().strip()
                cloner=AudioVoiceCloner(akey.text().strip() or key.text().strip(),
                                        str(c.get('audio_tts_base_url','') or 'https://dashscope.aliyuncs.com'),
                                        aws.text().strip())
                voice_id,out=await cloner.create(aclprefix.text().strip(), source,
                                                 amodel.currentText().strip(),
                                                 language_hints=[str(alang.currentData() or 'zh')])
                avoice.addItem('%s（自建）' % voice_id, voice_id); avoice.setCurrentIndex(avoice.count()-1)
                preview=((out.get('output') or {}).get('preview_audio') or {})
                status.setText('建好了：%s\n已自动填到「音色」框，保存后即可使用。\n'
                               '注意：它只属于 %s，换模型要重新建。%s'
                               % (voice_id, amodel.currentText().strip(),
                                  '（接口还回了预览音频）' if preview else ''))
            except Exception as exc:
                logging.getLogger('voice').warning('Qwen-Audio-TTS 复刻失败：%s', exc)
                status.setText('建音色失败：%s' % exc)
            finally:
                aclbtn.setEnabled(True)
        aclbtn.clicked.connect(lambda: asyncio.create_task(do_audio_clone()))
        def pick_audio_ref():
            path,_f=QFileDialog.getOpenFileName(d,'选择用于复刻的录音',aclfile.text().strip() or str(Path.home()),
                                                '音频文件 (*.wav *.mp3 *.m4a);;所有文件 (*)')
            if path:
                aclfile.setText(path)
                info=probe_clone_audio(path)
                lines=[]
                if info.get('seconds') is not None:
                    lines.append('时长 %.1f 秒 ｜ %s Hz ｜ %d 声道'
                                 % (info['seconds'], info['rate'], info['channels']))
                lines += ['✗ %s' % e for e in info.get('errors',[])]
                lines += ['✓ %s' % f for f in info.get('fixable',[])]
                lines += ['· %s' % n for n in info.get('notes',[])]
                if info.get('seconds') and not (10.0 <= info['seconds'] <= 20.0):
                    lines.append('· 官方推荐 10~20 秒，这个长度也能建，相似度可能差一点')
                status.setText('\n'.join(lines) or '这个文件读不出信息')
        aclpick.clicked.connect(pick_audio_ref)
        async def do_create():
            create.setEnabled(False); status.setText('正在创建音色，通常十几秒…（每个 0.2 元）')
            try:
                designer=VoiceDesigner(key.text().strip(), str(c.get('tts_base_url','')), target.text().strip())
                voice_id, audio, used = await designer.create(prompt.text().strip(), preview.text().strip())
                voice.addItem('%s（自建）' % voice_id, voice_id); voice.setCurrentIndex(voice.count()-1)
                model.setCurrentText(used)      # 自建音色必须用创建它的驱动模型合成，否则会失败
                if audio: play_audio(audio)
                status.setText('创建成功！音色名 = %s\n已自动把「合成模型」设为 %s，保存后即可使用。' % (voice_id, used))
            except Exception as exc:
                status.setText('创建失败：%s' % exc)
            finally:
                create.setEnabled(True)
        create.clicked.connect(lambda: asyncio.create_task(do_create()))

        def pick_file():
            path,_f=QFileDialog.getOpenFileName(d,'选择用于复刻的录音',clonefile.text().strip() or str(Path.home()),'音频文件 (*.wav *.mp3 *.m4a);;所有文件 (*)')
            if path:
                clonefile.setText(path); show_clone_report(probe_clone_audio(path))
        pick.clicked.connect(pick_file)

        def pick_ref():
            path,_f=QFileDialog.getOpenFileName(d,'选择参考音频（服务端本机路径）',gref.text().strip() or ghome.text().strip() or str(Path.home()),'音频文件 (*.wav *.mp3 *.m4a *.flac);;所有文件 (*)')
            if not path: return
            gref.setText(path)
            report_ref()
        gpick.clicked.connect(pick_ref)

        def report_ref():
            """选完参考音频就体检一次：本地接口只吃 3~10 秒，超了我会自动切一段。"""
            path=gref.text().strip()
            if not path: return
            try:
                from voice import prepare_local_ref
                _used, note=prepare_local_ref(path)
                status.setText('参考音频：%s\n%s' % (Path(path).name, note))
            except Exception as exc:
                status.setText('参考音频不能用：%s' % exc)
        gref.editingFinished.connect(report_ref)

        # ---------------- 本地服务：启动 / 停止 / 端口检测 / 切权重 ----------------
        def spawn_local_server():
            """把整合包的 api_v2.py 起在后台（独立进程 + 自带控制台窗口，桌宠退出也不受影响）。"""
            from voice import GptSoVitsTTS, start_local_server
            host,port=GptSoVitsTTS(gurl.text().strip() or LOCAL_TTS_URL).host_port()
            proc=start_local_server(ghome.text().strip(), host, port)
            if proc is None:
                status.setText('启动失败：在「整合包目录」里没找到 runtime\\python.exe 或 api_v2.py。\n'
                               '请确认目录填的是整合包根目录（例如 D:\\GPT-SoVITS）。')
            return proc

        async def do_start():
            gstart.setEnabled(False)
            try:
                engine=engine_from_ui()
                ok,why=await engine.health(timeout=1.5)
                if ok:
                    status.setText('本地服务本来就在跑（%s），不用重复启动。' % gurl.text().strip()); return
                if spawn_local_server() is None: return
                # 真实的加载时间在这台机器上是分钟级（我实测 2 分钟还没就绪），所以等 5 分钟，
                # 并把「已经等了多久」写出来，别让人以为卡死。
                waited=0
                while waited < 300:
                    await asyncio.sleep(3); waited+=3
                    ok,why=await engine.health(timeout=2)
                    if ok:
                        status.setText('本地服务已就绪（%s，等了 %d 秒）。\n'
                                       '刚才弹出的那个控制台窗口就是它的日志，别关掉它。\n'
                                       '注意：整合包默认加载 v2 权重，要用自己训的模型请填权重后点「应用权重」。'
                                       % (gurl.text().strip(), waited))
                        return
                    status.setText('正在启动本地服务…已等 %d 秒（端口 %s 还没通）\n'
                                   '模型加载是分钟级的，第一次会久一点；\n'
                                   '旁边弹出的控制台窗口能看到它的进度和报错，真卡住了把那里的内容发出来。'
                                   % (waited, gurl.text().strip()))
                status.setText('等了 5 分钟端口还没通。请看那个控制台窗口里的报错——\n'
                               '常见原因：模型文件缺失、内存不够、端口被占用（可换端口后改「服务地址」）。')
            finally:
                gstart.setEnabled(True)
        gstart.clicked.connect(lambda: asyncio.create_task(do_start()))

        async def do_stop():
            gstop.setEnabled(False)
            try:
                engine=engine_from_ui()
                await engine.stop_server()
                status.setText('已让本地服务退出（/control?command=exit）。')
            except Exception as exc:
                status.setText('停止失败：%s\n（服务可能本来就没开）' % exc)
            finally:
                gstop.setEnabled(True)
        gstop.clicked.connect(lambda: asyncio.create_task(do_stop()))

        async def do_port_check():
            gcheck.setEnabled(False); status.setText('正在探测本地端口…')
            try:
                engine=engine_from_ui()
                ok,why=await engine.health()
                conf, cwhy=engine.availability()
                status.setText('%s\n配置检查：%s' % (why, cwhy if conf else '不可用，'+cwhy))
            finally:
                gcheck.setEnabled(True)
        gcheck.clicked.connect(lambda: asyncio.create_task(do_port_check()))

        async def do_apply_weights():
            gapply.setEnabled(False); status.setText('正在切换权重…（服务会重新加载，可能十几秒）')
            lines=[]; base=gurl.text().strip() or LOCAL_TTS_URL
            try:
                import httpx
                async with httpx.AsyncClient(timeout=180) as client:
                    if gptw.text().strip():
                        r=await client.get(base+'/set_gpt_weights', params={'weights_path':gptw.text().strip()})
                        lines.append('GPT 权重：HTTP %s %s' % (r.status_code, r.text.strip()[:120]))
                    if svw.text().strip():
                        r=await client.get(base+'/set_sovits_weights', params={'weights_path':svw.text().strip()})
                        lines.append('SoVITS 权重：HTTP %s %s' % (r.status_code, r.text.strip()[:120]))
                status.setText('\n'.join(lines) or '两个权重框都空着，没做任何切换。')
            except Exception as exc:
                status.setText('切换失败：%s' % exc)
            finally:
                gapply.setEnabled(True)
        gapply.clicked.connect(lambda: asyncio.create_task(do_apply_weights()))

        def show_clone_report(info):
            """把体检结果摊开讲：不能用的原因、会自动修的地方、提醒。"""
            lines=[]
            if info.get('seconds') is not None:
                lines.append('时长 %.1f 秒 ｜ %d Hz ｜ %d 声道 ｜ %.1f MB'
                             % (info['seconds'], info['rate'], info['channels'], info['size']/1048576.0))
            elif info.get('size'):
                lines.append('%.1f MB（MP3/M4A 本地读不出时长和采样率）' % (info['size']/1048576.0))
            for text in info.get('errors',[]): lines.append('✗ %s' % text)
            for text in info.get('fixable',[]): lines.append('✓ %s' % text)
            for text in info.get('notes',[]): lines.append('· %s' % text)
            if not info.get('errors'): lines.append('可以直接用来复刻。')
            if info.get('seconds') is not None and info['seconds'] > 60:
                lines.append('创建时我会问你要不要只取开头 30 秒。')
            status.setText('\n'.join(lines))
        checkclone.clicked.connect(lambda: show_clone_report(probe_clone_audio(clonefile.text().strip())))

        async def do_clone():
            clone.setEnabled(False); status.setText('正在读取并整理录音…')
            try:
                path=clonefile.text().strip()
                info=probe_clone_audio(path)
                if not info.get('exists'): raise RuntimeError('请先选一个存在的录音文件')
                if info.get('errors') and info.get('seconds') is not None:
                    # 只有「太长」是能就地解决的，其余直接说清楚，别浪费一次调用。
                    over=[e for e in info['errors'] if '60 秒' in e]
                    if not over or len(over)!=len(info['errors']): raise RuntimeError('；'.join(info['errors']))
                trim=None
                if info.get('seconds') and info['seconds']>60:
                    ask=QMessageBox.question(d,'录音太长','这段录音有 %.1f 秒，超过接口上限 60 秒。\n\n要只取开头 30 秒来做音色吗？（也可以自己先剪好再选）' % info['seconds'])
                    if ask!=QMessageBox.StandardButton.Yes: status.setText('已取消：请先剪短到 60 秒以内。'); return
                    trim=30.0
                status.setText('正在上传并复刻，通常十几秒…（每个 0.01 元）')
                cloner=VoiceCloner(key.text().strip(), str(c.get('tts_base_url','')), clonetarget.text().strip())
                voice_id, notes = await cloner.create(path, clonename.text().strip(), clonetext.text().strip(), trim_seconds=trim)
                voice.addItem('%s（复刻）' % voice_id, voice_id); voice.setCurrentIndex(voice.count()-1)
                model.setCurrentText(clonetarget.text().strip())
                lines=['复刻成功！音色名 = %s' % voice_id,'已自动把「合成模型」设为 %s，保存后即可使用。' % clonetarget.text().strip()]
                lines.extend(notes or [])
                lines.append('下面用这个音色读一段「试听文本」；想换内容就改上面的试听框再点「用当前音色试听」。')
                status.setText('\n'.join(lines))
                # 复刻接口不返回预览音频，所以直接拿新音色合成一句给你听。
                from voice import QwenTTS
                engine=QwenTTS(key.text().strip(), clonetarget.text().strip(), voice_id, str(c.get('tts_base_url','')), str(c.get('tts_language','Chinese')))
                await do_speak(engine, audition_text(), what='复刻音色试听')
                status.setText('\n'.join(lines[:-1]))
            except Exception as exc:
                status.setText('复刻失败：%s' % exc)
            finally:
                clone.setEnabled(True)
        clone.clicked.connect(lambda: asyncio.create_task(do_clone()))

        async def do_list():
            listing.setEnabled(False); status.setText('正在读取自建音色列表（声音设计 + 声音复刻）…')
            try:
                items, problems = await list_all_voices(key.text().strip(), str(c.get('tts_base_url','')))
                known={voice.itemData(i) for i in range(voice.count())}
                added=0
                for item in items:
                    name=item.get('voice') or item.get('voice_id')
                    if not name or name in known: continue
                    voice.addItem('%s（%s）' % (name, item.get('_kind','自建')), name); added+=1
                text='自建音色共 %d 个，新增列出 %d 个。\n选一个后保存即可使用（合成模型会自动对齐它的驱动模型）。' % (len(items), added)
                if problems: text += '\n有 %d 类没查到：%s' % (len(problems), '；'.join(problems))
                status.setText(text)
            except Exception as exc:
                status.setText('读取失败：%s' % exc)
            finally:
                listing.setEnabled(True)
        listing.clicked.connect(lambda: asyncio.create_task(do_list()))
        # 选中自建音色时把合成模型对齐到它的驱动模型，省掉「选了音色却合成失败」这一坑。
        def align_voice(_=None):
            data=chosen_voice()
            if data and data not in [n for n, _d in SYSTEM_VOICES]:
                if 'vc-' in data and clonetarget.text().strip(): model.setCurrentText(clonetarget.text().strip())
                elif target.text().strip(): model.setCurrentText(target.text().strip())
        voice.currentIndexChanged.connect(align_voice)
        def save():
            c.update(snapshot()); d.close()
        dbox.accepted.connect(save); dbox.rejected.connect(d.close)
        status.setText(probe(snapshot()))
        d.setModal(False); d.show(); d.raise_()

    def _save_kb_entry(self,kb,item,category,content,refresh):
        if item: kb.save(category.currentText(),content.toPlainText(),item.get('source','手动'),item.get('status','待审核'),item['id']); refresh()
    def _approve_kb(self,kb,item,refresh):
        if item: kb.save(item['category'],item['content'],item.get('source',''), '已确认', item['id']); refresh()

    def _show_dedupe_result(self,kb,groups,method,note,refresh,parent=None):
        """把查重结果列出来让用户看过再删（每组保留最完整/已确认的那条）。"""
        if not groups:
            QMessageBox.information(parent or self,'检测重复','没有发现重复条目。\n\n判定方式：%s%s' % (method,note)); return
        # 二次保险：同一组内按「已确认 → 内容更长 → id 更小」重排，确保保留的是最好那条。
        groups=[kb._order_group(group) for group in groups]
        removable=sum(len(group)-1 for group in groups)
        lines=[]
        for index,group in enumerate(groups,1):
            keep=group[0]
            if keep.get('_merged'): lines.append('【第 %d 组】保留并合并 → %s' % (index,keep['_merged']))
            else: lines.append('【第 %d 组】保留 → [%s] %s' % (index,keep['status'],keep['content']))
            for dup in group[1:]:
                lines.append('            删除 → [%s] %s' % (dup['status'],dup['content']))
            lines.append('')
        d=QDialog(parent or self); d.setWindowTitle('检测到重复条目'); layout=QVBoxLayout(d); fit_dialog(d, 820, 560)
        total=len(kb.entries())
        warn=''
        if total and removable >= total * 0.5:
            warn='\n⚠ 这次要删掉约 %d%% 的条目，数量偏多，请务必逐组看过再确认。' % int(removable * 100.0 / total)
        layout.addWidget(QLabel('共 %d 组、可删除 %d 条。判定方式：%s%s\n标「保留并合并」的会先把被删条目的独有信息并进保留项再删除，不会丢内容：%s' % (len(groups),removable,method,note,warn)))
        view=QTextEdit(); view.setReadOnly(True); view.setPlainText('\n'.join(lines)); layout.addWidget(view)
        box=QDialogButtonBox(); box.addButton('删除这 %d 条重复项' % removable,QDialogButtonBox.ButtonRole.AcceptRole); box.addButton('取消',QDialogButtonBox.ButtonRole.RejectRole); layout.addWidget(box)
        def do_delete():
            ids=[]
            for group in groups:
                keep=group[0]
                # 先落盘合并后的内容，再删重复项，顺序不能反。
                if keep.get('_merged'): kb.save(keep['category'],keep['_merged'],keep.get('source',''),keep.get('status','待审核'),keep['id'])
                ids.extend(entry['id'] for entry in group[1:])
            kb.delete_many(ids); d.accept(); refresh()
            QMessageBox.information(parent or self,'清理完成','已合并并删除 %d 条重复条目。' % len(ids))
        box.accepted.connect(do_delete); box.rejected.connect(d.reject)
        d.exec()

    def _knowledge_api_dialog(self):
        c=self.manager.config; d=QDialog(self); d.setWindowTitle('知识库 API 设置'); fit_dialog(d, 660, 580); f=QFormLayout(d); provider=QComboBox(); provider.addItems(['echo','openai','compatible','ollama']); provider.setCurrentText(c.get('knowledge_api_provider','echo')); model=QComboBox(); model.setEditable(True); model.addItem(c.get('knowledge_api_model','gpt-4o-mini')); key=QLineEdit(c.get('knowledge_api_key','')); key.setEchoMode(QLineEdit.EchoMode.Password); url=QLineEdit(c.get('knowledge_api_url','http://localhost:11434/api/chat')); tokens=QSpinBox(); tokens.setRange(40,999999999); tokens.setValue(int(c.get('knowledge_api_max_tokens',500))); tokens.setToolTip('推理模型（如 deepseek-flash）的思维链很吃 token，建议 8000 以上'); web=QCheckBox('允许 API 自身联网检索（模型自己搜，无需额外 Key）'); web.setChecked(c.get('knowledge_api_web_search',False)); prompt=QTextEdit(c.get('knowledge_prompt','请联网检索并筛选客观事实，排除同名和不可信内容，按分类输出 JSON，不得编造。')); prompt.setMinimumHeight(100); test=QPushButton('测试连接'); load=QPushButton('加载模型'); test.clicked.connect(lambda:self._test_api(provider.currentText(),key.text(),url.text(),model.currentText())); load.clicked.connect(lambda:self._load_models(provider.currentText(),key.text(),url.text(),model)); f.addRow('Provider',provider); f.addRow('模型',model); f.addRow('API Key',key); f.addRow('地址',url); f.addRow('最大 token',tokens); f.addRow('联网',web); f.addRow('采集提示词',prompt); row=QHBoxLayout(); row.addWidget(test); row.addWidget(load); f.addRow('连通性',row); b=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel); f.addRow(b); b.accepted.connect(d.accept); b.rejected.connect(d.reject)
        # 上限一定要在 setValue 之前设好：否则 QSpinBox 会先把配置里的大值夹到旧上限，
        # 之后再放宽上限也没用——值已经被改小，保存时又把小值写回配置。
        # （这正是「最大 token 改不了」的原因）
        def save_kb_api():
            c.update({'knowledge_api_provider':provider.currentText(),'knowledge_api_model':model.currentText().strip(),'knowledge_api_key':key.text(),'knowledge_api_url':url.text().strip(),'knowledge_api_max_tokens':tokens.value(),'knowledge_api_web_search':web.isChecked(),'knowledge_prompt':prompt.toPlainText(),'knowledge_api_temperature':0.7,'knowledge_api_top_p':1.0,'knowledge_api_frequency_penalty':0.0,'knowledge_api_presence_penalty':0.0,'knowledge_web_backend':backend.currentText(),'knowledge_web_api_key':webkey.text().strip(),'knowledge_web_base_url':weburl.text().strip(),'knowledge_web_query':webquery.text().strip(),'knowledge_web_pages':webpages.value(),'knowledge_web_page_chars':pagechars.value(),'knowledge_web_timeout':timeout.value(),'knowledge_web_strict':strict.isChecked(),'knowledge_api_stream':stream.isChecked(),'knowledge_use_pending':pending.isChecked(),'knowledge_always_inject':always.isChecked()}); d.close()
        # 联网组件（web_tools）独立于模型服务商：即使 compatible 接口不支持联网工具，
        # 也能在本地先搜到网页再交给模型抽取。
        backend=QComboBox(); backend.addItems(list(BACKENDS)); backend.setCurrentText(c.get('knowledge_web_backend','auto'))
        webkey=QLineEdit(c.get('knowledge_web_api_key','')); webkey.setEchoMode(QLineEdit.EchoMode.Password); webkey.setPlaceholderText('tavily / bocha / serper / brave 的 Key；searxng 与 duckduckgo 免填')
        weburl=QLineEdit(c.get('knowledge_web_base_url','')); weburl.setPlaceholderText('仅 SearXNG 需要，如 http://localhost:8888')
        webquery=QLineEdit(c.get('knowledge_web_query','{name}')); webquery.setPlaceholderText('默认只搜角色名最准；要补充可写 {name}|{name} 语录，用 | 分隔多条')
        webpages=QSpinBox(); webpages.setRange(0,10); webpages.setValue(int(c.get('knowledge_web_pages',3))); webpages.setToolTip('抓取前 N 条结果的网页正文，0 表示只用搜索摘要')
        pagechars=QSpinBox(); pagechars.setRange(200,20000); pagechars.setSingleStep(100); pagechars.setValue(int(c.get('knowledge_web_page_chars',1500))); pagechars.setToolTip('每条网页正文最多截取多少字')
        timeout=QSpinBox(); timeout.setRange(3,120); timeout.setValue(int(c.get('knowledge_web_timeout',15))); timeout.setSuffix(' 秒')
        strict=QCheckBox('检索失败就中止（默认改为退回模型自身知识）'); strict.setChecked(c.get('knowledge_web_strict',False))
        stream=QCheckBox('流式请求（长提示词建议开启，可绕开网关 504）'); stream.setChecked(c.get('knowledge_api_stream',True))
        pending=QCheckBox('待审核条目也用于对话'); pending.setChecked(c.get('knowledge_use_pending',True))
        always=QCheckBox('没有关键词命中时也注入核心设定'); always.setChecked(c.get('knowledge_always_inject',True))
        probe=QPushButton('测试联网检索'); probe.setToolTip('用当前设置真实检索一次并显示结果，用来判断网络是否通')
        probe.clicked.connect(lambda: self._test_web(backend.currentText(),webkey.text().strip(),weburl.text().strip(),probe))
        for label,widget in (('联网后端',backend),('联网 Key',webkey),('SearXNG 地址',weburl),('检索词模板',webquery),('抓正文页数',webpages),('每页字数',pagechars),('联网超时',timeout),('联网兜底',strict),('请求方式',stream),('对话取材',pending),('对话兜底',always),('联网自检',probe)):
            f.addRow(label,widget)
        b.accepted.connect(save_kb_api); b.rejected.connect(d.close); d.setModal(False); d.show(); d.raise_()

    def _test_api(self, provider, key, url, model):
        import urllib.request, json
        try:
            if provider == 'echo': QMessageBox.information(self, '连接测试', 'Echo Provider 本地连接正常。'); return
            if provider == 'ollama':
                base=url.replace('/api/chat','').rstrip('/') + '/api/tags'; data=json.loads(urllib.request.urlopen(base,timeout=5).read()); QMessageBox.information(self,'连接测试',f"Ollama 连接正常，共 {len(data.get('models',[]))} 个模型。"); return
            base=url.rstrip('/'); base=base if base.endswith('/v1') else base+'/v1'; req=urllib.request.Request(base+'/models',headers={'Authorization':'Bearer '+key}); urllib.request.urlopen(req,timeout=8); QMessageBox.information(self,'连接测试','API 连接正常。')
        except Exception as e: QMessageBox.warning(self,'连接失败',str(e))

    def _load_models(self, provider, key, url, combo):
        import urllib.request, json
        try:
            if provider == 'ollama':
                base=url.replace('/api/chat','').rstrip('/') + '/api/tags'; data=json.loads(urllib.request.urlopen(base,timeout=5).read()); names=[x.get('name','') for x in data.get('models',[])]
            elif provider in ('openai','compatible'):
                base=url.rstrip('/'); base=base if base.endswith('/v1') else base+'/v1'; req=urllib.request.Request(base+'/models',headers={'Authorization':'Bearer '+key}); data=json.loads(urllib.request.urlopen(req,timeout=8).read()); names=sorted(x.get('id','') for x in data.get('data',[]) if x.get('id'))
            else: names=['echo']
            combo.clear(); combo.addItems(names or ['未找到模型']); QMessageBox.information(self,'模型列表',f'已加载 {len(names)} 个模型。')
        except Exception as e:
            # urllib 的错误对象里有服务端原文，带上它才看得出是 Key 不对还是地址不对。
            detail=str(e)
            reader=getattr(e,'read',None)
            if callable(reader):
                try: detail += '：' + reader().decode('utf-8','replace')[:200]
                except Exception: pass
            if ' 401' in detail or ' 403' in detail:
                detail += '\n\n（Key 无效或没有权限；也可能是这个地址不提供 /v1/models 列表）'
            QMessageBox.warning(self,'加载失败',detail)

    def _fetch_character(self,name,info,status):
        query=urllib.parse.quote(name.text().strip());
        if not query: return
        try:
            url=f'https://zh.wikipedia.org/api/rest_v1/page/summary/{query}'; data=json.loads(urllib.request.urlopen(url,timeout=8).read().decode('utf-8')); text=data.get('extract','')
            if text: info.setText(text[:2000]); status.setText('已获取 Wikipedia 公开资料')
            else: status.setText('未找到资料，请手动补充')
        except Exception as exc: status.setText(f'搜集失败，可手动编辑：{exc}')
    def _api_dialog(self):
        """对话 API 设置：可以保存多套自己填的第三方接口，随时切换。

        两块各管各的：
          * 上面「已保存的接口」= 一个**接口库**：增加/更新/删除**立刻写进 config.json**，
            所以点了取消也不会白填；
          * 下面的字段 + 「保存」= 让桌宠**现在就用**这套（改的是生效中的配置）。
        选中库里某一档会自动把字段填好，再点保存即可切换。
        """
        if not self.manager: return
        cfg=self.manager.config
        dialog=QDialog(self); dialog.setWindowTitle("API 与回复过滤")
        outer=QVBoxLayout(dialog)

        pbox=QGroupBox('已保存的接口（可存多套，随时切换）'); pv=QVBoxLayout(pbox)
        prow=QHBoxLayout()
        profiles_combo=QComboBox(); profiles_combo.setMinimumWidth(240)
        profiles_combo.setToolTip('选中某一档会自动把下面的字段填好，再点「保存」就切过去')
        add=QPushButton('增加'); rename=QPushButton('改名'); update=QPushButton('更新为当前填写')
        remove=QPushButton('删除')
        add.setToolTip('把下面填的这套接口存成一个新档位（只存 Provider / 模型 / Key / 地址 / token）')
        update.setToolTip('用下面填的内容覆盖选中的那一档')
        rename.setToolTip('给选中的档位改个名字')
        remove.setToolTip('删掉选中的档位（不影响桌宠当前正在用的配置）')
        prow.addWidget(profiles_combo, 1)
        for button in (add, rename, update, remove): prow.addWidget(button)
        pv.addLayout(prow)
        active=QLabel(); active.setWordWrap(True)
        active.setStyleSheet('color:#33415c;background:#f2f5fb;border-radius:6px;padding:6px')
        pv.addWidget(active)
        tip=QLabel('「增加 / 更新 / 删除」会**立刻写入** config.json（点取消也不会白填）；'
                   '想真正让桌宠改用某一套，选中它后点最下面的「保存」。\n'
                   'API Key 存在 config.json 里（和现在这一个 Key 的存放方式相同，没有额外加密）。')
        tip.setWordWrap(True); tip.setStyleSheet('color:#5a6478')
        pv.addWidget(tip)
        outer.addWidget(pbox)

        form=QFormLayout(); outer.addLayout(form)
        provider=QComboBox(); provider.addItems(["echo","openai","compatible","ollama"])
        provider.setCurrentText(cfg.get('provider','echo'))
        model=QComboBox(); model.setEditable(True); model.addItem(cfg.get('model','gpt-4o-mini'))
        key=QLineEdit(cfg.get('api_key','')); key.setEchoMode(QLineEdit.EchoMode.Password)
        url=QLineEdit(cfg.get('base_url',cfg.get('url','http://localhost:11434/api/chat')))
        url.setToolTip('第三方中转站通常填到域名即可，例如 https://你的中转站域名')
        max_tokens=QSpinBox(); max_tokens.setRange(20,999999999)
        max_tokens.setValue(int(cfg.get('max_tokens',120)))
        blocked=QLineEdit(','.join(cfg.get('blocked_keywords',self.manager.blocked_keywords)))
        blocked.setPlaceholderText('AI,模型,程序,提示词')
        stop=QLineEdit(','.join(cfg.get('stop',['（','(','【思考','首先','其次'])))
        stop.setPlaceholderText('（,(,首先')
        test=QPushButton('测试连接'); load=QPushButton('加载模型')
        test.clicked.connect(lambda:self._test_api(provider.currentText(),key.text(),url.text(),model.currentText()))
        load.clicked.connect(lambda:self._load_models(provider.currentText(),key.text(),url.text(),model))
        form.addRow('Provider',provider); form.addRow('模型',model); form.addRow('API Key',key)
        form.addRow('API 地址',url); form.addRow('最大回复 token',max_tokens)
        form.addRow('拦截关键词',blocked); form.addRow('停止词',stop)
        row=QHBoxLayout(); row.addWidget(test); row.addWidget(load); form.addRow('连通性',row)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel)
        form.addRow(buttons)

        def fields():
            return {'provider':provider.currentText().strip(),'model':model.currentText().strip(),
                    'api_key':key.text().strip(),'url':url.text().strip(),
                    'max_tokens':max_tokens.value()}

        def fill(profile):
            provider.setCurrentText(str(profile.get('provider') or 'echo'))
            model.setCurrentText(str(profile.get('model') or ''))
            key.setText(str(profile.get('api_key') or ''))
            url.setText(str(profile.get('url') or ''))
            try: max_tokens.setValue(int(profile.get('max_tokens') or 120))
            except (TypeError,ValueError): pass

        def refresh(select=None, note='', sync=False):
            """重建档位下拉，并把「当前生效」写清楚。

            `sync=True` 才会把选中档的值填进字段：打开对话框时**不能**填，
            否则会用库里的旧副本盖掉正在生效的配置（用户手动改过 Key 的话就丢了）；
            而增加/更新/改名/删除之后要填，否则字段会停在「刚被删掉那一档」的内容上。
            """
            items=api_profiles(cfg)
            profiles_combo.blockSignals(True); profiles_combo.clear()
            if items:
                for profile in items: profiles_combo.addItem(profile['name'])
                want=str(select if select is not None else cfg.get('api_profile','') or '')
                index=profiles_combo.findText(want)
                profiles_combo.setCurrentIndex(index if index>=0 else 0)
            else:
                profiles_combo.addItem('（还没有保存的接口）')
            profiles_combo.setEnabled(bool(items))
            profiles_combo.blockSignals(False)
            for button in (rename, update, remove): button.setEnabled(bool(items))
            if sync:
                index=profiles_combo.currentIndex()
                if 0<=index<len(items): fill(items[index])
            render(note)

        def render(note=''):
            """把「现在在用哪一档」和「下面这套存了没有」实时写清楚。

            实测踩过的坑：用户点「增加」把当时字段存成一档，**之后**才把地址/Key 改成新接口，
            再点「保存」——新值进了生效配置，档位里留的还是旧的，界面上又看不出差别，
            看起来就像「我建了新的、没保存」。所以这里必须实时说清下面这套到底存没存。
            """
            items=api_profiles(cfg)
            current=str(cfg.get('api_profile','') or '')
            if current and any(p['name']==current for p in items):
                line1='当前桌宠在用：%s ｜ %s' % (current, api_profiles_preview(
                    next(p for p in items if p['name']==current)))
            elif current:
                line1='当前桌宠在用：%s（这一档已经被删掉了，配置本身还在用）' % current
            else:
                line1='当前桌宠在用：（不是任何已存档）'
            matched=api_profile_match(cfg, fields())
            if matched:
                line2='下面这套 == 档位「%s」%s' % (matched, '，也就是当前在用的' if matched==current else '')
                color, bg, border = '#1f5130', '#f2fbf4', '#cfe9d6'
            else:
                line2=('⚠ 下面这套**还没存进任何档位**：点「增加」存成新档，'
                       '或点「更新为当前填写」覆盖选中的那一档')
                color, bg, border = '#8a5a12', '#fffaf0', '#f0e0c0'
            active.setText('%s\n%s%s' % (line1, line2, ('\n'+note) if note else ''))
            active.setStyleSheet('color:%s;background:%s;border:1px solid %s;border-radius:6px;padding:6px'
                                 % (color, bg, border))

        state={'index':0}

        def on_pick(index):
            """用户主动选了某一档：先把没存档的改动问清楚，绝不默默覆盖。

            只接 `activated`（用户真的点了），不接 `currentIndexChanged`——
            后者在程序里改选中项时也会响，容易和这里的确认框互相触发。
            另外 `activated` 在**重复点同一档**时也会发，正好用来当「撤销我的改动、
            回到这一档」的入口（Qt 对同一项不会发 currentIndexChanged）。
            """
            items=api_profiles(cfg)
            if not (0<=index<len(items)): return
            current_fields=fields()
            if api_profile_match(cfg, current_fields):
                fill(items[index]); state['index']=index; render(); return
            if not (current_fields.get('url') or current_fields.get('api_key') or current_fields.get('model')):
                fill(items[index]); state['index']=index; render(); return   # 没什么可丢的
            box=QMessageBox(dialog); box.setWindowTitle('切换档位')
            box.setText('下面填的这套还没存进任何档位，切到「%s」会把它覆盖掉。' % items[index]['name'])
            keep=box.addButton('先存成新档', QMessageBox.ButtonRole.AcceptRole)
            drop=box.addButton('放弃改动，切过去', QMessageBox.ButtonRole.DestructiveRole)
            box.addButton('取消', QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked=box.clickedButton()
            if clicked is keep:
                do_add(); return
            if clicked is drop:
                fill(items[index]); state['index']=index; render(); return
            # 取消：把下拉拨回原来选中的那一档
            profiles_combo.blockSignals(True)
            profiles_combo.setCurrentIndex(state['index'])
            profiles_combo.blockSignals(False)
            render()

        def write(items, current=None):
            values={'api_profiles':items}
            if current is not None: values['api_profile']=current
            cfg.update(values)          # 立刻落盘：档位是「库」，不该被取消按钮吞掉

        def do_add():
            """把**当前字段**存成一个新档位，返回档位名（取消则返回空串）。"""
            profile=fields()
            default=str(profile.get('model') or '').strip() or ('接口 %d' % (len(api_profiles(cfg))+1))
            name,ok=QInputDialog.getText(dialog,'增加接口','给这套接口起个名字（例如「阿里百炼」「云雾中转」）：',
                                         QLineEdit.EchoMode.Normal, default)
            if not ok: return ''
            name=str(name or '').strip()
            if not name:
                QMessageBox.warning(dialog,'增加接口','名字不能为空。'); return ''
            items=api_profiles(cfg)
            if any(p['name']==name for p in items):
                if QMessageBox.question(dialog,'增加接口','已经有一个叫「%s」的档位，用它替换掉吗？' % name) != QMessageBox.StandardButton.Yes:
                    return ''
                items=[p for p in items if p['name']!=name]
            items.append(dict(profile, name=name))
            write(items, name)
            refresh(select=name, sync=True, note='已保存「%s」：%s' % (name, api_profiles_preview(profile)))
            return name

        def do_rename():
            old=profiles_combo.currentText().strip()
            items=api_profiles(cfg)
            if not old or not any(p['name']==old for p in items): return
            name,ok=QInputDialog.getText(dialog,'档位改名','新名字：',QLineEdit.EchoMode.Normal,old)
            if not ok: return
            name=str(name or '').strip()
            if not name or name==old: return
            if any(p['name']==name for p in items):
                QMessageBox.warning(dialog,'档位改名','已经有一个叫「%s」的档位了。' % name); return
            items=[dict(p, name=name) if p['name']==old else p for p in items]
            current=cfg.get('api_profile','') or ''
            write(items, name if current==old else current)
            refresh(select=name, sync=True, note='已改名为「%s」' % name)

        def do_update():
            name=profiles_combo.currentText().strip()
            items=api_profiles(cfg)
            if not name or not any(p['name']==name for p in items): return
            profile=fields()
            if QMessageBox.question(dialog,'更新档位','用下面填的内容覆盖「%s」？' % name) != QMessageBox.StandardButton.Yes:
                return
            items=[dict(profile, name=name) if p['name']==name else p for p in items]
            write(items, name if (cfg.get('api_profile','') or '')==name else None)
            refresh(select=name, sync=True, note='「%s」已更新为当前填写的内容' % name)

        def do_remove():
            name=profiles_combo.currentText().strip()
            items=api_profiles(cfg)
            if not name or not any(p['name']==name for p in items): return
            if QMessageBox.question(dialog,'删除档位','删掉「%s」？（只删这一档，桌宠当前在用的配置不受影响）'
                                    % name) != QMessageBox.StandardButton.Yes:
                return
            items=[p for p in items if p['name']!=name]
            current=cfg.get('api_profile','') or ''
            write(items, '' if current==name else current)
            refresh(sync=True, note='已删除「%s」' % name)

        def save_api():
            """保存 = 让桌宠现在就用这套。

            如果这套**还没进任何档位**，先问一句要不要存下来——实测就是这么丢的：
            改了地址和 Key、点了保存，生效配置换了、档位里没记，用户以为「没保存」。
            """
            values=dict(fields()); values['base_url']=values['url']
            values.update({'blocked_keywords':[x.strip() for x in blocked.text().split(',') if x.strip()],
                           'stop':[x for x in stop.text().split(',') if x],
                           'temperature':0.7,'top_p':1.0,'frequency_penalty':0.0,'presence_penalty':0.0})
            matched=api_profile_match(cfg, values)
            if not matched:
                box=QMessageBox(dialog); box.setWindowTitle('保存')
                box.setText('下面这套配置还没存进任何档位。\n要把它存成一个新档位吗？'
                            '（存了以后随时能从上面的列表切回来）')
                keep=box.addButton('存成新档再保存', QMessageBox.ButtonRole.AcceptRole)
                plain=box.addButton('直接保存，不存档', QMessageBox.ButtonRole.DestructiveRole)
                box.addButton('取消', QMessageBox.ButtonRole.RejectRole)
                box.exec()
                clicked=box.clickedButton()
                if clicked is keep:
                    matched=do_add()          # 会顺带把这一档设为当前档
                    if not matched: return    # 名字那步取消了 → 也别保存
                elif clicked is not plain:
                    return
            values['api_profile']=matched
            self.manager.update_provider(values)
            self.manager.blocked_keywords=values['blocked_keywords']
            logging.getLogger('app').info('对话 API 已切换：%s @ %s（档位：%s）',
                                          values.get('model') or values.get('provider'),
                                          values.get('url'), matched or '未存档')
            dialog.close()

        profiles_combo.activated.connect(on_pick)
        add.clicked.connect(do_add); rename.clicked.connect(do_rename)
        update.clicked.connect(do_update); remove.clicked.connect(do_remove)
        buttons.accepted.connect(save_api); buttons.rejected.connect(dialog.close)
        # 字段一变就刷新状态行：让「这套存没存」永远是实时的，不用等保存才发现。
        for widget in (provider, model, key, url):
            signal = widget.currentTextChanged if isinstance(widget, QComboBox) else widget.textChanged
            signal.connect(lambda *_: render())
        max_tokens.valueChanged.connect(lambda *_: render())
        refresh()
        state['index']=profiles_combo.currentIndex()
        fit_dialog(dialog, 720, 640)
        dialog.setModal(False); dialog.show(); dialog.raise_()

    def _test_web(self, backend, key, url, button):
        """联网自检：本地组件和 API 自身联网都真测一遍。

        以前只测本地组件，于是会出现「自检通过、但搜集还是失败」——因为真正
        卡住的是 API 那条路。两边都测，结论才可信。
        """
        if not self.manager: return
        cfg=self.manager.config
        name=cfg.get('knowledge_name','李花子') or '李花子'
        searcher=WebSearcher(backend=backend, api_key=key, base_url=url, timeout=float(cfg.get('knowledge_web_timeout',15) or 15), pages=0)
        api_web=bool(cfg.get('knowledge_api_web_search',False))
        button.setEnabled(False); button.setText('检测中…')
        async def run():
            try:
                local=await searcher.diagnose(name)
            except Exception as exc:
                local=f'自检异常：{exc}'
            if api_web:
                try:
                    remote=await self._probe_api_web(cfg)
                except Exception as exc:
                    remote=f'检测异常：{exc}'
            else:
                remote='未启用（“允许 API 自身联网检索”没有勾选），跳过。'
            button.setEnabled(True); button.setText('测试联网检索')
            QMessageBox.information(self,'联网自检',f"【一、本地联网组件】\n{local}\n\n【二、API 自身联网】\n{remote}")
        asyncio.create_task(run())

    async def _probe_api_web(self, cfg):
        """单独测一次 API 自身联网。重点是核对它「到底有没有真的发起搜索」——
        有的模型会收下 web_search 工具却一次都不搜，然后凭训练数据编来源链接。"""
        from llm_provider import provider_from_config
        provider=provider_from_config({'provider':cfg.get('knowledge_api_provider','compatible'),'model':cfg.get('knowledge_api_model',''),'api_key':cfg.get('knowledge_api_key',''),'url':cfg.get('knowledge_api_url',''),'base_url':cfg.get('knowledge_api_url',''),'max_tokens':max(2000,int(cfg.get('knowledge_api_max_tokens',8000) or 8000)),'use_web_search':True,'stream':bool(cfg.get('knowledge_api_stream',True)),'timeout':float(cfg.get('knowledge_api_timeout',180) or 180)})
        text=str(await provider.chat([{'role':'user','content':'联网搜索一下这条消息：回末李花子出自哪部作品？给出来源链接。'}]) or '')
        if text.startswith('暂时无法连接'):
            return '不可用：\n'+text
        return '可用（已确认真正发起了搜索）：\n'+text[:220]+'…'


class RegionPicker(QWidget):
    """整块虚拟桌面上的半透明遮罩：按住拖一个框，松开就把区域交出去。

    只用来选「看屏幕的哪一块」，不抓图、不保存，选完立刻关掉。
    """

    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.CrossCursor)
        from screen_vision import virtual_desktop
        x, y, w, h = virtual_desktop()
        self._origin = (x, y)
        self.setGeometry(x, y, w, h)
        self._start = None; self._current = None; self.result = None

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 90))
        if self._start and self._current:
            rect = QRect(self._start, self._current).normalized()
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(rect, Qt.GlobalColor.transparent)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setPen(QPen(QColor('#7fd4ff'), 2)); painter.drawRect(rect)
            painter.setPen(QColor('#eaf6ff'))
            painter.drawText(rect.adjusted(6, 6, 0, 0), '%d x %d' % (rect.width(), rect.height()))

    def mousePressEvent(self, event):
        self._start = event.position().toPoint(); self._current = self._start; self.update()

    def mouseMoveEvent(self, event):
        if self._start: self._current = event.position().toPoint(); self.update()

    def mouseReleaseEvent(self, _event):
        if self._start and self._current:
            rect = QRect(self._start, self._current).normalized()
            if rect.width() >= 16 and rect.height() >= 16:
                self.result = [self._origin[0] + rect.x(), self._origin[1] + rect.y(),
                               rect.width(), rect.height()]
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape: self.result = None; self.close()
