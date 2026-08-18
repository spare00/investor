#!/usr/bin/env bash
# Install/start Ollama and pull the embedded-AI model used when LLM_RUNTIME=local.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

require_repo
cd "${REPO_ROOT}"
mkdir -p "${DATA_DIR}"

REQUESTED="${1:-}"
if [[ -z "${REQUESTED}" ]]; then
  REQUESTED="$(env_get LLM_LOCAL_MODEL)"
fi
REQUESTED="${REQUESTED:-qwen2.5:14b}"
# Derived tags are local-only (Modelfile). Pull the base weights from the hub.
if [[ "${REQUESTED}" == *-ctx ]]; then
  BASE_MODEL="${REQUESTED%-ctx}"
  DERIVED_MODEL="${REQUESTED}"
else
  BASE_MODEL="${REQUESTED}"
  DERIVED_MODEL="${REQUESTED}-ctx"
fi
HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"
NUM_CTX="${OLLAMA_NUM_CTX:-8192}"
export OLLAMA_CONTEXT_LENGTH="${NUM_CTX}"

wait_for_ollama() {
  local ready=0
  local i
  for i in $(seq 1 40); do
    if curl -fsS -m 1 "${HOST}/api/tags" >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 0.5
  done
  [[ "${ready}" == "1" ]] || die "ollama did not become ready on ${HOST}"
}

start_ollama_serve() {
  # Prefer a process we own so OLLAMA_CONTEXT_LENGTH is applied. brew services
  # starts without that env and silently truncates agent prompts at ~4k.
  if command -v brew >/dev/null 2>&1; then
    brew services stop ollama >/dev/null 2>&1 || true
  fi
  if [[ -f "${DATA_DIR}/ollama.pid" ]]; then
    old_pid="$(cat "${DATA_DIR}/ollama.pid" 2>/dev/null || true)"
    if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" 2>/dev/null; then
      kill "${old_pid}" 2>/dev/null || true
      sleep 0.5
    fi
  fi
  if curl -fsS -m 2 "${HOST}/api/tags" >/dev/null 2>&1; then
    echo "warning: ollama already listening on ${HOST}; derived model still sets num_ctx=${NUM_CTX}" >&2
    return 0
  fi
  echo "starting ollama serve (OLLAMA_CONTEXT_LENGTH=${NUM_CTX})"
  nohup env OLLAMA_CONTEXT_LENGTH="${NUM_CTX}" OLLAMA_FLASH_ATTENTION="${OLLAMA_FLASH_ATTENTION:-1}" \
    ollama serve >>"${DATA_DIR}/ollama.log" 2>&1 &
  echo $! >"${DATA_DIR}/ollama.pid"
}

if ! command -v ollama >/dev/null 2>&1; then
  if ! command -v brew >/dev/null 2>&1; then
    die "ollama is not installed and Homebrew is missing — install from https://ollama.com"
  fi
  echo "installing ollama via Homebrew"
  brew install ollama
fi

if ! curl -fsS -m 2 "${HOST}/api/tags" >/dev/null 2>&1; then
  start_ollama_serve
  wait_for_ollama
fi

echo "pulling ${BASE_MODEL}"
ollama pull "${BASE_MODEL}"

MODELFILE="${DATA_DIR}/Modelfile.${DERIVED_MODEL//[:\/]/_}"
cat >"${MODELFILE}" <<EOF
FROM ${BASE_MODEL}
PARAMETER num_ctx ${NUM_CTX}
EOF
echo "creating ${DERIVED_MODEL} (num_ctx=${NUM_CTX})"
ollama create "${DERIVED_MODEL}" -f "${MODELFILE}"

echo "embedded AI ready: ${DERIVED_MODEL} @ ${HOST}"
echo "Set LLM_LOCAL_MODEL=${DERIVED_MODEL} in .env (do not commit .env)"
