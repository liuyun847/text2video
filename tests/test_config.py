"""config 模块单测:TOML 加载、查找顺序、参数合并优先级。"""

import pytest

from text2video.config import find_config, load_config


def test_load_config_reads_text2video_section(tmp_path):
    p = tmp_path / "text2video.toml"
    p.write_text(
        '[text2video]\nvoice = "zh-CN-YunxiNeural"\nwidth = 1920\n'
        'bg_color = "112233"\n\n[other]\nx = 1\n',
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    assert cfg == {"voice": "zh-CN-YunxiNeural", "width": 1920,
                   "bg_color": "112233"}


def test_load_config_missing_section_returns_empty(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[other]\nx = 1\n", encoding="utf-8")
    assert load_config(str(p)) == {}


def test_load_config_bad_toml_raises(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text("not [ valid", encoding="utf-8")
    with pytest.raises(ValueError, match="解析失败"):
        load_config(str(p))


def test_find_config_explicit_and_default(tmp_path, monkeypatch):
    explicit = tmp_path / "x.toml"
    explicit.write_text("[text2video]\n", encoding="utf-8")
    assert find_config(str(explicit)) == str(explicit)
    with pytest.raises(FileNotFoundError):
        find_config(str(tmp_path / "nope.toml"))
    # 默认:当前目录下的 text2video.toml
    monkeypatch.chdir(tmp_path)
    assert find_config(None) is None
    default = tmp_path / "text2video.toml"
    default.write_text("[text2video]\n", encoding="utf-8")
    assert find_config(None) == str(default)


def test_cli_config_merge_precedence(tmp_path, monkeypatch):
    """命令行显式参数 > 配置文件 > 内置默认(显式=默认值也算显式)。"""
    from text2video.cli import _apply_config, build_parser

    cfg = tmp_path / "text2video.toml"
    cfg.write_text(
        "[text2video]\nvoice = \"zh-CN-YunxiNeural\"\nbg_color = \"112233\"\n"
        "transition = 0.5\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    # 未显式指定 → 配置生效
    argv = ["--text", "你好"]
    args = build_parser().parse_args(argv)
    _apply_config(args, argv)
    assert args.voice == "zh-CN-YunxiNeural"
    assert args.bg_color == "112233"
    assert args.transition == 0.5
    # 显式指定 → 命令行优先(哪怕值恰等于默认值)
    argv = ["--text", "你好", "--voice", "zh-CN-XiaoxiaoNeural"]
    args = build_parser().parse_args(argv)
    _apply_config(args, argv)
    assert args.voice == "zh-CN-XiaoxiaoNeural"
    assert args.bg_color == "112233"
    assert args.transition == 0.5


def test_cli_config_bad_explicit_raises(tmp_path):
    from text2video.cli import _apply_config, build_parser

    bad = tmp_path / "bad.toml"
    bad.write_text("[[[", encoding="utf-8")
    argv = ["--text", "你好", "--config", str(bad)]
    args = build_parser().parse_args(argv)
    with pytest.raises(SystemExit, match="配置文件错误"):
        _apply_config(args, argv)


def test_render_parser_accepts_config_and_ignores_unused_keys(tmp_path, monkeypatch, caplog):
    """render 子命令有 --config;配置中的渲染相关键(tts_backend)生效,无关键不告警。"""
    from text2video.cli import _apply_config, build_parser

    cfg = tmp_path / "text2video.toml"
    cfg.write_text(
        "[text2video]\nvoice = \"zh-CN-YunxiNeural\"\nbg_color = \"112233\"\n"
        "transition = 0.5\ntts_backend = \"sapi\"\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    argv = ["render", "--timeline", "x.timeline.json", "--config", str(cfg)]
    args = build_parser().parse_args(argv)
    assert args.command == "render"
    assert args.config == str(cfg)
    # render 只消费 tts_backend 等渲染相关键;voice/bg_color 等仅存在于顶层
    # namespace、render 不使用,应用后不影响渲染行为,也不应刷"未知参数"告警
    _apply_config(args, argv)
    assert args.tts_backend == "sapi"
    assert not any(r.levelname == "WARNING" for r in caplog.records)


def test_render_explicit_backend_beats_config(tmp_path, monkeypatch):
    """render 命令行显式 --tts-backend 时,配置不覆盖(子命令参数也要计入显式扫描)。"""
    from text2video.cli import _apply_config, build_parser

    cfg = tmp_path / "text2video.toml"
    cfg.write_text('[text2video]\ntts_backend = "sapi"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    argv = ["render", "--timeline", "x.timeline.json",
            "--tts-backend", "edge", "--config", str(cfg)]
    args = build_parser().parse_args(argv)
    _apply_config(args, argv)
    assert args.tts_backend == "edge"


def test_doctor_parser_accepts_config():
    from text2video.cli import build_parser

    args = build_parser().parse_args(["doctor", "--config", "my.toml"])
    assert args.command == "doctor"
    assert args.config == "my.toml"


def test_cli_config_section_not_table_raises_or_ignored(tmp_path, monkeypatch):
    """[text2video] 是字符串(非法表)时,显式 --config 报错,默认搜索告警忽略。"""
    from text2video.cli import _apply_config, build_parser

    bad = tmp_path / "bad.toml"
    bad.write_text('text2video = "not a table"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    argv = ["--text", "你好", "--config", str(bad)]
    args = build_parser().parse_args(argv)
    with pytest.raises(SystemExit, match="配置文件错误"):
        _apply_config(args, argv)
    # 无显式 --config 时(默认搜索到该文件)直接忽略不崩溃
    argv2 = ["--text", "你好"]
    args2 = build_parser().parse_args(argv2)
    _apply_config(args2, argv2)  # 不抛异常