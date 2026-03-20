# Code Quality Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 8 critical/high-priority issues (data corruption bugs, blocking I/O, structural duplication) across 4 sequential PRs.

**Architecture:** 4 PRs in dependency order — PR1 (data integrity) and PR2 (async/httpx) are independent; PR3 (tagger refactor) creates `src/utils.py`; PR4 (api.py split) depends on PR3's `utils.py`.

**Tech Stack:** Python 3.12+, FastAPI, mutagen, httpx, SQLite, pytest

**Spec:** `docs/superpowers/specs/2026-03-20-code-quality-fixes-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `src/pipeline/tagger.py` | Modify | PR1: FLAC tag fix; PR3: format dispatch refactor, remove aliases |
| `src/state.py` | Modify | PR1: COALESCE album, datetime.UTC |
| `src/api.py` | Modify | PR2: lifespan + httpx shared client + async conversion; PR4: extract _run_download_job, replace sanitize/resolve_dir |
| `src/utils.py` | Create | PR3: sanitize_path_component; PR4: move_to_music_dir, resolve_dir |
| `src/jobs.py` | Create | PR4: run_download_job extracted from api.py |
| `src/main.py` | Modify | PR4: import from jobs instead of api |
| `tests/unit/test_state.py` | Modify | PR1: test COALESCE behavior |
| `tests/unit/test_tagger.py` | Modify | PR1: test FLAC list format; PR3: update imports |
| `tests/unit/test_utils.py` | Create | PR3: test sanitize_path_component |
| `tests/unit/test_api_async.py` | Create | PR2: test async endpoints don't block |
| `tests/unit/test_jobs.py` | Create | PR4: test run_download_job |

---

## Task 1: PR1 — FLAC artist 태그 타입 통일 (#1)

**Files:**
- Modify: `src/pipeline/tagger.py:380`
- Modify: `tests/unit/test_tagger.py`

- [ ] **Step 1: Write failing test — FLAC artist should be list**

`tests/unit/test_tagger.py`에 추가:

```python
def test_write_tags_flac_artist_is_list(tmp_path):
    """_write_tags should write FLAC artist as list, not string."""
    flac_path = str(tmp_path / "test.flac")
    # Create minimal FLAC file
    from tests.unit.test_tagger import _make_flac  # use existing helper if available
    import mutagen.flac
    # Create a minimal valid FLAC
    silence = b"\x00" * 4096
    f = mutagen.flac.FLAC()
    f.save(flac_path)

    _write_tags(flac_path, "Radiohead", "Creep", "mb-123")

    f = mutagen.flac.FLAC(flac_path)
    assert isinstance(f.get("artist"), list), "FLAC artist tag should be a list"
    assert f["artist"] == ["Radiohead"]
```

**Note:** 기존 테스트에 FLAC 더미 파일 생성 헬퍼가 있는지 확인 후 재사용할 것. 없으면 `mutagen.flac.FLAC()` + `f.save(path)`로 생성.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_tagger.py::test_write_tags_flac_artist_is_list -v`
Expected: FAIL — `assert isinstance(str, list)` 실패

- [ ] **Step 3: Fix FLAC artist assignment in _write_tags**

`src/pipeline/tagger.py` line 380 수정:

```python
# Before (line 380)
f["artist"] = artist

# After
f["artist"] = [artist]
```

같은 함수 내 FLAC 분기의 `f["title"]`과 `f["musicbrainz_trackid"]`도 리스트로 통일:

```python
# Before (lines 381-382)
f["title"] = track_name
if mb_trackid:
    f["musicbrainz_trackid"] = mb_trackid

# After
f["title"] = [track_name]
if mb_trackid:
    f["musicbrainz_trackid"] = [mb_trackid]
```

또한 `_write_tags` 하단의 generic(else) 분기(lines ~403-407)도 리스트로 통일:

```python
# Before
f["artist"] = artist
f["title"] = track_name
if mb_trackid:
    f["musicbrainz_trackid"] = mb_trackid

# After
f["artist"] = [artist]
f["title"] = [track_name]
if mb_trackid:
    f["musicbrainz_trackid"] = [mb_trackid]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_tagger.py::test_write_tags_flac_artist_is_list -v`
Expected: PASS

- [ ] **Step 5: Run full tagger test suite**

Run: `pytest tests/unit/test_tagger.py -v`
Expected: All tests PASS (기존 _read_tags 테스트는 `(get() or [""])[0]`이라 리스트 호환)

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/tagger.py tests/unit/test_tagger.py
git commit -m "fix: FLAC tags now use list format consistent with Opus/OGG (#1)"
```

---

## Task 2: PR1 — mark_done album COALESCE + datetime.UTC (#2, #13)

**Files:**
- Modify: `src/state.py:86-92`
- Modify: `tests/unit/test_state.py`

- [ ] **Step 1: Write failing test — mark_done should preserve existing album when None**

`tests/unit/test_state.py`에 추가:

```python
def test_mark_done_preserves_album_when_none(tmp_state_db):
    """mark_done(album=None) should NOT overwrite existing album value."""
    mark_pending(tmp_state_db, "mbid-album-test", "Creep", "Radiohead")
    # Simulate enrichment setting album
    update_track_info(tmp_state_db, "mbid-album-test", album="The Bends")

    # mark_done with album=None should preserve "The Bends"
    mark_done(tmp_state_db, "mbid-album-test", file_path="/music/test.flac", album=None)

    row = get_download_by_mbid(tmp_state_db, "mbid-album-test")
    assert row["album"] == "The Bends", "album should be preserved when mark_done album=None"


def test_mark_done_overwrites_album_when_provided(tmp_state_db):
    """mark_done(album='X') should update album to 'X'."""
    mark_pending(tmp_state_db, "mbid-album-test2", "Creep", "Radiohead")
    update_track_info(tmp_state_db, "mbid-album-test2", album="The Bends")

    mark_done(tmp_state_db, "mbid-album-test2", file_path="/music/test.flac", album="OK Computer")

    row = get_download_by_mbid(tmp_state_db, "mbid-album-test2")
    assert row["album"] == "OK Computer"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_state.py::test_mark_done_preserves_album_when_none -v`
Expected: FAIL — album이 None으로 덮어씌워짐

- [ ] **Step 3: Fix mark_done SQL + datetime**

`src/state.py` lines 86-92 수정:

```python
# Before
def mark_done(db_path: str, mbid: str, file_path: str = None, album: str = None):
    with _conn(db_path) as conn:
        conn.execute("""
            UPDATE downloads
            SET status = 'done', downloaded_at = ?, file_path = ?, album = ?
            WHERE mbid = ?
        """, (datetime.utcnow().isoformat(), file_path, album, mbid))

# After
def mark_done(db_path: str, mbid: str, file_path: str = None, album: str = None):
    with _conn(db_path) as conn:
        conn.execute("""
            UPDATE downloads
            SET status = 'done', downloaded_at = ?, file_path = ?, album = COALESCE(?, album)
            WHERE mbid = ?
        """, (datetime.now(tz=timezone.utc).isoformat(), file_path, album, mbid))
```

**Note:** `datetime.utcnow()` → `datetime.now(tz=timezone.utc)` 변경. `from datetime import datetime`은 클래스 import이므로 `datetime.timezone`은 접근 불가. **`from datetime import timezone`을 imports에 추가**해야 함.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_state.py -v`
Expected: All PASS (신규 2개 + 기존 테스트)

- [ ] **Step 5: Commit**

```bash
git add src/state.py tests/unit/test_state.py
git commit -m "fix: mark_done preserves existing album with COALESCE, use datetime.UTC (#2, #13)"
```

---

## Task 3: PR2 — httpx.AsyncClient 공유 via lifespan (#6)

**Files:**
- Modify: `src/api.py:51-52` (app creation), lines ~979, 1051, 1108 (proxy endpoints)

- [ ] **Step 1: Add lifespan handler with shared httpx client**

`src/api.py` 수정 — imports에 `from contextlib import asynccontextmanager` 추가, app 생성 변경:

```python
# Before (line 51-52)
app = FastAPI(title="Music Bot")
app.mount("/static", StaticFiles(directory="src/static"), name="static")

# After
from contextlib import asynccontextmanager

@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(timeout=60.0)
    yield
    await app.state.http_client.aclose()

app = FastAPI(title="Music Bot", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory="src/static"), name="static")
```

- [ ] **Step 2: Replace httpx.AsyncClient() in subsonic_proxy**

Lines ~979 근처:

```python
# Before
client = httpx.AsyncClient(timeout=60.0)
try:
    upstream = await client.send(...)
    # ... StreamingResponse generator with finally: await upstream.aclose(); await client.aclose()
finally:
    await client.aclose()

# After
client = request.app.state.http_client
upstream = await client.send(...)
# StreamingResponse generator의 finally에서:
#   - await upstream.aclose()  ← 유지 (개별 요청의 응답 스트림 닫기)
#   - await client.aclose()    ← 삭제 (공유 클라이언트이므로 lifespan에서 관리)
```

3곳 모두 동일 패턴 적용 (subsonic_proxy, subsonic_authed_proxy, navidrome_proxy).

**핵심:** 각 프록시 엔드포인트의 streaming generator 내부 `finally` 블록에서 `await upstream.aclose()`는 반드시 유지하고, `await client.aclose()`만 제거할 것. 공유 클라이언트를 닫으면 다른 요청이 실패함.

- [ ] **Step 3: Run existing API tests**

Run: `pytest tests/ -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add src/api.py
git commit -m "refactor: share httpx.AsyncClient via FastAPI lifespan (#6)"
```

---

## Task 4: PR2 — blocking I/O → async 전환 (#3)

**Files:**
- Modify: `src/api.py` (rematch_search, rematch_apply, _navidrome_get_song, imports)
- Create: `tests/unit/test_api_async.py`

- [ ] **Step 1: Convert _navidrome_get_song to async**

`src/api.py` lines 595-615:

```python
# Before
def _navidrome_get_song(url: str, username: str, password: str, song_id: str) -> dict:
    ...
    resp = requests.get(endpoint, params=params, timeout=15)
    ...

# After
async def _navidrome_get_song(client: httpx.AsyncClient, url: str, username: str, password: str, song_id: str) -> dict:
    """Call Navidrome getSong and return the song dict, or raise on failure."""
    salt = secrets.token_hex(6)
    token = hashlib.md5(f"{password}{salt}".encode()).hexdigest()
    params = {
        "u": username, "t": token, "s": salt,
        "v": "1.16.1", "c": "brainstream", "f": "json",
        "id": song_id,
    }
    endpoint = f"{url.rstrip('/')}/rest/getSong"
    resp = await client.get(endpoint, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    subsonic = data.get("subsonic-response", {})
    if subsonic.get("status") != "ok":
        raise RuntimeError(f"getSong failed: {subsonic.get('error', data)}")
    return subsonic.get("song", {})
```

호출처에서 `request.app.state.http_client`를 첫 인수로 전달:

```python
# Before
song = _navidrome_get_song(_cfg.navidrome.url, ...)

# After
song = await _navidrome_get_song(request.app.state.http_client, _cfg.navidrome.url, ...)
```

- [ ] **Step 2: Convert rematch_search — requests.get → httpx async**

Lines ~464-496:

```python
# Before
r = requests.get(_MB_SEARCH_URL, params=..., headers=..., timeout=10)

# After
client = request.app.state.http_client
r = await client.get(_MB_SEARCH_URL, params=..., headers=..., timeout=10)
```

2곳의 `requests.get` → `await client.get` 변환.
2곳의 `time.sleep(1)` → `await asyncio.sleep(1)` 변환.

imports에 `import asyncio` 추가 (없으면).

- [ ] **Step 3: Convert rematch_apply — requests.get → httpx async**

Line ~677:

```python
# Before
time.sleep(1)
r = requests.get(f"{_MB_API}/release/{req.mb_album_id}", ...)

# After
await asyncio.sleep(1)
r = await client.get(f"{_MB_API}/release/{req.mb_album_id}", ...)
```

- [ ] **Step 4: Remove requests import if unused elsewhere**

`src/api.py` imports에서 `import requests` 제거. `requests`가 api.py 내 다른 곳에서 사용되지 않는지 grep으로 확인할 것.

**Note:** `_embed_cover_art`(tagger.py)와 `_embed_art_from_url`(tagger.py)은 requests를 사용하지만 별도 모듈이므로 api.py의 import만 제거.

- [ ] **Step 5: Write async test**

`tests/unit/test_api_async.py` 생성:

```python
"""rematch 엔드포인트가 이벤트 루프를 블로킹하지 않는지 검증."""
import pytest
from unittest.mock import AsyncMock, patch

# requests import가 api.py에서 제거되었는지 확인
def test_api_does_not_import_requests():
    import src.api as api_module
    import importlib
    importlib.reload(api_module)
    # api.py 소스에 'import requests'가 없어야 함
    import inspect
    source = inspect.getsource(api_module)
    assert "import requests" not in source, "api.py should not import requests after async conversion"
```

- [ ] **Step 6: Run all tests**

Run: `pytest tests/ -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/api.py tests/unit/test_api_async.py
git commit -m "fix: convert blocking requests/sleep to async httpx in rematch endpoints (#3)"
```

---

## Task 5: PR3 — sanitize 함수 통일 (#7)

**Files:**
- Create: `src/utils.py`
- Modify: `src/pipeline/tagger.py:22-28`
- Modify: `src/api.py:576-580`
- Create: `tests/unit/test_utils.py`

**Note:** `src/utils/` 디렉토리가 이미 존재할 수 있음 (`src/utils/logger.py` import가 conftest에 있음). 이 경우 `src/utils.py` 대신 `src/utils/fs.py`에 배치하거나, `src/fileutils.py`로 생성할 것. 탐색 시 확인 필요.

- [ ] **Step 1: Check existing src/utils structure**

Run: `ls -la src/utils*` — `src/utils/`가 패키지인지 단일 파일인지 확인.

`src/utils/` 패키지라면 → `src/utils/fs.py`에 작성
단일 파일이라면 → `src/fileutils.py`에 작성

- [ ] **Step 2: Write failing tests for sanitize_path_component**

```python
# tests/unit/test_utils.py (또는 test_fileutils.py)
"""sanitize_path_component 단위 테스트."""
from src.utils.fs import sanitize_path_component  # 경로는 Step 1 결과에 따라 조정


def test_sanitize_removes_unsafe_chars():
    assert sanitize_path_component('foo/bar:baz*"qux') == "foo_bar_baz__qux"


def test_sanitize_strips_dots_and_spaces():
    assert sanitize_path_component("  .hidden.  ") == "hidden"


def test_sanitize_empty_returns_default():
    assert sanitize_path_component("") == "_"
    assert sanitize_path_component("...", default="Unknown") == "Unknown"


def test_sanitize_truncates_to_max_len():
    long_name = "a" * 300
    assert len(sanitize_path_component(long_name)) == 255
    assert len(sanitize_path_component(long_name, max_len=50)) == 50
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/unit/test_utils.py -v`
Expected: FAIL — import error

- [ ] **Step 4: Implement sanitize_path_component**

```python
# src/utils/fs.py (또는 src/fileutils.py)
"""Filesystem utility functions shared across modules."""
import re


def sanitize_path_component(name: str, default: str = "_", max_len: int = 255) -> str:
    """Remove filesystem-unsafe characters, strip dots/spaces, truncate.

    Args:
        name: Raw string to sanitize.
        default: Fallback if result is empty.
        max_len: Maximum length of returned string.
    """
    sanitized = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", name)
    sanitized = sanitized.strip(". ")
    return (sanitized or default)[:max_len]
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/test_utils.py -v`
Expected: All PASS

- [ ] **Step 6: Replace _sanitize_filename in tagger.py**

```python
# Before (tagger.py line 22-28)
def _sanitize_filename(name: str) -> str:
    ...

# After — 함수 삭제, import 추가:
from src.utils.fs import sanitize_path_component

# 모든 _sanitize_filename(x) 호출을 sanitize_path_component(x)로 교체
```

tagger.py 내 `_sanitize_filename` 호출처를 모두 `sanitize_path_component`로 변경.

- [ ] **Step 7: Replace _sanitize_path_component in api.py**

```python
# Before (api.py line 576-580)
def _sanitize_path_component(name: str) -> str:
    ...

# After — 함수 삭제, import 추가:
from src.utils.fs import sanitize_path_component

# 모든 _sanitize_path_component(x) 호출을 sanitize_path_component(x)로 교체
```

- [ ] **Step 8: Run full test suite**

Run: `pytest tests/ -v`
Expected: All PASS

- [ ] **Step 9: Commit**

```bash
git add src/utils/fs.py tests/unit/test_utils.py src/pipeline/tagger.py src/api.py
git commit -m "refactor: unify sanitize functions into src/utils/fs.py (#7)"
```

---

## Task 6: PR3 — tagger.py 포맷 디스패치 리팩토링 (#4)

**Files:**
- Modify: `src/pipeline/tagger.py` (전면 리팩토링 — ~400줄 감소 예상)
- Modify: `tests/unit/test_tagger.py` (import 경로 변경)

이 Task는 가장 대규모 변경. 기존 테스트를 먼저 모두 통과시킨 후 리팩토링 시작.

- [ ] **Step 1: Run existing tagger tests (baseline)**

Run: `pytest tests/unit/test_tagger.py -v`
Expected: All PASS — 리팩토링 전 기준점 확인

- [ ] **Step 2: Add format dispatch infrastructure**

`src/pipeline/tagger.py` 상단 (`_sanitize_filename` 삭제 위치 부근)에 추가:

```python
import mutagen.flac
import mutagen.mp4
import mutagen.oggopus

def _detect_format(path: str) -> str:
    """확장자 기반 포맷 감지."""
    suffix = Path(path).suffix.lower()
    if suffix == ".flac":
        return "flac"
    elif suffix in (".opus", ".ogg"):
        return "opus"
    elif suffix in (".m4a", ".mp4"):
        return "mp4"
    return "generic"

_FORMAT_OPENER = {
    "flac": mutagen.flac.FLAC,
    "opus": mutagen.oggopus.OggOpus,
    "mp4": mutagen.mp4.MP4,
    "generic": mutagen.File,
}

_FORMAT_KEYS = {
    "flac":    {"artist": "artist", "title": "title", "album": "album",
                "mb_trackid": "musicbrainz_trackid"},
    "opus":    {"artist": "artist", "title": "title", "album": "album",
                "mb_trackid": "musicbrainz_trackid"},
    "mp4":     {"artist": "\xa9ART", "title": "\xa9nam", "album": "\xa9alb",
                "mb_trackid": "----:com.apple.iTunes:MusicBrainz Track Id"},
    "generic": {"artist": "artist", "title": "title", "album": "album",
                "mb_trackid": "musicbrainz_trackid"},
}


def _wrap_value(fmt: str, key_name: str, value: str):
    """포맷별 값 래핑. FLAC/Opus는 리스트, MP4 mb_trackid는 MP4FreeForm."""
    if key_name == "mb_trackid" and fmt == "mp4":
        return [mutagen.mp4.MP4FreeForm(value.encode("utf-8"))]
    if fmt == "mp4":
        return [value]
    if fmt in ("flac", "opus"):
        return [value]
    # generic — mutagen.File은 포맷에 따라 다르므로 리스트로 시도
    return [value]
```

- [ ] **Step 3: Rewrite _write_tags using dispatch**

```python
def _write_tags(file_path: str, artist: str, track_name: str, mb_trackid: str = ""):
    """Write artist, title, and optionally mb_trackid tags to audio file."""
    try:
        fmt = _detect_format(file_path)
        opener = _FORMAT_OPENER[fmt]
        keys = _FORMAT_KEYS[fmt]
        f = opener(file_path) if fmt != "generic" else opener(file_path)
        if f is None:
            log.warning("could not open file for tagging", file=file_path)
            return
        f[keys["artist"]] = _wrap_value(fmt, "artist", artist)
        f[keys["title"]] = _wrap_value(fmt, "title", track_name)
        if mb_trackid:
            f[keys["mb_trackid"]] = _wrap_value(fmt, "mb_trackid", mb_trackid)
        f.save()
        log.debug("wrote tags to file", file=file_path, artist=artist, title=track_name)
    except Exception as exc:
        log.warning("could not write tags to file", file=file_path, error=str(exc))
```

- [ ] **Step 4: Run tagger tests**

Run: `pytest tests/unit/test_tagger.py -v`
Expected: All PASS

- [ ] **Step 5: Rewrite single-tag write functions**

`_write_mb_trackid_tag`, `_write_album_tag`, `_write_artist_tag`, `_write_title_tag`를 공통 패턴으로 통합:

```python
def _write_single_tag(file_path: str, key_name: str, value: str):
    """Write a single tag to audio file."""
    try:
        fmt = _detect_format(file_path)
        f = _FORMAT_OPENER[fmt](file_path)
        if f is None:
            return
        f[_FORMAT_KEYS[fmt][key_name]] = _wrap_value(fmt, key_name, value)
        f.save()
        log.debug(f"wrote {key_name} tag", file=file_path, value=value)
    except Exception as exc:
        log.warning(f"could not write {key_name} tag", file=file_path, error=str(exc))


def write_mb_trackid_tag(file_path: str, mb_trackid: str):
    _write_single_tag(file_path, "mb_trackid", mb_trackid)

def write_album_tag(file_path: str, album: str):
    _write_single_tag(file_path, "album", album)

def write_artist_tag(file_path: str, artist: str):
    _write_single_tag(file_path, "artist", artist)

def write_title_tag(file_path: str, title: str):
    _write_single_tag(file_path, "title", title)
```

- [ ] **Step 6: Run tagger tests**

Run: `pytest tests/unit/test_tagger.py -v`
Expected: All PASS

- [ ] **Step 7: Rewrite _read_tags using dispatch**

```python
def _read_tags(file_path: str) -> dict:
    """Read artist, title, album, mb_trackid from audio file tags."""
    result = {"artist": "", "title": "", "album": "", "mb_trackid": "", "has_art": False}
    try:
        fmt = _detect_format(file_path)
        f = _FORMAT_OPENER[fmt](file_path)
        if f is None:
            return result
        keys = _FORMAT_KEYS[fmt]

        for tag in ("artist", "title", "album"):
            result[tag] = (f.get(keys[tag]) or [""])[0]

        # mb_trackid: MP4 needs bytes decode
        raw_mb = f.get(keys["mb_trackid"])
        if raw_mb:
            if fmt == "mp4":
                result["mb_trackid"] = bytes(raw_mb[0]).decode("utf-8", errors="replace")
            else:
                result["mb_trackid"] = (raw_mb if isinstance(raw_mb, list) else [raw_mb])[0]

        # has_art detection
        if fmt == "flac":
            result["has_art"] = bool(f.pictures)
        elif fmt == "opus":
            result["has_art"] = bool(f.get("metadata_block_picture"))
        elif fmt == "mp4":
            result["has_art"] = bool(f.get("covr"))

    except Exception as exc:
        log.warning("could not read tags from file", file=file_path, error=str(exc))
    return result
```

- [ ] **Step 8: Run tagger tests**

Run: `pytest tests/unit/test_tagger.py -v`
Expected: All PASS

- [ ] **Step 9: Refactor _embed_cover_art and _embed_art_from_url**

포맷별 임베더를 분리:

```python
def _embed_flac_art(f, image_data: bytes, content_type: str):
    pic = mutagen.flac.Picture()
    pic.type = 3
    pic.mime = content_type
    pic.data = image_data
    f.clear_pictures()
    f.add_picture(pic)

def _embed_opus_art(f, image_data: bytes, content_type: str):
    import base64
    pic = mutagen.flac.Picture()
    pic.type = 3
    pic.mime = content_type
    pic.data = image_data
    f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]

def _embed_mp4_art(f, image_data: bytes, content_type: str):
    fmt = mutagen.mp4.MP4Cover.FORMAT_JPEG
    if "png" in content_type:
        fmt = mutagen.mp4.MP4Cover.FORMAT_PNG
    f["covr"] = [mutagen.mp4.MP4Cover(image_data, imageformat=fmt)]

_ART_EMBEDDER = {
    "flac": _embed_flac_art,
    "opus": _embed_opus_art,
    "mp4": _embed_mp4_art,
}
```

`_embed_cover_art`와 `_embed_art_from_url`에서 이 디스패치 사용.

- [ ] **Step 10: Remove public aliases at bottom of tagger.py**

Lines 1258-1268 삭제. 위 Step 5에서 함수를 이미 public으로 선언했으므로 alias 불필요.

`api.py`의 import 경로 확인 — `from src.pipeline.tagger import write_album_tag, ...` 가 이미 public 함수명을 사용하므로 변경 불필요.

- [ ] **Step 11: Update test imports if needed**

`tests/unit/test_tagger.py`에서 `_write_artist_tag` 등 private import를 사용하는 경우 public 이름으로 변경:

```python
# Before
from src.pipeline.tagger import _write_artist_tag, _write_mb_trackid_tag

# After
from src.pipeline.tagger import write_artist_tag, write_mb_trackid_tag
```

- [ ] **Step 12: Run full test suite**

Run: `pytest tests/ -v`
Expected: All PASS

- [ ] **Step 13: Commit**

```bash
git add src/pipeline/tagger.py tests/unit/test_tagger.py
git commit -m "refactor: tagger format dispatch dictionary, remove ~400 lines of duplication (#4)"
```

---

## Task 7: PR4 — _run_download_job을 jobs.py로 분리 (#5)

**Files:**
- Create: `src/jobs.py`
- Modify: `src/api.py:137-238` (삭제)
- Modify: `src/main.py:124` (import 변경)
- Create: `tests/unit/test_jobs.py`

- [ ] **Step 1: Create src/jobs.py with extracted function**

`src/api.py` lines 137-238의 `_run_download_job`을 `src/jobs.py`로 이동:

```python
# src/jobs.py
"""Download job execution logic — extracted from api.py."""
import glob as _glob
import os

import src.worker as worker
from src.pipeline.downloader import download_track, download_track_by_id
from src.pipeline.navidrome import trigger_scan, wait_for_scan
from src.pipeline.tagger import tag_and_import
from src.state import (
    get_download_by_mbid,
    mark_done,
    mark_downloading,
    mark_failed,
    update_track_info,
)
from src.utils.logger import get_logger

log = get_logger(__name__)


def run_download_job(cfg, job_spec: dict):
    """Execute a download+tag+scan job. Called by worker_loop."""
    # ... (api.py의 _run_download_job 본문 그대로, 함수명만 public으로)
```

- [ ] **Step 2: Update main.py import**

`src/main.py` line 124:

```python
# Before
from src.api import _run_download_job

worker_thread = threading.Thread(
    target=worker_module.worker_loop,
    args=(cfg, _run_download_job),
    ...
)

# After
from src.jobs import run_download_job

worker_thread = threading.Thread(
    target=worker_module.worker_loop,
    args=(cfg, run_download_job),
    ...
)
```

- [ ] **Step 3: Remove _run_download_job from api.py**

`src/api.py` lines 137-238 삭제. api.py에서 `_run_download_job`이 참조되는 곳이 없는지 확인 (main.py에서만 import됨).

불필요해진 import도 정리: `import glob as _glob` (api.py 내 다른 곳에서 사용하지 않으면).

- [ ] **Step 4: Write basic test for jobs.py**

```python
# tests/unit/test_jobs.py
"""jobs.run_download_job 기본 테스트."""
from unittest.mock import MagicMock, patch

from src.jobs import run_download_job


def test_run_download_job_skips_existing_file(tmp_path):
    """이미 파일이 존재하면 다운로드를 스킵해야 함."""
    # Create dummy file
    music_file = tmp_path / "test.flac"
    music_file.write_bytes(b"fake")

    cfg = MagicMock()
    cfg.state_db = str(tmp_path / "state.db")

    job_spec = {"job_id": "test-001", "artist": "Test", "track": "Song"}

    with patch("src.jobs.get_download_by_mbid") as mock_get, \
         patch("src.jobs.mark_done") as mock_done, \
         patch("src.jobs.trigger_scan", return_value=False), \
         patch("src.jobs.worker") as mock_worker:
        mock_get.return_value = {"file_path": str(music_file), "album": "Album"}
        run_download_job(cfg, job_spec)
        mock_done.assert_called_once()
        mock_worker.emit.assert_called()
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/jobs.py src/api.py src/main.py tests/unit/test_jobs.py
git commit -m "refactor: extract _run_download_job from api.py to src/jobs.py (#5)"
```

---

## Task 8: PR4 — 파일 이동 공통 헬퍼 move_to_music_dir (#8)

**Files:**
- Modify: `src/utils/fs.py` (resolve_dir + move_to_music_dir 추가)
- Modify: `src/api.py` (edit_metadata, rematch_apply에서 사용)
- Modify: `src/jobs.py` (tag_and_import 호출은 tagger 내부이므로 변경 불필요할 수 있음 — 확인 필요)
- Modify: `tests/unit/test_utils.py`

- [ ] **Step 1: Write failing tests for resolve_dir and move_to_music_dir**

```python
# tests/unit/test_utils.py에 추가
import os

from src.utils.fs import resolve_dir, move_to_music_dir


def test_resolve_dir_finds_existing_case_insensitive(tmp_path):
    (tmp_path / "Radiohead").mkdir()
    assert resolve_dir(str(tmp_path), "radiohead") == "Radiohead"
    assert resolve_dir(str(tmp_path), "RADIOHEAD") == "Radiohead"


def test_resolve_dir_returns_sanitized_when_no_match(tmp_path):
    result = resolve_dir(str(tmp_path), "NewArtist")
    assert result == "NewArtist"


def test_move_to_music_dir_creates_dirs_and_moves(tmp_path):
    music_dir = str(tmp_path / "music")
    os.makedirs(music_dir)
    src_file = tmp_path / "staging" / "test.flac"
    src_file.parent.mkdir()
    src_file.write_bytes(b"fake audio")

    result = move_to_music_dir(str(src_file), music_dir, "Radiohead", "OK Computer", "Creep.flac")

    assert os.path.exists(result)
    assert "Radiohead" in result
    assert "OK Computer" in result
    assert not src_file.exists()  # source was moved


def test_move_to_music_dir_reuses_existing_artist_dir(tmp_path):
    music_dir = str(tmp_path / "music")
    (tmp_path / "music" / "Radiohead").mkdir(parents=True)
    src_file = tmp_path / "test.flac"
    src_file.write_bytes(b"fake")

    result = move_to_music_dir(str(src_file), music_dir, "radiohead", "OK Computer", "Creep.flac")

    # Should use existing "Radiohead" dir, not create "radiohead"
    assert "Radiohead" in result
    assert "radiohead" not in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_utils.py -v -k "resolve_dir or move_to_music_dir"`
Expected: FAIL — import error

- [ ] **Step 3: Implement resolve_dir and move_to_music_dir**

`src/utils/fs.py`에 추가:

```python
import os
import shutil


def resolve_dir(parent: str, name: str) -> str:
    """Case-insensitive directory matching within parent.

    If a directory with the same name (case-insensitive) exists in parent,
    return its actual name. Otherwise return sanitized name.
    """
    sanitized = sanitize_path_component(name)
    if os.path.isdir(parent):
        lower = sanitized.lower()
        for entry in os.listdir(parent):
            if entry.lower() == lower and os.path.isdir(os.path.join(parent, entry)):
                return entry
    return sanitized


def move_to_music_dir(
    src_path: str,
    music_dir: str,
    artist: str,
    album: str,
    filename: str,
) -> str:
    """Move file from src_path to music_dir/{artist}/{album}/{filename}.

    Uses resolve_dir for case-insensitive folder matching.
    Creates directories if they don't exist.
    Returns the final file path.
    """
    artist_dir_name = resolve_dir(music_dir, artist)
    artist_dir = os.path.join(music_dir, artist_dir_name)

    album_dir_name = resolve_dir(artist_dir, album)
    album_dir = os.path.join(artist_dir, album_dir_name)

    os.makedirs(album_dir, exist_ok=True)

    dest_path = os.path.join(album_dir, sanitize_path_component(filename))
    shutil.move(src_path, dest_path)
    return dest_path
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_utils.py -v`
Expected: All PASS

- [ ] **Step 5: Replace file move logic in edit_metadata**

`src/api.py` edit_metadata (lines ~871-902):

```python
# Before
new_artist_dir = os.path.join(music_root, _sanitize_path_component(new_artist))
new_album_dir = os.path.join(new_artist_dir, _sanitize_path_component(new_album))
new_filename = _sanitize_path_component(new_track_name) + ext
new_file_path = os.path.join(new_album_dir, new_filename)
if new_file_path != old_file_path:
    os.makedirs(new_album_dir, exist_ok=True)
    shutil.move(old_file_path, new_file_path)

# After
from src.utils.fs import move_to_music_dir, sanitize_path_component
new_filename = sanitize_path_component(new_track_name) + ext
new_file_path = move_to_music_dir(old_file_path, music_root, new_artist, new_album, new_filename)
```

**주의:** `move_to_music_dir`는 무조건 이동하므로, 경로가 동일한 경우(변경 없음)를 호출 전에 체크해야 함. 또한 기존의 빈 폴더 정리 로직도 유지 필요 — `move_to_music_dir` 호출 후 old 경로 정리.

- [ ] **Step 6: Replace file move logic in rematch_apply**

`src/api.py` rematch_apply (lines ~722-774) — 유사하게 `move_to_music_dir` 적용.

- [ ] **Step 7: Remove _resolve_dir and _sanitize_path_component from api.py**

이미 `src/utils/fs.py`로 이동했으므로 api.py에서 삭제. import로 대체.

- [ ] **Step 8: Run full test suite**

Run: `pytest tests/ -v`
Expected: All PASS

- [ ] **Step 9: Commit**

```bash
git add src/utils/fs.py src/api.py tests/unit/test_utils.py
git commit -m "refactor: unify file move logic with move_to_music_dir helper (#8)"
```

---

## Task 9: Final verification

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All PASS

- [ ] **Step 2: Verify api.py line count reduced**

Run: `wc -l src/api.py`
Expected: ~900-1000줄 (기존 1158줄에서 _run_download_job ~100줄 + 헬퍼 ~30줄 제거)

- [ ] **Step 3: Verify tagger.py line count reduced**

Run: `wc -l src/pipeline/tagger.py`
Expected: ~850-900줄 (기존 1268줄에서 ~400줄 감소)
