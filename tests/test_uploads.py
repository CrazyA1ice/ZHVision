import tempfile
from pathlib import Path
import unittest
from hashlib import sha256

from core import parse_markdown_text, load_records, extractive_topic_summary, apply_overrides, save_override, load_overrides


class ReusableUploadsTests(unittest.TestCase):
    def test_flexible_md_and_body_heading(self):
        doc = '''# 知乎用户「甲」的回答
- 回答总数: 2

## 如何炒股？
- 发布时间: 2026-09-01 00:00
- 原文链接: https://www.zhihu.com/question/1/answer/30

关于成交量，最好有自己的纪律。

## 我自己的小标题
这是同一篇正文。

## 另一个用户的回答
- 原文链接: https://www.zhihu.com/question/2/answer/31
- 赞同数: 8
- 发布时间: 2026-09-02

认真学习，别急。
'''
        rows, meta = parse_markdown_text(doc)
        self.assertEqual(meta['parsed'], 2)
        self.assertEqual(meta['declared'], 2)
        self.assertEqual(rows[0]['votes'], 0)
        self.assertIn('## 我自己的小标题', rows[0]['body_md'])
        self.assertEqual(rows[1]['votes'], 8)
        self.assertIn('认真学习', rows[1]['body'])

    def test_crlf_and_bom(self):
        doc = '\ufeff## 问题一\r\n- 原文链接: https://www.zhihu.com/question/1/answer/9\r\n- 发布时间: 2026-10-13\r\n\r\n内容\r\n'
        rows, _ = parse_markdown_text(doc)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['body'], '内容')

    def test_another_file_overrides_isolated(self):
        doc_a = '## 股票A\n- 原文链接: https://www.zhihu.com/question/1/answer/1\n- 发布时间: 2026-01-01\n\n成交量重要。\n'
        doc_b = '## 哲学B\n- 原文链接: https://www.zhihu.com/question/1/answer/1\n- 发布时间: 2026-01-01\n\n庄子思想。\n'
        with tempfile.TemporaryDirectory() as d:
            p_a, p_b = Path(d) / 'a.md', Path(d) / 'b.md'
            p_a.write_text(doc_a, encoding='utf8'); p_b.write_text(doc_b, encoding='utf8')
            a, ma = load_records(p_a); b, mb = load_records(p_b)
            self.assertEqual(len(a), 1); self.assertEqual(len(b), 1)
            overrides_a = Path(d) / (sha256(doc_a.encode()).hexdigest()[:24] + '.json')
            overrides_b = Path(d) / (sha256(doc_b.encode()).hexdigest()[:24] + '.json')
            save_override(overrides_a, a[0]['id'], {'category': '股票金融', 'tags': ['股票金融'], 'note': 'A独有'})
            self.assertNotEqual(overrides_a, overrides_b)
            self.assertEqual(apply_overrides(a, load_overrides(overrides_a))[0]['note'], 'A独有')
            self.assertEqual(apply_overrides(b, load_overrides(overrides_b))[0]['note'], '')

    def test_extractive_quotes_are_in_source(self):
        rows, _ = parse_markdown_text('''## 股市如何理解成交量？
- 原文链接: https://www.zhihu.com/question/1/answer/32
- 发布时间: 2026-01-01

股市成交量很重要，买卖的时候需要关注风险控制和交易纪律。''')
        extracted = extractive_topic_summary(rows, ['成交量'])
        self.assertTrue(extracted)
        self.assertIn(extracted[0]['quote'], rows[0]['body'])


if __name__ == '__main__':
    unittest.main()
