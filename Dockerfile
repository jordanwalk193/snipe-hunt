FROM python:3.12-slim

# git: optional syncing of the data folder. nodejs/npm: the Claude CLI used for scoring.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git nodejs npm ca-certificates tzdata \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY snipehunt ./snipehunt
COPY examples ./examples

ENV SNIPEHUNT_HOME=/var/lib/snipe-hunt \
    PYTHONUNBUFFERED=1
VOLUME /var/lib/snipe-hunt
EXPOSE 8766

# Creates the starter files on first run, then serves the API and runs the schedule.
CMD ["sh", "-c", "python -m snipehunt init >/dev/null && exec python -m snipehunt serve"]
