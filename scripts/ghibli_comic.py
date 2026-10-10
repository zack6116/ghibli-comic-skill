#!/usr/bin/env python3
"""Ghibli Comic — 用 Gemini 把一个主题变成吉卜力风封面 / 多格漫画 / 长图漫画。

主题 → (可选 LLM 规划分镜) → Gemini 出图 → 裁剪拼接 → 可选上传微信素材取 thumb_media_id

用法见 README.md 或 SKILL.md。
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

HOME = Path.home()
WX_CONFIG = Path(os.environ.get("WX_MP_CONFIG", HOME / ".wechat-mp.json"))

PROXY_CANDIDATES = [
    os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"),
    "http://127.0.0.1:7890",
    None,  # 直连
]

GOOGLE_PROBE = "https://generativelanguage.googleapis.com/v1beta/models?alt=json"

# gemini-3-pro-image 已下线（2026-10-05 实测），这两个是当前可用且实测稳定的
DEFAULT_IMAGE_MODEL = os.environ.get("GHIBLI_IMAGE_MODEL", "gemini-3.0-pro")
DEFAULT_TEXT_MODEL = os.environ.get("GHIBLI_TEXT_MODEL", "gemini-3.0-flash")


# --------------------------------------------------------------------------
# 代理
# --------------------------------------------------------------------------
def pick_proxy():
    """依次探测候选代理，返回第一个能连上 Google 的。"""
    for p in PROXY_CANDIDATES:
        if p and p.startswith("$"):
            continue
        handler = urllib.request.ProxyHandler({"http": p, "https": p}) if p else urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(handler)
        try:
            req = urllib.request.Request(GOOGLE_PROBE, headers={"User-Agent": "curl/8"})
            with opener.open(req, timeout=8):
                return p, opener
        except urllib.error.HTTPError:
            # 401/403 都算通，说明网络可达
            return p, opener
        except Exception:
            continue
    return None, urllib.request.build_opener(urllib.request.ProxyHandler({}))


PROXY, OPENER = pick_proxy()


def http_json(url, data=None, headers=None, timeout=300):
    h = {"User-Agent": "curl/8"}
    if headers:
        h.update(headers)
    body = json.dumps(data).encode() if data is not None else None
    if body:
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=h)
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001
        return -1, str(e).encode()


# --------------------------------------------------------------------------
# 主题抽取
# --------------------------------------------------------------------------
STOPWORDS = set("""
的 了 和 是 在 也 就 都 而 及 与 这 那 有 被 把 给 从 到 对 为 以 个 上 下 中 我们 你们 他们
如何 什么 为什么 怎么 可以 需要 一个 一篇 那些 这些 之后 之前 现在 今天 其实 真的 还是 因为 所以
什么 怎么 为什么 哪些 一个 一种 以及 或者 通过 进行 对于 关于
what how why the a an and or but if to of in on for with is are was were be been this that these those
""".split())


def extract_topic(md_path: Path) -> tuple[str, list[str]]:
    text = md_path.read_text(encoding="utf-8", errors="ignore")
    text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S)  # front matter

    title = ""
    m = re.search(r"^#\s+(.+)$", text, re.M)
    if m:
        title = m.group(1).strip()
    else:
        m = re.search(r"title:\s*[\"']?(.+?)[\"']?\s*$", text, re.M)
        if m:
            title = m.group(1).strip()

    headings = re.findall(r"^#{2,3}\s+(.+)$", text, re.M)
    body = re.sub(r"^#.*$", "", text, flags=re.M)
    keywords = _keywords(title + " " + " ".join(headings) + " " + body[:4000])
    return title or md_path.stem, keywords


def _keywords(text: str, top: int = 8) -> list[str]:
    words = re.findall(r"[一-龥]{2,4}|[A-Za-z][A-Za-z0-9+#.\-]{2,}", text)
    freq: dict[str, int] = {}
    for w in words:
        lw = w.lower()
        if lw in STOPWORDS or len(lw) < 2:
            continue
        freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:top]]


# --------------------------------------------------------------------------
# 场景预设
# --------------------------------------------------------------------------
SCENES = {
    "countryside": "rolling summer countryside: terraced rice fields, a dirt path climbing to a lone tree, distant blue mountains, tall grass bending in the wind",
    "sky": "vast sky filled with towering cumulus clouds, seen from high above, tiny fields and a river far below, soft shafts of sunlight",
    "town": "a nostalgic small town at golden hour: tiled roofs, laundry lines, a bakery window spilling warm orange light onto cobblestones",
    "sea": "seaside cliff with a white railing, deep blue sea and white surf, seagulls, hydrangeas along the path, bright summer haze",
    "forest": "ancient mossy forest, giant camphor tree, dappled sunlight falling through the canopy, ferns and small stone path",
    "workshop": "a cluttered attic workshop late at night: blueprints, brass gears, a single warm desk lamp, dust motes floating in the lamplight",
    "station": "a quiet rural train station at dawn: wooden benches, a single platform lamp, steam curling from a teacup, tracks vanishing into mist",
    "library": "a tall circular library: spiraling wooden shelves, a domed glass ceiling, dust motes in slanted light, a forgotten globe",
}

PRESET_HINTS = {
    "countryside": ["乡", "村", "田", "田野", "成长", "慢", "生活", "童年", "徒步", "故乡"],
    "sky": ["飞行", "云", "天空", "飞机", "远眺", "视野", "格局", "航天", "航"],
    "town": ["城市", "街", "老街", "店铺", "社区", "小店", "夜市", "烟火", "摊"],
    "sea": ["海", "浪", "岛", "旅行", "远洋", "潮汐", "港口", "航海"],
    "forest": ["森林", "树", "生态", "自然", "环保", "苔", "苔藓", "丛林"],
    "workshop": ["代码", "编程", "开发", "工具", "工程", "我做了", "产品", "机械", "调试", "制造",
                 "ai", "agent", "api", "cli", "服务器", "插件", "skill", "模型", "部署", "开源"],
    "station": ["等待", "错过", "告别", "重逢", "火车", "journey", "旅途", "站台", "离别"],
    "library": ["书", "阅读", "知识", "文档", "论文", "学习", "文档", "检索", "百科"],
}


def choose_preset(topic: str, keywords: list[str], override: str | None) -> str:
    if override and override != "auto":
        return override if override in SCENES else "countryside"
    hay = (topic + " " + " ".join(keywords)).lower()
    for name, hints in PRESET_HINTS.items():
        if any(h.lower() in hay for h in hints):
            return name
    return "countryside"


GHIBLI_BASE = (
    "Studio Ghibli anime film still, hand-painted cel animation aesthetic, "
    "Kazuo Oda style watercolor background painting, soft pastel palette, "
    "gentle warm afternoon light, delicate linework, lush detailed foliage, "
    "breathtaking cinematic composition, "
    "absolutely no text, no letters, no watermark, no logo, no signature, no speech bubbles, no UI elements"
)

GHIBLI_COMIC_BASE = (
    "Studio Ghibli manga page, hand-painted watercolor and soft cel shading, "
    "Kazuo Oda style delicate linework, muted warm pastel palette with aged paper texture, "
    "gentle natural light, expressive but simple character faces with large calm eyes, "
    "clean panel gutters, hand-inked panel borders, "
    "absolutely no text, no letters, no sound effects, no speech bubbles, no watermark, no logo, no signature"
)


# --------------------------------------------------------------------------
# 模式一：单张封面
# --------------------------------------------------------------------------
def build_cover_prompt(topic: str, keywords: list[str], preset: str, aspect: str) -> str:
    scene = SCENES[preset]
    flavor = f"The scene should quietly echo this theme without depicting it literally: {topic}."
    if keywords:
        flavor += " Motifs to weave in subtly: " + ", ".join(keywords[:5]) + "."
    # Gemini 不认 2.35:1，用最接近的 21:9 生成再裁
    ratio_line = {
        "2.35:1": "Ultra-wide 21:9 panoramic aspect ratio, 2.35:1 crop-safe, keep the horizon in the upper third and leave empty sky/negative space.",
        "16:9": "16:9 widescreen aspect ratio.",
        "1:1": "Square 1:1 composition.",
        "4:3": "4:3 classic frame.",
    }.get(aspect, "")

    return (
        f"An original illustration in the visual language of Studio Ghibli feature films.\n\n"
        f"SETTING: {scene}.\n"
        f"{flavor}\n"
        f"STYLE: {GHIBLI_BASE}.\n"
        f"FRAMING: {ratio_line}\n"
        f"MOOD: nostalgic, calm, quietly hopeful.\n"
        f"IMPORTANT: This is an original artwork inspired by an animation art style — "
        f"do not reproduce any copyrighted character and do not copy any existing film frame."
    )


# --------------------------------------------------------------------------
# 模式二：分镜规划（LLM）
# --------------------------------------------------------------------------
LAYOUT_RULES = {
    "four-panel": "exactly 4 panels in a 2x2 grid, clean straight gutters",
    "standard": "5 to 6 panels of varying sizes in a traditional manga page flow, one large establishing panel at top",
    "cinematic": "3 wide horizontal panels stacked vertically, like film storyboards",
    "splash": "one single full-bleed panel with no subdivision",
    "mixed": "4 to 6 panels with varied sizes, one dominant panel and smaller reaction shots",
}


def plan_storyboard(topic: str, keywords: list[str], pages: int, layout: str) -> dict:
    """用 Gemini 文本模型规划分镜。失败返回空 dict。"""
    prompt = (
        f"You are a storyboard artist for a Studio Ghibli style comic.\n\n"
        f"Topic: {topic}\n"
        f"Key motifs: {', '.join(keywords[:6]) or 'none given'}\n"
        f"Pages: {pages}\n"
        f"Layout per page: {layout} ({LAYOUT_RULES.get(layout, LAYOUT_RULES['mixed'])})\n\n"
        f"Write a {pages}-page storyboard that tells this topic as a quiet visual narrative.\n\n"
        f"Rules:\n"
        f"- Each page is ONE illustration containing multiple comic panels.\n"
        f"- Describe the panel layout explicitly for each page.\n"
        f"- Describe each panel's scene concretely: place, light, weather, objects, body posture.\n"
        f"- Recurring motifs should reappear across pages to create visual continuity.\n"
        f"- Do NOT write any dialogue, narration or sound effects — the art carries the story.\n"
        f"- Each page's description must be under 60 words.\n\n"
        f'Reply with ONLY valid JSON, no markdown fence, no commentary. Schema:\n'
        f'{{"title":"short evocative title in Chinese","character_note":"one sentence describing '
        f'the recurring protagonist (appearance, clothing, age) so all pages stay consistent",'
        f'"pages":[{{"page":1,"layout":"{layout}","scene":"...",'
        f'"continuity":"what ties this page to the previous one"}}]}}'
    )
    raw = gemini_text(prompt)
    if not raw:
        return {}
    return _parse_json(raw)


def _parse_json(raw: str) -> dict:
    """从模型输出里抠出 JSON。容忍 ```json 包裹和前后废话。"""
    txt = raw.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", txt, re.S)
    if m:
        txt = m.group(1).strip()
    start = txt.find("{")
    end = txt.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(txt[start:end + 1])
    except json.JSONDecodeError:
        # 尾逗号 / 单引号容错
        cleaned = re.sub(r",\s*([}\]])", r"\1", txt[start:end + 1])
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return {}


# --------------------------------------------------------------------------
# Prompt：漫画页
# --------------------------------------------------------------------------
def build_page_prompt(plan: dict, page_spec: dict, idx: int, total: int, preset: str) -> str:
    scene = SCENES[preset]
    layout = page_spec.get("layout", "mixed")
    layout_desc = LAYOUT_RULES.get(layout, LAYOUT_RULES["mixed"])
    title = plan.get("title", "")
    char = plan.get("character_note", "")
    continuity = page_spec.get("continuity", "")

    parts = [
        f"Page {idx} of {total} of a {total}-page Studio Ghibli comic strip about: {plan.get('topic', '')}.".strip(),
        f"SCENE ANCHOR: {scene}.",
        f"COMIC LAYOUT: {layout_desc}.",
    ]
    if char:
        parts.append(
            f"RECURRING CHARACTER (must look identical in every panel of every page): {char}. "
            f"Simple watercolor figure, understated face, consistent clothing colors."
        )
    if continuity:
        parts.append(f"CONTINUITY FROM PREVIOUS PAGE: {continuity}")
    parts.append(
        f"PANEL SCENE: {page_spec.get('scene', '')}\n"
        f"Draw the panels with clear visual variety: vary the camera distance (wide establishing / "
        f"medium / close-up on hands or an object), and vary the light source between panels."
    )
    parts.append(f"STYLE: {GHIBLI_COMIC_BASE}.")
    parts.append("IMPORTANT: original artwork inspired by an animation art style — do not reproduce "
                 "any copyrighted character and do not copy any existing film frame.")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# 后端：Gemini Web（会员额度）
# --------------------------------------------------------------------------
def find_bun() -> list[str] | None:
    import shutil

    b = shutil.which("bun")
    if b:
        return [b]
    npx = shutil.which("npx") or "/Users/zack/.workbuddy/binaries/node/versions/22.22.2-6/bin/npx"
    if Path(npx).exists():
        return [npx, "-y", "bun"]
    return None


def find_gemini_web_script() -> Path | None:
    for base in [HOME / ".claude/skills", HOME / ".workbuddy/skills",
                 HOME / ".codex/skills", Path.cwd()]:
        p = base / "baoyu-danger-gemini-web/scripts/main.ts"
        if p.exists():
            return p
    return None


def gemini_web_ready() -> tuple[bool, str]:
    script = find_gemini_web_script()
    if not script:
        return False, "未找到 baoyu-danger-gemini-web skill（gemini-web 后端依赖它）"
    consent = HOME / "Library/Application Support/baoyu-skills/gemini-web/consent.json"
    try:
        d = json.loads(consent.read_text())
    except Exception:  # noqa: BLE001
        return False, f"缺少逆向 API 使用同意书：{consent}"
    if not d.get("accepted") or d.get("disclaimerVersion") != "1.0":
        return False, f"同意书未通过：{consent}"
    if not find_bun():
        return False, "未找到 bun 或 npx"
    return True, "ok"


def run_gemini_web(bun: list[str], script: Path, prompt: str, model: str,
                   image_out: Path | None = None, refs: list[Path] | None = None) -> str:
    cmd = bun + [str(script), "--prompt", prompt, "--model", model]
    if image_out:
        cmd += ["--image", str(image_out)]
    if refs:
        cmd += ["--reference"] + [str(r) for r in refs]
    env = dict(os.environ)
    node_bin = "/Users/zack/.workbuddy/binaries/node/versions/22.22.2-6/bin"
    env["PATH"] = node_bin + ":" + env.get("PATH", "")
    if PROXY:
        env["HTTP_PROXY"] = PROXY
        env["HTTPS_PROXY"] = PROXY
        env["http_proxy"] = PROXY
        env["https_proxy"] = PROXY
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=900)
    if proc.returncode != 0:
        raise RuntimeError(f"gemini-web 失败({proc.returncode}): {(proc.stderr or proc.stdout)[-600:]}")
    return proc.stdout


def gemini_text(prompt: str, model: str | None = None) -> str | None:
    """调 Gemini 文本模型。失败时打印原因并返回 None（调用方决定是否降级）。"""
    bun = find_bun()
    if not bun:
        print("[text] 未找到 bun / npx，跳过分镜规划")
        return None
    script = find_gemini_web_script()
    if not script:
        print("[text] 未找到 baoyu-danger-gemini-web，跳过分镜规划")
        return None
    try:
        out = run_gemini_web(bun, script, prompt, model or DEFAULT_TEXT_MODEL)
        return (out or "").strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[text] 分镜规划失败：{str(exc)[-300:]}")
        return None


def gen_gemini_web(prompt: str, model: str, out: Path, aspect: str,
                   refs: list[Path] | None = None) -> Path:
    ok, why = gemini_web_ready()
    if not ok:
        raise SystemExit(f"[gemini-web] {why}")
    bun, script = find_bun(), find_gemini_web_script()
    ratio_map = {"2.35:1": "21:9", "16:9": "16:9", "1:1": "1:1", "4:3": "4:3", "3:4": "3:4"}
    full = prompt + f"\n\nOutput aspect ratio: {ratio_map.get(aspect, '16:9')}."
    tmp = out.with_suffix(".raw.png")
    run_gemini_web(bun, script, full, model=model, image_out=tmp, refs=refs)
    if not tmp.exists():
        raise RuntimeError("gemini-web 未产出图片文件")
    return tmp


# --------------------------------------------------------------------------
# 后端：Google AI Studio API Key
# --------------------------------------------------------------------------
def gen_google_api(prompt: str, model: str, out: Path, aspect: str) -> Path:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise SystemExit("[google-api] 未设置 GEMINI_API_KEY")
    ratio_map = {"2.35:1": "21:9", "16:9": "16:9", "1:1": "1:1", "4:3": "4:3", "3:4": "3:4"}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": ratio_map.get(aspect, "16:9")},
        },
    }
    st, raw = http_json(url, body, headers={"x-goog-api-key": key}, timeout=420)
    if st != 200:
        raise RuntimeError(f"google-api 失败 {st}: {raw[:500]!r}")
    try:
        parts = json.loads(raw)["candidates"][0]["content"]["parts"]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"google-api 响应解析失败: {raw[:400]!r}") from exc
    for p in parts:
        b64 = p.get("inlineData", {}).get("data")
        if b64:
            tmp = out.with_suffix(".raw.png")
            tmp.write_bytes(base64.b64decode(b64))
            return tmp
    raise RuntimeError("google-api 未返回图片数据")


# --------------------------------------------------------------------------
# 后处理
# --------------------------------------------------------------------------
TARGET_SIZE = {
    "2.35:1": (1800, 766),
    "16:9": (1920, 1080),
    "1:1": (800, 800),
    "4:3": (1600, 1200),
    "3:4": (1200, 1600),
}
RATIO = {"2.35:1": 2.35, "16:9": 16 / 9, "1:1": 1.0, "4:3": 4 / 3, "3:4": 3 / 4}


def target_ratio(size, aspect):
    w, h = size
    r = RATIO.get(aspect, 2.35)
    if w / h > r:
        return (round(h * r), h)
    return (w, round(w / r))


def postprocess(src: Path, dst: Path, aspect: str) -> Path:
    img = Image.open(src)
    img = ImageOps.exif_transpose(img).convert("RGB")
    img = ImageOps.fit(img, target_ratio(img.size, aspect), method=Image.LANCZOS, centering=(0.5, 0.45))
    img = img.resize(TARGET_SIZE.get(aspect, (1800, 766)), Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst, "JPEG", quality=88, optimize=True, progressive=True)
    kb = dst.stat().st_size / 1024
    if kb > 10 * 1024:  # 微信图片素材上限 10MB
        raise SystemExit(f"封面 {kb:.0f}KB 超过微信 10MB 限制")
    return dst


def stitch_vertical(paths: list[Path], dst: Path, gap: int = 16, bg=(255, 255, 255)) -> Path:
    """把多页漫画竖向拼成一张长图（适合公众号正文内插）。"""
    imgs = [Image.open(p).convert("RGB") for p in paths]
    width = min(i.width for i in imgs)
    scaled = [i.resize((width, round(i.height * width / i.width)), Image.LANCZOS) for i in imgs]
    height = sum(i.height for i in scaled) + gap * (len(scaled) - 1)
    canvas = Image.new("RGB", (width, height), bg)
    y = 0
    for im in scaled:
        canvas.paste(im, (0, y))
        y += im.height + gap
    dst.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dst, "JPEG", quality=86, optimize=True, progressive=True)
    return dst


# --------------------------------------------------------------------------
# 微信素材上传
# --------------------------------------------------------------------------
def wx_access_token() -> str:
    if not WX_CONFIG.exists():
        raise SystemExit(f"未找到微信凭证 {WX_CONFIG}")
    cfg = json.loads(WX_CONFIG.read_text())
    url = "https://api.weixin.qq.com/cgi-bin/stable_token"
    st, raw = http_json(url, {"grant_type": "client_credential",
                              "appid": cfg["appid"], "secret": cfg["secret"],
                              "force_refresh": False}, timeout=30)
    d = json.loads(raw)
    if "access_token" not in d:
        raise SystemExit(f"取微信 token 失败: {d}")
    return d["access_token"]


def wx_upload_image(path: Path, token: str) -> dict:
    url = ("https://api.weixin.qq.com/cgi-bin/material/add_material"
           f"?access_token={urllib.parse.quote(token)}&type=image")
    boundary = f"----ghiblicomic{uuid.uuid4().hex}"
    ctype = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    payload = b""
    payload += f"--{boundary}\r\n".encode()
    payload += (f'Content-Disposition: form-data; name="media"; filename="{path.name}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n").encode()
    payload += path.read_bytes() + b"\r\n"
    payload += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "curl/8",
    })
    # 微信不需要代理，直连更稳
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with direct.open(req, timeout=120) as r:
        d = json.loads(r.read())
    if "media_id" not in d:
        raise SystemExit(f"上传素材失败: {d}")
    return d


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Ghibli Comic — 用 Gemini 把主题变成吉卜力风封面 / 多格漫画")
    # 主题输入（三选一）
    ap.add_argument("--article", help="从 Markdown 抽取主题")
    ap.add_argument("--topic", help="直接指定主题")
    ap.add_argument("--title", help="只有标题时用它")
    ap.add_argument("--input-image", help="跳过生成，只做后处理/上传")
    # 模式
    ap.add_argument("--mode", default="auto", choices=["auto", "cover", "comic"],
                    help="cover=单张封面  comic=多格漫画  auto=有 --pages 则 comic")
    ap.add_argument("--pages", type=int, default=4, help="漫画页数（mode=comic 生效）")
    ap.add_argument("--layout", default="mixed", choices=list(LAYOUT_RULES),
                    help="每页分格布局")
    ap.add_argument("--stitch", action="store_true", help="把多页竖向拼成一张长图")
    # 画面
    ap.add_argument("--aspect", default="2.35:1", choices=list(RATIO),
                    help="封面 2.35:1/16:9/1:1/4:3；漫画页默认 3:4")
    ap.add_argument("--preset", default="auto", choices=["auto"] + list(SCENES))
    # 后端
    ap.add_argument("--backend", default="auto", choices=["auto", "gemini-web", "google-api"])
    ap.add_argument("--model", default=DEFAULT_IMAGE_MODEL, help=f"图片模型（默认 {DEFAULT_IMAGE_MODEL}）")
    ap.add_argument("--text-model", default=DEFAULT_TEXT_MODEL, help=f"文本模型（默认 {DEFAULT_TEXT_MODEL}）")
    # 输出
    ap.add_argument("--out", help="输出路径")
    ap.add_argument("--outdir", help="漫画多页输出目录")
    ap.add_argument("--upload", action="store_true", help="上传微信素材")
    ap.add_argument("--meta", help="结果写入 JSON")
    ap.add_argument("--keep-raw", action="store_true", help="保留 Gemini 原始输出")
    ap.add_argument("--dry-run", action="store_true", help="只打印 prompt，不调模型")
    a = ap.parse_args()

    if a.mode == "comic":
        a_aspect = a.aspect if a.aspect != "2.35:1" else "3:4"
    else:
        a_aspect = a.aspect
    a.layout = a.layout if a.layout in LAYOUT_RULES else "mixed"

    print(f"[proxy] {PROXY or '直连'}")

    # --- 主题 ---
    title, keywords = "", []
    if a.article:
        title, keywords = extract_topic(Path(a.article))
    if a.title:
        title = a.title
    if a.topic:
        title = title or a.topic
    topic = a.topic or title
    if not topic and not a.input_image:
        ap.error("需要 --article / --topic / --title / --input-image 之一")

    mode = a.mode
    if mode == "auto":
        mode = "comic" if (a.pages and a.pages > 1) else "cover"

    print(f"[topic] {topic}  [mode] {mode}")
    if keywords:
        print(f"[keywords] {', '.join(keywords)}")

    preset = choose_preset(topic, keywords, a.preset)

    backend = a.backend
    if backend == "auto" and not a.input_image:
        ok, why = gemini_web_ready()
        if ok:
            backend = "gemini-web"
        elif os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            backend = "google-api"
            print("[backend] gemini-web 不可用，回退 google-api")
        else:
            raise SystemExit(
                f"[backend] 没有可用后端。gemini-web 不可用（{why}），"
                f"且未设置 GEMINI_API_KEY。请先跑 gemini-web 的 --login 刷新 cookie，"
                f"或export GEMINI_API_KEY=... 后重试。"
            )

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^\w\-]+", "-", topic)[:24].strip("-") or "ghibli"
    result: dict = {"topic": topic, "mode": mode, "preset": preset}

    # ======================================================================
    # 漫画模式
    # ======================================================================
    if mode == "comic" and not a.input_image:
        plan: dict = {}
        if a.dry_run:
            plan = {"title": "(dry-run 不调 LLM)", "character_note": "(dry-run)",
                    "pages": [{"page": i + 1, "layout": a.layout,
                               "scene": f"(page {i + 1} scene)", "continuity": ""}
                              for i in range(a.pages)]}
        else:
            print("[plan] 规划分镜中…")
            plan = plan_storyboard(topic, keywords, a.pages, a.layout)
            if not plan:
                print("[plan] LLM 规划失败，退化为无分镜模式（每页同一主题）")
                plan = {"title": slug, "character_note": "",
                        "pages": [{"page": i + 1, "layout": a.layout,
                                   "scene": f"A quiet vignette that conveys: {topic}",
                                   "continuity": ""} for i in range(a.pages)]}
            else:
                print(f"[plan] 漫画标题：{plan.get('title', '')}")
                print(f"[plan] 角色设定：{plan.get('character_note', '')[:80]}")

        plan["topic"] = topic
        pages = plan.get("pages") or []
        outdir = Path(a.outdir or a.out or f"./ghibli-{slug}-{ts}").expanduser()
        outdir.mkdir(parents=True, exist_ok=True)
        pages_dir = outdir / "pages"
        pages_dir.mkdir(exist_ok=True)

        # 分镜表落盘（可复现记录）
        (outdir / "storyboard.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

        finals: list[Path] = []
        first_raw: Path | None = None
        for i, spec in enumerate(pages[: a.pages], start=1):
            prompt = build_page_prompt(plan, spec, i, len(pages[: a.pages]), preset)
            (pages_dir / f"{i:02d}-prompt.md").write_text(prompt, encoding="utf-8")
            if a.dry_run:
                print(f"\n--- page {i} prompt ---\n{prompt}")
                continue
            print(f"[gen] page {i}/{len(pages[: a.pages])}…")
            t0 = time.time()
            raw = outdir / f"page-{i:02d}-raw.png"
            if backend == "gemini-web":
                # 第一页做风格锚点，后续页以它为参考，保持画风一致
                refs = [first_raw] if (first_raw and first_raw.exists()) else None
                raw = gen_gemini_web(prompt, a.model, raw, a_aspect, refs=refs)
                if i == 1:
                    first_raw = raw
            else:
                raw = gen_google_api(prompt, a.model, raw, a_aspect)
            final = postprocess(raw, pages_dir / f"{i:02d}-{slug}.jpg", a_aspect)
            if not a.keep_raw:
                raw.unlink(missing_ok=True)
            print(f"      {time.time() - t0:.1f}s -> {final.name}")
            finals.append(final)

        if a.stitch and finals:
            stitched = postprocess_stitched(finals, outdir / f"00-{slug}-long.jpg")
            print(f"[stitch] {stitched}")
            result["stitched"] = str(stitched)

        result["pages"] = [str(p) for p in finals]
        result["output_dir"] = str(outdir)
        result["storyboard"] = str(outdir / "storyboard.json")
        result["comic_title"] = plan.get("title", "")

    # ======================================================================
    # 封面模式
    # ======================================================================
    else:
        prompt = build_cover_prompt(topic, keywords, preset, a_aspect)
        print(f"[preset] {preset}\n------ PROMPT ------\n{prompt}\n--------------------")

        if a.dry_run:
            return

        out = Path(a.out or f"cover-{slug}-{ts}.jpg").expanduser()
        if a.input_image:
            raw = Path(a.input_image)
            print(f"[gen] 跳过生成，使用 {raw}")
        else:
            print(f"[gen] backend={backend} model={a.model}")
            t0 = time.time()
            raw = gen_gemini_web(prompt, a.model, out, a_aspect) if backend == "gemini-web" \
                else gen_google_api(prompt, a.model, out, a_aspect)
            print(f"[gen] 完成 {time.time() - t0:.1f}s -> {raw}")

        final = postprocess(raw, out, a_aspect)
        if not a.keep_raw:
            raw.unlink(missing_ok=True)
        print(f"[out] {final}  {final.stat().st_size / 1024:.0f}KB  {Image.open(final).size[0]}x{Image.open(final).size[1]}")
        result["cover"] = str(final)
        result["prompt"] = prompt

    # --- 上传 ---
    if a.upload and not a.dry_run:
        target = Path(result.get("stitched") or result.get("cover")
                      or (result.get("pages") or [""])[0])
        token = wx_access_token()
        up = wx_upload_image(target, token)
        result.update({"thumb_media_id": up["media_id"], "url": up.get("url"), "uploaded": str(target)})
        print(f"[wx] thumb_media_id={up['media_id']} <- {target}")

    if a.meta:
        Path(a.meta).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[meta] {a.meta}")
    print(json.dumps(result, ensure_ascii=False))


def postprocess_stitched(paths: list[Path], dst: Path) -> Path:
    stitched = stitch_vertical(paths, dst)
    kb = stitched.stat().st_size / 1024
    if kb > 10 * 1024:
        raise SystemExit(f"长图 {kb:.0f}KB 超过微信 10MB 限制")
    print(f"[stitch] {len(paths)} 页 -> {stitched}  {kb:.0f}KB")
    return stitched


if __name__ == "__main__":
    main()