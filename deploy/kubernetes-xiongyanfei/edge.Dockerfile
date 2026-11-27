FROM node:22-bookworm-slim AS frontend-builder

WORKDIR /build/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build


FROM ubuntu:24.04

ARG TARGETARCH
ARG MINIFORGE_VERSION=25.3.1-0

ENV DEBIAN_FRONTEND=noninteractive \
    CONDA_DIR=/opt/conda \
    PATH=/opt/conda/envs/unilab/bin:/opt/conda/condabin:/opt/conda/bin:${PATH}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      bash bzip2 ca-certificates curl git tini \
    && case "${TARGETARCH}" in \
         amd64) miniforge_arch=x86_64 ;; \
         arm64) miniforge_arch=aarch64 ;; \
         *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL \
      "https://github.com/conda-forge/miniforge/releases/download/${MINIFORGE_VERSION}/Miniforge3-${MINIFORGE_VERSION}-Linux-${miniforge_arch}.sh" \
      -o /tmp/miniforge.sh \
    && bash /tmp/miniforge.sh -b -p "${CONDA_DIR}" \
    && rm /tmp/miniforge.sh \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Edge 的生产运行环境只由发布频道解算；环境名和频道顺序与本地部署文档一致。
RUN mamba create -y -n unilab \
      uni-lab::unilabos \
      -c robostack-staging \
      -c conda-forge \
    && mamba clean --all --yes

COPY setup.py setup.cfg /opt/unilabos/
COPY unilabos /opt/unilabos/unilabos
COPY unilabos_msgs /opt/unilabos/unilabos_msgs

# 前端必须由本次镜像构建产出，避免镜像携带工作区里残留的旧静态文件。
COPY --from=frontend-builder /build/unilabos/app/web/static/console /opt/unilabos/unilabos/app/web/static/console

# 用当前提交覆盖频道包中的 Python 业务代码，但不重新解算其运行依赖。
RUN /opt/conda/envs/unilab/bin/python -m pip install --no-cache-dir --no-deps /opt/unilabos \
    && /opt/conda/envs/unilab/bin/python -m pip install --no-cache-dir \
      opentelemetry-api==1.44.0 \
      opentelemetry-sdk==1.44.0 \
      opentelemetry-exporter-otlp-proto-grpc==1.44.0 \
      opentelemetry-exporter-otlp-proto-http==1.44.0 \
    && groupadd --gid 57439 unilab \
    && useradd --uid 57439 --gid 57439 --create-home --shell /bin/bash unilab \
    && mkdir -p /data \
    && chown -R 57439:57439 /data /opt/unilabos

USER 57439:57439
WORKDIR /data
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["unilab", "--help"]
