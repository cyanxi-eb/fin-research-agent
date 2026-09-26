# 归档的探针脚本

这些脚本是**一次性排查**用的，结论已经落进口径表注释、`EXTENSION.md` 坑表、
`tests/` 用例与 `scripts/probe_app_sources.py`，留在 `scripts/` 根目录只会增加噪音。

**什么时候来这里翻**：想复原"当初是怎么定位到某个字段/某个结论的"。
每个脚本的 docstring 里都写了当时的假设与玩法。

| 脚本 | 当时在查什么 | 结论去哪了 |
|---|---|---|
| `probe_sina_finance_api.py` | 新浪财报接口的表代号到底是哪个（试 `zcfz` 返回 `data:null` 才试出 `fzb`） | `config.DATA_SOURCES[*]["source"]` 注释 + `tests/test_fetch_sina.py::test_balance_uses_fzb_table_code` |
| `probe_sina_fields.py` | 新浪报表里「归属于母公司的股东权益合计」「营业支出」这些中文项目名到底叫什么 | `config.INDICATORS[*]["sources"]` + `tests/test_fetch_sina.py::test_chinese_field_names_are_used_directly_as_indicator_sources` |
| `probe_final_fields.py` | 收尾核对：三个源的值能不能互相对上（恒等式） | `scripts/verify_step2_db.py` §4.5 + `tests/test_collect_company.py` |

替代它们的是 **`scripts/probe_app_sources.py`**：一条命令打印某公司在**全部源**上的
收入/成本/权益类字段与值（东财 F10 ×3 + 数据中心 ×2 + 新浪 ×2 + 同花顺），
覆盖上面三个脚本的能力，且不用改代码就能换公司。

> 注意：这些脚本依赖 `src.config` 的旧接口名（`EM_SOURCES` 已改名为 `DATA_SOURCES`），
> 直接跑会因为 `AttributeError` 失败。要用的话先改名字 —— 归档就是为了不再维护它们。
