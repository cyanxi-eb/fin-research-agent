"""引用格式化 —— 全项目唯一的引用样式出处。

金融 RAG 的硬要求：答案里的每个结论都要能指向「哪家公司、哪一年、第几页、哪一节」。
所以引用必须**从 chunk 元数据生成**，不能靠 LLM 自己写（LLM 会编页码）。
"""
from __future__ import annotations


def format_citation(chunk: dict, index: int | None = None) -> str:
    """`[1] 贵州茅台2024年年报 P87 第三节 管理层讨论与分析`

    index 为 None 时不带序号（用于单条展示）。
    """
    prefix = f"[{index}] " if index is not None else ""
    company = chunk.get("company") or chunk.get("code") or "未知公司"
    year = chunk.get("year") or "?"
    rtype = {"annual": "年报", "semi": "半年报", "quarter": "季报"}.get(
        chunk.get("report_type", "annual"), "报告")
    parts = [f"{company}{year}年{rtype}"]
    if chunk.get("page_no") is not None:
        parts.append(f"P{chunk['page_no']}")
    section = chunk.get("section")
    if section:
        parts.append(section)
    if chunk.get("part") and chunk.get("parts_total", 1) > 1:
        parts.append(f"第{chunk['part']}/{chunk['parts_total']}段")
    return prefix + " ".join(parts)


def format_citation_list(chunks: list[dict]) -> list[str]:
    return [format_citation(c, i) for i, c in enumerate(chunks, start=1)]


if __name__ == "__main__":
    demo = {"company": "贵州茅台", "year": 2024, "report_type": "annual",
            "page_no": 87, "section": "管理层讨论与分析", "part": 2, "parts_total": 3}
    print(format_citation(demo, 1))
    print(format_citation({"code": "300750", "year": 2024, "page_no": 12}))
