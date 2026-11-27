# Default sandbox image for local Python repos (`mergewright run --repo ...`) and for the
# Docker guard tests. Build once:
#   docker build -t mergewright-python:1 docker/python
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pytest==9.2.0
