FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY edge ./edge
COPY cloud ./cloud
COPY dashboard ./dashboard

RUN mkdir -p /data

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "edge.capture_api.main:app", "--host", "0.0.0.0", "--port", "8000"]