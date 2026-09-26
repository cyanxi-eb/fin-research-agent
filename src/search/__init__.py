"""网络搜索兜底（可插拔通道 + 交叉验证 + 独立语料区）。

见各模块的说明：
- `provider.py`   搜索通道（默认免 Key 的 bing；可选 ddg / tavily；`none` 显式禁用）
- `fetch.py`      网页正文抓取（走 `src/net.py` 的薄封装）
- `crossvalidate.py` 按域名去重的确定性交叉验证
- `web_corpus.py` 网络语料的独立落盘与独立 BM25 索引（**绝不并入年报索引**）
"""