"""
tests/unit/test_tagger.py
tagger.py 단위 테스트
- _pretag: 실제 FLAC 더미 파일에 mutagen으로 태그 쓰기 검증
- beet import 결과 파싱 로직: skip → False, duplicate-skip → True
- 외부 프로세스(_beet subprocess, requests)는 mock 처리
"""
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import mutagen.flac

from src.pipeline.tagger import (
    _pretag,
    _import_log_size,
    _import_log_tail,
    tag_and_import,
)


# ── FLAC 더미 파일 생성 헬퍼 ─────────────────────────────────────────────────

def _make_minimal_flac(path: Path):
    """
    mutagen이 읽을 수 있는 최소 FLAC 파일을 생성한다.
    STREAMINFO 블록을 올바른 값으로 작성한다.

    STREAMINFO 레이아웃 (34 bytes):
      2B: min_blocksize
      2B: max_blocksize
      3B: min_framesize
      3B: max_framesize
      -- 8 bytes packed --
      20 bits: sample_rate (44100 = 0xAC44)
       3 bits: channels - 1  (2ch -> 1)
       5 bits: bits_per_sample - 1 (16bit -> 15)
      36 bits: total_samples (0)
      16B: MD5 signature (zeros OK)
    """
    min_blocksize = 4096
    max_blocksize = 4096
    min_framesize = 0
    max_framesize = 0
    sample_rate = 44100   # 0xAC44, 20 bits
    channels = 2          # stored as channels-1 = 1, 3 bits
    bits_per_sample = 16  # stored as bps-1 = 15, 5 bits
    total_samples = 0     # 36 bits

    # Pack the 8-byte combined field:
    #   [20:sample_rate | 3:(channels-1) | 5:(bps-1) | 36:total_samples]
    # = 64 bits total
    combined = 0
    combined |= (sample_rate & 0xFFFFF) << 44
    combined |= ((channels - 1) & 0x7) << 41
    combined |= ((bits_per_sample - 1) & 0x1F) << 36
    combined |= (total_samples & 0xFFFFFFFFF)

    import struct as _struct
    streaminfo = (
        _struct.pack(">HH", min_blocksize, max_blocksize)
        + _struct.pack(">I", min_framesize)[1:]   # 3 bytes
        + _struct.pack(">I", max_framesize)[1:]   # 3 bytes
        + _struct.pack(">Q", combined)             # 8 bytes
        + b"\x00" * 16                             # MD5
    )
    assert len(streaminfo) == 34

    with open(path, "wb") as fp:
        fp.write(b"fLaC")
        # last-metadata-block(1) | type=STREAMINFO(0) | length=34
        fp.write(bytes([0x80, 0x00, 0x00, 0x22]))
        fp.write(streaminfo)


def _make_flac(tmp_path: Path, name: str = "test.flac") -> Path:
    p = tmp_path / name
    _make_minimal_flac(p)
    return p


# ── _pretag 테스트 ────────────────────────────────────────────────────────────

def test_pretag_writes_artist_and_title_to_flac(tmp_path):
    """_pretag가 FLAC 파일에 artist/title 태그를 올바르게 기록하는지 검증한다."""
    flac_path = _make_flac(tmp_path)

    _pretag(flac_path, artist="Radiohead", track_name="Creep")

    f = mutagen.flac.FLAC(flac_path)
    assert f.get("artist") == ["Radiohead"]
    assert f.get("title") == ["Creep"]


def test_pretag_overwrites_existing_tags(tmp_path):
    """이미 태그가 있는 FLAC 파일에 _pretag를 호출하면 태그가 덮어씌워진다."""
    flac_path = _make_flac(tmp_path)

    # 먼저 기존 태그 기록
    f = mutagen.flac.FLAC(flac_path)
    f["artist"] = "Old Artist"
    f["title"] = "Old Title"
    f.save()

    _pretag(flac_path, artist="New Artist", track_name="New Title")

    f2 = mutagen.flac.FLAC(flac_path)
    assert f2.get("artist") == ["New Artist"]
    assert f2.get("title") == ["New Title"]


def test_pretag_nonexistent_file_does_not_raise(tmp_path):
    """존재하지 않는 파일에 대해 _pretag는 예외를 발생시키지 않는다 (경고 로그만)."""
    bad_path = tmp_path / "nonexistent.flac"
    # 예외 없이 조용히 실패해야 한다
    _pretag(bad_path, artist="Artist", track_name="Track")


# ── _import_log_size / _import_log_tail 테스트 ───────────────────────────────

def test_import_log_size_returns_zero_when_no_file(monkeypatch):
    """import log 파일이 없으면 0을 반환한다."""
    monkeypatch.setattr("src.pipeline.tagger._IMPORT_LOG", "/nonexistent/path/beets.log")
    assert _import_log_size() == 0


def test_import_log_size_returns_file_size(tmp_path, monkeypatch):
    log_file = tmp_path / "beets-import.log"
    log_file.write_text("hello world")
    monkeypatch.setattr("src.pipeline.tagger._IMPORT_LOG", str(log_file))
    assert _import_log_size() == len("hello world")


def test_import_log_tail_returns_content_after_offset(tmp_path, monkeypatch):
    log_file = tmp_path / "beets-import.log"
    log_file.write_text("BEFORE\nAFTER\n")
    offset = len("BEFORE\n")
    monkeypatch.setattr("src.pipeline.tagger._IMPORT_LOG", str(log_file))
    tail = _import_log_tail(offset)
    assert tail == "AFTER\n"


def test_import_log_tail_returns_empty_when_no_file(monkeypatch):
    monkeypatch.setattr("src.pipeline.tagger._IMPORT_LOG", "/nonexistent/beets.log")
    assert _import_log_tail(0) == ""


# ── tag_and_import: skip 감지 ─────────────────────────────────────────────────

def test_tag_and_import_returns_false_when_beets_skips(tmp_path, monkeypatch):
    """
    beet import 후 import log에 'skip' 키워드가 있으면 False를 반환한다.
    """
    flac_path = _make_flac(tmp_path)
    log_file = tmp_path / "beets-import.log"
    log_file.write_text("")

    monkeypatch.setattr("src.pipeline.tagger._IMPORT_LOG", str(log_file))

    def fake_beet(*args, timeout=60):
        # import 호출 시 log에 'skip' 기록
        if args and args[0] == "import":
            log_file.write_text("Skipping.\n")
        return True, ""

    monkeypatch.setattr("src.pipeline.tagger._beet", fake_beet)

    result = tag_and_import(
        str(flac_path),
        music_dir=str(tmp_path / "music"),
        artist="Artist",
        track_name="Track",
    )
    assert result is False


def test_tag_and_import_returns_true_when_duplicate_skip(tmp_path, monkeypatch):
    """
    beet import 후 import log에 'duplicate-skip' 키워드가 있으면
    이미 라이브러리에 있는 것으로 간주해 True를 반환한다.
    """
    flac_path = _make_flac(tmp_path)
    log_file = tmp_path / "beets-import.log"
    log_file.write_text("")

    monkeypatch.setattr("src.pipeline.tagger._IMPORT_LOG", str(log_file))

    def fake_beet(*args, timeout=60):
        if args and args[0] == "import":
            log_file.write_text("Duplicate-skip.\n")
        # list 호출에는 빈 결과 반환 (enrich 단계)
        return True, ""

    monkeypatch.setattr("src.pipeline.tagger._beet", fake_beet)
    # _enrich_track 내부의 _find_imported_file과 mediafile 호출도 mock
    monkeypatch.setattr("src.pipeline.tagger._enrich_track", lambda *args, **kwargs: None)

    result = tag_and_import(
        str(flac_path),
        music_dir=str(tmp_path / "music"),
        artist="Artist",
        track_name="Track",
    )
    assert result is True


def test_tag_and_import_returns_true_on_clean_import(tmp_path, monkeypatch):
    """
    beet import가 정상적으로 완료되고 log에 skip 관련 키워드가 없으면 True를 반환한다.
    """
    flac_path = _make_flac(tmp_path)
    log_file = tmp_path / "beets-import.log"
    log_file.write_text("")

    monkeypatch.setattr("src.pipeline.tagger._IMPORT_LOG", str(log_file))

    def fake_beet(*args, timeout=60):
        # import 성공, log에 아무것도 추가 안 함
        return True, ""

    monkeypatch.setattr("src.pipeline.tagger._beet", fake_beet)
    monkeypatch.setattr("src.pipeline.tagger._enrich_track", lambda *args, **kwargs: None)

    result = tag_and_import(
        str(flac_path),
        music_dir=str(tmp_path / "music"),
        artist="Artist",
        track_name="Track",
    )
    assert result is True


def test_tag_and_import_returns_false_when_file_not_found(tmp_path, monkeypatch):
    """staging 파일이 없으면 즉시 False를 반환한다."""
    missing = tmp_path / "missing.flac"
    result = tag_and_import(
        str(missing),
        music_dir=str(tmp_path / "music"),
    )
    assert result is False


def test_tag_and_import_returns_false_when_beet_fails(tmp_path, monkeypatch):
    """beet import 명령이 exit code != 0으로 실패하면 False를 반환한다."""
    flac_path = _make_flac(tmp_path)
    log_file = tmp_path / "beets-import.log"
    log_file.write_text("")

    monkeypatch.setattr("src.pipeline.tagger._IMPORT_LOG", str(log_file))

    def fake_beet(*args, timeout=60):
        return False, "error output"

    monkeypatch.setattr("src.pipeline.tagger._beet", fake_beet)

    result = tag_and_import(
        str(flac_path),
        music_dir=str(tmp_path / "music"),
        artist="Artist",
        track_name="Track",
    )
    assert result is False


def test_tag_and_import_returns_false_on_timeout(tmp_path, monkeypatch):
    """beet import가 TimeoutExpired를 발생시키면 False를 반환한다."""
    import subprocess
    flac_path = _make_flac(tmp_path)
    log_file = tmp_path / "beets-import.log"
    log_file.write_text("")

    monkeypatch.setattr("src.pipeline.tagger._IMPORT_LOG", str(log_file))

    def fake_beet(*args, timeout=60):
        raise subprocess.TimeoutExpired(cmd="beet", timeout=timeout)

    monkeypatch.setattr("src.pipeline.tagger._beet", fake_beet)

    result = tag_and_import(
        str(flac_path),
        music_dir=str(tmp_path / "music"),
        artist="Artist",
        track_name="Track",
    )
    assert result is False


def test_tag_and_import_returns_false_when_beet_not_found(tmp_path, monkeypatch):
    """beet 명령이 없을 때(FileNotFoundError) False를 반환한다."""
    flac_path = _make_flac(tmp_path)
    log_file = tmp_path / "beets-import.log"
    log_file.write_text("")

    monkeypatch.setattr("src.pipeline.tagger._IMPORT_LOG", str(log_file))

    def fake_beet(*args, timeout=60):
        raise FileNotFoundError("beet not found")

    monkeypatch.setattr("src.pipeline.tagger._beet", fake_beet)

    result = tag_and_import(
        str(flac_path),
        music_dir=str(tmp_path / "music"),
        artist="Artist",
        track_name="Track",
    )
    assert result is False
