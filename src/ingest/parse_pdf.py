"""年报 PDF 按页解析 —— **页码是溯源的地基**，丢了页码这个项目就没有卖点。

产出 `data/parsed/{code}/{year}.json`：
```
{"code","company","year","source_pdf","page_count","parser","empty_pages","pages":[
   {"page_no":1,"section":"重要提示、目录和释义",
    "section_marks":[{"offset":0,"section":"重要提示、目录和释义"}],
    "text":"...","chars":1234}, ...]}
```

三件必须做对的事：
1. **页码与文本严格绑定**：逐页提取、逐页记录，绝不做「全文拼起来再切」——
   那样页码就找不回来了。
2. **章节识别要跳过目录页、且只认页首标题**。年报目录页一次列全所有章节，
   若不做处理，第 2 页就会被判成「财务报告」并把后面全带偏。
   两道闸门见 `_collect_marks`。
3. **章节要精确到"页内位置"**（`section_marks`），不能只给整页一个标签。
   实测茅台把「第三节」与「管理层讨论与分析」分成两行写在页中段，
   整页打标签会把该页上半（属于上一节的续页）算错。见 `_collect_marks` 与 `chunk.py`。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

try:  # PyMuPDF >= 1.24 推荐 `import pymupdf`；老版本只有 `fitz`
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

from src import config

# 章节标题行允许带编号前缀：第X节 / 一、 / （一） / 1.
_NUM_PREFIX = re.compile(
    r"^[\s\u3000]*(?:第[一二三四五六七八九十百]+[节章]|"
    r"[一二三四五六七八九十]+[、.．]|"
    r"[（(][一二三四五六七八九十]+[）)]|\d+[、.．])?\s*"
)

# 章节名之后**只允许**这几种后缀（续页/分册标记）。
#
# 为什么必须白名单而不能是「任意 <=6 字符」：
# 实测中国平安的年报里，跨页页眉是 `公司治理报告`（= 公司治理 + 报告），
# 正文里还有句子碎片 `财务报告的真实性`（= 财务报告 + 的真实性）。
# 用宽松规则它们都会被当成章节标题，其中「公司治理」被一路带到 145 页之后，
# 让整本报告的章节标签全错 —— **宁可不标章节，也不能标错**。
_ALLOWED_SUFFIX = re.compile(
    r"^[\s\u3000]*(?:[（(](?:续|上|下|[一二三四五六七八九十]+)[）)])?[\s\u3000]*$"
)

# 目录页标志：独立成行的「目录」（不匹配「备查文件目录」这类含前后缀的行）
_TOC_LINE = re.compile(r"^目\s*录$")

# 「第X节」锚点：必须**行首**出现。正文交叉引用写作「详见第十节财务报告-九、…」/「参见
# "第十节 财务报告"之…」，都不以「第X节」开头，用行首锚定即可整类挡掉。
_NUM_SECTION_LINE = re.compile(r"^第[一二三四五六七八九十]+节")

# 标题行末尾的目录点线与页码残留（`释义 ...... 12` / `财务报告 .....`）
_LEADER = re.compile(r"[\s.．·…\u2026]+$")
_LEADER_PAGE_NO = re.compile(r"[\s.．·…\u2026]*\d+\s*$")


def _strip_leader(text: str) -> str:
    """去掉标题行末尾的目录点线与页码：`释义 .................... 12` -> `释义`。"""
    s = text.strip()
    for _ in range(2):  # 先剥页码，再剥点线（两种顺序都可能出现）
        s = _LEADER_PAGE_NO.sub("", s)
        s = _LEADER.sub("", s)
    return s.strip()


def _match_heading(text: str) -> str | None:
    """匹配章节名，允许标题行带目录点线与页码残留。"""
    # 先剥再匹配：带点线的标题行常超过 _heading_of 的长度上限，不先剥会漏
    return _heading_of(_strip_leader(text)) or _heading_of(text)


def _heading_of(line: str) -> str | None:
    """判断一行是不是「章节标题行」。要求整行就是章节名（可带编号前缀与续页码）。

    刻意不做 contains 匹配：正文里「详见第三节管理层讨论与分析」这种句子
    会让 contains 匹配产生大量假命中。
    """
    s = line.strip()
    if not s or len(s) > 30:
        return None
    body = _NUM_PREFIX.sub("", s).strip()
    for name in config.REPORT_SECTIONS:
        if body == name:
            return name
        # 章节别名（各家体例不同，见 config.SECTION_ALIASES）
        if body in (config.SECTION_ALIASES.get(name) or []):
            return name
        if body.startswith(name) and _ALLOWED_SUFFIX.match(body[len(name):]):
            return name
    return None


def _line_offsets(text: str) -> list[tuple[int, str]]:
    """返回每个**非空行**的 `(字符偏移, 去首尾空白后的行)`。

    带偏移是「节名下沉到 chunk 级」的前提：只有当块能对上页内位置，
    才能判断它落在某个节标题的哪一侧（见 `_collect_marks`）。
    这里按 `\\n` 切而不是 `str.splitlines()`：上游 `_strip_running_headers`
    就是用 `"\\n".join(...)` 拼回文本的，两者对同一份文本的切法必须一致，
    否则算出来的偏移会整体错位。
    """
    out: list[tuple[int, str]] = []
    pos = 0
    for raw in text.split("\n"):
        s = raw.strip()
        if s:
            out.append((pos + len(raw) - len(raw.lstrip()), s))
        pos += len(raw) + 1          # 行分隔符本身占 1 个字符
    return out


def _page_top_marks(text: str) -> list[tuple[int, str]]:
    """页面前 HEADING_TOP_LINES 个非空行里出现的章节名，带字符偏移（按出现顺序）。"""
    marks: list[tuple[int, str]] = []
    for n, (off, line) in enumerate(_line_offsets(text), start=1):
        if n > config.HEADING_TOP_LINES:
            break
        h = _match_heading(line)
        if h:
            marks.append((off, h))
    return marks


def _page_all_hits(text: str) -> list[str]:
    """整页扫描命中（用于判断目录页，目录页的章节名可能散落在任意行）。"""
    hits: list[str] = []
    for line in text.splitlines():
        if _NUM_SECTION_LINE.match(line.strip()):
            h = _match_heading(line)
            if h:
                hits.append(h)
        else:
            h = _heading_of(line)
            if h:
                hits.append(h)
    return hits


def _is_toc_page(text: str) -> bool:
    """判断某一页是不是目录页。"""
    # 判据一：前 8 个非空行里有独立成行的「目录」。
    # 这是各家年报都有的标志（实测 601318:P2、600519:P3、000858:P3、300750:P4、002594:P6）。
    head = [ln.strip() for ln in text.splitlines() if ln.strip()][:8]
    if any(_TOC_LINE.match(ln) for ln in head):
        return True
    # 判据二：整页命中 >=4 个不同章节名（正文不可能在一页里开四个章节）。
    # 兜底用：个别年报的目录页只有节号没有「目录」二字。
    return len(set(_page_all_hits(text))) >= 4


def _uses_numbered_sections(pages: list[str]) -> bool:
    """判断这份年报是否用「第X节」给一级章节编号（A 股年报基本都用）。"""
    n = 0
    for text in pages:
        for line in text.splitlines():
            if _NUM_SECTION_LINE.match(line.strip()):
                n += 1
                if n >= 5:  # 5 处足以判定，不必扫全本
                    return True
    return False


def _numbered_marks(pages: list[str]) -> list[tuple[int, int, str]]:
    """按「第X节」锚点抽取章节起始标记 `(页下标, 页内字符偏移, 章节名)`。

    节号与标题有两种版式，都要支持：
    - 同行：`第三节 管理层讨论与分析`（五粮液 / 比亚迪 / 宁德时代）
    - 分两行：`第三节` 换行再写 `管理层讨论与分析`（贵州茅台）
    标题行还可能带目录点线与页码残留（`释义 ....................`），由 `_match_heading` 清掉。

    偏移取**锚点行自身**的位置，而不是标题行的位置：`第三节` 这一行本身就是新一节的
    首行（节标题的一部分），把它算进上一节会让上一节末尾多出一条孤零零的「第三节」。
    """
    marks: list[tuple[int, int, str]] = []
    for i, text in enumerate(pages):
        lines = _line_offsets(text)
        for idx, (off, line) in enumerate(lines):
            if not _NUM_SECTION_LINE.match(line):
                continue
            name = _match_heading(line)          # 版式一：同行带标题
            if name is not None:
                marks.append((i, off, name))
                continue
            if idx + 1 < len(lines):             # 版式二：标题在下一行
                name = _match_heading(lines[idx + 1][1])
                if name:
                    marks.append((i, off, name))
            elif i + 1 < len(pages):
                # 节号是页尾最后一行、标题落在下一页页首 —— 该节应从下一页算起
                nxt = _line_offsets(pages[i + 1])
                if nxt:
                    name = _match_heading(nxt[0][1])
                    if name:
                        marks.append((i + 1, nxt[0][0], name))
    return marks


def _pagetop_marks(pages: list[str]) -> list[tuple[int, int, str]]:
    """按「页首标题」抽取章节起始标记（用于没有节号锚点的报告，如中国平安）。"""
    return [(i, off, h) for i, text in enumerate(pages)
            for off, h in _page_top_marks(text)]


def _is_header_noise(lead: str) -> bool:
    """页首到第一个节标题之间的这段文字，是不是"页眉页脚残留"而非正文。

    判据与 `_strip_running_headers` 同源：**含中文或字母才算内容**。
    纯数字/标点（印刷页码「146」、分隔线「—」）是排版残留 ——
    实测 601318/2024 P150 页首是「146」紧接「审计报告」，
    为一个 4 字符的页码补「上一节（公司治理）」标记，会让该页所有
    从 offset=0 起的 chunk 都挂到上一节，引用出处写错章节。
    """
    return not re.search(r"[A-Za-z\u4e00-\u9fff]", lead)


def _collect_marks(pages: list[str]) -> tuple[list[list[dict]], str]:
    """算出每页的章节起始标记，返回 `(每页标记, 判定模式)`。

    `marks[i]` 是**按页内偏移升序**的 `[{"offset", "section"}, ...]`，
    且**每页至少有一条**（`offset=0` 那条表示"本页从哪一节开始"）。
    这样下游（`chunk.py`）不必知道"上一页是什么章节"，只看本页标记就能判出
    任意位置属于哪一节 —— **跨页状态不往下游传**，是避免两边口径漂移的关键。

    为什么记"节起始点"而不是"整页标签"：实测 600519/2024 P7 的
    「第三节 / 管理层讨论与分析」两行出现在**页中段第 32 行**，
    而该页前 31 行是第二节「非经常性损益」表格的续页。
    按页打标签会让这 31 行被算进第三节，引用出处直接错一节。

    **闸门 · 丢弃目录页**。目录页把全部章节名挤在一页，一旦生效后面全错。
    判据见 `_is_toc_page`（独立成行的「目录」/ 一页命中 >=4 个不同章节）。

    **模式选择**——看这份年报有没有「第X节」锚点：
    - 有（A 股绝大多数）：只认「第X节」锚点后的标题。为什么必须这样而不是
      「认得章节名就行」：实测 000858 P37 / 002594 P72 / 300750 P64 的正文中段
      都有独立成行的一个「财务报告」——那是「内部控制自我评价」表格里的单元格标签
      （与「非财务报告重大缺陷数量」成对出现），按名字匹配会凭空切出一个假的
      「财务报告」区间，把真正的「环境和社会责任」等章节全吞掉。
      而节号锚点这类噪声一个都不会误命中（正文引用写作「详见第十节财务报告-…」，
      不以「第X节」开头，会被行首锚定挡掉）。
      **例外是首个锚点之前的封面/「重要提示」/「释义」几页**（茅台 2024 的
      「释义」在 P4，而 P2 只有一行「重要提示」）：这几页用页首标题兜底，
      否则第一章节整段没有标签。兜底范围严格限制在首个锚点之前，
      因此不会把正文中段的噪声（如内控表里的「财务报告」）重新引进来。
    - 没有（港式体例，如 601318 中国平安）：退回「页首标题」。
      该体例每页页首就是当前小节名（实测「财务报表」出现在 96 页页首），可用；
      但必须先剥掉跨页重复的页眉，否则「公司治理报告」这种页眉会一路带偏全书。

    **页首页码不单独成区间**（实测踩过，与页眉清理同一类错误）。每页至少有一条
    `offset=0` 的标记，表示"本页从哪一节开始"；但**如果页首到第一个真标题之间
    只有页码这类噪声**（纯数字/标点，见 `_is_header_noise`），就不该为那几个字符
    补一条"上一节"标记 —— 那会把所有从 offset=0 起的 chunk 推到上一节去。
    实测 601318/2024 P150：页首「146」紧接「审计报告」，审计报告的块被标成
    「公司治理」（上一节），而该页真正的章节是「财务报告」。
    判据与页眉清理同源：**含中文或字母才算内容**。

    宁可某几页/某几段没有章节标签（section=None，下游按「未归属」处理），也不能标错：
    标错的章节会写进引用的**出处文字**里，等于给出可核验的错误证据。
    """
    toc_pages = {i for i in range(min(12, len(pages))) if _is_toc_page(pages[i])}

    if _uses_numbered_sections(pages):
        mode = "节号锚点"
        raw = _numbered_marks(pages)
        first_anchor = min((m[0] for m in raw), default=len(pages))
        raw += [(i, off, h) for i in range(min(first_anchor, len(pages)))
                for off, h in _page_top_marks(pages[i])]
    else:
        mode = "页首标题"
        raw = _pagetop_marks(pages)

    by_page: list[list[dict]] = [[] for _ in pages]
    for i, off, name in raw:
        if i in toc_pages:
            continue
        by_page[i].append({"offset": off, "section": name})

    out: list[list[dict]] = []
    current: str | None = None
    for i in range(len(pages)):
        # 稳定排序（只按偏移）：同一位置有多条时保留原始先后，"最后一处标题生效"
        page_marks = sorted(by_page[i], key=lambda m: m["offset"])
        if page_marks:
            if page_marks[0]["offset"] > 0 and not _is_header_noise(
                    pages[i][:page_marks[0]["offset"]]):
                # 页首到第一个节标题之间仍是上一节 —— 显式补一条，让本页自解释
                page_marks.insert(0, {"offset": 0, "section": current})
            elif page_marks[0]["offset"] > 0:
                # 页首到第一个节标题之间**只有页码这类噪声**（如印刷页码「146」）：
                # 那不是"上一节的正文"，为一个 4 字符的页码补一条「上一节」标记，
                # 会让所有从 offset=0 起的 chunk 都落到上一节去 —— 实测 601318 P150
                # （页首「146」+ 紧接「审计报告」）的审计报告块被标成「公司治理」。
                # 所以把这条真实标题**提前到 offset=0**，让整页归它。
                page_marks[0]["offset"] = 0
            current = page_marks[-1]["section"]   # 同页多处标题时，以最后一处为准
        else:
            # 本页没有节起始点 → 整页继承上一节。
            # **这里刻意不判"页是否为空"**：章节归属是**按页推进**的（封面、插图页、
            # 被当作页眉清空的页都会落到这），加上"空页不给标记"会让后面所有页
            # 都丢掉章节标签 —— 那是把一份报告的大半章节归属全删了。
            # 空页本来也不产生 chunk（`chunk_parsed` 会跳过），留着标记无害。
            page_marks = [{"offset": 0, "section": current}]
        out.append(page_marks)
    return out, mode


def _detect_sections(pages: list[str]) -> tuple[list[str | None], str]:
    """每页的章节标签（`_collect_marks` 的页级投影），返回 `(每页章节, 判定模式)`。

    页级标签只用于自查（`section_pages` 的页码区间）与"整页落在哪一节"的粗判；
    **写进引用的章节来自 chunk 级判定**（见 `chunk.py`），两者同源、不会漂移。
    """
    marks, mode = _collect_marks(pages)
    return [m[-1]["section"] if m else None for m in marks], mode


def _strip_running_headers(pages: list[str]) -> tuple[list[str], list[str]]:
    """清理跨页重复的页眉页脚（如「贵州茅台2024年年度报告 第37页」）。

    判据：归一化后的行在 >= RUNNING_HEADER_RATIO 比例的页面上出现，且长度 <= 40。
    要归一化是因为页码会变——不归一化就匹配不到「同一行」。

    ⚠️ **纯数字行绝不能参与"重复"判定**（实测踩过，且后果极严重）：
    归一化会把数字换成 `#`，于是金额行 `127,187,293,298.17` 变成 `#,#,#,#.#`、
    `248,513,280.00` 变成 `#,#,#.#`。资产负债表上有几十行金额，各页的**归一化形状**
    又只有那么几种，于是这些"形状"在几十页上重复出现，被判成页眉 → **整行金额被删光**。
    症状是报表主表只剩科目名、没有金额（Step 5 实测：600519 年报 50 页之后有 40 页
    "零金额"），而 RAG 因此无法回答任何"某科目金额是多少"的问题 ——
    这类问题占真实提问的很大比例。

    为什么加"必须含文字"这一条就够：页眉页脚是**文字**（公司名、报告期、页码标记），
    纯数字行是**数据**。用"有没有文字"区分两者，比维护一份"哪些行是页眉"的白名单
    稳得多（白名单永远会漏，漏掉一个就是一次静默的数据丢失）。
    """
    def norm(ln: str) -> str:
        return re.sub(r"\d+", "#", ln.strip())

    def has_text(ln: str) -> bool:
        """含中文或字母 → 可能是页眉页脚；纯数字/标点 → 一定是数据。"""
        return bool(re.search(r"[A-Za-z\u4e00-\u9fff]", ln))

    counter: Counter[str] = Counter()
    for t in pages:
        for ln in {norm(x) for x in t.splitlines() if x.strip() and has_text(x)}:
            counter[ln] += 1

    threshold = max(3, int(len(pages) * config.RUNNING_HEADER_RATIO))
    repeated = {ln for ln, c in counter.items() if c >= threshold and len(ln) <= 40}
    if not repeated:
        return pages, []

    cleaned = []
    for t in pages:
        keep = [ln for ln in t.splitlines()
                if not (has_text(ln) and norm(ln) in repeated)]
        cleaned.append("\n".join(keep).strip())
    return cleaned, sorted(repeated)


def _normalize(text: str) -> str:
    """统一空白：全角空格、连续空行、行尾空格。中文文本别做别的加工。"""
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_pdf(pdf_path: Path, code: str, company: str, year: int,
              out_path: Path | None = None) -> dict:
    """解析一份年报 PDF，返回结果 dict（同时落盘）。"""
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF 不存在：{pdf_path}")

    doc = fitz.open(pdf_path)
    try:
        raw_pages = [doc[i].get_text("text") or "" for i in range(doc.page_count)]
        page_count = doc.page_count
    finally:
        doc.close()

    pages, dropped_headers = _strip_running_headers([_normalize(p) for p in raw_pages])
    marks, section_mode = _collect_marks(pages)

    out_pages: list[dict] = []
    empty_pages = 0
    for i, text in enumerate(pages, start=1):
        if len(text) < 20:
            empty_pages += 1  # 多为扫描页/纯图页，需要 OCR 兜底时据此判断
        page_marks = marks[i - 1]
        out_pages.append({
            "page_no": i,
            # 页级标签 = 本页最后一处切换点，即"本页大部分内容属于哪一节"（仅用于自查）
            "section": page_marks[-1]["section"] if page_marks else None,
            # chunk 级溯源用：本页内的章节起始点（含页首那条 offset=0），
            # 让下游能按块的**页内字符偏移**判章节，而不是整页一个标签
            "section_marks": page_marks,
            "text": text,
            "chars": len(text),
        })

    result = {
        "code": code,
        "company": company,
        "year": year,
        "source_pdf": str(pdf_path),
        "page_count": page_count,
        "empty_pages": empty_pages,
        "parser": f"pymupdf {getattr(fitz, '__version__', '?')}",
        "section_mode": section_mode,
        "dropped_headers": dropped_headers,
        "section_pages": _section_span(out_pages),
        "pages": out_pages,
    }
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def _section_span(pages: list[dict]) -> list[dict]:
    """章节 -> 页码区间，方便自查章节识别准不准（也便于人工核对）。"""
    spans: list[dict] = []
    for p in pages:
        s = p["section"]
        if s is None:
            continue
        if spans and spans[-1]["section"] == s:
            spans[-1]["end_page"] = p["page_no"]
        else:
            spans.append({"section": s, "start_page": p["page_no"], "end_page": p["page_no"]})
    return spans


def parsed_path(code: str, year: int) -> Path:
    return config.PARSED_DIR / code / f"{year}.json"


def parse_manifest_entry(code: str, year: int) -> dict | None:
    """从 raw 的 manifest 里取公司名/本地路径，省得调用方自己拼。"""
    from src.ingest.fetch_cninfo import load_manifest

    m = load_manifest(code)
    entry = m.get(str(year))
    if not entry:
        return None
    return {"name": m.get("_name") or code, "pdf": entry.get("local"),
            "title": entry.get("title"), "url": entry.get("url")}


if __name__ == "__main__":
    # 自检：python -m src.ingest.parse_pdf <code> <year>   （需先跑过 fetch）
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    if len(_sys.argv) < 3:
        print("用法：python -m src.ingest.parse_pdf <code> <year>")
        raise SystemExit(2)
    _code, _year = _sys.argv[1], int(_sys.argv[2])
    meta = parse_manifest_entry(_code, _year)
    if not meta:
        print(f"raw manifest 里没有 {_code}/{_year}，先跑 fetch_cninfo")
        raise SystemExit(1)
    r = parse_pdf(Path(meta["pdf"]), _code, meta["name"], _year, parsed_path(_code, _year))
    print(f"页数={r['page_count']} 空页={r['empty_pages']} 解析器={r['parser']}")
    print(f"丢弃的页眉页脚样式 {len(r['dropped_headers'])} 种：{r['dropped_headers'][:3]}")
    print("章节页码区间：")
    for s in r["section_pages"]:
        print(f"  {s['section']:<20} P{s['start_page']}-{s['end_page']}")
