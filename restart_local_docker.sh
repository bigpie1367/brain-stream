#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Worktree detection ---
WORKTREE_ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || echo "$SCRIPT_DIR")
WORKTREE_NAME=$(basename "$WORKTREE_ROOT")

# --- Project name (sanitized + hash suffix for uniqueness) ---
SANITIZED_NAME=$(echo "$WORKTREE_NAME" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed 's/^-//;s/-$//')
HASH=$(echo -n "$WORKTREE_ROOT" | cksum | awk '{print $1}')
SHORT_HASH=$(printf '%04x' "$(( HASH % 65536 ))")
export COMPOSE_PROJECT_NAME="brainstream-${SANITIZED_NAME}-${SHORT_HASH}"

# --- .env auto-copy ---
if [ ! -f "$SCRIPT_DIR/.env" ]; then
  MAIN_WORKTREE=$(git -C "$SCRIPT_DIR" worktree list --porcelain | head -1 | sed 's/^worktree //')
  if [ "$SCRIPT_DIR" = "$WORKTREE_ROOT" ]; then
    MAIN_ENV_DIR="$MAIN_WORKTREE"
  else
    REL_PATH="${SCRIPT_DIR#$WORKTREE_ROOT/}"
    MAIN_ENV_DIR="$MAIN_WORKTREE/$REL_PATH"
  fi

  if [ -f "$MAIN_ENV_DIR/.env" ]; then
    cp "$MAIN_ENV_DIR/.env" "$SCRIPT_DIR/.env"
    echo "Copied .env from main worktree"
  elif [ -f "$SCRIPT_DIR/.env.example" ]; then
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    echo "WARNING: .env created from .env.example — edit credentials before use"
  else
    echo "ERROR: No .env, no .env.example found. Create .env manually." >&2
    exit 1
  fi
fi

# --- Compose wrapper ---
dc() {
  docker compose -f "$SCRIPT_DIR/docker-compose.local.yml" "$@"
}

# --- Port resolution ---
resolve_port() {
  # 1. HOST_PORT env var explicitly set
  if [ -n "${HOST_PORT:-}" ]; then
    echo "$HOST_PORT"
    return
  fi

  # 2. Container running — query actual bound port
  local RUNNING_PORT
  RUNNING_PORT=$(dc port brainstream 8000 2>/dev/null | awk -F: '{print $NF}') || true
  if [ -n "$RUNNING_PORT" ]; then
    echo "$RUNNING_PORT"
    return
  fi

  # 3. Hash-based fallback
  echo "$(( (HASH % 919) + 8081 ))"
}

resolve_port_with_lsof() {
  # For restart: hash port + lsof conflict check
  if [ -n "${HOST_PORT:-}" ]; then
    echo "$HOST_PORT"
    return
  fi

  local PORT START_PORT
  PORT=$(( (HASH % 919) + 8081 ))

  START_PORT=$PORT
  while lsof -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; do
    PORT=$(( PORT + 1 ))
    if [ "$PORT" -gt 8999 ]; then
      PORT=8081
    fi
    if [ "$PORT" -eq "$START_PORT" ]; then
      echo "ERROR: No available port in range 8081-8999" >&2
      exit 1
    fi
  done
  echo "$PORT"
}

print_info() {
  echo "Project: $COMPOSE_PROJECT_NAME"
  echo "Port:    $HOST_PORT"
}

# --- Migration: clean up old .compose-port ---
if [ -f "$SCRIPT_DIR/.compose-port" ]; then
  rm -f "$SCRIPT_DIR/.compose-port"
  OLD_PROJECT="brainstream-${SANITIZED_NAME}"
  echo "NOTE: Project name changed to $COMPOSE_PROJECT_NAME."
  echo "      Run 'docker compose -p $OLD_PROJECT down -v' to clean up old resources."
fi

# --- Subcommands ---
CMD="${1:-restart}"

case "$CMD" in
  restart)
    dc down
    HOST_PORT=$(resolve_port_with_lsof)
    export HOST_PORT
    print_info
    dc up -d --build
    echo ""
    echo "Access at: http://localhost:${HOST_PORT}"
    ;;
  stop)
    HOST_PORT=$(resolve_port)
    export HOST_PORT
    print_info
    dc down
    ;;
  logs)
    HOST_PORT=$(resolve_port)
    export HOST_PORT
    print_info
    dc logs -f
    ;;
  status)
    HOST_PORT=$(resolve_port)
    export HOST_PORT
    print_info
    dc ps
    ;;
  info)
    HOST_PORT=$(resolve_port)
    export HOST_PORT
    print_info
    ;;
  *)
    echo "Usage: $0 {restart|stop|logs|status|info}"
    echo ""
    echo "Commands:"
    echo "  restart  Down + rebuild + up (default)"
    echo "  stop     Down containers"
    echo "  logs     Follow container logs"
    echo "  status   Show container status"
    echo "  info     Show project name and port"
    exit 1
    ;;
esac
