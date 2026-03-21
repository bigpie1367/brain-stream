# 데이터 모델

- **버전**: 2.0.0
- **작성일**: 2026-03-21

---

## 1. state.db (SQLite)

다운로드 상태를 추적하는 메인 데이터베이스.

**경로**: 컨테이너 내 `/app/db/state.db` (named Docker volume `db-data` 마운트). 호스트에서 직접 접근 불가 — `docker compose exec brainstream sqlite3 /app/db/state.db` 사용.

### 테이블: `downloads`

```sql
CREATE TABLE IF NOT EXISTS downloads (
    mbid          TEXT PRIMARY KEY,       -- MusicBrainz recording UUID 또는 "manual-{uuid8}"
    track_name    TEXT NOT NULL,          -- 트랙명
    artist        TEXT NOT NULL,          -- 아티스트명
    status        TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    downloaded_at TEXT,                   -- UTC ISO 8601, 완료 시 기록
    error_msg     TEXT,                   -- 실패 사유
    source        TEXT DEFAULT 'listenbrainz',  -- 'listenbrainz' | 'manual'
    file_path     TEXT,                   -- 임포트된 파일 경로 (beets 제거 후 직접 관리)
    album         TEXT,                   -- canonical 앨범명 (태깅 완료 후 기록)
    mb_recording_id TEXT                  -- MusicBrainz recording UUID (태깅 완료 후 기록, 수동 트랙은 null 가능)
);
```

### 컬럼 상세

| 컬럼 | 타입 | 설명 | 예시 |
|------|------|------|------|
| mbid | TEXT (PK) | LB 트랙: MusicBrainz recording UUID. 수동: `manual-{8자 hex}` | `a3e48b38-...` / `manual-a1b2c3d4` |
| track_name | TEXT | 트랙명. 태깅 완료 후 canonical title(iTunes/MB/Deezer)로 업데이트됨 | `Creep` |
| artist | TEXT | 아티스트명. 태깅 완료 후 canonical artist(MB/iTunes/Deezer)로 업데이트됨 | `Radiohead` |
| status | TEXT | 현재 처리 상태 (`pending` / `queued` / `downloading` / `done` / `failed` / `ignored`) | `done` |
| attempts | INTEGER | 총 시도 횟수 (실패 시 증가) | `1` |
| downloaded_at | TEXT | 성공 완료 시각 (UTC) | `2026-03-04T10:23:45` |
| error_msg | TEXT | 마지막 실패 사유 | `download failed` |
| source | TEXT | 트랙 출처 | `listenbrainz` / `manual` |
| file_path | TEXT | 임포트된 파일 경로. 삭제 API 및 enrichment에서 사용 | `/app/data/music/Radiohead/Pablo Honey/Creep.flac` |
| album | TEXT | 태깅 완료 후 canonical album명으로 업데이트됨 | `Pablo Honey` |
| mb_recording_id | TEXT | MusicBrainz recording UUID. LB 트랙은 mbid와 동일 값 저장. 수동 트랙은 MB 매칭 성공 시 기록, 실패 시 null | `3c3e5e5c-1234-5678-abcd-ef0123456789` |

---

## 2. 상태 전이도

```
                  ┌──────────┐
                  │  (시작)   │
                  └────┬─────┘
                       │ mark_pending()
                       ▼
                  ┌──────────┐
                  │ pending  │
                  └────┬─────┘
                       │ enqueue_job() → _work_queue
                       ▼
                  ┌──────────┐
                  │  queued  │  ← SSE 이벤트만, DB는 pending 유지
                  └────┬─────┘
                       │ mark_downloading()
                       ▼
                  ┌──────────────┐
         ┌────────│ downloading  │
         │        └──────┬───────┘
         │  크래시        │ 정상 완료/실패
         │        ┌──────┴──────────┐
         │        ▼                 ▼
         │   ┌────────┐        ┌──────────┐
         │   │  done  │        │  failed  │◄── attempts 증가
         │   └───┬────┘        └──────┬───┘
         │       │ DELETE              │ attempts < 3?
         │       ▼                    ├─ Yes → 다음 파이프라인 재시도
         │  ┌─────────┐               └─ No  → 영구 실패
         │  │ ignored │
         │  └─────────┘
         │
         └─► mark_failed("interrupted by restart")  ← 재시작 복구
               attempts++
               attempts < 3 → 재큐 (pending으로 재처리)
               attempts ≥ 3 → failed 유지 (재큐 안 함)
```

**참고**:
- `queued` 상태는 SSE 이벤트로만 표시. DB 컬럼 `status`에는 `pending`으로 유지됨
- 재시작 시 `downloading` 잡은 크래시로 중단된 것으로 간주하여 `attempts`를 증가시킴

---

## 3. 파일시스템 구조

```
db/                             # named volume (db-data:/app/db)
└── state.db                    # 다운로드 상태 DB (file_path 컬럼 포함)

data/
├── staging/                    # 임시 다운로드 디렉토리
│   └── {mbid}.flac             # 처리 완료 후 자동 삭제
├── music/                      # 최종 음악 라이브러리 (Navidrome이 읽음)
│   ├── {Artist}/
│   │   └── {Album}/
│   │       └── {Track}.flac
│   └── Unknown Artist/         # artist 정보 없는 경우 fallback
│       └── {Track}.flac
├── navidrome/                  # Navidrome 자체 DB 및 캐시
│   ├── navidrome.db
│   └── cache/
│       ├── images/             # 앨범아트 캐시
│       └── backgrounds/
└── logs/
    └── music-bot.log           # music-bot 애플리케이션 로그
```

---

## 4. 주요 state.py 함수

| 함수 | 설명 |
|------|------|
| `mark_pending(mbid, track_name, artist, source)` | 새 다운로드 레코드 생성 (status=pending) |
| `mark_pending_if_not_duplicate(mbid, track_name, artist, source)` | 동일 artist+track의 done/downloading/pending 레코드가 없을 때만 INSERT. 중복 시 기존 레코드 반환, 신규 시 None 반환. 단일 트랜잭션으로 원자적 처리 |
| `mark_downloading(mbid)` | status를 downloading으로 전이 |
| `mark_done(mbid, album)` | status=done, downloaded_at 기록, album 저장 |
| `mark_failed(mbid, error_msg)` | status=failed, attempts 증가, error_msg 기록 |
| `get_download_by_mbid(mbid)` | 단일 레코드 조회 |
| `get_all_downloads()` | 전체 레코드 조회 (최신 순, ignored 제외) |
| `get_downloads_page(limit, offset, search)` | 페이지네이션 조회. `{"items": [...], "total": int, "limit": int, "offset": int}` 반환. ignored 상태 제외. search로 artist/track_name/album LIKE 검색 |
| `find_active_download(artist, track_name)` | artist+track_name이 일치하는 done/downloading/pending 레코드 검색. 중복 다운로드 방지에 사용 |
| `update_track_info(mbid, ...)` | artist/file_path/album/mb_recording_id 선택적 업데이트 |
| `get_pending_jobs()` | pending/downloading 잡을 rowid ASC 순서로 반환 (재시작 복구용) |
