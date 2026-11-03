# alpha 关键验证:透明字幕视频能不能被 Remotion 正确叠加

- 结论:**能**。Remotion 4.0.538 的 `<OffthreadVideo transparent />` 能把带 alpha 的 VP9/webm 当透明视频层叠加,
  透明区正常透出下层画面,不出现黑块。协议 = "字幕在 Python 侧烧成带 alpha 的 webm,Remotion 只当视频层"**可行**。
- 但整条链上有 **两个必踩的坑**,任一没处理都会得到"黑块",而两个坑的报错都很安静(无警告)。
- 采集现场:2026-08 本机,ffmpeg 9.0-essentials(gyan)、Node v24.13.0、Edge `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`。

## 1. 证据链(四步闭环)

### 1.1 先证明 Remotion 侧可行

探针 Composition `AlphaProbe`(`src/probe/AlphaProbe.tsx`):左右半屏同一段透明度视频,
左半屏传 `transparent`,右半屏不传作对照;背景刻意用高饱和青色(#00E0FF)/黄色(#FFD400),
一旦 alpha 丢失出现黑块就是一眼可见的黑矩形。

复现命令:

```powershell
node examples/make-assets.mjs      # 生成 examples/assets/alpha-probe.webm 并复制到 public/probe/
node scripts/probe-alpha.mjs       # 渲染 out/probe/alpha-{30,60,90}.png + 像素统计
```

输出(1080×1080,60 fps,2 s 素材,统计区域 = 540×540 贴图区):

```
帧   区域                    青色    黄色    白字    纯黑    其他
30   左半屏 transparent=true     92.87%   0.00%   3.84%   2.14%   1.14%
30   右半屏 transparent=false     0.00%   0.00%   3.83%  95.30%   0.87%
60   左半屏 transparent=true     92.87%   0.00%   3.84%   2.14%   1.14%
60   右半屏 transparent=false     0.00%   0.00%   3.83%  95.30%   0.87%
90   左半屏 transparent=true     92.87%   0.00%   3.84%   2.14%   1.14%
90   右半屏 transparent=false     0.00%   0.00%   3.83%  95.30%   0.87%

结论:transparent=true 一侧背景透出 92.87%、纯黑 2.14%(字幕黑描边);对照 transparent=false 一侧纯黑 95.30%
PASS:VP9 alpha 被正确保留,透明区透出下层画面,无黑块
```

读法:
- 左半屏 92.87% 青 = 背景透出来了;2.14% 纯黑 = ASS 样式里的 3 px 黑描边 + 1 px 阴影(本来就是黑的,不是黑块);
- 右半屏 95.30% 纯黑 = 不传 `transparent` 时 alpha 被丢弃,整块变黑 → **这就是"黑块"故障的真实长相**。

证据图(可用图片查看器打开):
- `out/probe/alpha-60.png`(左:透明生效;右:黑块对照)
- `out/probe/alpha-30.png`、`out/probe/alpha-90.png`

### 1.2 为什么"第一版探针"也出黑块 —— 素材本身没有 alpha

第一版用 `-f lavfi -i color=c=black@0.0 ... -vf format=rgba,ass=subs.ass` 生成,结果两侧都 100% 纯黑。
逐环量化后定位到两个独立的坑(与本条第 1.3 节):

| 环节 | 观测 | 判定 |
| --- | --- | --- |
| lavfi `color=c=black@0.0` | 直接 `-pix_fmt rgba` 输出,alpha 通道 100% = 255 | `@0.0` 只影响画色,不产生透明底 |
| `ass` 滤镜(默认参数) | RGB 画上了白字(4.85% 非黑),alpha 通道 100% = 0/255 原样不变 | 滤镜默认**不写** alpha |

### 1.3 正确配方(ffmpeg 两件套,缺一不可)

```powershell
ffmpeg -y -f lavfi -i "color=c=black:s=1080x1080:r=60:d=24.168" `
  -vf "format=rgba,colorchannelmixer=aa=0,ass=demo.subs.ass:alpha=1" `
  -c:v libvpx-vp9 -pix_fmt yuva420p -auto-alt-ref 0 -b:v 0 -crf 30 -row-mt 1 -cpu-used 4 -deadline good `
  demo.subs.webm
```

1. `colorchannelmixer=aa=0` —— 底色必须**真的** alpha=0(`color=c=black@0.0` 不行,实测仍 100% 不透明)。
2. `ass=<file>:alpha=1` —— `ass`/`subtitles` 滤镜的 `alpha` 选项默认 `false`,不打开就不写 alpha 通道。

编码后文件里能看到 `TAG:alpha_mode=1`(VP9 的 alpha 存在附加流里,主流的 `pix_fmt` 仍显示 `yuv420p`,这是正常的)。

### 1.4 还有第三个坑:ffmpeg 原生 VP9 解码器不读 alpha

```
默认(原生 vp9 解码)   → a255=100.00%  a0=0.00%     ← alpha 被静默丢弃
-c:v libvpx-vp9        → a255=6.39%    a0=92.46%    ← alpha 正常
```

影响:
- **自测/验收脚本**若要用 ffmpeg 检查透明视频,必须显式 `-c:v libvpx-vp9`;
- Remotion 侧不受影响 —— 它用自带的 compositor 抽帧,实测走的是能读 alpha 的路径(见 1.1)。

## 2. 复现全部证据的最短路径

```powershell
node examples/make-assets.mjs    # 造素材(含 alpha_probe.webm)
node scripts/probe-alpha.mjs     # 出 PNG + 像素表 + PASS/FAIL(FAIL 时退出码 2)
```

## 3. 如果将来 alpha 路线真的走不通时的备选(本轮未启用)

按代价从低到高:

1. **PNG 序列 + `<Img>` 逐帧**:Python 侧用 `ffmpeg -i subs.ass -f image2 out/%05d.png` 出带 alpha 的 PNG 序列,
   Remotion 侧用 `<Img src={staticFile("run/x/subs/"+String(frame).padStart(5,"0")+".png")} />` 按帧取。
   代价:文件数 = 帧数(24 s × 60 fps ≈ 1450 个 PNG,百 MB 级),但精度最高、完全无 codec 风险;
   需要 `delayRender`/`Img` 的加载等待,渲染比视频层慢。
2. **APNG / MOV(ProRes 4444)**:`-c:v prores_ks -profile:v 4444 -pix_fmt yuva444p10le`,单文件且带 alpha,
   文件大(24 s 约 1–2 GB 量级),但消费端仍是视频层,改动最小。
3. **两路视频**:字幕烧到纯黑底 + 同帧的 alpha 蒙版视频,Remotion 侧用 `mixBlendMode` 无法重建 alpha,不推荐。

本轮结论是不需要备选,故上述第 1、2 条未实测,只作记录。
