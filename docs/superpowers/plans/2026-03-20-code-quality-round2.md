# Code Quality Fixes Round 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate duplicated MB API code into shared module, clean up aliases, and fix misc code quality issues (#9-#16).

**Architecture:** 2 independent PRs — PR1 creates `src/pipeline/musicbrainz.py` as single source of truth for MB constants and lookup, then cleans tagger.py aliases. PR2 fixes 3 independent small issues.

**Tech Stack:** Python 3.12+, requests, FastAPI, pytest

**Spec:** `docs/superpowers/specs/2026-03-20-code-quality-round2-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `src/pipeline/musicbrainz.py` | Create | MB_API, MB_HEADERS, MB_SEARCH_URL, lookup_recording, mb_search_recording, mb_album_from_recording_id |
| `src/pipeline/tagger.py` | Modify | Remove _MB_API/_MB_HEADERS, _lookup_recording_by_mbid, _mb_search_recording, _mb_album_from_recording_id; remove aliases; rename private→public |
| `src/pipeline/listenbrainz.py` | Modify | Remove _MB_API/_MB_HEADERS, _lookup_recording; import from musicbrainz |
| `src/pipeline/downloader.py` | Modify | Remove _MB_API/_MB_HEADERS; import from musicbrainz |
| `src/api.py` | Modify | Remove local MB constants; import from musicbrainz; merge update_track_info calls |
| `src/main.py` | Modify | Import lookup_recording from musicbrainz |
| `src/config.py` | Modify | BeetsConfig → MusicDirConfig |
| `src/pipeline/navidrome.py` | Modify | random.choices → secrets.token_hex |
| `tests/unit/test_musicbrainz.py` | Create | Tests for lookup_recording |
| `tests/conftest.py` | Modify | BeetsConfig → MusicDirConfig |
| `tests/unit/test_tagger.py` | Modify | Update monkeypatch paths |
| `tests/integration/test_api.py` | Modify | Update monkeypatch paths |

---

## Task 1: PR1 — Create `src/pipeline/musicbrainz.py` with constants + lookup_recording (#9, #10, #11)

**Files:**
- Create: `src/pipeline/musicbrainz.py`
- Create: `tests/unit/test_musicbrainz.py`

- [ ] **Step 1: Write test for lookup_recording**

```python
# tests/unit/test_musicbrainz.py
"""musicbrainz.py unit tests."""
from unittest.mock import MagicMock, patch

from src.pipeline.musicbrainz import MB_API, MB_HEADERS, lookup_recording


def test_mb_constants():
    assert "musicbrainz.org" in MB_API
    assert "brainstream" in MB_HEADERS["User-Agent"]


def test_lookup_recording_success():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "title": "Creep",
        "artist-credit": [
            {"artist": {"name": "Radiohead"}, "joinphrase": ""}
        ],
    }
    mock_resp.raise_for_status = MagicMock()

    with patch("src.pipeline.musicbrainz.requests.get", return_value=mock_resp):
        with patch("src.pipeline.musicbrainz.time.sleep"):
            result = lookup_recording("some-mbid")

    assert result["artist"] == "Radiohead"
    assert result["title"] == "Creep"


def test_lookup_recording_failure():
    with patch("src.pipeline.musicbrainz.requests.get", side_effect=Exception("network")):
        with patch("src.pipeline.musicbrainz.time.sleep"):
            result = lookup_recording("bad-mbid")

    assert result["artist"] == ""
    assert result["title"] == ""
```

- [ ] **Step 2: Run tests to verify they fail (import error)**

Run: `docker run --rm brainstream-test python -m pytest tests/unit/test_musicbrainz.py -v`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Create `src/pipeline/musicbrainz.py`**

```python
"""MusicBrainz API client — shared constants and lookup functions."""
import time

import requests

from src.utils.logger import get_logger

log = get_logger(__name__)

MB_API = "https://musicbrainz.org/ws/2"
MB_HEADERS = {"User-Agent": "brainstream/1.0 (https://github.com/bigpie1367/brain-stream)"}
MB_SEARCH_URL = f"{MB_API}/recording"


def lookup_recording(mbid: str) -> dict[str, str]:
    """Look up MB recording by mbid.

    Returns {"artist": str, "title": str}, empty strings on failure.
    Consolidates tagger._lookup_recording_by_mbid and listenbrainz._lookup_recording.
    """
    try:
        time.sleep(1)  # rate limit
        r = requests.get(
            f"{MB_API}/recording/{mbid}",
            params={"fmt": "json", "inc": "artist-credits"},
            headers=MB_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        title = data.get("title", "")
        artist_credits = data.get("artist-credit", [])
        artist_parts = []
        for credit in artist_credits:
            if isinstance(credit, dict):
                name = credit.get("artist", {}).get("name", "")
                if name:
                    artist_parts.append(name)
                joinphrase = credit.get("joinphrase", "")
                if joinphrase:
                    artist_parts.append(joinphrase)
        artist = "".join(artist_parts).strip()
        return {"artist": artist, "title": title}
    except Exception as exc:
        log.warning("MB recording lookup failed", mbid=mbid, error=str(exc))
        return {"artist": "", "title": ""}
```

- [ ] **Step 4: Run tests**

Run: `docker run --rm brainstream-test python -m pytest tests/unit/test_musicbrainz.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/musicbrainz.py tests/unit/test_musicbrainz.py
git commit -m "feat: create src/pipeline/musicbrainz.py with shared MB constants + lookup_recording (#9, #10, #11)"
```

---

## Task 2: PR1 — Move mb_search_recording + mb_album_from_recording_id to musicbrainz.py

**Files:**
- Modify: `src/pipeline/musicbrainz.py` (add functions)
- Modify: `src/pipeline/tagger.py` (remove functions, update internal calls)

- [ ] **Step 1: Move `_mb_search_recording` from tagger.py to musicbrainz.py**

Cut the entire `_mb_search_recording` function (lines ~257-585) from tagger.py. Paste into musicbrainz.py as `mb_search_recording` (public, no underscore).

Update internal references within the moved function:
- `_MB_API` → `MB_API`
- `_MB_HEADERS` → `MB_HEADERS`
- Any calls to other tagger.py functions that the moved function uses (e.g., `_collect_recording_candidates`, `_pick_best_recording`, `_is_live_title`, `_mb_lookup_artist_ids`) — these helper functions must also move if they're only used by mb_search_recording.

**Check which helpers are used only by the moving functions vs shared with tag_and_import. Grep BEFORE moving:**
- `_collect_recording_candidates` — used by mb_search_recording only → move
- `_pick_best_recording` — used by mb_search_recording only → move
- `_mb_lookup_artist_ids` — used by mb_search_recording only → move
- `_extract_mb_artist_name` — used by mb_search_recording only → move
- `_extract_mb_recording_title` — used by mb_search_recording only → move
- `_is_live_title` — used by BOTH mb_album_from_recording_id AND tag_and_import → **keep in tagger.py**, import in musicbrainz.py
- `_normalize_for_match` — used by BOTH mb_search_recording AND tagger enrichment functions → **keep in tagger.py**, import in musicbrainz.py
- `_primary_artist` — used by BOTH tag_and_import AND mb_search_recording → **keep in tagger.py**, import in musicbrainz.py

For shared helpers (`_is_live_title`, `_normalize_for_match`, `_primary_artist`): keep in tagger.py and import them in musicbrainz.py. No circular dependency since musicbrainz.py imports from tagger.py (one-way).

**Important:** Verify these dependencies by grepping before moving. If a helper is shared, keep it in tagger.py and import it in musicbrainz.py.

- [ ] **Step 2: Move `_mb_album_from_recording_id` from tagger.py to musicbrainz.py**

Cut `_mb_album_from_recording_id` (lines ~587-710) from tagger.py. Paste into musicbrainz.py as `mb_album_from_recording_id` (public).

Update `_MB_API` → `MB_API`, `_MB_HEADERS` → `MB_HEADERS`.

- [ ] **Step 3: Update tagger.py to import from musicbrainz**

In tagger.py, add imports and update call sites:

```python
# Add to tagger.py imports
from src.pipeline.musicbrainz import (
    MB_API,
    MB_HEADERS,
    lookup_recording,
    mb_album_from_recording_id,
    mb_search_recording,
)
```

Remove the old `_MB_API`, `_MB_HEADERS` constants (lines 19-20).
Remove `_lookup_recording_by_mbid` function.

Update all internal calls:
- `_lookup_recording_by_mbid(mbid)` → `lookup_recording(mbid)`
- `_mb_search_recording(...)` → `mb_search_recording(...)`
- `_mb_album_from_recording_id(...)` → `mb_album_from_recording_id(...)`

- [ ] **Step 4: Verify syntax**

Run: `python3.12 -c "import ast; ast.parse(open('src/pipeline/musicbrainz.py').read()); ast.parse(open('src/pipeline/tagger.py').read()); print('OK')"`

- [ ] **Step 5: Run tagger tests**

Run: `docker run --rm brainstream-test python -m pytest tests/unit/test_tagger.py -v --tb=short`

Tests that monkeypatch `src.pipeline.tagger._mb_search_recording` will fail — that's expected and fixed in Task 4.

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/musicbrainz.py src/pipeline/tagger.py
git commit -m "refactor: move mb_search_recording + mb_album_from_recording_id to musicbrainz.py"
```

---

## Task 3: PR1 — Update consumer files (listenbrainz, downloader, api, main)

**Files:**
- Modify: `src/pipeline/listenbrainz.py`
- Modify: `src/pipeline/downloader.py`
- Modify: `src/api.py`
- Modify: `src/main.py`

- [ ] **Step 1: Update listenbrainz.py**

Remove `_MB_API`, `_MB_HEADERS` (lines 11-12) and `_lookup_recording` function (lines 15-46).

Add import:
```python
from src.pipeline.musicbrainz import MB_HEADERS, lookup_recording
```

Update call site (line ~71):
```python
# Before
meta = _lookup_recording(mbid)
if not meta["artist"] or not meta["track_name"]:

# After
meta = lookup_recording(mbid)
if not meta["artist"] or not meta["title"]:  # key changed: track_name → title
```

**Note:** `listenbrainz.py` may still use `requests` and `time` for other purposes (e.g., fetching recommendations). Only remove `_MB_API`/`_MB_HEADERS` imports, not the full `requests`/`time` imports.

- [ ] **Step 2: Update downloader.py**

Remove `_MB_API`, `_MB_HEADERS` (lines 32-33).

Add import:
```python
from src.pipeline.musicbrainz import MB_API, MB_HEADERS
```

Update `_mb_recording_duration` to use `MB_API` and `MB_HEADERS` instead of `_MB_API` and `_MB_HEADERS`.

- [ ] **Step 3: Update api.py**

Remove local `_MB_SEARCH_URL` and `_MB_SEARCH_HEADERS` (lines 350-351).
Remove local `_MB_API` and `_MB_HEADERS` inside `rematch_apply` (lines 571-572).

Add import:
```python
from src.pipeline.musicbrainz import MB_API, MB_HEADERS, MB_SEARCH_URL
```

Update `rematch_search`:
- `_MB_SEARCH_URL` → `MB_SEARCH_URL`
- `_MB_SEARCH_HEADERS` → `MB_HEADERS`

Update `rematch_apply`:
- `_MB_API` → `MB_API`
- `_MB_HEADERS` → `MB_HEADERS`

- [ ] **Step 4: Update main.py**

Line 10:
```python
# Before
from src.pipeline.listenbrainz import _lookup_recording, fetch_recommendations

# After
from src.pipeline.listenbrainz import fetch_recommendations
from src.pipeline.musicbrainz import lookup_recording
```

Update call site (line ~64):
```python
# Before
meta = _lookup_recording(mbid)
artist = meta.get("artist", "")
track_name = meta.get("track_name", "")

# After
meta = lookup_recording(mbid)
artist = meta.get("artist", "")
track_name = meta.get("title", "")  # key changed: track_name → title
```

- [ ] **Step 5: Verify syntax for all modified files**

Run: `python3.12 -c "import ast; [ast.parse(open(f).read()) for f in ['src/pipeline/listenbrainz.py','src/pipeline/downloader.py','src/api.py','src/main.py']]; print('OK')"`

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/listenbrainz.py src/pipeline/downloader.py src/api.py src/main.py
git commit -m "refactor: update all MB API consumers to use shared musicbrainz.py (#9, #10)"
```

---

## Task 4: PR1 — Clean tagger.py aliases + rename private→public (#16)

**Files:**
- Modify: `src/pipeline/tagger.py`
- Modify: `tests/unit/test_tagger.py`
- Modify: `tests/integration/test_api.py`

- [ ] **Step 1: Rename private functions to public in tagger.py**

Functions to rename (remove underscore prefix from `def` line):
- `_itunes_search` → `itunes_search`
- `_deezer_search` → `deezer_search`
- `_embed_cover_art` → `embed_cover_art`
- `_embed_art_from_url` → `embed_art_from_url`
- `_write_album_tag` → `write_album_tag`
- `_write_artist_tag` → `write_artist_tag`
- `_write_title_tag` → `write_title_tag`
- `_write_mb_trackid_tag` → `write_mb_trackid_tag`

**Note:** The `write_*` functions in tagger.py are still defined with underscore prefix (e.g., `def _write_album_tag`). The public names only exist as aliases at the bottom. Rename the actual function definitions to remove the underscore, then delete the aliases. `_write_single_tag` and `_write_tags` remain private (internal use only).

- [ ] **Step 2: Delete ALL alias lines at bottom of tagger.py**

Delete lines ~1269-1280:
```python
# Public aliases for use by api.py (rematch endpoints)
mb_search_recording = _mb_search_recording       # moved to musicbrainz.py
mb_album_from_recording_id = _mb_album_from_recording_id  # moved to musicbrainz.py
embed_cover_art = _embed_cover_art               # function renamed to public
embed_art_from_url = _embed_art_from_url         # function renamed to public
write_album_tag = _write_album_tag               # public wrapper exists
write_artist_tag = _write_artist_tag             # public wrapper exists
write_title_tag = _write_title_tag               # public wrapper exists
write_mb_trackid_tag = _write_mb_trackid_tag     # public wrapper exists
itunes_search = _itunes_search                   # function renamed to public
deezer_search = _deezer_search                   # function renamed to public
```

- [ ] **Step 3: Update test monkeypatch paths**

In `tests/unit/test_tagger.py`, update all monkeypatch targets that reference the old private names:
- `"src.pipeline.tagger._itunes_search"` → `"src.pipeline.tagger.itunes_search"`
- `"src.pipeline.tagger._deezer_search"` → `"src.pipeline.tagger.deezer_search"`
- `"src.pipeline.tagger._embed_cover_art"` → `"src.pipeline.tagger.embed_cover_art"`
- `"src.pipeline.tagger._embed_art_from_url"` → `"src.pipeline.tagger.embed_art_from_url"`
- `"src.pipeline.tagger._mb_search_recording"` → `"src.pipeline.musicbrainz.mb_search_recording"`
- `"src.pipeline.tagger._mb_album_from_recording_id"` → `"src.pipeline.musicbrainz.mb_album_from_recording_id"`
- `"src.pipeline.tagger._lookup_recording_by_mbid"` → `"src.pipeline.musicbrainz.lookup_recording"`

Also update any direct imports of private names in test files.

In `tests/integration/test_api.py`, update relevant monkeypatch paths similarly.

- [ ] **Step 4: Run full test suite in Docker**

```bash
docker build --no-cache -t brainstream-test -f - . <<'DOCKERFILE'
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
COPY tests/ ./tests/
DOCKERFILE
docker run --rm brainstream-test python -m pytest tests/ -v --tb=short
```

Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/tagger.py tests/unit/test_tagger.py tests/integration/test_api.py
git commit -m "refactor: remove tagger.py public aliases, rename private functions to public (#16)"
```

---

## Task 5: PR2 — Merge update_track_info calls in rematch_apply (#12)

**Files:**
- Modify: `src/api.py`

- [ ] **Step 1: Merge two update_track_info calls**

In `rematch_apply`, replace the two separate calls (lines ~663-676 and ~695-708) with one call at the end:

Remove first call site (lines ~663-676):
```python
        # DELETE THIS BLOCK:
        if req.mbid is not None:
            try:
                update_track_info(
                    _cfg.state_db,
                    req.mbid,
                    artist=req.artist_name if req.artist_name else None,
                    file_path=file_path,
                )
            except Exception as exc:
                log.warning(...)
```

Replace second call site (lines ~695-708) with merged version:
```python
    # Update state.db with all changes at once
    if req.mbid is not None:
        try:
            update_track_info(
                _cfg.state_db,
                req.mbid,
                artist=req.artist_name if req.artist_name else None,
                file_path=file_path,
                album=album_name,
                mb_recording_id=req.mb_recording_id if req.mb_recording_id else None,
            )
        except Exception as exc:
            log.warning(
                "rematch_apply: state.db update failed",
                mbid=req.mbid,
                error=str(exc),
            )
```

- [ ] **Step 2: Verify syntax**

Run: `python3.12 -c "import ast; ast.parse(open('src/api.py').read()); print('OK')"`

- [ ] **Step 3: Commit**

```bash
git add src/api.py
git commit -m "fix: merge duplicate update_track_info calls in rematch_apply (#12)"
```

---

## Task 6: PR2 — BeetsConfig → MusicDirConfig (#14)

**Files:**
- Modify: `src/config.py`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Rename class in config.py**

Line 20-21:
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

Line 40:
```python
# Before
beets: BeetsConfig = field(default_factory=BeetsConfig)

# After
beets: MusicDirConfig = field(default_factory=MusicDirConfig)
```

- [ ] **Step 2: Update tests/conftest.py**

Line ~79:
```python
# Before
from src.config import (
    AppConfig,
    BeetsConfig,
    ...
)

# After
from src.config import (
    AppConfig,
    MusicDirConfig,
    ...
)
```

Line ~94:
```python
# Before
beets=BeetsConfig(music_dir=music_dir),

# After
beets=MusicDirConfig(music_dir=music_dir),
```

- [ ] **Step 3: Check for other references**

Grep for `BeetsConfig` across the codebase. If found elsewhere (e.g., test_config.py), update those too.

- [ ] **Step 4: Verify syntax + run tests**

```bash
python3.12 -c "import ast; ast.parse(open('src/config.py').read()); print('OK')"
```

- [ ] **Step 5: Commit**

```bash
git add src/config.py tests/conftest.py
git commit -m "refactor: rename BeetsConfig to MusicDirConfig (#14)"
```

---

## Task 7: PR2 — random.choices → secrets.token_hex (#15)

**Files:**
- Modify: `src/pipeline/navidrome.py`

- [ ] **Step 1: Update navidrome.py**

Imports (lines 1-3):
```python
# Before
import hashlib
import random
import string

# After
import hashlib
import secrets
```

Remove `import random` and `import string`.

Salt generation (line ~18):
```python
# Before
salt = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))

# After
salt = secrets.token_hex(6)
```

- [ ] **Step 2: Verify syntax**

Run: `python3.12 -c "import ast; ast.parse(open('src/pipeline/navidrome.py').read()); print('OK')"`

- [ ] **Step 3: Run navidrome tests**

Run: `docker run --rm brainstream-test python -m pytest tests/unit/test_navidrome.py -v`

- [ ] **Step 4: Commit**

```bash
git add src/pipeline/navidrome.py
git commit -m "fix: use secrets.token_hex instead of random.choices for auth salt (#15)"
```

---

## Task 8: Final verification

- [ ] **Step 1: Build and run full test suite**

```bash
docker build --no-cache -t brainstream-test -f - . <<'DOCKERFILE'
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
COPY tests/ ./tests/
DOCKERFILE
docker run --rm brainstream-test python -m pytest tests/ -v --tb=short
```

Expected: All PASS

- [ ] **Step 2: Verify no duplicate MB constants remain**

```bash
grep -rn "_MB_API\|_MB_HEADERS\|_MB_SEARCH" src/ --include="*.py"
```

Expected: 0 results (all replaced with shared imports)

- [ ] **Step 3: Verify no tagger aliases remain**

```bash
grep -n "^[a-z_]* = _[a-z]" src/pipeline/tagger.py
```

Expected: 0 results
