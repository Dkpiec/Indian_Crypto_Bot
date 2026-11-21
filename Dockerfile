FROM python:3.11-slim

LABEL maintainer="Dkpiec"
LABEL description="Indian Crypto Bot - Papi-native Binance trading bot"

# Non-root user for security
RUN groupadd -r bot && useradd -r -g bot -d /app -s /sbin/nologin bot

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data directory for SQLite DB and logs
RUN mkdir -p /app/data /app/logs && chown -R bot:bot /app

USER bot

# Health check endpoint (simple HTTP server on port 8080)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]