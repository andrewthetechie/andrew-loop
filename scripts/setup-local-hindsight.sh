#!/usr/bin/env bash
# Run Hindsight locally via the official Vectorize Docker image.
# See https://hindsight.vectorize.io/developer/installation

set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found. Install Docker and try again." >&2
  exit 1
fi

read -r -p "HINDSIGHT_API_LLM_PROVIDER [groq]: " HINDSIGHT_API_LLM_PROVIDER
HINDSIGHT_API_LLM_PROVIDER="${HINDSIGHT_API_LLM_PROVIDER:-groq}"

read -r -p "HINDSIGHT_API_LLM_MODEL [openai/gpt-oss-20b]: " HINDSIGHT_API_LLM_MODEL
HINDSIGHT_API_LLM_MODEL="${HINDSIGHT_API_LLM_MODEL:-openai/gpt-oss-20b}"

HINDSIGHT_API_LLM_API_KEY=""
while [[ -z "${HINDSIGHT_API_LLM_API_KEY}" ]]; do
  read -r -s -p "HINDSIGHT_API_LLM_API_KEY (required): " HINDSIGHT_API_LLM_API_KEY
  echo
  if [[ -z "${HINDSIGHT_API_LLM_API_KEY}" ]]; then
    echo "API key cannot be empty." >&2
  fi
done

echo
echo "Starting Hindsight (Ctrl+C to stop)…"
echo "  API Server:        http://localhost:8888"
echo "  Control Plane UI:  http://localhost:9999"
echo

docker run --rm -d --pull always --name hindsight \
  -p 8888:8888 \
  -p 9999:9999 \
  -e HINDSIGHT_API_LLM_PROVIDER="${HINDSIGHT_API_LLM_PROVIDER}" \
  -e HINDSIGHT_API_LLM_API_KEY="${HINDSIGHT_API_LLM_API_KEY}" \
  -e HINDSIGHT_API_LLM_MODEL="${HINDSIGHT_API_LLM_MODEL}" \
  -v "${HOME}/.hindsight-docker:/home/hindsight/.pg0" \
  ghcr.io/vectorize-io/hindsight:latest
