#!/usr/bin/env python3
"""
toolkit_web.py -- web front end for the music toolkit.

    python toolkit_web.py                 # http://127.0.0.1:8765 (this computer only)
    python toolkit_web.py --lan           # reachable from your phone/other PCs; prints a link with an access token
    python toolkit_web.py --port 9000

Same jobs as the desktop app (music_toolkit.py), same settings file, same history log. Both can be
open at once, but don't run the same job (e.g. two pipelines) from both at the same time.

Requires: pip install flask   (plus everything music_toolkit.py needs)
Files    : toolkit_web.py, toolkit_core.py, music_pipeline.py and the web/ folder, side by side.
"""

import argparse
import json
import logging
import os
import queue
import secrets
import socket
import string
import sys
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, Response, abort, jsonify, redirect, request, send_from_directory

import music_pipeline as mp
import toolkit_core as core

WEB_DIR = Path(__file__).with_name("web")
app = Flask(__name__, static_folder=None)
logging.getLogger("werkzeug").setLevel(logging.ERROR)   # request logs would land in the pipeline log

ACCESS_TOKEN = None          # set when listening beyond this computer


# =============================================================================
# Settings / history
# =============================================================================

_settings_lock = threading.Lock()
SETTINGS = {**core.default_settings(), **core.load_settings()}
HISTORY = core.History(core.Value(SETTINGS["history_dir"]), core.Value(SETTINGS["history_on"]))


def settings_update(values: dict) -> None:
    allowed = core.default_settings().keys() | {"last_tab"}
    with _settings_lock:
        for k, v in values.items():
            if k == "discogs_token" and not str(v or "").strip():
                continue                      # blank keeps the saved token; the browser never sees it
            if k in allowed:
                SETTINGS[k] = v
        core.save_settings(SETTINGS)
        HISTORY.folder_var.set(SETTINGS["history_dir"])
        HISTORY.enabled_var.set(SETTINGS["history_on"])
        HISTORY.snapshot()
        core.apply_paths_from(SETTINGS)


core.apply_paths_from(SETTINGS)
HISTORY.snapshot()


def capabilities() -> dict:
    import importlib.util
    import shutil
    plex = importlib.util.find_spec("plexapi") is not None
    return {"send2trash": core.send2trash is not None, "fingerprint": core.acoustid is not None,
            "plexapi": plex, "ffmpeg": shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None,
            "beets": shutil.which(mp.BEET_EXECUTABLE) is not None, "platform": sys.platform}


# =============================================================================
# Event hub (Server-Sent Events)
# =============================================================================

class Hub:
    """bus.post(kind, *payload) -> every connected browser. Rapid progress events are coalesced
    so a 6-worker conversion doesn't send hundreds of messages a second."""

    COALESCE = {"slot", "overall", "beets_progress", "dupes_progress", "health_progress",
                "review_progress", "plex_progress", "lyrics_progress"}

    def __init__(self):
        self.clients: list[queue.Queue] = []
        self.lock = threading.Lock()
        self.flush_lock = threading.Lock()
        self.pending: dict = {}
        threading.Thread(target=self._flusher, daemon=True).start()

    def post(self, kind, *payload):
        pipeline_state(kind, payload)
        if kind in self.COALESCE:
            key = (kind, payload[0]) if kind == "slot" else kind
            with self.lock:
                self.pending[key] = (kind, payload)
            return
        self._flush()            # queued progress must not arrive after a "done" message
        self._send(kind, payload)

    def _send(self, kind, payload):
        msg = json.dumps({"type": kind, "data": payload}, default=str)
        with self.lock:
            for q in list(self.clients):
                if q.qsize() < 5000:
                    q.put(msg)

    def _flush(self):
        with self.flush_lock:
            with self.lock:
                pending, self.pending = self.pending, {}
            for kind, payload in pending.values():
                self._send(kind, payload)

    def _flusher(self):
        while True:
            time.sleep(0.25)
            self._flush()

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)


HUB = Hub()


# =============================================================================
# Jobs
# =============================================================================

class JobBusy(RuntimeError):
    """Raised when a job can't start because another job holds the shared pipeline lock."""


class Job:
    def __init__(self, name):
        self.name, self.cancel, self.thread = name, threading.Event(), None
        self.progress = {"n": 0, "t": 1, "label": ""}
        self.post_done = name != "pipeline"      # run_pipeline posts its own pipeline_done

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, fn, *args):
        if self.running:
            raise RuntimeError(f"{self.name} is already running")
        # Every job except the pipeline takes the same lock run_pipeline uses, so a duplicate scan,
        # Plex apply or review can't overlap a pipeline run (CLI, desktop or web). The pipeline
        # takes the lock itself inside run_pipeline.
        lock = None
        if self.name != "pipeline":
            lock = mp.PipelineLock()
            try:
                lock.acquire()
            except mp.PipelineBusy as e:
                raise JobBusy(str(e)) from e
        self.cancel.clear()

        def wrap():
            err = None
            try:
                fn(*args)
            except Exception as e:
                import traceback
                traceback.print_exc(file=sys.__stderr__)
                err = repr(e)
            finally:
                if lock is not None:
                    lock.release()
            if self.post_done:
                HUB.post(f"{self.name}_done", err, self.cancel.is_set())
            elif err is not None:
                HUB.post(f"{self.name}_done", err)

        self.thread = threading.Thread(target=wrap, daemon=True)
        self.thread.start()

    def report(self, n, t, label=""):
        self.progress = {"n": n, "t": max(t, 1), "label": label}
        HUB.post(f"{self.name}_progress", n, max(t, 1), label)


JOBS = {n: Job(n) for n in ("pipeline", "dupes", "health", "review", "plex", "lyrics")}


def busy(name):
    return jsonify(error=f"{name.title()} is already running."), 409


def body() -> dict:
    return request.get_json(silent=True) or {}


# =============================================================================
# Pipeline
# =============================================================================

PIPE = {"log": [], "overall": None, "beets": None, "slots": {}, "slot_count": 0, "question": None,
        "last_result": None, "started": None}
_answers: dict[str, tuple[threading.Event, list]] = {}


def pipeline_state(kind, payload):
    """Keep a snapshot so a browser that opens mid-run sees the current state."""
    if kind == "log":
        PIPE["log"].append(payload[0])
        if len(PIPE["log"]) > 3000:
            del PIPE["log"][:1000]
    elif kind == "overall":
        PIPE["overall"], PIPE["beets"] = payload, None
    elif kind == "beets_progress":
        PIPE["beets"], PIPE["slot_count"], PIPE["slots"] = payload, 0, {}
    elif kind == "slots_init":
        PIPE["slot_count"], PIPE["slots"] = payload[0], {}
    elif kind == "slot":
        PIPE["slots"][payload[0]] = payload
    elif kind == "pipeline_done":
        PIPE["last_result"], PIPE["question"] = payload[0], None


def web_ask(folder: str) -> str:
    qid = secrets.token_hex(4)
    ev, holder = threading.Event(), []
    _answers[qid] = (ev, holder)
    PIPE["question"] = {"id": qid, "folder": folder}
    HUB.post("ask", PIPE["question"])
    job = JOBS["pipeline"]
    while not ev.wait(0.5):
        if job.cancel.is_set():
            holder.append("c")
            break
    _answers.pop(qid, None)
    PIPE["question"] = None
    HUB.post("ask_done", qid)
    return holder[0]


def _pipeline_opts(b):
    return dict(convert=bool(b.get("pipe_convert")), cleanup=bool(b.get("pipe_cleanup")),
                dry_run=bool(b.get("dry_run", True)), bitrate=b.get("pipe_bitrate") or "256k",
                run_beets=bool(b.get("pipe_beets")), contact=b.get("mb_contact") or "",
                workers=int(b.get("pipe_workers") or mp.DEFAULT_WORKERS),
                single_mode=b.get("pipe_single") or "ask",
                trash=bool(SETTINGS.get("pipe_trash")))


@app.post("/api/pipeline/preflight")
def pipeline_preflight():
    b = body()
    core.apply_paths_from(SETTINGS)
    errors, warnings = core.pipeline_preflight(Path(str(b.get("pipe_folder", "")).strip().strip('"')), _pipeline_opts(b))
    return jsonify(errors=errors, warnings=warnings)


@app.post("/api/pipeline/start")
def pipeline_start():
    job = JOBS["pipeline"]
    if job.running:
        return busy("pipeline")
    b = body()
    settings_update({k: v for k, v in b.items() if k != "dry_run"})
    folder = Path(str(b.get("pipe_folder", "")).strip().strip('"'))
    opts = _pipeline_opts(b)
    errors, _ = core.pipeline_preflight(folder, opts)
    if errors:
        return jsonify(error=" ".join(errors)), 400
    PIPE.update(log=[], overall=None, beets=None, slots={}, slot_count=0, last_result=None,
                started=time.time(), dry_run=opts["dry_run"])
    HUB.post("pipeline_started", opts["dry_run"])
    job.start(core.run_pipeline, folder.resolve(), opts, HUB, HISTORY, job.cancel, web_ask)
    return jsonify(ok=True)


@app.post("/api/pipeline/stop")
def pipeline_stop():
    JOBS["pipeline"].cancel.set()
    HUB.post("pipeline_stopping")
    return jsonify(ok=True)


@app.post("/api/pipeline/answer")
def pipeline_answer():
    b = body()
    ev_holder = _answers.get(b.get("id"))
    if not ev_holder:
        return jsonify(error="That question has already been answered."), 410
    ev, holder = ev_holder
    holder.append("c" if b.get("choice") == "c" else "m")
    ev.set()
    return jsonify(ok=True)


# =============================================================================
# Duplicates
# =============================================================================

DUPES = {"groups": [], "index": {}, "stopped": False, "error": None}


def _dupes_scan(root, order, mode, check, fp):
    job = JOBS["dupes"]
    res = core.find_duplicates(root, order, mode, check, fp,
                               progress=lambda n, t, l: job.report(n, t, l), cancel=job.cancel)
    groups, index = [], {}
    for g, (keep, rest) in enumerate(res):
        items = []
        for c, info in enumerate(rest):
            iid = f"g{g}c{c}"
            info["ticked"] = not info["suspect"]
            index[iid] = info
            items.append({"id": iid, "path": str(info["path"]), "name": info["path"].name,
                          "format": info["path"].suffix.lower(), "size": info["size"],
                          "duration": info["duration"], "note": info["note"], "suspect": info["suspect"]})
        groups.append({"id": f"g{g}", "keep": {"path": str(keep["path"]), "format": keep["path"].suffix.lower(),
                                               "size": keep["size"], "duration": keep["duration"]},
                       "items": items})
    DUPES.update(groups=groups, index=index, stopped=job.cancel.is_set(), error=None,
                 cross=(mode == "tags"), raw=res)


@app.post("/api/dupes/scan")
def dupes_scan():
    job = JOBS["dupes"]
    if job.running:
        return busy("duplicate scan")
    b = body()
    settings_update(b)
    root = Path(str(b.get("dupes_folder", "")).strip().strip('"'))
    if not root.is_dir():
        return jsonify(error="Pick a valid folder first."), 400
    fp = bool(b.get("dupes_fp")) and core.acoustid is not None
    job.start(_dupes_scan, root, core.PRIORITIES[SETTINGS["dupes_priority"]], core.MODES[SETTINGS["dupes_mode"]],
              bool(b.get("dupes_duration")), fp)
    return jsonify(ok=True)


@app.post("/api/dupes/stop")
def dupes_stop():
    JOBS["dupes"].cancel.set()
    return jsonify(ok=True)


@app.get("/api/dupes")
def dupes_get():
    return jsonify(groups=DUPES["groups"], stopped=DUPES["stopped"], cross=DUPES.get("cross", False),
                   running=JOBS["dupes"].running, progress=JOBS["dupes"].progress,
                   priorities=list(core.PRIORITIES), modes=list(core.MODES))


@app.post("/api/dupes/export")
def dupes_export():
    ticked = set(body().get("ids") or [])
    for iid, info in DUPES["index"].items():
        info["ticked"] = iid in ticked
    report, to_delete = core.dupes_report_text(DUPES.get("raw") or [])
    return jsonify(report=report, to_delete=to_delete)


@app.post("/api/dupes/delete")
def dupes_delete():
    ids = body().get("ids") or []
    trash = bool(SETTINGS.get("use_trash")) and core.send2trash is not None
    HISTORY.snapshot()
    ok, errors, removed = 0, [], []
    for iid in ids:
        info = DUPES["index"].get(iid)
        if not info:
            continue
        try:
            if info["path"].exists():
                core.remove_file(info["path"], trash)
                HISTORY.write("duplicates", "recycle" if trash else "delete", info["path"])
                ok += 1
            else:
                errors.append(f"{info['path']} (already gone)")
            removed.append(iid)
        except Exception as e:
            errors.append(f"{info['path']}: {e}")
    for iid in removed:
        DUPES["index"].pop(iid, None)
    for g in DUPES["groups"]:
        g["items"] = [i for i in g["items"] if i["id"] not in removed]
    DUPES["groups"] = [g for g in DUPES["groups"] if g["items"]]
    if DUPES.get("raw"):
        DUPES["raw"] = [(k, [i for i in r if i in DUPES["index"].values()]) for k, r in DUPES["raw"]]
        DUPES["raw"] = [(k, r) for k, r in DUPES["raw"] if r]
    return jsonify(removed=ok, errors=errors, trash=trash)


# =============================================================================
# Tag health
# =============================================================================

HEALTH = {"results": [], "error": None}


def _health_scan(root):
    job = JOBS["health"]
    folders = []
    for dp, _, fs in os.walk(root):
        music = [Path(dp) / f for f in fs if Path(f).suffix.lower() in mp.SUPPORTED_EXTENSIONS]
        if music:
            folders.append((Path(dp), music))
    res = []
    for i, (d, music) in enumerate(folders):
        if job.cancel.is_set():
            break
        job.report(i + 1, len(folders), d.name)
        try:
            res.append(core.album_health(d, music))
        except Exception as e:
            res.append({"folder": d, "files": len(music), "artist": "", "album": "",
                        "issues": [f"error: {e}"], "route": "?"})
    HEALTH["results"] = res


@app.post("/api/health/scan")
def health_scan():
    job = JOBS["health"]
    if job.running:
        return busy("tag health scan")
    b = body()
    settings_update(b)
    root = Path(str(b.get("health_folder", "")).strip().strip('"'))
    if not root.is_dir():
        return jsonify(error="Pick a valid folder first."), 400
    job.start(_health_scan, root)
    return jsonify(ok=True)


@app.post("/api/health/stop")
def health_stop():
    JOBS["health"].cancel.set()
    return jsonify(ok=True)


@app.get("/api/health")
def health_get():
    rows = [{**r, "folder": str(r["folder"]), "name": Path(r["folder"]).name} for r in HEALTH["results"]]
    return jsonify(results=rows, running=JOBS["health"].running, progress=JOBS["health"].progress)


@app.get("/api/health/csv")
def health_csv():
    return Response(core.health_csv(HEALTH["results"]), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=tag_health.csv"})


# =============================================================================
# Lyrics
# =============================================================================

LYRICS = {"items": [], "texts": {}, "apply": None}


def _lyrics_scan_job(root, prefer_synced, overwrite):
    job = JOBS["lyrics"]
    items, texts = core.lyrics_scan(root, prefer_synced, overwrite, progress=job.report, cancel=job.cancel)
    LYRICS.update(items=items, texts=texts, apply=None)


def _lyrics_apply_job(ids, dry):
    job = JOBS["lyrics"]
    chosen = [i for i in LYRICS["items"] if i["id"] in ids]
    done, errors = core.lyrics_apply(chosen, LYRICS["texts"], dry_run=dry, history=HISTORY,
                                     progress=job.report, cancel=job.cancel)
    done_set = set(done)
    if not dry:
        for it in LYRICS["items"]:
            if it["id"] in done_set:
                it.update(status="has_lyrics", replaces=False, message="lyrics written")
        for iid in done_set:
            LYRICS["texts"].pop(iid, None)
    LYRICS["apply"] = {"dry": dry, "done": len(done), "errors": errors}


@app.post("/api/lyrics/scan")
def lyrics_scan():
    job = JOBS["lyrics"]
    if job.running:
        return busy("Lyrics")
    b = body()
    settings_update(b)
    root = Path(str(b.get("lyrics_folder", "")).strip().strip('"'))
    if not root.is_dir():
        return jsonify(error="Pick a valid folder first."), 400
    job.start(_lyrics_scan_job, root, bool(SETTINGS.get("lyrics_prefer_synced")),
              bool(SETTINGS.get("lyrics_overwrite")))
    return jsonify(ok=True)


@app.post("/api/lyrics/apply")
def lyrics_apply():
    job = JOBS["lyrics"]
    if job.running:
        return busy("Lyrics")
    b = body()
    ids = [str(i) for i in (b.get("ids") or []) if str(i) in LYRICS["texts"]]
    if not ids:
        return jsonify(error="Nothing to apply. Scan first, then tick the tracks you want."), 400
    job.start(_lyrics_apply_job, ids, bool(b.get("dry_run", True)))
    return jsonify(ok=True)


@app.post("/api/lyrics/stop")
def lyrics_stop():
    JOBS["lyrics"].cancel.set()
    return jsonify(ok=True)


@app.get("/api/lyrics")
def lyrics_get():
    items = [{k: v for k, v in it.items()} for it in LYRICS["items"]]
    return jsonify(items=items, found=len(LYRICS["texts"]), running=JOBS["lyrics"].running,
                   progress=JOBS["lyrics"].progress, apply=LYRICS["apply"])


# =============================================================================
# Review
# =============================================================================

REVIEW = {"entries": None, "cache_loaded": False}
_mb_lock = threading.Lock()


def _review_entries():
    if REVIEW["entries"] is None:
        REVIEW["entries"] = mp.load_unmatched()
    return REVIEW["entries"]


def _mb_ready():
    core.apply_paths_from(SETTINGS)
    mp.musicbrainzngs.set_useragent(mp.MB_APP_NAME, mp.MB_APP_VERSION, SETTINGS.get("mb_contact") or mp.MB_CONTACT)
    if not REVIEW["cache_loaded"]:
        mp.load_cache()
        REVIEW["cache_loaded"] = True


def _with_fit(entry, cands):
    discs = core._discs_from_entry(entry)
    out = []
    for c in cands or []:
        fit = mp.assess(c.get("disc_map"), discs)
        out.append({**c, "fit": fit or "nofit", "tracks": core._tracks_label(c.get("disc_map"))})
    order = {"complete": 0, "incomplete": 1, "nofit": 2}
    return sorted(out, key=lambda c: (order[c["fit"]], -int(c.get("score") or 0)))


def _entry_json(key, e):
    return {"key": key, "folder": e["folder"], "name": Path(e["folder"]).name, "artist": e.get("artist") or "",
            "album": e.get("album") or "", "clean_album": mp.clean_name(e.get("album") or ""),
            "discs": e.get("discs") or {}, "reason": e.get("reason", ""), "match": e.get("match"),
            "exists": Path(e["folder"]).exists(), "added": e.get("added", ""),
            "tracks": sum(len(t) for t in (e.get("discs") or {}).values()),
            "candidates": _with_fit(e, e.get("candidates"))}


@app.get("/api/review")
def review_get():
    if request.args.get("reload"):
        REVIEW["entries"] = None
    entries = _review_entries()
    # Snapshot: a review job may mutate/delete keys while we serialize.
    snapshot = dict(entries)
    rows = sorted((_entry_json(k, e) for k, e in snapshot.items()), key=lambda r: r["artist"].lower())
    return jsonify(entries=rows, running=JOBS["review"].running, progress=JOBS["review"].progress)


@app.post("/api/review/search")
def review_search():
    b = body()
    e = _review_entries().get(b.get("key"))
    if not e:
        abort(404)
    artist, album = str(b.get("artist", "")).strip(), str(b.get("album", "")).strip()
    if not artist or not album:
        return jsonify(error="Enter an artist and an album."), 400
    with _mb_lock:
        _mb_ready()
        cands = mp.search_candidates(artist, album)
        for c in cands[:10]:
            dm = c.get("disc_map") or {}
            if not dm or not all(dm.values()):
                rel = mp.release_by_id(c["id"])
                if rel:
                    c["disc_map"] = rel["disc_map"]
        mp.save_cache()
    e["candidates"] = cands
    mp.save_unmatched(_review_entries())
    return jsonify(entry=_entry_json(b["key"], e))


@app.post("/api/review/lookup")
def review_lookup():
    b = body()
    e = _review_entries().get(b.get("key"))
    m = mp.MBID_RE.search(str(b.get("id", "")))
    if not e or not m:
        return jsonify(error="Paste a MusicBrainz release ID or a link like https://musicbrainz.org/release/…"), 400
    with _mb_lock:
        _mb_ready()
        rel = mp.release_by_id(m.group(0).lower())
        mp.save_cache()
    if not rel:
        return jsonify(error="No release with that ID (a release-group or recording ID won't work)."), 404
    e["candidates"] = [rel] + [c for c in (e.get("candidates") or []) if c["id"] != rel["id"]]
    return jsonify(entry=_entry_json(b["key"], e))


@app.post("/api/review/accept")
def review_accept():
    b = body()
    entries = _review_entries()
    e = entries.get(b.get("key"))
    if not e:
        abort(404)
    if not Path(e["folder"]).is_dir():
        return jsonify(error="That folder no longer exists. Dismiss it, or reload."), 410
    HISTORY.snapshot()
    with _mb_lock:
        _mb_ready()
        res = core.review_accept(e, b.get("id"), bool(SETTINGS.get("review_write_tags")), HISTORY)
        mp.save_cache()
    mp.save_unmatched(entries)
    return jsonify(result=res, entry=_entry_json(b["key"], e))


@app.post("/api/review/dismiss")
def review_dismiss():
    entries = _review_entries()
    entries.pop(body().get("key"), None)
    mp.save_unmatched(entries)
    return jsonify(ok=True)


def _review_recheck(include):
    job = JOBS["review"]
    with _mb_lock:
        _mb_ready()
        try:
            core.review_recheck(_review_entries(), include, job.cancel, job.report)
        finally:
            mp.save_cache()


def _review_move(keys, write):
    job = JOBS["review"]
    core.review_move(_review_entries(), keys, write, HISTORY, job.cancel, job.report)


@app.post("/api/review/recheck")
def review_recheck():
    job = JOBS["review"]
    if job.running:
        return busy("review")
    b = body()
    settings_update(b)
    HISTORY.snapshot()
    job.start(_review_recheck, bool(SETTINGS.get("review_scan_incomplete")))
    return jsonify(ok=True)


@app.post("/api/review/move")
def review_move():
    job = JOBS["review"]
    if job.running:
        return busy("review")
    entries = _review_entries()
    keys = body().get("keys") or [k for k, e in entries.items() if e.get("match") and Path(e["folder"]).is_dir()]
    if not keys:
        return jsonify(error="No matched folders to move."), 400
    core.apply_paths_from(SETTINGS)
    HISTORY.snapshot()
    job.start(_review_move, keys, bool(SETTINGS.get("review_write_tags")))
    return jsonify(ok=True, count=len(keys))


@app.post("/api/review/stop")
def review_stop():
    JOBS["review"].cancel.set()
    return jsonify(ok=True)


# =============================================================================
# Plex
# =============================================================================

PLEX = {"server": None, "sections": [], "groups": [], "index": {}, "name": "", "log": []}


def _map():
    return str(SETTINGS.get("plex_map_from") or "").strip(), str(SETTINGS.get("plex_map_to") or "").strip()


def to_local(plex_path: str) -> Path:
    f, t = _map()
    if f and plex_path.startswith(f):
        return Path(t) / plex_path[len(f):].lstrip("/\\").replace("/", os.sep)
    return Path(plex_path)


def to_plex(local: Path) -> str:
    f, t = _map()
    s = str(local)
    if f and t and s.lower().startswith(str(Path(t)).lower()):
        return f.rstrip("/") + "/" + s[len(str(Path(t))):].lstrip("/\\").replace("\\", "/")
    return s


def plex_log(s):
    PLEX["log"].append(s)
    PLEX["log"] = PLEX["log"][-1000:]
    HUB.post("plex_log", s)


@app.post("/api/plex/connect")
def plex_connect():
    b = body()
    settings_update(b)
    try:
        from plexapi.server import PlexServer
    except ImportError:
        return jsonify(error="Install plexapi first: pip install plexapi"), 400
    try:
        srv = PlexServer(SETTINGS["plex_url"], b.get("plex_token") or SETTINGS.get("plex_token"), timeout=30)
        secs = [s for s in srv.library.sections() if s.TYPE in ("movie", "show")]
    except Exception as e:
        return jsonify(error=f"Could not connect: {e}"), 502
    PLEX.update(server=srv, sections=secs, name=srv.friendlyName)
    return jsonify(name=srv.friendlyName, libraries=[{"index": i, "title": s.title, "type": s.TYPE}
                                                     for i, s in enumerate(secs)])


def _plex_scan(sel, opts, min_diff, want_orphans):
    job = JOBS["plex"]
    groups_raw, orphans = core.plex_scan(sel, opts, min_diff, want_orphans, job.report, job.cancel, to_local, to_plex, plex_log)
    groups, index = [], {}
    for g, gr in enumerate(groups_raw):
        auto = bool(gr["delete"])
        items = []
        for i, p in enumerate(gr["parts"]):
            reason = gr["reasons"].get(i, "keep" if auto else "")
            if p["parts_in_media"] > 1:
                reason += " · multi-part media"
            if not p["size"]:
                reason += " · size unknown"
            iid = f"p{g}_{i}"
            index[iid] = {"kind": "plex", "group": gr, "part": p, "reason": reason}
            items.append({"id": iid, "file": p["file"], "res": p["res"], "codec": f"{p['vcodec']}/{p['acodec']}",
                          "bitrate": p["bitrate"], "size": p["size"], "duration": (p["duration_ms"] or 0) / 1000 or None,
                          "reason": reason, "auto": i in gr["delete"]})
        groups.append({"id": f"g{g}", "title": gr["title"], "note": gr["note"], "review": bool(gr["note"]), "items": items})
    if orphans:
        items = []
        for i, o in enumerate(orphans):
            iid = f"o{i}"
            index[iid] = {"kind": "orphan", "path": o["path"], "reason": "orphan"}
            items.append({"id": iid, "file": str(o["path"]), "size": o["size"], "reason": o["section"], "auto": False,
                          "res": "", "codec": "", "bitrate": None, "duration": None})
        groups.append({"id": "orphans", "title": f"Orphans — on disk but not in Plex ({len(orphans)})",
                       "note": "never auto-ticked: extras, samples, or not scanned yet", "review": False,
                       "orphans": True, "items": items})
    PLEX.update(groups=groups, index=index)


@app.post("/api/plex/scan")
def plex_scan():
    job = JOBS["plex"]
    if job.running:
        return busy("Plex scan")
    if not PLEX["server"]:
        return jsonify(error="Connect to the server first."), 400
    b = body()
    settings_update(b)
    idx = b.get("libraries") or []
    sel = [PLEX["sections"][i] for i in idx if 0 <= i < len(PLEX["sections"])] or list(PLEX["sections"])
    opts = {"resolution": SETTINGS["plex_rule_res"], "format": SETTINGS["plex_rule_fmt"],
            "suffix": SETTINGS["plex_rule_suffix"],
            "tags": [t.strip() for t in str(SETTINGS["plex_tags"]).split(",") if t.strip()]
            if SETTINGS["plex_rule_tags"] else []}
    job.start(_plex_scan, sel, opts, float(SETTINGS.get("plex_min_diff") or 0), bool(SETTINGS["plex_orphans"]))
    return jsonify(ok=True)


@app.get("/api/plex")
def plex_get():
    return jsonify(connected=PLEX["server"] is not None, name=PLEX["name"],
                   libraries=[{"index": i, "title": s.title, "type": s.TYPE} for i, s in enumerate(PLEX["sections"])],
                   groups=PLEX["groups"], log=PLEX["log"][-200:], running=JOBS["plex"].running,
                   progress=JOBS["plex"].progress)


def _plex_apply(ids, dry, rm_folder, refresh, trash):
    job = JOBS["plex"]
    roots = [to_local(loc).resolve() for s in PLEX["sections"] for loc in getattr(s, "locations", [])]
    done, touched = core.plex_apply(ids, PLEX["index"], dry, rm_folder, refresh, trash,
                                    to_local, to_plex, roots, job.report, job.cancel, plex_log, HISTORY)
    if not dry:
        for iid in done:
            PLEX["index"].pop(iid, None)
        for g in PLEX["groups"]:
            g["items"] = [i for i in g["items"] if i["id"] not in done]
        PLEX["groups"] = [g for g in PLEX["groups"] if g["items"]]


@app.post("/api/plex/apply")
def plex_apply():
    job = JOBS["plex"]
    if job.running:
        return busy("Plex")
    b = body()
    ids = [i for i in (b.get("ids") or []) if i in PLEX["index"]]
    if not ids:
        return jsonify(error="Nothing ticked."), 400
    ticked = set(ids)
    for g in PLEX["groups"]:
        if g.get("orphans"):
            continue
        if g["items"] and all(i["id"] in ticked for i in g["items"]):
            return jsonify(error=f"Every copy of “{g['title']}” is ticked. Untick at least one to keep."), 400
    HISTORY.snapshot()
    trash = bool(SETTINGS.get("use_trash")) and core.send2trash is not None
    job.start(_plex_apply, ids, bool(b.get("dry_run", True)), bool(SETTINGS["plex_rm_folder"]),
              bool(SETTINGS["plex_refresh"]), trash)
    return jsonify(ok=True)


@app.post("/api/plex/stop")
def plex_stop():
    JOBS["plex"].cancel.set()
    return jsonify(ok=True)


# =============================================================================
# Settings, history, folders, state
# =============================================================================

@app.get("/api/settings")
def settings_get():
    s = dict(SETTINGS)
    s["plex_token_set"] = bool(s.get("plex_token"))
    s.pop("plex_token", None)            # never sent back to the browser
    s["discogs_token_set"] = bool(s.get("discogs_token"))
    s.pop("discogs_token", None)
    return jsonify(settings=s, capabilities=capabilities(), path_settings=[
        {"key": key, "label": label, "kind": kind} for _, label, key, kind in core.ALL_PATH_SETTINGS],
        defaults=core.PIPELINE_DEFAULT_PATHS)


@app.post("/api/settings")
def settings_post():
    settings_update(body())
    return settings_get()


@app.get("/api/history")
def history_get():
    HISTORY.snapshot()
    return jsonify(lines=core.history_today(HISTORY), path=str(HISTORY.path()))


@app.get("/api/fs")
def fs_list():
    """Folder browser for picking paths on the computer running this server."""
    p = str(request.args.get("path") or "").strip().strip('"')
    if not p:
        if sys.platform == "win32":
            drives = [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
            return jsonify(path="", parent=None, dirs=[{"name": d, "path": d} for d in drives])
        p = "/"
    path = Path(p)
    if not path.is_dir():
        return jsonify(error=f"Not a folder: {p}"), 404
    try:
        dirs = sorted((d for d in path.iterdir() if d.is_dir() and not d.name.startswith(".")),
                      key=lambda d: d.name.lower())
    except PermissionError:
        dirs = []
    parent = str(path.parent) if path.parent != path else ("" if sys.platform == "win32" else None)
    return jsonify(path=str(path), parent=parent, dirs=[{"name": d.name, "path": str(d)} for d in dirs[:2000]])


@app.post("/api/open")
def open_path():
    """Open a folder in Explorer/Finder on the computer running the server."""
    p = Path(str(body().get("path") or ""))
    if not p.exists():
        return jsonify(error="Not found"), 404
    if not p.is_dir():
        return jsonify(error="Only folders can be opened"), 400
    try:
        if sys.platform == "win32":
            os.startfile(str(p))  # noqa
        else:
            import subprocess
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(p)])
    except Exception as e:
        return jsonify(error=str(e)), 500
    return jsonify(ok=True)


@app.get("/api/state")
def state():
    return jsonify(
        jobs={n: {"running": j.running, "progress": j.progress} for n, j in JOBS.items()},
        pipeline={**{k: v for k, v in PIPE.items() if k != "slots"},
                  "slots": [PIPE["slots"].get(i) for i in range(PIPE["slot_count"])], "log": "".join(PIPE["log"][-1500:]),
                  "stopping": JOBS["pipeline"].cancel.is_set() and JOBS["pipeline"].running})


@app.get("/api/events")
def events():
    q = HUB.subscribe()

    def stream():
        try:
            yield "retry: 2000\n\n"
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            HUB.unsubscribe(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# =============================================================================
# Pages + access control
# =============================================================================

@app.errorhandler(JobBusy)
def _job_busy(e):
    return jsonify(error=str(e)), 409


def _token_ok(supplied) -> bool:
    # compare_digest raises TypeError on non-ASCII str, so compare the UTF-8 bytes instead.
    if not supplied:
        return False
    return secrets.compare_digest(str(supplied).encode("utf-8"), ACCESS_TOKEN.encode("utf-8"))


@app.before_request
def check_token():
    if not ACCESS_TOKEN:
        return
    supplied = request.args.get("token") or request.headers.get("X-Token") or request.cookies.get("mt_token")
    if not _token_ok(supplied):
        return Response("This music toolkit needs its access link (with ?token=…). "
                        "It is printed in the window where the server is running.", 403)


@app.get("/")
def index():
    if ACCESS_TOKEN and request.args.get("token"):
        # The link's token has been checked. Move it into the cookie and redirect to a clean URL, so it
        # doesn't stay in the address bar or browser history.
        resp = redirect("/")
        resp.headers["Cache-Control"] = "no-store"
        resp.set_cookie("mt_token", ACCESS_TOKEN, httponly=True, samesite="Strict", max_age=60 * 60 * 24 * 30)
        return resp
    resp = send_from_directory(WEB_DIR, "index.html")
    resp.headers["Cache-Control"] = "no-cache"
    if ACCESS_TOKEN:
        resp.set_cookie("mt_token", ACCESS_TOKEN, httponly=True, samesite="Strict", max_age=60 * 60 * 24 * 30)
    return resp


@app.get("/static/<path:name>")
def static_files(name):
    resp = send_from_directory(WEB_DIR, name)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    global ACCESS_TOKEN
    ap = argparse.ArgumentParser(description="Web front end for the music toolkit")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--lan", action="store_true", help="listen on the network (phone, other PCs); needs the token link")
    ap.add_argument("--token", help="access token to use with --lan (default: random each start)")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = ap.parse_args()

    if not (WEB_DIR / "index.html").exists():
        sys.exit(f"Missing {WEB_DIR / 'index.html'}: keep the web/ folder next to toolkit_web.py")

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Music Toolkit web app: {url}")
    if args.lan:
        ACCESS_TOKEN = args.token or secrets.token_urlsafe(12)
        print(f"From your phone or another PC (same network): http://{lan_ip()}:{args.port}/?token={ACCESS_TOKEN}")
        print("Anyone with that link can move and delete your files; don't share it.")
        url += f"?token={ACCESS_TOKEN}"
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        from waitress import serve               # production WSGI server (requirements-optional.txt)
    except ImportError:
        serve = None
    if serve is not None:
        serve(app, host=host, port=args.port, threads=8)
    else:
        print("Waitress is not installed; using the Flask development server. Run: pip install waitress")
        app.run(host=host, port=args.port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
