FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

COPY experiment_runner/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /tmp/requirements.txt

# pyproject.toml is read at app import time by `_load_build_meta()` in
# experiment_runner/app.py to stamp the running version on every page.
# Without this COPY the import crashes with FileNotFoundError and the
# container exits before the healthcheck can mark it healthy.
COPY pyproject.toml /app/pyproject.toml
COPY experiment_runner /app/experiment_runner

# GIT_SHA is read by `_load_build_meta()` and shown next to the version
# on every page. The container has no .git directory and `git rev-parse`
# would fail, so we bake the SHA in as a build arg from the deploy
# workflow instead.
ARG GIT_SHA=""
ENV GIT_SHA=$GIT_SHA

EXPOSE 8502

CMD ["python", "-m", "experiment_runner", "--root", "/data/experiments", "--port", "8502", "--host", "0.0.0.0"]
