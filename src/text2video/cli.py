"""text2video CLI:两阶段工作流(plan 审片 → render 渲染),并保留旧一键用法。

流水线:文本 → edge-tts(或离线估算/SAPI 本地)逐字时间轴 → 多轨对齐的
timeline.json / timeline.md / preview.html(人类可读、可单独查看)→ 确认后渲染 MP4。

子命令:
  plan   生成时间线产物(默认离线估算,不联网不渲染)
  render 读取 timeline.json,真实合成并渲染 MP4(自检不过默认拒绝)
  cover  生成封面图(背景 + 标题大字,输出 PNG)
  doctor 环境自检(ffmpeg/ffprobe/字体/edge-tts/SAPI/缓存目录/配置)
无子命令时保持旧的一键命令行(plan(realtime) + render 一次完成)。
参数除命令行外可来自配置文件(text2video.toml,优先级:命令行 > 配置 > 默认)。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence

from .layout import (
    auto_fontsize as _auto_fontsize,
)
from .layout import (
    auto_max_chars as _auto_max_chars,
)
from .layout import (
    resolve_font as _resolve_font,
)
from .render import render_from_timeline, write_timeline_artifacts
from .timeline import (
    build_timeline,
    check_material_files,
    checks_report,
    estimate_timings,
    load_materials,
)
from .tts import DEFAULT_VOICE

LOG = logging.getLogger("text2video")


# ---------------------------------------------------------------------------
# 参数定义(顶层一键 + plan 子命令共用)
# ---------------------------------------------------------------------------

def _add_common_args(p: argparse.ArgumentParser) -> None:
    src = p.add_argument_group("文本输入(三选一)")
    src.add_argument("-t", "--text", help="直接传入要转的文案")
    src.add_argument("-f", "--file", help="读取文案文件(UTF-8)")
    p.add_argument("--stdin", action="store_true", help="从标准输入读取文案")

    tts = p.add_argument_group("语音(TTS)")
    tts.add_argument("-v", "--voice", default=DEFAULT_VOICE, help=f"edge-tts 音色,默认 {DEFAULT_VOICE}")
    tts.add_argument("--rate", default="+0%", help="语速,如 +10%% / -10%%")
    tts.add_argument("--volume", default="+0%", help="音量,如 +20%%")
    tts.add_argument("--pitch", default="+0Hz", help="音调,如 +5Hz")
    tts.add_argument("--list-voices", action="store_true", help="列出可用中文音色后退出")

    sub = p.add_argument_group("字幕样式")
    sub.add_argument(
        "--style", choices=("karaoke", "scroll"), default="karaoke",
        help="karaoke=逐字点亮滚动高亮(默认);scroll=整段上滚走字幕",
    )
    sub.add_argument("--font", help="ASS 字体家族名,默认按系统字体自动推断")
    sub.add_argument("--font-size", type=int, default=0, help="字号,默认按画面高度自适应")
    sub.add_argument("--max-chars", type=int, default=0, help="一行最多中文数,默认自动")
    sub.add_argument("--highlight-color", default="0xFFFFFF", help="已念部分颜色,如 0xFFD580")
    sub.add_argument("--dim-color", default="0x9A9A9A", help="未念部分颜色")
    sub.add_argument("--margin-v", type=int, default=140, help="字幕距底部像素")
    sub.add_argument("--outline", type=int, default=3, help="描边宽度")
    sub.add_argument("--shadow", type=int, default=1, help="阴影宽度")
    sub.add_argument("--box", action="store_true", help="字幕加半透明底色框")

    vid = p.add_argument_group("画面/合成")
    vid.add_argument("--out", default="output.mp4", help="输出路径(plan 时作为产物基名)")
    vid.add_argument("--materials", metavar="JSON",
                     help="素材清单文件(assets/material/bgm 三段,见 README;"
                          "素材按段落全屏覆盖 + 可选 BGM 混音)")
    aspect = vid.add_mutually_exclusive_group()
    aspect.add_argument("--landscape", dest="aspect", action="store_const",
                        const="landscape", help="横屏 16:9 预设(1920x1080)")
    aspect.add_argument("--portrait", dest="aspect", action="store_const",
                        const="portrait", help="竖屏 9:16 预设(1080x1920)")
    aspect.add_argument("--square", dest="aspect", action="store_const",
                        const="square", help="方画幅 1:1 预设(1080x1080,默认)")
    vid.add_argument("--width", type=int, default=0, help="画幅宽(默认按预设/1080)")
    vid.add_argument("--height", type=int, default=0, help="画幅高(默认按预设/1080)")
    vid.add_argument("--fps", type=int, default=60)
    vid.add_argument("--transition", type=float, default=0.0,
                     help="素材段淡入淡出过渡时长(秒),默认 0=硬切")
    vid.add_argument("--bg-color", default="1B1B2A",
                     help="纯色背景 0xRRGGBB(写 RRGGBB);未指定 --bg-image 时生效")
    vid.add_argument("--bg-image", help="背景图(缩放裁切铺满画面);指定后优先生效")
    vid.add_argument("--crf", type=int, default=23, help="x264 质量(小=清晰) 18-28")
    vid.add_argument("--preset", default="medium", help="x264 预设")
    vid.add_argument("--audio-bitrate", default="192k", help="输出音频码率")
    vid.add_argument("--split-tracks", action="store_true",
                     help="渲染后额外输出纯画面(video.mp4)与纯音频(audio.mp3)")

    tts2 = p.add_argument_group("TTS/语音来源")
    tts2.add_argument("--tts-backend", choices=("edge", "sapi", "auto"),
                      default="auto",
                      help="语音引擎:edge=edge-tts 在线(默认);sapi=Windows 本地语音"
                           "(离线,逐字轴近似);auto=优先 edge,失败自动回退 sapi")
    tts2.add_argument("--tts-cache-dir",
                      help="TTS 缓存目录(默认 %LOCALAPPDATA%\\text2video\\tts_cache)")
    tts2.add_argument("--no-tts-cache", action="store_true", help="禁用 TTS 缓存(每次联网合成)")

    misc = p.add_argument_group("其他")
    misc.add_argument("--config", help="配置文件 TOML(默认查找当前目录 text2video.toml)")
    misc.add_argument("--skip-intermediate", action="store_true",
                      help="合成成功后删除中间文件(mp3/ass)")
    misc.add_argument("--log", default="INFO", help="日志级别 DEBUG/INFO/WARNING")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="text2video",
        description="文案转视频:两阶段(plan 审片 → render 渲染),输入文本生成多轨对齐"
                    "时间线,确认后合成短视频(默认 1:1 方画幅 1080x1080)。",
    )
    _add_common_args(p)
    subs = p.add_subparsers(title="子命令", dest="command", metavar="{plan, render}")

    sp = subs.add_parser(
        "plan", parents=[_add_common_args_p()],
        help="生成多轨对齐的时间线产物(json/md/preview.html),不渲染视频",
        description="生成时间线中间产物;默认离线估算时间轴(不联网)。",
    )
    sp.add_argument("--real", action="store_true",
                    help="用真实 edge-tts 时间轴(联网,较慢;默认离线估算)")

    r = subs.add_parser(
        "render",
        help="读取 timeline.json,真实合成并渲染最终 MP4",
        description="渲染:从 timeline.json 读取全部参数,真实 TTS 并对齐渲染;"
                    "自检未通过默认拒绝(可用 --force 跳过)。",
    )
    r.add_argument("--timeline", required=True, help="timeline.json 路径")
    r.add_argument("--out", default=None, help="输出 mp4 路径(默认=timeline 基名.mp4)")
    r.add_argument("--force", action="store_true", help="忽略一致性自检错误,强制渲染")
    r.add_argument("--split-tracks", action="store_true",
                   help="额外输出纯画面/纯音频两个文件")
    r.add_argument("--tts-backend", choices=("edge", "sapi", "auto"), default=None,
                   help="覆盖 timeline 中的语音引擎(默认读 timeline)")
    r.add_argument("--no-tts-cache", action="store_true", help="禁用 TTS 缓存")
    r.add_argument("--tts-cache-dir", help="TTS 缓存目录")
    r.add_argument("--skip-intermediate", action="store_true",
                   help="渲染后删除中间文件(mp3/ass)")
    r.add_argument("--config", default=None,
                   help="配置文件 TOML(仅渲染相关键生效,如 tts_backend/缓存目录)")
    r.add_argument("--log", default="INFO", help="日志级别 DEBUG/INFO/WARNING")

    cv = subs.add_parser(
        "cover",
        parents=[_add_common_args_p()],
        help="生成封面图(16:9 默认):背景 + 标题大字,输出 PNG",
        description="从文案(或 timeline.json)生成封面图;默认尺寸 1920x1080。",
    )
    cv.add_argument("--timeline", default=None, help="读取 timeline.json 的画幅/背景/字体(可加 --title)")
    cv.add_argument("--title", default=None, help="封面标题文字(默认=文案首句)")
    cv.add_argument("--subtitle", default=None, help="封面副标题(可选)")

    dc = subs.add_parser(
        "doctor",
        help="环境自检:ffmpeg/ffprobe/字体/edge-tts/SAPI/缓存目录/配置",
        description="诊断本机 text2video 运行环境,输出 PASS/WARN/FAIL 项。",
    )
    dc.add_argument("--online", action="store_true", help="额外测试 edge-tts 网络连通")
    dc.add_argument("--config", default=None, help="指定要检查的配置文件(默认自动查找)")
    dc.add_argument("--log", default="INFO", help="日志级别 DEBUG/INFO/WARNING")
    return p


def _add_common_args_p() -> argparse.ArgumentParser:
    """返回一个不带 help 的共享参数 parser,作为 plan/cover 的 parents(画幅/字体/背景等)。"""
    p = argparse.ArgumentParser(add_help=False)
    _add_common_args(p)
    return p


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )


def _resolve_text(args: argparse.Namespace) -> str:
    if args.list_voices:
        return ""
    sources = [bool(args.text), bool(args.file), bool(args.stdin)]
    if sum(sources) != 1:
        raise SystemExit("必须且只能指定一种文本来源: --text / --file / --stdin")
    if args.text:
        return args.text
    if args.file:
        with open(args.file, encoding="utf-8-sig") as f:
            return f.read()
    return sys.stdin.read()


def _print_voices() -> None:
    try:
        from .tts import list_voices
    except ImportError:
        return
    rows = list_voices("")
    zh = [v for v in rows if v.get("Locale", "").startswith("zh")] or rows
    print(f"{'音色名':<42}{'性别':<8}风格")
    for v in zh:
        tags = v.get("VoiceTag", {}) or {}
        cats = ", ".join(tags.get("ContentCategories", []))
        print(f"{v['ShortName']:<42}{v.get('Gender',''):<8}{cats}")


_ASPECT_PRESETS = {
    "landscape": (1920, 1080),
    "portrait": (1080, 1920),
    "square": (1080, 1080),
}
_DEFAULT_CANVAS = (1080, 1080)


def resolve_canvas(width: int, height: int, aspect: str | None) -> tuple[int, int]:
    """按 显式宽高 > 画幅预设 > 默认(1080x1080) 解析最终画幅。

    只给宽或高之一时,另一边按预设/默认补全,保证画幅与预设一致。
    """
    pw, ph = _ASPECT_PRESETS.get(aspect or "", _DEFAULT_CANVAS)
    return (width or pw), (height or ph)


def collect_meta(args: argparse.Namespace) -> dict:
    """把渲染/排版参数收进一份 meta dict,plan 时写入 timeline.json。"""
    width, height = resolve_canvas(args.width, args.height, args.aspect)
    fontsize = args.font_size or _auto_fontsize(width, height)
    _font_path, font_name = _resolve_font(args.font)  # 未指定时取系统字体的家族名
    return {
        "voice": args.voice, "rate": args.rate, "volume": args.volume,
        "pitch": args.pitch,
        "width": width, "height": height, "fps": args.fps,
        "transition": max(0.0, float(args.transition or 0.0)),
        "tts_backend": args.tts_backend,
        "bg_color": args.bg_color,
        "bg_image": os.path.abspath(args.bg_image) if args.bg_image else None,
        "style": args.style,
        "font": font_name,
        "font_size": fontsize,
        "max_chars": args.max_chars or _auto_max_chars(width, fontsize),
        "highlight_color": args.highlight_color,
        "dim_color": args.dim_color,
        "outline": args.outline, "shadow": args.shadow,
        "margin_v": args.margin_v, "box": args.box,
        "crf": args.crf, "preset": args.preset,
        "audio_bitrate": args.audio_bitrate,
    }


def _base_from_out(out: str) -> str:
    """输出基名:剥掉扩展名(plan 产物基名 / legacy 的 mp4 stem)。"""
    return os.path.splitext(out)[0]


def _plan_core(text: str, base: str, meta: dict, *, real: bool,
               materials_path: str | None = None, cache_dir: str | None = None):
    """核心 plan:构建并落盘时间线产物。返回 (tl, timings, audio_ref)。

    real=True 时真实 TTS 并同步产出 {base}.mp3(供 preview 试听)。
    materials_path 非空时读取素材清单(assets/material/bgm/annotations)并校验
    文件存在;带 crop 的素材段探测分辨率,把裁剪窗口几何(相对比例)写入
    data.frame,供审片在素材图上标注窗口位置(探测失败仅告警,不阻塞 plan)。
    cache_dir 非空时启用 TTS 缓存(命中跳过联网合成)。
    """
    meta = dict(meta)
    audio_ref = None
    assets = material = bgm = annotations = None
    if materials_path:
        assets, material, bgm, annotations = load_materials(materials_path)
        file_errors = check_material_files(assets)
        if file_errors:
            raise SystemExit("素材文件缺失:\n" + "\n".join(file_errors))
        _annotate_crop_frames(assets, material, meta)
    meta["timing_mode"] = "real" if real else "estimate"
    if real:
        audio_abs = f"{base}.mp3"
        from .render import synthesize_with_backend

        timings, used_backend = synthesize_with_backend(
            text, meta=meta, out_audio=audio_abs,
            cache_dir=cache_dir, backend_override=meta.get("tts_backend"),
        )
        meta["tts_backend"] = used_backend
        audio_ref = os.path.basename(audio_abs)
    else:
        timings = estimate_timings(text)
    tl = build_timeline(
        text, timings=timings, meta=meta,
        assets=assets, material=material, bgm=bgm, annotations=annotations,
    )
    write_timeline_artifacts(tl, base, audio_ref=audio_ref)
    return tl, timings, audio_ref


def _annotate_crop_frames(
    assets: list[dict], material: list[dict], meta: dict,
) -> None:
    """为带 crop 的素材段探测分辨率,回填 data.frame(窗口相对素材的比例)。

    frame 供审片标注(素材图上画框),纯展示用途;渲染时仍自行探测并计算像素。
    探测失败(无 ffmpeg/ffprobe、素材损坏)仅告警,不阻塞 plan。
    """
    from . import compose
    from .render import crop_frame

    by_id = {a["id"]: a for a in assets}
    out_w = int(meta.get("width", 1080))
    out_h = int(meta.get("height", 1080))
    try:
        ffmpeg = compose.find_ffmpeg()
        ffprobe = compose.find_ffprobe(ffmpeg)
    except RuntimeError as exc:
        LOG.warning("未找到 ffmpeg/ffprobe(审片窗口标注缺失): %s", exc)
        return
    for seg in material:
        crop = seg.get("crop")
        if not crop:
            continue
        asset = by_id.get(str(seg.get("asset", "")))
        if not asset or asset["type"] not in ("video", "image"):
            continue
        try:
            src_w, src_h = compose.probe_dimensions(ffprobe, asset["path"])
        except RuntimeError as exc:
            LOG.warning("素材 %r 分辨率探测失败(审片窗口标注缺失): %s",
                        asset["id"], exc)
            continue
        fx, fy, fw, fh = crop_frame(
            src_w, src_h, out_w, out_h,
            float(crop.get("x", 0.5)), float(crop.get("y", 0.5)),
        )
        seg["frame"] = {"x": round(fx, 4), "y": round(fy, 4),
                        "w": round(fw, 4), "h": round(fh, 4)}


# ---------------------------------------------------------------------------
# 三个入口
# ---------------------------------------------------------------------------

def default_tts_cache_dir() -> str:
    """默认 TTS 缓存目录:%LOCALAPPDATA%\\text2video\\tts_cache(可被环境变量覆盖)。"""
    env = os.environ.get("TEXT2VIDEO_TTS_CACHE")
    if env:
        return env
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "text2video", "tts_cache")


def _cache_dir_arg(args: argparse.Namespace) -> str | None:
    """从 args 解析缓存目录:--no-tts-cache → None;否则显式目录或默认目录。"""
    if getattr(args, "no_tts_cache", False):
        return None
    return args.tts_cache_dir or default_tts_cache_dir()


def run_plan(args: argparse.Namespace) -> int:
    text = _resolve_text(args).strip()
    if not text:
        raise SystemExit("文案为空,退出")
    base = _base_from_out(args.out)
    meta = collect_meta(args)
    tl, _timings, _audio_ref = _plan_core(
        text, base, meta, real=bool(args.real), materials_path=args.materials,
        cache_dir=_cache_dir_arg(args),
    )
    print(checks_report(tl.checks))
    print(f"时间线产物: {base}.timeline.json / {base}.timeline.md / {base}.preview.html")
    if not args.real:
        print("(估算模式,未含音频;需试听请 --real,或用 render 渲染后复核)")
    return 0


def run_render(args: argparse.Namespace) -> int:
    return render_from_timeline(
        args.timeline,
        out=args.out,
        skip_intermediate=args.skip_intermediate,
        force=args.force,
        tts_backend=args.tts_backend,
        cache_dir=_cache_dir_arg(args),
        split_tracks=args.split_tracks,
    )


def run_legacy(args: argparse.Namespace) -> int:
    """旧一键用法:plan(realtime) + render 一次完成,中间产物默认保留。"""
    text = _resolve_text(args).strip()
    if not text:
        raise SystemExit("文案为空,退出")
    base = _base_from_out(args.out)
    meta = collect_meta(args)
    cache_dir = _cache_dir_arg(args)
    tl, timings, _audio_ref = _plan_core(
        text, base, meta, real=True, materials_path=args.materials,
        cache_dir=cache_dir,
    )
    print(checks_report(tl.checks))
    return render_from_timeline(
        f"{base}.timeline.json", out=args.out,
        skip_intermediate=args.skip_intermediate, force=False, timings=timings,
        tts_backend=args.tts_backend, cache_dir=cache_dir,
        split_tracks=args.split_tracks,
    )


def run_cover(args: argparse.Namespace) -> int:
    """生成封面图:背景 + 标题大字,输出 PNG(默认 1920x1080 横屏)。"""
    from . import cover
    from .timeline import Timeline

    timeline_path = getattr(args, "timeline", None)
    if timeline_path:
        tl = Timeline.from_json(timeline_path)
        meta = dict(tl.meta)
        text = tl.text
        meta.setdefault("width", 1920)
        meta.setdefault("height", 1080)
        title = args.title or (text.splitlines()[0][:20] if text.splitlines() else "")
    else:
        text = _resolve_text(args).strip()
        if not text:
            raise SystemExit("文案为空,无法生成封面")
        meta = collect_meta(args)
        title = args.title or (text.splitlines()[0][:20] if text.splitlines() else "")
    if not args.title and timeline_path is None:
        LOG.info("未指定 --title,使用文案首句: %r", title)
    width = int(meta.get("width", 1920))
    height = int(meta.get("height", 1080))
    out = args.out
    if out.lower().endswith(".png") or out.lower().endswith(".jpg"):
        out_png = out
    else:
        # 基名(剥掉 .mp4 等扩展名)+ .cover.png
        out_png = os.path.splitext(out)[0] + ".cover.png"
    cover.render_cover(
        text=title,
        out=out_png,
        width=width, height=height,
        bg_color=meta.get("bg_color", "1B1B2A"),
        bg_image=meta.get("bg_image"),
        font=meta.get("font"),
        subtitle=args.subtitle,
    )
    print(f"封面图: {out_png}")
    return 0


def run_doctor(args: argparse.Namespace) -> int:
    from .doctor import run_all_checks

    results = run_all_checks(online=args.online, config_path=args.config)
    status_rank = {"PASS": 0, "WARN": 1, "FAIL": 2}
    for name, status, detail in results:
        print(f"[{status:<4}] {name}: {detail}")
    worst = max(status_rank[s] for _n, s, _d in results)
    print(f"检查完成: {sum(1 for r in results if r[1]=='PASS')} PASS / "
          f"{sum(1 for r in results if r[1]=='WARN')} WARN / "
          f"{sum(1 for r in results if r[1]=='FAIL')} FAIL")
    return 0 if worst == 0 else 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    argv_list = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv_list)
    _apply_config(args, argv_list)
    _configure_logging(args.log)

    if getattr(args, "list_voices", False):
        _print_voices()
        return 0

    if args.command == "plan":
        return run_plan(args)
    if args.command == "render":
        return run_render(args)
    if args.command == "cover":
        return run_cover(args)
    if args.command == "doctor":
        return run_doctor(args)
    return run_legacy(args)


def _apply_config(args: argparse.Namespace, argv: Sequence[str] | None = None) -> None:
    """把配置文件中的参数合并进 args(命令行显式值优先)。

    通过原始 argv 判断哪些参数是命令行显式给出的(即使值恰等于默认值),
    这些键不被配置覆盖;其余键仅当 args 中仍为默认值时用配置值覆盖。
    显式 --config 解析失败直接报错;默认搜索到的配置文件损坏时仅告警忽略。
    """
    from .config import find_config, load_config

    parser = build_parser()
    explicit = getattr(args, "config", None)
    try:
        path = find_config(explicit)
        cfg = load_config(path) if path else {}
    except (FileNotFoundError, ValueError, TypeError) as exc:
        if explicit:
            raise SystemExit(f"配置文件错误: {exc}")
        LOG.warning("忽略配置文件: %s", exc)
        return
    if not cfg:
        return
    # 扫描 argv 中显式出现的参数 dest(值与默认相同时也算显式)
    option_to_dest: dict[str, str] = {}
    for action in parser._actions:
        for opt in action.option_strings:
            option_to_dest[opt] = action.dest
    # 子命令 parser 的参数也在计(如 render 的 --tts-backend / --split-tracks)
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                for a in sub._actions:
                    for opt in a.option_strings:
                        option_to_dest[opt] = a.dest
    explicit_keys: set[str] = set()
    tokens = list(argv or [])
    for i, tok in enumerate(tokens):
        if tok.startswith("-"):
            name = tok.split("=", 1)[0]
            dest = option_to_dest.get(name)
            if dest:
                explicit_keys.add(dest)
    defaults = vars(parser.parse_args([]))
    applied: list[str] = []
    for key, value in cfg.items():
        if key in explicit_keys:
            continue  # 命令行显式指定,配置不覆盖
        if not hasattr(args, key):
            # 子命令(render/doctor)parser 本身没有的键:仅 debug 提示,不打扰
            LOG.debug("配置文件包含当前命令不支持的参数 %r,忽略", key)
            continue
        current = getattr(args, key)
        # 当前值为 None(子命令默认,如 render 的 --tts-backend)视为未指定;
        # 否则须仍等于顶层默认值才允许配置覆盖(命令行显式 ≠ None/默认 时不覆盖)
        if current is not None and current != defaults.get(key):
            continue  # 已非默认值(如经其他配置键联动),不覆盖
        setattr(args, key, value)
        applied.append(key)
    if applied:
        LOG.info("已从配置文件应用 %d 项参数: %s", len(applied), ", ".join(applied))


if __name__ == "__main__":
    raise SystemExit(main())
