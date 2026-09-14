# -*- coding: utf-8 -*-
"""李花子桌宠 · 语音输入（录音 + 语音识别）。

设计原则和项目其它部分保持一致：能不加依赖就不加，识别服务做成可插拔，
失败时如实说明原因，而不是假装听懂了。

两条识别路线：

1. windows —— Windows 自带的离线识别引擎（System.Speech），**零安装、零 Key、可离线**。
   前提是系统装了对应语言的识别引擎（中文机器通常自带
   "Microsoft Speech Recognizer 8.0 for Windows (Chinese Simplified)"）。
2. openai —— 任何 OpenAI 兼容的 /v1/audio/transcriptions 接口，例如
   硅基流动 SiliconFlow（有免费额度）、Groq、智谱等，准确率明显更好，但需要 Key。

录音统一成 16kHz 单声道 16bit 的 WAV：这是各家识别服务都吃的通用格式。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import wave
from abc import ABC, abstractmethod
from array import array
from pathlib import Path
from typing import Optional

LOG = logging.getLogger('speech')

# 随项目附带的本地语音模型目录（vosk 中文小模型，约 42MB）。
VOSK_MODEL_DIR = Path(__file__).resolve().parent / 'models' / 'vosk-model-small-cn-0.22'

# 录音超过这个时长自动停止，避免用户忘了按停、文件无限增长。
MAX_RECORD_SECONDS = 30


# ---------------------------------------------------------------------------
# 音频预处理
# ---------------------------------------------------------------------------
def preprocess_wav(path, target_peak=0.85, min_rms=80, max_rms=2500, guard_ms=150, pad_ms=100):
    """去掉首尾静音，并把音量拉到合适区间。原地改写 WAV。

    识别引擎对「一长段静音 + 偏小的说话声」很敏感：静音会让端点检测跑偏，
    音量偏低则字音糊在一起。这一步纯标准库实现（array 模块），不引入 numpy。

    **门限不能拿峰值当参考**：实测出现过峰值直接顶到满量程 32768（削顶）的录音，
    按峰值的比例算出来的门限比正常说话还高，于是把真正的语音整段剪掉、只留下几个
    爆音——识别结果自然全是错的。所以改用第 90 百分位当参考，并且给门限设绝对上限。
    """
    try:
        with wave.open(str(path), 'rb') as handle:
            channels, width, rate, count = handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getnframes()
            frames = handle.readframes(count)
    except (OSError, wave.Error) as exc:
        LOG.warning('预处理读取失败：%s', exc); return False
    if width != 2 or not frames: return False
    samples = array('h'); samples.frombytes(frames)
    step = max(1, int(rate * 0.02)) * channels          # 20ms 一帧
    chunks = [samples[i:i + step] for i in range(0, len(samples), step)]
    levels = []
    for chunk in chunks:
        if not chunk: levels.append(0); continue
        total = 0
        for value in chunk: total += value * value
        levels.append(int((total / len(chunk)) ** 0.5))
    peak = max(levels) if levels else 0
    if peak <= 0 or not levels: return False
    # 用第 90 百分位当「说话音量」的参考，避免单个削顶尖峰把门限整体抬高。
    ordered = sorted(levels)
    reference = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
    if reference <= 0: return False
    threshold = max(min_rms, min(int(reference * 0.12), max_rms))
    keep = [i for i, level in enumerate(levels) if level >= threshold]
    if not keep: return False
    guard = max(1, guard_ms // 20)
    start = max(0, keep[0] - guard) * step              # 前后各留一点，别把字头切掉
    end = min(len(samples), (keep[-1] + guard + 1) * step)
    out = samples[start:end]
    if len(out) < rate // 4: return False               # 太短就别折腾了
    top = max(abs(value) for value in out)
    gain = 1.0
    if top > 0:
        # 上限放到 8 倍：麦克风增益偏低时（实测有到满量程 9% 的）4 倍根本不够。
        gain = min(8.0, (target_peak * 32767) / top)
        if gain > 1.02:
            out = array('h', [max(-32768, min(32767, int(value * gain))) for value in out])
        else:
            gain = 1.0
    if pad_ms:
        out = array('h', [0] * (int(rate * pad_ms / 1000) * channels)) + out   # 前置静音，给引擎一个起始参考
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(channels); handle.setsampwidth(width); handle.setframerate(rate)
        handle.writeframes(out.tobytes())
    LOG.info('音频预处理：%.1f 秒 -> %.1f 秒，峰值 %d -> %d，门限 %d（音量放大 %.1f 倍）',
             len(samples) / float(channels) / rate, len(out) / float(channels) / rate,
             peak, min(32767, int(top * gain)), threshold, gain)
    if peak >= 32000:
        LOG.warning('录音峰值贴到满量程（%d），说明麦克风输入过载削顶——'
                    '去系统声音设置里把输入音量调低，识别率会明显改善', peak)
    return True


# ---------------------------------------------------------------------------
# 录音（QtMultimedia，不引入新依赖）
# ---------------------------------------------------------------------------
class Recorder:
    """把麦克风录成 16kHz 单声道 16bit 的 WAV。

    Qt 给的是裸 PCM，需要自己写 WAV 头——标准库 wave 就够了，不必装 numpy。
    """

    def __init__(self, sample_rate=16000):
        self.sample_rate = sample_rate
        self.buffer = bytearray()
        self.path: Optional[Path] = None
        self._source = None
        self._device = None
        self._io = None
        self.actual_rate = sample_rate
        self.actual_channels = 1

    # ---------------------------------------------------------------- 设备
    @staticmethod
    def input_devices():
        from PySide6.QtMultimedia import QMediaDevices
        return list(QMediaDevices.audioInputs())

    @classmethod
    def availability(cls):
        """返回 (能不能录, 说明)。"""
        try:
            devices = cls.input_devices()
        except Exception as exc:
            return False, '无法访问音频设备：%s' % exc
        if not devices:
            return False, '没有找到麦克风（系统音频输入设备列表为空）'
        return True, '麦克风：' + devices[0].description()

    # ---------------------------------------------------------------- 录制
    def start(self):
        from PySide6.QtCore import QIODevice
        from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
        ok, why = self.availability()
        if not ok: raise RuntimeError(why)
        device = QMediaDevices.defaultAudioInput()
        wanted = QAudioFormat()
        wanted.setSampleRate(self.sample_rate)
        wanted.setChannelCount(1)
        wanted.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not device.isFormatSupported(wanted):
            # 设备不支持 16k 单声道就退回它自己的首选格式，WAV 头按实际参数写。
            wanted = device.preferredFormat()
            LOG.info('设备不支持 16kHz/单声道，改用首选格式：%s Hz / %s 声道',
                     wanted.sampleRate(), wanted.channelCount())
        self.actual_rate = wanted.sampleRate()
        self.actual_channels = wanted.channelCount()
        self.buffer = bytearray()
        self._device = device
        self._source = QAudioSource(device, wanted)
        self._io = self._source.start()
        if self._io is None:
            self._source = None
            raise RuntimeError('麦克风无法开始录音（可能是被其它程序独占）')
        self._io.readyRead.connect(self._pull)

    def _pull(self):
        if self._io is None: return
        data = self._io.readAll()
        if data and data.size():
            self.buffer.extend(bytes(data.data()))

    def stop(self):
        """停止录音并落盘成 WAV，返回文件路径（没录到东西返回 None）。"""
        try:
            self._pull()
            if self._source is not None: self._source.stop()
        finally:
            self._source = None; self._io = None
        if not self.buffer: return None
        handle = tempfile.NamedTemporaryFile(prefix='lilyz_voice_', suffix='.wav', delete=False)
        handle.close()
        self.path = Path(handle.name)
        with wave.open(str(self.path), 'wb') as wav:
            wav.setnchannels(self.actual_channels)
            wav.setsampwidth(2)
            wav.setframerate(self.actual_rate)
            wav.writeframes(bytes(self.buffer))
        # 识别前先收拾音频：这一步对离线引擎的准确率影响很明显。
        preprocess_wav(self.path)
        LOG.info('录音完成：%.1f 秒，%d 字节 -> %s',
                 len(self.buffer) / 2.0 / max(1, self.actual_channels) / max(1, self.actual_rate),
                 self.path.stat().st_size, self.path)
        return self.path

    @property
    def seconds(self):
        return len(self.buffer) / 2.0 / max(1, self.actual_channels) / max(1, self.actual_rate)

    @staticmethod
    def cleanup(path):
        try:
            if path: Path(path).unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 语音识别
# ---------------------------------------------------------------------------
class Transcriber(ABC):
    name = ''

    def availability(self, language=None):
        """返回 (能不能用, 说明)，用于设置界面和启动自检。"""
        return True, ''

    @abstractmethod
    def transcribe(self, wav_path, language='zh-CN'):
        """同步识别，返回文本（听不清返回空字符串）。仅 Windows 离线引擎用。"""

    async def transcribe_async(self, wav_path, language='zh-CN'):
        """异步入口：云端走 httpx，离线引擎丢到线程池，避免卡住界面。"""
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: self.transcribe(wav_path, language))


# PowerShell 脚本：识别结果写进文件，而不是靠 stdout——
# 中文在控制台管道里很容易因为代码页问题变成乱码，走文件最稳。
_WINDOWS_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$out = $env:LILYZ_ASR_OUT
try {
  Add-Type -AssemblyName System.Speech
  $culture = [System.Globalization.CultureInfo]::GetCultureInfo($env:LILYZ_ASR_LANG)
  $engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine($culture)
  $engine.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
  $engine.SetInputToWaveFile($env:LILYZ_ASR_WAV)
  $result = $engine.Recognize()
  $text = ''
  if ($result) { $text = $result.Text }
  $engine.Dispose()
  [System.IO.File]::WriteAllText($out, 'OK' + [char]9 + $text, [System.Text.UTF8Encoding]::new($false))
} catch {
  [System.IO.File]::WriteAllText($out, 'ERR' + [char]9 + $_.Exception.Message, [System.Text.UTF8Encoding]::new($false))
}
'''


class WindowsOfflineTranscriber(Transcriber):
    """用 Windows 自带引擎离线识别：不用装包、不用 Key、不联网。"""

    name = 'windows'

    @staticmethod
    def _run(args, **kwargs):
        """调用 PowerShell；pythonw 下不能弹出黑窗口。"""
        if sys.platform == 'win32':
            kwargs.setdefault('creationflags', getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return subprocess.run(args, **kwargs)

    def _run_ps(self, script, extra_env=None, timeout=120):
        """跑一段 PowerShell，结果经 UTF-8 文件取回。

        不走 stdout：中文在控制台管道里会因为代码页变成乱码（实测踩过）。
        注意 mkstemp 会返回一个打开的文件描述符，必须关掉，否则临时文件删不掉
        （Windows 会报「另一个程序正在使用此文件」）。
        """
        handle, name = tempfile.mkstemp(prefix='lilyz_ps_', suffix='.txt')
        os.close(handle)
        out = Path(name)
        env = dict(os.environ); env['LILYZ_ASR_OUT'] = str(out)
        if extra_env: env.update(extra_env)
        try:
            self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
                      capture_output=True, timeout=timeout, env=env)
            return out.read_text(encoding='utf-8', errors='replace') if out.exists() else ''
        finally:
            out.unlink(missing_ok=True)

    def _recognizers(self):
        """列出系统装了哪些离线识别引擎（语言代码，纯 ASCII，不受代码页影响）。"""
        script = ("Add-Type -AssemblyName System.Speech;"
                  "[System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()"
                  " | ForEach-Object { $_.Culture.Name }")
        try:
            done = self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
                             capture_output=True, timeout=30)
            return [line.strip() for line in done.stdout.decode('utf-8', 'replace').splitlines() if line.strip()]
        except Exception as exc:
            LOG.warning('查询离线识别引擎失败：%s', exc)
            return []

    _probe_script = (
        "Add-Type -AssemblyName System.Speech;"
        "$out = $env:LILYZ_ASR_OUT;"
        "try {"
        "  $e = New-Object System.Speech.Recognition.SpeechRecognitionEngine([System.Globalization.CultureInfo]::GetCultureInfo($env:LILYZ_ASR_LANG));"
        "  $e.Dispose(); $t = 'OK'"
        "} catch { $t = 'ERR' + ($_.Exception.Message -replace \"`r|`n\", ' ') }"
        "[System.IO.File]::WriteAllText($out, $t, [System.Text.UTF8Encoding]::new($false))"
    )

    def availability(self, language='zh-CN'):
        """真的试着构造一次引擎。

        只查 InstalledRecognizers() 会撒谎：引擎可能列在那里，但构造时因权限
        （E_ACCESSDENIED）或语言包不完整而失败。设置界面不能报「可用」却发现用不了。
        """
        if sys.platform != 'win32':
            return False, 'Windows 离线识别只能在 Windows 上使用'
        available = self._recognizers()
        if not available:
            return False, '系统没有安装离线识别引擎（可在「设置 → 时间和语言 → 语音」里添加）'
        try:
            out = self._run_ps(self._probe_script, {'LILYZ_ASR_LANG': language or 'zh-CN'}, timeout=40).strip()
        except Exception as exc:
            return False, '无法调用系统识别引擎：%s' % exc
        if out.startswith('OK'):
            return True, '系统离线识别可用（已安装：%s）' % '、'.join(available)
        reason = out[3:].strip() or '未知原因'
        if 'ACCESSDENIED' in reason.upper() or '拒绝访问' in reason or 'Access is denied' in reason:
            reason = '系统拒绝了识别引擎的访问（E_ACCESSDENIED）——常见于以管理员或受限身份运行'
        return False, '离线识别引擎无法启动：%s' % reason.splitlines()[0][:160]

    def transcribe(self, wav_path, language='zh-CN'):
        if sys.platform != 'win32': raise RuntimeError('Windows 离线识别只能在 Windows 上使用')
        raw = self._run_ps(_WINDOWS_SCRIPT,
                           {'LILYZ_ASR_WAV': str(wav_path), 'LILYZ_ASR_LANG': language or 'zh-CN'}).strip()
        if not raw: raise RuntimeError('离线识别没有返回结果')
        status, _, text = raw.partition('\t')
        if status.startswith('ERR'):
            raise RuntimeError('离线识别失败：%s' % text.strip())
        return text.strip()


class VoskTranscriber(Transcriber):
    """本地离线识别（vosk / Kaldi 中文小模型）。

    完全离线、不联网、不用 Key。模型约 42MB，比 Windows 自带的 SAPI 引擎新得多，
    短句识别明显更准；缺点是长句和生僻词不如云端大模型。
    """

    name = 'vosk'
    _models = {}          # 加载模型要一两秒，按路径缓存复用，否则每次说话都重载

    def __init__(self, model_path=''):
        self.model_path = str(model_path or VOSK_MODEL_DIR)

    def availability(self, language=None):
        try:
            import vosk  # noqa: F401
        except Exception:
            return False, '没有安装 vosk（在项目目录执行：.venv\\Scripts\\python.exe -m pip install vosk）'
        path = Path(self.model_path)
        if not path.is_dir():
            return False, '没有找到语音模型目录：%s' % path
        # 真的加载一次：只查目录存在会「谎报可用」，而 vosk 恰恰有中文路径这个坑。
        try:
            self._model()
        except Exception as exc:
            return False, '模型无法加载：%s' % str(exc)[:120]
        return True, '本地模型：%s（已成功加载）' % path.name

    def _model(self):
        import vosk
        vosk.SetLogLevel(-1)          # 否则 vosk 会往控制台刷一堆日志
        model = VoskTranscriber._models.get(self.model_path)
        if model is None:
            model = self._load_model(vosk, self.model_path)
            VoskTranscriber._models[self.model_path] = model
        return model

    @staticmethod
    def _load_model(vosk, model_path):
        """加载模型，并绕开 vosk 不支持中文路径的问题。

        vosk 的 C++ 层按系统 ANSI 代码页解析路径：绝对路径里只要带中文就会报
        「does not contain model files」。本项目路径本身就含中文，必然踩到；
        Windows 短路径也救不了（中文目录的短名仍是中文）。
        所以路径全 ASCII 就直接用，否则临时切到模型目录、只把纯 ASCII 的目录名
        交给它——chdir 走的是宽字符 API，中文不受影响。
        """
        path = Path(model_path).resolve()
        if all(ord(char) < 128 for char in str(path)):
            return vosk.Model(str(path))
        previous = os.getcwd()
        try:
            os.chdir(str(path.parent))
            return vosk.Model(path.name)
        finally:
            os.chdir(previous)

    def transcribe(self, wav_path, language='zh-CN'):
        import vosk
        model = self._model()
        with wave.open(str(wav_path), 'rb') as handle:
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                raise RuntimeError('vosk 需要单声道 16bit 的 WAV（录音默认就是这个格式）')
            recognizer = vosk.KaldiRecognizer(model, handle.getframerate())
            while True:
                data = handle.readframes(4000)
                if not data: break
                recognizer.AcceptWaveform(data)
        result = json.loads(recognizer.FinalResult() or '{}')
        # vosk 的中文结果按词切分、中间带空格，去掉才是自然中文。
        return str(result.get('text') or '').replace(' ', '').strip()


class OpenAICompatTranscriber(Transcriber):
    """任何 OpenAI 兼容的 /v1/audio/transcriptions 接口。

    实测可用：硅基流动（FunAudioLLM/SenseVoiceSmall，有免费额度）、Groq（whisper-large-v3）。
    """

    name = 'openai'

    def __init__(self, base_url='', api_key='', model='', timeout=60):
        self.base_url = (base_url or '').strip().rstrip('/')
        self.api_key = (api_key or '').strip()
        self.model = (model or '').strip()
        self.timeout = float(timeout or 60)

    def endpoint(self):
        base = self.base_url
        if not base: return ''
        return base + '/audio/transcriptions' if base.endswith('/v1') else base + '/v1/audio/transcriptions'

    def availability(self, language=None):
        if not self.endpoint(): return False, '没有填写识别接口地址'
        if not self.api_key: return False, '没有填写识别 API Key'
        if not self.model: return False, '没有填写识别模型名'
        return True, '接口：%s' % self.endpoint()

    def transcribe(self, wav_path, language='zh-CN'):
        import httpx
        ok, why = self.availability()
        if not ok: raise RuntimeError(why)
        path = Path(wav_path)
        data = {'model': self.model}
        if language: data['language'] = language.split('-')[0]
        with path.open('rb') as handle:
            files = {'file': (path.name, handle, 'audio/wav')}
            response = httpx.post(self.endpoint(), headers={'Authorization': 'Bearer ' + self.api_key},
                                  files=files, data=data, timeout=self.timeout)
        if response.status_code >= 400 and 'language' in data:
            # 有的服务不认 language 参数，去掉再试一次。
            data.pop('language')
            with path.open('rb') as handle:
                files = {'file': (path.name, handle, 'audio/wav')}
                response = httpx.post(self.endpoint(), headers={'Authorization': 'Bearer ' + self.api_key},
                                      files=files, data=data, timeout=self.timeout)
        if response.status_code >= 400:
            raise RuntimeError('识别接口返回 HTTP %s：%s' % (response.status_code, response.text[:200]))
        try:
            payload = response.json()
        except ValueError:
            return response.text.strip()
        if isinstance(payload, dict):
            if isinstance(payload.get('text'), str): return payload['text'].strip()
            choices = payload.get('choices') or []
            if choices:
                message = choices[0].get('message') or {}
                return str(message.get('content') or '').strip()
        return ''


def transcriber_from_config(config):
    """按 config 里的 asr_provider 造一个识别器。config 可以是 ConfigManager 或 dict。"""
    def setting(key, default=None):
        if config is None: return default
        getter = getattr(config, 'get', None)
        return getter(key, default) if callable(getter) else config.get(key, default)

    provider = str(setting('asr_provider', 'vosk') or 'vosk').lower()
    language = str(setting('asr_language', 'zh-CN') or 'zh-CN')
    if provider in ('off', 'none', '关闭'):
        return None, language
    if provider == 'vosk':
        return VoskTranscriber(setting('asr_vosk_model', '')), language
    if provider in ('openai', 'cloud', 'compatible', 'third_party'):
        return OpenAICompatTranscriber(setting('asr_url', ''), setting('asr_api_key', ''),
                                       setting('asr_model', ''), setting('asr_timeout', 60)), language
    return WindowsOfflineTranscriber(), language


def probe(config=None):
    """给设置界面的「检测语音输入」用：返回一段人话报告。"""
    lines = []
    ok, why = Recorder.availability()
    lines.append('录音设备：' + ('可用，' + why if ok else '不可用，' + why))
    transcriber, language = transcriber_from_config(config)
    if transcriber is None:
        lines.append('语音识别：已关闭')
    else:
        tok, twhy = transcriber.availability(language)
        lines.append('语音识别（%s）：%s' % (transcriber.name, twhy if tok else '不可用，' + twhy))
    lines.append('识别语言：%s' % language)
    return '\n'.join(lines)
