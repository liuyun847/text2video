"""report 模块单测:markdown 分镜表与自包含 preview.html 的冒烟。"""

import text2video.report as rep
from text2video.timeline import build_timeline

META = {
    "voice": "zh-CN-XiaoxiaoNeural", "rate": "+0%", "volume": "+0%", "pitch": "+0Hz",
    "width": 1080, "height": 1080, "fps": 60,
    "bg_color": "1B1B2A", "bg_image": None,
    "style": "karaoke", "font": None,
    "font_size": 54, "max_chars": 18,
}


ASSETS = [
    {"id": "a1", "path": "clips/intro.mp4", "type": "video"},
    {"id": "a2", "path": "shots/bg1.png", "type": "image"},
]
MATERIAL = [
    {"start": 0.0, "end": 3.0, "asset": "a1", "in": 5.0, "out": 8.0},
    {"start": 3.0, "end": 6.0, "asset": "a2"},
]


def _tl():
    return build_timeline("你好。世界!", meta=META)


def _tl_with_materials():
    return build_timeline(
        "你好。世界!", meta=META,
        assets=ASSETS, material=MATERIAL, bgm={"asset": "a3", "volume": 0.2},
    )


def test_markdown_has_sections():
    md = rep.timeline_to_markdown(_tl())
    for name in ("元信息", "一致性自检", "语音轨", "字幕轨", "素材轨", "画面轨"):
        assert f"## {name}" in md
    assert "全部通过" in md
    assert "你好。" in md


def test_markdown_material_track_and_assets():
    md = rep.timeline_to_markdown(_tl_with_materials())
    assert "## 素材库" in md
    assert "| a1 | video | `clips/intro.mp4` |" in md
    # 素材段显示裁剪区间
    assert "a1 in=5.00 out=8.00" in md
    assert "a2" in md
    # BGM 出现在元信息
    assert "BGM a3" in md


def test_markdown_material_track_shows_crop():
    """素材轨带 crop 时分镜表显示裁剪位置与窗口几何(审片可见)。"""
    tl = build_timeline(
        "你好。世界!", meta=META,
        assets=[{"id": "a2", "path": "shots/bg1.png", "type": "image"}],
        material=[{"start": 0.0, "end": 3.0, "asset": "a2",
                   "crop": {"x": 0.0, "y": 1.0},
                   "frame": {"x": 0.0, "y": 0.6, "w": 1.0, "h": 0.4}}],
        annotations=[{"start": 0.5, "end": 2.0, "x": 0.1, "y": 0.2,
                      "w": 0.5, "h": 0.3, "color": "0xFF3B30"}],
    )
    md = rep.timeline_to_markdown(tl)
    assert "a2 crop(x=0,y=1)" in md
    assert "窗口(0,0.6) 1x0.4 ×素材" in md
    # 画面标注框小节
    assert "## 画面标注框" in md
    assert "0xFF3B30" in md


def test_markdown_reflects_meta():
    md = rep.timeline_to_markdown(_tl())
    assert "zh-CN-XiaoxiaoNeural" in md
    assert "1080x1080" in md


def test_preview_html_shot_panel_visibility_logic():
    """素材窗口/标注框面板:模板默认 hidden,JS 按有无内容显式切换(内容存在时可见)。"""
    html = rep.render_preview_html(_tl())
    # 模板默认隐藏两个新面板
    assert 'id="shot-panel" hidden' in html
    assert 'id="ann-panel" hidden' in html
    # JS 用有无内容决定显隐(有内容时 panel.hidden = false 生效)
    assert "panel.hidden = !segs.length" in html
    assert "panel.hidden = !anns.length" in html
    # 有 crop 素材与标注框时同样包含显隐逻辑 + 颜色 CSS 转换
    tl = build_timeline(
        "你好。世界!", meta=META,
        assets=[{"id": "a2", "path": "shots/bg1.png", "type": "image"}],
        material=[{"start": 0.0, "end": 3.0, "asset": "a2",
                   "crop": {"x": 0.5, "y": 0.5},
                   "frame": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 0.5}}],
        annotations=[{"start": 0.5, "end": 2.0, "x": 0.1, "y": 0.2,
                      "w": 0.5, "h": 0.3, "color": "0xFF3B30"}],
    )
    html_yes = rep.render_preview_html(tl)
    assert "panel.hidden = !segs.length" in html_yes
    assert "cssColor(a.color)" in html_yes
    assert "zoom=" not in html_yes      # 旧版 zoom 无残留


def test_preview_html_self_contained_embedding():
    html = rep.render_preview_html(_tl(), title="测试片")
    # 内嵌时间线 json 与标题
    assert "const TIMELINE =" in html
    assert "测试片" in html
    # 无音频:虚拟时钟(AUDIO_REF null)
    assert "const AUDIO_REF = null" in html
    # 轨道开关与轨道标签都在(含素材轨)
    for label in ("语音轨", "字幕轨", "素材轨", "画面轨"):
        assert label in html
    # 无外来依赖(自包含:不明引用外部脚本/样式)
    assert "<script src" not in html
    assert "http" not in html.split("script>")[1]


def test_preview_html_material_metadata_injected():
    html = rep.render_preview_html(_tl_with_materials())
    # 素材轨加入 ORDER 与 in/out 提示逻辑
    assert '"material"' in html
    assert "in=" in html
    assert "素材" in html


def test_preview_html_with_audio_ref():
    html = rep.render_preview_html(_tl(), audio_ref="sample.mp3")
    assert 'const AUDIO_REF = "sample.mp3"' in html


def test_preview_html_escapes_injectable_title():
    # 标题里的 < / > 不应破坏 HTML 结构
    html = rep.render_preview_html(_tl(), title="<b>x</b>")
    assert "<b>" not in html.split("<title>")[1].split("</title>")[0]
