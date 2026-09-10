FROM xiongyanfei/edge-backend:dev-1331701f

USER root
COPY unilabos /opt/unilabos/unilabos
COPY unilabos_msgs /opt/unilabos/unilabos_msgs
COPY setup.py setup.cfg /opt/unilabos/
RUN /opt/conda/bin/pip install --no-cache-dir \
      opentelemetry-api==1.44.0 \
      opentelemetry-sdk==1.44.0 \
      opentelemetry-exporter-otlp-proto-grpc==1.44.0 \
      opentelemetry-exporter-otlp-proto-http==1.44.0 \
    && chown -R 57439:57439 /opt/unilabos

USER 57439:57439
WORKDIR /data
