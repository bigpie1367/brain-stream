"""
tests/unit/test_tagger.py
tagger.py 단위 테스트 (beets 제거 후 MB 직접 매칭 구현 기준)

- _sanitize_filename: 파일시스템 특수문자 제거 검증
- _write_tags / _read_tags: 실제 FLAC 더미 파일에 mutagen 태그 쓰기/읽기 검증
- _pretag: 하위 호환 wrapper 검증
- tag_and_import: MB 검색 실패 → False, 성공 → True + 파일 복사
- _mb_search_recording fallback: artist 유사도 기반 선택
- _mb_album_from_recording_id: 날짜 기준 오름차순 정렬
- _itunes_search / _deezer_search: artist 검증 (유사도 0.4 미만 skip)
- 외부 네트워크 호출(requests)은 mock 처리
"""

from pathlib import Path
from unittest.mock import MagicMock

import mutagen.flac
from src.pipeline.tagger import (
    _collect_recording_candidates,
    _deezer_search,
    _enrich_track,
    _is_live_title,
    _itunes_search,
    _mb_album_from_recording_id,
    _mb_search_recording,
    _pick_best_recording,
    _pretag,
    _read_tags,
    _sanitize_filename,
    _write_tags,
    tag_and_import,
)

# ── FLAC 더미 파일 생성 헬퍼 ─────────────────────────────────────────────────


def _make_minimal_flac(path: Path):
    """
    mutagen이 읽을 수 있는 최소 FLAC 파일을 생성한다.
    STREAMINFO 블록을 올바른 값으로 작성한다.
    """
    min_blocksize = 4096
    max_blocksize = 4096
    min_framesize = 0
    max_framesize = 0
    sample_rate = 44100
    channels = 2
    bits_per_sample = 16
    total_samples = 0

    combined = 0
    combined |= (sample_rate & 0xFFFFF) << 44
    combined |= ((channels - 1) & 0x7) << 41
    combined |= ((bits_per_sample - 1) & 0x1F) << 36
    combined |= total_samples & 0xFFFFFFFFF

    import struct as _struct

    streaminfo = (
        _struct.pack(">HH", min_blocksize, max_blocksize)
        + _struct.pack(">I", min_framesize)[1:]
        + _struct.pack(">I", max_framesize)[1:]
        + _struct.pack(">Q", combined)
        + b"\x00" * 16
    )
    assert len(streaminfo) == 34

    with open(path, "wb") as fp:
        fp.write(b"fLaC")
        fp.write(bytes([0x80, 0x00, 0x00, 0x22]))
        fp.write(streaminfo)


def _make_flac(tmp_path: Path, name: str = "test.flac") -> Path:
    p = tmp_path / name
    _make_minimal_flac(p)
    return p


# ── _sanitize_filename 테스트 ────────────────────────────────────────────────


def test_sanitize_filename_removes_special_chars():
    assert "/" not in _sanitize_filename("AC/DC")
    assert "\\" not in _sanitize_filename("path\\file")
    assert ":" not in _sanitize_filename("foo:bar")
    assert "*" not in _sanitize_filename("star*fish")
    assert "?" not in _sanitize_filename("what?")
    assert '"' not in _sanitize_filename('say "hello"')
    assert "<" not in _sanitize_filename("<tag>")
    assert ">" not in _sanitize_filename("<tag>")
    assert "|" not in _sanitize_filename("pipe|line")


def test_sanitize_filename_limits_length():
    long_name = "a" * 300
    assert len(_sanitize_filename(long_name)) <= 255


def test_sanitize_filename_nonempty_fallback():
    # 모두 특수문자인 경우 "_"을 반환
    result = _sanitize_filename("///")
    assert result != ""
    assert len(result) > 0


def test_sanitize_filename_normal_name_unchanged():
    assert _sanitize_filename("Radiohead") == "Radiohead"
    assert _sanitize_filename("Pablo Honey") == "Pablo Honey"


# ── _write_tags / _read_tags 테스트 ──────────────────────────────────────────


def test_write_tags_and_read_tags_flac(tmp_path):
    """FLAC 파일에 tags를 쓰고 다시 읽어서 일치하는지 검증한다."""
    flac_path = _make_flac(tmp_path)

    _write_tags(str(flac_path), "Radiohead", "Creep", "some-mb-uuid")

    tags = _read_tags(str(flac_path))
    assert tags["artist"] == "Radiohead"
    assert tags["title"] == "Creep"
    assert tags["mb_trackid"] == "some-mb-uuid"


def test_write_tags_without_mb_trackid(tmp_path):
    """mb_trackid 없이 태그를 쓰면 빈 문자열로 읽힌다."""
    flac_path = _make_flac(tmp_path)

    _write_tags(str(flac_path), "Artist", "Track")

    tags = _read_tags(str(flac_path))
    assert tags["artist"] == "Artist"
    assert tags["title"] == "Track"
    assert tags["mb_trackid"] == ""


def test_read_tags_nonexistent_file_returns_defaults():
    """존재하지 않는 파일에 _read_tags를 호출하면 기본값 dict를 반환한다."""
    tags = _read_tags("/nonexistent/path/file.flac")
    assert tags["artist"] == ""
    assert tags["title"] == ""
    assert tags["mb_trackid"] == ""
    assert tags["has_art"] is False


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
    _pretag(bad_path, artist="Artist", track_name="Track")


# ── tag_and_import 테스트 ─────────────────────────────────────────────────────


def test_tag_and_import_returns_false_when_file_not_found(tmp_path):
    """staging 파일이 없으면 즉시 (False, '') 를 반환한다."""
    missing = tmp_path / "missing.flac"
    success, dest = tag_and_import(
        str(missing),
        music_dir=str(tmp_path / "music"),
    )
    assert success is False
    assert dest == ""


def test_tag_and_import_continues_when_mb_search_fails(tmp_path, monkeypatch):
    """MB 검색이 빈 리스트를 반환해도 import는 계속 진행된다 (iTunes/Deezer fallback)."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr("src.pipeline.tagger._mb_search_recording", lambda a, t: [])
    monkeypatch.setattr("src.pipeline.tagger._enrich_track", lambda *args, **kwargs: None)

    success, dest = tag_and_import(
        str(flac_path),
        music_dir=str(tmp_path / "music"),
        artist="Artist",
        track_name="Track",
    )
    assert success is True
    assert dest != ""


def test_tag_and_import_copies_file_on_success(tmp_path, monkeypatch):
    """MB 검색 성공 시 파일을 music_dir에 복사하고 (True, dest_path)를 반환한다."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger._mb_search_recording", lambda a, t: ["fake-recording-id"]
    )
    monkeypatch.setattr("src.pipeline.tagger._enrich_track", lambda *args, **kwargs: None)

    music_dir = tmp_path / "music"
    success, dest = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="Radiohead",
        track_name="Creep",
    )
    assert success is True
    assert dest != ""
    assert Path(dest).exists()


def test_tag_and_import_staging_file_removed_after_success(tmp_path, monkeypatch):
    """성공 시 staging 파일이 삭제된다."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger._mb_search_recording", lambda a, t: ["fake-recording-id"]
    )
    monkeypatch.setattr("src.pipeline.tagger._enrich_track", lambda *args, **kwargs: None)

    music_dir = tmp_path / "music"
    success, dest = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="Radiohead",
        track_name="Creep",
    )
    assert success is True
    assert not flac_path.exists()


def test_tag_and_import_dest_path_contains_artist(tmp_path, monkeypatch):
    """복사된 파일 경로에 sanitized artist 이름이 포함된다."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger._mb_search_recording", lambda a, t: ["fake-recording-id"]
    )
    monkeypatch.setattr("src.pipeline.tagger._enrich_track", lambda *args, **kwargs: None)

    music_dir = tmp_path / "music"
    success, dest = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="Radiohead",
        track_name="Creep",
    )
    assert success is True
    assert "Radiohead" in dest


def test_tag_and_import_duplicate_file_returns_true(tmp_path, monkeypatch):
    """이미 dest 경로에 파일이 존재하면 duplicate로 처리해 True를 반환한다."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger._mb_search_recording", lambda a, t: ["fake-recording-id"]
    )
    monkeypatch.setattr("src.pipeline.tagger._enrich_track", lambda *args, **kwargs: None)

    music_dir = tmp_path / "music"
    # 첫 번째 import
    success1, dest1 = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="Radiohead",
        track_name="Creep",
    )
    assert success1 is True

    # 두 번째 import: 동일 경로에 파일이 이미 존재
    flac_path2 = _make_flac(tmp_path, name="test2.flac")
    success2, dest2 = tag_and_import(
        str(flac_path2),
        music_dir=str(music_dir),
        artist="Radiohead",
        track_name="Creep",
    )
    assert success2 is True


def test_tag_and_import_no_artist_no_mb_search(tmp_path, monkeypatch):
    """artist/track_name이 없으면 MB 검색을 건너뛰고 파일을 복사한다."""
    flac_path = _make_flac(tmp_path)
    mb_called = []
    monkeypatch.setattr(
        "src.pipeline.tagger._mb_search_recording",
        lambda a, t: mb_called.append((a, t)) or [],
    )
    monkeypatch.setattr("src.pipeline.tagger._enrich_track", lambda *args, **kwargs: None)

    music_dir = tmp_path / "music"
    success, dest = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
    )
    # artist/track이 없으면 MB 검색 호출 없이 파일 복사
    assert mb_called == []
    assert success is True
    assert dest != ""


# ── _mb_search_recording fallback: artist 유사도 기반 선택 ─────────────────────


def test_mb_search_recording_fallback_picks_best_artist_match(monkeypatch):
    """recording-only fallback 재검색 결과에서 artist-credits를 비교해 가장 유사한 recording을 반환한다.
    호출 순서: 1=strict query (empty), 2=plain query (empty), 3=recording-only fallback (data).
    """
    call_count = [0]

    def fake_get(url, params=None, headers=None, timeout=10):
        call_count[0] += 1
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        if call_count[0] <= 2:
            resp.json.return_value = {"recordings": []}
        else:
            resp.json.return_value = {
                "recordings": [
                    {
                        "id": "wrong-id-001",
                        "artist-credit": [
                            {"artist": {"name": "Mariah Carey", "sort-name": "Carey, Mariah"}}
                        ],
                    },
                    {
                        "id": "correct-id-002",
                        "artist-credit": [
                            {"artist": {"name": "Butterfly Jones", "sort-name": "Butterfly Jones"}}
                        ],
                    },
                ]
            }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    result = _mb_search_recording("Butterfly Jones", "butterfly")
    assert result == ["correct-id-002"]


def test_mb_search_recording_fallback_returns_empty_when_below_threshold(monkeypatch):
    """recording-only fallback 결과의 모든 artist 유사도가 0.3 미만이면 빈 문자열을 반환한다.
    호출 순서: 1=strict query (empty), 2=plain query (empty), 3=recording-only fallback (data).
    """
    call_count = [0]

    def fake_get(url, params=None, headers=None, timeout=10):
        call_count[0] += 1
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        if call_count[0] <= 2:
            resp.json.return_value = {"recordings": []}
        else:
            resp.json.return_value = {
                "recordings": [
                    {
                        "id": "unrelated-id-001",
                        "artist-credit": [
                            {
                                "artist": {
                                    "name": "XYZ Totally Different",
                                    "sort-name": "Different, XYZ",
                                }
                            }
                        ],
                    }
                ]
            }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    result = _mb_search_recording("Radiohead", "butterfly")
    assert result == []


def test_mb_search_recording_fallback_returns_empty_when_no_results(monkeypatch):
    """모든 단계(strict, plain, recording-only) 에서 결과가 없으면 빈 리스트를 반환한다."""
    call_count = [0]

    def fake_get(url, params=None, headers=None, timeout=10):
        call_count[0] += 1
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {"recordings": []}
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    result = _mb_search_recording("Artist", "track")
    assert result == []


# ── _mb_album_from_recording_id: 날짜 기준 오름차순 정렬 ─────────────────────────


def test_mb_album_picks_earliest_official_album_release(monkeypatch):
    """Official Album release 중 date 오름차순으로 정렬해 가장 오래된 것을 선택한다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "releases": [
                {
                    "id": "remaster-2005",
                    "title": "Pablo Honey (Remastered)",
                    "status": "Official",
                    "date": "2005-03-15",
                    "release-group": {"primary-type": "Album"},
                },
                {
                    "id": "original-1993",
                    "title": "Pablo Honey",
                    "status": "Official",
                    "date": "1993-02-22",
                    "release-group": {"primary-type": "Album"},
                },
                {
                    "id": "jp-edition-1993",
                    "title": "Pablo Honey (Japan Edition)",
                    "status": "Official",
                    "date": "1993-04-01",
                    "release-group": {"primary-type": "Album"},
                },
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    album, candidates = _mb_album_from_recording_id("some-recording-id")
    assert album == "Pablo Honey"
    assert candidates[0] == "original-1993"
    assert "remaster-2005" in candidates
    assert "jp-edition-1993" in candidates


def test_mb_album_fallback_picks_earliest_release(monkeypatch):
    """Official Album이 없을 때 fallback releases도 date 오름차순으로 정렬한다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "releases": [
                {
                    "id": "single-2010",
                    "title": "Creep (Single 2010)",
                    "status": "Official",
                    "date": "2010-01-01",
                    "release-group": {"primary-type": "Single"},
                },
                {
                    "id": "single-1992",
                    "title": "Creep (Single 1992)",
                    "status": "Official",
                    "date": "1992-09-21",
                    "release-group": {"primary-type": "Single"},
                },
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    album, candidates = _mb_album_from_recording_id("some-recording-id")
    assert album == "Creep (Single 1992)"
    assert candidates[0] == "single-1992"


def test_mb_album_releases_without_date_sorted_last(monkeypatch):
    """date가 없는 release는 맨 뒤로 정렬된다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "releases": [
                {
                    "id": "no-date-id",
                    "title": "Album (No Date)",
                    "status": "Official",
                    "date": "",
                    "release-group": {"primary-type": "Album"},
                },
                {
                    "id": "dated-id",
                    "title": "Album (With Date)",
                    "status": "Official",
                    "date": "2000-01-01",
                    "release-group": {"primary-type": "Album"},
                },
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    album, candidates = _mb_album_from_recording_id("some-recording-id")
    assert album == "Album (With Date)"
    assert candidates[0] == "dated-id"


def test_mb_album_excludes_live_secondary_type(monkeypatch):
    """secondary-types에 'Live'가 있는 Official Album release는 제외된다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "releases": [
                {
                    "id": "live-album-id",
                    "title": "Live at the Garden",
                    "status": "Official",
                    "date": "2001-06-01",
                    "release-group": {"primary-type": "Album", "secondary-types": ["Live"]},
                },
                {
                    "id": "studio-album-id",
                    "title": "OK Computer",
                    "status": "Official",
                    "date": "1997-05-21",
                    "release-group": {"primary-type": "Album", "secondary-types": []},
                },
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    album, candidates = _mb_album_from_recording_id("some-recording-id")
    assert album == "OK Computer"
    assert "live-album-id" not in candidates
    assert "studio-album-id" in candidates


def test_mb_album_excludes_compilation_secondary_type(monkeypatch):
    """secondary-types에 'Compilation'이 있는 Official Album release는 제외된다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "releases": [
                {
                    "id": "compilation-id",
                    "title": "The Best Of",
                    "status": "Official",
                    "date": "2003-01-01",
                    "release-group": {"primary-type": "Album", "secondary-types": ["Compilation"]},
                },
                {
                    "id": "studio-id",
                    "title": "The Bends",
                    "status": "Official",
                    "date": "1995-03-13",
                    "release-group": {"primary-type": "Album", "secondary-types": []},
                },
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    album, candidates = _mb_album_from_recording_id("some-recording-id")
    assert album == "The Bends"
    assert "compilation-id" not in candidates
    assert "studio-id" in candidates


def test_mb_album_includes_empty_secondary_types(monkeypatch):
    """secondary-types가 빈 배열이거나 키가 없는 Official Album release는 포함된다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "releases": [
                {
                    "id": "no-key-id",
                    "title": "Pablo Honey",
                    "status": "Official",
                    "date": "1993-02-22",
                    "release-group": {"primary-type": "Album"},
                    # secondary-types 키 없음
                },
                {
                    "id": "empty-list-id",
                    "title": "The Bends",
                    "status": "Official",
                    "date": "1995-03-13",
                    "release-group": {"primary-type": "Album", "secondary-types": []},
                },
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    album, candidates = _mb_album_from_recording_id("some-recording-id")
    assert "no-key-id" in candidates
    assert "empty-list-id" in candidates
    # 날짜 기준 오름차순으로 Pablo Honey(1993)가 첫 번째
    assert candidates[0] == "no-key-id"


# ── _itunes_search: artist 검증 ───────────────────────────────────────────────


def test_itunes_search_skips_low_similarity_artist(monkeypatch):
    """iTunes 결과의 artistName 유사도가 0.4 미만이면 빈 dict를 반환한다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "results": [
                {
                    "artistName": "Totally Unrelated Artist",
                    "collectionName": "Wrong Album",
                    "artworkUrl100": "http://example.com/art.jpg",
                }
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)

    result = _itunes_search("Radiohead", "Creep")
    assert result == {}


def test_itunes_search_returns_first_matching_artist(monkeypatch):
    """iTunes 결과 중 유사도 0.4 이상인 첫 번째 결과를 반환한다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "results": [
                {
                    "artistName": "XYZ",
                    "collectionName": "Wrong Album",
                    "artworkUrl100": "http://example.com/wrong.jpg",
                },
                {
                    "artistName": "Radiohead",
                    "collectionName": "Pablo Honey",
                    "artworkUrl100": "http://example.com/pablo100x100bb.jpg",
                },
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)

    result = _itunes_search("Radiohead", "Creep")
    assert result.get("album") == "Pablo Honey"
    assert "artwork_url" in result


# ── _deezer_search: artist 검증 ───────────────────────────────────────────────


def test_deezer_search_skips_low_similarity_artist(monkeypatch):
    """Deezer 결과의 artist.name 유사도가 0.4 미만이면 빈 dict를 반환한다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "data": [
                {
                    "artist": {"name": "Completely Different Artist"},
                    "album": {"title": "Wrong Album", "cover_xl": "http://example.com/wrong.jpg"},
                }
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)

    result = _deezer_search("Radiohead", "Creep")
    assert result == {}


def test_deezer_search_returns_first_matching_artist(monkeypatch):
    """Deezer 결과 중 유사도 0.4 이상인 첫 번째 결과를 반환한다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "data": [
                {
                    "artist": {"name": "Unrelated Act"},
                    "album": {"title": "Wrong Album", "cover_xl": "http://example.com/wrong.jpg"},
                },
                {
                    "artist": {"name": "Radiohead"},
                    "album": {
                        "title": "Pablo Honey",
                        "cover_xl": "http://example.com/correct.jpg",
                    },
                },
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)

    result = _deezer_search("Radiohead", "Creep")
    assert result.get("album") == "Pablo Honey"
    assert result.get("artwork_url") == "http://example.com/correct.jpg"


# ── _is_live_title 테스트 ─────────────────────────────────────────────────────


def test_is_live_title_date_prefix():
    """YYYY-MM-DD: 패턴으로 시작하는 제목은 라이브로 판정한다."""
    assert _is_live_title("2003-04-20: Orpheum, Boston, MA, USA") is True


def test_is_live_title_date_prefix_with_comma():
    """YYYY-MM-DD, 패턴(쉼표 구분)으로 시작하는 제목도 라이브로 판정한다."""
    assert _is_live_title("2021-11-21, Corona Capital: Mexico City") is True
    assert _is_live_title("2019-07-06, Glastonbury Festival") is True


def test_is_live_title_unplugged_keyword():
    """'unplugged' 키워드가 포함된 제목은 라이브로 판정한다."""
    assert _is_live_title("MTV Unplugged") is True
    assert _is_live_title("Unplugged in New York") is True


def test_is_live_title_live_keyword():
    """'live' 키워드가 포함된 제목은 라이브로 판정한다."""
    assert _is_live_title("Live at the Garden") is True
    assert _is_live_title("Anywhere but Home (Live)") is True


def test_is_live_title_concert_keyword():
    assert _is_live_title("Live Concert 2003") is True


def test_is_live_title_festival_keyword():
    assert _is_live_title("Festival Set 2010") is True


def test_is_live_title_bootleg_keyword():
    assert _is_live_title("Slim House: Зима 2007 (Bootleg)") is True


def test_is_live_title_normal_album_returns_false():
    """정규 앨범 제목은 라이브로 판정하지 않는다."""
    assert _is_live_title("Elephant") is False
    assert _is_live_title("Pablo Honey") is False
    assert _is_live_title("Fallen") is False
    assert _is_live_title("OK Computer") is False


def test_mb_album_fallback_skips_live_only_releases(monkeypatch):
    """Official Album releases가 모두 라이브(secondary-types=[Live])일 때,
    fallback에서도 라이브 secondary-types를 제외하고 Official Single을 선택한다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "releases": [
                {
                    "id": "live-album-id",
                    "title": "Anywhere but Home",
                    "status": "Official",
                    "date": "2004-11-22",
                    "release-group": {"primary-type": "Album", "secondary-types": ["Live"]},
                },
                {
                    "id": "single-id",
                    "title": "Bring Me to Life",
                    "status": "Official",
                    "date": "2003-03-24",
                    "release-group": {"primary-type": "Single", "secondary-types": []},
                },
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    album, candidates = _mb_album_from_recording_id("some-recording-id")
    assert album == "Bring Me to Life"
    assert "live-album-id" not in candidates
    assert "single-id" in candidates


def test_mb_album_fallback_skips_live_date_title(monkeypatch):
    """fallback에서 YYYY-MM-DD: 패턴 제목을 가진 Bootleg release를 건너뛰고
    날짜 없는 다른 release를 선택한다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "releases": [
                {
                    "id": "bootleg-id",
                    "title": "2003-04-20: Orpheum, Boston, MA, USA",
                    "status": "Bootleg",
                    "date": None,
                    "release-group": {"primary-type": "Album", "secondary-types": ["Live"]},
                },
                {
                    "id": "single-id",
                    "title": "Seven Nation Army",
                    "status": "Official",
                    "date": "2003-01-27",
                    "release-group": {"primary-type": "Single", "secondary-types": []},
                },
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    album, candidates = _mb_album_from_recording_id("some-recording-id")
    assert album == "Seven Nation Army"
    assert "bootleg-id" not in candidates
    assert "single-id" in candidates


def test_mb_album_fallback_last_resort_when_all_live(monkeypatch):
    """모든 release가 라이브/Bootleg일 때는 최후 수단으로 가장 오래된 release를 선택한다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "releases": [
                {
                    "id": "bootleg-2005",
                    "title": "2005-06-10: Some Venue",
                    "status": "Bootleg",
                    "date": "2005-06-10",
                    "release-group": {"primary-type": "Album", "secondary-types": ["Live"]},
                },
                {
                    "id": "bootleg-2003",
                    "title": "2003-04-20: Orpheum, Boston, MA, USA",
                    "status": "Bootleg",
                    "date": None,
                    "release-group": {"primary-type": "Album", "secondary-types": ["Live"]},
                },
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    album, candidates = _mb_album_from_recording_id("some-recording-id")
    # 마지막 수단: 날짜 오름차순 중 첫 번째 (date=None → key "9999"이므로 2005-06-10이 먼저)
    assert album == "2005-06-10: Some Venue"
    assert "bootleg-2005" in candidates


def test_mb_album_primary_filter_excludes_live_title_even_without_secondary_types(monkeypatch):
    """secondary-types는 없지만 제목이 라이브 공연 패턴이면 primary 필터에서 제외된다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "releases": [
                {
                    "id": "suspicious-live-id",
                    "title": "2003-04-20: Orpheum, Boston",
                    "status": "Official",
                    "date": "2003-04-20",
                    "release-group": {"primary-type": "Album", "secondary-types": []},
                },
                {
                    "id": "studio-id",
                    "title": "Elephant",
                    "status": "Official",
                    "date": "2003-04-01",
                    "release-group": {"primary-type": "Album", "secondary-types": []},
                },
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    album, candidates = _mb_album_from_recording_id("some-recording-id")
    assert album == "Elephant"
    assert "suspicious-live-id" not in candidates
    assert "studio-id" in candidates


# ── _mb_search_recording: Lucene 쿼리 따옴표 및 aliases 처리 ─────────────────────


def test_mb_search_recording_primary_query_has_quotes(monkeypatch):
    """공백 포함 아티스트/트랙 검색 시 Lucene 쿼리에 따옴표가 포함되는지 검증한다."""
    captured_params = []

    def fake_get(url, params=None, headers=None, timeout=10):
        captured_params.append(params or {})
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {"recordings": [{"id": "found-id-001"}]}
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    _mb_search_recording("아이유", "밤편지")
    assert captured_params, "requests.get이 호출되지 않았다"
    query = captured_params[0].get("query", "")
    assert '"아이유"' in query
    assert '"밤편지"' in query


def test_mb_search_recording_fallback_matches_via_alias(monkeypatch):
    """한글 입력(아이유) vs 영문 primary name(IU)인 경우 aliases로 매칭된다.
    호출 순서: 1=strict query (empty), 2=plain query (empty), 3=recording-only fallback (data).
    """
    call_count = [0]

    def fake_get(url, params=None, headers=None, timeout=10):
        call_count[0] += 1
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        if call_count[0] <= 2:
            # strict/plain 검색: 0 results
            resp.json.return_value = {"recordings": []}
        else:
            # recording-only fallback: IU의 MB primary name은 "IU", alias에 "아이유" 포함
            resp.json.return_value = {
                "recordings": [
                    {
                        "id": "iu-recording-001",
                        "artist-credit": [
                            {
                                "artist": {
                                    "name": "IU",
                                    "sort-name": "IU",
                                    "aliases": [
                                        {"name": "아이유"},
                                        {"name": "李知恩"},
                                    ],
                                }
                            }
                        ],
                    }
                ]
            }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    result = _mb_search_recording("아이유", "밤편지")
    assert result == ["iu-recording-001"]


def test_mb_search_recording_fallback_no_aliases_uses_name(monkeypatch):
    """aliases가 없어도 name/sort-name 비교가 정상 동작한다.
    호출 순서: 1=strict query (empty), 2=plain query (empty), 3=recording-only fallback (data).
    """
    call_count = [0]

    def fake_get(url, params=None, headers=None, timeout=10):
        call_count[0] += 1
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        if call_count[0] <= 2:
            resp.json.return_value = {"recordings": []}
        else:
            resp.json.return_value = {
                "recordings": [
                    {
                        "id": "radiohead-creep-001",
                        "artist-credit": [
                            {
                                "artist": {
                                    "name": "Radiohead",
                                    "sort-name": "Radiohead",
                                    # aliases 키 없음
                                }
                            }
                        ],
                    }
                ]
            }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    result = _mb_search_recording("Radiohead", "Creep")
    assert result == ["radiohead-creep-001"]


# ── _mb_search_recording: strict query (Album/Official/no-Live) ────────────────


def test_mb_search_recording_strict_query_returns_first_result(monkeypatch):
    """strict 쿼리(첫 번째 시도)에서 결과가 있으면 바로 반환하고 이후 쿼리는 호출하지 않는다."""
    call_count = [0]

    def fake_get(url, params=None, headers=None, timeout=10):
        call_count[0] += 1
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "recordings": [{"id": "studio-recording-001", "title": "Seven Nation Army"}]
        }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    result = _mb_search_recording("The White Stripes", "Seven Nation Army")
    assert "studio-recording-001" in result
    assert call_count[0] == 1


def test_mb_search_recording_strict_query_contains_album_and_official(monkeypatch):
    """strict 쿼리에 primarytype:Album, status:Official, NOT secondarytype:Live 조건이 포함된다."""
    captured_params = []

    def fake_get(url, params=None, headers=None, timeout=10):
        captured_params.append(params or {})
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {"recordings": [{"id": "found-id"}]}
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    _mb_search_recording("The White Stripes", "Seven Nation Army")
    assert captured_params, "requests.get이 호출되지 않았다"
    query = captured_params[0].get("query", "")
    assert "primarytype:Album" in query
    assert "status:Official" in query
    assert "NOT secondarytype:Live" in query
    assert "NOT secondarytype:Compilation" in query


def test_mb_search_recording_strict_empty_then_plain_returns_result(monkeypatch):
    """strict 쿼리가 empty이면 plain 쿼리(두 번째 시도)로 결과를 반환한다."""
    call_count = [0]

    def fake_get(url, params=None, headers=None, timeout=10):
        call_count[0] += 1
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        if call_count[0] == 1:
            resp.json.return_value = {"recordings": []}
        else:
            resp.json.return_value = {
                "recordings": [{"id": "plain-recording-001", "title": "Some Track"}]
            }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    result = _mb_search_recording("Some Artist", "Some Track")
    assert "plain-recording-001" in result
    assert call_count[0] == 2


# ── _pick_best_recording 테스트 ───────────────────────────────────────────────


def test_pick_best_recording_prefers_official_album():
    """Official Album release를 가진 recording이 목록 앞에 없어도 우선 선택된다."""
    recordings = [
        {
            "id": "mixtape-rec-001",
            "title": "Without Me",
            "releases": [
                {
                    "status": "Official",
                    "release-group": {
                        "primary-type": "Album",
                        "secondary-types": ["Mixtape/Street"],
                    },
                }
            ],
        },
        {
            "id": "official-album-rec-002",
            "title": "Without Me",
            "releases": [
                {
                    "status": "Official",
                    "release-group": {"primary-type": "Album", "secondary-types": []},
                }
            ],
        },
    ]
    result = _pick_best_recording(recordings, "Without Me")
    assert result == "official-album-rec-002"


def test_pick_best_recording_skips_mixtape():
    """Mixtape/Street secondary-type만 가진 recording은 건너뛰고 Official Album을 선택한다."""
    recordings = [
        {
            "id": "djmix-rec-001",
            "title": "Seven Nation Army",
            "releases": [
                {
                    "status": "Official",
                    "release-group": {"primary-type": "Album", "secondary-types": ["DJ-mix"]},
                }
            ],
        },
        {
            "id": "studio-rec-002",
            "title": "Seven Nation Army",
            "releases": [
                {
                    "status": "Official",
                    "release-group": {"primary-type": "Album"},
                    # secondary-types 키 없음 → 정규 앨범으로 처리
                }
            ],
        },
    ]
    result = _pick_best_recording(recordings, "Seven Nation Army")
    assert result == "studio-rec-002"


def test_pick_best_recording_fallback_to_first():
    """Official Album release가 없으면 목록의 첫 번째 recording을 반환한다."""
    recordings = [
        {
            "id": "single-rec-001",
            "title": "Creep",
            "releases": [
                {
                    "status": "Official",
                    "release-group": {"primary-type": "Single"},
                }
            ],
        },
        {
            "id": "compilation-rec-002",
            "title": "Creep",
            "releases": [
                {
                    "status": "Official",
                    "release-group": {"primary-type": "Album", "secondary-types": ["Compilation"]},
                }
            ],
        },
    ]
    result = _pick_best_recording(recordings, "Creep")
    assert result == "single-rec-001"


def test_pick_best_recording_skips_partial_title_match():
    """'Without Me'를 검색할 때 'Life Ain't Shit Without Me' recording은 건너뛰고
    정확한 제목의 recording을 선택한다."""
    recordings = [
        {
            "id": "wrong-id-partial",
            "title": "Life Ain't Shit Without Me",
            "releases": [
                {
                    "status": "Official",
                    "release-group": {"primary-type": "Album", "secondary-types": []},
                }
            ],
        },
        {
            "id": "correct-id-exact",
            "title": "Without Me",
            "releases": [
                {
                    "status": "Official",
                    "release-group": {"primary-type": "Album", "secondary-types": []},
                }
            ],
        },
    ]
    result = _pick_best_recording(recordings, "Without Me")
    assert result == "correct-id-exact"


def test_pick_best_recording_title_threshold_0_8():
    """title 유사도 0.8 미만인 recording만 있으면 빈 문자열을 반환한다."""
    recordings = [
        {
            "id": "low-similarity-id",
            "title": "Completely Different Song Title Here",
            "releases": [
                {
                    "status": "Official",
                    "release-group": {"primary-type": "Album", "secondary-types": []},
                }
            ],
        },
    ]
    result = _pick_best_recording(recordings, "Without Me")
    assert result == ""


def test_pick_best_recording_no_track_name_returns_first():
    """track_name이 없으면 기존 fallback으로 첫 번째 recording을 반환한다."""
    recordings = [
        {
            "id": "first-rec-id",
            "title": "Some Song",
            "releases": [
                {
                    "status": "Official",
                    "release-group": {"primary-type": "Single"},
                }
            ],
        },
    ]
    result = _pick_best_recording(recordings, "")
    assert result == "first-rec-id"


def test_collect_recording_candidates_empty_when_all_below_threshold():
    """모든 recording의 title 유사도가 0.8 미만이면 빈 리스트를 반환한다."""
    recordings = [
        {
            "id": "wrong-id",
            "title": "Life Ain't Shit Without Me",
            "releases": [
                {
                    "status": "Official",
                    "release-group": {"primary-type": "Album", "secondary-types": []},
                }
            ],
        },
    ]
    result = _collect_recording_candidates(recordings, "Without Me")
    assert result == []


# ── _enrich_track: Unknown Album fallback ────────────────────────────────────


def test_enrich_track_writes_unknown_album_when_all_sources_fail(tmp_path, monkeypatch):
    """iTunes/Deezer/MB/YouTube 4단계 모두 실패하면 'Unknown Album' 태그를 기록한다."""
    flac_path = _make_flac(tmp_path)
    _write_tags(str(flac_path), "Artist", "Track")

    monkeypatch.setattr("src.pipeline.tagger._itunes_search", lambda a, t: {})
    monkeypatch.setattr("src.pipeline.tagger._deezer_search", lambda a, t: {})
    monkeypatch.setattr("src.pipeline.tagger._mb_search_recording", lambda a, t: [])
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    _enrich_track(
        str(flac_path),
        artist="Artist",
        track_name="Track",
        yt_metadata=None,
        recording_ids=None,
    )

    tags = _read_tags(str(flac_path))
    assert tags["album"] == "Unknown Album"


def test_enrich_track_unknown_album_not_written_when_has_album(tmp_path, monkeypatch):
    """이미 album 태그가 있으면 Unknown Album으로 덮어쓰지 않는다."""
    import mutagen.flac as _flac

    flac_path = _make_flac(tmp_path)
    _write_tags(str(flac_path), "Artist", "Track")
    f = _flac.FLAC(str(flac_path))
    f["album"] = "My Real Album"
    f.save()

    write_album_called = []
    monkeypatch.setattr(
        "src.pipeline.tagger._write_album_tag",
        lambda path, album: write_album_called.append(album),
    )

    _enrich_track(
        str(flac_path),
        artist="Artist",
        track_name="Track",
        yt_metadata=None,
        recording_ids=None,
    )

    assert "Unknown Album" not in write_album_called


def test_enrich_track_unknown_album_not_written_when_yt_channel_available(tmp_path, monkeypatch):
    """YouTube channel 이름이 있으면 Unknown Album이 아닌 channel 이름으로 album 태그를 쓴다."""
    flac_path = _make_flac(tmp_path)
    _write_tags(str(flac_path), "Artist", "Track")

    monkeypatch.setattr("src.pipeline.tagger._itunes_search", lambda a, t: {})
    monkeypatch.setattr("src.pipeline.tagger._deezer_search", lambda a, t: {})
    monkeypatch.setattr("src.pipeline.tagger._mb_search_recording", lambda a, t: [])
    monkeypatch.setattr("src.pipeline.tagger.time.sleep", lambda s: None)

    _enrich_track(
        str(flac_path),
        artist="Artist",
        track_name="Track",
        yt_metadata={"channel": "ArtistVEVO", "thumbnail_url": ""},
        recording_ids=None,
    )

    tags = _read_tags(str(flac_path))
    assert tags["album"] == "ArtistVEVO"
