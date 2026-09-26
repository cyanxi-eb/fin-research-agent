"""切分 + 元数据 —— 产出可直接用于检索与溯源的 chunk。

设计取舍（重要，面试会问）：

1. **按页切，不跨页**。常规做法是把全文拼起来再切，但对本项目是错的：
   跨页 chunk 会让「页码」变成一个区间，引用就无法精确到页。
   代价是每页末尾会有稍短的块（可接受）；overlap 只在页内做，不跨页。
2. **表格整块不切**（Step 1 暂无表格；接口已留好 `kind` 字段，
   Step 2 的表格块会以 `kind="table"` 进来，并跳过切分）。
3. **每个 chunk 必须自带完整元数据**，检索结果要能直接拼出
   `[1] 贵州茅台2024年年报 P87 第三节` —— 少一个字段都拼不出来。
4. **章节按块在页内的字符偏移判定**，不是整页一个标签。
   引用里既然写了章节名，那个章节名就必须对该块成立 ——
   出处文字错了比没有出处更糟，因为它是**看起来可核验的错误证据**。

产出 `data/index/chunks/{code}_{year}.json`。
"""
from __future__ import annotations

import json
from pathlib import Path

from src import config

# 由强到弱的分隔符：优先在段落/句子边界切，切不动再降级到硬切
_SEPARATORS: list[str] = ["\n\n", "\n", "。", "；", "！", "？", "，", "、", " ", ""]


def _has_content(text: str) -> bool:
    """判断一段文本里有没有「实词」（中文或字母数字）。

    纯标点碎片（`。`、`、`）不该单独成块：它既没检索价值，
    又会让页尾出现重复分隔符。
    """
    return any("\u4e00" <= ch <= "\u9fff" or ch.isalnum() for ch in text)


def split_text(text: str, size: int, overlap: int) -> list[str]:
    """递归切分：尽量在语义边界断开，并给相邻块加页内 overlap。

    简化版 RecursiveCharacterTextSplitter —— 不引 langchain 只为一个切分函数。
    """
    if len(text) <= size:
        return [text]

    # 丢掉「没内容」的块：按分隔符切时最后一段常为空，会带上一个多余的分隔符
    # （页尾出现「。。」）；纯标点的碎片（连续句号切出来的「。」）同样要丢，
    # 否则会和前一块的 overlap 尾巴拼成 `…分析。。` 这种不存在的文本。
    pieces = [p for p in _split_by_separators(text, size) if _has_content(p)]
    if overlap <= 0 or len(pieces) <= 1:
        return pieces

    out = [pieces[0]]
    for prev, cur in zip(pieces, pieces[1:]):
        if len(cur) + overlap <= size * 1.5:  # 加 overlap 后别膨胀太多
            out.append(prev[-overlap:] + cur)
        else:
            out.append(cur)
    return out


def _split_by_separators(text: str, size: int) -> list[str]:
    for sep in _SEPARATORS:
        if not sep:
            break
        if sep not in text:
            continue
        buf, chunks = "", []
        parts = text.split(sep)
        for pos, seg in enumerate(parts):
            # 分隔符补在段尾；**最后一段后面不能再补**，否则页尾会多出重复分隔符
            # （`……分析。` 的结尾空段会造出一个孤立的「。」，拼上去就是「。。」）
            piece = seg if pos == len(parts) - 1 else seg + sep
            if not _has_content(piece):
                continue
            if len(piece) > size and not buf:
                # 单段本身就超长：降级到更弱的分隔符继续切
                chunks.extend(_split_by_separators(piece, size))
                continue
            if len(buf) + len(piece) <= size:
                buf += piece
            else:
                if buf:
                    chunks.append(buf)
                if len(piece) <= size:
                    buf = piece
                else:
                    buf = ""
                    chunks.extend(_split_by_separators(piece, size))
        if buf:
            chunks.append(buf)
        if chunks and all(len(c) <= size * 1.5 for c in chunks):
            return chunks
    # 兜底：没有任何分隔符可用，硬切
    return [text[i:i + size] for i in range(0, len(text), size)]


def _merge_tiny(pieces: list[str], min_chars: int) -> list[str]:
    """把过短的碎块并回前一块，避免页尾孤字污染索引。"""
    out: list[str] = []
    for p in pieces:
        p = p.strip()
        if not p:
            continue
        if out and len(p) < min_chars:
            out[-1] = out[-1] + p
        else:
            out.append(p)
    return out


# ==================== 节名下沉到 chunk 级 ====================

def locate_offsets(text: str, pieces: list[str], overlap: int) -> list[int]:
    """求每个 piece 在页文本里的**起始字符偏移**（用于按位置判章节）。

    做法是"按顺序查找"而不是让 `split_text` 直接返回偏移：切分器的契约是
    "给字符串、还字符串"，改它要动 `_split_by_separators` 里所有拼接点，
    还会牵动既有用例。查找法是**只增不改**的，而且 piece 恒为原文的子串
    （overlap 前缀也只是上一块的尾巴），命中率很高。

    兜底策略是"沿用上一块的位置"（保证偏移单调不减）：万一定位不到，
    最坏情况只是这一块退回页级章节判断 —— **与改动前的行为一致，不会更差**。
    """
    offsets: list[int] = []
    cursor = 0
    for p in pieces:
        key = p.lstrip()                 # _merge_tiny 会 strip 掉首部空白
        probe = key[:40]                 # 只用开头一小段定位，避开块内被 strip 过的接缝
        start = -1
        if probe:
            # 允许回退 overlap+1：本块开头可能正是上一块尾巴的重叠部分
            start = text.find(probe, max(0, cursor - overlap - 1))
            if start < 0:
                start = text.find(probe, cursor)
        if start < 0:
            start = cursor
        if offsets:
            start = max(start, offsets[-1])   # 单调不减，避免错位后整体乱序
        offsets.append(start)
        cursor = start + max(1, len(key))
    return offsets


def section_at(marks: list[dict], start: int, end: int, fallback: str | None) -> str | None:
    """块的章节 = 块**起始位置**落在哪一节；取"最后一个 start <= offset"的标记。

    `marks` 来自 `parse_pdf` 的 `section_marks`（非空页至少含 offset=0 那条），
    因此 `fallback` 只在完全没有标记时才用得上。

    **补一条**：块首"未归属"（None）但块内出现了节标题时，采纳块内**第一个**节标题。
    实测 002594/2024 P5 的正文首行是页眉残留「2024 年年度报告」（12 字），
    真正的「第一节 重要提示、目录和释义」跟在后面且该页只有一个块 ——
    只看起始位置会判成"未归属"，而这块 97% 的内容都属于第一节。
    注意这里**只对 None 做这种提升**：若块首已有明确章节，就以块首为准，
    否则 601318/2024 P111（页首两行是「股本变动及股东情况」「公司管治」，
    正文讲的是股本变动）会被后半段带成「公司治理」—— 那是把正确的判成错的。
    """
    section = fallback
    for m in marks or []:
        if m.get("offset", 0) <= start:
            section = m.get("section")
        else:
            break
    if section is None:
        for m in marks or []:
            if start <= m.get("offset", 0) < end and m.get("section"):
                return m["section"]
    return section


def chunk_parsed(parsed: dict) -> list[dict]:
    """把 parse_pdf 的产物切成 chunk 列表。

    章节标签**按块在页内的字符偏移**判定（不是整页一个标签）：
    茅台把「第三节」与标题分成两行写在页中段，整页打标签会让该页上半判错节。
    """
    code = parsed["code"]
    company = parsed["company"]
    year = parsed["year"]
    chunks: list[dict] = []

    for page in parsed["pages"]:
        text = (page.get("text") or "").strip()
        if not text:
            continue
        if len(text) <= config.CHUNK_SIZE * 1.2:
            pieces = [text]
        else:
            pieces = _merge_tiny(
                split_text(text, config.CHUNK_SIZE, config.CHUNK_OVERLAP),
                config.MIN_CHUNK_CHARS)

        marks = page.get("section_marks") or []
        offsets = locate_offsets(text, pieces, config.CHUNK_OVERLAP) if marks else []

        for i, piece in enumerate(pieces, start=1):
            if len(piece) < config.MIN_CHUNK_CHARS and len(pieces) > 1:
                continue
            if marks:
                start = offsets[i - 1]
                section = section_at(marks, start, start + len(piece), page.get("section"))
            else:
                section = page.get("section")
            chunks.append({
                "chunk_id": f"{code}-{year}-p{page['page_no']}-{i}",
                "code": code,
                "company": company,
                "year": year,
                "report_type": "annual",
                "section": section,
                "page_no": page["page_no"],
                "part": i,
                "parts_total": len(pieces),
                "kind": "text",          # Step 2 的表格块用 "table"，切分时跳过
                "text": piece,
                "chars": len(piece),
            })
    return chunks


def chunks_path(code: str, year: int) -> Path:
    return config.CHUNKS_DIR / f"{code}_{year}.json"


def save_chunks(chunks: list[dict], code: str, year: int) -> Path:
    p = chunks_path(code, year)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
    return p


def load_all_chunks() -> list[dict]:
    """读 data/index/chunks/ 下所有 chunk 文件，供建索引用。"""
    if not config.CHUNKS_DIR.exists():
        return []
    out: list[dict] = []
    for f in sorted(config.CHUNKS_DIR.glob("*.json")):
        try:
            out.extend(json.loads(f.read_text(encoding="utf-8")))
        except Exception as e:  # 单个文件坏了不该让整次建索失败
            print(f"  [warn] 跳过损坏的 chunk 文件 {f.name}: {e}")
    return out


def build_from_parsed(parsed: dict) -> dict:
    """解析产物 -> chunk 落盘，返回统计。"""
    chunks = chunk_parsed(parsed)
    save_chunks(chunks, parsed["code"], parsed["year"])
    secs: dict[str, int] = {}
    for c in chunks:
        key = c["section"] or "(未识别)"
        secs[key] = secs.get(key, 0) + 1
    return {
        "code": parsed["code"], "year": parsed["year"],
        "pages": parsed["page_count"], "chunks": len(chunks),
        "avg_chars": round(sum(c["chars"] for c in chunks) / len(chunks), 1) if chunks else 0,
        "sections": secs,
    }


if __name__ == "__main__":
    # 自检：python -m src.ingest.chunk
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    demo = "公司实现营业总收入 123,456.78 万元，同比增长 12.34%。" * 40
    pieces = split_text(demo, 200, 30)
    print(f"切分自检：{len(demo)} 字 -> {len(pieces)} 块，长度={[len(p) for p in pieces]}")
    print(f"最长块 {max(len(p) for p in pieces)} 应 <= {int(200 * 1.5)}")
    print(f"无分隔符兜底：{len(split_text('A' * 1000, 300, 0))} 块")
