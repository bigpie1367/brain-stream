# API 명세서

- **버전**: 1.2.0
- **Base URL**: `http://localhost:8080`
- **작성일**: 2026-03-10

---

## 엔드포인트 목록

| Method | Path | 설명 |
|--------|------|------|
| GET | `/` | Web UI |
| POST | `/api/download` | 수동 다운로드 시작 |
| GET | `/api/sse/{job_id}` | SSE 실시간 진행 스트림 |
| GET | `/api/downloads` | 다운로드 이력 조회 |
| DELETE | `/api/downloads/{mbid}` | 트랙 삭제 (파일 + state.db) |
| POST | `/api/pipeline/run` | LB 파이프라인 수동 트리거 |
| GET | `/api/rematch/search` | 앨범 재매칭 후보 검색 |
| POST | `/api/rematch/apply` | 선택한 앨범으로 재태깅 |

---

## GET `/`

Web UI HTML을 반환한다.

**Response**: `text/html; charset=utf-8`

---

## POST `/api/download`

아티스트와 트랙명을 입력받아 수동 다운로드 잡을 시작한다.

**Request Body** (`application/json`)

```json
{
  "artist": "Radiohead",
  "track": "Creep"
}
```

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| artist | string | Y | 아티스트명 |
| track | string | Y | 트랙명 |

**Response** `200 OK`

```json
{
  "job_id": "manual-a1b2c3d4"
}
```

| 필드 | 타입 | 설명 |
|------|------|------|
| job_id | string | SSE 스트림 연결에 사용. 형식: `manual-{uuid8}` |

**Error Responses**

| Status | 설명 |
|--------|------|
| 503 | 서버 설정 미로드 (기동 직후 일시적) |

---

## GET `/api/sse/{job_id}`

특정 잡의 진행 상황을 실시간으로 수신한다 (Server-Sent Events).

**Path Parameters**

| 파라미터 | 설명 |
|---------|------|
| job_id | `/api/download` 응답으로 받은 job_id |

**Response Headers**

```
Content-Type: text/event-stream
Cache-Control: no-cache
X-Accel-Buffering: no
```

**SSE Event Format**

```
data: {"status": "downloading", "message": "YouTube 검색 중..."}

data: {"status": "tagging", "message": "beets 태깅 중..."}

data: {"status": "scanning", "message": "Navidrome 스캔 중..."}

data: {"status": "done", "message": "완료"}
```

**SSE Event 타입**

| status | 설명 |
|--------|------|
| `downloading` | YouTube 검색 및 다운로드 중 |
| `tagging` | mutagen 태깅 중 |
| `scanning` | Navidrome 라이브러리 스캔 중 |
| `done` | 모든 단계 완료 |
| `failed` | 처리 중 오류 발생 |

`done` 또는 `failed` 수신 후 연결이 종료된다.

**Keep-alive**: 30초 이상 이벤트가 없으면 `: keep-alive` 주석 전송.

**Error Responses**

| Status | 설명 |
|--------|------|
| 404 | job_id가 존재하지 않음 |

---

## GET `/api/downloads`

전체 다운로드 이력을 최신 순으로 반환한다 (최대 100건).

**Response** `200 OK` (`application/json`)

```json
[
  {
    "mbid": "a3e48b38-1234-5678-abcd-ef0123456789",
    "track_name": "Creep",
    "artist": "Radiohead",
    "status": "done",
    "source": "listenbrainz",
    "attempts": 1,
    "downloaded_at": "2026-03-04T10:23:45.123456",
    "error_msg": null
  },
  {
    "mbid": "manual-a1b2c3d4",
    "track_name": "밤편지",
    "artist": "IU",
    "status": "done",
    "source": "manual",
    "attempts": 1,
    "downloaded_at": "2026-03-04T11:00:00.000000",
    "error_msg": null
  }
]
```

**응답 필드**

| 필드 | 타입 | 설명 |
|------|------|------|
| mbid | string | LB 트랙은 MusicBrainz recording UUID, 수동 트랙은 `manual-{uuid8}` |
| track_name | string | 트랙명 |
| artist | string | 아티스트명 |
| status | string | `pending` / `downloading` / `done` / `failed` |
| source | string | `listenbrainz` / `manual` |
| attempts | integer | 시도 횟수 |
| downloaded_at | string \| null | 완료 시각 (UTC ISO 8601), 미완료 시 null |
| error_msg | string \| null | 실패 사유, 성공 시 null |

**Error Responses**

| Status | 설명 |
|--------|------|
| 503 | 서버 설정 미로드 |

---

## DELETE `/api/downloads/{mbid}`

라이브러리에서 트랙을 삭제한다. 파일을 제거하고 state.db 레코드를 삭제한 후 Navidrome 재스캔을 트리거한다.

**Path Parameters**

| 파라미터 | 설명 |
|---------|------|
| mbid | state.db의 mbid (LB 트랙: MB UUID, 수동 트랙: `manual-{uuid8}`) |

**Response** `200 OK`

```json
{
  "deleted": true,
  "files_removed": 1
}
```

| 필드 | 타입 | 설명 |
|------|------|------|
| deleted | boolean | state.db 레코드 삭제 여부 |
| files_removed | integer | 실제 삭제된 파일 수 (파일이 없던 경우 0) |

**동작 순서**

1. state.db에서 mbid로 file_path 조회
2. 파일이 존재하면 삭제
3. state.db에서 레코드 삭제
4. Navidrome 재스캔 트리거 (파일이 삭제된 경우)

**Error Responses**

| Status | 설명 |
|--------|------|
| 404 | mbid가 state.db에 없음 |
| 503 | 서버 설정 미로드 |

---

## POST `/api/pipeline/run`

ListenBrainz 파이프라인을 즉시 수동으로 실행한다.

**Request Body**: 없음

**Response** `200 OK`

```json
{
  "status": "started"
}
```

파이프라인은 백그라운드 스레드에서 비동기 실행된다. 실제 완료 여부는 로그 또는 `/api/downloads`로 확인.

**Error Responses**

| Status | 설명 |
|--------|------|
| 503 | 서버 설정 미로드 |

---

## GET `/api/rematch/search`

라이브러리 트랙의 앨범 재매칭을 위해 MusicBrainz에서 후보 앨범 목록을 검색한다.

**Query Parameters**

| 파라미터 | 타입 | 필수 | 설명 |
|---------|------|------|------|
| artist | string | Y | 아티스트명 |
| track | string | Y | 트랙명 |
| source | string | N | 검색 소스. 현재 `musicbrainz`만 지원 (기본값: `musicbrainz`) |

**Response** `200 OK`

```json
{
  "candidates": [
    {
      "source": "musicbrainz",
      "mb_recording_id": "3c3e5e5c-1234-5678-abcd-ef0123456789",
      "mb_album_id": "7a1b2c3d-abcd-ef01-2345-678901234567",
      "album_name": "OK Computer",
      "artist_name": "Radiohead",
      "year": 1997,
      "cover_url": "https://coverartarchive.org/release/7a1b2c3d-abcd-ef01-2345-678901234567/front"
    }
  ]
}
```

| 필드 | 타입 | 설명 |
|------|------|------|
| source | string | 검색 소스 (`musicbrainz`) |
| mb_recording_id | string \| null | MusicBrainz recording UUID |
| mb_album_id | string \| null | MusicBrainz release UUID (커버아트 조회에 사용) |
| album_name | string | 앨범명 |
| artist_name | string | 아티스트명 |
| year | integer \| null | 발매 연도 |
| cover_url | string \| null | Cover Art Archive 커버아트 URL |

매칭 결과가 없으면 `candidates: []` 반환.

**Error Responses**

| Status | 설명 |
|--------|------|
| 400 | 지원하지 않는 source |
| 503 | 서버 설정 미로드 |

---

## POST `/api/rematch/apply`

선택한 앨범 후보로 트랙을 재태깅한다. 앨범명 + 커버아트를 파일에 임베딩하고 Navidrome 재스캔을 트리거한다.

**Request Body** (`application/json`)

```json
{
  "song_id": "navidrome-song-id",
  "mb_recording_id": "3c3e5e5c-1234-5678-abcd-ef0123456789",
  "mb_album_id": "7a1b2c3d-abcd-ef01-2345-678901234567"
}
```

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| song_id | string | Y | Navidrome Subsonic song ID (라이브러리 탭에서 획득) |
| mb_recording_id | string | Y | `/api/rematch/search` 응답의 `mb_recording_id` |
| mb_album_id | string | Y | `/api/rematch/search` 응답의 `mb_album_id` |

**동작 순서**

1. Navidrome `getSong(song_id)` → `path` 필드로 파일 절대경로 획득
2. MB API로 release 정보 조회 → 앨범명 확인
3. mutagen으로 `album` 태그 재기록 (`mb_albumid`는 기록하지 않음 — Navidrome 앨범 분리 방지)
4. Cover Art Archive에서 커버아트 다운로드 → mutagen 임베딩 (실패 시 warning만, 전체 실패 아님)
5. Navidrome `startScan` 트리거

**Response** `200 OK`

```json
{
  "status": "ok",
  "album_name": "OK Computer"
}
```

**Error Responses**

| Status | 설명 |
|--------|------|
| 404 | song_id가 Navidrome에 없음 |
| 500 | 파일 재태깅 실패 |
| 503 | 서버 설정 미로드 |

---

## 상태(status) 전이

```
pending → downloading → (tagging) → done
                    └─────────────→ failed (최대 3회 재시도)
```

`attempts < 3`인 `failed` 상태는 다음 파이프라인 실행 시 자동 재시도됨.
