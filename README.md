# ZHVision · 知乎回答文本研究与可视化

**使用 Python + Streamlit 在本地浏览器分析任意同格式的知乎回答 Markdown 文件。** 不需要修改代码或重新爬取。首次可使用随仓库附带的 `answers.md`（3906 条），也可以在网页左侧上传自己的 `answers.md` 或 `answers_cache.json`。上传后所有模块自动切换到所选数据集。

## 快速启动（Windows）

1. 在 GitHub 页面点击 **Code → Download ZIP**，解压仓库。安装 Python 3.10+（建议 3.11）。
2. 双击 `start_windows.bat`；首次会自动创建 `.venv` 并安装依赖。
3. 在浏览器打开 **http://localhost:8501**（通常会自动弹出）。
4. 在网页左侧 **「数据管理与上传」→「上传知乎导出的 MD/JSON」** 选择一个或多个本地文件；在 **「当前分析的文件」** 选择目标文件，再切换到任何功能页。

终端手动启动：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py
```

macOS / Linux：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app.py
```

如果镜像安装失败，可尝试 `python -m pip install -r requirements.txt -i https://pypi.org/simple`。`streamlit` 的上传文件大小默认上限为 200MB，但本项目另外限制为 **40MB/文件**（可在 `app.py` 中自行修改）。

## 上传文件支持的格式

直接支持 `zhihu_scraper.py` 导出的 `answers.md` 和 `answers_cache.json`，以及**采用同类标题及元数据结构**的 `.md` 文件：

```markdown
# 知乎用户「示例」的全部回答
- 回答总数: 2

## 如何学习股票交易？
- 原文链接: https://www.zhihu.com/question/123/answer/456
- 发布时间: 2026-10-10 15:00
- 赞同数: 1

这里是第一条回答内容，支持任意多段正文、列表、图片 Markdown 和子标题。

## 生活有什么意义？
- 发布时间: 2026-10-11 09:20
- 原文链接: https://www.zhihu.com/question/789/answer/101

这里是第二条回答内容，缺少赞同数也能正常解析（记为 0）。
```

- 兼容 Windows 换行符、UTF-8 BOM、元数据字段顺序变化、缺少赞同数，以及正文里的 `##` 小标题（不会误认为另一篇回答）。
- 每篇回答须有 `## 问题标题` 且紧跟 `- 原文链接` 或 `- 发布时间` 等导出元数据。对任意不带这些字段的自由格式 Markdown **不保证识别**，会明确报错，不会假装分析成功。
- 只针对**作者自己的回答及作者留言**做分析；不是爬取所有用户评论的通用解析器。
- 多文件上传后会保存在本地 `workspace/uploads/`，可以在侧边栏随时选择此前上传的文件。**不会自动发送到 GitHub 或任何云 API。**
- 不同数据集以 **SHA-256 文件指纹**隔离人工分类/收藏/笔记，存放 `workspace/overrides/<指纹>.json`；再次上传完全相同文件能继续沿用标注，原始文件永不修改。

## 功能页面

| 页面 | 功能 |
|---|---|
| 📁 上传与管理 | 上传一个或多个 MD/JSON、切换当前数据集、查看解析数量与数据质量、格式示例 |
| 📊 全局概览 | 回答总数、类别分布、时间趋势、回答长度/星期分布、重复正文和数据质量 |
| 📖 回答资料库 | 标题或正文关键词检索、筛选类别、排序、分页、全文/作者回复/原文链接查看、CSV/JSON 导出 |
| 🧠 专题观点 | 股票、历史、哲学等主题的多标签检索；股票的成交量、交易纪律、风险等细分线索；**从当前文件原文抽取观点句，并列出/导出所有匹配回答** |
| ☁️ 词频词云 | Jieba 中文分词、添加停用词、词频柱状图、中文词云、PNG/CSV 导出 |
| 🔗 关联分析 | 主题随时间变化、关键词趋势对比、TF-IDF 相似回答搜索 |
| ✍️ 标注与导出 | 修正主/子分类与多标签、收藏、研究笔记、导出当前筛选结果、备份标注 |

**观点归纳说明：** 当前不依赖收费模型 API。以关键词检索和**原文句子抽取**形成可追溯的专题摘要，确保引文真实；不能把关键词命中当作认同某观点，也不保证语义层面的完整性。人工应检查原回答，尤其是讽刺和隐喻表达。股票信息不是投资建议。

## 项目结构

```text
ZHVision/
├── app.py                  # Streamlit 页面，文件上传/切换入口
├── core.py                 # Markdown/JSON 解析、清洗、分类、证据检索
├── taxonomy.py             # 多级中文主题词典及停用词
├── requirements.txt        # 运行依赖
├── start_windows.bat       # Windows 一键启动
├── start_linux.sh          # macOS/Linux 启动
├── zhihu_scraper.py        # 原始爬取脚本（与分析系统独立）
├── answers.md              # 附带的一份 3906 条回答样本
├── tests/test_core.py      # 核心功能及真实数据测试
├── tests/test_uploads.py   # 多数据集、Markdown 变体测试
├── build_offline.py        # 旧版离线数据快照生成器（非动态应用）
├── generate_stock_report.py
├── offline_dashboard.html  # **仅查看附带样例的历史离线快照**
├── 股票金融专题_观点与全部回答.md # 样例数据的历史专题报告
└── workspace/              # 本机运行时创建（Git 忽略）
    ├── uploads/
    └── overrides/
```

注意：`offline_dashboard.html` 是**历史样本的静态离线快照**，不是新版动态上传页面。请运行 `app.py` 打开 `http://localhost:8501` 以使用上传文件的全部功能。

## 验证和维护

```bash
python -m pytest -q
python -m compileall -q app.py core.py taxonomy.py
```

已针对原始 `answers.md` 验证 3906 条回答解析、分类与基本检索，也包含对不同用户/不同格式的小样本测试。`answers.md` 赞同数字段全部是零；不要根据该样本的赞同数生成热度排行。若上传的其它文件某些字段缺失，系统仅展示已知信息；自动分类是启发式规则 + 弱监督建议，必须人工复核重要观点。

数据隐私：示例 Markdown 来自公开平台用户的内容。若在公共仓库维护自己的内容，请确认分享权限；`workspace/` 默认不纳入 Git。图片需要原抓取脚本的本地 `images/` 目录，与 Markdown 中相对路径一致才会显示；没有图片不影响文本分析。
