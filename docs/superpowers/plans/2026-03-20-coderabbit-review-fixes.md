# CodeRabbit Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PR #10 CodeRabbit 리뷰에서 발견된 버그 2개 수정 + `.compose-port` 제거 + 문서 불일치 2개 정정.

**Architecture:** `restart_local_docker.sh` 수정 (프로젝트명 해시 suffix, .env lookup 분기, 포트 조회 방식 변경, 마이그레이션 안내) + `.gitignore` 정리 + 문서 2개 정정.

**Tech Stack:** Bash, Docker Compose

**Spec:** `docs/superpowers/specs/2026-03-20-coderabbit-review-fixes-design.md`

---

### Task 1: `restart_local_docker.sh` 버그 수정 + `.compose-port` 제거

**Files:**
- Modify: `restart_local_docker.sh`
- Modify: `.gitignore:24-25`

- [ ] **Step 1: 프로젝트명에 해시 suffix 추가 (Bug 1)**

Line 11-12를 변경:

```bash
# Before
SANITIZED_NAME=$(echo "$WORKTREE_NAME" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed 's/^-//;s/-$//')
export COMPOSE_PROJECT_NAME="brainstream-${SANITIZED_NAME}"

# After
SANITIZED_NAME=$(echo "$WORKTREE_NAME" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed 's/^-//;s/-$//')
HASH=$(echo -n "$WORKTREE_ROOT" | cksum | awk '{print $1}')
SHORT_HASH=$(printf '%04x' "$(( HASH % 65536 ))")
export COMPOSE_PROJECT_NAME="brainstream-${SANITIZED_NAME}-${SHORT_HASH}"
```

- [ ] **Step 2: .env lookup 분기 추가 (Bug 2)**

Line 46-48을 변경:

```bash
# Before
  MAIN_WORKTREE=$(git -C "$SCRIPT_DIR" worktree list --porcelain | head -1 | sed 's/^worktree //')
  REL_PATH="${SCRIPT_DIR#$WORKTREE_ROOT/}"
  MAIN_ENV_DIR="$MAIN_WORKTREE/$REL_PATH"

# After
  MAIN_WORKTREE=$(git -C "$SCRIPT_DIR" worktree list --porcelain | head -1 | sed 's/^worktree //')
  if [ "$SCRIPT_DIR" = "$WORKTREE_ROOT" ]; then
    MAIN_ENV_DIR="$MAIN_WORKTREE"
  else
    REL_PATH="${SCRIPT_DIR#$WORKTREE_ROOT/}"
    MAIN_ENV_DIR="$MAIN_WORKTREE/$REL_PATH"
  fi
```

- [ ] **Step 3: 포트 결정 로직 변경 — `.compose-port` 제거 + `docker compose port` 조회**

포트 결정 함수를 도입하고, 기존 PORT_FILE 로직을 전부 교체. 전체 포트 섹션(line 14-42)을 다음으로 교체:

```bash
# --- Port resolution ---
resolve_port() {
  # 1. HOST_PORT 환경변수 명시 지정 시 그대로 사용
  if [ -n "${HOST_PORT:-}" ]; then
    echo "$HOST_PORT"
    return
  fi

  # 2. 컨테이너 실행 중이면 실제 바인딩 포트 조회
  local RUNNING_PORT
  RUNNING_PORT=$(dc port brainstream 8000 2>/dev/null | awk -F: '{print $NF}')
  if [ -n "$RUNNING_PORT" ]; then
    echo "$RUNNING_PORT"
    return
  fi

  # 3. 해시 기반 폴백
  local H
  H=$(echo -n "$WORKTREE_ROOT" | cksum | awk '{print $1}')
  echo "$(( (H % 919) + 8081 ))"
}

resolve_port_with_lsof() {
  # restart 전용: 해시 포트 + lsof 충돌 검사
  if [ -n "${HOST_PORT:-}" ]; then
    echo "$HOST_PORT"
    return
  fi

  local H PORT START_PORT
  H=$(echo -n "$WORKTREE_ROOT" | cksum | awk '{print $1}')
  PORT=$(( (H % 919) + 8081 ))

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
```

- [ ] **Step 4: 마이그레이션 안내 추가**

dc() 함수 뒤, 서브커맨드 dispatch 전에 추가:

```bash
# --- Migration: clean up old .compose-port and warn about orphaned resources ---
if [ -f "$SCRIPT_DIR/.compose-port" ]; then
  rm -f "$SCRIPT_DIR/.compose-port"
  OLD_PROJECT="brainstream-${SANITIZED_NAME}"
  echo "NOTE: Project name changed to $COMPOSE_PROJECT_NAME."
  echo "      Run 'docker compose -p $OLD_PROJECT down -v' to clean up old resources."
fi
```

- [ ] **Step 5: 서브커맨드에서 새 포트 결정 함수 사용**

case문을 다음으로 교체:

```bash
# --- Subcommands ---
CMD="${1:-restart}"

case "$CMD" in
  restart)
    HOST_PORT=$(resolve_port_with_lsof)
    export HOST_PORT
    print_info
    dc down
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
```

- [ ] **Step 6: `.gitignore`에서 `.compose-port` 항목 제거**

`.gitignore`에서 다음 2줄 삭제:
```
# Port persistence (restart_local_docker.sh)
.compose-port
```

- [ ] **Step 7: `info`로 동작 확인**

Run: `./restart_local_docker.sh info`
Expected: `Project: brainstream-refact-local-env-XXXX` (4자리 hex suffix 포함), `Port: 8XXX`

- [ ] **Step 8: Commit**

```bash
git add restart_local_docker.sh .gitignore
git commit -m "fix: project name collision, .env lookup, replace .compose-port with docker compose port"
```

---

### Task 2: 문서 정정

**Files:**
- Modify: `docs/superpowers/specs/2026-03-20-multi-worktree-compose-isolation-design.md`
- Modify: `docs/superpowers/plans/2026-03-20-multi-worktree-compose-isolation.md`

- [ ] **Step 1: spec 문서 `.env` 설명 수정**

`docs/superpowers/specs/2026-03-20-multi-worktree-compose-isolation-design.md`에서:

"git rev-parse --show-toplevel로 찾은 원본 레포(메인 워킹트리)의 .env를 현재 워크트리로 복사"

→

"git worktree list --porcelain으로 찾은 메인 워킹트리의 .env를 현재 워크트리로 복사"

- [ ] **Step 2: spec 문서 프로젝트명 형식 업데이트**

프로젝트명 관련 설명을 `brainstream-{sanitized_name}-{4자리hex}` 형식으로 수정.

- [ ] **Step 3: spec 문서 포트 표시 방식 반영**

`.compose-port` 관련 내용을 제거하고, `docker compose port` 조회 방식으로 설명 교체.

- [ ] **Step 4: plan 문서에서 `.compose-port` 관련 내용 제거**

`docs/superpowers/plans/2026-03-20-multi-worktree-compose-isolation.md`에서:
- PORT_FILE, `.compose-port` 읽기/쓰기/삭제 로직을 새 `resolve_port`/`resolve_port_with_lsof` 방식으로 교체
- `.env` 탐색 설명도 `git worktree list --porcelain`으로 정정

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-03-20-multi-worktree-compose-isolation-design.md docs/superpowers/plans/2026-03-20-multi-worktree-compose-isolation.md
git commit -m "doc: sync spec and plan with CodeRabbit review fixes"
```

---

### Task 3: 통합 검증

- [ ] **Step 1: restart 실행**

Run: `./restart_local_docker.sh restart`
Expected: 프로젝트명에 hex suffix 포함, 컨테이너 시작, URL 출력

- [ ] **Step 2: info로 포트 일관성 확인**

Run: `./restart_local_docker.sh info`
Expected: restart와 동일한 포트 표시 (docker compose port 조회)

- [ ] **Step 3: status 확인**

Run: `./restart_local_docker.sh status`
Expected: brainstream, navidrome 컨테이너 running

- [ ] **Step 4: stop**

Run: `./restart_local_docker.sh stop`
Expected: 컨테이너 종료, `.compose-port` 파일 생성되지 않음
