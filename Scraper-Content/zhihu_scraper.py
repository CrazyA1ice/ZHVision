#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知乎用户回答批量导出与增量更新可视化工具（v3.4）
功能特性：
1. 文件夹命名带用户ID（昵称）：文件夹采用 `zhihu_url_token(用户ID)` 格式存放，支持识别半角 `()` 与全角 `（）` 括号（兼容有无 `zhihu_` 前缀及旧版纯 `zhihu_url_token` 文件夹），读取与增量更新时自动匹配已有目录。
2. 图片内置插入：自动下载回答中的图片并以 Base64 Data URI 方式直接内嵌到 .md 对应位置（亦支持切换相对路径模式），无需单独存放图片文件夹。
3. 可视化界面（GUI）：支持直接传入任意知乎用户主页链接（或 url_token）下载指定用户的全部回答，内置登录确认、进度展示与实时日志。
4. 一键更新列表与增量倒序前置写入：支持通过外部文件（默认 update_list.txt）及界面维护待更新用户列表；用户确认更新后，未下载过的用户自动从头全量下载，已下载过的用户以 .md 文件内实际已有的回答为准，抓取上次下载之后新增的回答，并按时间倒序插入到原有 .md 文件的已有回答之前，严格保持全文从新到旧的时间顺序。
"""

import asyncio
import base64
import hashlib
import html
import json
import os
import queue
import re
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from zhihu_works import WORK_TYPES, normalize_types, sync_user_works

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
except ImportError:
    tk = None
    filedialog = messagebox = scrolledtext = ttk = None

# 可选依赖检测：html2text（若未安装则提供内置 HTML->Markdown 转换器兜底）
try:
    import html2text as _html2text_mod
except ImportError:
    _html2text_mod = None

# 核心依赖检测：playwright
try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None


# ==================== 默认配置 ====================
DEFAULT_URL_TOKEN = "mdk82"
PAGE_SIZE = 20
SLEEP_BETWEEN_REQ = 0.6
CONCURRENCY_IMG = 6

BASE_DIR = Path(__file__).parent.resolve()
PROFILE_DIR = BASE_DIR / ".pw_profile"
DEFAULT_UPDATE_LIST_FILE = BASE_DIR / "update_list.txt"

ANSWER_INCLUDE = (
    "data[*].is_normal,admin_closed_comment,reward_info,is_collapsed,"
    "annotation_action,annotation_detail,collapse_reason,is_sticky,"
    "collapsed_by,suggest_edit,comment_count,can_comment,content,"
    "voteup_count,reshipment_settings,comment_permission,created_time,"
    "updated_time,relationship.is_authorized,is_author,voting,is_thanked,"
    "is_nothelp,status;"
    "data[*].question.created,question.updated,question.id,question.type,"
    "question.title,question.url"
)


# ==================== 环境依赖自检与一键安装 ====================


def ensure_playwright_installed(parent_window=None) -> bool:
    """检查当前 Python 环境是否已安装 playwright，未安装时支持弹窗一键自动安装。"""
    global async_playwright, _html2text_mod
    if async_playwright is not None:
        return True

    msg = (
        f"检测到当前运行的 Python 环境：\n{sys.executable}\n\n"
        "尚未安装 `playwright`（以及可选的 `html2text`）。\n"
        "是否立即为您自动执行 pip 安装？\n\n"
        "（也可以在终端手动执行：pip install playwright html2text）"
    )
    if parent_window is not None and messagebox is not None:
        ok = messagebox.askyesno("缺少依赖库 playwright", msg, parent=parent_window)
    else:
        print(msg)
        ans = input("是否立即自动安装？[Y/n]: ").strip().lower()
        ok = ans in ("", "y", "yes")

    if not ok:
        return False

    try:
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "playwright",
            "html2text",
            "-i",
            "https://pypi.tuna.tsinghua.edu.cn/simple",
        ]
        subprocess.check_call(cmd)
        from playwright.async_api import async_playwright as _apw

        async_playwright = _apw
        try:
            import html2text as _h2t

            _html2text_mod = _h2t
        except ImportError:
            pass
        return True
    except Exception as e:
        err_msg = f"自动安装失败：{e}\n\n请在终端运行：\n\"{sys.executable}\" -m pip install playwright html2text"
        if parent_window is not None and messagebox is not None:
            messagebox.showerror("安装失败", err_msg, parent=parent_window)
        else:
            print(err_msg)
        return False


# ==================== 用户标识与带括号文件夹解析工具 ====================

_TRAILING_PAREN_RE = re.compile(r"^(.*?)(?:\(([^)]*)\)|（([^）]*)）)\s*$")
_ILLEGAL_FILENAME_CHARS_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def sanitize_folder_label(label: str) -> str:
    """清理用户ID/昵称中不能作为 Windows/Linux 文件夹名称的非法字符。"""
    if not label:
        return ""
    cleaned = _ILLEGAL_FILENAME_CHARS_RE.sub("_", label.strip())
    cleaned = cleaned.replace("(", "（").replace(")", "）")
    return cleaned.strip()


def parse_zhihu_user_info(text: str) -> tuple:
    """
    从输入字符串（主页链接、带括号的文件夹名、或 url_token）中解析出 (url_token, user_label)。
    """
    if not text:
        return "", ""
    s = text.strip()
    comment_label = ""
    if "#" in s:
        s, comment_part = s.split("#", 1)
        s = s.strip()
        comment_label = comment_part.strip()
    if not s:
        return "", ""

    paren_label = ""
    m_paren = _TRAILING_PAREN_RE.match(s)
    if m_paren:
        s = m_paren.group(1).strip()
        paren_label = (m_paren.group(2) or m_paren.group(3) or "").strip()

    user_label = paren_label or comment_label

    m_url = re.search(r"zhihu\.com/(?:people|org)/([^/?#&\s(（]+)", s, re.IGNORECASE)
    if m_url:
        token = m_url.group(1).strip()
        return token, user_label

    if s.lower().startswith("zhihu_") and len(s) > 6:
        candidate = s[6:].strip()
        if re.match(r"^[A-Za-z0-9_\-\.]+$", candidate):
            return candidate, user_label

    if re.match(r"^[A-Za-z0-9_\-\.]+$", s):
        return s, user_label

    return "", ""


def parse_zhihu_user_token(text: str) -> str:
    """仅提取用户的 url_token。"""
    token, _ = parse_zhihu_user_info(text)
    return token


def match_folder_token_and_label(folder_name: str) -> tuple:
    """
    检查某个文件夹名称是否符合存放知乎回答的命名规则：
    支持：
    - zhihu_{url_token}({user_id}) / zhihu_{url_token}（{user_id}）
    - {url_token}({user_id})       / {url_token}（{user_id}）
    - zhihu_{url_token}
    返回 (url_token, user_id_in_paren, has_paren)
    """
    name = folder_name.strip()
    if not name or name.startswith("."):
        return "", "", False

    has_zhihu_prefix = False
    body = name
    if body.lower().startswith("zhihu_") and len(body) > 6:
        has_zhihu_prefix = True
        body = body[6:]

    m_paren = _TRAILING_PAREN_RE.match(body)
    if m_paren:
        token_part = m_paren.group(1).strip()
        label_part = (m_paren.group(2) or m_paren.group(3) or "").strip()
        if re.match(r"^[A-Za-z0-9_\-\.]+$", token_part):
            return token_part, label_part, True
        return "", "", False

    if has_zhihu_prefix and re.match(r"^[A-Za-z0-9_\-\.]+$", body):
        return body, "", False

    return "", "", False


def find_existing_user_dir(base_dir: Path, url_token: str) -> tuple:
    """
    在 base_dir 下查找对应 url_token 已存在的用户文件夹。
    """
    if not base_dir.exists() or not url_token:
        return None, ""

    target_lower = url_token.lower()
    candidates = []

    try:
        for child in base_dir.iterdir():
            if not child.is_dir():
                continue
            tok, label, has_paren = match_folder_token_and_label(child.name)
            if tok and tok.lower() == target_lower:
                has_md = (child / "answers.md").exists()
                has_cache = (child / "answers_cache.json").exists()
                has_zhihu_prefix = child.name.lower().startswith("zhihu_")
                score = (
                    (4 if (has_md or has_cache) else 0)
                    + (2 if has_paren and label else 0)
                    + (1 if has_zhihu_prefix else 0)
                )
                candidates.append((score, child, label))
    except Exception:
        pass

    if not candidates:
        return None, ""

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, best_dir, best_label = candidates[0]
    return best_dir, best_label


def build_user_dir_name(url_token: str, user_label: str = "") -> str:
    """构造标准文件夹名称：当有用户ID时为 `zhihu_{url_token}({user_id})`，否则为 `zhihu_{url_token}`。"""
    clean_label = sanitize_folder_label(user_label)
    if clean_label and clean_label.lower() != url_token.lower():
        return f"zhihu_{url_token}({clean_label})"
    return f"zhihu_{url_token}"


def get_user_paths(base_dir: Path, url_token: str, user_label: str = ""):
    """获取指定用户的输出目录、缓存文件路径、Markdown 文件路径、图片目录路径。"""
    existing_dir, _ = find_existing_user_dir(base_dir, url_token)
    if existing_dir is not None:
        out_dir = existing_dir
    else:
        out_dir = base_dir / build_user_dir_name(url_token, user_label)

    cache_file = out_dir / "answers_cache.json"
    md_file = out_dir / "answers.md"
    images_dir = out_dir / "images"
    return out_dir, cache_file, md_file, images_dir


def ensure_user_dir_with_id(base_dir: Path, url_token: str, target_name: str, log_fn=None):
    """确保目录名包含 `(用户ID)`。"""
    existing_dir, _ = find_existing_user_dir(base_dir, url_token)
    desired_name = build_user_dir_name(url_token, target_name)
    desired_dir = base_dir / desired_name

    if existing_dir is None:
        desired_dir.mkdir(parents=True, exist_ok=True)
        out_dir = desired_dir
    else:
        _, _, has_paren = match_folder_token_and_label(existing_dir.name)
        if not has_paren and existing_dir != desired_dir and not desired_dir.exists():
            try:
                existing_dir.rename(desired_dir)
                if log_fn:
                    log_fn(f"已将用户目录自动命名为: {desired_dir.name}")
                out_dir = desired_dir
            except Exception:
                out_dir = existing_dir
        else:
            out_dir = existing_dir
        out_dir.mkdir(parents=True, exist_ok=True)

    return (
        out_dir,
        out_dir / "answers_cache.json",
        out_dir / "answers.md",
        out_dir / "images",
    )


def extract_user_name_from_md(md_file: Path) -> str:
    """从已有的 answers.md 标题 `# 知乎用户「xxx」的全部回答` 中提取用户名称。"""
    if not md_file.exists():
        return ""
    try:
        with md_file.open("r", encoding="utf-8", errors="ignore") as f:
            for _ in range(10):
                line = f.readline()
                if not line:
                    break
                m = re.search(r"#\s*知乎用户「([^」]+)」", line)
                if m:
                    return m.group(1).strip()
    except Exception:
        pass
    return ""


def parse_existing_md_answer_ids(md_file: Path) -> set:
    """从已有的 answers.md 文件中提取所有已写入的 answer_id。"""
    if not md_file.exists():
        return set()
    try:
        content = md_file.read_text(encoding="utf-8", errors="ignore")
        return set(re.findall(r"zhihu\.com/question/\d+/answer/(\d+)", content))
    except Exception:
        return set()


def parse_existing_md_latest_ts(md_file: Path) -> int:
    """从已有的 answers.md 文件中提取所有 `- 发布时间: YYYY-MM-DD HH:MM` 的最大时间戳。"""
    if not md_file.exists():
        return 0
    max_ts = 0
    try:
        content = md_file.read_text(encoding="utf-8", errors="ignore")
        for dt_str in re.findall(r"^-\s*发布时间:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", content, flags=re.MULTILINE):
            try:
                ts = int(datetime.strptime(dt_str.strip(), "%Y-%m-%d %H:%M").timestamp())
                if ts > max_ts:
                    max_ts = ts
            except Exception:
                pass
    except Exception:
        pass
    return max_ts


def get_user_local_status(base_dir: Path, url_token: str, fallback_label: str = "") -> dict:
    """检查某个用户在本地的已下载状态（严格以 answers.md 中实际包含的回答数为准）。"""
    existing_dir, folder_label = find_existing_user_dir(base_dir, url_token)
    out_dir, cache_file, md_file, _ = get_user_paths(base_dir, url_token, fallback_label)

    md_exists = md_file.exists() and md_file.stat().st_size > 0
    md_ids = parse_existing_md_answer_ids(md_file)

    last_time = ""
    if md_exists:
        try:
            content = md_file.read_text(encoding="utf-8", errors="ignore")
            times = re.findall(r"^-\s*发布时间:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", content, flags=re.MULTILINE)
            if times:
                last_time = max(times)
        except Exception:
            pass

    user_name = folder_label or extract_user_name_from_md(md_file) or fallback_label or "-"

    return {
        "downloaded": md_exists and len(md_ids) > 0,
        "md_exists": md_exists,
        "count": len(md_ids),
        "last_answer_time": last_time,
        "md_file": md_file,
        "folder_name": existing_dir.name if existing_dir else build_user_dir_name(url_token, fallback_label),
        "user_name": user_name,
    }


def ensure_default_update_list(list_file: Path):
    """如果默认一键更新列表文件不存在，则创建一个带说明的示例文件。"""
    if not list_file.exists():
        sample_lines = [
            "# 知乎用户回答一键更新列表",
            "# 每行填写一个知乎用户主页链接或 url_token，支持在末尾括号中写上用户ID以便识别",
            "# 示例：",
            "# https://www.zhihu.com/people/mdk82(用户ID)",
            "# 或直接写文件夹同名格式：zhihu_mdk82(用户ID) / mdk82(用户ID)",
            f"https://www.zhihu.com/people/{DEFAULT_URL_TOKEN}",
            "",
        ]
        list_file.write_text("\n".join(sample_lines), encoding="utf-8")


def load_update_list_from_file(list_file: Path) -> list:
    """从列表文件中加载待更新用户列表。"""
    ensure_default_update_list(list_file)
    entries = []
    seen = set()
    try:
        lines = list_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        return entries

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        token, label = parse_zhihu_user_info(stripped)
        if token and token.lower() not in seen:
            seen.add(token.lower())
            entries.append({
                "raw": stripped,
                "token": token,
                "label": label,
                "url": f"https://www.zhihu.com/people/{token}",
            })
    return entries


def save_update_list_to_file(list_file: Path, items: list, base_dir: Path = None):
    """将用户列表保存到更新列表文件。"""
    lines = [
        "# 知乎用户回答一键更新列表",
        "# 每行填写一个知乎用户主页链接或 url_token，括号内为用户ID（昵称），以 # 开头的行为注释",
        "",
    ]
    seen = set()
    for item in items:
        if isinstance(item, dict):
            token = item.get("token", "")
            label = item.get("label", "")
        else:
            token, label = parse_zhihu_user_info(str(item))

        if not token or token.lower() in seen:
            continue
        seen.add(token.lower())

        if not label and base_dir is not None:
            st = get_user_local_status(base_dir, token)
            if st["user_name"] and st["user_name"] != "-":
                label = st["user_name"]

        if label and label.lower() != token.lower():
            lines.append(f"https://www.zhihu.com/people/{token}({label})")
        else:
            lines.append(f"https://www.zhihu.com/people/{token}")

    lines.append("")
    list_file.write_text("\n".join(lines), encoding="utf-8")


# ==================== HTML 与图片内嵌处理（标准库实现，无需 aiohttp） ====================

_NOSCRIPT_RE = re.compile(r"<noscript>.*?</noscript>", re.IGNORECASE | re.DOTALL)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


def _extract_src_from_img_tag(tag: str) -> str:
    """从知乎 <img> 标签中提取真实高清图片地址。"""
    for attr in ("data-original", "data-actualsrc", "data-default-watermark-src", "src"):
        m = re.search(rf'\b{attr}\s*=\s*["\']([^"\']+)["\']', tag, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if val and val.startswith("http") and not val.startswith("data:"):
                return val
    return ""


def _guess_mime_type(url: str, content_type: str = "", data: bytes = b"") -> str:
    """根据响应头、文件魔数或 URL 后缀推断图片 MIME 类型。"""
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct.startswith("image/"):
            return ct

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"

    ext = Path(urlparse(url).path).suffix.lower()
    mime_map = {
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".bmp": "image/bmp",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }
    return mime_map.get(ext, "image/jpeg")


def _sync_download_bytes(url: str, timeout: int = 30) -> tuple:
    """使用 Python 标准库 urllib.request 下载图片字节流与 Content-Type。"""
    req = urllib.request.Request(
        url,
        headers={
            "Referer": "https://www.zhihu.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
        },
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        status = getattr(resp, "status", 200)
        content_type = resp.headers.get("Content-Type", "") if resp.headers else ""
        data = resp.read()
        return status, content_type, data


async def fetch_image_as_data_uri(sem: asyncio.Semaphore, url: str, log_fn) -> str:
    """并发下载图片并转换为 Base64 Data URI。"""
    async with sem:
        try:
            status, content_type, raw_bytes = await asyncio.to_thread(_sync_download_bytes, url, 30)
            if status == 200 and raw_bytes:
                mime = _guess_mime_type(url, content_type, raw_bytes)
                b64 = base64.b64encode(raw_bytes).decode("ascii")
                return f"data:{mime};base64,{b64}"
            else:
                log_fn(f"  图片下载状态码 {status}: {url[:80]}")
        except Exception as e:
            log_fn(f"  图片下载异常 {e}: {url[:80]}")
        return ""


async def download_image_to_local(
    sem: asyncio.Semaphore, url: str, dest_dir: Path, log_fn
) -> str:
    """可选模式：下载图片到本地 images 目录并返回相对路径 images/xxx.jpg。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    ext = Path(parsed.path).suffix or ".jpg"
    if len(ext) > 5:
        ext = ".jpg"
    name = hashlib.md5(url.encode("utf-8")).hexdigest() + ext
    dest = dest_dir / name
    if dest.exists() and dest.stat().st_size > 0:
        return f"images/{name}"

    async with sem:
        try:
            status, _, raw_bytes = await asyncio.to_thread(_sync_download_bytes, url, 30)
            if status == 200 and raw_bytes:
                dest.write_bytes(raw_bytes)
                return f"images/{name}"
            else:
                log_fn(f"  图片下载状态码 {status}: {url[:80]}")
        except Exception as e:
            log_fn(f"  图片下载异常 {e}: {url[:80]}")
        return ""


def _fallback_html_to_md(html_str: str) -> str:
    """当环境未安装 html2text 时的轻量级内置 HTML -> Markdown 转换器。"""
    s = html_str
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</p\s*>", "\n\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<h1[^>]*>(.*?)</h1>", r"\n# \1\n\n", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<h2[^>]*>(.*?)</h2>", r"\n## \1\n\n", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<h3[^>]*>(.*?)</h3>", r"\n### \1\n\n", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<(?:b|strong)[^>]*>(.*?)</(?:b|strong)>", r"**\1**", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<(?:i|em)[^>]*>(.*?)</(?:i|em)>", r"*\1*", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(
        r"<blockquote[^>]*>(.*?)</blockquote>",
        lambda m: "\n" + "\n".join("> " + line for line in m.group(1).strip().splitlines()) + "\n\n",
        s,
        flags=re.IGNORECASE | re.DOTALL,
    )
    s = re.sub(
        r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        r"[\2](\1)",
        s,
        flags=re.IGNORECASE | re.DOTALL,
    )
    s = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1\n", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _convert_html_str_to_md(html_with_tokens: str) -> str:
    """优先使用 html2text，若未安装则自动回退到内置转换器。"""
    if _html2text_mod is not None:
        h2t = _html2text_mod.HTML2Text()
        h2t.body_width = 0
        h2t.ignore_links = False
        h2t.ignore_images = False
        h2t.protect_links = True
        h2t.unicode_snob = True
        return h2t.handle(html_with_tokens).strip()
    return _fallback_html_to_md(html_with_tokens)


async def convert_answer_html_to_markdown(
    html_body: str,
    img_sem: asyncio.Semaphore,
    log_fn,
    embed_base64: bool = True,
    images_dir: Path = None,
    progress_prefix: str = "",
) -> str:
    """将知乎回答的 HTML 转换为 Markdown，并将图片在原对应位置内置插入。"""
    if not html_body:
        return ""

    cleaned_html = _NOSCRIPT_RE.sub("", html_body)
    placeholders = []

    def _replace_img(match):
        tag = match.group(0)
        src = _extract_src_from_img_tag(tag)
        if not src:
            return ""
        idx = len(placeholders)
        token = f"ZHIHUIMGPLACEHOLDER{idx}END"
        placeholders.append((token, src))
        return f"<p>{token}</p>"

    html_with_tokens = _IMG_TAG_RE.sub(_replace_img, cleaned_html)

    unique_urls = list(dict.fromkeys(url for _, url in placeholders))
    url_to_embedded = {}

    if unique_urls:
        mode_desc = "内嵌Base64" if embed_base64 else "本地图片"
        log_fn(f"{progress_prefix}处理 {len(unique_urls)} 张图片（{mode_desc}）...")
        if embed_base64:
            tasks = [fetch_image_as_data_uri(img_sem, u, log_fn) for u in unique_urls]
        else:
            tasks = [download_image_to_local(img_sem, u, images_dir, log_fn) for u in unique_urls]

        results = await asyncio.gather(*tasks)
        for u, res in zip(unique_urls, results):
            url_to_embedded[u] = res if res else u

    md_text = _convert_html_str_to_md(html_with_tokens)

    for idx, (token, orig_url) in enumerate(placeholders, start=1):
        embedded_target = url_to_embedded.get(orig_url, orig_url)
        md_img = f"![图片{idx}]({embedded_target})"
        md_text = md_text.replace(token, md_img)

    return md_text


# ==================== 知乎 API 抓取核心逻辑 ====================


async def fetch_json(page, url: str):
    """用 page.goto 直接导航到 API URL，利用浏览器自身会话绕过 x-zse-96 签名。"""
    resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    status = resp.status if resp else 0
    await asyncio.sleep(0.3)
    body = await page.evaluate("document.body && document.body.innerText || ''")
    body = (body or "").strip()
    if not body:
        raise RuntimeError(f"HTTP {status}，返回空内容。URL: {url}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"HTTP {status}，返回不是 JSON（可能未登录或触发安全验证）：\n{body[:300]}\nURL: {url}"
        )
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"API 错误：{data['error']}\nURL: {url}")
    return data


async def fetch_answers_list(
    page,
    url_token: str,
    log_fn,
    stop_event: threading.Event = None,
    incremental: bool = False,
    known_answer_ids: set = None,
    latest_created_ts: int = 0,
) -> list:
    """
    拉取用户的回答列表。
    - 若 incremental=False（从头全量下载）：拉取全部回答。
    - 若 incremental=True（增量更新）：按创建时间从新到旧拉取，仅收集 answers.md 中尚未包含的新增回答。
    """
    answers = []
    known_answer_ids = known_answer_ids or set()
    offset = 0

    while True:
        if stop_event and stop_event.is_set():
            log_fn("已收到停止指令，中断拉取回答列表。")
            break

        url = (
            f"https://www.zhihu.com/api/v4/members/{url_token}/answers"
            f"?include={ANSWER_INCLUDE}"
            f"&limit={PAGE_SIZE}&offset={offset}&sort_by=created"
        )
        log_fn(f"正在拉取回答列表 offset={offset} ...")
        data = await fetch_json(page, url)
        batch = data.get("data", [])
        if not batch:
            break

        reached_existing = False
        for ans in batch:
            aid = str(ans.get("id", ""))
            if not aid:
                continue
            is_sticky = bool(ans.get("is_sticky"))
            created_ts = int(ans.get("created_time", 0) or 0)

            if incremental:
                if aid in known_answer_ids:
                    if is_sticky:
                        continue
                    else:
                        reached_existing = True
                        break
                if latest_created_ts > 0 and created_ts > 0 and created_ts <= latest_created_ts and not is_sticky:
                    reached_existing = True
                    break

            answers.append(ans)

        if reached_existing:
            log_fn(f"已触及上次 answers.md 中已有的回答边界，增量扫描完成（共发现 {len(answers)} 条新增回答）。")
            break

        paging = data.get("paging", {})
        if paging.get("is_end"):
            break
        offset += PAGE_SIZE
        await asyncio.sleep(SLEEP_BETWEEN_REQ)

    return answers


async def fetch_author_comments(page, answer_id: str, target_user_id: str, log_fn) -> list:
    """抓取回答下作者本人的主评论及作者回复他人的子评论。"""
    out = []
    offset = 0
    while True:
        url = (
            f"https://www.zhihu.com/api/v4/answers/{answer_id}/root_comments"
            f"?order=normal&limit=20&offset={offset}&status=open"
        )
        try:
            data = await fetch_json(page, url)
        except Exception as e:
            log_fn(f"  主评论 offset={offset} 抓取失败: {e}")
            break

        comments = data.get("data", [])
        if not comments:
            break

        for c in comments:
            author = c.get("author", {}) or {}
            is_self = (
                author.get("id") == target_user_id
                or author.get("is_author") is True
            )
            root_content = c.get("content", "") or ""
            root_author = author.get("name", "匿名用户")

            if is_self:
                out.append({"kind": "self_root", "content": root_content})

            child_url = c.get("child_comments")
            child_total = c.get("child_comment_count", 0) or 0
            if child_url and child_total > 0:
                child_offset = 0
                while True:
                    try:
                        sep = "&" if "?" in child_url else "?"
                        cdata = await fetch_json(
                            page, f"{child_url}{sep}limit=20&offset={child_offset}"
                        )
                    except Exception as e:
                        log_fn(f"  子评论抓取失败: {e}")
                        break
                    ccs = cdata.get("data", [])
                    if not ccs:
                        break
                    for cc in ccs:
                        cc_author = cc.get("author", {}) or {}
                        cc_is_self = (
                            cc_author.get("id") == target_user_id
                            or cc_author.get("is_author") is True
                        )
                        if cc_is_self and not is_self:
                            out.append({
                                "kind": "self_reply",
                                "root_author": root_author,
                                "root_content": root_content,
                                "reply_content": cc.get("content", "") or "",
                            })
                    if cdata.get("paging", {}).get("is_end", True):
                        break
                    child_offset += 20
                    await asyncio.sleep(SLEEP_BETWEEN_REQ)

        if data.get("paging", {}).get("is_end", True):
            break
        offset += 20
        await asyncio.sleep(SLEEP_BETWEEN_REQ)

    return out


# ==================== Markdown 格式化与文件写入（增量倒序前置写入） ====================


def format_single_answer_md(it: dict) -> list:
    """将单条回答数据格式化为 Markdown 行列表。"""
    lines = [
        f"## {it['question_title']}",
        "",
        f"- 原文链接: {it['answer_url']}",
        f"- 发布时间: {it['created_time']}",
        f"- 赞同数: {it['voteup']}",
        "",
        it.get("body_md", ""),
        "",
    ]

    acs = it.get("author_comments") or []
    if acs:
        lines.append("### 💬 作者在评论区的留言 / 回复")
        lines.append("")
        for ac in acs:
            if ac.get("kind") == "self_root":
                lines.append(f"- **作者本人评论**: {ac.get('content', '')}")
            else:
                lines.append(
                    f"> **{ac.get('root_author', '匿名')}**: "
                    f"{ac.get('root_content', '')}"
                )
                lines.append("")
                lines.append(f"**作者回复**: {ac.get('reply_content', '')}")
            lines.append("")

    lines.append("---")
    lines.append("")
    return lines


def write_full_markdown(md_file: Path, target_name: str, url_token: str, items: list):
    """首次全量下载或重建时，按时间倒序（最新在前）完整生成 answers.md 文件。"""
    sorted_items = sorted(
        items,
        key=lambda x: (int(x.get("created_ts", 0) or 0), x.get("created_time", "")),
        reverse=True,
    )
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# 知乎用户「{target_name}」的全部回答",
        "",
        f"- 用户主页: https://www.zhihu.com/people/{url_token}",
        f"- 导出时间: {now_str}",
        f"- 回答总数: {len(sorted_items)}",
        "",
        "---",
        "",
    ]
    for it in sorted_items:
        lines.extend(format_single_answer_md(it))

    md_file.write_text("\n".join(lines), encoding="utf-8")


def prepend_new_answers_to_markdown(
    md_file: Path,
    new_items: list,
    total_count: int,
):
    """
    增量更新写入：将本次新增的回答按时间倒序（最新在前）插入到已有 answers.md 中
    第一条已有回答（`## `）之前，从而保证整个 .md 文件从上到下严格按时间倒序排列，
    并同步刷新文件头部的导出时间与实际回答总数。
    """
    if not new_items:
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    sorted_new = sorted(
        new_items,
        key=lambda x: (int(x.get("created_ts", 0) or 0), x.get("created_time", "")),
        reverse=True,
    )

    existing_text = md_file.read_text(encoding="utf-8", errors="ignore")
    existing_text = re.sub(
        r"^- 导出时间:.*$",
        f"- 导出时间: {now_str}（最近增量更新）",
        existing_text,
        count=1,
        flags=re.MULTILINE,
    )

    new_lines = []
    for it in sorted_new:
        new_lines.extend(format_single_answer_md(it))
    new_block = "\n".join(new_lines)
    if not new_block.endswith("\n"):
        new_block += "\n"

    first_answer_match = re.search(r"^##\s+", existing_text, flags=re.MULTILINE)
    if first_answer_match:
        split_pos = first_answer_match.start()
        header_part = existing_text[:split_pos]
        body_part = existing_text[split_pos:]
        if not header_part.endswith("\n\n"):
            header_part = header_part.rstrip("\n") + "\n\n"
        updated_text = header_part + new_block + body_part
    else:
        if not existing_text.endswith("\n"):
            existing_text += "\n"
        updated_text = existing_text + "\n" + new_block

    # 统计写入后文件中实际包含的回答 ID 数量，确保文件头部的 `- 回答总数:` 与文件内实际回答条目数 100% 一致
    actual_ids = set(re.findall(r"zhihu\.com/question/\d+/answer/(\d+)", updated_text))
    final_count = len(actual_ids) if actual_ids else total_count
    updated_text = re.sub(
        r"^- 回答总数:.*$",
        f"- 回答总数: {final_count}",
        updated_text,
        count=1,
        flags=re.MULTILINE,
    )

    md_file.write_text(updated_text, encoding="utf-8")


append_new_answers_to_markdown = prepend_new_answers_to_markdown


# ==================== 单用户抓取与更新流程 ====================


async def process_single_user(
    page,
    img_sem: asyncio.Semaphore,
    base_dir: Path,
    url_token: str,
    mode: str,
    embed_base64: bool,
    fetch_comments: bool,
    log_fn,
    progress_cb=None,
    stop_event: threading.Event = None,
) -> dict:
    """处理单个知乎用户的下载或更新任务。"""
    out_dir, cache_file, md_file, images_dir = get_user_paths(base_dir, url_token)

    cache = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    # 关键修复：判断增量更新边界时，严格以 answers.md 中实际已存在的回答 ID 与时间戳为准！
    # 防止因 answers_cache.json 中残留缓存而漏写 answers.md 中缺失的新回答。
    existing_md_ids = parse_existing_md_answer_ids(md_file)
    has_previous_download = md_file.exists() and md_file.stat().st_size > 0 and len(existing_md_ids) > 0

    is_incremental_run = (mode == "incremental") and has_previous_download
    if mode == "incremental" and not has_previous_download:
        log_fn(f"用户 [{url_token}] 之前尚未生成有效 answers.md，将自动从头开始全量下载。")
    elif is_incremental_run:
        log_fn(
            f"用户 [{url_token}] 检测到已有 [{out_dir.name}/answers.md]（实际含 {len(existing_md_ids)} 条回答），将增量抓取新增回答并倒序前置写入。"
        )

    latest_created_ts = parse_existing_md_latest_ts(md_file) if is_incremental_run else 0

    me = await fetch_json(page, f"https://www.zhihu.com/api/v4/members/{url_token}")
    target_user_id = me.get("id")
    target_name = me.get("name", url_token)
    log_fn(f"目标用户: {target_name} (url_token={url_token}, id={target_user_id})")

    out_dir, cache_file, md_file, images_dir = ensure_user_dir_with_id(
        base_dir, url_token, target_name, log_fn=log_fn
    )

    answers = await fetch_answers_list(
        page=page,
        url_token=url_token,
        log_fn=log_fn,
        stop_event=stop_event,
        incremental=is_incremental_run,
        known_answer_ids=existing_md_ids,
        latest_created_ts=latest_created_ts,
    )

    if is_incremental_run:
        answers = [a for a in answers if str(a.get("id", "")) not in existing_md_ids]
        log_fn(f"用户「{target_name}」本次共发现 {len(answers)} 条 answers.md 中尚未写入的新增回答。")
        if not answers:
            if progress_cb:
                progress_cb(1, 1, f"{target_name}: 无新增回答")
            return {
                "token": url_token,
                "name": target_name,
                "folder": out_dir.name,
                "action": "incremental_none",
                "new_count": 0,
                "total_count": len(existing_md_ids),
                "md_file": str(md_file),
            }
    else:
        log_fn(f"用户「{target_name}」共获取到 {len(answers)} 条回答。")

    newly_processed_items = []
    total_to_process = len(answers)

    for i, ans in enumerate(answers):
        if stop_event and stop_event.is_set():
            log_fn("任务已被用户手动停止。")
            break

        aid = str(ans.get("id", ""))
        if not aid:
            continue

        if progress_cb:
            progress_cb(i + 1, max(total_to_process, 1), f"{target_name} [{i+1}/{total_to_process}]")

        # 如果缓存中已有该回答（且符合当前图片内嵌格式），直接复用缓存数据并加入待写入列表
        if aid in cache:
            cached_body = cache[aid].get("body_md", "")
            if not (embed_base64 and "](images/" in cached_body):
                log_fn(f"[{i+1}/{total_to_process}] 复用缓存: {cache[aid].get('question_title', aid)[:35]}")
                newly_processed_items.append(cache[aid])
                continue

        q = ans.get("question", {}) or {}
        q_title = q.get("title", "无标题")
        q_id = q.get("id")
        html_body = ans.get("content", "") or ""
        voteup = ans.get("voteup_count", 0)
        comment_count = ans.get("comment_count", 0)
        created_ts = int(ans.get("created_time", 0) or 0)
        created_str = (
            datetime.fromtimestamp(created_ts).strftime("%Y-%m-%d %H:%M")
            if created_ts else ""
        )

        md_body = await convert_answer_html_to_markdown(
            html_body=html_body,
            img_sem=img_sem,
            log_fn=log_fn,
            embed_base64=embed_base64,
            images_dir=images_dir,
            progress_prefix=f"  [{i+1}/{total_to_process}] ",
        )

        author_comments = []
        if fetch_comments and comment_count and comment_count > 0:
            log_fn(f"  [{i+1}/{total_to_process}] 抓取评论 ({comment_count} 条) → {q_title[:30]}")
            try:
                author_comments = await fetch_author_comments(page, aid, target_user_id, log_fn)
            except Exception as e:
                log_fn(f"  评论抓取失败: {e}")

        item_record = {
            "question_id": q_id,
            "question_title": q_title,
            "question_url": f"https://www.zhihu.com/question/{q_id}",
            "answer_id": aid,
            "answer_url": f"https://www.zhihu.com/question/{q_id}/answer/{aid}",
            "voteup": voteup,
            "comment_count": comment_count,
            "created_ts": created_ts,
            "created_time": created_str,
            "body_md": md_body,
            "author_comments": author_comments,
        }
        cache[aid] = item_record
        newly_processed_items.append(item_record)

        cache_file.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log_fn(f"[{i+1}/{total_to_process}] ✓ {q_title[:40]}")
        await asyncio.sleep(SLEEP_BETWEEN_REQ)

    if is_incremental_run:
        total_now = len(existing_md_ids | {it["answer_id"] for it in newly_processed_items})
        log_fn(f"正在将 {len(newly_processed_items)} 条新增回答按时间倒序前置写入 → {md_file}")
        prepend_new_answers_to_markdown(
            md_file=md_file,
            new_items=newly_processed_items,
            total_count=total_now,
        )
        action_type = "incremental_appended"
    else:
        total_now = len(cache)
        log_fn(f"正在生成完整 Markdown 文件 → {md_file}")
        write_full_markdown(
            md_file=md_file,
            target_name=target_name,
            url_token=url_token,
            items=list(cache.values()),
        )
        action_type = "full_downloaded"

    log_fn(f"用户「{target_name}」处理完成 → {md_file}")
    return {
        "token": url_token,
        "name": target_name,
        "folder": out_dir.name,
        "action": action_type,
        "new_count": len(newly_processed_items),
        "total_count": total_now,
        "md_file": str(md_file),
    }


async def run_batch_task(
    user_tokens: list,
    mode: str,
    base_dir: Path,
    embed_base64: bool,
    fetch_comments: bool,
    log_fn,
    wait_for_login_cb,
    progress_cb=None,
    stop_event: threading.Event = None,
    content_types: tuple = ("answers",),
) -> list:
    """启动 Playwright 浏览器，复用会话抓取所选作品类型。"""
    content_types = normalize_types(content_types)
    results = []
    img_sem = asyncio.Semaphore(CONCURRENCY_IMG)

    async with async_playwright() as p:
        log_fn("正在启动浏览器，请稍候...")
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://www.zhihu.com/", wait_until="domcontentloaded")

        log_fn("请在弹出的浏览器窗口中确认已完成知乎登录...")
        confirmed = await wait_for_login_cb()
        if not confirmed:
            log_fn("已取消操作，正在关闭浏览器。")
            await context.close()
            return results

        for idx, token in enumerate(user_tokens, start=1):
            if stop_event and stop_event.is_set():
                break
            log_fn("=" * 55)
            log_fn(f"开始处理第 {idx}/{len(user_tokens)} 位用户: {token}")
            user_meta = None
            for kind in content_types:
                if stop_event and stop_event.is_set():
                    break
                try:
                    if kind == "answers":
                        result = await process_single_user(
                            page=page, img_sem=img_sem, base_dir=base_dir,
                            url_token=token, mode=mode, embed_base64=embed_base64,
                            fetch_comments=fetch_comments, log_fn=log_fn,
                            progress_cb=progress_cb, stop_event=stop_event,
                        )
                    else:
                        if user_meta is None:
                            user_meta = await fetch_json(
                                page, f"https://www.zhihu.com/api/v4/members/{token}"
                            )
                        name = user_meta.get("name") or token
                        uid = str(user_meta.get("id") or "")
                        user_dir, _, _, _ = ensure_user_dir_with_id(
                            base_dir, token, name, log_fn=log_fn
                        )
                        result = await sync_user_works(
                            page=page, img_sem=img_sem, base_dir=user_dir,
                            token=token, kind=kind, mode=mode,
                            embed_base64=embed_base64, fetch_comments=fetch_comments,
                            fetch_json=fetch_json,
                            convert_html=convert_answer_html_to_markdown,
                            log_fn=log_fn, stop_event=stop_event,
                            progress_cb=progress_cb, user_name=name, user_id=uid,
                        )
                    results.append(result)
                except Exception as exc:
                    log_fn(f"❌ 用户 [{token}] 的 {kind} 归档失败: {exc}")
                    results.append({
                        "token": token, "name": token, "type": kind,
                        "action": "error", "error": str(exc),
                    })

        await context.close()
    return results


# ==================== 可视化界面 (Tkinter GUI) ====================


class ZhihuScraperApp:
    def __init__(self, root):
        self.root = root
        self.root.title("知乎用户回答批量下载与一键更新工具 v3.4")
        self.root.geometry("980x740")
        self.root.minsize(860, 650)

        self.log_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.login_event = threading.Event()
        self.login_confirmed = False
        self.is_running = False

        self.output_base_dir = tk.StringVar(value=str(BASE_DIR))
        self.update_list_path = tk.StringVar(value=str(DEFAULT_UPDATE_LIST_FILE))
        self.single_url_var = tk.StringVar(value=f"https://www.zhihu.com/people/{DEFAULT_URL_TOKEN}")
        self.embed_base64_var = tk.BooleanVar(value=True)
        self.fetch_comments_var = tk.BooleanVar(value=True)
        self.content_type_vars = {kind: tk.BooleanVar(value=(kind == "answers")) for kind in ("answers", *WORK_TYPES)}
        self.status_var = tk.StringVar(value="就绪")

        ensure_default_update_list(DEFAULT_UPDATE_LIST_FILE)
        self._build_ui()
        self._refresh_update_list_table()
        self.root.after(100, self._poll_log_queue)

    def _build_ui(self):
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")

        main_frame = ttk.Frame(self.root, padding=12)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # ---------- 1. 全局设置区 ----------
        cfg_frame = ttk.LabelFrame(main_frame, text="⚙️ 输出与图片内嵌设置（文件夹命名格式：zhihu_url_token(用户ID)）", padding=8)
        cfg_frame.pack(fill=tk.X, pady=(0, 8))

        row1 = ttk.Frame(cfg_frame)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="保存根目录:").pack(side=tk.LEFT)
        ttk.Entry(row1, textvariable=self.output_base_dir, width=55).pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        ttk.Button(row1, text="浏览...", command=self._choose_output_dir).pack(side=tk.LEFT)

        row2 = ttk.Frame(cfg_frame)
        row2.pack(fill=tk.X, pady=(6, 2))
        ttk.Checkbutton(
            row2,
            text="将图片以 Base64 内置插入到 .md 对应位置（单文件自包含，不生成单独图片文件）",
            variable=self.embed_base64_var,
        ).pack(side=tk.LEFT, padx=(0, 20))
        ttk.Checkbutton(
            row2,
            text="抓取作者在评论区的留言与回复",
            variable=self.fetch_comments_var,
        ).pack(side=tk.LEFT)

        types_frame = ttk.LabelFrame(main_frame, text="🗂 归档内容（多选）", padding=8)
        types_frame.pack(fill=tk.X, pady=(0, 8))
        for kind, label in (
            ("answers", "回答"), ("pins", "想法"), ("articles", "文章"),
            ("questions", "提问"), ("zvideos", "视频元数据"),
        ):
            ttk.Checkbutton(
                types_frame, text=label, variable=self.content_type_vars[kind]
            ).pack(side=tk.LEFT, padx=(2, 20))

        # ---------- 2. 指定单用户链接下载区 ----------
        single_frame = ttk.LabelFrame(
            main_frame,
            text="🔗 单用户回答下载（可传入知乎主页链接、url_token 或 url_token(用户ID)）",
            padding=8,
        )
        single_frame.pack(fill=tk.X, pady=(0, 8))

        s_row = ttk.Frame(single_frame)
        s_row.pack(fill=tk.X)
        ttk.Label(s_row, text="用户链接:").pack(side=tk.LEFT)
        url_entry = ttk.Entry(s_row, textvariable=self.single_url_var, width=48)
        url_entry.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)

        self.btn_single_full = ttk.Button(
            s_row, text="📥 全量归档所选", command=self._on_download_single_full
        )
        self.btn_single_full.pack(side=tk.LEFT, padx=3)

        self.btn_single_inc = ttk.Button(
            s_row, text="🔄 增量更新所选", command=self._on_download_single_incremental
        )
        self.btn_single_inc.pack(side=tk.LEFT, padx=3)

        ttk.Button(
            s_row, text="➕ 加入一键更新列表", command=self._on_add_single_to_list
        ).pack(side=tk.LEFT, padx=3)

        # ---------- 3. 一键更新列表维护区 ----------
        list_frame = ttk.LabelFrame(
            main_frame,
            text="📋 一键更新列表管理（自动识别 url_token(用户ID) 文件夹；未下载则从头下载，已下载则倒序前置写入新增回答）",
            padding=8,
        )
        list_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        file_row = ttk.Frame(list_frame)
        file_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(file_row, text="列表文件:").pack(side=tk.LEFT)
        ttk.Entry(file_row, textvariable=self.update_list_path, width=40).pack(
            side=tk.LEFT, padx=6, fill=tk.X, expand=True
        )
        ttk.Button(file_row, text="选择列表文件...", command=self._choose_list_file).pack(side=tk.LEFT, padx=2)
        ttk.Button(file_row, text="打开文件编辑", command=self._open_list_file_external).pack(side=tk.LEFT, padx=2)
        ttk.Button(file_row, text="扫描本地文件夹导入", command=self._scan_local_dirs_to_list).pack(side=tk.LEFT, padx=2)
        ttk.Button(file_row, text="刷新状态", command=self._refresh_update_list_table).pack(side=tk.LEFT, padx=2)

        columns = ("token", "user_name", "folder", "status", "count", "last_time")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=6)
        self.tree.heading("token", text="url_token")
        self.tree.heading("user_name", text="用户ID (昵称)")
        self.tree.heading("folder", text="存放文件夹名称")
        self.tree.heading("status", text="本地状态")
        self.tree.heading("count", text="答/想/文/问/视")
        self.tree.heading("last_time", text="最新回答时间")

        self.tree.column("token", width=110, anchor=tk.W)
        self.tree.column("user_name", width=140, anchor=tk.W)
        self.tree.column("folder", width=230, anchor=tk.W)
        self.tree.column("status", width=145, anchor=tk.CENTER)
        self.tree.column("count", width=130, anchor=tk.CENTER)
        self.tree.column("last_time", width=140, anchor=tk.CENTER)

        tree_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        action_bar = ttk.Frame(main_frame)
        action_bar.pack(fill=tk.X, pady=(0, 8))

        ttk.Button(action_bar, text="➖ 从列表移除选中用户", command=self._remove_selected_from_list).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(action_bar, text="📂 打开选中用户的 .md 文件", command=self._open_selected_md).pack(
            side=tk.LEFT, padx=6
        )

        self.btn_stop = ttk.Button(
            action_bar, text="⏹ 停止任务", command=self._on_stop_task, state=tk.DISABLED
        )
        self.btn_stop.pack(side=tk.RIGHT, padx=(6, 0))

        self.btn_batch_update = ttk.Button(
            action_bar,
            text="🚀 一键更新所选内容",
            command=self._on_batch_update_clicked,
        )
        self.btn_batch_update.pack(side=tk.RIGHT, padx=6)

        # ---------- 4. 进度与日志输出区 ----------
        prog_frame = ttk.Frame(main_frame)
        prog_frame.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(prog_frame, textvariable=self.status_var).pack(side=tk.LEFT)
        self.progress_bar = ttk.Progressbar(prog_frame, orient=tk.HORIZONTAL, length=320, mode="determinate")
        self.progress_bar.pack(side=tk.RIGHT)

        log_frame = ttk.LabelFrame(main_frame, text="📝 运行日志", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=11, font=("Consolas", 10))
        self.log_text.pack(fill=tk.BOTH, expand=True)

    # ---------- 列表与日志辅助方法 ----------

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {msg}")

    def _poll_log_queue(self):
        while not self.log_queue.empty():
            try:
                line = self.log_queue.get_nowait()
                self.log_text.insert(tk.END, line + "\n")
                self.log_text.see(tk.END)
            except queue.Empty:
                break
        self.root.after(100, self._poll_log_queue)

    def _update_progress(self, current: int, total: int, label: str = ""):
        def _apply():
            pct = int(round((current / max(total, 1)) * 100))
            self.progress_bar["value"] = pct
            if label:
                self.status_var.set(f"正在处理: {label} ({pct}%)")

        self.root.after(0, _apply)

    def _set_running_state(self, running: bool):
        self.is_running = running
        state_normal = tk.DISABLED if running else tk.NORMAL
        state_stop = tk.NORMAL if running else tk.DISABLED
        self.btn_single_full.configure(state=state_normal)
        self.btn_single_inc.configure(state=state_normal)
        self.btn_batch_update.configure(state=state_normal)
        self.btn_stop.configure(state=state_stop)
        if not running:
            self.status_var.set("就绪")
            self._refresh_update_list_table()

    def _choose_output_dir(self):
        selected = filedialog.askdirectory(initialdir=self.output_base_dir.get(), title="选择保存根目录")
        if selected:
            self.output_base_dir.set(selected)
            self._refresh_update_list_table()

    def _choose_list_file(self):
        selected = filedialog.askopenfilename(
            initialdir=str(BASE_DIR),
            title="选择一键更新列表文件",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if selected:
            self.update_list_path.set(selected)
            self._refresh_update_list_table()

    def _open_list_file_external(self):
        list_file = Path(self.update_list_path.get())
        ensure_default_update_list(list_file)
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(list_file))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(list_file)])
            else:
                subprocess.Popen(["xdg-open", str(list_file)])
        except Exception as e:
            messagebox.showinfo("列表文件路径", f"列表文件位于：\n{list_file}\n\n({e})")

    def _scan_local_dirs_to_list(self):
        """扫描保存根目录下的所有知乎用户文件夹，自动合并到更新列表中。"""
        base_dir = Path(self.output_base_dir.get())
        list_file = Path(self.update_list_path.get())
        entries = load_update_list_from_file(list_file)
        existing_map = {e["token"].lower(): e for e in entries}

        added = 0
        if base_dir.exists():
            for child in sorted(base_dir.iterdir()):
                if not child.is_dir():
                    continue
                tok, label, _ = match_folder_token_and_label(child.name)
                if not tok:
                    continue
                if not (
                    child.name.lower().startswith("zhihu_")
                    or (child / "answers.md").exists()
                    or (child / "answers_cache.json").exists()
                ):
                    continue
                if tok.lower() not in existing_map:
                    entries.append({"token": tok, "label": label})
                    existing_map[tok.lower()] = {"token": tok, "label": label}
                    added += 1
                elif label and not existing_map[tok.lower()].get("label"):
                    existing_map[tok.lower()]["label"] = label

        save_update_list_to_file(list_file, entries, base_dir=base_dir)
        self._refresh_update_list_table()
        self.log(f"扫描本地目录完成，新增导入 {added} 位用户至更新列表。")

    def _refresh_update_list_table(self):
        for row in self.tree.get_children():
            self.tree.delete(row)

        list_file = Path(self.update_list_path.get())
        base_dir = Path(self.output_base_dir.get())
        entries = load_update_list_from_file(list_file)

        for item in entries:
            token = item["token"]
            label = item.get("label", "")
            st = get_user_local_status(base_dir, token, fallback_label=label)
            status_text = "✅ 已下载（倒序前置续写）" if st["downloaded"] else "🆕 未下载（将从头下载）"
            self.tree.insert(
                "",
                tk.END,
                iid=token,
                values=(
                    token,
                    st["user_name"],
                    st["folder_name"],
                    status_text,
                    self._content_counts_display(base_dir, token, st["count"]),
                    st["last_answer_time"] or "-",
                ),
            )

    def _selected_content_types(self):
        kinds = [k for k, v in self.content_type_vars.items() if v.get()]
        if not kinds:
            messagebox.showwarning("未选择内容", "请至少勾选一种归档内容。")
            return ()
        return normalize_types(kinds)

    def _content_counts_display(self, base_dir, token, answer_count):
        directory, _, _, _ = get_user_paths(base_dir, token)
        result = [str(answer_count)]
        for kind in WORK_TYPES:
            try:
                file = directory / f"{kind}_cache.json"
                payload = json.loads(file.read_text(encoding="utf-8")) if file.exists() else {}
                result.append(str(len(payload)) if isinstance(payload, dict) else "?")
            except Exception:
                result.append("?")
        return "/".join(result)

    def _on_add_single_to_list(self):
        raw = self.single_url_var.get().strip()
        token, label = parse_zhihu_user_info(raw)
        if not token:
            messagebox.showwarning(
                "链接无效",
                "请输入有效的知乎用户主页链接（如 https://www.zhihu.com/people/mdk82）或 url_token(用户ID)。",
            )
            return

        list_file = Path(self.update_list_path.get())
        base_dir = Path(self.output_base_dir.get())
        entries = load_update_list_from_file(list_file)
        tokens_lower = [e["token"].lower() for e in entries]
        if token.lower() in tokens_lower:
            messagebox.showinfo("提示", f"用户 [{token}] 已存在于一键更新列表中。")
            return

        entries.append({"token": token, "label": label})
        save_update_list_to_file(list_file, entries, base_dir=base_dir)
        self._refresh_update_list_table()
        self.log(f"已将用户 [{token}] 添加至一键更新列表 ({list_file.name})。")

    def _remove_selected_from_list(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("提示", "请先在列表中选中要移除的用户。")
            return

        selected_lower = {s.lower() for s in selected}
        list_file = Path(self.update_list_path.get())
        base_dir = Path(self.output_base_dir.get())
        entries = load_update_list_from_file(list_file)
        remaining = [e for e in entries if e["token"].lower() not in selected_lower]
        save_update_list_to_file(list_file, remaining, base_dir=base_dir)
        self._refresh_update_list_table()
        self.log(f"已从更新列表中移除: {', '.join(selected)}")

    def _open_selected_md(self):
        selected = self.tree.selection()
        token = selected[0] if selected else parse_zhihu_user_token(self.single_url_var.get())
        if not token:
            messagebox.showinfo("提示", "请先在列表中选中一个用户，或在上方输入用户链接。")
            return

        _, _, md_file, _ = get_user_paths(Path(self.output_base_dir.get()), token)
        if not md_file.exists():
            messagebox.showinfo("文件不存在", f"该用户尚未生成 answers.md 文件：\n{md_file}")
            return

        try:
            if sys.platform.startswith("win"):
                os.startfile(str(md_file))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(md_file)])
            else:
                subprocess.Popen(["xdg-open", str(md_file)])
        except Exception as e:
            messagebox.showinfo("文件路径", f"Markdown 文件位于：\n{md_file}\n\n({e})")

    # ---------- 登录确认弹窗桥接 ----------

    async def _wait_for_login_gui(self) -> bool:
        self.login_event.clear()
        self.login_confirmed = False

        def _ask():
            ans = messagebox.askokcancel(
                "请确认知乎登录",
                "浏览器窗口已打开！\n\n"
                "1. 若尚未登录知乎，请在弹出的浏览器中扫码或密码登录；\n"
                "2. 若之前已登录过（自动保持会话），直接点击【确定】即可开始抓取。\n\n"
                "准备好后请点击【确定】继续，点击【取消】中止任务。",
            )
            self.login_confirmed = bool(ans)
            self.login_event.set()

        self.root.after(0, _ask)
        while not self.login_event.is_set():
            await asyncio.sleep(0.2)
        return self.login_confirmed

    # ---------- 任务触发与后台线程 ----------

    def _on_download_single_full(self):
        token = parse_zhihu_user_token(self.single_url_var.get())
        if not token:
            messagebox.showwarning("链接无效", "请输入有效的知乎用户主页链接（如 https://www.zhihu.com/people/mdk82）或用户标识。")
            return
        self._start_worker([token], mode="full")

    def _on_download_single_incremental(self):
        token = parse_zhihu_user_token(self.single_url_var.get())
        if not token:
            messagebox.showwarning("链接无效", "请输入有效的知乎用户主页链接（如 https://www.zhihu.com/people/mdk82）或用户标识。")
            return
        self._start_worker([token], mode="incremental")

    def _on_batch_update_clicked(self):
        self._refresh_update_list_table()
        list_file = Path(self.update_list_path.get())
        base_dir = Path(self.output_base_dir.get())
        entries = load_update_list_from_file(list_file)

        if not entries:
            messagebox.showwarning("列表为空", f"更新列表文件 ({list_file.name}) 中没有有效用户，请先添加用户。")
            return

        new_users = []
        inc_users = []
        for e in entries:
            st = get_user_local_status(base_dir, e["token"], fallback_label=e.get("label", ""))
            disp = f"{e['token']}({st['user_name']})" if st["user_name"] != "-" else e["token"]
            if st["downloaded"]:
                inc_users.append(f"• {disp}（已有 {st['count']} 条，倒序前置写入新增回答）")
            else:
                new_users.append(f"• {disp}（首次下载，将从头下载全部回答）")

        summary_lines = [f"即将更新列表中的 {len(entries)} 位知乎用户：\n"]
        if new_users:
            summary_lines.append("【首次下载（从头全量下载）】")
            summary_lines.extend(new_users[:8])
            if len(new_users) > 8:
                summary_lines.append(f"  ... 等共 {len(new_users)} 位用户")
            summary_lines.append("")
        if inc_users:
            summary_lines.append("【增量更新（将新增回答倒序写到已有回答之前）】")
            summary_lines.extend(inc_users[:8])
            if len(inc_users) > 8:
                summary_lines.append(f"  ... 等共 {len(inc_users)} 位用户")
            summary_lines.append("")

        summary_lines.append("是否确认立即开始一键更新？")
        confirmed = messagebox.askyesno("确认一键更新", "\n".join(summary_lines))
        if not confirmed:
            return

        tokens = [e["token"] for e in entries]
        self._start_worker(tokens, mode="incremental")

    def _on_stop_task(self):
        if self.is_running:
            self.stop_event.set()
            self.log("正在请求停止当前任务，请等待当前请求完成...")

    def _start_worker(self, tokens: list, mode: str):
        if self.is_running:
            messagebox.showinfo("任务进行中", "当前已有抓取任务正在运行，请等待完成或先停止。")
            return

        if not ensure_playwright_installed(self.root):
            return

        self.stop_event.clear()
        self.progress_bar["value"] = 0
        self._set_running_state(True)

        content_types = self._selected_content_types()
        if not content_types:
            self._set_running_state(False)
            return
        base_dir = Path(self.output_base_dir.get())
        embed_base64 = bool(self.embed_base64_var.get())
        fetch_comments = bool(self.fetch_comments_var.get())

        def _thread_target():
            try:
                results = asyncio.run(
                    run_batch_task(
                        user_tokens=tokens,
                        mode=mode,
                        base_dir=base_dir,
                        embed_base64=embed_base64,
                        fetch_comments=fetch_comments,
                        log_fn=self.log,
                        wait_for_login_cb=self._wait_for_login_gui,
                        progress_cb=self._update_progress,
                        stop_event=self.stop_event,
                        content_types=content_types,
                    )
                )
                self.root.after(0, lambda: self._on_worker_finished(results))
            except Exception as e:
                self.log(f"❌ 任务运行异常: {e}")
                self.root.after(0, lambda: self._set_running_state(False))

        t = threading.Thread(target=_thread_target, daemon=True)
        t.start()

    def _on_worker_finished(self, results: list):
        try:
            list_file = Path(self.update_list_path.get())
            base_dir = Path(self.output_base_dir.get())
            entries = load_update_list_from_file(list_file)
            if entries:
                save_update_list_to_file(list_file, entries, base_dir=base_dir)
        except Exception:
            pass

        self._set_running_state(False)
        if not results:
            return

        summary = []
        for r in results:
            act = r.get("action")
            name = r.get("name", r.get("token"))
            folder = r.get("folder", "")
            disp = f"{r.get('token')}({name})" if name and name != r.get("token") else name
            if act == "full_downloaded":
                summary.append(f"• {disp}: 全量下载完成（共 {r.get('total_count', 0)} 条）→ {folder}")
            elif act == "incremental_appended":
                summary.append(
                    f"• {disp}: 增量倒序前置写入 {r.get('new_count', 0)} 条新增回答（累计 {r.get('total_count', 0)} 条）→ {folder}"
                )
            elif act == "incremental_none":
                summary.append(f"• {disp}: 无新增回答（保持 {r.get('total_count', 0)} 条）")
            elif act == "works_synced":
                label = WORK_TYPES.get(r.get("type"), ("未知",))[0]
                summary.append(
                    f"• {disp} [{label}]: 新增 {r.get('new_count', 0)}、更新 {r.get('changed_count', 0)}、"
                    f"失败 {r.get('failed_count', 0)}、累计 {r.get('total_count', 0)} 条"
                )
            else:
                summary.append(f"• {disp}: 失败 ({r.get('error', '未知错误')})")

        self.log("=" * 55)
        self.log("🎉 任务执行汇总：")
        for line in summary:
            self.log("  " + line)
        messagebox.showinfo("任务完成", "本次抓取/更新已完成：\n\n" + "\n".join(summary))


# ==================== 命令行入口与 GUI 启动 ====================


async def cli_main(args):
    """命令行兼容模式（支持 --url 指定链接或 --update-list 一键更新列表）。"""
    import argparse

    parser = argparse.ArgumentParser(description="知乎用户回答批量导出与增量更新工具")
    parser.add_argument("--cli", action="store_true", help="使用命令行无界面模式运行")
    parser.add_argument("--url", type=str, default="", help="指定单个知乎用户链接或 url_token(用户ID)")
    parser.add_argument(
        "--update-list",
        type=str,
        default="",
        help="根据指定的一键更新列表文件（如 update_list.txt）执行批量增量更新",
    )
    parser.add_argument("--types", default="answers", help="逗号分隔：answers,pins,articles,questions,zvideos")
    parser.add_argument(
        "--full",
        action="store_true",
        help="强制从头全量下载（默认对已有记录的用户采用增量倒序前置更新）",
    )
    parser.add_argument(
        "--local-images",
        action="store_true",
        help="将图片保存为本地 images/ 相对路径（默认以 Base64 直接内置插入 .md 文件）",
    )
    parsed = parser.parse_args(args)

    if not ensure_playwright_installed(None):
        return

    def cli_log(msg):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

    async def cli_wait_login():
        print("\n" + "=" * 60)
        print("请在弹出的浏览器窗口里完成知乎登录（扫码即可）。")
        print("登录完成后，回到这里按回车继续...")
        print("=" * 60)
        input()
        return True

    tokens = []
    if parsed.update_list:
        list_file = Path(parsed.update_list)
        entries = load_update_list_from_file(list_file)
        tokens = [e["token"] for e in entries]
        if not tokens:
            cli_log(f"更新列表文件 {list_file} 为空。")
            return
        print(f"即将一键更新列表中的 {len(tokens)} 位用户: {', '.join(tokens)}")
        ans = input("确认开始更新吗？[Y/n]: ").strip().lower()
        if ans not in ("", "y", "yes"):
            cli_log("已取消更新。")
            return
    elif parsed.url:
        t = parse_zhihu_user_token(parsed.url)
        if not t:
            cli_log(f"无法从输入中解析出知乎用户标识: {parsed.url}")
            return
        tokens = [t]
    else:
        tokens = [DEFAULT_URL_TOKEN]

    mode = "full" if parsed.full else "incremental"
    await run_batch_task(
        user_tokens=tokens,
        mode=mode,
        base_dir=BASE_DIR,
        embed_base64=not parsed.local_images,
        fetch_comments=True,
        log_fn=cli_log,
        wait_for_login_cb=cli_wait_login,
        content_types=normalize_types(parsed.types),
    )


def main():
    if len(sys.argv) > 1 and any(a in sys.argv for a in ("--cli", "--url", "--update-list", "-h", "--help")):
        asyncio.run(cli_main(sys.argv[1:]))
    elif tk is not None:
        root = tk.Tk()
        ZhihuScraperApp(root)
        root.mainloop()
    else:
        print("当前 Python 环境未包含 tkinter 模块，已自动切换为命令行模式。")
        asyncio.run(cli_main(sys.argv[1:]))


if __name__ == "__main__":
    main()