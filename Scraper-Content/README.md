# 知乎用户作品归档 Scraper v4（开发分支）

在原有回答归档基础上增加知乎用户「想法、文章、提问、视频」四种内容类型。旧有回答文件 answers.md 和 answers_cache.json 的命名与默认行为不变。

## 支持内容与接口

| 类型参数 | 中文名 | 列表接口 | 详情 |
|---|---|---|---|
| answers | 回答 | /api/v4/members/{token}/answers | 保留 v3.4 原逻辑 |
| pins | 想法 | /api/v4/members/{token}/pins | /api/v4/pins/{id}?include=topics |
| articles | 文章 | /api/v4/members/{token}/articles | zhuanlan.zhihu.com/api/articles/{id} |
| questions | 提问 | /api/v4/members/{token}/questions | /api/v4/questions/{id}（可能被拒绝） |
| zvideos | 视频 | /api/v4/members/{token}/zvideos | 仅列表中的视频说明与元数据，**不下载视频** |

以上接口依据公开研究资料设计，**尚未使用实际登录会话完成验收**。非公开接口可能改动或要求权限。列表缺少正文时会标记为「仅列表信息，待补全」，不冒充完整内容。

## 使用方法

Windows 可双击 run_zhihu_scraper.bat；图形界面新增五种内容多选。**默认仅勾选回答**，以保持原有行为。所有操作仍需确认浏览器登录。

命令行单行示例（从仓库根目录运行）：

    python Scraper-Content/zhihu_scraper.py --cli --url "https://www.zhihu.com/people/mdk82" --types answers,pins,articles,questions,zvideos

批量全量扫描：

    python Scraper-Content/zhihu_scraper.py --cli --update-list Scraper-Content/update_list.txt --types answers,pins,articles,questions,zvideos --full

默认图片以 Base64 嵌入 Markdown。可以加 --local-images 改成相对路径图片文件。
首次运行会创建 Playwright 浏览器目录 .pw_profile（可能包含登录 Cookie，务必保密）。

## 文件结构

仍使用每用户独立目录，例如 zhihu_mdk82(无二小二)：

- answers.md、answers_cache.json：旧版回答归档
- pins.md、pins_cache.json、pins_state.json：想法
- articles.md、articles_cache.json、articles_state.json：文章
- questions.md、questions_cache.json、questions_state.json：提问
- zvideos.md、zvideos_cache.json、zvideos_state.json：视频元数据
- images/：选择本地图片模式时使用（沿用原版相对路径规则）

每个 JSON 缓存以原始作品 ID 为键，保留原始列表记录、成功取得的详情、
内容哈希、正文 Markdown、时间和互动计数。MD 按发布时间倒序从缓存重建；
文本写入使用临时文件与原子替换，避免中途覆盖成半份文件。

增量模式扫描重叠旧内容（至少 3 页/40 个已知条目），复核最近 40 条；
全量模式扫描全部可见列表并复核旧作。旧作品列表消失时只记入
state.json 的 not_seen_in_full_scan 字段，**不自动删除旧内容**。
详情失败、未补全数据、分页是否真正结束会单独记录。已有缓存损坏时报错，
不会悄悄以空缓存重写。

## 限制与下一轮验收

- 想法/文章正文使用不同数据结构；视频只保留可见说明和原始元数据，
  暂不下载 MP4。
- 新版作者评论接口可能变更，当前仅尽力采集作者可识别的主评论/部分回复，
  不承诺所有层级的评论均完整。
- 不自动采集收藏夹、用户关注、点赞记录、私人内容、圈子或动态；
  这些不属于同一类原创作品。遵循登录权限与限流，不绕验证码。
- 需使用真实账号对纯文本、多图、引用想法、文章正文、视频/问题、置顶和
  旧内容修改逐项验收，离线测试不证明线上接口可用。

离线测试（无需知乎账号）：

    python -m pytest -q tests/test_scraper_works.py
