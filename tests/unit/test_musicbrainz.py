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
        "artist-credit": [{"artist": {"name": "Radiohead"}, "joinphrase": ""}],
    }
    mock_resp.raise_for_status = MagicMock()

    with patch("src.pipeline.musicbrainz.requests.get", return_value=mock_resp):
        with patch("src.pipeline.musicbrainz.time.sleep"):
            result = lookup_recording("some-mbid")

    assert result["artist"] == "Radiohead"
    assert result["title"] == "Creep"


def test_lookup_recording_failure():
    with patch(
        "src.pipeline.musicbrainz.requests.get", side_effect=Exception("network")
    ):
        with patch("src.pipeline.musicbrainz.time.sleep"):
            result = lookup_recording("bad-mbid")

    assert result["artist"] == ""
    assert result["title"] == ""
