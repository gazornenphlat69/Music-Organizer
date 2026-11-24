from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import toolkit_core as core

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def make_track(path: Path, title="Song", artist="Band", album="Album"):
    """Create a 2-second tone with artist/title/album tags, in the format given by the file extension."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
           "-metadata", f"title={title}", "-metadata", f"artist={artist}", "-metadata", f"album={album}",
           str(path)]
    subprocess.run(cmd, check=True, timeout=120)
    return path


SYNCED = "[00:01.00] first line\n[00:03.00] second line"
PLAIN = "first line\nsecond line"


@needs_ffmpeg
@pytest.mark.parametrize("ext", [".mp3", ".flac", ".m4a", ".opus"])
def test_write_then_read_round_trip(tmp_path, ext):
    track = make_track(tmp_path / f"track{ext}")

    assert core.read_existing_lyrics(track) is False
    core.write_lyrics(track, PLAIN)

    assert core.read_existing_lyrics(track) is True


@needs_ffmpeg
def test_write_replaces_existing_lyrics(tmp_path):
    track = make_track(tmp_path / "track.mp3")
    core.write_lyrics(track, PLAIN)
    core.write_lyrics(track, SYNCED)

    from mutagen.id3 import ID3
    frames = ID3(track).getall("USLT")
    assert len(frames) == 1
    assert frames[0].text == SYNCED


@needs_ffmpeg
def test_unsupported_format_is_refused(tmp_path):
    track = make_track(tmp_path / "track.wav")

    with pytest.raises(ValueError):
        core.write_lyrics(track, PLAIN)


def test_pick_prefers_synced_then_plain():
    hit = {"syncedLyrics": SYNCED, "plainLyrics": PLAIN}
    assert core.pick_lyrics_text(hit, prefer_synced=True) == (SYNCED, True)
    assert core.pick_lyrics_text(hit, prefer_synced=False) == (PLAIN, False)
    assert core.pick_lyrics_text({"plainLyrics": PLAIN}, prefer_synced=True) == (PLAIN, False)
    assert core.pick_lyrics_text({"syncedLyrics": SYNCED}, prefer_synced=False) == (SYNCED, True)
    assert core.pick_lyrics_text({"instrumental": True}, prefer_synced=True) == (None, False)


def test_lookup_uses_get_then_falls_back_to_search_with_duration_check(monkeypatch):
    calls = []

    def fake_json(path, params, timeout=15):
        calls.append((path, dict(params)))
        if path == "get":
            return None                       # 404: exact match not found
        return [
            {"id": 1, "duration": 200, "plainLyrics": "wrong length"},
            {"id": 2, "duration": 181, "plainLyrics": PLAIN},
        ]

    monkeypatch.setattr(core, "_lrclib_get_json", fake_json)
    hit = core.lrclib_lookup("Band", "Song", "Album", duration=180)

    assert hit["id"] == 2
    assert [c[0] for c in calls] == ["get", "search"]


def test_lookup_returns_none_without_artist_or_title(monkeypatch):
    monkeypatch.setattr(core, "_lrclib_get_json", lambda *a, **k: pytest.fail("should not call the network"))
    assert core.lrclib_lookup("", "Song") is None


def test_network_failure_is_not_treated_as_no_lyrics(monkeypatch):
    def boom(*a, **k):
        raise core.LyricsNetworkError("offline")

    monkeypatch.setattr(core, "_lrclib_get_json", boom)
    with pytest.raises(core.LyricsNetworkError):
        core.lrclib_lookup("Band", "Song")


@needs_ffmpeg
def test_scan_skips_existing_then_finds_missing(tmp_path):
    has = make_track(tmp_path / "a" / "has.mp3", title="Has")
    core.write_lyrics(has, PLAIN)
    make_track(tmp_path / "b" / "needs.mp3", title="Needs")

    def lookup(artist, title, album, duration):
        return {"plainLyrics": f"lyrics for {title}"}

    items, texts = core.lyrics_scan(tmp_path, lookup=lookup)
    by_name = {i["name"]: i for i in items}

    assert by_name["has.mp3"]["status"] == "has_lyrics"
    assert by_name["needs.mp3"]["status"] == "found"
    assert texts[by_name["needs.mp3"]["id"]] == "lyrics for Needs"
    assert by_name["has.mp3"]["id"] not in texts


@needs_ffmpeg
def test_scan_overwrite_replaces_and_flags_it(tmp_path):
    track = make_track(tmp_path / "track.mp3")
    core.write_lyrics(track, PLAIN)

    items, texts = core.lyrics_scan(tmp_path, overwrite=True, lookup=lambda *a: {"syncedLyrics": SYNCED})
    item = items[0]

    assert item["status"] == "found"
    assert item["replaces"] is True
    assert item["synced"] is True
    assert texts[item["id"]] == SYNCED


@needs_ffmpeg
def test_scan_reports_missing_tags_and_no_match(tmp_path):
    untagged = tmp_path / "x" / "untagged.mp3"
    untagged.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=1", str(untagged)],
                   check=True, timeout=120)
    make_track(tmp_path / "y" / "nomatch.mp3", title="Obscure")

    items, texts = core.lyrics_scan(tmp_path, lookup=lambda *a: None)
    by_name = {i["name"]: i for i in items}

    assert by_name["untagged.mp3"]["status"] == "not_found"
    assert "artist and title" in by_name["untagged.mp3"]["message"]
    assert by_name["nomatch.mp3"]["status"] == "not_found"
    assert texts == {}


@needs_ffmpeg
def test_scan_records_network_error_per_file(tmp_path):
    make_track(tmp_path / "t.mp3")

    def offline(*a):
        raise core.LyricsNetworkError("offline")

    items, texts = core.lyrics_scan(tmp_path, lookup=offline)

    assert items[0]["status"] == "error"
    assert "offline" in items[0]["message"]
    assert texts == {}


@needs_ffmpeg
def test_apply_dry_run_does_not_write(tmp_path):
    track = make_track(tmp_path / "t.mp3")
    items, texts = core.lyrics_scan(tmp_path, lookup=lambda *a: {"plainLyrics": PLAIN})

    done, errors = core.lyrics_apply(items, texts, dry_run=True)

    assert done == [items[0]["id"]]
    assert errors == []
    assert core.read_existing_lyrics(track) is False


@needs_ffmpeg
def test_apply_writes_and_logs(tmp_path):
    track = make_track(tmp_path / "t.mp3")
    items, texts = core.lyrics_scan(tmp_path, lookup=lambda *a: {"plainLyrics": PLAIN})
    logged = []

    class FakeHistory:
        def write(self, tab, action, detail):
            logged.append((tab, action, detail))

    done, errors = core.lyrics_apply(items, texts, dry_run=False, history=FakeHistory())

    assert done == [items[0]["id"]]
    assert errors == []
    assert core.read_existing_lyrics(track) is True
    assert logged == [("lyrics", "write", str(track))]


@needs_ffmpeg
def test_web_scan_then_dry_run_apply(tmp_path, monkeypatch):
    import toolkit_web

    track = make_track(tmp_path / "album" / "t.mp3", title="Song")
    monkeypatch.setattr(core, "_lrclib_get_json",
                        lambda path, params, timeout=15: {"plainLyrics": PLAIN, "duration": 2})
    client = toolkit_web.app.test_client()

    response = client.post("/api/lyrics/scan", json={"lyrics_folder": str(tmp_path)})
    assert response.status_code == 200
    toolkit_web.JOBS["lyrics"].thread.join(timeout=30)

    data = client.get("/api/lyrics").get_json()
    assert data["running"] is False
    assert [i["status"] for i in data["items"]] == ["found"]
    item_id = data["items"][0]["id"]

    response = client.post("/api/lyrics/apply", json={"ids": [item_id], "dry_run": True})
    assert response.status_code == 200
    toolkit_web.JOBS["lyrics"].thread.join(timeout=30)

    assert client.get("/api/lyrics").get_json()["apply"]["dry"] is True
    assert core.read_existing_lyrics(track) is False


def test_web_apply_refuses_when_nothing_scanned(tmp_path):
    import toolkit_web

    toolkit_web.LYRICS.update(items=[], texts={}, apply=None)
    response = toolkit_web.app.test_client().post("/api/lyrics/apply", json={"ids": ["0"]})

    assert response.status_code == 400
