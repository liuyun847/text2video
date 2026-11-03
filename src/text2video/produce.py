"""产出模块:把真实时间轴加工成「Remotion 消费的中间件」(produce 子命令的实现)。

产出(全部落在输出基名 base 旁,路径写进 render.json 的都是绝对路径):
- {base}.audio.mp3     最终音轨(配音;有 BGM 时已按 mix_bgm 语义混好)
- {base}.subs.webm     透明字幕轨(ASS 烧到 alpha 画布:VP9 / pix_fmt=yuva420p)
- {base}.material/     素材中间件(逐段裁到输出画幅:video→h264 mp4 无音轨,image→png)
- {base}.render.json   给 Remotion 的清单(契约 version 1)
- {base}.timeline.json / .timeline.md / .preview.html  刷新为真实时间轴(v5,逐字轴落盘)
- {base}.ass           字幕源文件(可复现 subs.webm;ASS 是纯文本,便于比对)

与旧 render 的区别:不再合成整片 mp4,也不做音画分离;画面渲染交给 Remotion。
ffmpeg 在这里只当"中间件生产器":量长/裁切/转码/混音/烧透明字幕。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil

from . import compose
from .layout import auto_fontsize, auto_max_chars, resolve_font
from .report import render_preview_html, timeline_to_markdown
from .subs import build_karaoke_ass, build_scroll_ass, wrap_words
from .timeline import (
    Timeline,
    build_timeline,
    check_material_files,
    validate_timeline,
)
from .tts import synthesize
from .tts_local import synthesize_sapi

LOG = logging.getLogger(__name__)

# ---- 产物命名与契约版本 ----
AUDIO_SUFFIX = ".audio.mp3"
SUBS_SUFFIX = ".subs.webm"
MATERIAL_SUFFIX = ".material"
RENDER_SUFFIX = ".render.json"
RENDER_JSON_VERSION = 1

# 透明字幕轨编码参数:VP9 + yuva420p(alpha 平面),crf 30 对纯字形足够
SUBS_CRF = 30
# 素材中间件目录里的文件命名(mNN_<素材id>.<png|mp4>):重跑时只清理符合此命名的旧文件
_MATERIAL_NAME = re.compile(r"^m\d{2,}_.*\.(?:png|mp4)$")
_SAFE_ID = re.compile(r"[^\w.\-]+")


# ---------------------------------------------------------------------------
# 路径 / 基础工具
# ---------------------------------------------------------------------------

def timeline_json_to_base(path: str) -> str:
    """timeline.json 路径 → 输出基名(剥掉 .timeline.json 或普通扩展名)。"""
    if path.endswith(".timeline.json"):
        return path[: -len(".timeline.json")]
    return os.path.splitext(path)[0]


def _is_wav(path: str) -> bool:
    """按文件头判断是否为 WAV 容器(区分 SAPI wav 与 edge-tts mp3)。"""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"RIFF"
    except OSError:
        return False


def _safe_id(asset_id: str) -> str:
    """素材 id → 可用作文件名的片段(非法字符替换为下划线)。"""
    return _SAFE_ID.sub("_", asset_id) or "asset"


# ---------------------------------------------------------------------------
# 语音合成(edge 优先,失败回退 SAPI)
# ---------------------------------------------------------------------------

def synthesize_with_backend(
    text: str,
    *,
    meta: dict,
    out_audio: str,
    cache_dir: str | None = None,
    backend_override: str | None = None,
) -> tuple[list, str]:
    """按语音引擎合成整段配音,返回 (逐字时间轴, 实际 backend)。

    backend 优先级:显式覆盖 > meta.tts_backend > auto。
    auto:优先 edge-tts(带缓存),失败后回退 SAPI 本地语音并告警。

    参数:text 文案(整段一次合成,句间停顿由 TTS 自然给出);
    out_audio 音频落点(edge 写 mp3,SAPI 写 wav,由调用方按文件头判断);
    cache_dir 非空时启用 TTS 缓存;backend_override 非空时覆盖 meta 里的引擎。
    返回:WordTiming 列表(全局秒)与实际使用的 backend 名。
    异常:edge 指定但失败时抛原异常;auto 时回退 SAPI。
    """
    preferred = (backend_override
                 or meta.get("tts_backend")
                 or "auto")
    if preferred not in ("edge", "sapi"):
        preferred = "auto"

    def _edge() -> list:
        return synthesize(
            text,
            voice=meta.get("voice"),
            rate=meta.get("rate", "+0%"),
            volume=meta.get("volume", "+0%"),
            pitch=meta.get("pitch", "+0Hz"),
            out_audio=out_audio,
            cache_dir=cache_dir,
        )

    if preferred == "edge":
        return _edge(), "edge"
    if preferred == "sapi":
        return synthesize_sapi(text, out_audio=out_audio), "sapi"
    # auto:edge 优先,失败回退 sapi
    try:
        return _edge(), "edge"
    except Exception as exc:  # noqa: BLE001 - 回退兜底,记录原异常
        LOG.warning("edge-tts 合成失败(%s),自动回退本地 SAPI 语音", exc)
        return synthesize_sapi(text, out_audio=out_audio), "sapi"


# ---------------------------------------------------------------------------
# 画面裁剪窗口(cover 满幅 + 单轴滑动)
# ---------------------------------------------------------------------------

def compute_crop_window(
    src_w: int, src_h: int, out_w: int, out_h: int, x: float, y: float,
) -> tuple[int, int, int, int]:
    """按素材分辨率与输出画幅计算像素裁剪窗口 (cw, ch, cx, cy)。

    cover 满幅 + 单轴滑动语义:素材等比缩放到一条边贴满画幅(无黑边),窗口
    尺寸 = 画幅比例的满幅窗口;x/y(0~1,默认 0.5=居中)只在素材比画幅**多出的
    那条轴**上滑动窗口位置(素材更宽→x 滑动、素材更高→y 滑动、比例一致→无需
    裁剪)。返回素材全局像素坐标,可直接作为 ffmpeg crop=w:h:x:y。
    """
    ar = out_w / out_h
    if src_w / src_h >= ar:
        cw, ch = src_h * ar, src_h   # 素材更宽:横向有盈余(x 滑动),纵向正好贴满
        cx = x * (src_w - cw)
        cy = 0.0
    else:
        cw, ch = src_w, src_w / ar   # 素材更高:纵向有盈余(y 滑动)
        cx = 0.0
        cy = y * (src_h - ch)
    # 取整后钳制到素材内,保证 ffmpeg crop 永不越界(round 误差至多 1px)
    cw = max(1, round(cw))
    ch = max(1, round(ch))
    cx = min(max(round(cx), 0), src_w - cw)
    cy = min(max(round(cy), 0), src_h - ch)
    return cw, ch, cx, cy


def crop_frame(
    src_w: int, src_h: int, out_w: int, out_h: int, x: float, y: float,
) -> tuple[float, float, float, float]:
    """裁剪窗口相对素材的比例 (fx, fy, fw, fh),供审片在素材图上标注窗口。

    与 compute_crop_window 同一 cover+滑动语义,纯比例计算(无取整误差)。
    """
    ar = out_w / out_h
    if src_w / src_h >= ar:
        fw, fh = src_h * ar / src_w, 1.0
        fx, fy = x * (1.0 - fw), 0.0
    else:
        fw, fh = 1.0, src_w / ar / src_h
        fx, fy = 0.0, y * (1.0 - fh)
    return fx, fy, fw, fh


# ---------------------------------------------------------------------------
# 素材规划:探测 + 裁剪窗口 + 时间裁剪
# ---------------------------------------------------------------------------

def plan_materials(tl: Timeline, ffprobe: str) -> tuple[list[dict], list[str]]:
    """把素材轨逐段规划成"可直接切出来的一段素材",返回 (plans, errors)。

    plans 每项:{"index", "seg", "asset", "kind", "in", "out", "length",
                "crop_px": {"w","h","x","y"} | None, "frame": {"x","y","w","h"} | None}
    带 crop 的段会探测素材分辨率算出像素窗口(切素材用)与相对窗口(审片用);
    视频段还会探测时长,校验 out 不超素材实际长度。
    探测失败(损坏/无视频流/ffprobe 不可用)归入 errors,由调用方按自检语义处理
    (--force 可跳过),不裸抛。
    """
    errors: list[str] = []
    out_w = int(tl.meta.get("width", 1080))
    out_h = int(tl.meta.get("height", 1080))
    by_id = {a["id"]: a for a in tl.assets}
    plans: list[dict] = []
    for idx, seg in enumerate(tl.tracks.get("material", [])):
        asset = by_id.get(seg.text)
        if asset is None:
            errors.append(f"素材轨第 {idx} 段引用了不存在的素材: {seg.text!r}")
            continue
        kind = asset["type"]
        if kind not in ("video", "image"):
            errors.append(f"素材轨第 {idx} 段({seg.text!r})类型 {kind!r} 不能用于画面")
            continue
        in_point = float(seg.data.get("in", 0.0) or 0.0)
        out_point = float(seg.data.get("out", 0.0) or 0.0)
        if kind == "video":
            out_point = out_point or (in_point + (seg.end - seg.start))
            try:
                src_dur = compose.probe_video_duration(ffprobe, asset["path"])
            except RuntimeError as exc:
                errors.append(f"素材 {seg.text!r} 时长探测失败: {exc}")
            else:
                if out_point > src_dur + 0.05:
                    errors.append(
                        f"素材 {seg.text!r} 裁剪末端 {out_point:.2f}s "
                        f"超过素材实际时长 {src_dur:.2f}s"
                    )
        crop_px = None
        frame = None
        crop = seg.data.get("crop")
        if crop:
            if not isinstance(crop, dict):
                # 手改 timeline.json 的畸形 crop:报错并退化满幅,不崩溃
                errors.append(f"素材 {seg.text!r} crop 必须是对象,已忽略裁剪")
            else:
                try:
                    cx = float(crop.get("x", 0.5))
                    cy = float(crop.get("y", 0.5))
                except (ValueError, TypeError):
                    errors.append(f"素材 {seg.text!r} crop 数值非法,已忽略裁剪")
                else:
                    try:
                        src_w, src_h = compose.probe_dimensions(ffprobe, asset["path"])
                    except RuntimeError as exc:
                        errors.append(f"素材 {seg.text!r} 分辨率探测失败: {exc}")
                    else:
                        cw, ch, px, py = compute_crop_window(
                            src_w, src_h, out_w, out_h, cx, cy)
                        crop_px = {"w": cw, "h": ch, "x": px, "y": py}
                        fx, fy, fw, fh = crop_frame(src_w, src_h, out_w, out_h, cx, cy)
                        frame = {"x": round(fx, 4), "y": round(fy, 4),
                                 "w": round(fw, 4), "h": round(fh, 4)}
        plans.append({
            "index": idx, "seg": seg, "asset": asset, "kind": kind,
            "in": in_point, "out": out_point,
            "length": max(0.0, seg.end - seg.start),
            "crop_px": crop_px, "frame": frame,
        })
    return plans, errors


def annotate_crop_frames(
    assets: list[dict], material: list[dict], meta: dict,
) -> None:
    """为带 crop 的素材段探测分辨率,回填 data.frame(窗口相对素材的比例)。

    plan 阶段用:frame 供 preview.html 在素材缩略图上画红框,纯展示用途;
    produce 阶段会自行探测并同时算出像素窗口。探测失败(无 ffmpeg/ffprobe、
    素材损坏)仅告警,不阻塞 plan。
    """
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
# ASS 字幕与差异报告
# ---------------------------------------------------------------------------

def build_ass(meta: dict, text: str, timings, duration: float) -> str:
    """从 timeline 元信息 + 逐字轴生成 ASS 字幕文本。

    font_size / max_chars 缺省时按宽高回退自动推导,避免手改 json 误删导致崩溃。
    """
    width = int(meta.get("width", 1080))
    height = int(meta.get("height", 1080))
    fontsize = int(meta.get("font_size") or 0) or auto_fontsize(width, height)
    max_chars = int(meta.get("max_chars") or 0) or auto_max_chars(width, fontsize)
    style = meta.get("style", "karaoke")
    font = meta.get("font") or None
    hl = int(meta.get("highlight_color", "0xFFFFFF"), 16)
    dim = int(meta.get("dim_color", "0x9A9A9A"), 16)
    outline = int(meta.get("outline", 3))
    shadow = int(meta.get("shadow", 1))
    margin_v = int(meta.get("margin_v", 140))
    box = bool(meta.get("box", False))
    _font_path, font_name = resolve_font(font)

    if style == "karaoke":
        lines = wrap_words(timings, max_chars)
        return build_karaoke_ass(
            lines, width=width, height=height,
            font=font_name, fontsize=fontsize,
            highlight_rgb=hl, dim_rgb=dim,
            outline=outline, shadow=shadow,
            margin_v=margin_v, box=box, total_duration=duration,
        )
    return build_scroll_ass(
        text, duration=duration,
        width=width, height=height,
        font=font_name, fontsize=fontsize, max_chars=max_chars,
        outline=outline, shadow=shadow, box=box,
    )


def diff_report(estimated: Timeline, real: Timeline) -> str:
    """估算 vs 真实语音轨的逐段偏差报告(秒)。"""
    es = estimated.tracks.get("speech", [])
    rs = real.tracks.get("speech", [])
    lines = ["估算 vs 实际 语音轨偏差:", "  # | 估算结束 | 实际结束 | Δ(秒)"]
    deltas = []
    for i in range(max(len(es), len(rs))):
        e = es[i].end if i < len(es) else float("nan")
        r = rs[i].end if i < len(rs) else float("nan")
        delta = r - e
        if not math.isnan(delta):
            deltas.append(abs(delta))
        lines.append(f"  {i:<2}| {e:7.2f} | {r:7.2f} | {delta:+6.2f}")
    if deltas:
        lines.append(
            f"  汇总: max Δ {max(deltas):.2f}s, avg Δ {sum(deltas)/len(deltas):.2f}s"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# render.json(冻结契约 version 1)
# ---------------------------------------------------------------------------

def _contract_path(path: str | None) -> str:
    """清单里的媒体路径:存在且非空才给绝对路径,否则给空串(消费端据此跳过该轨)。"""
    if not path:
        return ""
    abs_path = os.path.abspath(path)
    try:
        if os.path.getsize(abs_path) > 0:
            return abs_path
    except OSError:
        pass
    return ""


def build_render_json(
    tl: Timeline,
    *,
    audio_path: str,
    subs_path: str,
    materials: list[dict],
) -> dict:
    """按冻结契约组装 render.json(version 1);路径一律转绝对路径。

    materials 为已切好的素材中间件列表([{"type","path","start","end","fade"}])。
    时间单位秒(round 3);durationInFrames = round(duration * fps)。
    audio/subtitle 即使文件缺失也保留 {"path": ""} —— 消费端按"空串=该轨不挂"处理,
    键永远存在,免得 Remotion 侧判空 undefined。
    """
    meta = tl.meta
    width = int(meta.get("width", 1080))
    height = int(meta.get("height", 1080))
    fps = int(meta.get("fps", 60))
    duration = round(float(tl.duration), 3)
    bg_image = meta.get("bg_image")
    background = (
        {"type": "image", "path": os.path.abspath(str(bg_image))}
        if bg_image else
        {"type": "color", "color": str(meta.get("bg_color", "1B1B2A"))}
    )
    annotations = []
    for ann in tl.annotations:
        annotations.append({
            "start": round(float(ann.get("start", 0.0)), 3),
            "end": round(float(ann.get("end", 0.0)), 3),
            "x": round(float(ann.get("x", 0.0)), 4),
            "y": round(float(ann.get("y", 0.0)), 4),
            "w": round(float(ann.get("w", 0.0)), 4),
            "h": round(float(ann.get("h", 0.0)), 4),
            "color": str(ann.get("color", "0xFF3B30")),
        })
    return {
        "version": RENDER_JSON_VERSION,
        "fps": fps,
        "width": width,
        "height": height,
        "duration": duration,
        "durationInFrames": round(duration * fps),
        "background": background,
        "audio": {"path": _contract_path(audio_path)},
        "subtitle": {"path": _contract_path(subs_path)},
        "materials": materials,
        "annotations": annotations,
        "timing": {"mode": str(meta.get("timing_mode", "real"))},
    }


def material_entry(
    kind: str, path: str, start: float, end: float, transition: float,
) -> dict:
    """组装 render.json 的一条素材项;淡入淡出时长钳到段长一半(0=硬切)。"""
    seg_len = max(0.0, end - start)
    fade = min(transition, seg_len / 2) if transition > 0 else 0.0
    return {
        "type": kind,
        "path": os.path.abspath(path),
        "start": round(start, 3),
        "end": round(end, 3),
        "fade": {"in": round(fade, 3), "out": round(fade, 3)},
    }


# ---------------------------------------------------------------------------
# 时间线三件套落盘
# ---------------------------------------------------------------------------

def write_timeline_artifacts(tl: Timeline, base: str, *, audio_ref: str | None) -> None:
    """落盘三件套:timeline.json + timeline.md + preview.html。

    audio_ref 为相对音频名(有音频时 preview.html 可直接试听),None 走虚拟时钟。
    """
    out_dir = os.path.dirname(os.path.abspath(base))
    os.makedirs(out_dir, exist_ok=True)
    json_path = f"{base}.timeline.json"
    md_path = f"{base}.timeline.md"
    html_path = f"{base}.preview.html"
    tl.to_json(json_path)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(timeline_to_markdown(tl, title=os.path.basename(base)))
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_preview_html(
            tl, audio_ref=audio_ref, title=os.path.basename(base),
        ))
    LOG.info("时间线产物: %s / %s / %s", json_path, md_path, html_path)


# ---------------------------------------------------------------------------
# 产出主流程
# ---------------------------------------------------------------------------

def _stage_font(font: str | None, out_dir: str, stem: str) -> str | None:
    """把系统中文字体复制到输出目录下的临时子目录,返回相对目录名(供 fontsdir)。

    fontsdir 会在该目录里逐个文件尝试注册为内嵌字体,故必须独占目录(否则
    字幕文件、素材文件都会被当成字体而报"Error opening memory font")。
    调用方用完负责删除该临时目录。
    """
    font_path, _family = resolve_font(font)
    if not font_path:
        return None
    dirname = f"{stem}.fonts"
    target = os.path.join(out_dir, dirname)
    shutil.rmtree(target, ignore_errors=True)
    os.makedirs(target, exist_ok=True)
    shutil.copyfile(font_path, os.path.join(target, os.path.basename(font_path)))
    return dirname


def _clean_material_dir(mat_dir: str) -> None:
    """清理素材中间件目录里上一轮产物(只删 mNN_<id>.<png|mp4>,不碰别人的文件)。"""
    for name in os.listdir(mat_dir):
        if _MATERIAL_NAME.match(name):
            try:
                os.remove(os.path.join(mat_dir, name))
            except OSError as exc:  # 被占用时只告警,不阻断产出
                LOG.warning("旧素材中间件删除失败(忽略): %s", exc)


def produce_timeline(
    tl: Timeline,
    *,
    base: str,
    force: bool = False,
    tts_backend: str | None = None,
    cache_dir: str | None = None,
    precheck: bool = True,
) -> int:
    """把一份时间线(或"只当参数容器"的时间线)产出成 Remotion 中间件。

    参数:tl 时间线 —— 提供 text / meta / assets / material / bgm / annotations;
            base 输出基名(绝对或相对,产物都以其为前缀);
            force 忽略时间线自检错误(素材文件缺失仍硬失败);
            tts_backend / cache_dir 覆盖语音引擎与缓存目录;
            precheck=False 跳过输入时间线自检(一键路径的输入是估算轴容器,
            真实轴建好后仍会完整自检)。
    返回:0 成功;自检不过抛 SystemExit(2);ffmpeg/ffprobe 失败抛 RuntimeError。
    """
    base = os.path.abspath(os.path.expanduser(base))
    out_dir = os.path.dirname(base)
    stem = os.path.basename(base)
    os.makedirs(out_dir, exist_ok=True)

    ffmpeg = compose.find_ffmpeg()
    ffprobe = compose.find_ffprobe(ffmpeg)
    meta = dict(tl.meta)
    width = int(meta.get("width", 1080))
    height = int(meta.get("height", 1080))
    fps = int(meta.get("fps", 60))
    transition = float(meta.get("transition", 0.0) or 0.0)
    bitrate = str(meta.get("audio_bitrate", "192k"))
    crf = int(meta.get("crf", 23))
    preset = str(meta.get("preset", "medium"))
    by_id = {a["id"]: a for a in tl.assets}

    # ---- 输入自检:素材文件缺失属硬错误(ffmpeg 必失败),不吃 --force ----
    file_errors = check_material_files(tl.assets)
    if file_errors:
        LOG.error("素材文件缺失,无法产出")
        for e in file_errors:
            LOG.error("  - %s", e)
        raise SystemExit(2)
    errors = validate_timeline(tl) if precheck else []
    plans, plan_errors = plan_materials(tl, ffprobe)
    errors += plan_errors
    if errors and not force:
        LOG.error("时间线自检未通过:拒绝产出(逐条修正或使用 --force)")
        for e in errors:
            LOG.error("  - %s", e)
        raise SystemExit(2)

    # ---- 1) 真实 TTS(本流程的唯一联网点) ----
    voice_tmp = os.path.join(out_dir, f"{stem}.voice.tmp")
    LOG.info("真实合成语音中(backend=%s)...", tts_backend or meta.get("tts_backend", "auto"))
    timings, used_backend = synthesize_with_backend(
        tl.text, meta=meta, out_audio=voice_tmp,
        cache_dir=cache_dir, backend_override=tts_backend,
    )
    if not timings:
        raise SystemExit("未取到时间轴,合成失败")
    voice_duration = compose.probe_duration(ffprobe, voice_tmp)
    LOG.info("语音完成(%s): %d 词元, 时长 %.2f 秒",
             used_backend, len(timings), voice_duration)

    # ---- 2) 最终音轨:有 BGM 先混,否则直接规整成 mp3 ----
    audio_path = base + AUDIO_SUFFIX
    try:
        if tl.bgm:
            bgm_asset = by_id.get(str(tl.bgm.get("asset", "")))
            if bgm_asset is None:
                raise SystemExit(f"bgm 引用了不存在的素材: {tl.bgm.get('asset')!r}")
            LOG.info("预混 BGM %s(volume=%.2f)...", bgm_asset["path"], tl.bgm["volume"])
            compose.mix_bgm(
                ffmpeg, voice_tmp, bgm_asset["path"],
                volume=float(tl.bgm["volume"]), duration=voice_duration,
                out=audio_path, bitrate=bitrate,
            )
        elif _is_wav(voice_tmp):
            # SAPI 产出的是 WAV:统一转成 mp3,成品音轨只有一种容器
            compose.transcode_audio(ffmpeg, voice_tmp, audio_path, bitrate=bitrate)
        else:
            shutil.copyfile(voice_tmp, audio_path)
        duration = compose.probe_duration(ffprobe, audio_path)
    finally:
        # 无论成败都不留中间音频
        if os.path.exists(voice_tmp):
            os.remove(voice_tmp)
    LOG.info("最终音轨: %s(%.3f 秒)", audio_path, duration)

    # ---- 3) 真实时间轴(v5:逐字轴落盘)+ 估算偏差报告 ----
    material_dicts: list[dict] = []
    for plan in plans:
        seg = plan["seg"]
        d = {"start": seg.start, "end": seg.end, "asset": seg.text, **seg.data}
        if plan["frame"]:
            d["frame"] = plan["frame"]
        material_dicts.append(d)
    real_meta = dict(meta, timing_mode="real", tts_backend=used_backend)
    real_tl = build_timeline(
        tl.text, timings=timings, meta=real_meta, duration=duration,
        assets=tl.assets, material=material_dicts, bgm=tl.bgm,
        annotations=tl.annotations,
    )
    real_errors = validate_timeline(real_tl)
    if real_errors and not force:
        LOG.error("真实时间轴自检未通过:拒绝产出(素材段可能超出真实总时长)")
        for e in real_errors:
            LOG.error("  - %s", e)
        raise SystemExit(2)
    if meta.get("timing_mode") == "estimate":
        # 两段式(plan 估算 → produce):打印偏差,供最终复核
        print(diff_report(tl, real_tl))

    # ---- 4) ASS 字幕(纯文本中间产物)+ 透明字幕轨 ----
    ass_name = f"{stem}.ass"
    with open(os.path.join(out_dir, ass_name), "w", encoding="utf-8") as f:
        f.write(build_ass(meta, tl.text, timings, duration))
    subs_name = f"{stem}.subs.webm"
    fonts_dir = _stage_font(meta.get("font") or None, out_dir, stem)
    try:
        compose.run_ffmpeg(
            compose.build_subtitle_track_cmd(
                ffmpeg, ass=ass_name, out=subs_name,
                width=width, height=height, fps=fps,
                duration=duration, fontsdir=fonts_dir, crf=SUBS_CRF,
            ),
            cwd=out_dir,
        )
    finally:
        if fonts_dir:
            shutil.rmtree(os.path.join(out_dir, fonts_dir), ignore_errors=True)
    subs_path = base + SUBS_SUFFIX
    if not compose.probe_alpha_mode(ffprobe, subs_path):
        raise RuntimeError(
            "透明字幕轨缺少 alpha_mode 标记:当前 ffmpeg 的 libvpx-vp9 可能不支持 "
            "yuva420p(需要带 alpha 的 VP9 编码器),请升级 ffmpeg"
        )
    LOG.info("透明字幕轨: %s(%dx%d @ %dfps, %.3f 秒, VP9/yuva420p)",
             subs_path, width, height, fps, duration)

    # ---- 5) 素材中间件(逐段裁到输出画幅;无素材时不留空目录) ----
    mat_dir = base + MATERIAL_SUFFIX
    entries: list[dict] = []
    if plans:
        os.makedirs(mat_dir, exist_ok=True)
        _clean_material_dir(mat_dir)
    elif os.path.isdir(mat_dir):
        # 上一轮有素材、这一轮没有:清掉旧片段,变空的目录一并删掉
        _clean_material_dir(mat_dir)
        if not os.listdir(mat_dir):
            os.rmdir(mat_dir)
    for plan in plans:
        seg = plan["seg"]
        ext = "png" if plan["kind"] == "image" else "mp4"
        name = f"m{plan['index']:02d}_{_safe_id(seg.text)}.{ext}"
        compose.run_ffmpeg(
            compose.build_material_cmd(
                ffmpeg, src=plan["asset"]["path"], out=name, kind=plan["kind"],
                width=width, height=height, fps=fps,
                in_point=plan["in"], out_point=plan["out"],
                crop=plan["crop_px"], crf=crf, preset=preset,
            ),
            cwd=mat_dir,
        )
        entries.append(material_entry(
            plan["kind"], os.path.join(mat_dir, name),
            seg.start, seg.end, transition,
        ))
        LOG.info("素材中间件 %d/%d: %s", plan["index"] + 1, len(plans), name)

    # ---- 6) render.json(冻结契约)+ 时间线三件套 ----
    render = build_render_json(
        real_tl, audio_path=audio_path, subs_path=subs_path, materials=entries,
    )
    with open(base + RENDER_SUFFIX, "w", encoding="utf-8") as f:
        json.dump(render, f, ensure_ascii=False, indent=2)
        f.write("\n")
    write_timeline_artifacts(real_tl, base, audio_ref=os.path.basename(audio_path))

    LOG.info("产出完成(%d 帧 @ %dfps): %s", render["durationInFrames"], fps, base)
    return 0


def produce_from_timeline(
    timeline_json: str,
    *,
    base: str | None = None,
    force: bool = False,
    tts_backend: str | None = None,
    cache_dir: str | None = None,
) -> int:
    """从 timeline.json 产出中间件;base 缺省=timeline 路径剥掉 .timeline.json。"""
    tl = Timeline.from_json(timeline_json)
    return produce_timeline(
        tl,
        base=base or timeline_json_to_base(timeline_json),
        force=force, tts_backend=tts_backend, cache_dir=cache_dir,
    )
