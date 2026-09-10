FROM python:3.12-slim

RUN useradd -r -u 2000 sync && mkdir -p /data && chown sync /data
WORKDIR /app
COPY sync.py /app/sync.py
USER sync
CMD ["python", "-u", "/app/sync.py"]