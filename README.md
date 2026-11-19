# Music Organizer

[![tests](https://github.com/gazornenphlat/Music-Organizer/actions/workflows/tests.yml/badge.svg)](https://github.com/gazornenphlat/Music-Organizer/actions/workflows/tests.yml)

A local toolkit for cleaning up a music library:

- Convert FLAC albums to M4A (AAC), verify the output, then remove the originals.
- Deduplicate, gap-check, and verify albums against MusicBrainz.
- Route albums into **complete**, **incomplete**, or **delete** folders.
- Review albums MusicBrainz could not confirm.
- Find duplicate songs and duplicate Plex movies/TV episodes.
- Report on tag and cover-art health.

It ships with three entry points that share the same engine and settings:

| Entry point | What it is |
| --- | --- |
| `music_toolkit.py` | Desktop app (Tkinter) |
| `toolkit_web.py` | Browser app (Flask + `web/`) |
| `music_pipeline.py` | Command-line pipeline |

## Requirements

- Python **3.10+**
- `ffmpeg` and `ffprobe` on `PATH` (required for FLAC conversion and duration checks)
- `beets` on `PATH` (optional; the pipeline can run its beets pass first)
- `fpcalc` on `PATH` (optional; only for audio-fingerprint duplicate checks)

## Install

```powershell
python -m pip install -r requirements.txt
```

Optional extras:

```powershell
python -m pip install -r requirements-optional.txt
```

That installs:

- `send2trash` — Recycle Bin support
- `plexapi` — the Plex tab
- `pyacoustid` — fingerprint duplicate checks
- `flask` — the web app

For development:

```powershell
python -m pip install -r requirements-dev.txt
```

## Run

Desktop app:

```powershell
python music_toolkit.py
```

Web app (this computer only):

```powershell
python toolkit_web.py
```

Reachable from a phone or another PC on the same network:

```powershell
python toolkit_web.py --lan
```

The `--lan` mode prints a URL containing an access token. Treat that link as a password: anyone who has it can move and delete files. Use `--port` to change the default port (`8765`), and `--no-browser` to stop it opening a browser tab.

Command-line pipeline:

```powershell
python music_pipeline.py "D:\Music\inbox"
python music_pipeline.py "D:\Music\inbox" --dry-run
python music_pipeline.py "D:\Music\inbox" --workers 6 -y
```

Useful flags:

- `--dry-run` — show what would happen; change nothing
- `--trash` — send deleted files to the Recycle Bin instead of deleting permanently (needs `send2trash` from `requirements-optional.txt`)
- `--skip-convert` — only run the cleanup stage
- `--skip-cleanup` — only run FLAC conversion
- `-y` — no prompts (useful for scheduled runs)
- `--contact EMAIL_OR_URL` — MusicBrainz contact (defaults to the placeholder in `music_pipeline.py`)
- `--bitrate 192k|256k|320k` — AAC bitrate for conversion (default `256k`)
- `--version` — print the version and exit

## Output folders

`music_pipeline.py` has defaults near the top:

```python
CUE_DEST       = M:\TO SORT\AAA_CUE
COMPLETE_DIR   = M:\sort_script\aaa_complete
INCOMPLETE_DIR = M:\sort_script\aaa_incomplete
DELETE_DIR     = M:\sort_script\aaa_delete
```

If those drives do not exist on your machine, change them in `music_pipeline.py` or use the desktop/web **Settings** tab, which saves overrides to `music_toolkit_settings.json` next to the scripts.

The settings file and history logs are generated at runtime and should not be committed.

## Safety model

- **Dry run** is available for every front end. The desktop app starts in dry-run mode.
- **Recycle Bin:** when enabled, deletes go to the Recycle Bin instead of being destroyed. This now includes:
  - converted FLAC originals
  - duplicate files
  - Plex file/folder cleanup
- If `send2trash` is not installed, the Recycle Bin option is disabled rather than silently falling back to permanent deletion.
- **Only empty folders are removed** after cleanup. A folder containing unsupported audio (WAV, OGG, APE, etc.), `Scans/`, artwork, `.cue` files, or notes is left in place.
- **One pipeline at a time:** the CLI, desktop app, and web app share `music_pipeline.lock`. A second pipeline is refused with a clear message instead of overwriting the first run's state.
- The pipeline runs beets first if enabled. Beets can move and reorganize files itself, so run a dry run before enabling it on a real library.

## Multi-disc albums

Stage 2 groups sibling folders into one album when they:

- share the same artist and album tags, and
- each contain a different disc number.

For example, `Album/CD1` and `Album/CD2` are analyzed together, so MusicBrainz sees the combined disc map instead of judging each disc as an incomplete one-disc album. The destination keeps the disc folders:

```text
Artist - Album/
  CD1/
  CD2/
```

If the tags do not include disc numbers, the folders are processed separately as before. This is a deliberate conservative fallback: merging the wrong folders is worse than leaving a multi-disc album split.

A whole missing disc is also caught: if MusicBrainz only finds releases whose discs cover every disc the folder has *plus* more, the album is flagged "suspected missing disc" and sent to Incomplete/Review instead of being marked complete by its own tags (each disc present is contiguous, so tag-only checking can't see it). This only fires for folders that already hold at least two discs, and an empty/Data medium in a release doesn't count as a missing disc.

## Screenshots

Web app (screenshots predate the Midnight Categorical restyle):

| Pipeline | Duplicates |
| --- | --- |
| ![Pipeline](docs/screenshots/pipeline.png) | ![Duplicates](docs/screenshots/dupes.png) |

| Review | Tag health |
| --- | --- |
| ![Review](docs/screenshots/review.png) | ![Tag health](docs/screenshots/health.png) |

| More tools | Settings |
| --- | --- |
| ![More](docs/screenshots/more.png) | ![Settings](docs/screenshots/settings.png) |

Desktop app (Tkinter, same engine and settings):

| Pipeline | Duplicates |
| --- | --- |
| ![Desktop pipeline](docs/screenshots/desktop-pipeline.png) | ![Desktop duplicates](docs/screenshots/desktop-dupes.png) |

| Tag health | Review |
| --- | --- |
| ![Desktop tag health](docs/screenshots/desktop-tag-health.png) | ![Desktop review](docs/screenshots/desktop-review.png) |

Mobile (390 px):

![Pipeline (mobile)](docs/screenshots/pipeline-mobile.png)
## Tests

```powershell
python -m pytest -q
```

The suite covers tag/path parsing, gap detection, completeness checks, folder grouping, safe empty-folder cleanup, the pipeline lock, trash routing, and the web `open` endpoint. Optional dependencies are stubbed by `tests/conftest.py`, so the tests run without a full MusicBrainz/Plex install.

## Files

- `music_pipeline.py` — conversion and cleanup engine, MusicBrainz matching, CLI
- `toolkit_core.py` — shared UI-free logic used by both front ends
- `music_toolkit.py` — desktop app
- `toolkit_web.py` — web server and JSON API
- `web/` — browser UI (`index.html`, `app.js`, `style.css`, `icons.js`, fonts)
- `fonts/` — desktop font files
- `tests/` — pytest suite
- `requirements*.txt` — dependencies

## Known limitations

- Output paths default to `M:\...`; set them before the first real run.
- `MB_CONTACT` in `music_pipeline.py` is a placeholder. Fill it in so MusicBrainz can identify the client.
- The web app uses Flask's development server. For long-running LAN use, put it behind a production WSGI server such as Waitress.
- The web access token is carried in the URL; do not share the `--lan` link.
- Running different jobs (e.g. duplicates and the pipeline) at the same time in both front ends is possible; only the pipeline is locked.
