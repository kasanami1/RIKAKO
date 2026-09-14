import faulthandler
import logging
import sys
import threading
from pathlib import Path

# faulthandler 需要一个常驻的文件对象，否则会被回收、崩溃时就写不出来了。
_FAULT_FILE = None
# Qt 的消息处理器也要留住引用，PySide 不会替你保管。
_QT_HANDLER = None


def early_faulthandler(root):
    """尽可能早地武装 faulthandler —— 必须在 `import PySide6` 之前调用。

    最典型的原生崩溃现场就是「加载 Qt 的 DLL 时炸了」，那时候进程还没走到
    setup_logging，等那儿再开就什么都留不下。pythonw 启动更是连报错窗口都没有。
    """
    global _FAULT_FILE
    if faulthandler.is_enabled():
        return
    try:
        _FAULT_FILE = open(Path(root) / 'app.log', 'a', encoding='utf-8', buffering=1)
        faulthandler.enable(file=_FAULT_FILE, all_threads=True)
    except Exception:
        pass


def _install_faulthandler(path: Path):
    """把「进程级硬崩溃」（访问违例、栈溢出等）的栈也写进 app.log。

    这类崩溃 pythonw 下完全没有控制台可看，不落盘就永久丢失现场。
    """
    global _FAULT_FILE
    if faulthandler.is_enabled():
        return
    try:
        _FAULT_FILE = open(path, 'a', encoding='utf-8', buffering=1)
        faulthandler.enable(file=_FAULT_FILE, all_threads=True)
    except Exception:
        logging.getLogger('app').warning('faulthandler 启动失败', exc_info=True)


def _thread_excepthook(args):
    """子线程里的未捕获异常：默认只打到 stderr，pythonw 下等于没有。"""
    if args.exc_type is SystemExit:
        return
    logging.getLogger('uncaught').error(
        '线程未捕获异常（%s）', getattr(args.thread, 'name', '?'),
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def _install_qt_handler():
    """把 Qt 自己的警告/错误也收进日志（多媒体、字体、平台插件的问题都在这里）。"""
    global _QT_HANDLER
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:
        return
    logger = logging.getLogger('qt')

    def handler(mode, context, message):
        # 消息处理器里绝不能抛异常：Qt 是在任意时刻回调它的，
        # 真抛出去会把整个进程带走，比丢一条日志严重得多。
        try:
            text = str(message)
            if mode in (QtMsgType.QtFatalMsg, QtMsgType.QtCriticalMsg):
                logger.error(text)
            elif mode == QtMsgType.QtWarningMsg:
                logger.info('Qt 警告：%s', text)
            else:
                logger.debug(text)
        except Exception:
            pass

    _QT_HANDLER = handler
    qInstallMessageHandler(handler)


def install_loop_hooks(loop):
    """asyncio 里没人接的异常也记下来（默认会漏掉，或者只在退出时冒一句）。"""
    def handler(active_loop, context):
        exc = context.get('exception')
        logging.getLogger('asyncio').error(
            '事件循环未处理异常：%s', context.get('message'),
            exc_info=exc if isinstance(exc, BaseException) else None)

    try:
        loop.set_exception_handler(handler)
    except Exception:
        logging.getLogger('app').warning('事件循环钩子安装失败', exc_info=True)


class _TeeStderr:
    """把 stderr 同时抄一份进日志。

    正常双击启动走的是 pythonw，根本没有控制台：第三方库（Qt、httpx 底层等）
    往 stderr 打的报错会直接蒸发。抄一份进 app.log，事后才有得查。

    级别按内容粗判：像报错的按 ERROR，其余按 INFO——否则 Qt/FFmpeg 那些
    「停止播放」之类的正常提示会全被记成错误，看日志时反而找不到真问题。
    """

    ERROR_HINTS = ('traceback', 'error', 'exception', 'failed', 'fatal',
                   'cannot', 'refused', 'denied', '失败', '错误', '异常')
    # FFmpeg 每播一段都会把这段媒体信息打到 stderr，属于噪音，不进日志（控制台仍然照打）。
    NOISE_HINTS = ('input #', 'output #', 'duration:', 'stream #', 'bitrate:')

    def __init__(self, stream, logger):
        self._stream, self._logger = stream, logger

    def write(self, text):
        text = str(text)
        if self._stream is not None:
            try: self._stream.write(text)
            except Exception: pass
        stripped = text.strip()
        if stripped:
            low = stripped.lower()
            if any(h in low for h in self.NOISE_HINTS):
                level = logging.DEBUG
            elif any(h in low for h in self.ERROR_HINTS):
                level = logging.ERROR
            else:
                level = logging.INFO
            try: self._logger.log(level, '%s', stripped)
            except Exception: pass
        return len(text)

    def flush(self):
        if self._stream is not None:
            try: self._stream.flush()
            except Exception: pass

    def isatty(self): return False

    def writable(self): return True

    def fileno(self):
        if self._stream is not None: return self._stream.fileno()
        raise OSError('没有可用的文件描述符（pythonw 无控制台）')


def _install_stderr_tee():
    if isinstance(sys.stderr, _TeeStderr):
        return
    try:
        sys.stderr = _TeeStderr(sys.stderr, logging.getLogger('stderr'))
    except Exception:
        logging.getLogger('app').warning('stderr 抄送安装失败', exc_info=True)


def setup_logging(root: Path):
    path = Path(root) / 'app.log'
    handler = logging.FileHandler(path, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    if not any(isinstance(h, logging.FileHandler) and getattr(h, 'baseFilename', '') == str(path) for h in logger.handlers): logger.addHandler(handler)
    def excepthook(exc_type, exc_value, exc_tb):
        logging.getLogger('uncaught').error('未捕获异常', exc_info=(exc_type, exc_value, exc_tb))
    sys.excepthook = excepthook
    # 下面这些补的是「主线程 excepthook 管不到」的几种情况：
    # 子线程异常、事件循环异常、Qt 自己的报错、stderr 输出、进程级硬崩溃。
    # 之前桌宠用 pythonw 启动时，这类报错只会打到并不存在的控制台，等于查无此错。
    threading.excepthook = _thread_excepthook
    _install_faulthandler(path)
    _install_qt_handler()
    _install_stderr_tee()
    return path
