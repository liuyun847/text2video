"""compose 模块单测:合成命令构建(filter_complex 素材段)与 BGM 预混命令。"""

from text2video.compose import build_video_cmd, mix_bgm


def _cmd(**kw):
    base = {
        "audio": "s.mp3", "ass": "s.ass", "out": "s.mp4",
        "width": 1080, "height": 1080, "fps": 60,
        "bg_color": "1B1B2A", "bg_image": None, "duration": 10.0,
    }
    base.update(kw)
    return build_video_cmd("ffmpeg", **base)


def test_plain_command_equivalence():
    """无素材时命令与旧行为等价:单背景输入 + 字幕滤镜 + 音频 map。"""
    cmd = _cmd()
    inputs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
    assert inputs == ["color=c=1B1B2A:s=1080x1080:r=60", "s.mp3"]
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert fc == "[0:v]subtitles=s.ass:fontsdir=.[vout]"
    i_map = cmd.index("-map")
    assert cmd[i_map + 1] == "[vout]"
    assert cmd[i_map + 3] == "1:a:0"      # 音频输入编号 = 1 + 素材数(0 素材)


def test_materials_construct_filter_complex():
    """两个素材段(视频+图片):overlay 窗口 + trim 裁剪 + map 编号推进。"""
    cmd = _cmd(materials=[
        {"path": "c:\\clips\\a.mp4", "type": "video", "start": 0.0, "end": 4.0,
         "in": 10.0, "out": 14.0},
        {"path": "c:\\shots\\b.png", "type": "image", "start": 4.0, "end": 10.0},
    ])
    start = cmd.index("-filter_complex")
    fc = ";".join(cmd[start + 1:start + 2])  # 单段 filter_complex 参数
    inputs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
    assert inputs == [
        "color=c=1B1B2A:s=1080x1080:r=60",     # 纯色背景(lavfi)
        "c:\\clips\\a.mp4", "c:\\shots\\b.png", "s.mp3",
    ]
    # 视频素材:trim + setpts + 铺满
    assert "trim=start=10.000:end=14.000,setpts=PTS-STARTPTS" in fc
    assert "scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080[m1]" in fc
    # 图片素材:不 trim,直接铺满
    assert "crop=1080:1080[m2]" in fc
    assert "overlay=0:0:enable='between(t,0.000,4.000)'[v1]" in fc
    assert "overlay=0:0:enable='between(t,4.000,10.000)'[v2]" in fc
    assert "[v2]subtitles=s.ass:fontsdir=.[vout]" in fc
    i_map = cmd.index("-map")
    assert cmd[i_map + 3] == "3:a:0"      # 2 素材 → 音频输入流 3


def test_materials_with_bg_image_keeps_chain():
    """背景图 + 素材:背景先缩放裁切,素材 overlay 在其后。"""
    cmd = _cmd(bg_image="c:\\bg.jpg", materials=[
        {"path": "c:\\clips\\a.mp4", "type": "video", "start": 0.0, "end": 3.0,
         "in": 0.0, "out": 3.0},
    ])
    fc = ";".join(cmd)
    assert "scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080[bg]" in fc
    assert "[bg][m1]overlay=0:0:enable='between(t,0.000,3.000)'[v1]" in fc
    assert "[v1]subtitles=s.ass:fontsdir=.[vout]" in fc


def test_materials_with_crop_emits_crop_scale():
    """素材带像素 crop 时滤镜为 crop=cw:ch:cx:cy,scale=WxH(不变形铺满)。"""
    cmd = _cmd(materials=[
        {"path": "c:\\clips\\a.mp4", "type": "video", "start": 0.0, "end": 4.0,
         "in": 10.0, "out": 14.0, "crop": {"w": 540, "h": 540, "x": 420, "y": 0}},
    ])
    fc = ";".join(cmd)
    # 时间裁剪(trim)仍存在,crop 与其正交
    assert "trim=start=10.000:end=14.000,setpts=PTS-STARTPTS" in fc
    # 空间裁剪替换为 crop=...:scale
    assert "crop=540:540:420:0,scale=1080:1080[m1]" in fc
    assert "force_original_aspect_ratio=increase" not in fc
    assert "overlay=0:0:enable='between(t,0.000,4.000)'[v1]" in fc


def test_materials_without_crop_keeps_previous_behavior():
    """无 crop 素材仍走等比铺满+中心裁切(回归保护)。"""
    cmd = _cmd(materials=[
        {"path": "c:\\shots\\b.png", "type": "image", "start": 0.0, "end": 4.0},
    ])
    fc = ";".join(cmd)
    assert "scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080[m1]" in fc


def test_annotations_emit_drawbox_chain():
    """有标注框时:先 drawbox(最后一个输出 [vsub])→ 再渲染字幕 → 框在字幕下层。"""
    cmd = _cmd(annotations=[
        {"start": 1.0, "end": 3.0, "x": 0.1, "y": 0.2, "w": 0.5, "h": 0.3,
         "color": "0xFF3B30"},
    ])
    fc = ";".join(cmd)
    # 1080x1080 画幅:0.1*1080=108,0.2*1080=216,0.5*1080=540,0.3*1080=324
    assert ("drawbox=x=108:y=216:w=540:h=324:color=0xFF3B30:t=6:"
            "enable='between(t,1.000,3.000)'[vsub]" in fc)
    assert "[vsub]subtitles=s.ass:fontsdir=.[vout]" in fc
    # 框先于字幕:fc 中 drawbox 出现在 subtitles 之前
    assert fc.index("drawbox") < fc.index("subtitles")


def test_annotations_multiple_chain():
    """多个标注框:中间框输出中间标签,最后一个输出 [vsub] 再接字幕;无纯标签对。"""
    cmd = _cmd(annotations=[
        {"start": 0.0, "end": 1.0, "x": 0.0, "y": 0.0, "w": 0.2, "h": 0.2},
        {"start": 1.0, "end": 2.0, "x": 0.3, "y": 0.3, "w": 0.2, "h": 0.2},
    ])
    fc = ";".join(cmd)
    assert "drawbox=x=0:y=0:w=216:h=216:color=0xFF3B30:t=6:enable='between(t,0.000,1.000)'[ann0]" in fc
    assert "[ann0]drawbox=x=324:y=324:w=216:h=216:color=0xFF3B30:t=6:enable='between(t,1.000,2.000)'[vsub]" in fc
    assert "[vsub]subtitles=s.ass:fontsdir=.[vout]" in fc
    assert "null" not in fc


def test_annotations_default_color():
    """标注框缺省颜色 = 红色;无标注时输出链路与旧版一致(回归)。"""
    cmd = _cmd(annotations=[{"start": 0.0, "end": 1.0,
                             "x": 0.0, "y": 0.0, "w": 0.1, "h": 0.1}])
    fc = ";".join(cmd)
    assert "drawbox=x=0:y=0:w=108:h=108:color=0xFF3B30" in fc
    cmd2 = _cmd()
    fc2 = ";".join(cmd2)
    assert "subtitles=s.ass:fontsdir=.[vout]" in fc2
    assert "drawbox" not in fc2


def test_annotations_all_invalid_skipped():
    """全部标注参数非法被跳过(手改/--force 场景防御):无 drawbox,
    素材链末标签直接接字幕滤镜合法收尾,不产生纯标签对/null。"""
    cmd = _cmd(annotations=[
        {"start": "x", "x": "y"},     # 数值非法
        {"start": None},              # 读取失败
    ])
    fc = ";".join(cmd)
    assert "drawbox" not in fc
    assert "null" not in fc
    fx = cmd[cmd.index("-filter_complex") + 1]
    assert fx == "[0:v]subtitles=s.ass:fontsdir=.[vout]"


def test_mix_bgm_filter_string():
    """验证 BGM 预混的命令形态:atrim/asetpts/volume/apad 链 + amix normalize=0。"""
    # mix_bgm 直接执行命令,这里通过 monkeypatch 捕获 cmd 验证组装
    from text2video import compose
    captured = {}
    original = compose.run_ffmpeg

    def fake_run(cmd, *, cwd):
        captured["cmd"] = cmd
        captured["cwd"] = cwd

    compose.run_ffmpeg = fake_run
    try:
        mix_bgm("ffmpeg", "v.mp3", "bgm.mp3", volume=0.25, duration=12.5,
                out="out\\mix.mp3")
    finally:
        compose.run_ffmpeg = original
    cmd = captured["cmd"]
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert fc == ("[1:a]atrim=0:12.500,asetpts=N/SR/TB,volume=0.25,apad[bg];"
                  "[0:a][bg]amix=inputs=2:duration=first:normalize=0[aout]")
    assert cmd[cmd.index("-map") + 1] == "[aout]"
    assert captured["cwd"].endswith("out")