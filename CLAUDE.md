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
| `devops` | `Dockerfile`, `docker-compose.yml`, `requirements.txt`, `beets/` | 인프라·빌드·환경 |
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
# Build and run all services
docker compose up --build -d

# View logs (music-bot only)
docker compose logs -f music-bot

# Restart music-bot after code changes (no rebuild needed for beets/config changes)
docker restart music-bot-temp-music-bot-1

# Rebuild and restart after Python source changes
docker compose up --build -d

# Inspect SQLite state DB
sqlite3 data/state.db "SELECT * FROM downloads ORDER BY rowid DESC LIMIT 20;"

# Tail beets import log
tail -f data/logs/beets-import.log

# Run a beet command inside the container
docker exec music-bot-temp-music-bot-1 beet list -f '$artist - $title [$album]'
docker exec music-bot-temp-music-bot-1 beet list -f '$path' artist:Radiohead

# Manually trigger the LB pipeline via API
curl -X POST http://localhost:8080/api/pipeline/run

# Check download history via API
curl http://localhost:8080/api/downloads | python3 -m json.tool
```

## Architecture

**Pipeline flow (automated, runs on startup and every N hours):**
```
ListenBrainz /cf/recommendation
  → state.db dedup (skip already done; retry failed < 3 attempts)
  → yt-dlp YouTube search ("ytsearch1:{artist} {track}")
  → mutagen pre-tag (write artist+title before beets sees the file)
  → beet import -q -s (singleton mode; serialized via threading.Lock)
  → import log offset-based skip detection (beet returns exit 0 on skip)
  → MusicBrainz API: recording/{mb_trackid}?inc=releases+release-groups
  → beet modify to set album= (mb_albumid NOT written — avoids Navidrome album duplication)
  → Cover Art Archive direct download + mutagen embed
  → Navidrome Subsonic API startScan + poll
```

**Manual download flow (Web UI):**
```
POST /api/download {artist, track}
  → job_id = "manual-{uuid8}" (also serves as mbid in state.db)
  → SSE stream GET /api/sse/{job_id}  (per-job Queue)
  → same pipeline steps as above (download → tag → scan)
  → SSE events: downloading → tagging → scanning → done/failed
```

**Threading model:**
- `main()` runs uvicorn on the **main thread** (blocking)
- LB pipeline runs in a **daemon thread** on startup
- Scheduler loop runs in a **daemon thread** (`schedule` library, 60s tick)
- Each manual download job runs in its own **daemon thread**
- `_beet_lock` (threading.Lock) serializes all `beet import` calls to prevent import log cross-contamination

## Key Files and their Roles

| File | Role |
|------|------|
| `src/main.py` | Entrypoint; wires config → DB → API → threads → uvicorn |
| `src/api.py` | FastAPI app; `_cfg` injected by main.py at startup |
| `src/state.py` | SQLite wrapper; `mbid` is PK (real MB UUID for LB tracks, `manual-{uuid8}` for manual) |
| `src/config.py` | YAML loader; env vars `LB_USERNAME`, `LB_TOKEN`, `NAVIDROME_USER`, `NAVIDROME_PASSWORD` override config |
| `src/pipeline/tagger.py` | Most complex module; handles pre-tagging, beets import, enrichment, art embedding |
| `beets/config.yaml` | Volume-mounted (no rebuild needed); changes take effect immediately on next import |

## Critical beets Constraints

- beets **must** be installed via pip (in `requirements.txt`), not apt — apt beets uses system Python and can't access app's pip packages
- In beets 2.x, `musicbrainz` is a **plugin** that must be listed explicitly in `beets/config.yaml`; without it, no MusicBrainz lookups happen at all
- `beet import` must use `-s` (singleton) flag for single-file imports — album mode skips files that don't match an album
- `strong_rec_thresh: 0.15` is required; stricter values reject legitimate matches (e.g., 88.9% similarity = distance 0.111 exceeds a 0.04 threshold)
- beet returns exit code 0 on skip — detect skips by reading the import log before/after with byte offsets
- Do **not** set `mb_albumid` in file tags via `beet modify`; doing so causes Navidrome to split same-album tracks into separate album entries (Navidrome groups by album name, not mb_albumid)

## Volume Mounts (docker-compose.yml)

| Host path | Container path | Notes |
|-----------|----------------|-------|
| `./data` | `/app/data` | Music files, staging, logs, state.db |
| `./config.yaml` | `/app/config.yaml` | Read-only; config changes need container restart |
| `./beets` | `/root/.config/beets` | beets config + state.pickle; changes take effect immediately |

## Services

- **music-bot**: `http://localhost:8080` (Web UI + API)
- **navidrome**: `http://localhost:4533` (music streaming; auto-scan disabled, triggered by music-bot)
