FROM python:3.14.7-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN addgroup --system app && adduser --system --ingroup app app
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --requirement requirements.txt
COPY --chown=app:app main.py .
RUN mkdir /data && chown app:app /data

USER app
ENV STATE_PATH=/data/state.sqlite3 \
    HEARTBEAT_PATH=/tmp/alexa2ha-heartbeat

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os,sys,time; p=os.getenv('HEARTBEAT_PATH','/tmp/alexa2ha-heartbeat'); i=float(os.getenv('POLL_INTERVAL_SECONDS','60')); sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<max(300,3*i) else 1)"

ENTRYPOINT ["python", "main.py"]
