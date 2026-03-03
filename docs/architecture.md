# 시스템 아키텍처

- **버전**: 1.0.0
- **작성일**: 2026-03-04

---

## 1. 전체 구성도

```
┌─────────────────────────────────────────────────────────────┐
│                     Docker Network                          │
│                                                             │
│  ┌──────────────────────────────────────┐                   │
│  │           music-bot :8080            │                   │
│  │                                      │                   │
│  │  ┌──────────┐  ┌──────────────────┐  │                   │
│  │  │ FastAPI  │  │  Pipeline Core   │  │                   │
│  │  │  (API)   │  │                  │  │                   │
│  │  │  Web UI  │  │  LB Fetcher      │  │                   │
│  │  │  SSE     │  │  Downloader      │  │                   │
│  │  └────┬─────┘  │  Tagger          │  │                   │
│  │       │        │  Navidrome       │  │                   │
│  │       └────────┤                  │  │                   │
│  │                └────────┬─────────┘  │                   │
│  │                         │            │                   │
│  │  ┌──────────────────────▼──────────┐ │                   │
│  │  │         state.db (SQLite)       │ │                   │
│  │  └─────────────────────────────────┘ │                   │
│  └──────────────────────────────────────┘                   │
│                         │                                   │
│  ┌──────────────────────▼──────────────┐                    │
│  │         navidrome :4533             │                    │
│  │   (Subsonic API + 스트리밍)          │                    │
│  └─────────────────────────────────────┘                    │
│                                                             │
│  Volumes:                                                   │
│    ./data/music   → music-bot:/app/data/music               │
│                   → navidrome:/music (read-only)            │
│    ./data/staging → music-bot:/app/data/staging             │
│    ./beets        → music-bot:/root/.config/beets           │
└─────────────────────────────────────────────────────────────┘

External APIs:
  ListenBrainz  → api.listenbrainz.org
  YouTube       → yt-dlp
  MusicBrainz   → musicbrainz.org/ws/2
  Cover Art     → coverartarchive.org
```

---

## 2. 컴포넌트 역할

| 파일 | 역할 |
|------|------|
| `src/main.py` | 진입점. 설정 로드 → DB 초기화 → API 설정 주입 → 파이프라인 스레드 시작 → uvicorn 실행 |
| `src/config.py` | `config.yaml` 로드. 환경변수(LB_USERNAME 등)로 오버라이드 가능 |
| `src/state.py` | SQLite `state.db` 래퍼. 다운로드 상태 CRUD |
| `src/api.py` | FastAPI 앱. Web UI 서빙, 수동 다운로드 API, SSE 스트림, 이력 조회 |
| `src/pipeline/listenbrainz.py` | ListenBrainz CF 추천 API 호출 |
| `src/pipeline/downloader.py` | yt-dlp로 YouTube 검색 및 다운로드 (FLAC → Opus fallback) |
| `src/pipeline/tagger.py` | mutagen 선-태깅 → beets import → MB enrichment → 앨범아트 임베딩 |
| `src/pipeline/navidrome.py` | Subsonic API token-auth, startScan + getScanStatus 폴링 |
| `src/utils/logger.py` | structlog 설정 (TTY: 컬러 콘솔, non-TTY: JSON) |
| `src/static/index.html` | 다크 테마 단일 파일 Web UI |
| `beets/config.yaml` | beets 설정 (볼륨 마운트, 재빌드 불필요) |

---

## 3. 파이프라인 흐름

### 3.1 자동 파이프라인 (ListenBrainz)

```
[Scheduler: 6h 주기 or 수동 트리거]
        │
        ▼
ListenBrainz CF API
  GET /1/cf/recommendation/user/{username}/recording
        │
        ▼
state.db 중복 체크 (mbid 기준)
  ├─ done → skip
  └─ 미처리 / 재시도 대상 → 처리 대상
        │
        ▼
[트랙별 처리 루프]
        │
        ├─ mark_pending(state.db)
        │
        ├─ yt-dlp YouTube 검색
        │    "ytsearch1:{artist} {track}"
        │    FLAC 우선 → Opus fallback
        │    출력: staging/{mbid}.flac
        │    실패 → mark_failed
        │
        ├─ mutagen 선-태깅
        │    artist + title 태그 삽입 (MusicBrainz 매칭 정확도 향상)
        │
        ├─ beet import -q -s (Lock 직렬화)
        │    import 로그 offset 기반 skip 감지
        │    skip     → mark_failed
        │    dup-skip → 성공 처리
        │    success  → mark_done
        │
        ├─ _enrich_track()
        │    imported_path = beet list -f $path
        │    mediafile → mb_trackid 읽기
        │    이미 album+art 있음 → 조기 리턴
        │    MB API /recording/{id}?inc=releases+release-groups
        │    → beet modify album= (mb_albumid는 기록 안 함)
        │    → coverartarchive.org 다운로드 + mutagen 임베딩
        │
        └─ (모든 트랙 완료 후)
           Navidrome startScan → getScanStatus 폴링 → 완료 대기
```

### 3.2 수동 다운로드 파이프라인 (Web UI)

```
[Web UI] POST /api/download {artist, track}
        │
        ▼
job_id = "manual-{uuid8}"
state.db mark_pending(source='manual')
Queue 생성 (job_id → Queue)
        │
        ├─ [Background Thread] _run_download_job()
        │    SSE emit: "downloading"
        │    yt-dlp 다운로드
        │    SSE emit: "tagging"
        │    tag_and_import() (자동 파이프라인과 동일)
        │    SSE emit: "scanning"
        │    Navidrome 스캔
        │    SSE emit: "done" / "failed"
        │
        └─ [SSE Stream] GET /api/sse/{job_id}
             Queue에서 이벤트 꺼내 클라이언트에 전달
             done/failed 수신 시 Queue 정리 및 스트림 종료
```

---

## 4. 스레딩 모델

```
Main Thread
  └─ uvicorn (HTTP 서버, 블로킹)

Daemon Thread 1
  └─ run_pipeline() (기동 시 즉시 1회 실행)

Daemon Thread 2
  └─ _run_scheduler() (60초 틱, schedule 라이브러리)
       └─ run_pipeline() (N시간마다 호출)

Daemon Thread 3~N (수동 다운로드 잡별)
  └─ _run_download_job(job_id, artist, track)
```

**동시성 제어:**
- `_beet_lock` (threading.Lock): beet import + import log 읽기를 직렬화
  - LB 파이프라인과 수동 다운로드 잡이 동시에 beet import 실행 시 로그 오염 방지

---

## 5. 데이터 흐름 (파일)

```
YouTube
  └─ yt-dlp 다운로드
       └─ data/staging/{mbid}.flac   ← 임시 파일
            └─ beet import -q -s
                 └─ data/music/{Artist}/{Album}/{Track}.flac  ← 영구 저장
                      └─ beet modify (album 태그 업데이트)
                           └─ mutagen (앨범아트 임베딩)
                                └─ Navidrome 스캔 → 라이브러리 반영
```

staging 파일은 import 성공/실패 후 삭제됨.

---

## 6. 설정 구조

```yaml
# config.yaml
listenbrainz:
  username: "..."       # LB_USERNAME 환경변수로 오버라이드 가능
  token: "..."          # LB_TOKEN
  recommendation_count: 25

download:
  staging_dir: /app/data/staging
  prefer_flac: true

beets:
  music_dir: /app/data/music

navidrome:
  url: "http://navidrome:4533"
  username: "..."       # NAVIDROME_USER
  password: "..."       # NAVIDROME_PASSWORD

scheduler:
  interval_hours: 6
```

민감 정보는 환경변수로 주입 권장 (`.env` 파일 또는 docker-compose `environment`).
