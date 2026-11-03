"""text2video CLI:两阶段工作流(plan 审片 → produce 产出中间件),并保留一键用法。

流水线:文本 → edge-tts(或离线估算/SAPI 本地)逐字时间轴 → 多轨对齐的
timeline.json / timeline.md / preview.html(人类可读、可单独查看)→ 确认后
produce 产出发给 Remotion 的中间件(音轨 / 透明字幕轨 / 素材片段 / render.json)。

子命令:
  plan    生成时间线产物(默认离线估算,不联网)
  produce 读取 timeline.json(或直接从文案现建),产出中间件(自检不过默认拒绝)
  edit    剪辑层:按 edit.json 拼 mezzanine 画面流 + 混一条母带,并回填 render.json
  render  出片:把 produce 的 render.json 交给内置 Remotion 渲染端(renderer/)渲染
  deliver 交付:成片 -> 横/竖/方多规格 + 封面 + 响度复核 + 音画对位校验 + 交付报告
  make    一键:把上面五个阶段串起来(plan -> produce ->(可选)edit -> render -> deliver),
          每阶段失败立即停止;纯文本路径默认给音轨加响度母带(--loudnorm)
  cover   生成封面图(背景 + 标题大字,输出 PNG)
  doctor  环境自检(ffmpeg/ffprobe/字体/edge-tts/SAPI/缓存目录/配置)
无子命令时保持一键用法(建真实时间轴 + produce 一次完成)。
参数除命令行外可来自配置文件(text2video.toml,优先级:命令行 > 配置 > 默认)。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence

from .audio import DEFAULT_TARGET_LUFS as _DEFAULT_TARGET_LUFS
from .audio import DEFAULT_TRUE_PEAK as _DEFAULT_TRUE_PEAK
from .layout import (
    auto_fontsize as _auto_fontsize,
)
from .layout import (
    auto_max_chars as _auto_max_chars,
)
from .layout import (
    resolve_font as _resolve_font,
)
from .produce import (
    annotate_crop_frames,
    produce_from_timeline,
    produce_timeline,
    synthesize_with_backend,
    write_timeline_artifacts,
)
from .sidecar import parse_formats as _parse_subs_formats
from .timeline import (
    build_timeline,
    check_material_files,
    checks_report,
    estimate_timings,
    load_materials,
)
from .tts import DEFAULT_VOICE

LOG = logging.getLogger("text2video")

# 产物基名默认值:所有产物都挂在它后面({基名}.audio.mp3 / .render.json / …)
_DEFAULT_OUT = "output"


# ---------------------------------------------------------------------------
# 参数定义(顶层一键 + plan/produce/cover 子命令共用)
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

    vid = p.add_argument_group("画幅/背景/编码")
    vid.add_argument("--out", default=_DEFAULT_OUT,
                     help=f"产物基名(默认 {_DEFAULT_OUT}):产物为 "
                          "{基名}.render.json / .audio.mp3 / .subs.webm / .material/ 等")
    vid.add_argument("--materials", metavar="JSON",
                     help="素材清单文件(assets/material/bgm 三段,见 README;"
                          "素材按段落铺满画幅并切成独立片段,可选 BGM 混音)")
    aspect = vid.add_mutually_exclusive_group()
    aspect.add_argument("--landscape", dest="aspect", action="store_const",
                        const="landscape", help="横屏 16:9 预设(1920x1080)")
    aspect.add_argument("--portrait", dest="aspect", action="store_const",
                        const="portrait", help="竖屏 9:16 预设(1080x1920)")
    aspect.add_argument("--square", dest="aspect", action="store_const",
                        const="square", help="方画幅 1:1 预设(1080x1080,默认)")
    vid.add_argument("--width", type=int, default=0, help="画幅宽(默认按预设/1080)")
    vid.add_argument("--height", type=int, default=0, help="画幅高(默认按预设/1080)")
    vid.add_argument("--fps", type=int, default=60, help="帧率(写进 render.json,素材按此帧率规整)")
    vid.add_argument("--transition", type=float, default=0.0,
                     help="素材段淡入淡出时长(秒),默认 0=硬切;写进 render.json 的 fade")
    vid.add_argument("--bg-color", default="1B1B2A",
                     help="纯色背景:写 RRGGBB(不带 0x),填进 render.json 的 background")
    vid.add_argument("--bg-image", help="背景图(绝对路径写进 render.json,由消费端铺满)")
    vid.add_argument("--crf", type=int, default=23, help="素材视频中间件的 x264 质量 18-28")
    vid.add_argument("--preset", default="medium", help="素材视频中间件的 x264 预设")
    vid.add_argument("--audio-bitrate", default="192k", help="最终音轨 mp3 码率")

    tts2 = p.add_argument_group("TTS/语音来源")
    tts2.add_argument("--tts-backend", choices=("edge", "sapi", "auto"),
                      default="auto",
                      help="语音引擎:edge=edge-tts 在线(默认);sapi=Windows 本地语音"
                           "(离线,逐字轴近似);auto=优先 edge,失败自动回退 sapi")
    tts2.add_argument("--tts-cache-dir",
                      help="TTS 缓存目录(默认 %%LOCALAPPDATA%%\\text2video\\tts_cache)")
    tts2.add_argument("--no-tts-cache", action="store_true", help="禁用 TTS 缓存(每次联网合成)")

    misc = p.add_argument_group("其他")
    misc.add_argument("--config", help="配置文件 TOML(默认查找当前目录 text2video.toml)")
    misc.add_argument("--log", default="INFO", help="日志级别 DEBUG/INFO/WARNING")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="text2video",
        description="文案转中间件:两阶段(plan 审片 → produce 产出),输入文本生成多轨对齐"
                    "时间线,确认后产出音轨 / 透明字幕轨 / 素材片段 / render.json;"
                    "render 用内置 Remotion 渲染端(renderer/)出成片"
                    "(默认 1:1 方画幅 1080x1080)。",
    )
    _add_common_args(p)
    subs = p.add_subparsers(
        title="子命令", dest="command",
        metavar="{plan, produce, edit, render, deliver, make, cover, doctor}",
    )

    sp = subs.add_parser(
        "plan", parents=[_add_common_args_p()],
        help="生成多轨对齐的时间线产物(json/md/preview.html),不做合成",
        description="生成时间线中间产物;默认离线估算时间轴(不联网)。",
    )
    sp.add_argument("--real", action="store_true",
                    help="用真实 edge-tts 时间轴(联网,较慢;默认离线估算)")

    pr = subs.add_parser(
        "produce", parents=[_add_common_args_p()],
        help="产出 Remotion 中间件:音轨 + 透明字幕轨 + 素材片段 + render.json",
        description="产出:从 timeline.json 或直接给文案,产出发给 Remotion 的中间件;"
                    "时间线自检未通过默认拒绝(可用 --force 跳过)。",
    )
    pr.add_argument("--timeline", default=None,
                    help="timeline.json 路径(与 --text/--file 二选一);缺省则从文案现建")
    pr.add_argument("--force", action="store_true", help="忽略一致性自检错误,强制产出")
    pr.add_argument("--no-sidecar-subs", action="store_true",
                    help="不额外导出外挂字幕(默认导出 {基名}.srt 与 .vtt)")
    pr.add_argument("--subs-formats", default="srt,vtt", metavar="LIST",
                    help="外挂字幕格式,逗号分隔: srt,vtt(默认两者都出)")
    pr.add_argument("--loudnorm", action="store_true",
                    help="音轨落盘后再做两遍 loudnorm 母带,让响度达标"
                         "(默认关;deliver 的响度复核就是量它)")
    pr.add_argument("--target-lufs", type=float, default=_DEFAULT_TARGET_LUFS, metavar="LUFS",
                    help=f"响度母带目标 integrated LUFS(默认 {_DEFAULT_TARGET_LUFS:g},仅 --loudnorm 时生效)")
    pr.add_argument("--true-peak", type=float, default=_DEFAULT_TRUE_PEAK, metavar="DBTP",
                    help=f"响度母带真峰值上限 dBTP(默认 {_DEFAULT_TRUE_PEAK:g},仅 --loudnorm 时生效)")
    # 与顶层共用参数集,但产物基名缺省跟着 --timeline 走(None=由 timeline 路径推导)
    pr.set_defaults(out=None)

    ed = subs.add_parser(
        "edit",
        help="剪辑层:按 edit.json 拼 mezzanine 画面流 + 混一条母带,并回填 render.json",
        description="剪辑层:读 timeline.json(时长/画幅/fps/配音)与 edit.json(段内 in/out、"
                    "变速、转场、素材原声、BGM),产出 {base}.mezzanine.mp4(**无音轨**画面流)、"
                    "{base}.mixed.mp3(配音 + 素材原声 + BGM 的母带,含 sidechain ducking 与"
                    "两遍 loudnorm)、{base}.edit.report.json;并把 {base}.render.json 回填成"
                    "单段素材(原文件先备份为 {base}.render.json.bak)。",
    )
    ed.add_argument("--timeline", required=True, metavar="JSON",
                    help="produce 产出的 {base}.timeline.json(提供时长/画幅/fps/配音)")
    ed.add_argument("--edit", required=True, metavar="JSON",
                    help="剪辑清单 edit.json(里面的相对路径按该文件所在目录解析)")
    ed.add_argument("--out", default=None, metavar="BASE",
                    help="产物基名;缺省=timeline 路径去掉 .timeline.json")
    ed.add_argument("--force", action="store_true",
                    help="覆盖已存在的 mezzanine/母带/报告(默认拒绝,防误覆盖)")
    ed.add_argument("--log", default="INFO", help="日志级别 DEBUG/INFO/WARNING")

    rd = subs.add_parser(
        "render",
        help="出片:调内置 Remotion 渲染端(renderer/render.mjs)把 render.json 渲染成 mp4",
        description="出片:把 produce 产出的 {base}.render.json 交给内置渲染端渲染整片;"
                    "node 与 renderer/ 由本命令按包根自行定位(不依赖当前目录)。",
    )
    rd.add_argument("--manifest", required=True, metavar="JSON",
                    help="produce 产出的 {base}.render.json(相对路径按当前目录解析)")
    rd.add_argument("--out", default=None, metavar="MP4",
                    help="成片路径,相对路径按当前目录解析;" \
                         "缺省用渲染端默认 renderer/out/demo.mp4")
    rd.add_argument("--stills", default=None, metavar="帧列表",
                    help="只出 PNG 静帧并透传给渲染端,如 0,120(此时不出 mp4)")
    rd.add_argument("--stills-dir", default=None, metavar="DIR",
                    help="静帧目录,相对路径按当前目录解析(缺省 renderer/out/)")
    rd.add_argument("--log", default="INFO", help="日志级别 DEBUG/INFO/WARNING")

    dl = subs.add_parser(
        "deliver",
        help="交付:成片 -> 横/竖/方多规格 + 封面 + 响度复核 + 音画对位校验 + 交付报告",
        description="把成片 mp4 加工成交付包:多规格(landscape 原样 / portrait 1080x1920 / "
                    "square 1080x1080)、封面抽帧、ebur128 响度结论、silencedetect 对位偏差,"
                    "最后写 {out}/" + "deliver.report.md。",
    )
    dl.add_argument("--video", required=True, metavar="MP4",
                    help="成片 mp4(相对路径按当前目录解析)")
    dl.add_argument("--out", default="out/deliver", metavar="DIR",
                    help="交付目录(默认 out/deliver)")
    dl.add_argument("--specs", default="landscape,portrait,square", metavar="LIST",
                    help="要交付的规格,逗号分隔: landscape,portrait,square")
    dl.add_argument("--cover-at", default="1.0", metavar="LIST",
                    help="封面抽帧时间点,逗号分隔;>=1 视为秒,0~1 视为全片比例(默认 1.0)")
    dl.add_argument("--fit", choices=("blur", "crop"), default="blur",
                    help="竖/方规格的画面适配:blur=模糊背景填充(默认,四周无黑边);"
                         "crop=居中裁切")
    dl.add_argument("--timeline", default=None, metavar="JSON",
                    help="timeline.json(可选):决定 landscape 画幅,并启用音画对位校验")
    dl.add_argument("--lufs-target", type=float, default=-14.0,
                    help="响度目标 integrated LUFS(默认 -14)")
    dl.add_argument("--dbtp-target", type=float, default=-1.5,
                    help="真峰值上限 dBTP(默认 -1.5)")
    dl.add_argument("--sync-tolerance", type=float, default=0.25,
                    help="音画对位偏差阈值(秒,默认 0.25)")
    dl.add_argument("--crf", type=int, default=20,
                    help="重编码规格的 x264 质量(默认 20;流复制路径不受影响)")
    dl.add_argument("--preset", default="medium", help="x264 预设")
    dl.add_argument("--log", default="INFO", help="日志级别 DEBUG/INFO/WARNING")

    mk = subs.add_parser(
        "make", parents=[_add_common_args_p()],
        help="一键:plan -> produce ->(可选)edit -> render -> deliver,产物与报告一次成型",
        description="一键编排:把 plan / produce / edit / render / deliver 串成一条命令,"
                    "每阶段打印耗时与产物,任一阶段失败立即停止并报出阶段名;"
                    "纯文本路径(无 --edit)默认给音轨加响度母带(--loudnorm)。",
    )
    mk.add_argument("--edit", default=None, metavar="JSON",
                    help="剪辑清单 edit.json(可省略):给了才执行 edit 阶段"
                         "(它会先回填 {base}.render.json,再由 render 出片)")
    mk.add_argument("--specs", default="landscape,portrait,square", metavar="LIST",
                    help="交付的多规格,逗号分隔: landscape,portrait,square"
                         "(透传 deliver,默认全出)")
    mk.add_argument("--cover-at", default="1.0", metavar="LIST",
                    help="封面抽帧时间点,逗号分隔(透传 deliver;>=1 视为秒,0~1 视为全片比例)")
    mk.add_argument("--fit", choices=("blur", "crop"), default="blur",
                    help="竖/方规格的画面适配(透传 deliver):blur=模糊背景填充(默认),crop=居中裁切")
    mk.add_argument("--skip", default="", metavar="LIST",
                    help="跳过的阶段,逗号分隔: plan,produce,edit,render,deliver"
                         "(跳过 plan/produce 时可直接对已有产物补跑 render+deliver)")
    mk.add_argument("--force", action="store_true",
                    help="覆盖已有产物(produce=忽略自检错误,edit=允许覆盖上一轮画面流/母带)")
    mk.add_argument("--loudnorm", action="store_true", default=None,
                    help="给 produce 的音轨加两遍 loudnorm 母带;缺省=无 --edit 时自动开、"
                         "有 --edit 时关(剪辑层的母带本身已做响度处理)")
    mk.add_argument("--target-lufs", type=float, default=_DEFAULT_TARGET_LUFS, metavar="LUFS",
                    help=f"响度目标 integrated LUFS(默认 {_DEFAULT_TARGET_LUFS:g};"
                         "同时作为 deliver 的复核目标)")
    mk.add_argument("--true-peak", type=float, default=_DEFAULT_TRUE_PEAK, metavar="DBTP",
                    help=f"真峰值上限 dBTP(默认 {_DEFAULT_TRUE_PEAK:g};"
                         "同时作为 deliver 的复核上限)")
    mk.add_argument("--sync-tolerance", type=float, default=0.25,
                    help="音画对位偏差阈值(秒,透传 deliver;默认 0.25)")
    mk.add_argument("--no-sidecar-subs", action="store_true",
                    help="不额外导出外挂字幕(透传 produce)")
    mk.add_argument("--subs-formats", default="srt,vtt", metavar="LIST",
                    help="外挂字幕格式,逗号分隔: srt,vtt(透传 produce)")

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
    """把排版/编码参数收进一份 meta dict,plan 时写入 timeline.json。"""
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
    """产物基名:剥掉扩展名(plan/produce 产物都以此为前缀)。"""
    return os.path.splitext(out)[0]


def _sidecar_formats(args: argparse.Namespace) -> tuple[str, ...]:
    """解析外挂字幕格式:--no-sidecar-subs 关掉;--subs-formats 非法时给可读报错。

    返回空元组表示"不导出"(produce 侧据此跳过写文件)。
    """
    if getattr(args, "no_sidecar_subs", False):
        return ()
    try:
        return _parse_subs_formats(getattr(args, "subs_formats", None))
    except ValueError as exc:
        raise SystemExit(f"错误: {exc}") from None


def _loudnorm_opts(args: argparse.Namespace) -> dict:
    """收集响度母带参数传给 produce(默认关;目标缺省 -14 LUFS / -1.5 dBTP)。

    参数:
        args: 命令行命名空间;老路径(顶层一键/无该选项的子命令)按 getattr 兜底。
    返回:
        {"loudnorm": bool, "target_lufs": float, "true_peak": float}。
    """
    return {
        "loudnorm": bool(getattr(args, "loudnorm", False)),
        "target_lufs": float(getattr(args, "target_lufs", _DEFAULT_TARGET_LUFS)),
        "true_peak": float(getattr(args, "true_peak", _DEFAULT_TRUE_PEAK)),
    }


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
        annotate_crop_frames(assets, material, meta)
    meta["timing_mode"] = "real" if real else "estimate"
    if real:
        audio_abs = f"{base}.mp3"
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


# ---------------------------------------------------------------------------
# 入口实现
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
        print("(估算模式,未含音频;需试听请 --real,或直接 produce 产出中间件)")
    return 0


def run_produce(args: argparse.Namespace) -> int:
    """produce 入口:--timeline 读文件产出;否则从文案现建时间线再产出(一键用法)。"""
    timeline_path = getattr(args, "timeline", None)
    if timeline_path:
        if args.text or args.file or args.stdin:
            raise SystemExit("--timeline 与 --text/--file/--stdin 不能同时使用")
        # 未给 --out 时产物落在 timeline 旁边(基名=timeline 路径去掉后缀)
        return produce_from_timeline(
            timeline_path, base=args.out, force=args.force,
            tts_backend=args.tts_backend, cache_dir=_cache_dir_arg(args),
            sidecar_formats=_sidecar_formats(args), **_loudnorm_opts(args),
        )

    text = _resolve_text(args).strip()
    if not text:
        raise SystemExit("文案为空,退出")
    base = _base_from_out(args.out or _DEFAULT_OUT)
    meta = collect_meta(args)
    assets = material = bgm = annotations = None
    if args.materials:
        assets, material, bgm, annotations = load_materials(args.materials)
        file_errors = check_material_files(assets)
        if file_errors:
            raise SystemExit("素材文件缺失:\n" + "\n".join(file_errors))
        annotate_crop_frames(assets, material, meta)
    # 这里的时间线只是"参数容器"(文本/参数/素材),真实轴由 produce 合成后重建
    meta["timing_mode"] = "estimate"
    tl = build_timeline(
        text, meta=meta,
        assets=assets, material=material, bgm=bgm, annotations=annotations,
    )
    return produce_timeline(
        tl, base=base, force=getattr(args, "force", False),
        tts_backend=args.tts_backend, cache_dir=_cache_dir_arg(args),
        precheck=False, sidecar_formats=_sidecar_formats(args),
        **_loudnorm_opts(args),
    )


def run_render(args: argparse.Namespace) -> int:
    """出片入口:把 render.json 交给内置 Remotion 渲染端(renderer/render.mjs)。

    参数:
        args: 含 manifest / out / stills / stills_dir 的命令行命名空间。

    返回值:
        int: 0 = 成功;非 0 = node 的退出码(原样作为本进程退出码)。
    """
    from .renderer import run_render as _run_renderer

    return _run_renderer(
        args.manifest, out=args.out, stills=args.stills, stills_dir=args.stills_dir,
    )


def run_deliver(args: argparse.Namespace) -> int:
    """交付入口:多规格 + 封面 + 响度复核 + 对位校验 + 报告(实现都在 deliver.py)。

    参数非法(规格/适配方式/封面时间点)转成可读报错(退出码由 SystemExit 给出);
    ffmpeg 失败抛 RuntimeError,由 main 统一打印为"错误: ..."并返回 1。
    """
    from .deliver import deliver

    try:
        return deliver(
            args.video, out=args.out, specs=args.specs, cover_at=args.cover_at,
            timeline=args.timeline, fit=args.fit,
            lufs_target=args.lufs_target, dbtp_target=args.dbtp_target,
            sync_tolerance=args.sync_tolerance, crf=args.crf, preset=args.preset,
        )
    except ValueError as exc:
        raise SystemExit(f"错误: {exc}") from None


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

    try:
        return _dispatch(args)
    except RuntimeError as exc:
        # ffmpeg/ffprobe 失败等外部原因:给可读报错,不打 traceback
        print(f"错误: {exc}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    """按子命令分发;无子命令时走一键 produce。"""
    if args.command == "plan":
        return run_plan(args)
    if args.command == "produce":
        return run_produce(args)
    if args.command == "edit":
        from .edit import run_edit as _run_edit

        return _run_edit(args)
    if args.command == "render":
        return run_render(args)
    if args.command == "deliver":
        return run_deliver(args)
    if args.command == "make":
        from .make import run_make as _run_make

        return _run_make(args)
    if args.command == "cover":
        return run_cover(args)
    if args.command == "doctor":
        return run_doctor(args)
    return run_produce(args)


def _apply_config(args: argparse.Namespace, argv: Sequence[str] | None = None) -> None:
    """把配置文件中的参数合并进 args(命令行显式值优先)。

    通过原始 argv 判断哪些参数是命令行显式给出的(即使值恰等于默认值),
    这些键不被配置覆盖;其余键仅当 args 中仍为默认值时用配置值覆盖。
    显式 --config 解析失败直接报错;默认搜索到的配置文件损坏时仅告警忽略。

    render / deliver 子命令不参与合并:它们的 --manifest / --out 是"路径"而非排版参数,
    配置里的 out(产物基名)若被套用会落成无扩展名的"成片"或错位的交付目录。
    """
    if getattr(args, "command", None) in ("render", "deliver"):
        return
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
    # 子命令 parser 的参数也在计(如 produce 的 --tts-backend / --force)
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
            # 子命令(produce/doctor)parser 本身没有的键:仅 debug 提示,不打扰
            LOG.debug("配置文件包含当前命令不支持的参数 %r,忽略", key)
            continue
        current = getattr(args, key)
        # 当前值为 None(子命令默认,如 produce 的 --tts-backend)视为未指定;
        # 否则须仍等于顶层默认值才允许配置覆盖(命令行显式 ≠ None/默认 时不覆盖)
        if current is not None and current != defaults.get(key):
            continue  # 已非默认值(如经其他配置键联动),不覆盖
        setattr(args, key, value)
        applied.append(key)
    if applied:
        LOG.info("已从配置文件应用 %d 项参数: %s", len(applied), ", ".join(applied))


if __name__ == "__main__":
    raise SystemExit(main())
