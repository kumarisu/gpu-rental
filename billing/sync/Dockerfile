FROM python:3.12-slim

RUN groupadd -g 2000 billing && useradd -r -u 2000 -g billing billing-sync && mkdir -p /data && chown billing-sync /data
WORKDIR /app
COPY sync.py /app/sync.py
USER billing-sync
CMD ["python", "-u", "/app/sync.py"]