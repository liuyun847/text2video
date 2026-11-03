"""compose 模块单测:透明字幕轨命令、素材裁切命令、音频混音/转码命令、探测。

画面整片合成(build_video_cmd)已随"画面交给 Remotion"删除,这里只锁中间件命令。
"""

import pytest

from text2video import compose
from text2video.compose import (
    build_material_cmd,
    build_subtitle_track_cmd,
    mix_bgm,
    transcode_audio,
)

# ---------------------------------------------------------------------------
# 透明字幕轨:ASS 烧到 alpha 画布 → VP9 / yuva420p
# ---------------------------------------------------------------------------


def _subs_cmd(**kw):
    base = {
        "ass": "demo.ass", "out": "demo.subs.webm",
        "width": 1080, "height": 1080, "fps": 60, "duration": 12.5,
    }
    base.update(kw)
    return build_subtitle_track_cmd("ffmpeg", **base)


def test_subtitle_track_canvas_and_alpha_chain():
    """透明底三步缺一不可:rgba 画布 + colorchannelmixer=aa=0 + 字幕滤镜 alpha=1。"""
    cmd = _subs_cmd()
    canvas = cmd[cmd.index("-i") + 1]
    assert canvas == "color=c=black:s=1080x1080:r=60:d=12.500"
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == ("format=rgba,colorchannelmixer=aa=0,"
                  "subtitles=demo.ass:alpha=1")
    assert cmd[0] == "ffmpeg" and "-y" in cmd


def test_subtitle_track_frame_count_locked():
    cmd = _subs_cmd()
    # 帧数锁死 = round(duration*fps),与 render.json 的 durationInFrames 对齐
    assert cmd[cmd.index("-frames:v") + 1] == "750"        # round(12.5 * 60)
    assert "-t" not in cmd


def test_subtitle_track_encodes_vp9_yuva420p():
    cmd = _subs_cmd(crf=28)
    assert cmd[cmd.index("-c:v") + 1] == "libvpx-vp9"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuva420p"
    # libvpx-vp9 保留 alpha 平面的必要条件
    assert cmd[cmd.index("-auto-alt-ref") + 1] == "0"
    assert cmd[cmd.index("-crf") + 1] == "28"
    assert cmd[cmd.index("-b:v") + 1] == "0"
    assert cmd[-1] == "demo.subs.webm"


def test_subtitle_track_fontsdir_relative():
    """字体目录用相对名(输出目录内),规避 Windows 盘符冒号在滤镜里的转义问题。"""
    cmd = _subs_cmd(fontsdir="demo.fonts")
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == ("format=rgba,colorchannelmixer=aa=0,"
                  "subtitles=demo.ass:fontsdir=demo.fonts:alpha=1")


# ---------------------------------------------------------------------------
# 素材中间件:裁到输出画幅,视频无音轨
# ---------------------------------------------------------------------------


def test_material_cmd_image_with_crop():
    cmd = build_material_cmd(
        "ffmpeg", src=r"C:\shots\a.png", out="m00_a.png", kind="image",
        width=1920, height=1080, fps=60,
        crop={"w": 1000, "h": 562, "x": 120, "y": 0},
    )
    vf = cmd[cmd.index("-vf") + 1]
    # 裁窗比例=画幅比例 → 直接 scale 到画幅(不变形、不二次裁切)
    assert vf == "crop=1000:562:120:0,scale=1920:1080,setsar=1"
    assert cmd[cmd.index("-frames:v") + 1] == "1"
    assert cmd[cmd.index("-update") + 1] == "1"
    assert cmd[-1] == "m00_a.png"
    assert "-an" not in cmd


def test_material_cmd_image_without_crop_covers_canvas():
    cmd = build_material_cmd(
        "ffmpeg", src=r"C:\shots\a.png", out="m00_a.png", kind="image",
        width=1080, height=1080, fps=60,
    )
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == (
        "scale=1080:1080:force_original_aspect_ratio=increase,"
        "crop=1080:1080,setsar=1"
    )


def test_material_cmd_video_trims_and_drops_audio():
    cmd = build_material_cmd(
        "ffmpeg", src=r"C:\clips\a.mp4", out="m01_a.mp4", kind="video",
        width=1080, height=1080, fps=30,
        in_point=10.0, out_point=13.2,
        crop={"w": 1080, "h": 1080, "x": 420, "y": 0},
        crf=20, preset="slow",
    )
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == (
        "trim=start=10.000:end=13.200,setpts=PTS-STARTPTS,"
        "crop=1080:1080:420:0,scale=1080:1080,setsar=1,fps=30"
    )
    # 素材自有音轨一律丢弃
    assert "-an" in cmd
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-crf") + 1] == "20"
    assert cmd[cmd.index("-preset") + 1] == "slow"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert cmd[-1] == "m01_a.mp4"


def test_material_cmd_video_without_in_out_raises():
    with pytest.raises(ValueError, match="in_point"):
        build_material_cmd(
            "ffmpeg", src="a.mp4", out="m00_a.mp4", kind="video",
            width=1080, height=1080, fps=60,
        )


# ---------------------------------------------------------------------------
# 音频:BGM 预混 / 转 mp3
# ---------------------------------------------------------------------------


def test_mix_bgm_filter_string(monkeypatch):
    """验证 BGM 预混的命令形态:atrim/asetpts/volume/apad 链 + amix normalize=0。"""
    captured = {}
    monkeypatch.setattr(
        compose, "run_ffmpeg",
        lambda cmd, *, cwd: captured.update(cmd=cmd, cwd=cwd),
    )
    mix_bgm("ffmpeg", "v.mp3", "bgm.mp3", volume=0.25, duration=12.5,
            out=r"out\mix.mp3", bitrate="256k")
    cmd = captured["cmd"]
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert fc == ("[1:a]atrim=0:12.500,asetpts=N/SR/TB,volume=0.25,apad[bg];"
                  "[0:a][bg]amix=inputs=2:duration=first:normalize=0[aout]")
    assert cmd[cmd.index("-map") + 1] == "[aout]"
    assert cmd[cmd.index("-b:a") + 1] == "256k"
    assert captured["cwd"].endswith("out")


def test_transcode_audio_to_mp3(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        compose, "run_ffmpeg",
        lambda cmd, *, cwd: captured.update(cmd=cmd, cwd=cwd),
    )
    transcode_audio("ffmpeg", "voice.wav", r"out\demo.audio.mp3", bitrate="192k")
    cmd = captured["cmd"]
    assert cmd[cmd.index("-i") + 1] == "voice.wav"
    assert cmd[cmd.index("-c:a") + 1] == "libmp3lame"
    assert cmd[cmd.index("-b:a") + 1] == "192k"
    assert "-vn" in cmd
    assert cmd[-1] == r"out\demo.audio.mp3"


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------


def test_probe_alpha_mode_reads_tag(monkeypatch):
    monkeypatch.setattr(compose, "_ffprobe_lines", lambda fp, args, path: ["1"])
    assert compose.probe_alpha_mode("ffprobe", "x.subs.webm") == "1"
    monkeypatch.setattr(compose, "_ffprobe_lines", lambda fp, args, path: [])
    assert compose.probe_alpha_mode("ffprobe", "x.webm") is None


def test_probe_dimensions_parses_and_reports(monkeypatch):
    monkeypatch.setattr(
        compose, "_ffprobe_lines",
        lambda fp, args, path: ["width=1920", "height=1080"],
    )
    assert compose.probe_dimensions("ffprobe", "a.png") == (1920, 1080)
    monkeypatch.setattr(compose, "_ffprobe_lines", lambda fp, args, path: [])
    with pytest.raises(RuntimeError, match="未读到完整尺寸"):
        compose.probe_dimensions("ffprobe", "a.png")


def test_probe_duration_unparsable_reports(monkeypatch):
    monkeypatch.setattr(compose, "_ffprobe_lines", lambda fp, args, path: ["abc"])
    with pytest.raises(RuntimeError, match="无法解析媒体时长"):
        compose.probe_duration("ffprobe", "a.mp3")


def test_ffprobe_failure_is_readable(monkeypatch):
    """ffprobe 非零退出:错误信息里带 stderr,便于定位素材问题。"""
    class _R:
        returncode = 1
        stdout = ""
        stderr = "No such file"

    monkeypatch.setattr(compose.subprocess, "run", lambda *a, **k: _R())
    with pytest.raises(RuntimeError, match="No such file"):
        compose._ffprobe_lines("ffprobe", ["-show_entries", "format=duration"], "x")
