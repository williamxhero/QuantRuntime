FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ARG RUNTIME_WHEEL=dist/quant_runtime-0.2.3-py3-none-any.whl
COPY ${RUNTIME_WHEEL} /tmp/quant_runtime-0.2.3-py3-none-any.whl
RUN python -m pip install --no-cache-dir --no-deps /tmp/quant_runtime-0.2.3-py3-none-any.whl \
    && rm /tmp/quant_runtime-0.2.3-py3-none-any.whl

USER 65534:65534
