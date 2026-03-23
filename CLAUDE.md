# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Multi-Session Workflow (Subagent 방식)

이 프로젝트는 **단일 Planner 세션 + 서브에이전트** 구조로 운영된다.
별도 세션을 여러 개 열 필요 없이, Planner(현재 세션)가 요구사항을 분석하고 적절한 서브에이전트에 자동 위임한다.

### 서브에이전트 (.claude/agents/)

| 에이전트 | 담당 파일 | 자동 호출 시점 |
|----------|-----------|---------------|
| `backend` | `src/pipeline/`, `src/state.py`, `src/config.py`, `src/main.py`, `src/api.py` | Python 코드 구현·수정 |
| `frontend` | `src/static/index.html` | Web UI 수정 |
| `devops` | `Dockerfile`, `docker-compose.yml`, `requirements.txt` | 인프라·빌드·환경 |
| `qa` | (읽기 전용 + Bash) | 기능 검증·버그 재현 |

### Planner 역할 (이 세션)
- 요구사항 분석, 허점 이의 제기, 서브에이전트 위임, 문서 업데이트
- 담당: `docs/`, `TASKS.md`, `CLAUDE.md`, `README.md`
- 코드·인프라 직접 수정 금지 (서브에이전트에 위임)

상세 워크플로우: [`docs/multi-session-workflow.md`](docs/multi-session-workflow.md)
Task board: [`TASKS.md`](../TASKS.md)

## Documentation

Before diving into code, read the relevant docs first to save context:

| Doc | When to read |
|-----|-------------|
| [`docs/architecture.md`](docs/architecture.md) | System overview, pipeline flows, threading model, component roles |
| [`docs/api-spec.md`](docs/api-spec.md) | All API endpoints, SSE events, request/response schemas |
| [`docs/data-model.md`](docs/data-model.md) | SQLite schema, status transitions, filesystem layout |
| [`docs/requirements.md`](docs/requirements.md) | Functional/non-functional requirements, constraints |
| [`docs/operations.md`](docs/operations.md) | Deployment, config, troubleshooting, log locations |
| [`docs/backlog.md`](docs/backlog.md) | Known bugs, enhancement candidates, technical debt |
| [`docs/multi-session-workflow.md`](docs/multi-session-workflow.md) | Multi-session roles, task board, handoff protocol |

## Commands

```bash
# Build and run all services (로컬)
docker compose -f docker-compose.local.yml up --build -d

# View logs
docker compose -f docker-compose.local.yml logs -f brainstream

# Restart after code changes (no rebuild needed for config changes)
docker compose -f docker-compose.local.yml restart brainstream

# Rebuild and restart after Python source changes
docker compose -f docker-compose.local.yml up --build -d

# Inspect SQLite state DB
docker compose -f docker-compose.local.yml exec brainstream sqlite3 /app/data/state.db "SELECT * FROM downloads ORDER BY rowid DESC LIMIT 20;"

# Manually trigger the LB pipeline via API
curl -X POST http://localhost:8080/api/pipeline/run

# Check download history via API
curl http://localhost:8080/api/downloads | python3 -m json.tool
```

## Architecture

**Pipeline flow (both LB and manual share the same worker queue):**
```
[LB pipeline] fetch recommendations → mark_pending() → worker.enqueue_job()
[Manual]      POST /api/download    → mark_pending() → worker.enqueue_job() → SSE emit "queued"

worker_loop (single thread, FIFO):
  → 잡 시작 전: staging/{mbid}.* 잔류 파일 삭제 (.part 포함)
  → file_path 설정 + 파일 존재 시: 재다운로드 스킵, scan + mark_done만 실행
  → yt-dlp YouTube 검색 ("ytsearch5:{artist} {track}")
      차단 영상(payment/private/members-only) 감지 → 다음 후보 retry
      5개 소진 시 "ytsearch1:" 폴백 / FLAC 우선 → Opus fallback
  → LB 트랙: _lookup_recording_by_mbid(mbid) 직접 조회 → 실패 시 _mb_search_recording() 폴백
  → _mb_search_recording(artist, track): MB API 4단계 검색
      1. strict:    artistname:{a} AND recording:{t} + primarytype:Album + NOT Live/Compilation/...
      2. plain:     artistname:{a} AND recording:{t}  (release-type 필터 없음)
      3. artist-id: _mb_lookup_artist_ids(artist) → arid:{mbid} AND recording:{t} 재검색
      4. fallback:  recording:{t} 만으로 검색, artist-credit/alias 유사도 0.3 이상인 것 선택
  → mutagen: staging 파일에 artist / title / mb_trackid 초기 태그 쓰기
  → _enrich_track() — staging 파일에서 직접 실행:
      앨범명 결정 순서:
        1. iTunes Search API (artist 유사도 0.4 이상)
        2. Deezer API (artist 유사도 0.4 이상)
        3. MB recording → release 조회 (Official Album, 최초 release 선택)
        4. YouTube channel 이름 (최후 수단)
        5. 모두 실패 시 → "Unknown Album"
      커버아트 결정 순서:
        1. Cover Art Archive (mb_albumid_candidates가 있을 때, 최대 3개 시도)
        2. iTunes artwork URL
        3. Deezer artwork URL
        4. YouTube thumbnail (최후 수단)
  → shutil.copy2: staging → data/music/{Artist}/{Album}/{Track}.ext
  → Navidrome Subsonic API startScan + poll
  → SSE emit "done" / "failed" (LB 트랙은 SSE 리스너 없으므로 무시됨)
```

**Restart recovery:**
- `pending` 잡 → 원래 순서(rowid ASC)대로 재큐
- `downloading` 잡 → mark_failed("interrupted by restart", attempts++) → attempts < 3이면 재큐

**Threading model:**
- `main()` runs uvicorn on the **main thread** (blocking)
- **Worker thread** (non-daemon): `worker_loop()` — FIFO 큐에서 잡을 꺼내 하나씩 순차 처리. `_shutdown_event` 기반 graceful shutdown (`try/finally`로 uvicorn 종료 후 join timeout=30s)
- **Pipeline thread** (daemon): `run_pipeline()` — LB 추천 fetch 후 enqueue_job() 호출
- **Scheduler thread** (daemon): 60s tick, N시간마다 run_pipeline() 호출

**Stability hardening (P0):**
- **Graceful shutdown**: `uvicorn.run()` 감싸는 `try/finally`에서 `_shutdown_event.set()` → 워커 스레드 join(30s) → `_yt_executor.shutdown()`. Docker `stop_grace_period: 40s`
- **yt-dlp 타임아웃**: 메타데이터 추출 60s, 다운로드 600s (`_run_with_timeout` + `socket_timeout: 30`)
- **API Rate Limiting**: POST 엔드포인트별 인메모리 슬라이딩 윈도우 (예: `/api/download` 10회/분). 429 반환
- **API 입력 검증**: Pydantic `Field(max_length=500)`, Query params `Query(max_length=500)`
- **SSE 큐 TTL**: `_job_queues`에 last-activity 타임스탬프 저장, 30분 비활성 시 자동 정리. `touch_sse_queue()`로 keep-alive 시 갱신
- **로그 로테이션**: `RotatingFileHandler` 50MB × 5 파일 (최대 ~300MB)

## Key Files and their Roles

| File | Role |
|------|------|
| `src/main.py` | Entrypoint; wires config → DB → API → worker/pipeline threads → reload pending jobs → uvicorn |
| `src/worker.py` | Shared work queue module; `_work_queue` (FIFO), `_job_queues` (SSE, last-activity TTL), `_shutdown_event`, `enqueue_job()`, `emit()`, `touch_sse_queue()`, `worker_loop()`, `_cleanup_expired_queues()` |
| `src/api.py` | FastAPI app; `_cfg` injected by main.py; POST /api/download calls `worker.enqueue_job()`; POST /api/edit/{song_id} 메타데이터 직접 편집 (mutagen 태그 수정 → 파일 이동 → state.db 업데이트 → Navidrome rescan) |
| `src/state.py` | SQLite wrapper; `mbid` is PK; `get_pending_jobs()` returns pending/downloading jobs in rowid ASC order |
| `src/config.py` | Env-var only config (no file needed); `LB_USERNAME`, `LB_TOKEN`, `NAVIDROME_USER`, `NAVIDROME_PASSWORD`, `NAVIDROME_URL` |
| `src/jobs.py` | Job execution logic; `run_download_job()` runs download→tag→scan→state-update flow in worker thread |
| `src/pipeline/musicbrainz.py` | Shared MB API client; `lookup_recording(mbid)`, `_escape_mb_query()`, extracted from tagger.py to prevent circular imports |
| `src/utils/fs.py` | Shared filesystem utilities; `sanitize_path_component()`, `resolve_dir()`, `move_to_music_dir()` |
| `src/pipeline/tagger.py` | Most complex module; MB 4단계 검색(Stage 2.5 artist-id 추가) → shutil 복사 → mutagen 태깅 → iTunes/Deezer/MB 앨범 enrichment → CAA/iTunes/Deezer/YouTube 커버아트 임베딩. `write_title_tag` public alias 추가 (edit API에서 사용) |

## Tagger Constraints

- **mb_albumid는 파일 태그에 쓰지 않는다**: Navidrome은 album name으로 그룹핑하므로, mb_albumid가 다르면 같은 앨범이 2개로 분리됨
- **iTunes/Deezer artist 유사도 임계값 0.4**: 너무 낮으면 동명 아티스트의 앨범이 매칭될 위험
- **MB 검색 4단계 폴백**: strict → plain → artist-id → recording-only 순서로 점점 완화. recording-only 단계에서는 artist 유사도 0.3 미만 시 실패 처리
- **CAA 커버아트는 mb_albumid_candidates가 있을 때만 시도**: iTunes/Deezer로만 앨범이 결정된 경우 CAA를 건너뜀

## Volume Mounts (docker-compose.prod.yml)

| Host path | Container path | Notes |
|-----------|----------------|-------|
| `./data` | `/app/data` | Music files, staging, logs, state.db |

## Services

- **brainstream**: `http://localhost:8080` (Web UI + API)
- **navidrome**: 외부 포트 미노출 — `http://localhost:8080/navidrome/` 로 접근 (brainstream 프록시)
