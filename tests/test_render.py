"""renderer 模块单测:参数拼装 / 路径解析 / node 缺失报错(不真跑 node)。"""

import types

import pytest

from text2video import renderer
from text2video.cli import _apply_config, build_parser


def test_render_entry_lives_in_repo_root():
    entry = renderer.render_entry()
    assert entry.name == "render.mjs"
    assert entry.parent == renderer.renderer_dir()
    assert entry.parent.name == "renderer"
    # 包根下应同时有 src/ 与 pyproject.toml(证明 parents[2] 推的是仓库根)
    assert entry.is_file()
    assert (entry.parent.parent / "pyproject.toml").is_file()


def test_build_node_argv_resolves_user_paths_against_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    argv = renderer.build_node_argv(
        "node.exe", "out/x.render.json", out="out/x.mp4",
    )
    assert argv[0] == "node.exe"
    assert argv[1] == str(renderer.render_entry())
    assert argv[2] == "--manifest"
    assert argv[3] == str(tmp_path / "out" / "x.render.json")
    assert argv[4:6] == ["--out", str(tmp_path / "out" / "x.mp4")]


def test_build_node_argv_stills_passthrough_ignores_out(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    argv = renderer.build_node_argv(
        "node.exe", "m.json", out="out/x.mp4", stills="0,120",
        stills_dir="out/s",
    )
    # 静帧模式不出 mp4:--out 不传
    assert "--out" not in argv
    assert argv[argv.index("--stills") + 1] == "0,120"
    assert argv[argv.index("--stills-dir") + 1] == str(tmp_path / "out" / "s")


def test_build_node_argv_without_optional_args_keeps_renderer_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    argv = renderer.build_node_argv("node.exe", "m.json")
    assert argv[3] == str(tmp_path / "m.json")
    assert "--out" not in argv and "--stills" not in argv


def test_build_node_argv_requires_manifest():
    with pytest.raises(ValueError):
        renderer.build_node_argv("node.exe", "")


def test_find_node_missing_gives_readable_hint(monkeypatch):
    monkeypatch.setattr(renderer.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError) as exc:
        renderer.find_node()
    msg = str(exc.value)
    assert "node" in msg and "PATH" in msg


def test_run_render_missing_entry(monkeypatch, tmp_path):
    monkeypatch.setattr(renderer, "render_entry", lambda: tmp_path / "nope.mjs")
    with pytest.raises(RuntimeError) as exc:
        renderer.run_render("m.json")
    assert "renderer" in str(exc.value)


def test_run_render_echoes_original_output_and_returns_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(renderer, "find_node", lambda: "node.exe")
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["cwd"] = argv, kwargs["cwd"]
        return types.SimpleNamespace(
            returncode=3, stdout="[render] 静帧 60 → out/s/still-60.png", stderr="boom",
        )

    monkeypatch.setattr(renderer.subprocess, "run", fake_run)
    rc = renderer.run_render("out/x.render.json", stills="60", stills_dir="out/s")
    assert rc == 3                                   # node 退出码原样上抛
    assert seen["cwd"] == str(renderer.renderer_dir())  # cwd 固定在 renderer/
    out, err = capsys.readouterr()
    assert "[render] 静帧 60" in out and "boom" in err
    assert "退出码 3" in err


def test_parser_render_subcommand_args():
    p = build_parser()
    ns = p.parse_args(["render", "--manifest", "out/x.render.json"])
    assert ns.command == "render" and ns.manifest == "out/x.render.json"
    assert ns.out is None and ns.stills is None and ns.stills_dir is None
    ns2 = p.parse_args(
        ["render", "--manifest", "m.json", "--stills", "0,120", "--stills-dir", "out/s"]
    )
    assert ns2.stills == "0,120" and ns2.stills_dir == "out/s"
    with pytest.raises(SystemExit):        # --manifest 必填
        p.parse_args(["render"])


def test_apply_config_skips_render(tmp_path, monkeypatch):
    """text2video.toml 里的 out 是产物基名,不能被 render 当成成片路径。"""
    (tmp_path / "text2video.toml").write_text('out = "out/wrong"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    args = build_parser().parse_args(["render", "--manifest", "m.json"])
    _apply_config(args, ["render", "--manifest", "m.json"])
    assert args.out is None
