# -*- coding: utf-8 -*-
"""李花子桌宠 · 角色语音（文字转语音）。

用阿里云百炼（DashScope）的 Qwen-TTS 把角色回复读出来，并且支持自建音色：

* **系统音色**：直接用平台预置音色名（Cherry、Ethan 等），零准备。
* **声音设计**：用一句自然语言描述音色（例如「年轻女性，语速偏快，语调上扬」），
  平台据此生成一个专属音色并返回音色名，之后合成时传这个音色即可。
  接口：POST /api/v1/services/audio/tts/customization，model=qwen-voice-design。
* **声音复刻**：拿手里现成的录音（10~20 秒最合适）复刻出一个音色。
  接口：同一个地址，model=qwen-voice-enrollment，音频以 Data URL 提交。

播放用 PySide6 自带的 QtMultimedia，不需要额外依赖。
"""

from __future__ import annotations

import array
import asyncio
import base64
import binascii
import io
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

LOG = logging.getLogger('voice')

# 北京地域的通用域名（文档里提到的 {WorkspaceId}.cn-beijing.maas.aliyuncs.com
# 是业务空间专属域名，但官方说明「现有域名仍可正常使用」，这里用通用域名免去填 Workspace ID）。
DEFAULT_BASE_URL = 'https://dashscope.aliyuncs.com'
TTS_PATH = '/api/v1/services/aigc/multimodal-generation/generation'
CUSTOM_PATH = '/api/v1/services/audio/tts/customization'
# Qwen-Audio-TTS / CosyVoice 走的是另一个接口（不是上面那个多模态生成接口），
# 两条路的参数名也不同：这条路用 input.instruction（单数），Qwen-TTS 用 instructions。
AUDIO_TTS_PATH = '/api/v1/services/audio/tts/SpeechSynthesizer'
# 业务空间专属域名（官方推荐）：填了 Workspace ID 就用它，留空走通用域名。
# 实测通用域名仍然服务这个接口，所以不填也能用，省一步。
AUDIO_TTS_HOST = '{workspace}.cn-beijing.maas.aliyuncs.com'

# 声音设计（自建音色）默认用的驱动模型：建出来的音色就用它合成，
# 合成时 model 必须与之一致，否则接口会报错。
DEFAULT_DESIGN_TARGET = 'qwen3-tts-vd-2026-01-26'
# 声音复刻默认用的驱动模型（非实时版，走 HTTP 合成）。
DEFAULT_CLONE_TARGET = 'qwen3-tts-vc-2026-01-22'

# 声音复刻的音频要求（官方：Qwen-TTS 声音复刻）。
CLONE_MAX_BYTES = 10 * 1024 * 1024
CLONE_MIN_SECONDS = 3.0
CLONE_MAX_SECONDS = 60.0
CLONE_BEST_SECONDS = (10.0, 20.0)
CLONE_MIN_RATE = 24000
CLONE_MIME = {'.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4'}
# 目标采样率：低于 24kHz 会被接口拒绝，统一重采样到 24kHz。
CLONE_RATE = 24000

# 常用系统音色（中文）；平台还有更多，可直接手填音色名。
SYSTEM_VOICES = (
    ('Cherry', '芊悦 · 阳光积极的女声'),
    ('Serena', '苏瑶 · 温柔女声'),
    ('Ethan', '晨煦 · 阳光男声'),
    ('Chelsie', '千雪 · 二次元女声'),
    ('Momo', '茉兔 · 撒娇搞怪女声'),
    ('Nofish', '不吃鱼 · 设计师男声'),
    ('Dylan', '北京话男声'),
    ('Sunny', '四川话女声'),
)

# Qwen-Audio-TTS 的系统音色（和上面那批**不通用**：音色只属于某个模型，
# 拿 Qwen-TTS 的音色去打这个接口会回 `Engine error [411]`）。
# 另外它还有 500 多个基础音色，名字形如 `qwen-audio-3.0-tts-flash-<后缀>`，
# 官方文档里有 Excel 清单和试听包，这里只列中文的几个常用项。
AUDIO_TTS_MODELS = (
    ('qwen-audio-3.0-tts-flash', 'Flash · 快、便宜；系统音色+复刻音色都认语气'),
    ('qwen-audio-3.0-tts-plus', 'Plus · 音质与表现力更好、更贵'),
)
AUDIO_TTS_VOICES = (
    ('longanfengyue', '龙安风月 · 自然亲切的女声（30 岁）'),
    ('longanyuanfei', '龙安元妃 · 高傲威严的女声（30 岁）'),
    ('longanlingxi', '龙安凌汐 · 可爱甜的女声（25 岁）'),
    ('longanxiaoxin', '龙安心心 · 亲切活泼的女声（22 岁）'),
    ('longanhuan_v3.6', '龙安欢 · 女声（25 岁）'),
    ('longpaopao_v3.6', '龙泡泡 · 软萌童声（5 岁 女）'),
    ('longjielidou_v3.6', '龙杰力豆 · 天真童声（5 岁 男）'),
    ('longhuohuo_v3.6', '龙火火 · 顽皮童声（8 岁 男）'),
    ('longchuanshu_v3.6', '龙川蜀 · 四川口音中年男声（40 岁）'),
    ('loongeva_v3.6', 'loongeva · 美国口音女声（28 岁）'),
    ('loongjohn', 'loongjohn · 美国口音男声（28 岁）'),
    ('loongmary', 'loongmary · 英式口音女声（20 岁）'),
)
AUDIO_TTS_LANGS = (
    ('zh', '中文'), ('en', '英文'), ('ja', '日文'), ('ko', '韩文'), ('ru', '俄文'),
    ('fr', '法文'), ('de', '德文'), ('es', '西班牙文'), ('it', '意大利文'),
    ('pt', '葡萄牙文'), ('th', '泰文'), ('id', '印尼文'), ('vi', '越南文'),
    ('ms', '马来文'), ('fil', '菲律宾文'), ('ar', '阿拉伯文'),
)
# 语气指令的长度限制：中日韩文字按 2 个字计，其余按 1 个，上限 100。
AUDIO_TTS_INSTRUCTION_LIMIT = 100


class TTSError(RuntimeError):
    pass


def _keys(payload: Dict[str, Any], *names):
    for name in names:
        value = payload.get(name)
        if value: return value
    return None


# --------------------------------------------------------------------- 录音体检
# 复刻效果几乎完全取决于这段录音。上传前先量一量，能本地修的（声道、采样率）
# 就修好，修不了的（太短、太长、几乎没声音）直接说清楚——不然就是白花一次调用。

def _int16_from_raw(raw: bytes, width: int) -> array.array:
    """把任意位宽的 PCM 数据统一成 int16 采样。"""
    if width == 1:                                  # 8bit 是无符号的
        return array.array('h', [(b - 128) << 8 for b in raw])
    if width == 2:
        out = array.array('h'); out.frombytes(raw[:len(raw) // 2 * 2]); return out
    if width == 3:                                  # 24bit 小端有符号
        out = array.array('h', bytes(len(raw) // 3 * 2))
        for i in range(len(out)):
            value = int.from_bytes(raw[i * 3:i * 3 + 3], 'little', signed=True)
            out[i] = value >> 8
        return out
    if width == 4:                                  # 32bit 小端有符号
        wide = array.array('i'); wide.frombytes(raw[:len(raw) // 4 * 4])
        return array.array('h', [v >> 16 for v in wide])
    raise TTSError('不支持的位宽：%d 字节/采样' % width)


def _resample_linear(samples: array.array, rate: int, target: int) -> array.array:
    """线性插值重采样。只用于把低于 24kHz 的录音补到接口要求的采样率。"""
    if rate == target or not samples: return samples
    ratio = float(rate) / float(target)
    count = int(len(samples) / ratio)
    out = array.array('h', bytes(count * 2))
    last = len(samples) - 1
    for i in range(count):
        pos = i * ratio
        left = int(pos)
        frac = pos - left
        right = left + 1 if left < last else last
        out[i] = int(samples[left] + (samples[right] - samples[left]) * frac)
    return out


def _to_mono(samples: array.array, channels: int) -> array.array:
    """多声道混合成单声道（取各声道平均，人声一般在中间，比只留首声道更稳）。"""
    if channels <= 1: return samples
    count = len(samples) // channels
    out = array.array('h', bytes(count * 2))
    for i in range(count):
        base = i * channels
        out[i] = int(sum(samples[base:base + channels]) / channels)
    return out


def probe_clone_audio(path) -> Dict[str, Any]:
    """体检一段待复刻的录音，不上传、不修改文件。

    返回的 errors 是「必须解决」的，notes 是「提醒」，fixable 是本地能自动修的项。
    """
    info: Dict[str, Any] = {'path': str(path), 'exists': False, 'size': 0, 'ext': '',
                            'mime': '', 'seconds': None, 'rate': None, 'channels': None,
                            'peak': None, 'errors': [], 'notes': [], 'fixable': []}
    file = Path(path)
    info['ext'] = file.suffix.lower()
    if not file.exists():
        info['errors'].append('文件不存在')
        return info
    info['exists'] = True
    info['size'] = file.stat().st_size
    info['mime'] = CLONE_MIME.get(info['ext'], '')
    if not info['mime']:
        info['errors'].append('只支持 WAV / MP3 / M4A，当前是 %s' % (info['ext'] or '无扩展名'))
    if info['size'] > CLONE_MAX_BYTES:
        info['errors'].append('文件 %.1f MB，超过接口上限 10 MB' % (info['size'] / 1048576.0))
    if info['size'] == 0:
        info['errors'].append('文件是空的')

    if info['ext'] != '.wav':
        info['notes'].append('MP3 / M4A 没有解码器、本地读不出时长和采样率，'
                             '这些只能交给接口判断（转成 WAV 就能本地体检）')
        return info
    try:
        with wave.open(str(file), 'rb') as fh:
            channels, width, rate, frames = (fh.getnchannels(), fh.getsampwidth(),
                                             fh.getframerate(), fh.getnframes())
            raw = fh.readframes(frames)
    except Exception as exc:
        info['errors'].append('WAV 读不出来（%s）；若是 32bit 浮点等非常规格式，'
                              '请先转成 16bit PCM' % exc)
        return info

    info['channels'], info['rate'] = channels, rate
    info['seconds'] = frames / float(rate) if rate else 0.0
    samples = _int16_from_raw(raw, width)
    info['peak'] = max(max(samples), -min(samples)) if samples else 0

    if info['seconds'] < CLONE_MIN_SECONDS:
        info['errors'].append('只有 %.1f 秒，接口要求至少 3 秒连续人声' % info['seconds'])
    elif info['seconds'] > CLONE_MAX_SECONDS:
        info['errors'].append('有 %.1f 秒，超过接口上限 60 秒（建议 10~20 秒）' % info['seconds'])
    elif not (CLONE_BEST_SECONDS[0] <= info['seconds'] <= CLONE_BEST_SECONDS[1]):
        info['notes'].append('长度 %.1f 秒，官方推荐 10~20 秒，这个范围复刻最稳' % info['seconds'])
    if info['peak'] is not None and info['peak'] < 500:
        info['errors'].append('这段录音几乎没有声音（峰值 %d/32767），先确认录到了人声' % info['peak'])
    elif info['peak'] is not None and info['peak'] >= 32767:
        info['notes'].append('录音有削顶（峰值已到顶），可能有些爆音')
    if channels > 1:
        info['fixable'].append('双声道（%d 声道）→ 自动混合成单声道' % channels)
    if rate < CLONE_MIN_RATE:
        info['fixable'].append('%d Hz 低于接口要求的 %d Hz → 自动重采样'
                               '（插值补点，不会真的变清晰）' % (rate, CLONE_MIN_RATE))
    if width != 2:
        info['fixable'].append('%d 字节/采样 → 自动转成 16bit' % width)
    return info


def prepare_clone_audio(path, trim_seconds: Optional[float] = None
                        ) -> Tuple[bytes, str, List[str]]:
    """把录音收拾成接口要的样子，返回 (字节, MIME, 说明)。

    WAV 会转成「单声道 / 16bit / ≥24kHz」；MP3、M4A 原样上传（无解码器，
    本地改不了）。trim_seconds 用于文件超长时截取开头一段。
    """
    file = Path(path)
    info = probe_clone_audio(file)
    if not info['exists']:
        raise TTSError('文件不存在：%s' % file)
    if not info['mime']:
        raise TTSError('只支持 WAV / MP3 / M4A，当前是 %s' % (info['ext'] or '无扩展名'))
    if info['size'] > CLONE_MAX_BYTES:
        raise TTSError('文件 %.1f MB 超过接口上限 10 MB，请先压缩或剪短'
                       % (info['size'] / 1048576.0))
    if info['ext'] != '.wav':
        # 无解码器：原样上传，能体检的只有扩展名和大小。
        return file.read_bytes(), info['mime'], list(info['notes'])
    # 传了 trim_seconds 就是已经决定截短了，「太长」这条不该再拦人。
    errors = [e for e in info['errors'] if not (trim_seconds and '60 秒' in e)]
    if errors:
        raise TTSError('；'.join(errors))

    with wave.open(str(file), 'rb') as fh:
        channels, width, rate, frames = (fh.getnchannels(), fh.getsampwidth(),
                                         fh.getframerate(), fh.getnframes())
        raw = fh.readframes(frames)

    notes: List[str] = []
    samples = _to_mono(_int16_from_raw(raw, width), channels)
    if channels > 1:
        notes.append('已把 %d 声道混合成单声道' % channels)
    if width != 2:
        notes.append('已转成 16bit')
    if trim_seconds:
        keep = int(trim_seconds * rate)
        if 0 < keep < len(samples):
            samples = samples[:keep]
            notes.append('已截取开头 %.0f 秒' % trim_seconds)
    if rate < CLONE_MIN_RATE:
        samples = _resample_linear(samples, rate, CLONE_RATE)
        notes.append('已把 %d Hz 重采样到 %d Hz（插值补点）' % (rate, CLONE_RATE))
        rate = CLONE_RATE

    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(rate)
        out.writeframes(samples.tobytes())
    data = buffer.getvalue()
    if len(data) > CLONE_MAX_BYTES:
        raise TTSError('转成 WAV 后 %.1f MB，超过接口上限 10 MB，请先剪短'
                       % (len(data) / 1048576.0))
    notes.append('已整理为 单声道 / 16bit / %d Hz / %.1f 秒'
                 % (rate, len(samples) / float(rate)))
    return data, 'audio/wav', notes


class QwenTTS:
    """Qwen-TTS 语音合成。"""

    # 云端一次返回整段、很快，不需要按句拆（拆了反而多花几次请求）。
    sentence_pipeline = False

    # 哪些模型真的认「语气指令」——这是实测出来的，不是看文档猜的：
    # 同一句话各发两次（不发指令 / 发「请用非常缓慢的速度朗读，每个字都拖长，声音低沉」），
    # 用「区间是否重叠」判断，避免把随机波动当成效果：
    #   qwen3-tts-flash         不加 3.28~3.92 秒，慢 3.28~3.36 秒 → 重叠，**不认**
    #   qwen3-tts-instruct-flash 不加 3.60~3.76 秒，慢 5.92~6.48 秒 → 明显变慢，**认**
    #                           同模型「快、急促」2.64~2.80 秒 → 也会变快（可开/不开润色）
    #   qwen3-tts-vc-2026-01-22 不加 3.76~4.16 秒，慢 3.68~4.80 秒 → 重叠，**不认**
    # 也就是说：想用语气，就得用 instruct 模型 + 系统音色；复刻出来的音色没有语气。
    INSTRUCT_MODELS = ('qwen3-tts-instruct',)

    @property
    def supports_tone(self):
        name = (self.model or '').lower()
        return any(name.startswith(prefix) for prefix in self.INSTRUCT_MODELS)

    def __init__(self, api_key='', model='qwen3-tts-flash', voice='Cherry',
                 base_url=DEFAULT_BASE_URL, language='Chinese', timeout=60,
                 instructions='', optimize_instructions=False):
        self.api_key = (api_key or '').strip()
        self.model = (model or 'qwen3-tts-flash').strip()
        self.voice = (voice or 'Cherry').strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).strip().rstrip('/')
        self.language = (language or 'Chinese').strip()
        self.timeout = float(timeout or 60)
        # 语气指令：只有 qwen3-tts-instruct-flash 这类 instruct 模型认这个参数（实测见类注释；
        # 复刻/设计音色绑的是 vc/vd 模型，收下这个字段但不用它）。
        self.instructions = (instructions or '').strip()
        self.optimize_instructions = bool(optimize_instructions)
        self._tone_warned = False

    def availability(self):
        if not self.api_key: return False, '没有填写语音合成 API Key（阿里云百炼）'
        if not self.voice: return False, '没有填写音色'
        return True, '模型 %s，音色 %s' % (self.model, self.voice)

    async def synthesize(self, text, voice=None, instructions=None) -> bytes:
        """把文本合成为音频，返回 WAV/MP3 字节。

        instructions 可逐句覆盖（默认用引擎上那条），方便「这一句用惊讶的语气」这类需求。
        """
        text = (text or '').strip()
        if not text: raise TTSError('要合成的文本为空')
        ok, why = self.availability()
        if not ok: raise TTSError(why)
        payload = {'model': self.model,
                   'input': {'text': text, 'voice': voice or self.voice, 'language_type': self.language}}
        tone = (self.instructions if instructions is None else str(instructions or '')).strip()
        if tone and not self.supports_tone:
            # 接口对这个字段是「收下但不用」（实测：flash / 复刻模型都不理它），
            # 与其让用户以为语气生效了，不如说清楚并干脆不发。
            if not self._tone_warned:
                self._tone_warned = True
                LOG.warning('模型 %s 不认语气指令，已忽略（只有 %s* 支持）；'
                            '想要语气请把合成模型换成 qwen3-tts-instruct-flash',
                            self.model, '、'.join(self.INSTRUCT_MODELS))
            tone = ''
        if tone:
            payload['input']['instructions'] = tone
            if self.optimize_instructions: payload['parameters'] = {'optimize_instructions': True}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.base_url + TTS_PATH,
                                         headers={'Authorization': 'Bearer ' + self.api_key,
                                                  'Content-Type': 'application/json'},
                                         json=payload)
            # 自建音色（qwen3-tts-vd-*）的官方示例不带 language_type；
            # 万一它不认这个字段，就去掉再来一次，免得整条朗读功能直接哑掉。
            if response.status_code == 400 and 'language_type' in payload['input']:
                LOG.info('合成被拒，去掉 language_type 重试：%s', response.text[:200])
                payload['input'].pop('language_type', None)
                response = await client.post(self.base_url + TTS_PATH,
                                             headers={'Authorization': 'Bearer ' + self.api_key,
                                                      'Content-Type': 'application/json'},
                                             json=payload)
            if response.status_code >= 400:
                raise TTSError(_explain(response))
            data = response.json()
            audio = ((data.get('output') or {}).get('audio')) or {}
            encoded = _keys(audio, 'data')
            if encoded:
                try:
                    return base64.b64decode(encoded)
                except (binascii.Error, ValueError) as exc:
                    raise TTSError('音频数据解码失败：%s' % exc) from exc
            url = _keys(audio, 'url')
            if url:
                got = await client.get(url)
                if got.status_code >= 400:
                    raise TTSError('下载合成音频失败：HTTP %s' % got.status_code)
                return got.content
        raise TTSError('接口没有返回音频：%s' % str(data)[:200])


def instruction_cost(text) -> int:
    """语气指令的字数口径：汉字（含日文汉字）算 2，其余（字母、数字、标点、假名）算 1。"""
    cost = 0
    for ch in str(text or ''):
        code = ord(ch)
        # 只数汉字区：假名（0x3040~0x30FF）按官方口径算 1 个字，别一起算进去。
        wide = (0x3400 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF
                or 0x20000 <= code <= 0x2FA1F)
        cost += 2 if wide else 1
    return cost


def clip_instruction(text, limit=AUDIO_TTS_INSTRUCTION_LIMIT):
    """把语气指令裁到接口允许的长度内，返回 (裁好的文本, 说明或空)。

    超长不是报错而是被接口拒掉，所以宁可先裁再发，并把「裁了」这件事如实说出去。
    """
    text = str(text or '').strip()
    if instruction_cost(text) <= limit: return text, ''
    kept, cost = [], 0
    for ch in text:
        step = instruction_cost(ch)
        if cost + step > limit: break
        kept.append(ch); cost += step
    clipped = ''.join(kept)
    return clipped, ('语气指令太长（%d 字，上限 %d），已截断成：%s'
                     % (instruction_cost(text), limit, clipped))


class QwenAudioTTS:
    """Qwen-Audio-TTS：和 QwenTTS 是**两套完全不同的接口**，别混用。

    为什么值得单开一条路（都是实测的）：
    * `input.instruction`（**单数**，Qwen-TTS 那边叫 `instructions`）对语气的作用
      非常大：同一句话不加语气 3.2 秒，「请用非常缓慢的速度朗读，每个字都拖长」
      8.9 秒，「快、急促」2.24 秒——比 qwen3-tts-instruct-flash 明显得多；
    * 系统音色和复刻音色都接受任意语气指令；
    * 代价：音色只属于这个模型，Qwen-TTS 复刻出来的音色在这里会用不了
      （回 `Engine error [411]`）。这边要单独复刻（见 AudioVoiceCloner）。
    """

    # 云端一次返回整段，很快，不需要按句拆。
    sentence_pipeline = False
    supports_tone = True

    def __init__(self, api_key='', model='qwen-audio-3.0-tts-flash', voice='longanfengyue',
                 base_url=DEFAULT_BASE_URL, workspace='', fmt='wav', sample_rate=24000,
                 volume=50, rate=1.0, pitch=1.0, seed=None, language_hints=('zh',),
                 instructions='', timeout=60):
        self.api_key = (api_key or '').strip()
        self.model = (model or 'qwen-audio-3.0-tts-flash').strip()
        self.voice = (voice or 'longanfengyue').strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).strip().rstrip('/')
        self.workspace = (workspace or '').strip()
        self.fmt = (fmt or 'wav').strip().lower()
        self.sample_rate = int(sample_rate or 24000)
        self.volume = int(volume if volume is not None else 50)
        self.rate = float(rate or 1.0)
        self.pitch = float(pitch or 1.0)
        self.seed = None if seed in (None, '') else int(seed)
        self.language_hints = [str(x).strip() for x in (language_hints or ['zh']) if str(x).strip()]
        # 属性名统一叫 instructions（和 QwenTTS 一致，conversation_manager.speak 按这个名字
        # 找「全局语气」）；发给接口时字段名才是单数的 input.instruction。
        self.instructions = (instructions or '').strip()
        self.timeout = float(timeout or 60)

    def endpoint(self):
        """接口地址：填了 Workspace ID 就用业务空间专属域名（官方推荐）。"""
        if self.workspace:
            return 'https://%s%s' % (AUDIO_TTS_HOST.format(workspace=self.workspace), AUDIO_TTS_PATH)
        return self.base_url + AUDIO_TTS_PATH

    def availability(self):
        if not self.api_key: return False, '没有填写语音合成 API Key（阿里云百炼）'
        if not self.voice: return False, '没有填写音色'
        return True, '模型 %s，音色 %s%s' % (self.model, self.voice,
                                            '，业务空间专属域名' if self.workspace else '（通用域名）')

    async def synthesize(self, text, voice=None, instructions=None) -> bytes:
        """合成一段语音，返回音频字节（wav/mp3/pcm/opus）。

        非流式模式下 `output.audio.data` 是空的，音频在 24 小时有效的 url 里，
        所以这里要多下载一次。
        """
        text = (text or '').strip()
        if not text: raise TTSError('要合成的文本为空')
        ok, why = self.availability()
        if not ok: raise TTSError(why)
        payload_input = {'text': text, 'voice': voice or self.voice,
                         'format': self.fmt, 'sample_rate': self.sample_rate,
                         'volume': self.volume, 'rate': self.rate, 'pitch': self.pitch}
        if self.language_hints: payload_input['language_hints'] = self.language_hints[:1]
        if self.seed is not None: payload_input['seed'] = self.seed
        tone = (self.instructions if instructions is None else str(instructions or '')).strip()
        if tone:
            tone, note = clip_instruction(tone)
            if note: LOG.warning('%s（Qwen-Audio-TTS）', note)
            if tone: payload_input['instruction'] = tone
        payload = {'model': self.model, 'input': payload_input}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.endpoint(),
                                         headers={'Authorization': 'Bearer ' + self.api_key,
                                                  'Content-Type': 'application/json'},
                                         json=payload)
            if response.status_code >= 400:
                raise TTSError(_explain(response))
            data = response.json()
            if data.get('code'):
                raise TTSError('接口报错：%s %s' % (data.get('code'), data.get('message')))
            audio = ((data.get('output') or {}).get('audio')) or {}
            encoded = _keys(audio, 'data')
            if encoded:
                try:
                    return base64.b64decode(encoded)
                except (binascii.Error, ValueError) as exc:
                    raise TTSError('音频数据解码失败：%s' % exc) from exc
            url = _keys(audio, 'url')
            if url:
                got = await client.get(url)
                if got.status_code >= 400:
                    raise TTSError('下载合成音频失败：HTTP %s' % got.status_code)
                return got.content
        raise TTSError('接口没有返回音频：%s' % str(data)[:200])


class AudioVoiceCloner:
    """Qwen-Audio-TTS / CosyVoice 的声音复刻（`model=voice-enrollment`）。

    和 Qwen-TTS 那套（`qwen-voice-enrollment` + `audio.data`）**不是一回事**：
    这条路的参数是 `action=create_voice` + `target_model` + `prefix` + `url`。

    关于「url 到底能不能放本地文件」——文档只写「公网可访问的 URL」，但**实测
    本地录音用 base64 的 data: 地址塞进去照样 200 建出音色**（2026-09-13 实测），
    所以本地文件不用先传公网。反过来，`audio.data` 那种写法在这条路上会被拒：
    `provide url, or provide both voice_prompt and preview_text`。
    （那句话也透露了还有一种「voice_prompt + preview_text」的设计式建音色，本文件没做。）

    删除音色：`action=delete_voice` + `voice_id`，实测可用。列音色的 action 名没试出来
    （`list`、`list_voices` 都回 `invalid action`），所以设置页不做「列出我的音色」。
    """

    def __init__(self, api_key='', base_url=DEFAULT_BASE_URL, workspace='', timeout=180):
        self.api_key = (api_key or '').strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).strip().rstrip('/')
        self.workspace = (workspace or '').strip()
        self.timeout = float(timeout or 180)

    def endpoint(self):
        if self.workspace:
            return 'https://%s%s' % (AUDIO_TTS_HOST.format(workspace=self.workspace), CUSTOM_PATH)
        return self.base_url + CUSTOM_PATH

    def availability(self):
        if not self.api_key: return False, '没有填写阿里云百炼 API Key'
        return True, '要一段 10~20 秒的干净人声（本地文件或公网地址都行）；建音色免费'

    @staticmethod
    def check_prefix(prefix) -> str:
        """音色前缀：只允许字母数字、最多 10 个字符。"""
        cleaned = re.sub(r'[^0-9A-Za-z]', '', str(prefix or ''))
        return cleaned[:10]

    @staticmethod
    def local_data_uri(path) -> Tuple[str, List[str]]:
        """把本机录音变成 data:base64 地址，返回 (地址, 处理说明)。

        走的是和 Qwen-TTS 同一套整理（单声道 / 16bit / ≥24kHz、必要时重采样）。
        """
        data, mime, notes = prepare_clone_audio(path)
        return 'data:%s;base64,%s' % (mime, base64.b64encode(data).decode('ascii')), notes

    @classmethod
    def resolve_audio(cls, source) -> Tuple[str, List[str]]:
        """把用户填的东西变成接口能收的地址。

        * 公网地址（http/https）→ 原样用
        * data:base64 → 原样用
        * 本地文件 → 整理后转成 data:base64（实测这条路照收）
        填错在这里就报错，不浪费一次请求。
        """
        text = str(source or '').strip().strip('"')
        if not text: raise TTSError('要先选一段参考录音，或者填一个公网音频地址')
        low = text.lower()
        if low.startswith('data:'):
            if ';base64,' not in low:
                raise TTSError('data: 地址里没有 base64 内容，看起来不是一个音频数据地址')
            return text, []
        if low.startswith(('http://', 'https://')):
            return text, []
        file = Path(text)
        if not file.exists():
            raise TTSError('既不是公网地址（http/https），也不是存在的本地文件：%s' % text)
        return cls.local_data_uri(file)

    @staticmethod
    def payload(prefix, url, target_model, language_hints=('zh',),
                max_prompt_audio_length=None, enable_preprocess=False,
                enable_volume_normalization=None):
        """拼请求体（url 应该是已经解析好的；单独抽出来是为了离线断言字段名）。

        前缀在这里也过一遍 check_prefix：这样「接口收到的前缀一定合法」是由构造保证的，
        不用依赖调用方记得先洗一遍。
        """
        body_input = {'action': 'create_voice', 'target_model': target_model,
                      'prefix': AudioVoiceCloner.check_prefix(prefix), 'url': url}
        hints = [str(x).strip() for x in (language_hints or []) if str(x).strip()]
        if hints: body_input['language_hints'] = hints[:1]
        if max_prompt_audio_length is not None:
            body_input['max_prompt_audio_length'] = float(max_prompt_audio_length)
        if enable_preprocess: body_input['enable_preprocess'] = True
        if enable_volume_normalization is not None:
            body_input['enable_volume_normalization'] = str(bool(enable_volume_normalization)).lower()
        return {'model': 'voice-enrollment', 'input': body_input}

    async def create(self, prefix, source, target_model='qwen-audio-3.0-tts-flash', **kwargs):
        """建音色，返回 (voice_id, 原始返回)。source 是本地文件路径或公网地址。"""
        if not self.api_key: raise TTSError('没有填写阿里云百炼 API Key')
        name = self.check_prefix(prefix)
        if not name: raise TTSError('音色前缀只能是字母和数字，且不能为空')
        url, notes = self.resolve_audio(source)
        for note in notes: LOG.info('参考音频：%s', note)
        body = self.payload(name, url, target_model, **kwargs)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.endpoint(),
                                         headers={'Authorization': 'Bearer ' + self.api_key,
                                                  'Content-Type': 'application/json'},
                                         json=body)
            if response.status_code >= 400:
                raise TTSError(_explain(response))
            data = response.json()
            if data.get('code'):
                raise TTSError('接口报错：%s %s' % (data.get('code'), data.get('message')))
            out = data.get('output') or {}
            voice_id = out.get('voice_id') or out.get('voice') or ''
            if not voice_id:
                raise TTSError('接口没返回 voice_id：%s' % str(data)[:200])
            LOG.info('复刻成功：%s', voice_id)
            return voice_id, data

    async def delete(self, voice_id):
        """删掉一个自建音色（清理用）。"""
        if not self.api_key: raise TTSError('没有填写阿里云百炼 API Key')
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.endpoint(),
                                         headers={'Authorization': 'Bearer ' + self.api_key,
                                                  'Content-Type': 'application/json'},
                                         json={'model': 'voice-enrollment',
                                               'input': {'action': 'delete_voice',
                                                         'voice_id': str(voice_id).strip()}})
        if response.status_code >= 400:
            raise TTSError(_explain(response))
        return response.json()


class VoiceDesigner:
    """声音设计：用文字描述创建一个专属音色。
    走的是 Qwen 声音设计（`model=qwen-voice-design`）：它建出来的音色直接用
    **HTTP** 的 Qwen-TTS 合成，跟本文件的 QwenTTS 是同一套接口。CosyVoice 那条
    路（`voice-enrollment` + `cosyvoice-v3.5-plus`）也能建音色，但合成要连
    业务空间专属域名的 WebSocket，本项目用不上，所以不采用。
    """

    def __init__(self, api_key='', base_url=DEFAULT_BASE_URL, target_model=DEFAULT_DESIGN_TARGET, timeout=120):
        self.api_key = (api_key or '').strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).strip().rstrip('/')
        self.target_model = (target_model or DEFAULT_DESIGN_TARGET).strip()
        self.timeout = float(timeout or 120)

    def availability(self):
        if not self.api_key: return False, '没有填写阿里云百炼 API Key'
        return True, '目标模型 %s' % self.target_model

    async def _call(self, body) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.base_url + CUSTOM_PATH,
                                         headers={'Authorization': 'Bearer ' + self.api_key,
                                                  'Content-Type': 'application/json'},
                                         json=body)
        if response.status_code >= 400:
            raise TTSError(_explain(response))
        data = response.json()
        if data.get('code'):                       # 有的错误是 200 + code
            raise TTSError('接口报错：%s %s' % (data.get('code'), data.get('message')))
        return data.get('output') or {}

    async def create(self, voice_prompt, preview_text, preferred_name='lilyz', language='zh'):
        """按描述创建音色，返回 (音色名, 预览音频字节, 目标模型)。"""
        if not self.api_key: raise TTSError('没有填写阿里云百炼 API Key')
        if not (voice_prompt or '').strip(): raise TTSError('请先写一段音色描述')
        if not (preview_text or '').strip(): raise TTSError('请先写一句试听文本（接口必填）')
        # 官方示例里用的是 qwen3-tts-vd-realtime-*，那个只能走 WebSocket 实时合成，
        # 用本文件的 HTTP 接口合不出来。提前拦下，别让你建完音色才发现读不出声。
        if 'realtime' in self.target_model.lower():
            raise TTSError('驱动模型 %s 是实时（realtime）模型，只能走 WebSocket 实时合成，'
                           '本项目用的是 HTTP 合成。请改成 %s。' % (self.target_model, DEFAULT_DESIGN_TARGET))
        name = ''.join(c for c in (preferred_name or '') if c.isalnum() or c == '_')[:16] or 'lilyz'
        output = await self._call({'model': 'qwen-voice-design',
                                   'input': {'action': 'create',
                                             'target_model': self.target_model,
                                             'preferred_name': name,
                                             'voice_prompt': voice_prompt,
                                             'preview_text': preview_text,
                                             'language': language or 'zh'},
                                   'parameters': {'sample_rate': 24000, 'response_format': 'wav'}})
        # 注意：Qwen 这条线返回的字段是 voice（不是 voice_id），别写错。
        voice = _keys(output, 'voice', 'voice_id')
        preview = ((output.get('preview_audio') or {}).get('data')) or ''
        audio = b''
        if preview:
            try: audio = base64.b64decode(preview)
            except (binascii.Error, ValueError): audio = b''
        if not voice: raise TTSError('接口没有返回音色名：%s' % str(output)[:200])
        LOG.info('声音设计成功：%s', voice)
        return voice, audio, output.get('target_model') or self.target_model

    async def list_voices(self) -> List[Dict[str, Any]]:
        output = await self._call({'model': 'qwen-voice-design',
                                   'input': {'action': 'list', 'page_index': 0, 'page_size': 50}})
        return output.get('voice_list') or []

    async def delete(self, voice):
        await self._call({'model': 'qwen-voice-design',
                          'input': {'action': 'delete', 'voice': voice}})


class VoiceCloner:
    """声音复刻：拿现成的录音文件复刻出一个专属音色。

    走 Qwen-TTS 声音复刻（`model=qwen-voice-enrollment`），音频以
    Data URL（`data:{mime};base64,...`）提交，不需要把文件传到公网。
    建出来的音色同样用 HTTP 的 Qwen-TTS 合成，只要合成时 model 与
    `target_model` 一致即可。
    """

    def __init__(self, api_key='', base_url=DEFAULT_BASE_URL, target_model=DEFAULT_CLONE_TARGET, timeout=180):
        self.api_key = (api_key or '').strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).strip().rstrip('/')
        self.target_model = (target_model or DEFAULT_CLONE_TARGET).strip()
        self.timeout = float(timeout or 180)

    def availability(self):
        if not self.api_key: return False, '没有填写阿里云百炼 API Key'
        return True, '目标模型 %s' % self.target_model

    async def _call(self, body) -> Dict[str, Any]:
        # 上传的是整段音频（base64 后可能十几 MB），超时给足。
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.base_url + CUSTOM_PATH,
                                         headers={'Authorization': 'Bearer ' + self.api_key,
                                                  'Content-Type': 'application/json'},
                                         json=body)
        if response.status_code >= 400:
            raise TTSError(_explain(response))
        data = response.json()
        if data.get('code'):
            raise TTSError('接口报错：%s %s' % (data.get('code'), data.get('message')))
        return data.get('output') or {}

    async def create(self, path, preferred_name='lilyz', text='', language='zh',
                     trim_seconds: Optional[float] = None):
        """用一段录音创建音色，返回 (音色名, 处理说明列表)。"""
        if not self.api_key: raise TTSError('没有填写阿里云百炼 API Key')
        if 'realtime' in self.target_model.lower():
            raise TTSError('驱动模型 %s 是实时（realtime）模型，只能走 WebSocket 实时合成，'
                           '本项目用的是 HTTP 合成。请改成 %s。' % (self.target_model, DEFAULT_CLONE_TARGET))
        audio, mime, notes = prepare_clone_audio(path, trim_seconds)
        name = ''.join(c for c in (preferred_name or '') if c.isalnum() or c == '_')[:16] or 'lilyz'
        payload = {'model': 'qwen-voice-enrollment',
                   'input': {'action': 'create',
                             'target_model': self.target_model,
                             'preferred_name': name,
                             'audio': {'data': 'data:%s;base64,%s'
                                               % (mime, base64.b64encode(audio).decode('ascii'))},
                             'language': language or 'zh'}}
        # 填了「录音里读的内容」会明显提升相似度；填错反而触发降级，所以是可选项。
        if (text or '').strip(): payload['input']['text'] = text.strip()
        output = await self._call(payload)
        voice = _keys(output, 'voice', 'voice_id')
        if not voice: raise TTSError('接口没有返回音色名：%s' % str(output)[:200])
        if output.get('fallback_mode'):
            reason = output.get('fallback_reason') or '未说明'
            notes.append('⚠ 接口以「降级模式」创建了这个音色（%s），相似度可能不理想；'
                         '多半是录音质量或「录音文本」与音频对不上' % reason)
        LOG.info('声音复刻成功：%s（%s）', voice, '；'.join(notes))
        return voice, notes

    async def list_voices(self) -> List[Dict[str, Any]]:
        output = await self._call({'model': 'qwen-voice-enrollment',
                                   'input': {'action': 'list', 'page_index': 0, 'page_size': 50}})
        return output.get('voice_list') or []

    async def delete(self, voice):
        await self._call({'model': 'qwen-voice-enrollment',
                          'input': {'action': 'delete', 'voice': voice}})


async def list_all_voices(api_key, base_url=DEFAULT_BASE_URL):
    """把「声音设计」和「声音复刻」两边的自建音色都列出来。

    这两类音色挂在不同的 model 下（qwen-voice-design / qwen-voice-enrollment），
    只查其中一个会看不到另一个。返回 (音色列表, 出错说明列表)。
    """
    voices: List[Dict[str, Any]] = []
    problems: List[str] = []
    seen = set()
    for kind, maker in (('声音设计', VoiceDesigner), ('声音复刻', VoiceCloner)):
        try:
            items = await maker(api_key, base_url).list_voices()
        except Exception as exc:
            problems.append('%s：%s' % (kind, exc)); continue
        for item in items:
            name = item.get('voice') or item.get('voice_id')
            if not name or name in seen: continue
            seen.add(name)
            item['_kind'] = kind
            voices.append(item)
    return voices, problems


def _explain(response) -> str:
    """把接口报错翻译成人话。"""
    detail = ''
    try:
        data = response.json()
        detail = '%s %s' % (data.get('code') or '', data.get('message') or '')
    except ValueError:
        detail = response.text[:300]
    hint = ''
    if 'AllocationQuota' in detail or 'FreeTierOnly' in detail:
        # 实测：这个错误跟 Key、跟参数都无关，是账号状态——控制台开着「只使用免费额度」
        # 或免费额度用完了。合成可能还能用，但复刻这种要计费的操作会被直接拦下。
        hint = ('（账号被限制：控制台开着「只使用免费额度」或免费额度已用完。'
                '去百炼控制台充值、或关掉「只使用免费额度」再试；跟 API Key 无关）')
    elif response.status_code in (401, 403):
        hint = '（API Key 不对，或没开通百炼/欠费；注意新加坡与北京地域的 Key 不通用）'
    elif response.status_code == 404:
        hint = '（模型名或接口地址不对）'
    elif 'Model' in detail and 'not' in detail.lower():
        hint = '（模型不存在或没有开通，请在百炼控制台确认）'
    elif 'Voice' in detail and 'supported' in detail.lower():
        hint = ('（这个音色不属于该模型：复刻音色只能用 qwen3-tts-vc-* 驱动，'
                '设计音色只能用 qwen3-tts-vd-* 驱动，两者不能换着用）')
    elif '411' in detail or ('Engine error' in detail and 'speak' in detail):
        # Qwen-Audio-TTS 这条路上，音色不带这个模型玩、或模型名不对，都是这个含糊的错。
        hint = ('（这个音色不被当前模型接受：Qwen-Audio-TTS 只认自己的系统音色/'
                '复刻音色，Qwen-TTS 的 Cherry、qwen3-tts-vc-* 之类都用不了）')
    elif 'instructions' in detail.lower():
        hint = ('（这个模型不认语气指令：只有 qwen3-tts-instruct-flash 支持，'
                '把「合成模型」换成它，或在设置里清空语气）')
    return '语音合成接口 HTTP %s：%s%s' % (response.status_code, detail.strip(), hint)


class SpeechPlayer:
    """用 QtMultimedia 播放合成出来的语音（无需额外依赖）。

    Qt6 把音量从播放器挪到了音频输出端：`QMediaPlayer` 已经**没有** `setVolume`，
    必须调 `QAudioOutput.setVolume(0~1)`。写成 player.setVolume 会直接
    `AttributeError: 'QMediaPlayer' object has no attribute 'setVolume'`，
    而且第一次播放才炸——所以这里把音量做成属性，谁先来谁后到都能正确落位。
    """

    def __init__(self):
        self._player = None
        self._output = None
        self._temp = None
        self._volume = 80
        self._queue = []          # 待播的音频段（本地按句合成时一段段进来）
        self._busy = False
        # 临时音频文件是延后清理的，退出时兜一次底，别把文件留在 %TEMP%。
        try:
            import atexit
            atexit.register(self.cleanup)
        except Exception:
            pass

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value):
        try:
            self._volume = max(0, min(100, int(value)))
        except (TypeError, ValueError):
            self._volume = 80
        self._apply_volume()

    def _apply_volume(self):
        if self._output is None:
            return
        try:
            self._output.setVolume(self._volume / 100.0)
        except Exception:
            LOG.warning('设置播放音量失败', exc_info=True)

    def available(self):
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput  # noqa: F401
            return True, 'QtMultimedia 可用'
        except Exception as exc:
            return False, 'QtMultimedia 不可用：%s' % exc

    def play(self, audio: bytes, suffix='.wav'):
        """播放一段音频（写进临时文件再播，Qt 需要文件或 URL）。"""
        if not audio: return False
        from PySide6.QtCore import QUrl
        from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
        previous = self._temp
        handle, name = tempfile.mkstemp(prefix='lilyz_tts_', suffix=suffix)
        with open(handle, 'wb') as fh: fh.write(audio)
        self._temp = Path(name)
        if self._player is None:
            self._player = QMediaPlayer()
            self._output = QAudioOutput()
            self._player.setAudioOutput(self._output)
            # 一段播完自动接下一段：本地逐句合成靠它串起来。
            self._player.mediaStatusChanged.connect(self._on_status)
            self._apply_volume()
        self._player.setSource(QUrl.fromLocalFile(str(self._temp)))
        self._player.play()
        # 新音源设好之后再删上一段：抢在前头删，后台解码还握着旧文件，
        # 会冒一句「Could not open media. No such file or directory」。
        self._discard(previous)
        return True

    def enqueue(self, audio, suffix='.wav'):
        """把一段音频排进播放队列，当前这段播完自动接着播。

        本地 CPU 合成是一条条出来的，用队列串起来才不会后一句打断前一句。
        """
        if not audio: return False
        self._queue.append((audio, suffix))
        if not self._busy:
            self._busy = True
            self._play_next()
        return True

    def _play_next(self):
        if not self._queue:
            self._busy = False
            return
        audio, suffix = self._queue.pop(0)
        if not self.play(audio, suffix):
            self._play_next()

    def _on_status(self, status):
        from PySide6.QtMultimedia import QMediaPlayer
        if status in (QMediaPlayer.MediaStatus.EndOfMedia, QMediaPlayer.MediaStatus.InvalidMedia):
            self._play_next()

    def stop(self):
        """停止播放并清空队列（点静音时就该立刻彻底安静）。

        临时文件**不在这里删**：Qt 的后台解码可能还握着它，删早了就会报
        「Could not open media」。留着由下次播放或程序退出时统一清理。
        """
        self._queue = []
        self._busy = False
        if self._player is not None:
            try: self._player.stop()
            except Exception: pass

    def cleanup(self):
        """停播并删掉临时文件（退出前调用）。"""
        self.stop()
        self._discard(self._temp)
        self._temp = None

    @staticmethod
    def _discard(path):
        if path is None: return
        try: Path(path).unlink(missing_ok=True)
        except OSError: pass


# --------------------------------------------------------------------- 本地 GPT-SoVITS
# 本地引擎独立于云端：自己起一个 api_v2.py 服务，桌宠通过 HTTP 调它。
# 好处是免费、离线、音色完全自己说了算；代价是 CPU 推理慢（本机是 AMD 核显，
# 没有 CUDA），长句要等十几秒，而且服务得先开着。

LOCAL_TTS_URL = 'http://127.0.0.1:9880'
# api_v2 的 _set_prompt_semantic 里写死了参考音频长度：librosa 读成 16k 后
# 样本数必须在 [48000, 160000]，也就是 3~10 秒，超了直接 400。
# （WebUI 的推理页同样有这个限制；只有「训练」没有，训练是把长音频切段。）
LOCAL_REF_MIN = 3.0
LOCAL_REF_MAX = 10.0
GPTSOVITS_LANGS = (('zh', '中文'), ('en', '英文'), ('ja', '日文'), ('ko', '韩文'), ('yue', '粤语'))
GPTSOVITS_SPLIT_METHODS = ('cut0', 'cut1', 'cut2', 'cut3', 'cut4', 'cut5')
GPTSOVITS_API_ARGS = ('api_v2.py', '-a', '127.0.0.1', '-p', '9880',
                      '-c', 'GPT_SoVITS/configs/tts_infer.yaml')

# 只有这些才是「一句话说完了」；`…`、`；`、`、`、`,` 都只是停顿，绝不能当句末切——
# 切了就会为半句话多开一次合成请求（本地每多一次都要多等几秒，还要多一道接缝）。
_SENTENCE_END = '。！？!?'
_CLOSERS = '」』”"\')）'   # 句末紧跟的收尾符号，跟着上一句走


def split_sentences(text, max_chars=45, min_chars=12):
    """把回复切成「值得单独合成一次」的片段。

    规则都是为了少发请求、少等：

    * **只在真正的句末切**（`。！？!?`）。像「私はリカコ……あ、また言い間違えました。」
      这种带省略号的整句，就该一次合成完。
    * **太短的片段并到旁边**（`min_chars`）：为「あ、」「はい。」单独跑一次推理不划算。
    * **单块上限 max_chars**：没标点的一大段硬切，免得第一句迟迟不出声。
    """
    source = str(text or '')
    if not source.strip(): return []
    pieces, current = [], ''
    index = 0
    while index < len(source):
        char = source[index]
        current += char
        index += 1
        if char in _SENTENCE_END or char == '\n':
            while index < len(source) and source[index] in _CLOSERS:   # 把 」” 等收尾带上
                current += source[index]; index += 1
            if current.strip(): pieces.append(current.strip())
            current = ''
    if current.strip(): pieces.append(current.strip())
    if not pieces: return []

    # 短片段合并：能并进上一块就并，保证同一句不会被拆成两次请求。
    merged = [pieces[0]]
    for piece in pieces[1:]:
        last = merged[-1]
        if (len(last) < min_chars or len(piece) < min_chars) and len(last) + len(piece) <= max_chars:
            merged[-1] = last + piece
        else:
            merged.append(piece)

    # 最后再兜一道硬切：单块太长就切开，避免一次等太久。
    result = []
    for piece in merged:
        while len(piece) > max_chars:
            result.append(piece[:max_chars]); piece = piece[max_chars:]
        if piece: result.append(piece)
    return result


def _setting(config, key, default=None):
    if config is None: return default
    getter = getattr(config, 'get', None)
    return getter(key, default) if callable(getter) else config.get(key, default)


def gptsovits_start_command(home=None, host='127.0.0.1', port=9880):
    """拼出启动本地服务的命令（在整合包根目录跑 api_v2.py）。

    返回 (可执行文件, 参数列表, 工作目录)；找不到整合包自带的 python.exe 时返回 None。
    """
    root = Path(home) if home else None
    if not root or not root.exists():
        return None
    python = root / 'runtime' / 'python.exe'
    if not python.exists():
        python = root / 'runtime' / 'pythonw.exe'
    if not python.exists():
        return None
    script = root / 'api_v2.py'
    if not script.exists():
        return None
    args = ['-I', str(script), '-a', host, '-p', str(int(port)), '-c', 'GPT_SoVITS/configs/tts_infer.yaml']
    return str(python), args, str(root)


def start_local_server(home, host='127.0.0.1', port=9880):
    """起一个独立的本地 api_v2 服务进程，返回 Popen；拼不出命令时返回 None。

    三个坑都在这里踩过：
    * **只用 CREATE_NEW_CONSOLE，不要 DETACHED_PROCESS**。这两个标志是互斥的
      （一个要新控制台、一个要没有控制台），一起给会变成两头不靠，进程起来就没了。
    * **不重定向 stdout/stderr**。那个新控制台窗口就是给你看加载进度和报错的；
      重定向到 DEVNULL 等于把唯一的现场扔了（之前就是这么干的，所以点完按钮啥也看不见）。
    * cwd 必须是整合包根目录、PATH 要带 runtime：api_v2 按相对路径找模型/配置，
      还要靠 runtime 里的 dll。
    """
    command = gptsovits_start_command(home, host, port)
    if not command:
        LOG.warning('启动本地服务失败：整合包目录里找不到 runtime\\python.exe 或 api_v2.py（%s）', home)
        return None
    exe, args, cwd = command
    env = dict(os.environ)
    env['PATH'] = str(Path(cwd) / 'runtime') + os.pathsep + env.get('PATH', '')
    new_console = getattr(subprocess, 'CREATE_NEW_CONSOLE', 0)
    breakaway = getattr(subprocess, 'CREATE_BREAKAWAY_FROM_JOB', 0)
    # 先试「新控制台 + 脱离父进程作业」：这样服务不会被父进程所在的作业对象连坐杀掉，
    # 桌宠关了它还在（这正是我们要的：点一次启动，服务独立跑）。作业不允许脱离时会
    # 以 OSError 失败，那就退回只用新控制台。
    attempts = [new_console | breakaway, new_console] if breakaway else [new_console]
    for index, flags in enumerate(attempts):
        try:
            proc = subprocess.Popen([exe] + args, cwd=cwd, env=env, creationflags=flags)
            LOG.info('启动本地服务：%s %s（cwd=%s，flags=0x%X）', exe, ' '.join(args), cwd, flags)
            return proc
        except OSError as exc:
            LOG.info('启动本地服务第 %d 次尝试失败（flags=0x%X）：%s', index + 1, flags, exc)
    LOG.error('启动本地服务失败：两次尝试都没成功')
    return None


def gptsovits_summary(config=None):
    """给界面/日志用的一句话描述当前本地配置。"""
    return '本地 GPT-SoVITS：%s ｜ 参考音频 %s ｜ %s→%s' % (
        _setting(config, 'gptsovits_url', LOCAL_TTS_URL) or LOCAL_TTS_URL,
        _setting(config, 'gptsovits_ref_audio', '') or '（未设置）',
        _setting(config, 'gptsovits_prompt_lang', 'zh'), _setting(config, 'gptsovits_text_lang', 'zh'))


class GptSoVitsTTS:
    """本地 GPT-SoVITS（api_v2.py 的 HTTP 接口）合成。

    几个关键约定（照官方 api 文档和本机实测）：
    * `ref_audio_path` 是**服务端本机路径**，不是上传文件——同一台机器给绝对路径即可。
    * `text_lang` / `prompt_lang` 必填；`prompt_text` 留空就走 ref_free（不需要参考音频的文稿，
      但填对了音色更准）。参考音频是日文原声时，prompt_lang 要按实际填 ja。
    * 返回体是裸音频字节；出错时是 400 + JSON（message/Exception）。
    """

    # 本地 CPU 合成慢，按句合成+边合成边播，第一句能先出声。
    sentence_pipeline = True
    # GPT-SoVITS 的 /tts 接口没有语气参数（情绪得靠参考音频本身带），所以不接语气指令。
    supports_tone = False

    def __init__(self, url=LOCAL_TTS_URL, ref_audio='', prompt_text='', prompt_lang='zh',
                 text_lang='zh', speed=1.0, timeout=300, split_method='cut5',
                 temperature=1.0, repetition_penalty=1.35, seed=-1, media_type='wav'):
        self.url = (url or LOCAL_TTS_URL).strip().rstrip('/')
        self.ref_audio = (ref_audio or '').strip().strip('"')
        self.prompt_text = (prompt_text or '').strip()
        self.prompt_lang = (prompt_lang or 'zh').strip().lower()
        self.text_lang = (text_lang or 'zh').strip().lower()
        self.speed = float(speed or 1.0)
        self.timeout = float(timeout or 300)
        self.split_method = (split_method or 'cut5').strip()
        self.temperature = float(temperature or 1.0)
        self.repetition_penalty = float(repetition_penalty or 1.35)
        self.seed = int(seed if seed is not None else -1)
        self.media_type = (media_type or 'wav').strip()
        # 整理过的参考音频（3~10 秒）与说明，第一次合成时才算。
        self._ready_ref = None
        self.ref_note = ''
        # 界面/日志沿用云端那套「模型名」的说法，这里用「本地 · 参考音色文件名」表示。
        self.model = '本地 GPT-SoVITS'

    @property
    def voice(self):
        return Path(self.ref_audio).name if self.ref_audio else '（未设置参考音频）'

    def _host_port(self):
        from urllib.parse import urlparse
        parsed = urlparse(self.url if '://' in self.url else 'http://' + self.url)
        return parsed.hostname or '127.0.0.1', parsed.port or 9880

    def host_port(self):
        """服务地址里的主机与端口（界面要用它拼启动命令）。"""
        return self._host_port()

    def availability(self):
        if not self.url: return False, '没有填写本地服务地址'
        if not self.ref_audio: return False, '没有选参考音频（GPT-SoVITS 靠它定音色）'
        if not Path(self.ref_audio).exists(): return False, '参考音频不存在：%s' % self.ref_audio
        if not self.text_lang: return False, '没有填要合成的语言'
        return True, '服务 %s，参考音色 %s' % (self.url, self.voice)

    async def health(self, timeout=3.0):
        """只证明端口通，不代表模型已经加载完（加载要几十秒）。

        真正的端到端验证是合成一句——用界面的「试听」最快。
        """
        host, port = self._host_port()
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
            writer.close()
            try: await writer.wait_closed()
            except Exception: pass
            return True, '服务在（%s:%d 通）' % (host, port)
        except Exception as exc:
            return False, ('连不上 %s:%d（%s）。本地服务要先启动：整合包根目录的 go-api.bat，'
                           '或者点上面的「启动本地服务」' % (host, port, type(exc).__name__))

    async def synthesize(self, text) -> bytes:
        text = (text or '').strip()
        if not text: raise TTSError('要合成的文本为空')
        ok, why = self.availability()
        if not ok: raise TTSError(why)
        # 参考音频先收拾成 3~10 秒（api_v2 的硬限制），结果缓存起来，后面每次合成都用它。
        if self._ready_ref is None:
            self._ready_ref, self.ref_note = prepare_local_ref(self.ref_audio)
        payload = {'text': text, 'text_lang': self.text_lang, 'ref_audio_path': self._ready_ref,
                   'prompt_text': self.prompt_text, 'prompt_lang': self.prompt_lang,
                   'text_split_method': self.split_method, 'batch_size': 1,
                   'media_type': self.media_type, 'streaming_mode': False,
                   'speed_factor': self.speed, 'temperature': self.temperature,
                   'repetition_penalty': self.repetition_penalty, 'seed': self.seed}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(self.url + '/tts', json=payload)
            except httpx.ConnectError as exc:
                raise TTSError('连不上本地服务 %s（%s）。先在整合包根目录跑 go-api.bat' % (self.url, exc)) from exc
            except httpx.TimeoutException as exc:
                raise TTSError('本地合成超时（%s 秒）。CPU 推理长句很慢，可以调小最大字数或加大超时' % self.timeout) from exc
            if response.status_code >= 400:
                raise TTSError(_explain_local(response))
            audio = response.content
        if not audio:
            raise TTSError('本地服务返回了空音频')
        if audio[:4] != b'RIFF' and self.media_type == 'wav':
            raise TTSError('返回内容不是 wav：%s' % audio[:120])
        return audio

    async def stop_server(self):
        """让本地服务自己退出（api_v2 的 /control?command=exit）。"""
        async with httpx.AsyncClient(timeout=8) as client:
            await client.get(self.url + '/control', params={'command': 'exit'})


def _explain_local(response) -> str:
    """把本地 api_v2 的报错翻成人话。"""
    detail = ''
    try:
        data = response.json()
        detail = str(data.get('Exception') or data.get('message') or data)
    except ValueError:
        detail = response.text[:300]
    hint = ''
    if 'ref_audio' in detail or 'reference' in detail.lower() or '音频' in detail:
        hint = '（多半是参考音频路径不对：这个路径要在服务端那台机器上能读到）'
    elif 'not found' in detail.lower() or '不存在' in detail:
        hint = '（文件或模型没找到）'
    return '本地合成失败 HTTP %s：%s%s' % (response.status_code, detail.strip(), hint)


def prepare_local_ref(path, cache_dir=None):
    """把参考音频收拾成 api_v2 能吃的 3~10 秒 WAV，返回 (可用路径, 说明)。

    只有「时长」需要动：api_v2 里 librosa 读成 16k 后样本数必须落在
    [48000, 160000]（3~10 秒），否则一律 400。超长的就掐一段（去掉首尾静音后再取
    开头最多 10 秒），太短的没法凭空造，只能报错让人换。
    切出来的片段写到临时目录，**原文件一个字节都不动**。
    """
    import hashlib
    file = Path(str(path))
    if not file.exists():
        raise TTSError('参考音频不存在：%s' % file)
    info = probe_clone_audio(file)
    seconds = info.get('seconds')
    if info.get('ext') != '.wav' or seconds is None:
        # 非 WAV 没有解码器，本地量不出时长；直接交给服务端判（它会给同样的 3~10 秒提示）。
        note = '参考音频不是 WAV，本地量不出时长，直接交给本地服务判断' if info.get('ext') != '.wav' else '参考音频读不出时长'
        return str(file), note
    if LOCAL_REF_MIN <= seconds <= LOCAL_REF_MAX:
        return str(file), '参考音频 %.1f 秒，符合本地接口的 3~10 秒要求' % seconds

    with wave.open(str(file), 'rb') as fh:
        channels, width, rate, frames = (fh.getnchannels(), fh.getsampwidth(),
                                         fh.getframerate(), fh.getnframes())
        raw = fh.readframes(frames)
    samples = _to_mono(_int16_from_raw(raw, width), channels)
    # 先把首尾的静音削掉：录音常见开头一段空白，直接截前 10 秒可能全是静音。
    peak = max((max(samples), -min(samples)), default=0)
    gate = max(int(peak * 0.06), 220)
    step = max(int(rate * 0.02), 1)
    start = end = None
    for i in range(0, max(len(samples) - step, 1), step):
        chunk = samples[i:i + step]
        if chunk and max(max(chunk), -min(chunk)) >= gate:
            if start is None: start = i
            end = i + step
    if start is None:                                  # 整段几乎是静音
        raise TTSError('参考音频几乎没有声音（峰值 %d/32767），换一段有清楚人声的' % peak)
    samples = samples[start:end]

    notes = []
    if start or end < len(_to_mono(_int16_from_raw(raw, width), channels)):
        notes.append('已削掉首尾静音')
    if len(samples) < int(LOCAL_REF_MIN * rate):
        raise TTSError('参考音频去掉静音后只有 %.1f 秒，接口要求 3~10 秒；请换一段更长的人声'
                       % (len(samples) / float(rate)))
    keep = int(LOCAL_REF_MAX * rate)
    if len(samples) > keep:
        samples = samples[:keep]
        notes.append('已截取开头 %.0f 秒' % LOCAL_REF_MAX)

    digest = hashlib.sha1(('%s|%s|%s' % (file, file.stat().st_mtime, len(samples))).encode('utf-8')).hexdigest()[:12]
    folder = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir())
    out = folder / ('lilyz_ref_%s.wav' % digest)
    if not out.exists():
        folder.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as fh:
            fh.setnchannels(1); fh.setsampwidth(2); fh.setframerate(rate)
            fh.writeframes(samples.tobytes())
        out.write_bytes(buffer.getvalue())
    LOG.info('参考音频已整理：%s -> %s（%s）', file.name, out, '；'.join(notes) or '无需改动')
    note = ('参考音频 %.1f 秒超出本地接口的 3~10 秒限制：%s；结果存成临时文件 %s（原文件没动）'
            % (seconds, '、'.join(notes) if notes else '截取一段', out.name))
    return str(out), note


def gptsovits_from_config(config):
    return GptSoVitsTTS(_setting(config, 'gptsovits_url', LOCAL_TTS_URL),
                        _setting(config, 'gptsovits_ref_audio', ''),
                        _setting(config, 'gptsovits_prompt_text', ''),
                        _setting(config, 'gptsovits_prompt_lang', 'zh'),
                        _setting(config, 'gptsovits_text_lang', 'zh'),
                        _setting(config, 'gptsovits_speed', 1.0),
                        _setting(config, 'gptsovits_timeout', 300),
                        _setting(config, 'gptsovits_split_method', 'cut5'),
                        _setting(config, 'gptsovits_temperature', 1.0),
                        _setting(config, 'gptsovits_repetition_penalty', 1.35),
                        _setting(config, 'gptsovits_seed', -1))


AUDIO_PROVIDERS = ('qwenaudio', 'audio', 'qwen-audio', 'qwen_audio')


def is_audio_provider(config):
    return str(_setting(config, 'tts_provider', 'qwen') or 'qwen').lower() in AUDIO_PROVIDERS


def audio_tts_from_config(config):
    """造 Qwen-Audio-TTS 引擎。Key 没单独填就沿用 Qwen-TTS 那条（通常就是同一个）。"""
    key = _setting(config, 'audio_tts_api_key', '') or _setting(config, 'tts_api_key', '')
    hints = _setting(config, 'audio_tts_language_hints', 'zh')
    if isinstance(hints, str): hints = [hints]
    # 勾了「每次随机」就不发 seed：同一个种子会得到逐字节相同的音频，桌宠会更呆。
    seed = None if _setting(config, 'audio_tts_random_seed', False) else _setting(config, 'audio_tts_seed', None)
    return QwenAudioTTS(key, _setting(config, 'audio_tts_model', 'qwen-audio-3.0-tts-flash'),
                        _setting(config, 'audio_tts_voice', 'longanfengyue'),
                        _setting(config, 'audio_tts_base_url', DEFAULT_BASE_URL),
                        _setting(config, 'audio_tts_workspace', ''),
                        _setting(config, 'audio_tts_format', 'wav'),
                        _setting(config, 'audio_tts_sample_rate', 24000),
                        _setting(config, 'audio_tts_volume', 50),
                        _setting(config, 'audio_tts_rate', 1.0),
                        _setting(config, 'audio_tts_pitch', 1.0),
                        seed,
                        hints,
                        _setting(config, 'tts_instructions', ''),
                        _setting(config, 'tts_timeout', 60))


def tts_from_config(config, overrides=None):
    """按配置造合成引擎（云端 Qwen-TTS / 云端 Qwen-Audio-TTS / 本地 GPT-SoVITS）。

    `overrides` 是「按角色」的那一层（prompts/persona.json 的 tts 块），
    只覆盖这几个字段，其余的仍走 config.json。
    """
    if not _setting(config, 'tts_enabled', False): return None
    # persona.json 里写的是短名（voice/model/instructions…），这里翻成 config 的键名，
    # 两种写法都认。Qwen-Audio-TTS 用的是另一套键（audio_tts_*），按 provider 选。
    audio = is_audio_provider(config)
    prefix = 'audio_tts_' if audio else 'tts_'
    keymap = {'model': prefix + 'model', 'voice': prefix + 'voice',
              'language': 'tts_language', 'instructions': 'tts_instructions',
              'optimize_instructions': 'tts_optimize_instructions'}
    over = {keymap.get(k, k): v for k, v in (overrides or {}).items() if v not in (None, '')}

    def pick(key, default=''):
        if key in over: return over[key]
        return _setting(config, key, default)

    if audio:
        engine = audio_tts_from_config(config)
        if 'audio_tts_model' in over: engine.model = over['audio_tts_model']
        if 'audio_tts_voice' in over: engine.voice = over['audio_tts_voice']
        if 'tts_instructions' in over: engine.instructions = over['tts_instructions']
        return engine
    if str(_setting(config, 'tts_provider', 'qwen')).lower() in ('gptsovits', 'local', 'gpt-sovits'):
        return gptsovits_from_config(config)
    return QwenTTS(pick('tts_api_key', ''), pick('tts_model', 'qwen3-tts-flash'),
                   pick('tts_voice', 'Cherry'), pick('tts_base_url', DEFAULT_BASE_URL),
                   pick('tts_language', 'Chinese'), pick('tts_timeout', 60),
                   pick('tts_instructions', ''),
                   pick('tts_optimize_instructions', False))


def persona_tone_from_file(path):
    """从 prompts/persona.json 里读出角色层的语气（读不到就返回空串）。

    probe() 只拿到 config，看不到角色层；而角色层是**优先**的，不读它就会
    报出「语气：没填」这种错话——明明在生效。所以给它一个显式的入口。
    """
    try:
        if not path: return ''
        file = Path(path)
        if not file.exists(): return ''
        data = json.loads(file.read_text(encoding='utf-8')) or {}
        return str((data.get('tts') or {}).get('instructions') or '').strip()
    except Exception:
        LOG.warning('读 persona 的 tts 块失败：%s', path, exc_info=True)
        return ''


def probe(config=None, persona_path=None):
    """设置界面的「检测语音合成」：只看配置与依赖，不发网络请求。

    `persona_path` 用来把角色层（prompts/persona.json）的语气也读出来，
    因为角色层优先于 config，不读它这里就会说错。
    """
    lines = []
    engine = tts_from_config(config)
    local = str(_setting(config, 'tts_provider', 'qwen') or 'qwen').lower() in ('gptsovits', 'local', 'gpt-sovits')
    audio = is_audio_provider(config)
    if engine is None:
        lines.append('语音合成：已关闭（勾选「启用」后角色才会说话）')
    else:
        ok, why = engine.availability()
        if local:
            lines.append('语音合成（本地 GPT-SoVITS）：%s' % (why if ok else '不可用，' + why))
            lines.append('  ' + gptsovits_summary(config))
        else:
            lines.append('语音合成（%s）：%s' % (engine.model, why if ok else '不可用，' + why))
            if audio:
                lines.append('  接口：%s' % engine.endpoint())
                lines.append('  参数：%s Hz / %s / 语速 %s / 音调 %s / 音量 %s / 语言 %s'
                             % (engine.sample_rate, engine.fmt, engine.rate, engine.pitch,
                                engine.volume, '、'.join(engine.language_hints) or '未指定'))
            tone = str(_setting(config, 'tts_instructions', '') or '').strip()
            persona_tone = persona_tone_from_file(persona_path)
            if persona_tone:
                lines.append('语气：**角色层**（prompts/persona.json）写着「%s」—— 以它为准，%s'
                             % (persona_tone, '会生效' if engine.supports_tone
                                else '但当前模型（%s）不认语气指令' % engine.model))
                if tone and tone != persona_tone:
                    lines.append('  （全局框里还写了「%s」，被角色层盖住了）' % tone)
            elif tone:
                cost = instruction_cost(tone)
                if audio:
                    # 这条路实测认语气，只是有 100 字（汉字算 2）的长度上限。
                    lines.append('语气：%s —— 认（%d/%d 字）%s' % (
                        tone, cost, AUDIO_TTS_INSTRUCTION_LIMIT,
                        '，**超长会被截断**' if cost > AUDIO_TTS_INSTRUCTION_LIMIT else ''))
                else:
                    # 实测：只有 instruct 模型认这个字段，别的模型收下但不用它——这里直说。
                    lines.append('语气：%s —— %s' % (
                        tone, 'instruct 模型，会生效' if engine.supports_tone
                        else '**当前模型（%s）不认，不会生效**；换成 qwen3-tts-instruct-flash 才行' % engine.model))
            else:
                lines.append('语气：没填（可以写进 prompts/persona.json 的 tts 块按角色生效，'
                             '也可以填在设置页的「语气」框里全局生效）')
    ok, why = SpeechPlayer().available()
    lines.append('播放器：%s' % (why if ok else '不可用，' + why))
    if local:
        lines.append('本地服务：%s（端口是否在跑要点「检测」或「只要端口检测」）'
                     % (_setting(config, 'gptsovits_url', LOCAL_TTS_URL)))
    elif audio:
        key = _setting(config, 'audio_tts_api_key', '') or _setting(config, 'tts_api_key', '')
        cloner = AudioVoiceCloner(key, _setting(config, 'audio_tts_base_url', DEFAULT_BASE_URL),
                                  _setting(config, 'audio_tts_workspace', ''))
        cok, cw = cloner.availability()
        lines.append('声音复刻（Qwen-Audio-TTS）：%s' % (cw if cok else '不可用，' + cw))
        lines.append('  已实测可用：本地录音走 base64 就能建音色，复刻音色同样认语气')
        lines.append('  注意：音色不跨模型，Qwen-TTS 复刻出来的音色在这条路上用不了')
    else:
        designer = VoiceDesigner(_setting(config, 'tts_api_key', ''), _setting(config, 'tts_base_url', DEFAULT_BASE_URL))
        dok, dw = designer.availability()
        lines.append('声音设计（描述生成音色）：%s' % (dw if dok else '不可用，' + dw))
        cloner = VoiceCloner(_setting(config, 'tts_api_key', ''), _setting(config, 'tts_base_url', DEFAULT_BASE_URL))
        cok, cw = cloner.availability()
        lines.append('声音复刻（用录音建音色）：%s' % (cw if cok else '不可用，' + cw))
    return '\n'.join(lines)
