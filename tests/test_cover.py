"""cover 模块单测:封面 ASS 内容、渲染命令形态(不真跑 ffmpeg)。"""

import re

from text2video import cover as cover_mod
from text2video.cli import run_cover
from text2video.cover import build_cover_ass


def _events(ass: str) -> list[str]:
    return [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]


def test_build_cover_ass_title_and_subtitle():
    ass = build_cover_ass("这是一个标题", width=1920, height=1080,
                          font="Microsoft YaHei", subtitle="副标题")
    evs = _events(ass)
    assert len(evs) == 2
    # 标题:短边 1080 // 6 = 180 号字(样式表),pos y = 1080*0.42 = 453
    assert "\\b1\\pos(960,453)}这是一个标题" in evs[0]
    # 副标题 y = 453 + 180*1.5 = 723,1080//10=108
    assert "\\fs108\\pos(960,723)}副标题" in evs[1]
    assert "Format: Layer, Start, End, Style" in ass


def test_build_cover_ass_fontsize_defaults():
    ass = build_cover_ass("标题", width=1080, height=1080)
    # 短边 1080 // 6 = 180
    assert re.search(r"Style: Base,.*?,180,", ass)


def test_cover_ass_no_subtitle_single_event():
    ass = build_cover_ass("只有标题", width=1920, height=1080)
    assert len(_events(ass)) == 1


def test_run_cover_writes_png(tmp_path, monkeypatch):
    """cover 子命令流程:mock ffmpeg 执行,断言产物路径与命令形态。"""
    seen = {}

    def fake_find_ffmpeg():
        return "ffmpeg"

    def fake_run(cmd, cwd):
        seen["cmd"] = cmd
        seen["cwd"] = cwd
        # 模拟 ffmpeg 写出 png
        out = cmd[-1]
        with open(out, "wb") as f:
            f.write(b"PNGFAKE")

    monkeypatch.setattr(cover_mod.compose, "find_ffmpeg", fake_find_ffmpeg)
    monkeypatch.setattr(cover_mod.compose, "run_ffmpeg", fake_run)

    out = tmp_path / "cover.png"
    from text2video.cli import build_parser
    args = build_parser().parse_args(
        ["cover", "--text", "这是我的第一条视频文案。", "--out", str(out),
         "--title", "测试标题", "--subtitle", "副标题"])
    assert args.command == "cover"
    rc = run_cover(args)
    assert rc == 0
    assert out.exists() and out.read_bytes() == b"PNGFAKE"
    # 命令含单帧输出与字幕滤镜
    assert "-frames:v" in seen["cmd"] and "1" in seen["cmd"]
    assert "subtitles=" in " ".join(seen["cmd"])


def test_run_cover_defaults_title_from_first_sentence(tmp_path, monkeypatch):
    """未给 --title 时取文案首句前 20 字。"""
    seen = {}

    monkeypatch.setattr(cover_mod.compose, "find_ffmpeg", lambda: "ffmpeg")

    def fake_build(title, **kw):
        seen["title"] = title
        return "fake ass"

    def fake_run(cmd, cwd):
        out = cmd[-1]
        with open(out, "wb") as f:
            f.write(b"PNG")

    monkeypatch.setattr(cover_mod, "build_cover_ass", fake_build)
    monkeypatch.setattr(cover_mod.compose, "run_ffmpeg", fake_run)
    out = tmp_path / "auto.png"
    from text2video.cli import build_parser
    args = build_parser().parse_args(
        ["cover", "--text", "第一句是标题内容,用于封面。第二句。", "--out", str(out)])
    run_cover(args)
    assert "第一句是标题内容" in seen["title"]