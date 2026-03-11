# 데이터 모델

- **버전**: 1.0.0
- **작성일**: 2026-03-04

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
    file_path     TEXT                    -- 임포트된 파일 경로 (beets 제거 후 직접 관리)
);
```

### 컬럼 상세

| 컬럼 | 타입 | 설명 | 예시 |
|------|------|------|------|
| mbid | TEXT (PK) | LB 트랙: MusicBrainz recording UUID. 수동: `manual-{8자 hex}` | `a3e48b38-...` / `manual-a1b2c3d4` |
| track_name | TEXT | 트랙명. 태깅 완료 후 canonical title(iTunes/MB/Deezer)로 업데이트됨 | `Creep` |
| artist | TEXT | 아티스트명. 태깅 완료 후 canonical artist(MB/iTunes/Deezer)로 업데이트됨 | `Radiohead` |
| status | TEXT | 현재 처리 상태 (`pending` / `downloading` / `done` / `failed` / `ignored`) | `done` |
| attempts | INTEGER | 총 시도 횟수 (실패 시 증가) | `1` |
| downloaded_at | TEXT | 성공 완료 시각 (UTC) | `2026-03-04T10:23:45` |
| error_msg | TEXT | 마지막 실패 사유 | `download failed` |
| source | TEXT | 트랙 출처 | `listenbrainz` / `manual` |
| file_path | TEXT | 임포트된 파일 경로. 삭제 API 및 enrichment에서 사용 | `/app/data/music/Radiohead/Pablo Honey/Creep.flac` |

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
                       │ mark_downloading()  ← 수동 다운로드 시
                       ▼
                  ┌──────────────┐
                  │ downloading  │
                  └──────┬───────┘
           성공           │           실패
    ┌──────────────────────┤
    ▼                      ▼
┌────────┐           ┌──────────┐
│  done  │           │  failed  │◄──── attempts 증가
└───┬────┘           └──────┬───┘
    │                       │
    │ DELETE /api/downloads  │ attempts < 3?
    ▼                       ├─ Yes → 다음 파이프라인 실행 시 재시도
┌─────────┐                 └─ No  → 영구 실패 (더 이상 재시도 안 함)
│ ignored │  ← 파일 삭제, DB 레코드 유지
└─────────┘  ← 파이프라인이 done과 동일하게 스킵
```

**주의**: `pending`에서 `downloading`을 거치지 않고 바로 `done`/`failed`로 전이될 수 있음 (LB 자동 파이프라인).

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
