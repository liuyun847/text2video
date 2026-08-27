"""本地 TTS 模块:用 Windows 自带 SAPI(System.Speech)离线合成中文语音。

作为 edge-tts(在线)的兜底:断网/网络不稳时仍能出片。要点:
- 零 Python 依赖,通过 PowerShell 调用 System.Speech,输出 WAV;
- SpeakProgress 事件给出"字符索引 + 音频位置",据此还原成逐字时间轴;
- 事件覆盖不足(语音包对文本分词粒度粗)时降级:估算轴按实测 wav 时长线性缩放,
  逐字点亮精度下降(近似),此模式会在日志中告警;
- 仅 Windows 可用;文本经临时文件传递(UTF-8),避免命令行编码问题。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import struct
import subprocess
import tempfile

from .subs import WordTiming

LOG = logging.getLogger(__name__)

# 固定 PowerShell 脚本体(数据经 env/临时文件传递,无转义问题)
_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$text = [System.IO.File]::ReadAllText($env:T2V_TEXT_FILE, [System.Text.Encoding]::UTF8)
if (-not $text) { Write-Error 'empty text'; exit 2 }
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  if ($env:T2V_VOICE) {
    $synth.SelectVoice($env:T2V_VOICE)
  } else {
    $zh = $synth.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo } |
          Where-Object { $_.Culture.Name -like 'zh*' } | Select-Object -First 1
    if ($zh) { $synth.SelectVoice($zh.Name) }
  }
  if ($env:T2V_RATE) { $synth.Rate = [int]$env:T2V_RATE }
  $list = New-Object System.Collections.ArrayList
  # 用 .NET 同步委托而非 Register-ObjectEvent:后者的事件 Action 异步排队,
  # Speak() 返回时可能尚未执行(收集为空且后续枚举列表会冲突)。
  # 注意:该事件的事件处理器类型是 EventHandler<SpeakProgressEventArgs>,
  # 不是 SpeakProgressEventHandler 类型(后者在部分 .NET 版本不存在)。
  $handler = [System.EventHandler[System.Speech.Synthesis.SpeakProgressEventArgs]]{
    param($s, $e)
    [void]$list.Add([pscustomobject]@{
      i = [int]$e.CharacterIndex
      c = [int]$e.CharacterCount
      t = [double]$e.AudioPosition.TotalSeconds
    })
  }
  $synth.add_SpeakProgress($handler)
  $synth.SetOutputToWaveFile($env:T2V_OUT)
  $synth.Speak($text)
  $synth.SetOutputToNull()
  $events = @($list | Sort-Object i, t | Select-Object i, c, t |
    ConvertTo-Json -Compress -Depth 3)
  Write-Output $events
} finally {
  $synth.Dispose()
}
"""


def _powershell_bin() -> str | None:
    """定位 powershell(5.1,Windows 自带);找不到再试 pwsh(7)。"""
    for name in ("powershell", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _encoded_command(script: str) -> str:
    """PowerShell -EncodedCommand:UTF-16LE + base64,规避引号/编码问题。"""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def wav_duration(path: str) -> float:
    """解析 WAV 头得到时长(秒);非 wav 或损坏时抛 ValueError。

    SAPI 输出的 WAV 可能带 JUNK/fact 等额外 chunk,不能假设 data 固定偏移,
    因此按 chunk 表遍历定位 data(size)与 fmt(rate/bits/channels)。
    """
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError(f"非 WAV 文件: {path}")
        sample_rate = channels = bits = None
        data_size = None
        try:
            while True:
                cid = f.read(4)
                if len(cid) < 4:
                    break
                csize_bytes = f.read(4)
                if len(csize_bytes) < 4:
                    raise ValueError(f"WAV 头截断: {path}")
                (csize,) = struct.unpack_from("<I", csize_bytes)
                if cid == b"fmt ":
                    fmt = f.read(csize)
                    if len(fmt) < 16:
                        raise ValueError(f"WAV fmt chunk 截断: {path}")
                    channels = struct.unpack_from("<H", fmt, 2)[0]
                    sample_rate = struct.unpack_from("<I", fmt, 4)[0]
                    bits = struct.unpack_from("<H", fmt, 14)[0]
                elif cid == b"data":
                    data_size = csize
                    break
                else:
                    f.seek(csize, 1)  # 跳过未知 chunk
                if csize % 2:  # chunk 内容对齐到偶字节
                    f.seek(1, 1)
        except struct.error as exc:
            raise ValueError(f"WAV 头损坏: {path}") from exc
    if not (sample_rate and channels and bits):
        raise ValueError(f"WAV 缺少 fmt 信息: {path}")
    if data_size is None:
        raise ValueError(f"WAV 缺少 data chunk: {path}")
    return data_size / (sample_rate * channels * bits / 8)


def list_sapi_voices() -> list[tuple[str, str]]:
    """枚举 SAPI 已安装语音,返回 [(名称, Culture 名)];不可用返回 []。"""
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "try { ($s.GetInstalledVoices() | ForEach-Object { "
        "\"$($_.VoiceInfo.Name)|$($_.VoiceInfo.Culture.Name)\" }) -join \"`n\" } "
        "finally { $s.Dispose() }"
    )
    try:
        proc = subprocess.run(
            [_powershell_bin(), "-NoProfile", "-NonInteractive",
             "-EncodedCommand", _encoded_command(script)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    voices: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, culture = line.partition("|")
        voices.append((name.strip(), culture.strip()))
    return voices


def sapi_available() -> bool:
    """SAPI 是否可用:Windows + 找到 powershell + 能枚举语音。"""
    if os.name != "nt" or not _powershell_bin():
        return False
    return bool(list_sapi_voices())


def _norm(s: str) -> str:
    """归一化:去空白与标点,仅留汉字/字母数字,用于事件拼接一致性比对。"""
    return "".join(ch for ch in s if ch.isalnum())


def _events_to_timings(
    text: str, events: list[dict], duration: float
) -> tuple[list[WordTiming], bool]:
    """SpeakProgress 事件列表 → 逐字时间轴(块级:SAPI 按词/块触发)。

    events: [{"i": 字符起点, "c": 覆盖字符数, "t": 音频位置(秒)}]。
    每个事件块 = 文本切片 [i, i+c),start=事件 t,end=下一事件 t;
    最后一块 end = 最后事件 t + 平均间隔。

    可靠性校验(不满足则返回 ([], False) 触发调用方降级估算轴):
    - CharacterIndex 不可靠(实测桌面语音恒为 0):拼接块与原文不一致;
    - AudioPosition 超出实际 WAV 时长(实测 Huihui Desktop 事件时间偏大)。
    返回 (timings, scaled):scaled=True 表示已做估算缩放降级。
    """
    if not events:
        return [], False
    events = sorted(events, key=lambda e: (e["i"], e["t"]))
    n = len(text)
    covered = 0
    for e in events:
        covered += max(0, min(e["i"] + e["c"], n) - max(e["i"], 0))
    norm_len = len(_norm(text))
    if norm_len and covered < norm_len * 0.5:
        LOG.warning("SAPI 事件覆盖不足(%d/%d 字符),将使用估算缩放轴",
                    covered, norm_len)
        return [], False
    # 拼接一致性:事件块按序拼接应还原出原文(容忍个别词边界偏差)
    if events and norm_len:
        glued = ""
        for e in sorted(events, key=lambda e: e["t"]):
            i0 = max(0, min(e["i"], n))
            i1 = max(0, min(e["i"] + e["c"], n))
            glued += _norm(text[i0:i1])
        match = 0
        for a, b in zip(_norm(text), glued):
            if a == b:
                match += 1
        if match < norm_len * 0.5:
            LOG.warning("SAPI 事件索引不可靠(拼接一致率 %d/%d),将使用估算缩放轴",
                        match, norm_len)
            return [], False
    # 音频位置不得明显超出 WAV 实际时长(超出则事件时间不可信)
    if events and any(e["t"] > duration + 0.5 for e in events):
        LOG.warning("SAPI 事件时间超出音频时长,将使用估算缩放轴")
        return [], False

    timings: list[WordTiming] = []
    for k, e in enumerate(events):
        i0 = max(0, min(e["i"], n))
        i1 = max(0, min(e["i"] + e["c"], n))
        chunk = text[i0:i1].strip()
        if not chunk:
            continue
        start = min(e["t"], duration)
        if k + 1 < len(events):
            end = min(max(start, events[k + 1]["t"]), duration)
        else:
            gaps = [events[j + 1]["t"] - events[j]["t"]
                    for j in range(len(events) - 1) if events[j + 1]["t"] > events[j]["t"]]
            gap = sum(gaps) / len(gaps) if gaps else 0.2
            end = min(start + gap, duration)  # 末块一律钳到音频时长内
        if end <= start:
            end = min(start + 0.1, duration)
        if end <= start:
            continue  # 时长边界内放不下,跳过该微块
        timings.append(WordTiming(chunk, start, end))
    return timings, False


def estimate_sapi_timeout(text: str) -> float:
    """按文本长度估算 SAPI 合成超时(秒)。

    System.Speech 的 Speak() 是实时渲染(约 1 秒音频≈1 秒墙钟),
    中文语速约 4 字/秒,按 1.8 倍余量 + 30s 启动开销估算,常用文案必有富余。
    """
    return max(60.0, len(text) / 4.0 * 1.8 + 30.0)


def _estimate_scaled(text: str, duration: float) -> list[WordTiming]:
    """降级轴:估算逐字轴(均匀)整体缩放到实测时长。"""
    from .timeline import estimate_timings

    est = estimate_timings(text)
    if not est or duration <= 0:
        return []
    scale = duration / max(est[-1].end, 0.001)
    return [WordTiming(w.text, w.start * scale, w.end * scale) for w in est]


def synthesize_sapi(
    text: str,
    *,
    out_audio: str,
    voice: str | None = None,
    rate: int = 0,
    timeout: float | None = None,
) -> list[WordTiming]:
    """SAPI 离线合成到 out_audio(wav),返回逐字时间轴(可能为估算缩放轴)。

    timeout 缺省按文本长度自动估算(见 estimate_sapi_timeout),
    避免长文案被固定短超时误杀(upspeak 为实时渲染)。
    """
    if not text.strip():
        raise ValueError("文案为空,无法合成")
    ps = _powershell_bin()
    if os.name != "nt" or not ps:
        raise RuntimeError("SAPI 仅支持 Windows(且需 powershell 可用)")
    if not os.path.isabs(out_audio):
        out_audio = os.path.abspath(out_audio)
    os.makedirs(os.path.dirname(out_audio), exist_ok=True)
    # 文本经临时文件传入,任何编码/特殊字符都安全
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".txt", delete=False
    ) as tf:
        tf.write(text)
        text_file = tf.name
    env = dict(os.environ)
    env["T2V_TEXT_FILE"] = text_file
    env["T2V_OUT"] = out_audio
    env["T2V_VOICE"] = voice or ""
    env["T2V_RATE"] = str(rate)
    try:
        eff_timeout = timeout or estimate_sapi_timeout(text)
        proc = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive",
             "-EncodedCommand", _encoded_command(_PS_SCRIPT)],
            env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=eff_timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"SAPI 合成超时({eff_timeout:.0f}s)") from exc
    finally:
        try:
            os.remove(text_file)
        except OSError:
            pass
    if proc.returncode != 0:
        raise RuntimeError(
            f"SAPI 合成失败(exit={proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[-500:]}"
        )
    try:
        duration = wav_duration(out_audio)
    except ValueError as exc:
        raise RuntimeError(f"SAPI 输出异常: {exc}") from exc

    events: list[dict] = []
    line = (proc.stdout or "").strip()
    if line:
        try:
            events = json.loads(line)
        except json.JSONDecodeError:
            events = []
    timings, scaled = _events_to_timings(text, events, duration)
    if not timings:
        LOG.warning("SAPI 逐字轴不可用,降级为估算缩放轴(点亮精度下降)")
        timings = _estimate_scaled(text, duration)
        scaled = True
    if not timings:
        raise RuntimeError("SAPI 合成未产出时间轴")
    LOG.info("SAPI 合成完成: %.2fs, %d 个词元(%s)",
             duration, len(timings), "估算缩放轴" if scaled else "事件轴")
    return timings