"""剪辑层(edit)单测:契约解析 / 时长与转场 / 滤镜与命令拼装 / 错误路径(不跑真 ffmpeg)。"""

import json

import pytest

from text2video import edit
from text2video.cli import build_parser
from text2video.edit import EditError
from text2video.timeline import Timeline

BASE_DIR = "base"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _touch(path):
    """造一个"存在即可"的假素材(parse_edit 只查存在性,不解析内容)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00")
    return path


def _clip(tmp_path, name="a.mp4", **extra):
    """在 tmp_path/clips/ 下造素材并返回 clips 项(相对路径)。"""
    _touch(tmp_path / "clips" / name)
    return {"path": "clips/" + name, **extra}


def _norm(path, *, in_point=0.0, out=2.0, speed=1.0, volume=1.0, audio=True,
          transition=None):
    """plan_edit / 滤镜层的输入(parse_edit 的规范化形态,手写免依赖解析)。"""
    return {
        "path": str(path), "in": in_point, "out": out, "speed": speed,
        "volume": volume, "audio": audio, "transition": transition,
    }


def _write_edit(tmp_path, payload, *, name="edit.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _plan3(tmp_path):
    """三段清单:1.0 变速 + 两个转场(第二个超长被钳),用于算时长/放置区间。"""
    clips = [
        _norm(_touch(tmp_path / "clips" / "c1.mp4"), in_point=1.0, out=4.0),
        _norm(_touch(tmp_path / "clips" / "c2.mp4"), out=3.0, speed=1.5,
              transition={"type": "fade", "duration": 0.4}),
        _norm(_touch(tmp_path / "clips" / "c3.mp4"), in_point=0.5, out=2.5,
              speed=2.0, transition={"type": "wipeleft", "duration": 3.0}),
    ]
    return clips


# ---------------------------------------------------------------------------
# 路径解析与基础换算
# ---------------------------------------------------------------------------

def test_resolve_path_relative_absolute_and_empty(tmp_path):
    assert edit.resolve_path("clips/a.mp4", str(tmp_path)) == str(tmp_path / "clips" / "a.mp4")
    abs_path = str(tmp_path / "x" / "b.mp4")
    assert edit.resolve_path(abs_path, str(tmp_path)) == abs_path
    assert edit.resolve_path(None, str(tmp_path)) == ""
    assert edit.resolve_path("  ", str(tmp_path)) == ""


def test_clip_placed_length_scales_with_speed():
    assert edit.clip_placed_length(1.0, 4.0, 1.0) == pytest.approx(3.0)
    assert edit.clip_placed_length(0.0, 2.0, 4.0) == pytest.approx(0.5)
    assert edit.clip_placed_length(0.0, 1.0, 0.25) == pytest.approx(4.0)


@pytest.mark.parametrize(("in_point", "out_point", "speed"), [
    (2.0, 2.0, 1.0),
    (3.0, 1.0, 1.0),
    (0.0, 1.0, 0.0),
])
def test_clip_placed_length_rejects_bad_input(in_point, out_point, speed):
    with pytest.raises(EditError):
        edit.clip_placed_length(in_point, out_point, speed)


def test_atempo_chain_covers_contract_range():
    # 1.0 不需要滤镜;单级范围内只插一级;0.25/4.0 需要拆成两级
    assert edit.atempo_chain(1.0) == []
    assert edit.atempo_chain(1.5) == [pytest.approx(1.5)]
    for speed in (0.25, 0.4, 0.5, 2.0, 3.0, 4.0):
        stages = edit.atempo_chain(speed)
        product = 1.0
        for stage in stages:
            assert 0.5 <= stage <= 100.0
            product *= stage
        assert product == pytest.approx(speed, rel=1e-6)
    assert len(edit.atempo_chain(0.25)) == 2


def test_clamp_transition_caps_at_half_of_shorter_neighbour():
    assert edit.clamp_transition(0.4, 3.0, 2.0) == pytest.approx(0.4)
    assert edit.clamp_transition(3.0, 2.0, 1.0) == pytest.approx(0.5)
    assert edit.clamp_transition(0.0, 3.0, 2.0) == 0.0
    assert edit.clamp_transition(0.5, 0.0, 2.0) == 0.0


# ---------------------------------------------------------------------------
# edit.json 契约解析
# ---------------------------------------------------------------------------

def test_parse_edit_fills_defaults(tmp_path):
    spec = edit.parse_edit(
        {"clips": [_clip(tmp_path, out=2.0)]}, base_dir=str(tmp_path),
    )
    clip = spec["clips"][0]
    assert clip["in"] == 0.0 and clip["out"] == 2.0
    assert clip["speed"] == 1.0 and clip["volume"] == 1.0
    assert clip["audio"] is True and clip["transition"] is None
    assert clip["path"] == str(tmp_path / "clips" / "a.mp4")
    assert spec["voice"] is None and spec["bgm"] is None
    assert spec["fps"] is None and spec["width"] is None and spec["height"] is None
    assert spec["master"] == {
        "target_lufs": edit.DEFAULT_TARGET_LUFS,
        "true_peak": edit.DEFAULT_TRUE_PEAK,
    }
    assert spec["version"] == edit.EDIT_JSON_VERSION
    assert spec["unknown"] == []


def test_parse_edit_full_contract(tmp_path):
    _touch(tmp_path / "voice.mp3")
    _touch(tmp_path / "bgm.mp3")
    spec = edit.parse_edit({
        "version": 1, "fps": 30, "width": 1920, "height": 1080,
        "voice": {"path": "voice.mp3", "offset": 0.5, "volume": 0.9},
        "clips": [{
            **_clip(tmp_path, "c1.mp4"), "in": 1.0, "out": 2.5,
            "speed": 0.5, "volume": 0.8,
            "audio": False,
            "transition": {"type": "dissolve", "duration": 0.6},
        }],
        "bgm": {"path": "bgm.mp3", "volume": 0.25, "duck": False},
        "master": {"target_lufs": -16, "true_peak": -2},
        "comment": "未知字段应被忽略并提示",
    }, base_dir=str(tmp_path))
    assert (spec["fps"], spec["width"], spec["height"]) == (30, 1920, 1080)
    assert spec["voice"] == {
        "path": str(tmp_path / "voice.mp3"), "offset": 0.5, "volume": 0.9,
    }
    assert spec["bgm"] == {
        "path": str(tmp_path / "bgm.mp3"), "volume": 0.25, "duck": False,
    }
    assert spec["master"] == {"target_lufs": -16.0, "true_peak": -2.0}
    clip = spec["clips"][0]
    assert clip["speed"] == 0.5 and clip["audio"] is False and clip["volume"] == 0.8
    assert clip["transition"] == {"type": "dissolve", "duration": 0.6}
    assert spec["unknown"] and "comment" in spec["unknown"][0]


def test_parse_edit_supports_relative_paths_from_edit_dir(tmp_path):
    edit_dir = tmp_path / "job"
    _touch(edit_dir / "media" / "x.mp4")
    spec = edit.parse_edit(
        {"clips": [{"path": "media/x.mp4", "out": 1.0}]},
        base_dir=str(edit_dir),
    )
    assert spec["clips"][0]["path"] == str(edit_dir / "media" / "x.mp4")


def test_parse_edit_rejects_empty_clips(tmp_path):
    with pytest.raises(EditError) as exc:
        edit.parse_edit({"clips": []}, base_dir=str(tmp_path))
    assert "clips" in str(exc.value)
    with pytest.raises(EditError):
        edit.parse_edit({}, base_dir=str(tmp_path))


def test_parse_edit_rejects_out_not_greater_than_in(tmp_path):
    payload = {"clips": [{**_clip(tmp_path), "in": 2.0, "out": 2.0}]}
    with pytest.raises(EditError) as exc:
        edit.parse_edit(payload, base_dir=str(tmp_path))
    assert "out" in str(exc.value)


def test_parse_edit_rejects_negative_in(tmp_path):
    with pytest.raises(EditError):
        edit.parse_edit({
            "clips": [{**_clip(tmp_path), "in": -1.0, "out": 1.0}],
        }, base_dir=str(tmp_path))


def test_parse_edit_rejects_missing_out(tmp_path):
    with pytest.raises(EditError):
        edit.parse_edit({"clips": [_clip(tmp_path)]}, base_dir=str(tmp_path))


@pytest.mark.parametrize("speed", [0.1, 0.2, 4.5, 10])
def test_parse_edit_rejects_speed_out_of_range(tmp_path, speed):
    payload = {"clips": [_clip(tmp_path, out=2.0, speed=speed)]}
    with pytest.raises(EditError) as exc:
        edit.parse_edit(payload, base_dir=str(tmp_path))
    assert "speed" in str(exc.value)


@pytest.mark.parametrize("speed", [0.25, 1.0, 4.0])
def test_parse_edit_accepts_boundary_speeds(tmp_path, speed):
    spec = edit.parse_edit({"clips": [_clip(tmp_path, out=2.0, speed=speed)]},
                           base_dir=str(tmp_path))
    assert spec["clips"][0]["speed"] == speed


def test_parse_edit_rejects_unknown_transition_type(tmp_path):
    payload = {"clips": [{
        **_clip(tmp_path, out=2.0),
        "transition": {"type": "starwipe", "duration": 0.3},
    }]}
    with pytest.raises(EditError) as exc:
        edit.parse_edit(payload, base_dir=str(tmp_path))
    assert "starwipe" in str(exc.value) and "fade" in str(exc.value)


def test_parse_edit_rejects_zero_transition_duration(tmp_path):
    payload = {"clips": [{
        **_clip(tmp_path, out=2.0),
        "transition": {"type": "fade", "duration": 0},
    }]}
    with pytest.raises(EditError):
        edit.parse_edit(payload, base_dir=str(tmp_path))


def test_parse_edit_rejects_missing_clip_file(tmp_path):
    with pytest.raises(EditError) as exc:
        edit.parse_edit({"clips": [{"path": "nope.mp4", "out": 1.0}]},
                        base_dir=str(tmp_path))
    assert "不存在" in str(exc.value)


def test_parse_edit_rejects_unsupported_version(tmp_path):
    payload = {"version": 2, "clips": [_clip(tmp_path, out=1.0)]}
    with pytest.raises(EditError) as exc:
        edit.parse_edit(payload, base_dir=str(tmp_path))
    assert "version" in str(exc.value)


def test_parse_edit_rejects_master_out_of_range(tmp_path):
    clips = [_clip(tmp_path, out=1.0)]
    with pytest.raises(EditError):
        edit.parse_edit({"clips": clips, "master": {"target_lufs": -80}},
                        base_dir=str(tmp_path))
    with pytest.raises(EditError):
        edit.parse_edit({"clips": clips, "master": {"true_peak": 2}},
                        base_dir=str(tmp_path))


def test_parse_edit_rejects_zero_bgm_volume(tmp_path):
    _touch(tmp_path / "bgm.mp3")
    payload = {"clips": [_clip(tmp_path, out=1.0)],
               "bgm": {"path": "bgm.mp3", "volume": 0}}
    with pytest.raises(EditError):
        edit.parse_edit(payload, base_dir=str(tmp_path))


def test_parse_edit_rejects_audio_flag_as_string(tmp_path):
    payload = {"clips": [{**_clip(tmp_path, out=1.0), "audio": "false"}]}
    with pytest.raises(EditError):
        edit.parse_edit(payload, base_dir=str(tmp_path))


def test_load_edit_missing_and_broken(tmp_path):
    with pytest.raises(EditError):
        edit.load_edit(str(tmp_path / "nope.json"))
    broken = tmp_path / "bad.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(EditError):
        edit.load_edit(str(broken))


def test_load_edit_reads_file(tmp_path):
    path = _write_edit(tmp_path, {"clips": [_clip(tmp_path, out=1.5)]})
    spec = edit.load_edit(str(path))
    assert spec["clips"][0]["out"] == 1.5


# ---------------------------------------------------------------------------
# 放置区间 / 总时长
# ---------------------------------------------------------------------------

def test_plan_edit_overlap_and_total_duration(tmp_path):
    plan = edit.plan_edit(_plan3(tmp_path))
    first, second, third = plan["clips"]
    # 1) 3.0s 无转场;2) 3.0/1.5 = 2.0s + 0.4 重叠;3) 2.0/2.0 = 1.0s + 0.5 重叠(被钳)
    assert first["length"] == pytest.approx(3.0)
    assert (first["start"], first["end"]) == (0.0, 3.0)
    assert second["length"] == pytest.approx(2.0)
    assert (second["start"], second["end"]) == (pytest.approx(2.6), pytest.approx(4.6))
    assert third["length"] == pytest.approx(1.0)
    assert third["transition"]["duration"] == pytest.approx(0.5)
    assert third["transition"]["clamped"] is True
    assert (third["start"], third["end"]) == (pytest.approx(4.1), pytest.approx(5.1))
    assert plan["transition_total"] == pytest.approx(0.9)
    # 总时长 = Σ变速后时长 - Σ转场重叠 = 6.0 - 0.9
    assert plan["duration"] == pytest.approx(5.1)


def test_plan_edit_without_transition_is_sum_of_lengths(tmp_path):
    clips = [
        _norm(_touch(tmp_path / "clips" / "c1.mp4"), out=2.0),
        _norm(_touch(tmp_path / "clips" / "c2.mp4"), out=3.0, speed=1.5),
    ]
    plan = edit.plan_edit(clips)
    assert plan["transition_total"] == 0.0
    assert plan["duration"] == pytest.approx(4.0)
    assert plan["clips"][1]["transition"] is None


def test_plan_edit_ignores_first_transition_and_warns(tmp_path):
    clips = [_norm(_touch(tmp_path / "clips" / "a.mp4"), out=2.0,
                   transition={"type": "fade", "duration": 0.4})]
    plan = edit.plan_edit(clips)
    assert plan["clips"][0]["transition"]["duration"] == 0.0
    assert plan["duration"] == pytest.approx(2.0)
    warnings = edit.plan_warnings(plan)
    assert warnings and "第 1 段" in warnings[0]


def test_plan_warnings_reports_clamped_transition(tmp_path):
    warnings = edit.plan_warnings(edit.plan_edit(_plan3(tmp_path)))
    assert any("钳到 0.500s" in item for item in warnings)


def test_plan_edit_rejects_empty_clips():
    with pytest.raises(EditError):
        edit.plan_edit([])


# ---------------------------------------------------------------------------
# 画面流滤镜 / 命令
# ---------------------------------------------------------------------------

def test_build_mezzanine_filtergraph_uses_xfade_and_concat(tmp_path):
    plan = edit.plan_edit(_plan3(tmp_path))
    graph = edit.build_mezzanine_filtergraph(plan, width=1920, height=1080, fps=30)
    assert graph.endswith("null[vout]")
    assert "xfade=transition=fade:duration=0.400:offset=2.600" in graph
    assert "xfade=transition=wipeleft:duration=0.500:offset=4.100" in graph
    assert "fps=30" in graph and "scale=1920:1080" in graph
    assert "settb=AVTB" in graph
    # 无转场时用 concat 硬接
    hard = edit.plan_edit([
        _norm(_touch(tmp_path / "clips" / "h1.mp4"), out=1.0),
        _norm(_touch(tmp_path / "clips" / "h2.mp4"), out=1.0),
    ])
    assert "concat=n=2:v=1:a=0" in edit.build_mezzanine_filtergraph(
        hard, width=1080, height=1080, fps=60,
    )


def test_build_mezzanine_filtergraph_single_clip(tmp_path):
    plan = edit.plan_edit([_norm(_touch(tmp_path / "clips" / "only.mp4"), out=2.0)])
    graph = edit.build_mezzanine_filtergraph(plan, width=1080, height=1920, fps=60)
    assert "xfade" not in graph and "concat" not in graph
    assert graph == (
        "[0:v]setpts=(PTS-STARTPTS)/1,fps=60,"
        "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "setsar=1,format=yuv420p,settb=AVTB[v0];[v0]null[vout]"
    )


def test_build_mezzanine_cmd_args(tmp_path):
    plan = edit.plan_edit(_plan3(tmp_path))
    cmd = edit.build_mezzanine_cmd(
        "ffmpeg", plan, width=1920, height=1080, fps=30, out="out/x.mezzanine.mp4",
    )
    assert cmd[0] == "ffmpeg"
    assert cmd.count("-i") == 3
    # 每段用输入侧 -ss/-t 切源区间
    assert cmd[cmd.index("-ss") + 1] == "1.000"
    assert cmd[cmd.index("-t") + 1] == "3.000"
    # 无音轨 + 帧数锁死 = round(总时长*fps)
    assert "-an" in cmd
    assert cmd[cmd.index("-frames:v") + 1] == str(round(5.1 * 30))
    assert cmd[-1] == "out/x.mezzanine.mp4"
    assert "-filter_complex" in cmd


def test_build_mezzanine_cmd_rejects_unknown_transition(tmp_path):
    plan = edit.plan_edit(_plan3(tmp_path))
    plan["clips"][1]["transition"]["type"] = "starwipe"
    with pytest.raises(EditError):
        edit.build_mezzanine_filtergraph(plan, width=1080, height=1080, fps=60)


# ---------------------------------------------------------------------------
# 母带混音滤镜 / 命令
# ---------------------------------------------------------------------------

def _clip_audio(tmp_path):
    return [{
        "index": 0, "path": str(tmp_path / "clips" / "c1.mp4"),
        "source_in": 1.0, "source_duration": 3.0, "speed": 1.0,
        "volume": 0.8, "start": 0.0,
    }, {
        "index": 1, "path": str(tmp_path / "clips" / "c2.mp4"),
        "source_in": 0.0, "source_duration": 3.0, "speed": 1.5,
        "volume": 0.6, "start": 2.6,
    }]


def test_build_premix_filtergraph_ducks_bgm_with_voice_sidechain(tmp_path):
    graph = edit.build_premix_filtergraph(
        clip_audio=_clip_audio(tmp_path),
        voice={"index": 2, "offset": 0.5, "volume": 0.9},
        bgm={"index": 3, "volume": 0.3, "duck": True},
        duration=5.1,
    )
    assert "sidechaincompress=" + edit.SIDECHAIN_ARGS in graph
    assert "[voice_raw]asplit=2[voice_mix][voice_sc]" in graph
    # 变速同步 + 放到剪辑后起点(2.6s -> 2600ms)
    assert "atempo=1.5" in graph and "adelay=2600:all=1" in graph
    assert "adelay=500:all=1" in graph           # voice.offset
    assert "volume=0.8" in graph and "volume=0.3" in graph
    assert "amix=inputs=4:duration=longest:normalize=0" in graph
    # 尾部只补不裁:atrim 在多输入链上会按不可靠的时间戳裁(实测把 7.404 裁成 5.404),
    # 裁长交给命令里的 -t;这里 atrim 只出现在"给循环 BGM 封口"的输入链上
    assert graph.endswith("apad=whole_dur=5.100[premix]")
    assert "normalize=0,apad=whole_dur=5.100[premix]" in graph


def test_build_premix_filtergraph_without_voice_degrades_to_fixed_volume(tmp_path):
    graph = edit.build_premix_filtergraph(
        clip_audio=[],
        voice=None,
        bgm={"index": 0, "volume": 0.3, "duck": True},
        duration=4.0,
    )
    assert "sidechaincompress" not in graph      # 无配音就没有侧链,退化为固定音量
    assert "[bgm_raw]anull[bgm]" in graph
    assert "apad=whole_dur=4.000" in graph


def test_build_premix_filtergraph_duck_false_skips_sidechain(tmp_path):
    graph = edit.build_premix_filtergraph(
        clip_audio=[],
        voice={"index": 0, "offset": 0.0, "volume": 1.0},
        bgm={"index": 1, "volume": 0.3, "duck": False},
        duration=4.0,
    )
    assert "sidechaincompress" not in graph
    # 不做 ducking 时不能留下没人接的侧链(asplit 两个输出都必须连上)
    assert "asplit" not in graph and "voice_sc" not in graph
    assert "[voice_raw]anull[voice_mix]" in graph


def test_build_premix_filtergraph_voice_without_bgm_has_no_sidechain(tmp_path):
    graph = edit.build_premix_filtergraph(
        clip_audio=_clip_audio(tmp_path)[:1],
        voice={"index": 1, "offset": 0.0, "volume": 1.0},
        bgm=None,
        duration=3.0,
    )
    assert "asplit" not in graph and "sidechaincompress" not in graph
    assert "amix=inputs=2" in graph


def test_build_premix_filtergraph_requires_any_source():
    with pytest.raises(EditError) as exc:
        edit.build_premix_filtergraph(
            clip_audio=[], voice=None, bgm=None, duration=1.0,
        )
    assert "音频源" in str(exc.value)


def test_build_premix_cmd_loops_bgm_and_indexes_inputs(tmp_path):
    cmd = edit.build_premix_cmd(
        "ffmpeg",
        clip_audio=_clip_audio(tmp_path)[:1],
        voice={"path": "voice.mp3", "offset": 0.0, "volume": 1.0},
        bgm={"path": "bgm.mp3", "volume": 0.3, "duck": True},
        duration=5.1,
        out="out/x.mixed.pre.wav",
    )
    # BGM 用 -stream_loop -1 循环(短 BGM 也能铺满母带)
    assert cmd[cmd.index("-stream_loop") + 1] == "-1"
    assert "bgm.mp3" in cmd
    # 素材原声用输入侧 -ss/-t 与画面段同一 in/out
    assert cmd[cmd.index("-ss") + 1] == "1.000"
    assert cmd[cmd.index("-t") + 1] == "3.000"
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:a]" in graph and "[1:a]" in graph and "[2:a]" in graph
    assert cmd[cmd.index("-t", cmd.index("-map")) + 1] == "5.100"
    assert cmd[-1] == "out/x.mixed.pre.wav"


# ---------------------------------------------------------------------------
# 时长策略 / 配音来源 / 清单回填
# ---------------------------------------------------------------------------

def test_mix_duration_follows_timeline_when_voice_present():
    assert edit.mix_duration(24.168, 8.4, has_voice=True) == pytest.approx(24.168)
    assert edit.mix_duration(24.168, 8.4, has_voice=False) == pytest.approx(8.4)
    assert edit.mix_duration(0.0, 8.4, has_voice=True) == pytest.approx(8.4)


def test_resolve_voice_prefers_edit_json_then_render_json_then_convention(tmp_path):
    master = str(tmp_path / "x.mixed.mp3")
    explicit = {"path": str(_touch(tmp_path / "v.mp3")), "offset": 0.0, "volume": 1.0}
    voice, warnings = edit.resolve_voice(
        {"voice": explicit}, base=str(tmp_path / "x"),
        render_doc={"audio": {"path": str(_touch(tmp_path / "old.mp3"))}},
        master_path=master,
    )
    assert voice == explicit and warnings == []

    recorded = str(_touch(tmp_path / "x.audio.mp3"))
    voice, _ = edit.resolve_voice(
        {"voice": None}, base=str(tmp_path / "x"),
        render_doc={"audio": {"path": recorded}}, master_path=master,
    )
    assert voice["path"] == recorded and voice["volume"] == 1.0

    voice, warnings = edit.resolve_voice(
        {"voice": None}, base=str(tmp_path / "x"), render_doc=None,
        master_path=master,
    )
    assert voice["path"] == recorded          # {base}.audio.mp3 约定产物
    assert warnings == []


def test_resolve_voice_skips_previous_master(tmp_path):
    # 重跑场景:render.json 的 audio.path 已是上一轮母带 -> 跳过,继续用 {base}.audio.mp3
    master = str(_touch(tmp_path / "x.mixed.mp3"))
    produced = str(_touch(tmp_path / "x.audio.mp3"))
    voice, warnings = edit.resolve_voice(
        {"voice": None}, base=str(tmp_path / "x"),
        render_doc={"audio": {"path": master}}, master_path=master,
    )
    assert voice["path"] == produced
    assert warnings == []
    # 但它是唯一候选时,最终告警要说明"跳过上一轮母带"
    voice, warnings = edit.resolve_voice(
        {"voice": None}, base=str(tmp_path / "y"),
        render_doc={"audio": {"path": master}}, master_path=master,
    )
    assert voice is None
    assert any("没有找到配音" in item and "母带" in item for item in warnings)


def test_resolve_voice_missing_everything_warns(tmp_path):
    voice, warnings = edit.resolve_voice(
        {"voice": None}, base=str(tmp_path / "x"), render_doc=None,
        master_path=str(tmp_path / "x.mixed.mp3"),
    )
    assert voice is None
    assert any("没有找到配音" in item for item in warnings)


def test_resolve_voice_rejects_master_as_explicit_voice(tmp_path):
    master = str(_touch(tmp_path / "x.mixed.mp3"))
    with pytest.raises(EditError):
        edit.resolve_voice(
            {"voice": {"path": master, "offset": 0.0, "volume": 1.0}},
            base=str(tmp_path / "x"), render_doc=None, master_path=master,
        )


def test_render_entry_is_single_mezzanine_segment(tmp_path):
    mezz = str(_touch(tmp_path / "x.mezzanine.mp4"))
    entry, warnings = edit.render_entry(
        mezz, edit_duration=8.0, timeline_duration=8.0,
    )
    assert entry["type"] == "video" and entry["path"] == mezz
    assert entry["start"] == 0.0 and entry["end"] == 8.0
    assert entry["fade"] == {"in": 0.0, "out": 0.0}
    assert warnings == []


def test_render_entry_clamps_end_and_warns_both_ways(tmp_path):
    mezz = str(_touch(tmp_path / "x.mezzanine.mp4"))
    long_entry, long_warnings = edit.render_entry(
        mezz, edit_duration=12.0, timeline_duration=8.0,
    )
    assert long_entry["end"] == 8.0
    assert any("比时间线" in item and "长" in item for item in long_warnings)
    short_entry, short_warnings = edit.render_entry(
        mezz, edit_duration=4.0, timeline_duration=8.0,
    )
    assert short_entry["end"] == 4.0
    assert any("露出背景" in item for item in short_warnings)
    # 差在容差内不打扰
    _entry, quiet = edit.render_entry(mezz, edit_duration=8.05, timeline_duration=8.0)
    assert quiet == []


def test_ensure_outputs_absent_blocks_without_force(tmp_path):
    existing = _touch(tmp_path / "x.mezzanine.mp4")
    with pytest.raises(EditError) as exc:
        edit.ensure_outputs_absent([str(existing)], force=False)
    assert "--force" in str(exc.value)
    edit.ensure_outputs_absent([str(existing)], force=True)
    edit.ensure_outputs_absent([str(tmp_path / "none.mp4")], force=False)


# ---------------------------------------------------------------------------
# 画幅校验 / 报告 / 命令拼装
# ---------------------------------------------------------------------------

def test_canvas_from_timeline_accepts_matching_and_rejects_mismatch():
    tl = Timeline(meta={"fps": 30, "width": 1920, "height": 1080})
    assert edit.canvas_from_timeline(tl, {"fps": 30, "width": 1920, "height": 1080}) == (
        30, 1920, 1080,
    )
    assert edit.canvas_from_timeline(tl, {"fps": None}) == (30, 1920, 1080)
    with pytest.raises(EditError) as exc:
        edit.canvas_from_timeline(tl, {"fps": 60})
    assert "fps" in str(exc.value)


def test_verify_mezzanine_reports_picture_problems(tmp_path, monkeypatch):
    fake = str(_touch(tmp_path / "x.mezzanine.mp4"))
    monkeypatch.setattr(edit.compose, "probe_dimensions", lambda *_a: (1920, 1080))
    monkeypatch.setattr(edit.compose, "probe_has_audio", lambda *_a: False)
    assert edit.verify_mezzanine("ffprobe", fake, width=1920, height=1080) == []

    monkeypatch.setattr(edit.compose, "probe_dimensions", lambda *_a: (1080, 1920))
    monkeypatch.setattr(edit.compose, "probe_has_audio", lambda *_a: True)
    warnings = edit.verify_mezzanine("ffprobe", fake, width=1920, height=1080)
    assert any("画幅" in item for item in warnings)
    assert any("音轨" in item for item in warnings)

    def _boom(*_args):
        raise RuntimeError("ffprobe 读取失败")

    monkeypatch.setattr(edit.compose, "probe_dimensions", _boom)
    assert "自检失败" in edit.verify_mezzanine("ffprobe", fake, width=1, height=1)[0]


def test_output_base_defaults_to_timeline_and_strips_media_ext(tmp_path):
    timeline = str(tmp_path / "x.timeline.json")
    assert edit.output_base(None, timeline) == str(tmp_path / "x")
    assert edit.output_base(str(tmp_path / "y.mp4"), timeline) == str(tmp_path / "y")
    assert edit.output_base(str(tmp_path / "z"), timeline) == str(tmp_path / "z")


def test_subs_path_from_prefers_recorded_path(tmp_path):
    assert edit.subs_path_from(None, str(tmp_path / "x")).endswith(".subs.webm")
    recorded = str(tmp_path / "custom.webm")
    assert edit.subs_path_from({"subtitle": {"path": recorded}}, str(tmp_path / "x")) == recorded


def _report_ctx(tmp_path):
    plan = edit.plan_edit(_plan3(tmp_path))
    clip_meta = [
        {"index": i, "source_duration": 6.7, "has_audio": i != 2}
        for i in range(len(plan["clips"]))
    ]
    return {
        "plan": plan,
        "clip_meta": clip_meta,
        "timeline_path": "x.timeline.json",
        "timeline_duration": 5.0,
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "voice_path": "voice.mp3",
        "mezzanine": "x.mezzanine.mp4",
        "master": "x.mixed.mp3",
        "report": "x.edit.report.json",
        "render": "x.render.json",
        "backup": str(tmp_path / "x.render.json.bak"),
        "mezzanine_duration": 5.2,
        "master_duration": 5.0,
        "loudness": {
            "target_lufs": -14.0, "true_peak": -1.5, "passes": 2, "mode": "linear",
            "before": {"i": -20.8, "tp": -13.3, "lra": 1.6},
            "after": {"i": -14.0, "tp": -6.7, "lra": 1.1},
        },
        "warnings": ["示例告警"],
    }


def test_build_report_contains_durations_transitions_loudness(tmp_path):
    report = edit.build_report(_report_ctx(tmp_path))
    assert report["duration"] == {
        "edit": 5.1, "mezzanine": 5.2, "master": 5.0, "timeline": 5.0,
    }
    assert report["transitions"]["count"] == 2
    assert report["transitions"]["total"] == 0.9
    assert [item["index"] for item in report["transitions"]["items"]] == [2, 3]
    assert report["clips"][0]["index"] == 1        # 报告里段号 1-based
    assert report["clips"][2]["transition"]["clamped"] is True
    assert report["clips"][2]["has_audio"] is False
    assert report["loudness"]["before"]["i"] == -20.8
    assert report["loudness"]["after"]["i"] == -14.0
    assert report["outputs"]["backup"] is None     # 备份文件不存在
    assert report["warnings"] == ["示例告警"]
    # 落盘后仍是合法 JSON
    out = tmp_path / "report.json"
    edit._write_json(str(out), report)
    assert json.loads(out.read_text(encoding="utf-8"))["version"] == edit.EDIT_JSON_VERSION


def test_summary_lines_mention_outputs_and_loudness(tmp_path):
    lines = "\n".join(edit.summary_lines(edit.build_report(_report_ctx(tmp_path))))
    assert "x.mezzanine.mp4" in lines and "无音轨" in lines
    assert "-20.8 → -14.0 LUFS" in lines
    assert "x.edit.report.json" in lines


def test_probe_clips_validates_out_against_source(tmp_path, monkeypatch):
    clips = edit.load_edit(str(_write_edit(tmp_path, {
        "clips": [{"path": str(_touch(tmp_path / "c.mp4")), "in": 0.0, "out": 10.0}],
    })))["clips"]
    monkeypatch.setattr(edit.compose, "probe_video_duration", lambda *_a: 4.0)
    monkeypatch.setattr(edit.compose, "probe_has_audio", lambda *_a: True)
    with pytest.raises(EditError) as exc:
        edit.probe_clips("ffprobe", clips)
    assert "超出素材" in str(exc.value)

    clips[0]["out"] = 4.0
    metas = edit.probe_clips("ffprobe", clips)
    assert metas == [{"index": 0, "source_duration": 4.0, "has_audio": True}]


def test_collect_clip_audio_skips_silent_sources(tmp_path):
    plan = edit.plan_edit([
        _norm(_touch(tmp_path / "clips" / "a.mp4"), out=1.0),
        _norm(_touch(tmp_path / "clips" / "b.mp4"), out=1.0, audio=False),
        _norm(_touch(tmp_path / "clips" / "c.mp4"), out=1.0),
    ])
    items, warnings = edit.collect_clip_audio(plan, {0: True, 1: True, 2: False})
    assert [item["index"] for item in items] == [0]
    assert warnings and "没有音频流" in warnings[0]


# ---------------------------------------------------------------------------
# CLI 接线
# ---------------------------------------------------------------------------

def test_parser_edit_subcommand():
    parser = build_parser()
    ns = parser.parse_args([
        "edit", "--timeline", "out/x.timeline.json", "--edit", "out/x.edit.json",
    ])
    assert ns.command == "edit"
    assert ns.timeline == "out/x.timeline.json" and ns.edit == "out/x.edit.json"
    assert ns.out is None and ns.force is False
    ns2 = parser.parse_args([
        "edit", "--timeline", "t.json", "--edit", "e.json",
        "--out", "out/y.mp4", "--force",
    ])
    assert ns2.out == "out/y.mp4" and ns2.force is True
    with pytest.raises(SystemExit):        # --timeline / --edit 必填
        parser.parse_args(["edit", "--edit", "e.json"])
    with pytest.raises(SystemExit):
        parser.parse_args(["edit", "--timeline", "t.json"])
