# MCP Hub - MCPプロキシ + 管理Web UI
# マルチステージビルド: builder → runtime
# 複数MCPサーバーを集約し、統合エンドポイントとして再公開

# ============================================================
# Stage 1: builder — ビルド依存 + アプリインストール
# ============================================================
FROM python:3.12-slim AS builder

ENV APP_HOME=/opt/mcp-hub \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    TZ=UTC

WORKDIR ${APP_HOME}

# Node.js LTS + ビルドツール (native addon ビルド用)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    curl git wget ca-certificates gnupg \
    && mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | \
    gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
    > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    nodejs gcc g++ make \
    && rm -rf /var/lib/apt/lists/*

# Python: uv パッケージマネージャ
RUN pip install --no-cache-dir uv

# 依存関係定義を先にコピー（レイヤーキャッシュ効率化）
COPY pyproject.toml ${APP_HOME}/

# 仮想環境作成 + setuptools インストール
RUN uv venv /opt/venv && \
    . /opt/venv/bin/activate && \
    uv pip install setuptools

# 依存パッケージのみ先にインストール（ソース変更の影響を受けない）
RUN . /opt/venv/bin/activate && \
    uv pip install $(python3 -c "import tomllib; \
    t = tomllib.load(open('pyproject.toml','rb')); \
    print(*t['project']['dependencies'], *t['project']['optional-dependencies']['embeddings'])")

# アプリケーションコード + 設定ファイル
COPY src/ ${APP_HOME}/src/
COPY hub.config.json ${APP_HOME}/hub.config.json

# アプリ本体をインストール（依存は既にあるので高速）
RUN . /opt/venv/bin/activate && uv pip install -e .

# ============================================================
# Stage 2: runtime — 最小実行イメージ
# ============================================================
FROM python:3.12-slim

ENV APP_HOME=/opt/mcp-hub \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    TZ=UTC \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR ${APP_HOME}

# Node.js ランタイムのみ (MCP サーバー実行用、ビルドツールは含めない)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    curl ca-certificates wget gnupg \
    && mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | \
    gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
    > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

# Puppeteer 用 Chromium (headless browser automation)
ENV PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    chromium chromium-sandbox \
    && rm -rf /var/lib/apt/lists/*

# gosu for PUID/PGID privilege dropping
RUN wget -O /usr/local/bin/gosu "https://github.com/tianon/gosu/releases/download/1.17/gosu-$(dpkg --print-architecture)" \
    && chmod +x /usr/local/bin/gosu

# uv/uvx — Python MCP server runner (e.g., mcp-server-fetch)
RUN pip install --no-cache-dir uv

# builder から仮想環境とアプリケーションと設定ファイルをコピー
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder ${APP_HOME}/src ${APP_HOME}/src
COPY --from=builder ${APP_HOME}/hub.config.json ${APP_HOME}/hub.config.json

# データディレクトリ
RUN mkdir -p ${APP_HOME}/data

# 非rootユーザー
RUN useradd --create-home --shell /bin/bash mcp-hub && \
    chown -R mcp-hub:mcp-hub ${APP_HOME}

# Entrypoint handles privilege dropping (gosu) based on PUID/PGID
# Container starts as root for usermod/groupmod, then drops to mcp-hub
COPY docker-entrypoint.sh /usr/local/bin/
ENTRYPOINT ["docker-entrypoint.sh"]

EXPOSE 26263

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:26263/admin/api/health || exit 1

CMD ["python", "-m", "mcp_hub.main"]
