# 答案级评测报告（Step 6 / C7）

- 生成时间：2026-09-23 08:34:28（本次评测耗时 823s）
- **判分模型：deepseek-flash**（faithfulness / answer_relevancy 由该模型判；判分失败计为「未判」）
- 评测集：`eval/golden_qa.jsonl` 共 55 题；**本次实际评测 55 题**（mode=bm25，每页配额=2，intent=auto）
- 题数与分类计数：数值题（`indicator`）31、引用题（`literal`）10、法规题（`article`）4、拒答题（`expect_refuse`）10；其中 `answer_expect.judge=true` **45 题**（`judge=false` 10 题，均为拒答题、无答案可判）

> ⚠️ **样本量提醒**：数值题 31 题 → 1 题 = 3.2pp；faithfulness 有效分 45 题 → 1 题 = 2.2pp；answer_relevancy 有效分 45 题 → 1 题 = 2.2pp。所以 5pp 量级的差异只能算「方向」，不能算「结论」。
> ℹ️ **可复现性**：确定性四项（数值/引用/拒答/路由）逐位可复现；faithfulness / answer_relevancy 由 LLM 判分，**同模型重跑会有 1~3pp 抖动**（实测同一 55 题两轮：23.7%/82.3% → 25.1%/79.8%）。因此这两个数必须与「判分模型名 + 生成时间」一起读，换模型或重入库后不可直接比。

## 1. 四个数字

| 指标 | 值 | 分母（口径） |
|---|---|---|
| **faithfulness**（忠实度） | 20.3%（45 题） | 有效分 45 / 判分 45 题；未判 0 题 |
| **answer_relevancy**（答案相关性） | 83.6%（45 题） | 有效分 45 / 判分 45 题；未判 0 题 |
| **数值准确率** | 100.0%（31 题） | `gt.type==indicator` 的题（含单位换算/千分位/符号） |
| **引用命中率** | 20.0%（40 题） | `expected_pages` 非空的题（**构成见 §3**） |

辅助：证据完整性（数值题答案数值能在被保留的检索片段里找到）63.3%（30 题）；路由命中率 91.1%（45 题）。

### 1.1 按「有无检索上下文」分桶（暴露上述结构性偏差）

| 分桶 | 题数 | faithfulness | answer_relevancy |
|---|---|---|---|
| 有检索上下文（literal / article） | 14 | 65.4%（14 题） | 47.5%（14 题） |
| 无检索上下文（indicator，走工具层） | 31 | 0.0%（31 题） | 99.8%（31 题） |

> 读法：**faithfulness 的头条数字主要反映「有多少题没有可判的上下文」，而不是「答案编造了多少」**。数值题的正确性应看「数值准确率」；引用题/法规题的忠实度才是有信息量的那一列。

> 口径说明：`faithfulness` / `answer_relevancy` 的均值**只按有效分算**，判分失败的题计为「未判」并**单列计数、不进分母** —— 把未判当 0 分会凭空压低指标，直接丢掉又不留痕（看不出这轮有多少题没判成）。这与 `eval_retrieval.py` 里「拒答正确率」用分桶的思路一致。

## 2. 逐题明细

`命中` 列：数✓/数✗ = 数值题是否命中金标准数值；引✓/引✗ = 引用题是否命中期望页。

| 题目 | 类型 | 路由意图 | 拒答 | 命中 | faithfulness | relevancy | 备注 |
|---|---|---|---|---|---|---|---|
| `ind-600519-2024-revenue`<br>贵州茅台2024年的营业总收入是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-600519-2024-np`<br>贵州茅台2024年归属于上市公司股东的净利润是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-600519-2024-gm`<br>贵州茅台2024年的毛利率是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-600519-2024-cf`<br>贵州茅台2024年经营活动产生的现金流量净额是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-600519-2024-roe`<br>贵州茅台2024年的净资产收益率ROE是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-600519-2024-eps`<br>贵州茅台2024年的基本每股收益是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-600519-2024-debt`<br>贵州茅台2024年末的资产负债率是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-600519-2025-revenue`<br>贵州茅台2025年的营业总收入是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 0.95 |  |
| `ind-000858-2024-revenue`<br>五粮液2024年的营业总收入是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-000858-2024-np`<br>五粮液2024年的归母净利润是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-000858-2024-gm`<br>五粮液2024年的毛利率是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-300750-2024-revenue`<br>宁德时代2024年的营业总收入是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-300750-2024-np`<br>宁德时代2024年的归母净利润是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-300750-2024-cf`<br>宁德时代2024年经营活动产生的现金流量净额是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-002594-2024-revenue`<br>比亚迪2024年的营业总收入是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-002594-2024-np`<br>比亚迪2024年的归母净利润是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-002594-2024-debt`<br>比亚迪2024年末的资产负债率是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-601318-2024-np`<br>中国平安2024年的归母净利润是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-601318-2024-equity`<br>中国平安2024年末的归母净资产是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-601318-2024-assets`<br>中国平安2024年末的总资产是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-601318-2024-revenue`<br>中国平安2024年的营业总收入是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-601318-2024-debt`<br>中国平安2024年末的资产负债率是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `var-600519-2024-bps`<br>600519 2024年的每股净资产是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `var-600519-2024-npm`<br>茅台2024年的净利率是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `var-000858-2023-revenue`<br>五粮液2023年的营业总收入是多少 | none | analysis | 否 | — | — | — |  |
| `lit-600519-2024-audit`<br>贵州茅台2024年年报的审计意见类型是什么 | literal | rag | 否 | 引✗ | 0.33 | 0.10 |  |
| `lit-600519-2024-dividend`<br>贵州茅台2024年度的利润分配方案是每10股派发现金 | literal | rag | 否 | 引✓ | 0.74 | 0.30 |  |
| `lit-000858-2024-dividend`<br>五粮液2024年度每10股派发现金红利多少元 | literal | rag | 否 | 引✓ | 0.30 | 0.70 |  |
| `lit-300750-2024-sales`<br>宁德时代2024年的电池销量是多少 | literal | rag | 否 | 引✗ | 0.71 | 0.00 |  |
| `lit-601318-2024-auditor`<br>中国平安2024年年报的审计机构是哪家 | literal | rag | 否 | 引✓ | 1.00 | 0.90 |  |
| `ref-food`<br>公司食堂的菜谱是什么 | none | rag | 是 | — | — | — |  |
| `ref-year-missing`<br>贵州茅台2020年的营业总收入是多少 | none | analysis | 否 | — | — | — |  |
| `ref-out-of-scope`<br>贵州茅台2024年的锂电池产能是多少 | none | rag | 否 | — | — | — |  |
| `ref-nonsense`<br>宁德时代2024年员工食堂满意度调查结果如何 | none | rag | 是 | — | — | — |  |
| `art-226-13-annual-report-deadline`<br>上市公司年度报告最晚应当在什么时候编制完成并披露 | article | rag | 否 | — | 0.55 | 0.00 |  |
| `art-226-18-earnings-preannounce`<br>上市公司在什么情况下必须进行业绩预告 | article | rag | 否 | — | 0.29 | 0.00 |  |
| `art-226-35-insider-info-ban`<br>上市公司通过业绩说明会、接受投资者调研时不得做什么 | article | rag | 否 | — | 0.30 | 0.00 |  |
| `art-226-17-periodic-report-review`<br>定期报告披露前需要经过哪些内部审议程序 | article | rag | 否 | — | 0.59 | 0.20 |  |
| `lit-600519-2024-auditno`<br>贵州茅台2024年年报的审计报告编号是多少 | literal | rag | 否 | 引✓ | 1.00 | 0.80 |  |
| `lit-600519-2025-auditno`<br>贵州茅台2025年年报的审计报告编号是多少 | literal | rag | 否 | 引✓ | 0.78 | 0.70 |  |
| `lit-000858-2024-auditno`<br>五粮液2024年年报的审计报告文号是什么 | literal | rag | 否 | 引✓ | 0.74 | 1.00 |  |
| `lit-300750-2024-auditno`<br>宁德时代2024年年报的审计报告编号是什么 | literal | rag | 否 | 引✓ | 0.82 | 1.00 |  |
| `lit-300750-2024-dividend`<br>宁德时代2024年度每10股派发现金分红多少元 | literal | rag | 否 | 引✓ | 1.00 | 0.95 |  |
| `ind-600519-2024-deducted-np`<br>贵州茅台2024年的扣非净利润是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-600519-2024-invest-cf`<br>贵州茅台2024年投资活动产生的现金流量净额是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-000858-2024-deducted-np`<br>五粮液2024年的扣非净利润是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-300750-2024-debt`<br>宁德时代2024年末的资产负债率是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-002594-2024-assets`<br>比亚迪2024年末的总资产是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-601318-2024-liabilities`<br>中国平安2024年末的总负债是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ind-600519-2025-deducted-roe`<br>贵州茅台2025年的扣非ROE是多少 | indicator | analysis | 否 | 数✓ | 0.00 | 1.00 |  |
| `ref-000858-2019-revenue`<br>五粮液2019年的营业总收入是多少 | none | analysis | 否 | — | — | — |  |
| `ref-601318-2018-np`<br>中国平安2018年的归母净利润是多少 | none | analysis | 否 | — | — | — |  |
| `ref-600519-2024-employee-height`<br>贵州茅台2024年的员工平均身高是多少 | none | rag | 是 | — | — | — |  |
| `ref-300750-2024-pig-slaughter`<br>宁德时代2024年的生猪出栏量是多少 | none | rag | 是 | — | — | — |  |
| `ref-cmb-2024-np`<br>招商银行2024年的归母净利润是多少 | none | analysis | 否 | — | — | — | ⏸ 待人工确认（citation_unsupported） |

## 3. 已知不足（自动列出）

- **引用命中率的分母含 30 道数值题（构成问题，未收窄分母）**：分母 40 题里数值题 30 道、引用题（literal）10 道。数值题走工具层（`src/tools/`），`citations` 恒为空、出处是「表.字段」而不是页码，**结构上不可能命中页码**。所以 20.0%（40 题） 这个数字不是「引用质量」的干净读数 —— 真要看引用质量应只看 literal 题。（未把数值题移出分母：那会让分母随题型构成漂移，报告与历史口径不可比。）
- **4 道法规题被路由成 `rag`（未修 `src/graph/router.py`）**：art-226-13-annual-report-deadline, art-226-18-earnings-preannounce, art-226-35-insider-info-ban, art-226-17-periodic-report-review。根因是 `_COMPLIANCE_CUES` 未覆盖这些问法，属真实缺口；本步不改 `src/graph/*`，仅如实标注。连带影响：法规题因此走年报检索，拿不到条文级引用。
- **判分失败 / 未判的题数**：faithfulness 与 answer_relevancy 均 45 题全部判成，无未判。
- **数值题的 faithfulness 是「无上下文判定」（结构性，不是质量差）**：判分的 31 道数值题走工具层、`citations` 为空，送进判分 prompt 的【资料】是空的 —— 模型只能判「无资料支撑」。所以 faithfulness 的头条数字被这批题拉低，**不能读成「答案在编造」**；数值题的正确性看「数值准确率」更合适。
- **待人工确认（`hitl.pending`，不是错误）**：1 题 ['ref-cmb-2024-np'] —— 这是「离线降级下等人工确认」这个设计在工作。

## 4. 复现

```bash
python scripts/eval_rag.py --judge          # 全量 + LLM 判分，写本报告与 report_answer.json
python scripts/eval_rag.py --judge --limit 6 # 冒烟（前 6 题，均为数值题）
python scripts/eval_rag.py --no-write --limit 8  # 不带判分：零 token、行为不变
```
