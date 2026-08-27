"""subs 模块的单元测试(纯函数,不联网)。"""

from text2video.subs import (
    WordTiming,
    ass_time,
    bgr_color,
    build_karaoke_ass,
    build_scroll_ass,
    display_width,
    join_words,
    karaoke_events,
    wrap_plain_text,
    wrap_words,
)


def word(text, start, end):
    return WordTiming(text, start, end)


def test_ass_time():
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(1.5) == "0:00:01.50"
    assert ass_time(61.234) == "0:01:01.23"
    assert ass_time(3661.5) == "1:01:01.50"
    assert ass_time(-3) == "0:00:00.00"


def test_ass_time_rounding_is_centisecond():
    assert ass_time(0.005) == "0:00:00.01" or ass_time(0.005) == "0:00:00.00"


def test_bgr_color():
    assert bgr_color(0xFFFFFF) == "&H00FFFFFF"
    assert bgr_color(0xFFD580) == "&H0080D5FF"  # R->B, B->R


def test_display_width():
    assert display_width("中") == 2
    assert display_width("a") == 1
    assert display_width("Ａ") == 2  # 全角字母


def test_join_words_chinese_no_space():
    words = ["你好", "世界"]
    assert join_words(words) == "你好世界"


def test_join_words_latin_space():
    assert join_words(["hello", "world"]) == "hello world"
    assert join_words(["AI", "视频"]) == "AI视频"


def test_wrap_words_preserves_order_and_time():
    ws = [word(f"s{i}", float(i), float(i) + 0.5) for i in range(12)]
    lines = wrap_words(ws, max_chars=5)  # 每行最多 5 个中文(10 格)
    flat = [w for ln in lines for w in ln]
    assert flat == ws
    assert all(len(ln) <= 5 for ln in lines)


def test_wrap_words_mixed_width():
    ws = [word("abc", 0, 1), word("中", 1, 2), word("def", 2, 3)]
    lines = wrap_words(ws, max_chars=4)  # 8 格预算:"abc"=3 + "中"=2 =5 -> "def"=3 -> 合计8
    assert len(lines) == 1
    assert [w.text for w in lines[0]] == ["abc", "中", "def"]


def test_karaoke_events_count_and_timing():
    ws = [word("今", 1.0, 1.3), word("天", 1.3, 1.6), word("好", 1.6, 1.9)]
    events = karaoke_events(ws)
    assert len(events) == 3
    # 每个事件起始 = 对应词开始
    assert events[0][0] == 1.0
    assert events[1][0] == 1.3
    assert events[2][0] == 1.6
    # 最后一个事件结尾 = 末词结束 + tail_hold
    assert events[2][1] == 1.9 + 0.35


def test_karaoke_events_highlight_prefix():
    ws = [word("a", 0, 1), word("b", 1, 2), word("c", 2, 3)]
    events = karaoke_events(ws)
    # 第一个事件:只有首词高亮
    assert events[0][2].startswith("{\\c&H00FFFFFF\\b1}a")
    # 最后一个事件:全部高亮,无灰色后缀
    last = events[-1][2]
    assert last.endswith(("abc", "a b c"))


def test_build_karaoke_ass_structure():
    lines = [[word("你好", 0, 1), word("世界", 1, 2)]]
    ass = build_karaoke_ass(lines, width=1080, height=1920)
    assert "[Script Info]" in ass
    assert "PlayResX: 1080" in ass
    assert "Dialogue:" in ass
    # 2 个词 -> 2 条事件
    assert ass.count("Dialogue:") == 2


def _style_field(ass: str, field: str) -> str:
    """从样式表取某个字段的值,便于校验 BorderStyle/BackColour 等。"""
    header = ass.split("[Events]")[0]
    fmt_line = next(
        l for l in header.splitlines() if l.startswith("Format: Name,")
    )
    style_line = next(l for l in header.splitlines() if l.startswith("Style: Base,"))
    idx = [t.strip() for t in fmt_line.split(",")].index(field)
    return style_line.split(",")[idx]


def test_karaoke_ass_respects_box():
    lines = [[word("hi", 0, 1)]]
    ass_box = build_karaoke_ass(lines, width=1080, height=1920, box=True)
    ass_no = build_karaoke_ass(lines, width=1080, height=1920, box=False)
    assert _style_field(ass_box, "BorderStyle") == "3"
    assert _style_field(ass_box, "BackColour") == "&H64101010"
    assert _style_field(ass_no, "BorderStyle") == "1"
    assert _style_field(ass_no, "BackColour") == "&H00101010"


def test_wrap_plain_text():
    line = "一二三四五六七八九十" * 2  # 20 字
    out = wrap_plain_text(line, max_chars=8)
    assert out == ["一二三四五六七八", "九十一二三四五六", "七八九十"]


def test_build_scroll_ass_uses_pos_slices():
    # 多行文本 + 每行每分片一条事件,总数远大于分片数
    ass = build_scroll_ass("一二三四五六七八九十一二三四五六七八九十",
                           duration=3.0, width=1080, height=1920, max_chars=8)
    assert "\\pos(" in ass  # 逐行用 \pos 分片模拟滚动,\move 在 ffmpeg 起点屏外时失效
    assert "0:00:03.00" in ass
    assert ass.count("Dialogue:") > 3 * 10


def test_scroll_positions_span_below_and_above_screen():
    import re

    ass = build_scroll_ass("一行字", duration=2.0, width=100, height=200,
                           fontsize=20, steps_per_second=8)
    ys = [float(m) for m in re.findall(r"\\pos\(-?\d+\.?\d*,(-?\d+\.?\d*)\)", ass)]
    assert ys, r"至少有一条 \pos 事件"
    # 行从底部进入(开始 y≈画面高)一直滚到接近顶部(y 很小),贯穿全屏
    assert max(ys) <= 200
    assert any(y > 180 for y in ys)   # 从底部附近进入
    assert any(y < 50 for y in ys)    # 滚到接近顶部


def test_scroll_wraps_long_text():
    long_text = "这是一个比较长的文案" * 10
    ass = build_scroll_ass(long_text, duration=5, max_chars=8)
    # 每行是独立事件,行文本不含 \N 换行符
    assert "\\N" not in ass
