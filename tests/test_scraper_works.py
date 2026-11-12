# -*- coding: utf-8 -*-
"""多类型知乎归档离线测试：使用假数据，绝不登录真实账号。"""
import asyncio
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Scraper-Content"))
from zhihu_works import (
    _content_to_html, detail_url, fetch_user_works_list, list_url,
    normalize_types, render_markdown, sync_user_works,
)


def pin(ident, body, ts):
    return {
        "id": ident, "created_time": ts, "updated_time": ts,
        "content_html": f"<p>{body}</p>", "comment_count": 0, "like_count": 3,
    }


class FakeFetcher:
    def __init__(self, kind, rows, details=None):
        self.kind = kind
        self.rows = rows
        self.details = details or {}
        self.calls = []

    async def __call__(self, _page, url):
        self.calls.append(url)
        parsed = urlparse(url)
        if parsed.path.endswith("/" + self.kind):
            offset = int(parse_qs(parsed.query).get("offset", ["0"])[0])
            batch = self.rows[offset:offset + 20]
            return {
                "data": batch,
                "paging": {"is_end": offset + 20 >= len(self.rows)},
            }
        ident = parsed.path.split("/")[-1]
        return self.details.get(ident, next((x for x in self.rows if str(x["id"]) == ident), {}))


async def fake_html(**kwargs):
    return re.sub(r"<[^>]*>", "", kwargs.get("html_body", ""))


async def run_work(directory, kind, fetch, mode="incremental"):
    return await sync_user_works(
        page=None, img_sem=asyncio.Semaphore(1),
        base_dir=Path(directory), token="sample", kind=kind, mode=mode,
        embed_base64=False, fetch_comments=False, fetch_json=fetch,
        convert_html=fake_html, log_fn=lambda msg: None,
        user_name="测试作者", user_id="example",
    )


class WorkArchiveTests(unittest.TestCase):
    def test_types_validation(self):
        self.assertEqual(normalize_types("pins,answers,pins,articles"), ("pins", "answers", "articles"))
        with self.assertRaises(ValueError):
            normalize_types("favorites")
        with self.assertRaises(ValueError):
            normalize_types("")

    def test_endpoints(self):
        self.assertIn("/members/test/pins?", list_url("test", "pins", 20))
        self.assertEqual(detail_url("articles", 321), "https://zhuanlan.zhihu.com/api/articles/321")
        self.assertIsNone(detail_url("zvideos", 123))

    def test_pin_structured_fallback(self):
        html_body, full = _content_to_html("pins", {
            "content": [{"type": "text", "content": "第一段"}, {"type": "image", "url": "https://pic.example/a.png"}]
        })
        self.assertTrue(full)
        self.assertIn("第一段", html_body)
        self.assertIn("a.png", html_body)

    def test_pin_repeat_and_edit_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = [pin("100", "旧内容", 1700000000), pin("101", "第二条", 1700000100)]
            first = asyncio.run(run_work(tmp, "pins", FakeFetcher("pins", records)))
            self.assertEqual(first["new_count"], 2)
            self.assertEqual(first["total_count"], 2)
            path = Path(tmp) / "pins.md"
            self.assertEqual(path.read_text(encoding="utf-8").count("- 原文链接:"), 2)

            same = asyncio.run(run_work(tmp, "pins", FakeFetcher("pins", records)))
            self.assertEqual(same["new_count"], 0)
            self.assertEqual(same["changed_count"], 0)

            edited = [pin("100", "新内容", 1700000200), records[1]]
            updated = asyncio.run(run_work(tmp, "pins", FakeFetcher("pins", edited), mode="full"))
            self.assertEqual(updated["new_count"], 0)
            self.assertEqual(updated["changed_count"], 1)
            body = path.read_text(encoding="utf-8")
            self.assertIn("新内容", body)
            self.assertNotIn("旧内容", body)
            self.assertEqual(body.count("- 原文链接:"), 2)
            self.assertEqual(len(json.loads((Path(tmp) / "pins_cache.json").read_text(encoding="utf-8"))), 2)

    def test_no_delete_on_full_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [pin("100", "A", 1700000000), pin("101", "B", 1700000100)]
            asyncio.run(run_work(tmp, "pins", FakeFetcher("pins", rows), mode="full"))
            asyncio.run(run_work(tmp, "pins", FakeFetcher("pins", rows[1:]), mode="full"))
            state = json.loads((Path(tmp) / "pins_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["not_seen_in_full_scan"], ["100"])
            self.assertEqual(state["total_count"], 2)

    def test_article_detail_preferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = {"id": "42", "title": "短标题", "created": 1700000000, "excerpt": "摘要"}
            detail = {"id": "42", "title": "完整文章", "content": "<p>全文部分</p>", "created": 1700000000}
            asyncio.run(run_work(tmp, "articles", FakeFetcher("articles", [row], {"42": detail})))
            md = (Path(tmp) / "articles.md").read_text(encoding="utf-8")
            self.assertIn("全文部分", md)
            self.assertIn("- 详情状态: 完整", md)

    def test_video_is_metadata_not_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = {"id": "video42", "title": "演示视频", "description": "介绍", "created": 1700000000}
            asyncio.run(run_work(tmp, "zvideos", FakeFetcher("zvideos", [row])))
            md = (Path(tmp) / "zvideos.md").read_text(encoding="utf-8")
            self.assertIn("/zvideo/video42", md)
            self.assertIn("介绍", md)
            self.assertFalse(list(Path(tmp).glob("*.mp4")))

    def test_journal_replay_after_interrupted_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run_work(tmp, "pins", FakeFetcher("pins", [pin("1", "已归档", 1700000000)])))
            recovered = {
                "id": "2", "content_type": "pins", "title": "恢复条目",
                "url": "https://www.zhihu.com/pin/2", "created_ts": 1700000100,
                "created_time": "2023-11-14 22:15", "updated_time": "",
                "body_md": "中断前已保存的正文", "detail_complete": True,
                "voteup": 0, "comment_count": 0, "author_comments": [],
                "body_sha256": "saved"
            }
            journal = Path(tmp) / "pins_updates.jsonl"
            journal.write_text(json.dumps(recovered, ensure_ascii=False) + "\n", encoding="utf-8")
            result = asyncio.run(run_work(tmp, "pins", FakeFetcher("pins", []), mode="full"))
            self.assertEqual(result["total_count"], 2)
            self.assertIn("中断前已保存的正文", (Path(tmp) / "pins.md").read_text(encoding="utf-8"))
            self.assertFalse(journal.exists())

    def test_invalid_cache_must_not_be_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pins_cache.json"
            path.write_text("{bad json", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                asyncio.run(run_work(tmp, "pins", FakeFetcher("pins", [])))
            self.assertEqual(path.read_text(encoding="utf-8"), "{bad json")

    def test_incremental_overlap_reaches_existing_page(self):
        rows = [pin(str(i), "文字", 1700000000 + i) for i in range(70)]
        fetched, reached, stopped = asyncio.run(
            fetch_user_works_list(None, "sample", "pins", "incremental", {str(i) for i in range(70)},
                                  FakeFetcher("pins", rows), lambda msg: None)
        )
        self.assertGreaterEqual(len(fetched), 40)
        self.assertFalse(stopped)


if __name__ == "__main__":
    unittest.main()
