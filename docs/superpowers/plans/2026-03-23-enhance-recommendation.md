# Enhance Recommendation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CF 추천 offset 진행 + LB Radio 탐색 소스 + 파이프라인 주기 UI 제어로 추천 다양성 확보

**Architecture:** 기존 `run_pipeline()`을 CF offset 진행 + LB Radio 두 소스로 확장. state.db에 `settings` 테이블 추가하여 offset/interval 영속화. `schedule` 라이브러리 제거 후 직접 시간 비교 스케줄러로 교체. UI에 interval 드롭다운 추가.

**Tech Stack:** Python 3.12, FastAPI, SQLite, requests, pytest

**Spec:** `docs/superpowers/specs/2026-03-23-enhance-recommendation-design.md`

---

### Task 1: settings 테이블 추가 (state.py)

**Files:**
- Modify: `src/state.py:25-60` (init_db에 settings 테이블 추가)
- Modify: `src/state.py` (get_setting, set_setting 함수 추가)
- Test: `tests/unit/test_state.py`

- [ ] **Step 1: Write failing tests for get_setting / set_setting**

`tests/unit/test_state.py` 하단에 추가:

```python
# ── settings 테이블 ──────────────────────────────────────────────────────────


def test_get_setting_returns_default_when_not_set(tmp_state_db):
    from src.state import get_setting
    assert get_setting(tmp_state_db, "cf_offset", "0") == "0"


def test_set_and_get_setting(tmp_state_db):
    from src.state import get_setting, set_setting
    set_setting(tmp_state_db, "cf_offset", "25")
    assert get_setting(tmp_state_db, "cf_offset", "0") == "25"


def test_set_setting_overwrites_existing(tmp_state_db):
    from src.state import get_setting, set_setting
    set_setting(tmp_state_db, "cf_offset", "25")
    set_setting(tmp_state_db, "cf_offset", "50")
    assert get_setting(tmp_state_db, "cf_offset", "0") == "50"


def test_get_setting_returns_default_when_empty_string_key(tmp_state_db):
    from src.state import get_setting
    assert get_setting(tmp_state_db, "nonexistent") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/roh-suin/.superset/worktrees/brain-stream/feat/enhance-recommendation && python -m pytest tests/unit/test_state.py -k "test_get_setting or test_set_setting" -v`
Expected: FAIL — `ImportError: cannot import name 'get_setting'`

- [ ] **Step 3: Implement settings table and functions**

`src/state.py` — `init_db()` 함수 끝에 settings 테이블 생성 추가:

```python
        # Settings table for persistent configuration
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
```

`src/state.py` — 파일 끝에 함수 추가:

```python
def get_setting(db_path: str, key: str, default: str = "") -> str:
    """settings 테이블에서 값 조회. 없으면 default 반환."""
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_setting(db_path: str, key: str, value: str) -> None:
    """settings 테이블에 값 저장 (INSERT OR REPLACE)."""
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/roh-suin/.superset/worktrees/brain-stream/feat/enhance-recommendation && python -m pytest tests/unit/test_state.py -k "test_get_setting or test_set_setting" -v`
Expected: 4 PASSED

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `cd /Users/roh-suin/.superset/worktrees/brain-stream/feat/enhance-recommendation && python -m pytest tests/unit/test_state.py -v`
Expected: All tests PASS (기존 테스트는 init_db가 settings 테이블도 생성하므로 영향 없음)

- [ ] **Step 6: Commit**

```bash
git add src/state.py tests/unit/test_state.py
git commit -m "feat: add settings table with get_setting/set_setting"
```

---

### Task 2: fetch_recommendations에 offset 파라미터 추가 (listenbrainz.py)

**Files:**
- Modify: `src/pipeline/listenbrainz.py:13-14` (함수 시그니처)
- Modify: `src/pipeline/listenbrainz.py:18` (params에 offset 추가)
- Test: `tests/unit/test_listenbrainz.py`

- [ ] **Step 1: Write failing test for offset parameter**

`tests/unit/test_listenbrainz.py` 하단에 추가:

```python
def test_fetch_recommendations_passes_offset_param(monkeypatch):
    """offset 파라미터가 API 요청에 전달되는지 확인한다."""
    fake_response = _make_mock_response({"payload": {"mbids": []}})

    with patch(
        "src.pipeline.listenbrainz.requests.get", return_value=fake_response
    ) as mock_get:
        fetch_recommendations("myuser", "mytoken", count=20, offset=50)

    _, kwargs = mock_get.call_args
    assert kwargs["params"] == {"count": 20, "offset": 50}


def test_fetch_recommendations_default_offset_is_zero(monkeypatch):
    """offset 미지정 시 기본값 0이 전달된다."""
    fake_response = _make_mock_response({"payload": {"mbids": []}})

    with patch(
        "src.pipeline.listenbrainz.requests.get", return_value=fake_response
    ) as mock_get:
        fetch_recommendations("myuser", "mytoken", count=10)

    _, kwargs = mock_get.call_args
    assert kwargs["params"] == {"count": 10, "offset": 0}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/roh-suin/.superset/worktrees/brain-stream/feat/enhance-recommendation && python -m pytest tests/unit/test_listenbrainz.py -k "test_fetch_recommendations_passes_offset or test_fetch_recommendations_default_offset" -v`
Expected: FAIL — `TypeError: fetch_recommendations() got an unexpected keyword argument 'offset'` 또는 params에 offset 미포함

- [ ] **Step 3: Implement offset parameter**

`src/pipeline/listenbrainz.py` — 함수 시그니처에 offset 추가:

```python
def fetch_recommendations(
    username: str, token: str, count: int = 25, offset: int = 0
) -> List[Dict[str, Any]]:
```

params에 offset 추가:

```python
    params = {"count": count, "offset": offset}
```

log에 offset 추가:

```python
    log.info("fetching recommendations", username=username, count=count, offset=offset)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/roh-suin/.superset/worktrees/brain-stream/feat/enhance-recommendation && python -m pytest tests/unit/test_listenbrainz.py -v`
Expected: All tests PASS (기존 test_fetch_recommendations_passes_correct_headers_and_params는 offset=0이 추가되므로 params 비교 업데이트 필요)

**주의**: 기존 `test_fetch_recommendations_passes_correct_headers_and_params` 테스트에서 `assert kwargs["params"] == {"count": 10}` → `assert kwargs["params"] == {"count": 10, "offset": 0}`으로 수정 필요.

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/listenbrainz.py tests/unit/test_listenbrainz.py
git commit -m "feat: add offset parameter to fetch_recommendations"
```

---

### Task 3: fetch_user_top_artists / fetch_lb_radio 추가 (listenbrainz.py)

**Files:**
- Modify: `src/pipeline/listenbrainz.py` (두 함수 추가)
- Test: `tests/unit/test_listenbrainz.py`

- [ ] **Step 1: Write failing tests for fetch_user_top_artists**

`tests/unit/test_listenbrainz.py`에 추가:

```python
from src.pipeline.listenbrainz import fetch_user_top_artists


def test_fetch_user_top_artists_parses_response(monkeypatch):
    """탑 아티스트 API 응답을 파싱하여 artist_name/artist_mbid 리스트를 반환한다."""
    fake_response = _make_mock_response({
        "payload": {
            "artists": [
                {"artist_name": "Radiohead", "artist_mbid": "a74b1b7f-71a5-4011-9441-d0b5e4122711"},
                {"artist_name": "IU", "artist_mbid": "b9545342-1e6d-4dae-84ac-013374ad8d7c"},
            ]
        }
    })

    with patch("src.pipeline.listenbrainz.requests.get", return_value=fake_response):
        results = fetch_user_top_artists("testuser", range_="quarter", count=5)

    assert len(results) == 2
    assert results[0]["artist_name"] == "Radiohead"
    assert results[1]["artist_mbid"] == "b9545342-1e6d-4dae-84ac-013374ad8d7c"


def test_fetch_user_top_artists_returns_empty_on_error(monkeypatch):
    """API 에러 시 빈 리스트를 반환한다."""
    with patch(
        "src.pipeline.listenbrainz.requests.get",
        side_effect=requests.ConnectionError("timeout"),
    ):
        results = fetch_user_top_artists("testuser")

    assert results == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/roh-suin/.superset/worktrees/brain-stream/feat/enhance-recommendation && python -m pytest tests/unit/test_listenbrainz.py -k "test_fetch_user_top_artists" -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement fetch_user_top_artists**

`src/pipeline/listenbrainz.py` 파일 끝에 추가:

```python
def fetch_user_top_artists(
    username: str, range_: str = "quarter", count: int = 10
) -> List[Dict[str, Any]]:
    """유저 탑 아티스트 조회. API 실패 시 빈 리스트 반환."""
    url = f"{LB_BASE}/stats/user/{username}/artists"
    params = {"range": range_, "count": count}
    try:
        log.info("fetching top artists", username=username, range=range_, count=count)
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        artists = data.get("payload", {}).get("artists", [])
        log.info("top artists fetched", count=len(artists))
        return [
            {"artist_name": a["artist_name"], "artist_mbid": a.get("artist_mbid", "")}
            for a in artists
            if a.get("artist_name")
        ]
    except Exception as exc:
        log.warning("failed to fetch top artists", error=str(exc))
        return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/roh-suin/.superset/worktrees/brain-stream/feat/enhance-recommendation && python -m pytest tests/unit/test_listenbrainz.py -k "test_fetch_user_top_artists" -v`
Expected: 2 PASSED

- [ ] **Step 5: Write failing tests for fetch_lb_radio**

`tests/unit/test_listenbrainz.py`에 추가:

```python
from src.pipeline.listenbrainz import fetch_lb_radio


def test_fetch_lb_radio_parses_jspf(monkeypatch):
    """JSPF 응답을 파싱하여 mbid/artist/track_name 리스트를 반환한다."""
    fake_response = _make_mock_response({
        "payload": {
            "jspf": {
                "playlist": {
                    "tracks": [
                        {
                            "title": "Creep",
                            "creator": "Radiohead",
                            "identifier": "https://musicbrainz.org/recording/aaaa-0001",
                        },
                        {
                            "title": "Karma Police",
                            "creator": "Radiohead",
                            "identifier": "https://musicbrainz.org/recording/bbbb-0002",
                        },
                    ]
                }
            }
        }
    })

    with patch("src.pipeline.listenbrainz.requests.get", return_value=fake_response):
        results = fetch_lb_radio("artist:(Radiohead)", mode="easy")

    assert len(results) == 2
    assert results[0]["mbid"] == "aaaa-0001"
    assert results[0]["artist"] == "Radiohead"
    assert results[0]["track_name"] == "Creep"


def test_fetch_lb_radio_skips_entries_without_identifier(monkeypatch):
    """identifier가 없는 트랙은 건너뛴다."""
    fake_response = _make_mock_response({
        "payload": {
            "jspf": {
                "playlist": {
                    "tracks": [
                        {"title": "Valid", "creator": "Artist", "identifier": "https://musicbrainz.org/recording/aaaa-0001"},
                        {"title": "Invalid", "creator": "Artist"},
                    ]
                }
            }
        }
    })

    with patch("src.pipeline.listenbrainz.requests.get", return_value=fake_response):
        results = fetch_lb_radio("artist:(Test)", mode="easy")

    assert len(results) == 1


def test_fetch_lb_radio_returns_empty_on_error(monkeypatch):
    """API 에러 시 빈 리스트를 반환한다."""
    with patch(
        "src.pipeline.listenbrainz.requests.get",
        side_effect=requests.ConnectionError("timeout"),
    ):
        results = fetch_lb_radio("artist:(Test)")

    assert results == []


def test_fetch_lb_radio_falls_back_to_lookup_when_creator_missing(monkeypatch):
    """creator가 없는 트랙은 lookup_recording으로 보강한다."""
    fake_response = _make_mock_response({
        "payload": {
            "jspf": {
                "playlist": {
                    "tracks": [
                        {
                            "title": "",
                            "creator": "",
                            "identifier": "https://musicbrainz.org/recording/aaaa-0001",
                        },
                    ]
                }
            }
        }
    })

    def fake_lookup(mbid):
        return {"artist": "Radiohead", "title": "Creep"}

    with (
        patch("src.pipeline.listenbrainz.requests.get", return_value=fake_response),
        patch("src.pipeline.listenbrainz.lookup_recording", side_effect=fake_lookup),
    ):
        results = fetch_lb_radio("artist:(Radiohead)", mode="easy")

    assert len(results) == 1
    assert results[0]["artist"] == "Radiohead"
    assert results[0]["track_name"] == "Creep"
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `cd /Users/roh-suin/.superset/worktrees/brain-stream/feat/enhance-recommendation && python -m pytest tests/unit/test_listenbrainz.py -k "test_fetch_lb_radio" -v`
Expected: FAIL — `ImportError`

- [ ] **Step 7: Implement fetch_lb_radio**

`src/pipeline/listenbrainz.py` 파일 끝에 추가:

```python
def fetch_lb_radio(
    prompt: str, mode: str = "easy"
) -> List[Dict[str, Any]]:
    """LB Radio API 호출. JSPF 파싱하여 [{mbid, artist, track_name}, ...] 반환.
    API 실패 시 빈 리스트 반환.
    """
    url = f"{LB_BASE}/explore/lb-radio"
    params = {"prompt": prompt, "mode": mode}
    try:
        log.info("fetching lb-radio", prompt=prompt, mode=mode)
        resp = requests.get(url, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        tracks = (
            data.get("payload", {})
            .get("jspf", {})
            .get("playlist", {})
            .get("tracks", [])
        )
        results = []
        for t in tracks:
            identifier = t.get("identifier", "")
            if not identifier:
                continue
            mbid = identifier.rstrip("/").split("/")[-1]
            artist = t.get("creator", "")
            title = t.get("title", "")
            if not artist or not title:
                meta = lookup_recording(mbid)
                artist = artist or meta.get("artist", "")
                title = title or meta.get("title", "")
            if not artist or not title:
                log.warning("skipping radio track: missing metadata", mbid=mbid)
                continue
            results.append({"mbid": mbid, "artist": artist, "track_name": title})
        log.info("lb-radio tracks fetched", count=len(results))
        return results
    except Exception as exc:
        log.warning("failed to fetch lb-radio", error=str(exc))
        return []
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd /Users/roh-suin/.superset/worktrees/brain-stream/feat/enhance-recommendation && python -m pytest tests/unit/test_listenbrainz.py -v`
Expected: All tests PASS

- [ ] **Step 9: Commit**

```bash
git add src/pipeline/listenbrainz.py tests/unit/test_listenbrainz.py
git commit -m "feat: add fetch_user_top_artists and fetch_lb_radio"
```

---

### Task 4: run_pipeline() 리팩터 — CF offset + Radio 통합 (main.py)

**Files:**
- Modify: `src/main.py:1-10` (import 변경 — schedule 제거, 새 함수 추가)
- Modify: `src/main.py:26-83` (run_pipeline 전면 리팩터)
- Modify: `src/main.py:86-91` (_run_scheduler 교체)
- Modify: `requirements.txt:4` (schedule 제거)
- Modify: `tests/conftest.py:27-32` (schedule stub 제거)

- [ ] **Step 1: Update imports in main.py**

`src/main.py` 상단 import 변경:

```python
import random
import threading
import time

import uvicorn

import src.api as api_module
import src.worker as worker_module
from src.config import load_config
from src.pipeline.listenbrainz import (
    fetch_lb_radio,
    fetch_recommendations,
    fetch_user_top_artists,
)
from src.pipeline.musicbrainz import lookup_recording
from src.state import (
    get_download_by_mbid,
    get_pending_jobs,
    get_retryable,
    get_setting,
    init_db,
    is_downloaded,
    mark_failed,
    mark_pending,
    set_setting,
)
from src.utils.logger import get_logger, setup_logger
```

`import schedule` 라인 삭제.

- [ ] **Step 2: Rewrite run_pipeline()**

`src/main.py`의 `run_pipeline(cfg)` 함수를 교체:

```python
def run_pipeline(cfg):
    log.info("pipeline started")
    db = cfg.state_db
    target = cfg.listenbrainz.recommendation_count  # default 25
    cf_target = round(target * 0.8)
    radio_target = target - cf_target
    cf_exhausted = False

    # ── 1. CF 추천 (취향 80%) ──
    cf_tracks = []
    try:
        offset = int(get_setting(db, "cf_offset", "0"))
        cf_tracks = fetch_recommendations(
            cfg.listenbrainz.username,
            cfg.listenbrainz.token,
            count=cf_target,
            offset=offset,
        )
        if cf_tracks:
            # 모델 갱신 감지
            new_first = cf_tracks[0]["mbid"]
            old_first = get_setting(db, "cf_first_mbid", "")
            if old_first and new_first != old_first and offset > 0:
                log.info("CF model refreshed, resetting offset")
                offset = 0
                cf_tracks = fetch_recommendations(
                    cfg.listenbrainz.username,
                    cfg.listenbrainz.token,
                    count=cf_target,
                    offset=0,
                )
                new_first = cf_tracks[0]["mbid"] if cf_tracks else ""
            if cf_tracks:
                set_setting(db, "cf_first_mbid", new_first)
                set_setting(db, "cf_offset", str(offset + len(cf_tracks)))
            else:
                cf_exhausted = True
                radio_target += cf_target
        else:
            cf_exhausted = True
            radio_target += cf_target
            log.info("CF pool exhausted, shifting target to radio")
    except Exception as exc:
        log.error("CF fetch failed, proceeding with radio only", error=str(exc))
        cf_exhausted = True
        radio_target += cf_target

    # ── 2. LB Radio (탐색 20%) ──
    radio_tracks = []
    try:
        top_artists = fetch_user_top_artists(
            cfg.listenbrainz.username, range_="quarter", count=10
        )
        if not top_artists:
            top_artists = fetch_user_top_artists(
                cfg.listenbrainz.username, range_="all_time", count=10
            )
        if top_artists:
            seed = random.choice(top_artists)
            prompt = f'artist:({seed["artist_name"]})'
            log.info("lb-radio seed artist", artist=seed["artist_name"])
            radio_tracks = fetch_lb_radio(prompt, mode="easy")
            radio_tracks = radio_tracks[:radio_target]
    except Exception as exc:
        log.warning("radio fetch failed", error=str(exc))

    # Radio 실패 시 CF 폴백 (CF가 소진되지 않은 경우에만)
    if not radio_tracks and not cf_exhausted:
        cur_offset = int(get_setting(db, "cf_offset", "0"))
        extra = fetch_recommendations(
            cfg.listenbrainz.username,
            cfg.listenbrainz.token,
            count=radio_target,
            offset=cur_offset,
        )
        radio_tracks = extra
        if extra:
            set_setting(db, "cf_offset", str(cur_offset + len(extra)))

    # ── 3. 중복 필터링 ──
    all_tracks = cf_tracks + radio_tracks
    new_tracks = [t for t in all_tracks if not is_downloaded(db, t["mbid"])]
    log.info("tracks to process", new=len(new_tracks), total=len(all_tracks))

    # ── 4. 재시도 큐 추가 ──
    retryable = get_retryable(db)
    if retryable:
        log.info("retrying failed tracks", count=len(retryable))
        new_tracks = retryable + new_tracks

    if not new_tracks:
        log.info("nothing new to download")
        return

    # ── 5. enqueue ──
    for track in new_tracks:
        mbid = track["mbid"]
        artist = track.get("artist", "")
        track_name = track.get("track_name", "")

        if (not artist or not track_name) and not mbid.startswith("manual-"):
            log.info("retry track missing metadata, re-looking up from MB", mbid=mbid)
            meta = lookup_recording(mbid)
            artist = artist or meta.get("artist", "")
            track_name = track_name or meta.get("title", "")
            if not artist or not track_name:
                log.warning("MB lookup still empty, skipping", mbid=mbid)
                mark_failed(db, mbid, "MB lookup returned empty artist/track")
                continue

        mark_pending(db, mbid, track_name, artist)
        worker_module.enqueue_job(
            job_id=mbid,
            artist=artist,
            track=track_name,
            source="listenbrainz",
        )

    log.info("pipeline finished — jobs enqueued")
```

- [ ] **Step 3: Replace _run_scheduler()**

`src/main.py`의 `_run_scheduler` 함수를 교체:

```python
def _run_scheduler(cfg):
    last_run = time.time()  # 초기 실행은 별도 스레드에서 이미 수행
    while not worker_module._shutdown_event.is_set():
        worker_module._shutdown_event.wait(60)
        interval = int(
            get_setting(
                cfg.state_db,
                "pipeline_interval_hours",
                str(cfg.scheduler.interval_hours),
            )
        )
        if time.time() - last_run >= interval * 3600:
            run_pipeline(cfg)
            last_run = time.time()
```

- [ ] **Step 4: Remove schedule from requirements.txt**

`requirements.txt`에서 `schedule>=1.2.0` 라인 삭제.

- [ ] **Step 5: Remove schedule stub from conftest.py**

`tests/conftest.py`에서 schedule stub 블록 삭제 (line 27-32):

```python
# 이 블록 전체 삭제:
# schedule: 스케줄러 라이브러리. 로컬에 없으면 stub으로 대체한다.
if "schedule" not in sys.modules:
    _schedule_stub = types.ModuleType("schedule")
    _schedule_stub.every = MagicMock()
    _schedule_stub.run_pending = MagicMock()
    sys.modules["schedule"] = _schedule_stub
```

- [ ] **Step 6: Run full test suite**

Run: `cd /Users/roh-suin/.superset/worktrees/brain-stream/feat/enhance-recommendation && python -m pytest tests/ -v`
Expected: All tests PASS (main.py의 import schedule이 제거되었으므로 conftest의 stub도 불필요)

- [ ] **Step 7: Commit**

```bash
git add src/main.py requirements.txt tests/conftest.py
git commit -m "feat: rewrite pipeline with CF offset progression and LB Radio source"
```

---

### Task 5: pipeline-interval API 엔드포인트 (api.py)

**Files:**
- Modify: `src/api.py` (GET/PUT /api/settings/pipeline-interval 추가)
- Test: `tests/integration/test_api.py`

- [ ] **Step 1: Write failing tests**

`tests/integration/test_api.py` 하단에 추가:

```python
# ── Pipeline Interval Settings ────────────────────────────────────────────


def test_get_pipeline_interval_returns_default(client):
    resp = client.get("/api/settings/pipeline-interval")
    assert resp.status_code == 200
    data = resp.json()
    assert data["interval_hours"] == 6


def test_put_pipeline_interval_updates_value(client):
    resp = client.put(
        "/api/settings/pipeline-interval",
        json={"interval_hours": 12},
    )
    assert resp.status_code == 200
    assert resp.json()["interval_hours"] == 12

    # Verify persisted
    resp2 = client.get("/api/settings/pipeline-interval")
    assert resp2.json()["interval_hours"] == 12


def test_put_pipeline_interval_rejects_invalid(client):
    resp = client.put(
        "/api/settings/pipeline-interval",
        json={"interval_hours": 0},
    )
    assert resp.status_code == 422

    resp2 = client.put(
        "/api/settings/pipeline-interval",
        json={"interval_hours": 25},
    )
    assert resp2.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/roh-suin/.superset/worktrees/brain-stream/feat/enhance-recommendation && python -m pytest tests/integration/test_api.py -k "test_get_pipeline_interval or test_put_pipeline_interval" -v`
Expected: FAIL — 404 Not Found

- [ ] **Step 3: Implement endpoints**

`src/api.py` — import에 `get_setting, set_setting` 추가:

```python
from src.state import (
    get_download_by_mbid,
    get_downloads_page,
    get_setting,
    mark_ignored,
    mark_pending_if_not_duplicate,
    set_setting,
    update_track_info,
)
```

`src/api.py` — rate limits에 PUT 추가:

```python
_RATE_LIMITS: dict[str, int] = {
    "POST /api/download": 10,
    "POST /api/pipeline/run": 2,
    "POST /api/rematch/apply": 10,
    "POST /api/edit/": 10,
    "DELETE /api/downloads/": 10,
    "PUT /api/settings/": 10,
}
```

`src/api.py` — 엔드포인트 추가 (파일 끝, 기존 엔드포인트 뒤):

```python
# ── Pipeline Interval Settings ────────────────────────────────────────────────


@app.get("/api/settings/pipeline-interval")
async def get_pipeline_interval():
    value = get_setting(
        _cfg.state_db,
        "pipeline_interval_hours",
        str(_cfg.scheduler.interval_hours),
    )
    return {"interval_hours": int(value)}


class IntervalUpdate(BaseModel):
    interval_hours: int = Field(ge=1, le=24)


@app.put("/api/settings/pipeline-interval")
async def set_pipeline_interval(body: IntervalUpdate):
    set_setting(
        _cfg.state_db,
        "pipeline_interval_hours",
        str(body.interval_hours),
    )
    return {"interval_hours": body.interval_hours}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/roh-suin/.superset/worktrees/brain-stream/feat/enhance-recommendation && python -m pytest tests/integration/test_api.py -k "test_get_pipeline_interval or test_put_pipeline_interval" -v`
Expected: 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/api.py tests/integration/test_api.py
git commit -m "feat: add GET/PUT /api/settings/pipeline-interval endpoints"
```

---

### Task 6: UI — 파이프라인 주기 드롭다운 (index.html)

**Files:**
- Modify: `src/static/index.html:1265-1270` (LB Pipeline 섹션에 드롭다운 추가)
- Modify: `src/static/index.html` (JS에 interval 로직 추가)

- [ ] **Step 1: Add interval dropdown HTML**

`src/static/index.html`의 LB Pipeline 섹션 (line ~1267-1270)을 수정:

기존:
```html
    <div class="lb-row">
      <button class="btn-secondary" onclick="runPipeline()">Run LB Pipeline</button>
      <span id="lb-msg" style="font-size:0.83rem; color:#718096;"></span>
    </div>
```

변경:
```html
    <div class="lb-row">
      <button class="btn-secondary" onclick="runPipeline()">Run LB Pipeline</button>
      <label style="font-size:0.83rem; color:#718096; display:inline-flex; align-items:center; gap:6px; margin-left:12px;">
        Every
        <select id="pipeline-interval" onchange="updateInterval(this.value)"
                style="background:#1a1d2e; color:#e2e8f0; border:1px solid #2d3147; border-radius:4px; padding:2px 6px; font-size:0.83rem;">
          <option value="1">1h</option>
          <option value="2">2h</option>
          <option value="3">3h</option>
          <option value="6" selected>6h</option>
          <option value="12">12h</option>
          <option value="24">24h</option>
        </select>
      </label>
      <span id="lb-msg" style="font-size:0.83rem; color:#718096;"></span>
    </div>
```

- [ ] **Step 2: Add interval JS logic**

`src/static/index.html`의 `runPipeline()` 함수 바로 위에 interval 로직 추가:

```javascript
  // ── Pipeline Interval ──────────────────────────────────────────────────────
  async function loadInterval() {
    try {
      const res = await fetch('/api/settings/pipeline-interval');
      if (res.ok) {
        const data = await res.json();
        document.getElementById('pipeline-interval').value = data.interval_hours;
      }
    } catch (e) { /* use default */ }
  }

  async function updateInterval(value) {
    const select = document.getElementById('pipeline-interval');
    const prev = select.dataset.prev || select.value;
    select.dataset.prev = value;
    try {
      const res = await fetch('/api/settings/pipeline-interval', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ interval_hours: parseInt(value) }),
      });
      if (!res.ok) throw new Error(await res.text());
    } catch (e) {
      select.value = prev;
      showToast('Failed to update interval: ' + e.message, true);
    }
  }

  loadInterval();
```

- [ ] **Step 3: Manual test — verify in browser**

브라우저에서 `http://localhost:8080` 접속:
1. LB Pipeline 섹션에 드롭다운이 보이는지 확인
2. 값 변경 시 PUT 요청이 발생하는지 DevTools Network 탭에서 확인
3. 페이지 새로고침 후 변경된 값이 유지되는지 확인

- [ ] **Step 4: Commit**

```bash
git add src/static/index.html
git commit -m "feat: add pipeline interval dropdown to UI"
```

---

### Task 7: 문서 업데이트

**Files:**
- Modify: `docs/architecture.md` (추천 소스 구조, 스케줄러 변경 반영)
- Modify: `docs/api-spec.md` (새 엔드포인트 문서화)
- Modify: `docs/data-model.md` (settings 테이블 추가)

- [ ] **Step 1: Update architecture.md**

파이프라인 흐름 섹션에 CF offset 진행 + LB Radio 소스 추가 설명.
스케줄러 섹션에서 `schedule` 라이브러리 → 직접 시간 비교로 변경 반영.

- [ ] **Step 2: Update api-spec.md**

새 엔드포인트 추가:
- `GET /api/settings/pipeline-interval`
- `PUT /api/settings/pipeline-interval`

- [ ] **Step 3: Update data-model.md**

`settings` 테이블 스키마 및 저장 키 목록 문서화.

- [ ] **Step 4: Commit**

```bash
git add docs/architecture.md docs/api-spec.md docs/data-model.md
git commit -m "doc: update docs for recommendation enhancement"
```
