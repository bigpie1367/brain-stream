# YouTube Download Accuracy Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two bugs causing wrong songs to be downloaded and mislabeled in the LB pipeline.

**Architecture:** Add title similarity scoring to YouTube candidate selection (`_select_best_entry`) and track name validation to iTunes/Deezer canonical title adoption (`_enrich_track`). Both use `difflib.SequenceMatcher` with existing `_normalize()` / `_normalize_for_match()` functions.

**Tech Stack:** Python, difflib, re, pytest

**Spec:** `docs/superpowers/specs/2026-03-24-youtube-download-accuracy-design.md`

---

### Task 1: Add `_extract_track_title` helper + tests

**Files:**
- Modify: `src/pipeline/downloader.py:108-131` (add helper near existing `_normalize`)
- Test: `tests/unit/test_downloader.py`

- [ ] **Step 1: Write failing tests for `_extract_track_title`**

```python
# tests/unit/test_downloader.py — append after existing imports

from src.pipeline.downloader import _extract_track_title


@pytest.mark.parametrize(
    "yt_title,artist,expected",
    [
        # Basic "Artist - Track (noise)" pattern
        (
            "Three Days Grace - Animal I Have Become (Official Video)",
            "Three Days Grace",
            "Animal I Have Become",
        ),
        # Noise bracket stripped, music bracket kept
        (
            "Imagine Dragons - Believer (Official Music Video)",
            "Imagine Dragons",
            "Believer",
        ),
        # Part II kept, Audio noise stripped
        (
            "Rihanna - Love The Way You Lie (Part II) (Audio) ft. Eminem",
            "Rihanna",
            "Love The Way You Lie (Part II)",
        ),
        # No artist prefix — just noise removal
        (
            "Fuck Off (Hard Drums Remix)",
            "Rihanna",
            "Fuck Off (Hard Drums Remix)",
        ),
        # 4K Upgrade stripped
        (
            "Leave Out All The Rest (Official Music Video) [4K Upgrade] - Linkin Park",
            "Linkin Park",
            "Leave Out All The Rest",
        ),
        # Remastered noise stripped
        (
            "Evanescence - Going Under (Remastered 2023) - Official Visualizer",
            "Evanescence",
            "Going Under",
        ),
        # ft. suffix stripped from title along with artist
        (
            "Cut The Bridge (Official Audio Visualizer) - Linkin Park",
            "Linkin Park",
            "Cut The Bridge",
        ),
        # Topic channel style — no separator
        (
            "i love you",
            "Billie Eilish",
            "i love you",
        ),
        # feat. in track title kept
        (
            "Eminem - Love the Way You Lie (feat. Rihanna)",
            "Eminem",
            "Love the Way You Lie (feat. Rihanna)",
        ),
    ],
)
def test_extract_track_title(yt_title, artist, expected):
    assert _extract_track_title(yt_title, artist) == expected
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose -f docker-compose.local.yml exec brainstream python -m pytest tests/unit/test_downloader.py::test_extract_track_title -v`
Expected: FAIL — `ImportError: cannot import name '_extract_track_title'`

- [ ] **Step 3: Implement `_extract_track_title`**

Add to `src/pipeline/downloader.py` after `_normalize` (line ~131):

```python
_NOISE_PAREN_RE = re.compile(
    r"\s*[\(\[]\s*(?:official|video|audio|lyrics?|lyric|visualizer|"
    r"remaster(?:ed)?(?:\s+\d{4})?|upgrade|hd|4k|mv|music\s+video)"
    r"[^\)\]]*[\)\]]",
    re.IGNORECASE,
)

_TRAILING_DASH_ARTIST_RE = re.compile(r"\s*[-–—]\s*$")


def _extract_track_title(yt_title: str, artist: str) -> str:
    """Extract the track title from a YouTube video title.

    Strips artist name, noise parentheticals (Official Video, etc.),
    and trailing punctuation. Preserves musical parentheticals (Part II,
    feat., Remix, etc.).
    """
    title = yt_title.strip()
    if not title:
        return ""

    # Remove "ft./feat." suffix at end of title (e.g. "... ft. Eminem")
    title = re.sub(
        r"\s+(?:ft\.?|feat\.?)\s+[^(\[]*$", "", title, flags=re.IGNORECASE
    )

    # Try "Artist - Track" pattern (most common)
    norm_artist = _normalize(artist)
    for sep in (" - ", " – ", " — ", " − "):
        if sep in title:
            parts = title.split(sep, 1)
            left = parts[0].strip()
            right = parts[1].strip()
            # Check if artist is on the left or right side
            if _normalize(left) and difflib.SequenceMatcher(
                None, norm_artist, _normalize(left)
            ).ratio() >= 0.7:
                title = right
                break
            elif _normalize(right) and difflib.SequenceMatcher(
                None, norm_artist, _normalize(right)
            ).ratio() >= 0.7:
                title = left
                break

    # Strip noise parentheticals
    title = _NOISE_PAREN_RE.sub("", title)

    # Clean up trailing " - " left after artist removal from end
    title = _TRAILING_DASH_ARTIST_RE.sub("", title)

    return title.strip()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose -f docker-compose.local.yml exec brainstream python -m pytest tests/unit/test_downloader.py::test_extract_track_title -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/downloader.py tests/unit/test_downloader.py
git commit -m "feat: add _extract_track_title helper for YouTube title parsing"
```

---

### Task 2: Add `_title_similarity` helper + tests

**Files:**
- Modify: `src/pipeline/downloader.py` (add helper after `_extract_track_title`)
- Test: `tests/unit/test_downloader.py`

- [ ] **Step 1: Write failing tests for `_title_similarity`**

```python
from src.pipeline.downloader import _title_similarity


@pytest.mark.parametrize(
    "yt_title,artist,track_name,min_expected,max_expected",
    [
        # Exact match after extraction
        (
            "Three Days Grace - Animal I Have Become (Official Video)",
            "Three Days Grace",
            "Animal I Have Become",
            0.99, 1.01,
        ),
        # Obvious mismatch
        (
            "Fuck Off (Hard Drums Remix)",
            "Rihanna",
            "Hard (Illmana Remix)",
            0.0, 0.35,
        ),
        # Close but different — Believer vs Believe Her
        (
            "Imagine Dragons - Believer (Official Music Video)",
            "Imagine Dragons",
            "Believe Her",
            0.7, 0.95,
        ),
        # Part II variant
        (
            "Rihanna - Love The Way You Lie (Part II) (Audio) ft. Eminem",
            "Eminem feat. Rihanna",
            "Love the Way You Lie",
            0.7, 0.95,
        ),
    ],
)
def test_title_similarity(yt_title, artist, track_name, min_expected, max_expected):
    ratio = _title_similarity(yt_title, artist, track_name)
    assert min_expected <= ratio <= max_expected, (
        f"Expected {min_expected}-{max_expected}, got {ratio}"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose -f docker-compose.local.yml exec brainstream python -m pytest tests/unit/test_downloader.py::test_title_similarity -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement `_title_similarity`**

Add to `src/pipeline/downloader.py` after `_extract_track_title`:

```python
def _title_similarity(yt_title: str, artist: str, track_name: str) -> float:
    """Compute similarity between YouTube title and requested track name.

    Both sides are normalized: YouTube title has artist/noise stripped,
    track_name has noise parentheticals stripped. Returns 0.0-1.0.
    """
    extracted = _normalize(_NOISE_PAREN_RE.sub("", _extract_track_title(yt_title, artist)))
    normalized_track = _normalize(_NOISE_PAREN_RE.sub("", track_name))
    if not extracted or not normalized_track:
        return 0.0
    return difflib.SequenceMatcher(None, extracted, normalized_track).ratio()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose -f docker-compose.local.yml exec brainstream python -m pytest tests/unit/test_downloader.py::test_title_similarity -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/downloader.py tests/unit/test_downloader.py
git commit -m "feat: add _title_similarity helper for YouTube candidate scoring"
```

---

### Task 3: Integrate title similarity into `_select_best_entry` + tests

**Files:**
- Modify: `src/pipeline/downloader.py:154-212` (`_select_best_entry`)
- Test: `tests/unit/test_downloader.py`

- [ ] **Step 1: Write failing tests**

```python
def test_select_best_entry_filters_low_title_similarity():
    """title 유사도 0.3 미만인 후보는 필터링되어야 한다."""
    entries = [
        _entry("Fuck Off (Hard Drums Remix)", 312.0, channel="Rihanna"),
        _entry("Rihanna - Hard (Official Audio)", 230.0),
    ]
    result = _select_best_entry(
        entries, mb_duration=312.0, artist="Rihanna", track_name="Hard (Illmana Remix)"
    )
    # "Fuck Off"는 유사도 < 0.3이므로 필터링됨, "Hard" 선택
    assert "Hard" in result["title"]


def test_select_best_entry_returns_none_when_all_filtered():
    """모든 후보가 title 유사도 0.3 미만이면 None을 반환해야 한다."""
    entries = [
        _entry("Completely Unrelated Song", 200.0),
        _entry("Another Wrong Track", 210.0),
    ]
    result = _select_best_entry(
        entries, mb_duration=200.0, artist="Radiohead", track_name="Creep"
    )
    assert result is None


def test_select_best_entry_title_similarity_affects_scoring():
    """title 유사도가 낮은 후보는 채널 보너스가 있어도 패널티를 받아야 한다."""
    entries = [
        # Wrong song but official channel → channel bonus -200
        _entry("Imagine Dragons - Believer (Official Music Video)", 217.0, channel="Imagine Dragons"),
        # Right song but random channel
        _entry("Believe Her - Imagine Dragons", 213.0, channel="RandomUser"),
    ]
    result = _select_best_entry(
        entries, mb_duration=213.0, artist="Imagine Dragons", track_name="Believe Her"
    )
    # "Believe Her" should win due to higher title similarity despite no channel bonus
    assert "Believe Her" in result["title"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose -f docker-compose.local.yml exec brainstream python -m pytest tests/unit/test_downloader.py::test_select_best_entry_filters_low_title_similarity tests/unit/test_downloader.py::test_select_best_entry_returns_none_when_all_filtered tests/unit/test_downloader.py::test_select_best_entry_title_similarity_affects_scoring -v`
Expected: FAIL

- [ ] **Step 3: Modify `_select_best_entry`**

Update `_select_best_entry` in `src/pipeline/downloader.py`:

1. Change return type to `Optional[dict]` (can return None when all filtered)
2. After existing live/cover filter, add title similarity filter (threshold 0.3)
3. Add `(1 - title_sim) * 800` to scoring
4. Log title_sim in "selected YouTube result"

```python
def _select_best_entry(
    entries: list[dict],
    mb_duration: Optional[float],
    artist: str = "",
    track_name: str = "",
    strict: bool = True,
) -> Optional[dict]:
    """Select the best YouTube entry using a scoring system.

    Returns None if no candidate meets the title similarity threshold (0.3).

    Scoring factors (lower = better):
    - Title dissimilarity: (1 - similarity) * 800
    - Cover/remix/karaoke title: +1000 penalty
    - Live performance title: +500 penalty
    - Official channel (artist/VEVO/Topic): -200 to -100 bonus
    - Duration proximity to MB: abs difference in seconds
    """
    if not entries:
        raise ValueError("entries list is empty")

    user_wants_cover = _is_cover(track_name)

    if strict:
        clean = [
            e
            for e in entries
            if not _is_live(e.get("title", ""))
            and (user_wants_cover or not _is_cover(e.get("title", "")))
        ]
        if clean:
            log.info(
                "strict mode: filtered entries",
                total=len(entries),
                clean=len(clean),
            )
            entries = clean
        else:
            log.warning(
                "strict mode: no clean entries found, falling back to all candidates",
                total=len(entries),
            )

    # Title similarity filter (threshold 0.3)
    if track_name:
        sim_entries = []
        for e in entries:
            sim = _title_similarity(e.get("title", ""), artist, track_name)
            e["_title_sim"] = sim
            if sim >= 0.3:
                sim_entries.append(e)
            else:
                log.info(
                    "title similarity too low, filtering candidate",
                    yt_title=e.get("title", ""),
                    track_name=track_name,
                    similarity=round(sim, 3),
                )
        if sim_entries:
            entries = sim_entries
        else:
            log.warning(
                "all candidates below title similarity threshold",
                track_name=track_name,
                count=len(entries),
            )
            return None

    def score(e: dict) -> float:
        s = 0.0
        title = e.get("title") or ""
        # Title similarity penalty
        title_sim = e.get("_title_sim")
        if title_sim is None and track_name:
            title_sim = _title_similarity(title, artist, track_name)
        if title_sim is not None:
            s += (1 - title_sim) * 800
        if not user_wants_cover and _is_cover(title):
            s += 1000
        if _is_live(title):
            s += 500
        s += _channel_score(e, artist)
        if mb_duration is not None:
            s += abs((e.get("duration") or 0) - mb_duration)
        return s

    best = min(entries, key=score)

    # Clean up temporary key
    for e in entries:
        e.pop("_title_sim", None)

    return best
```

- [ ] **Step 4: Update `download_track` to handle `None` return from `_select_best_entry`**

In `download_track`, restructure both calls to `_select_best_entry` (lines 250 and 305):

At line ~249-270 (metadata phase) — replace the existing `if entries:` block:
```python
            if entries:
                selected_entry = _select_best_entry(
                    entries, mb_duration, artist, track_name
                )
                if selected_entry is None:
                    log.warning(
                        "no YouTube candidate met title similarity threshold",
                        artist=artist,
                        track=track_name,
                    )
                    entries = []  # fall through to ytsearch1 fallback
                else:
                    yt_dur = selected_entry.get("duration")
                    log.info(
                        "selected YouTube result",
                        title=selected_entry.get("title", ""),
                        yt_duration=yt_dur,
                        mb_duration=mb_duration,
                    )
                    if mb_duration is not None and yt_dur is not None:
                        diff = abs(yt_dur - mb_duration)
                        if diff > _DURATION_WARN_THRESHOLD:
                            log.warning(
                                "YouTube duration deviates significantly from MB duration",
                                yt_duration=yt_dur,
                                mb_duration=mb_duration,
                                diff_seconds=diff,
                                artist=artist,
                                track=track_name,
                            )
```

At line ~304-313 (download loop) — replace the existing candidate selection:
```python
        if remaining_entries:
            candidate_entry = _select_best_entry(
                remaining_entries, mb_duration, artist, track_name
            )
            if candidate_entry is None:
                remaining_entries = []
                continue  # fall through to ytsearch1 fallback
            url = _entry_url(candidate_entry)
            if not url or url in attempted_urls:
                remaining_entries = [
                    e for e in remaining_entries if e is not candidate_entry
                ]
                continue
            download_target = url
```

At the ytsearch1 fallback download — add post-download title check. After line ~330 where download succeeds via ytsearch1, add a title similarity check on the downloaded entry:
```python
                    if info:
                        entry = (
                            info.get("entries", [info])[0]
                            if "entries" in info
                            else info
                        )
                        # ytsearch1 fallback: verify title similarity
                        if (
                            track_name
                            and download_target.startswith("ytsearch1:")
                            and entry.get("title")
                        ):
                            fallback_sim = _title_similarity(
                                entry["title"], artist, track_name
                            )
                            if fallback_sim < 0.3:
                                log.warning(
                                    "ytsearch1 fallback title similarity too low",
                                    yt_title=entry["title"],
                                    track_name=track_name,
                                    similarity=round(fallback_sim, 3),
                                )
                                # Clean up any partially downloaded file
                                for ext in ("flac", "opus", "webm", "m4a", "mp3"):
                                    candidate_file = Path(staging_dir) / f"{mbid}.{ext}"
                                    if candidate_file.exists():
                                        candidate_file.unlink()
                                break  # exit download loop → return None, None
                        thumbnail_url = entry.get("thumbnail", "")
                        channel = entry.get("channel") or entry.get("uploader", "")
                        # ... rest of existing metadata extraction ...
```

- [ ] **Step 5: Run all downloader tests**

Run: `docker compose -f docker-compose.local.yml exec brainstream python -m pytest tests/unit/test_downloader.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/downloader.py tests/unit/test_downloader.py
git commit -m "feat: add title similarity filter and scoring to _select_best_entry"
```

---

### Task 4: Add canonical title validation to `_enrich_track` + tests

**Files:**
- Modify: `src/pipeline/tagger.py:464-508` (`_enrich_track` iTunes/Deezer section)
- Test: `tests/unit/test_tagger.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_tagger.py`:

```python
def test_enrich_track_rejects_low_similarity_itunes_canonical_title(
    tmp_path, monkeypatch
):
    """iTunes trackName 유사도가 0.5 미만이면 canonical_title로 채택하지 않아야 한다."""
    flac_path = tmp_path / "test.flac"
    _make_minimal_flac(flac_path)

    # iTunes returns "Believer (Live in Vegas)" for request "Believe Her"
    monkeypatch.setattr(
        "src.pipeline.tagger.itunes_search",
        lambda artist, track, country=None: {
            "album": "Some Album",
            "artwork_url": "",
            "artistName": "Imagine Dragons",
            "trackName": "Believer (Live in Vegas)",
        },
    )
    monkeypatch.setattr(
        "src.pipeline.tagger.deezer_search",
        lambda artist, track: {},
    )
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording",
        lambda artist, track: ([], "", ""),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger.embed_art_from_url",
        lambda *a, **kw: False,
    )

    album, canonical_artist, canonical_title = _enrich_track(
        str(flac_path),
        artist="Imagine Dragons",
        track_name="Believe Her",
    )

    # Album should still be set (artist similarity passed)
    assert album == "Some Album"
    # But canonical_title should be empty (track similarity < 0.5)
    assert canonical_title == ""


def test_enrich_track_accepts_high_similarity_itunes_canonical_title(
    tmp_path, monkeypatch
):
    """iTunes trackName 유사도가 0.5 이상이면 canonical_title로 채택한다."""
    flac_path = tmp_path / "test.flac"
    _make_minimal_flac(flac_path)

    monkeypatch.setattr(
        "src.pipeline.tagger.itunes_search",
        lambda artist, track, country=None: {
            "album": "Minutes to Midnight",
            "artwork_url": "",
            "artistName": "Linkin Park",
            "trackName": "Leave Out All The Rest",
        },
    )
    monkeypatch.setattr(
        "src.pipeline.tagger.deezer_search",
        lambda artist, track: {},
    )
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording",
        lambda artist, track: ([], "", ""),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger.embed_art_from_url",
        lambda *a, **kw: False,
    )

    album, canonical_artist, canonical_title = _enrich_track(
        str(flac_path),
        artist="Linkin Park",
        track_name="Leave Out All the Rest",
    )

    assert album == "Minutes to Midnight"
    assert canonical_title == "Leave Out All The Rest"


def test_enrich_track_rejects_low_similarity_deezer_canonical_title(
    tmp_path, monkeypatch
):
    """Deezer trackName 유사도가 0.5 미만이면 canonical_title로 채택하지 않아야 한다."""
    flac_path = tmp_path / "test.flac"
    _make_minimal_flac(flac_path)

    # iTunes returns nothing, Deezer returns wrong track name
    monkeypatch.setattr(
        "src.pipeline.tagger.itunes_search",
        lambda artist, track, country=None: {},
    )
    monkeypatch.setattr(
        "src.pipeline.tagger.deezer_search",
        lambda artist, track: {
            "album": "Halcyon",
            "artwork_url": "",
            "artistName": "Ellie Goulding",
            "trackName": "Lights (Drop Lamond Remix)",
        },
    )
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording",
        lambda artist, track: ([], "", ""),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger.embed_art_from_url",
        lambda *a, **kw: False,
    )

    album, canonical_artist, canonical_title = _enrich_track(
        str(flac_path),
        artist="Ellie Goulding",
        track_name="Dead in the Water (Drop Lamond remix)",
    )

    assert album == "Halcyon"
    assert canonical_title == ""  # Deezer trackName rejected
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose -f docker-compose.local.yml exec brainstream python -m pytest tests/unit/test_tagger.py::test_enrich_track_rejects_low_similarity_itunes_canonical_title tests/unit/test_tagger.py::test_enrich_track_accepts_high_similarity_itunes_canonical_title -v`
Expected: FAIL (first test — canonical_title will be "Believer (Live in Vegas)" instead of "")

- [ ] **Step 3: Add track name similarity check to `_enrich_track`**

In `src/pipeline/tagger.py`, modify the iTunes canonical_title assignment (around line 476-477):

```python
        if album:
            canonical_artist = itunes_result.get("artistName", "")
            itunes_track_name = itunes_result.get("trackName", "")
            # Only adopt canonical title if it's similar enough to the requested track
            if itunes_track_name:
                track_sim = difflib.SequenceMatcher(
                    None,
                    _normalize_for_match(track_name),
                    _normalize_for_match(itunes_track_name),
                ).ratio()
                if track_sim >= 0.5:
                    canonical_title = itunes_track_name
                else:
                    log.info(
                        "iTunes trackName rejected (similarity too low)",
                        requested=track_name,
                        itunes_track=itunes_track_name,
                        similarity=round(track_sim, 3),
                    )
```

Apply the same pattern for Deezer (around line 500-502):

```python
            if album:
                canonical_artist = deezer_result.get("artistName", "")
                deezer_track_name = deezer_result.get("trackName", "")
                if not canonical_title and deezer_track_name:
                    track_sim = difflib.SequenceMatcher(
                        None,
                        _normalize_for_match(track_name),
                        _normalize_for_match(deezer_track_name),
                    ).ratio()
                    if track_sim >= 0.5:
                        canonical_title = deezer_track_name
                    else:
                        log.info(
                            "Deezer trackName rejected (similarity too low)",
                            requested=track_name,
                            deezer_track=deezer_track_name,
                            similarity=round(track_sim, 3),
                        )
```

- [ ] **Step 4: Run all tagger tests**

Run: `docker compose -f docker-compose.local.yml exec brainstream python -m pytest tests/unit/test_tagger.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/tagger.py tests/unit/test_tagger.py
git commit -m "feat: validate track name similarity before adopting iTunes/Deezer canonical title"
```

---

### Task 5: Docker rebuild and manual verification

**Files:** None (integration test)

- [ ] **Step 1: Rebuild and restart**

```bash
./restart_local_docker.sh
```

- [ ] **Step 2: Verify via logs**

Trigger a manual download of a known-problematic case and check logs:

```bash
# Request a download that previously failed
curl -X POST http://localhost:8080/api/download \
  -H "Content-Type: application/json" \
  -d '{"artist": "Imagine Dragons", "track": "Believe Her"}'

# Watch logs for title similarity filtering
docker compose -f docker-compose.local.yml logs -f brainstream | grep -E "(title similarity|filtering candidate|canonical.*rejected)"
```

Expected: "Believer" candidates should show title similarity logging, and if no good match exists, download should fail rather than downloading "Believer".

- [ ] **Step 3: Commit if any adjustments needed**

```bash
git add -A
git commit -m "fix: adjust title similarity thresholds based on manual testing"
```
