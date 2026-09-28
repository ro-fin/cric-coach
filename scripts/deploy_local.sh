#!/usr/bin/env bash
# cricAI LAN deployment — PROCESS PATH (Phase-7 plan; design milestone 6:
# local Docker is broken on the lab box, so this supervisor-less script is the
# DEFAULT way to run the stack: uvicorn (cricai_api) + rq worker + next start
# against an EXISTING local PostgreSQL 16 and Redis 7). Verified by unit tests +
# a runbook walkthrough; the first live rig-from-doc run is UAT-PA1.
#
# Usage:
#   scripts/deploy_local.sh start    # migrate, then start api + worker + web
#   scripts/deploy_local.sh stop     # stop everything recorded in the pidfile
#   scripts/deploy_local.sh status   # per-process running/stopped report
#
# Configuration comes from deploy/.env (copy deploy/.env.example; override the
# file location with CRICAI_ENV_FILE). Prerequisites, crontab lines and
# troubleshooting: docs/runbooks/deploy_lan.md. Smoke-verify a started stack
# with: uv run scripts/verify_deploy.py
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${CRICAI_RUN_DIR:-$REPO_ROOT/.cricai-run}"
PIDFILE="$RUN_DIR/deploy_local.pids"
ENV_FILE="${CRICAI_ENV_FILE:-$REPO_ROOT/deploy/.env}"
VENV_BIN="$REPO_ROOT/.venv/bin"
NEXT_BIN="$REPO_ROOT/apps/web/node_modules/.bin/next"

die() {
    echo "deploy_local: ERROR: $*" >&2
    exit 1
}

# Export everything the .env file defines (the same CRICAI_* names the API's
# pydantic settings, the worker context and alembic read).
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

# DB credential hygiene (finding 28): the process path must not silently run
# on the weak cricai:cricai fallback, and must flag a leftover placeholder.
if [ -z "${CRICAI_DATABASE_URL:-}" ]; then
    export CRICAI_DATABASE_URL="postgresql+psycopg://cricai:cricai@localhost:5432/cricai"
    echo "deploy_local: WARNING: CRICAI_DATABASE_URL is unset — falling back to the WEAK" \
        "default cricai:cricai credentials. Set it with a strong password in $ENV_FILE" \
        "(see deploy/.env.example)." >&2
else
    export CRICAI_DATABASE_URL
    case "$CRICAI_DATABASE_URL" in
    *:cricai@* | *:change-me@* | *REPLACE_WITH_DB_PASSWORD*)
        echo "deploy_local: WARNING: CRICAI_DATABASE_URL still carries a weak/placeholder" \
            "password — replace it in $ENV_FILE before this leaves the bench." >&2
        ;;
    esac
fi
export CRICAI_STORAGE_ROOT="${CRICAI_STORAGE_ROOT:-$REPO_ROOT/storage}"
API_PORT="${CRICAI_API_PORT:-8000}"
WEB_PORT="${CRICAI_WEB_PORT:-3000}"
REDIS_URL="${CRICAI_REDIS_URL:-redis://localhost:6379/0}"
RQ_QUEUE="${CRICAI_RQ_QUEUE:-cricai}"

# Fingerprints recorded in the pidfile so stop/status can tell a still-ours
# process from a recycled PID after a crash (findings 6/21/26). Each is one or
# more '|'-separated substrings of the launched command's argv; a match on any
# one means "ours". They are multi-token on purpose so an unrelated recycled
# PID whose argv merely contains "rq" or "next" (e.g. a file named next-steps.md)
# does NOT match: uvicorn keeps its factory arg; rq's title stays "rq: ..." (and
# the launch line is "rq worker ..."); Next's title becomes "next-server ..."
# (and the launch line is "next start ..."). A best-effort guard, not a lock —
# but it never TERM/KILLs a PID whose command no longer looks like ours, and an
# EMPTY fingerprint (e.g. a pre-fix two-field pidfile) is treated as recycled so
# the first stop after an upgrade fails safe rather than blind-killing.
API_FP="cricai_api.app:create_app"
WORKER_FP="rq worker|rq:"
WEB_FP="next-server|next start"

is_running() {
    kill -0 "$1" 2>/dev/null
}

# Current argv of a pid ("" if it is gone) — used to detect PID recycling.
proc_args() {
    ps -p "$1" -o args= 2>/dev/null || true
}

# Classify a recorded (pid, fingerprint): "ours" (alive + a fingerprint pattern
# matches the argv), "recycled" (alive but a different command now owns the PID,
# OR the fingerprint is empty and so unverifiable), or "dead". fp is a set of
# '|'-separated glob substrings; ANY match means ours.
pid_state() {
    local pid="$1" fp="$2" args pattern
    is_running "$pid" || { echo dead; return; }
    # An empty fingerprint (a pre-fix two-field pidfile) can't be verified as
    # ours — fail safe to recycled so it is reported and cleared, never killed.
    [ -n "$fp" ] || { echo recycled; return; }
    args="$(proc_args "$pid")"
    local IFS='|'
    for pattern in $fp; do
        case "$args" in
        *"$pattern"*)
            echo ours
            return
            ;;
        esac
    done
    echo recycled
}

cmd_start() {
    [ -f "$PIDFILE" ] && die "pidfile $PIDFILE exists — already running? ('$0 stop' first)"
    [ -x "$VENV_BIN/uvicorn" ] || die "$VENV_BIN/uvicorn missing — run: uv sync --all-packages"
    [ -x "$VENV_BIN/rq" ] || die "$VENV_BIN/rq missing — run: uv sync --all-packages"
    [ -x "$NEXT_BIN" ] || die "$NEXT_BIN missing — run: cd apps/web && pnpm install"
    [ -d "$REPO_ROOT/apps/web/.next" ] || die \
        "apps/web/.next missing — build the dashboard first (NEXT_PUBLIC_* are inlined at build; see docs/runbooks/deploy_lan.md)"
    # The API has no auth-off mode (US-L3): an empty parent token would make
    # the deployment unusable AND unverifiable (verify_deploy needs it).
    [ -n "${CRICAI_PARENT_TOKEN:-}" ] || die \
        "CRICAI_PARENT_TOKEN is empty — set role tokens in $ENV_FILE (openssl rand -hex 32)"

    mkdir -p "$RUN_DIR" "$CRICAI_STORAGE_ROOT"

    echo "deploy_local: applying migrations (alembic upgrade head)..."
    "$VENV_BIN/alembic" -c "$REPO_ROOT/packages/data/alembic.ini" upgrade head

    echo "deploy_local: starting api (uvicorn, port $API_PORT)..."
    nohup "$VENV_BIN/uvicorn" cricai_api.app:create_app --factory \
        --host 0.0.0.0 --port "$API_PORT" >"$RUN_DIR/api.log" 2>&1 &
    API_PID=$!

    echo "deploy_local: starting rq worker (queue '$RQ_QUEUE')..."
    nohup "$VENV_BIN/rq" worker "$RQ_QUEUE" --url "$REDIS_URL" \
        >"$RUN_DIR/worker.log" 2>&1 &
    WORKER_PID=$!

    echo "deploy_local: starting web (next start, port $WEB_PORT)..."
    nohup "$NEXT_BIN" start "$REPO_ROOT/apps/web" -p "$WEB_PORT" -H 0.0.0.0 \
        >"$RUN_DIR/web.log" 2>&1 &
    WEB_PID=$!

    {
        echo "api $API_PID $API_FP"
        echo "worker $WORKER_PID $WORKER_FP"
        echo "web $WEB_PID $WEB_FP"
    } >"$PIDFILE"

    sleep 2
    local failed=0
    while read -r name pid _fp; do
        if is_running "$pid"; then
            echo "deploy_local: $name running (pid $pid, log $RUN_DIR/$name.log)"
        else
            echo "deploy_local: $name FAILED to start — see $RUN_DIR/$name.log" >&2
            failed=1
        fi
    done <"$PIDFILE"
    [ "$failed" -eq 0 ] || { cmd_stop; die "one or more processes failed to start"; }

    echo "deploy_local: stack up — verify with: uv run scripts/verify_deploy.py" \
        "--api-base http://localhost:$API_PORT --web-base http://localhost:$WEB_PORT"
}

cmd_stop() {
    # Idempotent (finding 23): a missing pidfile is success, not an error — the
    # runbook's own "stop then start" advice and any scripted re-deploy rely on
    # it, and a post-crash `stop` must be safe to run.
    if [ ! -f "$PIDFILE" ]; then
        echo "deploy_local: nothing to stop (no pidfile at $PIDFILE)"
        return 0
    fi
    # TERM only the pids that are still OURS. A crash can leave a stale pidfile
    # whose PIDs the OS has recycled onto unrelated processes; those must be
    # reported and cleared, NEVER killed (findings 6/21/26).
    while read -r name pid fp; do
        case "$(pid_state "$pid" "$fp")" in
        ours)
            echo "deploy_local: stopping $name (pid $pid)..."
            kill "$pid" 2>/dev/null || true
            ;;
        recycled)
            echo "deploy_local: $name pid $pid is NOT ours anymore (PID recycled:" \
                "$(proc_args "$pid")) — leaving it alone" >&2
            ;;
        dead)
            echo "deploy_local: $name (pid $pid) already stopped"
            ;;
        esac
    done <"$PIDFILE"
    # Graceful drain of the ones we TERMed, then force what is still ours.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        local alive=0
        while read -r _ pid fp; do
            [ "$(pid_state "$pid" "$fp")" = ours ] && alive=1
        done <"$PIDFILE"
        [ "$alive" -eq 0 ] && break
        sleep 1
    done
    while read -r name pid fp; do
        if [ "$(pid_state "$pid" "$fp")" = ours ]; then
            echo "deploy_local: force-killing $name (pid $pid)" >&2
            kill -9 "$pid" 2>/dev/null || true
        fi
    done <"$PIDFILE"
    rm -f "$PIDFILE"
    echo "deploy_local: stopped"
}

cmd_status() {
    [ -f "$PIDFILE" ] || {
        echo "deploy_local: not running (no pidfile)"
        return 0
    }
    while read -r name pid fp; do
        case "$(pid_state "$pid" "$fp")" in
        ours) echo "deploy_local: $name running (pid $pid)" ;;
        recycled)
            echo "deploy_local: $name STALE — pid $pid was recycled onto another process" \
                "(not ours); '$0 stop' will clear it without killing it" >&2
            ;;
        dead) echo "deploy_local: $name STOPPED (stale pid $pid)" ;;
        esac
    done <"$PIDFILE"
}

case "${1:-}" in
start) cmd_start ;;
stop) cmd_stop ;;
status) cmd_status ;;
*)
    echo "usage: $0 start|stop|status" >&2
    exit 64
    ;;
esac
