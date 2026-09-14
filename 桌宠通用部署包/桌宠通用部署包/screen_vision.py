# -*- coding: utf-8 -*-
"""李花子桌宠 · 屏幕理解（看屏幕，主动说一句）。

设计约束（都来自需求）：

* **独立 API**：视觉模型和聊天模型分开配（`screen_*` 一组键），
  因为能看图的模型往往不是平时聊天的那个（DeepSeek 就不支持图片）。
* **优先级：用户输入 > 屏幕内容**。用户刚说过话、或者正在等回复时，
  绝不去看屏幕；看一眼的过程中用户发话了，这次观察直接作废。
* **隐私可控**：默认关闭；只截「整屏 / 自己划的矩形 / 指定的窗口」；
  **截图只在内存里流转，不落盘**；识别结果也**不写进对话数据库**，
  只用来让角色说一句（可在日志里看到截了什么范围、多大）。
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import hashlib
import logging
import time
from ctypes import wintypes
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QObject, QTimer, Signal

LOG = logging.getLogger('screen')

# 上传前把长边缩到这个尺寸：既省 token，也少暴露细节。
DEFAULT_MAX_EDGE = 1280


# --------------------------------------------------------------------- 窗口枚举
def list_windows() -> List[Dict[str, Any]]:
    """列出当前可见、有标题、不是最小化的顶层窗口。

    只用 Win32（ctypes），不引入新依赖。返回 [{'title','rect':[x,y,w,h],'hwnd'}]，
    rect 是**虚拟桌面坐标**（多屏时可能是负数）。
    """
    try:
        user32 = ctypes.windll.user32
    except Exception as exc:                       # 非 Windows
        LOG.info('列窗口不可用：%s', exc)
        return []
    items: List[Dict[str, Any]] = []

    def callback(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if not title:
                return True
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if width < 80 or height < 80 or rect.top < -10000:   # 极小/最小化的残影
                return True
            items.append({'hwnd': int(hwnd), 'title': title,
                          'rect': [int(rect.left), int(rect.top), int(width), int(height)]})
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(callback), 0)
    except Exception:
        LOG.exception('枚举窗口失败')
    return items


def find_window(title: str) -> Optional[Dict[str, Any]]:
    """按标题找窗口（先精确、再包含），每次都取**当前**位置，所以窗口挪了也能跟上。"""
    title = (title or '').strip()
    if not title:
        return None
    windows = list_windows()
    for window in windows:
        if window['title'] == title:
            return window
    needle = title.lower()
    for window in windows:
        if needle in window['title'].lower():
            return window
    return None


# --------------------------------------------------------------------- 截图
def virtual_desktop() -> Tuple[int, int, int, int]:
    """整个虚拟桌面（所有屏幕的并集），用于画选区遮罩。"""
    try:
        from PySide6.QtGui import QGuiApplication
        rect = None
        for screen in QGuiApplication.screens():
            geo = screen.geometry()
            rect = geo if rect is None else rect.united(geo)
        if rect is not None:
            return rect.x(), rect.y(), rect.width(), rect.height()
    except Exception:
        LOG.exception('取虚拟桌面尺寸失败')
    return 0, 0, 1920, 1080


def capture(rect: Optional[Sequence[int]] = None, max_edge: int = DEFAULT_MAX_EDGE):
    """截屏并编码成 PNG（内存里），返回 (png 字节, 信息 dict)。

    rect 为 None 时截整块主屏；给了 [x,y,w,h] 就只截这一块（虚拟桌面坐标）。
    先整屏抓、再裁剪，是为了绕开 Qt 在多屏 / 高 DPI 下 grabWindow 坐标口径不一致的老问题。
    """
    from PySide6.QtCore import QBuffer, QRect
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    if app is None:
        raise RuntimeError('截屏需要先创建 QApplication')

    screen = QGuiApplication.primaryScreen()
    if rect:
        # 选中的区域可能落在副屏上：挑包含它中心的那个屏幕来抓。
        centre_x, centre_y = rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0
        for candidate in QGuiApplication.screens():
            geo = candidate.geometry()
            if geo.contains(int(centre_x), int(centre_y)):
                screen = candidate; break
    if screen is None:
        raise RuntimeError('没有可用的屏幕')

    pixmap = screen.grabWindow(0)                      # 该屏幕的完整画面
    ratio = float(pixmap.devicePixelRatio() or 1.0)
    info: Dict[str, Any] = {'screen': screen.name(), 'device_pixel_ratio': ratio}

    if rect:
        geo = screen.geometry()
        # 区域被屏幕边界裁掉时要如实记下来，别让人以为截全了。
        left = max(rect[0], geo.x()); top = max(rect[1], geo.y())
        right = min(rect[0] + rect[2], geo.x() + geo.width())
        bottom = min(rect[1] + rect[3], geo.y() + geo.height())
        if right - left < 8 or bottom - top < 8:
            raise RuntimeError('选中的区域不在任何屏幕可见范围内（或太小）')
        crop = QRect(int((left - geo.x()) * ratio), int((top - geo.y()) * ratio),
                     int((right - left) * ratio), int((bottom - top) * ratio))
        pixmap = pixmap.copy(crop)
        info['clipped'] = (left, top) != (rect[0], rect[1]) or (right, bottom) != (rect[0] + rect[2], rect[1] + rect[3])

    if max_edge and max(pixmap.width(), pixmap.height()) > max_edge:
        pixmap = pixmap.scaled(max_edge, max_edge,
                               aspectMode=__import__('PySide6.QtCore', fromlist=['Qt']).Qt.AspectRatioMode.KeepAspectRatio,
                               mode=__import__('PySide6.QtCore', fromlist=['Qt']).Qt.TransformationMode.SmoothTransformation)

    buffer = QBuffer(); buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    if not pixmap.save(buffer, 'PNG'):
        raise RuntimeError('截屏编码 PNG 失败')
    data = bytes(buffer.data())
    info.update({'width': pixmap.width(), 'height': pixmap.height(), 'bytes': len(data),
                 'mode': 'region' if rect else 'full'})
    return data, info


def capture_for_config(config) -> Tuple[bytes, Dict[str, Any], str]:
    """按配置决定截哪里：锁定窗口 > 划定区域 > 整屏。返回 (png, 信息, 说明)。"""

    def setting(key, default=None):
        if config is None: return default
        getter = getattr(config, 'get', None)
        return getter(key, default) if callable(getter) else config.get(key, default)

    region = setting('screen_region') or None
    title = str(setting('screen_window_title', '') or '').strip()
    max_edge = int(setting('screen_max_edge', DEFAULT_MAX_EDGE) or DEFAULT_MAX_EDGE)

    if title:
        window = find_window(title)
        if window is None:
            raise RuntimeError('锁定窗口「%s」找不到了（已关闭或改了标题）' % title)
        png, info = capture(window['rect'], max_edge)
        info['window'] = window['title']
        return png, info, '锁定窗口「%s」' % window['title']
    if region and len(region) == 4:
        png, info = capture(region, max_edge)
        return png, info, '划定区域 %s' % (list(region),)
    png, info = capture(None, max_edge)
    return png, info, '整屏'


# --------------------------------------------------------------------- 视觉 API
class VisionError(RuntimeError):
    pass


class VisionClient:
    """独立的视觉模型客户端：OpenAI 兼容的 chat/completions + 图片。

    走 llm_provider 里那套 CompatibleProvider（它支持任意 messages），
    单独一份 screen_* 配置，所以不会影响平时聊天用的模型。
    """

    def __init__(self, config=None):
        self.config = config

    def _setting(self, key, default=None):
        if self.config is None: return default
        getter = getattr(self.config, 'get', None)
        return getter(key, default) if callable(getter) else self.config.get(key, default)

    def availability(self):
        if not self._setting('screen_base_url'):
            return False, '没有填视觉 API 地址'
        if not self._setting('screen_api_key'):
            return False, '没有填视觉 API Key'
        if not self._setting('screen_model'):
            return False, '没有填视觉模型名'
        return True, '模型 %s' % self._setting('screen_model')

    def _payload_config(self):
        return {'provider': self._setting('screen_provider', 'compatible') or 'compatible',
                'base_url': self._setting('screen_base_url', ''), 'api_key': self._setting('screen_api_key', ''),
                'model': self._setting('screen_model', ''), 'max_tokens': int(self._setting('screen_max_tokens', 200) or 200),
                'temperature': float(self._setting('screen_temperature', 0.8) or 0.8),
                'timeout': float(self._setting('screen_timeout', 90) or 90), 'stream': False}

    async def look(self, png: bytes, system_prompt: str, hint: str = '') -> str:
        """把截图交给模型，返回它的回答（一般是角色的一句台词）。"""
        ok, why = self.availability()
        if not ok: raise VisionError(why)
        if not png: raise VisionError('截图是空的')
        from llm_provider import provider_from_config
        data_uri = 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')
        messages = [{'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': [
                        {'type': 'text', 'text': hint or '看一眼我的屏幕。'},
                        {'type': 'image_url', 'image_url': {'url': data_uri}}]}]
        provider = provider_from_config(self._payload_config())
        try:
            raw = await provider.chat(messages)
        except Exception as exc:
            raise VisionError('%s: %s' % (type(exc).__name__, exc)) from exc
        text = str(raw or '').strip()
        # CompatibleProvider 出错时是「返回一句话」而不是抛异常（聊天那边靠这句提示用户），
        # 这里必须把它当失败处理，否则会把「暂时无法连接…」当成角色台词念出来。
        if text.startswith('暂时无法连接'):
            if ' 401' in text or ' 403' in text:
                text += ('（Key 无效或没有权限：这里用的是设置里填的那条 Key，没保存也会生效；'
                         '可以点「沿用聊天 API」把聊天那条能用的 Key 抄过来）')
            raise VisionError(text)
        if not text:
            raise VisionError('模型返回为空（可能是该模型不支持图片，或 max_tokens 太小）')
        LOG.info('屏幕理解完成：%d 字', len(text))
        return text

    async def probe(self) -> Tuple[bool, str]:
        """真发一张小图试一次：能过就说明这个模型确实吃图片。"""
        ok, why = self.availability()
        if not ok: return False, why
        try:
            from PySide6.QtCore import QBuffer, Qt
            from PySide6.QtGui import QColor, QImage, QPainter
            image = QImage(96, 48, QImage.Format.Format_RGB32)
            image.fill(QColor('#204080'))                      # 深蓝底
            painter = QPainter(image)
            painter.setPen(QColor('#ffe08a')); painter.drawText(8, 30, 'LILYZ')
            painter.end()
            buffer = QBuffer(); buffer.open(QBuffer.OpenModeFlag.WriteOnly)
            image.save(buffer, 'PNG')
            drew = bytes(buffer.data())
            png = drew or capture(None, 320)[0]
            answer = await self.look(png, '你是一个测试助手。', '这张图里有什么？只回答你看到的内容。')
            return True, '模型有响应：%s' % answer[:80]
        except Exception as exc:
            return False, str(exc)


# --------------------------------------------------------------------- 观察调度
class ScreenWatcher(QObject):
    """按节奏偷看一眼屏幕，把结果交给角色去说。

    优先级规则在这里落地：
    * 用户刚说过话（距上次发言不足 `screen_idle_seconds`）→ 不看
    * 角色正在回复（thinking）→ 不看
    * 看一眼的过程中用户发话了 → 这次结果直接丢弃，绝不插嘴
    """

    observed = Signal(str)          # 看懂的一句（依据 screen_reply_mode 可能是台词或客观描述）
    failed = Signal(str)

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self._timer = QTimer(self)
        self._timer.setInterval(3000)          # 每 3 秒检查一次「该不该看」
        self._timer.timeout.connect(self._tick)
        self._busy = False
        self._last_look = 0.0
        self._last_hash = ''
        self._last_note = ''
        self.looks = 0

    # ---------------- 生命周期
    def start(self):
        if not self._timer.isActive(): self._timer.start()

    def stop(self):
        self._timer.stop()

    def set_interval_minutes_aware(self):
        pass

    # ---------------- 配置读取
    def _setting(self, key, default=None):
        config = getattr(self.manager, 'config', None)
        if config is None: return default
        getter = getattr(config, 'get', None)
        return getter(key, default) if callable(getter) else config.get(key, default)

    @property
    def enabled(self):
        return bool(self._setting('screen_enabled', False))

    def status_text(self):
        if not self.enabled: return '屏幕理解：已关闭'
        client = VisionClient(self.manager.config)
        ok, why = client.availability()
        base = '屏幕理解：%s ｜ 每 %s 秒最多看一眼 ｜ 用户静默 %s 秒后才会主动看' % (
            why if ok else '配置不完整（%s）' % why,
            self._setting('screen_interval', 30), self._setting('screen_idle_seconds', 45))
        return base + ('｜上次：%s' % self._last_note if self._last_note else '')

    # ---------------- 节奏控制
    def _tick(self):
        if not self.enabled or self._busy: return
        if getattr(self.manager, 'thinking_now', False): return
        now = time.monotonic()
        idle = float(self._setting('screen_idle_seconds', 45) or 45)
        last_user = float(getattr(self.manager, 'last_user_activity', 0.0) or 0.0)
        if last_user and now - last_user < idle:
            return                                     # 用户刚说过话 → 让位
        gap = float(self._setting('screen_interval', 30) or 30)
        if now - self._last_look < gap: return
        self._busy = True
        self._last_look = now
        asyncio.create_task(self._look_once())

    async def _look_once(self):
        try:
            client = VisionClient(self.manager.config)
            ok, why = client.availability()
            if not ok:
                self.failed.emit(why); return
            try:
                png, info, note = capture_for_config(self.manager.config)
            except Exception as exc:
                self.failed.emit('截图失败：%s' % exc); return
            digest = hashlib.md5(png).hexdigest()
            if self._setting('screen_only_on_change', True) and digest == self._last_hash:
                LOG.info('画面没变化，跳过这次观察')
                return
            self._last_hash = digest
            self._last_note = '%s %dx%d' % (note, info.get('width', 0), info.get('height', 0))
            LOG.info('看屏幕：%s（PNG %d 字节）', self._last_note, len(png))

            hint = str(self._setting('screen_hint', '') or '')
            system = self.manager.screen_system_prompt()
            text = await client.look(png, system, hint)
            self.looks += 1

            # 观察期间用户说话了 → 丢弃结果，绝不盖过用户
            last_user = float(getattr(self.manager, 'last_user_activity', 0.0) or 0.0)
            if last_user and time.monotonic() - last_user < float(self._setting('screen_idle_seconds', 45) or 45):
                LOG.info('观察期间用户发话了，丢弃这次屏幕观察结果')
                return
            if getattr(self.manager, 'thinking_now', False):
                LOG.info('角色正在回复用户的输入，丢弃这次屏幕观察结果')
                return
            self.observed.emit(text)
        except Exception as exc:
            LOG.warning('屏幕观察失败：%s', exc)
            self.failed.emit(str(exc))
        finally:
            self._busy = False
