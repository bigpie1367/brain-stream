"""
tests/unit/test_downloader.py
downloader.py의 download_track 함수 단위 테스트 (yt-dlp mock)

yt-dlp는 Docker 컨테이너 안에서만 설치되므로, conftest.py에서 stub을 삽입한다.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yt_dlp

from src.pipeline.downloader import download_track


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
    """yt-dlp에 전달되는 URL이 'ytsearch1:{artist} {track}' 형식인지 확인한다."""
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
    assert captured_urls[0] == "ytsearch1:Radiohead Creep"
