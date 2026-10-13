"""数据处理与可解释主题分析。原文永不覆盖，清洗仅用于搜索、统计。"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from html import unescape
import hashlib
import json
import re
from pathlib import Path

import pandas as pd
from taxonomy import TAXONOMY, STOCK_LENSES, STOPWORDS

# 根据实际的原始导出格式匹配。仅以含有连续元数据的二级标题作为回答边界。
_HEADER = re.compile(
    r"(?m)^##[ \t]+(?P<title>[^\r\n]+)\r?\n[ \t]*\r?\n"
    r"- 原文链接:[ \t]*(?P<url>https?://\S+)\r?\n"
    r"- 发布时间:[ \t]*(?P<date>[^\r\n]*)\r?\n"
    r"- 赞同数:[ \t]*(?P<votes>\d+)\r?\n"
)
_SELF_COMMENTS = re.compile(r"(?m)^###\s*💬\s*作者在评论区的留言\s*/\s*回复.*$")
_IMG_LINK = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_TAG = re.compile(r"<[^>]+>")
_URL = re.compile(r"https?://\S+")
_SPACE = re.compile(r"[ \t\u00a0]+")
_ZW = re.compile(r"[\u200b\u200c\u200d\ufeff]")
_FINANCE_BIG = "股票金融"


def cleaning(text: str) -> str:
    """转纯文本，保留自然语言内容和段落；不删原始答案。"""
    t = unescape(str(text or ""))
    t = _ZW.sub("", t)
    t = re.sub(r"(?s)```.*?```", " ", t)
    t = _IMG_LINK.sub(" ", t)
    t = _MD_LINK.sub(r"\1", t)
    t = _URL.sub(" ", t)
    t = _TAG.sub(" ", t)
    t = re.sub(r"(?m)^\s{0,3}(?:>\s*|#{1,6}\s*|[-*+]\s+|\d+\.\s+)", "", t)
    t = re.sub(r"(?m)^\s*(?:---+|===+)\s*$", "", t)
    t = re.sub(r"[`*_~]", "", t)
    t = _SPACE.sub(" ", t)
    t = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", t)
    return t.strip()


def _split_body_comments(segment: str):
    m = _SELF_COMMENTS.search(segment)
    if not m:
        return segment.strip(), []
    body = segment[:m.start()].strip()
    tail = segment[m.end():].strip()
    tail = re.sub(r"(?m)^---\s*$", "", tail)
    comments = []
    for kind, val in re.findall(r"(?m)^- \*\*作者本人评论\*\*:\s*(.*)$|^\*\*作者回复\*\*:\s*(.*)$", tail):
        if kind:
            comments.append({"kind": "self_root", "text": cleaning(kind)})
        elif val:
            comments.append({"kind": "self_reply", "text": cleaning(val)})
    return body, comments


def _make_record(title, url, date, votes, body_md, comments=None):
    answer_id = re.search(r"/answer/(\d+)", str(url))
    key = answer_id.group(1) if answer_id else hashlib.sha1((str(url) + str(title) + str(date) + str(body_md)).encode()).hexdigest()[:16]
    pure = cleaning(body_md)
    return {
        "id": str(key), "title": cleaning(title), "url": str(url),
        "date": str(date or "").strip(), "votes": int(votes or 0),
        "body_md": str(body_md or ""), "body": pure,
        "comments": comments or [], "length": len(pure),
        "content_hash": hashlib.sha1(re.sub(r"\s+", "", pure).encode()).hexdigest() if pure else "",
    }


def parse_markdown_text(raw: str, source: str = "uploaded.md"):
    """解析该爬虫生成的 Markdown，也容忍字段缺失、次序变化、CRLF 和正文子标题。

    只有 ## 标题之后紧跟「原文链接 / 发布时间 / 赞同数」等导出字段的块才算回答，
    不会把回答正文中的 ## 小标题误拆成另一篇。原文和清洗文本独立保留。
    """
    raw = str(raw).lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    declared = re.search(r"(?m)^-\s*回答总数\s*[:：]\s*(\d+)", raw)
    marker = re.compile(r"(?m)^##[ \t]+([^\n]+)\n")
    fields = re.compile(r"^\s*[-*]\s*(原文链接|回答链接|发布时间|发布日期|赞同数|点赞数)\s*[:：]\s*(.*?)\s*$")
    boundaries = []
    for hit in marker.finditer(raw):
        tail = raw[hit.end():].split("\n", 10)[:10]
        found = {}
        end = hit.end()
        saw_meta = False
        for line in tail:
            match = fields.match(line)
            if match:
                found[match.group(1)] = match.group(2).strip()
                saw_meta = True
                end += len(line) + 1
            elif not line.strip():
                end += len(line) + 1
            else:
                break
        # 要有特征字段才认定为回答。避免把回答正文里的二级标题误判为新回答。
        if saw_meta and (any(k in found for k in ("原文链接", "回答链接", "发布时间", "发布日期"))):
            boundaries.append((hit.start(), end, hit.group(1).strip(), found))
    rows = []
    for i, (start, end, title, found) in enumerate(boundaries):
        next_start = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(raw)
        segment = raw[end:next_start].strip()
        if i + 1 == len(boundaries):
            segment = re.sub(r"\n?\s*---\s*$", "", segment)
        body, comments = _split_body_comments(segment)
        url = found.get("原文链接", found.get("回答链接", ""))
        dt = found.get("发布时间", found.get("发布日期", ""))
        vote = found.get("赞同数", found.get("点赞数", "0"))
        vote_match = re.search(r"\d+", vote)
        rows.append(_make_record(title, url, dt, vote_match.group(0) if vote_match else 0, body, comments))
    return rows, {"format": "markdown", "declared": int(declared.group(1)) if declared else None,
                  "parsed": len(rows), "file": source,
                  "unmatched_heading_count": max(0, len(list(marker.finditer(raw))) - len(boundaries))}


def parse_markdown(path: str | Path):
    p = Path(path)
    return parse_markdown_text(p.read_text(encoding="utf-8-sig"), str(p))


def parse_cache_text(raw: str, source: str = "uploaded.json"):
    data = json.loads(str(raw).lstrip("\ufeff"))
    if isinstance(data, dict):
        data = list(data.values())
    if not isinstance(data, list):
        raise ValueError("缓存 JSON 应为按回答 ID 索引的对象或记录数组。")
    rows = []
    for obj in data:
        if not isinstance(obj, dict):
            continue
        comments = []
        for c in (obj.get("author_comments") or []):
            if not isinstance(c, dict):
                continue
            if c.get("kind") == "self_root":
                comments.append({"kind": "self_root", "text": cleaning(c.get("content", ""))})
            else:
                comments.append({"kind": "self_reply", "root_author": c.get("root_author", ""),
                                 "root_content": cleaning(c.get("root_content", "")),
                                 "text": cleaning(c.get("reply_content", ""))})
        rows.append(_make_record(obj.get("question_title", "无标题"),
                                 obj.get("answer_url", ""), obj.get("created_time", ""),
                                 obj.get("voteup", 0), obj.get("body_md", ""), comments))
    return rows, {"format": "json", "declared": len(data), "parsed": len(rows), "file": source}


def parse_cache(path: str | Path):
    p = Path(path)
    return parse_cache_text(p.read_text(encoding="utf-8-sig"), str(p))

def _weight(w: str) -> float:
    # 过于泛化的词降低影响，以减少把股票、生活哲学都归到社会问题的误判
    weak = {"人生", "生活", "国家", "制度", "工作", "文化", "社会", "努力", "时间", "选择",
            "科学", "科技", "思考", "逻辑", "信仰", "投资", "自由", "孩子", "家庭", "学习",
            "权利", "权力", "银行", "指数", "股东", "资本", "利益", "交易", "生死", "电影"}
    return 0.52 if w in weak else 1.0


def _kw_contains(haystack: str, needle: str) -> bool:
    # ASCII 英文避免误命中其他英文单词，例如 AI in 'said'
    if needle.isascii() and needle.isalpha() and len(needle) <= 3:
        return bool(re.search(r"(?<![a-z])" + re.escape(needle.lower()) + r"(?![a-z])", haystack))
    return needle.lower() in haystack


def classify(title: str, body: str):
    title, body = title.lower(), body.lower()
    theme_scores = {}
    subscores = {}
    for category, subs in TAXONOMY.items():
        counts = {}
        for subtopic, keys in subs.items():
            title_hits = 0.0
            body_hits = 0.0
            for key in set(keys):
                w = _weight(key)
                if _kw_contains(title, key):
                    title_hits += w
                elif _kw_contains(body, key):
                    body_hits += w
            # 标题为分类主证据；正文最多贡献 3 个关键词，避免长文与弱相关领域泛匹配。
            sc = title_hits * 3.8 + min(body_hits, 3.0) * 0.9
            if title_hits or body_hits:
                counts[subtopic] = round(sc, 2)
        if counts:
            ordered = sorted(counts.values(), reverse=True)
            theme_scores[category] = ordered[0] + 0.18 * sum(ordered[1:])
            subscores[category] = counts
    qualified = sorted([k for k, v in theme_scores.items() if v >= 2.5],
                       key=lambda c: theme_scores[c], reverse=True)
    if not qualified:
        return "待分类", "待分类", ["待分类"], 0.0
    primary = qualified[0]
    primary_sub = max(subscores[primary], key=subscores[primary].get)
    tags = [t for t in qualified if theme_scores[t] >= max(2.5, theme_scores[primary] * 0.3)][:4]
    if primary not in tags:
        tags.insert(0, primary)
    # 规则分值不是统计概率
    return primary, primary_sub, tags, round(float(theme_scores[primary]), 2)


def _assist_unclassified(rows):
    """用已被强规则覆盖的记录弱监督训练，给剩余回答提供可编辑建议。

    这不是人工金标准；低概率不代表严格统计置信度。
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        return
    labeled = [i for i, r in enumerate(rows) if r["category"] != "待分类"]
    unlabeled = [i for i, r in enumerate(rows) if r["category"] == "待分类"]
    if len(labeled) < 30 or not unlabeled:
        return
    texts = [(r["title"] + "。") * 4 + r["body"][:1200] for r in rows]
    model = TfidfVectorizer(analyzer="char", ngram_range=(2, 3), max_features=45000,
                            min_df=2, sublinear_tf=True)
    try:
        features = model.fit_transform(texts)
        clf = LogisticRegression(max_iter=250, class_weight="balanced")
        clf.fit(features[labeled], [rows[i]["category"] for i in labeled])
        prediction = clf.predict_proba(features[unlabeled])
    except ValueError:
        return
    for idx, probs in zip(unlabeled, prediction):
        row = rows[idx]
        pred_ix = int(probs.argmax())
        row["category"] = str(clf.classes_[pred_ix])
        row["subcategory"] = "待细分"
        row["tags"] = [row["category"]]
        row["classification_method"] = "模型建议·需复核"
        row["category_score"] = round(float(probs[pred_ix]), 3)


def load_records(path: str | Path):
    p = Path(path)
    if p.suffix.lower() == ".json":
        rows, meta = parse_cache(p)
    else:
        rows, meta = parse_markdown(p)
    if not rows:
        raise ValueError("没有识别到回答条目。需要由爬虫导出的 ## 问题标题和 - 原文链接/- 发布时间 元数据；请检查 MD 格式。")
    dedup = {}
    collisions = 0
    for row in rows:
        if row["id"] in dedup:
            collisions += 1
            if len(row["body"]) <= len(dedup[row["id"]]["body"]):
                continue
        dedup[row["id"]] = row
    rows = list(dedup.values())
    for row in rows:
        row["category"], row["subcategory"], row["tags"], row["category_score"] = classify(row["title"], row["body"])
        row["classification_method"] = "规则关键词" if row["category"] != "待分类" else "待分类"
    _assist_unclassified(rows)
    meta.update({"deduplicated": len(rows), "duplicate_ids": collisions,
                 "empty_bodies": sum(not r["body"] for r in rows),
                 "zero_vote_records": sum(r["votes"] == 0 for r in rows),
                 "comments": sum(len(r["comments"]) for r in rows),
                 "machine_suggested": sum(r["classification_method"] == "模型建议·需复核" for r in rows)})
    return rows, meta


def load_overrides(path: str | Path):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_override(path: str | Path, answer_id: str, values: dict):
    p = Path(path)
    old = load_overrides(p)
    old[str(answer_id)] = {"category": values.get("category", "待分类"),
                           "subcategory": values.get("subcategory", "待分类"),
                           "tags": list(values.get("tags", [])),
                           "note": values.get("note", ""),
                           "favorite": bool(values.get("favorite", False))}
    p.parent.mkdir(parents=True, exist_ok=True)
    staging = p.with_suffix(p.suffix + ".tmp")
    staging.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
    staging.replace(p)


def apply_overrides(rows, overrides):
    clean = []
    for row in rows:
        x = dict(row)
        o = overrides.get(row["id"], {})
        if o:
            x["category"] = o.get("category") or x["category"]
            x["subcategory"] = o.get("subcategory") or x["subcategory"]
            x["tags"] = o.get("tags") or [x["category"]]
        x["note"] = o.get("note", "")
        x["favorite"] = bool(o.get("favorite", False))
        x["manually_labeled"] = bool(o)
        if o:
            x["classification_method"] = "人工修订"
        clean.append(x)
    return clean


def records_dataframe(rows):
    cols = ["id", "title", "date", "votes", "length", "category", "subcategory", "tags", "classification_method", "category_score", "favorite", "note", "url", "body", "comments"]
    frame = pd.DataFrame([{k: r.get(k) for k in cols} for r in rows], columns=cols)
    if frame.empty:
        return frame
    frame["datetime"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["month"] = frame["datetime"].dt.strftime("%Y-%m")
    frame["weekday"] = frame["datetime"].dt.dayofweek
    frame["hour"] = frame["datetime"].dt.hour
    frame["comment_count"] = frame["comments"].map(len)
    return frame


def filter_records(rows, *, cats=None, query="", search_in="全部", start=None, end=None,
                   favorites=False, subcategory=None, include_tags=True):
    cats = set(cats or [])
    tokens = [t.lower() for t in query.strip().split() if t]
    result = []
    for r in rows:
        if cats and not ((set(r["tags"]) & cats) if include_tags else (r["category"] in cats)):
            continue
        if subcategory and r["subcategory"] != subcategory:
            continue
        if favorites and not r.get("favorite"):
            continue
        d = r["date"][:10]
        if start and d and d < str(start):
            continue
        if end and d and d > str(end):
            continue
        text = (r["title"] if search_in == "问题标题" else r["body"] if search_in == "回答正文" else r["title"] + "\n" + r["body"] + "\n" + " ".join(c.get("text", "") for c in r["comments"])) .lower()
        if tokens and not all(t in text for t in tokens):
            continue
        result.append(r)
    return result


def extract_quotes(row, terms, count=2):
    text = row["body"].replace("\n", "。")
    candidates = [s.strip() for s in re.split(r"(?<=[。！？!?；;])", text) if len(s.strip()) >= 8]
    if not candidates:
        candidates = [text.strip()] if text.strip() else []
    scored = []
    for i, s in enumerate(candidates):
        hits = sum(1 for w in terms if _kw_contains(s.lower(), w.lower()))
        if hits:
            scored.append((hits * 4 + min(len(s), 75) / 75 - (i * 0.0001), s))
    scored.sort(reverse=True)
    result = [t[:220] + ("…" if len(t) > 220 else "") for _, t in scored[:count]]
    if not result and candidates:
        result = [candidates[0][:180] + ("…" if len(candidates[0]) > 180 else "")]
    return result


def lens_evidence(rows, lens, limit=None):
    terms = STOCK_LENSES.get(lens, [w.strip() for w in re.split(r"[,，、\s]+", lens) if w.strip()])
    found = []
    for r in rows:
        if not any(_kw_contains((r["title"] + " " + r["body"]).lower(), w.lower()) for w in terms):
            continue
        title_hits = sum(_kw_contains(r["title"].lower(), w.lower()) for w in terms)
        body_hits = sum(_kw_contains(r["body"].lower(), w.lower()) for w in terms)
        quotes = extract_quotes(r, terms)
        score = title_hits * 4 + min(body_hits, 5) + min(r["length"], 900) / 900
        found.append({**r, "quote": quotes[0] if quotes else "", "match_score": score})
    found.sort(key=lambda r: (-r["match_score"], r["date"]), reverse=False)
    return found[:limit] if limit else found


def jieba_words(rows, include_titles=False, extra_stopwords=None, min_len=2):
    import jieba
    text = "\n".join((r["title"] + "\n" if include_titles else "") + r["body"] for r in rows)
    stop = STOPWORDS | set(extra_stopwords or [])
    for w in jieba.cut(text):
        w = w.strip().lower()
        if len(w) < min_len or w in stop or w.isnumeric():
            continue
        if not re.search(r"[\u4e00-\u9fff]", w):
            continue
        yield w


def word_frequency(rows, include_titles=False, extra_stopwords=None, min_len=2):
    return Counter(jieba_words(rows, include_titles, extra_stopwords, min_len))


def repeated_texts(rows, min_chars=40):
    groups = defaultdict(list)
    for r in rows:
        if r["length"] >= min_chars and r["content_hash"]:
            groups[r["content_hash"]].append(r)
    return [group for group in groups.values() if len(group) > 1]


def semantic_neighbors(rows, answer_id: str, k=10):
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import linear_kernel
    ids = [r["id"] for r in rows]
    if answer_id not in ids or len(rows) < 2:
        return []
    corpus = [r["title"] * 2 + " " + r["body"][:1500] for r in rows]
    vect = TfidfVectorizer(analyzer="char", ngram_range=(2, 3), max_features=50000,
                           min_df=2, sublinear_tf=True)
    try:
        mat = vect.fit_transform(corpus)
    except ValueError:
        return []
    target = ids.index(answer_id)
    scores = linear_kernel(mat[target], mat).ravel()
    order = scores.argsort()[::-1]
    return [(rows[int(i)], round(float(scores[i]), 3)) for i in order if int(i) != target][:k]


def extractive_topic_summary(rows, terms=None, limit=5):
    """无外部 API 的证据式观点摘要：以原文完整句子呈现，不编造作者观点。"""
    terms = [x.lower() for x in (terms or []) if x]
    gathered = []
    seen = set()
    for r in rows:
        segments = re.split(r"[\n。！？!?；;]+", r.get("body", ""))
        for position, segment in enumerate(segments):
            sentence = re.sub(r"\s+", " ", segment).strip(" ，,：:")
            if not (16 <= len(sentence) <= 190):
                continue
            if sentence in seen:
                continue
            seen.add(sentence)
            hits = sum(x in sentence.lower() for x in terms)
            gathered.append((hits * 5 + min(100, len(sentence)) / 100 + min(200, r.get("length", 0))/1000 - position / 200,
                             sentence, r["title"], r["url"], r["id"]))
    gathered.sort(reverse=True)
    output = []
    for score, sentence, title, url, aid in gathered:
        if terms and score < 5:
            continue
        if any(sentence in x["quote"] or x["quote"] in sentence for x in output):
            continue
        output.append({"quote": sentence, "title": title, "url": url, "id": aid})
        if len(output) >= limit:
            break
    return output
