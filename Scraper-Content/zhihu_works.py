#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知乎用户非回答作品归档：想法、文章、提问、视频。

不改变旧版 answers.md / answers_cache.json。HTTP 和 HTML 转换依赖由
zhihu_scraper.py 注入，以继续复用原有 Playwright 登录会话。
"""
import asyncio
import hashlib
import html
import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

WORK_TYPES = {
    "pins": ("想法", "pins"),
    "articles": ("文章", "articles"),
    "questions": ("提问", "questions"),
    "zvideos": ("视频", "zvideos"),
}
ALL_TYPES = ("answers", *WORK_TYPES)
PAGE_SIZE = 20
REQUEST_DELAY = 0.6
MAX_PAGES = 5000
OVERLAP_PAGES = 3
OVERLAP_KNOWN = 40
RECENT_RECHECK = 40


def normalize_types(raw):
    """规范、去重并校验作品类型，绝不隐式扩展至收藏或关注数据。"""
    if isinstance(raw, str):
        raw = raw.split(",")
    selected = []
    for value in raw:
        kind = str(value).strip().lower()
        if kind not in ALL_TYPES:
            raise ValueError("不支持的类型: " + kind + "；可选 " + ",".join(ALL_TYPES))
        if kind not in selected:
            selected.append(kind)
    if not selected:
        raise ValueError("请至少选择一种内容类型")
    return tuple(selected)


def list_url(token, kind, offset):
    if kind not in WORK_TYPES:
        raise ValueError(kind)
    return (
        "https://www.zhihu.com/api/v4/members/"
        + quote(token, safe="")
        + "/" + kind
        + "?limit=20&offset=" + str(offset)
        + ("&sort_by=created" if kind != "zvideos" else "")
    )


def detail_url(kind, content_id):
    content_id = quote(str(content_id), safe="")
    if kind == "pins":
        return f"https://www.zhihu.com/api/v4/pins/{content_id}?include=topics"
    if kind == "articles":
        return f"https://zhuanlan.zhihu.com/api/articles/{content_id}"
    if kind == "questions":
        return f"https://www.zhihu.com/api/v4/questions/{content_id}"
    # 视频列表通常已包含说明/封面；不假设每个视频详情接口可读。
    return None


def original_url(kind, item_id):
    if kind == "pins":
        return f"https://www.zhihu.com/pin/{item_id}"
    if kind == "articles":
        return f"https://zhuanlan.zhihu.com/p/{item_id}"
    if kind == "questions":
        return f"https://www.zhihu.com/question/{item_id}"
    return f"https://www.zhihu.com/zvideo/{item_id}"


def _ts(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _time(value):
    value = _ts(value)
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M") if value > 0 else ""


def _atomic_write(path, text):
    """先写临时文件再替换，避免任务中断时留下半份归档。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _atomic_json(path, data):
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def _read_json(path, default):
    if not path.exists():
        return default
    # 缓存损坏时不可默默返回空字典，否则会覆盖已有归档。
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, type(default)):
        raise ValueError(f"归档结构异常：{path}")
    return value


def _get_id(item):
    return str(item.get("id") or item.get("entity_id") or "").strip()


def _created(item):
    return _ts(item.get("created_time") or item.get("created") or item.get("created_at"))


def _updated(item):
    return _ts(item.get("updated_time") or item.get("updated") or item.get("updated_at"))


def _like_count(item):
    reaction = item.get("reaction")
    statistics = (reaction.get("statistics") or {}) if isinstance(reaction, dict) else {}
    return _ts(item.get("voteup_count") or item.get("like_count") or statistics.get("like_count"))


def _content_to_html(kind, detail):
    """只把可确认的文本/图片块转换成 HTML，其余结构原样保留在 JSON。"""
    if kind == "pins":
        body = detail.get("content_html")
        if isinstance(body, str) and body.strip():
            return body, True
        blocks = detail.get("content")
        if isinstance(blocks, list):
            parts = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                value = block.get("content")
                typ = str(block.get("type", "")).lower()
                if typ == "text" and isinstance(value, str):
                    parts.append("<p>" + html.escape(value).replace("\n", "<br>") + "</p>")
                elif typ in ("image", "images"):
                    image = value if isinstance(value, dict) else block
                    if isinstance(image, dict):
                        url = image.get("original_url") or image.get("url") or image.get("src")
                        if isinstance(url, str) and url.startswith(("https://", "http://")):
                            parts.append('<img src="' + html.escape(url, quote=True) + '">')
            if parts:
                return "\n".join(parts), True
        return "", False

    if kind == "articles":
        body = detail.get("content")
        return (body, True) if isinstance(body, str) and body.strip() else ("", False)

    if kind == "questions":
        body = detail.get("detail") or detail.get("excerpt")
        if isinstance(body, str) and body.strip():
            return body, True
        return "", False

    if kind == "zvideos":
        body = detail.get("description") or detail.get("excerpt") or ""
        if isinstance(body, str):
            # 视频说明可能是普通文本，不承诺视频文件已下载。
            return "<p>" + html.escape(body) + "</p>", True
    return "", False


def _display_title(kind, detail, item_id):
    name = detail.get("title") or detail.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip().replace("\n", " ")
    return f"{WORK_TYPES[kind][0]} #{item_id}"


def _new_record(kind, item, detail, body_md, detail_complete, comments):
    ident = _get_id(item)
    created = _created(detail) or _created(item)
    updated = _updated(detail) or _updated(item)
    title = _display_title(kind, detail, ident)
    payload = detail if detail_complete else item
    fingerprint = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "id": ident,
        "content_type": kind,
        "title": title,
        "url": original_url(kind, ident),
        "created_ts": created,
        "created_time": _time(created),
        "updated_ts": updated,
        "updated_time": _time(updated),
        "voteup": _like_count(detail) or _like_count(item),
        "comment_count": _ts(detail.get("comment_count") or item.get("comment_count")),
        "body_md": body_md,
        "detail_complete": bool(detail_complete),
        "body_sha256": fingerprint,
        "author_comments": comments,
        "raw_list_item": item,
        "raw_detail": detail if detail_complete else None,
    }


def _as_markdown(record):
    lines = [
        "## " + record["title"].replace("\n", " "),
        "",
        "- 内容类型: " + WORK_TYPES[record["content_type"]][0],
        "- 原文链接: " + record["url"],
        "- 发布时间: " + record.get("created_time", ""),
        "- 最后修改: " + record.get("updated_time", ""),
        "- 赞同数: " + str(record.get("voteup", 0)),
        "- 评论数: " + str(record.get("comment_count", 0)),
        "- 详情状态: " + ("仅视频元数据（未下载视频文件）" if record["content_type"] == "zvideos" else ("完整" if record.get("detail_complete") else "仅列表信息，待补全")),
        "",
        record.get("body_md", ""),
        "",
    ]
    comments = record.get("author_comments") or []
    if comments:
        lines += ["### 作者在评论区的留言 / 回复", ""]
        for cm in comments:
            if cm.get("kind") == "self_root":
                lines.append("- 作者本人评论: " + cm.get("content", ""))
            else:
                lines.append("- 原评论: " + cm.get("root_content", ""))
                lines.append("- 作者回复: " + cm.get("reply_content", ""))
        lines.append("")
    lines.extend(["---", ""])
    return "\n".join(lines)


def render_markdown(kind, token, name, records):
    """以缓存为唯一来源重建衍生 MD，确保更新旧作后不产生重复段落。"""
    ordered = sorted(
        records.values(),
        key=lambda r: (_ts(r.get("created_ts")), r.get("id", "")),
        reverse=True,
    )
    lines = [
        f"# 知乎用户「{name}」的全部{WORK_TYPES[kind][0]}",
        "",
        f"- 用户主页: https://www.zhihu.com/people/{token}",
        f"- 导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- {WORK_TYPES[kind][0]}总数: {len(ordered)}",
        "",
        "---",
        "",
    ]
    return "\n".join(lines) + "".join(_as_markdown(r) for r in ordered)


async def _fetch_with_retry(page, url, fetch_json, log_fn):
    """临时网络错误有限重试；鉴权/验证码错误不盲目轰炸。"""
    for attempt in range(3):
        try:
            return await fetch_json(page, url)
        except Exception as exc:
            message = str(exc)
            if any(mark in message for mark in ("403", "验证码", "安全验证", "未登录", "10003")):
                raise
            if attempt == 2:
                raise
            log_fn(f"  请求失败，退避重试 {attempt+1}/2: {message[:140]}")
            await asyncio.sleep(min(2 ** attempt, 4))
    raise RuntimeError("请求重试耗尽")


async def fetch_user_works_list(page, token, kind, mode, known_ids, fetch_json, log_fn, stop_event=None):
    """列表分页，增量至少重叠 3 页/40 条；全量不以旧 ID 为停止条件。"""
    entries = {}
    offset, known_run, pages = 0, 0, 0
    seen_end = False
    interrupted = False
    while pages < MAX_PAGES:
        if stop_event and stop_event.is_set():
            interrupted = True
            break
        result = await _fetch_with_retry(page, list_url(token, kind, offset), fetch_json, log_fn)
        batch = result.get("data", [])
        if not isinstance(batch, list):
            raise RuntimeError("作品列表未返回 data 数组")
        pages += 1
        if not batch:
            seen_end = True
            break
        new_in_page = 0
        for entry in batch:
            if not isinstance(entry, dict):
                continue
            ident = _get_id(entry)
            if not ident or ident in entries:
                continue
            entries[ident] = entry
            new_in_page += 1
            known_run = known_run + 1 if ident in known_ids else 0
        paging = result.get("paging") or {}
        if paging.get("is_end") is True:
            seen_end = True
            break
        if new_in_page == 0:
            raise RuntimeError("分页没有新 ID，可能陷入重复页；停止以保护归档")
        if mode == "incremental" and pages >= OVERLAP_PAGES and known_run >= OVERLAP_KNOWN:
            break
        offset += len(batch)
        await asyncio.sleep(REQUEST_DELAY)
    else:
        raise RuntimeError(f"超过分页上限 {MAX_PAGES}，未确认列表结束")
    log_fn(f"  {WORK_TYPES[kind][0]}列表: 扫描 {pages} 页、{len(entries)} 条；全量结束={seen_end}，停止={interrupted}")
    return entries, seen_end, interrupted


async def fetch_author_comments(page, kind, content_id, user_id, fetch_json, log_fn):
    """评论接口因平台版本变化可能不兼容；仅收集作者自己发表的可见评论。"""
    if not user_id:
        return [], False
    comments = []
    offset = 0
    for _ in range(100):
        url = (
            f"https://www.zhihu.com/api/v4/comment_v5/{kind}/{content_id}/root_comment"
            f"?order_by=score&limit=20&offset={offset}"
        )
        try:
            data = await fetch_json(page, url)
        except Exception as exc:
            log_fn(f"  {kind}作者评论抓取不可用: {str(exc)[:120]}")
            return comments, False
        batch = data.get("data", [])
        if not isinstance(batch, list):
            return comments, False
        for cm in batch:
            if not isinstance(cm, dict):
                continue
            author = cm.get("author") or {}
            if author.get("id") == user_id or author.get("is_author") is True:
                comments.append({"kind": "self_root", "content": cm.get("content") or cm.get("text") or ""})
            # 新接口若在根评论中携带子评论，则同时过滤作者回复。
            children = cm.get("child_comments")
            if isinstance(children, list):
                for child in children:
                    who = child.get("author") or {}
                    if who.get("id") == user_id or who.get("is_author") is True:
                        comments.append({
                            "kind": "self_reply",
                            "root_content": cm.get("content") or cm.get("text") or "",
                            "reply_content": child.get("content") or child.get("text") or "",
                        })
        if (data.get("paging") or {}).get("is_end", True) or not batch:
            return comments, True
        offset += len(batch)
        await asyncio.sleep(REQUEST_DELAY)
    log_fn("  评论分页超过安全上限，标记为未完整")
    return comments, False


async def sync_user_works(
    page, img_sem, base_dir: Path, token: str, kind: str, mode: str,
    embed_base64: bool, fetch_comments: bool, fetch_json, convert_html,
    log_fn, stop_event=None, progress_cb=None, user_name="", user_id="",
):
    """单用户单作品类型归档。每条成功后持久化缓存，结束时原子重建 MD。"""
    if kind not in WORK_TYPES or mode not in ("full", "incremental"):
        raise ValueError("未知作品类型或同步模式")
    # 与旧版回答目录规则相同，由调用方传入已解析的用户根目录。
    base_dir.mkdir(parents=True, exist_ok=True)
    cache_file = base_dir / f"{kind}_cache.json"
    md_file = base_dir / f"{kind}.md"
    state_file = base_dir / f"{kind}_state.json"
    cache = _read_json(cache_file, {})
    previous_count = len(cache)
    entries, reached_end, stopped = await fetch_user_works_list(
        page, token, kind, mode, set(cache), fetch_json, log_fn, stop_event
    )
    new_count = changed_count = failed_count = partial_count = 0
    failures = []
    for idx, (ident, item) in enumerate(entries.items(), 1):
        if stop_event and stop_event.is_set():
            stopped = True
            break
        old = cache.get(ident)
        needs_refresh = (
            old is None or mode == "full" or idx <= RECENT_RECHECK
            or not old.get("detail_complete")
            or _updated(item) > _ts(old.get("updated_ts"))
        )
        if not needs_refresh:
            continue
        if progress_cb:
            progress_cb(idx, len(entries), f"{WORK_TYPES[kind][0]} [{idx}/{len(entries)}]")
        detail = item
        detail_complete = kind == "zvideos"  # 视频目前以列表元数据作为完整元数据，未下载视频。
        detail_link = detail_url(kind, ident)
        if detail_link:
            try:
                detail = await _fetch_with_retry(page, detail_link, fetch_json, log_fn)
                detail_complete = True
            except Exception as exc:
                # 文章等如拿不到正文，不能把列表摘要冒充完整全文。
                if old and old.get("detail_complete"):
                    failed_count += 1
                    failures.append({"id": ident, "reason": str(exc)[:300]})
                    continue
                log_fn(f"  {kind}/{ident} 详情无法取得，暂存列表记录: {str(exc)[:160]}")
                detail = item
                detail_complete = False
                failed_count += 1
                failures.append({"id": ident, "reason": str(exc)[:300]})
        html_body, has_body = _content_to_html(kind, detail)
        if not has_body and kind in ("pins", "articles") and detail_complete:
            detail_complete = False  # 没有全文时不宣称完整
        if not has_body:
            fallback = detail.get("excerpt") or item.get("excerpt") or ""
            if isinstance(fallback, str) and fallback:
                html_body = "<p>" + html.escape(fallback) + "</p>"
        try:
            body_md = await convert_html(
                html_body=html_body, img_sem=img_sem, log_fn=log_fn,
                embed_base64=embed_base64, images_dir=base_dir / "images",
                progress_prefix=f"  [{kind} {idx}/{len(entries)}] ",
            )
        except Exception as exc:
            failed_count += 1
            failures.append({"id": ident, "reason": "转换失败: " + str(exc)[:250]})
            continue
        author_comments = old.get("author_comments", []) if old else []
        if fetch_comments and _ts(detail.get("comment_count") or item.get("comment_count")):
            got, ok = await fetch_author_comments(page, kind, ident, user_id, fetch_json, log_fn)
            if ok:
                author_comments = got
        record = _new_record(kind, item, detail, body_md, detail_complete, author_comments)
        if old is None:
            new_count += 1
        elif old.get("body_sha256") != record["body_sha256"] or old.get("detail_complete") != record["detail_complete"]:
            changed_count += 1
        cache[ident] = record
        _atomic_json(cache_file, cache)
        if not record["detail_complete"]:
            partial_count += 1
        await asyncio.sleep(REQUEST_DELAY)

    # 未被本次列表扫描到的旧数据一律保留，绝不自动删除。
    missing = sorted(set(cache) - set(entries)) if reached_end and not stopped else []
    report = {
        "type": kind, "token": token, "mode": mode,
        "scanned_count": len(entries), "previous_count": previous_count,
        "total_count": len(cache), "new_count": new_count,
        "changed_count": changed_count, "failed_count": failed_count,
        "partial_count_this_run": partial_count,
        "incomplete_records": sum(not r.get("detail_complete") for r in cache.values()),
        "list_reached_end": reached_end, "interrupted": stopped,
        "not_seen_in_full_scan": missing if mode == "full" else [],
        "failures": failures, "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _atomic_json(state_file, report)
    _atomic_write(md_file, render_markdown(kind, token, user_name or token, cache))
    log_fn(f"  {WORK_TYPES[kind][0]}归档完成: +{new_count}, 修改{changed_count}, 失败{failed_count}, 总数{len(cache)}")
    return {
        "token": token, "name": user_name or token, "folder": base_dir.name,
        "action": "works_synced", "type": kind, "new_count": new_count,
        "changed_count": changed_count, "failed_count": failed_count,
        "total_count": len(cache), "md_file": str(md_file),
        "list_reached_end": reached_end, "interrupted": stopped,
    }
