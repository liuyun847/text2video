"""响度模块(audio)单测:loudnorm/ebur128 解析、目标校验、原地母带(不跑真 ffmpeg)。"""

import pytest

from text2video import audio

_LOUDNORM_STDERR = """
[Parsed_loudnorm_0 @ 000001] 
{
    "input_i" : "-20.83",
    "input_tp" : "-13.30",
    "input_lra" : "1.60",
    "input_thresh" : "-30.83",
    "output_i" : "-14.64",
    "output_tp" : "-7.37",
    "output_lra" : "1.20",
    "output_thresh" : "-24.64",
    "normalization_type" : "dynamic",
    "target_offset" : "0.64"
}
[out#0/null @ 000001] video:0KiB audio:969KiB
"""

_EBUR128_STDERR = """
[Parsed_ebur128_0 @ 1] t: 5.0  M: -15.6 S: -14.6 I: -13.9 LUFS LRA: 1.1 LU
[Parsed_ebur128_0 @ 1] Summary:

  Integrated loudness:
    I:         -14.0 LUFS
    Threshold: -24.0 LUFS

  Loudness range:
    LRA:         1.1 LU
    Threshold: -33.8 LUFS

  True peak:
    Peak:       -6.7 dBFS
"""


def test_parse_loudnorm_stats_reads_measured_values():
    stats = audio.parse_loudnorm_stats(_LOUDNORM_STDERR)
    assert stats["input_i"] == "-20.83"
    assert stats["input_tp"] == "-13.30"
    assert stats["target_offset"] == "0.64"


def test_parse_loudnorm_stats_missing_json_raises():
    with pytest.raises(audio.AudioError):
        audio.parse_loudnorm_stats("no json here")


def test_parse_ebur128_takes_summary_values():
    values = audio.parse_ebur128(_EBUR128_STDERR)
    assert values["i"] == pytest.approx(-14.0)
    assert values["lra"] == pytest.approx(1.1)
    assert values["tp"] == pytest.approx(-6.7)


def test_parse_ebur128_missing_raises():
    with pytest.raises(audio.AudioError):
        audio.parse_ebur128("nothing")


def test_validate_target_accepts_streaming_defaults():
    assert audio.validate_target(-14, -1.5) == (-14.0, -1.5)
    # 边界值也算合法(loudnorm 的选项区间是闭区间)
    assert audio.validate_target(-70, -9) == (-70.0, -9.0)


def test_validate_target_rejects_out_of_range():
    with pytest.raises(audio.AudioError, match="目标响度"):
        audio.validate_target(-80, -1.5)
    with pytest.raises(audio.AudioError, match="真峰值"):
        audio.validate_target(-14, -20)


def _fake_info():
    return {
        "target_lufs": -14.0,
        "true_peak": -1.5,
        "passes": 2,
        "mode": "linear",
        "before": {"i": -19.8, "tp": -3.2, "lra": 2.0},
        "after": {"i": -14.1, "lra": 2.0, "tp": -2.0},
    }


def test_apply_loudnorm_inplace_replaces_audio_and_removes_tmp(tmp_path, monkeypatch):
    target = tmp_path / "x.audio.mp3"
    target.write_bytes(b"raw")

    def fake(ffmpeg, src, out, *, target_lufs, true_peak, cwd):
        # 模拟两遍 loudnorm:把"成品"写进 out,原文件此时仍未被覆盖
        with open(out, "wb") as f:
            f.write(b"mastered")
        assert src.endswith("x.audio.mp3")
        assert (target_lufs, true_peak) == (-14.0, -1.5)
        return _fake_info(), []

    monkeypatch.setattr(audio, "apply_loudnorm", fake)
    info, warnings = audio.apply_loudnorm_inplace("ffmpeg", str(target))
    assert warnings == [] and info["after"]["i"] == -14.1
    assert target.read_bytes() == b"mastered"          # 原文件被成品替换
    assert not (tmp_path / "x.audio.loudnorm.tmp.mp3").exists()   # 临时文件已清理


def test_apply_loudnorm_inplace_keeps_original_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "x.audio.mp3"
    target.write_bytes(b"raw")

    def boom(*args, **kwargs):
        raise audio.AudioError("测量失败")

    monkeypatch.setattr(audio, "apply_loudnorm", boom)
    with pytest.raises(audio.AudioError, match="测量失败"):
        audio.apply_loudnorm_inplace("ffmpeg", str(target))
    assert target.read_bytes() == b"raw"
    assert not (tmp_path / "x.audio.loudnorm.tmp.mp3").exists()


def test_apply_loudnorm_inplace_rejects_bad_target(tmp_path):
    target = tmp_path / "x.audio.mp3"
    target.write_bytes(b"raw")
    with pytest.raises(audio.AudioError):
        audio.apply_loudnorm_inplace("ffmpeg", str(target), target_lufs=-99)


def test_summary_line_mentions_before_after_and_target():
    line = audio.summary_line(_fake_info())
    assert "-19.8 → -14.1 LUFS" in line
    assert "目标 -14" in line
    assert "2 遍 loudnorm/linear" in line
