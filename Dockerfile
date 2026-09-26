# 金融财报分析 Agent — 私有化交付镜像（Step 6 容器化）
# 构建: docker build -t fin-research-agent .
# 运行: docker compose up -d（推荐，见 docker-compose.yml）
FROM python:3.13-slim

WORKDIR /app

# 国内构建加速：默认走阿里云镜像。
# 说明：清华源(pypi.tuna.tsinghua.edu.cn)在部分网络环境会返回 403，故不作默认。
# 海外/官方源：docker compose build --build-arg PIP_INDEX_URL=https://pypi.org/simple/
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

# 先装依赖（利用镜像层缓存：requirements.txt 不变则这一层不重装）
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt

# 再拷代码与运行时资源
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY config/ ./config/
COPY web/ ./web/
COPY README.md ./README.md
# 一键启动器（容器外/裸机用；容器内 CMD 直接跑 uvicorn，故运行不依赖它）
COPY launcher.py ./launcher.py

# 语料 seed 烤到 /app/seed —— **不能放 /app/data**：
# compose 把数据卷挂在 /app/data 会把镜像里该目录整层遮住，seed 必须留在卷外，
# 由 entrypoint.sh 首次启动时 `cp -rn` 复制进数据卷（-n 不覆盖用户挂卷放的真语料）。
COPY seed/ /app/seed/

# 法规原始件（entrypoint 复制 seed 时已含 regulation；这里保留独立路径，便于排查与单独替换）
COPY data/regulation /app/data/regulation

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# 默认走 MySQL 后端（compose 里由 web-db 提供）；本机裸跑可覆盖为 sqlite
ENV PYTHONUNBUFFERED=1 FA_DB_BACKEND=mysql
EXPOSE 8000

# slim 镜像里没有 curl，健康检查用 python 标准库打 /api/health（不为此装 apt 包）
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"

# 建表/灌 seed 放到启动时（见 entrypoint.sh）：MySQL 后端下 build 阶段还没有数据库，
# 且数据卷是运行期才挂上的。
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8000"]