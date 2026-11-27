FROM mambaorg/micromamba:2.3.3

USER root

RUN micromamba install -y -n base \
      -c uni-lab \
      -c robostack-staging \
      -c conda-forge \
      unilabos-env=0.11.4 \
    && micromamba clean --all --yes

COPY setup.py setup.cfg /opt/unilabos/
COPY unilabos /opt/unilabos/unilabos
COPY unilabos_msgs /opt/unilabos/unilabos_msgs
COPY --from=szlab . /workspace

RUN micromamba run -n base python -m pip install --no-cache-dir /opt/unilabos /workspace \
    && micromamba run -n base uv pip install --python /opt/conda/bin/python \
      -r /opt/unilabos/unilabos/utils/requirements.txt

WORKDIR /workspace
ENV MAMBA_DOCKERFILE_ACTIVATE=1
CMD ["unilab", "--help"]
