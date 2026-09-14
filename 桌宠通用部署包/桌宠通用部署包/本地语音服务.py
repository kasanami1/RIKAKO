# -*- coding: utf-8 -*-
"""本地语音合成（GPT-SoVITS）一键脚本。

桌宠的「本地 · GPT-SoVITS」是免费离线的，但要先把那个服务跑起来。这个脚本就是干这个的——
把整合包、服务、参考音频、合成测试串成一条线，每一步都告诉你现在卡在哪。

用法（在项目目录里执行，随便哪种都行）：

    .venv\\Scripts\\python.exe 本地语音服务.py check      # 体检：目录/端口/参考音频/配置，不联网不花钱
    .venv\\Scripts\\python.exe 本地语音服务.py start      # 启动服务（会弹一个控制台窗口显示加载进度）
    .venv\\Scripts\\python.exe 本地语音服务.py test       # 服务起来后，真合成一句听听
    .venv\\Scripts\\python.exe 本地语音服务.py ref        # 检查并整理参考音频（3~10 秒规则）
    .venv\\Scripts\\python.exe 本地语音服务.py stop       # 停掉服务

不想记命令就双击同目录的两个 bat：
    `启动本地语音服务.bat`（= start，会等你按回车）
    `检查本地语音服务.bat`（= check，出结果后停住，方便看）

为什么要有这个脚本：工作台里也有「启动本地服务」按钮，功能一样；但整合包加载模型要几十秒到几分钟，
那个控制台窗口是**唯一能看见加载进度和报错**的地方，用这个脚本启动得到的正是一个不会被关掉的窗口。
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from voice import (LOCAL_REF_MAX, LOCAL_REF_MIN, LOCAL_TTS_URL, GptSoVitsTTS,  # noqa: E402
                   gptsovits_start_command, gptsovits_summary, prepare_local_ref,
                   probe_clone_audio, start_local_server)

CONFIG = HERE / 'config.json'
# 常见的整合包位置：填过 config 就用 config，没填就按这个顺序找一遍。
COMMON_HOMES = (
    r'D:\GPT-SoVITS', r'E:\GPT-SoVITS', r'D:\tools\GPT-SoVITS', r'C:\GPT-SoVITS',
    r'%USERPROFILE%\GPT-SoVITS', r'%USERPROFILE%\Desktop\GPT-SoVITS',
    r'%USERPROFILE%\Downloads\GPT-SoVITS',
)
MARKERS = ('api_v2.py', Path('runtime') / 'python.exe')


def say(text=''):
    print(text, flush=True)


def ok(text): say('  [OK] ' + text)
def info(text): say('  [信息] ' + text)
def warn(text): say('  [注意] ' + text)
def bad(text): say('  [错误] ' + text)


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_home(path: Path):
    """把找到的整合包目录写回 config.json，省得下次再找。"""
    try:
        cfg = load_config()
        cfg['gptsovits_home'] = str(path)
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
        ok('已把整合包目录写进 config.json：%s' % path)
    except Exception as exc:
        warn('写 config.json 失败（不影响本次使用）：%s' % exc)


def looks_like_home(path: Path) -> bool:
    return path.is_dir() and all((path / marker).exists() for marker in MARKERS)


def find_home(config: dict, explicit=None) -> Path | None:
    """找 GPT-SoVITS 整合包根目录：命令行 > config.json > 常见位置/旁边一层。"""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    saved = str(config.get('gptsovits_home') or '').strip().strip('"')
    if saved:
        candidates.append(Path(saved))
    for raw in COMMON_HOMES:
        candidates.append(Path(raw.replace('%USERPROFILE%', str(Path.home()))))
    # 项目目录的兄弟目录里也可能有（很多人习惯把整合包放在一起）
    try:
        for sibling in HERE.parent.iterdir():
            if sibling.is_dir() and 'sovits' in sibling.name.lower():
                candidates.append(sibling)
    except OSError:
        pass
    seen = set()
    for path in candidates:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if looks_like_home(path):
            return path
    return None


def local_notes(probe):
    """筛掉「云端声音复刻」的提示，只留对本地 GPT-SoVITS 有用的。

    `probe_clone_audio` 是给云端复刻写的（它按 10~20 秒最稳、≥24kHz 那套标准给建议），
    本地 GPT-SoVITS 的规则是 **3~10 秒**。两套标准一起打印会自相矛盾，所以这里过滤掉
    「10~20 秒」那条——不然用户会以为自己的 4.4 秒音频不合格。
    """
    return [note for note in probe.get('notes', []) if '10~20' not in note]


def host_port(config: dict):
    url = str(config.get('gptsovits_url') or LOCAL_TTS_URL)
    engine = GptSoVitsTTS(url)
    return engine.host_port()


def port_open(host, port, timeout=1.5) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(host, port, seconds=300, interval=5) -> bool:
    """等端口起来。整合包加载模型是**分钟级**的，所以这里默认等 5 分钟。"""
    deadline = time.monotonic() + max(5, int(seconds))
    started = time.monotonic()
    while time.monotonic() < deadline:
        if port_open(host, port):
            ok('服务已就绪：http://%s:%d（等了 %.0f 秒）'
               % (host, port, time.monotonic() - started))
            return True
        time.sleep(interval)
        info('还在加载… 已等 %.0f 秒（模型大，两三分钟很正常）' % (time.monotonic() - started))
    bad('等了 %d 秒端口还没起来' % seconds)
    return False


# ---------------------------------------------------------------- 子命令
def cmd_check(args):
    config = load_config()
    engine = GptSoVitsTTS(str(config.get('gptsovits_url') or LOCAL_TTS_URL))
    host, port = engine.host_port()

    say('== 1. 配置 ==')
    say('  ' + gptsovits_summary(config))
    say('  参考音频说的是：%s ｜ 要合成：%s'
        % (config.get('gptsovits_prompt_lang', 'zh'), config.get('gptsovits_text_lang', 'zh')))

    say('== 2. GPT-SoVITS 整合包 ==')
    home = find_home(config, args.home)
    if home:
        ok('找到整合包：%s' % home)
        for marker in MARKERS:
            ok('  %s 存在' % marker)
    else:
        bad('没找到整合包（需要里面同时有 api_v2.py 和 runtime\\python.exe）')
        info('装好后在「工作台 → 语音合成设置 → 本地」里填「整合包目录」，或再跑一次：')
        info('  .venv\\Scripts\\python.exe 本地语音服务.py check --home "D:\\你的\\GPT-SoVITS"')

    say('== 3. 服务端口 ==')
    if port_open(host, port):
        ok('http://%s:%d 已经在监听' % (host, port))
        info('想确认模型真的加载好了，跑：本地语音服务.py test')
    else:
        warn('http://%s:%d 没在监听 —— 服务还没起来（正常，第一次要手动启动）' % (host, port))
        info('双击 `启动本地语音服务.bat`，或跑：本地语音服务.py start')

    say('== 4. 参考音频 ==')
    ref = str(config.get('gptsovits_ref_audio') or '').strip().strip('"')
    if not ref:
        bad('还没设置参考音频（GPT-SoVITS 靠它决定音色）')
        info('在「工作台 → 语音合成设置 → 本地」里选一段 3~10 秒的干净人声')
    else:
        info('文件：%s' % ref)
        probe = probe_clone_audio(ref)
        if not probe.get('exists'):
            bad('文件不存在')
        else:
            seconds = probe.get('seconds')
            ok('时长 %s 秒 ｜ %s Hz ｜ %d 声道 ｜ %.1f MB'
               % (seconds, probe.get('rate'), probe.get('channels', 1),
                  (probe.get('size') or 0) / 1048576.0))
            for note in local_notes(probe):
                info(note)
            for problem in probe.get('errors', []):
                bad(problem)
            if seconds and not (LOCAL_REF_MIN <= seconds <= LOCAL_REF_MAX):
                warn('接口要求参考音频在 %.0f~%.0f 秒之间；别担心——'
                     '真正合成时桌宠会**自动削静音/截一段**（原文件不动）'
                     % (LOCAL_REF_MIN, LOCAL_REF_MAX))
            elif seconds:
                ok('时长在 %.0f~%.0f 秒范围内，正好' % (LOCAL_REF_MIN, LOCAL_REF_MAX))
    say('')
    return 0


def cmd_start(args):
    config = load_config()
    host, port = host_port(config)
    home = find_home(config, args.home)
    if not home:
        bad('没找到 GPT-SoVITS 整合包，先告诉我它在哪：')
        info('.venv\\Scripts\\python.exe 本地语音服务.py start --home "D:\\你的\\GPT-SoVITS"')
        info('（目录里要有 api_v2.py 和 runtime\\python.exe）')
        return 1
    if str(config.get('gptsovits_home') or '') != str(home):
        save_home(home)

    command = gptsovits_start_command(home, host, port)
    if not command:
        bad('整合包目录里找不到 runtime\\python.exe 或 api_v2.py：%s' % home)
        return 1
    exe, argv, cwd = command
    say('== 启动本地语音服务 ==')
    say('  命令：%s %s' % (exe, ' '.join(argv)))
    say('  目录：%s' % cwd)
    if args.dry_run:
        info('（--dry-run：只看命令，不真的启动）')
        return 0

    if port_open(host, port):
        info('http://%s:%d 已经在监听了，不用重复启动。' % (host, port))
        return 0

    proc = start_local_server(home, host, port)
    if proc is None:
        bad('启动失败，看看上面的报错。')
        return 1
    ok('已启动（PID %d）。**会弹出一个控制台窗口**，加载进度和报错都在那里，别关它。'
       % proc.pid)
    return 0 if wait_for_port(host, port, args.wait) else 1


def cmd_stop(args):
    config = load_config()
    host, port = host_port(config)
    if not port_open(host, port):
        info('http://%s:%d 本来就没在跑。' % (host, port))
        return 0
    import httpx
    url = str(config.get('gptsovits_url') or LOCAL_TTS_URL).rstrip('/')
    try:
        response = httpx.get(url + '/control', params={'command': 'exit'}, timeout=10)
        ok('已发送停止指令：HTTP %s' % response.status_code)
    except Exception as exc:
        warn('停止指令没发成功（%s）；可以直接关掉那个控制台窗口。' % exc)
    return 0


def cmd_ref(args):
    config = load_config()
    ref = args.path or str(config.get('gptsovits_ref_audio') or '').strip().strip('"')
    if not ref:
        bad('没给参考音频。用法：本地语音服务.py ref [音频路径]')
        return 1
    say('== 参考音频体检 ==')
    probe = probe_clone_audio(ref)
    if not probe.get('exists'):
        bad('文件不存在：%s' % ref)
        return 1
    info('时长 %s 秒 ｜ %s Hz ｜ %d 声道' % (probe.get('seconds'), probe.get('rate'),
                                             probe.get('channels', 1)))
    for note in local_notes(probe):
        info(note)
    for problem in probe.get('errors', []):
        bad(problem)
    say('== 整理成接口要的 3~10 秒 ==')
    try:
        out, note = prepare_local_ref(ref)
    except Exception as exc:
        bad('整理失败：%s' % exc)
        return 1
    ok('整理好了：%s' % out)
    say('  %s' % note)
    info('（这只是给接口用的临时副本，原文件一个字节都没动）')
    info('桌宠合成时会自动做同一件事，所以你平时不用手动跑这一步。')
    return 0


def cmd_test(args):
    config = load_config()
    engine = GptSoVitsTTS(str(config.get('gptsovits_url') or LOCAL_TTS_URL),
                          str(config.get('gptsovits_ref_audio') or ''),
                          str(config.get('gptsovits_prompt_text') or ''),
                          str(config.get('gptsovits_prompt_lang') or 'zh'),
                          str(config.get('gptsovits_text_lang') or 'zh'),
                          float(config.get('gptsovits_speed', 1.0) or 1.0),
                          float(config.get('gptsovits_timeout', 300) or 300),
                          str(config.get('gptsovits_split_method') or 'cut5'),
                          seed=int(config.get('gptsovits_seed', -1) or -1))
    host, port = engine.host_port()
    if not port_open(host, port):
        bad('服务没在跑（http://%s:%d）。先双击 `启动本地语音服务.bat`。' % (host, port))
        return 1
    ready, why = engine.availability()
    if not ready:
        bad(why)
        return 1

    text = args.text or '你好，我是你的桌面宠物。'
    say('== 合成测试 ==')
    say('  文本：%s（%d 字）' % (text, len(text)))
    say('  参考音色：%s' % engine.voice)
    info('CPU 推理慢，一句十几秒很正常，请稍等…')
    import asyncio
    import io
    import wave
    started = time.monotonic()
    try:
        audio = asyncio.run(engine.synthesize(text))
    except Exception as exc:
        bad('合成失败：%s' % exc)
        return 1
    elapsed = time.monotonic() - started

    def seconds(raw):
        try:
            with wave.open(io.BytesIO(raw), 'rb') as handle:
                rate = handle.getframerate() or 1
                frames = handle.getnframes()
                head = frames / float(rate)
                if head < 3600:
                    return head
                width = handle.getsampwidth() or 2
                channels = handle.getnchannels() or 1
                return max(len(raw) - 44, 0) / float(rate * channels * width)
        except Exception:
            return None

    out = Path(args.out or (HERE / '本地语音测试.wav'))
    out.write_bytes(audio)
    ok('成功：%.1f 秒出 %.1f 秒音频（%d 字节）' % (elapsed, seconds(audio) or 0, len(audio)))
    say('  已保存：%s' % out)
    say('  双击它听一下——这一步通过，说明「本地语音合成」整条路都通了。')
    if engine.ref_note:
        info(engine.ref_note)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='本地语音合成（GPT-SoVITS）一键脚本',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='子命令：check（体检）/ start（启动）/ test（合成测试）/ ref（整理参考音频）/ stop（停止）')
    parser.add_argument('action', nargs='?', default='check',
                        choices=['check', 'start', 'test', 'ref', 'stop', 'help'])
    parser.add_argument('--home', help='GPT-SoVITS 整合包根目录（里面有 api_v2.py 和 runtime）')
    parser.add_argument('--wait', type=int, default=300, help='start 时最多等多少秒（默认 300）')
    parser.add_argument('--dry-run', action='store_true', help='start 只打印命令，不真的启动')
    parser.add_argument('--text', help='test 要合成的文本')
    parser.add_argument('--out', help='test 输出到哪个 wav')
    parser.add_argument('path', nargs='?', help='ref 的音频路径')
    args = parser.parse_args(argv)
    if args.action == 'help':
        parser.print_help()
        return 0
    say()
    say('  本地语音合成（GPT-SoVITS）—— 当前动作：%s' % args.action)
    say('  ' + '-' * 52)
    handler = {'check': cmd_check, 'start': cmd_start, 'test': cmd_test,
               'ref': cmd_ref, 'stop': cmd_stop}[args.action]
    code = handler(args)
    say('')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
