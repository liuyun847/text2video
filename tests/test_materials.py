"""素材轨单测:load_materials 解析、validate_materials 自检、build_timeline 集成。"""

import json

import pytest

from text2video.render import _probe_material_bounds
from text2video.timeline import (
    TrackSegment,
    build_timeline,
    load_materials,
    validate_annotations,
    validate_materials,
    validate_timeline,
)

META = {
    "voice": "zh-CN-XiaoxiaoNeural", "rate": "+0%", "volume": "+0%", "pitch": "+0Hz",
    "width": 1080, "height": 1080, "fps": 60,
    "bg_color": "1B1B2A", "bg_image": None,
    "style": "karaoke", "font": None,
    "font_size": 54, "max_chars": 18,
}

SAMPLE = {
    "assets": [
        {"id": "a1", "path": "clips/intro.mp4", "type": "video"},
        {"id": "a2", "path": "shots/bg1.png", "type": "image"},
        {"id": "a3", "path": "music/bgm.mp3", "type": "audio"},
    ],
    "material": [
        {"start": 0.0, "end": 3.2, "asset": "a1", "in": 10.0, "out": 13.2},
        {"start": 3.2, "end": 7.6, "asset": "a2"},
    ],
    "bgm": {"asset": "a3", "volume": 0.25},
}


def test_load_materials_parses_and_resolves_relpaths(tmp_path):
    d = tmp_path / "sub"; d.mkdir()
    p = d / "m.json"
    p.write_text(json.dumps(SAMPLE), encoding="utf-8")
    assets, material, bgm, annotations = load_materials(str(p))
    # 相对路径按清单所在目录解析为绝对路径(分隔符归一后比较)
    import os
    assert os.path.normpath(assets[0]["path"]) == os.path.normpath(
        str(d / "clips" / "intro.mp4"))
    assert material[0] == {"start": 0.0, "end": 3.2, "asset": "a1",
                           "in": 10.0, "out": 13.2}
    assert material[1] == {"start": 3.2, "end": 7.6, "asset": "a2"}
    assert bgm == {"asset": "a3", "volume": 0.25}
    assert annotations == []


def test_load_materials_rejects_duplicate_id(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({
        "assets": [
            {"id": "a", "path": "x.mp4", "type": "video"},
            {"id": "a", "path": "y.mp4", "type": "video"},
        ],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="重复 id"):
        load_materials(str(p))


def test_load_materials_rejects_bad_type_and_image_clip(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({
        "assets": [{"id": "a", "path": "x.png", "type": "png"}],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="type 必须为"):
        load_materials(str(p))
    p.write_text(json.dumps({
        "assets": [{"id": "a", "path": "x.png", "type": "image"}],
        "material": [{"start": 0, "end": 1, "asset": "a", "in": 0, "out": 1}],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="无需 in/out"):
        load_materials(str(p))


def test_build_timeline_with_materials():
    # "你好。世界!" 估算轴 2.0s:素材段须落在时长内且裁剪长度一致
    assets, material, bgm = SAMPLE["assets"], [
        {"start": 0.0, "end": 1.0, "asset": "a1", "in": 10.0, "out": 11.0},
        {"start": 1.0, "end": 2.0, "asset": "a2"},
    ], SAMPLE["bgm"]
    tl = build_timeline("你好。世界!", meta=META,
                        assets=assets, material=material, bgm=bgm)
    mt = tl.tracks["material"]
    assert len(mt) == 2
    assert mt[0].text == "a1" and mt[0].data == {"in": 10.0, "out": 11.0}
    assert mt[1].text == "a2" and mt[1].data == {}
    assert tl.assets == assets and tl.bgm == bgm
    # 素材引用一致时自检通过
    assert tl.checks == []


def test_build_timeline_material_track_without_assets_still_passes():
    # 无素材(旧行为):material 轨为空,自检通过
    tl = build_timeline("你好。", meta=META)
    assert tl.tracks["material"] == []
    assert tl.checks == []


def test_validate_materials_flags_unknown_asset():
    errs = validate_materials(
        [{"id": "a", "path": "x", "type": "video"}],
        [TrackSegment(0, 1, "nope", data={"in": 0.0, "out": 1.0})],
        None,
    )
    assert any("不存在" in e for e in errs)


def test_validate_materials_flags_video_inout_mismatch():
    # 裁剪长度(2s)与时间线区间(5s)不一致 → 报错(保证 overlay 视觉确定)
    errs = validate_materials(
        [{"id": "a", "path": "x", "type": "video"}],
        [TrackSegment(0, 5, "a", data={"in": 0.0, "out": 2.0})],
        None,
    )
    assert any("不一致" in e for e in errs)


def test_validate_materials_flags_overlap():
    errs = validate_materials(
        SAMPLE["assets"],
        [TrackSegment(0, 4, "a1", data={"in": 0.0, "out": 4.0}),
         TrackSegment(3, 6, "a1", data={"in": 0.0, "out": 3.0})],
        None,
    )
    assert any("重叠" in e for e in errs)


def test_validate_materials_flags_audio_in_material_track():
    errs = validate_materials(
        SAMPLE["assets"],
        [TrackSegment(0, 2, "a3", data={})],
        None,
    )
    assert any("只能用于 bgm" in e for e in errs)


def test_validate_materials_flags_bad_bgm():
    errs = validate_materials(
        SAMPLE["assets"], [],
        {"asset": "a1", "volume": 0.5},   # a1 是 video
    )
    assert any("必须是 audio" in e for e in errs)
    errs = validate_materials(SAMPLE["assets"], [], {"asset": "zz", "volume": 2.0})
    assert any("不存在" in e for e in errs)
    assert any("volume" in e for e in errs)


def test_validate_materials_adjacent_segments_ok():
    # 首尾相接不算重叠
    errs = validate_materials(
        SAMPLE["assets"],
        [TrackSegment(0, 4, "a1", data={"in": 0.0, "out": 4.0}),
         TrackSegment(4, 8, "a2", data={})],
        None,
    )
    assert errs == []


def test_probe_material_bounds_catches_probe_failure(monkeypatch):
    # ffprobe 探测失败(损坏/无视频流)应并入 errors,而非裸抛(与 --force 语义一致)
    from text2video.timeline import Timeline

    tl = Timeline(assets=[{"id": "a", "path": "x.mp4", "type": "video"}])
    tl.tracks["material"] = [TrackSegment(0, 1, "a", data={"in": 0.0, "out": 1.0})]

    def boom(*a, **k):
        raise RuntimeError("无视频流或不可解码: 1")

    monkeypatch.setattr(
        "text2video.render.compose.probe_video_duration", boom
    )
    errs = _probe_material_bounds(tl, "ffprobe")
    assert any("时长探测失败" in e and "无视频流" in e for e in errs)


def test_timeline_json_roundtrip_with_materials(tmp_path):
    tl = build_timeline("你好。世界!", meta=META,
                        assets=SAMPLE["assets"],
                        material=[{"start": 0.0, "end": 1.0, "asset": "a1",
                                   "in": 10.0, "out": 11.0},
                                  {"start": 1.0, "end": 2.0, "asset": "a2"}],
                        bgm=SAMPLE["bgm"])
    p = tmp_path / "m.timeline.json"
    tl.to_json(str(p))
    back = type(tl).from_json(str(p))
    assert back.assets == tl.assets and back.bgm == tl.bgm
    assert [s.text for s in back.tracks["material"]] == ["a1", "a2"]
    assert back.tracks["material"][0].data == {"in": 10.0, "out": 11.0}
    assert validate_timeline(back) == []


# ---------------------------------------------------------------------------
# 画面裁剪 crop
# ---------------------------------------------------------------------------

CROP_SAMPLE = {
    "assets": [
        {"id": "pic", "path": "img.png", "type": "image"},
        {"id": "clip", "path": "clip.mp4", "type": "video"},
    ],
    "material": [
        {"start": 0.0, "end": 2.0, "asset": "pic",
         "crop": {"x": 0.2, "y": 0.8}},
        {"start": 2.0, "end": 4.0, "asset": "clip", "crop": {}},
    ],
}


def test_load_materials_parses_crop_defaults(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps(CROP_SAMPLE), encoding="utf-8")
    _assets, material, _bgm, _anns = load_materials(str(p))
    assert material[0]["crop"] == {"x": 0.2, "y": 0.8}
    # crop 缺键按默认补全(x/y=0.5)
    assert material[1]["crop"] == {"x": 0.5, "y": 0.5}


def test_load_materials_rejects_invalid_crop(tmp_path):
    p = tmp_path / "m.json"
    base = {"assets": [{"id": "a", "path": "x.png", "type": "image"}]}
    # crop 非对象
    d = dict(base, material=[{"start": 0, "end": 1, "asset": "a", "crop": 3}])
    p.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(ValueError, match="crop 必须是对象"):
        load_materials(str(p))
    # crop 含未知字段
    d = dict(base, material=[{"start": 0, "end": 1, "asset": "a", "crop": {"w": 10}}])
    p.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(ValueError, match="未知字段"):
        load_materials(str(p))
    # 旧版 zoom 报错提醒移除
    d = dict(base, material=[{"start": 0, "end": 1, "asset": "a",
                              "crop": {"x": 0.5, "y": 0.5, "zoom": 0.5}}])
    p.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(ValueError, match="zoom 已移除"):
        load_materials(str(p))


def test_validate_materials_crop_bounds():
    assets = [{"id": "p", "path": "x.png", "type": "image"},
              {"id": "v", "path": "x.mp4", "type": "video"}]

    def errs(crop, aid="p"):
        data = {"crop": crop}
        if aid == "v":
            data.update({"in": 0.0, "out": 1.0})  # video 段必须带合法 in/out
        return validate_materials(assets, [TrackSegment(0, 1, aid, data=data)], None)

    # 合法范围
    assert errs({"x": 0.5, "y": 0.5}) == []
    assert errs({"x": 0.0, "y": 1.0}, aid="v") == []
    # 越界
    assert any("crop.x" in e for e in errs({"x": -0.1, "y": 0.5}))
    assert any("crop.x" in e for e in errs({"x": 1.1, "y": 0.5}))
    assert any("crop.y" in e for e in errs({"x": 0.5, "y": 2.0}))
    # 旧版 zoom 键报错(含手改 timeline.json 场景)
    assert any("zoom 已移除" in e for e in errs({"x": 0.5, "y": 0.5, "zoom": 0.5}))


def test_validate_materials_tolerates_malformed_crop():
    """手改 timeline.json 产生的畸形 crop 应被自检拦截而非崩溃。"""
    assets = [{"id": "p", "path": "x.png", "type": "image"}]
    # 非对象(String)
    errs = validate_materials(
        assets, [TrackSegment(0, 1, "p", data={"crop": "zoom=0.5"})], None,
    )
    assert any("crop 必须是对象" in e for e in errs)
    # 数值字段非法
    errs = validate_materials(
        assets, [TrackSegment(0, 1, "p", data={"crop": {"x": "abc"}})], None,
    )
    assert any("crop 数值非法" in e for e in errs)


def test_build_timeline_carries_crop():
    assets, material = CROP_SAMPLE["assets"], [
        {"start": 0.0, "end": 1.0, "asset": "clip", "in": 0.0, "out": 1.0,
         "crop": {"x": 0.0, "y": 1.0},
         "frame": {"x": 0.1, "y": 0.2, "w": 0.6, "h": 1.0}},
        {"start": 1.0, "end": 2.0, "asset": "pic", "crop": {}},
    ]
    tl = build_timeline("你好。世界!", meta=META, assets=assets, material=material)
    mt = tl.tracks["material"]
    assert mt[0].data["crop"] == {"x": 0.0, "y": 1.0}
    # 手写 crop 缺键在 build_timeline 同样补默认
    assert mt[1].data["crop"] == {"x": 0.5, "y": 0.5}
    # 审片窗口几何 frame 原样透传
    assert mt[0].data["frame"] == {"x": 0.1, "y": 0.2, "w": 0.6, "h": 1.0}
    assert tl.checks == []


def test_timeline_json_roundtrip_preserves_crop(tmp_path):
    tl = build_timeline("你好。世界!", meta=META,
                        assets=CROP_SAMPLE["assets"],
                        material=[
                            {"start": 0.0, "end": 1.0, "asset": "pic",
                             "crop": {"x": 0.1, "y": 0.9},
                             "frame": {"x": 0.0, "y": 0.5, "w": 0.5, "h": 1.0}},
                            {"start": 1.0, "end": 2.0, "asset": "clip",
                             "in": 0.0, "out": 1.0, "crop": {}},
                        ],
                        annotations=[{"start": 0.5, "end": 1.5, "x": 0.1,
                                      "y": 0.2, "w": 0.5, "h": 0.3}])
    p = tmp_path / "crop.timeline.json"
    tl.to_json(str(p))
    back = type(tl).from_json(str(p))
    assert back.tracks["material"][0].data["crop"] == {"x": 0.1, "y": 0.9}
    assert back.tracks["material"][0].data["frame"] == {"x": 0.0, "y": 0.5,
                                                        "w": 0.5, "h": 1.0}
    assert back.tracks["material"][1].data["crop"] == {"x": 0.5, "y": 0.5}
    assert back.annotations == [{"start": 0.5, "end": 1.5, "x": 0.1, "y": 0.2,
                                 "w": 0.5, "h": 0.3}]
    assert validate_timeline(back) == []


# ---------------------------------------------------------------------------
# 画面标注框 annotations
# ---------------------------------------------------------------------------

ANNOTATIONS = [
    {"start": 1.0, "end": 3.0, "x": 0.1, "y": 0.2, "w": 0.5, "h": 0.4,
     "color": "0xFFFFFF"},
]


def test_load_materials_parses_annotations(tmp_path):
    p = tmp_path / "m.json"
    d = dict(SAMPLE, annotations=ANNOTATIONS)
    p.write_text(json.dumps(d), encoding="utf-8")
    _a, _m, _b, annotations = load_materials(str(p))
    assert annotations == ANNOTATIONS


def test_validate_annotations_checks():
    # 合法通过(含默认颜色省略)
    assert validate_annotations([{"start": 1.0, "end": 2.0,
                                  "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}]) == []
    assert any("时间非法" in e for e in validate_annotations(
        [{"start": 3.0, "end": 1.0, "x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}]))
    assert any("左上角" in e for e in validate_annotations(
        [{"start": 0.0, "end": 1.0, "x": 1.5, "y": 0.1, "w": 0.5, "h": 0.5}]))
    assert any("尺寸非法" in e for e in validate_annotations(
        [{"start": 0.0, "end": 1.0, "x": 0.6, "y": 0.1, "w": 0.5, "h": 0.5}]))
    assert any("数值非法" in e for e in validate_annotations(
        [{"start": "abc", "end": 1.0, "x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}]))
    # 颜色格式:0xRRGGBB 合法;非 0x 前缀命名色降级合法;0x 加错误位数非法
    assert validate_annotations([{"start": 0.0, "end": 1.0, "x": 0.1, "y": 0.1,
                                  "w": 0.5, "h": 0.5, "color": "0xFF3B30"}]) == []
    assert validate_annotations([{"start": 0.0, "end": 1.0, "x": 0.1, "y": 0.1,
                                  "w": 0.5, "h": 0.5, "color": "red"}]) == []
    assert any("颜色格式" in e for e in validate_annotations(
        [{"start": 0.0, "end": 1.0, "x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5,
          "color": "0xFF3B3"}]))


def test_validate_timeline_flags_annotation_past_end():
    """标注框末端超出总时长:validate_timeline 应报错(与素材段同一容差)。"""
    tl = build_timeline("你好。", meta=META,
                        annotations=[{"start": 0.0, "end": 5.0, "x": 0.1,
                                      "y": 0.1, "w": 0.5, "h": 0.5}])
    assert any("标注框 0 末端" in e for e in tl.checks)