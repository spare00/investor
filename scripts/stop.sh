#!/usr/bin/env bash
# Stop the Investor API started by scripts/start.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

require_repo
cd "${REPO_ROOT}"

FORCE="${INVESTOR_STOP_FORCE:-0}"
for arg in "$@"; do
  case "${arg}" in
    -f|--force) FORCE=1 ;;
    -h|--help)
      echo "Usage: $0 [--force]"
      echo "  Stops the process recorded in ${PID_FILE}."
      echo "  --force  also clears a matching listener on port ${INVESTOR_PORT} if pidfile is missing/stale."
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
      echo "stopped"
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
  echo "stopped (killed)"
}

pid="$(read_pidfile 2>/dev/null || true)"
if [[ -n "${pid}" ]]; then
  if pid_is_our_uvicorn "${pid}"; then
    stop_pid "${pid}"
    exit 0
  fi
  echo "warning: stale pid file (${pid}) — removing" >&2
  rm -f "${PID_FILE}"
fi

if [[ "${FORCE}" != "1" ]]; then
  die "not running (no valid pid file). Use --force to stop a matching listener on port ${INVESTOR_PORT}."
fi

# Force path: only kill listeners that look like our uvicorn on the expected port.
stopped_any=0
for pid in $(lsof -tiTCP:"${INVESTOR_PORT}" -sTCP:LISTEN 2>/dev/null || true); do
  if pid_is_our_uvicorn "${pid}"; then
    stop_pid "${pid}"
    stopped_any=1
  else
    echo "warning: leaving unrelated listener pid ${pid} on port ${INVESTOR_PORT}" >&2
  fi
done

if [[ "${stopped_any}" -eq 0 ]]; then
  if lsof -nP -iTCP:"${INVESTOR_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    die "port ${INVESTOR_PORT} is in use, but no matching investor uvicorn was found"
  fi
  echo "not running"
fi
