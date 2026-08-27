# text2video — 文案转视频

把一段纯文本,一键变成"带配音 + 滚动字幕"的短视频(默认 1:1 方画幅 1080x1080、60fps)。

**两阶段工作流**:先产出一份**人类可读的多轨时间线**(语音 / 字幕 / 素材 / 画面按时间对齐,可单独查看),审查确认后再合成最终视频。审片在前、渲染在后,改稿不需要重跑昂贵的合成。

```
文本 ──plan──▶ timeline.json / timeline.md / preview.html(审片)
                   │ 确认无误
                   ▼
              render ──▶ MP4(真实时间轴对齐)
```

## 安装

依赖:Python 3.12+,以及 **FFmpeg**(未安装可 `winget install Gyan.FFmpeg`;或用环境变量 `FFMPEG_BIN` 指定路径)。

```bash
uv sync            # 安装依赖(edge-tts、pytest)
```

## 两阶段工作流(推荐)

### 阶段 1:plan —— 生成时间线,审片

```bash
# 默认离线估算时间轴(不联网、秒出);产物三件套落在输出目录
uv run text2video plan --file examples/sample.txt --out out/sample

# 打开可视化时间线(多轨对齐,可勾选单独查看;播放 MP3 时逐轨同步高亮)
#   out/sample.preview.html
# 文本分镜表(按轨分节,只读报告;改稿直接改 timeline.json 或源文本重跑 plan)
#   out/sample.timeline.md
# 结构化数据(单一事实源,可 git diff;渲染只消费它)
#   out/sample.timeline.json
```

审片要点:看语音/字幕逐句是否对齐、断行是否合理、画面与总时长是否合适。**一致性自检**(`✅ 全部通过 / ❌ 问题列表`)会拦截明显错误。

> 估算轴用于审内容与布局;真实 edge-tts 的逐字节奏由 render 阶段负责并对齐,
> 因此 mp4 里的逐字点亮是精确的。需要联网预览真实节奏可加 `--real`。

### 阶段 2:render —— 确认后渲染

```bash
# 从 timeline.json 读取全部参数,真实合成语音并对齐渲染
uv run text2video render --timeline out/sample.timeline.json
# → out/sample.mp4
```

- render 只消费 timeline.json,**不再重新做排版决策**(审的就是渲染的);
- 渲染前再次自检,失败默认**拒绝渲染**(`--force` 可跳过,不推荐);
- 渲染后把 timeline.json/md/preview.html **刷新为真实时间轴**,并打印
  "估算 vs 实际"语音轨偏差表,供最终复核。

也可 `plan --real`(联网真实节奏预览)+ `render`,或在 plan 阶段直接指定渲染参数
(画幅/字体/背景等,这些都会写入 timeline.json)。

> 注意:`render` **总会重新真实合成语音**(保证时间轴新鲜、与源文本一致),
> 即使你之前用了 `plan --real`,render 也会再次联网 TTS。

## 快速使用(旧一键用法,保持兼容)

```bash
# 直接传文案
uv run text2video --text "你好,这是第一条文案视频。字幕会跟着语音逐字点亮。"

# 从文件读文案(等价于 plan(realtime) + render 一步完成)
uv run text2video --file examples/sample.txt --out out/sample.mp4

# 也可以 python -m text2video ...
uv run python -m text2video --file examples/sample.txt --out out/sample.mp4
```

## 常用选项(子命令 plan 与一键用法相同;render 单独见上)

| 选项 | 默认 | 说明 |
| --- | --- | --- |
| `--file` / `--text` / `--stdin` | 必选其一 | 文本来源 |
| `--voice` | `zh-CN-XiaoxiaoNeural` | 音色(`--list-voices` 查看,如 `zh-CN-YunxiNeural` 男声) |
| `--rate` `--volume` `--pitch` | `+0%` `+0%` `+0Hz` | 语速/音量/音调 |
| `--style karaoke\|scroll` | `karaoke` | 字幕样式:逐字点亮 / 整段上滚 |
| `--out` | `./output.mp4` | 输出路径(plan 时作为时间线产物基名) |
| `--materials JSON` | 无 | 素材清单文件(素材轨 + BGM,见下文"素材轨与背景音乐") |
| `--landscape` / `--portrait` / `--square` | `square` | 画幅预设:16:9(1920x1080)/ 9:16(1080x1920)/ 1:1(1080x1080);显式 `--width/--height` 优先 |
| `--width` `--height` | 按预设 | 画幅(显式指定后覆盖预设) |
| `--fps` | `60` | 帧率 |
| `--transition SEC` | `0` | 素材段淡入淡出时长(秒;0=硬切,每段内部钳制到段长一半) |
| `--bg-color RRGGBB` | `1B1B2A` | 纯色背景 |
| `--bg-image path` | 无 | 背景图(铺满裁切) |
| `--font` `--font-size` | 自动 `/` 短边÷20 | 字体家族;字号默认=画面短边÷20 向下取整(1080²→54px) |
| `--highlight-color` `--dim-color` | `0xFFFFFF` `0x9A9A9A` | 已念/未念颜色(0xRRGGBB) |
| `--box` | 关 | 字幕加半透明底色框 |
| `--max-chars` | 自动 | 一行最多中文数 |
| `--tts-backend edge\|sapi\|auto` | `auto` | 语音引擎:edge=联网 edge-tts;sapi=Windows 本地语音(离线,逐字轴近似);auto=优先 edge,失败自动回退 sapi |
| `--tts-cache-dir PATH` | `%LOCALAPPDATA%\text2video\tts_cache` | TTS 缓存目录(同参数文本合成只联网一次) |
| `--no-tts-cache` | 关 | 禁用 TTS 缓存 |
| `--split-tracks` | 关 | 渲染后额外输出 `{out}.video.mp4`(纯画面)/ `{out}.audio.mp3`(纯音频) |
| `--config PATH` | 无 | TOML 配置文件(默认查找当前目录 `text2video.toml`,见下文"配置文件") |
| `--skip-intermediate` | 关 | 渲染后删除中间 mp3/ass |

`render` 子命令专用:`--timeline XXX.json`(必填)、`--out`(默认=timeline 基名.mp4)、
`--force`(忽略自检强制渲染)、`--skip-intermediate`、`--split-tracks`、
`--tts-backend`/`--tts-cache-dir`/`--no-tts-cache`(覆盖 timeline 中的设置)。

### 配置文件(text2video.toml)

把常用参数写进 TOML(键与 CLI 参数同名,下划线式),命令行显式参数优先:

```toml
[text2video]
voice = "zh-CN-XiaoxiaoNeural"
bg_color = "1B1B2A"
width = 1920
height = 1080
transition = 0.3
tts_backend = "auto"
```

默认读取当前目录 `text2video.toml`;`--config PATH` 显式指定。配置仅影响命令行未显式给出的参数。

### 封面图(text2video cover)

生成 B站 16:9 封面 PNG:背景(纯色/背景图)+ 标题大字(默认取文案首句前 20 字,可 `--title` 覆盖)+ 可选副标题:

```bash
# 从 timeline.json 提取画幅/背景/字体
uv run text2video cover --timeline out/sample.timeline.json --title "标题" --subtitle "副标题" --out out/sample_cover.png
# 或直接给文案与画幅参数
uv run text2video cover --text "文案……" --title "标题" --landscape --out out/cover.png
```

- `--timeline` 模式下画幅/背景/字体取自 timeline(此时 `--landscape`/`--width` 等无效);
  要按其他画幅出封面,请改用文案模式直接传画幅参数。
- 输出 PNG(默认 1920x1080);`--out` 以 `.png`/`.jpg` 结尾时按扩展名决定输出格式
  (ffmpeg 按扩展名选编码器)。可复制进 `--materials` 作为封面素材复用。

### 环境诊断(text2video doctor)

```bash
uv run text2video doctor            # 检查 ffmpeg/ffprobe、字体、edge-tts、SAPI、缓存目录、配置
uv run text2video doctor --online   # 额外测试 edge-tts 网络连通
```

## 素材轨与背景音乐(materials.json)

`plan` / 一键用法加 `--materials materials.json`,即可在文案时间线上**按段落全屏覆盖
视频/图片素材**,并**混入背景音乐**。格式(示例见 `examples/materials.sample.json`,
素材路径相对该文件所在目录):

```json
{
  "assets": [
    {"id": "clip",  "path": "clips/intro.mp4", "type": "video"},
    {"id": "pic",   "path": "images/bg.png",   "type": "image"},
    {"id": "music", "path": "audio/bgm.mp3",   "type": "audio"}
  ],
  "material": [
    {"start": 0.0, "end": 3.0, "asset": "clip", "in": 5.0, "out": 8.0,
     "crop": {"x": 0.5, "y": 0.0}},
    {"start": 3.0, "end": 8.0, "asset": "pic"}
  ],
  "bgm": {"asset": "music", "volume": 0.3},
  "annotations": [
    {"start": 4.0, "end": 7.0, "x": 0.1, "y": 0.2, "w": 0.5, "h": 0.4,
     "color": "0xFFFFFF"}
  ]
}
```

- `material` 段:在时间线 `start..end` 区间全屏覆盖指定素材;视频段用 `in/out`
  指明素材内裁剪区间(**裁剪长度须与时间线区间一致**,容差 0.1s,保证渲染无拖尾);
  图片段无需 `in/out`;未覆盖区间保持 `--bg-color` / `--bg-image` 背景。
- **画面裁剪**(`crop`,图片/视频均可,可选):`{"x", "y"}` 只在**素材比例与画幅不匹配**
  时选择取哪一部分——
  - 素材按输出画幅**等比缩放到一条边贴满画幅**(cover,自动、无黑边),窗口比例恒 =
    画幅;素材比例与画幅一致时全图铺满、无需裁剪;
  - `x`/`y`(**0~1,默认 0.5 = 居中**):窗口在素材**多出来的那条轴**上的位置
    (素材更宽 → `x` 左右滑动;素材更高 → `y` 上下滑动),**默认居中**即可得到
    "中心缩放到无黑边"的标准构图;
  - 旧版 `zoom`(放大倍数)已移除,**出现即报错**,请从配置中删去;
  - 窗口永不越界;仅 video/image 可带,audio 素材仍只能用于 bgm。
- **审片标注窗口**:plan 会对带 crop 的素材探测分辨率,把窗口相对素材的几何
  (`data.frame`)写进 timeline;`preview.html` 在**素材缩略图上用红框标出**
  每段实际取用的画面区(不取到的情况一眼可见)。
- **画面标注框**(`annotations`,可选):在**最终画面**上叠加矩形框(如圈出重点),
  每项 `{"start": 起秒, "end": 止秒, "x"/"y"/"w"/"h": 画幅比例, "color": "0xRRGGBB"
  (可选,默认红)}`;渲染时以 drawbox 叠加在**字幕之下**(线宽 6px),只在该时段可见。
- `bgm`:指定音频素材低音量垫底(默认 0.25),渲染时预混进配音(需要 ffmpeg ≥ 6.1,
  使用 `amix normalize=0` 避免配音被音量归一化削弱)。
- 素材一律保留在 timeline.json(素材轨 + 素材库),审片产物(preview.html / md)
  会显示素材轨与裁剪区间;`render` 只消费 timeline.json,无需再传清单。
- 素材段的时间窗口按**绝对秒数**(start~end)铺在时间线上,**不随 TTS 真实时长
  伸缩**:估算轴短于真实轴时素材覆盖开头、尾部露出背景属正常;真实轴显著短于
  估算时(超过自检容差)渲染会被拒绝,需调整素材窗口后重跑。

## 时间线中间产物

```
out/sample.timeline.json     # 单一事实源:meta(渲染参数)+ 源文本 + 多轨(语音/字幕/素材/画面)+ 自检结果
out/sample.timeline.md       # 文本分镜表:元信息 / 素材库 / 自检 / 各轨道 分节
out/sample.preview.html      # 自包含单文件多轨时间线浏览器(零依赖,双击即开)
```

`preview.html` 支持:多轨并行时间轴、逐轨 checkbox **单独查看**、播放 MP3
(有音频时)或虚拟时钟(估算模式)同步高亮当前段、点击时间轴跳转、实时显示当前字幕。
渲染后会自动刷新为真实时间轴并附带音频,供最终试听复核。

## 文件说明

```
src/text2video/
  cli.py        # 入口:plan / render / cover / doctor 子命令 + 旧一键用法(+ 配置合并)
  config.py     # TOML 配置文件加载(text2video.toml,命令行优先级最高)
  tts.py        # edge-tts 合成语音 + 逐字时间轴 + 缓存 + 指数退避重试
  tts_local.py  # SAPI(Windows 本地语音)离线合成 + 逐字事件解析/估算降级
  timeline.py   # 多轨时间线模型 + 素材清单解析/校验 + 离线估算轴 + 一致性自检
  report.py     # timeline → markdown 分镜表 + 自包含 preview.html
  layout.py     # 字体/字号/每行字数的纯计算(plan 与 render 共用,保证一致)
  cover.py      # 封面图:标题 ASS + 单帧渲染 PNG(16:9 默认)
  doctor.py     # 环境诊断(doctor 子命令,逐项 PASS/WARN/FAIL)
  render.py     # 消费 timeline.json:真实 TTS(edge/sapi/auto)→ 素材/BGM → ASS → ffmpeg
  subs.py       # 时间轴 -> ASS 字幕(纯函数,两种样式;封面共用头部模板)
  compose.py    # 定位 ffmpeg / 组装合成命令(转场 fade / 音画分离)
tests/          # subs/timeline/report/compose/config/cover/doctor/tts 单测 + cli 回归
examples/sample.txt        # 示例文案
examples/materials.sample.json  # 素材清单模板(素材轨 + BGM)
```

## 已知说明

- **TTS 缓存**:同参数(文本+音色+语速音量音调)合成结果缓存于 `%LOCALAPPDATA%\text2video\tts_cache`
  (可 `--tts-cache-dir` 改、`--no-tts-cache` 关);缓存命中时 render 跳过联网合成,
  输出 mp3 与逐字时间轴即缓存内容。edge-tts 版本变化不影响命中(缓存不掺版本)。
- **网络重试**:edge-tts 合成失败自动重试(最多 3 次,间隔 1s/2s,第 3 次失败后抛出);
  `--tts-backend auto` 时抛出前自动回退本地 SAPI 并告警,`edge` 时直接抛出。
- **SAPI 本地语音**(`--tts-backend sapi`):走 Windows System.Speech,零新增依赖、完全离线;
  音质与逐字轴精度依赖系统语音包——实测 Huihui Desktop 的 CharacterIndex/AudioPosition
  事件不可靠,会自动降级为"估算轴×实测时长"缩放(逐字点亮为近似,README 已知说明也列于此)。
  建议作为断网兜底,日常仍用 edge。
- **画面裁剪(`crop`)**:plan 阶段做比例语义校验(与素材分辨率无关,离线可审),并对带
  crop 的素材探测分辨率、把窗口几何写进时间线供审片标注(探测失败仅告警);
  render 时同样用 ffprobe 探测分辨率换算像素裁剪窗口,探测失败(损坏/无视频流)
  归入自检并拒绝渲染(`--force` 可跳过,此时该段退化为满幅铺满)。裁剪是空间操作,
  与 `in/out` 时间裁剪正交,可同时使用。旧版 `zoom` 已在 v4 移除,出现即报错。
- **画面标注框(`annotations`)**:以 `drawbox` 滤镜(线宽 6px,颜色默认红色、可自定义)
  叠加在**字幕之下、素材之上**,不会遮挡字幕;时间/位置/颜色自检非法会阻止渲染。
- **转场**:`--transition 0.3` 给素材段加 alpha 淡入/淡出(与背景交叉淡化);时长钳制在
  段长一半内,不改变素材窗口与自检语义。
- **音画分离**:`--split-tracks` 额外输出 `{base}.video.mp4`(无音轨,流拷贝)与
  `{base}.audio.mp3`(含 BGM 时已混合),便于剪辑软件二次使用。
- 素材视频段的 `in/out` 裁剪长度必须与时间线上该段的区间长度一致(容差 0.1s),
  否则自检拒绝;这是为保持 overlay 渲染的视觉确定性(素材提前结束会闪回背景)。
- BGM 混音使用 `amix normalize=0`,需要 **ffmpeg ≥ 6.1**(Gyan.FFmpeg 当前版本满足)。
- edge-tts 为微软在线服务;`plan` 默认离线估算**不联网**,`render` 需联网真实合成
  (缓存/SAPI 可豁免)。
- **字幕样式**:默认白字 + 黑色描边(3px,浅色背景也清晰)+ 1px 深投影,**无背景框**;
  `--box` 可加半透明底色框(深色背景或素材杂乱时用)。
- **逐字点亮**来自 edge-tts 的 WordBoundary:对中文实测为**单字级**(每个汉字一条时间戳),英文为单词级,因此 karaoke 样式能实现真正的"念到哪个字点亮哪个字"。
- 字幕字体默认使用系统中文字体(Windows 复制"微软雅黑"到工作目录供 libass 加载,无需安装字体);Linux/macOS 上优先 `Noto Sans CJK` 等路径,找不到时回退系统默认并打日志告警。
- 视频总时长以 ffprobe 实测音频时长为准,字幕时间轴与 `-t` 使用同一时长,避免结尾错位或截断。
- scroll 走字幕样式因 ffmpeg 集成的 libass 对屏幕外 `\move`/深负偏移渲染有缺陷,改为逐行 `\pos` 分片实现匀速滚动(视觉等价)。
- **估算 vs 真实**:估算轴按语速均分 token,实测比真实轴整体偏慢约 1s(中文含句间停顿);审布局/总时长随之留有余量,最终对齐以 render 的真实轴为准。
