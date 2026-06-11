ARG BASE_IMAGE=public.ecr.aws/ubuntu/ubuntu:22.04

FROM node:22-alpine AS frontend-builder
WORKDIR /ui
COPY unilabos_local_ui/package*.json ./
RUN npm ci
COPY unilabos_local_ui/ ./
RUN npm run build

FROM ${BASE_IMAGE}

ARG MINIFORGE_URL=""

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/conda/envs/unilab/bin:/opt/conda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONUNBUFFERED=1

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        bzip2 \
        build-essential \
        ca-certificates \
        curl \
        git \
        libglib2.0-0 \
        libgl1 \
        tini \
        udev \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    arch="$(uname -m)"; \
    installer_url="${MINIFORGE_URL:-https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-${arch}.sh}"; \
    echo "Downloading Miniforge from ${installer_url}"; \
    curl --fail --location --show-error \
        --connect-timeout 20 \
        --max-time 600 \
        --retry 5 \
        --retry-delay 5 \
        --output /tmp/miniforge.sh \
        "${installer_url}"; \
    bash /tmp/miniforge.sh -b -p /opt/conda; \
    rm -f /tmp/miniforge.sh; \
    conda clean -a -y; \
    mamba --version

RUN mamba create -y -n unilab \
        -c uni-lab \
        -c robostack-staging \
        -c conda-forge \
        python=3.11.14 \
        uni-lab::unilabos-env \
        pip \
        uv \
    && mamba clean -a -y

WORKDIR /workspace

COPY setup.py setup.cfg ./
RUN mkdir -p unilabos/utils
COPY unilabos/utils/requirements.txt ./unilabos/utils/requirements.txt

RUN python -m pip install --no-cache-dir --upgrade pip \
    && uv pip install --python /opt/conda/envs/unilab/bin/python --no-cache -r unilabos/utils/requirements.txt

COPY . .
COPY --from=frontend-builder /ui/dist ./unilabos_local_ui/dist

RUN python -m pip install --no-cache-dir . \
    && mamba clean -a -y \
    && rm -rf /root/.cache /tmp/* \
    && find /opt/conda -type f -name '*.pyc' -delete \
    && find /opt/conda -type d -name '__pycache__' -prune -exec rm -rf '{}' +

RUN printf '%s\n' \
        '#!/usr/bin/env bash' \
        'set -e' \
        '' \
        'source /opt/conda/etc/profile.d/conda.sh' \
        'conda activate unilab' \
        '' \
        'exec "$@"' \
        > /usr/local/bin/docker-entrypoint.sh \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "scripts.workflow_ui", "--host", "0.0.0.0", "--port", "8000"]
