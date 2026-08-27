"""render 端画面裁剪单测:compute_crop_window(cover 满幅+单轴滑动)、crop_frame、
_resolve_material_crops。"""

from text2video.render import (
    _resolve_material_crops,
    compute_crop_window,
    crop_frame,
)
from text2video.timeline import Timeline, TrackSegment


def test_crop_window_wide_source_slides_x_only():
    """素材更宽(16:9 配 1:1 画幅):窗口 = 满幅 1080x1080,只在 X 轴滑动(Y 固定)。"""
    # x=0.5(默认居中):窗口横向居中(偏移 420)
    cw, ch, cx, cy = compute_crop_window(1920, 1080, 1080, 1080, 0.5, 0.5)
    assert (cw, ch, cx, cy) == (1080, 1080, 420, 0)
    # x=0 靠左、x=1 靠右(窗口右缘贴素材右边缘),y 任意不影响
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
    """素材比例 = 画幅比例(16:9 素材 + 16:9 画幅):全图铺满,任意 x/y 无滑动。"""
    cw, ch, cx, cy = compute_crop_window(1920, 1080, 1920, 1080, 0.0, 1.0)
    assert (cw, ch, cx, cy) == (1920, 1080, 0, 0)
    cw, ch, cx, cy = compute_crop_window(1920, 1080, 1920, 1080, 1.0, 0.0)
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
    # 竖长素材(1150x1750)配 16:9:纵向滑动,窗口高占比 = 1150/1.78/1750
    _fx, fy, fw, fh = crop_frame(1150, 1750, 1920, 1080, 0.5, 0.0)
    assert abs(fh - 1150 / (1920 / 1080) / 1750) < 1e-12
    assert fw == 1.0 and fy == 0.0
    # y=1 → 窗口底边贴素材底
    _fx, fy, fw, fh = crop_frame(1150, 1750, 1920, 1080, 0.5, 1.0)
    assert abs(fy + fh - 1.0) < 1e-9
    # 横向素材:y 无滑动,窗口占比 = 画幅比例 / 素材比例
    _fx, fy, fw, fh = crop_frame(1920, 1080, 1080, 1080, 0.5, 0.5)
    assert abs(fw - 0.5625) < 1e-12 and fh == 1.0 and fy == 0.0


def test_resolve_material_crops(monkeypatch):
    tl = Timeline(
        meta={"width": 1080, "height": 1080},
        assets=[{"id": "pic", "path": "x.png", "type": "image"},
                {"id": "v", "path": "x.mp4", "type": "video"}],
    )
    tl.tracks["material"] = [
        TrackSegment(0, 1, "pic", data={"crop": {"x": 0.5, "y": 0.5}}),
        TrackSegment(1, 2, "v"),   # 无 crop → None
        TrackSegment(2, 3, "v", data={"crop": {"x": 1.0, "y": 0.5}}),
    ]
    monkeypatch.setattr(
        "text2video.render.compose.probe_dimensions",
        lambda ffprobe, path: (1920, 1080),
    )
    result, errors = _resolve_material_crops(tl, "ffprobe")
    # 16:9 素材配 1:1 画幅:满幅 1080x1080,默认居中(偏移 420)
    assert result[0] == {"w": 1080, "h": 1080, "x": 420, "y": 0}
    assert result[1] is None
    # x=1 → 窗口右缘贴素材右边缘
    assert result[2] == {"w": 1080, "h": 1080, "x": 840, "y": 0}
    assert errors == []


def test_resolve_material_crops_probe_failure(monkeypatch):
    tl = Timeline(
        meta={"width": 1080, "height": 1080},
        assets=[{"id": "pic", "path": "x.png", "type": "image"}],
    )
    tl.tracks["material"] = [TrackSegment(0, 1, "pic", data={"crop": {"x": 0.2}})]

    def boom(*_a, **_k):
        raise RuntimeError("无视频流或不可解码: 1")

    monkeypatch.setattr("text2video.render.compose.probe_dimensions", boom)
    result, errors = _resolve_material_crops(tl, "ffprobe")
    # 失败段返回 None(force 时退化为满幅),并归入 errors
    assert result[0] is None
    assert any("分辨率探测失败" in e for e in errors)


def test_resolve_material_crops_tolerates_malformed_crop(monkeypatch):
    """手改 timeline.json 的畸形 crop(非对象)应报错退化,而非崩溃。"""
    tl = Timeline(
        meta={"width": 1080, "height": 1080},
        assets=[{"id": "pic", "path": "x.png", "type": "image"}],
    )
    tl.tracks["material"] = [TrackSegment(0, 1, "pic", data={"crop": "x"})]
    monkeypatch.setattr(
        "text2video.render.compose.probe_dimensions",
        lambda ffprobe, path: (1920, 1080),
    )
    result, errors = _resolve_material_crops(tl, "ffprobe")
    assert result[0] is None
    assert any("crop 必须是对象" in e for e in errors)