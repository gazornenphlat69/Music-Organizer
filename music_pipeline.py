#!/usr/bin/env python3
"""
music_pipeline.py -- one-shot music processing pipeline.

  Stage 1: FLAC -> M4A (AAC) conversion, in parallel, originals deleted only
           after the output is verified.
  Stage 2: Leftover cleanup -- beets first pass, in-folder dedup, track-gap
           check, MusicBrainz-verified completeness, then routing into
           complete / incomplete / delete folders.

Both stages run against the same source directory.

MusicBrainz matching (stage 2), tried in this order:
  1. A match you accepted by hand earlier (mb_manual_matches.json)
  2. A MusicBrainz album ID already in the files' tags (Picard / beets write these)
  3. Album search with the tags as-is, then with edition/disc/"feat." noise stripped;
     the first release whose disc + track counts fit the folder wins, so
     "Deluxe" vs "Standard" or the right country pressing is picked automatically
  4. Track-title search: the release that 2-3 of the folder's song titles share
  5. Nothing fits, but a release covers every disc the folder has *plus* more ->
     "suspected missing disc": routed to Incomplete/Review instead of trusting the
     tags (every disc present is contiguous, so tag-only checking would say complete)
Folders MusicBrainz couldn't confirm as complete are queued in mb_unmatched.json
for the Review tab in music_toolkit.py.

Usage:
  python music_pipeline.py [FOLDER] [-y] [--workers N]
                           [--skip-convert] [--skip-cleanup] [--dry-run]

Requires: pip install tqdm mutagen musicbrainzngs
          ffmpeg + ffprobe on PATH; beets on PATH (optional)
"""

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    from tqdm import tqdm
    import mutagen
    import musicbrainzngs
except ImportError as e:
    print(f"ERROR: missing dependency ({e.name}). Run: pip install tqdm mutagen musicbrainzngs")
    sys.exit(1)

try:
    from send2trash import send2trash
except ImportError:
    send2trash = None          # --trash refuses to run without it; nothing is faked

# =============================================================================
# CONFIGURATION
# =============================================================================

# Default library drive. Every default folder below derives from it; the Settings tab overrides each one.
LIBRARY_ROOT = Path("M:/")

# --- Stage 1: conversion ---
CUE_DEST = LIBRARY_ROOT / "TO SORT" / "AAA_CUE"
DEFAULT_WORKERS = min(os.cpu_count() or 4, 8)
AAC_BITRATE = "256k"            # 256k AAC is transparent for nearly everyone; 320k is fine too
DURATION_TOLERANCE_S = 1.0      # output must be within this of the source to count as good

# --- Stage 2: cleanup ---
COMPLETE_DIR = LIBRARY_ROOT / "sort_script" / "aaa_complete"
INCOMPLETE_DIR = LIBRARY_ROOT / "sort_script" / "aaa_incomplete"
DELETE_DIR = LIBRARY_ROOT / "sort_script" / "aaa_delete"
SUPPORTED_EXTENSIONS = {'.mp3', '.m4a', '.flac'}

__version__ = "3.0"   # single source of truth; pyproject.toml carries the same value
MB_APP_NAME = "LeftoverCleanupScript"
MB_APP_VERSION = __version__
MB_CONTACT = "your-email-or-project-url-here"   # <-- fill in (or set it in Settings)
DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN", "")   # set in Settings; goes into the beets config
MB_MIN_SCORE = 90                 # accept a search result on score alone
MB_MIN_SCORE_COUNTS_MATCH = 70    # accept a lower score when disc + track counts fit the folder exactly
MB_SEARCH_LIMIT = 15              # releases to look at per search (was: only the first)
MB_TRACK_FALLBACK = True          # last resort: find the release the folder's song titles share
CACHE_FILE = Path(__file__).with_name("mb_cache_v3.json")       # new format; the old mb_cache.json is ignored
MANUAL_MATCHES_FILE = Path(__file__).with_name("mb_manual_matches.json")
UNMATCHED_FILE = Path(__file__).with_name("mb_unmatched.json")
FALLBACK_TO_TAGS_IF_NOT_FOUND = True
VIDEO_MEDIA = ("dvd", "blu-ray", "vhs", "hd-dvd")   # bonus video discs never hold the audio files

RUN_BEETS_FIRST = True
BEET_EXECUTABLE = "beet"
BEET_IMPORT_ARGS = ["-q", "-m"]   # quiet + move. Without -q beets waits for input forever.
BEET_IMPORT_TIMEOUT = 1800       # stop beets only if it prints nothing for this long (seconds)
BEET_IMPORT_LOG = Path(__file__).with_name("beet_import_log.txt")   # pipeline-side beets output
# beets' own settings. config.yaml is a template: its __BEETS_*__ tokens are filled from these values
# into beets_runtime_config.yaml, which is passed to beet with -c.
BEETS_DIR = LIBRARY_ROOT / "beets"
BEETS_LIBRARY = Path.home() / "data" / "musiclibrary-7-2c-2026.db"
BEETS_IMPORT_LOG = BEETS_DIR / "import.log"
BEETS_CONFIG_TEMPLATE = Path(__file__).with_name("config.yaml")
BEETS_RUNTIME_CONFIG = Path(__file__).with_name("beets_runtime_config.yaml")

PROTECTED_DIRS = (COMPLETE_DIR, INCOMPLETE_DIR, DELETE_DIR, CUE_DEST)

DRY_RUN = False   # set by --dry-run
USE_TRASH = False  # set by --trash: route deletes through the Recycle Bin


def act(desc: str, fn, *a, **kw):
    """Run a filesystem-changing action, or just print it in dry-run mode."""
    if DRY_RUN:
        print(f"  [DRY RUN] {desc}")
        return None
    if USE_TRASH and fn in (os.remove, shutil.rmtree):
        if send2trash is None:
            raise RuntimeError("send2trash is not installed; refusing to delete permanently")
        return send2trash(str(a[0]))
    return fn(*a, **kw)


class PipelineBusy(RuntimeError):
    """Raised when another pipeline already holds the lock."""


class PipelineLock:
    """One pipeline at a time, across the CLI, the desktop app and the web app.

    Uses an in-process thread lock plus an atomically-created lock file, so the
    same mechanism works on Windows, macOS and Linux (and on network shares,
    where byte-range locking is unreliable). A stale lock left by a crashed
    process is detected via its recorded PID and cleared automatically.
    """

    _local = threading.Lock()

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else Path(__file__).with_name("music_pipeline.lock")
        self._held = False
        self._pid = os.getpid()

    def _pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            if sys.platform == "win32":
                import ctypes
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if not handle:
                    return False
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            else:
                os.kill(pid, 0)
                return True
        except (OSError, ValueError):
            return False

    # A lock file is created empty and the PID written a moment later, so an
    # empty (or torn) lock is only stale once it is old enough that no live
    # process could still be about to write its PID.
    STALE_LOCK_AGE_SECONDS = 10

    def _clear_stale(self) -> bool:
        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        if text.isdigit():
            if self._pid_alive(int(text)):
                return False
        else:
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                return False
            if age < self.STALE_LOCK_AGE_SECONDS:
                return False
        try:
            self.path.unlink()
        except OSError:
            return False
        return True

    def acquire(self) -> "PipelineLock":
        if not self._local.acquire(blocking=False):
            raise PipelineBusy("Another pipeline is already running in this process.")
        if self.path.exists() and self._clear_stale():
            pass
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            holder = ""
            try:
                holder = self.path.read_text(encoding="utf-8").strip()
            except OSError:
                holder = ""
            self._local.release()
            suffix = f" (PID {holder})" if holder.isdigit() else ""
            raise PipelineBusy(
                f"A pipeline is already running{suffix}. "
                f"Wait for it to finish, then try again. (Lock file: {self.path})"
            )
        except OSError as e:
            self._local.release()
            raise PipelineBusy(f"Could not create the pipeline lock file {self.path}: {e}") from e

        try:
            os.write(fd, f"{self._pid}\n".encode())
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        self._held = True
        return self

    def release(self) -> None:
        if not self._held:
            return
        try:
            if self.path.exists():
                try:
                    if self.path.read_text(encoding="utf-8").strip() == str(self._pid):
                        self.path.unlink()
                except OSError:
                    pass
        finally:
            self._held = False
            self._local.release()

    def __enter__(self) -> "PipelineLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def is_protected(folder: Path) -> bool:
    folder = folder.resolve()
    for p in PROTECTED_DIRS:
        p = p.resolve()
        if folder == p or p in folder.parents:
            return True
    return False


def unique_dest(dest: Path) -> Path:
    base, n = dest, 1
    while dest.exists():
        dest = base.with_name(f"{base.name} ({n})")
        n += 1
    return dest


# =============================================================================
# STAGE 1: FLAC -> M4A
# =============================================================================

def probe_duration(path: Path) -> float | None:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    try:
        return float(r.stdout.strip())
    except (ValueError, AttributeError):
        return None


def source_duration(path: Path) -> float | None:
    """ffprobe first; mutagen as a fallback so progress still works if ffprobe can't read the file."""
    d = probe_duration(path)
    if d:
        return d
    try:
        f = mutagen.File(path)
        return f.info.length if f and f.info and f.info.length else None
    except Exception:
        return None


class SlotPool:
    """Fixed set of tqdm bars so per-worker progress lines don't jump around."""

    def __init__(self, n: int) -> None:
        self._slots: queue.Queue[int] = queue.Queue()
        self._bars: list[tqdm] = []
        self._lock = threading.Lock()
        for i in range(n):
            bar = tqdm(total=100, desc=f"[slot {i+1}] idle", position=i + 1, leave=True,
                       bar_format="{desc:<50} {percentage:3.0f}%|{bar}|", dynamic_ncols=True)
            self._bars.append(bar)
            self._slots.put(i)

    def acquire(self) -> int:
        return self._slots.get()

    def release(self, slot: int) -> None:
        with self._lock:
            self._bars[slot].set_description_str(f"[slot {slot+1}] idle")
            self._bars[slot].reset(total=100)
        self._slots.put(slot)

    def update(self, slot: int, desc: str, cur: float, total: float) -> None:
        with self._lock:
            bar = self._bars[slot]
            bar.set_description_str(desc[:50])
            bar.n = min(100, int(cur / total * 100)) if total else 0
            bar.refresh()

    def close_all(self) -> None:
        for b in self._bars:
            b.close()


def convert_file(src: Path, slots: SlotPool, overall: tqdm) -> tuple[bool, str]:
    dst = src.with_suffix(".m4a")
    tmp = src.with_name(src.stem + ".partial.m4a")   # never clobber a real file mid-write
    short = src.name if len(src.name) <= 48 else src.name[:47] + "…"
    duration = source_duration(src)

    slot = slots.acquire()
    err = ""
    try:
        if dst.exists():
            ok, err = False, f"{dst.name} already exists; left FLAC alone"
        else:
            slots.update(slot, short, 0, duration or 1)
            # -progress pipe:1 writes "key=value" lines to stdout in the same format on every
            # ffmpeg version/OS; the human "-stats" line varies by build and can be suppressed.
            proc = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-nostats",
                 "-progress", "pipe:1", "-stats_period", "0.5",
                 "-i", str(src),
                 # keep embedded cover art instead of dropping it with -vn
                 "-map", "0:a", "-map", "0:v?", "-c:v", "copy", "-disposition:v", "attached_pic",
                 "-c:a", "aac", "-b:a", AAC_BITRATE,
                 "-map_metadata", "0", "-movflags", "+faststart",
                 "-f", "mp4", "-y", str(tmp)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
            )
            # Drain stderr on its own thread so a file spewing errors can't fill the pipe and hang us.
            tail: deque[str] = deque(maxlen=3)
            reader = threading.Thread(target=lambda: tail.extend(l.strip() for l in proc.stderr if l.strip()),
                                      daemon=True)
            reader.start()
            state = {"last": time.monotonic()}

            def watchdog():
                while proc.poll() is None:
                    if time.monotonic() - state["last"] > 120:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        return
                    time.sleep(1)

            threading.Thread(target=watchdog, daemon=True).start()
            for line in proc.stdout:
                state["last"] = time.monotonic()
                key, _, val = line.strip().partition("=")
                if key in ("out_time_us", "out_time_ms") and val.isdigit() and duration:
                    slots.update(slot, short, int(val) / 1_000_000, duration)   # both are microseconds
            proc.wait()
            reader.join(timeout=5)

            ok = proc.returncode == 0
            if ok:
                # Verify before deleting the original: output must exist and match length.
                out_dur = probe_duration(tmp)
                if out_dur is None or (duration and abs(out_dur - duration) > DURATION_TOLERANCE_S):
                    ok, err = False, f"duration mismatch (src={duration}, out={out_dur})"
            else:
                err = " | ".join(tail) or f"ffmpeg exit {proc.returncode}"

            if ok:
                tmp.replace(dst)
                try:
                    # Go through act() so a GUI run can send the original to the
                    # Recycle Bin and record it in the history log.
                    act(f"delete original {src.name}", os.remove, src)
                except OSError as e:
                    print(f"  Warning: converted {src.name} but could not remove the original: {e}")
                    err = f"converted; original kept ({e})"
            else:
                tmp.unlink(missing_ok=True)

        overall.update(1)
        overall.set_postfix_str(f"{'✓' if ok else '✗'} {short}", refresh=True)
        return ok, err
    finally:
        slots.release(slot)


def convert_all(files: list[Path], workers: int) -> list[tuple[Path, str]]:
    if not files:
        return []
    workers = max(1, min(workers, len(files)))
    overall = tqdm(total=len(files), desc="Overall", unit="file", position=0, leave=True,
                   bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]",
                   dynamic_ncols=True)
    slots = SlotPool(workers)
    failed: list[tuple[Path, str]] = []
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(convert_file, f, slots, overall): f for f in files}
            for fut in as_completed(futs):
                f = futs[fut]
                try:
                    ok, err = fut.result()
                except Exception as e:      # one bad file shouldn't kill the batch
                    ok, err = False, repr(e)
                if not ok:
                    failed.append((f, err))
    finally:
        slots.close_all()
        overall.close()
    return failed


def ask_choice(prompt: str, options: dict[str, str]) -> str:
    desc = "  ".join(f"[{k}] {v}" for k, v in options.items())
    while True:
        raw = input(f"{prompt}\n  {desc}\n  Choice ({'/'.join(options)}): ").strip().lower()
        if raw in options:
            return raw
        print(f"  Please enter one of: {'/'.join(options)}")


def _remove_stale_partials(folder: Path) -> int:
    """Delete <name>.partial.m4a files left behind by a run that was killed mid-convert.

    Nothing else ever writes this name, and the finished M4A is only renamed into
    place after verification, so a leftover partial is always junk.
    Returns how many were found.
    """
    stale = [p for p in folder.rglob("*.partial.m4a") if not is_protected(p.parent)]
    if DRY_RUN:
        # Summarize instead of one line per file.
        return len(stale)
    for p in stale:
        act(f"delete stale partial {p.name}", os.remove, p)
    return len(stale)


def run_conversion(folder: Path, workers: int, non_interactive: bool) -> None:
    print(f"\n{'=' * 60}\n  STAGE 1: FLAC -> M4A\n{'=' * 60}")
    stale = _remove_stale_partials(folder)
    if stale:
        if DRY_RUN:
            print(f"  [DRY RUN] would remove {stale} leftover .partial.m4a file(s) from a previous run.\n")
        else:
            print(f"  Cleaned {stale} leftover .partial.m4a file(s) from a previous run.\n")
    all_flac = [f for f in folder.rglob("*.flac") if not is_protected(f.parent)]
    if not all_flac:
        print("No FLAC files found.")
        return

    by_folder: dict[Path, list[Path]] = defaultdict(list)
    for f in all_flac:
        by_folder[f.parent].append(f)

    multi = [f for files in by_folder.values() if len(files) > 1 for f in files]
    singles = {d: fs for d, fs in by_folder.items() if len(fs) == 1}
    print(f"Found {len(all_flac)} FLAC file(s) in {len(by_folder)} folder(s): "
          f"{len(multi)} in multi-file folders, {len(singles)} single-file folder(s).\n")

    # Decide single-FLAC folders up front so the user isn't waiting on the batch.
    # A single FLAC next to a .cue is almost certainly a whole-album image: always
    # route those to the cue folder, even unattended.
    to_convert = list(multi)
    for d, fs in singles.items():
        has_cue = any(d.glob("*.cue"))
        print(f"Folder : {d}\nFile   : {fs[0].name}{'   (.cue found)' if has_cue else ''}")
        if has_cue:
            choice = "m"
            print("  Auto: .cue present -> move to cue folder")
        elif DRY_RUN:
            # Nothing changes either way in a dry run, so don't interrupt with a
            # prompt for every single-FLAC folder; assume convert for the report.
            choice = "c"
            print("  Auto: convert (dry run assumes convert; nothing would move)")
        elif non_interactive:
            choice = "c"
            print("  Auto: convert")
        else:
            choice = ask_choice("What to do with this folder?",
                                {"c": "Convert to M4A", "m": "Move to cue folder"})
        if choice == "c":
            to_convert.extend(fs)
        else:
            dest = unique_dest(CUE_DEST / d.name)
            CUE_DEST.mkdir(parents=True, exist_ok=True)
            act(f"move {d} -> {dest}", shutil.move, str(d), str(dest))
            print(f"  Moved to: {dest}")
        print()

    if DRY_RUN:
        print(f"[DRY RUN] would convert {len(to_convert)} file(s).")
        return

    print(f"Converting {len(to_convert)} file(s) with {min(workers, len(to_convert))} worker(s)...\n")
    failed = convert_all(to_convert, workers)
    if failed:
        print(f"\n  {len(failed)} file(s) failed (FLACs left in place):")
        for p, err in failed:
            print(f"    {p}\n      -> {err}")
    print()


# =============================================================================
# STAGE 2: TAGS
# =============================================================================

def sanitize_folder_name(name) -> str:
    # Also strip trailing dots/spaces, which Windows silently mangles.
    return re.sub(r'[\\/*?:"<>|]', "", str(name)).strip().rstrip(". ") or "Unknown"


def parse_int(value):
    if isinstance(value, tuple):
        return value[0] if value else None
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    try:
        return int(str(value).strip().split('/')[0])
    except ValueError:
        return None


def parse_total(value):
    if isinstance(value, tuple) and len(value) > 1:
        try:
            return int(value[1]) if value[1] else None
        except (TypeError, ValueError):
            return None
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None or '/' not in str(value):
        return None
    try:
        return int(str(value).split('/')[1])
    except ValueError:
        return None


MP4_MBID_KEY = "----:com.apple.iTunes:MusicBrainz Album Id"
MBID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def extract_tags(fp: Path) -> dict | None:
    try:
        f = mutagen.File(fp)
        if f is None:
            return None
        ext = fp.suffix.lower()
        t = {'bitrate': getattr(f.info, 'bitrate', 0) or 0, 'extension': ext}
        g = lambda k: (f.get(k) or [None])[0]   # noqa: E731

        if ext == '.flac':
            t.update(artist=g('artist'), album_artist=g('albumartist') or g('album artist'),
                     album=g('album'), title=g('title'), album_artist_sort=g('albumartistsort'),
                     track=parse_int(f.get('tracknumber')), disc=parse_int(f.get('discnumber')) or 1,
                     total_tracks=parse_total(f.get('tracknumber')) or parse_int(f.get('tracktotal')
                                                                                  or f.get('totaltracks')),
                     mb_albumid=g('musicbrainz_albumid'))
        elif ext == '.m4a':
            trkn = g('trkn') or (None, None)
            disk = g('disk') or (None, None)
            raw = g(MP4_MBID_KEY)
            t.update(artist=g('\xa9ART'), album_artist=g('aART'), album=g('\xa9alb'),
                     title=g('\xa9nam'), album_artist_sort=g('soaa'),
                     track=trkn[0] or None, total_tracks=trkn[1] or None,
                     disc=disk[0] or 1,       # MP4 stores "no disc" as 0
                     mb_albumid=bytes(raw).decode("utf-8", "ignore") if raw is not None else None)
        elif ext == '.mp3':
            txt = lambda k: f.get(k).text[0] if f.get(k) else None   # noqa: E731
            trck = txt('TRCK')
            t.update(artist=txt('TPE1'), album_artist=txt('TPE2'), album=txt('TALB'),
                     title=txt('TIT2'), album_artist_sort=txt('TSO2') or txt('TXXX:ALBUMARTISTSORT'),
                     track=parse_int(trck), total_tracks=parse_total(trck),
                     disc=parse_int(txt('TPOS')) or 1,
                     mb_albumid=txt('TXXX:MusicBrainz Album Id'))
        m = MBID_RE.search(str(t.get('mb_albumid') or ""))
        t['mb_albumid'] = m.group(0).lower() if m else None
        return t
    except Exception as e:
        print(f"  Error reading tags {fp.name}: {e}")
        return None


def write_album_id(fp: Path, mbid: str) -> None:
    """Store the MusicBrainz album ID in the file, where beets and Picard look for it."""
    from mutagen.id3 import TXXX
    from mutagen.mp4 import MP4FreeForm
    f = mutagen.File(fp)
    if f is None:
        raise ValueError("unreadable file")
    ext = fp.suffix.lower()
    if ext == '.flac':
        f['musicbrainz_albumid'] = mbid
    elif ext == '.m4a':
        if f.tags is None:
            f.add_tags()
        f.tags[MP4_MBID_KEY] = [MP4FreeForm(mbid.encode())]
    elif ext == '.mp3':
        if f.tags is None:
            f.add_tags()
        f.tags.delall('TXXX:MusicBrainz Album Id')
        f.tags.add(TXXX(encoding=3, desc='MusicBrainz Album Id', text=[mbid]))
    else:
        raise ValueError(f"unsupported type {ext}")
    f.save()


def quality_score(t: dict):
    return ({'.flac': 3, '.m4a': 2, '.mp3': 1}.get(t.get('extension'), 0), t.get('bitrate', 0))


def summarize(tagged: list[tuple[Path, dict]]) -> dict:
    """Disc/track map plus the folder's most common artist, album, MBID and its song titles."""
    discs: dict = {}
    for _, t in tagged:
        d, tr = t.get('disc'), t.get('track')
        if d is None or tr is None:
            continue
        v = discs.setdefault(d, {'tracks': set(), 'expected_total': None})
        v['tracks'].add(tr)
        if t.get('total_tracks'):
            v['expected_total'] = t['total_tracks']

    def most_common(field_fn):
        vals = [field_fn(t) for _, t in tagged if field_fn(t)]
        return Counter(vals).most_common(1)[0][0] if vals else None

    return {
        'discs': discs,
        'artist': most_common(lambda t: t.get('album_artist') or t.get('artist')),
        'album': most_common(lambda t: t.get('album')),
        'mbid': most_common(lambda t: t.get('mb_albumid')),
        'titles': [t['title'] for _, t in sorted(tagged, key=lambda x: (x[1].get('disc') or 1, x[1].get('track') or 0))
                   if t.get('title')],
    }


def scan_folder(folder: Path) -> dict:
    """summarize() for a folder on disk (used by the Review tab).

    Recursive so multi-disc destinations (Album/CD1, Album/CD2) are read too.
    """
    files = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS]
    info = summarize([(p, extract_tags(p) or {}) for p in files])
    info['files'] = files
    return info


# =============================================================================
# STAGE 2: MUSICBRAINZ MATCHING
# =============================================================================

musicbrainzngs.set_useragent(MB_APP_NAME, MB_APP_VERSION, MB_CONTACT)
musicbrainzngs.set_rate_limit(limit_or_interval=1.0, new_requests=1)
_mb_cache: dict = {}
_manual: dict = {}
_cache_dirty = False
_cache_lock = threading.Lock()


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        if path.exists():
            try:
                import time as _t
                path.replace(path.with_name(path.stem + f".corrupt-{_t.strftime('%Y%m%d%H%M%S')}" + path.suffix))
            except OSError:
                pass
        return {}


def _save_json(path: Path, data) -> None:
    if path.exists():
        try:
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        except OSError:
            pass
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)                    # atomic: a crash can't corrupt the file


def load_cache() -> None:
    global _mb_cache, _manual
    _mb_cache = _load_json(CACHE_FILE)
    _manual = _load_json(MANUAL_MATCHES_FILE)


def save_cache() -> None:
    global _cache_dirty
    if not _cache_dirty:
        return
    try:
        with _cache_lock:
            _save_json(CACHE_FILE, _mb_cache)
            _cache_dirty = False
    except Exception as e:
        print(f"  Warning: could not save MusicBrainz cache: {e}")


def _cache_put(key, value):
    global _cache_dirty
    with _cache_lock:
        _mb_cache[key] = value
        _cache_dirty = True


def match_key(artist, album) -> str:
    n = lambda s: re.sub(r"\s+", " ", str(s or "")).strip().lower()  # noqa: E731
    return f"{n(artist)}|||{n(album)}"


def remember_match(artist, album, mbid) -> None:
    """Record a release picked by hand so the same album is matched instantly next time."""
    if not _manual and MANUAL_MATCHES_FILE.exists():
        _manual.update(_load_json(MANUAL_MATCHES_FILE))
    _manual[match_key(artist, album)] = mbid
    _save_json(MANUAL_MATCHES_FILE, _manual)


_NOISE_WORDS = (r"deluxe|edition|remaster|expanded|anniversary|bonus|reissue|special|version|mono|stereo|"
                r"explicit|clean|disc|disk|cd\s*\d|vinyl|lp|web|flac|mp3|aac|\d{2,3}\s*k(bps)?|bootleg|import|"
                r"limited|collector|japan|japanese|us|uk|eu|promo|\d{4}")
_BRACKETED_NOISE = re.compile(r"\s*[\(\[\{][^\)\]\}]*\b(" + _NOISE_WORDS + r")\b[^\)\]\}]*[\)\]\}]", re.I)
_TRAILING_DISC = re.compile(r"\s*[-–:,]?\s*\b(disc|disk|cd)\s*\d+\s*$", re.I)
_FEAT = re.compile(r"\s+[\(\[]?(feat\.?|ft\.?|featuring)\s+.*$", re.I)


def clean_name(s) -> str:
    """'Album (Deluxe Edition) [2011 Remaster] CD1' -> 'Album'. Returns the input if nothing is left."""
    if not s:
        return s
    out = _BRACKETED_NOISE.sub("", str(s))
    out = _TRAILING_DISC.sub("", out)
    out = _FEAT.sub("", out)
    out = re.sub(r"\s+", " ", out).strip(" -–:,")
    return out or str(s)


def _disc_map_from_media(media: list) -> dict | None:
    """{disc number: track count}; bonus DVD/Blu-ray discs are skipped but keep their numbering."""
    out = {}
    for i, m in enumerate(media, 1):
        pos = int(m.get('position') or i)
        if any(v in str(m.get('format') or "").lower() for v in VIDEO_MEDIA):
            continue
        try:
            out[pos] = int(m.get('track-count') or len(m.get('track-list', [])) or 0)
        except (TypeError, ValueError):
            out[pos] = 0
    return out or None


def _slim(r: dict, score=None) -> dict:
    media = r.get('medium-list', [])
    fmts = [str(m.get('format') or "?") for m in media]
    return {
        'id': r['id'],
        'score': int(score if score is not None else r.get('ext:score', 0) or 0),
        'title': r.get('title', ""),
        'artist': r.get('artist-credit-phrase', ""),
        'date': r.get('date', ""),
        'country': r.get('country', ""),
        'format': " + ".join(f"{n}×{f}" if n > 1 else f for f, n in Counter(fmts).items()),
        'disc_map': _disc_map_from_media(media),
    }


def release_by_id(mbid: str) -> dict | None:
    key = f"id:{mbid}"
    if key in _mb_cache:
        return _mb_cache[key]
    try:
        r = musicbrainzngs.get_release_by_id(mbid, includes=['artists', 'media'])['release']
    except musicbrainzngs.ResponseError:
        _cache_put(key, None)            # a bad ID stays bad
        return None
    except Exception as e:
        print(f"  MusicBrainz lookup failed for release {mbid}: {e}")
        return None                      # network trouble: don't cache
    rel = _slim(r, score=100)
    if rel['disc_map']:
        rel['disc_map'] = {str(k): v for k, v in rel['disc_map'].items()}
    _cache_put(key, rel)
    return rel


def _int_map(dm) -> dict:
    return {int(k): v for k, v in (dm or {}).items()}


def search_candidates(artist, album) -> list[dict]:
    """Up to MB_SEARCH_LIMIT releases for artist + album, best score first. Cached."""
    key = f"s:{match_key(artist, album)}"
    if key in _mb_cache:
        return _mb_cache[key]
    try:
        res = musicbrainzngs.search_releases(artist=artist, release=album, limit=MB_SEARCH_LIMIT, strict=False)
    except Exception as e:
        print(f"  MusicBrainz search failed for '{artist} - {album}': {e}")
        return []                        # don't cache failures as "not found"
    cands = [_slim(r) for r in res.get('release-list', [])]
    for c in cands:
        if c['disc_map']:
            c['disc_map'] = {str(k): v for k, v in c['disc_map'].items()}
    _cache_put(key, cands)
    return cands


def assess(disc_map, discs: dict) -> str | None:
    """'complete' / 'incomplete' if the folder fits this release's discs and track counts, else None."""
    dm = _int_map(disc_map)
    if not dm or not discs or set(dm) != set(discs):
        return None
    for d, n in dm.items():
        if not n or max(discs[d]['tracks']) > n:
            return None
    complete = all(discs[d]['tracks'] == set(range(1, n + 1)) for d, n in dm.items())
    return "complete" if complete else "incomplete"


def missing_tracks(disc_map, discs: dict) -> dict:
    return {d: sorted(set(range(1, n + 1)) - discs.get(d, {'tracks': set()})['tracks'])
            for d, n in _int_map(disc_map).items()
            if set(range(1, n + 1)) - discs.get(d, {'tracks': set()})['tracks']}


def _full_map(c: dict) -> dict | None:
    """Search results sometimes omit track counts; fetch the release when they do."""
    dm = _int_map(c.get('disc_map'))
    if dm and all(dm.values()):
        return dm
    rel = release_by_id(c['id'])
    return _int_map(rel['disc_map']) if rel and rel.get('disc_map') else None


def _track_title_candidates(artist, titles) -> list[str]:
    """IDs of releases that several of the folder's song titles share, most shared first."""
    picks = [t for t in dict.fromkeys(clean_name(t) for t in titles) if t][:3]
    if len(picks) < 2:
        return []
    counts = Counter()
    for title in picks:
        key = f"rec:{match_key(artist, title)}"
        if key in _mb_cache:
            ids = _mb_cache[key]
        else:
            try:
                res = musicbrainzngs.search_recordings(artist=artist, recording=title, limit=25, strict=False)
            except Exception as e:
                print(f"  MusicBrainz track search failed for '{title}': {e}")
                return []
            ids = sorted({r['id'] for rec in res.get('recording-list', []) for r in rec.get('release-list', [])})
            _cache_put(key, ids)
        counts.update(ids)
    need = len(picks) if len(picks) == 2 else 2
    return [rid for rid, n in counts.most_common(10) if n >= need]


def find_release(artist, album, discs: dict, tag_mbid=None, titles=()) -> dict:
    """
    Returns {'result': True (complete) / False (incomplete) / None (no confident match),
             'release': dict or None, 'how': str, 'candidates': [top candidates for review]}
    """
    out = lambda res, rel, how, cands=None: {'result': res, 'release': rel, 'how': how,  # noqa: E731
                                             'candidates': (cands or ([rel] if rel else []))[:8]}

    # 1-2. Exact IDs: a match you made by hand, or an ID already in the tags
    for mbid, how in ((_manual.get(match_key(artist, album)), "your earlier match"), (tag_mbid, "MBID in tags")):
        if mbid:
            rel = release_by_id(mbid)
            if rel and rel.get('disc_map'):
                fit = assess(rel['disc_map'], discs)
                if fit:
                    return out(fit == "complete", rel, how)
                print(f"  {how} ({mbid}) doesn't fit this folder's discs/tracks; searching instead.")

    # 3. Album search: tags as-is, then with edition / disc / feat. noise removed
    if not artist or not album:
        return out(None, None, "missing artist/album tags")
    cands, seen = [], set()
    for a, al in dict.fromkeys([(artist, album), (clean_name(artist), clean_name(album))]):
        for c in search_candidates(a, al):
            if c['id'] not in seen:
                seen.add(c['id'])
                cands.append(c)
    cands.sort(key=lambda c: -c['score'])

    checked = 0
    for c in cands:                      # a release whose counts fit exactly, even at a lower score
        if c['score'] < MB_MIN_SCORE_COUNTS_MATCH:
            break
        dm = _full_map(c)
        checked += 1
        if dm and assess(dm, discs) == "complete":
            c['disc_map'] = {str(k): v for k, v in dm.items()}
            return out(True, c, f"search, counts match (score {c['score']})", cands)
        if checked >= 8:                 # each fetch costs a second; stop eventually
            break
    for c in cands:                      # high-score release the folder fits but is missing tracks from
        if c['score'] < MB_MIN_SCORE:
            break
        dm = _full_map(c)
        if dm and assess(dm, discs) == "incomplete":
            c['disc_map'] = {str(k): v for k, v in dm.items()}
            return out(False, c, f"search (score {c['score']}), tracks missing", cands)

    # 4. Track-title fallback
    if MB_TRACK_FALLBACK and titles:
        for rid in _track_title_candidates(clean_name(artist), titles):
            rel = release_by_id(rid)
            if rel and rel.get('disc_map') and assess(rel['disc_map'], discs) == "complete":
                return out(True, rel, "song titles", cands)

    # 5. Suspected missing disc(s). A whole disc can go missing when an album is
    # unpacked or fetched in parts. Tag-only checking can't see it: every disc the
    # folder *does* have is contiguous, so verify_via_tags would call it complete.
    # But the search found releases whose disc map covers all the folder's discs
    # plus more. Multi-disc folders of one album share the same album name, so this
    # only fires for folders that already hold at least two discs; and a Data/empty
    # medium in the release doesn't count as a missing disc. The result is a
    # suspicion, not a verdict: Incomplete + Review for a human to confirm.
    if len(discs) >= 2:
        checked = 0
        for c in cands:
            if c['score'] < MB_MIN_SCORE_COUNTS_MATCH:
                break
            dm = {d: n for d, n in (_full_map(c) or {}).items() if n}    # Data/empty media aren't discs
            checked += 1
            if all(d in dm for d in discs) and any(d not in discs for d in dm):
                extra = '+'.join(str(d) for d in sorted(dm) if d not in discs)
                c['disc_map'] = {str(k): v for k, v in dm.items()}
                return out(False, c, f"search (score {c['score']}), suspected missing disc(s) {extra}", cands)
            if checked >= 8:                 # each fetch costs a second; stop eventually
                break

    return out(None, None, "no release fits", cands)


# ----------------------------- review queue -----------------------------

_unmatched: dict = {}
_unmatched_dirty = False


def queue_for_review(folder: Path, info: dict, match: dict, reason: str) -> None:
    global _unmatched_dirty
    _unmatched[str(folder)] = {
        'folder': str(folder), 'artist': info['artist'], 'album': info['album'],
        'discs': {str(d): sorted(v['tracks']) for d, v in info['discs'].items()},
        'reason': reason, 'candidates': match.get('candidates', [])[:8],
        'added': time.strftime('%Y-%m-%d %H:%M'),
    }
    _unmatched_dirty = True


def load_unmatched() -> dict:
    return _load_json(UNMATCHED_FILE)


def save_unmatched(data: dict | None = None) -> None:
    """With data: overwrite (Review tab). Without: merge this run's additions into the file."""
    global _unmatched_dirty
    try:
        if data is not None:
            _save_json(UNMATCHED_FILE, data)
        elif _unmatched_dirty:
            merged = _load_json(UNMATCHED_FILE)
            merged.update(_unmatched)
            _save_json(UNMATCHED_FILE, merged)
            _unmatched_dirty = False
    except Exception as e:
        print(f"  Warning: could not save review queue: {e}")


# =============================================================================
# STAGE 2: CLEANUP
# =============================================================================

# Set by music_toolkit.py: progress callback and a threading.Event that stops beets.
# BEETS_PROGRESS(done_tracks, total_tracks, done_albums, total_albums, skipped, current_album)
BEETS_PROGRESS = None
CANCEL_EVENT = None
BEET_AUDIO_EXTS = {'.mp3', '.m4a', '.flac', '.ogg', '.opus', '.wma', '.aac', '.aiff', '.aif', '.wav',
                   '.ape', '.wv', '.mpc', '.alac', '.dsf'}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_BEET_ALBUM_LINE = re.compile(r"^(.*?)\s+\((\d+) items?\)\s*$")   # beets prints "<folder> (N items)" per album


def _beets_totals(source: Path) -> tuple[int, int]:
    tracks = albums = 0
    for root, _, files in os.walk(source):
        if is_protected(Path(root)):
            continue
        n = sum(1 for f in files if Path(f).suffix.lower() in BEET_AUDIO_EXTS)
        if n:
            tracks += n
            albums += 1
    return tracks, albums


def render_beets_config(template_text: str, values: dict) -> str:
    """Fill the __TOKEN__ placeholders in config.yaml. Path values get forward slashes (beets expects them);
    plain strings, such as the discogs token, are inserted as they are."""
    out = template_text
    for token, value in values.items():
        text = value.as_posix() if isinstance(value, Path) else str(value)
        out = out.replace(f"__{token}__", text)
    return out


def write_beets_runtime_config(dir_: Path, library: Path, import_log: Path, discogs_token: str = "") -> Path:
    """Write beets_runtime_config.yaml from the template, the configured paths and the discogs token."""
    text = BEETS_CONFIG_TEMPLATE.read_text(encoding="utf-8")
    rendered = render_beets_config(text, {"BEETS_DIR": dir_, "BEETS_LIBRARY": library,
                                          "BEETS_IMPORT_LOG": import_log,
                                          "DISCOGS_TOKEN": discogs_token.replace('"', "")})
    BEETS_RUNTIME_CONFIG.write_text(rendered, encoding="utf-8")
    return BEETS_RUNTIME_CONFIG


def run_beets_first_pass(source: Path) -> None:
    print(f"\n🎧 Running beets first pass on: {source}")
    if DRY_RUN:
        print("  [DRY RUN] skipping beets (it would move files).")
        return
    total_tracks, total_albums = _beets_totals(source)
    if not total_tracks:
        print("  Nothing for beets to import.")
        return
    print(f"  {total_albums} album folder(s), {total_tracks} track(s) to look up.")

    # Unbuffered + UTF-8 so beets' per-album lines arrive as they happen, not in 8 KB chunks.
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    try:
        BEETS_DIR.mkdir(parents=True, exist_ok=True)              # beets refuses to start without its log folder
        BEETS_IMPORT_LOG.parent.mkdir(parents=True, exist_ok=True)
        BEETS_LIBRARY.parent.mkdir(parents=True, exist_ok=True)
        cfg = write_beets_runtime_config(BEETS_DIR, BEETS_LIBRARY, BEETS_IMPORT_LOG, DISCOGS_TOKEN)
        proc = subprocess.Popen([BEET_EXECUTABLE, "-c", str(cfg), "import", *BEET_IMPORT_ARGS, str(source)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL,   # never hang on a prompt
                                text=True, encoding="utf-8", errors="replace", env=env)
    except FileNotFoundError:
        print(f"  ❌ '{BEET_EXECUTABLE}' not found on PATH; skipping beets pass.")
        return

    state = {'last_output': time.monotonic(), 'stopped': None}
    err_lines: list[str] = []

    def drain_stderr():
        for line in proc.stderr:
            err_lines.append(line)
            state['last_output'] = time.monotonic()

    def watchdog():
        # Stop if asked, or if beets has been silent for BEET_IMPORT_TIMEOUT seconds (stuck).
        # A big import can legitimately take hours, so total run time isn't limited.
        while proc.poll() is None:
            if CANCEL_EVENT is not None and CANCEL_EVENT.is_set():
                state['stopped'] = "stopped by user"
                proc.terminate()
                return
            if time.monotonic() - state['last_output'] > BEET_IMPORT_TIMEOUT:
                state['stopped'] = f"no output for {BEET_IMPORT_TIMEOUT // 60} min, looked stuck"
                proc.kill()
                return
            time.sleep(0.5)

    threading.Thread(target=drain_stderr, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()

    bar = None if BEETS_PROGRESS else tqdm(total=total_tracks, desc="beets", unit="track", dynamic_ncols=True)
    done_tracks = done_albums = skipped = 0
    current = ""

    def report():
        if BEETS_PROGRESS:
            BEETS_PROGRESS(min(done_tracks, total_tracks), total_tracks, done_albums, total_albums, skipped, current)
        else:
            bar.n = min(done_tracks, total_tracks)
            bar.set_postfix_str(f"{done_albums}/{total_albums} albums, {skipped} skipped", refresh=True)

    report()
    out_lines: list[str] = []
    for raw in proc.stdout:
        state['last_output'] = time.monotonic()
        line = _ANSI.sub("", raw).rstrip()
        out_lines.append(line)
        m = _BEET_ALBUM_LINE.match(line.strip())
        if m:
            current = Path(m.group(1).split(", ")[0]).name or m.group(1)
            done_tracks += int(m.group(2))
            done_albums += 1
            report()
        elif line.strip().lower().startswith("skipping"):
            skipped += 1
            report()
    proc.wait()
    if bar:
        bar.close()

    try:
        with open(BEET_IMPORT_LOG, "a", encoding="utf-8") as log:
            log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} | {source} ---\n")
            log.write("\n".join(out_lines) + "\n")
            if err_lines:
                log.write("\n[stderr]\n" + "".join(err_lines))
    except Exception as e:
        print(f"  Warning: could not write beets log: {e}")

    if state['stopped']:
        status = f"⚠️  stopped ({state['stopped']})"
    else:
        status = "✅ complete" if proc.returncode == 0 else f"⚠️  exited with code {proc.returncode}"
    print(f"  Beets pass {status}: looked at {done_albums} album(s), skipped {skipped}. "
          f"See {BEET_IMPORT_LOG.name}.\n")


def find_track_gap(discs: dict):
    for d in sorted(discs):
        tracks = discs[d]['tracks']
        if len(tracks) < 2:
            continue
        missing = sorted(set(range(1, max(tracks) + 1)) - tracks)
        if missing:
            return True, d, missing
    return False, None, None


def verify_via_tags(discs: dict) -> bool:
    return all(v['tracks'] == set(range(1, (v['expected_total'] or max(v['tracks'])) + 1))
               for v in discs.values())


def iter_album_groups(targets: list[tuple[Path, list[Path]]]):
    """Yield (root, sources, tagged) for each album to process.

    Sibling folders are treated as one album when they share artist + album tags
    and each folder holds a different disc number (the common Album/CD1, CD2
    layout). This lets MusicBrainz see the combined disc map instead of judging
    each disc as an incomplete one-disc album. Everything else is yielded as a
    normal single-folder album.
    """
    by_parent: dict[Path, list[tuple[Path, list[Path]]]] = defaultdict(list)
    for folder, music in targets:
        by_parent[folder.parent].append((folder, music))

    for parent, children in by_parent.items():
        prepared = []
        for folder, music in sorted(children, key=lambda x: x[0].name.lower()):
            if not folder.exists():
                continue
            music = [p for p in music if p.exists()]
            if not music:
                continue
            tagged = [(p, extract_tags(p) or {}) for p in music]
            prepared.append((folder, music, tagged, summarize(tagged)))

        if len(prepared) < 2:
            for folder, music, tagged, _ in prepared:
                yield folder, [(folder, music)], tagged
            continue

        keys, disc_sets = [], []
        for _, _, _, info in prepared:
            artist = str(info.get('artist') or "").strip()
            album = str(info.get('album') or "").strip()
            keys.append((clean_name(artist), clean_name(album)) if artist and album else None)
            disc_sets.append(set(info.get('discs') or {}))

        same_album = all(key is not None and key == keys[0] and key != ("", "") for key in keys)
        one_disc_each = all(len(discs) == 1 for discs in disc_sets)
        distinct_discs = len(set().union(*disc_sets)) == len(disc_sets)
        if same_album and one_disc_each and distinct_discs:
            print(f"\nMulti-disc album: {parent.name} ({len(prepared)} folders)")
            yield (parent,
                   [(folder, music) for folder, music, _, _ in prepared],
                   [item for _, _, tagged, _ in prepared for item in tagged])
        else:
            for folder, music, tagged, _ in prepared:
                yield folder, [(folder, music)], tagged


def process_folder(folder: Path, music_files: list[Path], stats: dict,
                   tagged: list[tuple[Path, dict]] | None = None,
                   sources: list[tuple[Path, list[Path]]] | None = None) -> None:
    """Analyze and route one album.

    ``folder`` is the album root, ``tagged`` is the optional pre-read tag list,
    and ``sources`` lists the folders to move (more than one for a multi-disc
    album). For a normal folder the defaults are just that folder.
    """
    sources = sources or [(folder, music_files)]
    multi = len(sources) > 1
    print(f"\nAnalyzing {'album' if multi else 'folder'}: {folder}")

    # 1-2. Tags + in-folder dedup
    if tagged is None:
        tagged = [(fp, extract_tags(fp) or {}) for fp in music_files]
    groups: dict = defaultdict(list)
    for fp, t in tagged:
        key = (t['disc'], t['track']) if t.get('disc') is not None and t.get('track') is not None \
            else f"orphan_{fp}"
        groups[key].append((fp, t))

    retained = []
    for key, items in groups.items():
        if len(items) > 1 and isinstance(key, tuple):
            items.sort(key=lambda x: quality_score(x[1]), reverse=True)
            retained.append(items[0])
            for fp, _ in items[1:]:
                print(f"  [DUPLICATE] Removing lower quality: {fp.name} (keeping {items[0][0].name})")
                try:
                    act(f"delete {fp}", os.remove, fp)
                    stats['dupes'] += 1
                except Exception as e:
                    print(f"  Failed to delete {fp.name}: {e}")
        else:
            retained.extend(items)

    # 3. Enough tags to query MusicBrainz?
    has_lookup_tags = bool(retained) and all(
        str(t.get('album_artist') or t.get('artist') or "").strip()
        and str(t.get('album') or "").strip()
        and t.get('track') is not None and t.get('disc') is not None
        for _, t in retained)

    info = summarize(retained)
    discs, artist, album = info['discs'], info['artist'], info['album']

    # 4. Obvious gap -> aaa_delete
    has_gap, gap_disc, missing = find_track_gap(discs)
    if has_gap:
        dest = unique_dest(DELETE_DIR / folder.name)
        print(f"  [GAP] Disc {gap_disc} missing track(s) {missing} -> {dest}")
        try:
            act(f"move {folder} -> {dest}", shutil.move, str(folder), str(dest))
            stats['gap'] += 1
        except Exception as e:
            print(f"  Failed to move to aaa_delete: {e}")
        return

    # 5. Completeness
    complete, source = False, "none (missing artist/album/track/disc tags)"
    match = {'result': None, 'candidates': []}
    if has_lookup_tags and discs:
        match = find_release(artist, album, discs, info['mbid'], info['titles'])
        if match['result'] is not None:
            rel = match['release']
            complete = match['result']
            source = f"musicbrainz via {match['how']}: {rel['artist']} - {rel['title']} [{rel['id']}]"
            stats['mb_' + match['how'].split(',')[0].split(' (')[0]] += 1
        elif FALLBACK_TO_TAGS_IF_NOT_FOUND:
            complete, source = verify_via_tags(discs), f"tags ({match['how']} on MusicBrainz)"
        else:
            source = f"unverifiable ({match['how']} on MusicBrainz)"
    print(f"  Completeness via {source}: {'COMPLETE' if complete else 'INCOMPLETE'}")

    # 6. Relocate (files only -- subfolders are visited separately by the walk)
    if complete:
        name_artist, name_album = artist, album
        if match.get('result') and match.get('release'):
            # name the folder after the release MusicBrainz matched: cleaner than raw tags
            name_artist, name_album = match['release']['artist'] or artist, match['release']['title'] or album
        dest = COMPLETE_DIR / f"{sanitize_folder_name(name_artist or 'Unknown Artist')} - " \
                              f"{sanitize_folder_name(name_album or folder.name)}"
        stats['complete'] += 1
    else:
        dest = INCOMPLETE_DIR / folder.name
        stats['incomplete'] += 1
    dest = unique_dest(dest)

    if DRY_RUN:
        print(f"  [DRY RUN] move {'album folders' if multi else 'folder files'} -> {dest}")
        return
    dest.mkdir(parents=True, exist_ok=True)
    if multi:
        # Keep each disc's folder name (CD1, CD2...) so track filenames can't collide.
        for src_dir, _ in sources:
            if not src_dir.exists():
                continue
            try:
                shutil.move(str(src_dir), str(dest / src_dir.name))
            except Exception as e:
                print(f"  Error moving folder {src_dir.name}: {e}")
    for item in folder.iterdir():
        if item.is_file():
            try:
                shutil.move(str(item), str(dest / item.name))
            except Exception as e:
                print(f"  Error moving {item.name}: {e}")
    print(f"  Result: -> {dest}")

    if not match.get('result'):          # anything MusicBrainz didn't confirm goes to the Review tab
        if match.get('release'):
            if "suspected missing disc" in match.get('how', ''):
                reason = f"MusicBrainz: {match['how']} — check whether a whole disc is missing"
            else:
                reason = f"MusicBrainz: tracks missing {missing_tracks(match['release']['disc_map'], discs)}"
        else:
            reason = f"MusicBrainz: {match.get('how', 'not searched')}"
            if complete:
                reason += "; complete by tags"
        queue_for_review(dest, info, match, reason)


def remove_empty_folders(source: Path) -> None:
    """Remove directories that are genuinely empty after processing.

    Deliberately conservative: a folder holding *any* file or subfolder is kept,
    even if it has no supported audio. That preserves WAV/OGG/APE albums,
    Scans/Artwork folders, .cue sheets, notes and cover art. Empty subfolders
    are removed bottom-up, so a parent that only contained empty folders still
    disappears.
    """
    print("\n🧹 Removing empty folders...")
    removed = 0
    for root, _, _ in os.walk(source, topdown=False):
        folder = Path(root)
        if folder == source or is_protected(folder) or not folder.exists() or folder.is_symlink():
            continue
        try:
            if any(folder.iterdir()):
                continue
        except OSError:
            continue
        print(f"  [REMOVE] empty folder {folder}")
        try:
            act(f"rmdir {folder}", os.rmdir, folder)
            if not DRY_RUN:
                removed += 1
        except OSError as e:
            print(f"  Failed to remove {folder}: {e}")
    if removed:
        print(f"  Removed {removed} empty folder(s).")


# Old name kept for callers/scripts; the behavior is now the safe version above.
remove_non_music_folders = remove_empty_folders



def run_cleanup(source: Path) -> None:
    print(f"\n{'=' * 60}\n  STAGE 2: LEFTOVER CLEANUP\n{'=' * 60}")
    load_cache()
    if RUN_BEETS_FIRST:
        run_beets_first_pass(source)
    for d in (COMPLETE_DIR, INCOMPLETE_DIR, DELETE_DIR):
        d.mkdir(parents=True, exist_ok=True)

    # Snapshot the folder list before touching anything: walking the tree while
    # moving folders out from under os.walk skips or errors on subfolders.
    targets = []
    for root, _, files in os.walk(source):
        folder = Path(root)
        if is_protected(folder):
            continue
        music = [folder / f for f in files if Path(f).suffix.lower() in SUPPORTED_EXTENSIONS]
        if music:
            targets.append((folder, music))

    stats = defaultdict(int)
    try:
        for root, sources, tagged in iter_album_groups(targets):
            if not root.exists():
                continue
            tagged = [(p, t) for p, t in tagged if p.exists()]
            if tagged:
                process_folder(root, [p for p, _ in tagged], stats,
                               tagged=tagged, sources=sources)
    finally:
        save_cache()                         # saved once (and on Ctrl+C), not per lookup
        save_unmatched()

    remove_empty_folders(source)
    print(f"\nSummary: {stats['complete']} complete, {stats['incomplete']} incomplete, "
          f"{stats['gap']} gap -> delete, {stats['dupes']} duplicate file(s) removed.")
    how = {k[3:]: v for k, v in stats.items() if k.startswith("mb_")}
    if how:
        print("MusicBrainz matched via: " + ", ".join(f"{k} {v}" for k, v in sorted(how.items())))
    if _unmatched:
        print(f"{len(_unmatched)} folder(s) queued for the Review tab ({UNMATCHED_FILE.name}).")


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    global DRY_RUN, USE_TRASH, AAC_BITRATE, MB_CONTACT
    ap = argparse.ArgumentParser(description="Convert FLAC to M4A, then dedup/verify/sort the folder.")
    ap.add_argument("folder", nargs="?", help="Music folder (prompted if omitted)")
    ap.add_argument("-y", "--non-interactive", action="store_true",
                    help="No prompts: default workers, convert single-FLAC folders without a .cue")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("FLAC_WORKERS", 0)) or None)
    ap.add_argument("--skip-convert", action="store_true")
    ap.add_argument("--skip-cleanup", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="Show what would happen; change nothing")
    ap.add_argument("--trash", action="store_true",
                    help="Send deleted files to the Recycle Bin instead of deleting permanently "
                         "(requires send2trash; refuses to run without it)")
    ap.add_argument("--contact", help="MusicBrainz contact (email or project URL)")
    ap.add_argument("--bitrate", help="AAC bitrate for conversion (default 256k)")
    ap.add_argument("--version", action="version", version=f"Music Organizer {__version__}")
    args = ap.parse_args()
    DRY_RUN = args.dry_run
    if args.trash:
        if send2trash is None:
            print("❌ --trash needs the send2trash package (pip install -r requirements-optional.txt).")
            sys.exit(1)
        USE_TRASH = True
    if args.contact:
        MB_CONTACT = args.contact
    if args.bitrate:
        AAC_BITRATE = args.bitrate
    if args.contact or args.bitrate:
        try:
            musicbrainzngs.set_useragent(MB_APP_NAME, MB_APP_VERSION, MB_CONTACT)
        except Exception:
            pass

    raw = args.folder
    while not raw or not Path(raw.strip().strip('"\'')).is_dir():
        if raw:
            print(f"❌ Not a directory: {raw}")
        if args.non_interactive:
            sys.exit(1)
        raw = input("Enter or drag-and-drop the music folder: ")
    folder = Path(raw.strip().strip('"\'')).resolve()

    if is_protected(folder):
        print("❌ Source is inside one of the output folders; refusing to run.")
        sys.exit(1)

    workers = args.workers
    if workers is None:
        if args.non_interactive:
            workers = DEFAULT_WORKERS
        else:
            r = input(f"Parallel conversion workers? [default {DEFAULT_WORKERS}]: ").strip()
            workers = int(r) if r.isdigit() and int(r) > 0 else DEFAULT_WORKERS

    for tool in ("ffmpeg", "ffprobe"):
        if not args.skip_convert and shutil.which(tool) is None:
            print(f"❌ {tool} not found on PATH.")
            sys.exit(1)

    print(f"\nSource: {folder}{'   (DRY RUN)' if DRY_RUN else ''}")
    lock = PipelineLock()
    try:
        lock.acquire()
    except PipelineBusy as e:
        print(f"❌ {e}")
        sys.exit(1)
    try:
        if not args.skip_convert:
            run_conversion(folder, workers, args.non_interactive)
        if not args.skip_cleanup:
            run_cleanup(folder)
        print("\nAll done!")
    finally:
        lock.release()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
