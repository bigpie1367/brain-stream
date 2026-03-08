"""
tests/unit/test_listenbrainz.py
listenbrainz.py의 fetch_recommendations 함수 단위 테스트 (HTTP mock)
"""
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.pipeline.listenbrainz import fetch_recommendations


def _make_mock_response(json_data: dict, status_code: int = 200, content: bytes = b"x"):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.content = content
    mock_resp.json.return_value = json_data
    mock_resp.raise_for_status = MagicMock()
    if status_code >= 400:
        mock_resp.raise_for_status.side_effect = requests.HTTPError(
            response=mock_resp
        )
    return mock_resp


# ── 정상 응답 파싱 ────────────────────────────────────────────────────────────

def test_fetch_recommendations_parses_recordings(monkeypatch):
    """
    LB API의 정상 응답을 파싱해 mbid/track_name/artist 딕셔너리 리스트를 반환한다.
    LB 응답에는 recording_mbid만 포함되며, _lookup_recording()으로 MB API에서
    artist/track_name을 조회한다.
    """
    fake_response = _make_mock_response({
        "payload": {
            "mbids": [
                {"recording_mbid": "aaaa-0001"},
                {"recording_mbid": "bbbb-0002"},
            ]
        }
    })

    def fake_lookup(mbid):
        if mbid == "aaaa-0001":
            return {"artist": "Radiohead", "track_name": "Creep"}
        if mbid == "bbbb-0002":
            return {"artist": "Radiohead", "track_name": "Karma Police"}
        return {"artist": "", "track_name": ""}

    with patch("src.pipeline.listenbrainz.requests.get", return_value=fake_response), \
         patch("src.pipeline.listenbrainz._lookup_recording", side_effect=fake_lookup):
        results = fetch_recommendations("testuser", "testtoken", count=25)

    assert len(results) == 2
    assert results[0]["mbid"] == "aaaa-0001"
    assert results[0]["track_name"] == "Creep"
    assert results[0]["artist"] == "Radiohead"
    assert results[1]["mbid"] == "bbbb-0002"
    assert results[1]["track_name"] == "Karma Police"


def test_fetch_recommendations_skips_entries_without_mbid(monkeypatch):
    """recording_mbid가 없는 항목은 결과에서 제외된다."""
    fake_response = _make_mock_response({
        "payload": {
            "mbids": [
                {"recording_mbid": "aaaa-0001"},
                {
                    # mbid 없음
                },
            ]
        }
    })

    def fake_lookup(mbid):
        if mbid == "aaaa-0001":
            return {"artist": "Artist", "track_name": "Valid Track"}
        return {"artist": "", "track_name": ""}

    with patch("src.pipeline.listenbrainz.requests.get", return_value=fake_response), \
         patch("src.pipeline.listenbrainz._lookup_recording", side_effect=fake_lookup):
        results = fetch_recommendations("testuser", "testtoken")

    assert len(results) == 1
    assert results[0]["mbid"] == "aaaa-0001"


# ── 빈 응답 처리 ──────────────────────────────────────────────────────────────

def test_fetch_recommendations_returns_empty_list_when_content_empty(monkeypatch):
    """응답 body가 비어 있으면 빈 리스트를 반환한다."""
    fake_response = _make_mock_response({}, content=b"")

    with patch("src.pipeline.listenbrainz.requests.get", return_value=fake_response):
        results = fetch_recommendations("testuser", "testtoken")

    assert results == []


def test_fetch_recommendations_returns_empty_list_when_no_payload(monkeypatch):
    """payload 키가 없으면 빈 리스트를 반환한다."""
    fake_response = _make_mock_response({"other": "data"})

    with patch("src.pipeline.listenbrainz.requests.get", return_value=fake_response):
        results = fetch_recommendations("testuser", "testtoken")

    assert results == []


def test_fetch_recommendations_returns_empty_list_when_mbids_empty(monkeypatch):
    """mbids 배열이 비어 있으면 빈 리스트를 반환한다."""
    fake_response = _make_mock_response({
        "payload": {
            "mbids": []
        }
    })

    with patch("src.pipeline.listenbrainz.requests.get", return_value=fake_response):
        results = fetch_recommendations("testuser", "testtoken")

    assert results == []


# ── HTTP 오류 전파 ────────────────────────────────────────────────────────────

def test_fetch_recommendations_raises_on_http_error(monkeypatch):
    """HTTP 4xx/5xx 응답 시 예외가 전파된다."""
    fake_response = _make_mock_response({}, status_code=401)

    with patch("src.pipeline.listenbrainz.requests.get", return_value=fake_response):
        with pytest.raises(requests.HTTPError):
            fetch_recommendations("baduser", "badtoken")


def test_fetch_recommendations_raises_on_connection_error(monkeypatch):
    """네트워크 오류(ConnectionError) 시 예외가 전파된다."""
    with patch(
        "src.pipeline.listenbrainz.requests.get",
        side_effect=requests.ConnectionError("network down"),
    ):
        with pytest.raises(requests.ConnectionError):
            fetch_recommendations("testuser", "testtoken")


# ── API 호출 파라미터 검증 ─────────────────────────────────────────────────────

def test_fetch_recommendations_passes_correct_headers_and_params(monkeypatch):
    """올바른 Authorization 헤더와 count 파라미터를 전달하는지 확인한다."""
    fake_response = _make_mock_response({"payload": {"mbids": []}})

    with patch("src.pipeline.listenbrainz.requests.get", return_value=fake_response) as mock_get:
        fetch_recommendations("myuser", "mytoken", count=10)

    mock_get.assert_called_once()
    _, kwargs = mock_get.call_args
    assert kwargs["headers"] == {"Authorization": "Token mytoken"}
    assert kwargs["params"] == {"count": 10}


def test_fetch_recommendations_url_contains_username(monkeypatch):
    """요청 URL에 username이 포함되어야 한다."""
    fake_response = _make_mock_response({"payload": {"mbids": []}})

    with patch("src.pipeline.listenbrainz.requests.get", return_value=fake_response) as mock_get:
        fetch_recommendations("specificuser", "token123")

    called_url = mock_get.call_args[0][0]
    assert "specificuser" in called_url
