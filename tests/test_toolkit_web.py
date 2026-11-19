from __future__ import annotations

import toolkit_web


def test_open_api_rejects_files(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(toolkit_web.os, "startfile", lambda path: calls.append(path), raising=False)

    target = tmp_path / "payload.exe"
    target.write_bytes(b"not really an executable")

    response = toolkit_web.app.test_client().post("/api/open", json={"path": str(target)})

    assert response.status_code == 400
    assert calls == []


def test_open_api_still_opens_folders(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(toolkit_web.sys, "platform", "win32")
    monkeypatch.setattr(toolkit_web.os, "startfile", lambda path: calls.append(path), raising=False)

    target = tmp_path / "album"
    target.mkdir()

    response = toolkit_web.app.test_client().post("/api/open", json={"path": str(target)})

    assert response.status_code == 200
    assert calls == [str(target)]


# ---------------------------------------------------------------- smoke tests

def test_web_app_state_endpoint():
    client = toolkit_web.app.test_client()
    response = client.get("/api/state")

    assert response.status_code == 200
    data = response.get_json()
    assert "jobs" in data
    assert "pipeline" in data
    assert set(data["jobs"]) == {"pipeline", "dupes", "health", "review", "plex"}


def test_web_app_settings_endpoint():
    client = toolkit_web.app.test_client()
    response = client.get("/api/settings")

    assert response.status_code == 200
    data = response.get_json()
    assert "settings" in data
    assert "capabilities" in data


def test_web_app_index_page():
    client = toolkit_web.app.test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b"Music Toolkit" in response.data


def test_pipeline_preflight_rejects_missing_folder(tmp_path):
    client = toolkit_web.app.test_client()
    response = client.post("/api/pipeline/preflight", json={"pipe_folder": str(tmp_path / "does-not-exist")})

    assert response.status_code == 200
    assert response.get_json()["errors"]


def test_settings_round_trip_filters_unknown_keys(tmp_path, monkeypatch):
    client = toolkit_web.app.test_client()
    resp = client.post("/api/settings", json={"history_on": False, "evil_key": "x", "mb_contact": "me@example.com"})
    assert resp.status_code == 200
    data = resp.get_json()["settings"]
    assert data["history_on"] is False
    assert data["mb_contact"] == "me@example.com"
    assert "evil_key" not in data


def test_settings_blank_plex_token_keeps_saved_token(tmp_path, monkeypatch):
    monkeypatch.setattr(toolkit_web.core, "SETTINGS_FILE", tmp_path / "music_toolkit_settings.json")
    monkeypatch.setitem(toolkit_web.SETTINGS, "plex_token", "saved-token")
    monkeypatch.setitem(toolkit_web.SETTINGS, "plex_remember_token", True)
    monkeypatch.setitem(toolkit_web.SETTINGS, "discogs_token", "saved-discogs")
    monkeypatch.setitem(toolkit_web.SETTINGS, "discogs_remember_token", True)
    client = toolkit_web.app.test_client()
    resp = client.post("/api/settings", json={"plex_token": "", "discogs_token": ""})
    assert resp.status_code == 200
    assert toolkit_web.SETTINGS["plex_token"] == "saved-token"
    assert toolkit_web.SETTINGS["discogs_token"] == "saved-discogs"
    assert resp.get_json()["settings"]["plex_token_set"] is True
    # a non-blank value still updates it
    resp = client.post("/api/settings", json={"plex_token": "new-token"})
    assert resp.status_code == 200
    assert toolkit_web.SETTINGS["plex_token"] == "new-token"


def test_fs_lists_directories(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "file.txt").write_text("x")
    client = toolkit_web.app.test_client()
    resp = client.get("/api/fs", query_string={"path": str(tmp_path)})
    assert resp.status_code == 200
    data = resp.get_json()
    names = [d["name"] for d in data["dirs"]]
    assert "alpha" in names and "beta" in names
    assert "file.txt" not in names
