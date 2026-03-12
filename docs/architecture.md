# 시스템 아키텍처

- **버전**: 1.3.0
- **작성일**: 2026-03-12

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
│  │  └───────┬────────┘  │   Tagger (mutagen)        │   │   │
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
│  │   포트 미노출 — Docker 내부 네트워크에서만 접근     │      │
│  └───────────────────────────────────────────────────┘      │
│                                                             │
│  Volumes:                                                   │
│    ./data/music   → brainstream:/app/data/music             │
│                   → navidrome:/music (read-only)            │
│    ./data/staging → brainstream:/app/data/staging           │
│    db-data        → brainstream:/app/db  (named volume)     │
└─────────────────────────────────────────────────────────────┘

External APIs:
  ListenBrainz  → api.listenbrainz.org
  YouTube       → yt-dlp
  MusicBrainz   → musicbrainz.org/ws/2
  Cover Art     → coverartarchive.org
  iTunes        → itunes.apple.com/search (앨범명/커버아트)
  Deezer        → api.deezer.com/search/track (앨범명/커버아트 폴백)

접근 경로 요약:
  외부 (인터넷)  → stream.example.com (nginx → brainstream:8080)
  LAN (내부망)   → server-ip:8080 (brainstream 직접)
  navidrome      → Docker 내부 전용, 외부 접근 불가
```

---

## 2. 컴포넌트 역할

| 파일 | 역할 |
|------|------|
| `src/main.py` | 진입점. 설정 로드 → DB 초기화 → API 설정 주입 → 파이프라인 스레드 시작 → uvicorn 실행 |
| `src/config.py` | 환경변수로 설정 로드 (config 파일 불필요) |
| `src/state.py` | SQLite `state.db` 래퍼. 다운로드 상태 CRUD. `update_track_info`로 artist/file_path 선택적 업데이트 |
| `src/api.py` | FastAPI 앱. Web UI 서빙, 수동 다운로드 API, SSE 스트림, 이력 조회, 앨범 재매칭 API (`/api/rematch/*`), `/rest/*` Subsonic API 프록시 (외부 클라이언트 → navidrome 중계). `_resolve_dir`로 대소문자 무시 기준 기존 폴더 재사용 (Navidrome conflicts 방지) |
| `src/pipeline/listenbrainz.py` | ListenBrainz CF 추천 API 호출; `recording_mbid`만 반환하므로 `_lookup_recording(mbid)`로 MB API에서 artist/track 조회 |
| `src/pipeline/downloader.py` | yt-dlp로 YouTube 검색 및 다운로드 (FLAC → Opus fallback); `ytsearch5:` 5개 후보 검색 후 차단 영상 감지 시 다음 후보 retry; `(file_path, yt_metadata)` 튜플 반환. `search_candidates(artist, track)`: 다운로드 없이 후보 5개 메타데이터 반환. `download_track_by_id(video_id, ...)`: 지정 video_id로 직접 다운로드 |
| `src/pipeline/tagger.py` | MB API recording 검색 (artist 유사도 검증) → mutagen 전체 태그 쓰기 → shutil 파일 복사 → MB enrichment → CAA/iTunes/Deezer 커버아트 임베딩 → YouTube 썸네일/채널명 폴백. `_write_artist_tag`, `_write_album_tag`, `_itunes_search(country=)` 등 public alias로 `api.py` 재매칭에도 사용 |
| `src/pipeline/navidrome.py` | Subsonic API token-auth, startScan + getScanStatus 폴링 |
| `src/utils/logger.py` | structlog 설정 (TTY: 컬러 콘솔, non-TTY: JSON) |
| `src/static/index.html` | 다크 테마 단일 파일 Web UI. Downloads 탭 + Library 탭 (아티스트/앨범/트랙 브라우징, 트랙별 앨범 재매칭 버튼). 수동 다운로드 섹션에 Auto/Pick 모드 토글 — Pick 모드에서 YouTube 후보 카드(썸네일, 제목, 채널, 재생시간, Live/Cover 배지) 표시 후 원하는 영상 선택 다운로드. UI 텍스트 영어 통일, 버튼 min-width 고정, 테이블 fixed layout |

---

## 3. 파이프라인 흐름

### 3.1 자동 파이프라인 (ListenBrainz)

```
[Scheduler: 6h 주기 or 수동 트리거]
        │
        ▼
ListenBrainz CF API
  GET /1/cf/recommendation/user/{username}/recording
  → recording_mbid 목록만 반환
  → _lookup_recording(mbid): MB API GET /ws/2/recording/{mbid}?inc=artist-credits
       artist/track이 비어있으면 skip
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
        │   retry 트랙 empty artist/track → _lookup_recording() 재조회, 여전히 비면 mark_failed
        │
        ├─ yt-dlp YouTube 검색
        │    "ytsearch5:{artist} {track} official audio" (5개 후보)
        │    결제/비공개/멤버십/접근불가 감지 → 다음 후보 retry
        │    5개 모두 소진 시 "ytsearch1:{artist} {track} official audio" 폴백
        │    FLAC 우선 → Opus fallback
        │    출력: staging/{mbid}.flac
        │    실패 → mark_failed
        │
        ├─ _mb_search_recording(artist, track_name)  ← 다운로드 전에 실행
        │    stage 1 (strict):  artistname:{a} AND recording:{t}
        │                       + primarytype:Album + status:Official
        │                       + NOT secondarytype:Live/Compilation/Soundtrack/...
        │    stage 2 (plain):   artistname:{a} AND recording:{t}  (release-type 필터 없음)
        │    stage 3 (fallback): recording:{t} 만 검색,
        │                        artist-credit + aliases 유사도 0.3 이상인 것 선택
        │    반환: (recording_ids, mb_artist_name, mb_recording_title)
        │    recording_id 없으면 → mark_failed
        │
        ├─ mutagen: staging 파일에 artist / title / mb_trackid 초기 태그 쓰기
        │
        ├─ yt_metadata 수집 (download_track 반환)
        │    thumbnail_url, channel (YouTube 채널명)
        │
        ├─ _enrich_track()  ← staging 파일에서 직접 실행
        │    이미 album+art 있음 → 조기 리턴
        │    반환: (album, canonical_artist, canonical_title)
        │
        │    [canonical_artist 결정 순서]
        │    1. MB artist-credit[0].artist.name (MB 매칭 성공 시)
        │    2. iTunes artistName (artist 유사도 0.4 이상)
        │    3. Deezer artist.name (artist 유사도 0.4 이상)
        │    4. 원본 요청 아티스트명 (fallback)
        │
        │    [canonical_title 결정 순서]
        │    1. iTunes trackName (artist 유사도 0.4 이상)
        │    2. MB recording title (MB 매칭 성공 시)
        │    3. Deezer title (artist 유사도 0.4 이상)
        │    4. 원본 요청 track_name (fallback)
        │
        │    [앨범명 결정 순서]
        │    1. iTunes Search API (artist 유사도 0.4 이상) → album 태그 쓰기
        │    2. Deezer API (artist 유사도 0.4 이상) → album 태그 쓰기
        │    3. MB API /recording/{id}?inc=releases+release-groups
        │       → Official Album 중 최초 release 선택 → album 태그 쓰기
        │       → mb_albumid는 기록 안 함 (Navidrome 앨범 분리 방지)
        │    4. YouTube channel → album 태그 쓰기 (최후 수단)
        │    5. 모두 실패 시 → "Unknown Album" 태그 쓰기
        │
        │    [커버아트 결정 순서]
        │    1. Cover Art Archive: mb_albumid_candidates 최대 3개 순차 시도 + mutagen 임베딩
        │    2. iTunes artwork URL → mutagen 임베딩
        │    3. Deezer artwork URL → mutagen 임베딩
        │    4. YouTube thumbnail_url → mutagen 임베딩 (최후 수단)
        │
        ├─ mutagen: canonical artist / title 태그를 staging 파일에 덮어쓰기
        │    (요청 키워드가 아닌 MB/iTunes/Deezer 정규명으로 파일 태그 통일)
        │
        ├─ shutil.copy2: staging → data/music/{canonical_artist}/{Album}/{canonical_title}.ext
        │    폴더명: _primary_artist(canonical_artist) — feat. 제거 후 sanitize
        │    파일명: canonical_title sanitize
        │    staging 원본 삭제
        │    state.db: file_path 저장 → mark_done
        │    state.db: artist / track_name을 canonical 값으로 업데이트
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
        │    tag_and_import() → (success, dest_path, canonical_artist, canonical_title)
        │      성공 시 state.db artist/track_name을 canonical 값으로 업데이트
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
- 별도 lock 불필요. 각 다운로드 잡은 고유한 mbid 기반 파일명을 사용하므로 파일 충돌 없음.

---

## 5. 데이터 흐름 (파일)

```
YouTube
  └─ yt-dlp 다운로드
       └─ data/staging/{mbid}.flac   ← 임시 파일
            └─ shutil.copy2
                 └─ data/music/{Artist}/{Album}/{Track}.flac  ← 영구 저장
                      └─ mutagen (album 태그 업데이트 + 앨범아트 임베딩)
                           └─ Navidrome 스캔 → 라이브러리 반영
```

staging 파일은 복사 성공/실패 후 삭제됨.

---

## 6. 설정 구조

config 파일 없이 환경변수만으로 동작합니다. `.env` 파일 또는 docker-compose `environment`로 주입합니다.

| 환경변수 | 기본값 | 필수 |
|----------|--------|------|
| `LB_USERNAME` | `""` | 필수 |
| `LB_TOKEN` | `""` | 필수 |
| `NAVIDROME_URL` | `http://navidrome:4533/navidrome` | |
| `NAVIDROME_USER` | `admin` | |
| `NAVIDROME_PASSWORD` | `""` | 필수 |
