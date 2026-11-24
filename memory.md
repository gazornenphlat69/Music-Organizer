# Music Organizer — Project Memory

## Overview
Local music/media library toolkit with three front ends sharing one engine:
- `music_pipeline.py` — engine + CLI (FLAC→M4A conversion, cleanup/sort, MusicBrainz matching)
- `toolkit_core.py` — UI-free shared logic (used by both UIs)
- `music_toolkit.py` — desktop Tkinter app
- `toolkit_web.py` — Flask web app + `web/` (HTML/CSS/JS)

Source of truth: GitHub repo `gazornenphlat69/Music-Organizer` (work on a branch, open a PR). The synced local copy at `T:\sync\PROJECTS\music_toolkit` is a plain copy with no `.git`. Older notes referred to `C:\Users\Bobu\Downloads\Music Organizer`; that path is obsolete.

## Key paths / constants
- Default output dirs (hardcoded in `music_pipeline.py`, overridable via settings): `M:\TO SORT\AAA_CUE`, `M:\sort_script\aaa_complete`, `aaa_incomplete`, `aaa_delete`.
- Settings file: `music_toolkit_settings.json` (next to scripts). History logs: `history/history_YYYY-MM-DD.log`.
- Caches: `mb_cache_v3.json`, `mb_manual_matches.json`, `mb_unmatched.json`.
- Pipeline lock: `music_pipeline.lock` (cross-process via msvcrt/fcntl + in-process threading.Lock).
- Protected dirs = the four output dirs (source must not be inside them).

## Safety model
- `DRY_RUN` global; `act(desc, fn, *a, **kw)` prints instead of running when dry.
- `remove_path(p, use_trash)` raises if trash requested but `send2trash` missing (no silent permanent delete).
- `remove_empty_folders` only removes genuinely empty dirs (preserves WAV/OGG/APE, Scans/, artwork, .cue, notes).
- Converted FLAC originals, duplicate deletes, Plex cleanup route through `act()`/`remove_path()` so Recycle Bin + history apply.
- `PipelineLock` prevents concurrent pipeline runs across CLI/desktop/web.

## MusicBrainz matching ladder (stage 2)
1. Manual matches (`mb_manual_matches.json`)
2. MBID already in tags
3. Album search (tags as-is, then noise-stripped); first release whose disc+track counts fit wins
4. Track-title search (2-3 shared titles)
5. Suspected missing disc: if a release covers all folder discs plus more (folder holds ≥2 discs), route to Incomplete/Review instead of trusting tags.

## Multi-disc grouping
- `iter_album_groups()` groups sibling folders sharing artist+album tags where each has a distinct single disc number.
- `process_folder(folder, music_files, stats, tagged=..., sources=...)` handles one or many source folders; multi-disc keeps `CD1`/`CD2` subfolders in destination.
- `scan_folder()` is recursive (for Review tab multi-disc support).
- Review re-check/move and desktop Plex scan were consolidated into `toolkit_core` helpers.

## Web design
Current theme: "Midnight Categorical Dashboard" (dark, adopted 2026-11-02, replacing "Ember Pill Mobile"). Files: `web/style.css`, `web/index.html`.
- Palette: see CLAUDE.md "UI / design". Base `#0F0F11`; coral primary, periwinkle secondary, butter accent, mint success; one hue per screen via `--cat`.
- Fonts: bundled Space Grotesk 700, Plus Jakarta Sans 500/700, JetBrains Mono 500 in `web/fonts/` (no Google Fonts link, per the no-CDN rule). Icons: Solar bold (bundled in `web/icons.js`).
- Shape language: 24 px cards, pill buttons/chips, 12 px icon tiles, flat hairlines, floating capsule tab bar.

## Desktop theme
`music_toolkit.py` `T` dict uses the same Midnight palette. Fonts: bundled Plus Jakarta Sans, Space Grotesk and JetBrains Mono in `fonts/` (loaded privately on Windows).

## Beets config and paths
- All library locations are configurable in Settings: output folders, beets directory, beets library database, beets import log, and the pipeline beets log. Defaults derive from `LIBRARY_ROOT` (`M:/`) in `music_pipeline.py`.
- `config.yaml` is a template with `__BEETS_DIR__`, `__BEETS_LIBRARY__` and `__BEETS_IMPORT_LOG__` tokens. The pipeline renders it to `beets_runtime_config.yaml` (git-ignored) and runs `beet -c` with it.
- Before this change the pipeline ran `beet` without `-c`, so it used the user's own beets config and ignored `config.yaml`.

## Testing
- `pytest.ini`: `testpaths = tests`. `tests/conftest.py` stubs `musicbrainzngs`, `tqdm`, `mutagen` if missing.
- Run: `python -m pytest -q` → **87 passed** (2026-11-24, after the Lyrics tool) (2026-11-20, after the token, Waitress and job-lock changes). `pyflakes` clean; `node --check web/app.js` clean.
- Tests cover the access-token check, the token redirect and in-process job locking (added 2026-11-20). Cross-process lock contention is not tested.
- Test files: `test_music_pipeline.py`, `test_toolkit_core.py`, `test_toolkit_web.py`.
- Optional deps in dev: `requirements-dev.txt` (core + pytest + flask).

## Dependencies
- Core: `tqdm`, `mutagen`, `musicbrainzngs` (requirements.txt).
- Optional: `send2trash`, `plexapi`, `pyacoustid`, `flask` (requirements-optional.txt).
- External binaries: `ffmpeg`/`ffprobe` (required), `beets` (optional), `fpcalc` (optional, for fingerprints).

## Recent changes (summary)
- Multi-disc album grouping + recursive scan_folder + review helpers.
- `/api/open` restricted to directories (400 for files).
- `remove_path` + Plex trash-aware delete (skips API delete when trashing mapped local file, then refreshes library).
- FLAC originals routed through `act()` (Recycle Bin/history/dry-run).
- `PipelineLock`/`PipelineBusy` cross-process.
- `remove_non_music_folders` → alias to safe `remove_empty_folders`.
- Missing-disc detection in `find_release`.
- Dry-run no longer prompts for single-FLAC folders.
- Stale `.partial.m4a` sweep at conversion start.
- Duplicate tag scan parallelized; web review read/write race fixed.
- CLI `--contact`, `--bitrate`, `--version` flags.
- Desktop settings reset uses `PIPELINE_DEFAULT_PATHS`.
- Consolidated Review/Plex scan+apply into `toolkit_core` (web + desktop both delegate).
- Checkbox visibility fixed in both UIs.
- Restyled both UIs to Ember Pill Mobile.

## Known limitations
- **Flask dev server (resolved 2026-11-20).** `toolkit_web.py` now serves with Waitress (8 threads) when it is installed (`requirements-optional.txt`). It falls back to the Flask dev server with a printed hint otherwise.
- **Access token in URL (resolved 2026-11-20).** A valid `?token=` on `/` sets the HttpOnly, SameSite=Strict cookie (30 days) and redirects to a clean URL, so the token leaves the address bar and history. `check_token` compares UTF-8 bytes, so non-ASCII input returns 403 instead of a 500. The token can still appear in shell history if passed via `--token`, and in the startup printout.
- **Partial locking (resolved 2026-11-20).** Every web job except the pipeline takes the same `PipelineLock` that `run_pipeline` uses (duplicate scan, tag health, review scan/recheck/move, Plex scan and apply). A job that can't get the lock returns HTTP 409. Quick synchronous endpoints do not take the lock: dupes delete, review accept/dismiss, and Plex connect. A pipeline run can overlap those.
- Default paths derive from `LIBRARY_ROOT` (`M:/`); every location is set in the Settings tab.
- `MB_CONTACT` still has its placeholder default. Set the MusicBrainz contact in Settings; the cleanup step refuses the placeholder.
- The Discogs token is stored in the settings file only when "Remember token on disk" is on. Otherwise it lasts for the session.
- Flask dev server; use waitress for LAN production.
- Web access token carried in URL.
- Only pipeline is locked; other jobs can overlap across front ends.
- Multi-disc grouping requires distinct disc tags (conservative fallback otherwise).
- Web and desktop bundle Plus Jakarta Sans, Space Grotesk and JetBrains Mono (`web/fonts/`, `fonts/`).
- Icons: `tools/build_icons.py` rebuilds `web/icons.js` from the pinned Solar 1.2.13 set. Three icons keep their previous bodies (`tools/legacy_icons.json`) because that release doesn't have them.
- Git: work on a branch and open a PR. The synced `T:\sync` folder is a plain copy, not a repo.

---

# Music Organizer — Session Work Log (memory2)

This file documents the changes made in this session, appended to memory.md.

## Follow-up 1: Multi-disc album grouping
- `music_pipeline.py`: added `iter_album_groups()`; `process_folder()` accepts `tagged=` and `sources=`; multi-disc destinations keep CD1/CD2 subfolders; gap/complete/incomplete/review operate on whole album.
- `scan_folder()` made recursive so multi-disc destinations are read.
- `toolkit_core.py` + `music_toolkit.py`: review re-check/move and `review_accept` updated to recurse (`rglob`) for nested disc folders.
- Tests: grouping tests, multi-disc routing test, scan_folder nested test, review recheck/move nested tests.

## Follow-up 2: Restrict `/api/open`
- `toolkit_web.py`: `/api/open` returns 400 for non-directories (404 for missing).
- Tests: file rejection + folder acceptance.

## Follow-up 3: Requirements + README
- Added `requirements.txt`, `requirements-optional.txt`, `requirements-dev.txt`, `README.md`, `.gitignore`, `pytest.ini`.

## Improvements done
1. `QueueWriter` now matches "Analyzing album:" too (history source fix for multi-disc).
2. Desktop `SettingsTab.reset_paths` uses `PIPELINE_DEFAULT_PATHS`.
3. Stale `.partial.m4a` sweep (`_remove_stale_partials`) at conversion start, dry-run summarized.
4. Missing-disc detection in `find_release` (suspected missing disc → Incomplete/Review).
5. Dry-run no longer prompts for single-FLAC folders.
6. Duplicate tag scan parallelized (`ThreadPoolExecutor`).
7. Web review read/write race fixed (snapshot dict under lock).
8. Settings label corrected; CLI `--contact`/`--bitrate`/`--version` flags.
9. `pyproject.toml` added; web smoke tests added.
10. Consolidated Review + Plex scan/apply into `toolkit_core` (web + desktop delegate).
11. Checkbox visibility fixed (web `.tick` + desktop ttk indicator).
12. Restyled both UIs to Ember Pill Mobile (light theme, Nunito Sans, ember orange).
13. `memory.md` created.

## Test count
Started at 37; ended at 50 passing. py_compile clean on all four Python files throughout.

## Lyrics tool (added 2026-11-24)
- Core (`toolkit_core.py`, UI-free): `lrclib_lookup` (LRCLIB `/api/get`, then `/api/search` with a 3 s duration check; stdlib `urllib`, no new dependency), `pick_lyrics_text`, `read_existing_lyrics`, `write_lyrics` (ID3 `USLT` for MP3, `\xa9lyr` for M4A, `LYRICS` Vorbis comment for FLAC/Ogg/Opus; synced LRC text goes in the same tag, like beets), `lyrics_scan`, `lyrics_apply`.
- Network failures raise `LyricsNetworkError` and are recorded per file as errors, never as "no lyrics".
- Existing lyrics are kept unless "Replace existing lyrics" is on. Dry run is the default and is never persisted.
- Writes log through `History.write("lyrics", "write", path)`. The Recycle Bin is not involved because nothing is deleted.
- Web: `/api/lyrics` (GET), `/api/lyrics/scan`, `/api/lyrics/apply`, `/api/lyrics/stop`; the `lyrics` job holds the shared `PipelineLock`. Settings: `lyrics_folder`, `lyrics_prefer_synced`, `lyrics_overwrite`.
- Desktop: `LyricsTab` in `music_toolkit.py`, between Tag health and Review.
- Verified: unit tests with ffmpeg-generated MP3/FLAC/M4A/Opus, web endpoints, the desktop tab under Xvfb (`python3.12` with Tk). Field names come from beets' LRCLIB client.
- Not verified: a live LRCLIB request. The build sandbox's proxy blocks lrclib.net. Also untested: Windows, and real libraries with existing lyrics from beets' `lyrics` plugin (PR #3).
