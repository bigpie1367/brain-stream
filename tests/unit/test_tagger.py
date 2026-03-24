"""
tests/unit/test_tagger.py
tagger.py 단위 테스트 (beets 제거 후 MB 직접 매칭 구현 기준)

- sanitize_path_component: 파일시스템 특수문자 제거 검증 (src.utils.fs)
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
from src.pipeline.musicbrainz import (
    _collect_recording_candidates,
    _mb_lookup_artist_ids,
    _pick_best_recording,
    lookup_recording,
    mb_album_from_recording_id,
    mb_search_recording,
)
from src.pipeline.tagger import (
    _enrich_track,
    _is_live_title,
    _pretag,
    _primary_artist,
    _read_tags,
    _write_tags,
    deezer_search,
    itunes_search,
    tag_and_import,
    write_artist_tag,
    write_mb_trackid_tag,
)
from src.utils.fs import sanitize_path_component

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


# ── sanitize_path_component 테스트 ───────────────────────────────────────────


def test_sanitize_filename_removes_special_chars():
    assert "/" not in sanitize_path_component("AC/DC")
    assert "\\" not in sanitize_path_component("path\\file")
    assert ":" not in sanitize_path_component("foo:bar")
    assert "*" not in sanitize_path_component("star*fish")
    assert "?" not in sanitize_path_component("what?")
    assert '"' not in sanitize_path_component('say "hello"')
    assert "<" not in sanitize_path_component("<tag>")
    assert ">" not in sanitize_path_component("<tag>")
    assert "|" not in sanitize_path_component("pipe|line")


def test_sanitize_filename_limits_length():
    long_name = "a" * 300
    assert len(sanitize_path_component(long_name)) <= 255


def test_sanitize_filename_nonempty_fallback():
    # 모두 특수문자인 경우 "_"을 반환
    result = sanitize_path_component("///")
    assert result != ""
    assert len(result) > 0


def test_sanitize_filename_normal_name_unchanged():
    assert sanitize_path_component("Radiohead") == "Radiohead"
    assert sanitize_path_component("Pablo Honey") == "Pablo Honey"


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


def test_write_tags_flac_stores_list_format(tmp_path):
    """_write_tags가 FLAC 파일에 artist/title/mb_trackid를 list 형식으로 저장하는지 검증한다.

    mutagen FLAC은 Vorbis Comment 스펙 준수를 위해 list 형식을 사용한다.
    _read_tags는 (get() or [""])[0] 패턴으로 읽으므로 list가 정규 형식이다.
    """
    flac_path = _make_flac(tmp_path)

    _write_tags(str(flac_path), "Radiohead", "Creep", "some-mb-uuid")

    f = mutagen.flac.FLAC(str(flac_path))
    assert f.get("artist") == ["Radiohead"], "FLAC artist must be stored as list"
    assert f.get("title") == ["Creep"], "FLAC title must be stored as list"
    assert f.get("musicbrainz_trackid") == ["some-mb-uuid"], (
        "FLAC mb_trackid must be stored as list"
    )


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


# ── _write_artist_tag 테스트 ──────────────────────────────────────────────────


def test_write_artist_tag_writes_to_flac(tmp_path):
    """FLAC 파일에 artist 태그를 올바르게 기록한다."""
    flac_path = _make_flac(tmp_path)
    write_artist_tag(str(flac_path), "NewArtist")

    f = mutagen.flac.FLAC(str(flac_path))
    assert f.get("artist") == ["NewArtist"]


def test_write_artist_tag_overwrites_existing_artist(tmp_path):
    """기존 artist 태그가 있어도 새 값으로 덮어쓴다."""
    flac_path = _make_flac(tmp_path)
    _write_tags(str(flac_path), "OldArtist", "Track")

    write_artist_tag(str(flac_path), "UpdatedArtist")

    f = mutagen.flac.FLAC(str(flac_path))
    assert f.get("artist") == ["UpdatedArtist"]


def test_write_artist_tag_public_alias(tmp_path):
    """write_artist_tag public alias가 _write_artist_tag와 동일하게 동작한다."""
    flac_path = _make_flac(tmp_path)
    write_artist_tag(str(flac_path), "AliasArtist")

    f = mutagen.flac.FLAC(str(flac_path))
    assert f.get("artist") == ["AliasArtist"]


def test_write_artist_tag_nonexistent_file_raises(tmp_path):
    """존재하지 않는 파일에 대해 write_artist_tag는 예외를 발생시킨다."""
    bad_path = tmp_path / "nonexistent.flac"
    with pytest.raises(Exception):
        write_artist_tag(str(bad_path), "Artist")


# ── _write_mb_trackid_tag 테스트 ──────────────────────────────────────────────


def test_write_mb_trackid_tag_flac(tmp_path):
    """FLAC 파일에 musicbrainz_trackid 태그를 올바르게 기록한다."""
    flac_path = _make_flac(tmp_path)
    recording_id = "aaaabbbb-cccc-dddd-eeee-ffff00001111"

    write_mb_trackid_tag(str(flac_path), recording_id)

    f = mutagen.flac.FLAC(str(flac_path))
    assert f.get("musicbrainz_trackid") == [recording_id]


def test_write_mb_trackid_tag_opus(tmp_path):
    """Opus 파일에 musicbrainz_trackid 태그를 올바르게 기록한다."""

    opus_path = tmp_path / "test.opus"

    # 최소 OggOpus 파일 생성 (mutagen이 읽을 수 있는 실제 OggOpus 포맷)
    # OggOpus 헤더: OpusHead + OpusTags 페이지 필요
    def _make_ogg_page(header_type, granule, serial, seq, data):
        import struct as _struct
        import zlib

        segments = []
        offset = 0
        while offset < len(data):
            seg = data[offset : offset + 255]
            segments.append(seg)
            offset += 255
        segment_table = bytes([len(s) for s in segments])
        lacing = len(segments)
        header = (
            b"OggS"
            + _struct.pack("<B", 0)  # version
            + _struct.pack("<B", header_type)
            + _struct.pack("<Q", granule)
            + _struct.pack("<I", serial)
            + _struct.pack("<I", seq)
            + _struct.pack("<I", 0)  # checksum placeholder
            + _struct.pack("<B", lacing)
            + segment_table
        )
        page = header + b"".join(segments)
        crc = zlib.crc32(page) & 0xFFFFFFFF
        page = page[:22] + _struct.pack("<I", crc) + page[26:]
        return page

    import struct as _struct

    # OpusHead
    opus_head = (
        b"OpusHead"
        + _struct.pack("<B", 1)  # version
        + _struct.pack("<B", 2)  # channels
        + _struct.pack("<H", 312)  # pre-skip
        + _struct.pack("<I", 48000)  # input sample rate
        + _struct.pack("<H", 0)  # output gain
        + _struct.pack("<B", 0)  # channel mapping
    )
    page0 = _make_ogg_page(0x02, 0, 1, 0, opus_head)

    # OpusTags (vendor string + 0 comments)
    vendor = b"libopus"
    opus_tags = (
        b"OpusTags"
        + _struct.pack("<I", len(vendor))
        + vendor
        + _struct.pack("<I", 0)  # user comment list length
    )
    page1 = _make_ogg_page(0x00, 0, 1, 1, opus_tags)

    with open(opus_path, "wb") as fp:
        fp.write(page0 + page1)

    recording_id = "11112222-3333-4444-5555-666677778888"
    write_mb_trackid_tag(str(opus_path), recording_id)

    import mutagen.oggopus as _oggopus

    f = _oggopus.OggOpus(str(opus_path))
    assert f.get("musicbrainz_trackid") == [recording_id]


def test_write_mb_trackid_tag_public_alias(tmp_path):
    """write_mb_trackid_tag public alias가 _write_mb_trackid_tag와 동일하게 동작한다."""
    flac_path = _make_flac(tmp_path)
    recording_id = "deadbeef-dead-beef-dead-beefdeadbeef"

    write_mb_trackid_tag(str(flac_path), recording_id)

    f = mutagen.flac.FLAC(str(flac_path))
    assert f.get("musicbrainz_trackid") == [recording_id]


def test_write_mb_trackid_tag_nonexistent_file_raises(tmp_path):
    """존재하지 않는 파일에 대해 write_mb_trackid_tag는 예외를 발생시킨다."""
    bad_path = tmp_path / "nonexistent.flac"
    with pytest.raises(Exception):
        write_mb_trackid_tag(str(bad_path), "some-uuid")


# ── tag_and_import 테스트 ─────────────────────────────────────────────────────


def test_tag_and_import_returns_false_when_file_not_found(tmp_path):
    """staging 파일이 없으면 즉시 (False, '') 를 반환한다."""
    missing = tmp_path / "missing.flac"
    success, dest, *_ = tag_and_import(
        str(missing),
        music_dir=str(tmp_path / "music"),
    )
    assert success is False
    assert dest == ""


def test_tag_and_import_continues_when_mb_search_fails(tmp_path, monkeypatch):
    """MB 검색이 빈 리스트를 반환해도 import는 계속 진행된다 (iTunes/Deezer fallback)."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording", lambda a, t: ([], "", "")
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track", lambda *args, **kwargs: ("", "", "")
    )

    success, dest, *_ = tag_and_import(
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
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: (["fake-recording-id"], "", ""),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track", lambda *args, **kwargs: ("", "", "")
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
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
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: (["fake-recording-id"], "", ""),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track", lambda *args, **kwargs: ("", "", "")
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
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
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: (["fake-recording-id"], "", ""),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track", lambda *args, **kwargs: ("", "", "")
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
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
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: (["fake-recording-id"], "", ""),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track", lambda *args, **kwargs: ("", "", "")
    )

    music_dir = tmp_path / "music"
    # 첫 번째 import
    success1, dest1, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="Radiohead",
        track_name="Creep",
    )
    assert success1 is True

    # 두 번째 import: 동일 경로에 파일이 이미 존재
    flac_path2 = _make_flac(tmp_path, name="test2.flac")
    success2, dest2, *_ = tag_and_import(
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
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: (mb_called.append((a, t)) or ([], "", "")),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track", lambda *args, **kwargs: ("", "", "")
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
    )
    # artist/track이 없으면 MB 검색 호출 없이 파일 복사
    assert mb_called == []
    assert success is True
    assert dest != ""


def test_tag_and_import_returns_6tuple_on_success(tmp_path, monkeypatch):
    """tag_and_import 성공 시 6-tuple (bool, str, str, str, str, str)을 반환한다."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: (["fake-rec-id"], "Radiohead", "Creep"),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("Pablo Honey", "Radiohead", "Creep"),
    )

    music_dir = tmp_path / "music"
    result = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="Radiohead",
        track_name="Creep",
    )
    assert len(result) == 6
    (
        success,
        dest,
        canonical_artist,
        canonical_title,
        canonical_album,
        mb_recording_id,
    ) = result
    assert success is True
    assert dest != ""
    assert mb_recording_id == "fake-rec-id"


def test_tag_and_import_returns_6tuple_on_failure(tmp_path):
    """tag_and_import 실패 시 6-tuple을 반환하며 첫 번째 요소가 False이다."""
    missing = tmp_path / "missing.flac"
    result = tag_and_import(str(missing), music_dir=str(tmp_path / "music"))
    assert len(result) == 6
    assert result[0] is False
    assert all(v == "" for v in result[1:])


def test_tag_and_import_mb_recording_id_empty_when_no_mb_search(tmp_path, monkeypatch):
    """MB 검색이 빈 결과를 반환하면 mb_recording_id는 빈 문자열이다."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording", lambda a, t: ([], "", "")
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track", lambda *args, **kwargs: ("", "", "")
    )

    music_dir = tmp_path / "music"
    result = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="Artist",
        track_name="Track",
    )
    assert len(result) == 6
    success, dest, _, _, _, mb_recording_id = result
    assert success is True
    assert mb_recording_id == ""


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
                            {
                                "artist": {
                                    "name": "Mariah Carey",
                                    "sort-name": "Carey, Mariah",
                                }
                            }
                        ],
                    },
                    {
                        "id": "correct-id-002",
                        "artist-credit": [
                            {
                                "artist": {
                                    "name": "Butterfly Jones",
                                    "sort-name": "Butterfly Jones",
                                }
                            }
                        ],
                    },
                ]
            }
        return resp

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    result, mb_artist, mb_title = mb_search_recording("Butterfly Jones", "butterfly")
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

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    result, mb_artist, mb_title = mb_search_recording("Radiohead", "butterfly")
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

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    result, mb_artist, mb_title = mb_search_recording("Artist", "track")
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

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    album, candidates = mb_album_from_recording_id("some-recording-id")
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

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    album, candidates = mb_album_from_recording_id("some-recording-id")
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

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    album, candidates = mb_album_from_recording_id("some-recording-id")
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
                    "release-group": {
                        "primary-type": "Album",
                        "secondary-types": ["Live"],
                    },
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

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    album, candidates = mb_album_from_recording_id("some-recording-id")
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
                    "release-group": {
                        "primary-type": "Album",
                        "secondary-types": ["Compilation"],
                    },
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

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    album, candidates = mb_album_from_recording_id("some-recording-id")
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

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    album, candidates = mb_album_from_recording_id("some-recording-id")
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

    result = itunes_search("Radiohead", "Creep")
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

    result = itunes_search("Radiohead", "Creep")
    assert result.get("album") == "Pablo Honey"
    assert "artwork_url" in result


def test_itunes_search_country_param_passed_to_request(monkeypatch):
    """country 파라미터가 주어지면 requests.get params에 포함된다."""
    captured_params = {}

    def fake_get(url, params=None, headers=None, timeout=10):
        captured_params.update(params or {})
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {"results": []}
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)

    itunes_search("Radiohead", "Creep", country="KR")
    assert captured_params.get("country") == "KR"


def test_itunes_search_no_country_param_when_none(monkeypatch):
    """country가 None이면 params에 country 키가 없다."""
    captured_params = {}

    def fake_get(url, params=None, headers=None, timeout=10):
        captured_params.update(params or {})
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {"results": []}
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)

    itunes_search("Radiohead", "Creep")
    assert "country" not in captured_params


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
                    "album": {
                        "title": "Wrong Album",
                        "cover_xl": "http://example.com/wrong.jpg",
                    },
                }
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.tagger.requests.get", fake_get)

    result = deezer_search("Radiohead", "Creep")
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
                    "album": {
                        "title": "Wrong Album",
                        "cover_xl": "http://example.com/wrong.jpg",
                    },
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

    result = deezer_search("Radiohead", "Creep")
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
                    "release-group": {
                        "primary-type": "Album",
                        "secondary-types": ["Live"],
                    },
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

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    album, candidates = mb_album_from_recording_id("some-recording-id")
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
                    "release-group": {
                        "primary-type": "Album",
                        "secondary-types": ["Live"],
                    },
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

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    album, candidates = mb_album_from_recording_id("some-recording-id")
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
                    "release-group": {
                        "primary-type": "Album",
                        "secondary-types": ["Live"],
                    },
                },
                {
                    "id": "bootleg-2003",
                    "title": "2003-04-20: Orpheum, Boston, MA, USA",
                    "status": "Bootleg",
                    "date": None,
                    "release-group": {
                        "primary-type": "Album",
                        "secondary-types": ["Live"],
                    },
                },
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    album, candidates = mb_album_from_recording_id("some-recording-id")
    # 마지막 수단: 날짜 오름차순 중 첫 번째 (date=None → key "9999"이므로 2005-06-10이 먼저)
    assert album == "2005-06-10: Some Venue"
    assert "bootleg-2005" in candidates


def test_mb_album_primary_filter_excludes_live_title_even_without_secondary_types(
    monkeypatch,
):
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

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    album, candidates = mb_album_from_recording_id("some-recording-id")
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

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    mb_search_recording("아이유", "밤편지")
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

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    result, mb_artist, mb_title = mb_search_recording("아이유", "밤편지")
    assert result == ["iu-recording-001"]
    assert mb_artist == "IU"


# ── _lookup_recording_by_mbid 테스트 ──────────────────────────────────────────


def test_lookup_recording_by_mbid_returns_artist_and_title(monkeypatch):
    """MB recording 직접 조회가 성공하면 artist와 title을 반환한다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "title": "Creep",
            "artist-credit": [
                {"artist": {"name": "Radiohead"}, "joinphrase": ""},
            ],
        }
        return resp

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    result = lookup_recording("some-uuid-001")
    assert result["artist"] == "Radiohead"
    assert result["title"] == "Creep"


def test_lookup_recording_by_mbid_joins_multiple_artist_credits(monkeypatch):
    """여러 artist-credit이 있을 때 joinphrase를 포함해 이어붙인 artist 이름을 반환한다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "title": "Collab Track",
            "artist-credit": [
                {"artist": {"name": "Artist A"}, "joinphrase": " feat. "},
                {"artist": {"name": "Artist B"}, "joinphrase": ""},
            ],
        }
        return resp

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    result = lookup_recording("some-uuid-002")
    assert result["artist"] == "Artist A feat. Artist B"
    assert result["title"] == "Collab Track"


def test_lookup_recording_by_mbid_returns_empty_on_http_error(monkeypatch):
    """HTTP 오류 발생 시 빈 문자열 dict를 반환한다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status.side_effect = Exception("404 Not Found")
        return resp

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    result = lookup_recording("nonexistent-uuid")
    assert result == {"artist": "", "title": ""}


# ── tag_and_import: LB mbid 직접 조회 테스트 ─────────────────────────────────


def test_tag_and_import_uses_direct_lookup_for_lb_track(tmp_path, monkeypatch):
    """mbid가 'manual-'로 시작하지 않으면 _mb_search_recording 대신
    _lookup_recording_by_mbid를 호출한다."""
    flac_path = _make_flac(tmp_path)

    search_called = []
    lookup_called = []

    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: (search_called.append((a, t)) or ([], "", "")),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger.lookup_recording",
        lambda m: (
            lookup_called.append(m) or {"artist": "Radiohead", "title": "Creep"}
        ),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track", lambda *args, **kwargs: ("", "", "")
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="Radiohead",
        track_name="Creep",
        mbid="valid-lb-uuid-001",
    )

    assert success is True
    assert lookup_called == ["valid-lb-uuid-001"]
    assert search_called == []


def test_tag_and_import_uses_search_for_manual_track(tmp_path, monkeypatch):
    """mbid가 'manual-'로 시작하면 _lookup_recording_by_mbid 대신
    _mb_search_recording을 호출한다."""
    flac_path = _make_flac(tmp_path)

    search_called = []
    lookup_called = []

    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: (search_called.append((a, t)) or (["fake-id"], "", "")),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger.lookup_recording",
        lambda m: (lookup_called.append(m) or {"artist": "", "title": ""}),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track", lambda *args, **kwargs: ("", "", "")
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="Radiohead",
        track_name="Creep",
        mbid="manual-abc12345",
    )

    assert success is True
    assert lookup_called == []
    assert search_called == [("Radiohead", "Creep")]


def test_tag_and_import_lb_track_falls_back_to_search_on_lookup_failure(
    tmp_path, monkeypatch
):
    """LB mbid로 직접 조회가 실패(빈 응답)하면 _mb_search_recording으로 폴백한다."""
    flac_path = _make_flac(tmp_path)

    search_called = []

    monkeypatch.setattr(
        "src.pipeline.tagger.lookup_recording",
        lambda m: {"artist": "", "title": ""},
    )
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: (search_called.append((a, t)) or ([], "", "")),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track", lambda *args, **kwargs: ("", "", "")
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="Radiohead",
        track_name="Creep",
        mbid="valid-lb-uuid-002",
    )

    assert success is True
    assert search_called == [("Radiohead", "Creep")]


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

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    result, mb_artist, mb_title = mb_search_recording("Radiohead", "Creep")
    assert result == ["radiohead-creep-001"]
    assert mb_artist == "Radiohead"


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

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    result, mb_artist, mb_title = mb_search_recording(
        "The White Stripes", "Seven Nation Army"
    )
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

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    mb_search_recording("The White Stripes", "Seven Nation Army")
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

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    result, mb_artist, mb_title = mb_search_recording("Some Artist", "Some Track")
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
                    "release-group": {
                        "primary-type": "Album",
                        "secondary-types": ["DJ-mix"],
                    },
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
                    "release-group": {
                        "primary-type": "Album",
                        "secondary-types": ["Compilation"],
                    },
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

    monkeypatch.setattr("src.pipeline.tagger.itunes_search", lambda a, t: {})
    monkeypatch.setattr("src.pipeline.tagger.deezer_search", lambda a, t: {})
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording", lambda a, t: ([], "", "")
    )
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

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
        "src.pipeline.tagger.write_album_tag",
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


def test_enrich_track_unknown_album_not_written_when_yt_channel_available(
    tmp_path, monkeypatch
):
    """YouTube channel 이름이 있으면 Unknown Album이 아닌 channel 이름으로 album 태그를 쓴다."""
    flac_path = _make_flac(tmp_path)
    _write_tags(str(flac_path), "Artist", "Track")

    monkeypatch.setattr("src.pipeline.tagger.itunes_search", lambda a, t: {})
    monkeypatch.setattr("src.pipeline.tagger.deezer_search", lambda a, t: {})
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording", lambda a, t: ([], "", "")
    )
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    _enrich_track(
        str(flac_path),
        artist="Artist",
        track_name="Track",
        yt_metadata={"channel": "ArtistVEVO", "thumbnail_url": ""},
        recording_ids=None,
    )

    tags = _read_tags(str(flac_path))
    assert tags["album"] == "ArtistVEVO"


# ── tag_and_import: 파일 이동 로직 ────────────────────────────────────────────


def test_tag_and_import_moves_file_to_album_folder(tmp_path, monkeypatch):
    """앨범 매칭 성공 시 Unknown Album/ 에서 실제 앨범 폴더로 파일이 이동된다."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: (["fake-recording-id"], "", ""),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("Pablo Honey", "", ""),
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="Radiohead",
        track_name="Creep",
    )

    assert success is True
    dest_path = Path(dest)
    # 파일이 실제 앨범 폴더에 있어야 한다
    assert "Pablo Honey" in str(dest_path)
    assert "Unknown Album" not in str(dest_path)
    assert dest_path.exists()
    # Unknown Album 폴더에 파일이 없어야 한다
    unknown_album_path = music_dir / "Radiohead" / "Unknown Album" / "Creep.flac"
    assert not unknown_album_path.exists()


def test_tag_and_import_stays_in_unknown_album_when_no_match(tmp_path, monkeypatch):
    """앨범 매칭 실패 시 (Unknown Album 반환) 파일이 Unknown Album/ 에 그대로 남는다."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording", lambda a, t: ([], "", "")
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("Unknown Album", "", ""),
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="Radiohead",
        track_name="Creep",
    )

    assert success is True
    dest_path = Path(dest)
    assert "Unknown Album" in str(dest_path)


# ── _primary_artist 테스트 ────────────────────────────────────────────────────


def test_primary_artist_feat_dot():
    """`feat.` 이후 전부 제거한다."""
    assert _primary_artist("Eminem feat. Nate Dogg") == "Eminem"


def test_primary_artist_feat_no_dot():
    """`feat ` (점 없음) 이후 전부 제거한다."""
    assert _primary_artist("Eminem feat Rihanna") == "Eminem"


def test_primary_artist_featuring():
    """`featuring` 이후 전부 제거한다."""
    assert _primary_artist("Eminem featuring Dido") == "Eminem"


def test_primary_artist_ft_dot():
    """`ft.` 이후 전부 제거한다."""
    assert _primary_artist("Drake ft. Future") == "Drake"


def test_primary_artist_ft_no_dot():
    """`ft ` (점 없음) 이후 전부 제거한다."""
    assert _primary_artist("Drake ft Future") == "Drake"


def test_primary_artist_comma():
    """쉼표 이후 전부 제거한다."""
    assert _primary_artist("Eminem, DJ Haze & DJ Capcom") == "Eminem"


def test_primary_artist_feat_then_comma():
    """`feat.` 제거 후 남은 쉼표도 제거된다."""
    assert _primary_artist("Artist feat. Guest, Another") == "Artist"


def test_primary_artist_case_insensitive():
    """대소문자 무시하고 패턴을 제거한다."""
    assert _primary_artist("Eminem FEAT. Nate Dogg") == "Eminem"
    assert _primary_artist("Eminem Featuring Dido") == "Eminem"
    assert _primary_artist("Eminem FT. Rihanna") == "Eminem"


def test_primary_artist_no_feat():
    """피처링 표기가 없으면 원본 그대로 반환한다."""
    assert _primary_artist("Radiohead") == "Radiohead"


def test_primary_artist_earth_wind_fire():
    """`Earth, Wind & Fire`는 feat./ft. 패턴이 없어 쉼표 이후가 제거된다.

    `Earth`만 남는 것이 현재 구현의 의도된 동작이다 (쉼표 패턴 마지막 적용).
    """
    assert _primary_artist("Earth, Wind & Fire") == "Earth"


def test_primary_artist_strips_whitespace():
    """앞뒤 공백을 strip 한다."""
    assert _primary_artist("  Eminem feat. Rihanna  ") == "Eminem"


def test_primary_artist_empty_string():
    """빈 문자열 입력 시 빈 문자열을 반환한다."""
    assert _primary_artist("") == ""


def test_tag_and_import_uses_primary_artist_for_path(tmp_path, monkeypatch):
    """tag_and_import가 파일 경로 결정 시 _primary_artist를 적용한다."""
    flac_path = tmp_path / "test.flac"
    _make_minimal_flac(flac_path)

    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording", lambda a, t: ([], "", "")
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("The Marshall Mathers LP", "", ""),
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="Eminem feat. Nate Dogg",
        track_name="'Till I Collapse",
    )

    assert success is True
    dest_path = Path(dest)
    # 폴더명은 피처링 제거 후 "Eminem" 이어야 한다
    assert "Eminem" in dest_path.parts
    # "Eminem feat. Nate Dogg" 전체가 폴더명이 되면 안 된다
    assert not any("feat" in part for part in dest_path.parts)
    assert dest_path.exists()


def test_tag_and_import_uses_canonical_artist_for_path(tmp_path, monkeypatch):
    """_enrich_track이 canonical_artist를 반환하면, 그 이름이 폴더명으로 사용된다.

    예: 요청 아티스트 'iu' → iTunes canonical 'IU' → 폴더 'IU'
    MB 검색 실패 시 iTunes/Deezer canonical_artist가 2순위로 사용된다.
    """
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording", lambda a, t: ([], "", "")
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("삐삐", "IU", ""),
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="iu",
        track_name="삐삐",
    )

    assert success is True
    dest_path = Path(dest)
    # 정규화된 canonical_artist 'IU'가 폴더명이 되어야 한다
    assert "IU" in dest_path.parts
    # 원본 요청 아티스트 'iu'가 폴더명이 되면 안 된다
    assert "iu" not in dest_path.parts
    assert dest_path.exists()


def test_tag_and_import_falls_back_to_original_artist_when_no_canonical(
    tmp_path, monkeypatch
):
    """_enrich_track이 canonical_artist를 빈 문자열로 반환하면, 원본 아티스트명을 사용한다."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording", lambda a, t: ([], "", "")
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("Unknown Album", "", ""),
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="아이유",
        track_name="우울시계",
    )

    assert success is True
    dest_path = Path(dest)
    # canonical이 없으므로 원본 '아이유'가 폴더명이 되어야 한다
    assert "아이유" in dest_path.parts
    assert dest_path.exists()


def test_tag_and_import_canonical_artist_feat_stripped(tmp_path, monkeypatch):
    """canonical_artist에도 피처링 표기가 있으면 _primary_artist 적용으로 제거된다."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording", lambda a, t: ([], "", "")
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("Some Album", "Eminem feat. Dr. Dre", ""),
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="Eminem",
        track_name="Forgot About Dre",
    )

    assert success is True
    dest_path = Path(dest)
    assert "Eminem" in dest_path.parts
    assert not any("feat" in part for part in dest_path.parts)
    assert dest_path.exists()


# ── tag_and_import: canonical_title 파일명 반영 ───────────────────────────────


def test_tag_and_import_uses_canonical_title_for_filename(tmp_path, monkeypatch):
    """_enrich_track이 canonical_title을 반환하면 파일명에 canonical_title이 사용된다.

    예: 요청 track '삐삐' → iTunes canonical 'Bbibbi' → 파일명 'Bbibbi.flac'
    """
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording", lambda a, t: ([], "", "")
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("Palette", "IU", "Bbibbi"),
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="iu",
        track_name="삐삐",
    )

    assert success is True
    dest_path = Path(dest)
    assert dest_path.stem == "Bbibbi"
    assert dest_path.exists()


def test_tag_and_import_falls_back_to_original_track_when_no_canonical_title(
    tmp_path, monkeypatch
):
    """canonical_title이 빈 문자열이면 원본 요청 track_name을 파일명으로 사용한다."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording", lambda a, t: ([], "", "")
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("Unknown Album", "", ""),
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="아이유",
        track_name="삐삐",
    )

    assert success is True
    dest_path = Path(dest)
    assert dest_path.stem == "삐삐"
    assert dest_path.exists()


def test_tag_and_import_canonical_title_applied_with_sanitize(tmp_path, monkeypatch):
    """canonical_title에 파일시스템 특수문자가 있으면 sanitize_path_component가 적용된다."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording", lambda a, t: ([], "", "")
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("Some Album", "Artist", "Title: Subtitle"),
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="Artist",
        track_name="original",
    )

    assert success is True
    dest_path = Path(dest)
    assert ":" not in dest_path.stem
    assert dest_path.exists()


# ── MB artist-credit name을 1순위 canonical_artist로 사용 ─────────────────────


def test_tag_and_import_uses_mb_artist_name_over_itunes(tmp_path, monkeypatch):
    """MB artist-credit name이 있으면 iTunes/Deezer canonical_artist보다 우선하여 폴더명으로 사용된다.

    예: 요청 아티스트 '아이유' → MB artist-credit 'IU' → 폴더 'IU'
    (iTunes canonical도 '아이유'를 반환할 수 있지만 MB가 1순위)
    """
    flac_path = _make_flac(tmp_path)
    # MB 검색 결과에 mb_artist_name="IU" 포함
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: (["some-recording-id"], "IU", ""),
    )
    # _enrich_track은 iTunes canonical_artist "아이유"를 반환한다고 가정
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("밤편지", "아이유", "Through the Night"),
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="아이유",
        track_name="밤편지",
    )

    assert success is True
    dest_path = Path(dest)
    # MB artist-credit "IU"가 폴더명이 되어야 한다
    assert "IU" in dest_path.parts
    # iTunes canonical "아이유"가 폴더명이 되면 안 된다
    assert "아이유" not in dest_path.parts
    assert dest_path.exists()


def test_tag_and_import_mb_artist_empty_falls_back_to_itunes_canonical(
    tmp_path, monkeypatch
):
    """MB artist name이 빈 문자열이면 iTunes/Deezer canonical_artist(2순위)를 폴더명으로 사용한다."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: ([], "", ""),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("Palette", "IU", ""),
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="iu",
        track_name="Palette",
    )

    assert success is True
    dest_path = Path(dest)
    assert "IU" in dest_path.parts
    assert "iu" not in dest_path.parts
    assert dest_path.exists()


def test_mb_search_recording_returns_artist_name_in_strict_path(monkeypatch):
    """strict 검색 성공 시 artist-credit name이 두 번째 반환값으로 포함된다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "recordings": [
                {
                    "id": "iu-strict-001",
                    "title": "밤편지",
                    "artist-credit": [{"artist": {"name": "IU", "sort-name": "IU"}}],
                }
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    result, mb_artist, mb_title = mb_search_recording("아이유", "밤편지")
    assert "iu-strict-001" in result
    assert mb_artist == "IU"


def test_mb_search_recording_returns_empty_artist_name_on_failure(monkeypatch):
    """모든 검색 단계 실패 시 ([], '') 를 반환한다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {"recordings": []}
        return resp

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    result, mb_artist, mb_title = mb_search_recording("NoArtist", "NoTrack")
    assert result == []
    assert mb_artist == ""


# ── tag_and_import: canonical artist/title 태그 덮어쓰기 ──────────────────────


def test_tag_and_import_writes_canonical_artist_to_file_tag(tmp_path, monkeypatch):
    """canonical artist name이 파일의 artist 태그에 기록된다.

    예: 요청 'iu' → MB artist-credit 'IU' → 파일 태그 artist='IU'
    """
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: (["some-id"], "IU", ""),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("Palette", "IU", "Palette"),
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="iu",
        track_name="palette",
    )

    assert success is True
    tags = _read_tags(dest)
    assert tags["artist"] == "IU"


def test_tag_and_import_writes_canonical_title_to_file_tag(tmp_path, monkeypatch):
    """canonical title이 파일의 title 태그에 기록된다.

    예: 요청 '삐삐' → iTunes canonical 'Bbibbi' → 파일 태그 title='Bbibbi'
    """
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: ([], "", ""),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("IU", "IU", "Bbibbi"),
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="iu",
        track_name="삐삐",
    )

    assert success is True
    tags = _read_tags(dest)
    assert tags["title"] == "Bbibbi"


def test_tag_and_import_falls_back_to_original_artist_in_tag_when_no_canonical(
    tmp_path, monkeypatch
):
    """canonical artist가 없으면 원본 요청 artist가 파일 태그에 기록된다."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: ([], "", ""),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("Unknown Album", "", ""),
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="아이유",
        track_name="밤편지",
    )

    assert success is True
    tags = _read_tags(dest)
    assert tags["artist"] == "아이유"


def test_tag_and_import_falls_back_to_original_title_in_tag_when_no_canonical(
    tmp_path, monkeypatch
):
    """canonical title이 없으면 원본 요청 track_name이 파일 태그에 기록된다."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: ([], "", ""),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("Unknown Album", "", ""),
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="아이유",
        track_name="밤편지",
    )

    assert success is True
    tags = _read_tags(dest)
    assert tags["title"] == "밤편지"


def test_tag_and_import_artist_tag_uses_full_name_not_primary_artist(
    tmp_path, monkeypatch
):
    """파일 artist 태그에는 feat. 포함 전체 canonical name이 기록된다.

    _primary_artist()는 폴더 경로 구성에만 적용되고 태그에는 영향을 주지 않는다.
    """
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: (["some-id"], "Eminem feat. Nate Dogg", ""),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("8 Mile", "Eminem feat. Nate Dogg", "Till I Collapse"),
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="Eminem feat. Nate Dogg",
        track_name="Till I Collapse",
    )

    assert success is True
    tags = _read_tags(dest)
    # 태그에는 전체 이름, 폴더에는 _primary_artist 적용 후 "Eminem"
    assert tags["artist"] == "Eminem feat. Nate Dogg"
    assert "Eminem" in Path(dest).parts


# ── MB recording title canonical_title 2순위 동작 ────────────────────────────


def test_mb_search_recording_returns_recording_title(monkeypatch):
    """strict 검색 성공 시 best recording의 title이 세 번째 반환값으로 포함된다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "recordings": [
                {
                    "id": "iu-strict-001",
                    "title": "Through the Night",
                    "artist-credit": [{"artist": {"name": "IU", "sort-name": "IU"}}],
                }
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    # "Through the Night" 검색 — recording title과 일치해야 candidates가 생성됨
    result, mb_artist, mb_title = mb_search_recording("IU", "Through the Night")
    assert "iu-strict-001" in result
    assert mb_artist == "IU"
    assert mb_title == "Through the Night"


def test_mb_search_recording_returns_empty_title_on_failure(monkeypatch):
    """검색 실패 시 세 번째 반환값이 빈 문자열이다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {"recordings": []}
        return resp

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    result, mb_artist, mb_title = mb_search_recording("NoArtist", "NoTrack")
    assert result == []
    assert mb_title == ""


def test_enrich_track_uses_mb_recording_title_when_itunes_fails(tmp_path, monkeypatch):
    """iTunes가 title을 반환 못 할 때 MB recording title이 canonical_title 2순위로 사용된다."""
    flac_path = _make_flac(tmp_path)
    _write_tags(str(flac_path), "Artist", "Track")

    monkeypatch.setattr(
        "src.pipeline.tagger.itunes_search",
        lambda a, t, **kw: {},
    )
    monkeypatch.setattr(
        "src.pipeline.tagger.deezer_search",
        lambda a, t: {
            "album": "Some Album",
            "artwork_url": "",
            "artistName": "Artist",
            "trackName": "Deezer Title",
        },
    )

    album, canonical_artist, canonical_title = _enrich_track(
        str(flac_path),
        artist="Artist",
        track_name="Track",
        yt_metadata=None,
        recording_ids=None,
        mb_recording_title="MB Title",
    )

    assert canonical_title == "MB Title"


def test_enrich_track_itunes_title_beats_mb_recording_title(tmp_path, monkeypatch):
    """iTunes trackName이 있으면 MB recording title보다 우선한다."""
    flac_path = _make_flac(tmp_path)
    _write_tags(str(flac_path), "Artist", "Track")

    monkeypatch.setattr(
        "src.pipeline.tagger.itunes_search",
        lambda a, t, **kw: {
            "album": "iTunes Album",
            "artwork_url": "",
            "artistName": "Artist",
            "trackName": "iTunes Title",
        },
    )

    album, canonical_artist, canonical_title = _enrich_track(
        str(flac_path),
        artist="Artist",
        track_name="Track",
        yt_metadata=None,
        recording_ids=None,
        mb_recording_title="MB Title",
    )

    assert canonical_title == "iTunes Title"


def test_tag_and_import_writes_mb_recording_title_when_itunes_fails(
    tmp_path, monkeypatch
):
    """MB recording title이 iTunes 실패 시 파일 title 태그에 기록된다."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: (["some-id"], "IU", "Through the Night"),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("밤편지", "IU", "Through the Night"),
    )

    music_dir = tmp_path / "music"
    success, dest, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="아이유",
        track_name="밤편지",
    )

    assert success is True
    tags = _read_tags(dest)
    assert tags["title"] == "Through the Night"


# ── tag_and_import: canonical artist/title 반환값 검증 ────────────────────────


def test_tag_and_import_returns_canonical_artist_and_title(tmp_path, monkeypatch):
    """tag_and_import가 6-tuple을 반환하며, canonical_artist, canonical_title, canonical_album이 포함된다."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: (["some-id"], "IU", "Through the Night"),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("밤편지", "IU", "Through the Night"),
    )

    music_dir = tmp_path / "music"
    result = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="아이유",
        track_name="밤편지",
    )

    assert len(result) == 6
    (
        success,
        dest,
        canonical_artist,
        canonical_title,
        canonical_album,
        mb_recording_id,
    ) = result
    assert success is True
    assert canonical_artist == "IU"
    assert canonical_title == "Through the Night"
    assert canonical_album == "밤편지"
    assert mb_recording_id == "some-id"


def test_tag_and_import_returns_empty_canonical_on_file_not_found(tmp_path):
    """staging 파일이 없으면 canonical_artist, canonical_title, canonical_album, mb_recording_id도 빈 문자열로 반환한다."""
    missing = tmp_path / "missing.flac"
    (
        success,
        dest,
        canonical_artist,
        canonical_title,
        canonical_album,
        mb_recording_id,
    ) = tag_and_import(
        str(missing),
        music_dir=str(tmp_path / "music"),
    )
    assert success is False
    assert dest == ""
    assert canonical_artist == ""
    assert canonical_title == ""
    assert canonical_album == ""
    assert mb_recording_id == ""


def test_tag_and_import_returns_canonical_artist_for_duplicate(tmp_path, monkeypatch):
    """이미 파일이 존재하는 duplicate 케이스에서도 canonical 정보가 반환된다."""
    flac_path = _make_flac(tmp_path)
    monkeypatch.setattr(
        "src.pipeline.tagger.mb_search_recording",
        lambda a, t: (["some-id"], "Radiohead", "Creep"),
    )
    monkeypatch.setattr(
        "src.pipeline.tagger._enrich_track",
        lambda *args, **kwargs: ("Pablo Honey", "Radiohead", "Creep"),
    )

    music_dir = tmp_path / "music"
    # 첫 번째 import
    success1, dest1, c_artist1, c_title1, *_ = tag_and_import(
        str(flac_path),
        music_dir=str(music_dir),
        artist="radiohead",
        track_name="creep",
    )
    assert success1 is True
    assert c_artist1 == "Radiohead"
    assert c_title1 == "Creep"

    # 두 번째 import: duplicate 경로
    flac_path2 = _make_flac(tmp_path, name="test2.flac")
    success2, dest2, c_artist2, c_title2, *_ = tag_and_import(
        str(flac_path2),
        music_dir=str(music_dir),
        artist="radiohead",
        track_name="creep",
    )
    assert success2 is True
    assert c_artist2 == "Radiohead"
    assert c_title2 == "Creep"


# ── _mb_lookup_artist_ids 테스트 ──────────────────────────────────────────────


def test_mb_lookup_artist_ids_returns_ids(monkeypatch):
    """MB artist API 정상 응답 시 artist MBID 목록을 반환한다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "artists": [
                {"id": "arid-001", "name": "Junho"},
                {"id": "arid-002", "name": "2PM"},
                {"id": "arid-003", "name": "Jun. K"},
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    result = _mb_lookup_artist_ids("준호")
    assert result == ["arid-001", "arid-002", "arid-003"]


def test_mb_lookup_artist_ids_returns_empty_on_failure(monkeypatch):
    """HTTP 오류 발생 시 빈 리스트를 반환한다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status.side_effect = Exception("500 Server Error")
        return resp

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    result = _mb_lookup_artist_ids("준호")
    assert result == []


def test_mb_lookup_artist_ids_skips_entries_without_id(monkeypatch):
    """id 필드가 없는 artist 항목은 결과에서 제외된다."""

    def fake_get(url, params=None, headers=None, timeout=10):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "artists": [
                {"id": "arid-001", "name": "Junho"},
                {"name": "No ID Artist"},  # id 없음
            ]
        }
        return resp

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    result = _mb_lookup_artist_ids("준호")
    assert result == ["arid-001"]


# ── _mb_search_recording: Stage 2.5 arid 기반 매칭 테스트 ─────────────────────


def test_mb_search_recording_stage25_matches_via_arid(monkeypatch):
    """stage 1(strict), stage 2(plain) 모두 실패하면 stage 2.5(arid 기반 검색)로 매칭한다.
    호출 순서: 1=strict(empty), 2=plain(empty), 3=artist lookup, 4=arid recording search(data).
    """
    call_count = [0]

    def fake_get(url, params=None, headers=None, timeout=10):
        call_count[0] += 1
        resp = MagicMock()
        resp.raise_for_status = lambda: None

        if "/artist" in url:
            # stage 2.5: artist ID 조회
            resp.json.return_value = {"artists": [{"id": "junho-arid-001"}]}
        elif call_count[0] <= 2:
            # stage 1, 2: recording 검색 (empty)
            resp.json.return_value = {"recordings": []}
        else:
            # stage 2.5: arid 기반 recording 검색
            resp.json.return_value = {
                "recordings": [
                    {
                        "id": "junho-rec-001",
                        "title": "해야 (The Day)",
                        "artist-credit": [
                            {
                                "artist": {"name": "Junho"},
                                "joinphrase": "",
                            }
                        ],
                    }
                ]
            }
        return resp

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    result, mb_artist, mb_title = mb_search_recording("준호", "해야 (The Day)")
    assert result == ["junho-rec-001"]
    assert mb_artist == "Junho"
    assert mb_title == "해야 (The Day)"


def test_mb_search_recording_stage25_skips_low_title_similarity(monkeypatch):
    """stage 2.5에서 recording title 유사도 0.4 미만인 결과는 건너뛴다."""
    call_count = [0]
    artist_lookup_called = [False]

    def fake_get(url, params=None, headers=None, timeout=10):
        call_count[0] += 1
        resp = MagicMock()
        resp.raise_for_status = lambda: None

        if "/artist" in url:
            artist_lookup_called[0] = True
            resp.json.return_value = {"artists": [{"id": "some-arid-001"}]}
        elif call_count[0] <= 2:
            resp.json.return_value = {"recordings": []}
        else:
            # title 유사도가 0.4 미만인 recording만 반환
            resp.json.return_value = {
                "recordings": [
                    {
                        "id": "wrong-rec-001",
                        "title": "완전히 다른 곡",
                        "artist-credit": [
                            {"artist": {"name": "Junho"}, "joinphrase": ""}
                        ],
                    }
                ]
            }
        return resp

    monkeypatch.setattr("src.pipeline.musicbrainz.requests.get", fake_get)
    monkeypatch.setattr("src.pipeline.musicbrainz.time.sleep", lambda s: None)

    # stage 2.5가 실패하면 stage 3(recording-only)으로 넘어가 최종 빈 리스트 반환
    result, mb_artist, mb_title = mb_search_recording("준호", "해야")
    # stage 2.5에서 아무것도 매칭되지 않고 stage 3도 빈 결과 → 빈 리스트
    assert result == []
    assert artist_lookup_called[0] is True
