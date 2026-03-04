"""
tests/unit/test_config.py
config.py의 load_config 함수 단위 테스트
"""
import os
import textwrap

import pytest

from src.config import load_config, AppConfig


def _write_config(tmp_path, content: str) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(content))
    return str(p)


# ── 기본 YAML 로딩 ─────────────────────────────────────────────────────────────

def test_load_config_basic(tmp_path, monkeypatch):
    """YAML 파일의 값이 AppConfig에 올바르게 매핑되는지 검증한다."""
    # 환경변수 오버라이드가 없도록 제거
    monkeypatch.delenv("LB_USERNAME", raising=False)
    monkeypatch.delenv("LB_TOKEN", raising=False)
    monkeypatch.delenv("NAVIDROME_USER", raising=False)
    monkeypatch.delenv("NAVIDROME_PASSWORD", raising=False)

    path = _write_config(tmp_path, """\
        listenbrainz:
          username: "myuser"
          token: "mytoken"
          recommendation_count: 50

        download:
          staging_dir: /tmp/staging
          prefer_flac: false

        beets:
          music_dir: /tmp/music

        navidrome:
          url: "http://nd:4533"
          username: "nd_user"
          password: "nd_pass"

        scheduler:
          interval_hours: 12

        state_db: /tmp/state.db
        log_level: "DEBUG"
    """)
    cfg = load_config(path)

    assert isinstance(cfg, AppConfig)
    assert cfg.listenbrainz.username == "myuser"
    assert cfg.listenbrainz.token == "mytoken"
    assert cfg.listenbrainz.recommendation_count == 50
    assert cfg.download.staging_dir == "/tmp/staging"
    assert cfg.download.prefer_flac is False
    assert cfg.beets.music_dir == "/tmp/music"
    assert cfg.navidrome.url == "http://nd:4533"
    assert cfg.navidrome.username == "nd_user"
    assert cfg.navidrome.password == "nd_pass"
    assert cfg.scheduler.interval_hours == 12
    assert cfg.state_db == "/tmp/state.db"
    assert cfg.log_level == "DEBUG"


# ── 환경변수 오버라이드 ────────────────────────────────────────────────────────

def test_lb_username_overridden_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LB_USERNAME", "env_user")
    monkeypatch.delenv("LB_TOKEN", raising=False)

    path = _write_config(tmp_path, """\
        listenbrainz:
          username: "yaml_user"
          token: "yaml_token"
    """)
    cfg = load_config(path)
    assert cfg.listenbrainz.username == "env_user"
    # token은 환경변수 없으므로 YAML 값 유지
    assert cfg.listenbrainz.token == "yaml_token"


def test_lb_token_overridden_by_env(tmp_path, monkeypatch):
    monkeypatch.delenv("LB_USERNAME", raising=False)
    monkeypatch.setenv("LB_TOKEN", "env_token")

    path = _write_config(tmp_path, """\
        listenbrainz:
          username: "yaml_user"
          token: "yaml_token"
    """)
    cfg = load_config(path)
    assert cfg.listenbrainz.token == "env_token"


def test_navidrome_user_overridden_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIDROME_USER", "env_admin")
    monkeypatch.delenv("NAVIDROME_PASSWORD", raising=False)

    path = _write_config(tmp_path, """\
        listenbrainz:
          username: "u"
          token: "t"
        navidrome:
          url: "http://localhost:4533"
          username: "yaml_admin"
          password: "yaml_pass"
    """)
    cfg = load_config(path)
    assert cfg.navidrome.username == "env_admin"
    assert cfg.navidrome.password == "yaml_pass"


def test_navidrome_password_overridden_by_env(tmp_path, monkeypatch):
    monkeypatch.delenv("NAVIDROME_USER", raising=False)
    monkeypatch.setenv("NAVIDROME_PASSWORD", "env_secret")

    path = _write_config(tmp_path, """\
        listenbrainz:
          username: "u"
          token: "t"
        navidrome:
          url: "http://localhost:4533"
          username: "admin"
          password: "yaml_pass"
    """)
    cfg = load_config(path)
    assert cfg.navidrome.password == "env_secret"


# ── 기본값 (YAML 섹션 생략 시) ────────────────────────────────────────────────

def test_defaults_when_sections_omitted(tmp_path, monkeypatch):
    """download/beets/navidrome/scheduler 섹션이 없으면 dataclass 기본값을 사용한다."""
    monkeypatch.delenv("LB_USERNAME", raising=False)
    monkeypatch.delenv("LB_TOKEN", raising=False)
    monkeypatch.delenv("NAVIDROME_USER", raising=False)
    monkeypatch.delenv("NAVIDROME_PASSWORD", raising=False)

    path = _write_config(tmp_path, """\
        listenbrainz:
          username: "u"
          token: "t"
    """)
    cfg = load_config(path)
    assert cfg.download.staging_dir == "/app/data/staging"
    assert cfg.download.prefer_flac is True
    assert cfg.beets.music_dir == "/app/data/music"
    assert cfg.navidrome.url == "http://navidrome:4533"
    assert cfg.navidrome.username == "admin"
    assert cfg.scheduler.interval_hours == 6
    assert cfg.state_db == "/app/db/state.db"
