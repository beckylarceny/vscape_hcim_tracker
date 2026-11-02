FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# The build context is the project root.
COPY config/ ./config/
COPY src/ ./src/
COPY tests/ ./tests/
COPY scripts/internal/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod 755 /usr/local/bin/docker-entrypoint.sh \
    && useradd --create-home --uid 1000 tracker \
    && rm -rf /app/src/__pycache__ /app/tests/__pycache__ \
    && mkdir -p /app/reports \
    && chown -R tracker:tracker /app

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "src.collector", "--loop"]
