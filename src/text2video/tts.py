"""TTS 模块:调用 edge-tts(微软 Edge 在线神经语音)合成语音,
并同时提取逐字时间轴(WordBoundary),作为滚动字幕的同步依据。

edge-tts 的 WordBoundary 事件字段:offset/duration 单位是 100ns 的 tick,
换算成秒 = tick / 10_000_000(与官方 SubMaker 的换算一致)。

缓存:按 (voice, rate, volume, pitch, text) 哈希,命中时跳过联网合成,
直接复用 mp3 与逐字时间轴(render 二次渲染不重复联网)。
重试:网络抖动时按指数退避重试,重试耗尽抛最后一次异常。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time

from .subs import WordTiming

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
_TICKS_PER_SECOND = 10_000_000
_MAX_TRIES = 3
_BASE_DELAY = 1.0  # 指数退避基数(秒):1, 2, 4

LOG = logging.getLogger(__name__)


async def _synthesize_async(
    text: str,
    *,
    voice: str,
    rate: str,
    volume: str,
    pitch: str,
) -> tuple[bytes, list[WordTiming]]:
    from edge_tts import Communicate

    # boundary="WordBoundary" 才有逐字时间轴
    comm = Communicate(
        text, voice,
        rate=rate, volume=volume, pitch=pitch,
        boundary="WordBoundary",
    )
    words: list[WordTiming] = []
    audio = bytearray()
    async for chunk in comm.stream():
        ctype = chunk["type"]
        if ctype == "audio":
            audio += chunk["data"]
        elif ctype == "WordBoundary":
            token = (chunk.get("text") or "").strip()
            if not token:
                continue
            offset = float(chunk["offset"]) / _TICKS_PER_SECOND
            duration = float(chunk["duration"]) / _TICKS_PER_SECOND
            words.append(WordTiming(token, offset, offset + duration))
    # 内部 aiohttp session 在 stream() 的 async with 中自动关闭,无需手动 close
    if not audio:
        raise RuntimeError("edge-tts 未返回任何音频,请检查网络或音色参数")
    return bytes(audio), words


def _call_with_retry(fn, tries: int = _MAX_TRIES, base_delay: float = _BASE_DELAY):
    """对无参调用 fn 做指数退避重试(网络类异常),耗尽时抛最后一次异常。"""
    last: Exception | None = None
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 网络/合成失败都要重试
            last = exc
            if attempt < tries:
                delay = base_delay * (2 ** (attempt - 1))
                LOG.warning("edge-tts 合成失败(第 %d/%d 次): %s;%.1fs 后重试",
                            attempt, tries, exc, delay)
                time.sleep(delay)
    assert last is not None
    raise last


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------

def cache_key(voice: str, rate: str, volume: str, pitch: str, text: str) -> str:
    """TTS 缓存 key:hash(voice|rate|volume|pitch|text),变化任一参数即失效。"""
    payload = f"{voice}|{rate}|{volume}|{pitch}|{text}".encode()
    return hashlib.sha256(payload).hexdigest()


def _words_to_json(words: list[WordTiming]) -> list[dict]:
    return [{"text": w.text, "start": w.start, "end": w.end} for w in words]


def cache_load(cache_dir: str, key: str) -> tuple[bytes, list[WordTiming]] | None:
    """读取缓存(mp3 + timings json)。任一缺失/损坏返回 None(视为未命中)。"""
    mp3_path = os.path.join(cache_dir, f"{key}.mp3")
    ts_path = os.path.join(cache_dir, f"{key}.timings.json")
    try:
        with open(mp3_path, "rb") as f:
            audio = f.read()
        with open(ts_path, encoding="utf-8") as f:
            data = json.load(f)
        words = [WordTiming(d["text"], float(d["start"]), float(d["end"]))
                 for d in data]
    except (OSError, KeyError, ValueError, TypeError):
        return None
    if not audio or not words:
        return None
    return audio, words


def cache_save(cache_dir: str, key: str, audio: bytes,
               words: list[WordTiming]) -> None:
    """写缓存;目录自动创建,失败仅告警(缓存不影响主流程)。"""
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, f"{key}.mp3"), "wb") as f:
            f.write(audio)
        with open(os.path.join(cache_dir, f"{key}.timings.json"),
                  "w", encoding="utf-8") as f:
            json.dump(_words_to_json(words), f, ensure_ascii=False)
    except OSError as exc:
        LOG.warning("写入 TTS 缓存失败(忽略): %s", exc)


def synthesize(
    text: str,
    *,
    voice: str = DEFAULT_VOICE,
    rate: str = "+0%",
    volume: str = "+0%",
    pitch: str = "+0Hz",
    out_audio: str,
    cache_dir: str | None = None,
) -> list[WordTiming]:
    """合成语音到 out_audio(mp3),返回逐字时间轴列表。

    cache_dir 非空时先查缓存:命中则拷贝缓存音频并读时间轴,不联网;
    未命中则联网合成并写回缓存。
    """
    key = cache_key(voice, rate, volume, pitch, text)
    if cache_dir:
        hit = cache_load(cache_dir, key)
        if hit is not None:
            audio, words = hit
            LOG.info("TTS 缓存命中(%s),跳过联网合成", key[:12])
            with open(out_audio, "wb") as f:
                f.write(audio)
            return words

    audio, words = _call_with_retry(lambda: asyncio.run(
        _synthesize_async(
            text, voice=voice, rate=rate, volume=volume, pitch=pitch,
        )
    ))
    with open(out_audio, "wb") as f:
        f.write(audio)
    if cache_dir:
        cache_save(cache_dir, key, audio, words)
    return words


def list_voices(language: str = "zh-CN") -> list[dict]:
    """列出可用音色(language 形如 'zh-CN',None 表示全部)。"""
    from edge_tts import list_voices as _list_voices

    voices = asyncio.run(_list_voices())
    if language:
        voices = [v for v in voices if v.get("Locale", "").lower() == language.lower()]
    return voices