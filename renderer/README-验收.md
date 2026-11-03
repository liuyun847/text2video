# 验收记录

- 现场:2026-08,本机 Windows;Node v24.13.0 / npm 11.6.2;ffmpeg 9.0-essentials(gyan);
  浏览器 `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`
- 依赖:`npm install --registry=https://registry.npmmirror.com` → 258 包 / 22 s,
  remotion = @remotion/{cli,bundler,renderer} = **4.0.538**、react/react-dom **19.2.0**、typescript 5.9.3
- 类型检查:`npx tsc --noEmit` → 退出码 0(无输出)

## (a) 出片

```powershell
cd C:\Users\MLTZ\Desktop\code\working\remotion-video
node render.mjs --manifest examples/manifest.json --out out/demo.mp4
```

关键输出:

```
[render] 清单 ...\examples\manifest.json
[render] 复制 ...\demo.audio.mp3 → public/run/manifest/demo.audio.mp3
[render] 复制 ...\demo.subs.webm → public/run/manifest/demo.subs.webm
[render] 复制 ...\shot_counter.png → public/run/manifest/shot_counter.png
[render] 复制 ...\demo.material-video.mp4 → public/run/manifest/demo.material-video.mp4
[render] 复制 ...\shot_parallel.png → public/run/manifest/shot_parallel.png
[render] 渲染用 props 已写出:...\out\demo.props.json
[render] composition TextVideo 1080x1080 @60fps 1450 帧 = 24.167 s
[render] 渲染 1450/1450 帧
[render] 成片 ...\out\demo.mp4(7.44 MB)
[render] ffprobe: 1080x1080 fps=60/1 frames=1450 时长=24.171000 s
```

ffprobe 复核(与清单 `fps 60 / 1080x1080 / duration 24.168 / durationInFrames 1450` 对齐):

```
index=0  codec_name=h264  width=1080  height=1080  r_frame_rate=60/1  duration=24.166667  nb_frames=1450
index=1  codec_name=aac   sample_rate=48000  channels=2  duration=24.171000
format   duration=24.171000  size=7801613
```

读法:
- 视频流 `nb_frames=1450` = 清单 `durationInFrames`;`24.166667 s = 1450/60`,与清单 `duration=24.168` 的差 = 3.3 ms(帧化取整,不可能更准);
- 音轨比视频长 4 ms = AAC 帧对齐的编码器填充,属正常。

耗时:两次实测 138.9 s / 139.6 s(1450 帧、1080²、60 fps、concurrency 4)= **约 10.4 帧/秒**。

## (b) 静帧

```powershell
node render.mjs --manifest examples/manifest.json --stills 0,120,300
```

```
[render] 渲染用 props 已写出:...\out\manifest.props.json
[render] composition TextVideo 1080x1080 @60fps 1450 帧 = 24.167 s
[render] 静帧 0 → C:\Users\MLTZ\Desktop\code\working\remotion-video\out\still-0.png
[render] 静帧 120 → C:\Users\MLTZ\Desktop\code\working\remotion-video\out\still-120.png
[render] 静帧 300 → C:\Users\MLTZ\Desktop\code\working\remotion-video\out\still-300.png
```

绝对路径:

- `C:\Users\MLTZ\Desktop\code\working\remotion-video\out\still-0.png`
- `C:\Users\MLTZ\Desktop\code\working\remotion-video\out\still-120.png`
- `C:\Users\MLTZ\Desktop\code\working\remotion-video\out\still-300.png`

逐帧核对(人工看图 + 取像素):

| 帧 | 时间 | 应看到 | 实测 |
| --- | --- | --- | --- |
| 0 | 0.000 | 只有背景色(#1B1B2A),素材尚未淡入 | 采样 rgb(27,27,42) = #1B1B2A ✔ |
| 120 | 2.000 | 素材 1 + 字幕第 1 句(白色粗体前半 + 灰色后半) | 整幅素材 + "示例字幕层:由 ASS 烧成透明视频" ✔ |
| 300 | 5.000 | 素材 1 + 字幕第 2 句 | 同上,字幕换行 ✔ |

附加的层序/分支核对(不是验收要求,留作证据,在 `out/verify/`):

| 文件 | 用途 | 结论 |
| --- | --- | --- |
| `still-440.png` | 视频素材段第 2 帧(fade in 0.4 s 起点附近) | 视频层 + 字幕叠加正常,透明度忠实于 fade 插值 ✔ |
| `still-1100.png` | 标注框时段内 | 红框 6 px 画在素材上、字幕**压**在框线上 = 层序正确 ✔ |
| `still-30.png` | `manifest-image-bg.json`(背景图分支) | 背景图 cover 铺满 + 字幕叠加 ✔ |
| `still-400.png` | 同上的标注框时段 | 背景图 + 标注框 + 字幕三层并存 ✔ |
| `still-60.png` | 1080x1920 @30fps 的临时清单 | 见 (d) ✔ |

## (c) 宽高 / fps / 时长确实由清单决定

同一条 `TextVideo` Composition,只换清单:

```
清单 examples/manifest.json             → composition TextVideo 1080x1080 @60fps 1450 帧 = 24.167 s
清单 work-tmp/manifest-probe.json       → composition TextVideo 1080x1920 @30fps  240 帧 = 8.000 s
```

机制:`<Composition id="TextVideo" calculateMetadata={computeMetadata} />` 上不写死尺寸,
`render.mjs` 调 `selectComposition({ serveUrl, id, inputProps })` 传清单,Remotion 4.0.538 的
`calculateMetadata({ props })` 依清单返回 `{ fps, width, height, durationInFrames }` 并用于后续渲染。
证据:上面两行 composition 日志 + `out/verify/still-60.png`(1080×1920 竖幅实拍)。

## (d) alpha 验证

见 `docs\alpha-验证.md`:`node scripts/probe-alpha.mjs` 输出 PASS,
左半屏 `transparent` 背景透出 92.87% / 纯黑 2.14%(黑描边),对照组合 95.30% 纯黑。
证据图:`out/probe/alpha-{30,60,90}.png`。

## (e) 其他

- `git init` 已执行;本记录写就时**未 commit**(按当时任务要求),其后已做首次提交(见 `git log`)。`.gitignore` 已忽略 `node_modules/ work-tmp/ out/ public/run/ public/probe/`。
- 未修改 `text2video/`、`cmd-lesson/` 及其他项目;对 `text2video/out` 只做只读复制。
