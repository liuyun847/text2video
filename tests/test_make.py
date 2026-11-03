"""make(一键编排)单测:阶段规划/跳过/参数传递/产物总表(不跑真 ffmpeg 与 TTS)。"""

import os

import pytest

from text2video import cli, make
from text2video import deliver as deliver_mod
from text2video.cli import build_parser


def _parse(*argv):
    """用真实 parser 造一个 make 命名空间(便于验证参数形状)。"""
    return build_parser().parse_args(["make", *argv])


def _ctx(tmp_path, **over):
    """直接造 MakeContext(单测各阶段的参数传递用)。"""
    paths = make.make_paths(str(tmp_path / "mk"))
    fields = {
        "args": _parse("--text", "你好", "--out", str(tmp_path / "mk")),
        "paths": paths,
        "text": "你好",
        "edit": None,
        "loudnorm": True,
        "target_lufs": -14.0,
        "true_peak": -1.5,
        "force": False,
        "specs": "landscape,portrait,square",
        "cover_at": "1.0",
        "fit": "blur",
        "sync_tolerance": 0.25,
    }
    fields.update(over)
    return make.MakeContext(**fields)


# ---------------------------------------------------------------------------
# 参数形状
# ---------------------------------------------------------------------------

def test_parser_defaults():
    ns = _parse("--text", "你好")
    assert ns.command == "make"
    assert ns.out == "output"
    assert ns.edit is None
    assert ns.specs == "landscape,portrait,square"
    assert ns.cover_at == "1.0"
    assert ns.fit == "blur"
    assert ns.skip == ""
    assert ns.force is False
    # --loudnorm 是三态:None=未指定(按有无剪辑层自动决定)
    assert ns.loudnorm is None
    assert ns.target_lufs == -14.0
    assert ns.true_peak == -1.5
    assert ns.sync_tolerance == 0.25
    assert ns.subs_formats == "srt,vtt" and ns.no_sidecar_subs is False
    # 画幅/编码参数与 produce 同名同义(共同参数集)
    assert ns.fps == 60 and ns.aspect is None and ns.crf == 23


# ---------------------------------------------------------------------------
# 阶段规划(纯函数)
# ---------------------------------------------------------------------------

def test_parse_skip_normalizes_and_dedupes():
    assert make.parse_skip("plan, produce ,plan") == ("plan", "produce")
    assert make.parse_skip("") == ()
    assert make.parse_skip(None) == ()
    assert make.parse_skip(["edit", " edit "]) == ("edit",)


def test_parse_skip_unknown_raises():
    with pytest.raises(SystemExit, match="未知阶段"):
        make.parse_skip("plan,nope")


def test_plan_stages_without_edit_marks_edit_skipped():
    stages = make.plan_stages(has_edit=False)
    assert [name for name, _run in stages] == list(make.STAGES)
    assert dict(stages) == {
        "plan": True, "produce": True, "edit": False, "render": True, "deliver": True,
    }


def test_plan_stages_with_edit_runs_all_five():
    assert all(run for _name, run in make.plan_stages(has_edit=True))


def test_plan_stages_honours_skip():
    stages = dict(make.plan_stages(skip=("plan", "produce"), has_edit=False))
    assert stages["plan"] is False and stages["produce"] is False
    assert stages["render"] is True and stages["deliver"] is True


def test_plan_stages_rejects_edit_with_skip_edit():
    with pytest.raises(SystemExit, match="冲突"):
        make.plan_stages(skip=("edit",), has_edit=True)


def test_plan_stages_rejects_skipping_everything():
    with pytest.raises(SystemExit, match="全部阶段"):
        make.plan_stages(skip=make.STAGES, has_edit=False)


def test_skip_reason_distinguishes_missing_edit_from_skip():
    assert "未给 --edit" in make.skip_reason("edit", has_edit=False)
    assert make.skip_reason("render", has_edit=True) == "--skip"


def test_loudnorm_switch_auto_by_edit():
    # 无剪辑层:自动开(纯文本路径响度也要达标);有剪辑层:自动关(母带自己处理)
    assert make.loudnorm_switch(None) is True
    assert make.loudnorm_switch(None, has_edit=True) is False
    # 显式指定时以命令行为准
    assert make.loudnorm_switch(True, has_edit=True) is True
    assert make.loudnorm_switch(False) is False


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def test_make_paths_are_absolute_with_expected_suffixes(tmp_path):
    paths = make.make_paths(str(tmp_path / "out" / "demo.mp4"))
    assert paths.base == os.path.join(str(tmp_path), "out", "demo")
    assert paths.timeline == paths.base + ".timeline.json"
    assert paths.render_json == paths.base + ".render.json"
    assert paths.video == paths.base + ".mp4"
    assert paths.deliver_dir == paths.base + ".deliver"


def test_resolve_base_defaults_to_output():
    assert make.resolve_base(None) == os.path.abspath("output")
    assert make.resolve_base("  ") == os.path.abspath("output")
    assert make.resolve_base("out/x.mkv") == os.path.abspath(os.path.join("out", "x"))


def test_sidecar_suffixes_respect_flags():
    assert make.sidecar_suffixes(False, "srt,vtt") == (".srt", ".vtt")
    assert make.sidecar_suffixes(False, "vtt") == (".vtt",)
    assert make.sidecar_suffixes(False, None) == (".srt", ".vtt")
    assert make.sidecar_suffixes(True, "srt,vtt") == ()


# ---------------------------------------------------------------------------
# 产物总表
# ---------------------------------------------------------------------------

def test_artifact_rows_lists_deliver_outputs(tmp_path):
    paths = make.make_paths(str(tmp_path / "mk"))
    for path in (paths.video, paths.timeline, paths.render_json):
        with open(path, "wb") as f:
            f.write(b"x")
    for suffix in (".srt", ".vtt"):
        with open(paths.base + suffix, "wb") as f:
            f.write(b"x")
    os.makedirs(paths.deliver_dir, exist_ok=True)
    for name in ("mk-landscape.mp4", "mk-portrait.mp4", "cover-01-1.00s.png", "deliver.report.md"):
        with open(os.path.join(paths.deliver_dir, name), "wb") as f:
            f.write(b"x")
    rows = make.artifact_rows(paths, has_edit=False)
    labels = [label for label, _path, _ok in rows]
    assert labels[0] == "成片"
    assert labels.count("多规格") == 2 and labels.count("封面") == 1
    assert labels.count("字幕") == 2 and "交付报告" in labels
    assert "画面流" not in labels                      # 没跑 edit 就不列剪辑层产物
    assert all(ok for _label, _path, ok in rows)


def test_artifact_rows_marks_missing_artifacts(tmp_path):
    paths = make.make_paths(str(tmp_path / "mk"))
    rows = make.artifact_rows(paths, has_edit=True, subs_suffixes=())
    assert {label: ok for label, _path, ok in rows}["成片"] is False
    assert "画面流" in [label for label, _path, _ok in rows]
    assert "字幕" not in [label for label, _path, _ok in rows]


# ---------------------------------------------------------------------------
# 编排:顺序 / 跳过 / 失败即停
# ---------------------------------------------------------------------------

def _record_stages(monkeypatch, calls, *, fail=None):
    """把 5 个阶段都换成记录器;fail=(阶段名, 异常) 让该阶段抛错。"""
    def fake(name):
        def runner(ctx):
            calls.append((name, ctx))
            if fail and fail[0] == name:
                raise fail[1]
            return f"{name}-ok"
        return runner

    for name in make.STAGES:
        monkeypatch.setattr(make, f"stage_{name}", fake(name))


def test_run_make_runs_stages_in_order_without_edit(tmp_path, monkeypatch, capsys):
    calls = []
    _record_stages(monkeypatch, calls)
    code = make.run_make(_parse("--text", "你好", "--out", str(tmp_path / "mk")))
    assert code == 0
    assert [name for name, _ctx in calls] == ["plan", "produce", "render", "deliver"]
    assert calls[0][1].paths.base == os.path.join(str(tmp_path), "mk")
    assert calls[0][1].loudnorm is True                 # 无剪辑层:自动加母带
    out = capsys.readouterr().out
    assert "[1/5] plan …" in out and "完成" in out
    assert "[3/5] edit 跳过(未给 --edit" in out
    assert "产物总表:" in out


def test_run_make_with_edit_runs_edit_before_render(tmp_path, monkeypatch, capsys):
    calls = []
    _record_stages(monkeypatch, calls)
    edit_json = tmp_path / "edit.json"
    edit_json.write_text("{}", encoding="utf-8")
    code = make.run_make(_parse(
        "--text", "你好", "--out", str(tmp_path / "mk"), "--edit", str(edit_json),
    ))
    assert code == 0
    assert [name for name, _ctx in calls] == list(make.STAGES)
    ctx = calls[0][1]
    assert ctx.edit == os.path.abspath(str(edit_json))
    assert ctx.loudnorm is False                        # 有剪辑层:母带自己处理响度
    assert f"剪辑清单: {ctx.edit}" in capsys.readouterr().out


def test_run_make_skips_requested_stages(tmp_path, monkeypatch, capsys):
    calls = []
    _record_stages(monkeypatch, calls)
    code = make.run_make(_parse(
        "--out", str(tmp_path / "mk"), "--skip", "plan,produce",
    ))
    assert code == 0
    # 跳过 plan/produce 时不需要文案
    assert [name for name, _ctx in calls] == ["render", "deliver"]
    assert "[1/5] plan 跳过(--skip)" in capsys.readouterr().out


def test_run_make_stops_at_first_failure(tmp_path, monkeypatch, capsys):
    calls = []
    _record_stages(monkeypatch, calls, fail=("produce", RuntimeError("语音合成炸了")))
    code = make.run_make(_parse("--text", "你好", "--out", str(tmp_path / "mk")))
    assert code == 1
    assert [name for name, _ctx in calls] == ["plan", "produce"]   # 后续阶段不再执行
    err = capsys.readouterr().err
    assert "在阶段 produce(2/5)失败" in err
    assert "语音合成炸了" in err


def test_run_make_keeps_systemexit_code(tmp_path, monkeypatch, capsys):
    calls = []
    _record_stages(monkeypatch, calls, fail=("produce", SystemExit(2)))
    code = make.run_make(_parse("--text", "你好", "--out", str(tmp_path / "mk")))
    assert code == 2
    assert "退出码 2" in capsys.readouterr().err


def test_run_make_passes_readable_reason_for_missing_edit(tmp_path, monkeypatch, capsys):
    # edit 阶段缺失清单时,edit.run_edit 会抛 EditError("edit.json 不存在: ...")
    from text2video.edit import EditError

    calls = []
    _record_stages(monkeypatch, calls)
    monkeypatch.setattr(make, "stage_edit", lambda ctx: (_ for _ in ()).throw(
        EditError(f"edit.json 不存在: {ctx.edit}")))
    edit_json = tmp_path / "nope.json"
    code = make.run_make(_parse(
        "--text", "你好", "--out", str(tmp_path / "mk"), "--edit", str(edit_json),
    ))
    assert code == 1
    err = capsys.readouterr().err
    assert "在阶段 edit(3/5)失败" in err and "edit.json 不存在" in err


# ---------------------------------------------------------------------------
# 参数传递:各阶段拿到的命名空间
# ---------------------------------------------------------------------------

def test_stage_args_returns_copy_without_mutating_original(tmp_path):
    ctx = _ctx(tmp_path)
    ns = make.stage_args(ctx, out=ctx.paths.video)
    assert ns is not ctx.args
    assert ns.out == ctx.paths.video and ns.timeline is None
    assert ctx.args.out == str(tmp_path / "mk")        # 原始 args 未被就地修改


def test_stage_produce_passes_base_and_loudnorm(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "run_produce", lambda ns: seen.update(vars(ns)) or 0)
    ctx = _ctx(tmp_path, loudnorm=False, target_lufs=-16.0, true_peak=-2.0)
    detail = make.stage_produce(ctx)
    assert seen["out"] == ctx.paths.base and seen["timeline"] is None
    assert seen["loudnorm"] is False
    assert (seen["target_lufs"], seen["true_peak"]) == (-16.0, -2.0)
    assert ".render.json" in detail


def test_stage_plan_uses_estimate_mode(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "run_plan", lambda ns: seen.update(vars(ns)) or 0)
    make.stage_plan(_ctx(tmp_path))
    assert seen["out"] == _ctx(tmp_path).paths.base
    assert seen["real"] is False


def test_stage_edit_passes_absolute_timeline_and_edit(tmp_path, monkeypatch):
    from text2video import edit as edit_mod

    seen = {}
    monkeypatch.setattr(edit_mod, "run_edit", lambda ns: seen.update(vars(ns)) or 0)
    ctx = _ctx(tmp_path, edit=str(tmp_path / "edit.json"), force=True)
    make.stage_edit(ctx)
    assert seen["timeline"] == ctx.paths.timeline
    assert seen["edit"] == ctx.edit and seen["out"] == ctx.paths.base
    assert seen["force"] is True


def test_stage_render_passes_manifest_and_video_path(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "run_render", lambda ns: seen.update(vars(ns)) or 0)
    ctx = _ctx(tmp_path)
    with open(ctx.paths.render_json, "w", encoding="utf-8") as f:
        f.write("{}")
    assert make.stage_render(ctx) == ctx.paths.video
    assert seen["manifest"] == ctx.paths.render_json and seen["out"] == ctx.paths.video
    assert seen["stills"] is None and seen["stills_dir"] is None


def test_stage_render_fails_readably_without_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "run_render", lambda ns: 0)
    with pytest.raises(make.MakeError, match="找不到清单"):
        make.stage_render(_ctx(tmp_path))


def test_stage_render_wraps_nonzero_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "run_render", lambda ns: 3)
    ctx = _ctx(tmp_path)
    with open(ctx.paths.render_json, "w", encoding="utf-8") as f:
        f.write("{}")
    with pytest.raises(make.MakeError, match="退出码 3"):
        make.stage_render(ctx)


def test_stage_deliver_passes_specs_cover_fit_and_targets(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        deliver_mod, "deliver",
        lambda video, **kw: seen.update({"video": video, **kw}) or 0,
    )
    ctx = _ctx(tmp_path, specs="landscape", cover_at="0.5", fit="crop",
               target_lufs=-15.0, true_peak=-2.5, sync_tolerance=0.4)
    with open(ctx.paths.video, "wb") as f:
        f.write(b"x")
    assert make.stage_deliver(ctx) == ctx.paths.deliver_dir
    assert seen["video"] == ctx.paths.video
    assert seen["out"] == ctx.paths.deliver_dir
    assert seen["specs"] == "landscape" and seen["cover_at"] == "0.5" and seen["fit"] == "crop"
    assert (seen["lufs_target"], seen["dbtp_target"]) == (-15.0, -2.5)
    assert seen["sync_tolerance"] == 0.4
    # timeline 不存在时不传(跳过对位校验)
    assert seen["timeline"] is None


def test_stage_deliver_passes_timeline_when_present(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        deliver_mod, "deliver",
        lambda video, **kw: seen.update({"video": video, **kw}) or 0,
    )
    ctx = _ctx(tmp_path)
    for path in (ctx.paths.video, ctx.paths.timeline):
        with open(path, "wb") as f:
            f.write(b"x")
    make.stage_deliver(ctx)
    assert seen["timeline"] == ctx.paths.timeline


def test_stage_deliver_fails_readably_without_video(tmp_path, monkeypatch):
    monkeypatch.setattr(deliver_mod, "deliver", lambda video, **kw: 0)
    with pytest.raises(make.MakeError, match="找不到成片"):
        make.stage_deliver(_ctx(tmp_path))
