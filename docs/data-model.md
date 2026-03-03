# 데이터 모델

- **버전**: 1.0.0
- **작성일**: 2026-03-04

---

## 1. state.db (SQLite)

다운로드 상태를 추적하는 메인 데이터베이스.

**경로**: `data/state.db` (컨테이너 내 `/app/data/state.db`)

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
    source        TEXT DEFAULT 'listenbrainz'  -- 'listenbrainz' | 'manual'
);
```

### 컬럼 상세

| 컬럼 | 타입 | 설명 | 예시 |
|------|------|------|------|
| mbid | TEXT (PK) | LB 트랙: MusicBrainz recording UUID. 수동: `manual-{8자 hex}` | `a3e48b38-...` / `manual-a1b2c3d4` |
| track_name | TEXT | 트랙명 | `Creep` |
| artist | TEXT | 아티스트명 | `Radiohead` |
| status | TEXT | 현재 처리 상태 | `done` |
| attempts | INTEGER | 총 시도 횟수 (실패 시 증가) | `1` |
| downloaded_at | TEXT | 성공 완료 시각 (UTC) | `2026-03-04T10:23:45` |
| error_msg | TEXT | 마지막 실패 사유 | `download failed` |
| source | TEXT | 트랙 출처 | `listenbrainz` / `manual` |

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
└────────┘           └──────┬───┘
                            │
                  attempts < 3?
                  ├─ Yes → 다음 파이프라인 실행 시 재시도
                  └─ No  → 영구 실패 (더 이상 재시도 안 함)
```

**주의**: `pending`에서 `downloading`을 거치지 않고 바로 `done`/`failed`로 전이될 수 있음 (LB 자동 파이프라인).

---

## 3. beets.db (SQLite)

beets가 자체적으로 관리하는 음악 라이브러리 DB.

**경로**: `data/beets.db` (컨테이너 내 `/app/data/beets.db`)

beets 내부 포맷이므로 직접 수정하지 않음. beets CLI로만 접근:

```bash
# beets 라이브러리 목록
docker exec music-bot-temp-music-bot-1 beet list -f '$artist - $title [$album]'

# 특정 아티스트 파일 경로 확인
docker exec music-bot-temp-music-bot-1 beet list -f '$path' artist:Radiohead
```

---

## 4. 파일시스템 구조

```
data/
├── state.db                    # 다운로드 상태 DB (music-bot 관리)
├── beets.db                    # beets 라이브러리 DB (beets 관리)
├── staging/                    # 임시 다운로드 디렉토리
│   └── {mbid}.flac             # 처리 완료 후 자동 삭제
├── music/                      # 최종 음악 라이브러리 (Navidrome이 읽음)
│   ├── {Artist}/
│   │   └── {Album}/
│   │       └── {Track}.flac
│   └── Non-Album/              # 앨범 정보 없는 경우 fallback
│       └── {Artist}/
│           └── {Track}.flac
├── navidrome/                  # Navidrome 자체 DB 및 캐시
│   ├── navidrome.db
│   └── cache/
│       ├── images/             # 앨범아트 캐시
│       └── backgrounds/
└── logs/
    ├── beets-import.log        # beets import 로그 (offset 기반 skip 감지에 사용)
    └── music-bot.log           # music-bot 애플리케이션 로그
```

---

## 5. beets/state.pickle

beets 임포트 세션 상태. `beets/` 디렉토리 볼륨에 저장.

**경로**: `beets/state.pickle`

자동 관리되는 파일이므로 직접 수정 불필요. 문제 발생 시 삭제 후 재임포트 가능.
