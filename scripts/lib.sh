# Shared helpers for investor start/stop scripts.
# shellcheck shell=bash

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${REPO_ROOT}/.data"
PID_FILE="${DATA_DIR}/investor.pid"
LOG_FILE="${DATA_DIR}/investor.log"
UVICORN_APP="app.main:app"

# Secure local defaults (override via env).
INVESTOR_HOST="${INVESTOR_HOST:-127.0.0.1}"
INVESTOR_PORT="${INVESTOR_PORT:-8000}"
INVESTOR_RELOAD="${INVESTOR_RELOAD:-0}"
INVESTOR_ALLOW_LIVE="${INVESTOR_ALLOW_LIVE:-0}"
INVESTOR_LOG_FORMAT="${INVESTOR_LOG_FORMAT:-console}"
# Start/stop local docker-compose Postgres when DATABASE_URL points at localhost.
INVESTOR_MANAGE_DB="${INVESTOR_MANAGE_DB:-1}"

die() {
  echo "error: $*" >&2
  exit 1
}

require_repo() {
  [[ -f "${REPO_ROOT}/pyproject.toml" ]] || die "not an investor repo root: ${REPO_ROOT}"
  [[ -d "${REPO_ROOT}/app" ]] || die "missing app/ under ${REPO_ROOT}"
}

venv_uvicorn() {
  local bin="${REPO_ROOT}/.venv/bin/uvicorn"
  [[ -x "${bin}" ]] || die "missing ${bin} — create the venv and install deps first"
  printf '%s' "${bin}"
}

# Read a KEY=value from .env without sourcing (avoids shell injection from .env).
env_get() {
  local key="$1"
  local file="${REPO_ROOT}/.env"
  [[ -f "${file}" ]] || return 0
  # Match KEY=... at line start; ignore comments/exports.
  local line
  line="$(grep -E "^[[:space:]]*${key}=" "${file}" | tail -n1 || true)"
  [[ -n "${line}" ]] || return 0
  line="${line#*=}"
  line="${line%\"}"
  line="${line#\"}"
  line="${line%\'}"
  line="${line#\'}"
  printf '%s' "${line}"
}

assert_not_live_unless_allowed() {
  local mode enabled enable_live
  mode="$(env_get TRADING_MODE | tr '[:upper:]' '[:lower:]')"
  enabled="$(env_get LIVE_TRADING_ENABLED | tr '[:upper:]' '[:lower:]')"
  enable_live="$(env_get ENABLE_LIVE_TRADING | tr '[:upper:]' '[:lower:]')"
  if [[ "${mode}" == "live" || "${enabled}" == "true" || "${enabled}" == "1" \
     || "${enable_live}" == "true" || "${enable_live}" == "1" ]]; then
    if [[ "${INVESTOR_ALLOW_LIVE}" != "1" ]]; then
      die "live trading flags are set in .env; refusing to start. Set INVESTOR_ALLOW_LIVE=1 to override."
    fi
    echo "warning: starting with live trading flags enabled (INVESTOR_ALLOW_LIVE=1)" >&2
  fi
}

pid_is_our_uvicorn() {
  local pid="$1"
  [[ -n "${pid}" && "${pid}" =~ ^[0-9]+$ ]] || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  local cmd
  cmd="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
  [[ "${cmd}" == *"uvicorn"* && "${cmd}" == *"app.main"* ]] || return 1
  # Prefer processes started from this repo when cwd is available (macOS/Linux).
  if [[ -d "/proc/${pid}" ]]; then
    local cwd
    cwd="$(readlink "/proc/${pid}/cwd" 2>/dev/null || true)"
    [[ -z "${cwd}" || "${cwd}" == "${REPO_ROOT}" ]] || return 1
  fi
  return 0
}

read_pidfile() {
  [[ -f "${PID_FILE}" ]] || return 1
  local pid
  pid="$(tr -d '[:space:]' < "${PID_FILE}")"
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "${pid}"
}

# True when .env DATABASE_URL looks like the local compose Postgres.
needs_local_db() {
  [[ "${INVESTOR_MANAGE_DB}" == "1" ]] || return 1
  local url
  url="$(env_get DATABASE_URL)"
  if [[ -z "${url}" ]]; then
    return 0
  fi
  case "${url}" in
    *sqlite*) return 1 ;;
    *127.0.0.1*|*localhost*|*@db:*) return 0 ;;
    *) return 1 ;;
  esac
}

docker_ready() {
  command -v docker >/dev/null 2>&1 || return 1
  docker info >/dev/null 2>&1
}

ensure_docker_runtime() {
  if docker_ready; then
    return 0
  fi
  if ! command -v docker >/dev/null 2>&1; then
    die "docker is required for local Postgres (or set INVESTOR_MANAGE_DB=0)"
  fi
  if command -v colima >/dev/null 2>&1; then
    echo "docker daemon not running — starting colima…"
    colima start
    docker_ready || die "colima started but docker is still unavailable"
    return 0
  fi
  die "docker daemon not running (start Docker Desktop / colima, or set INVESTOR_MANAGE_DB=0)"
}

compose() {
  docker compose -f "${REPO_ROOT}/docker-compose.yml" "$@"
}

db_is_ready() {
  docker_ready || return 1
  # Prefer healthcheck; fall back to pg_isready inside the container.
  local health
  health="$(compose ps --status running --format '{{.Health}}' db 2>/dev/null | head -n1 || true)"
  if [[ "${health}" == "healthy" ]]; then
    return 0
  fi
  compose exec -T db pg_isready -U investor -d investor >/dev/null 2>&1
}

ensure_local_db() {
  needs_local_db || return 0
  ensure_docker_runtime
  echo "ensuring local Postgres (docker compose db)…"
  compose up -d db
  local i
  for i in $(seq 1 90); do
    if db_is_ready; then
      echo "postgres ready"
      return 0
    fi
    sleep 1
  done
  die "postgres did not become ready — check: docker compose logs db"
}

stop_local_db() {
  needs_local_db || return 0
  if ! docker_ready; then
    echo "docker not running — skipping db stop"
    return 0
  fi
  echo "stopping local Postgres…"
  compose stop db >/dev/null
  echo "postgres stopped"
}
