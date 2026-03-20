#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Worktree detection ---
WORKTREE_ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || echo "$SCRIPT_DIR")
WORKTREE_NAME=$(basename "$WORKTREE_ROOT")

# --- Project name (sanitized) ---
SANITIZED_NAME=$(echo "$WORKTREE_NAME" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed 's/^-//;s/-$//')
export COMPOSE_PROJECT_NAME="brainstream-${SANITIZED_NAME}"

# --- Port assignment ---
PORT_FILE="$SCRIPT_DIR/.compose-port"

if [ -n "${HOST_PORT:-}" ]; then
  # Explicit override — use it and persist
  echo "$HOST_PORT" > "$PORT_FILE"
elif [ -f "$PORT_FILE" ]; then
  # Reuse previously assigned port (avoids lsof self-detection)
  HOST_PORT=$(cat "$PORT_FILE")
else
  # First run — hash + lsof conflict resolution
  HASH=$(echo -n "$WORKTREE_ROOT" | cksum | awk '{print $1}')
  HOST_PORT=$(( (HASH % 919) + 8081 ))

  START_PORT=$HOST_PORT
  while lsof -iTCP:"$HOST_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; do
    HOST_PORT=$(( HOST_PORT + 1 ))
    if [ "$HOST_PORT" -gt 8999 ]; then
      HOST_PORT=8081
    fi
    if [ "$HOST_PORT" -eq "$START_PORT" ]; then
      echo "ERROR: No available port in range 8081-8999" >&2
      exit 1
    fi
  done

  echo "$HOST_PORT" > "$PORT_FILE"
fi
export HOST_PORT

# --- .env auto-copy ---
if [ ! -f "$SCRIPT_DIR/.env" ]; then
  MAIN_WORKTREE=$(git -C "$SCRIPT_DIR" worktree list --porcelain | head -1 | sed 's/^worktree //')
  REL_PATH="${SCRIPT_DIR#$WORKTREE_ROOT/}"
  MAIN_ENV_DIR="$MAIN_WORKTREE/$REL_PATH"

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

print_info() {
  echo "Project: $COMPOSE_PROJECT_NAME"
  echo "Port:    $HOST_PORT"
}

# --- Subcommands ---
CMD="${1:-restart}"

case "$CMD" in
  restart)
    print_info
    dc down
    dc up -d --build
    echo ""
    echo "Access at: http://localhost:${HOST_PORT}"
    ;;
  stop)
    print_info
    dc down
    rm -f "$PORT_FILE"
    ;;
  logs)
    print_info
    dc logs -f
    ;;
  status)
    print_info
    dc ps
    ;;
  info)
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
