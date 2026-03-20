# Code Quality Fixes — Design Spec

**Date**: 2026-03-20
**Scope**: Issues #1-8 (심각 + 높음)
**Strategy**: 4개 순차 PR, 의미 단위 묶음

---

## PR1 — 데이터 정합성

### #1 FLAC artist 태그 타입 통일

**파일**: `src/pipeline/tagger.py`

`_write_tags`의 FLAC 분기(line ~380)에서 artist를 문자열로 쓰는 버그 수정:

```python
# Before
f["artist"] = artist        # str — FLAC만 불일치

# After
f["artist"] = [artist]      # list — Opus/OGG와 동일
```

`_read_tags`는 이미 `(get() or [""])[0]`으로 리스트 기반 읽기를 하므로 하위 호환성 문제 없음.

### #2 mark_done album 덮어쓰기 방지

**파일**: `src/state.py`

`mark_done()` SQL을 조건부 업데이트로 변경:

```sql
-- Before
SET status = 'done', downloaded_at = ?, file_path = ?, album = ?

-- After
SET status = 'done', downloaded_at = ?, file_path = ?, album = COALESCE(?, album)
```

`album=None`으로 호출 시 기존 값 유지. 명시적 album 삭제 유스케이스는 현재 없음.

추가: `datetime.utcnow()` → `datetime.now(datetime.UTC)` 수정 (#13 함께 해결).

---

## PR2 — async/httpx 전환

### #6 httpx.AsyncClient 공유

**파일**: `src/api.py`

FastAPI lifespan 핸들러에서 공유 클라이언트 생성/종료:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(timeout=60.0)
    yield
    await app.state.http_client.aclose()
```

프록시 엔드포인트 3곳(line ~979, 1051, 1108)에서 `request.app.state.http_client` 사용.

### #3 blocking I/O → async 전환

**파일**: `src/api.py`

`rematch_search`, `rematch_apply`, 및 이들이 호출하는 헬퍼(`_navidrome_get_song` 등) 내부 변경:

| Before | After |
|--------|-------|
| `requests.get()` | `await request.app.state.http_client.get()` |
| `time.sleep(1)` | `await asyncio.sleep(1)` |

`_navidrome_get_song`도 `requests.get`을 사용하므로 async 전환 대상에 포함.
호출 체인의 모든 `requests` 사용처를 httpx async로 전환해야 `requests` import를 제거할 수 있음.

`edit_metadata`의 mutagen 파일 I/O는 단건 ms 단위이므로 현 상태 유지.

`requests` import는 전환 완료 후 미사용 시 제거.

---

## PR3 — tagger 리팩토링

### #4 포맷 디스패치 딕셔너리

**파일**: `src/pipeline/tagger.py`

모듈 상단에 포맷 감지 + 디스패치 테이블 정의:

```python
def _detect_format(path: str) -> str:
    """확장자 기반 포맷 감지. 'flac' | 'opus' | 'mp4' | 'generic' 반환"""

_FORMAT_KEYS = {
    "flac":    {"artist": "artist", "title": "title", "album": "album",
                "mb_trackid": "musicbrainz_trackid"},
    "opus":    {"artist": "artist", "title": "title", "album": "album",
                "mb_trackid": "musicbrainz_trackid"},
    "mp4":     {"artist": "\u00a9ART", "title": "\u00a9nam", "album": "\u00a9alb",
                "mb_trackid": "----:com.apple.iTunes:MusicBrainz Track Id"},
    "generic": {"artist": "artist", "title": "title", "album": "album",
                "mb_trackid": "musicbrainz_trackid"},
}

_FORMAT_OPENER = {
    "flac": mutagen.flac.FLAC,
    "opus": mutagen.oggopus.OggOpus,
    "mp4":  mutagen.mp4.MP4,
    "generic": mutagen.File,
}
```

**통합 대상 (8개 → 3개 패턴):**

1. **write 계열 5개** (`_write_tags`, `_write_mb_trackid_tag`, `_write_album_tag`, `_write_artist_tag`, `_write_title_tag`)
   → 공통 `_write_tag(path, key_name, value)` + 포맷별 값 래핑
   - FLAC/Opus/OGG: `[value]` (리스트)
   - MP4 mb_trackid: `MP4FreeForm(value.encode(), ...)`
   - MP4 일반: `[value]`
   - `_write_tags`는 여러 키를 한 번에 쓰는 batch 래퍼로 유지

2. **_read_tags** → `_detect_format` + `_FORMAT_KEYS` + `_FORMAT_OPENER`로 단일 함수

3. **embed 2개** (`_embed_cover_art`, `_embed_art_from_url`)
   → 디스패치 딕셔너리 + 포맷별 임베더 함수 (`_embed_flac`, `_embed_opus`, `_embed_mp4`)

**public alias 정리:** 9개 alias 제거, 함수를 처음부터 public으로 선언 (underscore 제거).

### #7 sanitize 함수 통일

**새 파일**: `src/utils.py`
**변경 파일**: `src/pipeline/tagger.py`, `src/api.py`

```python
# src/utils.py
def sanitize_path_component(name: str, default: str = "_", max_len: int = 255) -> str:
    sanitized = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", name)
    sanitized = sanitized.strip(". ")
    return (sanitized or default)[:max_len]
```

tagger.py의 `_sanitize_filename`과 api.py의 `_sanitize_path_component`를 이 함수로 교체.
기본값은 tagger 기준 (`"_"`, 255자 제한).

---

## PR4 — api.py 구조 분리

### #5 _run_download_job 분리

**새 파일**: `src/jobs.py`
**변경 파일**: `src/api.py`

`api.py`에서 `_run_download_job` (line ~137-238)과 내부 전용 헬퍼들을 `src/jobs.py`로 이동.

`jobs.py` imports:
- `src/pipeline/tagger` — 태깅/enrichment
- `src/state` — DB 상태 업데이트
- `src/worker` — emit (SSE)
- `src/config` — 설정값

`api.py`는 `worker.enqueue_job()`에 `jobs.run_download_job`을 콜백으로 전달.
api.py에는 라우트 정의만 남김.

### #8 파일 이동 공통 헬퍼

**변경 파일**: `src/utils.py` (PR3에서 생성), `src/api.py`, `src/jobs.py`

```python
# src/utils.py
def move_to_music_dir(
    src_path: str,
    music_dir: str,
    artist: str,
    album: str,
    filename: str,
) -> str:
    """staging → music_dir/{artist}/{album}/{filename} 이동.
    resolve_dir로 기존 폴더 case-insensitive 매칭,
    없으면 sanitize_path_component로 새 폴더 생성.
    Returns: 최종 file_path
    """
```

`_resolve_dir`도 `api.py`에서 `utils.py`로 이동.

`rematch_apply`, `edit_metadata`, `jobs.run_download_job` 모두 `move_to_music_dir()` 호출로 통일.

**의도된 동작 변경**: `edit_metadata`는 현재 `_resolve_dir`을 사용하지 않아 같은 아티스트가 대소문자 차이로 별도 폴더에 들어갈 수 있음. `move_to_music_dir`을 적용하면 `edit_metadata`에도 case-insensitive 폴더 매칭이 추가됨. 이는 #8의 목적(폴더 분리 방지) 자체이므로 의도된 변경.

---

## PR 의존성

```
PR1 (데이터 정합성)  ──독립──  PR2 (async/httpx)
                                    │
PR3 (tagger 리팩토링) ←─────────────┘ (선후관계 없으나 PR2 먼저 권장)
        │
        ▼
PR4 (api.py 구조 분리)  ← PR3의 utils.py에 의존
```

PR1과 PR2는 병렬 진행 가능. PR3→PR4는 순서 의존.

---

## 범위 외 (중간/낮음 — 후속 작업)

다음 이슈는 이번 스코프에서 제외. 별도 PR로 후속 처리:

- #9 MB User-Agent 불일치
- #10 _MB_API/_MB_HEADERS 상수 중복
- #11 _lookup_recording 로직 중복
- #12 rematch_apply update_track_info 2회 호출
- #14 BeetsConfig 네이밍
- #15 navidrome.py random 사용
- #16 Public alias 패턴 (PR3에서 해결됨)
- #17 .dockerignore 없음
- #18 Health check 엔드포인트 없음
- #19 Rate limiter 메모리 누수
- #20 get_all_downloads 페이지네이션
- #21 수동 다운로드 중복 체크
