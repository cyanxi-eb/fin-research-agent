---
intent: >
  完成 fin-research-agent 的 Step 6，把 Step 0–5 的能力交付成「可一条命令复现、可演示、可量化」的成品：
  ① 服务化补全（SSE 流式问答 / api/compare 多公司对比 / 多轮指代消解）；
  ② 单文件前端 web/index.html（对话 + 可点击引用卡片 + 对比表 + 趋势图 + HITL 确认，离线可用、不引 CDN）；
  ③ 答案级评估（自实现同口径 faithfulness / answer_relevancy + 数值准确率 + 引用命中率），并把金标准从 34 题扩到 50~100 题；
  ④ 容器化交付（app + MySQL 8 双容器默认起，语料 seed 进镜像），在 VM 192.168.57.128 上真跑验证；
  ⑤ README / CHANGELOG / EXTENSION / 不足清单 四份文档与实现一致。
  约束：不引 ragas 等重依赖；不做接口鉴权（登记为已知不足）；源码与文档中不得出现真实密钥；
  前端不得依赖任何外部 CDN（演示环境可能断网）。
success_criteria:
  - eval/report_answer.md 给出四个数字（faithfulness、answer_relevancy、数值准确率、引用命中率），并标注模型名与时间戳
  - 在 VM 192.168.57.128 上 docker compose up -d --build 后，GET /api/health 返回 ok=true 且 index/vector/regulation/checkpointer 全 ok、业务库后端为 mysql
  - python launcher.py 一条命令起服务并打开 web/index.html；页面具备对话（SSE 流式）、引用卡片、对比表、趋势图、HITL 确认五项能力
  - pytest -q 全绿，用例数 ≥ 350，且新增用例覆盖 SSE 契约 / compare / 多轮指代 / 答案级判定
  - README/CHANGELOG/EXTENSION/不足清单 四份文档与实现一致，无过期数字
risk_level: medium
auto_approve: true
worktree: false
---

## 批次 A — 服务化补全（SSE / compare / 多轮指代）

- [x] **Step A1: 写 SSE 事件协议与流式合成的失败用例**
action: 新建 `tests/test_streaming.py`。断言 `src/streaming.py` 需提供的契约：事件名序列为 `meta` → `token`* → `citations` → `verify` → `done`（挂起时 `meta` → `token`* → `citations` → `verify` → `hitl` → `done`）；模型不可用时只发一次 `token`（内容为降级摘录）再发 `done`，且 `done.response.degraded` 为 True —— 注意 `src/llm.py::is_ready()` 返回的是 `(bool, str)` **元组**，判断要写 `is_ready()[0] is False`；`meta` 必须带 `intent` / `route` / `thread_id` 三个键。**conftest.py 里没有 LLM 夹具**（只有 `synth_pdf` 与 `synth_db` 两个）—— 照 `tests/test_answer_citation.py` 第 54 行 `fake_llm` 的做法，在本文件内用 `monkeypatch.setattr(llm, "is_ready", lambda: (True, "ok"))` 替换就绪状态，并 monkeypatch 本步 A2 要新增的 `answer._astream_llm`（异步生成器，逐字符产出）。此时 `src/streaming.py` 不存在，用例必须报 `ModuleNotFoundError` 才算真失败。
loop: false
verify: .venv/Scripts/python.exe -c "import subprocess,sys; r=subprocess.run([sys.executable,'-m','pytest','tests/test_streaming.py','-q','--tb=line'],capture_output=True,text=True); out=r.stdout+r.stderr; assert r.returncode!=0, 'RED 校验失败：实现前用例不该通过'; assert ('error' in out.lower() or 'failed' in out.lower()), out[-600:]; print('RED 已确认'); print(out[-400:])"
gate: auto

- [x] **Step A2: 实现 src/streaming.py**
action: ① 在 `src/answer.py` 新增 `async def _astream_llm(question: str, hits: list[dict]) -> AsyncIterator[str]` —— 与既有 `_call_llm`（第 363 行）**同 prompt/同模型配置**（复用 `llm.get_active_llm()` 拿 base_url/api_key/model），内部用 `ChatOpenAI(...).astream(messages)` 逐块 `yield`。模型的构造只在这两处，`src/streaming.py` 不得自己 new client。② 新建 `src/streaming.py`，导出 `EVENTS: tuple[str, ...]`（含 `meta/token/citations/verify/hitl/done/error`）与 `async def stream_answer(question, *, history=None, code=None, year=None, mode=None, topk=None, use_llm=True, force_intent=None, thread_id=None) -> AsyncIterator[tuple[str, dict]]`。实现要点：① 先跑 router（复用 `src/graph/router.py`）产出 `meta`；② 数值/合规两条意图**不流式**（它们的答案本来就是确定性拼装，没有 token 可流）——`meta` 后直接给 `citations`/`verify`/`done`；③ `rag` 意图先 `retrieve`，再调 `answer._astream_llm` 逐段发 `token`；④ 流式失败（超时/异常/`llm.is_ready()[0]` 为 False）必须回落为「一次性 `token` + `done`」，且 `done.response.degraded=True`、`notes` 里写明原因 —— 绝不静默中断；⑤ 结束时 `done` 携带与 `src/graph/state.py::to_response()` 同形的完整响应（复用 `src/answer.py` 的 `validate_citations` / `collect_citation_list`，不得另写一套）。模块头注释写清「流式与一次性两条路的输出必须同形，否则前端要写两套渲染」。
loop: until .venv/Scripts/python.exe -m pytest tests/test_streaming.py -q 通过
max_iterations: 4
verify: .venv/Scripts/python.exe -m pytest tests/test_streaming.py -q
gate: auto

- [x] **Step A3: 在 server.py 暴露 POST /api/ask/stream（SSE）**
action: 在 `src/server.py` 新增 `POST /api/ask/stream`，`media_type="text/event-stream"`，请求体复用现有 `AskRequest`（不新增字段，多轮历史用已有的 `thread_id` 取用）。用 `StreamingResponse` 包装 `stream_answer`，每个事件序列化为 `event: <name>\ndata: <json>\n\n`（`ensure_ascii=False`）。加响应头 `Cache-Control: no-cache` 与 `X-Accel-Buffering: no`。端点必须写成 `async def`（流式必须留在事件循环里，但内部调用的检索是同步 CPU 活 —— 用 `asyncio.to_thread` 包住 `stream_answer` 里的同步检索段，避免阻塞事件循环）。同时在 `tests/test_server.py` 追加断言：无索引时该端点返回 503；正常时首个事件是 `meta`。
loop: until .venv/Scripts/python.exe -m pytest tests/test_server.py tests/test_streaming.py -q 通过
max_iterations: 3
verify: .venv/Scripts/python.exe -m pytest tests/test_server.py tests/test_streaming.py -q
gate: auto

- [x] **Step A4: 写多公司对比的失败用例**
action: 新建 `tests/test_compare.py`。断言 `src/compare.py` 的契约：`compare(indicator, codes, *, period=None, db_path=None) -> dict`，返回键固定为 `ok/indicator/unit/rows/periods_consistent/chart/note`；`rows` 每行含 `code/name/period/value/unit/source`；`periods_consistent=False` 时 `note` 必须非空且说明各家实际期次；`chart.series` 为 `[{code,name,points:[{period,value}]}]`；指标名不认识返回 `ok=False` + `error="unknown_indicator"`；公司数少于 2 返回 `ok=False` + `error="need_at_least_two_companies"`。**趋势图要多点，不是单点**：当 `period=None` 时 `chart.series[i].points` 必须覆盖该指标在库里**全部可用期次**（`synth_db` 造 ≥3 期数据 → 每系列 points ≥3 个，且按期次**升序**），`chart.periods` 给出全局有序期次列表；显式传 `period=` 时退化为单点且 `chart.periods == [period]`。用例跑在 `synth_db` 合成数据夹具上（不联网、不碰真实库）。
loop: false
verify: .venv/Scripts/python.exe -c "import subprocess,sys; r=subprocess.run([sys.executable,'-m','pytest','tests/test_compare.py','-q','--tb=line'],capture_output=True,text=True); out=r.stdout+r.stderr; assert r.returncode!=0, 'RED 校验失败：实现前用例不该通过'; assert ('error' in out.lower() or 'failed' in out.lower()), out[-600:]; print('RED 已确认'); print(out[-400:])"
gate: auto

- [x] **Step A5: 实现 src/compare.py 与 GET/POST /api/compare**
action: 新建 `src/compare.py`：`compare()` **必须复用**已注册的工具 `compare_companies`（实现体在 `src/tools/indicators.py` 第 324 行，经 `@tool` 注册进 `src/tools/registry.py` 的 `_TOOLS`）—— 用 `registry.get_tool("compare_companies")` 取到后调用，不得另写取数逻辑；把工具返回的 `{ok, indicator, rows[{rank,code,name,period,value,display,source}], periods_consistent, missing}` 转成上述结构。**然后补 `chart`**：趋势图要的是**同一家公司跨多个期次**的序列，而 `compare_companies` 只给每家「最新一期」的一个点（`periods=1`）—— 所以 `chart.series` 必须另外用**已注册的 `get_financial_indicator` 工具**（`src/tools/indicators.py` 第 250 行，参数 `periods: int = 6`）逐公司取多期序列来构建，同样不得直接写 SQL；`points` 按期次升序，`chart.periods` 为全局有序期次并集。显式传 `period=` 时只取该期（`points` 单点）。然后在 `src/server.py` 加 `GET /api/compare?indicator=营业总收入&codes=600519,000858&period=2024-12-31` 与 `POST /api/compare`（同一实现，GET 便于前端与人工验证）。端点用同步 `def`（走工具层读库，秒级以内，交给线程池）。
loop: until .venv/Scripts/python.exe -m pytest tests/test_compare.py tests/test_server.py -q 通过
max_iterations: 3
verify: .venv/Scripts/python.exe -m pytest tests/test_compare.py tests/test_server.py -q
gate: auto

- [x] **Step A6: 写多轮指代消解的失败用例**
action: 新建 `tests/test_history.py`。断言：① `src/graph/state.py` 的 `initial_state(..., history=[...])` 接受 `history: list[dict]`，每项 `{question, intent, code, year}`，缺省为空列表；② `src/retrieve/filters.py` 新增 `resolve_entities(question, history=None) -> dict`：当问句里**抽不到公司**而 history 中最近一轮有唯一 `code` 时，用该 `code` 补上并在 `note` 写明「沿用上一轮的公司：XXX」；③ 问句里抽到了公司**绝不**用 history 覆盖（本轮显式实体优先）；④ history 中最近一轮的公司不唯一或为空时**不补**（宁愿不猜）；⑤ 年份同理但不能跨公司沿用（公司来自本轮、年份来自上一轮时必须都标明来源）。用例覆盖这 5 条，纯函数、不联网。
loop: false
verify: .venv/Scripts/python.exe -c "import subprocess,sys; r=subprocess.run([sys.executable,'-m','pytest','tests/test_history.py','-q','--tb=line'],capture_output=True,text=True); out=r.stdout+r.stderr; assert r.returncode!=0, 'RED 校验失败：实现前用例不该通过'; assert ('error' in out.lower() or 'failed' in out.lower()), out[-600:]; print('RED 已确认'); print(out[-400:])"
gate: auto

- [x] **Step A7: 接入多轮指代（state / filters / pipeline / builder / server）**
action: 改 5 处：① `src/graph/state.py` 加 `history: list[dict]` 并在 `initial_state` 里加参数（默认 `None` → `[]`），`to_response()` 不导出 history（响应体不膨胀）；② `src/retrieve/filters.py` 实现 `resolve_entities()`（同文件已有 `detect_company` / `detect_year` / `detect` 三个纯函数，本函数据此扩展），并在 `pipeline.py` 第 317 行的 `_apply_auto_filter` 里用它替代原先的直接 `filters.detect` 调用，`retrieve()` 只加 `history=None` 参数往下透传；③ `src/graph/builder.py` 的 `run_qa` / `run_agent` / `resume_agent` 加 `history=None` 透传；④ `src/server.py` 的 `/api/ask` 与 `/api/ask/stream` 在传了 `thread_id` 时，从 Checkpointer 的上一轮响应里取回 `history` 拼进本轮（**只取实体，不取答案**）；⑤ 新增 `history` 的**上限裁剪**（只保留最近 `FA_HISTORY_MAX` 轮，默认 5，配置写进 `src/config.py`）并在 `scripts/README` 式的接口约定表里登记。回归约束：`tests/test_answer_citation.py` 与 `tests/test_agent_graph.py` 必须继续全绿（Step 3 定的「直接调用与走图返回值一致」不能破）。
loop: until .venv/Scripts/python.exe -m pytest tests/test_history.py tests/test_agent_graph.py tests/test_answer_citation.py tests/test_query_filter.py -q 通过
max_iterations: 3
verify: .venv/Scripts/python.exe -m pytest tests/test_history.py tests/test_agent_graph.py tests/test_answer_citation.py tests/test_query_filter.py tests/test_server.py -q
gate: auto

## 批次 B — 单文件前端

- [x] **Step B1: 前端骨架（布局 + 会话 + 流式渲染 + 结果面板）**
action: 新建 `web/index.html` 单文件（内联 CSS/JS，零外部 CDN、零构建步骤）。实现：① 顶栏显示 `/api/health` 的各通道状态（index/vector/rerank/regulation/checkpointer/audit/backend），任一不可用用黄标而不是红叉（降级是设计内的）；② 中部对话流；③ 提问框支持「提问 / 检索模式 bm25|hybrid / 强制意图 auto|rag|analysis|compliance」；④ 提交走 `fetch('/api/ask/stream')` + `EventSource` 式的手写 SSE 解析（`response.body.getReader()` + 按 `\n\n` 切事件），逐字渲染 `token`，收到 `done` 后用 `done.response` 覆盖渲染（**以终态为准，防止流式丢字**）；⑤ 底部状态条显示 intent / route.rule / confidence / degraded / notes。所有文案用「」而不是中文引号。**此时不要引用尚未创建的 DOM 节点 id**，id 命名集中写在文件顶部一个 `const ID = {...}` 里。
loop: false
verify:
  - type: artifact
    path: web
    assert:
      kind: matches-glob
      value: "index.html"
  - type: shell
    command: .venv/Scripts/python.exe -c "import pathlib,re; h=pathlib.Path('web/index.html').read_text(encoding='utf-8'); assert 'http://' not in h.replace('http://127.0.0.1','').replace('http://localhost',''), '前端不得引外部 CDN'; assert 'api/ask/stream' in h; print('ok', len(h))"
gate: auto

- [x] **Step B2: 让 FastAPI 托管前端并加 scripts/smoke_step6.py**
action: 在 `src/server.py` 用 `StaticFiles` 挂载 `web/` 到 `/static`，并加 `GET /` 返回 `web/index.html`（`FileResponse`）—— 此时 B1 已建好该文件，`/` 必须返回它的 HTML 内容。`/api/health` 增报 `frontend: {"available": bool, "path": str}`（`available` 用 `Path.exists()` 真值判断，不要硬编码 True）。加 `scripts/smoke_step6.py`：依次断言 ① `/` 返回 200 且 `content-type` 含 `text/html`；② `/api/health` 的 `frontend.available` 为 True；③ `/api/compare?indicator=营业总收入&codes=600519,000858` 返回 `ok=true` 且 `rows` 长度 2；④ `/api/ask/stream` 首个事件为 `meta`（用 `httpx`/`TestClient` 流式读，不配 Key 时走降级路径）。脚本用 `fastapi.testclient`，不需要起真服务、不花 token，失败时打印实际响应体。
loop: until .venv/Scripts/python.exe scripts/smoke_step6.py 退出码为 0
max_iterations: 3
verify: .venv/Scripts/python.exe scripts/smoke_step6.py
gate: auto

- [x] **Step B3: 引用卡片（可点击、可核对）**
action: 在 `web/index.html` 增加引用卡片区：每条引用展示 `[n] 公司年份 报告类型 P<页码> <章节> (第x/y段)`（直接用后端返回的 `citation` 字段，不在前端重新拼格式）。点击卡片 → 调 `GET /api/citations?thread_id=...` 取该会话引用明细并展开：片段原文、`code/year/page_no/section/part`、以及数值题的 `repo_source`（`表.字段`）。卡片上必须有「回源核对」按钮：点击后用 `window.open` 打开巨潮原文链接（后端在 citation 里给的 `pdf_url` 或 `adjunct_url`；没有该字段时按钮置灰并提示「本条引用未收录原文链接」）。**不得**在前端做任何引用的合法性判断（那是 `verify` 的职责，前端只显示）。
loop: false
verify: .venv/Scripts/python.exe -c "import pathlib; h=pathlib.Path('web/index.html').read_text(encoding='utf-8'); assert '/api/citations' in h and 'citation' in h; print('ok')"
gate: auto

- [x] **Step B4: 对比表 + 趋势图（手绘 SVG，不引图表库）**
action: 在 `web/index.html` 增加「对比分析」页签：输入指标名与 2+ 家公司代码 → 调 `GET /api/compare` → 渲染 ① 对比表（列：公司 / 期次 / 数值 / 单位 / 来源字段；`periods_consistent=false` 时在表头显示告警条）② 趋势图（用返回的 `chart.series` 手绘内联 SVG 折线：横轴期次、纵轴数值、每条序列一种颜色 + 图例，纯 `document.createElementNS` 生成，不引任何图表库）。数值格式统一走一个 `fmtNum()`（千分位 + 保留 2 位），与后端 `unit` 一致。
loop: false
verify: .venv/Scripts/python.exe -c "import pathlib; h=pathlib.Path('web/index.html').read_text(encoding='utf-8'); assert '/api/compare' in h and 'createElementNS' in h and 'periods_consistent' in h; print('ok')"
gate: auto

- [x] **Step B5: HITL 挂起与确认面板**
action: 在 `web/index.html` 增加挂起面板：当 `done.response.hitl.pending` 为 True 时展示 **触发原因 `hitl.reason`**、`verify` 明细（checked/supported/unsupported/detail）、以及已落盘的候选答案，并提供「确认放行 / 驳回」两个按钮 → 调 `POST /api/hitl/{thread_id}/confirm`（body `{"decision":"approve|reject","reviewer":"web","note":"..."}`）后把返回的最终响应替换到对话流。409（无挂起）时显示明确提示而不是静默失败。挂起面板必须显示「这是待确认、不是错误」的措辞。
loop: false
verify: .venv/Scripts/python.exe -c "import pathlib; h=pathlib.Path('web/index.html').read_text(encoding='utf-8'); assert '/confirm' in h and 'hitl' in h; print('ok')"
gate: auto

- [x] **Step B6: 浏览器实测前端五项能力**
action: 本地起服务（PowerShell 里 `$env:FA_HITL_FORCE_REASON=$null; .venv/Scripts/python.exe -m uvicorn src.server:app --port 8000`），用浏览器打开 `http://127.0.0.1:8000/` 逐项实测：① 提一个数值题（「贵州茅台2024年的毛利率是多少」）看到逐字渲染 + 终态答案；② 点引用卡片展开片段并看到回源按钮；③ 对比页签选「营业总收入」+ 600519/000858 看到表格与折线图；④ 提一个超范围问题（「公司食堂的菜谱是什么」）看到拒答文案且无引用；⑤ 停掉服务后以 `$env:FA_HITL_FORCE_REASON='low_confidence'` 重启（PowerShell 语法，勿写成 bash 的 `VAR=x cmd`），提一个问题看到挂起面板并点「确认放行」得到最终答案。把每一步的实际观察记进 `docs/plans/` 同目录的 `2026-09-22-step6-browser-notes.md`（只记观察与失败现象，不写结论性承诺）。
loop: until 五项全部有实测记录
max_iterations: 3
verify:
  type: browser
  url: http://127.0.0.1:8000/
  check: 对话流式渲染、引用卡片可展开、对比表与折线图可见、越界问题显示拒答、HITL 面板可确认
gate: human

## 批次 C — 答案级评估

- [x] **Step C1: 扩金标准到 50~100 题（含答案级期望）**
action: 扩 `eval/golden_qa.jsonl`，新增 ≥20 题，四类各 ≥4 题：① **法规题**（`gt.type="article"`，含 `doc_no`/`article`，如「定期报告披露期限」→ 226 号第三十二条）；② **引用题**（`gt.type="literal"`，`gt.any=[关键术语]`，术语必须是**唯一标识类**（报告编号、公告编号、审计机构全名），禁用人名/机构通称）；③ **拒答题**（`expect_refuse=true`）；④ **数值题**（`gt.type="indicator"`，`value`/`unit` 直接取结构化库的真实值）。每题必须填全既有字段（`qa_id/scene/question/code/year/expect_refuse/gt/markers/expected_pages/verified/note/broad/expected_sections/gt_page`）并新增 `answer_expect` 字段（`{"judge": true|false}`，标该题是否纳入 LLM 判分）。`markers` 一律用实测扫源 PDF 能得到的形式。新增题必须先在 `scripts/build_golden.py` 上跑通（输出里不得有 `✗` 开头的失败行，也不允许 `verified=false` 未解决的题留在集合里）。注意：`markers` 只证明「这页有这几个字」，因此每题 `note` 写明 marker 的选择理由（G-14）。
loop: until 下方 verify 通过（build_golden 无 `✗` 失败行且 eval/golden_qa.jsonl 行数 ≥ 54）
max_iterations: 4
verify: .venv/Scripts/python.exe -c "import subprocess,sys,pathlib; r=subprocess.run([sys.executable,'scripts/build_golden.py'],capture_output=True,text=True); out=r.stdout+r.stderr; print(out[-1200:]); assert r.returncode==0, 'build_golden 非零退出'; assert '✗' not in out, '存在失败/未定位题（✗ 行）'; n=len([l for l in pathlib.Path('eval/golden_qa.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]); assert n>=54, n; print('qa 总数', n)"
gate: auto

- [x] **Step C2: 重算检索级指标并记录数字变化**
action: 按 G-15 固化流程重跑：`scripts/build_golden.py` → `scripts/eval_retrieval.py`。评测集变大后 `eval/report.md` 的所有百分比都会变（分母变了）。把新旧数字并列记进 `CHANGELOG.md` 待写的 0.7.0 段落素材里（先写到 `docs/plans/2026-09-22-step6-metrics-raw.md`，Step E2 再整理进 CHANGELOG），并在报告正文里确认「样本量提醒」是按数据算出来的（不是硬编码）。若某项指标因新题而**下降**，不要改口径或删题去把它抬起来 —— 如实记录并归因。
loop: false
verify: .venv/Scripts/python.exe scripts/eval_retrieval.py
gate: auto

- [x] **Step C3: 复核 9 道 literal 题的 marker 语义（G-14）**
action: 对 `eval/golden_qa.jsonl` 里所有 `gt.type="literal"` 的题，逐条用 `scripts/build_golden.py --grep <marker>` 回原文看命中页的上下文，判断「命中是否回答了这个问题」（G-05 的「普华永道」教训）。发现语义无关的命中就换 marker（优先换唯一标识类）并重算 `expected_pages`，把修正理由写进该题 `note`。复核结论（每条题号 + 判定 + 是否修正）写进 `docs/plans/2026-09-22-step6-metrics-raw.md`。
loop: false
verify: .venv/Scripts/python.exe -c "import json,pathlib; rows=[json.loads(l) for l in pathlib.Path('eval/golden_qa.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]; lit=[r for r in rows if r.get('gt',{}).get('type')=='literal']; assert lit, 'no literal'; assert all(r.get('note') for r in lit), 'literal 题必须有 note 说明 marker 选择理由'; print('literal', len(lit), 'ok')"
gate: auto

- [x] **Step C4: 写答案级判定的失败用例**
action: 新建 `tests/test_eval_metrics.py`，断言待建的 `src/judge.py` 纯函数契约：① `extract_numbers(text)` 复用 `src/numeric.py`，能把「1,741.44 亿元」与「174144069958.25 元」判为同一数值（容差 `max(0.1%, 0.005)`）；② `numeric_hit(answer, gt) -> bool` 对 `{type:indicator,value,unit}` 与 `{type:article,doc_no,article}`、`{type:literal,any:[...]}` 三种 `gt` 分别给判定；③ `parse_judge_json(raw, *, metric) -> dict|None`：模型返回含 markdown 代码围栏、前后废话、或非法 JSON 时，能剥出 JSON；完全解析不出**返回 None 而不是抛异常**（判分失败要能被统计成"未判"，不能把整轮评测带塌）；④ `score_from_judge(parsed, metric) -> float` 把 0~1 的分数夹到 [0,1]，缺失字段返回 None；⑤ `aggregate(scores)` 对 `None` 与有效分分别计数，平均值只按有效分算。用例用构造的假 JSON 文本，不联网。
loop: false
verify: .venv/Scripts/python.exe -c "import subprocess,sys; r=subprocess.run([sys.executable,'-m','pytest','tests/test_eval_metrics.py','-q','--tb=line'],capture_output=True,text=True); out=r.stdout+r.stderr; assert r.returncode!=0, 'RED 校验失败：实现前用例不该通过'; assert ('error' in out.lower() or 'failed' in out.lower()), out[-600:]; print('RED 已确认'); print(out[-400:])"
gate: auto

- [x] **Step C5: 实现 src/judge.py（自实现同口径指标）**
action: 新建 `src/judge.py`，提供与 RAGAS 同名的两个指标口径（模块头注释写明「与 RAGAS 的定义对齐、但自己实现，理由：可复现、零新增重依赖」）：① `faithfulness_prompt(question, answer, contexts) -> list[dict]`（要求模型只输出 JSON `{"claims":[{"claim":...,"supported":true|false}]}`）+ `faithfulness(parsed) -> support 比例`；② `relevancy_prompt(question, answer) -> list[dict]`（输出 `{"score":0~1,"reason":...}`）+ `parse`。两个 prompt 都必须**以 JSON 为唯一输出格式**并在 prompt 里写明「不要输出任何解释文字」。判分调用走 `src/llm.py`（不新建 client）。`judge_answer(question, answer, contexts, *, model=None) -> dict` 汇总两项并返回 `{"faithfulness": float|None, "answer_relevancy": float|None, "judge_model": str, "notes":[...]}`，任何失败只填 `None` + notes，不抛异常。
loop: until .venv/Scripts/python.exe -m pytest tests/test_eval_metrics.py -q 通过
max_iterations: 4
verify: .venv/Scripts/python.exe -m pytest tests/test_eval_metrics.py -q
gate: auto

- [x] **Step C6: 实现 scripts/eval_rag.py 的确定性部分**
action: 新建 `scripts/eval_rag.py`（结构对齐 `scripts/eval_retrieval.py`：同样的 `GOLDEN_PATH` / 报告写出风格 / 样本量提醒按数据算）。先只做**不调模型**的四项：① 数值准确率（走 `run_agent` 拿答案 → `numeric_hit(answer, gt)`；`gt.type=indicator` 的题为分母）；② 引用命中率（引用里的 `code/year/page_no` 是否命中 `expected_pages`）；③ 拒答正确率（`expect_refuse` vs `refused`）；④ 路由命中率（法规题是否走 `compliance`、数值题是否走 `analysis`、引用题是否走 `rag`）。CLI：`--limit N` / `--mode bm25|hybrid` / `--quota 1|2` / `--intent auto|rag|analysis|compliance` / `--json` / `--no-write`。产出写 `eval/report_answer.json`（机器读）。验收：不带 `--judge`（默认关）时全流程不调模型即可跑完并产出 JSON。
loop: until .venv/Scripts/python.exe scripts/eval_rag.py --no-write --limit 8 退出码为 0
max_iterations: 4
verify: .venv/Scripts/python.exe scripts/eval_rag.py --no-write --limit 8
gate: auto

- [x] **Step C7: 接上 LLM 判分并产出答案级报告**
action: 给 `scripts/eval_rag.py` 加 `--judge`（默认关）：只对 `answer_expect.judge=true` 且未拒答的题调 `judge_answer()`，把 `faithfulness` / `answer_relevancy` 按**有效分数**求平均（判分失败的题计为「未判」并单列计数，不进分母 —— 这与 `eval_retrieval.py` 的「拒答正确率」用分桶的思路一致）。写人读报告 `eval/report_answer.md`：头部必须有**生成时间 / 判分模型名 / 评测集题数与分类计数 / 样本量提醒**；正文给出四个数字（faithfulness、answer_relevancy、数值准确率、引用命中率）+ 逐题明细表 + 「已知不足」自动小节。同时在 `scripts/eval_retrieval.py` 的 `eval/report.md` 末尾追加一行指向答案级报告（改脚本本身，保证重跑后仍在）。
loop: until .venv/Scripts/python.exe scripts/eval_rag.py --judge --limit 6 能产出 eval/report_answer.md 且四个数字字段齐全
max_iterations: 4
verify:
  - type: shell
    command: .venv/Scripts/python.exe scripts/eval_rag.py --judge --limit 6
  - type: artifact
    path: eval
    assert:
      kind: matches-glob
      value: "report_answer.md"
gate: human

- [x] **Step C8: G-13 配额 1 vs 2 的答案级判定 + G-06 重排按意图再试**
action: 用刚建好的答案级指标回答两个悬而未决的问题：① **G-13**：跑 `scripts/eval_rag.py --quota 1` 与 `--quota 2` 两轮，比较「引用命中率」与「数值准确率」，并加一项**证据完整性**统计：数值题的答案数值是否能在**被保留的那一块**引用片段原文里找到 —— 若配额 1 的证据完整性明显更低，就维持默认 2，并把这个数字写进不足清单的 G-13；② **G-06**：跑 `--mode hybrid` 与 `RERANK_BACKEND=api --mode hybrid` 两轮，看答案级指标（不是检索级）是否仍为负收益；分意图（数值题 vs 引用题）分别看。结论写进 `docs/plans/2026-09-22-step6-metrics-raw.md`，Step E4 再整理进不足清单。
loop: false
verify:
  - type: shell
    command: .venv/Scripts/python.exe scripts/eval_rag.py --no-write --quota 1 --json
  - type: shell
    command: .venv/Scripts/python.exe scripts/eval_rag.py --no-write --quota 2 --json
gate: auto

## 批次 D — 容器化交付与 VM 实测

- [x] **Step D1: 放开 MySQL 后端依赖并本地验证双后端**
action: ① 在 `requirements.txt` 放开 `langgraph-checkpoint-mysql>=3.0` 与 `PyMySQL[rsa]>=1.1`（保留原有「必须带 `[rsa]`」的注释），新增「部署（VM 部署脚本用）」小节加 `paramiko>=3.4`；② 安装：`.venv/Scripts/python.exe -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/`；③ 本机有原生 MySQL 8.0（3306 在监听），用 `FA_DB_BACKEND=mysql` + `data/db_keys.local.json` 跑 `scripts/init_db.py`，确认业务库与 **MySQL Checkpointer** 都能建表（`src/graph/checkpoint.py` 走 MySQL 分支时需 `autocommit=True` 且要调 `setup()` —— workflow-agent 踩过的坑，本步必须实测而不是照抄）。
loop: until .venv/Scripts/python.exe scripts/init_db.py 在 FA_DB_BACKEND=mysql 下退出码为 0
max_iterations: 4
verify: .venv/Scripts/python.exe -c "import os,subprocess,sys; env=dict(os.environ, FA_DB_BACKEND='mysql'); r=subprocess.run([sys.executable,'scripts/init_db.py'],env=env,capture_output=True,text=True); print(r.stdout[-2000:]); print(r.stderr[-2000:]); sys.exit(r.returncode)"
gate: auto

- [x] **Step D2: 写 seed 导出与载入脚本（后端中立，双容器可复用）**
action: 新建 `scripts/export_seed.py`：把当前语料与业务数据导出到 `seed/` —— ① 复制 `data/{parsed,index,vector,regulation}` → `seed/data/`（index 含 `bm25.pkl` 与 `bm25_regulation.pkl`，vector 含四件套）；② 用 `src/db.py` 读 SQLite 业务库并写 **后端中立** 的 `seed/business.json`（`companies` / `reports` / `financial_indicators` / `golden_qa` 四张表的行）。新建 `scripts/load_seed.py`：反向把 `seed/business.json` 灌进**当前后端**（先 `init_db` 建表，再按表 upsert），SQLite 与 MySQL 走同一份代码（不生成 mysqldump —— 换后端只改 ⚠️ 注释里那一个环境变量）。两个脚本都要幂等（已存在同 `qa_id`/主键的行 upsert 而不是重复插入）并在结束时打印各表行数。新建 `tests/test_seed_db.py` 断言：`load_seed` 在 SQLite 上灌完后四张表行数与 `business.json` 一致，且重复执行行数不变。
loop: until .venv/Scripts/python.exe -m pytest tests/test_seed_db.py -q 通过
max_iterations: 4
verify: .venv/Scripts/python.exe -m pytest tests/test_seed_db.py -q
gate: auto

- [x] **Step D3: 生成 seed 并核对产物**
action: 跑 `scripts/export_seed.py` 生成 `seed/`，核对：① `seed/data/index/bm25.pkl` 与 `seed/data/vector/vectors.npy` 存在且与 `data/` 下的**字节数一致**；② `seed/business.json` 里 `financial_indicators` 行数 = 5 公司 ×22 指标 ×6 期 的实际值数（含中国平安 120，合计 ≥ 640）；③ `seed/data/parsed/` 覆盖 5 家公司 6 个年度。把核对数字写进 `docs/plans/2026-09-22-step6-metrics-raw.md`。
loop: false
verify: .venv/Scripts/python.exe -c "import json,pathlib; b=json.loads(pathlib.Path('seed/business.json').read_text(encoding='utf-8')); n=len(b['financial_indicators']); print('indicators',n); assert n>=640, n; import hashlib; a=pathlib.Path('data/vector/vectors.npy'); c=pathlib.Path('seed/data/vector/vectors.npy'); assert a.stat().st_size==c.stat().st_size; print('seed ok')"
gate: auto

- [x] **Step D4: Dockerfile + .dockerignore**
action: 新建 `Dockerfile`（基线照 workflow-agent 已验证的做法）：`python:3.13-slim`、`WORKDIR /app`、pip 用阿里云源、先 `COPY requirements.txt` 再装依赖（利用层缓存）、再 `COPY src scripts web config seed entrypoint.sh launcher.py README.md /app/`、`COPY data/regulation /app/data/regulation`；**语料 seed 放 `/app/seed`（不放 `/app/data`）** —— compose 给 `/app/data` 挂卷时卷会遮住镜像里的内容，seed 必须留在卷外（workflow-agent 踩过）。`ENV PYTHONUNBUFFERED=1 FA_DB_BACKEND=mysql`、`EXPOSE 8000`、`HEALTHCHECK` 用 `python -c` 打 `/api/health`（slim 里没有 curl，不为此装 apt 包）、`ENTRYPOINT ["/app/entrypoint.sh"]`、`CMD ["uvicorn","src.server:app","--host","0.0.0.0","--port","8000"]`。新建 `.dockerignore`：排除 `.venv/ __pycache__/ .pytest_cache/ .git/ tests/ eval/ docs/ data/raw/ data/parsed/ data/index/ data/vector/ data/db/ *.db *.local.json` —— **但必须放行 `seed/` 与 `data/regulation/`**（`.dockerignore` 与 `.gitignore` 是两套规则，别混用）。
loop: false
verify: .venv/Scripts/python.exe -c "import pathlib; d=pathlib.Path('.dockerignore').read_text(encoding='utf-8'); assert 'seed' in d and 'data/regulation' in d; assert 'llm_keys.local.json' in d or '*.local.json' in d; f=pathlib.Path('Dockerfile').read_text(encoding='utf-8'); assert '/app/seed' in f and 'HEALTHCHECK' in f and '8000' in f; print('ok')"
gate: auto

- [x] **Step D5: entrypoint.sh（等库 → 建表 → 灌 seed → 起服务）**
action: 新建 `entrypoint.sh`（`#!/bin/sh` + `set -e`）：① `cp -rn /app/seed/data/. /app/data/`（`-n` 不覆盖，用户挂卷放了真语料就以卷为准）；② 若 `FA_DB_BACKEND=mysql`，用 `python -c` 的 socket 循环等 `MYSQL_HOST:MYSQL_PORT` 就绪（最多 60s，超时打印明确错误并退出 1 —— **不要**无声继续）；③ `python scripts/init_db.py` 建表 + 同步清单；④ `python scripts/load_seed.py` 灌 seed（幂等，重复启动安全）；⑤ `exec "$@"` 起服务。每一步打印带 `[entrypoint]` 前缀的日志，便于 `docker logs` 定位。
loop: false
verify: .venv/Scripts/python.exe -c "import pathlib; t=pathlib.Path('entrypoint.sh').read_text(encoding='utf-8'); [print('missing',k) or exit(1) for k in [] ]; assert 'set -e' in t and 'load_seed' in t and 'init_db' in t and 'exec \"$@\"' in t and 'cp -rn' in t; print('ok')"
gate: auto

- [x] **Step D6: docker-compose.yml（app + MySQL 8 双容器默认起）**
action: 新建 `docker-compose.yml`：服务 `web-db`（`mysql:8.0`，`command: ["mysqld","--default-time-zone=+08:00","--character-set-server=utf8mb4"]` —— 加时区参数是因为 `TZ` 环境变量会让 MySQL 初始化慢十几倍；环境 `MYSQL_ROOT_PASSWORD/MYSQL_DATABASE/MYSQL_USER/MYSQL_PASSWORD` 全部从 `.env` 读且**给默认值不写真实口令**；`healthcheck` 用 `mysqladmin ping -h 127.0.0.1 -u root -p$$MYSQL_ROOT_PASSWORD`；卷 `fin-research-mysql:/var/lib/mysql` —— 注释写明「**卷只在首次创建时初始化**，改了 `MYSQL_*` 要 `docker compose down -v`」）；服务 `app`（`build: .`，`FA_DB_BACKEND=mysql` + `MYSQL_HOST=web-db` 等，`depends_on: {web-db: {condition: service_healthy}}`，`ports: ["8000:8000"]`，`volumes: [fin-research-data:/app/data]`，`healthcheck` 打 `/api/health`，`restart: unless-stopped`）。同时补 `.env.example` 里的 compose 相关键（不含真实值）。
loop: false
verify: .venv/Scripts/python.exe -c "import pathlib; c=pathlib.Path('docker-compose.yml').read_text(encoding='utf-8'); assert 'default-time-zone=+08:00' in c and 'service_healthy' in c and 'web-db' in c and 'FA_DB_BACKEND' in c; assert '123456' not in c, 'compose 里不得出现真实口令'; print('ok')"
gate: auto

- [x] **Step D7: 搬 scripts/vm_ssh.py + 写 VM 部署验收脚本**
action: 从 `../workflow-agent/scripts/vm_ssh.py` 搬 `scripts/vm_ssh.py`（原样搬，改默认 `VM_HOST=192.168.57.128`、`SKIP_FILES` 加 `db_keys.local.json`、`SKIP_DIRS` 加 `seed` 的可选上传开关）。新建 `scripts/deploy_vm.py`：用同一套 paramiko 连接做端到端验收 —— ① 上传项目到 `/home/<user>/fin-research-agent`（跳过 `.venv`/`data`/`seed` 之外的大目录，但**必须上传 `seed/`**）；② `docker compose up -d --build`（长任务，读超时不设限）；③ 轮询 `http://127.0.0.1:8000/api/health` 最多 180s，断言 `ok=true` 且 `index/vector/regulation/checkpointer` 都 ok、`backend=mysql`；④ 断言 `/api/compare` 返回 2 行、`/api/ask/stream` 首个事件为 `meta`；⑤ 任一断言失败自动 `docker compose logs --tail=120` 并原样打印（**别让失败变成一句"没通过"**）。CLI：`--verify-only` / `--down` / `--keep`。
loop: false
verify: .venv/Scripts/python.exe -c "import pathlib; [__import__('sys').exit('missing '+p) for p in ['scripts/vm_ssh.py','scripts/deploy_vm.py'] if not pathlib.Path(p).exists()]; print('ok')"
gate: auto

- [x] **Step D8: 在 VM 上真跑容器化验收**

> ✅ **已完成（2026-09-23）**：`deploy_vm.py --verify-only` → **EXIT 0**；
> 四条断言全绿（health 的 index/vector/regulation/checkpointer + `/api/compare` 2 行 +
> `/api/ask/stream` 首帧 `meta`）。实测记录见 `2026-09-22-step6-vm-verify.md`，
> 决策链与独立验证见 `2026-09-22-step6-metrics-raw.md` §15。
> 环境侧两处已留痕的偏离：**宿主端口用 8001**（8000 被姊妹项目占用）、
> **基础镜像从 daocloud 拉取后打本地 tag**（VM 的 `daemon.json` 两个加速器域名已 NXDOMAIN）。
> 另新增 `deploy_vm.py --with-env-keys`（默认关闭）用于注入 Key —— 不注 Key 时
> `vector.available` 会按设计降级为 false（纯 BM25）。

> ⚠️ **执行顺序调整（控制器裁定，2026-09-22）**：D4 要求 `Dockerfile` 里 `COPY … launcher.py …`，
> 但 `launcher.py` 到 **D9** 才建 —— 照原序会在 `docker build` 阶段硬失败（`COPY failed: launcher.py: not found`）。
> 故**先执行 D9 把 `launcher.py` 建出来并把它加回 `Dockerfile` 的 COPY，再跑本步**。
> 依据见 `docs/plans/2026-09-22-step6-metrics-raw.md` §12.5。

action: 跑 `scripts/deploy_vm.py`。注意 VM 内存只有 3.8G（可用 1.1G）且上面还有别的负载（Dify）—— 若 MySQL 起不来或 OOM，先 `docker system prune -f` 与停掉无关容器，仍不够就在 compose 里给 MySQL 限内存（`mem_limit`）并如实记录这一约束。验收目标：`/api/health` 的 `ok=true` 且 `index/vector/regulation/checkpointer` 全 ok、`backend=mysql`。把实际输出（health JSON 原文、耗时、镜像大小、遇到的资源问题）写进 `docs/plans/2026-09-22-step6-vm-verify.md`。
loop: until scripts/deploy_vm.py --verify-only 的 health 断言与两个接口断言全部通过
max_iterations: 5
verify: .venv/Scripts/python.exe scripts/deploy_vm.py --verify-only
gate: human

- [x] **Step D9: launcher.py 一键启动**
action: 新建 `launcher.py`（照 workflow-agent 的做法搬改）：`--port`（默认 8000）/ `--host` / `--mode bm25|hybrid` / `--no-browser` / `--check`（只做预检）/ `--docker`（改走 `docker compose up -d` 并等 health）。行为：① 预检 —— venv 存在、`data/index/bm25.pkl` 存在、`data/db/fin_research.db` 或 MySQL 可连（任一缺就给**可操作**的指引，如「先跑 scripts/ingest_all.py」而不是「文件不存在」）；② 用 `urllib` 探 `/api/health`，已在运行就直接开浏览器（不重复起服务）；③ 起 `uvicorn` 子进程，轮询就绪后 `webbrowser.open`。退出时确保子进程被回收（Windows 上注意 `CREATE_NEW_PROCESS_GROUP`）。
loop: until .venv/Scripts/python.exe launcher.py --check 退出码为 0
max_iterations: 3
verify: .venv/Scripts/python.exe launcher.py --check
gate: auto

## 批次 E — 三件套与不足清单收尾

- [x] **Step E1: README 更新到 v0.7.0**
action: 更新 `README.md`：① 标题版本改 v0.7.0（Step 6 已完成）；② 「当前进度」表格把 Step 6 置 ✅；③ 新增 Step 6 章节：SSE 事件协议表（每个事件的字段）、`/api/compare` 的返回结构、多轮指代消解的规则与边界（何时沿用、何时不猜）、前端五项能力与**离线可用（零 CDN）**的取舍、容器化（`docker compose up -d` 一条命令 + seed 在 `/app/seed` 的理由 + 卷只初始化一次的坑）、答案级评估的四个数字与「判分模型 + 时间戳」标注要求；④ 「快速开始」补 13) `launcher.py` 与 14) `docker compose up -d`；⑤ 「手动修改接口约定」表补 `FA_HISTORY_MAX` / `--quota` / `FA_SEED_*` / 判分模型等新键；⑥ 「测试」段落的用例数改成本轮实测值；⑦ 里程碑清单勾选 Step 6 并写清「容器链路在 VM 192.168.57.128 上实测，本机无 Docker」。
loop: false
verify: .venv/Scripts/python.exe -c "import pathlib,re; h=pathlib.Path('README.md').read_text(encoding='utf-8'); assert 'v0.7.0' in h, 'version'; assert 'api/ask/stream' in h and '/api/compare' in h and 'launcher.py' in h and 'docker compose up -d' in h; assert '[x] **Step 6**' in h or '[x] Step 6' in h, 'milestone'; print('ok')"
gate: auto

- [x] **Step E2: CHANGELOG 0.7.0**
action: 在 `CHANGELOG.md` 顶部新增 `## [0.7.0] - 2026-09-22（Step 6：…）`，沿用既有体例（Added / Changed / Fixed·踩过的坑 / 实测结果 / 已知不足）：① **实测结果**段落给出四条真实数字（答案级四个指标、VM 上 health 的原文摘要、容器构建耗时与镜像大小、测试用例数）；② 明确标注「答案级指标与本轮语料绑定，重入库后必须重跑（沿用 G-15）」；③ **实测结果**里把检索级数字的变化与旧值并列（评测集从 34 扩到 N 题，分母变了，所以与 0.6.0 不可直接比）；④ 「踩过的坑」把本轮新踩的逐条写进去（体例同前：现象 + 根因 + 解法）。
loop: false
verify: .venv/Scripts/python.exe -c "import pathlib; h=pathlib.Path('CHANGELOG.md').read_text(encoding='utf-8'); assert '[0.7.0]' in h; assert 'Step 6' in h; print('ok')"
gate: auto

- [x] **Step E3: EXTENSION 增补坑表与扩展点**
action: 更新 `EXTENSION.md`：① 版本行改 v0.7；② 新增两组坑表 —— 「服务化与前端类」（如 SSE 事件顺序/终态覆盖、前端零 CDN、静态挂载与 `FileResponse`、流式与一次性输出必须同形）与「容器化与 seed 类」（`/app/seed` 不能放 `/app/data`、`.dockerignore` 与 `.gitignore` 是两套规则、MySQL 卷只初始化一次、`--default-time-zone=+08:00`、MySQL saver 要 `autocommit` + `setup()`、`PyMySQL[rsa]`、slim 镜像没有 curl、VM 内存只有 3.8G）；③ 新增「答案级评测类」（判分模型与时间戳必须标注、判分失败计为「未判」不进分母、marker 语义复核、语料变更后答案级指标同样要重跑）；④ 扩展点 E6/E7 按本轮结果更新（E6 从「还剩答案级」改为「已完成，见 eval/report_answer.md」）。
loop: false
verify: .venv/Scripts/python.exe -c "import pathlib; h=pathlib.Path('EXTENSION.md').read_text(encoding='utf-8'); assert 'v0.7' in h; assert '容器' in h and '答案级' in h; print('ok')"
gate: auto

- [x] **Step E4: 不足清单更新（新增 F 项 + G 项状态）**
action: 更新 `不足清单与处置方案.md`：① 新增本轮已修项 F-13…（SSE 无流式、无对比接口、多轮指代未做、评测集小、容器化缺失等，每条带证据与守它的用例）；② 更新 G 项状态 —— G-03（评测集已扩到 N 题 + 答案级指标已建）标 ✅；G-13（配额 1 vs 2 的答案级结论）标 ✅ 或保留 🧭 并写清新证据；G-14（9 道 literal 题已复核）标 ✅；G-06（重排按意图的答案级结论）按 C8 实测如实更新；G-11 的鉴权部分**明确标为 ⏸ 不修**并写清代价（演示用、README 已警示、无公网暴露计划）；③ 新增本轮发现但未修的项（如判分成本、SSE 未覆盖 compliance/analysis 流式）；④ 「处置状态总览」表同步。
loop: false
verify: .venv/Scripts/python.exe -c "import pathlib; h=pathlib.Path('不足清单与处置方案.md').read_text(encoding='utf-8'); assert 'F-13' in h; assert 'G-14' in h and 'G-13' in h; assert 'G-11' in h; print('ok')"
gate: auto

- [x] **Step E5: 最终回归与验收核对**

> ✅ **已完成（2026-09-23，控制器直跑）**：`pytest -q` → **369 passed**（≥350）；
> 三条冒烟 `smoke_qa.py --no-llm` / `smoke_step5.py` / `smoke_step6.py` 退出码全 **0**；
> **五项 success_criteria 全部有实测证据**（③ 的 `--docker` 分支与自动开浏览器除外，如实标为「未验证」）。
> 核对结论见 `2026-09-22-step6-vm-verify.md` §9。
> E5 期中的两处落地：**`APP_VERSION` 0.6.0 → 0.7.0**（对齐文档口径，闭环 §7-6）；
> **EXTENSION §3 第 12/14 条**的过期表述（"34 题 / RAGAS 留 Step 6"）已同步。
> 两条偏离已留痕：计划 E1 提到的 `FA_SEED_*` 全仓不存在（实为命令行参数）；EXTENSION 两处过期表述由控制器就地修。
> **`gate: human` ✅ 已放行（2026-09-23 —— 用户验收通过，Step 6 收尾）。**

action: ① 全量 `.venv/Scripts/python.exe -m pytest -q`，断言全绿且用例数 ≥ 350；② 重跑 `scripts/smoke_qa.py --no-llm`、`scripts/smoke_step5.py`、`scripts/smoke_step6.py` 三个冒烟，确认没有回归；③ 逐条核对本 workflow 的 5 条 success_criteria 是否**都有实测证据**（答案级报告存在且四数字齐全 / VM 上 health 实测通过 / launcher 一条命令起服务且前端五项已实测 / pytest 全绿 / 四份文档与实现一致）；④ 任何一条没有证据的，**不要**在文档里写成已完成 —— 改成如实标注「未验证」并说明原因。核对结论写进 `docs/plans/2026-09-22-step6-vm-verify.md` 的末节。
loop: false
verify:
  - type: shell
    command: .venv/Scripts/python.exe -m pytest -q
  - type: artifact
    path: eval
    assert:
      kind: matches-glob
      value: "report_answer.md"
gate: human