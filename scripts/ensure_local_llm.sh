#!/usr/bin/env bash
# Install/start Ollama and pull the embedded-AI model used when LLM_RUNTIME=local.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

require_repo
cd "${REPO_ROOT}"
mkdir -p "${DATA_DIR}"

MODEL="${1:-}"
if [[ -z "${MODEL}" ]]; then
  MODEL="$(env_get LLM_LOCAL_MODEL)"
fi
MODEL="${MODEL:-qwen2.5:14b}"
HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"

if ! command -v ollama >/dev/null 2>&1; then
  if ! command -v brew >/dev/null 2>&1; then
    die "ollama is not installed and Homebrew is missing — install from https://ollama.com"
  fi
  echo "installing ollama via Homebrew"
  brew install ollama
fi

if ! curl -fsS -m 2 "${HOST}/api/tags" >/dev/null 2>&1; then
  echo "starting ollama serve"
  if command -v brew >/dev/null 2>&1 && brew services list 2>/dev/null | grep -q '^ollama'; then
    brew services start ollama >/dev/null || true
  fi
  if ! curl -fsS -m 2 "${HOST}/api/tags" >/dev/null 2>&1; then
    nohup ollama serve >>"${DATA_DIR}/ollama.log" 2>&1 &
    echo $! >"${DATA_DIR}/ollama.pid"
  fi
  ready=0
  for _ in $(seq 1 40); do
    if curl -fsS -m 1 "${HOST}/api/tags" >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 0.5
  done
  [[ "${ready}" == "1" ]] || die "ollama did not become ready on ${HOST}"
fi

echo "pulling ${MODEL}"
ollama pull "${MODEL}"
echo "embedded AI ready: ${MODEL} @ ${HOST}"
