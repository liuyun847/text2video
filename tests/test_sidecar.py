"""sidecar 模块单测:时间格式、断行、cue 构建、SRT/VTT 渲染与落盘(不跑 ffmpeg)。"""

import pytest

from text2video.sidecar import (
    DEFAULT_FORMATS,
    SUPPORTED_FORMATS,
    Cue,
    build_cues,
    clamp_cue,
    cue_lines,
    format_time,
    parse_formats,
    render_srt,
    render_vtt,
    resolve_max_chars,
    srt_time,
    vtt_time,
    write_sidecars,
)
from text2video.subs import WordTiming, wrap_plain_text
from text2video.timeline import Timeline, build_timeline

META = {"width": 1080, "height": 1080, "font_size": 54, "max_chars": 6}


def _timings(tokens: str = "你好世界你好世界。", step: float = 0.2) -> list[WordTiming]:
    """按固定步长造一条逐字轴(时间与下标线性相关,便于断言)。"""
    return [WordTiming(ch, i * step, (i + 1) * step) for i, ch in enumerate(tokens)]


def _timeline(text: str = "你好世界你好世界。", *, duration: float = 2.0,
              meta: dict | None = None, timings: list[WordTiming] | None = None):
    """造一份带逐字轴的 v5 时间线(produce 之后的真实轴形态)。"""
    return build_timeline(
        text, timings=timings if timings is not None else _timings(),
        meta=dict(META if meta is None else meta), duration=duration,
    )


# ---------------------------------------------------------------------------
# 时间格式
# ---------------------------------------------------------------------------


def test_format_time_zero_and_basic():
    assert format_time(0) == "00:00:00,000"
    assert format_time(1.5) == "00:00:01,500"
    assert format_time(61.25) == "00:01:01,250"
    assert format_time(3661.234) == "01:01:01,234"


def test_format_time_negative_clamped_and_rounding():
    assert format_time(-3.2) == "00:00:00,000"
    # 毫秒四舍五入(2.9996 -> 3.000),不留 999ms 的抖动
    assert format_time(2.9996) == "00:00:03,000"


def test_srt_vtt_time_separators():
    assert srt_time(1.5) == "00:00:01,500"
    assert vtt_time(1.5) == "00:00:01.500"
    assert vtt_time(3661.234) == "01:01:01.234"
    # 小时位固定两位,超长仍原样展开
    assert srt_time(360000) == "100:00:00,000"


# ---------------------------------------------------------------------------
# 格式参数
# ---------------------------------------------------------------------------


def test_parse_formats_defaults_and_order():
    assert DEFAULT_FORMATS == ("srt", "vtt")
    assert SUPPORTED_FORMATS == ("srt", "vtt")
    assert parse_formats(None) == DEFAULT_FORMATS
    assert parse_formats("srt,vtt") == ("srt", "vtt")
    # 大小写/空白/重复都容忍,顺序保留
    assert parse_formats(" VTT , srt ,vtt") == ("vtt", "srt")
    assert parse_formats(["srt"]) == ("srt",)


def test_parse_formats_rejects_unknown_and_empty():
    with pytest.raises(ValueError, match="不支持的字幕格式"):
        parse_formats("ass")
    with pytest.raises(ValueError, match="字幕格式为空"):
        parse_formats(" , ")


# ---------------------------------------------------------------------------
# 断行
# ---------------------------------------------------------------------------


def test_cue_lines_uses_ass_wrapping_with_words():
    """有逐字轴:沿用 ASS 的 wrap_words(6 个中文/行 -> 8 字句拆两行)。"""
    words = [{"text": ch, "start": 0.0, "end": 0.1} for ch in "你好世界你好世界"]
    lines = cue_lines("你好世界你好世界", words, 6)
    assert lines == ("你好世界你好", "世界")


def test_cue_lines_falls_back_to_plain_wrap_without_words():
    """无逐字轴(旧档):按显示宽度贪心断行。"""
    lines = cue_lines("这是很长的一句话", None, 4)
    assert lines == tuple(wrap_plain_text("这是很长的一句话", 4))
    assert lines == ("这是很长", "的一句话")


def test_cue_lines_keeps_ascii_word_spacing():
    words = [{"text": "Hello", "start": 0.0, "end": 0.3},
             {"text": "world", "start": 0.3, "end": 0.6}]
    assert cue_lines("Hello world", words, 18) == ("Hello world",)


def test_cue_lines_empty_text():
    assert cue_lines("", None, 6) == ()
    assert cue_lines("", [], 6) == ()


# ---------------------------------------------------------------------------
# 时间钳制
# ---------------------------------------------------------------------------


def test_clamp_cue_inside_range_untouched():
    assert clamp_cue(1.0, 2.0, 10.0) == (1.0, 2.0)


def test_clamp_cue_negative_and_reversed():
    assert clamp_cue(-1.0, 0.5, 10.0) == (0.0, 0.5)
    # 倒挂区间补到最小时长(不产出零时长 cue)
    start, end = clamp_cue(1.0, 0.9, 10.0)
    assert start == 1.0 and end - start == pytest.approx(0.05)


def test_clamp_cue_capped_by_duration():
    start, end = clamp_cue(9.98, 10.0, 10.0)
    assert end == 10.0 and start <= 10.0 and end - start >= 0.05
    start, end = clamp_cue(5.0, 5.0, None)
    assert start == 5.0 and end == pytest.approx(5.05)


# ---------------------------------------------------------------------------
# cue 构建
# ---------------------------------------------------------------------------


def test_build_cues_one_cue_per_sentence():
    tl = build_timeline(
        "第一句。第二句。",
        timings=[WordTiming(ch, i * 0.2, (i + 1) * 0.2) for i, ch in enumerate("第一句。第二句。")],
        meta=dict(META), duration=1.8,
    )
    cues = build_cues(tl)
    assert [c.index for c in cues] == [1, 2]
    assert [c.lines for c in cues] == [("第一句。",), ("第二句。",)]
    assert cues[0].start == 0.0 and cues[0].end == pytest.approx(0.8)
    assert cues[1].start == pytest.approx(0.8)


def test_build_cues_respects_meta_max_chars():
    tl = _timeline()          # meta.max_chars = 6,句有 9 个 token
    cues = build_cues(tl)
    assert len(cues) == 1
    assert cues[0].lines == ("你好世界你好", "世界。")


def test_build_cues_max_chars_override():
    tl = _timeline()
    cues = build_cues(tl, max_chars=2)
    assert all(len(line) <= 2 for line in cues[0].lines)
    assert len(cues[0].lines) > 1


def test_build_cues_empty_timeline():
    assert build_cues(Timeline(meta=dict(META), duration=1.0)) == []


def test_resolve_max_chars_falls_back_to_auto():
    tl = Timeline(meta={"width": 1080, "height": 1080}, duration=1.0)
    # 1080 宽 + 自动字号 54 -> 每行 18 个中文
    assert resolve_max_chars(tl) == 18
    tl.meta["max_chars"] = 7
    assert resolve_max_chars(tl) == 7


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def test_render_srt_exact_layout():
    cues = [Cue(1, 0.0, 1.5, ("第一行", "第二行")), Cue(2, 2.0, 3.25, ("第二句",))]
    assert render_srt(cues) == (
        "1\n"
        "00:00:00,000 --> 00:00:01,500\n"
        "第一行\n"
        "第二行\n"
        "\n"
        "2\n"
        "00:00:02,000 --> 00:00:03,250\n"
        "第二句\n"
    )


def test_render_vtt_exact_layout():
    cues = [Cue(1, 0.0, 1.5, ("第一行", "第二行"))]
    assert render_vtt(cues) == (
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.500\n第一行\n第二行\n"
    )


def test_render_empty_lists():
    assert render_srt([]) == ""
    assert render_vtt([]) == "WEBVTT\n"


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------


def test_write_sidecars_writes_utf8_without_bom(tmp_path):
    tl = _timeline()
    base = str(tmp_path / "demo")
    written = write_sidecars(tl, base)
    assert [p.replace(str(tmp_path), "") for p in written] == [
        "\\demo.srt", "\\demo.vtt",
    ]
    srt_raw = (tmp_path / "demo.srt").read_bytes()
    vtt_raw = (tmp_path / "demo.vtt").read_bytes()
    assert not srt_raw.startswith(b"\xef\xbb\xbf")     # 无 BOM
    assert not vtt_raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in srt_raw                       # LF 换行
    srt = srt_raw.decode("utf-8")
    vtt = vtt_raw.decode("utf-8")
    assert srt.startswith("1\n00:00:00,000 --> ")
    assert vtt.startswith("WEBVTT\n\n")
    assert "你好世界你好" in srt and "你好世界你好" in vtt


def test_write_sidecars_creates_missing_dir(tmp_path):
    tl = _timeline()
    base = str(tmp_path / "deep" / "nested" / "demo")
    write_sidecars(tl, base)
    assert (tmp_path / "deep" / "nested" / "demo.srt").is_file()


def test_write_sidecars_only_requested_format(tmp_path):
    tl = _timeline()
    base = str(tmp_path / "demo")
    write_sidecars(tl, base, ("vtt",))
    assert (tmp_path / "demo.vtt").is_file()
    assert not (tmp_path / "demo.srt").exists()


def test_write_sidecars_disabled_writes_nothing(tmp_path):
    tl = _timeline()
    base = str(tmp_path / "demo")
    assert write_sidecars(tl, base, ()) == []
    assert list(tmp_path.iterdir()) == []


def test_write_sidecars_rejects_unknown_format(tmp_path):
    tl = _timeline()
    with pytest.raises(ValueError, match="不支持的字幕格式"):
        write_sidecars(tl, str(tmp_path / "demo"), ("srt", "ass"))


def test_write_sidecars_requires_speech(tmp_path):
    tl = Timeline(meta=dict(META), duration=1.0)
    tl.tracks["speech"] = []
    with pytest.raises(ValueError, match="没有语音段"):
        write_sidecars(tl, str(tmp_path / "demo"))


# ---------------------------------------------------------------------------
# cli 接线(produce 的开关)
# ---------------------------------------------------------------------------


def test_produce_cli_exposes_sidecar_flags():
    from text2video.cli import build_parser

    ns = build_parser().parse_args(["produce", "--timeline", "x.timeline.json"])
    assert ns.subs_formats == "srt,vtt"      # 默认两种都出
    assert ns.no_sidecar_subs is False
    ns = build_parser().parse_args(["produce", "--text", "你好", "--no-sidecar-subs"])
    assert ns.no_sidecar_subs is True


def test_cli_sidecar_formats_helper():
    from text2video.cli import _sidecar_formats, build_parser

    ns = build_parser().parse_args(["produce", "--text", "x"])
    assert _sidecar_formats(ns) == ("srt", "vtt")
    ns = build_parser().parse_args(["produce", "--text", "x", "--subs-formats", "vtt"])
    assert _sidecar_formats(ns) == ("vtt",)
    ns = build_parser().parse_args(["produce", "--text", "x", "--no-sidecar-subs"])
    assert _sidecar_formats(ns) == ()        # 空元组 = produce 侧不写文件
    ns = build_parser().parse_args(
        ["produce", "--text", "x", "--subs-formats", "ass"])
    with pytest.raises(SystemExit, match="不支持的字幕格式"):
        _sidecar_formats(ns)


def test_sidecar_times_align_with_timeline_duration(tmp_path):
    """cue 末端不超过 timeline 总时长(尾句被钳进片尾)。"""
    tl = _timeline(duration=1.5)
    cues = build_cues(tl)
    assert cues[-1].end <= 1.5 + 1e-9
    write_sidecars(tl, str(tmp_path / "demo"), ("srt",))
    text = (tmp_path / "demo.srt").read_text(encoding="utf-8")
    assert "00:00:01,500" in text      # 片尾时间戳确实出现且未越界
