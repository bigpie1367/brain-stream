# BrainStream

ListenBrainz 추천 기반 음악 자동 수집 및 스트리밍 시스템.

ListenBrainz 추천 → YouTube 다운로드 → beets 자동 태깅 → Navidrome 스트리밍까지 완전 자동화된 파이프라인.

## 빠른 시작

### 로컬 개발

```bash
# 1. 환경변수 설정
cp .env.example .env
# .env에서 LB_TOKEN, NAVIDROME_PASSWORD 입력

# 2. 서비스 시작 (소스 빌드)
./restart_local_docker.sh

# 3. 접속
# Web UI:    http://localhost:8080
# Navidrome: http://localhost:8080/navidrome/  (brainstream 프록시)
```

### 서버 배포 (이미지 전용)

```bash
# 1. docker-compose.prod.yml만 서버에 복사
# 2. .env 파일 생성
cp .env.example .env
# .env에서 LB_TOKEN, NAVIDROME_PASSWORD 입력

# 3. 서비스 시작
./restart_production_docker.sh
```

## 개발 워크플로우

여러 Claude Code 세션을 역할별로 나눠 협업합니다. 각 세션은 `TASKS.md`를 통해 조율됩니다.

| 역할 | 담당 영역 |
|------|-----------|
| Planner | 요구사항 분석, 문서화, 태스크 배정 |
| Backend | `src/pipeline/`, API, DB |
| Frontend | `src/static/index.html` |
| DevOps | `Dockerfile`, `docker-compose.*.yml`, `beets/` |
| QA | 기능 검증, 버그 리포트 |

자세한 세션 초기화 프롬프트 및 협업 규칙: [docs/multi-session-workflow.md](docs/multi-session-workflow.md)

## 문서

| 문서 | 설명 |
|------|------|
| [요구사항 정의서](docs/requirements.md) | 기능/비기능 요구사항, 외부 의존성 |
| [시스템 아키텍처](docs/architecture.md) | 컴포넌트 구조, 파이프라인 흐름, 스레딩 모델 |
| [API 명세서](docs/api-spec.md) | REST API 엔드포인트, SSE 이벤트 스펙 |
| [데이터 모델](docs/data-model.md) | SQLite 스키마, 상태 전이도 |
| [운영 가이드](docs/operations.md) | 배포, 설정, 로그, 트러블슈팅 |
| [프로젝트 백로그](docs/backlog.md) | 완료된 기능 이력, 알려진 이슈, 개선 후보 |
| [멀티 세션 워크플로우](docs/multi-session-workflow.md) | 역할 정의, 태스크 보드, 핸드오프 프로토콜 |

## 서비스 구성

| 서비스 | 포트 | 역할 |
|--------|------|------|
| brainstream | 8080 | Web UI + REST API + 파이프라인 + Navidrome 프록시 |
| navidrome | 미노출 | 음악 스트리밍 (Docker 내부 전용, `/navidrome/` 경로로 접근) |

## 주요 명령어

```bash
# 로그 확인
docker compose logs -f brainstream

# 컨테이너 재시작 (소스 변경 후)
docker restart brainstream

# 전체 재빌드 (Python 소스 변경 후)
docker compose up --build -d

# LB 파이프라인 수동 실행
curl -X POST http://localhost:8080/api/pipeline/run

# 다운로드 이력 확인
curl http://localhost:8080/api/downloads | python3 -m json.tool

# SQLite 상태 확인
sqlite3 data/state.db "SELECT * FROM downloads ORDER BY rowid DESC LIMIT 20;"
```
