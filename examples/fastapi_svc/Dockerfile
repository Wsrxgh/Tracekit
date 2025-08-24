FROM python:3.11-slim
WORKDIR /app
COPY app.py tracekit.py .
RUN pip install --no-cache-dir fastapi==0.111.0 uvicorn==0.30.0 requests==2.32.3 \
    && mkdir -p /logs
ENV NODE_ID=vm0 STAGE=edge LOG_PATH=/logs MODULE_ID=svc APP_ID=app
EXPOSE 8080
CMD sh -c 'uvicorn app:app --host 0.0.0.0 --port 8080 --workers ${WORKERS:-1} --log-level warning'

