"""
tests/unit/test_config.py
config.py의 load_config 함수 단위 테스트
"""
import pytest

from src.config import load_config, AppConfig


# ── 기본값 (환경변수 없음) ──────────────────────────────────────────────────────

def test_defaults_when_no_env_vars(monkeypatch):
    """환경변수가 없으면 dataclass 기본값을 사용한다."""
    monkeypatch.delenv("LB_USERNAME", raising=False)
    monkeypatch.delenv("LB_TOKEN", raising=False)
    monkeypatch.delenv("NAVIDROME_URL", raising=False)
    monkeypatch.delenv("NAVIDROME_USER", raising=False)
    monkeypatch.delenv("NAVIDROME_PASSWORD", raising=False)

    cfg = load_config()

    assert isinstance(cfg, AppConfig)
    assert cfg.listenbrainz.username == ""
    assert cfg.listenbrainz.token == ""
    assert cfg.listenbrainz.recommendation_count == 25
    assert cfg.download.staging_dir == "/app/data/staging"
    assert cfg.download.prefer_flac is True
    assert cfg.beets.music_dir == "/app/data/music"
    assert cfg.navidrome.url == "http://navidrome:4533"
    assert cfg.navidrome.username == "admin"
    assert cfg.navidrome.password == ""
    assert cfg.scheduler.interval_hours == 6
    assert cfg.state_db == "/app/db/state.db"
    assert cfg.log_level == "INFO"
    assert cfg.log_file == "/app/data/logs/music-bot.log"


# ── 환경변수 로딩 ──────────────────────────────────────────────────────────────

def test_lb_username_from_env(monkeypatch):
    monkeypatch.setenv("LB_USERNAME", "env_user")
    monkeypatch.delenv("LB_TOKEN", raising=False)

    cfg = load_config()
    assert cfg.listenbrainz.username == "env_user"
    assert cfg.listenbrainz.token == ""


def test_lb_token_from_env(monkeypatch):
    monkeypatch.delenv("LB_USERNAME", raising=False)
    monkeypatch.setenv("LB_TOKEN", "env_token")

    cfg = load_config()
    assert cfg.listenbrainz.token == "env_token"
    assert cfg.listenbrainz.username == ""


def test_navidrome_url_from_env(monkeypatch):
    monkeypatch.setenv("NAVIDROME_URL", "http://custom-nd:4533")

    cfg = load_config()
    assert cfg.navidrome.url == "http://custom-nd:4533"


def test_navidrome_user_from_env(monkeypatch):
    monkeypatch.setenv("NAVIDROME_USER", "env_admin")
    monkeypatch.delenv("NAVIDROME_PASSWORD", raising=False)

    cfg = load_config()
    assert cfg.navidrome.username == "env_admin"
    assert cfg.navidrome.password == ""


def test_navidrome_password_from_env(monkeypatch):
    monkeypatch.delenv("NAVIDROME_USER", raising=False)
    monkeypatch.setenv("NAVIDROME_PASSWORD", "env_secret")

    cfg = load_config()
    assert cfg.navidrome.password == "env_secret"


def test_all_env_vars_loaded(monkeypatch):
    """모든 환경변수가 동시에 설정된 경우 올바르게 매핑되는지 검증한다."""
    monkeypatch.setenv("LB_USERNAME", "lb_user")
    monkeypatch.setenv("LB_TOKEN", "lb_token")
    monkeypatch.setenv("NAVIDROME_URL", "http://nd:4533")
    monkeypatch.setenv("NAVIDROME_USER", "nd_user")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "nd_pass")

    cfg = load_config()
    assert cfg.listenbrainz.username == "lb_user"
    assert cfg.listenbrainz.token == "lb_token"
    assert cfg.navidrome.url == "http://nd:4533"
    assert cfg.navidrome.username == "nd_user"
    assert cfg.navidrome.password == "nd_pass"
