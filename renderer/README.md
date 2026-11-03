# renderer —— Remotion 渲染端(原独立仓库 `remotion-video`,已并入 text2video)

按一份 `render.json`(version 1)清单,把「音轨 + 透明字幕视频 + 素材 + 标注」合成成片。
本目录是 [text2video](../README.md) 仓库内的渲染端,与 `src/`、`tests/` 同级:
上游 Python 侧只负责**产出清单和素材文件**,这里只负责**按清单出片**,两侧唯一接口就是清单。

- 命令行入口两个:直接 `node render.mjs ...`(本节),或在上层仓库 `uv run text2video render --manifest <清单>`
  (薄壳,自动定位 node 与本目录,见 [总 README 的「出片:render」](../README.md#出片render))
- 目录内的相对路径约定(`public/`、`work-tmp/`、`out/`、`examples/`)一律**以本目录为根**;
  `render.mjs` 用 `import.meta.url` 定位自己,所以从哪个 cwd 调用都一致
- Remotion **4.0.538** / React **19.2.0** / TypeScript **5.9**(版本对齐 `cmd-lesson/video`,只抄渲染侧)
- Composition id 固定 `TextVideo`;宽高 / fps / 时长**全部由清单决定**(走 `calculateMetadata`)
- 字幕不由 Remotion 画:Python 侧用 ASS 烧成**带 alpha 的视频**,这里只当透明视频层叠加

## 1. 快速开始

```powershell
cd C:\Users\MLTZ\Desktop\code\working\text2video\renderer
npm ci --registry=https://registry.npmmirror.com        # 首次/删了 node_modules 后;实测 22 s / 258 包
node examples/make-assets.mjs                          # 造自测素材 + 两份清单样例
node render.mjs --manifest examples/manifest.json --out out/demo.mp4
node render.mjs --manifest examples/manifest.json --stills 0,120,300
```

| 命令 | 作用 |
| --- | --- |
| `node render.mjs --manifest <清单> --out out/x.mp4` | 出片(h264 + aac) |
| `node render.mjs --manifest <清单> --stills 0,120,300` | 只出 PNG 静帧到 `out/still-<帧>.png` |
| `node render.mjs --manifest <清单> --stills 0 --stills-dir out/x` | 指定静帧目录 |
| `node examples/make-assets.mjs` | 生成 `examples/assets/` 与 `examples/manifest*.json`(幂等) |
| `node scripts/probe-alpha.mjs` | alpha 可行性验证(出证据图 + 像素表,PASS/FAIL) |
| `npm run studio` | Remotion Studio 看组件(默认空白清单) |
| `npx remotion studio src/index.ts --props=out/manifest.props.json` | 用真实清单预览(先跑一次 render 生成 props) |

`render.mjs` 还能用:`--concurrency 4`、`--crf 18`、`--browser <路径>`、`--id TextVideo`。

### 接 text2video 产物(端到端)

上游 [text2video](../README.md)(同仓库 `src/`)产清单 + 中间件,本目录只负责出片。
推荐在上层仓库根用 Python 子命令(自动定位 node 与本目录,不用先 cd):

```powershell
cd C:\Users\MLTZ\Desktop\code\working\text2video

# 1) 文本 → 中间件:{base}.audio.mp3 / {base}.subs.webm / {base}.material/ / {base}.render.json
uv run text2video produce --file examples/sample.txt --out out\sample

# 2) 清单 → 成片(等价于下面那条 node 命令)
uv run text2video render --manifest out\sample.render.json --out out\sample.mp4

# 2') 只出静帧先看画面(整片很慢,出帧只要一次 bundle)
uv run text2video render --manifest out\sample.render.json --stills 120,720,1380 --stills-dir out\check
```

注意两边的相对路径基准不同:`text2video render` 的 `--manifest/--out/--stills-dir` 按**当前目录**解析;
本目录里直接跑 `node render.mjs` 时,这些参数和它自己的默认值(`out/demo.mp4`)都按**本目录**解析。

```powershell
# 直接跑 node(在本目录内)——效果与上面 text2video render 相同
cd C:\Users\MLTZ\Desktop\code\working\text2video\renderer
node render.mjs --manifest ..\out\sample.render.json --out out\sample.mp4
node render.mjs --manifest ..\out\sample.render.json --stills 120 --stills-dir out\check
```

实测(本机,Edge + concurrency 4):1080²@60 / 1450 帧 ≈ **140 s**;1920×1080@60 / 2681 帧 ≈ **4.5 min**。

## 2. 清单契约(render.json,version 1,冻结)

字段名一字不改;路径一律**绝对路径**(Windows 反斜杠原样);时间单位秒(float,round 3);
`durationInFrames = round(duration * fps)`。

```json
{
  "version": 1,
  "fps": 60, "width": 1080, "height": 1080,
  "duration": 24.168, "durationInFrames": 1450,
  "background": { "type": "color", "color": "1B1B2A" },
  "audio":    { "path": "C:\\...\\xxx.audio.mp3" },
  "subtitle": { "path": "C:\\...\\xxx.subs.webm" },
  "materials": [
    { "type": "image", "path": "C:\\...\\a.png", "start": 0.0, "end": 7.3, "fade": { "in": 0.3, "out": 0.3 } }
  ],
  "annotations": [
    { "start": 17.5, "end": 24.5, "x": 0.1, "y": 0.3, "w": 0.8, "h": 0.55, "color": "0xFF3B30" }
  ],
  "timing": { "mode": "real" }
}
```

| 字段 | 约定 |
| --- | --- |
| `background` | `{type:"color",color:"RRGGBB"}`(**无** `#`/`0x` 前缀)或 `{type:"image",path:"<绝对路径>"}`,图片按 `cover` 铺满 |
| `audio` | 配音音轨 mp3(上游已把 BGM 混进同一条,渲染端不做增益) |
| `subtitle` | 带 alpha 的字幕视频(webm/mov),**整幅同尺寸**叠加;生成配方见 §5 |
| `materials[]` | 每段 `start/end` 是**绝对秒**;素材已由上游预切到输出画幅,这里只 `cover` 铺满 + 淡入淡出 |
| `materials[].fade` | 秒,`in`/`out` 可只给一边;省略 = 硬切 |
| `annotations[]` | 坐标 0~1 归一化,边框固定 6 px,`color` 形如 `0xRRGGBB`;层级 = **素材之上、字幕之下** |
| `timing` | `real`(真实 TTS 轴)/ `estimate`(离线估算轴);渲染端只透传,不影响画面 |

行为细节:
- **帧化**:所有秒 → 帧统一走 `round(sec*fps)`(`src/manifest.ts` 的 `toFrame`),全工程只此一处。
- **越界**:素材/标注的 `end` 超过 `duration` 时自动夹到成片末尾(样例清单里的 `annotations.end=24.5` 就是这种情况,渲染时告警一次)。
- **校验**:`render.mjs` 先跑一遍契约校验(版本、正整数、偶数宽高、路径绝对且存在、`durationInFrames` 自洽),
  失败直接退出并列出全部问题;浏览器侧 `assertManifest` 再兜一层。

## 3. 静态资源规则(最容易踩的地方)

`staticFile()` 只认 `package.json` 同级的 `public/`,并且**明确拒绝**绝对路径、`..`、`public/` 前缀;
v4 起它自己会对路径逐段做 `encodeURIComponent`。所以清单里的绝对路径必须在渲染前搬运:

1. `render.mjs` 读清单,把所有引用文件(`background.image`、`audio`、`subtitle`、`materials[].path`)复制到
   `public/run/<清单文件名不含扩展名>/<原文件名>`;
2. 同名冲突(不同目录的同名文件)自动加 `-2`/`-3` 后缀,并打日志;
3. 每次渲染前**只清本清单自己的** `public/run/<清单名>/`,一次渲染一个干净沙箱,不留上一份清单的残留;
   按清单分目录还让**不同清单可以同时渲染**(并行时别用同一份清单);
4. 组件拿到的路径已改写成 `run/<清单名>/<文件名>`,统一走 `staticFile()`。

中文/空格文件名可以正常用(实测 `public/run/manifest/...` + `encodeURIComponent` 通路):
`staticFile()` 编码的是 URL,而静态服务端按 URL 解码回文件系统,两者一致。
但**不要在文件名里用** `#` `?` `&` —— 它们会被编码成 `%23` 等,`staticFile()` 会警告"你似乎传了已编码路径",
属于已知易踩区,建议上游产文件名时避开。

渲染真正用的 props 会写到 `out/<成片名>.props.json`(静帧模式写 `out/<清单名>.props.json`),可直接给 Studio:

```powershell
node render.mjs --manifest examples/manifest.json --stills 0 --stills-dir out/dryrun
npx remotion studio src/index.ts --props=out/manifest.props.json
```

> 注意:`--props` 里的路径是 `public/run/...` 相对路径,依赖上一次 render 留下的 `public/run/`。
> 单独跑 `npm run studio` 时用的是内置空白清单(纯色底、无音轨/字幕/素材),只是看组件用。

## 4. 层结构(自底向上)

```
Background            纯色 或 图片(cover)
  └─ MaterialLayer    每段 <Sequence from=round(start*fps) durationInFrames=round(end*fps)-from>,opacity 插值淡入淡出
       └─ AnnotationLayer   div + 6px 边框,坐标 round(x*W)/round(y*H),画在素材之上
            └─ SubtitleTrack <OffthreadVideo transparent muted /> 透明字幕视频,画在最上层画面
                 └─ AudioTrack  <Audio src=配音 mp3 />
```

- 视频素材用 `<OffthreadVideo muted>`(`muted` = 丢弃素材自带音轨,与 text2video 的 `-map` 只取配音流一致);
- 图片素材用 `<Img>`;`objectFit: cover` 兜底;
- 标注框 `box-sizing: border-box` + 6 px 边框 = ffmpeg `drawbox ... t=6` 的"向内画"语义。

## 5. 字幕素材必须真的带 alpha(两个必踩的坑)

完整证据见 [docs/alpha-验证.md](docs/alpha-验证.md),这里只放结论:

1. `<OffthreadVideo>` **必须显式传 `transparent`**,否则抽帧丢 alpha → 透明区变黑块(实测:不传时 95.30% 纯黑);
2. 生成透明 webm 时 ffmpeg 要**两件套**:
   - `colorchannelmixer=aa=0` 造真透明底(`color=c=black@0.0` 实测**不透明**);
   - `ass=<file>:alpha=1`(`ass`/`subtitles` 滤镜默认**不写** alpha)。

```powershell
ffmpeg -y -f lavfi -i "color=c=black:s=1080x1080:r=60:d=24.168" `
  -vf "format=rgba,colorchannelmixer=aa=0,ass=my.subs.ass:alpha=1" `
  -c:v libvpx-vp9 -pix_fmt yuva420p -auto-alt-ref 0 -b:v 0 -crf 30 -row-mt 1 -cpu-used 4 -deadline good `
  my.subs.webm
```

另有一个只影响自测的坑:ffmpeg **原生** VP9 解码器不读 alpha,验证时必须 `-c:v libvpx-vp9`。

## 6. 本机坑(照做就行)

1. **`TEMP`/`TMP` 必须在 `require("@remotion/*")` 之前指到项目内 `work-tmp/`**。
   Remotion 自带 ffmpeg 写 `%TEMP%\react-motion-render*\pre-encode.mp4` 会被拒(`Permission denied`),
   而系统 ffmpeg / Node 写同一目录正常;`makeTempDir` 参数替换不了那一处。`render.mjs`/`scripts/probe-alpha.mjs` 都已处理。
2. **浏览器**:默认 `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`(本机存在);
   可用 `--browser <路径>` 或 env `REMOTION_BROWSER_EXECUTABLE` 覆盖;
   找不到时回退 Remotion 自带 Chrome Headless Shell(首次运行会下载)。
3. **Node v24.13.0 / npm 11.6.2** 实测可用;依赖装不动时加 `--registry=https://registry.npmmirror.com`(本轮实测 22 s)。
4. `work-tmp/` 与 `public/run/` 都是可随时删除的中间态,已在 `.gitignore` 里。
   `work-tmp/` 每次渲染还会留下 `puppeteer_dev_chrome_profile-*` 与 `remotion-v4.0.538-assets*` 目录(单个渲染周期数百 MB),
   定期 `Remove-Item -Recurse work-tmp\*` 即可,不影响下次渲染。

## 7. 目录

```
text2video/                     ← 仓库根
  src/text2video/               Python 生产端(produce 出清单与素材)
  tests/                        Python 单测
  renderer/                     ← 本目录:Remotion 渲染端
    render.mjs                  清单 → 校验 → 复制素材 → bundle → 渲染(CLI 主入口)
    src/
      index.tsx                 注册 Composition:TextVideo + AlphaProbe
      manifest.ts               契约类型、toFrame/颜色归一化、calculateMetadata
      TextVideo.tsx             根组件(层序见 §4)
      layers/                   Background / MaterialLayer / AnnotationLayer / SubtitleTrack / AudioTrack
      lib/util.ts               纯函数:fadeOpacity、annotationRect、frameWindow
      probe/AlphaProbe.tsx      alpha 验证探针(非生产路径)
    scripts/probe-alpha.mjs     alpha 验证脚本(证据图 + 像素统计)
    examples/
      make-assets.mjs           生成素材与样例清单(幂等)
      manifest.json             样例清单(1080² / 60fps / 24.168 s / 3 段素材 / 1 个标注框)
      manifest-image-bg.json    背景图分支样例
      assets/                   样例素材(含 ffmpeg 现造的透明字幕 webm 与 .ass 源)
    docs/alpha-验证.md          alpha 验证的证据与配方
    package.json / tsconfig.json / package-lock.json
    public/                     静态资源根(run/ 为渲染时生成)
    out/                        成片、静帧、*.props.json、probe/ 证据图
    work-tmp/                   Remotion bundle 与临时文件(运行期生成)
    README-验收.md              本渲染端自身的验收记录
```

## 8. 已知限制 / 待办

### 与 text2video 联调的已知约束(2026-08 实测)

1. **画幅 / fps 必须三方一致**:清单 `width/height/fps` = `subs.webm` = `.material/*.mp4`。
   `render.mjs` 会用 ffprobe 预检字幕轨与视频素材的宽高/帧率,不一致时**告警但不中断**
   (告警文案指明是哪一项);改画幅或帧率必须重跑 `text2video produce`,不要手改清单。
2. **字幕轨是整幅叠加**:`subs.webm` 与成片同尺寸、同帧数,`SubtitleTrack` 用 `style={{width,height}}` 强制铺满。
   画幅不一致会被**拉伸**(不是裁剪)——别拿旧画幅的字幕轨配新清单。
3. **素材已在上游预切**:`materials[].path` 已裁到输出画幅(video 还做了 `trim in/out` 且 `-an` 去音轨),
   渲染端只 `objectFit:cover` + 淡入淡出;契约里没有 `in`/`out`/`crop`,不要指望这里二次裁剪。
4. **BGM 已混进 `audio` 单轨**:清单没有 bgm 字段,`AudioTrack` 不设 volume,不要二次混音或增益。
5. **音频会被上混成立体声**:上游 mp3 是 24 kHz 单声道,Remotion 输出 48 kHz 立体声(aac),单声道按等功率上混
   —— 每声道峰值低 3 dB、总功率不变。核对音量时别拿单声道峰值直接与新成片比。
6. **并行渲染**:`public/run/<清单名>/` 与 `work-tmp/bundle-<清单名>/` 按清单隔离,不同清单可以同时渲染;
   **同一份清单**并行仍会互相覆盖。
7. **标注框按清单颜色画**:`0xFFFFFF`(白框)叠在浅色素材上肉眼几乎不可见,联调时若怀疑"框没画出来",
   把清单里的 `color` 临时改成 `0xFF3B30` 出静帧即可定位(实测框线 6 px、矩形与 ffmpeg `drawbox` 完全一致)。

### 渲染端自身的已知限制

- `fade` 只按段内边界线性插值,没有 ffmpeg `fade` 的 "段长一半" 自动钳制;上游给超长淡入时会表现成整段半透明。
- 素材里的视频不做 `in/out` 裁剪(契约里没有这两个字段),需要裁剪请上游预切。
- 未做"渲染前逐帧抽检字幕对位"的自动比对;alpha 验证脚本只覆盖透明链路。
- 60 fps × 1080² 的渲染成本约为 30 fps 的 2 倍;1450 帧实测耗时见 §9。

## 9. 验收记录

- `README-验收.md`:渲染端自身的验收(出片 / 静帧 / 清单决定宽高 fps / alpha),命令原文与 ffprobe 输出。
- 并入 text2video 仓库后的搬迁验证(2026-11-03):在本目录对 `../out/produce_sample.render.json`
  出静帧 `--stills 120` 成功(1080×1080、字幕逐字点亮正常),`node_modules` 随目录同盘搬移后直接可用、无需重装。
- 与 `text2video` 真实产物的端到端联调(两条成片、新旧对照图、视频素材/BGM/背景图专项):
  见工作区 `.dsh\.project\text2video-remotion-联调验收.md`。
