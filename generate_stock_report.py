"""生成股票金融专题研究目录，不联网，不杜撰文献观点。"""
from pathlib import Path
from core import load_records, lens_evidence
from taxonomy import STOCK_LENSES

root=Path(__file__).parent
records, meta=load_records(root/'answers.md')
related=[r for r in records if '股票金融' in r['tags']]
ideas={
    '普通人参与市场的风险':'多次提醒一般投资者评估亏损风险和自身能力边界。',
    '成交量与牛熊判断':'经常强调总成交量、流动性等观察量，对仅用指数和技术图形判断行情持怀疑态度。',
    '情绪影响与交易纪律':'认为情绪波动会促使交易者追涨杀跌，主张尽量减少临场冲动。',
    '操纵、主力与筹码':'常从机构力量、筹码变动和价格行为的角度解释行情；具体操纵指控未经独立核实。',
    '买卖时机与仓位':'提出过尾盘交易、提前设置交易点位、按照市场成交水平调整仓位等方法。',
    '学习、独立思考与交易技术':'质疑照搬技术指标，重视自身逻辑、反复验证及执行。',
    '价值投资与长期持有':'对部分价值投资论述持怀疑态度，但也提到长期持有银行股的想法，需放在各篇回答上下文中理解。',
}
parts=['# 股票金融专题 · 全部回答及观点证据目录','',
       f'> 来源：知乎用户无二小二，导出文件 answers.md。规则分类和辅助模型共识别 {len(related)} 条股票金融相关回答。',
       '> 下文观点为机器辅助整理的原文线索，不代表事实已验证、交易建议、或作者系统阐述的观点。',
       f'> 分类来源：规则关键词 {sum(r["classification_method"]=="规则关键词" for r in related)} 条；模型建议（需人工复核） {sum(r["classification_method"]!="规则关键词" for r in related)} 条。',
       '', '## 一、主要观点线索与代表性回答', '']
for key,idea in ideas.items():
    ev=lens_evidence(related,key)
    parts.extend([f'### {key}', '', idea, '', f'关键词检索命中：{len(ev)} 条（检索命中不等于支持该观点）。',''])
    for r in ev[:6]:
        parts.extend([f'- [{r["title"]}]({r["url"]})（{r["date"][:10]}）：{r["quote"].replace(chr(10)," ")}', ''])
parts.extend(['## 二、全部股票金融相关回答（含原文全文）',''])
for i,r in enumerate(sorted(related,key=lambda x:x['date'],reverse=True),1):
    parts.extend([f'### {i}. {r["title"]}', '',f'- 日期：{r["date"]}',
                  f'- 原文：{r["url"]}',f'- 分类：{r["category"]} / {r["subcategory"]}（{r["classification_method"]}）','',r['body_md'],'','---',''])
file=root/'股票金融专题_观点与全部回答.md'
file.write_text('\n'.join(parts),encoding='utf8')
print('股票金融相关',len(related),'条；报告',file.name,'大小',file.stat().st_size)
