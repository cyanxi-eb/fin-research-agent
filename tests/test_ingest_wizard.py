"""数据入库向导 —— 需求单模型与归一器的契约用例（工作流 Step 1 起）。

## 为什么归一器是整条向导的地基

向导的第一步是「自然语言 → 需求单」，而 LLM 的输出**不可直接信任**（设计 D2）：
它可能把「不存在的公司」编造成代码、把「不认识的指标」张冠李戴。
所以归一做成**独立纯函数** `normalize_request`：LLM 只负责"听懂人话"，
认公司/认指标一律走库内既有解析（`resolve_company` /
`config.resolve_indicator` / `config.resolve_ratio`），认不上的**不丢弃、不报错**，
进 `unresolved` 留给人在表单里改 —— 与数值分析子图「宁可拒答不猜」是同一条纪律。

全部用例**不联网**：公司池显式传入（或 synth_db），LLM 用假对象。
"""
from __future__ import annotations

from types import SimpleNamespace

from src import config
from src.ingest.wizard import normalize_request, parse_request  # Step 1 时应 ModuleNotFoundError

# 显式公司池（免建库）：名字形态与 list_companies() 返回一致
POOL = [
    {"code": "600519", "name": "贵州茅台", "market": "sse", "industry": "食品饮料"},
    {"code": "000001", "name": "平安银行", "market": "szse", "industry": "银行"},
]


def _fields(plan):
    """IngestPlan（pydantic）→ dict，断言统一走字段名。"""
    return plan.model_dump() if hasattr(plan, "model_dump") else dict(plan)


# ==================== ① 归一：认得出的收下、认不出的进 unresolved ====================

def test_normalize_resolves_known_and_parks_unknown():
    raw = {
        "companies": ["贵州茅台", "不存在的公司"],
        "indicators": ["营业总收入", "不认识的指标"],
        "ratios": ["毛利率"],
        "years": 5,
    }
    plan = normalize_request(raw, pool=POOL)
    d = _fields(plan)

    assert d["companies"] == [{"code": "600519", "name": "贵州茅台"}]
    # 指标/比率走 config 既有别名归一，标准名回填
    assert d["indicators"] == ["营业总收入"]
    assert d["ratios"] == ["毛利率"]
    # LLM 草稿里的 years 语义 → 需求单的 periods
    assert d["periods"] == 5
    # 归不上的**保序**进 unresolved，绝不静默丢弃
    assert d["unresolved"] == ["不存在的公司", "不认识的指标"]


def test_normalize_accepts_company_code_and_alias():
    """六位代码与指标别名都要能走通既有解析（不另写一套匹配规则）。"""
    raw = {"companies": ["000001"], "indicators": ["营收"], "ratios": ["roe"]}
    plan = normalize_request(raw, pool=POOL)
    d = _fields(plan)
    assert d["companies"] == [{"code": "000001", "name": "平安银行"}]
    assert d["indicators"] and d["indicators"][0] in config.INDICATORS
    assert d["ratios"] and d["ratios"][0] in config.RATIOS
    assert d["unresolved"] == []


# ==================== ② 防呆上限（D6） ====================

def test_caps_clamp_years_and_companies():
    big_pool = [
        {"code": f"60000{i}", "name": f"测试公司{i}", "market": "sse", "industry": "测试"}
        for i in range(6)
    ]
    raw = {"companies": [c["name"] for c in big_pool], "years": 15}
    plan = normalize_request(raw, pool=big_pool)
    d = _fields(plan)

    assert d["periods"] == min(15, config.INGEST_MAX_PERIODS) == 10
    assert len(d["companies"]) == config.INGEST_MAX_COMPANIES == 5


# ==================== ③ 空草稿 = 合法空需求单（LLM 挂时手填的起点） ====================

def test_empty_draft_is_a_valid_empty_plan():
    plan = normalize_request({}, pool=POOL)
    d = _fields(plan)
    assert d["companies"] == []
    assert d["indicators"] == []
    assert d["ratios"] == []
    assert d["unresolved"] == []
    assert d["periods"] is None


def test_none_and_garbage_drafts_do_not_raise():
    """None / 纯文本等乱输入也要归一成空单 —— 归一器是最后一道防呆，不抛异常。"""
    for bad in (None, "帮我把茅台加进来", 42):
        plan = normalize_request(bad, pool=POOL)
        assert _fields(plan)["companies"] == []


# ==================== ④ LLM → 需求单（Step 3） ====================

class FakeLLM:
    """假模型：invoke 返回预置 content 或抛异常，全程不联网、不花 token。"""

    def __init__(self, content="", error=None):
        self.content, self.error = content, error
        self.prompt = None

    def invoke(self, messages):
        self.prompt = messages
        if self.error:
            raise self.error
        return SimpleNamespace(content=self.content)


def test_parse_llm_json_goes_through_normalizer():
    """LLM 回 JSON（带围栏与寒暄）→ 剥壳 → 必过归一器（D2：不直接信任）。"""
    raw = ('好的，需求单如下：```json\n'
           '{"companies": ["贵州茅台", "不存在的公司"], "indicators": ["营业总收入"],'
           ' "ratios": ["毛利率"], "years": 3}\n```')
    res = parse_request("把贵州茅台近3年加进来", llm_model=FakeLLM(raw), pool=POOL)
    plan = res["plan"]
    assert [(c["code"], c["name"]) for c in plan.companies] == [("600519", "贵州茅台")]
    assert plan.indicators == ["营业总收入"]
    assert plan.ratios == ["毛利率"]
    assert plan.periods == 3
    assert "不存在的公司" in plan.unresolved
    # 有未识别项时 note 要点名，让人知道去表单里改什么
    assert "不存在的公司" in res["note"]


def test_parse_llm_failure_returns_empty_plan_with_note():
    """LLM 抛异常 → 空需求单 + note，绝不抛 500（手填表单路径照常可用）。"""
    res = parse_request("加一家公司", llm_model=FakeLLM(error=RuntimeError("超时")), pool=POOL)
    assert res["plan"].companies == []
    assert res["plan"].indicators == []
    assert "LLM 不可用" in res["note"]


def test_parse_llm_bad_json_returns_empty_plan_with_note():
    """LLM 回了废话（剥不出 JSON）→ 同样收敛成空单 + note。"""
    res = parse_request("加一家公司", llm_model=FakeLLM("抱歉我做不到"), pool=POOL)
    assert res["plan"].companies == []
    assert "LLM 不可用" in res["note"]


def test_parse_without_llm_when_not_ready(monkeypatch):
    """LLM 通道未就绪（无 Key）→ 不发起调用，空单 + note。"""
    from src import llm as llm_mod
    monkeypatch.setattr(llm_mod, "is_ready", lambda: (False, "未配置 API Key"))
    res = parse_request("加一家公司", pool=POOL)
    assert res["plan"].companies == []
    assert "LLM 不可用" in res["note"]


# ==================== ⑤ dry-run 预览（Step 4：只抓不写） ====================

def _collect_result(code: str) -> dict:
    """合成 collect_company 返回：与真实形态一致（含补充源行）。"""
    return {
        "code": code,
        "periods": ["2024-12-31", "2023-12-31"],
        "by_period": {
            "2024-12-31": [
                {"code": code, "period": "2024-12-31", "report_type": "年报",
                 "indicator": "营业总收入", "value": 1.0e11, "unit": "元",
                 "source_table": "income", "source_field": "TOTAL_OPERATE_INCOME"},
                {"code": code, "period": "2024-12-31", "report_type": "年报",
                 "indicator": "归母净资产", "value": 9.0e10, "unit": "元",
                 "source_table": "sina_balance", "source_field": "归属于母公司的股东权益合计"},
            ],
            "2023-12-31": [
                {"code": code, "period": "2023-12-31", "report_type": "年报",
                 "indicator": "营业总收入", "value": 9.0e10, "unit": "元",
                 "source_table": "income", "source_field": "TOTAL_OPERATE_INCOME"},
            ],
        },
        "missing": ["毛利率"], "partial": {"2023-12-31": ["归母净资产"]},
        "sources_used": ["income", "sina_balance"], "source_errors": {},
    }


def test_preview_grid_shape_and_supplement_flag(monkeypatch):
    """网格行带值/单位/来源表字段/补充源命中；plan 指标做白名单过滤。"""
    import src.ingest.wizard as wz

    calls: list[tuple] = []

    def fake_collect(code, periods=None, report_type=None):
        calls.append((code, periods))
        return _collect_result(code)

    monkeypatch.setattr(wz, "collect_company", fake_collect)
    plan = normalize_request({"companies": ["贵州茅台"], "indicators": ["营业总收入"],
                              "years": 3}, pool=POOL)
    res = wz.preview(plan)

    assert calls == [("600519", 3)]                 # 只调采集，periods 透传
    comp = res["companies"][0]
    assert comp["error"] is None and comp["code"] == "600519"
    assert comp["periods"] == ["2024-12-31", "2023-12-31"]
    rows = comp["rows"]
    # 白名单：只留 plan.indicators 里的指标
    assert [r["indicator"] for r in rows] == ["营业总收入", "营业总收入"]
    row = rows[0]
    assert row["value"] == 1.0e11 and row["unit"] == "元"
    assert row["source_table"] == "income" and row["source_field"] == "TOTAL_OPERATE_INCOME"
    assert row["is_supplement"] is False


def test_preview_failed_company_marks_error_not_raise(monkeypatch):
    """单公司抓取失败只标 error，另一家照常，不整单失败（D6）。"""
    import src.ingest.wizard as wz

    def flaky(code, periods=None, report_type=None):
        if code == "000001":
            raise RuntimeError("接口超时")
        return _collect_result(code)

    monkeypatch.setattr(wz, "collect_company", flaky)
    plan = normalize_request({"companies": ["贵州茅台", "平安银行"]}, pool=POOL)
    res = wz.preview(plan)
    by_code = {c["code"]: c for c in res["companies"]}
    assert "接口超时" in by_code["000001"]["error"]
    assert by_code["600519"]["error"] is None and by_code["600519"]["rows"]


def test_preview_does_not_write_db(monkeypatch, synth_db):
    """只抓不写的机器证明：preview 前后指标行数相等，且 save_company 一被调就红。"""
    import sqlite3

    import src.ingest.wizard as wz

    def new_company(code, periods=None, report_type=None):
        return {"code": code, "periods": ["2024-12-31"],
                "by_period": {"2024-12-31": [
                    {"code": code, "period": "2024-12-31", "report_type": "年报",
                     "indicator": "营业总收入", "value": 1.0, "unit": "元",
                     "source_table": "income", "source_field": "TOTAL_OPERATE_INCOME"}]},
                "missing": [], "partial": {}, "sources_used": ["income"], "source_errors": {}}

    def must_not_save(result):
        raise AssertionError("preview 调用了 save_company —— 只抓不写被破坏")

    monkeypatch.setattr(wz, "collect_company", new_company)
    monkeypatch.setattr("src.ingest.fetch_eastmoney.save_company", must_not_save)

    with sqlite3.connect(synth_db) as conn:
        before = conn.execute("SELECT COUNT(*) FROM financial_indicators").fetchone()[0]

    plan = normalize_request({"companies": ["贵州茅台"]}, pool=POOL)
    wz.preview(plan)

    with sqlite3.connect(synth_db) as conn:
        after = conn.execute("SELECT COUNT(*) FROM financial_indicators").fetchone()[0]
    assert before == after


def test_preview_resolves_name_only_form_companies(monkeypatch, synth_db):
    """手填表单 companies 只有 name：preview 内部 resolve 成 code 再抓（Step 7 支撑）；
    认不出的公司标 error，不猜代码。"""
    import src.ingest.wizard as wz
    from src.ingest.wizard import IngestPlan

    seen: list[str] = []

    def fake_collect(code, periods=None, report_type=None):
        seen.append(code)
        return {"code": code, "periods": [], "by_period": {}, "missing": [],
                "partial": {}, "sources_used": [], "source_errors": {}}

    monkeypatch.setattr(wz, "collect_company", fake_collect)
    plan = IngestPlan(companies=[{"name": "贵州茅台"}, {"name": "不存在的公司"}])
    res = wz.preview(plan)

    assert seen == ["600519"]                       # 只抓认得出的那家
    by = res["companies"]
    assert by[0]["code"] == "600519" and by[0]["error"] is None
    assert "未识别" in by[1]["error"]


# ==================== ⑦ 库外新公司的 6 位代码通道（Step 8 手测发现） ====================

def test_normalize_accepts_unknown_six_digit_code():
    """向导的存在意义就是往库里加公司 —— 新公司必然不在 companies 表里，
    库内 resolve 必然认不出。6 位纯数字原文是股票代码，直接当抓取代码收下，
    不能把它当"未识别"挡在门外（否则向导自己就死了）。
    注意用例必须用**池外**代码：600036 不在 POOL 也不在 synth_db，才是库外形态。"""
    plan = normalize_request({"companies": ["600036"]}, pool=POOL)
    assert plan.companies == [{"code": "600036", "name": "600036"}]
    assert plan.unresolved == []


def test_normalize_still_parks_unresolvable_names():
    """库外公司的**名字**（非代码）仍然进 unresolved —— 宁可让人改，不可猜代码。
    「招商银行」不在 POOL 里，且不是 6 位数字，必须原地留在 unresolved。"""
    plan = normalize_request({"companies": ["招商银行"]}, pool=POOL)
    assert plan.companies == []
    assert plan.unresolved == ["招商银行"]


def test_preview_resolves_name_that_is_a_code(monkeypatch, synth_db):
    """手填表单/LLM 草稿把 6 位代码放在 name 里：preview 同样能转成抓取代码。
    600036 不在 synth_db 种子库中 —— 只有走"6 位代码直通"才能被 collect 到。"""
    import src.ingest.wizard as wz
    from src.ingest.wizard import IngestPlan

    seen: list[str] = []

    def fake_collect(code, periods=None, report_type=None):
        seen.append(code)
        return {"code": code, "periods": [], "by_period": {}, "missing": [],
                "partial": {}, "sources_used": [], "source_errors": {}}

    monkeypatch.setattr(wz, "collect_company", fake_collect)
    res = wz.preview(IngestPlan(companies=[{"name": "600036"}]))
    assert seen == ["600036"]
    assert res["companies"][0]["code"] == "600036"


# ==================== ⑥ commit 勾选写库 + 审计（Step 5） ====================

def _actor() -> dict:
    return {"sub": "user-1", "username": "alice", "role": "admin"}


def test_commit_writes_selected_rows_and_audits(monkeypatch, synth_db):
    """commit：勾选格走既有 upsert 入库、companies 表补档案、audit_logs 有 data_ingest 行。"""
    import json
    import sqlite3

    import src.ingest.wizard as wz

    def fake_collect(code, periods=None, report_type=None):
        if code == "999999":
            return {"code": code, "periods": ["2024-12-31"],
                    "by_period": {"2024-12-31": [
                        {"code": code, "period": "2024-12-31", "report_type": "年报",
                         "indicator": "营业总收入", "value": 5.0e10, "unit": "元",
                         "source_table": "income", "source_field": "TOTAL_OPERATE_INCOME"}]},
                    "missing": [], "partial": {}, "sources_used": ["income"], "source_errors": {}}
        return _collect_result(code)

    monkeypatch.setattr(wz, "collect_company", fake_collect)
    pool = POOL + [{"code": "999999", "name": "测试新公司", "market": "szse", "industry": "测试"}]
    plan = normalize_request({"companies": ["贵州茅台", "测试新公司"],
                              "indicators": ["营业总收入"]}, pool=pool)
    selected = [("600519", "2024-12-31", "营业总收入"),
                ("999999", "2024-12-31", "营业总收入")]
    res = wz.commit(plan, selected, actor=_actor())

    assert res["rows"] == 2 and res["batch"]
    with sqlite3.connect(synth_db) as conn:
        conn.row_factory = sqlite3.Row
        # 新公司指标已入库且 companies 表补了档案（否则工具层 resolve 不到，问了也白问）
        new_rows = conn.execute(
            "SELECT value FROM financial_indicators WHERE code='999999'").fetchall()
        comp = conn.execute(
            "SELECT name FROM companies WHERE code='999999'").fetchone()
        audits = conn.execute(
            "SELECT actor, detail_json FROM audit_logs WHERE action='data_ingest'").fetchall()
    assert [(r["value"]) for r in new_rows] == [5.0e10]
    assert comp and comp["name"] == "测试新公司"
    assert len(audits) == 1 and audits[0]["actor"] == "user-1"
    detail = json.loads(audits[0]["detail_json"])
    assert detail["batch"] == res["batch"]          # batch 只活在审计里（D5）
    assert detail["rows"] == 2


def test_commit_skips_unselected_cells(monkeypatch, synth_db):
    """未勾选的格不入库：2023 期那格没勾，就不能出现在库里。"""
    import sqlite3

    import src.ingest.wizard as wz

    monkeypatch.setattr(wz, "collect_company",
                        lambda code, periods=None, report_type=None: _collect_result(code))
    plan = normalize_request({"companies": ["贵州茅台"],
                              "indicators": ["营业总收入"]}, pool=POOL)
    wz.commit(plan, [("600519", "2024-12-31", "营业总收入")], actor=_actor())

    with sqlite3.connect(synth_db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM financial_indicators "
            "WHERE code='600519' AND period='2023-12-31' AND indicator='营业总收入'"
        ).fetchone()[0]
    assert n == 0


def test_commit_with_no_selection_is_rejected(monkeypatch, synth_db):
    """没勾任何格 → 直接拒绝（不写库、不写审计），不产生空批次。"""
    import sqlite3

    import src.ingest.wizard as wz

    plan = normalize_request({"companies": ["贵州茅台"]}, pool=POOL)
    res = wz.commit(plan, [], actor=_actor())
    assert res.get("error") and res.get("rows") == 0
    with sqlite3.connect(synth_db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    assert n == 0


# ==================== ⑧ 代码审查修复（Step 10，先 RED 后 GREEN） ====================

def test_commit_does_not_wipe_existing_company_archive(monkeypatch, synth_db):
    """审查发现①：commit 补档案只对**库外**新公司 —— upsert_companies_sql 的
    ON CONFLICT 会用 excluded 覆盖 market/industry/org_id，对库内既有公司
    恒传空串等于把茅台的档案抹掉（管理员重复入库一次就毁一次）。"""
    import sqlite3

    import src.ingest.wizard as wz

    monkeypatch.setattr(wz, "collect_company",
                        lambda code, periods=None, report_type=None: _collect_result(code))
    plan = normalize_request({"companies": ["贵州茅台"],
                              "indicators": ["营业总收入"]}, pool=POOL)
    wz.commit(plan, [("600519", "2024-12-31", "营业总收入")], actor=_actor())

    with sqlite3.connect(synth_db) as conn:
        row = conn.execute(
            "SELECT market, industry FROM companies WHERE code='600519'").fetchone()
    assert tuple(row) == ("sse", "食品饮料"), "入库既有公司不得清空其档案字段"


def test_preview_whitelist_includes_ratios(monkeypatch, synth_db):
    """审查发现②：指标与比率同时点名时，比率行不得被白名单滤掉 ——
    collect 返回的行里「毛利率」这类比率口径是合法指标行（source_table=main），
    白名单只并 indicators 会把它滤掉，用户永远勾不到。"""
    import src.ingest.wizard as wz
    from src.ingest.wizard import IngestPlan

    def fake_collect(code, periods=None, report_type=None):
        return {"code": code, "periods": ["2024-12-31"],
                "by_period": {"2024-12-31": [
                    {"period": "2024-12-31", "indicator": "营业总收入", "value": 1.7e11,
                     "unit": "元", "source_table": "income", "source_field": "TOTAL_OPERATE_INCOME"},
                    {"period": "2024-12-31", "indicator": "毛利率", "value": 91.6,
                     "unit": "%", "source_table": "main", "source_field": "GML"}]},
                "missing": [], "partial": {}, "sources_used": [], "source_errors": {}}

    monkeypatch.setattr(wz, "collect_company", fake_collect)
    res = wz.preview(IngestPlan(companies=[{"code": "600519", "name": "贵州茅台"}],
                                indicators=["营业总收入"], ratios=["毛利率"]))
    rows = res["companies"][0]["rows"]
    assert [r["indicator"] for r in rows] == ["营业总收入", "毛利率"]


def test_resolve_plan_validates_client_code(synth_db):
    """审查发现③：客户端直传的 code 也要过 6 位校验 —— 垃圾串不能原样拼进
    东财 filter。code 非法时回落到 name 解析（解析不出 → code 置空，上层标 error）。"""
    import src.ingest.wizard as wz
    from src.ingest.wizard import IngestPlan

    out = wz._resolve_plan_companies(
        IngestPlan(companies=[{"code": "GARBAGE", "name": "垃圾"},
                              {"code": "600519", "name": "贵州茅台"}]),
        wz._company_pool(None))
    assert out[0]["code"] == "", "垃圾代码不得原样放行"
    assert out[1]["code"] == "600519"
