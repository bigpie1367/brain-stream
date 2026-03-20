"""sanitize_path_component unit tests."""

from src.utils.fs import sanitize_path_component


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
