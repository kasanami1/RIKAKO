# -*- coding: utf-8 -*-
"""语气功能的离线自测：标记解析 + 发给百炼的请求体。

不联网：httpx.AsyncClient 被换成假的，只把 payload 记下来断言。
用 .venv\\Scripts\\python.exe -u _tone_test.py 跑。
"""
import asyncio, base64, json, sys, types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
ROOT = Path(__file__).parent

from conversation_manager import ConversationManager as CM
import voice

fails = []


def check(name, got, want):
    if got == want:
        print('  OK   %s' % name)
    else:
        fails.append(name)
        print('  FAIL %s\n       got  = %r\n       want = %r' % (name, got, want))


print('== parse_tone ==')
cases = [
    ('普通回复，没有标记', '你好呀。', ('你好呀。', [('', '你好呀。')])),
    ('开头一个标记',
     '[语气:小声嘀咕] 我不太想说话。',
     ('我不太想说话。', [('小声嘀咕', '我不太想说话。')])),
    ('两句不同语气',
     '[语气:小声] 甲。[语气:大喊] 乙。',
     ('甲。乙。', [('小声', '甲。'), ('大喊', '乙。')])),
    ('全角括号也能认',
     '（语气：冷淡）随便你。',
     ('随便你。', [('冷淡', '随便你。')])),
    ('前面有正文',
     '哦。 [语气:兴奋] 真的吗？',
     ('哦。真的吗？', [('', '哦。'), ('兴奋', '真的吗？')])),
    ('只有标记不能把回复弄空',
     '[语气:开心]',
     ('[语气:开心]', [])),
    ('不是标记的方括号不动它',
     '[表情:开心] 好呀。',
     ('[表情:开心] 好呀。', [('', '[表情:开心] 好呀。')])),
    ('标记里的说明会进语气字段',
     '[语气:语速放慢，句尾上扬] 这样。',
     ('这样。', [('语速放慢，句尾上扬', '这样。')])),
]
for name, raw, want in cases:
    check(name, CM.parse_tone(raw), want)


print('== 发给百炼的请求体 ==')
captured = []


class FakeResponse:
    status_code = 200
    text = '{}'

    def json(self):
        return {'output': {'audio': {'data': base64.b64encode(b'RIFFfake').decode()}}}


class FakeClient:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, headers=None, json=None):
        captured.append(json)
        return FakeResponse()
    async def get(self, url):
        return FakeResponse()


real = voice.httpx.AsyncClient
voice.httpx = types.SimpleNamespace(AsyncClient=FakeClient)


async def main():
    engine = voice.QwenTTS('k', 'qwen3-tts-instruct-flash', 'Cherry',
                           instructions='语气温柔，语速偏慢')
    await engine.synthesize('甲。')
    check('默认带上全局语气', captured[-1]['input'].get('instructions'), '语气温柔，语速偏慢')
    check('没开润色时不发 parameters', 'parameters' in captured[-1], False)

    await engine.synthesize('乙。', instructions='大喊')
    check('逐句覆盖全局语气', captured[-1]['input']['instructions'], '大喊')

    await engine.synthesize('丙。', instructions='')
    check('传空串=这一句不带语气', 'instructions' in captured[-1]['input'], False)

    loud = voice.QwenTTS('k', 'qwen3-tts-instruct-flash', 'Cherry',
                         instructions='温柔', optimize_instructions=True)
    await loud.synthesize('丁。')
    check('开了润色就发 parameters', captured[-1]['parameters'], {'optimize_instructions': True})

    # 实测结论：flash / 复刻模型收下这个字段但不用它，所以干脆不发（免得用户以为生效了）
    check('flash 模型不算支持语气', voice.QwenTTS('k', 'qwen3-tts-flash', 'v').supports_tone, False)
    check('vc 模型不算支持语气', voice.QwenTTS('k', 'qwen3-tts-vc-2026-01-22', 'v').supports_tone, False)
    check('instruct 模型算支持语气',
          voice.QwenTTS('k', 'qwen3-tts-instruct-flash', 'v').supports_tone, True)
    unsupported = voice.QwenTTS('k', 'qwen3-tts-vc-2026-01-22', 'v', instructions='欢快')
    await unsupported.synthesize('己。')
    check('不支持的模型不发 instructions', 'instructions' in captured[-1]['input'], False)

    # 没写语气指令时，请求体里不该多出字段（改动前的行为要保住）
    plain = voice.QwenTTS('k', 'qwen3-tts-flash', 'Cherry')
    await plain.synthesize('戊。')
    check('没填语气就不发 instructions', 'instructions' in captured[-1]['input'], False)

    # 按角色覆盖：persona.json 的 tts 块
    class Cfg(dict):
        def get(self, k, d=None): return dict.get(self, k, d)

    cfg = Cfg(tts_enabled=True, tts_api_key='k', tts_model='qwen3-tts-flash',
              tts_voice='Cherry', tts_instructions='全局温柔', tts_provider='qwen')
    check('全局语气生效', voice.tts_from_config(cfg).instructions, '全局温柔')
    check('persona 覆盖语气',
          voice.tts_from_config(cfg, {'instructions': '清冷'}).instructions, '清冷')
    picked = voice.tts_from_config(cfg, {'model': 'qwen3-tts-instruct-flash', 'voice': 'Ethan'})
    check('persona 覆盖模型', picked.model, 'qwen3-tts-instruct-flash')
    check('persona 覆盖音色', picked.voice, 'Ethan')
    check('没被覆盖的字段仍来自 config', picked.instructions, '全局温柔')
    check('空串不覆盖', voice.tts_from_config(cfg, {'instructions': ''}).instructions, '全局温柔')
    check('本地引擎不接语气', voice.GptSoVitsTTS.supports_tone, False)

    # ---- Qwen-Audio-TTS：另一套接口，字段名是 input.instruction（单数） ----
    print('== Qwen-Audio-TTS 的请求体 ==')
    captured.clear()
    audio_engine = voice.QwenAudioTTS('k', 'qwen-audio-3.0-tts-flash', 'longanfengyue',
                                      instructions='语速偏慢')
    await audio_engine.synthesize('甲。')
    got = captured[-1]
    check('走的是语音合成专用接口', 'instruction' in got['input'], True)
    check('字段名是单数 instruction（不是 instructions）', got['input']['instruction'], '语速偏慢')
    check('模型名透传', got['model'], 'qwen-audio-3.0-tts-flash')
    check('基础参数按规范给全',
          (got['input']['format'], got['input']['sample_rate'], got['input']['volume'],
           got['input']['rate'], got['input']['pitch'], got['input']['language_hints']),
          ('wav', 24000, 50, 1.0, 1.0, ['zh']))
    await audio_engine.synthesize('乙。', instructions='')
    check('传空串=这一句不带语气', 'instruction' in captured[-1]['input'], False)
    check('这条路的引擎声明自己认语气', audio_engine.supports_tone, True)

    check('汉字算 2 个字', voice.instruction_cost('语速偏慢'), 8)
    check('字母标点算 1 个字', voice.instruction_cost('abc,.'), 5)
    check('假名算 1 个字（官方口径）', voice.instruction_cost('あいう'), 3)
    check('没超限就不裁', voice.clip_instruction('语速偏慢')[1], '')
    long_tone = '请用非常缓慢的速度朗读这段话' * 10
    clipped, note = voice.clip_instruction(long_tone)
    check('超长会被裁到上限内', voice.instruction_cost(clipped) <= 100, True)
    check('裁了就说出来', bool(note), True)
    captured.clear()
    await voice.QwenAudioTTS('k', 'qwen-audio-3.0-tts-flash', 'v').synthesize('丙。', instructions=long_tone)
    check('超长语气发出去的是裁好的那份',
          voice.instruction_cost(captured[-1]['input']['instruction']) <= 100, True)

    # 复刻：公网地址原样用，本地文件转 base64（实测这条路收 data: 地址）
    cloner = voice.AudioVoiceCloner('k')
    url, notes = voice.AudioVoiceCloner.resolve_audio('https://example.com/a.wav')
    check('公网地址原样用', url, 'https://example.com/a.wav')
    body = cloner.payload('lilyz', url, 'qwen-audio-3.0-tts-flash', language_hints=['ja'])
    check('复刻用 create_voice + url（不是 audio.data）',
          (body['model'], body['input']['action'], body['input']['url']),
          ('voice-enrollment', 'create_voice', 'https://example.com/a.wav'))
    check('复刻的 target_model 就是合成模型', body['input']['target_model'], 'qwen-audio-3.0-tts-flash')
    check('音色前缀只留字母数字、最多 10 位', voice.AudioVoiceCloner.check_prefix('li-lyz_花子123456789'),
          'lilyz12345')
    check('前缀跟着 payload 走到请求体里',
          voice.AudioVoiceCloner.payload('my-voice', url, 'm')['input']['prefix'], 'myvoice')

    # 本地文件 → base64（临时造一段 4 秒的 24kHz 单声道 wav）
    import tempfile, wave as _wave, math, array as _array
    tmp = Path(tempfile.gettempdir()) / '_tone_test_ref.wav'
    samples = _array.array('h', (int(3000 * math.sin(i / 20.0)) for i in range(24000 * 4)))
    with _wave.open(str(tmp), 'wb') as fh:
        fh.setnchannels(1); fh.setsampwidth(2); fh.setframerate(24000); fh.writeframes(samples.tobytes())
    resolved, notes = voice.AudioVoiceCloner.resolve_audio(str(tmp))
    check('本地文件被转成 data:base64 地址',
          resolved.startswith('data:audio/wav;base64,'), True)
    check('base64 能解回一段音频', len(base64.b64decode(resolved.split(',', 1)[1])) > 1000, True)
    try:
        tmp.unlink()
    except OSError:
        pass

    for bad in ('D:/不存在/xx.wav', 'ftp://x/y.wav', ''):
        try:
            voice.AudioVoiceCloner.resolve_audio(bad)
            blocked = False
        except voice.TTSError:
            blocked = True
        check('拦下没法用的地址（%r）' % bad[:14], blocked, True)

    # provider 切换：tts_provider=qwenaudio 时要用 audio_tts_* 这套键
    audio_cfg = Cfg(tts_enabled=True, tts_provider='qwenaudio', tts_api_key='k1',
                    tts_instructions='欢快', audio_tts_voice='longanlingxi',
                    audio_tts_model='qwen-audio-3.0-tts-flash', audio_tts_random_seed=True,
                    audio_tts_seed=7)
    picked = voice.tts_from_config(audio_cfg)
    check('provider=qwenaudio 造出 Audio 引擎', type(picked).__name__, 'QwenAudioTTS')
    check('音色来自 audio_tts_voice', picked.voice, 'longanlingxi')
    check('语气来自共用的 tts_instructions', picked.instructions, '欢快')
    check('Key 留空时沿用 tts_api_key', picked.api_key, 'k1')
    check('勾了「每次随机」就不发 seed', picked.seed, None)
    check('没填 Workspace ID 就走通用域名',
          picked.endpoint(), 'https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer')
    ws = voice.QwenAudioTTS('k', workspace='llm-abc')
    check('填了 Workspace ID 就用专属域名',
          ws.endpoint(),
          'https://llm-abc.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer')
    check('persona 能按角色换音色',
          voice.tts_from_config(audio_cfg, {'voice': 'loongjohn'}).voice, 'loongjohn')

    # 检测（probe）必须把「角色层语气」读出来，否则会谎报「语气：没填」
    # 注意：这里要自己造一份带语气的 persona——不能依赖项目里那份 prompts/persona.json
    # （通用部署包里它是空的，一依赖就会误报失败。打包时就是这么被抓出来的）。
    real_persona = ROOT / 'prompts' / 'persona.json'
    real_tone = ''
    try:
        real_tone = str((json.loads(real_persona.read_text(encoding='utf-8')).get('tts') or {})
                        .get('instructions') or '').strip()
    except Exception:
        pass
    check('项目自带的 persona.json 能被读出来（值可以为空）',
          voice.persona_tone_from_file(real_persona), real_tone)
    check('persona 路径不存在时安静返回空串',
          voice.persona_tone_from_file(ROOT / '不存在的.json'), '')

    persona_file = ROOT / '_tmp_probe_persona.json'
    persona_file.write_text(json.dumps({'tts': {'instructions': '语速偏慢，字句轻柔'}},
                                      ensure_ascii=False), encoding='utf-8')
    check('能从 persona.json 读出角色层语气',
          voice.persona_tone_from_file(persona_file), '语速偏慢，字句轻柔')
    report = voice.probe(config=dict(audio_cfg, tts_instructions=''), persona_path=persona_file)
    check('检测里会说明角色层语气在生效', '角色层' in report and '会生效' in report, True)
    check('检测里不会谎报「语气：没填」', '语气：没填' not in report, True)
    report2 = voice.probe(config=dict(audio_cfg, tts_instructions='全局这条'), persona_path=persona_file)
    check('角色层和全局都写了时，说明谁盖住谁', '盖住' in report2, True)
    persona_file.unlink(missing_ok=True)

    # ---- speak 怎么把语气分发到每一次合成 ----
    print('== speak 的语气分发 ==')
    calls = []

    class FakeEngine:
        def __init__(self, tone=True, pipeline=False, instructions=''):
            self.supports_tone = tone
            self.sentence_pipeline = pipeline
            self.instructions = instructions
        def availability(self): return True, 'fake'
        async def synthesize(self, text, voice=None, instructions=None):
            calls.append((text, instructions))
            return b'RIFF'

    class FakePlayer:
        def __init__(self): self.volume = 0; self.played = []
        def enqueue(self, audio): self.played.append(audio)
        def stop(self): pass

    def manager_for(engine):
        mgr = CM.__new__(CM)
        mgr.config = {'tts_volume': 80}
        mgr.persona = Path('不存在的 persona.json')
        mgr._player = FakePlayer()
        mgr._speech_generation = 0
        voice.tts_from_config = lambda cfg, over=None: engine
        return mgr

    async def speak_case(name, engine, text, segments, want):
        calls.clear()
        await manager_for(engine).speak(text, segments)
        check(name, calls, want)

    await speak_case('两段不同语气 → 两次请求，各带各的语气',
                     FakeEngine(), '甲。乙。',
                     [('小声', '甲。'), ('大喊', '乙。')],
                     [('甲。', '小声'), ('乙。', '大喊')])
    await speak_case('只有一段语气 → 一次请求读完，并套上那条语气',
                     FakeEngine(), '甲。乙。',
                     [('兴奋', '甲。乙。')],
                     [('甲。乙。', '兴奋')])
    await speak_case('没写标记 → 用引擎的全局语气',
                     FakeEngine(instructions='温柔'), '甲。乙。', None,
                     [('甲。乙。', '温柔')])
    await speak_case('模型不认语气 → 只发一次、不发语气参数',
                     FakeEngine(tone=False), '甲。乙。',
                     [('小声', '甲。'), ('大喊', '乙。')],
                     [('甲。乙。', '')])
    await speak_case('本地引擎 → 按句拆，不带语气参数',
                     FakeEngine(tone=False, pipeline=True),
                     '这是第一句话，说得相当长哦。这是第二句话，也说得挺长的。', None,
                     [('这是第一句话，说得相当长哦。', None), ('这是第二句话，也说得挺长的。', None)])
    await speak_case('括号里的动作描述不朗读',
                     FakeEngine(), '（微笑）甲。', None,
                     [('甲。', '')])


asyncio.run(main())
voice.httpx = types.SimpleNamespace(AsyncClient=real)


# ---------------------------------------------------------------- 语气标注提示词块
class _Cfg(dict):
    def get(self, k, d=None): return dict.get(self, k, d)


def tone_block_tests():
    print('== 语气标注提示词块 ==')
    mgr = CM.__new__(CM)
    mgr.persona = ROOT / '不存在的.json'
    mgr.config = _Cfg(tone_prompt_enabled=True)
    block = mgr.tone_prompt_block()
    check('默认就注入（恒输入）', bool(block), True)
    check('区块有明确标题', '【语气标注' in block, True)
    check('教的是「每一句都标」', '每一句' in block, True)
    check('给了标记格式', '【语气:说明】' in block, True)
    check('说明标记不会显示也不会被念出来', '看不到' in block and '读出来' in block, True)
    check('内置默认不是空话', len(CM.DEFAULT_TONE_PROMPT) > 80, True)

    mgr.config = _Cfg(tone_prompt_enabled=False)
    check('关掉开关就不注入', mgr.tone_prompt_block(), '')

    mgr.config = _Cfg(tone_prompt_enabled=True, tone_prompt='只说两个字：平淡')
    check('全局自定义生效', '只说两个字：平淡' in mgr.tone_prompt_block(), True)

    persona = ROOT / '_tmp_persona.json'
    persona.write_text(json.dumps({'tts': {'tone_prompt': '角色层这条优先',
                                           'tone_prompt_enabled': True}}, ensure_ascii=False),
                       encoding='utf-8')
    mgr.persona = persona
    check('角色层自定义优先于全局', '角色层这条优先' in mgr.tone_prompt_block(), True)
    persona.write_text(json.dumps({'tts': {'tone_prompt_enabled': False}}, ensure_ascii=False),
                       encoding='utf-8')
    check('角色层也能单独关掉', mgr.tone_prompt_block(), '')
    try:
        persona.unlink()
    except OSError:
        pass


tone_block_tests()

print()
if fails:
    print('%d 项失败：%s' % (len(fails), '、'.join(fails)))
    sys.exit(1)
print('全部通过')
