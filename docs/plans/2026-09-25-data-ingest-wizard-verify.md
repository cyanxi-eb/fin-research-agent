# 数据入库向导 —— 验收证据汇总（Step 8）

工作流：[.trae/documents/2026-09-25-data-ingest-wizard-workflow.md](../../.trae/documents/2026-09-25-data-ingest-wizard-workflow.md)
设计：[docs/designs/2026-09-25-data-ingest-wizard-design.md](../designs/2026-09-25-data-ingest-wizard-design.md)
实测时间：2026-09-25 → 2026-09-26（Step 8 手测跨夜）

## 1. 自动化测试证据（Step 1–7 汇总）

| 套件 | 结果 | 覆盖点 |
| --- | --- | --- |
| tests/test_ingest_wizard.py | 19 passed | 归一 5（含乱输入防呆）、LLM→需求单 4、preview 只抓不写 4、commit 勾选+审计 3、**6 位代码通道 3** |
| tests/test_server_ingest.py | 5 passed | 三端点 401 无票、analyst 403、admin 200、免鉴权形态可用 |
| tests/test_frontend.py | 23 passed | ingest 页签/视图、三端点接线、admin 提示、「已入库」文案、ID 表自洽 |
| 全量（Step 7 时点） | 63 passed（四套） | — |

**只抓不写的机器证明**（Step 4）：preview 前后 `financial_indicators` 行数不变 + save_company 打桩必红断言。

## 2. Step 8 手测中发现的缺陷与修复（RED→GREEN）

**库外新公司死锁**：手测「把平安银行近3年年报的营业总收入和归母净利润加进来」→
LLM 输出公司名 → companies 表查无此人 → 进 unresolved → 预览报「未识别的公司」→ 全流程 BLOCKED。
根因：向导的存在意义就是往库里加公司，而公司解析的事实来源恰是 companies 表 —— 新公司必然 resolve 不出。

**修复**（6 位纯数字 = 股票代码直通通道）：
1. `_SYSTEM_PLAN` prompt 要求 LLM 优先填 6 位代码（平安银行填 000001）
2. `normalize_request` / `_resolve_plan_companies`：resolve 不出但原文是 6 位纯数字 → 直通收下
   （name 暂与代码相同）；库外**名字**仍进 unresolved 等人改 —— 宁可让人改，不可猜代码
3. 前端 unresolved 提示补「库外新公司请填 6 位股票代码」

测试：新 3 条用例先 RED（2 failed，用例初稿误用夹具内标识，已改为真库外的 600036/招商银行）→ 实现 → 全套 GREEN。

## 3. 浏览器端到端手测（2026-09-26，全通过）

环境：`python launcher.py --no-auth --no-browser --port 8020`，真实库 fin_research.db
（手测前 companies 表：600519 贵州茅台 / 000858 五粮液 / 300750 宁德时代 / 002594 比亚迪 / 601318 中国平安，**无平安银行**）。

| 步骤 | 结果 | 证据 |
| --- | --- | --- |
| ① 需求转单 | 通过 | LLM 输出 companies=000001（prompt 修复生效）、indicators=营业总收入,归母净利润、periods=3、无 unresolved 红标 |
| ② 抓取预览 | 通过 | 预览网格 6 格（近 3 期 × 2 指标），每格含值+单位+来源表名，无失败标记 |
| ③ 勾选 | 通过 | 勾 2024-12-31 与 2023-12-31 的两指标，共 4 格 |
| ④ 确认入库 | 通过 | 提示「已入库 4 格，新数据立即可问。批次：f08bb1ac82d24b7da3cf6d75948118cd」 |
| ⑤ 提问验证 | 通过 | 「平安银行2024年的营业总收入是多少」→ analysis 路由正常作答，校验行「核对 4 个数字 · 有出处 · 无出处数字 0 · 死链引用 0」 |
| console | 通过 | 无 4xx/5xx、无 JS 异常 |

### SQL 直查核库（trust but verify）

- `companies`：新增 `('000001', '000001', ...)` 档案行（name=代码原文，见已知不足）
- `financial_indicators`：4 行真实值落库 ——
  000001 / 2024-12-31 / 营业总收入 1,466.95 亿元（income.TOTAL_OPERATE_INCOME）、
  2024-12-31 / 归母净利润 445.08 亿（income.PARENT_NETPROFIT）、2023-12-31 两指标同步入库
- `audit_logs`：`action=data_ingest`，batch 与页面提示一致，selected 与勾选 4 格逐一吻合，actor=anonymous（免登录形态预期）

### 已知不足（如实登记）

1. **截图未存档**：browser_take_screenshot 全部因 IDE 命令超时失败，
   `docs/plans/assets/2026-09-25-ingest/` 暂为空目录；本文件文字证据 + SQL 直查代偿。
2. **新公司档案 name=代码原文**：东财采集器按需取列（不含 SECURITY_NAME_ABBR），
   入库时拿不到真实简称，档案显示「000001」而非「平安银行」。功能不受影响（可问可对比），
   回填需扩展 fetch_eastmoney 契约，登记入不足清单待后续版本。
3. **analyst 提交 → admin 审批的两级入库流**本期未做（设计 D3 只做了 admin 闸门）。

## 4. Step 10 代码审查（2026-09-26）与全量回归

独立审查代理对新增代码逐条核查 10 项纪律（D1/D2/D3/D4、字段映射、XSS、缓存、测试有效性），结论：
**核心纪律全部成立**；发现 4 项（④不修）：

| # | 发现 | 处置 |
| --- | --- | --- |
| ① | commit 补档案对**既有公司**也会执行，ON CONFLICT 用空串覆盖 market/industry/org_id —— 重复入库即抹档案 | ✅ 已修：仅对 companies 表查不到的代码补档案（RED→GREEN） |
| ② | preview 白名单只并 indicators，指标+比率同时点名时比率行被滤掉、无法勾选 | ✅ 已修：白名单并入 ratios（RED→GREEN） |
| ③ | `_resolve_plan_companies` 对客户端直传的 code 不做格式校验，垃圾串原样拼进东财 filter | ✅ 已修：code 先过 `_CODE_RE`，非法回落 name 解析（RED→GREEN） |
| ④ | `_SOURCE_CACHE` 进程内无界增长 | ⏸ 不修：fetch_eastmoney 既有行为，非本轮引入，仅内存量级 |

全量回归（修复后）：**pytest 484 passed**（= 453 基线 + 本轮新增 31：wizard 22 + server_ingest 5 + frontend 4）。
相关四套件（wizard/server_ingest/server_auth/frontend）60 passed。

## 5. 人工闸门（gate: human）—— 已确认（2026-09-26）

- [x] **预览表 UI 形态**：预览网格每格 = 值 + 单位 + 来源表名 + 勾选框；补充源标黄、失败标红 —— **用户确认通过**
- [x] **入库字段映射**：指标行六字段（period/indicator/value/unit/source_table/source_field）
      原样进库；companies 档案 (code, name=代码原文, market/industry/org_id 空) —— **用户接受**，
      简称回填（东财 SECURITY_NAME_ABBR）登记入不足清单，后续版本处理
