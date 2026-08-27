"""compose 新增能力单测:素材转场 fade 链、画面/音轨分离命令。"""

from text2video.compose import build_video_cmd, split_tracks


def _mat(video=True, **kw):
    base = {"path": "clip.mp4", "type": "video", "start": 0.0, "end": 3.0,
            "in": 1.0, "out": 4.0}
    base.update(kw)
    return base


def test_build_video_cmd_transition_video_segment():
    args = build_video_cmd(
        "ffmpeg", audio="a.mp3", ass="s.ass", out="o.mp4",
        width=1080, height=1080, fps=60, bg_color="1B1B2A",
        bg_image=None, duration=5.0,
        materials=[_mat()], transition=0.3,
    )
    fc = args[args.index("-filter_complex") + 1]
    chain = fc.split(";")[0]  # 素材链在 filter_complex 第一节
    assert "trim=start=1.000:end=4.000,setpts=PTS-STARTPTS" in chain
    assert "format=rgba,fade=t=in:st=0.000:d=0.300:alpha=1," in chain
    assert "fade=t=out:st=2.700:d=0.300:alpha=1," in chain
    assert "overlay=0:0:enable='between(t,0.000,3.000)'" in fc


def test_build_video_cmd_transition_image_segment_global_time():
    args = build_video_cmd(
        "ffmpeg", audio="a.mp3", ass="s.ass", out="o.mp4",
        width=1080, height=1080, fps=60, bg_color="1B1B2A",
        bg_image=None, duration=5.0,
        materials=[{"path": "pic.png", "type": "image",
                    "start": 1.0, "end": 4.0}],
        transition=0.3,
    )
    fc = args[args.index("-filter_complex") + 1]
    assert "fade=t=in:st=1.000:d=0.300:alpha=1" in fc
    assert "fade=t=out:st=3.700:d=0.300:alpha=1" in fc  # end-fade


def test_build_video_cmd_transition_clamped_to_half_segment():
    args = build_video_cmd(
        "ffmpeg", audio="a.mp3", ass="s.ass", out="o.mp4",
        width=1080, height=1080, fps=60, bg_color="1B1B2A",
        bg_image=None, duration=5.0,
        materials=[{"path": "pic.png", "type": "image",
                    "start": 0.0, "end": 0.4}],
        transition=5.0,  # 段长 0.4 → 钳到 0.2
    )
    fc = args[args.index("-filter_complex") + 1]
    assert "fade=t=in:st=0.000:d=0.200:alpha=1" in fc
    assert "fade=t=out:st=0.200:d=0.200:alpha=1" in fc


def test_build_video_cmd_no_transition_keeps_old_shape():
    args = build_video_cmd(
        "ffmpeg", audio="a.mp3", ass="s.ass", out="o.mp4",
        width=1080, height=1080, fps=60, bg_color="1B1B2A",
        bg_image=None, duration=5.0, materials=[_mat()],
    )
    fc = args[args.index("-filter_complex") + 1]
    assert "fade=t=in" not in fc
    assert "format=rgba" not in fc.split(";")[1]


def test_split_tracks_command(monkeypatch):
    """split_tracks 应组装 -an -c copy 的流拷贝命令。"""
    seen = {}

    def fake_run(cmd, cwd):
        seen["cmd"] = cmd
        seen["cwd"] = cwd

    monkeypatch.setattr("text2video.compose.run_ffmpeg", fake_run)
    split_tracks("ffmpeg", r"C:\out\a.mp4", r"C:\out\a.video.mp4")
    assert seen["cmd"][:5] == ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    assert "-an" in seen["cmd"] and "-c" in seen["cmd"] and "copy" in seen["cmd"]
    assert seen["cmd"][-1] == r"C:\out\a.video.mp4"
    assert seen["cwd"] == r"C:\out"