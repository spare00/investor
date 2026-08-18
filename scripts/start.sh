#!/usr/bin/env bash
# Start local Postgres (if needed) + Investor API (localhost-bound, paper-safe).
set -euo pipefail

umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

require_repo
cd "${REPO_ROOT}"

[[ -f "${REPO_ROOT}/.env" ]] || die "missing .env — copy .env.example and configure before starting"
assert_not_live_unless_allowed

runtime="$(env_get LLM_RUNTIME | tr '[:upper:]' '[:lower:]')"
if [[ "${runtime}" == "local" || "${runtime}" == "ollama" || "${runtime}" == "embedded" ]]; then
  if ! curl -fsS -m 1 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    echo "warning: LLM_RUNTIME=${runtime} but Ollama is not reachable on :11434" >&2
    echo "  ./scripts/ensure_local_llm.sh" >&2
  fi
fi

# Only bind loopback by default so the ops API is not exposed on all interfaces.
if [[ "${INVESTOR_HOST}" != "127.0.0.1" && "${INVESTOR_HOST}" != "localhost" && "${INVESTOR_HOST}" != "::1" ]]; then
  if [[ "${INVESTOR_ALLOW_PUBLIC_BIND:-0}" != "1" ]]; then
    die "refusing non-loopback bind (${INVESTOR_HOST}). Set INVESTOR_ALLOW_PUBLIC_BIND=1 to override."
  fi
  echo "warning: binding to ${INVESTOR_HOST} (public bind override enabled)" >&2
fi

mkdir -p "${DATA_DIR}"
chmod 700 "${DATA_DIR}"

existing="$(read_pidfile 2>/dev/null || true)"
if [[ -n "${existing}" ]]; then
  if pid_is_our_uvicorn "${existing}"; then
    die "already running (pid ${existing}). Use scripts/stop.sh first."
  fi
  echo "warning: removing stale pid file (${existing})" >&2
  rm -f "${PID_FILE}"
fi

if lsof -nP -iTCP:"${INVESTOR_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  die "port ${INVESTOR_PORT} is already in use"
fi

ensure_local_db

UVICORN="$(venv_uvicorn)"

# Do not `source .env` — the app loads it. Avoid leaking secrets into the shell.
export LOG_FORMAT="${INVESTOR_LOG_FORMAT}"

# Exclusive lock to avoid two starters racing.
set -o noclobber
echo "$$" > "${PID_FILE}.starting" || die "another start is in progress"
set +o noclobber

cleanup_starting() {
  rm -f "${PID_FILE}.starting"
}
trap cleanup_starting EXIT

touch "${LOG_FILE}"
chmod 600 "${LOG_FILE}"

if [[ "${INVESTOR_RELOAD}" == "1" ]]; then
  nohup "${UVICORN}" "${UVICORN_APP}" \
    --host "${INVESTOR_HOST}" \
    --port "${INVESTOR_PORT}" \
    --reload \
    >>"${LOG_FILE}" 2>&1 &
else
  nohup "${UVICORN}" "${UVICORN_APP}" \
    --host "${INVESTOR_HOST}" \
    --port "${INVESTOR_PORT}" \
    >>"${LOG_FILE}" 2>&1 &
fi
APP_PID=$!

# With --reload, nohup's PID is the reloader; still fine for stop verification.
echo "${APP_PID}" > "${PID_FILE}"
chmod 600 "${PID_FILE}"
rm -f "${PID_FILE}.starting"
trap - EXIT

# Brief readiness wait
for _ in $(seq 1 50); do
  if ! kill -0 "${APP_PID}" 2>/dev/null; then
    die "process exited during startup — see ${LOG_FILE}"
  fi
  if curl -fsS -m 1 "http://${INVESTOR_HOST}:${INVESTOR_PORT}/health" >/dev/null 2>&1; then
    echo "started pid ${APP_PID}"
    echo "  url   http://${INVESTOR_HOST}:${INVESTOR_PORT}/dashboard"
    echo "  log   ${LOG_FILE}"
    echo "  pid   ${PID_FILE}"
    if needs_local_db; then
      echo "  db    docker compose service 'db' (managed)"
    fi
    exit 0
  fi
  sleep 0.2
done

echo "warning: process is up but /health not ready yet — check ${LOG_FILE}" >&2
echo "started pid ${APP_PID}"
exit 0
