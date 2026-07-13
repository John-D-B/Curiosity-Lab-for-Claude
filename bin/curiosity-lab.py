#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1 OR LicenseRef-MountainInformatik-Commercial
# SPDX-FileCopyrightText: 2026 Mountain Informatik GmbH — original software by John Buehrer
"""
Curiosity Lab for Claude (née mini-workbench) — a small chat window for
putting curiosity back into AI conversations, on the Anthropic API.

A local Tkinter window that talks to api.anthropic.com and bills your
Console credits, not your Max plan. It exists to make the API's
fundamentals concrete —

  * the API is stateless: the conversation is a list WE keep and resend
    (see self.history)
  * responses stream token-by-token (client.messages.stream)
  * every call returns a usage tally, which is your bill (see the cost bar)

— and to serve as a laboratory for personas, curiosity riders, Me-files,
and the cost of context.

Run (from a shell where the key is set for THIS window only, so it never
shadows your Max login elsewhere):

    $env:ANTHROPIC_API_KEY = "sk-ant-..."   # PowerShell
    python bin/curiosity-lab.py

With no key set, the first send shows an auth error in the transcript —
itself an accurate demonstration of how the SDK resolves credentials.

Claude is a trademark of Anthropic, PBC. This project is not affiliated
with or endorsed by Anthropic.
"""

from __future__ import annotations

__version__ = "2.7.0"

import datetime
import json
import os
import queue
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from tkinter import ttk, scrolledtext, filedialog

try:
    import anthropic
except ImportError:
    anthropic = None


# Price per 1M tokens (USD): (input, output). Standard rates.

PRICING = {
    "claude-fable-5":   (10.0, 50.0),
    "claude-opus-4-8":  (5.0, 25.0),
    "claude-sonnet-5":  (2.0, 10.0),   # intro rate through 2026-08-31 (sticker: 3.0 / 15.0)
    "claude-haiku-4-5": (1.0, 5.0),
}
DEFAULT_MODEL = "claude-haiku-4-5"   # cheapest — the sensible default for practice
MAX_TOKENS = 4096

# Server-side web tools (v2.6.0), declared per-request when the checkboxes
# are on. The dated type tags are Anthropic's frozen contract versions, not
# build stamps; the basic variants below run on every model in PRICING.
# Search carries a per-use surcharge; fetch bills only the tokens the
# fetched page occupies. Update SEARCH_COST with Anthropic's price list —
# the checkbox label and the meter both read it.
SEARCH_COST = 10.0 / 1000            # USD per search
SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search",
               "max_uses": 5}
FETCH_TOOL = {"type": "web_fetch_20250910", "name": "web_fetch",
              "max_uses": 5, "citations": {"enabled": True},
              "max_content_tokens": 25_000}   # guard against runaway PDFs

# Code execution (v2.7.0): an Anthropic-hosted Linux sandbox (Python 3.11 +
# bash, 1 CPU / 5 GiB, NO internet). Free up to the org's monthly container-
# hour allowance, then $0.05/hour — but the API does not report container
# time in `usage`, so the meter can COUNT runs, not price them. GA, no beta
# header; every model in PRICING accepts the tool type.
EXEC_TOOL = {"type": "code_execution_20260521", "name": "code_execution"}

# A server-tool turn can pause (stop_reason "pause_turn") and be resumed by
# resending the paused assistant content. Cap the resume loop so a runaway
# can't spin forever.
MAX_CONTINUATIONS = 5

FONT_FAMILY = "Segoe UI"             # Tk substitutes the system font on macOS
MONO_FAMILY = "Courier New"          # for `code` spans
FONT_SIZES = [9, 10, 11, 12, 14, 16, 18, 20]
DEFAULT_FONT_SIZE = 12

# Markdown subset rendered into the transcript: ***bold italic***, **bold**,
# *italic*, `code` inline; headings, bullets, and rules per line.
INLINE_MD = re.compile(r"\*\*\*(.+?)\*\*\*|\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`")


def demote_headings(md_text, levels=2):
    """Shift Markdown headings down (# -> ###) so a saved reply's own
    structure sorts below the exporter's turn headers."""
    return re.sub(
        r"(?m)^(#{1,4})(\s)",
        lambda m: "#" * min(len(m.group(1)) + levels, 6) + m.group(2),
        md_text)

# Personas and curiosity riders live in editable JSON files at the project
# top level, created from the defaults below on first run. Each file is a
# list of {"tag": ..., "text": ...} entries; the UI selects by tag so the
# texts can be long without cluttering the window. settings.json (optional)
# holds pre-loaded preferences: model / persona / curiosity / size / prompt
# / mefile.
#
# Curiosity (see JohnB.1-feedback: curiosity is a practice, not a
# prescription): the selected rider is appended to the outgoing user message
# — visibly, in the transcript, so demos show exactly what was injected. It
# operationalizes Le Cunff's three design asks: visible sources, competing
# explanations, and onward exploration.
# Note: the rider is stored in history (what you see is what was sent is what's
# billed), so it is re-sent with every later turn — recurring cost each turn.

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# The script lives in bin/; the JSON configs sit at the project top level.
CONFIG_DIR = (os.path.dirname(APP_DIR)
              if os.path.basename(APP_DIR) == "bin" else APP_DIR)
PERSONAS_FILE = "personas.json"
CURIOSITIES_FILE = "curiosities.json"
SETTINGS_FILE = "settings.json"
DEMOS_FILE = "demos.json"
APIKEY_FILE = "apikey.txt"   # auto-loaded at startup when present
NO_CURIOSITY = "(none)"

DEFAULT_PERSONAS = [
    {"tag": "Concise",
     "text": "You are a concise, helpful assistant."},
    {"tag": "Teacher",
     "text": "You are a patient teacher. Explain ideas step by step, with "
             "one concrete example each, and check understanding as you go."},
    {"tag": "Skeptic",
     "text": "You are a friendly skeptic. Before agreeing with a premise, "
             "test it; point out weak evidence and hidden assumptions."},
    {"tag": "Pirate",
     "text": "You are a pirate. Answer everything in pirate speak."},
    {"tag": "Me, Myself",
     "text": "You are the user themself, as described in the loaded "
             "Me-file. Adopt their voice, interests, and mannerisms. If no "
             "Me-file has been loaded, admit you have no idea who you are "
             "supposed to be, and ask for one (the Me button)."},
]

DEFAULT_CURIOSITIES = [
    {"tag": "full rider",
     "text": "[Curiosity rider] Along with your answer, briefly: "
             "(1) name your sources or how I could verify this; "
             "(2) give one competing explanation or counterpoint you find "
             "credible; (3) suggest two adjacent questions I didn't think "
             "to ask."},
    {"tag": "sources",
     "text": "[Curiosity rider] Along with your answer, briefly name your "
             "sources or how I could verify this."},
    {"tag": "counterpoint",
     "text": "[Curiosity rider] Along with your answer, give one competing "
             "explanation or counterpoint you find credible."},
    {"tag": "questions",
     "text": "[Curiosity rider] Along with your answer, suggest two adjacent "
             "questions I didn't think to ask."},
]


# Demo bundles: pre-reviewed knob combinations users can pick from the
# Demos button. Each bundle is a "tag" plus any settings.json keys
# (Geometry excluded). The file is read fresh on every click, so a
# downloaded or hand-edited demos.json works without a restart.
DEFAULT_DEMOS = [
    {"tag": "Pirate wisdom",
     "Model": "claude-haiku-4-5",
     "Persona": "Pirate", "Curiosity": "Questions",
     "Prompt": "Should I get out of bed tomorrow morning?"},
    {"tag": "Skeptic vs. microservices",
     "Persona": "Skeptic", "Curiosity": "Counterpoint",
     "Prompt": "We switched to microservices and productivity doubled. "
                "Should everyone do it?"},
    {"tag": "Teach me OAuth",
     "Persona": "Teacher", "Curiosity": "Sources",
     "Prompt": "Explain how OAuth login works, like I'm smart but new "
                "to it."},
    {"tag": "Who am I? (needs a Me-file)",
     "Persona": "Me, Myself", "Curiosity": "(none)",
     "Prompt": "Is a hotdog a sandwich?"},
]


def load_choices(filename, defaults):
    """Return ({tag: text}, error_or_None) from a JSON file in CONFIG_DIR.
    First run writes the defaults out so there is a file to edit; a
    malformed file falls back to the defaults without being overwritten,
    and the error is returned so the UI can show it in the transcript."""
    path = os.path.join(CONFIG_DIR, filename)
    error = None
    try:
        with open(path, encoding="utf-8") as f:
            return {e["tag"]: e["text"] for e in json.load(f)}, None
    except FileNotFoundError:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(defaults, f, indent=2, ensure_ascii=False)
    except json.JSONDecodeError as exc:
        error = f"{filename}: {exc} — using built-in defaults"
    except (KeyError, TypeError):
        error = (f"{filename}: entries must look like "
                 '{"tag": ..., "text": ...} — using built-in defaults')
    return {e["tag"]: e["text"] for e in defaults}, error


def load_settings():
    """Return (settings, notes) from settings.json in CONFIG_DIR — missing
    file is fine. Accepts a dict or a list of single-key dicts; keys are
    case-insensitive with plural aliases ("Curiosities" counts). Anything
    unparseable becomes a note for the transcript."""
    path = os.path.join(CONFIG_DIR, SETTINGS_FILE)
    notes = []
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}, notes
    except json.JSONDecodeError as exc:
        return {}, [f"{SETTINGS_FILE}: {exc} — settings ignored"]
    if isinstance(raw, list):
        merged = {}
        for item in raw:
            if isinstance(item, dict):
                merged.update(item)
            else:
                notes.append(f"{SETTINGS_FILE}: ignored entry {item!r}")
        raw = merged
    if not isinstance(raw, dict):
        return {}, [f"{SETTINGS_FILE}: expected a dict or list of dicts"]
    return normalize_keys(raw, notes, SETTINGS_FILE), notes


SETTINGS_ALIASES = {"model": "model", "models": "model",
                    "persona": "persona", "personas": "persona",
                    "tone": "persona", "tones": "persona",  # pre-1.9 names
                    "curiosity": "curiosity", "curiosities": "curiosity",
                    "size": "size",
                    "prompt": "prompt", "preload": "prompt",  # pre-2.2 name
                    "mefile": "mefile", "me-file": "mefile", "me": "mefile",
                    "geometry": "geometry", "window": "geometry",
                    "position": "geometry",
                    "apikey": "apikey", "api-key": "apikey",
                    "keyfile": "apikey",
                    "search": "search", "websearch": "search",
                    "web-search": "search",
                    "fetch": "fetch", "webfetch": "fetch",
                    "web-fetch": "fetch",
                    "sandbox": "sandbox", "python": "sandbox",
                    "exec": "sandbox", "code": "sandbox"}


def normalize_keys(raw, notes, source):
    """Map a raw {key: value} dict onto canonical settings keys,
    case-insensitively and through the aliases above. Unknown keys
    become a note naming the source file."""
    settings = {}
    for key, value in raw.items():
        norm = SETTINGS_ALIASES.get(str(key).strip().lower())
        if norm is None:
            notes.append(f"{source}: unknown key {key!r} ignored")
        else:
            settings[norm] = value
    return settings


def load_bundles(filename, defaults):
    """Return (bundles, error) from a demos file in CONFIG_DIR: a list of
    bundles, each a dict with a "tag" plus settings-style keys. First run
    writes the defaults out so there is a file to edit; a malformed file
    falls back to the defaults without being overwritten."""
    path = os.path.join(CONFIG_DIR, filename)
    error = None
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if (isinstance(raw, list) and raw
                and all(isinstance(b, dict) and b.get("tag") for b in raw)):
            return raw, None
        error = (f"{filename}: expected a list of bundles, each with a "
                 f"\"tag\" — using built-in demos")
    except FileNotFoundError:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(defaults, f, indent=2, ensure_ascii=False)
    except json.JSONDecodeError as exc:
        error = f"{filename}: {exc} — using built-in demos"
    return defaults, error


def match_tag(value, tags):
    """Case-insensitive tag lookup; returns the canonical tag or None."""
    lookup = {t.lower(): t for t in tags}
    return lookup.get(str(value).strip().lower())


def parse_bool(value):
    """A forgiving boolean for settings values: JSON true/false plus the
    usual spellings. Returns None when unparseable, so the caller can
    report it instead of guessing."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "on", "yes", "1"):
        return True
    if text in ("false", "off", "no", "0"):
        return False
    return None


class RoundButton(tk.Canvas):
    """A rounded, colored button drawn on a Canvas. Native Tk buttons
    can't be tinted or resized on macOS (aqua ignores background and
    vertical padding), so Send draws its own chrome — which also means
    it looks exactly the same on Windows."""

    def __init__(self, parent, text, command, font,
                 fill="#8fd18f", active_fill="#6fbf6f",
                 radius=10, pad_x=22, pad_y=6, width=None, height=None,
                 v_inset=0):
        f = tkfont.Font(font=font)
        self.font = f
        w = width or f.measure(text) + 2 * pad_x
        h = height or f.metrics("linespace") + 2 * pad_y
        super().__init__(parent, width=w, height=h, highlightthickness=0,
                         background=parent.winfo_toplevel().cget("background"))
        self._command = command
        self._fill, self._active = fill, active_fill
        r = radius
        # Native buttons draw their pill inset inside the widget box;
        # v_inset mimics that so a RoundButton lines up visually with
        # ttk neighbours of the same requested size.
        y0, y1 = v_inset, h - v_inset
        pts = [r, y0, w - r, y0, w, y0, w, y0 + r, w, y1 - r, w, y1,
               w - r, y1, r, y1, 0, y1, 0, y1 - r, 0, y0 + r, 0, y0]
        self.shape = self.create_polygon(pts, smooth=True, fill=fill,
                                         outline="")
        self.label = self.create_text(w // 2, h // 2, text=text, font=f,
                                      fill="#173517")
        self.bind("<ButtonPress-1>",
                  lambda e: self.itemconfig(self.shape, fill=self._active))
        self.bind("<ButtonRelease-1>", self._release)

    def _release(self, event):
        self.itemconfig(self.shape, fill=self._fill)
        if (0 <= event.x <= self.winfo_width()
                and 0 <= event.y <= self.winfo_height()):
            self._command()

    def set_mode(self, text, fill, active_fill, command):
        """Re-skin the button in place — Send ⇄ Stop. Width is fixed at
        construction; the two labels are the same length, so no resize."""
        self._command = command
        self._fill, self._active = fill, active_fill
        self.itemconfig(self.shape, fill=fill)
        self.itemconfig(self.label, text=text)


class Workbench(tk.Tk):
    def __init__(self, verbose=False):
        super().__init__()
        self.verbose = verbose          # -v | --verbose: keep stderr visible
        self.title(f"Curiosity Lab for Claude - v{__version__} - using API tokens")
        self.protocol("WM_DELETE_WINDOW", self._quit)   # red dot saves too

        self.client = anthropic.Anthropic() if anthropic else None
        self.history: list[dict] = []   # the conversation — WE own it, WE resend it
        self.log: list[dict] = []       # session journal for Save (survives New)
        self.me_text = ""               # Me-file contents (rides in the system prompt)
        self.me_name = ""
        self._last_prompt = ""          # restored to the entry after each reply
        self._last_sent_persona = None  # detects mid-chat persona switches
        self.spend = 0.0                # running USD estimate this session
        self.q: queue.Queue = queue.Queue()
        self.streaming = False
        self._spinning = False             # drives the live activity spinner
        self._turn_start = 0.0
        self._cancel = False               # Stop button → abort the turn
        self._active_stream = None         # live stream, for force-close on Stop
        self._gen = 0                      # turn generation; drops abandoned output
        self._sent_model = DEFAULT_MODEL   # model of the in-flight turn
        self._web_used_last_turn = False   # drives the evidence-drop note
        self._link_seq = 0                 # unique Tk tag per clickable URL

        self._build_ui()
        # The app picks its own size: wide enough that the top bar always
        # fits, tall enough to read. Only the POSITION is remembered in
        # settings.json — see _save_geometry.
        self.update_idletasks()
        self.geometry(f"{max(self.winfo_reqwidth(), 900)}x640")
        for err in self._config_errors:
            self._append(f"[{err}]\n", "note")
        default_key = os.path.join(CONFIG_DIR, APIKEY_FILE)
        if os.path.exists(default_key):
            self._set_api_key(default_key)   # settings/env still documented
        self._apply_settings()
        # Launched from a terminal, Tk windows on macOS start BEHIND the
        # launcher. Briefly claim topmost, then let go.
        self.lift()
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False))
        self.focus_force()
        self.after(50, self._pump)

    # ---- UI --------------------------------------------------------------
    @staticmethod
    def _fit_width(tags, minimum=12):
        """Combobox width in Tk character units that just fits the longest
        tag. Measured in pixels: character units assume average-width
        glyphs, which overshoots for mostly-lowercase tags."""
        f = tkfont.nametofont("TkDefaultFont")
        unit = max(1, f.measure("0"))
        return max(minimum, max(map(f.measure, tags)) // unit + 1)

    def _build_ui(self):
        top = ttk.Frame(self, padding=(12, 10, 12, 6))
        top.pack(fill="x")

        ttk.Label(top, text="Model:").pack(side="left")
        self.model = tk.StringVar(value=DEFAULT_MODEL)
        model_box = ttk.Combobox(top, textvariable=self.model,
                                 values=list(PRICING),
                                 state="readonly", width=18)
        model_box.pack(side="left", padx=(4, 12))

        self._config_errors = []
        self.personas, err = load_choices(PERSONAS_FILE, DEFAULT_PERSONAS)
        if err:
            self._config_errors.append(err)
        persona_label = ttk.Label(top, text="Persona:")
        persona_label.pack(side="left")
        self.persona = tk.StringVar(value=next(iter(self.personas)))
        # Width tracks the longest tag, so long persona names aren't clipped.
        self.persona_box = ttk.Combobox(top, textvariable=self.persona,
                                        values=list(self.personas),
                                        height=len(self.personas),
                                        state="readonly",
                                        width=self._fit_width(self.personas))
        self.persona_box.pack(side="left", padx=(4, 12))

        self.curiosities, err = load_choices(CURIOSITIES_FILE,
                                             DEFAULT_CURIOSITIES)
        if err:
            self._config_errors.append(err)
        ttk.Label(top, text="Curiosity:").pack(side="left")
        self.curiosity = tk.StringVar(value=NO_CURIOSITY)
        self.curiosity_box = ttk.Combobox(top, textvariable=self.curiosity,
                                          values=[NO_CURIOSITY] + list(self.curiosities),
                                          height=len(self.curiosities) + 1,
                                          state="readonly",
                                          width=self._fit_width(self.curiosities))
        self.curiosity_box.pack(side="left", padx=4)

        ttk.Label(top, text="Size:").pack(side="left", padx=(12, 0))
        self.font_size = tk.IntVar(value=DEFAULT_FONT_SIZE)
        size_box = ttk.Combobox(top, textvariable=self.font_size,
                                values=[str(s) for s in FONT_SIZES],
                                state="readonly", width=3)
        size_box.pack(side="left", padx=4)
        size_box.bind("<<ComboboxSelected>>", self._apply_font_size)

        # Readonly comboboxes keep their value text highlighted after a
        # pick until focus moves — clear that selection immediately.
        for box in (model_box, self.persona_box, self.curiosity_box,
                    size_box):
            box.bind("<<ComboboxSelected>>",
                     lambda e: e.widget.selection_clear(), add="+")

        ttk.Button(top, text="API", command=self._load_api_key).pack(
            side="left", padx=(12, 0))
        ttk.Button(top, text="Me", command=self._load_me).pack(side="left",
                                                                padx=4)
        self.demos_btn = ttk.Button(top, text="Demos",
                                    command=self._pick_demo)
        self.demos_btn.pack(side="left", padx=4)
        # New is the other big-deal button — same green chrome as Send: it
        # wipes the conversation (and the web checkboxes) for a clean run.
        self.update_idletasks()
        new_font = tkfont.nametofont("TkDefaultFont").copy()
        new_font.configure(weight="bold")
        RoundButton(top, text="New", command=self._reset, font=new_font,
                    width=self.demos_btn.winfo_reqwidth(),
                    height=self.demos_btn.winfo_reqheight(),
                    v_inset=3).pack(side="left", padx=4)

        # The two server-side web tools, one checkbox each — the UI mirrors
        # the API mechanism split (fetch is free beyond tokens; search
        # carries a surcharge). Both default off: the control experiment.
        # The block sits aligned under the Persona: label (measured, not
        # guessed, so it tracks combobox widths), introduced by a vertical
        # separator as a visual guide; the right edge stays free for
        # future buttons. Inside the block the checkboxes stay
        # left-aligned so the boxes line up vertically.
        webrow = ttk.Frame(self, padding=(12, 2, 12, 4))
        webrow.pack(fill="x")
        self.update_idletasks()   # top row must be laid out to measure it
        offset = max(0, persona_label.winfo_x() - 12)
        # A thin outline groups the pair; it replaces the earlier separator.
        checks = ttk.Frame(webrow, borderwidth=1, relief="solid",
                           padding=(8, 3))
        checks.pack(side="left", padx=(offset, 0))
        self.fetch = tk.BooleanVar(value=False)
        self.search = tk.BooleanVar(value=False)
        self.sandbox = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            checks, variable=self.fetch,
            text="Use Internet web-fetch API, for all URLs in this dialog "
                 "(no extra cost)").pack(anchor="w")
        ttk.Checkbutton(
            checks, variable=self.search,
            text="Use Internet web-search API / search engine "
                 f"(extra cost: ${SEARCH_COST:.2f} per search)").pack(anchor="w")
        ttk.Checkbutton(
            checks, variable=self.sandbox,
            text="Use API Linux Sandbox (free tier, then $0.05/hr)"
            ).pack(anchor="w")

        # Transcript and entry share a vertical PanedWindow: the sash
        # between them can be dragged to grow the input area for long,
        # multi-line prompts.
        self.paned = ttk.PanedWindow(self, orient="vertical")

        # All fonts are set centrally by _apply_font_size(); only the
        # colors live here.
        self.view = scrolledtext.ScrolledText(self.paned, wrap="word",
                                              state="disabled",
                                              padx=8, pady=6)
        self.view.tag_config("user", foreground="#1a7a55")
        self.view.tag_config("assistant", foreground="#222222")
        self.view.tag_config("note", foreground="#c03030")
        self.view.tag_config("curious", foreground="#1a55a0")
        self.view.tag_config("meta", foreground="#888888")
        self.view.tag_config("prompt", foreground="#222222")
        for t in ("md_bold", "md_italic", "md_bolditalic",
                  "md_h1", "md_h2", "md_h3"):
            self.view.tag_config(t, foreground="#222222")
        self.view.tag_config("md_code", foreground="#1a1a1a",
                             background="#f2f2f2")
        self.view.tag_config("md_codeblock", foreground="#1a1a1a",
                             background="#f2f2f2",
                             lmargin1=12, lmargin2=12)
        self.view.tag_config("md_rule", foreground="#999999")
        # The transcript is read-only (state="disabled") but must stay
        # selectable and copyable — especially the code blocks, whose
        # background otherwise hides the selection highlight. Raise 'sel'
        # above every tag, and bind copy explicitly so Cmd/Ctrl+C works
        # even in a disabled Text widget.
        self.view.tag_raise("sel")
        for seq in ("<Command-c>", "<Control-c>"):
            self.view.bind(seq, self._copy_selection)
        # Marks where the current reply begins, so the raw streamed text can
        # be re-rendered as Markdown once the reply is complete.
        self.view.mark_set("reply_start", "end-1c")
        self.view.mark_gravity("reply_start", "left")

        bottom = ttk.Frame(self.paned, padding=(0, 4, 0, 10))
        # The button column is packed FIRST (from the right) so it always
        # keeps its size; the entry's nominal width stays small so its
        # font-dependent requested width can't squeeze the buttons out at
        # large font sizes.
        btns = ttk.Frame(bottom)
        btns.pack(side="right", fill="y", padx=6)
        # Send is the vital button: the platform's own button font in
        # bold, on a green rounded RoundButton (see above) sized to match
        # Save/Quit exactly — same footprint, more presence.
        save_btn = ttk.Button(btns, text="Save", command=self._save)
        quit_btn = ttk.Button(btns, text="Quit", command=self._quit)
        self.update_idletasks()
        send_font = tkfont.nametofont("TkDefaultFont").copy()
        send_font.configure(weight="bold")
        self.send_btn = RoundButton(
            btns, text="Send", command=self._send, font=send_font,
            width=save_btn.winfo_reqwidth(),
            height=save_btn.winfo_reqheight(), v_inset=3)
        self.send_btn.pack()
        save_btn.pack(pady=2)
        quit_btn.pack()
        self.entry = tk.Text(bottom, height=3, wrap="word", width=10,
                             padx=8, pady=6)
        self.entry.pack(side="left", fill="both", expand=True)
        self.entry.bind("<Return>", self._on_return)   # Enter sends; Shift+Enter = newline

        self.status = tk.StringVar(value="ready — $0.000000 this session")
        status_bar = ttk.Label(self, textvariable=self.status, anchor="w",
                               relief="sunken", padding=3)

        # Pack order = squeeze priority: the cost meter claims its space
        # FIRST, then the PanedWindow absorbs the rest — transcript above
        # (weight 1: it takes any height surplus), entry row below. The
        # sash between the panes is the drag handle.
        status_bar.pack(fill="x", side="bottom")
        self.paned.pack(fill="both", expand=True, padx=12, pady=(2, 0))
        self.paned.add(self.view, weight=1)
        self.paned.add(bottom, weight=0)

        self._apply_font_size()

    def _apply_font_size(self, _event=None):
        """Set the transcript and input fonts from the Size selector; the
        note/rider tags stay one point smaller, as before."""
        size = int(self.font_size.get())
        self.view.configure(font=(FONT_FAMILY, size))
        self.view.tag_config("user", font=(FONT_FAMILY, size, "bold"))
        self.view.tag_config("note", font=(FONT_FAMILY, size - 1, "italic"))
        self.view.tag_config("curious", font=(FONT_FAMILY, size - 1, "italic"))
        self.view.tag_config("meta", font=(FONT_FAMILY, size - 1, "italic"))
        self.view.tag_config("prompt", font=(FONT_FAMILY, size, "italic"))
        self.view.tag_config("md_bold", font=(FONT_FAMILY, size, "bold"))
        self.view.tag_config("md_italic", font=(FONT_FAMILY, size, "italic"))
        self.view.tag_config("md_bolditalic",
                             font=(FONT_FAMILY, size, "bold italic"))
        self.view.tag_config("md_code", font=(MONO_FAMILY, size))
        self.view.tag_config("md_codeblock", font=(MONO_FAMILY, size - 1))
        self.view.tag_config("md_h1", font=(FONT_FAMILY, size + 4, "bold"))
        self.view.tag_config("md_h2", font=(FONT_FAMILY, size + 2, "bold"))
        self.view.tag_config("md_h3", font=(FONT_FAMILY, size + 1, "bold"))
        # Hanging indent for the annotation lines: wrapped continuations
        # align under the bracketed text instead of snapping to column 0.
        margin = int(size * 1.5)
        for t in ("meta", "curious", "note"):
            self.view.tag_config(t, lmargin2=margin)
        self.entry.configure(font=(FONT_FAMILY, size))

    def _apply_settings(self):
        """Apply settings.json — pre-loaded preferences at startup."""
        settings, notes = load_settings()
        self._apply_prefs(settings, notes)

    def _pick_demo(self):
        """Demos button: read demos.json fresh and pop a menu of bundles.
        Picking one sets the knobs and pre-fills the prompt — the text
        stays editable before Send."""
        demos, err = load_bundles(DEMOS_FILE, DEFAULT_DEMOS)
        if err:
            self._append(f"[{err}]\n", "note")
        menu = tk.Menu(self, tearoff=0)
        for bundle in demos:
            menu.add_command(label=str(bundle["tag"]),
                             command=lambda b=bundle: self._apply_demo(b))
        menu.tk_popup(self.demos_btn.winfo_rootx(),
                      self.demos_btn.winfo_rooty()
                      + self.demos_btn.winfo_height())

    def _apply_demo(self, bundle):
        """Apply one demo bundle — the same machinery as settings.json,
        with Geometry excluded (a demo shouldn't move your window)."""
        notes = []
        settings = normalize_keys(
            {k: v for k, v in bundle.items() if str(k).lower() != "tag"},
            notes, DEMOS_FILE)
        if settings.pop("geometry", None) is not None:
            notes.append(f"{DEMOS_FILE}: Geometry is ignored in demos")
        self._append(f"\n[Demo “{bundle['tag']}” — edit the prompt below, "
                     "or just press Send]\n", "meta")
        self._apply_prefs(settings, notes, source=DEMOS_FILE)

    def _apply_prefs(self, settings, notes, source=SETTINGS_FILE):
        """Apply normalized preferences (from settings.json or a demo
        bundle). Values are normalized forgivingly ('claude-haiku-4.5' →
        'claude-haiku-4-5', '16pt' → 16); anything unusable is reported
        in the transcript and skipped."""
        if "model" in settings:
            raw_model = str(settings["model"]).strip()
            model = raw_model.replace(".", "-")
            if model not in PRICING:
                # A bare family name ("claude-sonnet", "sonnet") resolves to
                # its PRICING entry, so config files aren't locked to
                # version numbers across model bumps. An ambiguous name
                # ("claude" matches everything) resolves to the CHEAPEST
                # match. The combobox shows what was resolved.
                matches = [k for k in PRICING
                           if k.startswith(model)
                           or k.startswith("claude-" + model)]
                if matches:
                    model = min(matches, key=lambda k: PRICING[k])
            if model in PRICING:
                self.model.set(model)
                if "." in raw_model:
                    notes.append(f"{source}: model {raw_model!r} is "
                                 f"not a valid model ID — using '{model}'; "
                                 f"please fix the file")
            else:
                notes.append(f"{source}: unknown model "
                             f"{settings['model']!r} — keeping "
                             f"{self.model.get()}")
        if "persona" in settings:
            tag = match_tag(settings["persona"], self.personas)
            if tag:
                self.persona.set(tag)
            else:
                notes.append(f"{source}: unknown persona tag "
                             f"{settings['persona']!r} — keeping "
                             f"{self.persona.get()}")
        if "curiosity" in settings:
            tag = match_tag(settings["curiosity"],
                            list(self.curiosities) + [NO_CURIOSITY])
            if tag:
                self.curiosity.set(tag)
            else:
                notes.append(f"{source}: unknown curiosity tag "
                             f"{settings['curiosity']!r} — keeping "
                             f"{self.curiosity.get()}")
        if "size" in settings:
            raw_size = settings["size"]
            digits = re.search(r"\d+", str(raw_size))
            if digits:
                size = int(digits.group())
                self.font_size.set(size)
                self._apply_font_size()
                if str(raw_size).strip() != str(size):
                    notes.append(f"{source}: size {raw_size!r} read "
                                 f"as {size} — please make it a plain "
                                 f"number in the file")
            else:
                notes.append(f"{source}: unreadable size "
                             f"{settings['size']!r} — keeping "
                             f"{self.font_size.get()}")
        if "geometry" in settings:
            geo = str(settings["geometry"]).strip()
            pos = re.search(r"[+-]\d+[+-]\d+$", geo)
            if pos and re.fullmatch(r"(\d+x\d+)?[+-]\d+[+-]\d+", geo):
                self.geometry(pos.group())   # position only; size is ours
            else:
                notes.append(f"{source}: unreadable geometry "
                             f"{settings['geometry']!r} — expected "
                             f"'+x+y'")
        if "apikey" in settings:
            raw_path = os.path.expanduser(str(settings["apikey"]).strip())
            path = (raw_path if os.path.isabs(raw_path)
                    else os.path.join(CONFIG_DIR, raw_path))
            if os.path.exists(path):
                self._set_api_key(path)
            else:
                notes.append(f"{source}: api-key file {raw_path!r} "
                             f"not found — skipped")
        if "mefile" in settings:
            raw_path = os.path.expanduser(str(settings["mefile"]).strip())
            path = (raw_path if os.path.isabs(raw_path)
                    else os.path.join(CONFIG_DIR, raw_path))
            if os.path.exists(path):
                self._set_me(path)
            else:
                notes.append(f"{source}: me-file {raw_path!r} "
                             f"not found — skipped")
        for key, var in (("fetch", self.fetch), ("search", self.search),
                         ("sandbox", self.sandbox)):
            if key in settings:
                val = parse_bool(settings[key])
                if val is None:
                    notes.append(f"{source}: {key} must be true or false "
                                 f"— keeping {var.get()}")
                else:
                    var.set(val)
        prompt = str(settings.get("prompt") or "").strip()
        if prompt:
            self.entry.delete("1.0", "end")
            self.entry.insert("1.0", prompt)
        for n in notes:
            self._append(f"[{n}]\n", "note")

    # ---- transcript helpers ---------------------------------------------
    def _append(self, text, tag):
        self.view.configure(state="normal")
        self.view.insert("end", text, tag)
        self.view.see("end")
        self.view.configure(state="disabled")

    def _rerender_reply(self, reply):
        """Replace the raw streamed reply with a Markdown-rendered version.
        Streaming stays plain text (a `**` can be split across chunks); once
        the reply is complete it is re-rendered in place."""
        self.view.configure(state="normal")
        # Delete to end-1c, not "end": a delete range ending at "end" also
        # swallows the newline BEFORE the range (Tk avoids a trailing empty
        # line), which glued the reply onto the model-label line.
        self.view.delete("reply_start", "end-1c")
        self.view.configure(state="disabled")
        self._render_markdown(reply)

    def _render_markdown(self, text):
        lines = text.rstrip("\n").split("\n")
        in_code = False
        for i, line in enumerate(lines):
            nl = "\n" if i < len(lines) - 1 else ""
            # Fenced code blocks: the ``` fence lines are dropped, the
            # lines between them render verbatim in block style — no
            # inline Markdown parsing inside code.
            if re.match(r"\s*```", line):
                in_code = not in_code
                continue
            if in_code:
                self._append(line + nl, "md_codeblock")
                continue
            heading = re.match(r"(#{1,6})\s+(.*)", line)
            bullet = re.match(r"(\s*)[-*]\s+(.*)", line)
            if heading:
                level = min(len(heading.group(1)), 3)
                self._append(heading.group(2) + nl, f"md_h{level}")
            elif re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", line.strip()):
                self._append("─" * 40 + nl, "md_rule")
            elif bullet:
                self._append(bullet.group(1) + "• ", "assistant")
                self._render_inline(bullet.group(2))
                self._append(nl, "assistant")
            else:
                self._render_inline(line)
                self._append(nl, "assistant")

    def _render_inline(self, line):
        pos = 0
        for m in INLINE_MD.finditer(line):
            if m.start() > pos:
                self._append(line[pos:m.start()], "assistant")
            bold_ital, bold, ital, code = m.groups()
            if bold_ital is not None:
                self._append(bold_ital, "md_bolditalic")
            elif bold is not None:
                self._append(bold, "md_bold")
            elif ital is not None:
                self._append(ital, "md_italic")
            else:
                self._append(code, "md_code")
            pos = m.end()
        if pos < len(line):
            self._append(line[pos:], "assistant")

    def _reset(self):
        if self.streaming:
            return
        self.history.clear()
        self._web_used_last_turn = False   # New clears the whole conversation
        self.fetch.set(False)              # web access is per-conversation:
        self.search.set(False)             # a clean run starts offline
        self.sandbox.set(False)            # ...and without the sandbox
        if self.log and self.log[-1]["kind"] != "divider":
            self.log.append({"kind": "divider"})   # Save keeps the whole session
        self.view.configure(state="normal")
        self.view.delete("1.0", "end")
        self.view.configure(state="disabled")
        self._reload_choices()
        self.status.set(f"new conversation, personas & curiosities reloaded "
                        f"— session total: ${self.spend:.6f}")

    def _reload_choices(self):
        """Re-read the JSON files (wired to New). A selection whose tag
        survived the edit stays; a vanished tag falls back to the default.
        Parse errors are shown in the transcript."""
        self.personas, err_p = load_choices(PERSONAS_FILE, DEFAULT_PERSONAS)
        self.curiosities, err_c = load_choices(CURIOSITIES_FILE,
                                               DEFAULT_CURIOSITIES)
        for err in (err_p, err_c):
            if err:
                self._append(f"[{err}]\n", "note")
        self.persona_box["values"] = list(self.personas)
        self.persona_box["height"] = len(self.personas)
        self.persona_box["width"] = self._fit_width(self.personas)
        if self.persona.get() not in self.personas:
            self.persona.set(next(iter(self.personas)))
        self.curiosity_box["values"] = [NO_CURIOSITY] + list(self.curiosities)
        self.curiosity_box["height"] = len(self.curiosities) + 1
        self.curiosity_box["width"] = self._fit_width(self.curiosities)
        if (self.curiosity.get() != NO_CURIOSITY
                and self.curiosity.get() not in self.curiosities):
            self.curiosity.set(NO_CURIOSITY)

    # ---- send / stream ---------------------------------------------------
    def _show_user_turn(self, text, suffix, persona_hint=False, web_note=""):
        """Transcript display for an outgoing turn: the You: header, then
        the injected persona, me-file, web tools, and rider as indented
        annotation lines — visible, not hidden — then the user's own words."""
        persona = self.persona.get()
        self._append("\nYou:\n", "user")
        if persona_hint:
            self._append("  [Persona changed mid-chat — replies may echo "
                         "the old voice; press New]\n", "note")
        self._append(f"  [Persona “{persona}”: {self.personas[persona]}]\n",
                     "meta")
        if self.me_text:
            self._append(f"  [Me-file “{self.me_name}” rides along, "
                         f"~{len(self.me_text) // 4} tokens]\n", "meta")
        if web_note:
            self._append(f"  [{web_note}]\n", "meta")
        if suffix:
            self._append("  " + suffix.strip() + "\n", "curious")
        self._append("\n" + text + "\n", "prompt")
        self._append(f"\n{self.model.get()}:\n", "user")
        self.view.mark_set("reply_start", "end-1c")   # re-render point

    def _current_system(self):
        """The system prompt actually sent: the persona text plus, when
        loaded, the Me-file appended as context about the user."""
        system = self.personas[self.persona.get()]
        if self.me_text:
            system += (f"\n\n[About the user — from their Me-file "
                       f"“{self.me_name}”]\n{self.me_text}")
        return system

    def _persona_changed_midchat(self):
        """True when the persona differs from the last send and history
        already exists — the old voice in the resent history usually
        out-pulls the new system prompt; New is the clean switch."""
        persona = self.persona.get()
        changed = (bool(self.history)
                   and self._last_sent_persona not in (None, persona))
        self._last_sent_persona = persona
        return changed

    def _current_suffix(self):
        tag = self.curiosity.get()
        return "" if tag == NO_CURIOSITY else "\n\n" + self.curiosities[tag]

    def _on_return(self, event):
        if event.state & 0x0001:   # Shift held -> let the default newline through
            return None
        self._send()
        return "break"

    def _send(self):
        if self.streaming:
            return
        text = self.entry.get("1.0", "end").strip()
        if not text:
            return
        if self.client is None:
            self._append("\n[the 'anthropic' package isn't installed — "
                         "run: pip install anthropic]\n", "note")
            return

        self.entry.delete("1.0", "end")
        self._last_prompt = text
        persona_hint = self._persona_changed_midchat()
        suffix = self._current_suffix()
        # The prior turn's search results arrived as encrypted replay blobs;
        # this app resends plain text only, so the model keeps its summary
        # but loses the source pages. Say so, once, where it happens.
        if self._web_used_last_turn and self.history:
            evidence = ("search evidence from the prior turn was not resent "
                        "— the model keeps its summary, not the sources")
            self._append(f"\n[{evidence}]\n", "note")
            self.log.append({"kind": "note", "text": evidence})
        self._web_used_last_turn = False
        tools = ([FETCH_TOOL] if self.fetch.get() else []) \
              + ([SEARCH_TOOL] if self.search.get() else []) \
              + ([EXEC_TOOL] if self.sandbox.get() else [])
        web_note = self._web_annotation()
        self.history.append({"role": "user", "content": text + suffix})
        self.log.append({"kind": "user", "text": text + suffix,
                         "persona": self.persona.get(),
                         "me": self.me_name,
                         "curiosity": self.curiosity.get() if suffix else "",
                         "web": web_note})
        self._show_user_turn(text, suffix, persona_hint, web_note)

        self.streaming = True
        self._cancel = False
        self._gen += 1                       # this turn's id; older output is dropped
        self._sent_model = self.model.get()
        self.send_btn.set_mode("Stop", "#e0a84e", "#c8923a", self._stop)
        self._spinning = True
        self._turn_start = time.monotonic()
        self._spin()
        threading.Thread(
            target=self._worker,
            args=(self._gen, self._sent_model, self._current_system(),
                  list(self.history), tools),
            daemon=True,
        ).start()

    def _web_annotation(self):
        """The outgoing-turn annotation for enabled server tools: what is
        armed and what each mechanism costs."""
        parts = []
        if self.search.get():
            parts.append(f"search ≤{SEARCH_TOOL['max_uses']} "
                         f"@ ${SEARCH_COST:.2f}")
        if self.fetch.get():
            parts.append(f"fetch ≤{FETCH_TOOL['max_uses']}, tokens only")
        if self.sandbox.get():
            parts.append("Linux sandbox (free tier, then $0.05/hr)")
        return "Tools enabled: " + ", ".join(parts) if parts else ""

    def _worker(self, gen, model, system, messages, tools=None):
        """Runs OFF the UI thread (Tkinter isn't thread-safe). Pushes
        (gen, kind, payload) onto the queue; _pump drops any gen that is no
        longer the current turn — so a worker abandoned by Stop (or New)
        can finish blocked in the SDK and its late output is simply ignored.

        A server-tool turn can stop with stop_reason 'pause_turn' when the
        server-side loop hits its own iteration limit mid-work. We resume it
        by appending the paused assistant content and re-streaming, up to
        MAX_CONTINUATIONS, so a long search/fetch/code turn finishes instead
        of being silently truncated."""
        stream = None
        try:
            convo = list(messages)
            final = None
            for _ in range(MAX_CONTINUATIONS + 1):
                with self.client.messages.stream(
                    model=model,
                    max_tokens=MAX_TOKENS,
                    system=system,
                    messages=convo,
                    # Server-side tools run mid-request on Anthropic's side;
                    # the stream simply pauses while a search, fetch, or code
                    # cell executes.
                    **({"tools": tools} if tools else {}),
                    # We deliberately omit `thinking` — valid on every model
                    # here (Fable runs adaptive, Opus runs without). On Fable
                    # 5 a safety refusal surfaces below as stop_reason ==
                    # "refusal"; a production Fable call would add the
                    # server-side `fallbacks` parameter here so Opus 4.8
                    # rescues the turn — exactly what you watched happen in
                    # the Desktop App.
                ) as stream:
                    self._active_stream = stream   # so Stop can force-close
                    try:
                        for chunk in stream.text_stream:
                            if self._cancel:
                                break
                            self.q.put((gen, "text", chunk))
                    except Exception:
                        # A Stop-triggered close() interrupts the blocked
                        # read as an exception; if this turn was abandoned,
                        # just exit quietly — the UI already moved on.
                        if gen != self._gen or self._cancel:
                            return
                        raise
                    if gen != self._gen or self._cancel:
                        return   # abandoned by Stop/New — UI already finalized
                    final = stream.get_final_message()
                if final.stop_reason != "pause_turn":
                    break
                # Resume: hand the paused assistant content back and continue.
                convo = convo + [{"role": "assistant",
                                  "content": final.content}]
                self.q.put((gen, "pause", None))
            # Carry the send-time model with the result: the combobox may
            # have been switched mid-stream, and the bill belongs to the
            # model that actually served the request.
            self.q.put((gen, "done", (model, final)))
        except Exception as exc:   # noqa: BLE001 — surface everything to the user
            self.q.put((gen, "error", f"{type(exc).__name__}: {exc}"))
        finally:
            if self._active_stream is stream:   # don't clobber a newer turn
                self._active_stream = None

    def _pump(self):
        try:
            while True:
                gen, kind, payload = self.q.get_nowait()
                if gen != self._gen:
                    continue   # output from a stopped/superseded turn — drop
                if kind == "text":
                    self._append(payload, "assistant")
                elif kind == "pause":
                    self._append("\n  [turn paused by a server tool — "
                                 "continuing]\n", "note")
                elif kind == "error":
                    self._spinning = False   # stop before writing status
                    self.log.append({"kind": "note", "text": payload})
                    self._append(f"\n[{payload}]\n", "note")
                    self.status.set(f"error — see transcript    "
                                    f"session total: ${self.spend:.6f}")
                    self._finish()
                elif kind == "done":
                    self._on_done(*payload)
        except queue.Empty:
            pass
        self.after(50, self._pump)

    def _stop(self):
        """Stop button: finalize the turn IMMEDIATELY on the UI thread, so
        Stop is instant even when the worker is blocked deep in the SDK
        waiting on a sandbox or search (a close() from here can't reliably
        interrupt that blocked read). We bump the generation so the
        abandoned worker's eventual output is dropped, read whatever the
        stream has accumulated for an estimated bill, then best-effort
        close the connection so the orphaned request winds down."""
        if not self.streaming:
            return
        self._cancel = True
        self._gen += 1            # abandon this turn; late worker output ignored
        s = self._active_stream
        snap = None
        if s is not None:
            try:
                snap = s.current_message_snapshot
            except Exception:      # noqa: BLE001 — no snapshot yet
                snap = None
            try:
                s.close()
            except Exception:      # noqa: BLE001 — best-effort interrupt
                pass
        self._on_stopped(self._sent_model, snap)

    def _on_stopped(self, model, snap):
        """User pressed Stop. Keep whatever text streamed as the (partial)
        reply, mark it stopped, and post an ESTIMATED bill — the turn never
        emitted its final usage event, so input tokens are exact but output
        is a running count."""
        self._spinning = False
        reply = ""
        if snap is not None:
            reply = "".join(getattr(b, "text", "") for b in
                            getattr(snap, "content", []) or []
                            if getattr(b, "type", "") == "text")
        if reply.strip():
            self.history.append({"role": "assistant", "content": reply})
            self.log.append({"kind": "assistant", "model": model,
                             "text": reply})
            self._rerender_reply(reply)
        elif self.history and self.history[-1]["role"] == "user":
            self.history.pop()   # nothing usable — don't resend a dead turn
        self._append("\n  [stopped by user]\n", "note")
        self.log.append({"kind": "note", "text": "stopped by user"})
        self._add_cost_estimate(model, snap)
        self._append("\n", "assistant")
        self._finish()

    def _add_cost_estimate(self, model, snap):
        """Meter line for a stopped turn: same shape as _add_cost, but every
        figure carries (est) because the final usage never arrived. Input
        tokens are exact (set at message_start); output is the count so far;
        any search that already ran is included."""
        rate_in, rate_out = PRICING.get(model, (0.0, 0.0))
        u = getattr(snap, "usage", None) if snap is not None else None
        in_tok = getattr(u, "input_tokens", 0) or 0
        out_tok = getattr(u, "output_tokens", 0) or 0
        in_cost = in_tok * rate_in / 1_000_000
        out_cost = out_tok * rate_out / 1_000_000
        token_cost = in_cost + out_cost
        searches = fetches = runs = 0
        if snap is not None:
            searches, fetches = self._web_usage(snap)
            runs = self._code_runs(snap)
        search_cost = searches * SEARCH_COST
        self.spend += token_cost + search_cost
        elapsed = time.monotonic() - self._turn_start
        parts = [f"Elapsed: {elapsed:.1f}s (stopped)",
                 f"Tokens: In {in_tok} (+${in_cost:.6f}) "
                 f"/ Out {out_tok} (+${out_cost:.6f})"]
        if searches:
            parts.append(f"Searches: {searches} (+${search_cost:.6f})")
        if fetches:
            parts.append(f"Fetches: {fetches} (tokens only)")
        if runs:
            parts.append(f"Code runs: {runs} (free tier / $0.05/hr)")
        self.status.set("    |    ".join(parts)
                        + f"    |    Session total: ${self.spend:.6f} (est)")

    def _on_done(self, model, msg):
        self._spinning = False   # stop before the final meter reading lands
        if msg.stop_reason == "refusal":
            # Drop the refused user turn too — left in place it would be
            # re-sent (re-billed, and likely re-refused) on every later turn.
            self.history.pop()
            details = getattr(msg, "stop_details", None)
            cat = getattr(details, "category", None) if details else None
            expl = getattr(details, "explanation", None) if details else None
            note = ("refused by safety classifier"
                    + (f" (category: {cat})" if cat else "")
                    + " — turn removed from history; a production call "
                    "would auto-fall-back to Opus 4.8 here. In the Lab "
                    "the refusal is the exhibit: switch the Model knob "
                    "and press Send to retry")
            self.log.append({"kind": "note", "text": note})
            self._append(f"\n[{note}]\n", "note")
            if expl:
                self._append(f"  [classifier explanation: {expl}]\n", "note")
                self.log.append({"kind": "note",
                                 "text": f"classifier explanation: {expl}"})
        else:
            reply = "".join(b.text for b in msg.content if b.type == "text")
            self.history.append({"role": "assistant", "content": reply})
            self.log.append({"kind": "assistant", "model": model,
                             "text": reply})
            self._rerender_reply(reply)
            self._note_web_activity(msg)
            searches, fetches = self._web_usage(msg)
            self._web_used_last_turn = bool(searches or fetches)
        self._add_cost(model, msg)
        self._append("\n", "assistant")
        self._finish()

    def _note_web_activity(self, msg):
        """Post-hoc transcript notes for server-side tool use, read from
        the final message: the model's server_tool_use blocks say what was
        ATTEMPTED, the paired result blocks say whether it worked (a fetch
        of a model-invented URL is refused, for example), and citation
        blocks say which sources the reply leaned on. The live stream
        itself only shows a pause."""
        failures = {}
        for b in msg.content:
            if getattr(b, "type", "") in ("web_search_tool_result",
                                          "web_fetch_tool_result"):
                code = getattr(getattr(b, "content", None),
                               "error_code", None)
                if code:
                    failures[b.tool_use_id] = code
        notes = []   # (text, clickable url or None, suffix, display tag)
        for b in msg.content:
            if getattr(b, "type", "") == "server_tool_use":
                inp = dict(getattr(b, "input", None) or {})
                fail = failures.get(b.id)
                suffix = f"  ({fail})" if fail else ""
                if b.name == "web_search":
                    verb = "search failed" if fail else "searched"
                    notes.append((f'{verb}: "{inp.get("query", "?")}"',
                                  None, suffix, "note" if fail else "curious"))
                elif b.name == "web_fetch":
                    verb = "fetch failed" if fail else "fetched"
                    notes.append((f"{verb}: ", inp.get("url", "?"),
                                  suffix, "note" if fail else "curious"))
        seen = []
        for b in msg.content:
            for c in (getattr(b, "citations", None) or []):
                url = getattr(c, "url", None)
                if url and url not in seen:
                    seen.append(url)
                    notes.append(("source: ", url, "", "curious"))
        if notes:
            self._append("\n", "curious")   # never glue onto the reply line
        for text, url, suffix, tag in notes:
            self._append(f"  [{text}", tag)
            if url:
                self._append_link(url, tag)
            self._append(f"{suffix}]\n", tag)
            self.log.append({"kind": "note",
                             "text": text + (url or "") + suffix})
        self._note_code_activity(msg)

    def _note_code_activity(self, msg):
        """Post-hoc notes for the Linux sandbox, read from the final message.
        This is the 'it really executed' evidence — the live stream only
        pauses. Two sub-tools surface here: bash (commands + stdout/stderr,
        non-zero exit in red) and the file editor (create/edit — the actual
        file the model WROTE, e.g. the script itself, shown regardless of
        whether the model bothered to narrate it)."""
        for b in msg.content:
            t = getattr(b, "type", "")
            if t == "server_tool_use" and b.name == "bash_code_execution":
                cmd = dict(getattr(b, "input", None) or {}).get("command", "?")
                self._append(f"\n  [ran: {cmd}]\n", "curious")
                self.log.append({"kind": "note", "text": f"ran: {cmd}"})
            elif (t == "server_tool_use"
                  and b.name == "text_editor_code_execution"):
                inp = dict(getattr(b, "input", None) or {})
                cmd = inp.get("command", "")
                path = inp.get("path", "?")
                body = inp.get("file_text") or inp.get("new_str") or ""
                if cmd in ("create", "str_replace", "insert") and body:
                    verb = "wrote" if cmd == "create" else "edited"
                    self._append(f"\n  [{verb} {path}]\n", "curious")
                    self.log.append({"kind": "note",
                                     "text": f"{verb} {path}"})
                    self._append_code_block(body)
                    # Save the full file body too, so the exported Markdown
                    # carries the script, not just a "[wrote ...]" note.
                    lang = "python" if str(path).endswith(".py") else ""
                    self.log.append({"kind": "code", "lang": lang,
                                     "text": body})
            elif t == "bash_code_execution_tool_result":
                c = getattr(b, "content", None)
                rc = getattr(c, "return_code", 0) or 0
                out = (getattr(c, "stdout", "") or "").rstrip()
                err = (getattr(c, "stderr", "") or "").rstrip()
                if out:
                    for line in out.split("\n"):
                        self._append(f"{line}\n", "md_codeblock")
                    self.log.append({"kind": "code", "lang": "", "text": out})
                if rc or err:
                    self._append(f"  [exit {rc}] {err}\n", "note")
                    self.log.append({"kind": "note",
                                     "text": f"exit {rc}: {err}"})

    def _copy_selection(self, _event=None):
        """Copy the transcript selection to the clipboard. Explicit binding
        so copy works in the read-only (disabled) transcript on every
        platform, including over code blocks."""
        try:
            sel = self.view.get("sel.first", "sel.last")
        except tk.TclError:
            return "break"          # nothing selected
        if sel:
            self.clipboard_clear()
            self.clipboard_append(sel)
        return "break"

    def _append_code_block(self, text, max_lines=60):
        """Render file contents (a written script, an edit) in the code
        block style. The visual indent comes from the md_codeblock tag's
        left margin, NOT from injected spaces — so a copy-paste yields the
        exact source, without leading whitespace that would break Python.
        Capped so a runaway summary doc truncates instead of flooding — the
        cap itself signals the model wrote something bulky."""
        lines = text.rstrip("\n").split("\n")
        for line in lines[:max_lines]:
            self._append(f"{line}\n", "md_codeblock")
        extra = len(lines) - max_lines
        if extra > 0:
            self._append(f"… ({extra} more lines)\n", "note")

    def _append_link(self, url, tag):
        """Insert url into the transcript as a clickable link — underlined,
        hand cursor, opens the default browser. Pure rendering: no tool,
        no API involvement."""
        name = f"link{self._link_seq}"
        self._link_seq += 1
        self.view.tag_config(name, underline=True)
        self.view.tag_bind(name, "<Button-1>",
                           lambda e, u=url: webbrowser.open(u))
        self.view.tag_bind(name, "<Enter>",
                           lambda e: self.view.configure(cursor="hand2"))
        self.view.tag_bind(name, "<Leave>",
                           lambda e: self.view.configure(cursor=""))
        self._append(url, (tag, name))

    @staticmethod
    def _web_usage(msg):
        """(searches, fetches) from the usage block — both 0 when no
        server tool ran (the field is absent then)."""
        st = getattr(msg.usage, "server_tool_use", None)
        return (getattr(st, "web_search_requests", 0) or 0,
                getattr(st, "web_fetch_requests", 0) or 0)

    @staticmethod
    def _code_runs(msg):
        """Count sandbox bash executions from the content blocks. The API
        prices code execution by container-time, not per run, and does NOT
        report it in `usage` — so this is a run COUNT, never a dollar
        figure. The meter says so."""
        return sum(1 for b in msg.content
                   if getattr(b, "type", "") == "bash_code_execution_tool_result")

    def _add_cost(self, model, msg):
        """The meter: one parenthesized sub-cost per mechanism — tokens,
        then searches — and the session total sums them. Zero counts are
        not shown; fetched pages already appear inside the token count."""
        rate_in, rate_out = PRICING.get(model, (0.0, 0.0))
        u = msg.usage
        in_cost = u.input_tokens * rate_in / 1_000_000
        out_cost = u.output_tokens * rate_out / 1_000_000
        token_cost = in_cost + out_cost
        searches, fetches = self._web_usage(msg)
        search_cost = searches * SEARCH_COST
        self.spend += token_cost + search_cost
        elapsed = time.monotonic() - self._turn_start
        parts = [f"Elapsed: {elapsed:.1f}s",
                 f"Tokens: In {u.input_tokens} (+${in_cost:.6f}) "
                 f"/ Out {u.output_tokens} (+${out_cost:.6f})"]
        if searches:
            parts.append(f"Searches: {searches} (+${search_cost:.6f})")
        if fetches:
            parts.append(f"Fetches: {fetches} (tokens only)")
        runs = self._code_runs(msg)
        if runs:
            # Container-time isn't in `usage`; count only, not a dollar
            # figure — the one lab mechanism the meter cannot price.
            parts.append(f"Code runs: {runs} (free tier / $0.05/hr)")
        self.status.set("    |    ".join(parts)
                        + f"    |    Session total: ${self.spend:.6f}")

    def _spin(self):
        """Live activity indicator in the status bar: a rotating glyph and
        an elapsed-seconds clock, so a long server-tool turn (a search, or
        the sandbox grinding on pi) visibly shows it's alive — and the
        clock stands in for the money quietly spinning away. Runs on the UI
        thread via `after`; stops the moment the turn finishes."""
        if not self._spinning:
            return
        frames = "◐◓◑◒"
        i = int((time.monotonic() - self._turn_start) * 5) % len(frames)
        secs = time.monotonic() - self._turn_start
        self.status.set(f"{frames[i]}  working… {secs:0.1f}s   "
                        f"(tokens tick, the meter tallies when it returns)")
        self.after(120, self._spin)

    def _finish(self):
        self.streaming = False
        self._spinning = False
        self._cancel = False
        self._active_stream = None
        self.send_btn.set_mode("Send", "#8fd18f", "#6fbf6f", self._send)
        # Put the sent prompt back in the entry, so re-asking the same
        # question with different knobs is one click away — but never
        # clobber anything typed while the reply was streaming.
        if (self._last_prompt
                and not self.entry.get("1.0", "end").strip()):
            self.entry.insert("1.0", self._last_prompt)

    # ---- quit / window position -------------------------------------------
    def _quit(self):
        self._save_geometry()
        self.destroy()

    def _save_geometry(self):
        """Remember the window POSITION in settings.json — written on Quit
        or window close, applied at next startup. The size is not saved:
        the app computes its own natural size, so it always fits its
        content. A missing settings file is created; a broken one is
        left alone."""
        pos = re.search(r"[+-]\d+[+-]\d+$", self.geometry())
        if not pos:
            return
        geo = pos.group()       # "+x+y" — position only
        path = os.path.join(CONFIG_DIR, SETTINGS_FILE)
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            raw = []
        except (OSError, json.JSONDecodeError):
            return   # never clobber a file the user needs to fix
        if isinstance(raw, list):
            for item in raw:
                if (isinstance(item, dict) and len(item) == 1
                        and next(iter(item)).strip().lower() == "geometry"):
                    item[next(iter(item))] = geo
                    break
            else:
                raw.append({"Geometry": geo})
        elif isinstance(raw, dict):
            key = next((k for k in raw if k.strip().lower() == "geometry"),
                       "Geometry")
            raw[key] = geo
        else:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(raw, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except OSError:
            pass   # closing anyway; don't block quit on a write error

    # ---- save ------------------------------------------------------------
    def _file_dialog(self, func, **kwargs):
        """Run a Tk file dialog. macOS AppKit prints a one-time "NSSavePanel
        overrides the method identifier" diagnostic to stderr when the
        native panel class first loads — it comes from the Tk framework,
        not this script, and is harmless. Silence stderr around the dialog
        unless --verbose asked to keep everything visible."""
        if self.verbose:
            return func(parent=self, **kwargs)
        stderr_copy = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
        try:
            return func(parent=self, **kwargs)
        finally:
            os.dup2(stderr_copy, 2)
            os.close(devnull)
            os.close(stderr_copy)

    # ---- api key -----------------------------------------------------------
    def _load_api_key(self):
        """API button: pick a file whose first line is an Anthropic API
        key — the no-terminal alternative to environment variables. A file
        named apikey.txt next to the configs loads automatically at
        startup."""
        path = self._file_dialog(
            filedialog.askopenfilename,
            title="Load an API key file (first line: sk-ant-…)",
            filetypes=[("Text", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self._set_api_key(path)

    def _set_api_key(self, path):
        """Read a key file and rebuild the API client with it. Only the
        last four characters are ever shown; the key itself is never
        logged, echoed, or written anywhere."""
        if anthropic is None:
            self._append("\n[the 'anthropic' package isn't installed — "
                         "run: pip install anthropic]\n", "note")
            return
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read().strip()
        except OSError as exc:
            self._append(f"\n[api-key file: {exc}]\n", "note")
            return
        key = content.split()[0] if content else ""
        name = os.path.basename(path)
        if not key:
            self._append(f"\n[api-key file “{name}” is empty]\n", "note")
            return
        self.client = anthropic.Anthropic(api_key=key)
        masked = key[-4:] if len(key) >= 12 else "????"
        warn = ("" if key.startswith("sk-ant-")
                else " — warning: doesn't look like an Anthropic key")
        self._append(f"\n[API key …{masked} loaded from “{name}”"
                     f"{warn}]\n", "meta")
        self.status.set(f"API key …{masked} active (from {name})")

    # ---- me-file -----------------------------------------------------------
    def _load_me(self):
        """Pick a Me-file (Markdown): its contents ride along in the system
        prompt of every call — the visible, honest version of "the AI knows
        me". Loading a new file replaces the previous one."""
        path = self._file_dialog(
            filedialog.askopenfilename,
            title="Load a Me-file (Markdown)",
            filetypes=[("Markdown", "*.md"), ("Text", "*.txt"),
                       ("All files", "*.*")],
        )
        if path:
            self._set_me(path)

    def _set_me(self, path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                self.me_text = f.read().strip()
        except OSError as exc:
            self._append(f"\n[me-file: {exc}]\n", "note")
            return
        self.me_name = os.path.basename(path)
        tokens = len(self.me_text) // 4   # rough: ~4 chars per token
        self._append(f"\n[Me-file loaded: “{self.me_name}”, ~{tokens} tokens "
                     "— appended to the system prompt, re-billed as input "
                     "on every turn]\n", "meta")
        self.status.set(f"me-file “{self.me_name}” active — "
                        f"~{tokens} tok added to every call")

    def _save(self):
        """Save the whole session (across News) as a Markdown file."""
        default = datetime.date.today().isoformat() + ".chat.md"
        path = self._file_dialog(
            filedialog.asksaveasfilename,
            title="Save session as Markdown",
            initialfile=default,
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._session_markdown())
            # Confirm in the transcript, not the status bar — the meter's
            # last cost reading stays put where you can still read it.
            self._append(f"\n[session saved — {path}]\n", "note")
        except OSError as exc:
            self._append(f"\n[save failed: {exc}]\n", "note")

    def _session_markdown(self):
        """The session journal as Markdown, laid out for skimming: a rule
        before every turn, Tone/Curiosity as their own lines, the prompt
        as a blockquote (visibly input, not output), and reply headings
        demoted two levels so the model's own structure can't out-shout
        the turn labels."""
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [f"# Curiosity Lab chat — {now}", "",
                 f"*Exported by Curiosity Lab for Claude v{__version__}; "
                 f"session spend ${self.spend:.6f}.*", ""]
        for entry in self.log:
            kind = entry["kind"]
            if kind == "user":
                lines += ["---", "", "## You", "",
                          f"- Persona: {entry['persona']}"]
                if entry.get("me"):
                    lines.append(f"- Me-file: {entry['me']}")
                if entry["curiosity"]:
                    lines.append(f"- Curiosity: {entry['curiosity']}")
                if entry.get("web"):
                    lines.append(f"- {entry['web']}")
                lines.append("")
                lines += [f"> {ln}".rstrip() for ln in entry["text"].split("\n")]
                lines.append("")
            elif kind == "assistant":
                lines += [f"## {entry['model']}", "",
                          demote_headings(entry["text"]), ""]
            elif kind == "note":
                lines += [f"> [{entry['text']}]", ""]
            elif kind == "code":
                # Fenced block so a saved script / stdout is copy-paste
                # clean, not wrapped in blockquote brackets.
                lines += [f"```{entry.get('lang', '')}",
                          entry["text"].rstrip("\n"), "```", ""]
            elif kind == "divider":
                lines += ["*(new conversation)*", ""]
        return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    class _HelpFormat(argparse.RawDescriptionHelpFormatter):
        """Keep the hand-wrapped description and end with a blank line."""
        def format_help(self):
            return super().format_help() + "\n"

    parser = argparse.ArgumentParser(
        prog="curiosity-lab",
        formatter_class=_HelpFormat,
        description="Curiosity Lab for Claude — a small chat window where "
                    "you pick a persona,\nadd curiosity to your questions, "
                    "and watch what every answer costs.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="keep stderr visible (don't suppress Tk "
                             "file-dialog diagnostics)")
    parser.add_argument("-V", "--version", action="version",
                        version=f"Curiosity Lab for Claude {__version__}")
    args = parser.parse_args()
    Workbench(verbose=args.verbose).mainloop()
