"""deliver 模块单测:参数解析 / 规格尺寸 / 命令拼装 / 日志解析 / 报告 / 主流程(mock ffmpeg)。

真 ffmpeg 一条都不跑:纯函数直接断言,主流程用假 runner 顶掉 `deliver.run_capture`,
只验证"调度 + 结果 + 报告"的逻辑,交付冒烟由人工在真实成片上跑一遍(见 README)。
"""

import os

import pytest

from text2video import deliver as dv
from text2video.deliver import (
    DEFAULT_SPECS,
    FIT_MODES,
    REPORT_NAME,
    SPEC_SIZES,
    SUPPORTED_SPECS,
    CoverOutput,
    DeliverResult,
    Loudness,
    MediaInfo,
    SpecOutput,
    SyncRow,
    align_onsets,
    build_cover_cmd,
    build_fit_cmd,
    build_loudness_cmd,
    build_remux_cmd,
    build_silencedetect_cmd,
    deliver,
    fit_filter,
    format_report,
    human_size,
    loudness_verdict,
    normalize_size,
    parse_cover_at,
    parse_fit,
    parse_loudness,
    parse_silences,
    parse_specs,
    resolve_cover_time,
    resolve_spec_size,
    speech_onsets,
    sync_conclusion,
    sync_mean,
    sync_rows,
)
from text2video.timeline import Timeline, TrackSegment

# ---- 日志样本(与 ffmpeg 9.0 实际输出同构) ----
LOUD_LOG = """\
[Parsed_ebur128_0 @ 000001] t: 0.1   M: -120.7 S:-120.7  I: -120.7 LUFS  LRA:   0.0 LU
[Parsed_ebur128_0 @ 000001] t: 24.0  M:  -16.9 S: -18.3  I:  -17.1 LUFS  LRA:   5.6 LU
[Parsed_ebur128_0 @ 000001] Summary:

  Integrated loudness:
    I:         -17.2 LUFS
    Threshold: -27.4 LUFS

  Loudness range:
    LRA:         5.6 LU
    Threshold: -37.4 LUFS
    LRA low:   -22.1 LUFS
    LRA high:  -16.5 LUFS

  True peak:
    Peak:       -1.8 dBFS
"""
SIL_LOG = """\
[silencedetect @ 000001] silence_start: 0
[silencedetect @ 000001] silence_end: 0.62 | silence_duration: 0.62
[silencedetect @ 000001] silence_start: 2.0
[silencedetect @ 000001] silence_end: 3.05 | silence_duration: 1.05
[silencedetect @ 000001] silence_start: 23.9
"""


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def test_parse_specs_defaults_and_order():
    assert SUPPORTED_SPECS == ("landscape", "portrait", "square")
    assert DEFAULT_SPECS == SUPPORTED_SPECS
    assert parse_specs(None) == DEFAULT_SPECS
    assert parse_specs("portrait,square") == ("portrait", "square")
    # 大小写/空白/重复容忍,顺序保留
    assert parse_specs(" SQUARE , landscape ,square") == ("square", "landscape")
    assert parse_specs(["landscape"]) == ("landscape",)


def test_parse_specs_rejects_unknown_and_empty():
    with pytest.raises(ValueError, match="不支持的规格"):
        parse_specs("landscape,vertical")
    with pytest.raises(ValueError, match="规格为空"):
        parse_specs(" , ")


def test_parse_fit():
    assert FIT_MODES == ("blur", "crop")
    assert parse_fit(None) == "blur"
    assert parse_fit("crop") == "crop"
    assert parse_fit(" BLUR ") == "blur"
    with pytest.raises(ValueError, match="不支持的适配方式"):
        parse_fit("stretch")


def test_parse_cover_at_values():
    assert parse_cover_at(None) == (1.0,)
    assert parse_cover_at("1.0,0.3") == (1.0, 0.3)
    assert parse_cover_at(" 1.0 , 0.3 ,1.0") == (1.0, 0.3)
    assert parse_cover_at([2.5, 0.5]) == (2.5, 0.5)
    assert parse_cover_at(3) == (3.0,)


def test_parse_cover_at_rejects_bad_values():
    with pytest.raises(ValueError, match="不是数字"):
        parse_cover_at("abc")
    with pytest.raises(ValueError, match="不能为负"):
        parse_cover_at("-1")
    with pytest.raises(ValueError, match="为空"):
        parse_cover_at(" , ")


def test_resolve_cover_time_ratio_vs_seconds():
    # >= 1 视为秒(所以默认的 1.0 是"片头 1 秒",不是 100% 处)
    assert resolve_cover_time(1.0, 20.0) == 1.0
    assert resolve_cover_time(5.0, 20.0) == 5.0
    # 0~1 视为全片比例
    assert resolve_cover_time(0.3, 20.0) == 6.0
    assert resolve_cover_time(0.5, 24.0) == 12.0
    # 越过片尾时钳到片尾前(保证抽得到帧)
    assert resolve_cover_time(100.0, 20.0) == pytest.approx(19.95)
    assert resolve_cover_time(0.0, 20.0) == 0.0


# ---------------------------------------------------------------------------
# 规格尺寸
# ---------------------------------------------------------------------------


def test_spec_sizes_fixed():
    assert SPEC_SIZES["portrait"] == (1080, 1920)
    assert SPEC_SIZES["square"] == (1080, 1080)
    assert resolve_spec_size("portrait", (1920, 1080)) == (1080, 1920)
    assert resolve_spec_size("square", (1920, 1080)) == (1080, 1080)


def test_spec_size_landscape_falls_back_to_source():
    """landscape 没给 timeline 时"原样":直接取源分辨率(于是走流复制)。"""
    assert resolve_spec_size("landscape", (1920, 1080)) == (1920, 1080)
    assert resolve_spec_size("landscape", (1080, 1080)) == (1080, 1080)


def test_spec_size_landscape_follows_timeline_meta():
    meta = {"width": 1920, "height": 1080}
    assert resolve_spec_size("landscape", (1080, 1080), meta) == (1920, 1080)
    # meta 缺字段/非法 -> 回退源分辨率
    assert resolve_spec_size("landscape", (1080, 1080), {}) == (1080, 1080)
    assert resolve_spec_size("landscape", (1080, 1080), {"width": "x"}) == (1080, 1080)


def test_normalize_size_makes_even():
    assert normalize_size(1081, 1921) == (1082, 1922)
    assert normalize_size(1080, 1920) == (1080, 1920)
    assert normalize_size(0, 0) == (16, 16)


# ---------------------------------------------------------------------------
# 命令拼装
# ---------------------------------------------------------------------------


def test_fit_filter_blur_has_background_overlay():
    chain = fit_filter(1080, 1920, "blur")
    assert "gblur=sigma=" in chain
    assert "force_original_aspect_ratio=increase" in chain   # 背景铺满
    assert "force_original_aspect_ratio=decrease" in chain   # 前景 contain
    assert "overlay=(W-w)/2:(H-h)/2" in chain                # 居中
    assert chain.endswith("[v]")


def test_fit_filter_crop_centered():
    chain = fit_filter(1080, 1080, "crop")
    assert "crop=1080:1080" in chain
    assert "gblur" not in chain and "overlay" not in chain


def test_fit_filter_rejects_unknown_mode():
    with pytest.raises(ValueError):
        fit_filter(1080, 1080, "stretch")


def test_build_remux_cmd_is_stream_copy():
    cmd = build_remux_cmd("ffmpeg", "in.mp4", "out.mp4")
    assert cmd[0] == "ffmpeg" and cmd[-1] == "out.mp4"
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert "libx264" not in cmd


def test_build_fit_cmd_maps_filter_output():
    cmd = build_fit_cmd("ffmpeg", "in.mp4", "out.mp4", 1080, 1920, "blur", crf=18)
    assert cmd[cmd.index("-filter_complex") + 1] == fit_filter(1080, 1920, "blur")
    assert cmd[cmd.index("-map") + 1] == "[v]"
    assert "0:a?" in cmd                       # 无音轨也不报错
    assert cmd[cmd.index("-crf") + 1] == "18"
    assert cmd[-1] == "out.mp4"


def test_build_cover_cmd_seeks_before_input():
    cmd = build_cover_cmd("ffmpeg", "in.mp4", "cover.png", 1.5)
    assert cmd.index("-ss") < cmd.index("-i")   # 先定位再解码
    assert cmd[cmd.index("-ss") + 1] == "1.500"
    assert cmd[-1] == "cover.png"


def test_build_loudness_and_silencedetect_cmd():
    loud = build_loudness_cmd("ffmpeg", "in.mp4")
    assert "ebur128=peak=true" in loud
    # -f null <devnull>:跑滤镜但不落文件
    assert loud[loud.index("-f") + 1] == "null"
    assert loud[-1] == os.devnull
    sil = build_silencedetect_cmd("ffmpeg", "in.mp4", noise_db=-30, min_silence=0.2)
    assert "silencedetect=noise=-30dB:d=0.2" in sil


# ---------------------------------------------------------------------------
# 日志解析
# ---------------------------------------------------------------------------


def test_parse_loudness_takes_summary_not_progress():
    loud = parse_loudness(LOUD_LOG)
    assert loud.integrated == -17.2      # Summary 的值,不是进度行的 -120.7
    assert loud.true_peak == -1.8
    assert loud.lra == 5.6


def test_parse_loudness_missing_everything():
    loud = parse_loudness("")
    assert loud == Loudness(None, None, None)
    # -inf(全程静音)按"测不到"处理
    assert parse_loudness("I:           -inf LUFS").integrated is None


def test_parse_silences_pairs_start_end():
    pairs = parse_silences(SIL_LOG)
    assert pairs == [(0.0, 0.62), (2.0, 3.05), (23.9, None)]
    assert parse_silences("") == []


def test_speech_onsets_from_silences():
    onsets = speech_onsets(parse_silences(SIL_LOG), 24.1)
    assert onsets == [0.62, 3.05]
    # 开头不是静音 -> 0 秒本身算一个语音起点;静音结束后语音重新开始
    assert speech_onsets([(1.0, 2.0)], 10.0) == [0.0, 2.0]
    # 尾部静音(只有 silence_start)之后不再有起点
    assert speech_onsets([(0.0, 1.0), (9.0, None)], 10.0) == [1.0]
    # 整片静音 -> 一个起点都没有
    assert speech_onsets([(0.0, None)], 10.0) == []


def test_align_onsets_nearest_and_missing():
    assert align_onsets([0.6, 3.0], [0.62, 3.05]) == [0.02, 0.05]
    assert align_onsets([1.0], []) == [None]
    # 允许复用同一个起点(体检式粗对齐,不做刚性时钟回归)
    assert align_onsets([1.0, 1.2], [1.1]) == [0.1, pytest.approx(-0.1)]


def test_sync_rows_and_conclusion_ok():
    speech = [TrackSegment(0.6, 2.0, "你好。"), TrackSegment(3.0, 5.0, "第二句。")]
    rows = sync_rows(speech, [0.62, 3.05])
    assert [(r.index, r.expected, r.actual) for r in rows] == [
        (1, 0.6, 0.62), (2, 3.0, 3.05),
    ]
    max_abs, bad, text = sync_conclusion(rows, 0.25)
    assert max_abs == pytest.approx(0.05) and bad == [] and text.startswith("✅")
    assert "平均 +0.035s" in text          # (0.02 + 0.05) / 2


def test_sync_rows_truncates_long_sentence():
    speech = [TrackSegment(0.0, 1.0, "这是一句非常非常非常长的句子,长到要截断显示。")]
    row = sync_rows(speech, [0.0], max_sentence_chars=10)[0]
    assert len(row.sentence) == 10 and row.sentence.endswith("…")


def test_sync_conclusion_flags_over_tolerance():
    rows = [SyncRow(1, "甲", 0.0, 0.5, 0.5), SyncRow(2, "乙", 1.0, 1.02, 0.02)]
    max_abs, bad, text = sync_conclusion(rows, 0.25)
    assert max_abs == 0.5 and len(bad) == 1 and "❌" in text
    # 平均偏移用于区分"系统性offset"与"个别错位"
    assert sync_mean(rows) == pytest.approx(0.26)


def test_sync_mean_handles_unmeasured():
    assert sync_mean([]) is None
    assert sync_mean([SyncRow(1, "甲", 0.0, None, None)]) is None
    assert sync_mean([SyncRow(1, "甲", 0.0, 0.1, 0.1)]) == pytest.approx(0.1)


def test_sync_conclusion_no_measurement():
    max_abs, bad, text = sync_conclusion([SyncRow(1, "甲", 0.0, None, None)], 0.25)
    assert max_abs is None and bad == [] and "没找到可比较的语音起点" in text


# ---------------------------------------------------------------------------
# 响度结论
# ---------------------------------------------------------------------------


def test_loudness_verdict_pass():
    ok, text = loudness_verdict(Loudness(-14.2, -1.8, 5.0), -14.0, -1.5)
    assert ok is True and text.startswith("达标") and "-14.2 LUFS" in text


def test_loudness_verdict_too_quiet_and_loud():
    ok, text = loudness_verdict(Loudness(-17.2, -3.0, 5.0), -14.0, -1.5)
    assert ok is False and "偏轻" in text and "3.2 LU" in text
    ok, text = loudness_verdict(Loudness(-10.5, -3.0, 5.0), -14.0, -1.5)
    assert ok is False and "偏响" in text


def test_loudness_verdict_true_peak_over():
    ok, text = loudness_verdict(Loudness(-14.0, -0.4, 5.0), -14.0, -1.5)
    assert ok is False and "真峰值" in text and "-0.4 dBTP" in text


def test_loudness_verdict_unknown():
    ok, text = loudness_verdict(None, -14.0, -1.5)
    assert ok is False and "未测到" in text
    ok, _ = loudness_verdict(Loudness(None, -2.0, None), -14.0, -1.5)
    assert ok is False


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def _result(tmp_path) -> DeliverResult:
    return DeliverResult(
        video=MediaInfo("C:\\v\\master.mp4", 1080, 1080, 60.0, 24.0, 2048),
        out_dir=str(tmp_path),
        specs=[
            SpecOutput("landscape", 1080, 1080, str(tmp_path / "master-landscape.mp4"),
                       2048, "copy"),
            SpecOutput("portrait", 1080, 1920, str(tmp_path / "master-portrait.mp4"),
                       4096, "blur"),
        ],
        covers=[CoverOutput(1.0, 1.0, str(tmp_path / "cover-01-1.00s.png"))],
        loudness=Loudness(-14.2, -1.8, 5.0),
        sync=[SyncRow(1, "你好。", 0.6, 0.62, 0.02)],
    )


def test_format_report_sections(tmp_path):
    report = format_report(_result(tmp_path), generated_at="2026-01-01 00:00:00")
    assert report.startswith("# 交付报告")
    for section in ("## 规格清单", "## 封面", "## 响度复核", "## 音画对位校验", "## 警告"):
        assert section in report
    assert "| landscape | 1080x1080 | 原样(流复制)" in report
    assert "| portrait | 1080x1920 | 模糊背景填充" in report
    assert "2.00 KB" in report and "4.00 KB" in report
    assert "2026-01-01 00:00:00" in report


def test_format_report_marks_skipped_checks(tmp_path):
    result = _result(tmp_path)
    result.loudness = None
    result.sync = []
    result.skipped = {"loudness": "成片没有音频流", "sync": "未提供 --timeline"}
    report = format_report(result)
    assert "跳过:成片没有音频流" in report
    assert "跳过:未提供 --timeline" in report


def test_format_report_lists_warnings(tmp_path):
    result = _result(tmp_path)
    result.warnings = ["对位偏差超阈值:❌ 1/1 句"]
    report = format_report(result)
    assert "## 警告" in report and "对位偏差超阈值" in report


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(2048) == "2.00 KB"
    assert human_size(1024 * 1024) == "1.00 MB"


# ---------------------------------------------------------------------------
# cli 接线
# ---------------------------------------------------------------------------


def test_deliver_cli_parser_defaults():
    from text2video.cli import build_parser

    ns = build_parser().parse_args(["deliver", "--video", "a.mp4"])
    assert ns.command == "deliver"
    assert ns.specs == "landscape,portrait,square"
    assert ns.cover_at == "1.0" and ns.fit == "blur"
    assert ns.out == "out/deliver"
    assert ns.lufs_target == -14.0 and ns.dbtp_target == -1.5
    assert ns.sync_tolerance == 0.25
    assert ns.timeline is None
    ns2 = build_parser().parse_args(
        ["deliver", "--video", "a.mp4", "--specs", "square",
         "--cover-at", "2.0", "--fit", "crop"])
    assert (ns2.specs, ns2.cover_at, ns2.fit) == ("square", "2.0", "crop")
    with pytest.raises(SystemExit):
        build_parser().parse_args(["deliver"])          # --video 必填


def test_run_deliver_turns_bad_specs_into_readable_exit(tmp_path):
    from text2video.cli import build_parser, run_deliver

    video = _make_video(tmp_path)
    ns = build_parser().parse_args(
        ["deliver", "--video", video, "--out", str(tmp_path / "d"),
         "--specs", "story"])
    with pytest.raises(SystemExit, match="不支持的规格"):
        run_deliver(ns)


# ---------------------------------------------------------------------------
# 主流程(mock 掉 ffmpeg)
# ---------------------------------------------------------------------------


class _Runner:
    """假 ffmpeg:滤镜类命令返回预设日志,产出类命令写占位文件。"""

    def __init__(self, *, loud_log: str = LOUD_LOG, sil_log: str = SIL_LOG,
                 fail_on: str | None = None):
        self.calls: list[tuple[str, list[str]]] = []
        self.loud_log = loud_log
        self.sil_log = sil_log
        self.fail_on = fail_on

    def __call__(self, cmd, *, what: str) -> str:
        self.calls.append((what, list(cmd)))
        joined = " ".join(cmd)
        if self.fail_on and self.fail_on in what:
            raise RuntimeError(f"{what}失败(exit=1)\n模拟 ffmpeg 失败")
        if "ebur128=peak=true" in joined:
            return self.loud_log
        if "silencedetect=" in joined:
            return self.sil_log
        with open(cmd[-1], "wb") as f:
            f.write(b"FAKE" * 128)
        return ""

    def whats(self) -> list[str]:
        return [w for w, _c in self.calls]


def _patch(monkeypatch, runner: _Runner, *, size=(1080, 1080), duration=24.0):
    monkeypatch.setattr(dv, "run_capture", runner)
    monkeypatch.setattr(dv.compose, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(dv.compose, "find_ffprobe", lambda _f: "ffprobe")
    monkeypatch.setattr(dv.compose, "probe_dimensions", lambda _p, _x: size)
    monkeypatch.setattr(dv.compose, "probe_duration", lambda _p, _x: duration)
    monkeypatch.setattr(dv, "probe_frame_rate", lambda _p, _x: 60.0)
    return runner


def _make_video(tmp_path, name: str = "master.mp4") -> str:
    path = tmp_path / name
    path.write_bytes(b"VIDEO" * 512)
    return str(path)


def test_deliver_writes_specs_covers_and_report(tmp_path, monkeypatch):
    runner = _patch(monkeypatch, _Runner())
    video = _make_video(tmp_path)
    out_dir = tmp_path / "deliver"
    assert deliver(video, out=str(out_dir), cover_at="1.0") == 0

    # 三条规格 + 一张封面 + 报告
    assert (out_dir / "master-landscape.mp4").is_file()
    assert (out_dir / "master-portrait.mp4").is_file()
    assert (out_dir / "master-square.mp4").is_file()
    assert (out_dir / "cover-01-1.00s.png").is_file()
    report = (out_dir / REPORT_NAME).read_text(encoding="utf-8")
    assert "| landscape | 1080x1080 | 原样(流复制)" in report
    assert "| portrait | 1080x1920 | 模糊背景填充" in report
    assert "integrated -17.2 LUFS" in report      # ebur128 解析到了
    assert "偏轻" in report                        # 相对 -14 LUFS 的结论

    # 尺寸与源一致的两条走流复制;portrait 走 blur 滤镜
    whats = runner.whats()
    assert "规格 landscape 流复制" in whats
    assert "规格 square 流复制" in whats
    portrait_cmd = next(c for w, c in runner.calls if "规格 portrait" in w)
    chain = portrait_cmd[portrait_cmd.index("-filter_complex") + 1]
    assert "gblur" in chain and "overlay=(W-w)/2:(H-h)/2" in chain


def test_deliver_remux_failure_falls_back_to_encode(tmp_path, monkeypatch):
    runner = _patch(monkeypatch, _Runner(fail_on="流复制"))
    video = _make_video(tmp_path)
    out_dir = tmp_path / "deliver"
    assert deliver(video, out=str(out_dir)) == 0
    # 流复制失败后回退重编码:文件仍在,报告里有警告
    assert (out_dir / "master-landscape.mp4").is_file()
    report = (out_dir / REPORT_NAME).read_text(encoding="utf-8")
    assert "回退重编码" in report
    # 回退时改用 fit 命令(libx264)
    fallback = [c for w, c in runner.calls if "规格 landscape(模糊背景填充)" in w]
    assert fallback and "libx264" in fallback[0]


def test_deliver_cover_at_multiple_and_ratio(tmp_path, monkeypatch):
    _patch(monkeypatch, _Runner())
    video = _make_video(tmp_path)
    out_dir = tmp_path / "deliver"
    deliver(video, out=str(out_dir), specs="square", cover_at="1.0,0.3")
    # 0.3 * 24s = 7.2s
    assert (out_dir / "cover-01-1.00s.png").is_file()
    assert (out_dir / "cover-02-7.20s.png").is_file()
    assert not (out_dir / "master-portrait.mp4").exists()   # --specs 生效


def test_deliver_without_timeline_skips_sync(tmp_path, monkeypatch):
    _patch(monkeypatch, _Runner())
    video = _make_video(tmp_path)
    out_dir = tmp_path / "deliver"
    deliver(video, out=str(out_dir), specs="square")
    report = (out_dir / REPORT_NAME).read_text(encoding="utf-8")
    assert "未提供 --timeline" in report


def test_deliver_with_timeline_checks_sync_and_size(tmp_path, monkeypatch):
    runner = _patch(monkeypatch, _Runner())
    video = _make_video(tmp_path)
    tl = Timeline(text="你好。第二句。", meta={"width": 1920, "height": 1080},
                  duration=24.0)
    tl.tracks["speech"] = [TrackSegment(0.6, 2.0, "你好。"),
                           TrackSegment(3.0, 5.0, "第二句。")]
    tl_path = tmp_path / "master.timeline.json"
    tl.to_json(str(tl_path))

    out_dir = tmp_path / "deliver"
    deliver(video, out=str(out_dir), specs="landscape", timeline=str(tl_path))
    # landscape 按 timeline 画幅(1920x1080 != 源 1080x1080)-> 走滤镜而不是流复制
    cmd = next(c for w, c in runner.calls if "规格 landscape" in w)
    assert "libx264" in cmd
    assert "-filter_complex" in cmd
    report = (out_dir / REPORT_NAME).read_text(encoding="utf-8")
    assert "timeline 语音段: 2 句" in report
    assert "平均偏移: +0.035s" in report
    assert "| 1 | 你好。 | 0.600s | 0.620s | +0.020s |" in report


def test_deliver_sync_over_tolerance_warns(tmp_path, monkeypatch):
    sil = ("[silencedetect @ 0] silence_start: 0\n"
           "[silencedetect @ 0] silence_end: 2.0 | silence_duration: 2.0\n")
    _patch(monkeypatch, _Runner(sil_log=sil))
    video = _make_video(tmp_path)
    tl = Timeline(text="你好。", meta={"width": 1080, "height": 1080}, duration=24.0)
    tl.tracks["speech"] = [TrackSegment(0.5, 2.0, "你好。")]
    tl_path = tmp_path / "master.timeline.json"
    tl.to_json(str(tl_path))
    out_dir = tmp_path / "deliver"
    deliver(video, out=str(out_dir), specs="square", timeline=str(tl_path))
    report = (out_dir / REPORT_NAME).read_text(encoding="utf-8")
    assert "对位校验:❌" in report and "+1.500s" in report


def test_deliver_loudness_failure_degrades_to_warning(tmp_path, monkeypatch):
    _patch(monkeypatch, _Runner(fail_on="响度复核"))
    video = _make_video(tmp_path)
    out_dir = tmp_path / "deliver"
    assert deliver(video, out=str(out_dir), specs="square") == 0
    report = (out_dir / REPORT_NAME).read_text(encoding="utf-8")
    assert "响度复核跳过" in report


def test_deliver_missing_video_raises(tmp_path, monkeypatch):
    _patch(monkeypatch, _Runner())
    with pytest.raises(RuntimeError, match="找不到成片"):
        deliver(str(tmp_path / "nope.mp4"), out=str(tmp_path / "d"))


def test_deliver_rejects_bad_specs_before_running(tmp_path, monkeypatch):
    runner = _patch(monkeypatch, _Runner())
    video = _make_video(tmp_path)
    with pytest.raises(ValueError, match="不支持的规格"):
        deliver(video, out=str(tmp_path / "d"), specs="story")
    assert runner.calls == []      # 参数错误不落到 ffmpeg
