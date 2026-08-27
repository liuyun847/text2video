"""诊断模块:text2video doctor —— 本机运行环境逐项自检。

检查项:Python/包版本、ffmpeg/ffprobe 位置与版本、系统中文字体、
edge-tts 可用性、SAPI(Windows 本地语音)、TTS 缓存目录可写、配置文件加载;
--online 时额外测试 edge-tts 网络连通。

每项返回 (名称, PASS|WARN|FAIL, 说明);FAIL 表示不可用,会阻塞出片。
"""

from __future__ import annotations

import importlib.metadata
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable

from . import compose

LOG = logging.getLogger(__name__)

Check = tuple[str, str, str]  # (名称, 状态, 说明)


def ffmpeg_major_version(version_line: str) -> int:
    """从 ffmpeg 版本输出第一行解析主版本号;解析失败返回 0。

    兼容带后缀(如 "9.0-essentials_build-www.gyan.dev")与 git tag
    (如 "n6.1-3-g1234")形态的版本串。
    """
    match = re.search(r"ffmpeg version \D*(\d+)", version_line)
    return int(match.group(1)) if match else 0


def _ffmpeg_check() -> Check:
    try:
        path = compose.find_ffmpeg()
    except RuntimeError as exc:
        return ("ffmpeg", "FAIL", str(exc))
    try:
        ver = subprocess.run(
            [path, "-version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False, timeout=15,
        ).stdout.splitlines()[0]
    except (OSError, subprocess.TimeoutExpired):
        return ("ffmpeg", "FAIL", f"{path} 无法执行")
    # 版本号形如 "ffmpeg version 7.1-...";BGM 混音需要 >= 6.1
    if "version" not in ver:
        return ("ffmpeg", "WARN", f"{path}(输出异常:{ver[:60]})")
    major = ffmpeg_major_version(ver)
    note = "BGM 混音需 >= 6.1" if major < 6 else ""
    shown = ver.split("ffmpeg version ")[-1].split()[0]
    return ("ffmpeg", "PASS",
            f"{path}({shown}{' ' + note if note else ''})")


def _ffprobe_check() -> Check:
    try:
        path = compose.find_ffmpeg()
        probe = compose.find_ffprobe(path)
    except RuntimeError as exc:
        return ("ffprobe", "FAIL", str(exc))
    return ("ffprobe", "PASS", probe)


def _font_check() -> Check:
    path = compose.pick_system_font()
    if path:
        return ("中文字体", "PASS", path)
    return ("中文字体", "WARN", "未找到系统中文字体,字幕可能回退默认字体")


def _edge_tts_check() -> Check:
    try:
        ver = importlib.metadata.version("edge-tts")
    except importlib.metadata.PackageNotFoundError:
        return ("edge-tts", "FAIL", "未安装(uv sync 安装依赖)")
    return ("edge-tts", "PASS", f"v{ver}")


def _sapi_check() -> Check:
    from .tts_local import list_sapi_voices

    if os.name != "nt":
        return ("SAPI 本地语音", "WARN", "仅 Windows 可用,本机非 Windows")
    voices = list_sapi_voices()
    zh = [v for v in voices if v[1].lower().startswith("zh")]
    if zh:
        return ("SAPI 本地语音", "PASS",
                f"{len(voices)} 个语音,含中文: {zh[0][0]} ({zh[0][1]})")
    if voices:
        detail = (
            f"{len(voices)} 个语音但均为非中文"
            f"({', '.join(f'{n}({c})' for n, c in voices)});降级轴仍可出片,音质差"
        )
        return ("SAPI 本地语音", "WARN", detail)
    return ("SAPI 本地语音", "WARN", "未找到语音(System.Speech 不可用)")


def _cache_dir_check() -> Check:
    from .cli import default_tts_cache_dir

    path = default_tts_cache_dir()
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        return ("TTS 缓存目录", "WARN", f"{path} 不可写({exc}),缓存停用")
    return ("TTS 缓存目录", "PASS", path)


def _config_check(arg: str | None = None) -> Check:
    from .config import find_config, load_config

    try:
        path = find_config(arg)
        if not path:
            return ("配置文件", "PASS", "未使用(默认仅当前目录 text2video.toml)")
        cfg = load_config(path)
        return ("配置文件", "PASS", f"{path}({len(cfg)} 项)")
    except (FileNotFoundError, ValueError, TypeError) as exc:
        return ("配置文件", "WARN", f"读取失败: {exc}")


def _network_check() -> Check:
    """edge-tts 连通性(--online 时执行)。"""
    import asyncio

    from edge_tts import Communicate

    async def _probe() -> bool:
        try:
            async for chunk in Communicate("连通性测试", "zh-CN-XiaoxiaoNeural",
                                            boundary="WordBoundary").stream():
                if chunk["type"] == "audio":
                    return True
            return False
        except Exception:  # noqa: BLE001
            return False

    try:
        ok = asyncio.run(asyncio.wait_for(_probe(), timeout=15))
    except TimeoutError:
        return ("edge-tts 网络", "FAIL", "连接超时")
    return ("edge-tts 网络", "PASS", "可达") if ok else ("edge-tts 网络", "FAIL", "无音频返回")


def run_all_checks(
    *,
    online: bool = False,
    checks: list[Callable[[], Check]] | None = None,
    config_path: str | None = None,
) -> list[Check]:
    """执行全部检查;checks 可注入以覆盖默认项(测试用)。

    config_path:doctor --config 指定的配置文件路径,传给配置检查项。
    """
    items = checks or [
        lambda: ("Python", "PASS", f"{sys.version.split()[0]} on {os.name}"),
        _edge_tts_check,
        _ffmpeg_check,
        _ffprobe_check,
        _font_check,
        _sapi_check,
        _cache_dir_check,
        lambda: _config_check(config_path),
    ]
    if online:
        items.append(_network_check)
    return [fn() for fn in items]