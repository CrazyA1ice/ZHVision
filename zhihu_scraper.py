#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知乎用户回答批量导出工具（v2）
目标用户: https://www.zhihu.com/people/mdk82
"""

import asyncio
import json
import re
import hashlib
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime

import aiohttp
from playwright.async_api import async_playwright
import html2text

URL_TOKEN = "mdk82"
PAGE_SIZE = 20
SLEEP_BETWEEN_REQ = 0.6
CONCURRENCY_IMG = 6

BASE_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = BASE_DIR / f"zhihu_{URL_TOKEN}"
IMAGES_DIR = OUTPUT_DIR / "images"
CACHE_FILE = OUTPUT_DIR / "answers_cache.json"
MD_FILE = OUTPUT_DIR / "answers.md"
PROFILE_DIR = BASE_DIR / ".pw_profile"


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


h2t = html2text.HTML2Text()
h2t.body_width = 0
h2t.ignore_links = False
h2t.ignore_images = False
h2t.protect_links = True
h2t.unicode_snob = True


async def fetch_json(page, url):
    """用 page.goto 直接导航到 API URL，绕过 x-zse-96 签名。"""
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
            f"HTTP {status}，返回不是 JSON（前 500 字符）：\n{body[:500]}\nURL: {url}"
        )
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"API 错误：{data['error']}\nURL: {url}")
    return data


_IMG_TAG_RE = re.compile(r"<img[^>]*>", re.IGNORECASE)


def extract_image_urls(html):
    urls = []
    for tag in _IMG_TAG_RE.findall(html):
        src = None
        m = re.search(r'data-original="([^"]+)"', tag)
        if m:
            src = m.group(1)
        else:
            m = re.search(r'\ssrc="([^"]+)"', tag)
            if m:
                src = m.group(1)
        if src and not src.startswith("data:") and src.startswith("http"):
            urls.append(src)
    return urls


async def download_image(session, url, dest_dir):
    parsed = urlparse(url)
    ext = Path(parsed.path).suffix or ".jpg"
    if len(ext) > 5:
        ext = ".jpg"
    name = hashlib.md5(url.encode("utf-8")).hexdigest() + ext
    dest = dest_dir / name
    if dest.exists() and dest.stat().st_size > 0:
        return name
    headers = {
        "Referer": "https://www.zhihu.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    }
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status == 200:
                dest.write_bytes(await r.read())
                return name
            else:
                log(f"  图片下载 {r.status}: {url[:80]}")
    except Exception as e:
        log(f"  图片异常 {e}: {url[:80]}")
    return None


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


async def fetch_all_answers(page, url_token):
    answers = []
    offset = 0
    while True:
        url = (
            f"https://www.zhihu.com/api/v4/members/{url_token}/answers"
            f"?include={ANSWER_INCLUDE}"
            f"&limit={PAGE_SIZE}&offset={offset}&order=created"
        )
        log(f"拉回答列表 offset={offset}")
        data = await fetch_json(page, url)
        batch = data.get("data", [])
        if not batch:
            break
        answers.extend(batch)
        paging = data.get("paging", {})
        if paging.get("is_end"):
            break
        offset += PAGE_SIZE
        await asyncio.sleep(SLEEP_BETWEEN_REQ)
    return answers


async def fetch_author_comments(page, answer_id, target_user_id):
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
            log(f"  主评论 offset={offset} 失败: {e}")
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
                        log(f"  子评论失败: {e}")
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


async def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    cache = {}
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            log(f"已加载缓存: {len(cache)} 条")
        except Exception:
            cache = {}

    async with async_playwright() as p:
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

        print("\n" + "=" * 60)
        print("请在弹出的浏览器窗口里完成知乎登录（扫码即可）。")
        print("登录完成后，回到这里按回车继续...")
        print("=" * 60)
        input()

        me = await fetch_json(
            page, f"https://www.zhihu.com/api/v4/members/{URL_TOKEN}"
        )
        target_user_id = me.get("id")
        target_name = me.get("name", URL_TOKEN)
        log(f"目标用户: {target_name}  (id={target_user_id})")

        answers = await fetch_all_answers(page, URL_TOKEN)
        log(f"共 {len(answers)} 条回答")

        timeout = aiohttp.ClientTimeout(total=60)
        connector = aiohttp.TCPConnector(limit=CONCURRENCY_IMG)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            for i, ans in enumerate(answers):
                aid = str(ans.get("id", ""))
                if not aid:
                    continue
                if aid in cache:
                    log(f"[{i+1}/{len(answers)}] 跳过缓存: {aid}")
                    continue

                q = ans.get("question", {}) or {}
                q_title = q.get("title", "无标题")
                q_id = q.get("id")
                html_body = ans.get("content", "") or ""
                voteup = ans.get("voteup_count", 0)
                comment_count = ans.get("comment_count", 0)
                created_ts = ans.get("created_time", 0) or 0
                created_str = (
                    datetime.fromtimestamp(created_ts).strftime("%Y-%m-%d %H:%M")
                    if created_ts else ""
                )

                img_urls = extract_image_urls(html_body)
                img_map = {}
                if img_urls:
                    log(f"[{i+1}/{len(answers)}] {len(img_urls)} 张图 → {q_title[:30]}")
                    tasks = [download_image(session, u, IMAGES_DIR) for u in img_urls]
                    results = await asyncio.gather(*tasks)
                    for u, local in zip(img_urls, results):
                        if local:
                            img_map[u] = f"images/{local}"

                processed_html = html_body
                for orig, local in img_map.items():
                    processed_html = processed_html.replace(orig, local)

                md_body = h2t.handle(processed_html).strip()

                author_comments = []
                if comment_count and comment_count > 0:
                    log(f"[{i+1}/{len(answers)}] 抓评论 ({comment_count} 条) → {q_title[:30]}")
                    try:
                        author_comments = await fetch_author_comments(
                            page, aid, target_user_id
                        )
                    except Exception as e:
                        log(f"  评论抓取失败: {e}")

                cache[aid] = {
                    "question_id": q_id,
                    "question_title": q_title,
                    "question_url": f"https://www.zhihu.com/question/{q_id}",
                    "answer_id": aid,
                    "answer_url": f"https://www.zhihu.com/question/{q_id}/answer/{aid}",
                    "voteup": voteup,
                    "comment_count": comment_count,
                    "created_time": created_str,
                    "body_md": md_body,
                    "author_comments": author_comments,
                }

                CACHE_FILE.write_text(
                    json.dumps(cache, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                log(f"[{i+1}/{len(answers)}] ✓ {q_title[:40]}")
                await asyncio.sleep(SLEEP_BETWEEN_REQ)

        await context.close()

    log("生成 answers.md ...")
    items = sorted(
        cache.values(),
        key=lambda x: x.get("created_time", ""),
        reverse=True,
    )

    lines = [
        f"# 知乎用户「{target_name}」的全部回答",
        "",
        f"- 用户主页: https://www.zhihu.com/people/{URL_TOKEN}",
        f"- 导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 回答总数: {len(items)}",
        "",
        "---",
        "",
    ]

    for it in items:
        lines.append(f"## {it['question_title']}")
        lines.append("")
        lines.append(f"- 原文链接: {it['answer_url']}")
        lines.append(f"- 发布时间: {it['created_time']}")
        lines.append(f"- 赞同数: {it['voteup']}")
        lines.append("")
        lines.append(it.get("body_md", ""))
        lines.append("")

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

    MD_FILE.write_text("\n".join(lines), encoding="utf-8")
    log(f"完成 → {MD_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
