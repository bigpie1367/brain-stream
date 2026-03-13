# API 명세서

- **버전**: 1.7.0
- **Base URL**: `http://localhost:8080`
- **작성일**: 2026-03-13

---

## 엔드포인트 목록

| Method | Path | 설명 |
|--------|------|------|
| GET | `/` | Web UI |
| GET | `/api/download/candidates` | YouTube 후보 목록 검색 (다운로드 없음) |
| POST | `/api/download` | 수동 다운로드 시작 |
| GET | `/api/sse/{job_id}` | SSE 실시간 진행 스트림 |
| GET | `/api/downloads` | 다운로드 이력 조회 |
| DELETE | `/api/downloads/{mbid}` | 트랙 삭제 (파일 제거 + state.db를 ignored로 마킹) |
| POST | `/api/pipeline/run` | LB 파이프라인 수동 트리거 |
| GET | `/api/downloads/{mbid}/detail` | 트랙 상세 정보 조회 (앨범명, 연도, 커버아트) |
| GET | `/api/rematch/search` | 앨범 재매칭 후보 검색 |
| POST | `/api/rematch/apply` | 선택한 앨범으로 재태깅 |
| GET | `/api/stream/{mbid}` | 다운로드된 트랙 오디오 스트리밍 |

---

## GET `/`

Web UI HTML을 반환한다.

**Response**: `text/html; charset=utf-8`

---

## GET `/api/download/candidates`

yt-dlp로 YouTube를 검색하여 후보 영상 5개의 메타데이터를 반환한다. 실제 다운로드는 수행하지 않는다.

**Query Parameters**

| 파라미터 | 타입 | 필수 | 설명 |
|---------|------|------|------|
| artist | string | Y | 아티스트명 |
| track | string | Y | 트랙명 |

**Response** `200 OK` (`application/json`)

```json
{
  "candidates": [
    {
      "video_id": "XFkzRNyygfk",
      "title": "Radiohead - Creep",
      "channel": "Radiohead",
      "duration": 238,
      "thumbnail_url": "https://i.ytimg.com/vi/XFkzRNyygfk/hqdefault.jpg",
      "url": "https://www.youtube.com/watch?v=XFkzRNyygfk",
      "is_live": false,
      "is_cover": false
    }
  ]
}
```

**응답 필드 (candidates 항목)**

| 필드 | 타입 | 설명 |
|------|------|------|
| video_id | string | YouTube 영상 ID |
| title | string | 영상 제목 |
| channel | string | 업로드 채널명 |
| duration | integer | 영상 길이 (초) |
| thumbnail_url | string | 썸네일 이미지 URL |
| url | string | 영상 전체 URL |
| is_live | boolean | 라이브/공연 영상 여부 |
| is_cover | boolean | 커버곡 영상 여부 |

검색 결과가 없으면 `candidates: []` 반환.

**Error Responses**

| Status | 설명 |
|--------|------|
| 503 | 서버 설정 미로드 |

---

## POST `/api/download`

아티스트와 트랙명을 입력받아 수동 다운로드 잡을 시작한다.

**Request Body** (`application/json`)

```json
{
  "artist": "Radiohead",
  "track": "Creep",
  "video_id": "XFkzRNyygfk"
}
```

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| artist | string | Y | 아티스트명 |
| track | string | Y | 트랙명 |
| video_id | string | N | `/api/download/candidates` 응답의 `video_id`. 지정 시 해당 영상을 직접 다운로드. 미지정 시 자동 선택 (기존 동작) |

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
data: {"status": "queued", "message": "다운로드 대기 중..."}

data: {"status": "downloading", "message": "YouTube 검색 중..."}

data: {"status": "tagging", "message": "태깅 중..."}

data: {"status": "scanning", "message": "Navidrome 스캔 중..."}

data: {"status": "done", "message": "완료"}
```

**SSE Event 타입**

| status | 설명 |
|--------|------|
| `queued` | 작업 큐에 적재됨, 워커 픽업 대기 중 |
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
    "album": "Pablo Honey",
    "status": "done",
    "source": "listenbrainz",
    "attempts": 1,
    "downloaded_at": "2026-03-04T10:23:45.123456",
    "error_msg": null,
    "file_path": "/app/data/music/Radiohead/Pablo Honey/Creep.flac",
    "mb_recording_id": "a3e48b38-1234-5678-abcd-ef0123456789"
  },
  {
    "mbid": "manual-a1b2c3d4",
    "track_name": "밤편지",
    "artist": "IU",
    "album": "밤의 편지",
    "status": "done",
    "source": "manual",
    "attempts": 1,
    "downloaded_at": "2026-03-04T11:00:00.000000",
    "error_msg": null,
    "file_path": "/app/data/music/IU/밤의 편지/밤편지.flac",
    "mb_recording_id": null
  }
]
```

**응답 필드**

| 필드 | 타입 | 설명 |
|------|------|------|
| mbid | string | LB 트랙은 MusicBrainz recording UUID, 수동 트랙은 `manual-{uuid8}` |
| track_name | string | 트랙명 |
| artist | string | 아티스트명 |
| album | string \| null | canonical 앨범명. 태깅 완료 전 null |
| status | string | `pending` / `queued` / `downloading` / `done` / `failed` / `ignored` |
| source | string | `listenbrainz` / `manual` |
| attempts | integer | 시도 횟수 |
| downloaded_at | string \| null | 완료 시각 (UTC ISO 8601), 미완료 시 null |
| error_msg | string \| null | 실패 사유, 성공 시 null |
| file_path | string \| null | 임포트된 파일의 컨테이너 내 절대 경로. 미완료 시 null |
| mb_recording_id | string \| null | MusicBrainz recording UUID. LB 트랙은 mbid와 동일. 수동 트랙은 MB 매칭 성공 시 기록, 실패 시 null |

**Error Responses**

| Status | 설명 |
|--------|------|
| 503 | 서버 설정 미로드 |

---

## DELETE `/api/downloads/{mbid}`

라이브러리에서 트랙을 삭제한다. 물리적 파일을 제거하고 state.db 레코드를 `ignored`로 마킹한 후 Navidrome 재스캔을 트리거한다.

> **참고**: 레코드를 완전히 삭제하지 않고 `ignored`로 마킹하므로, 이후 LB 파이프라인이 같은 mbid를 추천해도 재다운로드하지 않는다.

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
| deleted | boolean | state.db 레코드가 `ignored`로 마킹됐는지 여부 |
| files_removed | integer | 실제 삭제된 파일 수 (파일이 없던 경우 0) |

**동작 순서**

1. state.db에서 mbid로 file_path 조회
2. 파일이 존재하면 삭제. 삭제 후 비어진 앨범 폴더 → 아티스트 폴더 순으로 자동 정리
3. state.db 레코드를 `ignored`로 마킹 (레코드 자체는 유지)
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

## GET `/api/downloads/{mbid}/detail`

라이브러리에 저장된 트랙의 상세 메타데이터를 파일에서 직접 읽어 반환한다.

**Path Parameters**

| 파라미터 | 설명 |
|---------|------|
| mbid | state.db의 mbid (LB 트랙: MB UUID, 수동 트랙: `manual-{uuid8}`) |

**Response** `200 OK`

```json
{
  "album_name": "Pablo Honey",
  "year": "1993",
  "cover_art": "data:image/jpeg;base64,/9j/4AAQSkZJRg..."
}
```

| 필드 | 타입 | 설명 |
|------|------|------|
| album_name | string \| null | 파일의 album 태그 |
| year | string \| null | 파일의 date 또는 year 태그 |
| cover_art | string \| null | `data:{mime};base64,{...}` 형식의 임베드 커버아트. 없으면 null |

파일이 없거나 경로가 없는 경우 세 필드 모두 null로 반환한다.

**Error Responses**

| Status | 설명 |
|--------|------|
| 404 | mbid가 state.db에 없음 |
| 503 | 서버 설정 미로드 |

---

## GET `/api/rematch/search`

라이브러리 트랙의 앨범 재매칭을 위해 MusicBrainz, iTunes US, iTunes KR 세 소스에서 후보 앨범 목록을 한 번에 검색한다.

**Query Parameters**

| 파라미터 | 타입 | 필수 | 설명 |
|---------|------|------|------|
| artist | string | Y | 아티스트명 |
| track | string | Y | 트랙명 |

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
    },
    {
      "source": "itunes",
      "mb_recording_id": "",
      "mb_album_id": "",
      "album_name": "OK Computer",
      "artist_name": "Radiohead",
      "year": "",
      "cover_url": "https://is1-ssl.mzstatic.com/.../600x600bb.jpg"
    },
    {
      "source": "itunes-kr",
      "mb_recording_id": "",
      "mb_album_id": "",
      "album_name": "가슴이 차가운 여자 - Single",
      "artist_name": "류민희",
      "year": "",
      "cover_url": "https://is1-ssl.mzstatic.com/.../600x600bb.jpg"
    }
  ]
}
```

**소스별 동작**

| source | 설명 |
|--------|------|
| `musicbrainz` | MusicBrainz recording 검색 (최대 10개). `mb_album_id`로 CAA 커버아트 사용 가능 |
| `itunes` | iTunes US 스토어 검색 (아티스트 유사도 0.4 이상 첫 결과). `mb_album_id` 없음 |
| `itunes-kr` | iTunes KR 스토어 검색. US와 앨범명이 동일하면 중복 제거 |

**응답 필드**

| 필드 | 타입 | 설명 |
|------|------|------|
| source | string | 검색 소스 (`musicbrainz` / `itunes` / `itunes-kr`) |
| mb_recording_id | string | MusicBrainz recording UUID. iTunes 후보는 빈 문자열 |
| mb_album_id | string | MusicBrainz release UUID. iTunes 후보는 빈 문자열 |
| album_name | string | 앨범명 |
| artist_name | string | 아티스트명 |
| year | integer \| string | 발매 연도. iTunes 후보는 빈 문자열 |
| cover_url | string | 커버아트 URL |

매칭 결과가 없으면 `candidates: []` 반환.

**Error Responses**

| Status | 설명 |
|--------|------|
| 503 | 서버 설정 미로드 |

---

## POST `/api/rematch/apply`

선택한 앨범 후보로 트랙을 재태깅하고, 필요 시 파일을 새 경로로 이동한다.

**Request Body** (`application/json`)

```json
{
  "song_id": "navidrome-song-id",
  "mb_recording_id": "3c3e5e5c-1234-5678-abcd-ef0123456789",
  "mb_album_id": "7a1b2c3d-abcd-ef01-2345-678901234567",
  "album_name": "",
  "artist_name": "Radiohead",
  "cover_url": ""
}
```

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| song_id | string | \* | Navidrome song ID (라이브러리 탭). `mbid`와 둘 중 하나 필수 |
| mbid | string | \* | state.db의 mbid (다운로드 탭). `song_id`와 둘 중 하나 필수 |
| mb_recording_id | string | Y | `/api/rematch/search` 응답의 `mb_recording_id` |
| mb_album_id | string | Y | MusicBrainz release UUID. iTunes 후보는 빈 문자열 |
| album_name | string | N | iTunes 후보일 때 앨범명 직접 전달 (`mb_album_id`가 비어있을 때 필수) |
| artist_name | string | N | 재매칭 후보의 아티스트명. 변경 시 파일 경로와 태그 모두 업데이트 |
| cover_url | string | N | iTunes 후보의 커버아트 URL (`mb_album_id`가 비어있을 때 사용) |

**동작 순서**

1. `mbid` → state.db에서 `file_path` 직접 조회 / `song_id` → Navidrome `getSong`으로 경로 획득
2. `mb_album_id`가 있으면 MB API로 release 조회 → 앨범명 확인. 없으면 `album_name` 필드 직접 사용
3. mutagen으로 `album` 태그 재기록 (`mb_albumid`는 기록 안 함 — Navidrome 앨범 분리 방지)
3a. `mb_recording_id`가 있으면 `write_mb_trackid_tag()`로 파일 포맷별 mb_trackid 태그 업데이트 (FLAC/Opus/MP4/기타 포맷 자동 감지). state.db `mb_recording_id` 컬럼도 해당 값으로 업데이트
4. `artist_name`이 있으면 mutagen으로 `artist` 태그도 재기록
5. 앨범명·아티스트명 기준으로 새 경로(`data/music/{Artist}/{Album}/{Track}.ext`) 계산 → 변경됐으면 `shutil.move`로 이동. state.db `file_path` / `artist` 업데이트. 이동 후 비어진 폴더 자동 정리
6. `mb_album_id`가 있으면 CAA 커버아트 임베딩. 없고 `cover_url`이 있으면 URL에서 다운로드 후 임베딩 (실패 시 warning만)
7. Navidrome `startScan` 트리거

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
| 404 | mbid/song_id가 없거나 파일이 존재하지 않음 |
| 422 | `mb_album_id`가 비어있는데 `album_name`도 미전달 |
| 500 | MB 조회 실패 / 태그 쓰기 실패 / 파일 이동 실패 |
| 503 | 서버 설정 미로드 |

---

## GET `/api/stream/{mbid}`

state.db에 기록된 `file_path`를 기반으로 다운로드된 트랙의 오디오를 스트리밍한다.

**Path Parameters**

| 파라미터 | 설명 |
|---------|------|
| mbid | state.db의 mbid (LB 트랙: MB UUID, 수동 트랙: `manual-{uuid8}`) |

**Response** `200 OK`

오디오 파일을 바이트 스트림으로 반환한다. `Content-Type`은 파일 확장자에 따라 결정된다.

| 확장자 | Content-Type |
|--------|-------------|
| `.flac` | `audio/flac` |
| `.opus` | `audio/ogg; codecs=opus` |
| 기타 | `audio/mpeg` |

**Error Responses**

| Status | 설명 |
|--------|------|
| 404 | mbid가 state.db에 없거나, `file_path`가 기록되지 않았거나, 파일이 존재하지 않음 |

---

## 상태(status) 전이

```
pending → queued(*) → downloading → (tagging) → done
                               └─────────────→ failed (최대 3회 재시도)

done → DELETE /api/downloads/{mbid} → ignored (파일 삭제, DB 레코드 유지)
```

(*) `queued`는 SSE 이벤트로만 표시. DB에는 `pending`으로 유지되다가 워커가 픽업하면 `downloading`으로 전이됨.

`attempts < 3`인 `failed` 상태는 다음 파이프라인 실행 시 자동 재시도됨.

`ignored` 상태는 `done`과 동일하게 파이프라인 스킵 대상. LB 파이프라인이 같은 mbid를 추천해도 재다운로드하지 않음.
