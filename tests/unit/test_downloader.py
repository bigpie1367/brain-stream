"""
tests/unit/test_downloader.py
downloader.py의 download_track 함수 단위 테스트 (yt-dlp mock)

yt-dlp는 Docker 컨테이너 안에서만 설치되므로, conftest.py에서 stub을 삽입한다.
"""

from unittest.mock import patch

import pytest
import yt_dlp
from src.pipeline.downloader import _is_cover, _is_live, _select_best_entry, download_track

# ── 성공 케이스 ───────────────────────────────────────────────────────────────


def test_download_track_returns_flac_path(tmp_path):
    """
    yt-dlp 다운로드 성공 시 (file_path, yt_metadata) 튜플을 반환한다.
    yt_dlp.YoutubeDL 자체를 mock해서 실제 네트워크 호출을 막는다.
    """
    mbid = "test-mbid-001"
    expected_file = tmp_path / f"{mbid}.flac"
    expected_file.touch()

    mock_info = {
        "entries": [{"thumbnail": "http://example.com/thumb.jpg", "channel": "TestChannel"}]
    }

    class MockYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=True):
            return mock_info

    with patch("src.pipeline.downloader.yt_dlp.YoutubeDL", MockYDL):
        file_path, yt_metadata = download_track(
            mbid=mbid,
            artist="Radiohead",
            track_name="Creep",
            staging_dir=str(tmp_path),
            prefer_flac=True,
        )

    assert file_path == str(expected_file)
    assert isinstance(yt_metadata, dict)
    assert yt_metadata["thumbnail_url"] == "http://example.com/thumb.jpg"
    assert yt_metadata["channel"] == "TestChannel"


def test_download_track_returns_opus_path(tmp_path):
    """prefer_flac=False 시 (opus_path, yt_metadata) 튜플을 반환한다."""
    mbid = "test-mbid-opus"
    expected_file = tmp_path / f"{mbid}.opus"
    expected_file.touch()

    mock_info = {"thumbnail": "http://example.com/thumb.jpg", "channel": "QueenChannel"}

    class MockYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=True):
            return mock_info

    with patch("src.pipeline.downloader.yt_dlp.YoutubeDL", MockYDL):
        file_path, yt_metadata = download_track(
            mbid=mbid,
            artist="Queen",
            track_name="Bohemian Rhapsody",
            staging_dir=str(tmp_path),
            prefer_flac=False,
        )

    assert file_path == str(expected_file)
    assert isinstance(yt_metadata, dict)


def test_download_track_flac_fallback_to_opus(tmp_path):
    """
    prefer_flac=True일 때 FLAC 다운로드가 실패하면 Opus로 폴백한다.
    첫 번째 YoutubeDL 호출은 DownloadError, 두 번째는 성공으로 mock.
    """
    mbid = "test-mbid-fallback"
    expected_file = tmp_path / f"{mbid}.opus"
    expected_file.touch()

    call_count = 0

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=True):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise yt_dlp.utils.DownloadError("flac not available")
            return {"thumbnail": "", "channel": "FallbackChannel"}

    with patch("src.pipeline.downloader.yt_dlp.YoutubeDL", MockYDL):
        file_path, yt_metadata = download_track(
            mbid=mbid,
            artist="Artist",
            track_name="Track",
            staging_dir=str(tmp_path),
            prefer_flac=True,
        )

    assert file_path == str(expected_file)
    assert call_count == 2  # FLAC 시도 + Opus 시도


# ── 실패 케이스 ───────────────────────────────────────────────────────────────


def test_download_track_returns_none_when_all_fail(tmp_path):
    """모든 다운로드 시도가 실패하면 (None, None)을 반환한다."""

    class AlwaysFailYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=True):
            raise yt_dlp.utils.DownloadError("network error")

    with patch("src.pipeline.downloader.yt_dlp.YoutubeDL", AlwaysFailYDL):
        result = download_track(
            mbid="fail-mbid",
            artist="Artist",
            track_name="Track",
            staging_dir=str(tmp_path),
            prefer_flac=True,
        )

    assert result == (None, None)


def test_download_track_returns_none_when_no_file_created(tmp_path):
    """
    yt-dlp가 예외 없이 종료했지만 파일이 없으면 (None, None)을 반환한다
    (다른 확장자로 저장되거나 download 자체가 0건인 경우).
    """

    class NoFileYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=True):
            return None  # 파일 없이 성공

    with patch("src.pipeline.downloader.yt_dlp.YoutubeDL", NoFileYDL):
        result = download_track(
            mbid="no-file-mbid",
            artist="Artist",
            track_name="Track",
            staging_dir=str(tmp_path),
            prefer_flac=True,
        )

    assert result == (None, None)


# ── staging_dir 생성 ──────────────────────────────────────────────────────────


def test_download_track_creates_staging_dir(tmp_path):
    """staging_dir이 없으면 자동 생성해야 한다."""
    staging = tmp_path / "nonexistent_staging"
    assert not staging.exists()

    class NoFileYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=True):
            return None

    with patch("src.pipeline.downloader.yt_dlp.YoutubeDL", NoFileYDL):
        download_track(
            mbid="dir-test",
            artist="Artist",
            track_name="Track",
            staging_dir=str(staging),
        )

    assert staging.exists()


# ── 검색 쿼리 형식 ────────────────────────────────────────────────────────────


def test_download_track_uses_ytsearch1_query(tmp_path):
    """yt-dlp에 전달되는 URL이 'ytsearch5:{artist} {track}' 형식으로 메타데이터 fetch를 시작한다.
    entries가 없을 때 최종 폴백으로 'ytsearch1:{artist} {track}'을 사용한다."""
    mbid = "query-test"
    (tmp_path / f"{mbid}.flac").touch()

    captured_urls = []

    class CapturingYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=True):
            captured_urls.append(url)
            return {"thumbnail": "", "channel": ""}

    with patch("src.pipeline.downloader.yt_dlp.YoutubeDL", CapturingYDL):
        download_track(
            mbid=mbid,
            artist="Radiohead",
            track_name="Creep",
            staging_dir=str(tmp_path),
            prefer_flac=True,
        )

    assert len(captured_urls) >= 1
    # 첫 번째 호출은 메타데이터 fetch용 ytsearch5 쿼리여야 한다
    assert captured_urls[0] == "ytsearch5:Radiohead Creep official audio"


# ── _is_live 단위 테스트 ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "title,expected",
    [
        # 감지되어야 하는 케이스
        ("Radiohead - Creep (Live in Japan)", True),
        ("Radiohead - Creep [Live]", True),
        ("Radiohead - Creep LIVE at Glastonbury", True),
        ("Coldplay - Yellow (Concert)", True),
        ("BTS World Tour 2022", True),
        ("Lollapalooza Festival Set", True),
        ("Nirvana Unplugged", True),
        ("Taylor Swift - Acoustic Version", True),
        # 단어 경계 — 일부인 경우 감지되지 않아야 하는 케이스
        ("Alive by Pearl Jam", False),
        ("Liveliness of Music", False),
        ("Oliver - Tourniquet", False),  # 'tour'는 포함되지 않음 (단어 경계)
        ("Radiohead - Creep (Official Audio)", False),
        ("Radiohead - Creep (Music Video)", False),
        ("", False),
    ],
)
def test_is_live(title, expected):
    assert _is_live(title) is expected


# ── _select_best_entry 단위 테스트 ────────────────────────────────────────────


def _entry(title: str, duration: float, channel: str = "") -> dict:
    return {"title": title, "duration": duration, "channel": channel}


def test_select_best_entry_prefers_studio_over_live_with_duration():
    """studio 후보가 있으면 duration이 더 가깝더라도 live는 선택되지 않는다."""
    entries = [
        _entry("Radiohead - Creep (Live in Japan)", 230.0),  # 매우 가깝지만 live
        _entry("Radiohead - Creep (Official Audio)", 238.0),  # studio
    ]
    mb_duration = 232.0
    result = _select_best_entry(entries, mb_duration)
    assert result["title"] == "Radiohead - Creep (Official Audio)"


def test_select_best_entry_penalizes_cover():
    """cover 영상은 패널티를 받아서 원곡이 선택된다."""
    entries = [
        _entry("Radiohead - Creep (Fan Cover)", 232.0),  # 정확한 duration이지만 cover
        _entry("Radiohead - Creep (Official Audio)", 238.0),  # studio
    ]
    mb_duration = 232.0
    result = _select_best_entry(entries, mb_duration)
    assert result["title"] == "Radiohead - Creep (Official Audio)"


def test_select_best_entry_allows_cover_when_user_wants_it():
    """track_name에 cover 키워드가 있으면 커버 패널티를 적용하지 않는다."""
    entries = [
        _entry("Radiohead - Creep (Fan Cover)", 232.0),
        _entry("Radiohead - Creep (Official Audio)", 238.0),
    ]
    mb_duration = 232.0
    # track_name에 "cover"가 있으므로 Fan Cover가 duration 기준으로 선택됨
    result = _select_best_entry(entries, mb_duration, track_name="Creep cover")
    assert result["title"] == "Radiohead - Creep (Fan Cover)"


def test_select_best_entry_prefers_official_channel():
    """공식 채널의 영상이 우선 선택된다."""
    entries = [
        _entry("Radiohead - Creep", 238.0, channel="RandomUploader"),
        _entry("Radiohead - Creep", 240.0, channel="Radiohead"),
    ]
    mb_duration = 232.0
    result = _select_best_entry(entries, mb_duration, artist="Radiohead")
    assert result["channel"] == "Radiohead"


def test_select_best_entry_prefers_vevo_channel():
    """VEVO 채널의 영상이 우선 선택된다."""
    entries = [
        _entry("Eminem - Without Me", 238.0, channel="RandomUser"),
        _entry("Eminem - Without Me", 240.0, channel="EminemVEVO"),
    ]
    mb_duration = 232.0
    result = _select_best_entry(entries, mb_duration, artist="Eminem")
    assert result["channel"] == "EminemVEVO"


def test_select_best_entry_falls_back_to_live_when_all_live():
    """모든 후보가 live이면 duration 기준으로 그 중 최선을 선택한다."""
    entries = [
        _entry("Radiohead - Creep Live at MSG", 290.0),
        _entry("Radiohead - Creep (Live in Japan)", 235.0),
        _entry("Radiohead - Creep Concert 2008", 310.0),
    ]
    mb_duration = 232.0
    result = _select_best_entry(entries, mb_duration)
    assert result["title"] == "Radiohead - Creep (Live in Japan)"


def test_select_best_entry_no_mb_duration_returns_first_non_live():
    """mb_duration이 None이면 첫 번째 non-live 후보를 반환한다."""
    entries = [
        _entry("Radiohead - Creep Live at Glastonbury", 280.0),
        _entry("Radiohead - Creep (Official Audio)", 238.0),
        _entry("Radiohead - Creep (Remaster)", 239.0),
    ]
    result = _select_best_entry(entries, mb_duration=None)
    assert result["title"] == "Radiohead - Creep (Official Audio)"


def test_select_best_entry_no_mb_duration_all_live_returns_first():
    """mb_duration이 None이고 모두 live이면 첫 번째 항목을 반환한다."""
    entries = [
        _entry("Radiohead - Creep Live 2001", 290.0),
        _entry("Radiohead - Creep (Live in Japan)", 235.0),
    ]
    result = _select_best_entry(entries, mb_duration=None)
    assert result["title"] == "Radiohead - Creep Live 2001"


def test_select_best_entry_single_live_entry_is_returned():
    """후보가 live 영상 하나뿐이면 그것을 선택한다."""
    entries = [_entry("Coldplay - Yellow Live at Glastonbury", 250.0)]
    result = _select_best_entry(entries, mb_duration=245.0)
    assert result["title"] == "Coldplay - Yellow Live at Glastonbury"


def test_select_best_entry_raises_on_empty():
    """entries가 빈 리스트이면 ValueError를 발생시킨다."""
    with pytest.raises(ValueError, match="entries list is empty"):
        _select_best_entry([], mb_duration=200.0)


def test_select_best_entry_duration_none_treated_as_zero():
    """entry의 duration이 None이면 0으로 처리한다."""
    entries = [
        _entry("Radiohead - Creep (Live)", None),
        _entry("Radiohead - Creep (Official)", None),
    ]
    result = _select_best_entry(entries, mb_duration=200.0)
    assert result["title"] == "Radiohead - Creep (Official)"


# ── strict 모드 단위 테스트 ───────────────────────────────────────────────────


def test_strict_mode_filters_live_entries():
    """strict=True(기본값): 클린 entry가 있으면 라이브 entry를 사전 필터링한다."""
    entries = [
        _entry("Radiohead - Creep Live at MSG", 232.0),
        _entry("Radiohead - Creep (Official Audio)", 238.0),
    ]
    result = _select_best_entry(entries, mb_duration=230.0, strict=True)
    assert result["title"] == "Radiohead - Creep (Official Audio)"


def test_strict_mode_falls_back_when_all_live():
    """strict=True: 모든 entry가 라이브이면 전체 후보에서 최선을 선택한다."""
    entries = [
        _entry("Radiohead - Creep Live at MSG", 290.0),
        _entry("Radiohead - Creep (Live in Japan)", 235.0),
        _entry("Radiohead - Creep Concert 2008", 310.0),
    ]
    result = _select_best_entry(entries, mb_duration=232.0, strict=True)
    # 전체 폴백 후 duration 기준 최선 선택
    assert result["title"] == "Radiohead - Creep (Live in Japan)"


def test_strict_mode_false_keeps_live_entries():
    """strict=False: 라이브 entry를 필터링하지 않고 점수 기준으로만 선택한다."""
    entries = [
        _entry("Radiohead - Creep (Official Audio)", 300.0),  # duration 멀지만 스튜디오
        _entry("Radiohead - Creep Live in Japan", 232.0),  # duration 가깝지만 라이브
    ]
    mb_duration = 232.0
    # strict=False이면 라이브 패널티(+500)와 duration 차이(68)가 합산되어
    # Official(duration diff 68, 패널티 없음) vs Live(duration diff 0, 패널티 +500)
    # → Official이 점수 낮으므로 선택됨. 단, strict 사전필터는 적용 안 됨을 확인.
    result = _select_best_entry(entries, mb_duration=mb_duration, strict=False)
    assert result["title"] == "Radiohead - Creep (Official Audio)"


def test_strict_mode_filters_cover_unless_requested():
    """strict=True: track_name에 cover 없으면 커버 entry 제외, 있으면 유지한다."""
    entries = [
        _entry("Radiohead - Creep (Fan Cover)", 232.0),
        _entry("Radiohead - Creep (Official Audio)", 238.0),
    ]
    # cover를 요청하지 않은 경우 — 커버 entry 필터링 → Official 선택
    result_no_cover = _select_best_entry(
        entries, mb_duration=232.0, track_name="Creep", strict=True
    )
    assert result_no_cover["title"] == "Radiohead - Creep (Official Audio)"

    # cover를 요청한 경우 — 커버 entry 유지 → duration 기준 Fan Cover 선택
    result_wants_cover = _select_best_entry(
        entries, mb_duration=232.0, track_name="Creep cover", strict=True
    )
    assert result_wants_cover["title"] == "Radiohead - Creep (Fan Cover)"


# ── _is_cover 단위 테스트 ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Eminem - Without Me (Cover)", True),
        ("Without Me - Piano Version", True),
        ("Eminem Without Me Karaoke", True),
        ("Without Me (Remix)", True),
        ("Without Me Instrumental", True),
        ("Without Me 8-bit", True),
        ("Eminem - Without Me (Official Audio)", False),
        ("Eminem - Without Me", False),
        ("", False),
    ],
)
def test_is_cover(title, expected):
    assert _is_cover(title) is expected


# ── _run_with_timeout 단위 테스트 ────────────────────────────────────────────

import time as _time

from src.pipeline.downloader import _run_with_timeout


def test_run_with_timeout_raises_on_timeout():
    """_run_with_timeout should raise DownloadError when function exceeds timeout."""

    def slow_fn():
        _time.sleep(10)

    with pytest.raises(yt_dlp.utils.DownloadError, match="timed out"):
        _run_with_timeout(slow_fn, timeout_sec=0.5)


def test_run_with_timeout_returns_result():
    """_run_with_timeout should return function result when within timeout."""
    result = _run_with_timeout(lambda: 42, timeout_sec=5)
    assert result == 42
