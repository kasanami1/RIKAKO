import sys
import asyncio
import logging
from datetime import datetime
from pathlib import Path

# 先武装崩溃捕捉，再导入 PySide6：DLL 加载阶段的原生崩溃
# （「python.exe - 应用程序错误：该内存不能为 read」那种）只有这样才能留下痕迹。
# app_logging 只用标准库，放在这里不会拖慢启动，也不会互相牵制。
from app_logging import early_faulthandler, install_loop_hooks, setup_logging

early_faulthandler(Path(__file__).parent)

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox
import qasync

from pet_window import PetWindow
from control_panel import ControlPanel
from conversation_manager import ConversationManager
from chat_window import ChatWindow, SpeechBubble
from screen_vision import ScreenWatcher
from sprite_sheet import SpriteSheet
from asset_registry import AssetRegistry


def _log_build_stamp(root):
    """在日志里写下「当前跑的是哪个版本」。

    改完代码忘了重启是很常见的坑：桌宠一直在后台跑着，看到的还是旧行为，
    却以为是改动没生效。记一个源码时间戳就能一眼对出来。
    """
    try:
        newest = max((p.stat().st_mtime for p in Path(root).glob('*.py')), default=0)
        stamp = datetime.fromtimestamp(newest).strftime('%Y-%m-%d %H:%M:%S')
    except OSError:
        stamp = '未知'
    # 顺带记下是哪个解释器跑的：用错 Python（比如系统 Python 而不是 .venv）
    # 导致的「少依赖」，看这一行就能认出来。
    logging.getLogger('app').info('=== 启动 === 源码版本：%s ｜ Python %s ｜ %s',
                                  stamp, sys.version.split()[0], sys.executable)

# pet   = 只启动桌宠（角色 + 气泡 + 聊天输入框）
# panel = 工作台（在桌宠基础上多开一个工作台面板）
MODES = ('pet', 'panel')
PANEL_ALIASES = {'panel', 'workbench', 'gongzuotai', '工作台'}


def parse_mode(argv):
    """默认桌宠模式；只有明确要求 panel/工作台 时才开工作台。"""
    words = {str(a).strip().lower().lstrip('-') for a in argv}
    return 'panel' if words & PANEL_ALIASES else 'pet'


def acquire_single_instance(root):
    """单实例锁。

    拆成两个启动入口后，「桌宠开着又去开工作台」会变成两只桌宠同时在桌面上，
    而且两份进程会抢同一个 sqlite 文件。用文件锁拦住第二个：进程退出（含崩溃）
    时锁由系统自动释放，所以不会留下需要手动清理的死锁状态。
    返回需要保持引用的句柄；拿不到锁时返回 None。
    """
    if sys.platform != 'win32':
        return True
    import msvcrt
    handle = open(Path(root) / '.lilyz.lock', 'a+')
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def _running_marker(root):
    return Path(root) / '.lilyz.running'


def check_previous_run(root, mode):
    """看看上一次是不是「没打招呼就没了」。

    原生崩溃（访问违例那种）根本不会走 Python 的异常处理，日志里可能一条都不留。
    每次启动写一个标记、正常退出时删掉，下次启动就能立刻知道上次是不是崩的。
    """
    marker = _running_marker(root)
    log = logging.getLogger('app')
    try:
        if marker.exists():
            log.warning('上次运行（%s）没有正常退出：多半是崩溃或被强制结束。'
                        '如果刚才你看到过「应用程序错误」弹窗，那说的就是它。',
                        marker.read_text(encoding='utf-8').strip() or '时间未知')
        marker.write_text('%s ｜ %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), mode),
                          encoding='utf-8')
    except OSError:
        log.warning('运行标记读写失败', exc_info=True)


def install_translations(app):
    """装 Qt 自带的中文翻译。

    Qt **不会**按系统语言自动加载 .qm 文件，所以不装这一步时，所有标准按钮
    （QDialogButtonBox 的 Save / Cancel、QMessageBox 的 Yes / No / OK）在中文界面上
    全是英文的——实测系统语言是 zh_CN 也一样。装完就统一成「保存 / 取消 / 是 / 否」。
    """
    try:
        from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
        folder = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        wanted = ['qtbase_%s' % QLocale.system().name()]
        if not wanted[0].endswith('_CN'): wanted.append('qtbase_zh_CN')
        for name in wanted:
            translator = QTranslator(app)          # 挂在 app 上，别被回收
            if translator.load(name, folder):
                app.installTranslator(translator)
                logging.getLogger('app').info('已加载 Qt 翻译：%s', name)
                return True
        logging.getLogger('app').warning('没找到 Qt 中文翻译（目录 %s），按钮会显示英文', folder)
    except Exception:
        logging.getLogger('app').warning('加载 Qt 翻译失败', exc_info=True)
    return False


def main():
    if sys.platform == 'win32' and hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    app = QApplication(sys.argv)
    # Some Windows/font fallback configurations expose a -1 point size.
    # Set a valid application font before constructing any widgets.
    app.setFont(QFont("Segoe UI", 10))
    root = Path(__file__).parent
    setup_logging(root)
    install_translations(app)
    _log_build_stamp(root)
    lock = acquire_single_instance(root)
    if lock is None:
        QMessageBox.information(
            None, '李花子桌宠',
            '桌宠已经在运行了。\n\n'
            '工作台模式里本来就带着桌宠，所以不用再开一个。\n'
            '想进工作台的话，请先关掉正在运行的桌宠窗口。')
        return 0
    mode = parse_mode(sys.argv[1:])
    logging.getLogger('app').info('启动模式：%s', mode)
    check_previous_run(root, mode)
    registry = AssetRegistry(root / "assets")
    character = registry.get_character_list()[0]
    sheet = SpriteSheet(registry.character_root(character))
    pet = PetWindow(sheet)
    manager = ConversationManager(root)
    manager.character = character
    pet.set_track_interval(int(manager.config.get('pet_track_interval', pet.DEFAULT_TRACK_INTERVAL) or pet.DEFAULT_TRACK_INTERVAL))
    # 桌宠大小：三处都能改（右键 →→ 调整大小 / 按住 Ctrl 滚轮 / 工作台滑块），
    # 这里负责「启动时读回来」和「改完写回去」，所以关掉再开还是你调好的大小。
    pet.set_scale(float(manager.config.get('pet_scale', 1.0) or 1.0), persist=False)
    pet.scaleChanged.connect(lambda value: manager.config.update({'pet_scale': round(float(value), 3)}))
    # 表情：模型在回复里指定 [表情:名字]，PetWindow 校验白名单后临时换上、过几秒收回。
    pet.expression_hold_seconds = float(manager.config.get('expression_hold_seconds', 5) or 5)
    manager.expression_requested.connect(pet.set_expression)
    # 待机动作：鼠标长时间不动时，在「没有八方向」的动作之间随机轮播；
    # 鼠标一动就回到八方向的追踪动作。没这类素材时这套逻辑自动失效。
    pet.tracking_motion = str(manager.config.get('tracking_motion', '') or '')
    pet.idle_motions = list(manager.config.get('idle_motions', []) or [])
    pet.idle_after_seconds = float(manager.config.get('idle_after_seconds', 20) or 20)
    pet.idle_motion_seconds = float(manager.config.get('idle_motion_seconds', 10) or 10)
    pet.refresh_idle_plan()
    panel = ControlPanel(sheet, pet, manager) if mode == 'panel' else None
    pet.show()
    pet.start_warmup()      # 后台逐张预热帧缓存，免得转头时撞上首次解码掉帧
    if panel: panel.show()
    # SpeechBubble 没有父窗口，必须保留引用，否则会被回收掉、气泡就不显示了。
    bubble = SpeechBubble(pet)
    chat = ChatWindow(manager, pet.width(), pet)
    manager.reply_received.connect(pet.show_bubble)
    # 接口报错**不当台词**：只写日志 + 在气泡里明确标成「接口出错」，绝不朗读
    # （以前那句「暂时无法连接…HTTP 504…」会被当成回复念出来）。
    def _show_api_error(text):
        logging.getLogger('conversation').warning('接口出错，已在气泡提示：%s', str(text)[:120])
        pet.show_bubble('（接口出错：%s）' % str(text)[:80])
    manager.error_received.connect(_show_api_error)
    manager.thinking.connect(pet.set_talk_state)
    chat.show()
    # 屏幕理解：默认关闭（隐私优先）。开了才按节奏偷看一眼，把看到的内容交给角色去说；
    # 「用户输入优先」的规则在 ScreenWatcher 里把关（用户刚说过话 / 正在回复时绝不看）。
    watcher = ScreenWatcher(manager)
    watcher.observed.connect(lambda text: asyncio.create_task(manager.ambient_say(text)))
    watcher.failed.connect(lambda why: logging.getLogger('screen').info('屏幕观察未完成：%s', why))
    watcher.start()
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    install_loop_hooks(loop)
    try:
        with loop:
            loop.run_forever()
    except BaseException:
        # 包括 KeyboardInterrupt 和启动阶段就抛出来的异常：留个明确的收尾记录，
        # 这样 app.log 里能分清「正常关闭」和「崩了」。
        logging.getLogger('app').exception('事件循环异常退出')
        raise
    finally:
        logging.getLogger('app').info('=== 退出 ===（%s）', mode)
        try:
            _running_marker(root).unlink(missing_ok=True)
        except OSError:
            pass
        if hasattr(manager, 'close'):
            manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
