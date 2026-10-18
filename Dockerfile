FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \\
    && pip install --no-cache-dir -r requirements.txt

COPY run.py models.py state_store.py app_cfg_example.py ./

RUN useradd --create-home --uid 10001 appuser
USER appuser

CMD ["python", "run.py"]
