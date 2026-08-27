"""时间线模块:把"语音 / 字幕 / 素材 / 画面"多条轨道按时间对齐,产出一份人类可读、
可审查、可复现的中间产物(timeline.json)。它是 plan → render 两阶段流程的单一事实源。

两条时间轴来源:
- 估算(estimate):离线按字数速率生成逐字轴,无需联网,用于快速排版草稿;
- 真实(real):来自 edge-tts 的 WordBoundary 逐字时间轴,渲染校验时的最终依据。

轨道说明:
- speech   语音轨:按句子聚合的段落 [{start, end, text}]
- subtitle 字幕轨:默认与语音同源(显示与朗读一致),保留独立字段以便未来分离
- visual   画面轨:场景分段(当前单场景,预留多场景扩展字段)
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from typing import Any

from .subs import WordTiming

# 句末标点:遇到即断句(中文省略号、问号、叹号、分号等)
_SENTENCE_END = set("。！？!?….;；")

# 估算语速:每个发音 token 约耗时(秒),与原 --dry-run 的 0.22s/字 一致
_SECONDS_PER_TOKEN = 0.22
_MIN_DURATION = 2.0

# 校验容差:语音轨末端与总时长允许的偏差(真实 TTS 尾部有 padding)
_DURATION_TOLERANCE = 0.6


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrackSegment:
    """一条轨道上的一个时间片段。text 语义取决于所在轨道。

    素材轨(material)额外用 data 携带素材引用:{"asset": id, "in": .., "out": ..}。
    """

    start: float
    end: float
    text: str
    data: dict = field(default_factory=dict)


@dataclass
class Timeline:
    """完整时间线:元信息 + 源文本 + 总时长 + 轨道 + 素材库 + 标注框 + 校验报告。"""

    meta: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    duration: float = 0.0
    tracks: dict[str, list[TrackSegment]] = field(default_factory=dict)
    assets: list[dict] = field(default_factory=list)   # 素材库 [{id,path,type}]
    bgm: dict | None = None                            # 背景音乐 {asset, volume}
    annotations: list[dict] = field(default_factory=list)  # 画面标注框 [{start,end,x,y,w,h,color}]
    checks: list[str] = field(default_factory=list)
    version: int = 4   # v2 起增加 assets/bgm/material 轨;v3 起素材段可带画面裁剪 crop;
                       # v4 crop 移除 zoom(cover 满幅+单轴滑动),素材段增加窗口几何 frame,
                       # 时间线增加画面标注框 annotations;旧文件仍可从 from_dict 读入

    # -- 序列化 --------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "meta": self.meta,
            "text": self.text,
            "duration": round(self.duration, 3),
            "tracks": {
                name: [asdict(s) for s in segs]
                for name, segs in self.tracks.items()
            },
            "assets": self.assets,
            "bgm": self.bgm,
            "annotations": self.annotations,
            "checks": self.checks,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Timeline:
        return cls(
            meta=dict(data.get("meta", {})),
            text=str(data.get("text", "")),
            duration=float(data.get("duration", 0.0)),
            tracks={
                name: [TrackSegment(
                           float(s["start"]), float(s["end"]), str(s["text"]),
                           dict(s.get("data", {})))
                       for s in segs]
                for name, segs in data.get("tracks", {}).items()
            },
            assets=[dict(a) for a in data.get("assets", [])],
            bgm=dict(data["bgm"]) if data.get("bgm") else None,
            annotations=[dict(a) for a in data.get("annotations", [])],
            checks=list(data.get("checks", [])),
            version=int(data.get("version", 1)),
        )

    def to_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
            f.write("\n")

    @classmethod
    def from_json(cls, path: str) -> Timeline:
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


# ---------------------------------------------------------------------------
# 素材清单(materials.json):assets + material 轨 + bgm
# ---------------------------------------------------------------------------

# 素材段的时间线区间长度 与 素材内裁剪长度 允许的最大差异(秒):
# 过大会造成 overlay 提前结束/拖尾,渲染层按此约束保证视觉确定性
_MATERIAL_CROP_TOLERANCE = 0.1

_ASSET_TYPES = ("video", "image", "audio")

# 画面裁剪 crop 字段默认值(缺失键补全);语义:cover 满幅窗口 + 单轴滑动定位
_DEFAULT_CROP = {"x": 0.5, "y": 0.5}


def _parse_crop(raw: object) -> dict[str, float]:
    """标准化素材段 crop 字段为 {"x","y"},非法输入抛 ValueError/TypeError。

    x/y:裁剪窗口左上角在素材整个画面中的比例(0~1,默认 0.5=居中)。裁剪语义为
    "cover 满幅 + 单轴滑动":素材等比缩放到一条边贴满画幅(无黑边),x/y 只在素材
    比画幅多出的那条轴上滑动窗口;素材比例与画幅一致时无需裁剪。
    旧版 zoom(放大倍数)已移除,出现即报错。数值范围校验交给 validate_materials。
    """
    if not isinstance(raw, dict):
        raise TypeError("crop 必须是对象 {x, y}")
    unknown = set(raw) - set(_DEFAULT_CROP)
    if unknown:
        raise ValueError(
            f"crop 仅支持 x/y 字段(旧版 zoom 已移除),未知字段: {sorted(unknown)}"
        )
    return {
        "x": float(raw.get("x", _DEFAULT_CROP["x"])),
        "y": float(raw.get("y", _DEFAULT_CROP["y"])),
    }


def load_materials(path: str) -> tuple[list[dict], list[dict], dict | None, list[dict]]:
    """读取素材清单 JSON,返回 (assets, material 段 dict 列表, bgm, annotations)。

    格式:
      assets:   [{"id": "a1", "path": "clips/x.mp4", "type": "video|image|audio"}]
      material: [{"start": 0.0, "end": 3.2, "asset": "a1", "in": 10.0, "out": 13.2}]
      bgm:      {"asset": "a3", "volume": 0.25}   # 可选
      annotations: [{"start": 秒, "end": 秒, "x": 0~1, "y": 0~1, "w": 0~1, "h": 0~1,
                     "color": "0xRRGGBB"}]        # 可选,画面叠加标注框(比例相对画幅)

    相对路径按本文件所在目录解析并转绝对;字段完整性在此校验,
    引用一致性/时长等交给 validate_materials / validate_annotations
    (plan 与 render 共用)。校验失败抛 ValueError。
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    base = os.path.dirname(os.path.abspath(path))
    errors: list[str] = []

    assets: list[dict] = []
    seen: set[str] = set()
    for i, a in enumerate(data.get("assets", [])):
        aid = str(a.get("id", ""))
        if not aid:
            errors.append(f"assets[{i}] 缺少 id")
        elif aid in seen:
            errors.append(f"assets 存在重复 id: {aid!r}")
        seen.add(aid)
        atype = a.get("type", "")
        if atype not in _ASSET_TYPES:
            errors.append(f"素材 {aid!r} 的 type 必须为 video/image/audio,实际 {atype!r}")
        apath = a.get("path", "")
        if not apath:
            errors.append(f"素材 {aid!r} 缺少 path")
        else:
            apath = apath if os.path.isabs(apath) else os.path.join(base, apath)
        assets.append({"id": aid, "path": apath, "type": atype})

    material: list[dict] = []
    for i, m in enumerate(data.get("material", [])):
        start = float(m.get("start", -1))
        end = float(m.get("end", -1))
        aid = str(m.get("asset", ""))
        mtype = "?"
        for a in assets:
            if a["id"] == aid:
                mtype = a["type"]
        seg: dict = {"start": start, "end": end, "asset": aid}
        if mtype == "video":
            seg["in"] = float(m.get("in", -1))
            seg["out"] = float(m.get("out", -1))
        elif mtype == "image" and ("in" in m or "out" in m):
            errors.append(f"material[{i}]: 图片素材 {aid!r} 无需 in/out,请移除")
        if "crop" in m:
            try:
                seg["crop"] = _parse_crop(m["crop"])
            except (ValueError, TypeError) as exc:
                errors.append(f"material[{i}]: crop 无效: {exc}")
        material.append(seg)

    bgm: dict | None = None
    if data.get("bgm"):
        bgm = {"asset": str(data["bgm"].get("asset", "")),
               "volume": float(data["bgm"].get("volume", 0.25))}
        if not 0 < bgm["volume"] <= 1:
            errors.append(f"bgm.volume 必须在 (0,1] 内,实际 {bgm['volume']}")

    annotations: list[dict] = []
    for i, a in enumerate(data.get("annotations", [])):
        if not isinstance(a, dict):
            errors.append(f"annotations[{i}] 必须是对象")
            continue
        try:
            ann: dict = {
                "start": float(a.get("start", -1)),
                "end": float(a.get("end", -1)),
                "x": float(a.get("x", 0.0)),
                "y": float(a.get("y", 0.0)),
                "w": float(a.get("w", 0.0)),
                "h": float(a.get("h", 0.0)),
            }
        except (ValueError, TypeError) as exc:
            errors.append(f"annotations[{i}] 数值无效: {exc}")
            continue
        color = a.get("color")
        if color is not None:
            ann["color"] = str(color)
        annotations.append(ann)

    if errors:
        raise ValueError("素材清单校验失败:\n" + "\n".join(errors))
    return assets, material, bgm, annotations


def check_material_files(assets: list[dict]) -> list[str]:
    """素材文件存在性检查(plan/render 共用),返回错误列表。"""
    missing = [a["path"] for a in assets if not os.path.isfile(a["path"])]
    return [f"素材文件不存在: {p}" for p in missing]


def validate_materials(
    assets: list[dict], material: list[TrackSegment], bgm: dict | None
) -> list[str]:
    """素材一致性自检(引用/类型/数值/重叠),返回错误列表;为空表示通过。"""
    errors: list[str] = []
    by_id = {a["id"]: a for a in assets}
    for a in assets:
        if a["type"] not in _ASSET_TYPES:
            errors.append(f"素材 {a['id']!r} 的 type 非法: {a['type']!r}")

    for i, m in enumerate(material):
        aid = m.text
        asset = by_id.get(aid)
        if asset is None:
            errors.append(f"素材轨第 {i} 段引用了不存在的素材: {aid!r}")
            continue
        d = m.data
        crop = d.get("crop")
        if crop is not None and asset["type"] != "audio":
            # 画面裁剪数值自检:audio 段带 crop 已由"只能用于 bgm"拦截,此处只管 video/image
            # 手改 timeline.json 可能产生畸形 crop(非对象/数值/旧版 zoom),一律报错而非崩溃
            if not isinstance(crop, dict):
                errors.append(f"素材轨第 {i} 段({aid!r}) crop 必须是对象 {{x, y}}")
            else:
                unknown_crop = set(crop) - {"x", "y"}
                if unknown_crop:
                    errors.append(
                        f"素材轨第 {i} 段({aid!r}) crop 仅支持 x/y(旧版 zoom 已移除),"
                        f"未知字段: {sorted(unknown_crop)}"
                    )
                try:
                    x = float(crop.get("x", _DEFAULT_CROP["x"]))
                    y = float(crop.get("y", _DEFAULT_CROP["y"]))
                except (ValueError, TypeError):
                    errors.append(f"素材轨第 {i} 段({aid!r}) crop 数值非法")
                else:
                    if not 0.0 <= x <= 1.0:
                        errors.append(f"素材轨第 {i} 段({aid!r}) crop.x 必须在 [0,1],实际 {x:g}")
                    if not 0.0 <= y <= 1.0:
                        errors.append(f"素材轨第 {i} 段({aid!r}) crop.y 必须在 [0,1],实际 {y:g}")
        if asset["type"] == "video":
            inp, outp = d.get("in", -1.0), d.get("out", -1.0)
            if inp < 0 or outp <= inp:
                errors.append(f"素材轨第 {i} 段(video {aid!r})in/out 非法: {inp}..{outp}")
            elif m.end > m.start + 0.001 and abs((outp - inp) - (m.end - m.start)) > _MATERIAL_CROP_TOLERANCE:
                errors.append(
                    f"素材轨第 {i} 段(video {aid!r})裁剪长度 {outp - inp:.2f}s "
                    f"与时间线区间 {m.end - m.start:.2f}s 不一致(容差 {_MATERIAL_CROP_TOLERANCE}s)"
                )
        elif asset["type"] == "audio":
            errors.append(f"素材轨第 {i} 段引用音频素材 {aid!r}:音频只能用于 bgm")

    # 素材段互不重叠(相邻首尾相接允许 0.01s 容差)
    order = sorted(material, key=lambda s: s.start)
    for a, b in pairwise(order):
        if b.start < a.end - 0.01:
            errors.append(
                f"素材轨段重叠: [{a.start:.2f},{a.end:.2f}] 与 [{b.start:.2f},{b.end:.2f}]"
            )

    if bgm:
        asset = by_id.get(bgm.get("asset", ""))
        if asset is None:
            errors.append(f"bgm 引用了不存在的素材: {bgm.get('asset')!r}")
        elif asset["type"] != "audio":
            errors.append(f"bgm 素材 {asset['id']!r} 必须是 audio 类型,实际 {asset['type']}")
        vol = bgm.get("volume", 0.25)
        if not 0 < vol <= 1:
            errors.append(f"bgm.volume 必须在 (0,1] 内,实际 {vol}")
    return errors


# ---------------------------------------------------------------------------
# 估算逐字时间轴(离线,不联网)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    """把文本切成"发音 token":中文单字一个、连续 ASCII 字母/数字一个、标点跳过。"""
    toks: list[str] = []
    buf: list[str] = []
    for ch in text:
        if ch.isspace():
            if buf:
                toks.append("".join(buf))
                buf = []
            continue
        if ch.isascii() and ch.isalnum():
            buf.append(ch)
        else:
            if buf:
                toks.append("".join(buf))
                buf = []
            toks.append(ch)
    if buf:
        toks.append("".join(buf))
    return toks


def estimate_timings(text: str) -> list[WordTiming]:
    """离线估算逐字时间轴:token 均分总时长,确定性可复现。"""
    toks = _tokenize(text)
    if not toks:
        return []
    duration = max(_MIN_DURATION, len(toks) * _SECONDS_PER_TOKEN)
    per = duration / len(toks)
    timings: list[WordTiming] = []
    for i, tok in enumerate(toks):
        start = i * per
        end = duration if i == len(toks) - 1 else (i + 1) * per
        timings.append(WordTiming(tok, start, end))
    return timings


# ---------------------------------------------------------------------------
# 句子切分与逐字 token 归句
# ---------------------------------------------------------------------------

def split_sentences(text: str) -> list[str]:
    """按句末标点或换行断句,保留标点在句尾;返回干净句子列表(可为空)。"""
    sents: list[str] = []
    cur: list[str] = []
    for ch in text:
        if ch == "\n" or ch == "\r":
            s = "".join(cur).strip()
            if s:
                sents.append(s)
            cur = []
            continue
        cur.append(ch)
        if ch in _SENTENCE_END:
            s = "".join(cur).strip()
            if s:
                sents.append(s)
            cur = []
    tail = "".join(cur).strip()
    if tail:
        sents.append(tail)
    return sents


def _norm_pronounce(s: str) -> str:
    """发音归一化:去掉空白与标点,只保留字/词,用于逐字与句子的对齐匹配。"""
    s = re.sub(r"\s+", "", s)
    return re.sub(r"[^\w\u4e00-\u9fff]", "", s, flags=re.UNICODE)


def assign_tokens_to_sentences(
    sentences: list[str], timings: list[WordTiming]
) -> list[list[WordTiming]]:
    """把逐字 token 流按出现顺序分到各句(容忍标点缺失、空白差异)。

    对齐依据是"发音字符数":每个句子期望的发音字数 = _norm_pronounce 长度;
    依次从 token 流里消费发音字符数直到凑够本句,再吸收紧随的纯标点
    (句末标点)。若 TTS 与文本不一致(过多/过少),按长度配额防呆归句。
    """
    result: list[list[WordTiming]] = []
    rest = list(timings)
    for sent in sentences:
        target_len = len(_norm_pronounce(sent))
        seg: list[WordTiming] = []
        i = 0
        pron = 0
        while i < len(rest):
            tok = rest[i]
            tn = _norm_pronounce(tok.text)
            if tn and len(tn) > target_len - pron:
                break  # 该发音 token 超出本句所需,归下一句
            seg.append(tok)
            i += 1
            if tn:
                pron += len(tn)
                if pron >= target_len:
                    break
        # 本句发音凑够后,吸纳紧跟其后的纯标点(句末标点,如 "。")
        while i < len(rest) and not _norm_pronounce(rest[i].text):
            seg.append(rest[i])
            i += 1
        if pron == 0 and not seg:
            seg = []
        result.append(seg)
        rest = rest[i:]
    if not result:
        result.append([])
    if rest:  # 残余 token 归入最后一句
        result[-1].extend(rest)
    return result


# ---------------------------------------------------------------------------
# 构建时间线
# ---------------------------------------------------------------------------

def scene_label(meta: dict) -> str:
    """画面轨单个场景的可读描述(单场景,text 里带说明)。"""
    image = meta.get("bg_image")
    if image:
        return f"背景图 {os.path.basename(str(image))}"
    return f"纯色 #{meta.get('bg_color', '1B1B2A')}"


def build_timeline(
    text: str,
    *,
    timings: list[WordTiming] | None = None,
    meta: dict[str, Any] | None = None,
    duration: float | None = None,
    assets: list[dict] | None = None,
    material: list[dict] | None = None,
    bgm: dict | None = None,
    annotations: list[dict] | None = None,
) -> Timeline:
    """从源文本 + 逐字轴(缺省则离线估算轴)构建轨道对齐的时间线。

    duration:真实模式应由 ffprobe 实测音频时长提供(音频尾部有 padding,比末字
    轴略长),这样 timeline 总时长与最终 mp4 一致;估算模式缺省即末字结束。

    assets/material/bgm/annotations:素材清单(见 load_materials)。material 段
    dict 格式:{"start","end","asset", 视频另有 "in"/"out", 可选 "crop"/"frame"},
    将转换为素材轨 TrackSegment(text=asset id, data={"in","out",...})。
    frame 为裁剪窗口相对素材的比例 {"x","y","w","h"},由 plan 阶段探测素材分辨率
    后写入,供审片在素材图上标注窗口位置(纯展示,渲染仍自行计算像素)。
    """
    text = text.strip()
    if not text:
        raise ValueError("文案为空,无法构建时间线")
    if timings is None:
        timings = estimate_timings(text)
        meta = dict(meta or {})
        meta.setdefault("timing_mode", "estimate")
    else:
        meta = dict(meta or {})
        # 显式传入逐字轴时默认按真实 TTS 处理;调用方可预先置 timing_mode="estimate"
        meta.setdefault("timing_mode", "real")

    sents = split_sentences(text)
    segs = assign_tokens_to_sentences(sents, timings)

    speech: list[TrackSegment] = []
    subtitle: list[TrackSegment] = []
    for sent, toks in zip(sents, segs):
        if toks:
            start, end = toks[0].start, toks[-1].end
        else:
            start = end = 0.0
        speech.append(TrackSegment(start, end, sent))
        subtitle.append(TrackSegment(start, end, sent))

    if duration is None:
        duration = speech[-1].end if speech else 0.0
    visual = [TrackSegment(0.0, duration, scene_label(meta))]

    material_track: list[TrackSegment] = []
    for m in material or []:
        d = {}
        if "in" in m:
            d["in"] = float(m["in"])
        if "out" in m:
            d["out"] = float(m["out"])
        if "crop" in m:
            d["crop"] = _parse_crop(m["crop"])
        if "frame" in m:
            d["frame"] = dict(m["frame"])
        material_track.append(TrackSegment(
            float(m["start"]), float(m["end"]), str(m["asset"]), data=d,
        ))

    # scroll 样式:字幕整段滚动,分句粒度仅作"语音对齐参考",在 meta 里标注
    if meta.get("style") == "scroll":
        meta.setdefault("subtitle_note", "scroll 样式:字幕整段上滚,分句仅作语音对齐参考")

    tl = Timeline(meta=meta, text=text, duration=duration,
                  assets=assets or [], bgm=bgm,
                  annotations=annotations or [])
    tl.tracks = {
        "speech": speech, "subtitle": subtitle,
        "visual": visual, "material": material_track,
    }
    tl.checks = validate_timeline(tl)
    return tl


# ---------------------------------------------------------------------------
# 一致性自检(渲染前放行依据)
# ---------------------------------------------------------------------------

def validate_annotations(annotations: list[dict]) -> list[str]:
    """画面标注框自检:时间先后与位置尺寸(画幅比例)合法性,颜色格式;返回错误列表。"""
    errors: list[str] = []
    for i, a in enumerate(annotations):
        try:
            start = float(a.get("start", -1))
            end = float(a.get("end", -1))
            x = float(a.get("x", 0.0))
            y = float(a.get("y", 0.0))
            w = float(a.get("w", 0.0))
            h = float(a.get("h", 0.0))
        except (ValueError, TypeError):
            errors.append(f"标注框 {i} 数值非法")
            continue
        if not 0.0 <= start < end:
            errors.append(f"标注框 {i} 时间非法({start=:.2f},{end=:.2f})")
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            errors.append(f"标注框 {i} 左上角 x/y 必须在 [0,1](实际 {x:g},{y:g})")
        if not (0 < w <= 1.0 - x + 1e-9 and 0 < h <= 1.0 - y + 1e-9):
            errors.append(
                f"标注框 {i} 尺寸非法(w={w:g},h={h:g}):必须为正且不超出画幅"
            )
        color = a.get("color")
        if color is not None and (not isinstance(color, str)
                                  or color.startswith("0x")
                                  and not re.fullmatch(r"0x[0-9A-Fa-f]{6}", color)):
            errors.append(f"标注框 {i} 颜色格式非法(应为 0xRRGGBB): {color!r}")
    return errors


def validate_timeline(tl: Timeline) -> list[str]:
    """对时间线做一致性自检,返回错误消息列表;为空表示全部通过。"""
    errors: list[str] = []

    for name, segs in tl.tracks.items():
        for i, s in enumerate(segs):
            if s.start < 0 or s.end < s.start:
                errors.append(f"{name} 第 {i} 段时间非法({s.start=:.2f},{s.end=:.2f})")

    errors += validate_materials(
        tl.assets, tl.tracks.get("material", []), tl.bgm
    )
    errors += validate_annotations(tl.annotations)

    # 标注框末端不得超过总时长(与素材段同一容差语义)
    for i, a in enumerate(tl.annotations):
        try:
            a_end = float(a.get("end", 0.0))
        except (ValueError, TypeError):
            continue  # validate_annotations 已报数值非法
        if a_end > tl.duration + _DURATION_TOLERANCE:
            errors.append(
                f"标注框 {i} 末端 {a_end:g}s 超出总时长 {tl.duration:g}s"
            )

    speech = tl.tracks.get("speech", [])
    subtitle = tl.tracks.get("subtitle", [])
    if not speech:
        errors.append("语音轨为空:无法确定时间轴")
        return errors

    # 语音与字幕逐段对齐:数量一致 + 文本一致(容忍两侧首尾空白差异)
    if len(speech) != len(subtitle):
        errors.append(
            f"语音轨 {len(speech)} 段与字幕轨 {len(subtitle)} 段数量不一致"
        )
    else:
        for i, (a, b) in enumerate(zip(speech, subtitle)):
            if a.text.strip() != b.text.strip():
                errors.append(f"第 {i} 段语音与字幕文本不一致: {a.text!r} vs {b.text!r}")

    # 语音末端与总时长对齐(TTS 尾部有 padding,允许容差)
    if abs(speech[-1].end - tl.duration) > _DURATION_TOLERANCE:
        errors.append(
            f"语音末端 {speech[-1].end:.2f}s 与总时长 {tl.duration:.2f}s 偏差超 "
            f"{_DURATION_TOLERANCE}s"
        )

    # 画面轨覆盖 [0, duration] 且连续
    visual = tl.tracks.get("visual", [])
    if not visual:
        errors.append("画面轨为空")
    else:
        if abs(visual[0].start) > 0.01:
            errors.append("画面轨未从 0s 开始")
        if abs(visual[-1].end - tl.duration) > _DURATION_TOLERANCE:
            errors.append(f"画面轨末端 {visual[-1].end:.2f}s 与总时长不符")
        for i in range(len(visual) - 1):
            if visual[i].end > visual[i + 1].start + 0.01:
                errors.append(f"画面轨第 {i}/{i+1} 段区间重叠或倒序")

    # 远端边界不超过总时长
    for name, segs in tl.tracks.items():
        for i, s in enumerate(segs):
            if s.end > tl.duration + _DURATION_TOLERANCE:
                errors.append(
                    f"{name} 第 {i} 段末端 {s.end:.2f}s 超出总时长 {tl.duration:.2f}s"
                )

    return errors


def checks_report(checks: list[str]) -> str:
    """把自检结果渲染成可读文本(空 → 全绿)。"""
    if not checks:
        return "一致性自检: ✅ 全部通过"
    lines = ["一致性自检: ❌ 发现问题"]
    lines += [f"  - {c}" for c in checks]
    return "\n".join(lines)
