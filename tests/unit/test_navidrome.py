"""
tests/unit/test_navidrome.py
navidrome.py의 _auth_params, trigger_scan, wait_for_scan 테스트 (requests mock)
"""
import hashlib
from unittest.mock import MagicMock, patch, call

import pytest
import requests

from src.pipeline.navidrome import _auth_params, trigger_scan, wait_for_scan


# ── _auth_params: MD5+salt 토큰 생성 검증 ─────────────────────────────────────

def test_auth_params_contains_required_keys():
    """_auth_params 반환값에 필수 키가 모두 있는지 확인한다."""
    params = _auth_params("admin", "password")
    for key in ("u", "t", "s", "v", "c", "f"):
        assert key in params


def test_auth_params_username():
    params = _auth_params("testuser", "pass")
    assert params["u"] == "testuser"


def test_auth_params_format_is_json():
    params = _auth_params("admin", "pass")
    assert params["f"] == "json"


def test_auth_params_token_is_md5_of_password_plus_salt():
    """
    token = MD5(password + salt) 공식을 검증한다.
    salt는 매번 랜덤이므로, params에서 salt를 꺼내 직접 계산한 MD5와 비교한다.
    """
    params = _auth_params("admin", "mysecret")
    salt = params["s"]
    expected_token = hashlib.md5(("mysecret" + salt).encode()).hexdigest()
    assert params["t"] == expected_token


def test_auth_params_token_is_32char_hex():
    """MD5 hexdigest는 항상 32자 16진수 문자열이어야 한다."""
    params = _auth_params("admin", "pass")
    assert len(params["t"]) == 32
    assert all(c in "0123456789abcdef" for c in params["t"])


def test_auth_params_salt_changes_on_each_call():
    """salt는 호출마다 달라야 한다 (고정값 사용 금지)."""
    salts = {_auth_params("admin", "pass")["s"] for _ in range(20)}
    # 20번 중 적어도 2개 이상 다른 salt가 나와야 한다
    assert len(salts) > 1


# ── trigger_scan ──────────────────────────────────────────────────────────────

def _make_resp(json_data: dict, status_code: int = 200):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data
    mock_resp.raise_for_status = MagicMock()
    if status_code >= 400:
        mock_resp.raise_for_status.side_effect = requests.HTTPError()
    return mock_resp


def test_trigger_scan_returns_true_on_success():
    fake_resp = _make_resp({"subsonic-response": {"status": "ok"}})
    with patch("src.pipeline.navidrome.requests.get", return_value=fake_resp):
        result = trigger_scan("http://localhost:4533", "admin", "pass")
    assert result is True


def test_trigger_scan_returns_false_when_status_not_ok():
    fake_resp = _make_resp({"subsonic-response": {"status": "failed", "error": {"code": 10}}})
    with patch("src.pipeline.navidrome.requests.get", return_value=fake_resp):
        result = trigger_scan("http://localhost:4533", "admin", "pass")
    assert result is False


def test_trigger_scan_returns_false_on_request_exception():
    with patch(
        "src.pipeline.navidrome.requests.get",
        side_effect=requests.ConnectionError("unreachable"),
    ):
        result = trigger_scan("http://localhost:4533", "admin", "pass")
    assert result is False


def test_trigger_scan_url_contains_startScan():
    """요청 URL이 /rest/startScan 엔드포인트를 포함해야 한다."""
    fake_resp = _make_resp({"subsonic-response": {"status": "ok"}})
    with patch("src.pipeline.navidrome.requests.get", return_value=fake_resp) as mock_get:
        trigger_scan("http://localhost:4533", "admin", "pass")
    called_url = mock_get.call_args[0][0]
    assert "startScan" in called_url


def test_trigger_scan_strips_trailing_slash():
    """URL 끝의 슬래시를 올바르게 처리해야 한다."""
    fake_resp = _make_resp({"subsonic-response": {"status": "ok"}})
    with patch("src.pipeline.navidrome.requests.get", return_value=fake_resp) as mock_get:
        trigger_scan("http://localhost:4533/", "admin", "pass")
    called_url = mock_get.call_args[0][0]
    # 이중 슬래시(//) 없이 /rest/startScan
    assert "//rest" not in called_url


# ── wait_for_scan ─────────────────────────────────────────────────────────────

def test_wait_for_scan_returns_true_when_not_scanning():
    """스캔이 이미 완료된 상태(scanning=False)면 즉시 True를 반환한다."""
    fake_resp = _make_resp({
        "subsonic-response": {
            "scanStatus": {"scanning": False, "count": 42}
        }
    })
    with patch("src.pipeline.navidrome.requests.get", return_value=fake_resp):
        with patch("src.pipeline.navidrome.time.sleep"):  # sleep 스킵
            result = wait_for_scan("http://localhost:4533", "admin", "pass", timeout=30)
    assert result is True


def test_wait_for_scan_polls_until_done():
    """
    첫 번째 poll에서는 scanning=True, 두 번째 poll에서는 False를 반환하는
    시나리오에서 True를 반환한다.
    """
    scanning_resp = _make_resp({
        "subsonic-response": {"scanStatus": {"scanning": True, "count": 0}}
    })
    done_resp = _make_resp({
        "subsonic-response": {"scanStatus": {"scanning": False, "count": 10}}
    })

    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return scanning_resp if call_count == 1 else done_resp

    with patch("src.pipeline.navidrome.requests.get", side_effect=side_effect):
        with patch("src.pipeline.navidrome.time.sleep"):
            result = wait_for_scan("http://localhost:4533", "admin", "pass", timeout=30)

    assert result is True
    assert call_count == 2


def test_wait_for_scan_returns_false_on_timeout(monkeypatch):
    """timeout 내에 스캔이 완료되지 않으면 False를 반환한다."""
    import time

    scanning_resp = _make_resp({
        "subsonic-response": {"scanStatus": {"scanning": True, "count": 0}}
    })

    # time.time()을 조작해서 즉시 deadline을 초과시킨다
    original_time = time.time
    call_count_time = [0]

    def fake_time():
        call_count_time[0] += 1
        # 첫 호출은 실제 시간, 이후는 deadline을 훨씬 초과한 값 반환
        if call_count_time[0] <= 1:
            return original_time()
        return original_time() + 10000

    with patch("src.pipeline.navidrome.requests.get", return_value=scanning_resp):
        with patch("src.pipeline.navidrome.time.sleep"):
            with patch("src.pipeline.navidrome.time.time", fake_time):
                result = wait_for_scan("http://localhost:4533", "admin", "pass", timeout=1)

    assert result is False


def test_wait_for_scan_continues_on_request_exception():
    """
    poll 도중 RequestException이 발생해도 계속 폴링하고
    이후 성공하면 True를 반환한다.
    """
    done_resp = _make_resp({
        "subsonic-response": {"scanStatus": {"scanning": False, "count": 5}}
    })

    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise requests.ConnectionError("temporary error")
        return done_resp

    with patch("src.pipeline.navidrome.requests.get", side_effect=side_effect):
        with patch("src.pipeline.navidrome.time.sleep"):
            result = wait_for_scan("http://localhost:4533", "admin", "pass", timeout=30)

    assert result is True
    assert call_count == 2
