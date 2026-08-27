"""渲染模块:消费 timeline.json,真实合成最终 MP4。

与 plan 解耦的原则:
- render 只从 timeline.json 读取"文本 / 结构 / 渲染参数",不做任何新的排版决策;
- 逐字时间轴必须真实(edge-tts 或本地 SAPI),因为 mp4 的字幕对齐要求精确;
- 渲染后把 timeline.json / md / preview.html 刷新成"真实时间轴"版本,
  并打印"估算 vs 实际"差异报告,供审片复核。
"""

from __future__ import annotations

import logging
import math
import os
import shutil

from . import compose
from .layout import auto_fontsize, auto_max_chars, resolve_font
from .report import render_preview_html, timeline_to_markdown
from .subs import build_karaoke_ass, build_scroll_ass, wrap_words
from .timeline import Timeline, build_timeline, check_material_files, validate_timeline
from .tts import synthesize
from .tts_local import synthesize_sapi

LOG = logging.getLogger(__name__)


def synthesize_with_backend(
    text: str,
    *,
    meta: dict,
    out_audio: str,
    cache_dir: str | None = None,
    backend_override: str | None = None,
) -> tuple[list, str]:
    """按语音引擎合成,返回 (timings, 实际 backend)。

    backend 优先级:显式覆盖 > timeline meta.tts_backend > auto。
    auto:优先 edge-tts(带缓存),失败后回退 SAPI 本地语音并告警。
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


def _resolve_material_crops(
    tl: Timeline, ffprobe: str,
) -> tuple[list[dict | None], list[str]]:
    """为带 crop 的素材段探测分辨率并计算像素裁剪窗口。

    返回 (逐段 crop_px 或 None, errors),与 material 段一一对位。crop_px =
    {"w","h","x","y"}(像素)。探测/计算失败归入 errors,由调用方按自检语义
    处理(--force 可跳过),不裸抛。无 crop 的段返回 None(沿用铺满裁切)。
    """
    errors: list[str] = []
    by_id = {a["id"]: a for a in tl.assets}
    out_w = int(tl.meta.get("width", 1080))
    out_h = int(tl.meta.get("height", 1080))
    result: list[dict | None] = []
    for s in tl.tracks.get("material", []):
        crop = s.data.get("crop")
        asset = by_id.get(s.text)
        if not crop or not asset or asset["type"] not in ("video", "image"):
            result.append(None)
            continue
        if not isinstance(crop, dict):
            # 手改 timeline.json 产生的畸形 crop:报错并退化满幅,不崩溃
            errors.append(f"素材 {s.text!r} crop 必须是对象,已忽略裁剪")
            result.append(None)
            continue
        try:
            w, h = compose.probe_dimensions(ffprobe, asset["path"])
        except RuntimeError as exc:
            errors.append(f"素材 {s.text!r} 分辨率探测失败: {exc}")
            result.append(None)
            continue
        try:
            cw, ch, cx, cy = compute_crop_window(
                w, h, out_w, out_h,
                float(crop.get("x", 0.5)), float(crop.get("y", 0.5)),
            )
        except (ValueError, TypeError) as exc:
            errors.append(f"素材 {s.text!r} 裁剪窗口计算失败: {exc}")
            result.append(None)
            continue
        result.append({"w": cw, "h": ch, "x": cx, "y": cy})
    return result, errors


def _probe_material_bounds(tl: Timeline, ffprobe: str) -> list[str]:
    """探测视频素材实际时长,校验 in/out 裁剪不超素材时长(渲染前放行依据)。

    探测失败(损坏/无视频流/ffprobe 不可用)归入返回的 errors,由调用方
    按自检语义处理(--force 可跳过),避免以裸异常中断。
    """
    errors: list[str] = []
    by_id = {a["id"]: a for a in tl.assets}
    for s in tl.tracks.get("material", []):
        asset = by_id.get(s.text)
        if not asset or asset["type"] != "video":
            continue
        try:
            dur = compose.probe_video_duration(ffprobe, asset["path"])
        except RuntimeError as exc:
            errors.append(f"素材 {s.text!r} 时长探测失败: {exc}")
            continue
        outp = float(s.data.get("out", 0.0))
        if outp > dur + 0.05:
            errors.append(
                f"素材 {s.text!r} 裁剪末端 {outp:.2f}s 超过素材实际时长 {dur:.2f}s"
            )
    return errors


def _is_wav(path: str) -> bool:
    """按文件头判断是否为 WAV 容器(区分 SAPI wav 与 edge-tts mp3)。"""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"RIFF"
    except OSError:
        return False


def timeline_json_to_base(path: str) -> str:
    """timeline.json 路径 -> 输出基名(默认 mp4/preview 落点)。"""
    if path.endswith(".timeline.json"):
        return path[: -len(".timeline.json")]
    return os.path.splitext(path)[0]


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


def _build_ass(meta: dict, text: str, timings, duration: float) -> str:
    """从 timeline 元信息重建 ASS 字幕(与 plan 时代替相同排版函数)。

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


def render_from_timeline(
    timeline_json: str,
    *,
    out: str | None = None,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    skip_intermediate: bool = False,
    force: bool = False,
    timings: list | None = None,
    tts_backend: str | None = None,
    cache_dir: str | None = None,
    split_tracks: bool = False,
) -> int:
    """从 timeline.json 渲染 mp4;自检不过(非 force)时拒绝渲染。

    timings 可选:legacy 一键路径可传入 plan 已合成的真实时间轴,避免二次联网。
    tts_backend:覆盖 timeline 中的语音引擎(edge/sapi/auto);缺省读 timeline。
    cache_dir:非空时启用 TTS 缓存(命中跳过合成)。
    split_tracks:渲染后额外输出 {base}.video.mp4(纯画面)与 {base}.audio.mp3(纯音频)。
    """
    tl = Timeline.from_json(timeline_json)
    ffmpeg_bin = ffmpeg or compose.find_ffmpeg()
    errors = validate_timeline(tl)
    # 素材文件缺失属硬错误(ffmpeg 必失败),不参与 --force 语义
    file_errors = check_material_files(tl.assets)
    if file_errors:
        LOG.error("素材文件缺失,无法渲染")
        for e in file_errors:
            LOG.error("  - %s", e)
        raise SystemExit(2)
    ffprobe_bin = ffprobe or compose.find_ffprobe(ffmpeg_bin)
    crop_resolved: list[dict | None] = []
    if tl.assets:  # 有素材时探测视频素材时长,超界归入自检(force 可跳过)
        errors += _probe_material_bounds(tl, ffprobe_bin)
        # 带 crop 的素材段探测分辨率并算像素裁剪窗口,失败归入自检(force 可跳过)
        crop_resolved, crop_errors = _resolve_material_crops(tl, ffprobe_bin)
        errors += crop_errors
    if errors and not force:
        LOG.error("时间线自检未通过:拒绝渲染(逐条修正或使用 --force)")
        for e in errors:
            LOG.error("  - %s", e)
        raise SystemExit(2)

    meta = tl.meta
    base = timeline_json_to_base(timeline_json)
    out_path = os.path.abspath(os.path.expanduser(out or (base + ".mp4")))
    workdir, audio_name, ass_name = compose.prepare_workdir(out_path)
    audio_abs = os.path.join(workdir, audio_name)
    ass_abs = os.path.join(workdir, ass_name)

    # ---- 真实 TTS(render 的唯一联网点;mp4 对齐必须用真实时间轴)----
    if timings:
        LOG.info("使用已合成的真实时间轴(跳过二次合成): %d 词元", len(timings))
        used_backend = meta.get("tts_backend", "edge")
    else:
        LOG.info("真实合成语音中(backend=%s)...",
                 tts_backend or meta.get("tts_backend", "auto"))
        timings, used_backend = synthesize_with_backend(
            tl.text, meta=meta, out_audio=audio_abs,
            cache_dir=cache_dir, backend_override=tts_backend,
        )
    if not timings:
        raise SystemExit("未取到时间轴,合成失败")
    duration = compose.probe_duration(ffprobe_bin, audio_abs) or timings[-1].end
    LOG.info("真实语音完成(%s): %d 词元, 时长 %.2f 秒",
             used_backend, len(timings), duration)

    # ---- 估算 vs 实际 差异报告 + 真实时间线 ----
    # duration 以 ffprobe 实测为准,确保时间线与最终 mp4 总时长一致(W2)
    # 素材轨随真实轴一并重建:素材段数据原样带入,仅重新套用真实时长
    material_dicts = [
        {"start": s.start, "end": s.end, "asset": s.text, **s.data}
        for s in tl.tracks.get("material", [])
    ]
    real_meta = dict(meta, timing_mode="real", tts_backend=used_backend)
    real_tl = build_timeline(
        tl.text, timings=timings,
        meta=real_meta, duration=duration,
        assets=tl.assets, material=material_dicts, bgm=tl.bgm,
        annotations=tl.annotations,
    )
    real_errors = validate_timeline(real_tl)
    if real_errors and not force:
        LOG.error("真实时间轴自检未通过:拒绝渲染(素材段可能超出真实总时长)")
        for e in real_errors:
            LOG.error("  - %s", e)
        raise SystemExit(2)
    est = Timeline.from_json(timeline_json)
    if est.meta.get("timing_mode") == "estimate":
        # 两段式(plan 估算 → render):打印估算 vs 实际偏差,供审片复核
        print(diff_report(est, real_tl))
    else:
        # legacy/plan --real:timeline 已是真实轴,无估算偏差可对比
        LOG.info("timeline 已是真实时间轴,无需估算对比")

    # ---- 重建 ASS 字幕 ----
    ass = _build_ass(meta, tl.text, timings, duration)
    with open(ass_abs, "w", encoding="utf-8") as f:
        f.write(ass)

    # ---- 素材:解析渲染参数 + BGM 预混(配音为主)----
    render_materials: list[dict] = []
    by_id = {a["id"]: a for a in tl.assets}
    for idx, s in enumerate(tl.tracks.get("material", [])):
        asset = by_id[s.text]
        if asset["type"] == "video":
            in_keep = float(s.data.get("in", 0.0))
            out_keep = float(s.data.get("out", in_keep + (s.end - s.start)))
            item = {"path": asset["path"], "type": asset["type"],
                    "start": s.start, "end": s.end,
                    "in": in_keep, "out": out_keep}
        else:
            item = {"path": asset["path"], "type": asset["type"],
                    "start": s.start, "end": s.end}
        crop_px = crop_resolved[idx] if idx < len(crop_resolved) else None
        if crop_px:
            # 像素裁剪窗口(已按素材分辨率与画幅比例算好),compose 据此 crop→scale
            item["crop"] = crop_px
        render_materials.append(item)
    audio_input = audio_name
    mix_abs = None
    if tl.bgm:
        bgm_asset = by_id[tl.bgm["asset"]]
        mix_name = f"{os.path.splitext(audio_name)[0]}_bgm.mp3"
        mix_abs = os.path.join(workdir, mix_name)
        LOG.info("预混 BGM %s(volume=%.2f)...", bgm_asset["path"], tl.bgm["volume"])
        compose.mix_bgm(
            ffmpeg_bin, audio_abs, bgm_asset["path"],
            volume=tl.bgm["volume"], duration=duration, out=mix_abs,
        )
        audio_input = mix_name

    # ---- 字体 + 背景 ----
    font_path, _ = resolve_font(meta.get("font") or None)
    font_in_work = None
    if font_path:
        ext = os.path.splitext(font_path)[1] or ".ttc"
        font_in_work = os.path.join(workdir, f"subfont{ext}")
        shutil.copyfile(font_path, font_in_work)
    bg_image = meta.get("bg_image") or None

    cmd = compose.build_video_cmd(
        ffmpeg_bin,
        audio=audio_input, ass=ass_name,
        out=os.path.basename(out_path),
        width=int(meta.get("width", 1080)), height=int(meta.get("height", 1080)),
        fps=int(meta.get("fps", 60)),
        bg_color=meta.get("bg_color", "1B1B2A"),
        bg_image=bg_image, duration=duration,
        materials=render_materials,
        transition=float(meta.get("transition", 0.0) or 0.0),
        crf=int(meta.get("crf", 23)), preset=meta.get("preset", "medium"),
        audio_bitrate=meta.get("audio_bitrate", "192k"),
        annotations=tl.annotations,
    )
    compose.run_ffmpeg(cmd, cwd=workdir)

    # ---- 画面 / 音轨分离(流拷贝,不重编码)----
    if split_tracks:
        video_out = os.path.splitext(out_path)[0] + ".video.mp4"
        audio_src = mix_abs or audio_abs  # 已混 BGM 时用混合产物
        # 音频按实际内容定扩展名:本地 SAPI 产出 WAV,edge-tts 产出 MP3
        audio_ext = ".wav" if _is_wav(audio_src) else ".mp3"
        audio_out = os.path.splitext(out_path)[0] + ".audio" + audio_ext
        LOG.info("分离音画: %s / %s", video_out, audio_out)
        compose.split_tracks(ffmpeg_bin, out_path, video_out)
        shutil.copyfile(audio_src, audio_out)

    if skip_intermediate:
        for p in (audio_abs, ass_abs, font_in_work, mix_abs):
            if p and os.path.exists(p):
                os.remove(p)

    # ---- 刷新为真实时间线产物(便于复核)----
    write_timeline_artifacts(
        real_tl, base,
        audio_ref=os.path.basename(audio_abs) if not skip_intermediate else None,
    )
    LOG.info("完成: %s", out_path)
    return 0


def write_timeline_artifacts(tl: Timeline, base: str, *, audio_ref: str | None) -> None:
    """落盘三件套:timeline.json(真实或估算)+ timeline.md + preview.html。"""
    os.makedirs(base and os.path.dirname(os.path.abspath(base)) or ".", exist_ok=True)
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
