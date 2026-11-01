FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# The build context is the project root so the shared data.py is included.
COPY data.py ./data.py
COPY src/ ./src/
COPY tests/ ./tests/

RUN useradd --create-home --uid 1000 tracker \
    && rm -rf /app/src/__pycache__ /app/tests/__pycache__ \
    && mkdir -p /app/reports \
    && chown -R tracker:tracker /app
USER tracker

CMD ["python", "-m", "src", "--loop", "--report-dir", "/app/reports"]
