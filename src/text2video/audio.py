"""响度处理模块:两遍 loudnorm 母带 + ebur128 复测(produce 与 edit 共用)。

两个调用场景:
- edit 层:前置混音 WAV -> apply_loudnorm(两遍 loudnorm)-> 母带 mp3;
- produce 层:--loudnorm 时对刚落盘的音轨 apply_loudnorm_inplace(先写临时文件,
  成功后再原子替换),让"纯文本一键"路径的响度也达标。

量测口径(与 ffmpeg 9.0 实测一致):
- 第一遍 loudnorm 带 print_format=json,从 stderr 取 input_i/input_tp/input_lra/
  input_thresh/target_offset;
- 第二遍带 measured_* 应用,优先线性增益(linear=true),失败退回动态模式;
- 之后用 ebur128=peak=true 复测成品,报告里的"响度之后值"就是这次复测结果。

所有 ffmpeg 调用只把测量结果打在 stderr,故用 subprocess 直接收 stderr(不打 INFO 日志,
避免把几百行测量输出灌进正常日志);参数一律按列表传(Windows 路径不拼字符串)。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess

LOG = logging.getLogger(__name__)

# 母带目标:流媒体常用 -14 LUFS / -1.5 dBTP(可被 edit.json 的 master 或命令行覆盖)
DEFAULT_TARGET_LUFS = -14.0
DEFAULT_TRUE_PEAK = -1.5
DEFAULT_LRA = 11.0
# loudnorm 允许的取值区间(与 ffmpeg 9.0 的滤镜选项范围一致)
LUFS_RANGE = (-70.0, -5.0)
PEAK_RANGE = (-9.0, 0.0)
# 母带规格:统一 48 kHz(混音前先对齐采样率)+ 192k mp3
SAMPLE_RATE = 48000
AUDIO_BITRATE = "192k"

# loudnorm 第一遍 JSON 里需要的字段
_LOUDNORM_KEYS = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
# ebur128 Summary 的三行(I / LRA / true peak):取最后一批匹配(Summary 在最后)
_EBUR128_I = re.compile(r"\bI:\s*(-?\d+(?:\.\d+)?)\s*LUFS")
_EBUR128_LRA = re.compile(r"\bLRA:\s*(-?\d+(?:\.\d+)?)\s*LU\b")
_EBUR128_TP = re.compile(r"\bPeak:\s*(-?\d+(?:\.\d+)?)\s*dBFS")


class AudioError(RuntimeError):
    """响度处理失败(参数越界 / 测量失败 / 应用失败);cli 打成"错误: ..."并返回 1。"""


def validate_target(target_lufs: float, true_peak: float) -> tuple[float, float]:
    """校验响度目标落在 loudnorm 的选项区间内,原样返回 (target_lufs, true_peak)。

    参数:
        target_lufs: 目标整合响度(LUFS);true_peak: 真峰值上限(dBTP)。
    返回:
        (float, float) 两个已转成 float 的目标值,便于调用方链式使用。
    异常:
        AudioError —— 任一值越界(loudnorm 会直接报滤镜参数非法,这里提前给可读原因)。
    """
    lufs = float(target_lufs)
    peak = float(true_peak)
    if not LUFS_RANGE[0] <= lufs <= LUFS_RANGE[1]:
        raise AudioError(
            f"目标响度 {lufs:g} LUFS 超出 loudnorm 允许区间 {LUFS_RANGE}"
            f"(流媒体常用 {DEFAULT_TARGET_LUFS:g})"
        )
    if not PEAK_RANGE[0] <= peak <= PEAK_RANGE[1]:
        raise AudioError(
            f"真峰值上限 {peak:g} dBTP 超出 loudnorm 允许区间 {PEAK_RANGE}"
            f"(流媒体常用 {DEFAULT_TRUE_PEAK:g})"
        )
    return lufs, peak


def _run_capture(ffmpeg: str, args: list[str], *, cwd: str) -> str:
    """跑一条只关心 stderr 的 ffmpeg 命令(loudnorm 测量 / ebur128 复测),返回 stderr。

    参数:
        ffmpeg: 可执行文件;args: 其余参数(不含可执行文件名);cwd: 工作目录。
    返回:
        stderr 文本(ffmpeg 9.0 把 loudnorm/ebur128 的测量结果打在 stderr)。
    异常:
        AudioError —— 退出码非 0(带 stderr 尾部,便于定位滤镜参数问题)。
    """
    cmd = [ffmpeg, *args]
    LOG.debug("ffmpeg 测量命令: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if proc.returncode != 0:
        raise AudioError(
            f"ffmpeg 测量失败(exit={proc.returncode})\n{proc.stderr.strip()[-2000:]}"
        )
    return proc.stderr


def parse_loudnorm_stats(stderr: str) -> dict:
    """从 loudnorm 的 print_format=json 输出里取出实测值(第一遍的测量结果)。

    参数:
        stderr: 一次 loudnorm 测量命令的 stderr。
    返回:
        {"input_i","input_tp","input_lra","input_thresh","target_offset"}(均为字符串,
        与 ffmpeg 输出一致;需要数值请自行 float)。
    异常:
        AudioError —— 没有 JSON / JSON 残缺 / 缺字段。
    """
    start = stderr.rfind("{")
    if start < 0:
        raise AudioError("loudnorm 没有输出 JSON 测量结果(检查 -af loudnorm 的参数)")
    try:
        data = json.JSONDecoder().raw_decode(stderr[start:])[0]
    except ValueError:
        raise AudioError("loudnorm 的 JSON 测量结果解析失败") from None
    missing = [key for key in _LOUDNORM_KEYS if key not in data]
    if missing:
        raise AudioError(f"loudnorm 测量结果缺字段: {', '.join(missing)}")
    return {key: data[key] for key in _LOUDNORM_KEYS}


def parse_ebur128(stderr: str) -> dict:
    """从 ebur128 的 Summary 里取整合响度 / LRA / true peak。

    参数:
        stderr: 一次 ebur128=peak=true 测量命令的 stderr。
    返回:
        {"i": LUFS, "lra": LU, "tp": dBFS}(浮点;取最后一批匹配 = Summary)。
    异常:
        AudioError —— 三类数值有缺失(说明滤镜没开 peak 或输出被截断)。
    """
    values: dict[str, float] = {}
    for key, pattern in (("i", _EBUR128_I), ("lra", _EBUR128_LRA), ("tp", _EBUR128_TP)):
        hits = pattern.findall(stderr)
        if not hits:
            raise AudioError(
                f"ebur128 输出里没有 {key} 的实测值(检查 -af ebur128=peak=true)"
            )
        values[key] = float(hits[-1])
    return values


def measure_loudness(ffmpeg: str, path: str, *, cwd: str) -> dict:
    """用 ebur128 量一条音频的响度(母带成品复测,即报告里的"响度之后值")。

    参数:
        ffmpeg: 可执行文件;path: 待测音频;cwd: 工作目录。
    返回:
        {"i","lra","tp"}。
    异常:
        AudioError —— ffmpeg 失败或数值解析失败。
    """
    stderr = _run_capture(
        ffmpeg,
        ["-hide_banner", "-nostats", "-i", path,
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        cwd=cwd,
    )
    return parse_ebur128(stderr)


def apply_loudnorm(
    ffmpeg: str,
    src: str,
    out: str,
    *,
    target_lufs: float,
    true_peak: float,
    cwd: str,
    sample_rate: int = SAMPLE_RATE,
    bitrate: str = AUDIO_BITRATE,
) -> tuple[dict, list[str]]:
    """两遍 loudnorm:第一遍实测(input_i/tp/lra...),第二遍带 measured_* 应用到 mp3。

    参数:
        ffmpeg: 可执行文件;src: 未做响度处理的音频;out: 输出 mp3;
        target_lufs/true_peak: 目标整合响度与 true peak;cwd: 工作目录;
        sample_rate/bitrate: 输出音频的采样率与 mp3 码率。
    返回:
        (响度信息 {before, after, target_lufs, true_peak, passes, mode}, 告警);
        before 来自 loudnorm 第一遍,before/after 里的 i/tp 单位分别是 LUFS / dBFS。
    异常:
        AudioError —— 测量或应用失败(线性模式失败会先退回动态模式再试)。
    """
    base_filter = f"loudnorm=I={target_lufs:g}:TP={true_peak:g}:LRA={DEFAULT_LRA:g}"
    stats = parse_loudnorm_stats(_run_capture(
        ffmpeg,
        ["-hide_banner", "-nostats", "-i", src,
         "-af", f"{base_filter}:print_format=json", "-f", "null", "-"],
        cwd=cwd,
    ))
    warnings: list[str] = []
    applied, mode = _loudnorm_apply_filter(base_filter, stats, linear=True)
    try:
        _run_capture(ffmpeg, [
            "-y", "-hide_banner", "-nostats", "-i", src, "-af", applied,
            "-ar", str(sample_rate), "-c:a", "libmp3lame", "-b:a", bitrate, out,
        ], cwd=cwd)
    except AudioError as exc:
        warnings.append(
            "loudnorm 线性模式失败,已退回动态模式(动态模式会压缩动态范围)"
            f": {str(exc).splitlines()[0]}"
        )
        applied, mode = _loudnorm_apply_filter(base_filter, stats, linear=False)
        _run_capture(ffmpeg, [
            "-y", "-hide_banner", "-nostats", "-i", src, "-af", applied,
            "-ar", str(sample_rate), "-c:a", "libmp3lame", "-b:a", bitrate, out,
        ], cwd=cwd)
    after = measure_loudness(ffmpeg, out, cwd=cwd)
    info = {
        "target_lufs": target_lufs,
        "true_peak": true_peak,
        "passes": 2,
        "mode": mode,
        "before": {
            "i": float(stats["input_i"]),
            "tp": float(stats["input_tp"]),
            "lra": float(stats["input_lra"]),
        },
        "after": after,
    }
    return info, warnings


def _loudnorm_apply_filter(base_filter: str, stats: dict, *, linear: bool) -> tuple[str, str]:
    """拼第二遍的 loudnorm 参数(linear=True 走线性增益,失败时调用方改 False)。

    参数:
        base_filter: 第一遍用的 loudnorm 基础参数串;stats: 第一遍实测值;
        linear: True = linear=true(只做整体增益),False = 动态模式。
    返回:
        (第二遍滤镜参数串, 模式名 "linear" / "dynamic")。
    """
    flag = "true" if linear else "false"
    applied = (
        f"{base_filter}:measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
        f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
        f":offset={stats['target_offset']}:linear={flag}"
    )
    return applied, "linear" if linear else "dynamic"


def apply_loudnorm_inplace(
    ffmpeg: str,
    path: str,
    *,
    target_lufs: float = DEFAULT_TARGET_LUFS,
    true_peak: float = DEFAULT_TRUE_PEAK,
) -> tuple[dict, list[str]]:
    """对已落盘的音轨原地做两遍 loudnorm(先写临时文件,成功后原子替换)。

    参数:
        ffmpeg: 可执行文件;path: 音轨路径(mp3);
        target_lufs/true_peak: 目标整合响度与真峰值上限。
    返回:
        (响度信息 {before, after, target_lufs, true_peak, passes, mode}, 告警列表)。
    异常:
        AudioError —— 参数越界或 ffmpeg 失败(此时原文件保持不变)。
    副作用:
        成功时覆盖 path;临时文件 {path 去扩展名}.loudnorm.tmp.mp3 不论成败都删除。
    """
    lufs, peak = validate_target(target_lufs, true_peak)
    abs_path = os.path.abspath(os.path.expanduser(path))
    tmp = f"{os.path.splitext(abs_path)[0]}.loudnorm.tmp.mp3"
    try:
        info, warnings = apply_loudnorm(
            ffmpeg, abs_path, tmp, target_lufs=lufs, true_peak=peak,
            cwd=os.path.dirname(abs_path),
        )
        os.replace(tmp, abs_path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError as exc:  # 临时文件被占用(如杀毒扫描)只告警
                LOG.warning("响度母带临时文件删除失败(忽略): %s", exc)
    return info, warnings


def summary_line(info: dict) -> str:
    """把响度信息压成一行人看的结论(produce/make 打印用;纯函数)。

    参数:
        info: apply_loudnorm / apply_loudnorm_inplace 返回的响度信息。
    返回:
        一行字符串:"响度母带: -19.8 → -14.1 LUFS(目标 -14),true peak ..."。
    """
    before = info["before"]
    after = info["after"]
    return (
        f"响度母带: {before['i']:.1f} → {after['i']:.1f} LUFS"
        f"(目标 {info['target_lufs']:g}),true peak "
        f"{before['tp']:.1f} → {after['tp']:.1f} dBFS"
        f"(上限 {info['true_peak']:g},{info['passes']} 遍 loudnorm/{info['mode']})"
    )
