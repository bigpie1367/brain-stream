# Multi-Worktree Compose Isolation

## Problem

로컬 개발 환경에서 git worktree를 활용해 여러 브랜치를 동시에 작업할 때, 각 워크트리별로 Docker Compose 스택을 독립적으로 띄울 수 없다. 호스트 포트(8080 하드코딩), Compose 프로젝트명, named volumes가 충돌한다.

## Goal

각 워크트리에서 `restart_local_docker.sh`를 실행하면 다른 워크트리와 충돌 없이 독립된 Compose 스택이 뜨도록 한다.

## Design Decisions

- **접근 방식**: 스크립트 중심. Compose 파일은 최소 변경, 로직은 스크립트에 집중.
- **데이터 격리**: 완전 격리. 각 워크트리의 `./data`가 독립 사용되고, named volumes는 프로젝트명으로 자동 스코핑.
- **포트 할당**: 워크트리 절대경로 해싱 → 8081~8999 범위 자동 배정 (8080은 기본값 예약). 충돌 시 다음 빈 포트 자동 탐색. `HOST_PORT` 환경변수로 수동 오버라이드도 가능.
- **`.env` 자동 복사**: 워크트리에 `.env`가 없으면 원본 레포의 `.env`를 자동 복사. 모든 워크트리가 동일한 크레덴셜을 사용하는 전제.
- **Navidrome**: 워크트리마다 각각 포함 (완전 격리). Navidrome은 호스트 포트를 노출하지 않고 brainstream 리버스 프록시(`/navidrome/`)를 통해 접근하므로 포트 변경 불필요.

## Changes

### 1. `docker-compose.local.yml`

포트 선언 1줄 변경:

```yaml
# Before
ports:
  - "8080:8000"

# After
ports:
  - "${HOST_PORT:-8080}:8000"
```

Navidrome은 호스트 포트 매핑이 없으므로(brainstream 리버스 프록시 경유) 변경 불필요.

### 2. `restart_local_docker.sh`

서브커맨드 지원 + 워크트리 자동 감지로 재작성. 모든 `docker compose` 호출에 `-f docker-compose.local.yml`을 명시.

#### 자동 설정 로직

1. `git rev-parse --show-toplevel`로 워크트리 루트 절대경로 감지
2. **프로젝트명용**: 루트 디렉터리 basename → sanitize(소문자 변환, `[a-z0-9-]` 외 문자는 `-`로 치환) → `COMPOSE_PROJECT_NAME=brainstream-{sanitized_name}`
3. **포트용**: 워크트리 **절대경로 전체**를 `cksum` 해싱 → `(hash % 919) + 8081` → 8081~8999 (8080은 비워크트리 기본값으로 예약)
4. `HOST_PORT` 환경변수가 이미 설정되어 있으면 해시 대신 해당 값 사용 (수동 오버라이드)

#### 포트 충돌 자동 해결

해시 포트가 이미 사용 중인 경우, 8081~8999 범위 내에서 다음 빈 포트를 순차 탐색하여 자동 배정. 변경된 포트는 출력에 표시.

#### `.env` 자동 복사

스크립트 실행 시 `.env` 파일이 없으면 `git rev-parse --show-toplevel`로 찾은 원본 레포(메인 워킹트리)의 `.env`를 현재 워크트리로 복사. 원본에도 `.env`가 없으면 `.env.example`에서 복사하고 경고 출력.

#### 서브커맨드

| 커맨드 | 동작 |
|--------|------|
| `restart` (기본, 인자 없을 때) | `docker compose down` → `docker compose up -d --build` |
| `stop` | `docker compose down` |
| `logs` | `docker compose logs -f` |
| `status` | `docker compose ps` |
| `info` | 프로젝트명, 포트 출력만 |

모든 서브커맨드 실행 시 프로젝트명과 포트를 먼저 출력한다. `restart` 완료 후에는 접근 URL도 출력: `Access at: http://localhost:{PORT}`

#### 사용 예시

```bash
./restart_local_docker.sh                    # restart (기본)
./restart_local_docker.sh stop
./restart_local_docker.sh logs
./restart_local_docker.sh status
./restart_local_docker.sh info
HOST_PORT=9090 ./restart_local_docker.sh     # 포트 수동 지정
```

### 3. 격리 메커니즘 요약

| 리소스 | 격리 방법 |
|--------|-----------|
| 호스트 포트 | `HOST_PORT` 환경변수 (절대경로 해시 → 8081~8999 자동 배정, 수동 오버라이드 가능) |
| Compose 프로젝트 | `COMPOSE_PROJECT_NAME` (워크트리명 기반, sanitized) |
| Named volumes | 프로젝트명에 자동 스코핑 (`brainstream-xxx_db-data`) |
| 데이터 디렉터리 | 각 워크트리의 `./data` (물리 경로 자연 분리) |
| 컨테이너 이름 | 프로젝트명에 자동 스코핑 |

## Non-Goals

- 워크트리 간 데이터 공유
- 원격/프로덕션 환경 지원
- 워크트리별 다른 크레덴셜 사용
