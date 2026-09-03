FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ARG RUNTIME_WHEEL=dist/quant_runtime-0.2.3-py3-none-any.whl
ARG DEPENDENCY_LOCK_ID=sha256:5690bb318285226c8cd3a06ed91bdff9fd82fa05156f6db73ea923708d64be22
LABEL org.quant-runtime.dependency-lock=${DEPENDENCY_LOCK_ID}

COPY containers/sandbox-requirements.lock /tmp/sandbox-requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/sandbox-requirements.lock \
    && rm /tmp/sandbox-requirements.lock
COPY ${RUNTIME_WHEEL} /tmp/quant_runtime-0.2.3-py3-none-any.whl
RUN python -m pip install --no-cache-dir --no-deps /tmp/quant_runtime-0.2.3-py3-none-any.whl \
    && rm /tmp/quant_runtime-0.2.3-py3-none-any.whl

USER 65534:65534
