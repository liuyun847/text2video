"""报告模块:把 Timeline 渲染成两种人类可读产物。

- timeline.md:按轨分节的 Markdown 报告,适合在编辑器里快速通读(只读,
  改稿请改 timeline.json 或源文本后重新 plan/render,render 会覆盖 md);
- preview.html:自包含单文件(零依赖、双击即开)的多轨时间线浏览器:
  多条平行时间轴互相对齐,每条轨道可独立开关(单独查看),可播放 MP3
  (有音频)或虚拟时钟(无音频)同步高亮当前段、显示当前字幕文本。
"""

from __future__ import annotations

import json

from .timeline import Timeline, checks_report

_TRACK_NAMES = {
    "speech": "语音轨 (speech)",
    "subtitle": "字幕轨 (subtitle)",
    "material": "素材轨 (material)",
    "visual": "画面轨 (visual)",
}
_TRACK_COLORS = {
    "speech": "#3b82f6",
    "subtitle": "#22c55e",
    "material": "#a78bfa",
    "visual": "#f59e0b",
}


# ---------------------------------------------------------------------------
# Markdown 分镜表
# ---------------------------------------------------------------------------

def _fmt_meta_table(tl: Timeline) -> list[str]:
    m = tl.meta
    rows = [
        ("总时长", f"{tl.duration:.2f}s"),
        ("时间轴模式", m.get("timing_mode", "?")),
        ("画幅", f"{m.get('width','?')}x{m.get('height','?')} @ {m.get('fps','?')}fps"),
        ("音色", (f"{m.get('voice','?')} "
                  f"(rate {m.get('rate','+0%')}, volume {m.get('volume','+0%')}, "
                  f"pitch {m.get('pitch','+0Hz')})")),
        ("背景", m.get("bg_image") or f"纯色 #{m.get('bg_color','1B1B2A')}"),
        ("字幕样式", (f"{m.get('style','karaoke')} "
                      f"(字体 {m.get('font','?')}, 字号 {m.get('font_size','?')}, "
                      f"每行 {m.get('max_chars','?')} 字)")),
        ("素材/BGM", (f"{len(tl.assets)} 个素材"
                      f"{' · BGM ' + tl.bgm['asset'] + ' @' + str(tl.bgm.get('volume')) if tl.bgm else ''}")),
    ]
    return [f"| {k} | {v} |" for k, v in rows]


def _track_table(segments: list, key: str = "") -> list[str]:
    lines = ["| # | 开始(s) | 结束(s) | 内容 |", "| --- | --- | --- | --- |"]
    for i, s in enumerate(segments):
        content = s.text
        if key == "material":
            d = s.data
            clip = ""
            if "in" in d:
                clip = f" in={d['in']:.2f} out={d['out']:.2f}"
            if "crop" in d:
                c = d["crop"]
                clip += f" crop(x={c.get('x', 0.5):g},y={c.get('y', 0.5):g})"
            if d.get("frame"):
                f = d["frame"]
                clip += (f" 窗口({f.get('x', 0.0):g},{f.get('y', 0.0):g})"
                         f" {f.get('w', 1.0):g}x{f.get('h', 1.0):g} ×素材")
            content = f"{content}{clip}"
        lines.append(f"| {i} | {s.start:.2f} | {s.end:.2f} | {content} |")
    return lines


def _ann_num(a: dict, key: str) -> str:
    """标注框字段的容错格式化:数值转 :g,畸形(手改 timeline)原样显示。"""
    try:
        return f"{float(a.get(key, 0)):g}"
    except (ValueError, TypeError):
        return str(a.get(key, "?"))


def _annotations_table(annotations: list[dict]) -> list[str]:
    lines = ["| # | 起(s) | 止(s) | 位置(x,y) | 尺寸(w,h) | 颜色 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for i, a in enumerate(annotations):
        lines.append(
            f"| {i} | {_ann_num(a, 'start')} | {_ann_num(a, 'end')} "
            f"| {_ann_num(a, 'x')},{_ann_num(a, 'y')} "
            f"| {_ann_num(a, 'w')},{_ann_num(a, 'h')} "
            f"| {a.get('color', '0xFF3B30')} |"
        )
    return lines


def timeline_to_markdown(tl: Timeline, title: str = "时间线预览") -> str:
    """Timeline → Markdown 分镜表(按轨分节,可直接审改)。"""
    parts = [f"# {title}", ""]
    parts += ["## 元信息", ""]
    parts += ["| 键 | 值 |", "| --- | --- |"] + _fmt_meta_table(tl)
    if tl.assets:
        parts += ["", "## 素材库", ""]
        parts += ["| id | 类型 | 路径 |", "| --- | --- | --- |"]
        for a in tl.assets:
            parts += [f"| {a['id']} | {a['type']} | `{a['path']}` |"]
        parts += [""]
    parts += ["## 一致性自检", "", ""]
    parts += [f"```\n{checks_report(tl.checks)}\n```", ""]

    for key, label in _TRACK_NAMES.items():
        parts += [f"## {label}", ""]
        segs = tl.tracks.get(key, [])
        if not segs:
            parts += ["(空)", ""]
            continue
        parts += _track_table(segs, key) + [""]
        if key == "subtitle" and tl.meta.get("subtitle_note"):
            parts += [f"> {tl.meta['subtitle_note']}", ""]
        if key == "material" and tl.assets:
            parts += ["> 素材按段落全屏覆盖,未覆盖区间保持背景;素材裁剪窗口见 preview.html 素材预览面板", ""]
    if tl.annotations:
        parts += ["## 画面标注框", ""]
        parts += _annotations_table(tl.annotations) + [""]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# preview.html(自包含多轨时间线浏览器)
# ---------------------------------------------------------------------------

_PREVIEW_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ — 时间线预览</title>
<style>
  :root { --bg:#0f1117; --panel:#171a22; --fg:#e6e8ee; --dim:#8b93a5;
          --accent:#4f8cff; --border:#262b38; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 "Microsoft YaHei", system-ui, sans-serif; }
  .wrap { max-width:980px; margin:0 auto; padding:20px; }
  h1 { font-size:18px; margin:0 0 4px; }
  .sub { color:var(--dim); font-size:12px; margin-bottom:14px; }
  .panel { background:var(--panel); border:1px solid var(--border);
           border-radius:8px; padding:12px 14px; margin-bottom:10px; }
  .meta { display:flex; flex-wrap:wrap; gap:6px 18px; font-size:12px; color:var(--dim); }
  .meta b { color:var(--fg); font-weight:600; }
  .transport { display:flex; align-items:center; gap:12px; }
  button { background:var(--accent); color:#fff; border:0; border-radius:6px;
           padding:6px 14px; cursor:pointer; font-size:13px; }
  button.ghost { background:transparent; border:1px solid var(--border);
                 color:var(--fg); }
  .time { font-variant-numeric:tabular-nums; font-size:15px; }
  .bar { flex:1; height:14px; background:#0b0d12; border-radius:7px;
         cursor:pointer; position:relative; overflow:hidden; }
  .bar > i { position:absolute; left:0; top:0; bottom:0; width:0;
             background:var(--accent); opacity:.85; border-radius:7px; }
  .toggles { display:flex; gap:16px; flex-wrap:wrap; font-size:13px; }
  .toggles label { display:inline-flex; align-items:center; gap:6px; cursor:pointer; }
  .toggles input { accent-color:var(--accent); }
  .dot { width:10px; height:10px; border-radius:3px; display:inline-block; }
  .lane { border-radius:8px; padding:14px; }
  .lane[hidden] { display:none; }
  .lane h3 { margin:0 0 8px; font-size:13px; font-weight:600; }
  .lane h3 .info { color:var(--dim); font-weight:400; font-size:11px; }
  .track { position:relative; height:52px; background:#0b0d12; border-radius:6px;
           cursor:pointer; overflow:hidden; }
  .seg { position:absolute; top:6px; bottom:6px; border-radius:5px;
         color:rgba(255,255,255,.9); font-size:11px; line-height:1.2;
         padding:2px 6px; overflow:hidden; white-space:nowrap; border:1px solid rgba(0,0,0,.35);
         transition:box-shadow .12s; }
  .seg.active { box-shadow:0 0 0 2px #fff, 0 0 12px rgba(255,255,255,.5); z-index:2; }
  .shots { display:flex; flex-wrap:wrap; gap:12px; }
  .shot { position:relative; width:240px; border-radius:8px; overflow:hidden;
          border:1px solid var(--border); background:#0b0d12; }
  .shot img { display:block; width:100%; height:auto; }
  .shot-frame { position:absolute; border:2px solid #ff5d4d; border-radius:2px;
                box-shadow:0 0 0 1px rgba(0,0,0,.5), 0 0 10px rgba(255,93,77,.45);
                pointer-events:none; }
  .shot-cap { position:absolute; left:0; right:0; bottom:0; padding:3px 6px;
              font-size:11px; color:#fff; background:rgba(10,12,18,.72); }
  .anns { font-size:12px; color:var(--fg); }
  .anns div { padding:3px 0; border-bottom:1px dashed var(--border); }
  .anns b { color:var(--accent); }
  .ticks { position:relative; height:14px; margin:2px 0 2px; font-size:10px; color:var(--dim); }
  .ticks > span { position:absolute; top:0; transform:translateX(-50%); }
  .reader { min-height:52px; font-size:16px; line-height:1.6; }
  .reader .cur { color:var(--accent); font-weight:700; }
  .footer { margin-top:14px; font-size:11px; color:var(--dim); text-align:center; }
</style>
</head>
<body>
<div class="wrap">
  <h1>__TITLE__</h1>
  <div class="sub" id="sub-epoch"></div>

  <div class="panel meta" id="meta-panel"></div>

  <div class="panel transport">
    <button id="btn-play">▶ 播放</button>
    <span class="time" id="time-cur">0:00.000</span>
    <div class="bar" id="bar"><i id="bar-fill"></i></div>
  </div>
  <div class="panel toggles" id="toggles"></div>

  <div class="panel" id="lanes"></div>

  <div class="panel" id="shot-panel" hidden>
    <div class="cardhead"><h2>素材裁剪窗口(素材图上红框 = 当前段取用的画面区)</h2></div>
    <div class="shots" id="shot-list"></div>
  </div>
  <div class="panel" id="ann-panel" hidden>
    <div class="cardhead"><h2>画面标注框(渲染时叠加在画面上)</h2></div>
    <div class="anns" id="ann-list"></div>
  </div>

  <div class="panel reader">
    <div>当前字幕:</div>
    <div id="reader" class="reader"><span class="cur" id="reader-text">(未播放)</span></div>
  </div>

  <div class="footer" id="footer"></div>
</div>

<script>
"use strict";
const TIMELINE = __TIMELINE_JSON__;
const AUDIO_REF = __AUDIO_REF__;   // null 或相对 mp3 路径
const META = TIMELINE.meta || {};
const DURATION = TIMELINE.duration || 1;
const ORDER = ["speech", "subtitle", "material", "visual"];
const COLORS = __COLOR_MAP__;
const LABELS = __LABEL_MAP__;

let t = 0, playing = false, audio = null, mode = AUDIO_REF ? "audio" : "clock";
let clockTimer = null, lastTs = performance.now();

function fmt(sec) {
  sec = Math.max(0, sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60);
  const s = sec % 60, ss = Math.floor(s), ms = Math.floor((s - ss) * 1000);
  return (h ? h + ":" : "") + String(m).padStart(2, "0") + ":" +
         String(ss).padStart(2, "0") + "." + String(ms).padStart(3, "0");
}
function esc(s) {
  const d = document.createElement("div");
  d.textContent = String(s == null ? "" : s);
  return d.innerHTML;
}
function cssColor(c) {
  // 标注框颜色为 ffmpeg 风格 0xRRGGBB,CSS 只认 #RRGGBB(或命名色)
  c = String(c);
  return c.slice(0, 2).toLowerCase() === "0x" ? "#" + c.slice(2) : c;
}

// ---- 元信息 ----
(function () {
  const rows = [
    ["时长", fmt(DURATION)], ["时间轴", META.timing_mode || "?"],
    ["画幅", META.width + "x" + META.height + " @ " + META.fps + "fps"],
    ["音色", META.voice + " (" + META.rate + ")"],
    ["背景", META.bg_image || ("纯色 #" + META.bg_color)],
    ["字幕", META.style + " · " + META.font + " " + META.font_size + "px"],
    ["素材", ((TIMELINE.assets || []).length + " 个"
      + (TIMELINE.bgm ? " · BGM " + TIMELINE.bgm.asset : ""))],
  ];
  document.getElementById("meta-panel").innerHTML =
    rows.map(r => "<span>" + esc(r[0]) + ": <b>" + esc(r[1]) + "</b></span>").join("");
  document.getElementById("sub-epoch").textContent =
    "plan → render 两阶段中间产物 · " + (AUDIO_REF ? "已含音频,可播放审片" :
      "无音频(估算模式),使用虚拟时钟推进查看布局");
})();

// ---- 轨道开关 ----
(function () {
  const box = document.getElementById("toggles");
  ORDER.forEach(k => {
    const l = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.dataset.track = k; cb.checked = true;
    cb.onchange = () => { document.querySelector('.lane[data-track="' + k + '"]').hidden = !cb.checked; };
    l.appendChild(cb);
    l.innerHTML += '<span class="dot" style="background:' + COLORS[k] + '"></span>' +
                   esc(LABELS[k]);
    box.appendChild(l);
  });
})();

// ---- 轨道渲染 ----
function segDiv(k, s, i) {
  const d = document.createElement("div");
  d.className = "seg";
  d.dataset.k = k; d.dataset.i = i;
  d.style.left = (s.start / DURATION * 100).toFixed(3) + "%";
  d.style.width = Math.max(0.3, (s.end - s.start) / DURATION * 100).toFixed(3) + "%";
  d.style.background = COLORS[k];
  let extra = "";
  if (k === "material" && s.data) {
    if (s.data.in !== undefined) extra = " in=" + s.data.in.toFixed(2) + " out=" + s.data.out.toFixed(2);
    if (s.data.crop) {
      extra += " crop x=" + s.data.crop.x + " y=" + s.data.crop.y;
    }
  }
  d.title = fmt(s.start) + " → " + fmt(s.end) + "  " + s.text + extra;
  d.textContent = s.text;
  return d;
}
(function () {
  const lanes = document.getElementById("lanes");
  ORDER.forEach(k => {
    const pane = document.createElement("div");
    pane.className = "lane"; pane.dataset.track = k;
    pane.style.background = COLORS[k] + "1a";
    const h = document.createElement("h3");
    h.innerHTML = '<span class="dot" style="background:' + COLORS[k] + '"></span> ' +
      esc(LABELS[k]) + ' <span class="info">(共 ' +
      (TIMELINE.tracks[k] || []).length + ' 段)</span>';
    const tr = document.createElement("div"); tr.className = "track";
    (TIMELINE.tracks[k] || []).forEach((s, i) => tr.appendChild(segDiv(k, s, i)));
    tr.addEventListener("click", ev => seekFromElement(tr, ev));
    pane.appendChild(h); pane.appendChild(tr);
    lanes.appendChild(pane);
  });
  // 时间标尺
  const tickPane = document.createElement("div");
  tickPane.className = "ticks";
  for (let s = 0; s <= DURATION + 0.001; s += Math.max(1, Math.round(DURATION / 10))) {
    const sp = document.createElement("span");
    sp.style.left = (s / DURATION * 100).toFixed(3) + "%";
    sp.textContent = Math.round(s) + "s";
    tickPane.appendChild(sp);
  }
  lanes.prepend(tickPane);
})();

// ---- 素材裁剪窗口标注(素材缩略图 + 红框)----
(function () {
  const panel = document.getElementById("shot-panel");
  const assetsById = {};
  (TIMELINE.assets || []).forEach(a => { assetsById[a.id] = a; });
  const segs = (TIMELINE.tracks.material || []).filter(
    s => s.data && s.data.frame && s.data.crop);
  panel.hidden = !segs.length;   // 有内容时显式显示(模板默认 hidden)
  if (!segs.length) { return; }
  const list = document.getElementById("shot-list");
  segs.forEach((s, i) => {
    const a = assetsById[s.text];
    if (!a) return;
    const f = s.data.frame;
    const d = document.createElement("div"); d.className = "shot";
    const img = document.createElement("img");
    img.src = a.path;
    img.alt = "素材 " + s.text;
    const fr = document.createElement("div"); fr.className = "shot-frame";
    fr.style.left = (f.x * 100).toFixed(2) + "%";
    fr.style.top = (f.y * 100).toFixed(2) + "%";
    fr.style.width = (f.w * 100).toFixed(2) + "%";
    fr.style.height = (f.h * 100).toFixed(2) + "%";
    const cap = document.createElement("div"); cap.className = "shot-cap";
    cap.textContent = "#" + i + " " + s.text + " [" + fmt(s.start) + "→" +
      fmt(s.end) + "] crop(x=" + s.data.crop.x + ",y=" + s.data.crop.y + ")";
    d.appendChild(img); d.appendChild(fr); d.appendChild(cap);
    list.appendChild(d);
  });
})();

// ---- 画面标注框(文本列表)----
(function () {
  const panel = document.getElementById("ann-panel");
  const anns = TIMELINE.annotations || [];
  panel.hidden = !anns.length;   // 有内容时显式显示(模板默认 hidden)
  if (!anns.length) { return; }
  const list = document.getElementById("ann-list");
  anns.forEach((a, i) => {
    const d = document.createElement("div");
    d.innerHTML = "<b>#" + i + "</b> " + fmt(a.start) + "→" + fmt(a.end) +
      " @(" + Math.round(a.x * 100) + "%," + Math.round(a.y * 100) + "%) " +
      Math.round(a.w * 100) + "%×" + Math.round(a.h * 100) + "%" +
      (a.color ? ' <span style="color:' + esc(cssColor(a.color)) + '">■</span>' : "");
    list.appendChild(d);
  });
})();

function seekFromElement(el, ev) {
  const r = el.getBoundingClientRect();
  const ratio = (ev.clientX - r.left) / r.width;
  gotoTime(ratio * DURATION);
}
function gotoTime(v) {
  t = Math.max(0, Math.min(DURATION, v));
  if (mode === "audio" && audio) { try { audio.currentTime = t; } catch (e) {} }
  update();
}

// ---- 播放 ----
const btn = document.getElementById("btn-play");
function switchMode(next) {
  mode = next;
  const foot = document.getElementById("footer");
  foot.textContent = mode === "audio"
    ? "音频模式:播放 MP3 时各轨道按当前时间同步高亮。"
    : "虚拟时钟模式:时间轴为估算布局,无音频;按钮模拟播放推进。";
}
function startClock() {
  lastTs = performance.now();
  clearInterval(clockTimer);
  clockTimer = setInterval(() => {
    const now = performance.now();
    t += (now - lastTs) / 1000; lastTs = now;
    if (t >= DURATION) { t = DURATION; stop(); }
    update();
  }, 33);
}
function stop() {
  playing = false; btn.textContent = "▶ 播放";
  clearInterval(clockTimer);
  if (mode === "audio" && audio) { try { audio.pause(); } catch (e) {} }
}
btn.addEventListener("click", () => {
  if (playing) { stop(); return; }
  playing = true; btn.textContent = "⏸ 暂停";
  if (mode === "audio") { audio.currentTime = t; audio.play().catch(stop); }
  else startClock();
});

function initAudio() {
  if (!modeAudio || !AUDIO_REF) return;
  audio = new Audio(AUDIO_REF);
  audio.addEventListener("error", () => { audio = null; switchMode("clock"); update(); });
  audio.ontimeupdate = () => { t = audio.currentTime; update(); };
  audio.onended = () => { t = DURATION; stop(); update(); };
  // 可播放才算音频模式,出错即降级虚拟时钟
  audio.addEventListener("loadeddata", () => { switchMode("audio"); });
}

// ---- 高亮 / 进度 ----
function activeIdx(segs) {
  const n = (segs || []).length;
  for (let i = 0; i < n; i++) {
    const s = segs[i];
    if (t >= s.start && t < s.end) return i;
  }
  if (n && t >= segs[n - 1].end) return n - 1;
  return -1;
}
function update() {
  document.getElementById("time-cur").textContent = fmt(t);
  document.getElementById("bar-fill").style.width = (t / DURATION * 100).toFixed(2) + "%";
  let curText = "", curSet = false;
  ORDER.forEach(k => {
    const segs = TIMELINE.tracks[k] || [];
    const idx = activeIdx(segs);
    document.querySelectorAll('.seg[data-track="' + k + '"]').forEach(el => {
      el.classList.toggle("active", parseInt(el.dataset.i, 10) === idx);
    });
    if (k === "speech" && idx >= 0) { curText = segs[idx].text; curSet = true; }
  });
  const rd = document.getElementById("reader-text");
  rd.textContent = curSet ? curText : (playing ? "(当前段无文本)" : "(未播放)");
}

// write 音频引用:存在则用真实音频播放,否则虚拟时钟
const modeAudio = AUDIO_REF !== null && AUDIO_REF !== undefined;
initAudio();
switchMode(modeAudio ? "audio" : "clock");
update();
</script>
</body>
</html>
"""


def render_preview_html(
    tl: Timeline,
    *,
    audio_ref: str | None = None,
    title: str = "时间线",
) -> str:
    """Timeline → 自包含 preview.html。audio_ref 为相对 mp3 名(可无,虚拟时钟)。"""
    data = tl.to_dict()
    json_text = json.dumps(data, ensure_ascii=False)
    json_text = json_text.replace("</", "<\\/")  # 防止 </script> 提前闭合
    html = (
        _PREVIEW_HTML
        .replace("__TIMELINE_JSON__", json_text)
        .replace("__AUDIO_REF__", json.dumps(audio_ref) if audio_ref else "null")
        .replace("__COLOR_MAP__", json.dumps(_TRACK_COLORS, ensure_ascii=False))
        .replace("__LABEL_MAP__", json.dumps(_TRACK_NAMES, ensure_ascii=False))
        .replace("__TITLE__", title.replace("<", "&lt;").replace(">", "&gt;"))
    )
    return html
