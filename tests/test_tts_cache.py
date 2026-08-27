"""TTS 缓存 + 重试单测(不联网,纯缓存读写与重试逻辑)。"""

import pytest

from text2video.subs import WordTiming
from text2video.tts import (
    _call_with_retry,
    cache_key,
    cache_load,
    cache_save,
    synthesize,
)


def test_cache_key_stable_and_sensitive():
    k1 = cache_key("v", "+0%", "+0%", "+0Hz", "你好")
    assert k1 == cache_key("v", "+0%", "+0%", "+0Hz", "你好")
    assert k1 != cache_key("v2", "+0%", "+0%", "+0Hz", "你好")
    assert k1 != cache_key("v", "+10%", "+0%", "+0Hz", "你好")
    assert k1 != cache_key("v", "+0%", "+0%", "+0Hz", "你好。")


def test_cache_roundtrip(tmp_path):
    key = "abc"
    audio = b"MP3FAKE"
    words = [WordTiming("你", 0.0, 0.5), WordTiming("好", 0.5, 1.0)]
    cache_save(str(tmp_path), key, audio, words)
    hit = cache_load(str(tmp_path), key)
    assert hit is not None
    got_audio, got_words = hit
    assert got_audio == audio
    assert [(w.text, w.start, w.end) for w in got_words] == \
        [("你", 0.0, 0.5), ("好", 0.5, 1.0)]


def test_cache_load_partial_missing_returns_none(tmp_path):
    assert cache_load(str(tmp_path), "nope") is None
    (tmp_path / "x.mp3").write_bytes(b"xx")
    assert cache_load(str(tmp_path), "x") is None  # 缺 timings.json


def test_synthesize_hits_cache_without_network(tmp_path, monkeypatch):
    """命中缓存时不调用 edge-tts(monkeypatch 让联网必抛)。"""
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("不应联网")

    # 先手动灌一份缓存(用真实 key 计算)
    key = cache_key("zh-CN-XiaoxiaoNeural", "+0%", "+0%", "+0Hz", "你好")
    cache_save(str(tmp_path), key, b"MP3", [WordTiming("你好", 0.0, 1.0)])
    monkeypatch.setattr("text2video.tts._call_with_retry", _boom)
    out = tmp_path / "a.mp3"
    words = synthesize("你好", out_audio=str(out), cache_dir=str(tmp_path))
    assert calls["n"] == 0
    assert out.read_bytes() == b"MP3"
    assert words[0].text == "你好"


def test_synthesize_misses_cache_then_writes(tmp_path, monkeypatch):
    """未命中时走 _call_with_retry;本次用假实现避免真联网。"""
    fake = lambda fn: (b"NEWMP3", [WordTiming("哈", 0.0, 0.9)])
    monkeypatch.setattr("text2video.tts._call_with_retry", lambda fn: fake(fn))
    out = tmp_path / "b.mp3"
    words = synthesize("哈", out_audio=str(out), cache_dir=str(tmp_path))
    assert out.read_bytes() == b"NEWMP3"
    assert words[0].text == "哈"
    # 缓存已写入,二次命中
    hit = cache_load(str(tmp_path), cache_key("zh-CN-XiaoxiaoNeural", "+0%", "+0%", "+0Hz", "哈"))
    assert hit is not None


def test_call_with_retry_succeeds_after_failures():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("boom")
        return "ok"

    assert _call_with_retry(flaky, tries=3, base_delay=0.01) == "ok"
    assert attempts["n"] == 3


def test_call_with_retry_exhausts_raises():
    def always_fail():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _call_with_retry(always_fail, tries=2, base_delay=0.01)