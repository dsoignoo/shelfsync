FROM node:22-bookworm-slim AS codex
ARG CODEX_VERSION=0.160.1
RUN npm install --global @openai/codex@${CODEX_VERSION}

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 shelfsync \
    && mkdir -p /data /home/shelfsync/.codex \
    && chown -R shelfsync:shelfsync /data /home/shelfsync

COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py codex_provider.py assemblyai_provider.py ./
COPY static/ static/

ENV CODEX_HOME=/home/shelfsync/.codex DATA_DIR=/data
USER shelfsync

EXPOSE 8765

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8765"]
