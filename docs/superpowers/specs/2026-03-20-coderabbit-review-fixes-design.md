# CodeRabbit Review Fixes — PR #10

## Problem

CodeRabbit이 PR #10에서 Major 버그 2개, Minor 문서 불일치 2개를 발견. 추가로 `.compose-port` 파일 기반 포트 영속화 방식을 `docker compose port` 조회 방식으로 교체.

## Changes

### Bug 1: 프로젝트명 충돌 위험 (Major)

**문제:** basename만으로 프로젝트명을 만들어서, 다른 경로에 같은 이름의 워크트리가 있으면 Docker Compose 프로젝트/볼륨/네트워크 충돌.

**수정:** 절대경로 해시 4자리 hex suffix 추가.
- Before: `brainstream-{sanitized_name}`
- After: `brainstream-{sanitized_name}-{4자리hex}`
- hex는 `WORKTREE_ROOT`의 `cksum` 값에서 파생 (포트 해싱과 동일 소스)

### Bug 2: `.env` lookup 실패 (Major)

**문제:** `SCRIPT_DIR == WORKTREE_ROOT`일 때 `${SCRIPT_DIR#$WORKTREE_ROOT/}` 확장이 동작하지 않아 `REL_PATH`가 절대경로로 남음.

**수정:** `SCRIPT_DIR == WORKTREE_ROOT` 분기 추가.
```bash
if [ "$SCRIPT_DIR" = "$WORKTREE_ROOT" ]; then
  MAIN_ENV_DIR="$MAIN_WORKTREE"
else
  REL_PATH="${SCRIPT_DIR#$WORKTREE_ROOT/}"
  MAIN_ENV_DIR="$MAIN_WORKTREE/$REL_PATH"
fi
```

### 포트 표시 방식 변경 (`.compose-port` 제거)

**문제:** `.compose-port` 파일로 포트를 영속화하는 방식은 추가 파일 관리가 필요하고 `.gitignore` 항목도 필요.

**수정:** `docker compose port` 명령으로 실행 중인 컨테이너에서 실제 포트를 조회하는 방식으로 교체.
- 컨테이너 실행 중: `docker compose port brainstream 8000`에서 실제 포트 추출
- 컨테이너 미실행: 해시 기반 포트값 사용
- lsof 충돌 검사는 `restart`에서만 실행
- `.compose-port` 파일 및 관련 로직 전부 제거
- `.gitignore`에서 `.compose-port` 항목 제거

### Minor 1: plan 문서 업데이트

`docs/superpowers/plans/2026-03-20-multi-worktree-compose-isolation.md`에서 `.compose-port` 관련 내용 제거, 새 포트 조회 방식 반영.

### Minor 2: spec 문서 `.env` 탐색 방법 수정

`docs/superpowers/specs/2026-03-20-multi-worktree-compose-isolation-design.md`의 `.env` 자동 복사 섹션에서 "git rev-parse --show-toplevel로 찾은 원본 레포"를 "git worktree list --porcelain으로 찾은 메인 워킹트리"로 정정.
