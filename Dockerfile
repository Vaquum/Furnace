FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

COPY experiment_runner/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /tmp/requirements.txt

COPY experiment_runner /app/experiment_runner

EXPOSE 8502

CMD ["python", "-m", "experiment_runner", "--root", "/data/experiments", "--port", "8502", "--host", "0.0.0.0"]
