"""知乎回答研究台：streamlit run app.py"""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
from datetime import date
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from core import (
    apply_overrides, filter_records, lens_evidence, load_overrides, load_records,
    records_dataframe, repeated_texts, save_override, semantic_neighbors, word_frequency,
    extractive_topic_summary,
)
from taxonomy import TAXONOMY, STOCK_LENSES

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT / "workspace"
UPLOAD_DIR = WORKSPACE / "uploads"
OVERRIDE_DIR = WORKSPACE / "overrides"
ALL_CATEGORIES = list(TAXONOMY) + ["待分类"]
STOCK_IDEAS = {
    "普通人参与市场的风险": "反复质疑普通投资者能否在市场中稳定获利，提醒关注亏损风险与个人能力边界。",
    "成交量与牛熊判断": "经常将总成交量作为判断行情的重要观察量，对单纯观察指数或图形持怀疑态度。",
    "情绪影响与交易纪律": "强调交易者容易受到涨跌与板块轮动引发的情绪影响，因此主张建立可执行的交易纪律。",
    "操纵、主力与筹码": "经常从市场参与者、筹码分布和价格行为的角度解释波动；其中涉及市场操纵的具体指控尚需独立核实。",
    "买卖时机与仓位": "提出过尾盘操作、预设买卖点位及不同成交量水平对应不同仓位等个人做法。",
    "学习、独立思考与交易技术": "对现成技术指标和策略存在明显怀疑，强调以自身逻辑、反复验证和可执行的方法为基础。",
    "价值投资与长期持有": "对价值投资的表述不完全一致：既有对长期持有和投资逻辑的质疑，也提到过普通人定期购买银行股的思路。应并列阅读语境。",
}

st.set_page_config(page_title="知乎内容研究台", page_icon="📚", layout="wide", initial_sidebar_state="expanded")
st.markdown("""
<style>
  .block-container {padding-top:1.7rem; max-width:1500px}
  h1,h2,h3 {letter-spacing:-0.02em}
  div[data-testid='stMetric'] {background:rgba(126,142,169,.07);padding:14px 16px;border-radius:13px;}
  div[data-testid='stSidebar'] {border-right:1px solid rgba(120,120,120,.15)}
  a {text-decoration:none}
</style>
""", unsafe_allow_html=True)


@st.cache_data(show_spinner="解析数据并进行主题标注...")
def cached_load(file: str, mtime: int):
    return load_records(file)


@st.cache_data(show_spinner="正在进行中文分词...")
def cached_wordfreq(text_ids_and_values, include_titles, stopwords, min_len):
    from core import word_frequency
    raw = [{"title": x[1], "body": x[2]} for x in text_ids_and_values]
    return word_frequency(raw, include_titles, stopwords, min_len)


def download_table(rows, filename="answers_clean.csv"):
    # 面向 Excel 的文本导出：避免公式注入，避免中文乱码
    df = records_dataframe(rows)
    if df.empty:
        st.warning("当前筛选结果为空。")
        return
    df["tags"] = df["tags"].map(lambda x: "; ".join(x))
    df["comments"] = df["comments"].map(lambda x: json.dumps(x, ensure_ascii=False))
    for c in df.select_dtypes(include="object").columns:
        df[c] = df[c].map(lambda v: "'" + v if isinstance(v, str) and re.match(r"^[=+\-@]", v) else v)
    st.download_button("⬇ 下载清洗后的 CSV", df.to_csv(index=False).encode("utf-8-sig"),
                       file_name=filename, mime="text/csv", use_container_width=True)


def export_json(rows, filename="answers_classified.json"):
    payload = json.dumps(rows, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    st.download_button("⬇ 下载结构化 JSON", payload, file_name=filename,
                       mime="application/json", use_container_width=True)


def chart(fig):
    fig.update_layout(template="plotly_white", margin=dict(t=30, l=15, b=25, r=15),
                      font=dict(family="Microsoft YaHei, Noto Sans CJK SC, sans-serif"),
                      height=380)
    st.plotly_chart(fig, use_container_width=True)


def record_card(r, *, key_prefix="record", expanded=False, show_excerpt=True):
    ident = f"{key_prefix}_{r['id']}"
    label = f"{r['title']}  ·  {r['date'][:10]}"
    with st.expander(label, expanded=expanded):
        st.caption(f"{r['category']} / {r['subcategory']}  ·  {r['length']} 字 · ID: {r['id']} · 分类来源: {r.get('classification_method', '未知')}")
        if r.get("quote") and show_excerpt:
            st.info("匹配段落：" + r["quote"])
        st.markdown(r["body_md"] if r["body_md"] else "（正文为空）")
        # 若原脚本生成的 images/ 目录已放在程序目录，显示对应的本地图片。
        image_refs = re.findall(r"!\[[^\]]*\]\((images/[^)]+)\)", r["body_md"])
        for img_ref in image_refs[:15]:
            img_path = (ROOT / img_ref).resolve()
            if img_path.is_file() and img_path.is_relative_to(ROOT.resolve()):
                st.image(str(img_path), caption=Path(img_ref).name)
        if r.get("comments"):
            st.markdown("**作者在评论区的留言与回复**")
            for c in r["comments"]:
                if c.get("root_content"):
                    st.caption("前文：" + c["root_content"])
                st.write(c.get("text", ""))
        st.caption("标签：" + " · ".join(r["tags"]))
        if r.get("note"):
            st.caption("我的笔记：" + r["note"])
        if r["url"].startswith("https://www.zhihu.com/"):
            st.link_button("查看知乎原文 ↗", r["url"])


def paginate(rows, prefix, default_size=15):
    a, b = st.columns([1, 3])
    sizes = [10, 15, 25, 50]
    default = sizes.index(default_size) if default_size in sizes else 1
    size = a.selectbox("每页显示", sizes, index=default, key=f"{prefix}_size")
    max_page = max(1, (len(rows) + size - 1) // size)
    page = b.number_input("页码", min_value=1, max_value=max_page, step=1, key=f"{prefix}_page")
    begin = (page - 1) * size
    st.caption(f"显示第 {begin+1 if rows else 0}—{min(begin + size, len(rows))} 条，共 {len(rows)} 条，{max_page} 页")
    return rows[begin:begin + size]


def show_overview(rows, meta):
    st.subheader("数据概览")
    df = records_dataframe(rows)
    cols = st.columns(5)
    cols[0].metric("回答数", f"{len(rows):,}")
    cols[1].metric("分类大类", f"{df['category'].nunique()}")
    cols[2].metric("正文总字数", f"{df['length'].sum():,}")
    cols[3].metric("正文中位数", f"{df['length'].median():,.0f} 字")
    cols[4].metric("待人工复核标签", f"{(df['classification_method']=='模型建议·需复核').sum():,}")
    if meta["zero_vote_records"] == meta["parsed"]:
        st.warning("源数据全部回答的赞同数为 0，无法据此评估真实热度。")
    left, right = st.columns([1, 1])
    with left:
        st.markdown("**各领域回答数（主类别）**")
        counts = df["category"].value_counts().reset_index()
        counts.columns = ["类别", "回答数"]
        chart(px.bar(counts, x="回答数", y="类别", orientation="h", color="回答数",
                     color_continuous_scale="Blues").update_yaxes(categoryorder="total ascending"))
    with right:
        st.markdown("**月度回答数量**")
        monthly = df.groupby("month").size().reset_index(name="回答数").sort_values("month")
        chart(px.line(monthly, x="month", y="回答数", markers=True))
    a, b = st.columns(2)
    with a:
        st.markdown("**篇幅分布**")
        chart(px.histogram(df, x="length", nbins=55, labels={"length": "正文字符数"}))
    with b:
        st.markdown("**星期分布**")
        weekday = df["weekday"].value_counts().reindex(range(7), fill_value=0).reset_index()
        weekday.columns = ["星期", "回答数"]
        weekday["星期"] = weekday["星期"].map(dict(enumerate(list("一二三四五六日"))))
        chart(px.bar(weekday, x="星期", y="回答数", text="回答数"))
    st.markdown("**数据质量检查**")
    st.write(f"原始条目 {meta['parsed']}；去重后 {meta['deduplicated']}；重复 ID {meta['duplicate_ids']}；空正文 {meta['empty_bodies']}；作者留言 {meta['comments']}；模型建议待核实 {meta.get('machine_suggested',0)}。")
    with st.expander("查看分类规则与质量说明"):
        st.write("采用规则关键词 + 从规则标注结果训练的字符 TF-IDF 模型辅助分类。模型建议仅作为检索分组，未经过人工标注的准确率验证，尤其可能误判讽刺、隐喻与短句；请在标注页修订。")
    if meta.get("declared") is not None and meta["declared"] != meta["parsed"]:
        st.error(f"源文件声明 {meta['declared']} 条，但仅解析到 {meta['parsed']} 条，请检查文件是否完整。")
    repeats = repeated_texts(rows)
    st.write(f"正文超过 40 字的完全重复内容组：{len(repeats)} 组（同一内容可能是不同问题下重复发布，默认不删除）。")
    if repeats:
        with st.expander("查看重复内容对应回答"):
            for group in repeats[:20]:
                st.markdown(" / ".join(f"[{x['title']}]({x['url']})" for x in group))


def show_library(rows):
    st.subheader("回答资料库")
    st.caption("搜索支持多个空格分隔词，默认所有词同时命中；分类筛选在左侧。点击标题可查看回答全文和原文链接。")
    a,b,c = st.columns([2,1,1])
    query = a.text_input("全文关键词", placeholder="例如：成交量 情绪 / 庄子 / 牛市")
    location = b.selectbox("检索范围", ["全部", "问题标题", "回答正文"])
    order = c.selectbox("排序", ["时间（新到旧）", "时间（旧到新）", "篇幅（长到短）", "篇幅（短到长）"])
    only_favs = st.checkbox("只显示收藏")
    results = filter_records(rows, query=query, search_in=location, favorites=only_favs)
    keyfunc = (lambda r: r["date"]) if order.startswith("时间") else (lambda r: r["length"])
    results.sort(key=keyfunc, reverse=order.endswith("新到旧）") or order.endswith("长到短）"))
    st.metric("匹配回答", len(results))
    aa, bb = st.columns(2)
    with aa:
        download_table(results, "filtered_answers.csv")
    with bb:
        export_json(results, "filtered_answers.json")
    if results:
        for row in paginate(results, "library"):
            record_card(row, key_prefix="library")


def make_topic_markdown(category, rows, idea=None):
    lines = [f"# {category}：回答证据目录", "", f"筛选条件：{idea or '所有相关回答'}", f"数量：{len(rows)}", "",
             "> 自动筛选并非人工事实核验；观点不代表真实市场事实或投资建议。", ""]
    for i, r in enumerate(rows, 1):
        lines.extend([f"## {i}. {r['title']}", "", f"- 发布：{r['date']}",
                      f"- 来源：{r['url']}", "", r["body_md"], ""])
    return "\n".join(lines).encode("utf-8")


def show_topic(rows, original_rows):
    st.subheader("专题研究 · 观点与证据")
    st.caption("观点总结为可解释的文本分析线索，不是对作者整体立场的最终判定。可展开每条原文，也可一次导出该专题所有回答。")
    a,b = st.columns([2,1])
    category = a.selectbox("研究领域", list(TAXONOMY))
    all_corpus = b.checkbox("使用完整数据集（忽略左侧筛选）", value=True)
    base = original_rows if all_corpus else rows
    related = [r for r in base if category in r["tags"]]
    st.metric("该主题相关回答（含多标签）", len(related))
    if not related:
        st.warning("此主题没有匹配的回答。")
        return
    dd = records_dataframe(related)
    sub = dd["subcategory"].value_counts().reset_index()
    sub.columns = ["细分类", "数量"]
    col1,col2 = st.columns([1,1])
    with col1:
        chart(px.bar(sub, x="数量", y="细分类", orientation="h").update_yaxes(categoryorder="total ascending"))
    with col2:
        per_month = dd.groupby("month").size().reset_index(name="数量").sort_values("month")
        chart(px.line(per_month, x="month", y="数量", markers=True))
    st.markdown("#### 核心思想与原文证据")
    st.caption("使用当前数据集的原文句子生成抽取式摘要；仅展示作者确实写过的话，不自动推断其赞同或反对立场。摘要可按当前上传文件变化。")
    if category == "股票金融":
        for focus in STOCK_LENSES:
            evid = lens_evidence(related, focus)
            terms = STOCK_LENSES[focus]
            with st.expander(f"{focus} · 匹配 {len(evid)} 篇回答"):
                st.write(f"这份材料中与『{focus}』相关的回答有 {len(evid)} 篇。以下是从回答中抽取的代表性原话：")
                summaries = extractive_topic_summary(evid, terms, 4)
                if summaries:
                    for item in summaries:
                        st.markdown(f"- 『{item['quote']}』 — [原回答：{item['title']}]({item['url']})")
                else:
                    st.info("相关回答多为短句，请打开原文逐条核实。")
        lens = st.selectbox("选择观点线索，展示对应的全部回答", list(STOCK_LENSES))
        matched = lens_evidence(related, lens)
    else:
        st.write(f"『{category}』共有 {len(related)} 篇相关回答，下面是可追溯到原文的代表性语句：")
        extracts = extractive_topic_summary(related, limit=6)
        for item in extracts:
            st.markdown(f"- 『{item['quote']}』 — [原回答：{item['title']}]({item['url']})")
        try:
            freq = cached_wordfreq(tuple((r["id"], r["title"], r["body"]) for r in related), True, (), 2)
            st.markdown("**高频讨论词：** " + "、".join(f"{w}（{n}）" for w,n in freq.most_common(12)))
        except ImportError:
            st.warning("需要安装 jieba 才能显示分词。")
        lens = st.text_input("按关键词深入某一观点（逗号或空格分隔，留空表示全部）", value="")
        matched = lens_evidence(related, lens) if lens.strip() else list(related)
        matched.sort(key=lambda r: r.get("match_score", 0), reverse=True)
    st.caption(f"关键词召回 {len(matched)} 条；命中一个词不等于作者认同相关观点，逐条点击核对。")
    st.markdown("#### 全部匹配回答（可逐条查看）")
    a,b,c = st.columns(3)
    a.metric("找到", len(matched))
    with b:
        st.download_button("⬇ 导出完整专题 Markdown", make_topic_markdown(category, matched, lens),
                           file_name=f"专题_{category}.md", mime="text/markdown", use_container_width=True)
    with c:
        download_table(matched, f"topic_{category}.csv")
    for r in paginate(matched, "topic", 15):
        record_card(r, key_prefix="topic")


def find_chinese_font():
    opts = [
        os.getenv("ZH_FONT", ""),
        r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    for p in opts:
        if p and Path(p).is_file():
            return p
    return None


def show_words(rows):
    st.subheader("中文词频 · 词云")
    if not rows:
        st.warning("没有满足筛选条件的数据。")
        return
    a,b,c = st.columns(3)
    include_titles = a.checkbox("统计时包含问题标题", value=False)
    min_len = b.slider("最短词长", min_value=2, max_value=5, value=2)
    topn = c.slider("展示词数", 20, 150, 60, 10)
    stop = st.text_area("额外停用词（逗号、空格或换行分隔）", value="", height=65)
    extra = tuple(sorted(set(x for x in re.split(r"[,，\s]+", stop.strip()) if x)))
    try:
        freq = cached_wordfreq(tuple((r["id"],r["title"],r["body"]) for r in rows), include_titles, extra, min_len)
    except ImportError:
        st.error("缺少 jieba，请执行 pip install -r requirements.txt")
        return
    st.metric("有效词语种数", len(freq))
    if not freq:
        st.warning("筛选范围没有可统计的中文词。")
        return
    tops = freq.most_common(topn)
    freq_df = pd.DataFrame(tops, columns=["词语", "出现次数"])
    u,v = st.columns([1.1,0.9])
    with u:
        st.markdown("**词云**")
        try:
            from wordcloud import WordCloud
            font = find_chinese_font()
            if not font:
                st.warning("未发现中文字体；设置环境变量 ZH_FONT 为系统上的中文字体文件路径后即可生成词云。")
            else:
                wc = WordCloud(font_path=font, width=1150, height=630, background_color="white",
                               max_words=topn, colormap="viridis", random_state=42,
                               collocations=False).generate_from_frequencies(dict(tops))
                image_bytes = BytesIO()
                wc.to_image().save(image_bytes, format="PNG")
                st.image(image_bytes.getvalue(), use_container_width=True)
                st.download_button("⬇ 保存词云 PNG", image_bytes.getvalue(), "wordcloud.png", "image/png")
        except ImportError:
            st.warning("缺少 wordcloud 库，词频统计仍然可用。")
    with v:
        st.markdown("**高频词 TOP 25**")
        chart(px.bar(freq_df.head(25).iloc[::-1], x="出现次数", y="词语", orientation="h"))
    st.dataframe(freq_df, use_container_width=True, hide_index=True)
    st.download_button("⬇ 下载词频 CSV", freq_df.to_csv(index=False).encode("utf-8-sig"),
                       "word_frequency.csv", "text/csv")
    st.caption("词频为分词后的出现次数，不代表作者对该词的正面评价；可加入停用词进一步改善。")


def show_associations(rows):
    st.subheader("主题关联 · 时间演化 · 相似回答")
    if not rows:
        return
    st.markdown("**主题随时间的变化（以主类别计）**")
    df = records_dataframe(rows)
    bymonth = df.groupby(["month", "category"]).size().reset_index(name="回答数")
    fig = px.area(bymonth.sort_values("month"), x="month", y="回答数", color="category",
                  labels={"month":"月份"})
    chart(fig)
    st.markdown("**比较关键词的发文趋势**")
    terms = st.text_input("比较词（逗号隔开）", value="成交量,牛市,股票,庄子")
    termlist = [x.strip() for x in re.split(r"[,，、]", terms) if x.strip()][:8]
    if termlist:
        records = []
        for month, group in df.groupby("month"):
            combined = (group["title"] + " " + group["body"]).tolist()
            for term in termlist:
                records.append({"月份":month, "关键词":term, "匹配回答数":sum(term.lower() in t.lower() for t in combined)})
        chart(px.line(pd.DataFrame(records).sort_values("月份"), x="月份", y="匹配回答数", color="关键词", markers=True))
    st.markdown("**相似回答发现（TF-IDF 字符 n-gram）**")
    opts = {f"{r['title'][:70]} · {r['id']}": r["id"] for r in rows}
    chosen = st.selectbox("选择要查找相似回答的文章", list(opts))
    if st.button("查找相关内容", type="primary"):
        with st.spinner("正在计算语义文本相似度（基于字词重叠，而非大型语言模型）..."):
            similar = semantic_neighbors(rows, opts[chosen], 12)
        if not similar:
            st.info("没有找到相似回答。")
        for r,score in similar:
            st.markdown(f"**相似度 {score:.3f}** · [{r['title']}]({r['url']}) · {r['date'][:10]}")
            st.caption(r["body"][:240])
    st.caption("相似度用于帮助发现重复论点和跨领域联系，不应视为观点完全一致。")


def show_labels(rows, filtered, override_path):
    st.subheader("人工分类、收藏与研究笔记")
    st.caption("自动分类可能误判，尤其是短句、反讽、隐喻。模型辅助类别均为待核实建议。人工修改保存在 workspace/overrides.json；不改动原始 answers.md。")
    a,b = st.columns(2)
    with a:
        pool = filtered or rows
        if st.checkbox("仅看模型建议（待人工复核）"):
            suggested = [r for r in pool if r.get("classification_method") == "模型建议·需复核"]
            if suggested:
                pool = suggested
        opts = {f"{x['title'][:72]}  [{x['id']}]": x["id"] for x in pool}
        option = st.selectbox("选择一篇回答", list(opts), key="annotation_select")
        chosen = next(r for r in pool if r["id"] == opts[option])
        st.markdown("**原文**")
        st.markdown(chosen["body_md"])
        st.markdown(f"[知乎原文]({chosen['url']})")
    with b:
        cats = ALL_CATEGORIES
        category = st.selectbox("主类别", cats,
                                index=cats.index(chosen["category"]) if chosen["category"] in cats else len(cats)-1,
                                key=f"category_{chosen['id']}")
        subs = ["待分类"] + list(TAXONOMY.get(category, {}))
        current_sub = chosen["subcategory"] if chosen["subcategory"] in subs else "待分类"
        sub = st.selectbox("细分类", subs, index=subs.index(current_sub), key=f"sub_{chosen['id']}")
        tags = st.multiselect("多标签", cats, default=[k for k in chosen["tags"] if k in cats],
                              key=f"tags_{chosen['id']}")
        favorite = st.checkbox("标记为收藏", value=chosen.get("favorite",False), key=f"fav_{chosen['id']}")
        note = st.text_area("研究笔记", value=chosen.get("note",""), height=130,
                            key=f"note_{chosen['id']}")
        if st.button("💾 保存人工修订", type="primary"):
            save_override(override_path, chosen["id"], {"category": category, "subcategory": sub,
                                                       "tags": sorted(set(tags + [category])),
                                                       "note": note, "favorite": favorite})
            st.success("保存成功。分类与笔记会在重新运行程序后保留。")
            st.rerun()
    st.divider()
    st.markdown("**批量导出当前筛选的内容**")
    d1,d2 = st.columns(2)
    with d1:
        download_table(filtered, "zhihu_curated.csv")
    with d2:
        export_json(filtered, "zhihu_curated.json")
    if override_path.exists():
        st.download_button("⬇ 备份人工标注文件", override_path.read_bytes(), "overrides.json", "application/json")
    st.caption("各数据集的人工标注相互隔离。相同文件内容重新上传仍会沿用已有标注；源内容改变会使用新的标注文件。")


def available_sources():
    """扫描随包示例文件及历史上传文件。全部上传均在本地保存，无远端 API。"""
    candidates = [ROOT / "answers.md", ROOT / "answers_cache.json"]
    if UPLOAD_DIR.exists():
        candidates += sorted((p for p in UPLOAD_DIR.iterdir() if p.suffix.lower() in {".md", ".json"}),
                             key=lambda p: p.stat().st_mtime_ns, reverse=True)
    return [p for p in candidates if p.is_file()]


def safe_uploaded_filename(name):
    # Streamlit UploadedFile.name 由客户端提供，只采用 basename 字符；避免路径穿越。
    cleaned = re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]", "_", Path(str(name).replace("\\", "/")).name)
    return (cleaned[:75] or "answers.md")


def import_uploads(uploaded):
    accepted = []
    for item in uploaded or []:
        ext = Path(item.name).suffix.lower()
        if ext not in {".md", ".json"}:
            continue
        payload = item.getvalue()
        if not payload or len(payload) > 40 * 1024 * 1024:
            st.warning(f"跳过 {item.name}：文件为空或大于 40 MB")
            continue
        digest = sha256(payload).hexdigest()[:16]
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        path = UPLOAD_DIR / f"{digest}__{safe_uploaded_filename(item.name)}"
        if not path.exists():
            path.write_bytes(payload)
        accepted.append(path)
    return accepted


def show_data_management(src, rows, meta, digest):
    st.subheader("数据文件管理")
    st.success(f"正在分析：{src.name} · {len(rows):,} 条回答")
    st.write("你可以在左侧上传一份或多份由爬虫导出的 Markdown 文件，选择任意一个进行分析；全部图表、搜索、观点摘要和人工标注均依据当前选中文件计算。")
    st.code(f"数据 SHA-256: {digest}\n本地原始文件: {src}", language="text")
    st.json({"源格式": meta.get("format"), "文件声明条数": meta.get("declared"),
             "实际解析条数": meta["parsed"], "去重后": meta["deduplicated"],
             "重复ID": meta["duplicate_ids"], "空正文": meta["empty_bodies"],
             "评论条数": meta["comments"], "分类待复核": meta["machine_suggested"]})
    st.markdown("**格式示例：**")
    st.code("""# 知乎用户「某某」的全部回答
- 回答总数: 1

## 股票市场该怎么看？
- 原文链接: https://www.zhihu.com/question/123/answer/456
- 发布时间: 2026-10-13 12:00
- 赞同数: 4

这里是回答正文，也可以包含多段文字、列表、图片 Markdown 链接。""", language="markdown")
    st.info("不会自动上传到 GitHub 或云端；被上传的文件保存在本机 workspace/uploads/，" 
            "标注保存在 workspace/overrides/。复制这两个文件夹可备份你的工作。")


def main():
    st.title("ZHVision · 知乎回答研究台")
    st.caption("内容清洗 · 分类浏览 · 关键词与词云 · 专题思想证据库 · 手工修订")
    with st.sidebar:
        st.header("📁 数据管理与上传")
        uploaded = st.file_uploader("上传知乎导出的 MD/JSON，可一次选择多个文件",
                                    type=["md", "json"], accept_multiple_files=True,
                                    help="支持原始脚本导出的 answers.md、类似字段顺序的 Markdown、answers_cache.json")
        just_uploaded = import_uploads(uploaded)
        sources = available_sources()
        if not sources:
            st.warning("请先上传 Markdown 文件。"); st.stop()
        labels = {str(p): p.name.split("__", 1)[-1] if "__" in p.name else p.name for p in sources}
        # 新文件上传时优先选择新文件，但用户之后可以自由切换历史上传文件。
        default_idx = sources.index(just_uploaded[-1]) if just_uploaded and just_uploaded[-1] in sources else 0
        src = st.selectbox("当前分析的文件", sources, index=default_idx,
                           format_func=lambda p: labels[str(p)])
        data_hash = sha256(src.read_bytes()).hexdigest()
        override_path = OVERRIDE_DIR / f"{data_hash[:24]}.json"
        try:
            base_rows, meta = cached_load(str(src), src.stat().st_mtime_ns)
        except Exception as exc:
            st.error(f"文件格式不能识别：{exc}")
            st.caption("需要 ## 问题标题及标题下的原文链接或发布时间；可检查上传页面中的格式示例。")
            st.stop()
        original = apply_overrides(base_rows, load_overrides(override_path))
        st.caption(f"当前文件：{src.name} · {len(original):,} 条")
        st.divider()
        page = st.radio("模块", ["📁 上传与管理", "📊 全局概览", "📖 回答资料库", "🧠 专题观点",
                                 "☁️ 词频词云", "🔗 关联分析", "✍️ 标注与导出"], label_visibility="collapsed")
        st.divider()
        st.markdown("**全局筛选**")
        cats = st.multiselect("所属类别（支持多标签）", ALL_CATEGORIES, default=[], key=f"cats_{data_hash[:10]}")
        frame = records_dataframe(original)
        valid_date = frame["datetime"].dropna()
        default_start = valid_date.min().date() if not valid_date.empty else date(2000,1,1)
        default_end = valid_date.max().date() if not valid_date.empty else date.today()
        picked_dates = st.date_input("发布时间范围", value=(default_start, default_end),
                                     min_value=default_start, max_value=max(default_end,date.today()),
                                     key=f"dates_{data_hash[:10]}")
        start = picked_dates[0] if picked_dates else default_start
        end = picked_dates[1] if len(picked_dates) > 1 else default_end
        if st.button("重新解析 / 清理缓存"):
            st.cache_data.clear()
            st.rerun()
    filtered = filter_records(original, cats=cats, start=start, end=end)
    st.caption(f"当前范围：**{len(filtered):,}** / {len(original):,} 条回答")
    if page.startswith("📁"):
        show_data_management(src, original, meta, data_hash)
    elif page.startswith("📊"):
        show_overview(filtered, meta)
    elif page.startswith("📖"):
        show_library(filtered)
    elif page.startswith("🧠"):
        show_topic(filtered, original)
    elif page.startswith("☁️"):
        show_words(filtered)
    elif page.startswith("🔗"):
        show_associations(filtered)
    elif page.startswith("✍️"):
        show_labels(original, filtered, override_path)
    st.divider()
    st.caption("所有统计只反映这份离线抓取材料；自动归纳不应被视为真实性核验，涉及投资的言论不是投资建议。")


if __name__ == "__main__":
    main()
