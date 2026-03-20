# CodeRabbit Review Fixes — PR #10

## Problem

CodeRabbit이 PR #10에서 Major 버그 2개, Minor 문서 불일치 2개를 발견. 추가로 `.compose-port` 파일 기반 포트 영속화 방식을 `docker compose port` 조회 방식으로 교체.

## Changes

### Bug 1: 프로젝트명 충돌 위험 (Major)

**문제:** basename만으로 프로젝트명을 만들어서, 다른 경로에 같은 이름의 워크트리가 있으면 Docker Compose 프로젝트/볼륨/네트워크 충돌.

**수정:** 절대경로 해시 4자리 hex suffix 추가.
- Before: `brainstream-{sanitized_name}`
- After: `brainstream-{sanitized_name}-{4자리hex}`
- 파생 방법: `printf '%04x' "$(( $(echo -n "$WORKTREE_ROOT" | cksum | awk '{print $1}') % 65536 ))"`
- 포트 해싱과 동일한 `cksum` 소스를 사용하되, 포트는 `% 919 + 8081`, 프로젝트명은 `% 65536 → hex`

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

#### 서브커맨드별 포트 결정 흐름

| 서브커맨드 | 포트 결정 방식 |
|-----------|--------------|
| `restart` | 해시 기반 포트 계산 + lsof 충돌 검사 → `HOST_PORT`로 설정 → `dc up` |
| `info`, `status`, `logs` | `docker compose port brainstream 8000`으로 실제 포트 조회 → 실패 시 해시 기반 폴백 (lsof 검사 없음) |
| `stop` | `docker compose port`로 조회 → 실패 시 해시 기반 폴백 |

#### `docker compose port` 출력 파싱

출력 형식: `0.0.0.0:8819` 또는 `:::8819`
파싱: `awk -F: '{print $NF}'` (마지막 콜론 뒤의 포트 번호 추출)

#### 제거 항목
- `.compose-port` 파일 및 관련 로직 (PORT_FILE 변수, 읽기/쓰기/삭제)
- `.gitignore`에서 `.compose-port` 항목

### 마이그레이션 주의사항

프로젝트명이 `brainstream-{name}`에서 `brainstream-{name}-{hex}`로 변경되므로, 기존 Docker 리소스(볼륨, 네트워크, 컨테이너)가 orphan 상태로 남음.

스크립트 시작 시 기존 `.compose-port` 파일이 발견되면 삭제하고, 구 프로젝트명의 리소스가 남아있을 수 있다는 안내를 한 번 출력:
```
NOTE: Project name changed. Run 'docker compose -p brainstream-{old_name} down -v' to clean up old resources.
```

### Minor 1: plan 문서 업데이트

`docs/superpowers/plans/2026-03-20-multi-worktree-compose-isolation.md`:
- `.compose-port` 관련 내용 제거, 새 포트 조회 방식 반영
- `.env` 탐색 설명도 `git worktree list --porcelain`으로 정정

### Minor 2: spec 문서 수정

`docs/superpowers/specs/2026-03-20-multi-worktree-compose-isolation-design.md`:
- `.env` 자동 복사 섹션: "git rev-parse --show-toplevel로 찾은 원본 레포" → "git worktree list --porcelain으로 찾은 메인 워킹트리"로 정정
- 포트 표시 방식 변경사항 반영
