# Multi-Worktree Compose Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 각 git worktree에서 독립적인 Docker Compose 스택을 충돌 없이 동시에 실행할 수 있도록 한다.

**Architecture:** 스크립트 중심 접근. `docker-compose.local.yml`은 포트 변수화 1줄만 변경하고, `restart_local_docker.sh`에서 워크트리 감지, 프로젝트명/포트 자동 결정, 서브커맨드 처리를 담당.

**Tech Stack:** Bash, Docker Compose, cksum

**Spec:** `docs/superpowers/specs/2026-03-20-multi-worktree-compose-isolation-design.md`

---

### Task 1: `docker-compose.local.yml` 포트 변수화

**Files:**
- Modify: `docker-compose.local.yml:19`

- [ ] **Step 1: 포트 하드코딩을 환경변수로 변경**

```yaml
# Before (line 19)
      - "8080:8000"

# After
      - "${HOST_PORT:-8080}:8000"
```

- [ ] **Step 2: 변경 확인**

Run: `grep HOST_PORT docker-compose.local.yml`
Expected: `- "${HOST_PORT:-8080}:8000"`

- [ ] **Step 3: Commit**

```bash
git add docker-compose.local.yml
git commit -m "feat: parameterize host port in docker-compose.local.yml"
```

---

### Task 2: `restart_local_docker.sh` 재작성 — 자동 설정 로직

**Files:**
- Modify: `restart_local_docker.sh` (전체 재작성)

- [ ] **Step 1: 스크립트 재작성**

```bash
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
if [ -z "${HOST_PORT:-}" ]; then
  HASH=$(echo -n "$WORKTREE_ROOT" | cksum | awk '{print $1}')
  HOST_PORT=$(( (HASH % 919) + 8081 ))

  # Auto-resolve port conflicts
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
```

- [ ] **Step 2: 실행 권한 확인**

Run: `ls -la restart_local_docker.sh`
Expected: `-rwxr-xr-x` (이미 실행 권한 있어야 함. 없으면 `chmod +x restart_local_docker.sh`)

- [ ] **Step 3: `info` 서브커맨드로 기본 동작 확인**

Run: `./restart_local_docker.sh info`
Expected output (예시):
```
Project: brainstream-brain-stream
Port:    8XXX
```

- [ ] **Step 4: Commit**

```bash
git add restart_local_docker.sh
git commit -m "feat: rewrite restart script with worktree isolation and subcommands"
```

---

### Task 3: 통합 검증

- [ ] **Step 1: 현재 워크트리에서 restart 실행**

Run: `./restart_local_docker.sh restart`
Expected: 컨테이너 빌드 및 시작, 포트와 URL 출력

- [ ] **Step 2: status로 컨테이너 상태 확인**

Run: `./restart_local_docker.sh status`
Expected: brainstream, navidrome 컨테이너 running

- [ ] **Step 3: HTTP 접근 확인**

Run: `curl -s -o /dev/null -w "%{http_code}" http://localhost:$(./restart_local_docker.sh info 2>/dev/null | grep Port | awk '{print $2}')`
Expected: `200` 또는 `301`/`302`

- [ ] **Step 4: stop으로 정리**

Run: `./restart_local_docker.sh stop`
Expected: 컨테이너 종료

- [ ] **Step 5: 스펙 문서에 완료 기록 & Final commit**

```bash
git add docker-compose.local.yml restart_local_docker.sh
git commit -m "feat: multi-worktree compose isolation complete"
```
