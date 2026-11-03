"""一键编排(make 子命令):plan -> produce ->(可选)edit -> render -> deliver。

定位:只做"阶段编排 + 参数传递",每个阶段都直接调用对应子命令的实现(cli.run_plan /
cli.run_produce / edit.run_edit / cli.run_render / deliver.deliver),不复制任何阶段内
逻辑;交给下游的一律是绝对路径(渲染端按 renderer/ 为根解析相对路径、交付目录按当前
目录解析,统一成绝对路径才不会落错地方)。

阶段与产物(基名 base = --out 的绝对路径,扩展名会被剥掉):
  1 plan     {base}.timeline.json / .timeline.md / .preview.html(离线估算,早期校验)
  2 produce  {base}.audio.mp3 / .subs.webm / .material/ / .render.json / .srt / .vtt
  3 edit     仅给了 --edit:{base}.mezzanine.mp4 / .mixed.mp3 / .edit.report.json,
             并把 {base}.render.json 回填为单段画面流(原文件备份 .bak)
  4 render   {base}.mp4(交给内置 Remotion 渲染端 renderer/)
  5 deliver  {base}.deliver/(多规格 + 封面 + deliver.report.md)

响度:produce --loudnorm 默认在"纯文本路径"(无 --edit)下自动打开,让 audio.mp3
两遍 loudnorm 到 -14 LUFS;有 --edit 时关掉(剪辑层的母带自己会做响度处理)。

失败语义:任一阶段失败立即停止,打印"失败于阶段 N/M: 可读原因"并以非 0 退出码
返回(SystemExit 的字符串消息、RuntimeError、ValueError 都压成一行可读原因)。
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .edit import MEZZANINE_SUFFIX, MIXED_SUFFIX
from .edit import REPORT_SUFFIX as EDIT_REPORT_SUFFIX
from .produce import RENDER_SUFFIX

LOG = logging.getLogger(__name__)

# 阶段顺序:进度里的 [i/5] 与"跳过"都以它为准(固定 5 段,不因 --edit 而变)
STAGES: tuple[str, ...] = ("plan", "produce", "edit", "render", "deliver")
TIMELINE_SUFFIX = ".timeline.json"
VIDEO_SUFFIX = ".mp4"
DELIVER_DIR_SUFFIX = ".deliver"
# 交付目录里的产物命名由 deliver 决定:多规格 {stem}-{spec}.mp4、封面 cover-NN-Xs.png
DELIVER_CATALOG: tuple[tuple[str, str], ...] = (
    ("多规格", "*-*.mp4"),
    ("封面", "cover-*.png"),
    ("交付报告", "deliver.report.md"),
)


class MakeError(RuntimeError):
    """编排层面的失败(如渲染端返回非 0 退出码);阶段内部异常原样冒泡。"""


# ---------------------------------------------------------------------------
# 路径与阶段规划(纯函数,便于单测)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MakePaths:
    """make 的产物路径表(全部绝对路径)。"""

    base: str
    timeline: str
    render_json: str
    video: str
    deliver_dir: str


def resolve_base(raw: object, default: str = "output") -> str:
    """--out 基名 -> 绝对基名(剥掉扩展名,与 plan/produce 的 --out 语义一致)。

    参数:
        raw: 用户给的基名(可带 .mp4 等扩展名,相对当前目录);default: 空值时用的基名。
    返回:
        绝对路径字符串(无扩展名)。
    """
    text = str(raw or "").strip() or default
    return os.path.splitext(os.path.abspath(os.path.expanduser(text)))[0]


def make_paths(raw_base: object) -> MakePaths:
    """按基名推出全部产物路径(成片/清单/时间线/交付目录)。

    参数:
        raw_base: --out 取值(相对当前目录或绝对,可带扩展名)。
    返回:
        MakePaths(四个派生路径均为绝对路径)。
    """
    base = resolve_base(raw_base)
    return MakePaths(
        base=base,
        timeline=base + TIMELINE_SUFFIX,
        render_json=base + RENDER_SUFFIX,
        video=base + VIDEO_SUFFIX,
        deliver_dir=base + DELIVER_DIR_SUFFIX,
    )


def parse_skip(raw: str | Sequence[str] | None) -> tuple[str, ...]:
    """解析 --skip 的逗号列表:去空白、去重、保序,未知阶段名直接报错。

    参数:
        raw: 逗号分隔字符串(或已是序列);None/空串返回空元组。
    返回:
        tuple[str, ...] 阶段名(顺序与输入一致,均已校验属于 STAGES)。
    异常:
        SystemExit —— 出现未知阶段名(带可选值提示,退出码 1)。
    """
    if not raw:
        return ()
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    names: list[str] = []
    unknown: list[str] = []
    for item in items:
        name = str(item).strip()
        if not name:
            continue
        if name not in STAGES:
            unknown.append(name)
        elif name not in names:
            names.append(name)
    if unknown:
        raise SystemExit(
            f"错误: --skip 里有未知阶段 {'、'.join(unknown)};"
            f"可选 {', '.join(STAGES)}"
        )
    return tuple(names)


def plan_stages(
    *, skip: Sequence[str] = (), has_edit: bool = False,
) -> tuple[tuple[str, bool], ...]:
    """算出 5 个阶段各自是否执行(顺序固定,便于打印 [i/5] 与"跳过")。

    参数:
        skip: 要跳过的阶段名(先过 parse_skip 校验);has_edit: 是否给了 --edit。
    返回:
        ((阶段名, 是否执行), ...) 共 5 项;edit 阶段仅在给 --edit 且未 --skip 时执行。
    异常:
        SystemExit —— 给了 --edit 又 --skip edit(清单会失效)/ 全部阶段都被跳过。
    """
    skipped = set(skip)
    if has_edit and "edit" in skipped:
        raise SystemExit(
            "错误: --edit 与 --skip edit 冲突:给了剪辑清单就要跑 edit 阶段,"
            "否则 render 会用 produce 的旧清单(要跳过 edit 就别给 --edit)"
        )
    stages: list[tuple[str, bool]] = []
    for name in STAGES:
        if name in skipped:
            stages.append((name, False))
        elif name == "edit":
            stages.append((name, bool(has_edit)))
        else:
            stages.append((name, True))
    if not any(run for _name, run in stages):
        raise SystemExit("错误: --skip 跳过了全部阶段,没有可执行的步骤")
    return tuple(stages)


def skip_reason(name: str, *, has_edit: bool = False) -> str:
    """跳过原因文案:没给剪辑清单 vs 被 --skip 显式跳过(纯函数)。

    参数:
        name: 阶段名;has_edit: 是否给了 --edit。
    返回:
        一行中文原因,拼进"[3/5] edit 跳过(...)"。
    """
    if name == "edit" and not has_edit:
        return "未给 --edit,纯文本路径不需要剪辑层"
    return "--skip"


def loudnorm_switch(flag: bool | None, *, has_edit: bool = False) -> bool:
    """决定 produce 是否给音轨加响度母带:显式 --loudnorm 优先,否则按有无剪辑层自动。

    参数:
        flag: --loudnorm 的取值(None=未指定);has_edit: 是否给了 --edit。
    返回:
        bool: True = 让 produce 跑两遍 loudnorm。
    """
    if flag is not None:
        return bool(flag)
    return not has_edit


def sidecar_suffixes(disabled: bool, formats: str | None) -> tuple[str, ...]:
    """外挂字幕的后缀名(只用于最终总表列产物;--no-sidecar-subs 时为空)。

    参数:
        disabled: 是否 --no-sidecar-subs;formats: --subs-formats 原值(缺省 srt,vtt)。
    返回:
        (".srt", ".vtt") 这类后缀元组;非法格式留给 produce 阶段自行告警。
    """
    if disabled:
        return ()
    items = [f.strip().lower() for f in str(formats or "srt,vtt").split(",") if f.strip()]
    return tuple(f".{name}" for name in items)


def artifact_rows(
    paths: MakePaths,
    *,
    has_edit: bool = False,
    subs_suffixes: Sequence[str] = (".srt", ".vtt"),
) -> list[tuple[str, str, bool]]:
    """列出本次产物(分组标签, 绝对路径, 是否存在),供最终总表打印。

    参数:
        paths: 产物路径表;has_edit: 是否跑了 edit 阶段;subs_suffixes: 外挂字幕后缀。
    返回:
        [(标签, 路径, 是否存在), ...];顺序 = 成片 -> 多规格 -> 封面 -> 字幕 ->
        交付报告 ->(剪辑层产物)-> 时间线/清单;交付目录按 deliver 的命名枚举实际文件。
    """
    rows: list[tuple[str, str, bool]] = [
        ("成片", paths.video, os.path.isfile(paths.video)),
    ]
    for label, pattern in DELIVER_CATALOG:
        for hit in sorted(glob.glob(os.path.join(paths.deliver_dir, pattern))):
            rows.append((label, hit, True))
    for suffix in subs_suffixes:
        path = paths.base + suffix
        rows.append(("字幕", path, os.path.isfile(path)))
    if has_edit:
        for label, suffix in (
            ("画面流", MEZZANINE_SUFFIX),
            ("母带", MIXED_SUFFIX),
            ("剪辑报告", EDIT_REPORT_SUFFIX),
        ):
            path = paths.base + suffix
            rows.append((label, path, os.path.isfile(path)))
    rows.append(("时间线", paths.timeline, os.path.isfile(paths.timeline)))
    rows.append(("清单", paths.render_json, os.path.isfile(paths.render_json)))
    return rows


# ---------------------------------------------------------------------------
# 阶段入口:每个阶段都只是"把参数搬到对应子命令"(不复制阶段内逻辑)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MakeContext:
    """一次 make 的上下文:原始参数 + 已解析的绝对路径与开关。"""

    args: argparse.Namespace
    paths: MakePaths
    text: str
    edit: str | None
    loudnorm: bool
    target_lufs: float
    true_peak: float
    force: bool
    specs: str
    cover_at: str
    fit: str
    sync_tolerance: float


def stage_args(ctx: MakeContext, *, out: str) -> argparse.Namespace:
    """复制一份命令行命名空间给某个阶段用(改 out/timeline 而不污染 ctx.args)。

    参数:
        ctx: make 上下文;out: 该阶段自己的 --out 语义(produce=基名,render=成片路径,
        deliver=交付目录)。
    返回:
        新的 argparse.Namespace(out 已换成 out,timeline 置空由各阶段自行填)。
    """
    ns = argparse.Namespace(**vars(ctx.args))
    ns.out = out
    ns.timeline = None
    return ns


def stage_plan(ctx: MakeContext) -> str:
    """阶段 plan:离线估算时间轴并落三件套(不联网,只做早期校验与预览)。

    参数:
        ctx: make 上下文。
    返回:
        产物简述(拼进"完成 0.4s -> ...")。
    """
    from .cli import run_plan

    ns = stage_args(ctx, out=ctx.paths.base)
    ns.real = False  # 真实时间轴由 produce 重建,这里只要估算轴(秒出、不联网)
    run_plan(ns)
    return f"{ctx.paths.timeline} + .timeline.md + .preview.html"


def stage_produce(ctx: MakeContext) -> str:
    """阶段 produce:合成配音、字幕轨、素材片段与 render.json(--loudnorm 可选)。

    参数:
        ctx: make 上下文。
    返回:
        产物简述。
    """
    from .cli import run_produce

    ns = stage_args(ctx, out=ctx.paths.base)
    ns.loudnorm = ctx.loudnorm
    ns.target_lufs = ctx.target_lufs
    ns.true_peak = ctx.true_peak
    run_produce(ns)
    return f"{ctx.paths.base}.audio.mp3 + .subs.webm + .render.json"


def stage_edit(ctx: MakeContext) -> str:
    """阶段 edit:拼画面流 + 混母带,并把 render.json 回填成单段画面流。

    参数:
        ctx: make 上下文(必须已给 --edit)。
    返回:
        产物简述。
    异常:
        edit.EditError —— 清单缺失/不合法(可读原因由 make 的失败处理统一打印)。
    """
    from .edit import run_edit

    ns = stage_args(ctx, out=ctx.paths.base)
    ns.timeline = ctx.paths.timeline
    ns.edit = ctx.edit
    ns.force = ctx.force
    run_edit(ns)
    return f"{ctx.paths.base}.mezzanine.mp4 + .mixed.mp3(render.json 已回填)"


def stage_render(ctx: MakeContext) -> str:
    """阶段 render:把 render.json 交给内置 Remotion 渲染端,出 {base}.mp4。

    参数:
        ctx: make 上下文。
    返回:
        成片绝对路径。
    异常:
        MakeError —— 清单缺失或渲染端返回非 0 退出码(它的原始输出已回显到屏幕)。
    """
    from .cli import run_render

    if not os.path.isfile(ctx.paths.render_json):
        raise MakeError(
            f"找不到清单 {ctx.paths.render_json}:produce 阶段可能被 --skip 跳过了,"
            "请先跑一次 produce(或去掉 --skip produce)"
        )
    ns = stage_args(ctx, out=ctx.paths.video)  # 渲染端的 --out 是成片文件
    ns.manifest = ctx.paths.render_json
    ns.stills = None
    ns.stills_dir = None
    code = run_render(ns)
    if code != 0:
        raise MakeError(f"渲染端返回退出码 {code}(详见上方 node 输出)")
    return ctx.paths.video


def stage_deliver(ctx: MakeContext) -> str:
    """阶段 deliver:多规格 + 封面 + 响度复核 + 音画对位 + 交付报告。

    参数:
        ctx: make 上下文。
    返回:
        交付目录绝对路径。
    异常:
        ValueError —— 规格/适配/封面时间点非法;RuntimeError —— ffmpeg 或成片不可读。
    """
    from .deliver import deliver

    if not os.path.isfile(ctx.paths.video):
        raise MakeError(
            f"找不到成片 {ctx.paths.video}:render 阶段可能被 --skip 跳过了,"
            "请先出片(或去掉 --skip render)"
        )
    deliver(
        ctx.paths.video,
        out=ctx.paths.deliver_dir,
        specs=ctx.specs,
        cover_at=ctx.cover_at,
        # 有 timeline 才做音画对位校验(它提供语音段起点)
        timeline=ctx.paths.timeline if os.path.isfile(ctx.paths.timeline) else None,
        fit=ctx.fit,
        lufs_target=ctx.target_lufs,
        dbtp_target=ctx.true_peak,
        sync_tolerance=ctx.sync_tolerance,
    )
    return ctx.paths.deliver_dir


def stage_runner(name: str) -> Callable[[MakeContext], str]:
    """按阶段名取实现(运行时查模块全局,方便测试替换单个阶段)。

    参数:
        name: STAGES 里的阶段名。
    返回:
        该阶段的执行函数。
    """
    runners: dict[str, Callable[[MakeContext], str]] = {
        "plan": stage_plan,
        "produce": stage_produce,
        "edit": stage_edit,
        "render": stage_render,
        "deliver": stage_deliver,
    }
    return runners[name]


# ---------------------------------------------------------------------------
# 编排主流程
# ---------------------------------------------------------------------------

def _edit_path(raw: object) -> str | None:
    """--edit -> 绝对路径(不校验存在性:缺失留给 edit 阶段报可读错误)。"""
    text = str(raw or "").strip()
    if not text:
        return None
    return os.path.abspath(os.path.expanduser(text))


def _read_text(args: argparse.Namespace, *, needed: bool) -> str:
    """解析文案(复用 cli 的 --text/--file/--stdin 三选一逻辑)。

    参数:
        args: 命令行命名空间;needed: 是否需要文案(plan+produce 都被跳过时不需要)。
    返回:
        文案字符串(不需要时为空串)。
    异常:
        SystemExit —— 三种来源都没给或给了多种(cli 给出可读提示)。
    """
    if not needed:
        return ""
    from .cli import _resolve_text

    text = _resolve_text(args).strip()
    if not text:
        raise SystemExit("错误: 文案为空,退出")
    return text


def _failure(name: str, idx: int, total: int, exc: BaseException) -> int:
    """把阶段异常压成"一行可读原因"+ 非 0 退出码。

    参数:
        name: 阶段名;idx/total: 进度;exc: 阶段抛出的异常。
    返回:
        int 退出码:SystemExit 的数字退出码原样返回(0/None 视作 1),其余为 1。
    """
    if isinstance(exc, SystemExit):
        code = exc.code
        if isinstance(code, str):
            detail, exit_code = code, 1
        else:
            detail = f"退出码 {code}(原因见上方日志)"
            exit_code = 1 if code in (None, 0) else int(code)
    else:
        detail = str(exc).strip() or exc.__class__.__name__
        exit_code = 1
    lines = detail.splitlines() or ["未知原因"]
    print(
        f"错误: make 在阶段 {name}({idx}/{total})失败: {lines[0]}",
        file=sys.stderr,
    )
    for extra in lines[1:]:
        print(f"  {extra}", file=sys.stderr)
    return exit_code


def print_summary(rows: Sequence[tuple[str, str, bool]]) -> None:
    """打印产物总表(缺失的标注"未产出",便于一眼看出哪个阶段没跑)。

    参数:
        rows: artifact_rows 的返回值。
    副作用:
        往 stdout 打印,不返回值。
    """
    width = max((len(label) for label, _path, _ok in rows), default=0)
    print("产物总表:")
    for label, path, exists in rows:
        mark = "" if exists else "  (未产出)"
        print(f"  {label.ljust(width)}  {path}{mark}")


def run_make(args: argparse.Namespace) -> int:
    """make 入口:按固定阶段顺序跑完整条流水线,任一阶段失败立即停止。

    参数:
        args: make 子命令的命名空间(--out/--edit/--skip/--specs/--cover-at/--fit/
        --force/--loudnorm/--target-lufs/--true-peak/--sync-tolerance 等)。
    返回:
        int: 0 = 全部执行的阶段都成功;非 0 = 失败阶段的退出码(阶段异常为 1)。
    副作用:
        各阶段按自己的语义写产物(都落在 --out 基名旁);本函数只打印进度与总表。
    """
    skip = parse_skip(getattr(args, "skip", "") or "")
    edit = _edit_path(getattr(args, "edit", None))
    has_edit = edit is not None
    stages = plan_stages(skip=skip, has_edit=has_edit)
    paths = make_paths(getattr(args, "out", None))
    loudnorm = loudnorm_switch(getattr(args, "loudnorm", None), has_edit=has_edit)
    running = [name for name, run in stages if run]
    text = _read_text(args, needed=any(n in ("plan", "produce") for n in running))
    ctx = MakeContext(
        args=args,
        paths=paths,
        text=text,
        edit=edit,
        loudnorm=loudnorm,
        target_lufs=float(getattr(args, "target_lufs", -14.0)),
        true_peak=float(getattr(args, "true_peak", -1.5)),
        force=bool(getattr(args, "force", False)),
        specs=str(getattr(args, "specs", "landscape,portrait,square")),
        cover_at=str(getattr(args, "cover_at", "1.0")),
        fit=str(getattr(args, "fit", "blur")),
        sync_tolerance=float(getattr(args, "sync_tolerance", 0.25)),
    )

    print("一键 make: " + " → ".join(running))
    print(f"产物基名: {paths.base}")
    if has_edit:
        print(f"剪辑清单: {edit}")
    print(f"音轨响度母带(produce): {'开' if loudnorm else '关'}")

    total = len(stages)
    for idx, (name, should_run) in enumerate(stages, 1):
        if not should_run:
            reason = skip_reason(name, has_edit=has_edit)
            print(f"[{idx}/{total}] {name} 跳过({reason})")
            continue
        # 先单独占一行报阶段名:阶段自己的日志/进度直接跟在下面,不会跟这行挤在一起
        print(f"[{idx}/{total}] {name} …", flush=True)
        started = time.perf_counter()
        try:
            detail = stage_runner(name)(ctx)
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - 统一转可读报错
            print(f"  失败 {time.perf_counter() - started:.1f}s")
            return _failure(name, idx, total, exc)
        print(f"  完成 {time.perf_counter() - started:.1f}s → {detail}")

    print_summary(artifact_rows(
        paths,
        has_edit=has_edit,
        subs_suffixes=sidecar_suffixes(
            bool(getattr(args, "no_sidecar_subs", False)),
            getattr(args, "subs_formats", "srt,vtt"),
        ),
    ))
    return 0
