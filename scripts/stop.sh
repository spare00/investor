#!/usr/bin/env bash
# Stop the Investor API (and local Postgres by default).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

require_repo
cd "${REPO_ROOT}"

FORCE="${INVESTOR_STOP_FORCE:-0}"
API_ONLY=0
STOP_DB=1
for arg in "$@"; do
  case "${arg}" in
    -f|--force) FORCE=1 ;;
    --api-only) API_ONLY=1; STOP_DB=0 ;;
    --with-db) STOP_DB=1 ;;
    -h|--help)
      cat <<EOF
Usage: $0 [--force] [--api-only]

  Stops the API recorded in ${PID_FILE}.
  By default also stops local docker-compose Postgres when managed.

  --force     also clear a matching listener on port ${INVESTOR_PORT} if pidfile is missing/stale
  --api-only  leave Postgres running (faster API-only restarts)

  INVESTOR_MANAGE_DB=0 disables DB start/stop entirely.
EOF
      exit 0
      ;;
    *) die "unknown argument: ${arg}" ;;
  esac
done

stop_pid() {
  local pid="$1"
  if ! pid_is_our_uvicorn "${pid}"; then
    die "pid ${pid} is not a running investor uvicorn — refusing to kill"
  fi
  echo "stopping pid ${pid}…"
  kill -TERM "${pid}" 2>/dev/null || true

  local i
  for i in $(seq 1 25); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      rm -f "${PID_FILE}"
      echo "api stopped"
      return 0
    fi
    sleep 0.2
  done

  echo "warning: still running after SIGTERM — sending SIGKILL" >&2
  kill -KILL "${pid}" 2>/dev/null || true
  sleep 0.3
  if kill -0 "${pid}" 2>/dev/null; then
    die "failed to stop pid ${pid}"
  fi
  rm -f "${PID_FILE}"
  echo "api stopped (killed)"
}

api_stopped=0
pid="$(read_pidfile 2>/dev/null || true)"
if [[ -n "${pid}" ]]; then
  if pid_is_our_uvicorn "${pid}"; then
    stop_pid "${pid}"
    api_stopped=1
  else
    echo "warning: stale pid file (${pid}) — removing" >&2
    rm -f "${PID_FILE}"
  fi
fi

if [[ "${api_stopped}" -eq 0 ]]; then
  if [[ "${FORCE}" == "1" ]]; then
    for pid in $(lsof -tiTCP:"${INVESTOR_PORT}" -sTCP:LISTEN 2>/dev/null || true); do
      if pid_is_our_uvicorn "${pid}"; then
        stop_pid "${pid}"
        api_stopped=1
      else
        echo "warning: leaving unrelated listener pid ${pid} on port ${INVESTOR_PORT}" >&2
      fi
    done
    if [[ "${api_stopped}" -eq 0 ]]; then
      if lsof -nP -iTCP:"${INVESTOR_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
        die "port ${INVESTOR_PORT} is in use, but no matching investor uvicorn was found"
      fi
      echo "api not running"
    fi
  else
    if [[ -z "${pid}" ]]; then
      echo "api not running (no valid pid file)"
    else
      echo "api not running"
    fi
    if [[ "${FORCE}" != "1" && "${api_stopped}" -eq 0 && "${STOP_DB}" -eq 0 ]]; then
      die "nothing to stop. Use --force and/or omit --api-only to stop db."
    fi
  fi
fi

if [[ "${STOP_DB}" -eq 1 && "${API_ONLY}" -eq 0 ]]; then
  stop_local_db
fi
