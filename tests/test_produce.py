"""produce 模块单测:裁剪窗口、素材规划、render.json 契约、产出全流程(不跑真 ffmpeg)。"""

import json
import os

import pytest

from text2video import produce
from text2video.produce import (
    RENDER_JSON_VERSION,
    _safe_id,
    build_render_json,
    compute_crop_window,
    crop_frame,
    material_entry,
    plan_materials,
    produce_timeline,
)
from text2video.subs import WordTiming
from text2video.timeline import Timeline, TrackSegment, build_timeline

META = {
    "voice": "zh-CN-XiaoxiaoNeural", "rate": "+0%", "volume": "+0%", "pitch": "+0Hz",
    "width": 1080, "height": 1080, "fps": 60, "transition": 0.0,
    "bg_color": "1B1B2A", "bg_image": None,
    "style": "karaoke", "font": "Microsoft YaHei",
    "font_size": 54, "max_chars": 18,
}

# ---------------------------------------------------------------------------
# 裁剪窗口(cover 满幅 + 单轴滑动)
# ---------------------------------------------------------------------------


def test_crop_window_wide_source_slides_x_only():
    """素材更宽(16:9 配 1:1 画幅):窗口 = 满幅 1080x1080,只在 X 轴滑动(Y 固定)。"""
    cw, ch, cx, cy = compute_crop_window(1920, 1080, 1080, 1080, 0.5, 0.5)
    assert (cw, ch, cx, cy) == (1080, 1080, 420, 0)
    cw, ch, cx, _ = compute_crop_window(1920, 1080, 1080, 1080, 0.0, 0.9)
    assert (cw, ch, cx) == (1080, 1080, 0)
    cw, ch, cx, _ = compute_crop_window(1920, 1080, 1080, 1080, 1.0, 0.1)
    assert (cw, ch, cx) == (1080, 1080, 840)


def test_crop_window_tall_source_slides_y_only():
    """素材更高(9:16 配 1:1 画幅):窗口只在 Y 轴滑动(X 固定 0)。"""
    cw, ch, cx, cy = compute_crop_window(1080, 1920, 1080, 1080, 0.5, 0.5)
    assert (cw, ch, cx, cy) == (1080, 1080, 0, 420)
    cw, ch, _, cy = compute_crop_window(1080, 1920, 1080, 1080, 0.9, 1.0)
    assert (cw, ch, cy) == (1080, 1080, 840)


def test_crop_window_same_ratio_is_noop():
    """素材比例 = 画幅比例:全图铺满,任意 x/y 无滑动。"""
    cw, ch, cx, cy = compute_crop_window(1920, 1080, 1920, 1080, 0.0, 1.0)
    assert (cw, ch, cx, cy) == (1920, 1080, 0, 0)


def test_crop_window_never_exceeds_source():
    """极端 x/y,窗口始终落在素材内(cover 窗口按定义不越界)。"""
    for sw, sh, ow, oh in ((1920, 1080, 1080, 1080), (1080, 1920, 1080, 1080),
                           (1000, 800, 1920, 1080), (1920, 1080, 1920, 1080)):
        for x in (0.0, 0.25, 1.0):
            for y in (0.0, 0.5, 1.0):
                cw, ch, cx, cy = compute_crop_window(sw, sh, ow, oh, x, y)
                assert cw <= sw and ch <= sh
                assert cx >= 0 and cx + cw <= sw
                assert cy >= 0 and cy + ch <= sh


def test_crop_frame_matches_pixel_semantics():
    """crop_frame 比例与 compute_crop_window 像素同语义(纵向滑动示例)。"""
    _fx, fy, fw, fh = crop_frame(1150, 1750, 1920, 1080, 0.5, 0.0)
    assert abs(fh - 1150 / (1920 / 1080) / 1750) < 1e-12
    assert fw == 1.0 and fy == 0.0
    _fx, fy, fw, fh = crop_frame(1150, 1750, 1920, 1080, 0.5, 1.0)
    assert abs(fy + fh - 1.0) < 1e-9
    _fx, fy, fw, fh = crop_frame(1920, 1080, 1080, 1080, 0.5, 0.5)
    assert abs(fw - 0.5625) < 1e-12 and fh == 1.0 and fy == 0.0


# ---------------------------------------------------------------------------
# 素材规划
# ---------------------------------------------------------------------------


def _load(path: str) -> dict:
    """读回 JSON 产物(测试里多处复用)。"""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _material_tl(material, assets=None):
    return Timeline(
        meta={"width": 1080, "height": 1080},
        duration=10.0,
        assets=assets or [
            {"id": "pic", "path": "x.png", "type": "image"},
            {"id": "v", "path": "x.mp4", "type": "video"},
        ],
        tracks={"material": material},
    )


def test_plan_materials_crop_px_and_frame(monkeypatch):
    tl = _material_tl([
        TrackSegment(0, 1, "pic", data={"crop": {"x": 0.5, "y": 0.5}}),
        TrackSegment(1, 2, "v"),   # 无 crop → None
        TrackSegment(2, 3, "v", data={"in": 1.0, "out": 2.0,
                                      "crop": {"x": 1.0, "y": 0.5}}),
    ])
    monkeypatch.setattr(produce.compose, "probe_dimensions",
                        lambda ffprobe, path: (1920, 1080))
    monkeypatch.setattr(produce.compose, "probe_video_duration",
                        lambda ffprobe, path: 30.0)
    plans, errors = plan_materials(tl, "ffprobe")
    assert errors == []
    # 16:9 素材配 1:1 画幅:满幅 1080x1080,默认居中(偏移 420)
    assert plans[0]["crop_px"] == {"w": 1080, "h": 1080, "x": 420, "y": 0}
    frame = plans[0]["frame"]
    assert frame["w"] == pytest.approx(0.5625) and frame["h"] == 1.0
    assert frame["y"] == 0.0 and frame["x"] == pytest.approx(0.21875, abs=1e-3)
    assert plans[1]["crop_px"] is None and plans[1]["frame"] is None
    # x=1 → 窗口右缘贴素材右边缘
    assert plans[2]["crop_px"] == {"w": 1080, "h": 1080, "x": 840, "y": 0}
    assert plans[2]["in"] == 1.0 and plans[2]["out"] == 2.0


def test_plan_materials_probe_failure_goes_to_errors(monkeypatch):
    tl = _material_tl([
        TrackSegment(0, 1, "pic", data={"crop": {"x": 0.2}}),
    ])

    def boom(*_a, **_k):
        raise RuntimeError("无视频流或不可解码: 1")

    monkeypatch.setattr(produce.compose, "probe_dimensions", boom)
    plans, errors = plan_materials(tl, "ffprobe")
    # 失败段仍产出一条 plan(退化为满幅铺满),错误交给自检语义处理
    assert plans[0]["crop_px"] is None
    assert any("分辨率探测失败" in e for e in errors)


def test_plan_materials_tolerates_malformed_crop(monkeypatch):
    """手改 timeline.json 的畸形 crop(非对象)应报错退化,而非崩溃。"""
    tl = _material_tl([
        TrackSegment(0, 1, "pic", data={"crop": "x"}),
    ])
    plans, errors = plan_materials(tl, "ffprobe")
    assert plans[0]["crop_px"] is None
    assert any("crop 必须是对象" in e for e in errors)


def test_plan_materials_flags_video_out_beyond_source(monkeypatch):
    tl = _material_tl([
        TrackSegment(0, 5, "v", data={"in": 0.0, "out": 5.0}),
    ])
    monkeypatch.setattr(produce.compose, "probe_video_duration",
                        lambda ffprobe, path: 3.0)
    _plans, errors = plan_materials(tl, "ffprobe")
    assert any("超过素材实际时长" in e for e in errors)


def test_plan_materials_flags_unknown_asset():
    tl = _material_tl([TrackSegment(0, 1, "nope")])
    plans, errors = plan_materials(tl, "ffprobe")
    assert plans == []
    assert any("引用了不存在的素材" in e for e in errors)


def test_probe_material_bounds_catches_probe_failure(monkeypatch):
    """ffprobe 探测失败(损坏/无视频流)应并入 errors,而非裸抛。"""
    tl = _material_tl([TrackSegment(0, 1, "v", data={"in": 0.0, "out": 1.0})])

    def boom(*a, **k):
        raise RuntimeError("无视频流或不可解码: 1")

    monkeypatch.setattr(produce.compose, "probe_video_duration", boom)
    _plans, errors = plan_materials(tl, "ffprobe")
    assert any("时长探测失败" in e and "无视频流" in e for e in errors)


# ---------------------------------------------------------------------------
# render.json 契约
# ---------------------------------------------------------------------------


def _real_tl(**kw):
    timings = [WordTiming("你", 0.0, 0.5), WordTiming("好", 0.5, 1.0)]
    return build_timeline(
        "你好。", timings=timings, meta=dict(META, timing_mode="real"),
        duration=kw.pop("duration", 1.2), **kw,
    )


def test_build_render_json_contract_fields(tmp_path):
    tl = _real_tl()
    audio = tmp_path / "demo.audio.mp3"
    subs = tmp_path / "demo.subs.webm"
    audio.write_bytes(b"MP3")
    subs.write_bytes(b"WEBM")
    render = build_render_json(tl, audio_path=str(audio), subs_path=str(subs),
                               materials=[])
    assert render["version"] == RENDER_JSON_VERSION == 1
    # 字段名一字不改(消费端契约)
    assert set(render) == {
        "version", "fps", "width", "height", "duration", "durationInFrames",
        "background", "audio", "subtitle", "materials", "annotations", "timing",
    }
    assert render["fps"] == 60 and render["width"] == 1080 and render["height"] == 1080
    assert render["duration"] == 1.2
    assert render["durationInFrames"] == 72           # round(1.2 * 60)
    assert render["background"] == {"type": "color", "color": "1B1B2A"}
    # 路径一律绝对
    assert render["audio"]["path"] == os.path.abspath(str(audio))
    assert render["subtitle"]["path"] == os.path.abspath(str(subs))
    assert render["materials"] == [] and render["annotations"] == []
    assert render["timing"] == {"mode": "real"}


def test_build_render_json_empty_media_paths(tmp_path):
    """媒体缺失/为空时给 {"path": ""}(键恒在),消费端据此跳过该轨。"""
    tl = _real_tl()
    empty = tmp_path / "empty.subs.webm"
    empty.write_bytes(b"")
    render = build_render_json(
        tl, audio_path=str(tmp_path / "nope.mp3"), subs_path=str(empty),
        materials=[])
    assert render["audio"] == {"path": ""}
    assert render["subtitle"] == {"path": ""}


def test_build_render_json_background_image(tmp_path):
    tl = _real_tl()
    tl.meta["bg_image"] = r"C:\bg\cover.png"
    render = build_render_json(tl, audio_path="a.mp3", subs_path="s.webm",
                               materials=[])
    assert render["background"] == {
        "type": "image", "path": os.path.abspath(r"C:\bg\cover.png"),
    }


def test_build_render_json_annotations_rounded(tmp_path):
    tl = _real_tl(annotations=[
        {"start": 0.1, "end": 0.9, "x": 0.1, "y": 0.3, "w": 0.8, "h": 0.55,
         "color": "0xFF3B30"},
        {"start": 0.0, "end": 0.5, "x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5},
    ])
    render = build_render_json(tl, audio_path="a.mp3", subs_path="s.webm",
                              materials=[])
    assert render["annotations"][0] == {
        "start": 0.1, "end": 0.9, "x": 0.1, "y": 0.3, "w": 0.8, "h": 0.55,
        "color": "0xFF3B30",
    }
    # 缺 color 时补默认红(契约字段恒定存在)
    assert render["annotations"][1]["color"] == "0xFF3B30"


def test_material_entry_fade_clamped():
    e = material_entry("image", r"C:\m\a.png", 0.0, 0.4, transition=5.0)
    assert e["type"] == "image"
    assert e["start"] == 0.0 and e["end"] == 0.4
    assert e["fade"] == {"in": 0.2, "out": 0.2}      # 钳到段长一半
    assert e["path"] == os.path.abspath(r"C:\m\a.png")
    hard = material_entry("video", "m.mp4", 1.0, 3.0, transition=0.0)
    assert hard["fade"] == {"in": 0.0, "out": 0.0}   # 0=硬切


def test_safe_id_sanitizes():
    assert _safe_id("shot counter/1") == "shot_counter_1"
    assert _safe_id("") == "asset"


# ---------------------------------------------------------------------------
# 产出全流程(monkeypatch:不联网、不真跑 ffmpeg)
# ---------------------------------------------------------------------------


class _FakeMedia:
    """记录 ffmpeg 调用并写出目标文件,让后续探测/拷贝能跑通。"""

    def __init__(self):
        self.calls = []

    def run(self, cmd, *, cwd):
        self.calls.append((cmd, cwd))
        target = cmd[-1]
        path = target if os.path.isabs(target) else os.path.join(cwd, target)
        with open(path, "wb") as f:
            f.write(b"FAKE")

    def names(self):
        return [c[0][-1] for c in self.calls]


def _patch_pipeline(monkeypatch, *, duration=3.0, bgm=False, force_alpha=True):
    fake = _FakeMedia()
    # 末字结束须与 duration 对齐(自检容差 0.6s),否则产出会按设计拒绝
    timings = [WordTiming("你", 0.1, 0.5), WordTiming("好", 0.5, 0.9),
               WordTiming("。", 2.0, 2.9)]
    monkeypatch.setattr(produce.compose, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(produce.compose, "find_ffprobe", lambda f: "ffprobe")
    monkeypatch.setattr(produce.compose, "run_ffmpeg", fake.run)
    monkeypatch.setattr(produce.compose, "probe_duration",
                        lambda fp, path: duration)
    monkeypatch.setattr(produce.compose, "probe_dimensions",
                        lambda fp, path: (1920, 1080))
    monkeypatch.setattr(produce.compose, "probe_video_duration",
                        lambda fp, path: 30.0)
    monkeypatch.setattr(produce.compose, "probe_alpha_mode",
                        lambda fp, path: "1" if force_alpha else None)
    monkeypatch.setattr(produce, "_stage_font", lambda font, out_dir, stem: None)

    def fake_synth(text, *, meta, out_audio, cache_dir=None, backend_override=None):
        with open(out_audio, "wb") as f:
            f.write(b"MP3FAKE")
        return timings, "edge"

    monkeypatch.setattr(produce, "synthesize_with_backend", fake_synth)
    return fake


def test_produce_timeline_writes_all_artifacts(tmp_path, monkeypatch):
    fake = _patch_pipeline(monkeypatch)
    tl = build_timeline("你好。", meta=dict(META, timing_mode="estimate"))
    base = str(tmp_path / "demo")
    assert produce_timeline(tl, base=base, precheck=False) == 0

    # 五类产物齐全 + 字幕源 + 时间线三件套
    for suffix in (".audio.mp3", ".subs.webm", ".ass", ".render.json",
                   ".timeline.json", ".timeline.md", ".preview.html"):
        assert os.path.isfile(base + suffix), suffix
    # 无素材时不建 material 目录(空目录没意义)
    assert not os.path.isdir(base + ".material") or \
        os.listdir(base + ".material") == []
    # 中间语音临时文件已清理
    assert not os.path.exists(str(tmp_path / "demo.voice.tmp"))

    render = _load(base + ".render.json")
    assert render["duration"] == 3.0
    assert render["durationInFrames"] == 180
    assert render["timing"] == {"mode": "real"}
    assert render["audio"]["path"].endswith("demo.audio.mp3")

    # 时间线升到 v5 且逐字轴落盘
    raw = _load(base + ".timeline.json")
    assert raw["version"] == 5
    words = raw["tracks"]["speech"][0]["data"]["words"]
    assert [w["text"] for w in words] == ["你", "好", "。"]
    assert words[0] == {"text": "你", "start": 0.1, "end": 0.5}
    assert words[-1]["end"] == 2.9
    # subtitle 段不重复存逐字轴
    assert raw["tracks"]["subtitle"][0]["data"] == {}

    # 字幕轨命令确实带 alpha 画布与 yuva420p
    subs_cmds = [c for c in fake.calls if c[0][-1].endswith(".subs.webm")]
    assert len(subs_cmds) == 1
    assert "yuva420p" in subs_cmds[0][0]


def test_produce_timeline_requires_alpha(tmp_path, monkeypatch):
    """字幕轨丢了 alpha 标记 → 直接可读报错,不静默产出坏文件。"""
    _patch_pipeline(monkeypatch, force_alpha=False)
    tl = build_timeline("你好。", meta=dict(META, timing_mode="estimate"))
    with pytest.raises(RuntimeError, match="alpha_mode"):
        produce_timeline(tl, base=str(tmp_path / "demo"), precheck=False)


def test_produce_timeline_with_materials(tmp_path, monkeypatch):
    fake = _patch_pipeline(monkeypatch)
    img = tmp_path / "shot.png"
    img.write_bytes(b"PNG")
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"MP4")
    tl = build_timeline(
        "你好。", meta=dict(META, timing_mode="estimate", transition=0.3),
        assets=[{"id": "shot", "path": str(img), "type": "image"},
                {"id": "clip", "path": str(clip), "type": "video"}],
        material=[{"start": 0.0, "end": 1.5, "asset": "shot",
                   "crop": {"x": 0.5, "y": 0.0}},
                  {"start": 1.5, "end": 3.0, "asset": "clip",
                   "in": 2.0, "out": 3.5}],
        duration=3.0,
    )
    base = str(tmp_path / "demo")
    assert produce_timeline(tl, base=base, precheck=False) == 0

    mat_dir = base + ".material"
    files = sorted(os.listdir(mat_dir))
    assert files == ["m00_shot.png", "m01_clip.mp4"]
    render = _load(base + ".render.json")
    assert [m["type"] for m in render["materials"]] == ["image", "video"]
    assert render["materials"][0]["path"] == os.path.abspath(
        os.path.join(mat_dir, "m00_shot.png"))
    assert render["materials"][0]["fade"] == {"in": 0.3, "out": 0.3}
    # 素材 mp4 命令:trim in/out + 丢弃音轨 + 尺寸=输出画幅
    mp4_cmd = next(c[0] for c in fake.calls if c[0][-1] == "m01_clip.mp4")
    vf = mp4_cmd[mp4_cmd.index("-vf") + 1]
    assert "trim=start=2.000:end=3.500" in vf
    assert "scale=1080:1080" in vf
    assert "-an" in mp4_cmd
    # 时间线带审片窗口几何
    raw = _load(base + ".timeline.json")
    assert raw["tracks"]["material"][0]["data"]["frame"]["w"] == 0.5625


def test_produce_timeline_cleans_stale_material_files(tmp_path, monkeypatch):
    """重跑时清掉上一轮命名规则内的素材中间件,不留残缺段。"""
    _patch_pipeline(monkeypatch)
    img = tmp_path / "shot.png"
    img.write_bytes(b"PNG")
    mat_dir = tmp_path / "demo.material"
    mat_dir.mkdir()
    (mat_dir / "m09_old.png").write_bytes(b"OLD")
    (mat_dir / "keep_me.txt").write_text("x", encoding="utf-8")
    tl = build_timeline(
        "你好。", meta=dict(META, timing_mode="estimate"),
        assets=[{"id": "shot", "path": str(img), "type": "image"}],
        material=[{"start": 0.0, "end": 1.5, "asset": "shot"}], duration=3.0,
    )
    produce_timeline(tl, base=str(tmp_path / "demo"), precheck=False)
    assert not (mat_dir / "m09_old.png").exists()
    assert (mat_dir / "keep_me.txt").exists()      # 不碰非本流程文件
    assert (mat_dir / "m00_shot.png").exists()


def test_produce_removes_stale_material_dir_when_no_materials(tmp_path, monkeypatch):
    """这一轮没有素材段:上一轮的片段要清掉,目录变空即删(不留悬空目录)。"""
    _patch_pipeline(monkeypatch)
    mat_dir = tmp_path / "demo.material"
    mat_dir.mkdir()
    (mat_dir / "m00_old.png").write_bytes(b"OLD")
    tl = build_timeline("你好。", meta=dict(META, timing_mode="estimate"))
    produce_timeline(tl, base=str(tmp_path / "demo"), precheck=False)
    assert not mat_dir.exists()


def test_produce_rejects_bad_timeline(tmp_path, monkeypatch):
    """自检不过默认拒绝产出,且不进入 TTS/ffmpeg。"""
    called = {"n": 0}
    monkeypatch.setattr(produce.compose, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(produce.compose, "find_ffprobe", lambda f: "ffprobe")
    monkeypatch.setattr(
        produce, "synthesize_with_backend",
        lambda *a, **k: called.update(n=called["n"] + 1),
    )
    tl = Timeline(text="你好。", meta={"width": 100, "height": 100})
    tl.tracks["visual"] = []
    tl.tracks["speech"] = [TrackSegment(2.0, 1.0, "你好。")]
    tl.tracks["subtitle"] = [TrackSegment(0.0, 1.0, "你好。")]
    tl.duration = 1.0
    with pytest.raises(SystemExit) as exc:
        produce_timeline(tl, base=str(tmp_path / "bad"))
    assert exc.value.code == 2
    assert called["n"] == 0
