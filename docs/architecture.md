# 시스템 아키텍처

- **버전**: 1.0.0
- **작성일**: 2026-03-04

---

## 1. 전체 구성도

```
인터넷 클라이언트 (Amperfy 등 Subsonic 앱, 브라우저)
        │ HTTPS :443
        ▼
   [nginx 리버스 프록시]
   stream.example.com → brainstream:8080
        │
        │  /rest/*  (Subsonic API 프록시)
        │  /        (Web UI)
        │  /api/*   (REST API + SSE)
        ▼
┌─────────────────────────────────────────────────────────────┐
│                     Docker Network                          │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │               brainstream :8080                      │   │
│  │                                                      │   │
│  │  ┌────────────────┐  ┌──────────────────────────┐   │   │
│  │  │   FastAPI      │  │     Pipeline Core        │   │   │
│  │  │   Web UI       │  │                          │   │   │
│  │  │   SSE          │  │   LB Fetcher             │   │   │
│  │  │   /rest/* ─────┼──┼──▶ Subsonic Proxy        │   │   │
│  │  │   proxy        │  │   Downloader (yt-dlp)    │   │   │
│  │  └───────┬────────┘  │   Tagger (beets/mutagen) │   │   │
│  │          │           │   Navidrome (scan)        │   │   │
│  │          └───────────┤                          │   │   │
│  │                      └──────────┬───────────────┘   │   │
│  │                                 │                    │   │
│  │  ┌──────────────────────────────▼──────────────┐    │   │
│  │  │              state.db (SQLite)               │    │   │
│  │  └──────────────────────────────────────────────┘    │   │
│  └──────────────────────────────────────────────────────┘   │
│                             │                               │
│  ┌──────────────────────────▼────────────────────────┐      │
│  │              navidrome :4533                       │      │
│  │   (Subsonic API + 스트리밍)                         │      │
│  │   LAN 직접 접근용 포트 노출 (외부 방화벽 차단 권장) │      │
│  └───────────────────────────────────────────────────┘      │
│                                                             │
│  Volumes:                                                   │
│    ./data/music   → brainstream:/app/data/music             │
│                   → navidrome:/music (read-only)            │
│    ./data/staging → brainstream:/app/data/staging           │
│    ./beets        → brainstream:/root/.config/beets         │
└─────────────────────────────────────────────────────────────┘

External APIs:
  ListenBrainz  → api.listenbrainz.org
  YouTube       → yt-dlp
  MusicBrainz   → musicbrainz.org/ws/2
  Cover Art     → coverartarchive.org

접근 경로 요약:
  외부 (인터넷)  → stream.example.com (nginx → brainstream:8080)
  LAN (내부망)   → server-ip:4533 (navidrome 직접, 방화벽 설정에 따라 차단 가능)
  LAN (내부망)   → server-ip:8080 (brainstream 직접)
```

---

## 2. 컴포넌트 역할

| 파일 | 역할 |
|------|------|
| `src/main.py` | 진입점. 설정 로드 → DB 초기화 → API 설정 주입 → 파이프라인 스레드 시작 → uvicorn 실행 |
| `src/config.py` | 환경변수로 설정 로드 (config 파일 불필요) |
| `src/state.py` | SQLite `state.db` 래퍼. 다운로드 상태 CRUD |
| `src/api.py` | FastAPI 앱. Web UI 서빙, 수동 다운로드 API, SSE 스트림, 이력 조회, `/rest/*` Subsonic API 프록시 (외부 클라이언트 → navidrome 중계) |
| `src/pipeline/listenbrainz.py` | ListenBrainz CF 추천 API 호출 |
| `src/pipeline/downloader.py` | yt-dlp로 YouTube 검색 및 다운로드 (FLAC → Opus fallback) |
| `src/pipeline/tagger.py` | mutagen 선-태깅 → beets import → MB enrichment → 앨범아트 임베딩 |
| `src/pipeline/navidrome.py` | Subsonic API token-auth, startScan + getScanStatus 폴링 |
| `src/utils/logger.py` | structlog 설정 (TTY: 컬러 콘솔, non-TTY: JSON) |
| `src/static/index.html` | 다크 테마 단일 파일 Web UI |
| `beets/config.yaml` | beets 설정 (Dockerfile에 번들, 변경 시 재빌드 필요) |

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

config 파일 없이 환경변수만으로 동작합니다. `.env` 파일 또는 docker-compose `environment`로 주입합니다.

| 환경변수 | 기본값 | 필수 |
|----------|--------|------|
| `LB_USERNAME` | `""` | 필수 |
| `LB_TOKEN` | `""` | 필수 |
| `NAVIDROME_URL` | `http://navidrome:4533` | |
| `NAVIDROME_USER` | `admin` | |
| `NAVIDROME_PASSWORD` | `""` | 필수 |
