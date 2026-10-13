import json
import tempfile
import unittest
from pathlib import Path

from core import (parse_markdown, parse_cache, cleaning, load_records, classify,
                  filter_records, lens_evidence, repeated_texts, save_override, load_overrides,
                  apply_overrides)


class ImportAndAnalysisTests(unittest.TestCase):
    def test_real_markdown_every_entry_and_metadata(self):
        path = Path(__file__).parents[1] / "answers.md"
        if not path.exists():
            self.skipTest("仅在包含示例源数据的发行包中运行")
        rows, meta = parse_markdown(path)
        self.assertEqual(meta["declared"], 3906)
        self.assertEqual(meta["parsed"], 3906)
        self.assertEqual(len({r["id"] for r in rows}), 3906)
        self.assertTrue(all(r["title"] and r["body"] and r["url"] for r in rows))
        self.assertEqual(rows[0]["date"], "2026-10-12 21:55")
        self.assertEqual(rows[0]["title"], "男朋友妈妈以死相逼我们分手，我该怎么办？")

    def test_real_classification(self):
        p = Path(__file__).parents[1] / "answers.md"
        if not p.exists():
            self.skipTest("缺少示例数据")
        rows, meta = load_records(p)
        self.assertEqual(len(rows), 3906)
        stock = [r for r in rows if r["title"] == "在牛市中，你是怎么保持进攻节奏，把资金效率用到最高的？"]
        self.assertEqual(len(stock), 1)
        self.assertIn("股票金融", stock[0]["tags"])
        self.assertTrue(lens_evidence(stock, "成交量与牛熊判断"))
        self.assertEqual(meta["zero_vote_records"], 3906)
        self.assertEqual(meta["comments"], 0)

    def test_cleaning_not_damage_source(self):
        raw = "这是**我**的[链接](https://example.org) ![图](images/a.png)\n\n> 观点"
        clean = cleaning(raw)
        self.assertIn("这是我的链接", clean)
        self.assertNotIn("images/a.png", clean)
        self.assertIn("观点", clean)
        self.assertIn("![图]", raw)

    def test_parsing_comment_blocks(self):
        demo = """# A

- 回答总数: 1

## 问题

- 原文链接: https://www.zhihu.com/question/1/answer/21
- 发布时间: 2025-01-01 10:12
- 赞同数: 9

答案正文

### 💬 作者在评论区的留言 / 回复

- **作者本人评论**: 哈哈

> **张三**: 为什么

**作者回复**: 因为如此

---
"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "demo.md"
            p.write_text(demo, encoding="utf8")
            rows, meta = parse_markdown(p)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["body"], "答案正文")
            self.assertEqual([c["text"] for c in rows[0]["comments"]], ["哈哈", "因为如此"])

    def test_json_cache_comments(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cache.json"
            p.write_text(json.dumps({"1": {"answer_url": "https://www.zhihu.com/question/1/answer/2",
                     "question_title": "在炒股吗？", "created_time": "2025-01-01", "voteup": 2,
                     "body_md": "做股票交易", "author_comments": [{"kind":"self_reply", "reply_content": "回复A", "root_content":"对方Q"}]}},
                    ensure_ascii=False), encoding="utf8")
            rows, _ = parse_cache(p)
            self.assertEqual(rows[0]["votes"], 2)
            self.assertEqual(rows[0]["comments"][0]["text"], "回复A")

    def test_filter_and_manual_notes(self):
        rows = [{"id":"1", "title":"股票问题", "body":"交易风险", "comments":[], "date":"2026-10-01",
                "category":"股票金融", "subcategory":"股票与交易", "tags":["股票金融"],"url":"https://example.org",
                "length":4, "votes":1}]
        self.assertEqual(len(filter_records(rows, cats=["股票金融"], query="交易")),1)
        self.assertFalse(filter_records(rows, query="黄金"))
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"overrides.json"
            save_override(p,"1", {"category":"心理成长", "subcategory":"认知与思考", "tags":["心理成长"], "note":"复核", "favorite":True})
            self.assertIn("1",load_overrides(p))
            altered=apply_overrides(rows,load_overrides(p))
            self.assertEqual(altered[0]["category"],"心理成长")
            self.assertEqual(altered[0]["note"],"复核")
            self.assertTrue(altered[0]["favorite"])
            self.assertEqual(rows[0]["category"],"股票金融")


if __name__ == "__main__":
    unittest.main()
