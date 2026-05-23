# Common Egg Docker runtime image.
#
# This image is used directly for ephemeral sandboxed tool calls and as the
# filesystem/toolchain base for the persistent REPL session image.  Keep shared
# developer tools here so Docker sandboxing and Docker REPLs do not drift.

ARG GO_VERSION=1.22.12

FROM golang:${GO_VERSION}-bookworm AS go-toolchain

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    EGG_BRIDGE_DIR=/egg-bridge \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    GOROOT=/usr/local/go \
    GOPATH=/tmp/go \
    ELAN_HOME=/opt/elan \
    PATH=/usr/local/go/bin:/opt/elan/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

COPY --from=go-toolchain /usr/local/go /usr/local/go

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    jq \
    make \
    nodejs \
    npm \
    patch \
    procps \
    ripgrep \
    sqlite3 \
    unzip \
    w3m \
    && rm -rf /var/lib/apt/lists/*

# Lean projects use the checked-in lean-toolchain file; installing elan lets
# `lake`/`lean` pick the right toolchain while the project and its .lake cache
# stay mounted from the host workspace.  Symlink the common Lean commands into
# /usr/local/bin because login shells reset PATH on some bases.
RUN curl -fsSL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh \
    | sh -s -- -y --default-toolchain none \
    && elan toolchain install leanprover/lean4:v4.29.1 \
    && elan default leanprover/lean4:v4.29.1 \
    && ln -sf /opt/elan/bin/elan /usr/local/bin/elan \
    && ln -sf /opt/elan/bin/lake /usr/local/bin/lake \
    && ln -sf /opt/elan/bin/lean /usr/local/bin/lean \
    && ln -sf /opt/elan/bin/leanc /usr/local/bin/leanc \
    && ln -sf /opt/elan/bin/leanchecker /usr/local/bin/leanchecker \
    && chmod -R a+rX /opt/elan

COPY bin/applypatch /usr/bin/applypatch
COPY bin/apply_patch.py /usr/bin/apply_patch.py
COPY bin/apply_patch /usr/bin/apply_patch

RUN pip install \
    aiohttp \
    build \
    coverage[toml] \
    fastapi \
    h2 \
    httpx \
    hypercorn \
    numpy \
    pandas \
    pdoc3 \
    prompt_toolkit \
    pyflakes \
    pytest \
    pytest-asyncio \
    python-multipart \
    pydantic \
    readchar \
    requests \
    rich \
    scipy \
    sse-starlette \
    tox \
    trafilatura \
    uvloop \
    websockets

WORKDIR /workspace
CMD ["bash"]
