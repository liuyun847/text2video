"""cli 模块的默认值/取整逻辑/子命令参数/流程冒烟单测(不联网,仅解析参数与纯逻辑)。"""

import pytest

from text2video.cli import (
    _auto_fontsize,
    _auto_max_chars,
    build_parser,
    collect_meta,
    resolve_canvas,
    run_plan,
)
from text2video.render import render_from_timeline
from text2video.timeline import Timeline, TrackSegment


def test_parser_defaults():
    ns = build_parser().parse_args(["--text", "你好"])
    # 画幅/帧率默认(本轮调整:1:1 方画幅 + 60fps;width/height 0=按预设解析)
    assert ns.width == 0
    assert ns.height == 0
    assert ns.fps == 60
    assert ns.aspect is None
    # 字幕/合成默认
    assert ns.style == "karaoke"
    assert ns.out == "output.mp4"
    assert ns.bg_color == "1B1B2A"
    assert ns.crf == 23
    assert ns.preset == "medium"
    # 新增默认:转场关、TTS auto、分离关
    assert ns.transition == 0.0
    assert ns.tts_backend == "auto"
    assert ns.split_tracks is False


def test_aspect_presets():
    for flag, (w, h) in (("--landscape", (1920, 1080)),
                         ("--portrait", (1080, 1920)),
                         ("--square", (1080, 1080))):
        ns = build_parser().parse_args(["--text", "你好", flag])
        assert resolve_canvas(ns.width, ns.height, ns.aspect) == (w, h)
    # 显式宽高优先于预设
    ns = build_parser().parse_args(
        ["--text", "你好", "--landscape", "--width", "800"])
    assert resolve_canvas(ns.width, ns.height, ns.aspect) == (800, 1080)
    # 默认方画幅
    ns = build_parser().parse_args(["--text", "你好"])
    assert resolve_canvas(ns.width, ns.height, ns.aspect) == (1080, 1080)


def test_parser_subcommands_plan_and_render():
    p = build_parser()
    plan = p.parse_args(["plan", "--file", "a.txt", "--out", "out/x"])
    assert plan.command == "plan"
    assert plan.file == "a.txt"
    assert plan.out == "out/x"
    render = p.parse_args(["render", "--timeline", "out/x.timeline.json"])
    assert render.command == "render"
    assert render.timeline == "out/x.timeline.json"
    assert render.out is None  # render 默认由 timeline 基名推导


def test_collect_meta_derives_fontsize_maxchars_family():
    ns = build_parser().parse_args(
        ["--text", "你好", "--width", "1080", "--height", "1920"]
    )
    meta = collect_meta(ns)
    assert meta["width"] == 1080 and meta["height"] == 1920
    assert meta["font_size"] == _auto_fontsize(1080, 1920)  # 短边 1080 → 54
    assert meta["max_chars"] == _auto_max_chars(1080, 54)
    assert meta["style"] == "karaoke"
    assert meta["bg_image"] is None
    # 未指定 --font 时应解析出可用的系统字体家族名,而非 None(null)
    assert meta["font"] not in (None, "")


def test_run_plan_writes_three_artifacts(tmp_path):
    out = tmp_path / "demo"
    ns = build_parser().parse_args(
        ["plan", "--text", "你好,世界。第一句。第二句话。", "--out", str(out)]
    )
    run_plan(ns)
    json_p, md_p, html_p = (
        tmp_path / "demo.timeline.json",
        tmp_path / "demo.timeline.md",
        tmp_path / "demo.preview.html",
    )
    assert json_p.exists() and md_p.exists() and html_p.exists()
    tl = Timeline.from_json(str(json_p))
    assert tl.checks == [] and tl.meta["timing_mode"] == "estimate"
    assert set(tl.tracks) == {"speech", "subtitle", "visual", "material"}
    assert tl.assets == [] and tl.bgm is None


def test_parser_materials_option():
    ns = build_parser().parse_args(
        ["--text", "你好", "--materials", "m.json"]
    )
    assert ns.materials == "m.json"


def test_run_plan_with_materials(tmp_path):
    # 素材文件只需存在(plan 阶段不探测内容);写入假文件 + 清单 json
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake")
    img = tmp_path / "bg.png"
    img.write_bytes(b"fake")
    bgm = tmp_path / "bgm.mp3"
    bgm.write_bytes(b"fake")
    mjson = tmp_path / "m.json"
    mjson.write_text(
        '{"assets": ['
        '{"id": "a1", "path": "' + str(clip).replace("\\", "/") + '", "type": "video"},'
        '{"id": "a2", "path": "' + str(img).replace("\\", "/") + '", "type": "image"},'
        '{"id": "a3", "path": "' + str(bgm).replace("\\", "/") + '", "type": "audio"}],'
        '"material": [{"start": 0.0, "end": 1.5, "asset": "a1", "in": 1.0, "out": 2.5},'
        '{"start": 1.5, "end": 2.0, "asset": "a2"}],'
        '"bgm": {"asset": "a3", "volume": 0.2}}',
        encoding="utf-8",
    )
    out = tmp_path / "demo"
    ns = build_parser().parse_args(
        ["plan", "--text", "你好。", "--out", str(out), "--materials", str(mjson)]
    )
    run_plan(ns)
    tl = Timeline.from_json(str(tmp_path / "demo.timeline.json"))
    assert tl.assets and tl.bgm == {"asset": "a3", "volume": 0.2}
    assert [s.text for s in tl.tracks["material"]] == ["a1", "a2"]
    assert tl.checks == []


def test_run_plan_with_missing_material_file_fails(tmp_path):
    mjson = tmp_path / "m.json"
    mjson.write_text(
        '{"assets": [{"id": "a", "path": "nope.mp4", "type": "video"}],'
        '"material": [{"start": 0, "end": 3, "asset": "a", "in": 0, "out": 3}]}',
        encoding="utf-8",
    )
    ns = build_parser().parse_args(
        ["plan", "--text", "你好。", "--out", str(tmp_path / "demo"),
         "--materials", str(mjson)]
    )
    with pytest.raises(SystemExit, match="素材文件缺失"):
        run_plan(ns)


def test_render_rejects_bad_timeline(tmp_path):
    # 手改出非法时间线(语音段 start>end),render 应自检拒绝,不进 TTS/ffmpeg
    tl = Timeline(text="你好。", meta={"width": 100, "height": 100})
    tl.tracks["visual"] = []
    tl.tracks["speech"] = [TrackSegment(2.0, 1.0, "你好。")]
    tl.tracks["subtitle"] = [TrackSegment(0.0, 1.0, "你好。")]
    tl.duration = 1.0
    bad = tmp_path / "bad.timeline.json"
    tl.to_json(str(bad))
    with pytest.raises(SystemExit) as exc:
        render_from_timeline(str(bad))
    assert exc.value.code == 2


def test_auto_fontsize_short_side_div20():
    # 默认字号 = 短边 // 20(向下取整),任意比例一致
    assert _auto_fontsize(1080, 1080) == 54
    assert _auto_fontsize(1080, 1920) == 54   # 竖屏仍按短边(宽)算
    assert _auto_fontsize(1280, 720) == 36    # 720 // 20
    assert _auto_fontsize(100, 100) == 16     # 下限保护


def test_auto_max_chars_consistent_with_fontsize():
    # 1080 宽 + 54px 字号 → 每行约 18 个中文
    assert _auto_max_chars(1080, _auto_fontsize(1080, 1080)) == 18
    # 至少 4 字下限
    assert _auto_max_chars(100, 200) == 4
