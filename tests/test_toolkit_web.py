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


# ---------------------------------------------------------------- access token

def test_token_check_rejects_non_ascii_without_crashing(monkeypatch):
    monkeypatch.setattr(toolkit_web, "ACCESS_TOKEN", "abc123")
    response = toolkit_web.app.test_client().get("/api/state?token=t%C3%B6k%C3%A9n")

    assert response.status_code == 403


def test_token_check_accepts_correct_token_and_rejects_wrong(monkeypatch):
    monkeypatch.setattr(toolkit_web, "ACCESS_TOKEN", "abc123")
    client = toolkit_web.app.test_client()

    assert client.get("/api/state?token=abc123").status_code == 200
    assert client.get("/api/state?token=wrong").status_code == 403
    assert client.get("/api/state").status_code == 403


def test_index_moves_token_from_url_into_cookie(monkeypatch):
    monkeypatch.setattr(toolkit_web, "ACCESS_TOKEN", "abc123")
    response = toolkit_web.app.test_client().get("/?token=abc123")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")
    assert "token=" not in response.headers["Location"]
    assert "mt_token=abc123" in response.headers["Set-Cookie"]


def test_cookie_alone_is_enough_after_redirect(monkeypatch):
    monkeypatch.setattr(toolkit_web, "ACCESS_TOKEN", "abc123")
    client = toolkit_web.app.test_client()
    client.set_cookie("mt_token", "abc123")

    assert client.get("/").status_code == 200


# ---------------------------------------------------------------- job lock

def test_job_refuses_to_start_while_pipeline_lock_is_held():
    import music_pipeline as mp

    holder = mp.PipelineLock().acquire()
    try:
        job = toolkit_web.Job("dupes")
        try:
            job.start(lambda: None)
        except toolkit_web.JobBusy:
            pass
        else:
            raise AssertionError("expected JobBusy")
        assert not job.running
    finally:
        holder.release()


def test_job_releases_lock_when_it_finishes():
    import music_pipeline as mp

    job = toolkit_web.Job("plex")
    job.start(lambda: None)
    job.thread.join(timeout=5)

    lock = mp.PipelineLock().acquire()   # would raise PipelineBusy if the job still held it
    lock.release()


def test_dupes_scan_endpoint_returns_409_when_locked(tmp_path):
    import music_pipeline as mp

    holder = mp.PipelineLock().acquire()
    try:
        response = toolkit_web.app.test_client().post("/api/dupes/scan", json={"dupes_folder": str(tmp_path)})
        assert response.status_code == 409
    finally:
        holder.release()
