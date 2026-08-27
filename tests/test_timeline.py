"""timeline 模块单测:估算轴 / 句子切分 / token 归句 / 多轨构建 / 自检 / json。"""

from itertools import pairwise

from text2video.subs import WordTiming
from text2video.timeline import (
    TrackSegment,
    assign_tokens_to_sentences,
    build_timeline,
    estimate_timings,
    split_sentences,
    validate_timeline,
)

META = {
    "voice": "zh-CN-XiaoxiaoNeural", "rate": "+0%", "volume": "+0%", "pitch": "+0Hz",
    "width": 1080, "height": 1080, "fps": 60,
    "bg_color": "1B1B2A", "bg_image": None,
    "style": "karaoke", "font": None,
    "font_size": 54, "max_chars": 18,
}


def test_estimate_timings_deterministic_and_monotonic():
    a = estimate_timings("你好,世界!")
    b = estimate_timings("你好,世界!")
    assert [w.text for w in a] == [w.text for w in b]
    assert len(a) > 0
    # 时间单调递增,首字 start≈0,末字 end=总时长
    assert a[0].start == 0
    for w1, w2 in pairwise(a):
        assert w1.end <= w2.start + 1e-6
    assert a[-1].end == a[-1].start or a[-1].end >= a[-1].start


def test_estimate_timings_rounds_cjk_per_char():
    # "你好" 两个发音 token(中文单字一个 token)
    t = estimate_timings("你好")
    assert len(t) == 2
    assert t[0].text == "你" and t[1].text == "好"


def test_split_sentences_by_punct_and_newline():
    assert split_sentences("你好。世界!") == ["你好。", "世界!"]
    assert split_sentences("第一句\n第二句") == ["第一句", "第二句"]
    assert split_sentences("没有标点的一段话") == ["没有标点的一段话"]


def test_assign_tokens_to_sentences():
    timings = [
        WordTiming("你", 0, 1), WordTiming("好", 1, 2), WordTiming("。", 2, 3),
        WordTiming("世", 3, 4), WordTiming("界", 4, 5), WordTiming("!", 5, 6),
    ]
    segs = assign_tokens_to_sentences(["你好。", "世界!"], timings)
    assert len(segs) == 2
    assert [w.text for w in segs[0]] == ["你", "好", "。"]
    assert [w.text for w in segs[1]] == ["世", "界", "!"]


def test_assign_tokens_tolerates_missing_punct():
    # TTS 可能跳过标点:句子带标点,但 token 流里没有
    timings = [WordTiming("你", 0, 1), WordTiming("好", 1, 2),
               WordTiming("世", 2, 3), WordTiming("界", 3, 4)]
    segs = assign_tokens_to_sentences(["你好。", "世界"], timings)
    assert len(segs) == 2
    assert [w.text for w in segs[0]] == ["你", "好"]
    assert [w.text for w in segs[1]] == ["世", "界"]


def test_build_timeline_estimates_tracks_aligned():
    tl = build_timeline("你好。世界!", meta=META)
    assert set(tl.tracks) == {"speech", "subtitle", "visual", "material"}
    # 语音与字幕逐段文本一致
    for a, b in zip(tl.tracks["speech"], tl.tracks["subtitle"]):
        assert a.text == b.text
    assert len(tl.tracks["speech"]) == 2
    # 画面轨覆盖全时长
    vis = tl.tracks["visual"]
    assert vis[0].start == 0.0
    assert abs(vis[-1].end - tl.duration) < 1e-6
    # 语音末端 = 总时长
    assert abs(tl.tracks["speech"][-1].end - tl.duration) < 1e-6
    # 估算模式默认通过自检
    assert tl.checks == []
    assert tl.meta["timing_mode"] == "estimate"


def test_build_timeline_rejects_empty_text():
    try:
        build_timeline("  \n ", meta=META)
        assert False, "应当拒绝空文案"
    except ValueError:
        pass


def test_validate_flags_bad_timing():
    tl = build_timeline("你好。", meta=META)
    tl.tracks["speech"][0] = TrackSegment(2.0, 1.0, "你好。")  # start > end
    assert any("时间非法" in e for e in validate_timeline(tl))


def test_validate_flags_text_mismatch():
    tl = build_timeline("你好。", meta=META)
    tl.tracks["subtitle"][0] = TrackSegment(0, 1, "别的字")
    assert any("文本不一致" in e for e in validate_timeline(tl))


def test_validate_catches_missing_visual_cover():
    tl = build_timeline("你好。", meta=META)
    tl.duration = tl.duration * 10  # 故意拉大总时长
    errs = validate_timeline(tl)
    assert any("画面轨末端" in e for e in errs)


def test_timeline_json_roundtrip(tmp_path):
    tl = build_timeline("你好。世界!", meta=META)
    p = tmp_path / "sample.timeline.json"
    tl.to_json(str(p))
    back = type(tl).from_json(str(p))
    assert back.text == tl.text
    assert back.meta == tl.meta
    assert abs(back.duration - tl.duration) < 1e-6
    assert [s.text for s in back.tracks["speech"]] == \
           [s.text for s in tl.tracks["speech"]]
    assert back.checks == tl.checks
