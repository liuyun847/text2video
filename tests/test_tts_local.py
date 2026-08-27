"""tts_local 模块单测:WAV 时长解析、事件→时间轴、估算缩放降级。

完整 SAPI 合成依赖 Windows 语音,不在此处联网/运行(仅纯函数与格式)。
"""

import pytest

from text2video.tts_local import (
    _estimate_scaled,
    _events_to_timings,
    wav_duration,
)


def _make_wav(path, sample_rate=16000, channels=1, bits=16, seconds=1.0):
    """写一个标准 PCM WAV 头 + 静音体。"""
    data_size = int(sample_rate * channels * bits / 8 * seconds)
    import struct
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))
        f.write(b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                            sample_rate * channels * bits // 8,
                            channels * bits // 8, bits))
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(b"\x00" * data_size)


def test_wav_duration(tmp_path):
    p = tmp_path / "a.wav"
    _make_wav(p, seconds=2.0)
    assert abs(wav_duration(str(p)) - 2.0) < 0.01


def test_wav_duration_with_extra_chunks(tmp_path):
    """SAPI 输出可能带 JUNK/fact chunk:按 chunk 表遍历应仍能解析。"""
    import struct
    p = tmp_path / "j.wav"
    rate, seconds = 8000, 1.0
    data_size = int(rate * 2 * seconds)
    junk = b"JUNK" + struct.pack("<I", 8) + b"\x00" * 8
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    data = b"data" + struct.pack("<I", data_size) + b"\x00" * data_size
    with open(p, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 4 + len(junk) + len(fmt) + len(data))
                + b"WAVE" + junk + fmt + data)
    assert abs(wav_duration(str(p)) - 1.0) < 0.01


def test_wav_duration_no_data_chunk(tmp_path):
    import struct
    p = tmp_path / "nodata.wav"
    with open(p, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36) + b"WAVE"
                + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16))
    with pytest.raises(ValueError, match="data chunk"):
        wav_duration(str(p))


def test_wav_duration_invalid(tmp_path):
    p = tmp_path / "bad.wav"
    p.write_bytes(b"NOTWAVE")
    with pytest.raises(ValueError, match="非 WAV"):
        wav_duration(str(p))


def test_wav_duration_truncated_header_raises_valueerror(tmp_path):
    """头被截断(如 data size 声明超文件尾)应抛 ValueError 而非 struct.error。"""
    import struct
    p = tmp_path / "trunc.wav"
    with open(p, "wb") as f:
        # fmt chunk 声明 16 字节,但实际不足
        f.write(b"RIFF" + struct.pack("<I", 60) + b"WAVE"
                + b"fmt " + struct.pack("<I", 16) + b"\x00\x00")
    with pytest.raises(ValueError, match="截断|损坏"):
        wav_duration(str(p))


def test_estimate_sapi_timeout_scales_with_text():
    from text2video.tts_local import estimate_sapi_timeout

    assert estimate_sapi_timeout("") >= 60.0
    short = estimate_sapi_timeout("你好" * 10)     # 20 字
    long = estimate_sapi_timeout("你好" * 200)     # 400 字
    assert long > short
    assert long > 120.0  # 400 字实时渲染远超旧固定 120s


def test_events_to_timings_last_block_clamped_to_duration():
    """末块 end 不得超出音频时长(原实现 duration==start 时可能越界)。"""
    text = "你好世界"
    events = [{"i": 0, "c": 2, "t": 0.0}, {"i": 2, "c": 2, "t": 1.5}]
    timings, scaled = _events_to_timings(text, events, duration=1.5)
    assert scaled is False
    assert all(w.end <= 1.5 + 1e-9 for w in timings)
    assert timings[-1].end == 1.5


def test_events_to_timings_block_level():
    text = "你好世界"
    events = [{"i": 0, "c": 2, "t": 0.0}, {"i": 2, "c": 2, "t": 0.8}]
    timings, scaled = _events_to_timings(text, events, duration=1.6)
    assert scaled is False
    assert [(w.text, w.start, w.end) for w in timings] == [
        ("你好", 0.0, 0.8), ("世界", 0.8, 1.6)]


def test_events_to_timings_unreliable_index_falls_back():
    """实测 Huihui Desktop 的 CharacterIndex 恒为 0:拼接不符 → 降级。"""
    text = "大家好欢迎来到频道"
    events = [{"i": 0, "c": 2, "t": 0.5} for _ in range(5)]
    timings, scaled = _events_to_timings(text, events, duration=5.0)
    assert timings == [] and scaled is False


def test_events_to_timings_time_beyond_duration_falls_back():
    """AudioPosition 超出 WAV 实测时长 → 时间不可信,降级。"""
    text = "你好世界"
    events = [{"i": 0, "c": 2, "t": 0.0}, {"i": 2, "c": 2, "t": 6.0}]
    timings, scaled = _events_to_timings(text, events, duration=1.6)
    assert timings == [] and scaled is False


def test_events_to_timings_insufficient_coverage_falls_back():
    text = "你好世界,这是一段很长的中文内容用来拉低覆盖率。"
    events = [{"i": 0, "c": 2, "t": 0.0}]  # 覆盖率远低于 50%
    timings, scaled = _events_to_timings(text, events, duration=5.0)
    assert timings == [] and scaled is False


def test_events_to_timings_empty():
    assert _events_to_timings("你好", [], 1.0) == ([], False)


def test_estimate_scaled_axis():
    text = "你好世界"
    est = _estimate_scaled(text, duration=4.0)
    assert est and abs(est[-1].end - 4.0) < 0.01
    assert len(est) == 4  # 每字一段
    assert [w.text for w in est] == ["你", "好", "世", "界"]


def test_wav_header_footer_bytes():
    """偶数 data_size 下头部总长 44,便于解析。"""
    import io
    import struct
    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + 16000))
    buf.write(b"WAVEfmt ")
    buf.write(struct.pack("<IHHIIHH", 16, 1, 1, 8000, 8000, 1, 8))
    buf.write(b"data")
    buf.write(struct.pack("<I", 16000))
    head = buf.getvalue()
    assert len(head) == 44 and head[:4] == b"RIFF"