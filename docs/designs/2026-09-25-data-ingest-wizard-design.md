---
design_type: feature
created_at: 2026-09-25
---

# 数据入库向导（前端入库通道）· 设计

对应需求：库中数据太少，直接从文件加数据不方便 —— 在已打开的界面里加一个入库窗口，
流程为「自然语言转需求 → 根据需求查数据 → 筛选数据 → 格式化入库」。

## Intent Contract

```
intent: 在前端提供「自然语言 → 抓数 → 预览筛选 → 确认入库」的五步向导，
        让新公司/新指标不碰文件、不写 SQL 就能进结构化财务库。
constraints:
  - 前端仍是 web/index.html 单文件、零 CDN、零构建；
  - 既有 453 条测试的语义一条不变；
  - 写库动作必须 JWT + admin 角色 + audit_logs 留痕；
  - LLM 产出不得直接信任：公司/指标必须过既有归一器；
  - 抓取复用 src/ingest 既有 9 个源，失败明确报错、不静默降级；
  - 不触碰年报 BM25 索引与评测语料（结构化入库与检索索引完全隔离）。
success_criteria:
  - pytest 全绿（≥453 + 新增向导用例）；
  - preview 阶段以「库内指标行数不变」证明只抓不写；
  - commit 后新公司立即可被 list_companies 查到、新指标可答，
    且 audit_logs 出现带 batch 的 data_ingest 记录；
  - LLM 不可用时手填表单路径完整可用；
  - 浏览器手测：五步走通一遍真实入库（新公司）。
risk_level: medium
```

## Verification Contract

```
verify_steps:
  - run tests: pytest -q（新增 tests/test_ingest_wizard.py，假 provider/假 LLM，不联网）
  - check: preview 前后各数一次结构化库指标行数，两次必须相等
  - check: commit 用非 admin 令牌必须 403；audit_logs 新增 action=data_ingest 且 actor 取自令牌
  - check: 浏览器端到端走通五步（截图留档到 docs/plans/assets/）
  - confirm: 提问新入库公司的指标，答案数值与刚入库的一致
```

## Governance Contract

```
approval_gates:
  - 预览表 UI 形态（期次×指标网格、补充源标黄）实现后浏览器确认；
  - 入库字段映射（抓取行 → 库表列）在 code-review 时逐列核对；
  - 任何「自动入库、跳过预览」的提议默认拒绝（除非用户显式改契约）。
rollback:
  - 前端单文件整体回退；三个新端点独立可摘除，不影响既有问答链路；
  - 误入数据按 audit_logs 里的 batch 定位后手工 SQL 删除（设计上不做自动回滚表）。
ownership: 项目所有者（用户）对本仓实现与验收签字；实现由 agent 执行。
```

## Scope

| 方向 | 内容 |
|---|---|
| In | 五步向导 UI（需求描述 / 需求单可手改 / 抓取 / 预览筛选 / 确认入库）；`ingest/plan`、`ingest/preview`、`ingest/commit` 三个端点；LLM→需求单解析（deepseek，不可用时降级手填）；dry-run 只抓不写形态；按勾选格写库 + 审计；期数/指标数防呆上限 |
| Out | 文件上传导入（CSV/Excel）—— 若要做另立 feature；年报 PDF 的 BM25 语料入库（已有 ingest 链路）；自动回滚表 / 双写批次表；多用户协同编辑需求单；定时自动抓取 |

## Decisions

| # | 决策 | 弃选方案及理由 |
|---|---|---|
| D1 | 抓取复用 ingest 层既有采集与补缺逻辑，新增**只抓不写**（dry-run）形态 | 另写一套抓取 —— 两处维护必漂移 |
| D2 | LLM 只产「需求单草稿」，公司/指标一律过既有归一器（公司→代码、指标→别名表），归不上的留在界面让人改 | 直接执行 LLM JSON —— 解析错就写错库；纯规则表单 —— 「自然语言」名不副实 |
| D3 | commit 端点要求 JWT + admin 角色，batch id 记入 audit_logs | 匿名/普通 analyst 可写库 —— 高危动作无闸门 |
| D4 | 只写结构化库，不重建、不触碰 BM25 年报索引；公司池来自结构化库，新数据立即可问 | 入库顺手重建索引 —— 污染既有评测数字 |
| D5 | 不新建批次表：batch 只活在 audit_logs 里 | 独立批次表 —— 引入 SQLite/MySQL 双后端 DDL 负担，YAGNI |
| D6 | 单次抓取的期数与指标数设上限，走 net 层既有限速/重试 | 无上限 —— 一次点错拉爆外部 API 限速 |

## Surface

**APIs** — 三个新端点，全部挂既有 JWT Bearer 依赖：`POST /api/ingest/plan`（自然语言 → 需求单草稿，LLM 不可用时返回空草稿让前端走手填）；`POST /api/ingest/preview`（按需求单 dry-run 抓取，返回期次×指标网格与每格来源/单位/补缺标记，**不写库**）；`POST /api/ingest/commit`（勾选格 → 写结构化库 + 审计，需 admin 角色）。

**Storage** — 只写既有结构化财务库的指标值表（列已含 source_table / source_field / unit / 期次 / 公司代码），无 DDL 变更、无新表；audit_logs 新增 `data_ingest` 动作，detail 里带需求单与勾选摘要、batch id。

**Components** — 前端在 `web/index.html` 内新增「数据入库」页签（与对比分析同级的第三页签，仅登录后可见，admin 可用；非 admin 显示只读提示），五步向导为同屏分步卡片，风格沿用 HITL 面板与骨架屏；后端逻辑放 ingest 层新模块（需求单模型 / 归一 / dry-run 采集 / 勾选写库），服务端点挂在 server 的端点区。

**Files touched** — `web/index.html`、`src/server.py`、`src/ingest/`（dry-run 形态 + 新向导模块 + LLM 需求单解析）、`tests/test_ingest_wizard.py`（新）、`requirements.txt`（如有新依赖需同步）、README / CHANGELOG / EXTENSION / 不足清单四份文档的对应小节。

## Risks & Open Questions

- **LLM 解析错实体** → 归一器 + 预览确认双闸兜底；解析率不设 KPI，归不上就让人改。
- **外部 API 限速/字段变更** → net 层重试 + 明确报错；失败行在预览表里标红而非整单失败。
- **错误口径数据入库** → 预览表每格标来源与单位；补充源命中的格子标黄提示口径差异；commit 前强制勾选确认。
- **开放问题**：非 admin（analyst）是否需要「提交入库申请、admin 审批」的两级流？本期不做（只有 admin 能开向导），登记到不足清单看后续需求。
- **开放问题**：新公司没有年报 PDF（BM25 无语料）时，问答会走「库内数值可答、原文引用不可答」的混合形态 —— 预期行为，验收时要在文档里向使用者说明。
