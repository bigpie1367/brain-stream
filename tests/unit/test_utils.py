"""sanitize_path_component, resolve_dir, and move_to_music_dir unit tests."""

import os

from src.utils.fs import move_to_music_dir, resolve_dir, sanitize_path_component


def test_sanitize_removes_unsafe_chars():
    assert sanitize_path_component('foo/bar:baz*"qux') == "foo_bar_baz__qux"


def test_sanitize_strips_dots_and_spaces():
    assert sanitize_path_component("  .hidden.  ") == "hidden"


def test_sanitize_empty_returns_default():
    assert sanitize_path_component("") == "_"
    assert sanitize_path_component("...", default="Unknown") == "Unknown"


def test_sanitize_truncates_to_max_len():
    long_name = "a" * 300
    assert len(sanitize_path_component(long_name)) == 255
    assert len(sanitize_path_component(long_name, max_len=50)) == 50


def test_resolve_dir_finds_existing_case_insensitive(tmp_path):
    (tmp_path / "Radiohead").mkdir()
    assert resolve_dir(str(tmp_path), "radiohead") == "Radiohead"
    assert resolve_dir(str(tmp_path), "RADIOHEAD") == "Radiohead"


def test_resolve_dir_returns_sanitized_when_no_match(tmp_path):
    result = resolve_dir(str(tmp_path), "NewArtist")
    assert result == "NewArtist"


def test_move_to_music_dir_creates_dirs_and_moves(tmp_path):
    music_dir = str(tmp_path / "music")
    os.makedirs(music_dir)
    src_file = tmp_path / "staging" / "test.flac"
    src_file.parent.mkdir()
    src_file.write_bytes(b"fake audio")
    result = move_to_music_dir(
        str(src_file), music_dir, "Radiohead", "OK Computer", "Creep.flac"
    )
    assert os.path.exists(result)
    assert "Radiohead" in result
    assert "OK Computer" in result
    assert not src_file.exists()


def test_move_to_music_dir_reuses_existing_artist_dir(tmp_path):
    music_dir = str(tmp_path / "music")
    (tmp_path / "music" / "Radiohead").mkdir(parents=True)
    src_file = tmp_path / "test.flac"
    src_file.write_bytes(b"fake")
    result = move_to_music_dir(
        str(src_file), music_dir, "radiohead", "OK Computer", "Creep.flac"
    )
    assert "Radiohead" in result
    assert "radiohead" not in result
