#!/usr/bin/env python3
"""
music_toolkit.py -- desktop app for the music & media tools (a web version is in toolkit_web.py).

  Pipeline    : FLAC -> M4A conversion + leftover cleanup (music_pipeline.py)
  Duplicates  : same-name dupes in a folder, or the same song anywhere (tags + optional fingerprint)
  Tag health  : per-album tag/cover report, predicts where the pipeline will route each folder
  Review      : albums MusicBrainz couldn't confirm; pick the right release once
  Plex        : duplicate Movies / TV episodes on a Plex server
  Settings    : output folders, Recycle Bin, history log

Keep these together: music_toolkit.py, toolkit_core.py, music_pipeline.py, fonts/
Run:  python music_toolkit.py

Requires : pip install tqdm mutagen musicbrainzngs
Optional : pip install send2trash   -> deletes go to the Recycle Bin
           pip install plexapi      -> enables the Plex tab
           pip install pyacoustid   -> fingerprint check in the Duplicates tab (also needs fpcalc on PATH)

Settings are saved to music_toolkit_settings.json next to these files and shared with the web app.
"""

import csv
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import defaultdict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import music_pipeline as mp
import toolkit_core as core
from toolkit_core import (ALL_PATH_SETTINGS, BEETS_PATH_SETTINGS, DEFAULT_TAGS, HISTORY_DEFAULT, MODES,
                          PATH_SETTINGS, PIPELINE_DEFAULT_PATHS, PRIORITIES, SETTINGS_FILE, History,
                          acoustid, album_health, find_duplicates, fmt_dur, fmt_size, remove_file,
                          run_pipeline, send2trash, _discs_counts, _discs_from_entry, _tracks_label)




# =============================================================================
# THEME: "Midnight Categorical Dashboard" (same tokens as web/style.css)
# =============================================================================

T = {   # Midnight Categorical Dashboard (same values as web/style.css)
    "bg": "#0F0F11", "surface": "#1A1A1C", "surface_alt": "#27272A", "surface_accent": "#3F3F46",
    "on_surface": "#FAFAFA", "muted": "#A1A1AA", "border": "#3F3F46", "primary": "#F7A5A5",
    "primary_pressed": "#E88F8F", "on_primary": "#1A1A1A", "success": "#4ADE80", "error": "#EF4444",
    "warning": "#FCE570", "secondary": "#8E99F3", "accent": "#FCE570", "violet": "#C084FC",
    "select": "#27272A", "disabled": "#52525B",
}
FONTS = {}   # filled by apply_theme() once Tk exists
FONT_DIR = Path(__file__).with_name("fonts")


def _load_private_fonts():
    """Make the bundled Plus Jakarta Sans / Space Grotesk / JetBrains Mono usable by Tk without installing them (Windows)."""
    if sys.platform != "win32" or not FONT_DIR.is_dir():
        return
    try:
        import ctypes
        FR_PRIVATE = 0x10
        for f in FONT_DIR.glob("*.ttf"):
            ctypes.windll.gdi32.AddFontResourceExW(str(f), FR_PRIVATE, 0)
    except Exception:
        pass


def apply_theme(root):
    import tkinter.font as tkfont
    fams = set(tkfont.families(root))
    fallback = "Segoe UI" if sys.platform == "win32" else "TkDefaultFont"
    body = "Plus Jakarta Sans Medium" if "Plus Jakarta Sans Medium" in fams else (
        "Plus Jakarta Sans" if "Plus Jakarta Sans" in fams else fallback)
    bold = "Plus Jakarta Sans" if "Plus Jakarta Sans" in fams else body      # labels: 700 weight
    head = "Space Grotesk" if "Space Grotesk" in fams else bold                # headings and numbers: 700
    mono = "JetBrains Mono" if "JetBrains Mono" in fams else ("Consolas" if sys.platform == "win32" else "TkFixedFont")
    FONTS.update(body=(body, 10), label=(bold, 10, "bold"), bold=(bold, 10, "bold"), caption=(body, 9),
                 log=(mono, 9), headline_sm=(head, 13, "bold"), headline=(head, 18, "bold"),
                 tab=(bold, 10, "bold"))

    root.configure(bg=T["bg"])
    # (no global "*Font": it would override ttk style fonts, e.g. the headline)
    for opt, val in (("*Text.font", FONTS["log"]), ("*TCombobox*Listbox.background", T["surface"]),
                     ("*TCombobox*Listbox.foreground", T["on_surface"]),
                     ("*TCombobox*Listbox.selectBackground", T["primary"]),
                     ("*TCombobox*Listbox.selectForeground", T["on_primary"]),
                     ("*TCombobox*Listbox.font", FONTS["body"]),
                     ("*Listbox.font", FONTS["body"]), ("*Listbox.background", T["surface"]), ("*Listbox.foreground", T["on_surface"]),
                     ("*Listbox.selectBackground", T["primary"]), ("*Listbox.selectForeground", T["on_primary"]),
                     ("*Listbox.relief", "flat"), ("*Listbox.highlightThickness", 1),
                     ("*Listbox.highlightBackground", T["border"])):
        root.option_add(opt, val)

    st = ttk.Style(root)
    st.theme_use("clam")               # the only built-in theme that honours custom colours everywhere
    bg, sf, sa, bd, on, mu, pr = T["bg"], T["surface"], T["surface_alt"], T["border"], T["on_surface"], T["muted"], T["primary"]
    st.configure(".", background=bg, foreground=on, fieldbackground=sf, bordercolor=bd, lightcolor=sf,
                 darkcolor=sf, troughcolor=sa, selectbackground=T["select"], selectforeground=on,
                 insertcolor=on, font=FONTS["body"], focuscolor=pr)
    st.configure("TFrame", background=bg)
    st.configure("TLabel", background=bg, foreground=on)
    st.configure("Muted.TLabel", foreground=mu)
    st.configure("Head.TLabel", font=FONTS["headline"])
    st.configure("TLabelframe", background=bg, bordercolor=bd, lightcolor=bd, darkcolor=bd, relief="solid", borderwidth=1)
    st.configure("TLabelframe.Label", background=bg, foreground=on, font=FONTS["headline_sm"])

    st.configure("TButton", background=sf, foreground=on, bordercolor=bd, lightcolor=sf, darkcolor=sf,
                 padding=(14, 7), font=FONTS["label"], relief="flat", focusthickness=0)
    st.map("TButton", background=[("disabled", sf), ("pressed", T["surface_accent"]), ("active", T["surface_accent"])],
           foreground=[("disabled", T["disabled"])], bordercolor=[("focus", bd)])
    st.configure("Accent.TButton", background=pr, foreground=T["on_primary"], bordercolor=pr, lightcolor=pr, darkcolor=pr)
    st.map("Accent.TButton", background=[("disabled", T["surface_alt"]), ("pressed", T["primary_pressed"]), ("active", T["primary_pressed"])],
           foreground=[("disabled", T["disabled"])], bordercolor=[("disabled", T["surface_alt"])])
    st.configure("Danger.TButton", foreground=T["error"])

    for w in ("TCheckbutton", "TRadiobutton"):
        st.configure(w, background=bg, foreground=on, indicatorbackground=T["surface_alt"], indicatorforeground=T["primary"],
                     upperbordercolor=T["muted"], lowerbordercolor=T["muted"], focusthickness=0, font=FONTS["body"])
        st.map(w, background=[("active", bg)], foreground=[("disabled", mu)])

    for w in ("TEntry", "TCombobox", "TSpinbox"):
        st.configure(w, fieldbackground=sf, foreground=on, bordercolor=bd, lightcolor=sf, darkcolor=sf,
                     arrowcolor=mu, padding=(8, 5), insertcolor=on)
        st.map(w, bordercolor=[("focus", pr)], lightcolor=[("focus", pr)],
               fieldbackground=[("readonly", sf), ("disabled", bg)], foreground=[("disabled", mu)],
               selectbackground=[("readonly", sf)], selectforeground=[("readonly", on)])

    st.configure("Treeview", background=sf, fieldbackground=sf, foreground=on, bordercolor=bd,
                 lightcolor=sf, darkcolor=sf, rowheight=26, font=FONTS["body"])
    st.map("Treeview", background=[("selected", T["select"])], foreground=[("selected", on)])
    st.configure("Treeview.Heading", background=sa, foreground=mu, bordercolor=bd, lightcolor=sa, darkcolor=sa,
                 relief="flat", font=FONTS["label"], padding=(6, 6))
    st.map("Treeview.Heading", background=[("active", T["surface_accent"])])

    st.configure("Horizontal.TProgressbar", troughcolor=sa, background=pr, bordercolor=sa,
                 lightcolor=pr, darkcolor=pr, thickness=6)

    st.configure("TNotebook", background=bg, bordercolor=bd, tabmargins=(12, 8, 12, 0))
    st.configure("TNotebook.Tab", background=bg, foreground=mu, bordercolor=bg, lightcolor=bg,
                 padding=(18, 9), font=FONTS["tab"])
    st.map("TNotebook.Tab", background=[("selected", sf), ("active", sf)], foreground=[("selected", pr), ("active", on)],
           bordercolor=[("selected", bd)], lightcolor=[("selected", sf)])

    for w in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        st.configure(w, background=sf, troughcolor=bg, bordercolor=bg, arrowcolor=mu, lightcolor=sf, darkcolor=sf,
                     gripcount=0)
        st.map(w, background=[("active", T["surface_accent"])])
    st.configure("TPanedwindow", background=bg)
    st.configure("Sash", sashthickness=6, background=bg)


ACCENT_PREFIXES = ("▶ Run", "🔍 Scan", "🔍 Re-check", "Connect", "✔ Accept", "✔ Move", "Search")
DANGER_PREFIXES = ("🗑",)


def style_buttons(widget):
    """Give each screen's main action the primary (blue) style and destructive actions red text."""
    for child in widget.winfo_children():
        if isinstance(child, ttk.Button):
            text = str(child.cget("text"))
            if text.startswith(ACCENT_PREFIXES):
                child.configure(style="Accent.TButton")
            elif text.startswith(DANGER_PREFIXES):
                child.configure(style="Danger.TButton")
        style_buttons(child)

# =============================================================================
# Shared plumbing
# =============================================================================

class Settings:
    def __init__(self):
        try:
            self.data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            self.data = {}
        self.vars: dict[str, tk.Variable] = {}

    def bind(self, key, var: tk.Variable):
        if key in self.data:
            try:
                var.set(self.data[key])
            except tk.TclError:
                pass
        self.vars[key] = var
        return var

    def save(self):
        for k, v in self.vars.items():
            try:
                self.data[k] = v.get()
            except tk.TclError:
                pass
        if not self.data.get("plex_remember_token"):
            self.data.pop("plex_token", None)
        try:
            tmp = SETTINGS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            tmp.replace(SETTINGS_FILE)
        except Exception as e:
            print(f"Could not save settings: {e}")




def init_shared(settings):
    """Variables several tabs share. Must run after the Tk root exists."""
    settings.paths = {attr: settings.bind(key, tk.StringVar(value=str(getattr(mp, attr))))
                      for attr, _, key, _kind in ALL_PATH_SETTINGS}
    settings.use_trash = settings.bind("use_trash", tk.BooleanVar(value=send2trash is not None))
    settings.pipe_trash = settings.bind("pipe_trash", tk.BooleanVar(value=send2trash is not None))
    settings.history = History(settings.bind("history_dir", tk.StringVar(value=HISTORY_DEFAULT)),
                               settings.bind("history_on", tk.BooleanVar(value=True)))
    apply_paths(settings)


def apply_paths(settings):
    """Push the Settings-tab folders into music_pipeline so every check uses them."""
    for attr, var in settings.paths.items():
        v = var.get().strip()
        if v:
            setattr(mp, attr, Path(v))
    mp.PROTECTED_DIRS = (mp.COMPLETE_DIR, mp.INCOMPLETE_DIR, mp.DELETE_DIR, mp.CUE_DEST)


class UiBus:
    """Worker threads never touch Tk; they post here and the main loop dispatches."""

    def __init__(self, root: tk.Tk):
        self.root, self.q, self.handlers = root, queue.Queue(), {}
        root.after(100, self._pump)

    def on(self, kind, fn):
        self.handlers[kind] = fn

    def post(self, kind, *payload):
        self.q.put((kind, payload))

    def _pump(self):
        try:
            for _ in range(500):
                kind, payload = self.q.get_nowait()
                if kind in self.handlers:
                    self.handlers[kind](*payload)
        except queue.Empty:
            pass
        self.root.after(100, self._pump)



def make_log(parent, height=14):
    log = _make_scrolled(parent, height)
    # swap Tk's classic scrollbar for the themed ttk one
    log.vbar.destroy()
    log.vbar = ttk.Scrollbar(log.frame, orient="vertical", command=log.yview)
    log.vbar.pack(side="right", fill="y")
    log.configure(yscrollcommand=log.vbar.set)
    log.frame.configure(bg=T["bg"])
    return log


def _make_scrolled(parent, height):
    return ScrolledText(parent, height=height, wrap="word", font=FONTS["log"], state="disabled",
                        bg=T["surface_alt"], fg=T["muted"], insertbackground=T["on_surface"], relief="flat",
                        highlightthickness=1, highlightbackground=T["border"], highlightcolor=T["primary"],
                        padx=10, pady=8, selectbackground=T["select"])


def log_append(log, text):
    log.configure(state="normal")
    log.insert("end", text)
    log.see("end")
    log.configure(state="disabled")


def folder_row(parent, label, var, row):
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 6))
    ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew")
    ttk.Button(parent, text="Browse…",
               command=lambda: var.set(filedialog.askdirectory() or var.get())
               ).grid(row=row, column=2, padx=(6, 0))


def file_row(parent, label, var, row):
    """Like folder_row, but for a single file (beets library database, log files)."""
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 6))
    ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew")
    ttk.Button(parent, text="Browse…",
               command=lambda: var.set(filedialog.askopenfilename() or var.get())
               ).grid(row=row, column=2, padx=(6, 0))


def open_in_explorer(path):
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
    except Exception as e:
        messagebox.showerror("Open", str(e))




class TickTree(ttk.Frame):
    """Treeview of groups -> items, each item tickable (☑ delete / ☐ keep)."""

    def __init__(self, master, columns, on_change=None):
        super().__init__(master)
        self.on_change = on_change or (lambda: None)
        self.rows: dict[str, dict] = {}
        self._n = 0
        cols = ("action",) + tuple(c for c, _ in columns)
        self.tree = ttk.Treeview(self, columns=cols, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="File")
        self.tree.column("#0", width=440)
        self.tree.heading("action", text="Action")
        self.tree.column("action", width=80)
        for c, w in columns:
            self.tree.heading(c, text=c.title())
            self.tree.column(c, width=w, anchor="w")
        for tag, color in (("keep", T["success"]), ("suspect", T["warning"]), ("review", T["violet"]), ("muted", T["muted"])):
            self.tree.tag_configure(tag, foreground=color)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.toggle(self.tree.selection()))
        self.tree.bind("<space>", lambda e: self.toggle(self.tree.selection()))

    def clear(self):
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()

    def _iid(self):
        self._n += 1
        return f"r{self._n}"

    def add_group(self, text, values=(), tags=(), action="", open_=True):
        return self.tree.insert("", "end", iid=self._iid(), text=text, open=open_, tags=tags,
                                values=(action,) + tuple(values))

    def add_item(self, parent, text, values, info, ticked, tags=()):
        iid = self.tree.insert(parent, "end", iid=self._iid(), text=text, tags=tags, values=("",) + tuple(values))
        info["ticked"] = ticked
        self.rows[iid] = info
        self.render(iid)
        return iid

    def render(self, iid):
        self.tree.set(iid, "action", "☑ delete" if self.rows[iid]["ticked"] else "☐ keep")

    def toggle(self, iids):
        for iid in iids:
            if iid in self.rows:
                self.rows[iid]["ticked"] = not self.rows[iid]["ticked"]
                self.render(iid)
        self.on_change()

    def set_all(self, fn):
        for iid, info in self.rows.items():
            info["ticked"] = bool(fn(info))
            self.render(iid)
        self.on_change()

    def ticked(self):
        return [(iid, i) for iid, i in self.rows.items() if i["ticked"]]

    def siblings(self, iid):
        return [self.rows[c] for c in self.tree.get_children(self.tree.parent(iid)) if c in self.rows]

    def remove(self, iid):
        parent = self.tree.parent(iid)
        self.tree.delete(iid)
        self.rows.pop(iid, None)
        if parent and not self.tree.get_children(parent):
            self.tree.delete(parent)


# =============================================================================
# TAB 1: PIPELINE
# =============================================================================



class PipelineTab(ttk.Frame):
    def __init__(self, master, bus, settings: Settings):
        super().__init__(master, padding=10)
        self.bus, self.running, self.slot_vars, self.settings = bus, False, [], settings
        self.cancel = threading.Event()
        S = settings.bind
        self.folder = S("pipe_folder", tk.StringVar())
        self.workers = S("pipe_workers", tk.IntVar(value=mp.DEFAULT_WORKERS))
        self.bitrate = S("pipe_bitrate", tk.StringVar(value=mp.AAC_BITRATE))
        self.single_mode = S("pipe_single", tk.StringVar(value="ask"))
        self.do_convert = S("pipe_convert", tk.BooleanVar(value=True))
        self.do_cleanup = S("pipe_cleanup", tk.BooleanVar(value=True))
        self.run_beets = S("pipe_beets", tk.BooleanVar(value=mp.RUN_BEETS_FIRST))
        self.dry_run = tk.BooleanVar(value=True)          # never remembered: every session starts safe
        self.contact = S("mb_contact", tk.StringVar(value=mp.MB_CONTACT))

        top = ttk.Frame(self)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        folder_row(top, "Music folder:", self.folder, 0)

        o = ttk.LabelFrame(self, text="Options", padding=8)
        o.pack(fill="x", pady=8)
        for i, (txt, var) in enumerate([("Stage 1: convert FLAC → M4A", self.do_convert),
                                        ("Stage 2: cleanup & sort", self.do_cleanup),
                                        ("Run beets first", self.run_beets),
                                        ("Dry run (change nothing)", self.dry_run)]):
            ttk.Checkbutton(o, text=txt, variable=var).grid(row=0, column=i, sticky="w")
        ttk.Label(o, text="Workers:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Spinbox(o, from_=1, to=32, textvariable=self.workers, width=5).grid(row=1, column=0, sticky="e", pady=(6, 0))
        ttk.Label(o, text="AAC bitrate:").grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Combobox(o, textvariable=self.bitrate, values=["192k", "256k", "320k"], width=6,
                     state="readonly").grid(row=1, column=1, sticky="e", pady=(6, 0))
        ttk.Label(o, text="Single-FLAC folders (no .cue):").grid(row=2, column=0, sticky="w", pady=(6, 0))
        for i, (val, txt) in enumerate([("ask", "Ask each time"), ("c", "Always convert"),
                                        ("m", "Always move to cue folder")]):
            ttk.Radiobutton(o, text=txt, value=val, variable=self.single_mode).grid(row=2, column=1 + i, sticky="w", pady=(6, 0))
        ttk.Label(o, text="MusicBrainz contact (email or URL):").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(o, textvariable=self.contact).grid(row=3, column=1, columnspan=3, sticky="ew", pady=(6, 0))

        bar = ttk.Frame(self)
        bar.pack(fill="x")
        self.run_btn = ttk.Button(bar, text="▶ Run pipeline", command=self.start)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="■ Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(bar, text="Open output folders", command=self._open_outputs).pack(side="left")
        self.status = ttk.Label(bar, text="Idle")
        self.status.pack(side="left", padx=10)

        self.total_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.total_var, font=FONTS["headline_sm"]).pack(anchor="w", pady=(8, 0))
        self.overall = ttk.Progressbar(self, maximum=100)
        self.overall.pack(fill="x", pady=(2, 4))
        self.slot_frame = ttk.Frame(self)
        self.slot_frame.pack(fill="x")
        self.log = make_log(self, 16)
        self.log.pack(fill="both", expand=True, pady=(6, 0))

        bus.on("log", lambda s: log_append(self.log, s))
        bus.on("slots_init", self._slots_init)
        bus.on("slot", self._slot)
        bus.on("overall", self._overall)
        bus.on("beets_progress", self._beets)
        bus.on("ask", self._ask)
        bus.on("pipeline_done", self._done)

    def _open_outputs(self):
        for d in (mp.COMPLETE_DIR, mp.INCOMPLETE_DIR, mp.DELETE_DIR):
            if d.exists():
                open_in_explorer(d)

    def _slots_init(self, n):
        for w in self.slot_frame.winfo_children():
            w.destroy()
        self.slot_vars = []
        for i in range(n):
            v = tk.StringVar(value=f"[{i+1}] idle")
            ttk.Label(self.slot_frame, textvariable=v, font=FONTS["caption"], style="Muted.TLabel").pack(anchor="w")
            self.slot_vars.append(v)

    def _slot(self, i, desc, pct):
        if i < len(self.slot_vars):
            self.slot_vars[i].set(f"[{i+1}] {pct:3d}%  {desc}")

    def _overall(self, pct, done, total, eta):
        self.overall.configure(value=pct)
        eta_txt = f"  ·  about {fmt_dur(eta)} left" if eta is not None else ""
        self.total_var.set(f"Total: {pct:5.1f}%  ·  {done} of {total} files converted{eta_txt}")
        self.status.configure(text=f"Converting {done}/{total}")

    def _beets(self, pct, albums_done, albums_total, skipped, current, eta):
        if self.slot_vars:                       # conversion worker lines don't apply to beets
            self._slots_init(0)
        self.overall.configure(value=pct)
        eta_txt = f"  ·  about {fmt_dur(eta)} left" if eta is not None else "  ·  estimating time left…"
        self.total_var.set(f"Beets: {pct:5.1f}%  ·  {albums_done} of {albums_total} albums looked up"
                           f"  ·  {skipped} skipped{eta_txt}")
        self.status.configure(text=f"Beets: {current}" if current else "Beets: looking up first album…")

    def _ask(self, folder, holder, event):
        if self.cancel.is_set():
            holder.append("c")
        else:
            yes = messagebox.askyesno(
                "Single-FLAC folder",
                f"{folder}\n\nThis folder holds just one FLAC.\n\n"
                "Yes = convert it to M4A\nNo = move the folder to the cue folder")
            holder.append("c" if yes else "m")
        event.set()

    def _done(self, err):
        self.running = False
        self.run_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        stopped = self.cancel.is_set()
        self.status.configure(text="Stopped" if stopped else ("Failed — see log" if err else "Done"))
        if err and not stopped:
            log_append(self.log, f"\n❌ {err}\n")

    def stop(self):
        self.cancel.set()
        self.stop_btn.configure(state="disabled")
        self.status.configure(text="Stopping after the current file/folder…")

    def start(self):
        if self.running:
            return
        folder = Path(self.folder.get().strip().strip('"'))
        if not folder.is_dir():
            messagebox.showerror("Pipeline", "Pick a valid music folder first.")
            return
        apply_paths(self.settings)
        self.settings.history.snapshot()
        self.trash = self.settings.pipe_trash.get() and send2trash is not None
        if mp.is_protected(folder):
            messagebox.showerror("Pipeline", "That folder is inside one of the output folders.")
            return
        if self.do_convert.get() and not self.dry_run.get():
            missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
            if missing:
                messagebox.showerror("Pipeline", f"Not found on PATH: {', '.join(missing)}")
                return
        contact = self.contact.get().strip()
        if self.do_cleanup.get() and (not contact or "your-email" in contact):
            if not messagebox.askokcancel("Pipeline", "MusicBrainz contact is still the placeholder.\n"
                                          "MusicBrainz may throttle or block anonymous clients.\nRun anyway?"):
                return
        if not self.dry_run.get() and not messagebox.askokcancel(
                "Pipeline", "This is a LIVE run: files will be converted, moved and deleted.\nContinue?"):
            return
        self.cancel.clear()
        self.running = True
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status.configure(text="Running…")
        opts = dict(convert=self.do_convert.get(), cleanup=self.do_cleanup.get(), dry_run=self.dry_run.get(),
                    bitrate=self.bitrate.get(), run_beets=self.run_beets.get(), contact=self.contact.get(),
                    workers=int(self.workers.get()), single_mode=self.single_mode.get(), trash=self.trash)
        threading.Thread(target=self._worker, args=(folder.resolve(), opts), daemon=True).start()

    def _worker(self, folder, opts):
        bus = self.bus

        def ask(folder_name):
            holder, ev = [], threading.Event()
            bus.post("ask", folder_name, holder, ev)
            ev.wait()
            return holder[0]

        run_pipeline(folder, opts, bus, self.settings.history, self.cancel, ask)


# =============================================================================
# TAB 2: MUSIC DUPLICATES
# =============================================================================



class DupesTab(ttk.Frame):
    def __init__(self, master, bus, settings: Settings):
        super().__init__(master, padding=10)
        self.bus, self.cancel = bus, threading.Event()
        S = settings.bind
        self.folder = S("dupes_folder", tk.StringVar())
        self.priority = S("dupes_priority", tk.StringVar(value=next(iter(PRIORITIES))))
        self.mode = S("dupes_mode", tk.StringVar(value=next(iter(MODES))))
        self.check_dur = S("dupes_duration", tk.BooleanVar(value=True))
        self.use_fp = S("dupes_fp", tk.BooleanVar(value=False))
        self.use_trash, self.history = settings.use_trash, settings.history

        top = ttk.Frame(self)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        folder_row(top, "Music folder:", self.folder, 0)

        o = ttk.Frame(self)
        o.pack(fill="x", pady=(8, 0))
        ttk.Label(o, text="Match:").pack(side="left")
        ttk.Combobox(o, textvariable=self.mode, values=list(MODES), state="readonly", width=34).pack(side="left", padx=6)
        ttk.Label(o, text="Keep:").pack(side="left")
        ttk.Combobox(o, textvariable=self.priority, values=list(PRIORITIES), state="readonly", width=44).pack(side="left", padx=6)
        o2 = ttk.Frame(self)
        o2.pack(fill="x", pady=(4, 8))
        ttk.Checkbutton(o2, text="Compare durations", variable=self.check_dur).pack(side="left")
        fp = ttk.Checkbutton(o2, text="Verify with audio fingerprint (slow)", variable=self.use_fp)
        fp.pack(side="left", padx=10)
        if acoustid is None:
            fp.configure(state="disabled", text="Fingerprint (pip install pyacoustid + fpcalc)")
            self.use_fp.set(False)
        tr = ttk.Checkbutton(o2, text="Send to Recycle Bin", variable=self.use_trash)
        tr.pack(side="left")
        if send2trash is None:
            tr.configure(state="disabled", text="Recycle Bin (pip install send2trash)")

        b = ttk.Frame(self)
        b.pack(fill="x")
        self.scan_btn = ttk.Button(b, text="🔍 Scan", command=self.scan)
        self.scan_btn.pack(side="left")
        self.stop_btn = ttk.Button(b, text="■ Stop", command=self.cancel.set, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(b, text="Tick all safe", command=lambda: self.tt.set_all(lambda i: not i["suspect"])).pack(side="left", padx=4)
        ttk.Button(b, text="Untick all", command=lambda: self.tt.set_all(lambda i: False)).pack(side="left")
        ttk.Button(b, text="Export report…", command=self.export).pack(side="left", padx=4)
        ttk.Button(b, text="🗑 Delete ticked", command=self.delete).pack(side="right")

        self.summary = ttk.Label(self, text="Scan a folder. Double-click or press Space to tick/untick a file.")
        self.summary.pack(fill="x", pady=(6, 2))
        self.progress = ttk.Progressbar(self, maximum=1)
        self.progress.pack(fill="x")
        self.tt = TickTree(self, [("format", 60), ("size", 80), ("duration", 70), ("note", 240)], self._update_summary)
        self.tt.pack(fill="both", expand=True, pady=(6, 0))

        bus.on("dupes_progress", self._progress)
        bus.on("dupes_result", self._show)

    def _progress(self, n, t, label):
        self.progress.configure(maximum=max(t, 1), value=n)
        if label:
            self.summary.configure(text=f"{label} {n}/{t}")

    def scan(self):
        root = Path(self.folder.get().strip().strip('"'))
        if not root.is_dir():
            messagebox.showerror("Duplicates", "Pick a valid folder first.")
            return
        self.cancel.clear()
        self.scan_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        args = (root, PRIORITIES[self.priority.get()], MODES[self.mode.get()],
                self.check_dur.get(), self.use_fp.get())

        def work():
            try:
                res = find_duplicates(*args, progress=lambda n, t, l: self.bus.post("dupes_progress", n, t, l),
                                      cancel=self.cancel)
                self.bus.post("dupes_result", res, None)
            except Exception as e:
                self.bus.post("dupes_result", [], repr(e))

        threading.Thread(target=work, daemon=True).start()

    def _show(self, results, err):
        self.scan_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.tt.clear()
        if err:
            self.summary.configure(text=f"Scan failed: {err}")
            return
        cross = MODES[self.mode.get()] == "tags"
        for keep, rest in results:
            g = self.tt.add_group(str(keep["path"]), (keep["path"].suffix.lower(), fmt_size(keep["size"]),
                                                      fmt_dur(keep["duration"]), ""), tags=("keep",), action="KEEP")
            for c in rest:
                self.tt.add_item(g, str(c["path"]) if cross else c["path"].name,
                                 (c["path"].suffix.lower(), fmt_size(c["size"]), fmt_dur(c["duration"]), c["note"]),
                                 c, ticked=not c["suspect"], tags=("suspect",) if c["suspect"] else ())
        self._update_summary(stopped=self.cancel.is_set())

    def _update_summary(self, stopped=False):
        ticked = [i for _, i in self.tt.ticked()]
        sus = sum(1 for i in self.tt.rows.values() if i["suspect"])
        self.summary.configure(
            text=("Stopped early — results are partial · " if stopped else "")
            + f"{len(self.tt.tree.get_children())} duplicate set(s) · {len(ticked)} ticked "
              f"({fmt_size(sum(i['size'] for i in ticked))})" + (f" · {sus} flagged" if sus else ""))

    def export(self):
        if not self.tt.rows:
            return
        out = filedialog.askdirectory(title="Folder for dupes_report.txt / dupes_to_delete.txt")
        if not out:
            return
        out = Path(out)
        with open(out / "dupes_report.txt", "w", encoding="utf-8") as rf, \
             open(out / "dupes_to_delete.txt", "w", encoding="utf-8") as dl:
            for g in self.tt.tree.get_children():
                rf.write(f"KEEP  : {self.tt.tree.item(g, 'text')}\n")
                for c in self.tt.tree.get_children(g):
                    i = self.tt.rows[c]
                    rf.write(f"{'DELETE' if i['ticked'] else 'SKIP  '}: {i['path']}  "
                             f"({fmt_size(i['size'])}, {fmt_dur(i['duration'])}) {i['note']}\n")
                    if i["ticked"]:
                        dl.write(f"{i['path']}\n")
                rf.write("\n")
        messagebox.showinfo("Duplicates", f"Saved to {out}")

    def delete(self):
        ticked = self.tt.ticked()
        if not ticked:
            return
        trash = self.use_trash.get() and send2trash is not None
        self.history.snapshot()
        if not messagebox.askyesno("Duplicates", f"{'Move to the Recycle Bin' if trash else 'PERMANENTLY delete'} "
                                   f"{len(ticked)} file(s) ({fmt_size(sum(i['size'] for _, i in ticked))})?"):
            return
        ok, errors = 0, []
        for iid, info in ticked:
            try:
                if info["path"].exists():
                    remove_file(info["path"], trash)
                    self.history.write("duplicates", "recycle" if trash else "delete", info["path"])
                    ok += 1
                else:
                    errors.append(f"{info['path']} (already gone)")
                self.tt.remove(iid)
            except Exception as e:
                errors.append(f"{info['path']}: {e}")
        self._update_summary()
        msg = f"Removed {ok} file(s)." + (f"\n\n{len(errors)} problem(s):\n" + "\n".join(errors[:15]) if errors else "")
        messagebox.showinfo("Duplicates", msg)


# =============================================================================
# TAB 3: TAG HEALTH
# =============================================================================



class TagHealthTab(ttk.Frame):
    def __init__(self, master, bus, settings: Settings):
        super().__init__(master, padding=10)
        self.bus, self.cancel, self.results = bus, threading.Event(), []
        self.folder = settings.bind("health_folder", tk.StringVar())
        self.only_issues = settings.bind("health_only_issues", tk.BooleanVar(value=True))

        top = ttk.Frame(self)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        folder_row(top, "Music folder:", self.folder, 0)
        b = ttk.Frame(self)
        b.pack(fill="x", pady=8)
        self.scan_btn = ttk.Button(b, text="🔍 Scan", command=self.scan)
        self.scan_btn.pack(side="left")
        self.stop_btn = ttk.Button(b, text="■ Stop", command=self.cancel.set, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Checkbutton(b, text="Only folders with issues", variable=self.only_issues,
                        command=self._render).pack(side="left", padx=8)
        ttk.Button(b, text="Export CSV…", command=self.export).pack(side="right")

        self.summary = ttk.Label(self, text="Double-click a row to open the folder. "
                                            "* = complete by its own tags; MusicBrainz still has the final say.")
        self.summary.pack(fill="x")
        self.progress = ttk.Progressbar(self, maximum=1)
        self.progress.pack(fill="x", pady=(2, 6))

        cols = (("files", 50), ("artist", 160), ("album", 200), ("route", 140), ("issues", 380))
        self.tree = ttk.Treeview(self, columns=[c for c, _ in cols], show="tree headings")
        self.tree.heading("#0", text="Folder")
        self.tree.column("#0", width=260)
        for c, w in cols:
            self.tree.heading(c, text=c.title(), command=lambda c=c: self._sort(c))
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("bad", foreground=T["error"])
        self.tree.tag_configure("warn", foreground=T["warning"])
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._open)
        self._sort_key, self._iid_map = None, {}

        bus.on("health_progress", lambda n, t: self.progress.configure(maximum=max(t, 1), value=n))
        bus.on("health_done", self._done)

    def scan(self):
        root = Path(self.folder.get().strip().strip('"'))
        if not root.is_dir():
            messagebox.showerror("Tag health", "Pick a valid folder first.")
            return
        self.cancel.clear()
        self.scan_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

        def work():
            res = []
            try:
                folders = []
                for dp, _, fs in os.walk(root):
                    music = [Path(dp) / f for f in fs if Path(f).suffix.lower() in mp.SUPPORTED_EXTENSIONS]
                    if music:
                        folders.append((Path(dp), music))
                for i, (d, music) in enumerate(folders):
                    if self.cancel.is_set():
                        break
                    self.bus.post("health_progress", i + 1, len(folders))
                    try:
                        res.append(album_health(d, music))
                    except Exception as e:
                        res.append({"folder": d, "files": len(music), "artist": "", "album": "",
                                    "issues": [f"error: {e}"], "route": "?"})
                self.bus.post("health_done", res, None)
            except Exception as e:
                self.bus.post("health_done", res, repr(e))

        threading.Thread(target=work, daemon=True).start()

    def _done(self, res, err):
        self.scan_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.results = res
        self._render()
        if err:
            self.summary.configure(text=f"Scan failed: {err}")

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        self._iid_map = {}
        rows = [r for r in self.results if r["issues"] or not self.only_issues.get()]
        if self._sort_key:
            rows.sort(key=lambda r: str(r.get(self._sort_key, "")).lower())
        for r in rows:
            tag = "bad" if r["route"].startswith("→") else ("warn" if r["issues"] else "")
            iid = self.tree.insert("", "end", text=r["folder"].name, tags=(tag,),
                                   values=(r["files"], r["artist"], r["album"], r["route"], "; ".join(r["issues"])))
            self._iid_map[iid] = r
        bad = sum(1 for r in self.results if r["issues"])
        routes = defaultdict(int)
        for r in self.results:
            routes[r["route"]] += 1
        self.summary.configure(text=f"{len(self.results)} folder(s), {bad} with issues · "
                               + " · ".join(f"{k}: {v}" for k, v in sorted(routes.items())))

    def _sort(self, col):
        self._sort_key = col
        self._render()

    def _open(self, _e):
        sel = self.tree.selection()
        if sel and sel[0] in self._iid_map:
            open_in_explorer(self._iid_map[sel[0]]["folder"])

    def export(self):
        if not self.results:
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="tag_health.csv",
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["folder", "files", "artist", "album", "predicted route", "issues"])
            for r in self.results:
                w.writerow([r["folder"], r["files"], r["artist"], r["album"], r["route"], "; ".join(r["issues"])])
        messagebox.showinfo("Tag health", f"Saved {path}")


# =============================================================================
# TAB: REVIEW (albums MusicBrainz couldn't confirm)
# =============================================================================



class LyricsTab(ttk.Frame):
    """Fetch lyrics from LRCLIB for tracks that have none and write the ones you pick into the files.
    The lookup and tag writing live in toolkit_core; this tab only collects choices and shows results."""

    STATUS_TEXT = {"found": "Found", "has_lyrics": "Has lyrics", "not_found": "Not found", "error": "Error"}

    def __init__(self, master, bus, settings: Settings):
        super().__init__(master, padding=10)
        self.bus, self.settings_obj, self.cancel = bus, settings, threading.Event()
        self.items, self.texts = [], {}
        self.folder = settings.bind("lyrics_folder", tk.StringVar())
        self.prefer_synced = settings.bind("lyrics_prefer_synced", tk.BooleanVar(value=True))
        self.overwrite = settings.bind("lyrics_overwrite", tk.BooleanVar(value=False))
        self.dry = tk.BooleanVar(value=True)            # never persisted: a live write needs an explicit untick

        top = ttk.Frame(self)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        folder_row(top, "Music folder:", self.folder, 0)
        opts = ttk.Frame(self)
        opts.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(opts, text="Prefer synced lyrics", variable=self.prefer_synced).pack(side="left")
        ttk.Checkbutton(opts, text="Replace existing lyrics", variable=self.overwrite).pack(side="left", padx=8)
        b = ttk.Frame(self)
        b.pack(fill="x", pady=8)
        self.scan_btn = ttk.Button(b, text="🔍 Scan", command=self.scan)
        self.scan_btn.pack(side="left")
        self.stop_btn = ttk.Button(b, text="■ Stop", command=self.cancel.set, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Checkbutton(b, text="Dry run (change nothing)", variable=self.dry).pack(side="left", padx=8)
        self.apply_btn = ttk.Button(b, text="Write selected", style="Accent.TButton", command=self.apply,
                                    state="disabled")
        self.apply_btn.pack(side="right")

        self.summary = ttk.Label(self, text="Select rows (Ctrl/Shift-click) to choose which tracks get lyrics. "
                                            "Found rows are selected after a scan.")
        self.summary.pack(fill="x")
        self.progress = ttk.Progressbar(self, maximum=1)
        self.progress.pack(fill="x", pady=(2, 6))

        cols = (("status", 110), ("artist", 160), ("title", 220), ("album", 200), ("note", 240))
        self.tree = ttk.Treeview(self, columns=[c for c, _ in cols], show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="File")
        self.tree.column("#0", width=200)
        for c, w in cols:
            self.tree.heading(c, text=c.title())
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("found", foreground=T["success"])
        self.tree.tag_configure("bad", foreground=T["error"])
        self.tree.tag_configure("muted", foreground=T["muted"])
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        bus.on("lyrics_progress", lambda n, t, label: self.progress.configure(maximum=max(t, 1), value=n))
        bus.on("lyrics_scanned", self._scanned)
        bus.on("lyrics_applied", self._applied)

    def scan(self):
        root = Path(self.folder.get().strip().strip('"'))
        if not root.is_dir():
            messagebox.showerror("Lyrics", "Pick a valid folder first.")
            return
        prefer, overwrite = self.prefer_synced.get(), self.overwrite.get()   # read Tk vars on the main thread
        self.cancel.clear()
        self.scan_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.apply_btn.configure(state="disabled")

        def work():
            try:
                items, texts = core.lyrics_scan(
                    root, prefer, overwrite,
                    progress=lambda n, t, label: self.bus.post("lyrics_progress", n, t, label),
                    cancel=self.cancel)
                self.bus.post("lyrics_scanned", items, texts, None)
            except Exception as e:
                self.bus.post("lyrics_scanned", [], {}, repr(e))

        threading.Thread(target=work, daemon=True).start()

    def _scanned(self, items, texts, err):
        self.scan_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.items, self.texts = items, texts
        self._render(select_found=True)
        if err:
            self.summary.configure(text=f"Scan failed: {err}")

    def apply(self):
        chosen_ids = [iid for iid in self.tree.selection() if self._item(iid) and self._item(iid)["status"] == "found"]
        if not chosen_ids:
            messagebox.showinfo("Lyrics", "Select at least one Found row.")
            return
        dry = self.dry.get()                        # Tk var read on the main thread
        chosen = [self._item(i) for i in chosen_ids]
        self.cancel.clear()
        self.apply_btn.configure(state="disabled")
        self.scan_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

        def work():
            try:
                done, errors = core.lyrics_apply(
                    chosen, self.texts, dry_run=dry, history=self.settings_obj.history,
                    progress=lambda n, t, label: self.bus.post("lyrics_progress", n, t, label),
                    cancel=self.cancel)
                self.bus.post("lyrics_applied", done, errors, dry, None)
            except Exception as e:
                self.bus.post("lyrics_applied", [], [], dry, repr(e))

        threading.Thread(target=work, daemon=True).start()

    def _applied(self, done, errors, dry, err):
        self.scan_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        done_set = set(done)
        if not dry:
            for it in self.items:
                if it["id"] in done_set:
                    it.update(status="has_lyrics", replaces=False, message="lyrics written")
                    self.texts.pop(it["id"], None)
        self._render(select_found=False)
        if err:
            self.summary.configure(text=f"Write failed: {err}")
            return
        extra = f" {len(errors)} failed: " + "; ".join(errors[:3]) if errors else ""
        verb = "would get lyrics (dry run, nothing changed)" if dry else "wrote lyrics to"
        self.summary.configure(text=f"{len(done)} file(s) {verb}.{extra}")

    def _item(self, iid):
        return next((i for i in self.items if i["id"] == iid), None)

    def _render(self, select_found=False):
        self.tree.delete(*self.tree.get_children())
        for it in self.items:
            tag = {"found": "found", "error": "bad"}.get(it["status"], "muted" if it["status"] == "has_lyrics" else "")
            note = "; ".join(x for x in (it.get("message") or "", "synced" if it.get("synced") else "") if x)
            self.tree.insert("", "end", iid=it["id"], text=it["name"], tags=(tag,),
                             values=(self.STATUS_TEXT.get(it["status"], it["status"]), it.get("artist", ""),
                                     it.get("title", ""), it.get("album", ""), note))
        if select_found:
            found = [it["id"] for it in self.items if it["status"] == "found"]
            self.tree.selection_set(found)
        counts = defaultdict(int)
        for it in self.items:
            counts[it["status"]] += 1
        self.apply_btn.configure(state="normal" if counts["found"] else "disabled")
        self.summary.configure(text=f"{len(self.items)} file(s) · " + " · ".join(
            f"{self.STATUS_TEXT.get(k, k)}: {v}" for k, v in sorted(counts.items())) if self.items else
            "No audio files found in that folder.")


class ReviewTab(ttk.Frame):
    """
    Works through mb_unmatched.json: folders the pipeline sent to Incomplete (or sorted on tags alone)
    because MusicBrainz didn't confirm them. Pick the right release once and:
      - the choice is remembered (mb_manual_matches.json) so the pipeline matches that album instantly,
      - optionally the release ID is written into the files, so beets and Picard match it too,
      - if the folder turns out complete, it's moved to the Complete folder under the release's name.
    """

    def __init__(self, master, bus, settings: Settings):
        super().__init__(master, padding=10)
        self.bus, self.settings, self.history = bus, settings, settings.history
        self.cancel, self.busy, self.loaded = threading.Event(), False, False
        self.entries: dict[str, dict] = {}
        self.write_tags = settings.bind("review_write_tags", tk.BooleanVar(value=True))
        self.include_incomplete = settings.bind("review_scan_incomplete", tk.BooleanVar(value=True))
        self.q_artist, self.q_album, self.q_id = tk.StringVar(), tk.StringVar(), tk.StringVar()

        # Row 1: scan actions on the left, the one primary action on the right.
        b = ttk.Frame(self)
        b.pack(fill="x")
        ttk.Button(b, text="↻ Reload", command=self.reload).pack(side="left")
        self.recheck_btn = ttk.Button(b, text="🔍 Re-check all", command=self.recheck)
        self.recheck_btn.pack(side="left", padx=(6, 0))
        # Stop is shown only while a scan runs (see _set_scanning).
        self.stop_btn = ttk.Button(b, text="■ Stop", command=self.cancel.set)
        self.move_btn = ttk.Button(b, text="✔ Move confirmed to Complete", command=self.move_confirmed)
        self.move_btn.pack(side="right")

        # Row 2: options that change what the scan and the move do.
        o = ttk.Frame(self)
        o.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(o, text="Also scan every folder in Incomplete", variable=self.include_incomplete).pack(side="left")
        ttk.Checkbutton(o, text="Write release ID into files", variable=self.write_tags).pack(side="left", padx=(16, 0))

        self.summary = ttk.Label(self, text="")
        self.summary.pack(fill="x", pady=(6, 2))
        self.progress = ttk.Progressbar(self, maximum=1)
        self.progress.pack(fill="x")

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, pady=(6, 0))

        left = ttk.Frame(pane)
        cols = (("status", 90), ("artist", 150), ("album", 180), ("tracks", 60), ("reason", 220))
        self.tree = ttk.Treeview(left, columns=[c for c, _ in cols], show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Folder")
        self.tree.column("#0", width=200)
        for c, w in cols:
            self.tree.heading(c, text=c.title())
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("ok", foreground=T["success"])
        self.tree.tag_configure("gone", foreground=T["muted"])
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._select())
        self.tree.bind("<Double-1>", lambda e: self._open_folder())
        pane.add(left, weight=3)

        right = ttk.Frame(pane, padding=(8, 0, 0, 0))
        self.detail = ttk.Label(right, text="Select a folder.", justify="left", wraplength=520)
        self.detail.pack(fill="x")
        s = ttk.Frame(right)
        s.pack(fill="x", pady=6)
        s.columnconfigure(1, weight=1)
        ttk.Label(s, text="Artist:").grid(row=0, column=0, sticky="w")
        ttk.Entry(s, textvariable=self.q_artist).grid(row=0, column=1, sticky="ew")
        ttk.Label(s, text="Album:").grid(row=1, column=0, sticky="w")
        ttk.Entry(s, textvariable=self.q_album).grid(row=1, column=1, sticky="ew")
        ttk.Button(s, text="Search", command=self.search).grid(row=0, column=2, rowspan=2, sticky="ns", padx=(6, 0))
        ttk.Label(s, text="Release ID or URL:").grid(row=2, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(s, textvariable=self.q_id).grid(row=2, column=1, sticky="ew", pady=(4, 0))
        ttk.Button(s, text="Look up", command=self.lookup_id).grid(row=2, column=2, sticky="ew", padx=(6, 0), pady=(4, 0))

        ccols = (("fit", 80), ("score", 45), ("artist", 120), ("title", 170), ("date", 75), ("country", 50),
                 ("format", 110), ("tracks", 70))
        self.ctree = ttk.Treeview(right, columns=[c for c, _ in ccols], show="headings", selectmode="browse", height=12)
        for c, w in ccols:
            self.ctree.heading(c, text=c.title())
            self.ctree.column(c, width=w, anchor="w")
        self.ctree.tag_configure("complete", foreground=T["success"])
        self.ctree.tag_configure("incomplete", foreground=T["warning"])
        self.ctree.tag_configure("nofit", foreground=T["muted"])
        self.ctree.pack(fill="both", expand=True)
        self.ctree.bind("<Double-1>", lambda e: self.accept())
        bb = ttk.Frame(right)
        bb.pack(fill="x", pady=(6, 0))
        ttk.Button(bb, text="✔ Accept selected release", command=self.accept).pack(side="left")
        ttk.Button(bb, text="Open in MusicBrainz", command=self._open_mb).pack(side="left", padx=4)
        ttk.Button(bb, text="Open folder", command=self._open_folder).pack(side="left")
        ttk.Button(bb, text="Dismiss", command=self.dismiss).pack(side="right")
        ttk.Label(right, text="Fit compares the release's discs and track counts with the folder's tags. "
                              "Green = folder is complete for that release.",
                  foreground=T["muted"], wraplength=520).pack(fill="x", pady=(6, 0))
        pane.add(right, weight=4)
        self._cands: dict[str, dict] = {}

        bus.on("review_progress", lambda n, t, l: (self.progress.configure(maximum=max(t, 1), value=n),
                                                   self.summary.configure(text=l)))
        bus.on("review_done", self._work_done)
        bus.on("review_candidates", self._show_candidates)
        bus.on("review_accepted", self._accepted)
        self.reload()

    # ---- background helper: one MusicBrainz job at a time ----
    def _set_scanning(self, running):
        """Show Stop beside Re-check only while a scan runs."""
        if running:
            self.stop_btn.configure(state="normal")
            self.stop_btn.pack(side="left", padx=(6, 0), after=self.recheck_btn)
        else:
            self.stop_btn.configure(state="disabled")
            self.stop_btn.pack_forget()

    def _run(self, fn, *args):
        if self.busy:
            messagebox.showinfo("Review", "Still working on the previous request.")
            return
        self.busy = True
        self.cancel.clear()
        self.recheck_btn.configure(state="disabled")
        self._set_scanning(True)
        contact = self.settings.vars.get("mb_contact")
        contact = contact.get().strip() if contact else mp.MB_CONTACT
        apply_paths(self.settings)
        self.history.snapshot()

        def work():
            err = None
            try:
                mp.musicbrainzngs.set_useragent(mp.MB_APP_NAME, mp.MB_APP_VERSION, contact or mp.MB_CONTACT)
                if not self.loaded:
                    mp.load_cache()
                    self.loaded = True
                fn(*args)
            except Exception as e:
                import traceback
                traceback.print_exc()
                err = repr(e)
            finally:
                mp.save_cache()
                self.bus.post("review_done", err)

        threading.Thread(target=work, daemon=True).start()

    def _work_done(self, err):
        self.busy = False
        self.recheck_btn.configure(state="normal")
        self._set_scanning(False)
        self.progress.configure(value=0)
        self._render()
        if err:
            messagebox.showerror("Review", err)

    # ---- queue ----
    def reload(self):
        self.entries = mp.load_unmatched()
        self._render()

    def _save(self):
        mp.save_unmatched(self.entries)

    def _render(self):
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for key, e in sorted(self.entries.items(), key=lambda kv: (kv[1].get('artist') or "").lower()):
            gone = not Path(e['folder']).exists()
            status = "folder gone" if gone else ("✔ matched" if e.get('match') else "needs review")
            n = sum(len(t) for t in (e.get('discs') or {}).values())
            self.tree.insert("", "end", iid=key, text=Path(e['folder']).name,
                             tags=("gone",) if gone else (("ok",) if e.get('match') else ()),
                             values=(status, e.get('artist') or "", e.get('album') or "", n, e.get('reason', "")))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])
        ok = sum(1 for e in self.entries.values() if e.get('match'))
        self.summary.configure(text=f"{len(self.entries)} folder(s) in the review queue · {ok} matched and ready to move")

    def _entry(self):
        sel = self.tree.selection()
        return (sel[0], self.entries.get(sel[0])) if sel else (None, None)

    def _select(self):
        key, e = self._entry()
        if not e:
            return
        discs = e.get('discs') or {}
        lines = [e['folder'], "Tracks on disk: " + "  ".join(
            f"disc {d}: {', '.join(map(str, t))}" for d, t in sorted(discs.items(), key=lambda x: int(x[0])))]
        if e.get('match'):
            m = e['match']
            lines.append(f"Matched: {m['artist']} - {m['title']} ({m.get('date', '')}, {m.get('country', '')})")
        self.detail.configure(text="\n".join(lines))
        self.q_artist.set(e.get('artist') or "")
        self.q_album.set(mp.clean_name(e.get('album') or ""))
        self.q_id.set("")
        self._show_candidates(key, e.get('candidates') or [])

    def _show_candidates(self, key, cands):
        cur, _ = self._entry()
        if key != cur:
            return
        e = self.entries.get(key) or {}
        if cands is not (e.get('candidates')):
            e['candidates'] = cands
        discs = _discs_from_entry(e)
        self.ctree.delete(*self.ctree.get_children())
        self._cands = {}
        order = {"complete": 0, "incomplete": 1, None: 2}
        for c in sorted(cands, key=lambda c: (order[mp.assess(c.get('disc_map'), discs)], -c.get('score', 0))):
            fit = mp.assess(c.get('disc_map'), discs)
            label = {"complete": "✔ complete", "incomplete": "missing tracks", None: "doesn't fit"}[fit]
            if not c.get('disc_map') or not all(_discs_counts(c)):
                label = "? (look up)" if fit is None else label
            iid = self.ctree.insert("", "end", values=(label, c.get('score', ''), c.get('artist', ''), c.get('title', ''),
                                                       c.get('date', ''), c.get('country', ''), c.get('format', ''),
                                                       _tracks_label(c.get('disc_map'))),
                                    tags=(fit or "nofit",))
            self._cands[iid] = c
        kids = self.ctree.get_children()
        if kids:
            self.ctree.selection_set(kids[0])

    # ---- searching ----
    def search(self):
        key, e = self._entry()
        if not e:
            return
        artist, album = self.q_artist.get().strip(), self.q_album.get().strip()
        if not artist or not album:
            return

        def job():
            self.bus.post("review_progress", 0, 1, f"Searching MusicBrainz for {artist} - {album}…")
            cands = mp.search_candidates(artist, album)
            for c in cands[:10]:          # fill in missing track counts so Fit is accurate
                if self.cancel.is_set():
                    break
                if not c.get('disc_map') or not all(_discs_counts(c)):
                    rel = mp.release_by_id(c['id'])
                    if rel:
                        c['disc_map'] = rel['disc_map']
            self.bus.post("review_candidates", key, cands)

        self._run(job)

    def lookup_id(self):
        key, e = self._entry()
        m = mp.MBID_RE.search(self.q_id.get())
        if not e or not m:
            messagebox.showinfo("Review", "Paste a MusicBrainz release ID or a link like\n"
                                          "https://musicbrainz.org/release/…")
            return
        mbid = m.group(0).lower()

        def job():
            rel = mp.release_by_id(mbid)
            if rel:
                others = [c for c in (e.get('candidates') or []) if c['id'] != mbid]
                self.bus.post("review_candidates", key, [rel] + others)
            else:
                raise ValueError(f"No release with ID {mbid} (a release-group or recording ID won't work)")

        self._run(job)

    # ---- accepting ----
    def accept(self):
        key, e = self._entry()
        sel = self.ctree.selection()
        if not e or not sel:
            return
        cand = self._cands[sel[0]]
        folder = Path(e['folder'])
        if not folder.is_dir():
            messagebox.showerror("Review", "That folder no longer exists. Dismiss it, or Reload.")
            return
        write = self.write_tags.get()

        def job():
            res = core.review_accept(e, cand['id'], write, self.history)
            self.bus.post("review_accepted", key, res["release"], res["fit"],
                          res["missing"], res["tagged"], res["errors"])

        self._run(job)

    def _accepted(self, key, rel, fit, missing, tagged, errors):
        e = self.entries.get(key)
        if not e:
            return
        note = f"Saved: {rel['artist']} - {rel['title']}." + (f" Wrote the ID into {tagged} file(s)." if tagged else "")
        if errors:
            note += f"\n{len(errors)} file(s) couldn't be tagged:\n" + "\n".join(errors[:8])
        if fit == "complete":
            e['match'], e['reason'] = rel, "matched by you; complete"
            self._save()
            self._render()
            if messagebox.askyesno("Review", note + "\n\nThe folder is complete for this release. "
                                                    "Move it to the Complete folder now?"):
                self._run(self._move_entries, [key])
        else:
            e.pop('match', None)
            e['reason'] = (f"matched by you; missing {missing}" if fit == "incomplete"
                           else "matched by you, but the release's discs/track counts don't fit the folder")
            self._save()
            self._render()
            messagebox.showinfo("Review", note + "\n\n" + (
                "The folder is missing tracks for this release:\n" +
                "\n".join(f"  disc {d}: {', '.join(map(str, t))}" for d, t in missing.items())
                if fit == "incomplete" else
                "This release's discs or track numbers don't line up with the folder's tags. "
                "If the release is right, the track or disc tags in the files are probably off."))

    # ---- re-check and move ----
    def recheck(self):
        apply_paths(self.settings)
        if self.include_incomplete.get() and mp.INCOMPLETE_DIR.is_dir():
            known = {Path(e['folder']).resolve() for e in self.entries.values()}
            for d in mp.INCOMPLETE_DIR.iterdir():
                if d.is_dir() and d.resolve() not in known and any(
                        p.is_file() and p.suffix.lower() in mp.SUPPORTED_EXTENSIONS for p in d.rglob("*")):
                    self.entries[str(d)] = {'folder': str(d), 'artist': None, 'album': None, 'discs': {},
                                            'reason': "not checked yet", 'candidates': [],
                                            'added': time.strftime('%Y-%m-%d %H:%M')}
        keys = [k for k, e in self.entries.items() if Path(e['folder']).is_dir() and not e.get('match')]
        if not keys:
            messagebox.showinfo("Review", "Nothing to re-check.")
            return
        self._run(self._recheck_job, keys)

    def _recheck_job(self, keys):
        core.review_recheck(self.entries, False, self.cancel,
                            lambda n, t, l: self.bus.post("review_progress", n, t, l))

    def move_confirmed(self):
        keys = [k for k, e in self.entries.items() if e.get('match') and Path(e['folder']).is_dir()]
        if not keys:
            messagebox.showinfo("Review", "No matched folders to move. Run a re-check or accept releases first.")
            return
        if messagebox.askyesno("Review", f"Move {len(keys)} matched folder(s) to {mp.COMPLETE_DIR}?\n"
                                         "Each is renamed 'Artist - Album' after the matched release."
                               + ("\nThe release ID is written into each file first." if self.write_tags.get() else "")):
            self._run(self._move_entries, keys)

    def _move_entries(self, keys):
        write = self.write_tags.get()
        core.review_move(self.entries, keys, write, self.history, self.cancel,
                         lambda n, t, l: self.bus.post("review_progress", n, t, l))

    def dismiss(self):
        key, e = self._entry()
        if e:
            del self.entries[key]
            self._save()
            self._render()

    def _open_folder(self):
        _, e = self._entry()
        if e and Path(e['folder']).exists():
            open_in_explorer(e['folder'])

    def _open_mb(self):
        sel = self.ctree.selection()
        if sel:
            import webbrowser
            webbrowser.open(f"https://musicbrainz.org/release/{self._cands[sel[0]]['id']}")




# =============================================================================
# TAB 4: PLEX DUPLICATES  (replaces plex_dedup.py)
# =============================================================================



class PlexTab(ttk.Frame):
    def __init__(self, master, bus, settings: Settings):
        super().__init__(master, padding=10)
        self.bus, self.settings, self.plex, self.sections = bus, settings, None, []
        self.history, self._map = settings.history, ("", "")
        self.cancel = threading.Event()
        S = settings.bind
        self.url = S("plex_url", tk.StringVar(value=os.environ.get("PLEX_BASEURL", "http://localhost:32400")))
        self.token = S("plex_token", tk.StringVar(value=os.environ.get("PLEX_TOKEN", "")))
        self.remember = S("plex_remember_token", tk.BooleanVar(value=False))
        self.tags = S("plex_tags", tk.StringVar(value=DEFAULT_TAGS))
        self.min_diff = S("plex_min_diff", tk.DoubleVar(value=0.0))
        self.r_res = S("plex_rule_res", tk.BooleanVar(value=True))
        self.r_fmt = S("plex_rule_fmt", tk.BooleanVar(value=True))
        self.r_suf = S("plex_rule_suffix", tk.BooleanVar(value=True))
        self.r_tag = S("plex_rule_tags", tk.BooleanVar(value=True))
        self.orphans = S("plex_orphans", tk.BooleanVar(value=False))
        self.rm_folder = S("plex_rm_folder", tk.BooleanVar(value=True))
        self.refresh = S("plex_refresh", tk.BooleanVar(value=True))
        self.map_from = S("plex_map_from", tk.StringVar())
        self.map_to = S("plex_map_to", tk.StringVar())
        self.dry_run = tk.BooleanVar(value=True)

        import importlib.util
        self.available = importlib.util.find_spec("plexapi") is not None
        if not self.available:
            ttk.Label(self, text="The Plex tab needs plexapi:  pip install plexapi", foreground=T["error"]).pack(anchor="w")

        c = ttk.LabelFrame(self, text="Server", padding=8)
        c.pack(fill="x")
        c.columnconfigure(1, weight=1)
        ttk.Label(c, text="URL:").grid(row=0, column=0, sticky="w")
        ttk.Entry(c, textvariable=self.url).grid(row=0, column=1, sticky="ew")
        ttk.Label(c, text="Token:").grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Entry(c, textvariable=self.token, show="•", width=28).grid(row=0, column=3)
        ttk.Checkbutton(c, text="Remember token", variable=self.remember).grid(row=0, column=4, padx=6)
        self.connect_btn = ttk.Button(c, text="Connect", command=self.connect,
                                      state="normal" if self.available else "disabled")
        self.connect_btn.grid(row=0, column=5)
        ttk.Label(c, text="Libraries:").grid(row=1, column=0, sticky="nw", pady=(6, 0))
        self.libs = tk.Listbox(c, selectmode="multiple", height=3, exportselection=False)
        self.libs.grid(row=1, column=1, columnspan=5, sticky="ew", pady=(6, 0))
        ttk.Label(c, text="Path map — Plex path:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(c, textvariable=self.map_from).grid(row=2, column=1, sticky="ew", pady=(6, 0))
        ttk.Label(c, text="= local path:").grid(row=2, column=2, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Entry(c, textvariable=self.map_to, width=28).grid(row=2, column=3, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(c, text="(only if this PC sees the files under a different path, e.g. /data/movies = D:\\Movies)",
                  foreground=T["muted"]).grid(row=3, column=1, columnspan=5, sticky="w")

        r = ttk.LabelFrame(self, text="Auto-pick rules (applied in this order; each only acts if a keeper remains)", padding=8)
        r.pack(fill="x", pady=8)
        r.columnconfigure(5, weight=1)
        for i, (txt, var) in enumerate([("1. Higher resolution", self.r_res), ("2. Container mkv > mp4 > avi", self.r_fmt),
                                        ("3. Drop .1/.2 copies", self.r_suf), ("4. Drop tagged names:", self.r_tag)]):
            ttk.Checkbutton(r, text=txt, variable=var).grid(row=0, column=i, sticky="w", padx=(0, 10))
        ttk.Entry(r, textvariable=self.tags).grid(row=0, column=4, columnspan=2, sticky="ew")
        ttk.Label(r, text="Skip groups within (MB):").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Spinbox(r, from_=0, to=10000, textvariable=self.min_diff, width=7).grid(row=1, column=0, sticky="e", pady=(6, 0))
        ttk.Checkbutton(r, text="Find orphans on disk", variable=self.orphans).grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Checkbutton(r, text="Remove folder left with no video", variable=self.rm_folder).grid(row=1, column=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(r, text="Refresh library after", variable=self.refresh).grid(row=1, column=3, sticky="w", pady=(6, 0))
        ttk.Checkbutton(r, text="Dry run", variable=self.dry_run).grid(row=1, column=4, sticky="w", pady=(6, 0))

        b = ttk.Frame(self)
        b.pack(fill="x")
        self.scan_btn = ttk.Button(b, text="🔍 Scan", command=self.scan, state="disabled")
        self.scan_btn.pack(side="left")
        self.stop_btn = ttk.Button(b, text="■ Stop", command=self.cancel.set, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(b, text="Untick all", command=lambda: self.tt.set_all(lambda i: False)).pack(side="left")
        ttk.Button(b, text="Re-apply rules", command=lambda: self.tt.set_all(lambda i: i["auto"])).pack(side="left", padx=4)
        ttk.Button(b, text="Export plan…", command=self.export).pack(side="left")
        self.apply_btn = ttk.Button(b, text="🗑 Apply ticked deletes", command=self.apply)
        self.apply_btn.pack(side="right")

        self.summary = ttk.Label(self, text="Connect, pick libraries (none = all), then Scan. Nothing is deleted until you Apply.")
        self.summary.pack(fill="x", pady=(6, 2))
        self.progress = ttk.Progressbar(self, maximum=1)
        self.progress.pack(fill="x")
        pane = ttk.PanedWindow(self, orient="vertical")
        pane.pack(fill="both", expand=True, pady=(6, 0))
        self.tt = TickTree(pane, [("res", 60), ("codec", 90), ("bitrate", 80), ("size", 80), ("length", 70),
                                  ("reason", 260)], self._update_summary)
        self.log = make_log(pane, 6)
        pane.add(self.tt, weight=4)
        pane.add(self.log, weight=1)

        bus.on("plex_log", lambda s: log_append(self.log, s))
        bus.on("plex_connected", self._connected)
        bus.on("plex_progress", self._progress)
        bus.on("plex_scanned", self._scanned)
        bus.on("plex_applied", self._applied)

    # ---- helpers ----
    def snapshot_map(self):
        """Tk variables must only be read on the main thread; workers use this copy."""
        self._map = (self.map_from.get().strip(), self.map_to.get().strip())

    def to_local(self, plex_path: str) -> Path:
        f, t = self._map
        if f and plex_path.startswith(f):
            rest = plex_path[len(f):].lstrip("/\\").replace("/", os.sep)
            return Path(t) / rest
        return Path(plex_path)

    def to_plex(self, local: Path) -> str:
        f, t = self._map
        s = str(local)
        if f and t and s.lower().startswith(str(Path(t)).lower()):
            rest = s[len(str(Path(t))):].lstrip("/\\").replace("\\", "/")
            return f.rstrip("/") + "/" + rest
        return s

    def log_(self, s):
        self.bus.post("plex_log", s + "\n")

    def _progress(self, n, t, label):
        self.progress.configure(maximum=max(t, 1), value=n)
        self.summary.configure(text=label)

    # ---- connect ----
    def connect(self):
        url, token = self.url.get().strip(), self.token.get().strip()
        if not url or not token:
            messagebox.showerror("Plex", "Enter the server URL and token.")
            return
        self.connect_btn.configure(state="disabled")

        def work():
            try:
                from plexapi.server import PlexServer
                plex = PlexServer(url, token, timeout=30)
                secs = [s for s in plex.library.sections() if s.TYPE in ("movie", "show")]
                self.bus.post("plex_connected", plex, secs, None)
            except Exception as e:
                self.bus.post("plex_connected", None, [], repr(e))

        threading.Thread(target=work, daemon=True).start()

    def _connected(self, plex, secs, err):
        self.connect_btn.configure(state="normal")
        if err:
            messagebox.showerror("Plex", f"Could not connect:\n{err}")
            return
        self.plex, self.sections = plex, secs
        self.libs.delete(0, "end")
        for s in secs:
            self.libs.insert("end", f"{s.title}  ({s.TYPE})")
        self.scan_btn.configure(state="normal")
        self.summary.configure(text=f"Connected to {plex.friendlyName}. {len(secs)} movie/TV librar(ies).")

    # ---- scan ----
    def scan(self):
        sel = [self.sections[i] for i in self.libs.curselection()] or list(self.sections)
        opts = {"resolution": self.r_res.get(), "format": self.r_fmt.get(), "suffix": self.r_suf.get(),
                "tags": [t.strip() for t in self.tags.get().split(",") if t.strip()] if self.r_tag.get() else []}
        min_diff, want_orphans = float(self.min_diff.get() or 0), self.orphans.get()
        self.snapshot_map()
        self.cancel.clear()
        self.scan_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

        def work():
            try:
                groups, orphans = core.plex_scan(sel, opts, min_diff, want_orphans,
                                                 lambda n, t, l: self.bus.post("plex_progress", n, t, l),
                                                 self.cancel, self.to_local, self.to_plex, self.log_)
                self.bus.post("plex_scanned", groups, orphans,
                              "stopped" if self.cancel.is_set() else None)
            except Exception as e:
                self.bus.post("plex_scanned", [], [], repr(e))

        threading.Thread(target=work, daemon=True).start()

    def _scanned(self, groups, orphans, err):
        self.scan_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.progress.configure(value=0)
        self.tt.clear()
        for g in groups:
            auto = bool(g["delete"])
            gid = self.tt.add_group(g["title"], ("", "", "", "", "", g["note"] or "auto-resolved"),
                                    tags=("review",) if g["note"] else ("keep",), open_=bool(g["note"]))
            for i, p in enumerate(g["parts"]):
                dur = (p["duration_ms"] or 0) / 1000 or None
                reason = g["reasons"].get(i, "keep" if auto else "")
                if p["parts_in_media"] > 1:
                    reason += " · multi-part media"
                if not p["size"]:
                    reason += " · size unknown"
                info = {"kind": "plex", "group": g, "part": p, "auto": i in g["delete"], "size": p["size"], "reason": reason}
                self.tt.add_item(gid, p["file"], (p["res"], f"{p['vcodec']}/{p['acodec']}",
                                                  f"{p['bitrate']} kbps" if p["bitrate"] else "?", fmt_size(p["size"]),
                                                  fmt_dur(dur), reason),
                                 info, ticked=i in g["delete"], tags=() if i in g["delete"] else ("keep",))
        if orphans:
            oid = self.tt.add_group(f"Orphans — on disk but not in Plex ({len(orphans)})",
                                    ("", "", "", "", "", "never auto-ticked: extras, samples, or not scanned yet"),
                                    tags=("muted",), open_=False)
            for o in orphans:
                info = {"kind": "orphan", "path": o["path"], "auto": False, "size": o["size"], "reason": "orphan"}
                self.tt.add_item(oid, str(o["path"]), ("", "", "", fmt_size(o["size"]), "", o["section"].title),
                                 info, ticked=False, tags=("muted",))
        self._update_summary()
        if err:
            self.summary.configure(text=("Stopped early — partial results. " if err == "stopped" else f"Scan error: {err}. ")
                                   + self.summary.cget("text"))

    def _update_summary(self):
        ticked = [i for _, i in self.tt.ticked()]
        groups = [g for g in self.tt.tree.get_children()]
        review = sum(1 for g in groups if "review" in self.tt.tree.item(g, "tags"))
        self.summary.configure(text=f"{len(groups)} group(s) · {review} need review · {len(ticked)} file(s) ticked "
                                    f"({fmt_size(sum(i['size'] for i in ticked))})")

    def export(self):
        if not self.tt.rows:
            return
        path = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="plex_dedup_plan.txt")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            for g in self.tt.tree.get_children():
                f.write(f"== {self.tt.tree.item(g, 'text')}  [{self.tt.tree.item(g, 'values')[-1]}]\n")
                for c in self.tt.tree.get_children(g):
                    i, v = self.tt.rows[c], self.tt.tree.item(c, "values")
                    f.write(f"  {'DELETE' if i['ticked'] else 'keep  '}  {self.tt.tree.item(c, 'text')}  ({v[-1]})\n")
                f.write("\n")
        messagebox.showinfo("Plex", f"Saved {path}")

    # ---- apply ----
    def apply(self):
        ticked = self.tt.ticked()
        if not ticked:
            return
        # Never let a whole group go: at least one copy of every title must survive.
        for iid, info in ticked:
            if info["kind"] == "plex" and all(s["ticked"] for s in self.tt.siblings(iid)):
                self.tt.tree.see(iid)
                self.tt.tree.selection_set(iid)
                messagebox.showerror("Plex", f"Every copy of “{info['group']['title']}” is ticked.\n"
                                             "Untick at least one to keep.")
                return
        dry = self.dry_run.get()
        trash = self.settings.use_trash.get() and send2trash is not None
        if not dry and not messagebox.askyesno(
                "Plex", f"{'Move to the Recycle Bin' if trash else 'PERMANENTLY delete'} "
                        f"{len(ticked)} file(s) ({fmt_size(sum(i['size'] for _, i in ticked))})?\n\n"
                        + ("Files go to the Recycle Bin; Plex is refreshed afterwards."
                           if trash else
                           "Plex deletes go through the server (Settings → Library → 'Allow media deletion' must be on); "
                           "otherwise the file is removed directly from this PC.")):
            return
        self.apply_btn.configure(state="disabled")
        self.snapshot_map()
        self.history.snapshot()
        history = self.history
        rm_folder, refresh = self.rm_folder.get(), self.refresh.get()
        roots = [self.to_local(loc).resolve() for s in self.sections for loc in getattr(s, "locations", [])]

        index = {iid: info for iid, info in ticked}
        ids = [iid for iid, _ in ticked]

        def work():
            try:
                done, _touched = core.plex_apply(
                    ids, index, dry, rm_folder, refresh, trash,
                    self.to_local, self.to_plex, roots,
                    lambda n, t, l: self.bus.post("plex_progress", n, t, l),
                    self.cancel, self.log_, history)
                self.bus.post("plex_applied", done, dry)
            except Exception as e:
                self.log_(f"ERROR: {e}")
                self.bus.post("plex_applied", [], dry)

        threading.Thread(target=work, daemon=True).start()

    def _applied(self, done, dry):
        self.apply_btn.configure(state="normal")
        if not dry:
            for iid in done:
                if self.tt.tree.exists(iid):
                    self.tt.remove(iid)
        self._update_summary()
        messagebox.showinfo("Plex", f"{'Dry run: would delete' if dry else 'Deleted'} {len(done)} file(s). See the log below.")


# =============================================================================
# TAB 5: SETTINGS
# =============================================================================

class SettingsTab(ttk.Frame):
    def __init__(self, master, bus, settings: Settings):
        super().__init__(master, padding=10)
        self.settings = settings

        f = ttk.LabelFrame(self, text="Pipeline output folders", padding=8)
        f.pack(fill="x")
        f.columnconfigure(1, weight=1)
        for row, (attr, label, _) in enumerate(PATH_SETTINGS):
            folder_row(f, label + ":", settings.paths[attr], row)
        ttk.Label(f, text="The pipeline refuses to run on a source folder inside any of these.",
                  foreground=T["muted"]).grid(row=len(PATH_SETTINGS), column=1, sticky="w", pady=(4, 0))
        sv = ttk.LabelFrame(self, text="Services", padding=8)
        sv.pack(fill="x", pady=(8, 0))
        sv.columnconfigure(1, weight=1)
        ttk.Label(sv, text="MusicBrainz contact (email or URL):").grid(row=0, column=0, sticky="w", padx=(0, 6))
        contact = settings.vars.get("mb_contact") or settings.bind("mb_contact", tk.StringVar(value=mp.MB_CONTACT))
        ttk.Entry(sv, textvariable=contact).grid(row=0, column=1, sticky="ew")
        ttk.Label(sv, text="Required for MusicBrainz lookups. Its placeholder value is refused.",
                  foreground=T["muted"]).grid(row=1, column=1, sticky="w", pady=(2, 8))
        ttk.Label(sv, text="Discogs token:").grid(row=2, column=0, sticky="w", padx=(0, 6))
        token = settings.bind("discogs_token", tk.StringVar(value=""))
        ttk.Entry(sv, textvariable=token, show="•").grid(row=2, column=1, sticky="ew")
        remember = settings.bind("discogs_remember_token", tk.BooleanVar(value=False))
        ttk.Checkbutton(sv, text="Remember token on disk", variable=remember).grid(row=3, column=1, sticky="w", pady=(4, 0))

        bf = ttk.LabelFrame(self, text="Beets and logs", padding=8)
        bf.pack(fill="x", pady=(8, 0))
        bf.columnconfigure(1, weight=1)
        for row, (attr, label, key, kind) in enumerate(BEETS_PATH_SETTINGS):
            (folder_row if kind == "dir" else file_row)(bf, label + ":", settings.paths[attr], row)
        ttk.Label(bf, text="The pipeline runs beets with config.yaml, filled with these paths and the Discogs token. Your own beets config is not used.",
                  foreground=T["muted"], wraplength=640, justify="left"
                  ).grid(row=len(BEETS_PATH_SETTINGS), column=1, sticky="w", pady=(4, 0))

        ttk.Button(self, text="Reset all paths to defaults", command=self.reset_paths).pack(anchor="w", pady=(8, 0))

        d = ttk.LabelFrame(self, text="Deleting", padding=8)
        d.pack(fill="x", pady=8)
        t1 = ttk.Checkbutton(d, text="Duplicates tab: send deletes to the Recycle Bin", variable=settings.use_trash)
        t1.pack(anchor="w")
        t2 = ttk.Checkbutton(d, text="Pipeline: send its duplicate deletes and converted FLAC originals to the Recycle Bin",
                             variable=settings.pipe_trash)
        t2.pack(anchor="w")
        if send2trash is None:
            for t in (t1, t2):
                t.configure(state="disabled")
            ttk.Label(d, text="Install send2trash to enable:  pip install send2trash",
                      foreground=T["error"]).pack(anchor="w")
        ttk.Label(d, text="Plex deletes always go through the Plex server (or are permanent if it can't), "
                          "so the Recycle Bin doesn't apply there.", foreground=T["muted"]).pack(anchor="w", pady=(4, 0))

        h = ttk.LabelFrame(self, text="History log", padding=8)
        h.pack(fill="x")
        h.columnconfigure(1, weight=1)
        ttk.Checkbutton(h, text="Record every move and delete (one file per day)",
                        variable=settings.history.enabled_var).grid(row=0, column=0, columnspan=3, sticky="w")
        folder_row(h, "History folder:", settings.history.folder_var, 1)
        b = ttk.Frame(h)
        b.grid(row=2, column=1, sticky="w", pady=(6, 0))
        ttk.Button(b, text="Open today's log", command=self.open_today).pack(side="left")
        ttk.Button(b, text="Open history folder", command=self.open_folder).pack(side="left", padx=6)

        ttk.Label(self, text=f"Settings are saved to {SETTINGS_FILE} when you close the window.",
                  foreground=T["muted"]).pack(anchor="w", pady=(10, 0))
        ttk.Button(self, text="Save settings now", command=settings.save).pack(anchor="w", pady=(4, 0))

    def reset_paths(self):
        # Defaults captured from music_pipeline.py before any settings override them
        # (see PIPELINE_DEFAULT_PATHS in toolkit_core.py) -- one source of truth.
        for attr, _, key, _kind in ALL_PATH_SETTINGS:
            self.settings.paths[attr].set(PIPELINE_DEFAULT_PATHS[key])

    def open_today(self):
        self.settings.history.snapshot()
        p = self.settings.history.path()
        if p.exists():
            open_in_explorer(p)
        else:
            messagebox.showinfo("History", "Nothing has been logged today yet.")

    def open_folder(self):
        p = Path(self.settings.history.folder_var.get() or HISTORY_DEFAULT)
        p.mkdir(parents=True, exist_ok=True)
        open_in_explorer(p)


# =============================================================================
# MAIN WINDOW
# =============================================================================

def main():
    _load_private_fonts()
    root = tk.Tk()
    root.title("Music Toolkit")
    root.geometry("1180x840")
    apply_theme(root)

    settings = Settings()
    init_shared(settings)
    bus = UiBus(root)

    bar = ttk.Frame(root, padding=(24, 14, 24, 4))
    bar.pack(fill="x")
    ttk.Label(bar, text="Music Toolkit", style="Head.TLabel").pack(side="left")
    ttk.Label(bar, text="  Convert · dedupe · verify · sort", style="Muted.TLabel").pack(side="left", pady=(6, 0))

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=12, pady=(4, 12))
    pipeline = PipelineTab(nb, bus, settings)
    tabs = [(pipeline, "Pipeline"), (DupesTab(nb, bus, settings), "Duplicates"),
            (TagHealthTab(nb, bus, settings), "Tag health"), (LyricsTab(nb, bus, settings), "Lyrics"),
            (ReviewTab(nb, bus, settings), "Review"),
            (PlexTab(nb, bus, settings), "Plex"),
            (SettingsTab(nb, bus, settings), "Settings")]
    for tab, name in tabs:
        nb.add(tab, text=name)
    style_buttons(root)
    last = settings.bind("last_tab", tk.IntVar(value=0))
    try:
        nb.select(last.get())
    except tk.TclError:
        pass
    nb.bind("<<NotebookTabChanged>>", lambda e: last.set(nb.index(nb.select())))

    def on_close():
        if pipeline.running and not messagebox.askokcancel(
                "Quit", "The pipeline is still running. Quitting may leave a half-converted file. Quit anyway?"):
            return
        settings.save()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
