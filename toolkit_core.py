#!/usr/bin/env python3
"""
toolkit_core.py -- logic shared by the desktop app (music_toolkit.py) and the web app (toolkit_web.py).

Nothing in here touches a user interface. Long-running work reports through a "bus": any object with
post(kind, *payload). The desktop app's bus hands events to Tk; the web app's bus streams them to
browsers.
"""

import csv
import io
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import mutagen
import music_pipeline as mp

try:
    from send2trash import send2trash
except ImportError:
    send2trash = None

try:
    import acoustid
    import chromaprint
except ImportError:
    acoustid = chromaprint = None

SETTINGS_FILE = Path(__file__).with_name("music_toolkit_settings.json")
HISTORY_DEFAULT = str(Path(__file__).with_name("history"))

# Pipeline output folders: defaults come from music_pipeline.py, overridable in the Settings tab.
PATH_SETTINGS = [
    ("COMPLETE_DIR", "Complete albums", "path_complete"),
    ("INCOMPLETE_DIR", "Incomplete albums", "path_incomplete"),
    ("DELETE_DIR", "Albums with gaps (to delete)", "path_delete"),
    ("CUE_DEST", "Single-FLAC / cue folders", "path_cue"),
]
# beets and pipeline-log locations: (attr on music_pipeline, label, settings key, kind).
BEETS_PATH_SETTINGS = [
    ("BEETS_DIR", "Beets directory", "path_beets_dir", "dir"),
    ("BEETS_LIBRARY", "Beets library database", "path_beets_library", "file"),
    ("BEETS_IMPORT_LOG", "Beets import log (skipped albums)", "path_beets_import_log", "file"),
    ("BEET_IMPORT_LOG", "Pipeline beets output log", "path_pipeline_log", "file"),
]
# Every configurable location, with its kind ("dir" or "file").
ALL_PATH_SETTINGS = [(a, l, k, "dir") for a, l, k in PATH_SETTINGS] + BEETS_PATH_SETTINGS

# Keep references to the unpatched pipeline functions so repeated runs don't stack wrappers.
_MP_PROCESS_FOLDER = mp.process_folder
_MP_CONVERT_FILE = mp.convert_file
_MP_ACT = mp.act
_MP_CONVERT_ALL = mp.convert_all
# music_pipeline.py's own output folders, captured before any settings override them
PIPELINE_DEFAULT_PATHS = {key: str(getattr(mp, attr)) for attr, _, key, _kind in ALL_PATH_SETTINGS}
_MP_ASK_CHOICE = mp.ask_choice


class History:
    """Appends every move/delete to <folder>/history_YYYY-MM-DD.log so you can trace where things went."""

    def __init__(self, folder_var, enabled_var):
        self.folder_var, self.enabled_var, self.lock = folder_var, enabled_var, threading.Lock()
        # read the Tk vars once per run from the main thread; workers use the snapshot
        self.folder, self.enabled = None, True

    def snapshot(self):
        self.folder = Path(self.folder_var.get().strip() or HISTORY_DEFAULT)
        self.enabled = self.enabled_var.get()

    def path(self):
        return (self.folder or Path(self.folder_var.get() or HISTORY_DEFAULT)) / f"history_{time.strftime('%Y-%m-%d')}.log"

    def write(self, tab, action, detail):
        if not self.enabled:
            return
        if self.folder is None:
            self.snapshot()
        try:
            with self.lock:
                self.folder.mkdir(parents=True, exist_ok=True)
                with open(self.path(), "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%H:%M:%S')}\t{tab}\t{action}\t{detail}\n")
        except Exception:
            pass


class QueueWriter:
    """Replaces stdout/stderr during a pipeline run so print() lands in the log."""

    def __init__(self, bus, kind, history=None, dry_run=False):
        self.bus, self.kind, self.last_folder = bus, kind, ""
        self.history, self.dry_run, self.analyzing = history, dry_run, ""

    def write(self, s):
        if not s:
            return
        for line in s.splitlines():
            st = line.strip()
            if line.startswith("Folder :"):
                self.last_folder = line[8:].strip()
            elif st.startswith(("Analyzing folder:", "Analyzing album:")):
                self.analyzing = st.split(":", 1)[1].strip()
            elif st.startswith("Result: ->") and self.history and not self.dry_run:
                # album files are moved with shutil.move directly, so record them from the log line
                self.history.write("pipeline", "sort", f"{self.analyzing} -> {st[len('Result: ->'):].strip()}")
        if "\r" not in s:
            self.bus.post(self.kind, s)

    def flush(self):
        pass

    def isatty(self):
        return False


class Cancelled(Exception):
    pass



def fmt_size(b):
    if not b:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def fmt_dur(sec):
    if sec is None:
        return "?"
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def remove_path(p: Path, use_trash: bool):
    """Delete a file or directory, preferring the Recycle Bin.

    Unlike the old call sites, this raises on failure instead of using
    `ignore_errors=True`, so the caller can report what went wrong.
    """
    p = Path(p)
    if use_trash:
        if send2trash is None:
            raise RuntimeError("send2trash is not installed; refusing to delete permanently")
        send2trash(str(p))
    elif p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        p.unlink()


def remove_file(p: Path, use_trash: bool):
    """Kept for existing callers; handles files and directories now."""
    remove_path(p, use_trash)


class ConvProgress:
    """Overall conversion progress weighted by audio length: finished seconds plus the seconds
    each worker has done on its current file, over the total length of every file in the batch."""

    def __init__(self, bus):
        self.bus, self.lock = bus, threading.Lock()
        self.durations, self.total_secs, self.done_secs = {}, 0.0, 0.0
        self.files_done = self.files_total = 0
        self.active: dict[int, float] = {}
        self.started, self.last_post = time.monotonic(), 0.0

    def start(self, files):
        known = {}
        for f in files:
            try:
                fi = mutagen.File(f)
                known[f] = fi.info.length if fi and fi.info and fi.info.length else 0.0
            except Exception:
                known[f] = 0.0
        avg = (sum(known.values()) / max(1, sum(1 for v in known.values() if v))) or 240.0
        self.durations = {f: v or avg for f, v in known.items()}   # unreadable length -> assume average
        self.total_secs, self.done_secs = sum(self.durations.values()), 0.0
        self.files_total, self.files_done = len(files), 0
        self.active.clear()
        self.started = time.monotonic()
        self.post(force=True)

    def slot_progress(self, slot, secs):
        with self.lock:
            self.active[slot] = secs
        self.post()

    def slot_idle(self, slot):
        with self.lock:
            self.active.pop(slot, None)

    def file_done(self, src):
        with self.lock:
            self.done_secs += self.durations.get(src, 0.0)
            self.files_done += 1
        self.post(force=True)

    def post(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_post < 0.25:      # don't flood the GUI
            return
        self.last_post = now
        with self.lock:
            done = self.done_secs + sum(self.active.values())
        pct = min(100.0, done / self.total_secs * 100) if self.total_secs else 0.0
        elapsed = now - self.started
        eta = elapsed * (100 - pct) / pct if pct > 0.5 else None
        self.bus.post("overall", pct, self.files_done, self.files_total, eta)


class GuiSlots:
    def __init__(self, n, bus, progress: ConvProgress):
        self.bus, self.q, self.progress = bus, queue.Queue(), progress
        for i in range(n):
            self.q.put(i)
        bus.post("slots_init", n)

    def acquire(self):
        return self.q.get()

    def release(self, slot):
        self.progress.slot_idle(slot)
        self.bus.post("slot", slot, "idle", 0)
        self.q.put(slot)

    def update(self, slot, desc, cur, total):
        cur = min(cur, total) if total else 0
        self.progress.slot_progress(slot, cur)
        self.bus.post("slot", slot, desc, int(cur / total * 100) if total else 0)

    def close_all(self):
        pass


class GuiOverall:
    """Stands in for the pipeline's tqdm bar; file counting happens in ConvProgress."""

    def __init__(self, bus):
        self.bus = bus

    def update(self, n=1):
        pass

    def set_postfix_str(self, s, refresh=True):
        self.bus.post("log", f"  {s}\n")

    def close(self):
        pass


AUDIO_EXTENSIONS = {".m4a", ".flac", ".mp3", ".aac", ".ogg", ".wma", ".alac",
                    ".aiff", ".aif", ".wav", ".opus", ".ape", ".wv"}
PRIORITIES = {
    "M4A > FLAC > MP3 (matches the pipeline's output)": [".m4a", ".flac", ".mp3"],
    "FLAC > M4A > MP3 (keep lossless)": [".flac", ".m4a", ".mp3"],
}
MODES = {
    "Same folder, same filename": "name",
    "Anywhere, same artist + title (tags)": "tags",
}
DURATION_TOLERANCE_S = 2.0
FINGERPRINT_MIN_SIMILARITY = 0.80


def normalize_stem(stem):
    s = re.sub(r"[-_.]+", " ", stem.lower())
    return re.sub(r"\s+", " ", s).strip()


def normalize_text(s):
    s = re.sub(r"[^\w\s]", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def read_basic(p: Path):
    """(duration, artist, title) -- anything unreadable is None."""
    try:
        f = mutagen.File(p, easy=True)
        if not f:
            return None, None, None
        dur = round(f.info.length, 1) if f.info else None
        get = lambda k: (f.get(k) or [None])[0] if f.tags else None  # noqa: E731
        return dur, get("artist") or get("albumartist"), get("title")
    except Exception:
        return None, None, None


def fp_similarity(a: Path, b: Path):
    """Bit-level similarity of two Chromaprint fingerprints (0..1), or None if unavailable."""
    if acoustid is None:
        return None
    try:
        fa = chromaprint.decode_fingerprint(acoustid.fingerprint_file(str(a))[1])[0]
        fb = chromaprint.decode_fingerprint(acoustid.fingerprint_file(str(b))[1])[0]
        n = min(len(fa), len(fb))
        if not n:
            return None
        errors = sum(bin((x ^ y) & 0xFFFFFFFF).count("1") for x, y in zip(fa[:n], fb[:n]))
        return 1 - errors / (32 * n)
    except Exception:
        return None


def find_duplicates(root: Path, order, mode, check_duration, use_fp, progress=None, cancel=None):
    rank = {ext: i for i, ext in enumerate(order)}
    files = [Path(dp) / n for dp, _, fs in os.walk(root) for n in fs
             if Path(n).suffix.lower() in AUDIO_EXTENSIONS and ".partial." not in n]
    total = len(files)
    infos = {}
    need_tags = mode == "tags" or check_duration

    def _read_one(p):
        if cancel and cancel.is_set():
            return None
        try:
            size = p.stat().st_size
        except OSError:
            return None
        if cancel and cancel.is_set():
            return None
        dur, artist, title = read_basic(p) if need_tags else (None, None, None)
        return p, size, dur, artist, title

    # Tag reading is the slow part; run it in parallel (mutagen releases the GIL
    # during I/O). The non-tag path only needs stat(), so it stays sequential.
    if need_tags:
        workers = min(8, max(1, len(files)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_read_one, p): p for p in files}
            done = 0
            for fut in as_completed(futures):
                if cancel and cancel.is_set():
                    break
                try:
                    res = fut.result()
                except Exception:
                    res = None
                if res is None:
                    continue
                p, size, dur, artist, title = res
                infos[p] = {"path": p, "size": size, "duration": dur, "artist": artist, "title": title, "suspect": False, "note": ""}
                done += 1
                if progress and done % 10 == 0:
                    progress(done, total, "Reading tags…")
    else:
        for i, p in enumerate(files):
            if cancel and cancel.is_set():
                break
            try:
                size = p.stat().st_size
            except OSError:
                continue
            infos[p] = {"path": p, "size": size, "duration": None, "artist": None, "title": None, "suspect": False, "note": ""}
            if progress and i % 10 == 0:
                progress(i, total, "Listing…")

    # --- group ---
    raw_groups = defaultdict(list)
    for p, info in infos.items():
        if mode == "name":
            raw_groups[(str(p.parent), normalize_stem(p.stem))].append(info)
        elif info["artist"] and info["title"]:
            raw_groups[(normalize_text(info["artist"]), normalize_text(info["title"]))].append(info)

    groups = []
    for members in raw_groups.values():
        if len(members) < 2:
            continue
        if mode == "tags":
            # split into clusters of similar length so a live 7:00 and a studio 3:30 don't pair up
            members.sort(key=lambda m: m["duration"] or 0)
            cluster = [members[0]]
            for m in members[1:]:
                if (m["duration"] or 0) - (cluster[-1]["duration"] or 0) <= DURATION_TOLERANCE_S:
                    cluster.append(m)
                else:
                    if len(cluster) > 1:
                        groups.append(cluster)
                    cluster = [m]
            if len(cluster) > 1:
                groups.append(cluster)
        else:
            groups.append(members)

    results = []
    for gi, members in enumerate(groups):
        if cancel and cancel.is_set():
            break
        if progress:
            progress(gi, len(groups), "Comparing…")
        members.sort(key=lambda i: (rank.get(i["path"].suffix.lower(), len(order)), -i["size"]))
        keep, rest = members[0], members[1:]
        for c in rest:
            if check_duration:
                if keep["duration"] is None or c["duration"] is None:
                    c["suspect"], c["note"] = True, "couldn't read length"
                elif abs(keep["duration"] - c["duration"]) > DURATION_TOLERANCE_S:
                    c["suspect"], c["note"] = True, "length differs — check before deleting"
            if use_fp and not c["suspect"]:
                sim = fp_similarity(keep["path"], c["path"])
                if sim is None:
                    c["note"] = "fingerprint unavailable"
                elif sim < FINGERPRINT_MIN_SIMILARITY:
                    c["suspect"], c["note"] = True, f"audio differs (fingerprint {sim:.0%})"
                else:
                    c["note"] = f"fingerprint match {sim:.0%}"
        results.append((keep, rest))
    if progress:
        progress(1, 1, "")
    return results


COVER_NAMES = {"cover", "folder", "front", "album", "albumart"}


def has_embedded_cover(fp: Path):
    try:
        f = mutagen.File(fp)
        if f is None:
            return False
        if hasattr(f, "pictures") and f.pictures:
            return True
        tags = f.tags or {}
        return "covr" in tags or any(str(k).startswith("APIC") for k in tags.keys())
    except Exception:
        return False


def album_health(folder: Path, files: list[Path]):
    tags = [(fp, mp.extract_tags(fp) or {}) for fp in files]
    n = len(tags)
    issues = []

    def missing(field):
        return sum(1 for _, t in tags if not str(t.get(field) or "").strip())

    for field, label in (("album_artist", "album artist"), ("album", "album"), ("artist", "artist"),
                         ("title", "title"), ("track", "track #")):
        m = missing(field)
        if m:
            issues.append(f"no {label} ({m}/{n})")

    albums = {str(t.get("album")).strip() for _, t in tags if t.get("album")}
    aartists = {str(t.get("album_artist")).strip() for _, t in tags if t.get("album_artist")}
    if len(albums) > 1:
        issues.append(f"{len(albums)} different albums")
    if len(aartists) > 1:
        issues.append(f"{len(aartists)} different album artists")

    discs = {}
    for _, t in tags:
        if t.get("disc") is not None and t.get("track") is not None:
            v = discs.setdefault(t["disc"], {"tracks": set(), "expected_total": None})
            v["tracks"].add(t["track"])
            if t.get("total_tracks"):
                v["expected_total"] = t["total_tracks"]
    has_gap, gap_disc, gap_missing = mp.find_track_gap(discs)
    if has_gap:
        issues.append(f"disc {gap_disc} missing {','.join(map(str, gap_missing))}")
    for d, v in discs.items():
        if v["expected_total"] and len(v["tracks"]) < v["expected_total"] and not has_gap:
            issues.append(f"disc {d}: {len(v['tracks'])} of {v['expected_total']} tracks")

    folder_art = any(p.stem.lower() in COVER_NAMES and p.suffix.lower() in (".jpg", ".jpeg", ".png")
                     for p in folder.rglob("*") if p.is_file())
    embedded = sum(1 for fp, _ in tags if has_embedded_cover(fp))
    if not folder_art and embedded == 0:
        issues.append("no cover art")
    elif not folder_art and embedded < n:
        issues.append(f"cover art on {embedded}/{n}")

    lookup_ok = all(str(t.get("album_artist") or t.get("artist") or "").strip() and str(t.get("album") or "").strip()
                    and t.get("track") is not None for _, t in tags)
    if has_gap:
        route = "→ delete (gap)"
    elif not lookup_ok:
        route = "→ incomplete (tags)"
    elif discs and mp.verify_via_tags(discs):
        route = "likely complete*"
    else:
        route = "MusicBrainz decides"
    album = next(iter(albums), "") if len(albums) == 1 else ""
    artist = next(iter(aartists), "") if len(aartists) == 1 else ""
    return {"folder": folder, "files": n, "artist": artist, "album": album, "issues": issues, "route": route}


def _discs_from_entry(entry):
    return {int(d): {'tracks': set(t), 'expected_total': None} for d, t in (entry.get('discs') or {}).items()}


def _tracks_label(dm):
    dm = {int(k): v for k, v in (dm or {}).items()}
    return " + ".join(str(dm[k]) for k in sorted(dm)) if dm else "?"


def _discs_counts(c):
    return list((c.get('disc_map') or {}).values())


VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".wmv"}
FORMAT_PRIORITY = {"mkv": 0, "mp4": 1, "avi": 2}
DEFAULT_TAGS = "HDTV-720p,WEBDL-1080p,HDTV-1080p,WEBDL-720p,1080p,720p,480p"
COPY_SUFFIX_RE = re.compile(r"\.(\d+)$")
DURATION_DIFF_PCT = 0.05     # >5% length difference = probably a different cut, never auto-delete


def resolution_rank(res):
    s = str(res or "").lower()
    if s in ("4k", "2160"):
        return 2160
    if s == "sd":
        return 480
    m = re.match(r"\d+", s)
    return int(m.group()) if m else 0


def part_info(media, part):
    return {"file": part.file, "size": getattr(part, "size", 0) or 0,
            "container": getattr(media, "container", "?"), "vcodec": getattr(media, "videoCodec", "?"),
            "acodec": getattr(media, "audioCodec", "?"), "res": getattr(media, "videoResolution", "?"),
            "bitrate": getattr(media, "bitrate", None), "duration_ms": getattr(media, "duration", None),
            "media_id": media.id, "parts_in_media": len(media.parts)}


def plan_group(parts, opts):
    """Decide which parts to delete. Each rule only acts if it leaves at least one keeper.
    Returns (set of indices to delete, {index: reason}, status_note)."""
    durs = [p["duration_ms"] for p in parts if p["duration_ms"]]
    if len(durs) == len(parts) and max(durs) and (max(durs) - min(durs)) / max(durs) > DURATION_DIFF_PCT:
        return set(), {}, f"lengths differ by {(max(durs) - min(durs)) / 60000:.0f} min — different cut? review"

    remaining = list(range(len(parts)))
    reasons = {}

    def apply(keep, lose, why):
        nonlocal remaining
        if keep and lose:
            for i in lose:
                reasons[i] = why(i, keep)
            remaining = keep

    name = lambda i: os.path.basename(parts[i]["file"])  # noqa: E731

    if opts["resolution"] and len(remaining) > 1:
        best = max(resolution_rank(parts[i]["res"]) for i in remaining)
        if best:
            apply([i for i in remaining if resolution_rank(parts[i]["res"]) == best],
                  [i for i in remaining if resolution_rank(parts[i]["res"]) < best],
                  lambda i, k: f"lower resolution ({parts[i]['res']} < {parts[k[0]]['res']})")

    if opts["format"] and len(remaining) > 1:
        rank = lambda i: FORMAT_PRIORITY.get(os.path.splitext(parts[i]["file"])[1].lower().lstrip("."), 3)  # noqa
        best = min(rank(i) for i in remaining)
        top = [i for i in remaining if rank(i) == best]
        if len(top) == 1:   # only act when one file clearly wins
            apply(top, [i for i in remaining if rank(i) > best],
                  lambda i, k: f"container .{os.path.splitext(parts[i]['file'])[1][1:]} < .{os.path.splitext(parts[k[0]]['file'])[1][1:]}")

    if opts["suffix"] and len(remaining) > 1:
        suf = lambda i: bool(COPY_SUFFIX_RE.search(os.path.splitext(name(i))[0]))  # noqa
        apply([i for i in remaining if not suf(i)], [i for i in remaining if suf(i)], lambda i, k: "copy suffix (.N)")

    if opts["tags"] and len(remaining) > 1:
        tags = [t.lower() for t in opts["tags"]]
        hit = lambda i: next((t for t in tags if t in name(i).lower()), None)  # noqa
        apply([i for i in remaining if not hit(i)], [i for i in remaining if hit(i)],
              lambda i, k: f"tag '{hit(i)}' (untagged version kept)")

    return set(reasons), reasons, "" if len(remaining) == 1 else "needs review"


def plex_scan(sel, opts, min_diff, want_orphans, progress, cancel, to_local, to_plex, log=None):
    """Scan Plex sections for duplicate media. Returns (groups, orphans).

    ``groups`` is a list of dicts with keys title/item/section/parts/delete/reasons/note.
    ``orphans`` is a list of dicts with keys path/size/section.
    ``progress(n, t, label)`` and ``log(msg)`` are callbacks; ``cancel`` is a threading.Event.
    """
    if log is None:
        log = lambda _msg: None
    groups_raw, orphans = [], []
    for si, sec in enumerate(sel):
        if cancel.is_set():
            break
        progress(si, len(sel), f"Loading '{sec.title}'…")
        items = sec.all() if sec.TYPE == "movie" else sec.search(libtype="episode")
        known = set()
        for n, it in enumerate(items):
            if cancel.is_set():
                break
            if n % 200 == 0:
                progress(n, len(items), f"Scanning '{sec.title}' {n}/{len(items)}")
            parts = [part_info(m, p) for m in it.media for p in m.parts]
            known.update(p["file"] for p in parts)
            if len(parts) < 2:
                continue
            for p in parts:
                if not p["size"]:
                    try:
                        p["size"] = to_local(p["file"]).stat().st_size
                    except OSError:
                        pass
            sizes = [p["size"] for p in parts]
            if min_diff and (max(sizes) - min(sizes)) / 1_048_576 < min_diff:
                continue
            title = (f"{it.title} ({getattr(it, 'year', '') or '?'})" if sec.TYPE == "movie" else
                     f"{it.grandparentTitle} - S{(it.parentIndex or 0):02d}E{(it.index or 0):02d} - {it.title}")
            dels, reasons, note = plan_group(parts, opts)
            groups_raw.append({"title": title, "item": it, "section": sec, "parts": parts,
                               "delete": dels, "reasons": reasons, "note": note})
        if want_orphans and not cancel.is_set():
            for loc in sec.locations:
                root = to_local(loc)
                if not root.is_dir():
                    log(f"Orphan scan: can't see {loc} as {root} (set the path map?)")
                    continue
                for dp, _, fs in os.walk(root):
                    for fn in fs:
                        p = Path(dp) / fn
                        if p.suffix.lower() in VIDEO_EXTENSIONS and to_plex(p) not in known \
                                and str(p) not in known and "sample" not in fn.lower():
                            try:
                                orphans.append({"path": p, "size": p.stat().st_size, "section": sec.title})
                            except OSError:
                                pass
    return groups_raw, orphans


def plex_apply(ids, index, dry, rm_folder, refresh, trash, to_local, to_plex, roots, progress, cancel, log, history):
    """Apply ticked Plex deletes. Returns (done_ids, touched_sections).

    ``index`` maps id -> info dict (successful entries are popped).
    ``roots`` is the list of library roots (Path) used for safe folder removal.
    """
    if log is None:
        log = lambda _msg: None

    def safe_to_remove(folder):
        f = folder.resolve()
        return any(r in f.parents for r in roots) and f not in roots

    def cleanup_folder(local):
        folder = local.parent
        if not rm_folder or not folder.is_dir() or local.exists() or not safe_to_remove(folder):
            return
        if any(p.suffix.lower() in VIDEO_EXTENSIONS for p in folder.rglob("*") if p.is_file()):
            return
        if dry:
            log(f"    [DRY RUN] would remove folder: {folder}")
        else:
            try:
                remove_path(folder, trash)
            except Exception as e:
                log(f"    Could not remove folder {folder}: {e}")
                return
            history.write("plex", "recycle" if trash else "rmtree", folder)
            log(f"    Removed folder (no video left): {folder}")

    done, touched = [], set()
    for n, iid in enumerate(ids):
        if cancel.is_set():
            break
        info = index.get(iid)
        if not info:
            continue
        progress(n, len(ids), "Deleting…")
        try:
            if info["kind"] == "orphan":
                p = info["path"]
                log(f"{'[DRY RUN] ' if dry else ''}delete orphan {p}")
                if not dry:
                    remove_path(p, trash)
                    history.write("plex", "recycle" if trash else "delete-orphan", p)
                cleanup_folder(p)
            else:
                part, g = info["part"], info["group"]
                local = to_local(part["file"])
                log(f"{'[DRY RUN] ' if dry else ''}delete {part['file']}  ({info.get('reason', '')})")
                if not dry:
                    if trash and local.exists():
                        remove_path(local, trash)
                        how = "recycle"
                    else:
                        ok, how = False, "plex-api"
                        try:
                            for m in g["item"].media:
                                if m.id == part["media_id"]:
                                    m.delete()
                                    ok = True
                                    break
                        except Exception as e:
                            log(f"    Plex API delete failed ({e}); trying the file directly")
                        if not ok or local.exists():
                            if local.exists():
                                remove_path(local, trash)
                                ok, how = True, "disk"
                            elif not ok:
                                raise FileNotFoundError(f"not found locally: {local}")
                    history.write("plex", f"delete ({how})", f"{part['file']}  [{g['title']}; {info.get('reason', '')}]")
                    touched.add(g["section"])
                cleanup_folder(local)
            done.append(iid)
        except Exception as e:
            log(f"    ERROR: {e}")
    if refresh and not dry:
        for sec in touched:
            try:
                sec.update()
                log(f"Refreshing '{sec.title}'…")
            except Exception as e:
                log(f"Could not refresh '{sec.title}': {e}")
    if not dry:
        for iid in done:
            index.pop(iid, None)
    log(f"{'Dry run: would delete' if dry else 'Deleted'} {len(done)} file(s).")
    return done, touched


# =============================================================================
# Settings + pipeline runner shared by both front ends
# =============================================================================

class Value:
    """Minimal stand-in for a Tk variable (get/set) so History works without Tk."""

    def __init__(self, v=None):
        self.v = v

    def get(self):
        return self.v

    def set(self, v):
        self.v = v


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        if SETTINGS_FILE.exists():
            try:
                import time as _t
                SETTINGS_FILE.replace(SETTINGS_FILE.with_name(
                    SETTINGS_FILE.stem + f".corrupt-{_t.strftime('%Y%m%d%H%M%S')}" + SETTINGS_FILE.suffix))
            except OSError:
                pass
        return {}


def save_settings(data: dict) -> None:
    data = dict(data)
    if not data.get("plex_remember_token"):
        data.pop("plex_token", None)
    if not data.get("discogs_remember_token"):
        data.pop("discogs_token", None)
    if SETTINGS_FILE.exists():
        try:
            shutil.copy2(SETTINGS_FILE, SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".bak"))
        except OSError:
            pass
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


def default_settings() -> dict:
    d = dict(PIPELINE_DEFAULT_PATHS)
    d.update(
        pipe_folder="", pipe_workers=mp.DEFAULT_WORKERS, pipe_bitrate=mp.AAC_BITRATE, pipe_single="ask",
        pipe_convert=True, pipe_cleanup=True, pipe_beets=mp.RUN_BEETS_FIRST, mb_contact=mp.MB_CONTACT,
        use_trash=send2trash is not None, pipe_trash=send2trash is not None,
        history_dir=HISTORY_DEFAULT, history_on=True,
        dupes_folder="", dupes_priority=next(iter(PRIORITIES)), dupes_mode=next(iter(MODES)),
        dupes_duration=True, dupes_fp=False,
        health_folder="", health_only_issues=True,
        lyrics_folder="", lyrics_prefer_synced=True, lyrics_overwrite=False,
        review_write_tags=True, review_scan_incomplete=True,
        plex_url=os.environ.get("PLEX_BASEURL", "http://localhost:32400"),
        plex_token=os.environ.get("PLEX_TOKEN", ""), plex_remember_token=False, plex_tags=DEFAULT_TAGS,
        discogs_token="", discogs_remember_token=False,
        plex_min_diff=0.0, plex_rule_res=True, plex_rule_fmt=True, plex_rule_suffix=True, plex_rule_tags=True,
        plex_orphans=False, plex_rm_folder=True, plex_refresh=True, plex_map_from="", plex_map_to="",
    )
    return d


def apply_paths_from(values: dict) -> None:
    """Push configured folders and files (by settings key) into music_pipeline."""
    for attr, _, key, _kind in ALL_PATH_SETTINGS:
        v = str(values.get(key) or "").strip()
        if v:
            setattr(mp, attr, Path(v))
    mp.PROTECTED_DIRS = (mp.COMPLETE_DIR, mp.INCOMPLETE_DIR, mp.DELETE_DIR, mp.CUE_DEST)
    # service credentials: an empty value keeps whatever was set before
    if str(values.get("mb_contact") or "").strip():
        mp.MB_CONTACT = str(values["mb_contact"]).strip()
    if str(values.get("discogs_token") or "").strip():
        mp.DISCOGS_TOKEN = str(values["discogs_token"]).strip()


def pipeline_preflight(folder: Path, opts: dict) -> tuple[list[str], list[str]]:
    """(errors that block the run, warnings the user should confirm)."""
    errors, warnings = [], []
    if not folder.is_dir():
        errors.append("Pick a valid music folder first.")
        return errors, warnings
    if mp.is_protected(folder):
        errors.append("That folder is inside one of the output folders.")
    if opts.get("convert") and not opts.get("dry_run"):
        missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
        if missing:
            errors.append(f"Not found on PATH: {', '.join(missing)}")
    contact = str(opts.get("contact") or "").strip()
    if opts.get("cleanup") and (not contact or "your-email" in contact):
        warnings.append("MusicBrainz contact is still the placeholder; MusicBrainz may throttle anonymous clients.")
    if not opts.get("dry_run"):
        warnings.append("This is a LIVE run: files will be converted, moved and deleted.")
    return errors, warnings


class _Patched:
    """Context manager that sets attributes on a module and restores them on exit.

    Used by run_pipeline() so the monkey-patches it installs on music_pipeline
    are always undone, even when an exception escapes.
    """

    def __init__(self, module, values: dict):
        self.module = module
        self.values = values
        self.originals: dict = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.originals[key] = getattr(self.module, key, None)
            setattr(self.module, key, value)
        return self

    def __exit__(self, *exc):
        for key in self.values:
            setattr(self.module, key, self.originals[key])
        return False


def run_pipeline(folder: Path, opts: dict, bus, history, cancel: threading.Event, ask) -> str | None:
    """
    Run conversion and/or cleanup. Blocking; run it on a worker thread.
    opts: convert, cleanup, dry_run, bitrate, run_beets, contact, workers, single_mode ('ask'|'c'|'m'), trash
    ask(folder) -> 'c' or 'm'; called for single-FLAC folders when single_mode is 'ask'.
    Events posted: log(text), slots_init(n), slot(i, desc, pct), overall(pct, done, total, eta),
                   beets_progress(pct, albums_done, albums_total, skipped, current, eta), pipeline_done(err)
    Returns None, "cancelled", or an error string.
    """
    trash = bool(opts.get("trash")) and send2trash is not None
    dry = bool(opts.get("dry_run"))
    writer = QueueWriter(bus, "log", history, dry)
    old = sys.stdout, sys.stderr

    lock = mp.PipelineLock()
    try:
        lock.acquire()
    except mp.PipelineBusy as e:
        msg = str(e)
        bus.post("log", f"\n❌ {msg}\n")
        bus.post("pipeline_done", msg)
        return msg

    sys.stdout = sys.stderr = writer
    err = None
    try:
        def logged_act(desc, fn, *a, **kw):
            """Every delete/move/rmtree the pipeline makes goes through act(); log it,
            and send deletes to the Recycle Bin instead of destroying them."""
            if mp.DRY_RUN:
                return _MP_ACT(desc, fn, *a, **kw)
            if trash and fn in (os.remove, shutil.rmtree):
                history.write("pipeline", "recycle", desc.split(" ", 1)[-1])
                return send2trash(str(a[0]))
            history.write("pipeline", desc.split(" ", 1)[0], desc.split(" ", 1)[-1])
            return fn(*a, **kw)

        conv = ConvProgress(bus)

        def guarded_convert_file(src, slots, overall):
            if cancel.is_set():
                conv.file_done(src)
                return False, "cancelled"
            try:
                ok, e = _MP_CONVERT_FILE(src, slots, overall)
            finally:
                conv.file_done(src)
            history.write("pipeline", "convert" if ok else "convert-failed",
                          f"{src} -> {src.with_suffix('.m4a').name}" if ok else f"{src} ({e})")
            return ok, e

        def guarded_process_folder(*a, **k):
            if cancel.is_set():
                raise Cancelled()
            return _MP_PROCESS_FOLDER(*a, **k)

        def ui_convert_all(files, workers):
            if not files:
                return []
            workers = max(1, min(workers, len(files)))
            bus.post("log", f"Measuring {len(files)} file(s) for the progress total…\n")
            conv.start(files)
            slots, overall = GuiSlots(workers, bus, conv), GuiOverall(bus)
            failed = []
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(guarded_convert_file, f, slots, overall): f for f in files}
                for fut in as_completed(futs):
                    try:
                        ok, e = fut.result()
                    except Exception as ex:
                        ok, e = False, repr(ex)
                    if not ok and e != "cancelled":
                        failed.append((futs[fut], e))
            return failed

        mode = opts.get("single_mode") or "ask"

        def ui_ask_choice(prompt, options):
            if mode in ("c", "m"):
                return mode
            if cancel.is_set():
                return "c"
            return ask(writer.last_folder)

        beets_start = {}

        def ui_beets_progress(done, total, albums_done, albums_total, skipped, current):
            now = time.monotonic()
            beets_start.setdefault('t', now)
            pct = done / total * 100 if total else 0.0
            elapsed = now - beets_start['t']
            eta = elapsed * (100 - pct) / pct if pct > 0.5 else None
            bus.post("beets_progress", pct, albums_done, albums_total, skipped, current, eta)

        patches = {
            "act": logged_act,
            "DRY_RUN": dry,
            "AAC_BITRATE": opts.get("bitrate") or mp.AAC_BITRATE,
            "RUN_BEETS_FIRST": bool(opts.get("run_beets")),
            "MB_CONTACT": str(opts.get("contact") or "").strip() or mp.MB_CONTACT,
            "BEETS_PROGRESS": ui_beets_progress,
            "CANCEL_EVENT": cancel,
            "convert_all": ui_convert_all,
            "ask_choice": ui_ask_choice,
            "convert_file": guarded_convert_file,
            "process_folder": guarded_process_folder,
        }

        with _Patched(mp, patches):
            mp.musicbrainzngs.set_useragent(mp.MB_APP_NAME, mp.MB_APP_VERSION, mp.MB_CONTACT)

            print(f"\n{'#' * 60}\nSource: {folder}{'  (DRY RUN)' if dry else ''}")
            if opts.get("convert"):
                mp.run_conversion(folder, int(opts.get("workers") or mp.DEFAULT_WORKERS), non_interactive=(mode == "c"))
            if cancel.is_set():
                raise Cancelled()
            if opts.get("cleanup"):
                mp.run_cleanup(folder)
            print("\nAll done!")
    except Cancelled:
        print("\n■ Stopped by user. Files already processed stay where they were moved; "
              "unconverted FLACs were left untouched.")
        err = "cancelled"
    except Exception as e:
        import traceback
        traceback.print_exc()
        err = repr(e)
    finally:
        sys.stdout, sys.stderr = old
        bus.post("pipeline_done", err)
        lock.release()
    return err


# ---------------------------------------------------------------- review helpers

def review_accept(entry: dict, mbid: str, write_tags: bool, history) -> dict:
    """Remember a release for a folder, optionally tag the files, and report how the folder fits."""
    folder = Path(entry['folder'])
    rel = mp.release_by_id(mbid)
    if not rel:
        raise ValueError(f"No MusicBrainz release with ID {mbid}")
    info = mp.scan_folder(folder)
    fit = mp.assess(rel.get('disc_map'), info['discs'])
    mp.remember_match(info['artist'] or entry.get('artist'), info['album'] or entry.get('album'), rel['id'])
    history.write("review", "match", f"{folder} = {rel['artist']} - {rel['title']} [{rel['id']}]")
    tagged, errors = 0, []
    if write_tags:
        for fp in info['files']:
            try:
                mp.write_album_id(fp, rel['id'])
                tagged += 1
            except Exception as ex:
                errors.append(f"{fp.name}: {ex}")
    missing = mp.missing_tracks(rel.get('disc_map'), info['discs']) if fit else {}
    if fit == "complete":
        entry['match'], entry['reason'] = rel, "matched by you; complete"
    else:
        entry.pop('match', None)
        entry['reason'] = (f"matched by you; missing {missing}" if fit == "incomplete"
                           else "matched by you, but the release's discs/track counts don't fit the folder")
    return {"release": rel, "fit": fit, "missing": {str(k): v for k, v in missing.items()},
            "tagged": tagged, "errors": errors}


def review_recheck(entries: dict, include_incomplete: bool, cancel, progress) -> int:
    """Run the matcher over every unmatched queue entry (and, optionally, every Incomplete folder)."""
    if include_incomplete and mp.INCOMPLETE_DIR.is_dir():
        known = {Path(e['folder']).resolve() for e in entries.values()}
        for d in mp.INCOMPLETE_DIR.iterdir():
            if d.is_dir() and d.resolve() not in known and any(
                    p.is_file() and p.suffix.lower() in mp.SUPPORTED_EXTENSIONS for p in d.rglob("*")):
                entries[str(d)] = {'folder': str(d), 'artist': None, 'album': None, 'discs': {},
                                   'reason': "not checked yet", 'candidates': [],
                                   'added': time.strftime('%Y-%m-%d %H:%M')}
    keys = [k for k, e in entries.items() if Path(e['folder']).is_dir() and not e.get('match')]
    found = 0
    for i, key in enumerate(keys):
        if cancel.is_set():
            break
        e = entries[key]
        progress(i, len(keys), f"Checking {i + 1}/{len(keys)}: {Path(e['folder']).name}  ·  {found} newly matched")
        try:
            info = mp.scan_folder(Path(e['folder']))
        except OSError:
            continue
        e.update(artist=info['artist'], album=info['album'],
                 discs={str(d): sorted(v['tracks']) for d, v in info['discs'].items()})
        if not info['discs']:
            e['reason'] = "no track/disc tags"
            continue
        m = mp.find_release(info['artist'], info['album'], info['discs'], info['mbid'], info['titles'])
        e['candidates'] = m['candidates']
        if m['result']:
            e['match'], e['reason'] = m['release'], f"matched via {m['how']}"
            found += 1
        elif m['release']:
            e['reason'] = f"missing tracks {mp.missing_tracks(m['release']['disc_map'], info['discs'])}"
        else:
            e['reason'] = m['how']
        if i % 20 == 0:
            mp.save_cache()
            mp.save_unmatched(entries)
    mp.save_unmatched(entries)
    progress(1, 1, f"Re-check done: {found} folder(s) newly matched.")
    return found


def review_move(entries: dict, keys: list, write_tags: bool, history, cancel, progress) -> int:
    """Move matched folders to Complete, named after the matched release."""
    mp.COMPLETE_DIR.mkdir(parents=True, exist_ok=True)
    moved = 0
    for i, key in enumerate(keys):
        if cancel.is_set():
            break
        e = entries.get(key)
        if not e:
            continue
        src, rel = Path(e['folder']), e.get('match')
        if not rel or not src.is_dir():
            continue
        progress(i, len(keys), f"Moving {i + 1}/{len(keys)}: {src.name}")
        if write_tags:
            for fp in src.rglob("*"):
                if fp.is_file() and fp.suffix.lower() in mp.SUPPORTED_EXTENSIONS:
                    try:
                        mp.write_album_id(fp, rel['id'])
                    except Exception:
                        pass
        dest = mp.unique_dest(mp.COMPLETE_DIR / f"{mp.sanitize_folder_name(rel['artist'] or e.get('artist') or 'Unknown Artist')}"
                                                 f" - {mp.sanitize_folder_name(rel['title'] or e.get('album') or src.name)}")
        try:
            shutil.move(str(src), str(dest))
            history.write("review", "move", f"{src} -> {dest}")
            del entries[key]
            moved += 1
        except Exception as ex:
            e['reason'] = f"move failed: {ex}"
    mp.save_unmatched(entries)
    return moved


# ---------------------------------------------------------------- reports

def dupes_report_text(groups) -> tuple[str, str]:
    """(report, to_delete list) for [(keep, [candidates with 'ticked'])]."""
    rep, dl = io.StringIO(), io.StringIO()
    for keep, rest in groups:
        rep.write(f"KEEP  : {keep['path']}\n")
        for i in rest:
            rep.write(f"{'DELETE' if i.get('ticked') else 'SKIP  '}: {i['path']}  "
                      f"({fmt_size(i['size'])}, {fmt_dur(i['duration'])}) {i.get('note', '')}\n")
            if i.get('ticked'):
                dl.write(f"{i['path']}\n")
        rep.write("\n")
    return rep.getvalue(), dl.getvalue()


def health_csv(results) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["folder", "files", "artist", "album", "predicted route", "issues"])
    for r in results:
        w.writerow([r["folder"], r["files"], r["artist"], r["album"], r["route"], "; ".join(r["issues"])])
    return out.getvalue()


def history_today(history, limit=500) -> list[str]:
    try:
        lines = history.path().read_text(encoding="utf-8").splitlines()
        return lines[-limit:]
    except Exception:
        return []


# =============================================================================
# Lyrics: LRCLIB lookup, reading and writing lyrics tags (used by the Lyrics tab)
# =============================================================================
LRCLIB_API = "https://lrclib.net/api"
LRCLIB_USER_AGENT = "music-toolkit (https://github.com/gazornenphlat69/Music-Organizer)"
LYRICS_DURATION_TOLERANCE_S = 3.0
LYRICS_EXTENSIONS = {".mp3", ".flac", ".ogg", ".opus", ".m4a"}


class LyricsNetworkError(RuntimeError):
    """LRCLIB could not be reached or returned an error. Such lookups are not cached or treated as 'no lyrics'."""


def _lrclib_get_json(path, params, timeout=15):
    """GET a LRCLIB endpoint. Returns the decoded JSON, or None on HTTP 404."""
    req = Request(f"{LRCLIB_API}/{path}?{urlencode(params)}", headers={"User-Agent": LRCLIB_USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        if e.code == 404:
            return None
        raise LyricsNetworkError(f"LRCLIB returned HTTP {e.code}") from e
    except (URLError, OSError, ValueError) as e:
        raise LyricsNetworkError(f"LRCLIB request failed: {e}") from e


def _has_text(hit):
    return bool((hit.get("plainLyrics") or "").strip() or (hit.get("syncedLyrics") or "").strip())


def lrclib_lookup(artist, title, album="", duration=None):
    """Best LRCLIB match for a track, or None. Tries an exact /get first, then /search with a duration check.
    Instrumental tracks and results with no lyrics text are treated as no match."""
    if not artist or not title:
        return None
    base = {"artist_name": artist, "track_name": title}
    get_params = dict(base)
    if album:
        get_params["album_name"] = album
    if duration:
        get_params["duration"] = int(round(duration))
    hit = _lrclib_get_json("get", get_params)
    if hit and _has_text(hit):
        return hit
    for cand in _lrclib_get_json("search", base) or []:
        if not _has_text(cand):
            continue
        d = cand.get("duration")
        if duration and d is not None and abs(float(d) - float(duration)) > LYRICS_DURATION_TOLERANCE_S:
            continue
        return cand
    return None


def pick_lyrics_text(hit, prefer_synced=True):
    """(text, is_synced) from a LRCLIB hit. Synced lyrics are used when preferred and available."""
    synced = (hit.get("syncedLyrics") or "").strip()
    plain = (hit.get("plainLyrics") or "").strip()
    if prefer_synced and synced:
        return synced, True
    if plain:
        return plain, False
    if synced:
        return synced, True
    return None, False


def read_existing_lyrics(fp: Path) -> bool:
    """True if the file already has non-empty lyrics text in its tags."""
    f = mutagen.File(fp)
    if f is None or f.tags is None:
        return False
    ext = Path(fp).suffix.lower()
    if ext == ".mp3":
        frames = f.tags.getall("USLT") if hasattr(f.tags, "getall") else []
        return any((fr.text or "").strip() for fr in frames)
    if ext == ".m4a":
        return any(str(v).strip() for v in (f.tags.get("\xa9lyr") or []))
    return any(str(v).strip() for v in (f.tags.get("LYRICS") or []))


def write_lyrics(fp: Path, text: str) -> None:
    """Store lyrics text in the file's tags, replacing any existing lyrics. Synced lyrics go in the same
    tag as LRC-timestamped text, the way beets stores them. Unsupported formats raise ValueError."""
    ext = Path(fp).suffix.lower()
    if ext not in LYRICS_EXTENSIONS:
        raise ValueError(f"lyrics aren't supported for {ext} files")
    f = mutagen.File(fp)
    if f is None:
        raise ValueError("not a readable audio file")
    if f.tags is None:
        f.add_tags()
    if ext == ".mp3":
        from mutagen.id3 import USLT
        f.tags.delall("USLT")
        f.tags.add(USLT(encoding=3, lang="eng", desc="", text=text))
    elif ext == ".m4a":
        f.tags["\xa9lyr"] = [text]
    else:
        f.tags["LYRICS"] = [text]
    f.save()


def _read_track_meta(fp: Path):
    f = mutagen.File(fp, easy=True)
    if f is None:
        raise ValueError("not a readable audio file")
    get = lambda k: (f.get(k) or [""])[0] if f.tags else ""  # noqa: E731
    duration = round(f.info.length, 1) if f.info else None
    return {"artist": get("artist") or get("albumartist"), "title": get("title"),
            "album": get("album"), "duration": duration}


def lyrics_scan(root: Path, prefer_synced=True, overwrite=False, progress=None, cancel=None,
                lookup=lrclib_lookup):
    """Check every supported audio file under root and look up lyrics for those that need them.

    Returns (items, texts): items are dicts describing each file (status is one of has_lyrics, found,
    not_found, error); texts maps item id -> the lyrics text that Apply would write. Nothing is written here.
    """
    files = sorted(Path(dp) / n for dp, _, fs in os.walk(root) for n in fs
                   if Path(n).suffix.lower() in LYRICS_EXTENSIONS and ".partial." not in n)
    items, texts = [], {}
    for i, fp in enumerate(files):
        if cancel and cancel.is_set():
            break
        if progress:
            progress(i + 1, len(files), fp.name)
        item = {"id": str(i), "path": str(fp), "name": fp.name, "artist": "", "title": "", "album": "",
                "duration": None, "status": "error", "synced": False, "replaces": False, "message": ""}
        items.append(item)
        try:
            meta = _read_track_meta(fp)
            item.update(meta)
            existing = read_existing_lyrics(fp)
            if existing and not overwrite:
                item.update(status="has_lyrics", message="already has lyrics")
                continue
            if not meta["artist"] or not meta["title"]:
                item.update(status="not_found", message="needs artist and title tags to look up lyrics")
                continue
            hit = lookup(meta["artist"], meta["title"], meta["album"], meta["duration"])
            text, synced = pick_lyrics_text(hit, prefer_synced) if hit else (None, False)
            if not text:
                item.update(status="not_found", message="no lyrics on LRCLIB")
                continue
            texts[item["id"]] = text
            item.update(status="found", synced=synced, replaces=existing,
                        message="replaces existing lyrics" if existing else "")
        except LyricsNetworkError as e:
            item.update(status="error", message=str(e))
        except Exception as e:
            item.update(status="error", message=f"could not read file: {e}")
    return items, texts


def lyrics_apply(items, texts, dry_run=True, history=None, progress=None, cancel=None):
    """Write the found lyrics into their files. Returns (done_ids, errors). Dry run reports without writing."""
    targets = [it for it in items if it["status"] == "found" and it["id"] in texts]
    done, errors = [], []
    for i, it in enumerate(targets):
        if cancel and cancel.is_set():
            break
        fp = Path(it["path"])
        if progress:
            progress(i + 1, len(targets), fp.name)
        if dry_run:
            done.append(it["id"])
            continue
        try:
            write_lyrics(fp, texts[it["id"]])
            if not read_existing_lyrics(fp):
                raise RuntimeError("lyrics did not save")
            if history is not None:
                history.write("lyrics", "write", str(fp))
            done.append(it["id"])
        except Exception as e:
            errors.append(f"{fp.name}: {e}")
    return done, errors
