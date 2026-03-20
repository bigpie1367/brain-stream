# Code Quality Fixes Round 2 — Design Spec

**Date**: 2026-03-20
**Scope**: Issues #9, #10, #11, #12, #14, #15, #16 (중간 우선순위)
**Strategy**: 2개 PR

---

## PR1 — MB API 통합 + alias 정리 (#9, #10, #11, #16)

### 신규 모듈: `src/pipeline/musicbrainz.py`

MB API와의 통신을 한 곳으로 격리:

```python
# src/pipeline/musicbrainz.py
"""MusicBrainz API client — shared constants and lookup functions."""
import time
import requests
from src.utils.logger import get_logger

log = get_logger(__name__)

MB_API = "https://musicbrainz.org/ws/2"
MB_HEADERS = {"User-Agent": "brainstream/1.0 (https://github.com/bigpie1367/brain-stream)"}
```

**User-Agent 통일**: `"brainstream/1.0 (https://github.com/bigpie1367/brain-stream)"` — MB 정책에 맞는 `appname/version (contact-url)` 포맷. 기존 4곳의 불일치를 해소.

### #11 lookup_recording 통합

`tagger.py:_lookup_recording_by_mbid`와 `listenbrainz.py:_lookup_recording`을 `musicbrainz.py:lookup_recording`으로 통합:

```python
def lookup_recording(mbid: str) -> dict:
    """MB recording 조회.
    Returns: {"artist": str, "title": str} or empty dict on failure.
    """
```

기존 두 함수의 차이점:
- tagger: `{"artist": str, "title": str}` 반환
- listenbrainz: `{"artist": str, "track_name": str}` 반환

통합 후 `{"artist": str, "title": str}`로 통일. listenbrainz.py 호출처에서 `"track_name"` → `"title"` 키 변경.

### #10 상수 교체 (4개 파일)

| 파일 | 제거 | 추가 import |
|------|------|------------|
| `tagger.py` | `_MB_API`, `_MB_HEADERS` 상수 (line 18-19), `_lookup_recording_by_mbid` 함수 | `from src.pipeline.musicbrainz import MB_API, MB_HEADERS, lookup_recording` |
| `downloader.py` | `_MB_API`, `_MB_HEADERS` 상수 (line 32-33) | `from src.pipeline.musicbrainz import MB_API, MB_HEADERS` |
| `listenbrainz.py` | `_MB_API`, `_MB_HEADERS` 상수 (line 11-12), `_lookup_recording` 함수 | `from src.pipeline.musicbrainz import MB_API, MB_HEADERS, lookup_recording` |
| `api.py` | `_MB_API`, `_MB_HEADERS` (rematch_apply line 731-732), `_MB_SEARCH_HEADERS` (line 498) | `from src.pipeline.musicbrainz import MB_API, MB_HEADERS` |

api.py의 `_MB_SEARCH_URL` (line 497)도 `musicbrainz.py`로 이동:
```python
MB_SEARCH_URL = f"{MB_API}/recording"
```

### #16 alias 정리

**musicbrainz.py로 이동하는 함수들** (tagger.py에서 제거):
- `_mb_search_recording` → `musicbrainz.mb_search_recording` (public)
- `_mb_album_from_recording_id` → `musicbrainz.mb_album_from_recording_id` (public)
- `_lookup_recording_by_mbid` → `musicbrainz.lookup_recording` (public)

**tagger.py에 남는 alias 삭제**:
```python
# 삭제 대상 (tagger.py 하단)
write_title_tag = _write_title_tag       # 이미 public wrapper 존재 (dispatch 리팩토링)
write_album_tag = _write_album_tag       # 이미 public wrapper 존재
write_artist_tag = _write_artist_tag     # 이미 public wrapper 존재
write_mb_trackid_tag = _write_mb_trackid_tag  # 이미 public wrapper 존재
embed_cover_art = _embed_cover_art       # 함수를 public으로 rename
embed_art_from_url = _embed_art_from_url # 함수를 public으로 rename
itunes_search = _itunes_search           # 함수를 public으로 rename
mb_album_from_recording_id = ...         # musicbrainz.py로 이동
```

`_embed_cover_art`, `_embed_art_from_url`, `_itunes_search`, `_deezer_search` — underscore 제거하여 public으로 선언. 테스트의 monkeypatch 경로도 업데이트.

**api.py import 업데이트**:
```python
# Before
from src.pipeline.tagger import (
    embed_art_from_url, embed_cover_art, itunes_search,
    tag_and_import, write_album_tag, write_artist_tag,
    write_mb_trackid_tag, write_title_tag,
)

# After
from src.pipeline.tagger import (
    embed_art_from_url, embed_cover_art, itunes_search,
    tag_and_import, write_album_tag, write_artist_tag,
    write_mb_trackid_tag, write_title_tag,
)
from src.pipeline.musicbrainz import MB_API, MB_HEADERS, mb_search_recording
```

---

## PR2 — 잡수정 (#12, #14, #15)

### #12 update_track_info 2회 → 1회

**파일**: `src/api.py` — `rematch_apply`

현재 두 곳에서 호출:
1. line ~763: 파일 이동 후 `artist` + `file_path` 업데이트
2. line ~795: album + mb_recording_id 업데이트

함수 끝에서 한 번에 호출로 합침:

```python
# rematch_apply 함수 끝 (파일 이동 + 태그 쓰기 완료 후)
if req.mbid is not None:
    update_track_info(
        _cfg.state_db,
        req.mbid,
        artist=req.artist_name if req.artist_name else None,
        file_path=file_path,
        album=album_name,
        mb_recording_id=req.mb_recording_id if req.mb_recording_id else None,
    )
```

### #14 BeetsConfig → MusicDirConfig

**파일**: `src/config.py`, `tests/conftest.py`

클래스명만 변경:
```python
# Before
@dataclass
class BeetsConfig:
    music_dir: str = "/app/data/music"

# After
@dataclass
class MusicDirConfig:
    music_dir: str = "/app/data/music"
```

`AppConfig`의 필드: `beets: BeetsConfig` → `beets: MusicDirConfig`. 필드명 `beets`는 유지하여 호출처(`cfg.beets.music_dir`) 변경 불필요.

### #15 random.choices → secrets

**파일**: `src/pipeline/navidrome.py`

```python
# Before
import random
import string
salt = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))

# After
import secrets
salt = secrets.token_hex(6)
```

`import random`, `import string` 제거, `import secrets` 추가. `secrets.token_hex(6)`은 12자리 hex 문자열을 반환하며, Subsonic API salt 요구사항을 충족.

---

## PR 의존성

```
PR1 (MB API 통합) ──독립── PR2 (잡수정)
```

두 PR은 완전히 독립. 병렬 진행 가능.

---

## 범위 외 (낮음 — 후속 작업)

- #17 .dockerignore 없음
- #18 Health check 엔드포인트 없음
- #19 Rate limiter 메모리 누수
- #20 get_all_downloads 페이지네이션
- #21 수동 다운로드 중복 체크
