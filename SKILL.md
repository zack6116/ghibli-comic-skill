---
name: ghibli-comic
description: 用 Gemini 把一个主题变成宫崎骏/吉卜力风格的单张封面或多格漫画，并可一键上传微信公众号素材库取 thumb_media_id。当用户说「做张宫崎骏封面」「吉卜力漫画」「用 Ghibli 风格画」「公众号封面」「把文章变成漫画」「长图漫画」时使用。
license: MIT
---

# Ghibli Comic

把「一个主题」变成吉卜力风（宫崎骏风）**单张封面**或**多格漫画**。

底层走 Gemini：默认用 **Gemini Web 逆向 API**（花你的会员额度，不消耗付费 API 额度），
也可切 Google AI Studio API Key。

## 目录

`{baseDir}` = 本 SKILL.md 所在目录。主脚本：`{baseDir}/scripts/ghibli_comic.py`。

```bash
PY=/Users/zack/.workbuddy/binaries/python/envs/default/bin/python3

# 单张封面
$PY {baseDir}/scripts/ghibli_comic.py --topic "AI 编程工具横评"

# 4 页多格漫画 + 拼成长图
$PY {baseDir}/scripts/ghibli_comic.py --topic "一个人做产品的第100天" --mode comic --pages 4 --stitch

# 只看 prompt 不花钱
$PY {baseDir}/scripts/ghibli_comic.py --topic "..." --dry-run
```

## 两种模式

### cover — 单张封面

一个主题 → 一张吉卜力风插画。适合公众号首条大图（2.35:1）。

```bash
$PY scripts/ghibli_comic.py --topic "AI 编程工具横评" --aspect 2.35:1 --upload
```

`--upload` 会把图传进微信素材库，直接拿到封面用的 `thumb_media_id`。

### comic — 多格漫画

主题 → **LLM 规划分镜** → 逐页出图 → 可选竖向拼成长图。

```bash
$PY scripts/ghibli_comic.py \
  --topic "一个人做产品的第100天" \
  --mode comic --pages 4 --layout mixed --stitch \
  --outdir ./ghibli-day100
```

产物结构：

```
ghibli-day100/
├── storyboard.json        # 分镜表（可复现记录）
├── 00-ghibli-day100-long.jpg   # 拼好的长图（--stitch）
└── pages/
    ├── 01-prompt.md       # 每页的完整 prompt
    ├── 01-ghibli-....jpg
    └── ...
```

**画风一致性**是第一页出图后被当作 `--reference` 传给后续页，配合 LLM 规划出的
`character_note`（主角外貌/衣着）来锁住角色。

## 分格布局

| `--layout` | 每页结构 |
|---|---|
| `four-panel` | 2×2 四格，最经典 |
| `standard` | 5-6 格，顶部一张大 establishing |
| `cinematic` | 3 张横向宽格，像分镜表 |
| `splash` | 单张满版，无分格 |
| `mixed` | 4-6 格大小混排，一张主图+反应镜头（默认） |

## 场景预设

按关键词自动匹配，也可用 `--preset` 强制指定。

| preset | 画面基调 | 触发关键词举例 |
|---|---|---|
| `countryside` | 夏日乡野、稻田、远山 | 乡/村/成长/童年/故乡 |
| `sky` | 积云、俯瞰、视野 | 云/飞行/远眺/格局/航天 |
| `town` | 黄昏老街、店铺暖光 | 城市/街/夜市/烟火 |
| `sea` | 海边悬崖、浪、白护栏 | 海/浪/岛/旅行/港口 |
| `forest` | 苔藓巨木、光斑 | 森林/树/生态/自然 |
| `workshop` | 深夜工作室、图纸、台灯 | 代码/AI/agent/开发/产品/开源 |
| `station` | 清晨乡站、长椅、蒸汽 | 等待/告别/重逢/旅途 |
| `library` | 环形书架、穹顶光尘 | 书/知识/文档/学习/检索 |

匹配不上默认落 `countryside`。

## 后端

| 后端 | 花谁 | 前置条件 |
|---|---|---|
| `gemini-web`（默认） | **Gemini 会员额度** | 已登录 gemini.google.com + 同意书 |
| `google-api` | 付费 API | `GEMINI_API_KEY` |

**模型**（这两个是当前实测可用的）：

- 画图：`gemini-3.0-pro`（默认）
- 分镜规划：`gemini-3.0-flash`

可用 `--model` / `--text-model` 覆盖，也可用环境变量
`GHIBLI_IMAGE_MODEL` / `GHIBLI_TEXT_MODEL`。

⚠️ **`gemini-3-pro-image` 已失效**，别用。报错原文：
`Unknown model name. Available models: unspecified, gemini-3.0-pro, gemini-3.0-flash,
gemini-3.0-flash-thinking, gemini-3.1-pro-preview`

### gemini-web 首次使用前

必须存在同意书 `~/Library/Application Support/baoyu-skills/gemini-web/consent.json`
且 `accepted: true`、`disclaimerVersion: "1.0"`。没有就先征求用户同意再创建，别默默绕过。

验证可用性（会弹 Chrome，需要时手动登录一次 Gemini）：

```bash
cd <baoyu-danger-gemini-web 所在目录> && \
  /Users/zack/.workbuddy/binaries/node/versions/22.22.2-6/bin/npx -y bun scripts/main.ts --login
```

报 `SECURE_1PSIDTS could get expired frequently` = cookie 过期，重新跑上面那条 `--login`。

## 参数表

| 参数 | 说明 | 默认 |
|---|---|---|
| `--article <md>` | 从 Markdown 抽主题 | — |
| `--topic <text>` | 直接指定主题 | — |
| `--title <text>` | 只有标题时用 | — |
| `--input-image <path>` | 跳过生成，只做后处理/上传 | — |
| `--mode` | `auto` / `cover` / `comic` | `auto`（`--pages>1` 则 comic） |
| `--pages <n>` | 漫画页数 | `4` |
| `--layout` | 见上表 | `mixed` |
| `--stitch` | 多页竖向拼成长图 | 关 |
| `--aspect` | `2.35:1`/`16:9`/`1:1`/`4:3`/`3:4` | cover=`2.35:1`，comic=`3:4` |
| `--preset` | 场景预设 | `auto` |
| `--backend` | `auto`/`gemini-web`/`google-api` | `auto` |
| `--model` | 画图模型 | `gemini-3.0-pro` |
| `--text-model` | 分镜规划模型 | `gemini-3.0-flash` |
| `--out <path>` | 单图输出路径 | 自动 |
| `--outdir <path>` | 漫画输出目录 | 自动 |
| `--upload` | 上传微信素材取 thumb_media_id | 关 |
| `--meta <json>` | 结果写 JSON | — |
| `--keep-raw` | 保留 Gemini 原始输出 | 关 |
| `--dry-run` | 只打印 prompt，不调模型 | 关 |

## 输出尺寸

| aspect | 尺寸 | 用途 |
|---|---|---|
| `2.35:1` | 1800×766 | 公众号首条大图 |
| `16:9` | 1920×1080 | 通用宽图 |
| `1:1` | 800×800 | 公众号次条 / 头像 |
| `4:3` | 1600×1200 | 横版配图 |
| `3:4` | 1200×1600 | 漫画页默认 |

## 已知坑

- ⚠️ **Gemini 不认 2.35:1**。脚本会先生成 21:9 再裁到 2.35:1，别直接让它出 2.35:1。
- ⚠️ **漫画模式下 `--aspect` 默认改成 3:4**。传了 `2.35:1` 也会被覆写，
  因为漫画页竖版更合理。想竖版以外的用 `--aspect` 指定其它值。
- ⚠️ **cookie 会过期**。逆向 API 的固有问题，报
  `SECURE_1PSIDTS could get expired frequently` 就跑一次 `--login`。
- ⚠️ **零文字**。所有 prompt 硬编码 `no text, no letters, no watermark, no speech bubbles`。
  吉卜力风 + 文字 = 模型大概率画成乱码。要带中文标题的封面，别用这个，
  用 ImageGen（混元）那条路。
- ⚠️ **比例裁剪中心偏上**（`centering=(0.5, 0.45)`），防止地平线被切。
- **代理**：本机 Google 接口走 `http://127.0.0.1:7890`。脚本依次探测
  `HTTPS_PROXY` → 7890 → 直连，取第一个能通 Google 的。微信接口直连（不走代理）。
- **个人订阅号**没有群发接口。推到草稿箱后要用户自己在后台点发布。
- 公众号 **2.35:1 只在首条生效**，次条是 1:1。
- 多页漫画很吃额度：`--pages 4` = 4 次出图 + 1 次分镜规划。
  先用 `--dry-run` 确认 prompt，满意了再实跑。
- **角色一致性不是 100%**。第一页作参考 + `character_note` 能锁得比较稳，
  但翻页后细微差异仍可能出现。这是当前模型能力的上限，不是脚本 bug。