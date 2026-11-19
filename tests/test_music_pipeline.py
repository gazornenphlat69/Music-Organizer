from __future__ import annotations

import os
import re
import time
from collections import defaultdict
from pathlib import Path

import pytest

import music_pipeline as mp


# ---------------------------------------------------------------- tag/path helpers

def test_sanitize_folder_name_removes_windows_invalid_characters():
    assert mp.sanitize_folder_name("AC/DC: Live?") == "ACDC Live"
    assert mp.sanitize_folder_name('a<b>c|d*e"f?g') == "abcdefg"


def test_sanitize_folder_name_strips_trailing_dots_and_spaces():
    assert mp.sanitize_folder_name("Album... ") == "Album"
    assert mp.sanitize_folder_name("  ...  ") == "Unknown"
    assert mp.sanitize_folder_name("") == "Unknown"


@pytest.mark.parametrize(("value", "expected"), [
    ((3, 12), 3),
    (["4/9"], 4),
    ("07/12", 7),
    ("12", 12),
    (None, None),
    ("not-a-number", None),
])
def test_parse_int(value, expected):
    assert mp.parse_int(value) == expected


@pytest.mark.parametrize(("value", "expected"), [
    (("3", "12"), 12),
    (["4/9"], 9),
    ("4/9", 9),
    ("4", None),
    (None, None),
])
def test_parse_total(value, expected):
    assert mp.parse_total(value) == expected


def test_quality_score_prefers_lossless_then_bitrate():
    flac = mp.quality_score({"extension": ".flac", "bitrate": 900})
    m4a = mp.quality_score({"extension": ".m4a", "bitrate": 256})
    mp3 = mp.quality_score({"extension": ".mp3", "bitrate": 320})
    assert flac > m4a > mp3
    assert mp.quality_score({"extension": ".flac", "bitrate": 1000}) > flac


# ---------------------------------------------------------------- completeness logic

def test_find_track_gap_reports_the_missing_track():
    assert mp.find_track_gap({1: {"tracks": {1, 2, 4}}}) == (True, 1, [3])


def test_find_track_gap_ignores_contiguous_and_single_track_discs():
    assert mp.find_track_gap({1: {"tracks": {1, 2, 3}}}) == (False, None, None)
    assert mp.find_track_gap({1: {"tracks": {7}}}) == (False, None, None)


def test_find_track_gap_checks_each_disc():
    has_gap, disc, missing = mp.find_track_gap({
        1: {"tracks": {1, 2}},
        2: {"tracks": {1, 3}},
    })
    assert (has_gap, disc, missing) == (True, 2, [2])


def test_verify_via_tags_uses_expected_totals():
    assert mp.verify_via_tags({1: {"tracks": {1, 2}, "expected_total": 2}})
    assert not mp.verify_via_tags({1: {"tracks": {1, 2}, "expected_total": 3}})


def test_verify_via_tags_without_totals_requires_a_full_run():
    assert mp.verify_via_tags({1: {"tracks": {1, 2, 3}, "expected_total": None}})
    assert not mp.verify_via_tags({1: {"tracks": {1, 3}, "expected_total": None}})


def test_assess_matches_disc_and_track_counts():
    discs = {1: {"tracks": {1, 2, 3}}}
    assert mp.assess({1: 3}, discs) == "complete"
    assert mp.assess({1: 4}, discs) == "incomplete"
    assert mp.assess({1: 2}, discs) is None          # folder has too many tracks
    assert mp.assess({1: 3, 2: 2}, discs) is None    # wrong number of discs


def test_missing_tracks_lists_only_gaps():
    assert mp.missing_tracks({1: 4}, {1: {"tracks": {1, 2, 4}}}) == {1: [3]}
    assert mp.missing_tracks({1: 3}, {1: {"tracks": {1, 2, 3}}}) == {}


def test_summarize_builds_disc_map_and_common_metadata():
    tagged = [
        (Path("01.flac"), {"disc": 1, "track": 1, "album_artist": "Artist", "album": "Album",
                          "title": "One", "total_tracks": 2, "mb_albumid": "mbid-1"}),
        (Path("02.flac"), {"disc": 1, "track": 2, "album_artist": "Artist", "album": "Album",
                          "title": "Two", "total_tracks": 2, "mb_albumid": "mbid-1"}),
    ]
    info = mp.summarize(tagged)
    assert info["discs"] == {1: {"tracks": {1, 2}, "expected_total": 2}}
    assert info["artist"] == "Artist"
    assert info["album"] == "Album"
    assert info["mbid"] == "mbid-1"
    assert info["titles"] == ["One", "Two"]


# ---------------------------------------------------------------- safe folder cleanup

def test_remove_empty_folders_only_removes_empty_directories(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mp, "DRY_RUN", False)
    source = tmp_path / "source"
    source.mkdir()

    empty = source / "empty"
    empty.mkdir()
    nested = source / "nested" / "deeper"
    nested.mkdir(parents=True)

    wav_album = source / "wav-album"
    wav_album.mkdir()
    (wav_album / "track.wav").write_bytes(b"not deleted")

    album = source / "album"
    scans = album / "Scans"
    scans.mkdir(parents=True)
    (scans / "cover.jpg").write_bytes(b"artwork")

    mp.remove_empty_folders(source)

    assert not empty.exists()
    assert not (source / "nested").exists()
    assert (wav_album / "track.wav").exists()
    assert (scans / "cover.jpg").exists()
    assert "Removed 3 empty folder(s)." in capsys.readouterr().out


def test_remove_empty_folders_is_a_no_op_in_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(mp, "DRY_RUN", True)
    source = tmp_path / "source"
    empty = source / "empty"
    empty.mkdir(parents=True)

    mp.remove_empty_folders(source)

    assert empty.exists()


# ---------------------------------------------------------------- pipeline lock

def test_pipeline_lock_is_exclusive_and_reusable(tmp_path):
    path = tmp_path / "pipeline.lock"
    first, second = mp.PipelineLock(path), mp.PipelineLock(path)
    first.acquire()
    try:
        with pytest.raises(mp.PipelineBusy):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    try:
        assert path.exists()
    finally:
        second.release()


def test_pipeline_lock_treats_empty_lock_as_stale(tmp_path):
    # A crash between creating the lock file and writing the PID leaves an
    # empty file that used to block every later run forever.
    path = tmp_path / "pipeline.lock"
    path.write_text("")
    old = time.time() - 60
    os.utime(path, (old, old))
    lock = mp.PipelineLock(path)
    lock.acquire()
    try:
        assert path.exists()
    finally:
        lock.release()


def test_pipeline_lock_treats_garbage_lock_as_stale(tmp_path):
    path = tmp_path / "pipeline.lock"
    path.write_text("not-a-pid")
    old = time.time() - 60
    os.utime(path, (old, old))
    lock = mp.PipelineLock(path)
    lock.acquire()
    try:
        assert path.exists()
    finally:
        lock.release()


def test_pipeline_lock_does_not_clear_fresh_empty_lock(tmp_path):
    # Just created: a live process may still be about to write its PID, so a
    # fresh empty lock must still refuse - and the message names the file.
    path = tmp_path / "pipeline.lock"
    path.write_text("")
    with pytest.raises(mp.PipelineBusy) as excinfo:
        mp.PipelineLock(path).acquire()
    assert str(path) in str(excinfo.value)


# ---------------------------------------------------------------- --trash routing

def test_act_routes_deletes_to_trash_when_enabled(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(mp, "send2trash", lambda p: calls.append(p))
    monkeypatch.setattr(mp, "USE_TRASH", True)
    target = tmp_path / "gone.txt"
    target.write_text("x")
    mp.act("delete gone.txt", os.remove, target)
    assert calls == [str(target)]
    assert target.exists()  # the stub records the call; it did not delete


def test_act_refuses_permanent_delete_when_trash_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(mp, "send2trash", None)
    monkeypatch.setattr(mp, "USE_TRASH", True)
    target = tmp_path / "gone.txt"
    target.write_text("x")
    with pytest.raises(RuntimeError):
        mp.act("delete gone.txt", os.remove, target)
    assert target.exists()


def test_act_still_deletes_permanently_without_trash(tmp_path, monkeypatch):
    monkeypatch.setattr(mp, "USE_TRASH", False)
    target = tmp_path / "gone.txt"
    target.write_text("x")
    mp.act("delete gone.txt", os.remove, target)
    assert not target.exists()


# ---------------------------------------------------------------- version

def test_version_is_single_source_of_truth():
    pyproject = Path(mp.__file__).with_name("pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
    assert declared == mp.__version__
    assert mp.MB_APP_VERSION == mp.__version__


# ---------------------------------------------------------------- multi-disc grouping

def _fake_disc_tags(folder_name: str, disc: int, album: str = "Album") -> dict:
    return {"disc": disc, "track": 1, "artist": "Artist", "album_artist": "Artist",
            "album": album, "title": f"{folder_name} track"}


def test_iter_album_groups_combines_sibling_disc_folders(tmp_path, monkeypatch):
    album = tmp_path / "Album"
    cd1, cd2 = album / "CD1", album / "CD2"
    cd1.mkdir(parents=True)
    cd2.mkdir()
    first, second = cd1 / "01.flac", cd2 / "01.flac"
    first.write_bytes(b"x")
    second.write_bytes(b"x")

    tags = {first: _fake_disc_tags("CD1", 1), second: _fake_disc_tags("CD2", 2)}
    monkeypatch.setattr(mp, "extract_tags", lambda p: tags[p])

    groups = list(mp.iter_album_groups([(cd1, [first]), (cd2, [second])]))

    assert len(groups) == 1
    root, sources, tagged = groups[0]
    assert root == album
    assert {folder.name for folder, _ in sources} == {"CD1", "CD2"}
    assert len(tagged) == 2


def test_iter_album_groups_does_not_merge_different_albums(tmp_path, monkeypatch):
    parent = tmp_path / "Artist"
    first_dir, second_dir = parent / "Album One", parent / "Album Two"
    first_dir.mkdir(parents=True)
    second_dir.mkdir()
    first, second = first_dir / "01.flac", second_dir / "01.flac"
    first.write_bytes(b"x")
    second.write_bytes(b"x")

    tags = {first: _fake_disc_tags("Album One", 1, "Album One"),
            second: _fake_disc_tags("Album Two", 1, "Album Two")}
    monkeypatch.setattr(mp, "extract_tags", lambda p: tags[p])

    groups = list(mp.iter_album_groups([(first_dir, [first]), (second_dir, [second])]))

    assert len(groups) == 2


def test_iter_album_groups_does_not_merge_duplicate_disc_one(tmp_path, monkeypatch):
    parent = tmp_path / "Artist"
    copy_a, copy_b = parent / "Copy A", parent / "Copy B"
    copy_a.mkdir(parents=True)
    copy_b.mkdir()
    first, second = copy_a / "01.flac", copy_b / "01.flac"
    first.write_bytes(b"x")
    second.write_bytes(b"x")

    tags = {first: _fake_disc_tags("Copy A", 1), second: _fake_disc_tags("Copy B", 1)}
    monkeypatch.setattr(mp, "extract_tags", lambda p: tags[p])

    groups = list(mp.iter_album_groups([(copy_a, [first]), (copy_b, [second])]))

    assert len(groups) == 2


def test_process_folder_keeps_disc_folders_for_a_multi_disc_album(tmp_path, monkeypatch):
    album = tmp_path / "Album"
    cd1, cd2 = album / "CD1", album / "CD2"
    cd1.mkdir(parents=True)
    cd2.mkdir()
    first, second = cd1 / "01.flac", cd2 / "01.flac"
    first.write_bytes(b"x")
    second.write_bytes(b"x")

    tags = {first: _fake_disc_tags("CD1", 1), second: _fake_disc_tags("CD2", 2)}
    out = tmp_path / "incomplete"
    monkeypatch.setattr(mp, "extract_tags", lambda p: tags[p])
    monkeypatch.setattr(mp, "find_release", lambda *a, **k: {"result": None, "candidates": [],
                                                             "how": "test"})
    monkeypatch.setattr(mp, "verify_via_tags", lambda discs: False)
    monkeypatch.setattr(mp, "queue_for_review", lambda *a, **k: None)
    monkeypatch.setattr(mp, "DRY_RUN", False)
    monkeypatch.setattr(mp, "INCOMPLETE_DIR", out)

    stats = defaultdict(int)
    mp.process_folder(album, [first, second], stats,
                      tagged=[(first, tags[first]), (second, tags[second])],
                      sources=[(cd1, [first]), (cd2, [second])])

    assert (out / "Album" / "CD1" / "01.flac").exists()
    assert (out / "Album" / "CD2" / "01.flac").exists()
    assert stats["incomplete"] == 1


def test_scan_folder_finds_files_in_disc_subfolders(tmp_path, monkeypatch):
    album = tmp_path / "Album"
    cd1, cd2 = album / "CD1", album / "CD2"
    cd1.mkdir(parents=True)
    cd2.mkdir()
    first, second = cd1 / "01.flac", cd2 / "01.flac"
    first.write_bytes(b"x")
    second.write_bytes(b"x")

    tags = {first: _fake_disc_tags("CD1", 1), second: _fake_disc_tags("CD2", 2)}
    monkeypatch.setattr(mp, "extract_tags", lambda p: tags[p])

    info = mp.scan_folder(album)

    assert set(info["files"]) == {first, second}
    assert set(info["discs"]) == {1, 2}


# ---------------------------------------------------------------- stale partial sweep

def test_stale_partial_files_are_removed(tmp_path, monkeypatch):
    stale = tmp_path / "01 Song.partial.m4a"
    stale.write_bytes(b"half-written output")
    flac = tmp_path / "01 Song.flac"
    flac.write_bytes(b"audio")
    monkeypatch.setattr(mp, "DRY_RUN", False)

    found = mp._remove_stale_partials(tmp_path)

    assert found == 1
    assert not stale.exists()
    assert flac.exists()


def test_stale_partial_cleanup_respects_dry_run(tmp_path, monkeypatch):
    stale = tmp_path / "01 Song.partial.m4a"
    stale.write_bytes(b"half-written output")
    monkeypatch.setattr(mp, "DRY_RUN", True)

    found = mp._remove_stale_partials(tmp_path)

    assert found == 1
    assert stale.exists()          # dry run only reports


def test_stale_partial_cleanup_skips_protected_folders(tmp_path, monkeypatch):
    protected = tmp_path / "out"
    protected.mkdir()
    (protected / "x.partial.m4a").write_bytes(b"junk")
    monkeypatch.setattr(mp, "DRY_RUN", False)
    monkeypatch.setattr(mp, "PROTECTED_DIRS", (protected,))

    found = mp._remove_stale_partials(tmp_path)

    assert found == 0
    assert (protected / "x.partial.m4a").exists()


# ---------------------------------------------------------------- missing-disc detection

def _mb_release(cid, disc_map, score=95):
    return {"id": cid, "score": score, "title": "Album", "artist": "Artist",
            "date": "", "country": "", "format": "",
            "disc_map": {str(k): v for k, v in disc_map.items()}}


def test_find_release_flags_a_suspected_missing_disc(monkeypatch):
    discs = {1: {"tracks": {1, 2, 3}, "expected_total": 3},
             2: {"tracks": {1, 2, 3}, "expected_total": 3}}
    monkeypatch.setattr(mp, "search_candidates", lambda *a, **k: [_mb_release("rel-3", {1: 3, 2: 3, 3: 4})])
    monkeypatch.setattr(mp, "release_by_id", lambda mbid: None)

    m = mp.find_release("Artist", "Album", discs)

    assert m["result"] is False
    assert "suspected missing disc" in m["how"]
    assert "3" in m["how"]
    assert m["release"]["id"] == "rel-3"


def test_find_release_ignores_empty_extra_media(monkeypatch):
    discs = {1: {"tracks": {1, 2, 3}, "expected_total": 3},
             2: {"tracks": {1, 2, 3}, "expected_total": 3}}
    monkeypatch.setattr(mp, "search_candidates", lambda *a, **k: [_mb_release("rel-data", {1: 3, 2: 3, 3: 0})])
    monkeypatch.setattr(mp, "release_by_id", lambda mbid: None)

    m = mp.find_release("Artist", "Album", discs)

    assert m["result"] is None
    assert m["how"] == "no release fits"


def test_find_release_does_not_flag_single_disc_folders(monkeypatch):
    discs = {1: {"tracks": {1, 2, 3}, "expected_total": 3}}
    monkeypatch.setattr(mp, "search_candidates", lambda *a, **k: [_mb_release("rel-2", {1: 3, 2: 4})])
    monkeypatch.setattr(mp, "release_by_id", lambda mbid: None)

    m = mp.find_release("Artist", "Album", discs)

    assert m["result"] is None
    assert m["how"] == "no release fits"


# ---------------------------------------------------------------- dry-run conversion

def test_dry_run_does_not_prompt_for_single_flac_folders(tmp_path, monkeypatch, capsys):
    (tmp_path / "Whole Album.flac").write_bytes(b"audio")

    def fail_prompt(*a, **k):
        raise AssertionError("run_conversion must not prompt during a dry run")

    monkeypatch.setattr(mp, "DRY_RUN", True)
    monkeypatch.setattr(mp, "ask_choice", fail_prompt)

    mp.run_conversion(tmp_path, 1, non_interactive=False)

    out = capsys.readouterr().out
    assert "Auto: convert" in out
    assert (tmp_path / "Whole Album.flac").exists()
