FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends git libgomp1 \
 && rm -rf /var/lib/apt/lists/*

COPY furnace/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /tmp/requirements.txt

# Fail the BUILD, not production, when the installed (unpinned, PyPI-latest)
# limen cannot import — e.g. a new native dependency like lightgbm needing
# libgomp.so.1, which crash-looped both containers on 2026-06-11. This line
# is why a broken runtime image can never ship again.
RUN python -c "import limen.experiment.trainer"

# pyproject.toml is read at app import time by `_load_build_meta()` in
# furnace/app.py to stamp the running version on every page.
# Without this COPY the import crashes with FileNotFoundError and the
# container exits before the healthcheck can mark it healthy.
COPY pyproject.toml /app/pyproject.toml
COPY furnace /app/furnace

# GIT_SHA is read by `_load_build_meta()` and shown next to the version
# on every page. The container has no .git directory and `git rev-parse`
# would fail, so we bake the SHA in as a build arg from the deploy
# workflow instead.
ARG GIT_SHA=""
ENV GIT_SHA=$GIT_SHA
ARG BUILD_DATE=""
ENV BUILD_DATE=$BUILD_DATE
ARG GIT_SUBJECT=""
ENV GIT_SUBJECT=$GIT_SUBJECT

EXPOSE 8502

CMD ["python", "-m", "furnace", "--root", "/data/experiments", "--port", "8502", "--host", "0.0.0.0"]
