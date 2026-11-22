FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    GRADER_DATA_DIR=/data \
    GRADER_HOST=0.0.0.0 \
    GRADER_PORT=9131 \
    GRADER_CONFIG=/config/config.json

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY grader ./grader
COPY rubrics ./rubrics
COPY rubric_global_generic.md ./
COPY backup.py ./
COPY VERSION ./

RUN mkdir -p /data && chown -R 1000:1000 /data
USER 1000:1000
EXPOSE 9131

STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; port=os.environ.get('GRADER_PORT','9131'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health/ready', timeout=3)"

ENTRYPOINT ["python", "-m", "grader.server"]
