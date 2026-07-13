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

__version__ = "2.9.0"

import base64
import datetime
import io
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

# Pillow is OPTIONAL (v2.9.0). Native Tk renders PNG/GIF with integer-only
# scaling; Pillow adds smooth downscaling and JPEG/WebP. The app degrades
# gracefully to native Tk when Pillow isn't importable — no new hard dependency.
try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None


# - Prefer to read this from the scipt's --help option:
GITHUB_URL = "https://github.com/John-D-B/Curiosity-Lab-for-Claude"

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

# Sandbox image output (v2.9.0): files the sandbox writes (e.g. a matplotlib
# PNG) are captured server-side and referenced by `file_id` in the code-
# execution result blocks. The Files API downloads the bytes — a beta path,
# so its calls carry this beta flag. Download is free; the image was already
# billed inside the code-execution tokens.
FILES_BETA = "files-api-2025-04-14"
IMAGE_MAX_WIDTH = 600            # inline render cap (px); larger images downscale

# Does the outgoing prompt ask the model to actually RUN code? Keyword-based
# and deliberately conservative — used only to warn, at Send, when the Sandbox
# tool is off: the model can't execute, so it will IMAGINE the output (the
# "imagined vs actual execution" lesson). Tune the trigger words here.
EXEC_INTENT_RE = re.compile(
    r"\bsandbox\b|\bexecute\b"
    r"|\brun\s+(it|this|and|the\s+(script|code|program))\b", re.I)

FONT_FAMILY = "Segoe UI"             # Tk substitutes the system font on macOS
MONO_FAMILY = "Courier New"          # for `code` spans
FONT_SIZES = [9, 10, 11, 12, 14, 16, 18, 20]

# ---- Section headers: the "Response" / "Prompt" labels above the two boxes.
# Findable knobs — tweak these to restyle both labels at once.
SECTION_LABEL_SIZE = 15              # point size
SECTION_LABEL_WEIGHT = "bold"        # "normal" | "bold"
SECTION_LABEL_SLANT = "italic"       # "roman" | "italic"
SECTION_LABEL_COLOR = "#8a8a8a"      # text colour (grey)
SECTION_LABEL_PAD = 6                # grey space above & below each label (px)
DEFAULT_FONT_SIZE = 12

# Prompt box height, in lines. FIXED — a long prompt (e.g. a demo with a
# Parameters block at the end) scrolls inside the box, with a scrollbar that
# appears only on overflow, rather than growing the box and pushing the whole
# window taller. The user enlarges the box by dragging the sash above it.
PROMPT_MIN_LINES = 3

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
NO_PERSONAS = "(none)"
## PERSONAS_TINT_TOP = "..."
PERSONAS_TINT_BODY = "#FCEAFA"

CURIOSITIES_FILE = "curiosities.json"
NO_CURIOSITY = "(none)"
## CURIOSITIES_TINT_TOP = "..."
CURIOSITIES_TINT_BODY = "#E2F3FE"  ## "#FFFEDE"

SETTINGS_FILE = "settings.json"

APIKEY_FILE = "apikey.txt"   # auto-loaded at startup when present

DEMOS_FILE = "demos.json"
NO_DEMO = "(none)"
DEMOS_TINT_TOP = "#9DEC9D"
DEMOS_TINT_BODY = "#EBFBE9"

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


class _MenuHover:
    """Makes a readonly combobox's dropdown feel like a native Mac menu: the
    highlight follows the cursor with no button held, and a delayed tooltip
    shows the hovered item's full text (for values the narrow box clips).
    macOS doesn't deliver hover-motion to the popdown, so this POLLS the
    pointer while the list is open instead of relying on <Motion> events."""

    def __init__(self, combo, listbox_path, textfn=None, debug=False,
                 delay=400, period=70):
        self.combo = combo
        self.tk = combo.tk
        self.lb = listbox_path
        self.textfn = textfn     # optional: map an item's text -> tooltip text
        self.debug = debug       # -v: log tooltip events to stderr
        self.delay = delay
        self.period = period
        self.tip = None
        self.cur = None          # index the highlight/tooltip is currently on
        self.dwell = 0           # ms the cursor has rested on `cur`
        # A visible selection colour so the highlight shows even when the
        # listbox isn't focused (macOS mutes an inactive selection otherwise).
        try:
            self.tk.call(self.lb, "configure",
                         "-selectbackground", "#3875d7",
                         "-selectforeground", "white", "-activestyle", "none")
        except tk.TclError:
            pass
        self._poll()

    def _poll(self):
        try:
            mapped = int(self.tk.call("winfo", "ismapped", self.lb))
        except tk.TclError:
            return                          # widget gone — stop the loop
        if mapped:
            self._track()
        elif self.tip is not None or self.cur is not None:
            self._hide()
        self.combo.after(self.period, self._poll)

    def _track(self):
        try:
            px = int(self.tk.call("winfo", "pointerx", self.lb))
            py = int(self.tk.call("winfo", "pointery", self.lb))
            x = int(self.tk.call("winfo", "rootx", self.lb))
            y = int(self.tk.call("winfo", "rooty", self.lb))
            w = int(self.tk.call("winfo", "width", self.lb))
            h = int(self.tk.call("winfo", "height", self.lb))
        except tk.TclError:
            return
        if not (x <= px < x + w and y <= py < y + h):
            self._hide()
            return
        try:
            idx = int(self.tk.call(self.lb, "nearest", py - y))
            text = self.tk.call(self.lb, "get", idx)
        except (tk.TclError, ValueError):
            return
        if idx < 0:
            return
        if self.textfn is not None:         # Demos shows its prompt, not the tag
            try:
                text = self.textfn(text)
            except Exception:
                pass
        try:                                # the highlight follows the cursor
            self.tk.call(self.lb, "selection", "clear", 0, "end")
            self.tk.call(self.lb, "selection", "set", idx)
            self.tk.call(self.lb, "activate", idx)
        except tk.TclError:
            pass
        if idx != self.cur:
            self.cur = idx                  # moved → reset dwell, drop old tip
            self.dwell = 0
            self._destroy_tip()
        elif self.tip is None:
            self.dwell += self.period       # resting → show tip after delay
            if self.dwell >= self.delay:
                self._show(text, py)

    def _show(self, text, y):
        try:
            lb_x = int(self.tk.call("winfo", "rootx", self.lb))
            lb_w = int(self.tk.call("winfo", "width", self.lb))
        except tk.TclError:
            return
        self.tip = tk.Toplevel(self.combo)
        self.tip.wm_overrideredirect(True)
        try:
            self.tip.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(self.tip, text=str(text), justify="left", wraplength=420,
                 background="#ffffe0", foreground="#000000", relief="solid",
                 borderwidth=1, padx=5, pady=2).pack()
        # Prefer just right of the dropdown; if that runs off the screen edge,
        # flip to the left of it so the panel is always fully visible.
        self.tip.update_idletasks()
        tw = self.tip.winfo_reqwidth()
        sw = self.combo.winfo_screenwidth()
        x = lb_x + lb_w + 4
        if x + tw > sw - 8:
            x = max(8, lb_x - tw - 4)
        self.tip.wm_geometry(f"+{x}+{y - 6}")
        self.tip.lift()
        # Diagnostic — uncomment to trace hover firing under -v:
        # if self.debug:
        #     import sys
        #     print(f"[hover] show @ {x},{y-6}  lb={self.lb}", file=sys.stderr)

    def _destroy_tip(self):
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None

    def _hide(self):
        self._destroy_tip()
        self.cur = None
        self.dwell = 0


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
        self._images: list = []            # keep PhotoImage refs — Tk GCs them
        #                                    otherwise and the picture vanishes

        self._build_ui()
        # The app picks a sensible default size (wide enough for the top bar,
        # tall enough to read); the last SIZE and POSITION are then restored
        # from settings.json by _apply_settings below — see _save_geometry.
        self.update_idletasks()
        self.geometry(f"{max(self.winfo_reqwidth(), 900)}x640")
        self._welcome()   # greet the empty Response area with how to begin
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

    def _attach_menu_hover(self, combo, textfn=None):
        """Wire a hover tooltip onto a readonly combobox's dropdown list.
        `textfn` optionally maps an item's text to what the panel shows (Demos
        maps a tag to its prompt). Cosmetic and fully guarded — any failure
        here leaves the window working, just without the panel on that menu."""
        try:
            popdown = combo.tk.call("ttk::combobox::PopdownWindow", combo)
            _MenuHover(combo, f"{popdown}.f.l", textfn=textfn,
                       debug=self.verbose)
            # Diagnostic — uncomment to trace attach under -v:
            # if self.verbose:
            #     import sys
            #     print(f"[hover] attached  lb={popdown}.f.l", file=sys.stderr)
        except Exception:
            pass
            # if self.verbose:
            #     import sys
            #     print("[hover] attach FAILED", file=sys.stderr)

    def _demo_prompt(self, tag):
        """Tooltip text for a Demos item: its prompt (what Send will fire),
        not the tag echoed back. Falls back to the tag if there's no prompt."""
        if tag == NO_DEMO:
            return "(no demo — pick one to preload a prompt and settings)"
        bundle = self.demo_bundles.get(tag)
        if bundle:
            for k, v in bundle.items():
                if str(k).lower() in ("prompt", "preload"):
                    return str(v)
        return tag

    def _persona_text(self, tag):
        """Persona hover shows the system-prompt text, not the tag."""
        if tag == NO_PERSONAS:
            return "(no persona — the model answers without one)"
        return self.personas.get(tag) or tag

    def _curiosity_text(self, tag):
        """Curiosity hover shows the rider text, not the tag."""
        if tag == NO_CURIOSITY:
            return "(no rider — nothing appended to your prompt)"
        return self.curiosities.get(tag) or tag

    def _add_face(self, combo, textvar, color):
        """Overlay a coloured 'face' Label on a readonly combobox's field so it
        reads as FILLED — macOS aqua won't tint the native field itself. The
        face shows the current value, leaves the arrow exposed, and clicking it
        posts the dropdown. Inset a few px for breathing room. (Size doesn't
        rescale the selectors, so the face needs no font tracking.)"""
        face = tk.Label(combo.master, textvariable=textvar, background=color,
                        foreground="#000000", anchor="w", padx=6)
        face.place(in_=combo, x=3, y=3,
                   relwidth=1.0, relheight=1.0, width=-28, height=-6)
        face.bind("<Button-1>",
                  lambda e: combo.tk.call("ttk::combobox::Post", combo))

    def _section_label(self, parent, text):
        """A centred grey section header ('Response' / 'Prompt') above a box.
        Styling comes entirely from the SECTION_LABEL_* constants near the top
        of the file — change those to restyle both labels at once."""
        font = tkfont.Font(family=FONT_FAMILY, size=SECTION_LABEL_SIZE,
                           weight=SECTION_LABEL_WEIGHT,
                           slant=SECTION_LABEL_SLANT)
        return tk.Label(parent, text=text, font=font,
                        foreground=SECTION_LABEL_COLOR,
                        background=self.cget("bg"), anchor="center",
                        pady=SECTION_LABEL_PAD)

    def _tint_menu(self, combo, color):
        """Tint a combobox's dropdown-list background. ttk restyles the listbox
        when it posts, so reapply on <Map>. Cosmetic and fully guarded."""
        try:
            popdown = combo.tk.call("ttk::combobox::PopdownWindow", combo)
            lb = f"{popdown}.f.l"
            combo.tk.call(lb, "configure", "-background", color)
            reapply = combo.register(
                lambda: combo.tk.call(lb, "configure", "-background", color))
            combo.tk.call("bind", lb, "<Map>", f"+{reapply}")
        except Exception:
            pass

    def _money(self, amount):
        """USD for the status bar. Verbose keeps full 6-decimal precision.
        Otherwise two decimals — but a real cost that rounds down to zero
        shows '<$0.01', never a bare '$0.00', so "there is a cost" reads
        differently from "there is no cost"."""
        if self.verbose:
            return f"${amount:.6f}"
        if amount <= 0:
            return "$0.00"
        if round(amount, 2) == 0:
            return "<$0.01"
        return f"${amount:.2f}"

    @staticmethod
    def _thousands(n):
        """Group digits with apostrophes (Swiss style): 1234567 -> 1'234'567."""
        return f"{n:,}".replace(",", "’")

    def _build_ui(self):
        top = ttk.Frame(self, padding=(12, 10, 12, 6))
        top.pack(fill="x")

        # Selectors in one left-hand column: Demos, Model, Persona, Curiosity,
        # Size. All four menus share a single width (Size excepted), so load
        # every list first and size to the longest entry across them all.
        self._config_errors = []
        self.personas, err_p = load_choices(PERSONAS_FILE, DEFAULT_PERSONAS)
        self.curiosities, err_c = load_choices(CURIOSITIES_FILE,
                                               DEFAULT_CURIOSITIES)
        demos_now, err_d = load_bundles(DEMOS_FILE, DEFAULT_DEMOS)
        for e in (err_p, err_c, err_d):
            if e:
                self._config_errors.append(e)
        self.demo_bundles = {str(b["tag"]): b for b in demos_now}
        cur_values = [NO_CURIOSITY] + list(self.curiosities)
        # Cap the shared width so one long persona/demo name doesn't stretch
        # the whole column; a clipped value is still legible via the hover
        # panel over the open dropdown.
        menu_w = min(18, max(self._fit_width(list(PRICING)),
                             self._fit_width(self.personas),
                             self._fit_width(cur_values),
                             self._fit_width(list(self.demo_bundles)
                                             + [NO_DEMO])))

        selgrid = ttk.Frame(top)
        selgrid.pack(side="left", anchor="n")

        # Demos: a readonly Combobox — the same widget as the others, so the
        # widths line up exactly (a Menubutton renders wider for the same char
        # count, and chops when squeezed). Picking a tag loads that demo and
        # the box then shows it; values reload with New, like the other lists.
        ttk.Label(selgrid, text="Demos:").grid(row=0, column=0, sticky="w",
                                               pady=1)
        self.demo_choice = tk.StringVar(value=NO_DEMO)
        self.demos_box = ttk.Combobox(selgrid, textvariable=self.demo_choice,
                                      values=[NO_DEMO]
                                      + list(self.demo_bundles),
                                      height=len(self.demo_bundles) + 1,
                                      state="readonly", width=menu_w)
        self.demos_box.grid(row=0, column=1, sticky="w", padx=(4, 0), pady=1)
        self.demos_box.bind("<<ComboboxSelected>>", self._on_demo_selected)

        ttk.Label(selgrid, text="Model:").grid(row=1, column=0, sticky="w",
                                               pady=1)
        self.model = tk.StringVar(value=DEFAULT_MODEL)
        self.model_box = ttk.Combobox(selgrid, textvariable=self.model,
                                      values=list(PRICING), state="readonly",
                                      width=menu_w)
        self.model_box.grid(row=1, column=1, sticky="w", padx=(4, 0), pady=1)

        ttk.Label(selgrid, text="Persona:").grid(row=2, column=0, sticky="w",
                                                 pady=1)
        self.persona = tk.StringVar(value=next(iter(self.personas)))
        self.persona_box = ttk.Combobox(selgrid, textvariable=self.persona,
                                        values=[NO_PERSONAS]
                                        + list(self.personas),
                                        height=len(self.personas) + 1,
                                        state="readonly", width=menu_w)
        self.persona_box.grid(row=2, column=1, sticky="w", padx=(4, 0), pady=1)

        ttk.Label(selgrid, text="Curiosity:").grid(row=3, column=0, sticky="w",
                                                   pady=1)
        self.curiosity = tk.StringVar(value=NO_CURIOSITY)
        self.curiosity_box = ttk.Combobox(selgrid, textvariable=self.curiosity,
                                          values=cur_values,
                                          height=len(self.curiosities) + 1,
                                          state="readonly", width=menu_w)
        self.curiosity_box.grid(row=3, column=1, sticky="w", padx=(4, 0),
                                pady=1)

        ttk.Label(selgrid, text="Size:").grid(row=4, column=0, sticky="w",
                                              pady=1)
        # sticky "ew" makes this row span column 1 (the combobox column) so the
        # Reset button, packed to the RIGHT, lines its edge up with the menu
        # comboboxes above; Size stays at the left.
        sizerow = ttk.Frame(selgrid)
        sizerow.grid(row=4, column=1, sticky="ew", padx=(4, 0), pady=1)
        self.font_size = tk.IntVar(value=DEFAULT_FONT_SIZE)
        size_box = ttk.Combobox(sizerow, textvariable=self.font_size,
                                values=[str(s) for s in FONT_SIZES],
                                state="readonly", width=3)
        size_box.pack(side="left")
        size_box.bind("<<ComboboxSelected>>", self._apply_font_size)
        # Reset (not New): Demos / Persona / Curiosity back to (none) and the
        # TOOLS boxes cleared, leaving the prompt and transcript untouched.
        ttk.Button(sizerow, text="Reset", command=self._reset_menus).pack(
            side="right")

        # Readonly comboboxes keep their value text highlighted after a
        # pick until focus moves — clear that selection immediately.
        for box in (self.demos_box, self.model_box, self.persona_box,
                    self.curiosity_box, size_box):
            box.bind("<<ComboboxSelected>>",
                     lambda e: e.widget.selection_clear(), add="+")
        # Long values clip in the narrow menus; a hover panel over an open
        # dropdown shows an item's full text after a short delay.
        self._attach_menu_hover(self.demos_box, textfn=self._demo_prompt)
        self._attach_menu_hover(self.persona_box, textfn=self._persona_text)
        self._attach_menu_hover(self.curiosity_box, textfn=self._curiosity_text)
        self._attach_menu_hover(self.model_box)   # model id is self-explanatory
        # Each selector may carry a BODY tint (its dropdown list) and a TOP
        # tint (a filled "face" over the closed field — aqua won't colour the
        # native field). A TOP constant left commented just means no fill.
        for combo, var, key in (
                (self.demos_box, self.demo_choice, "DEMOS"),
                (self.persona_box, self.persona, "PERSONAS"),
                (self.curiosity_box, self.curiosity, "CURIOSITIES")):
            body_tint = globals().get(f"{key}_TINT_BODY")
            top_tint = globals().get(f"{key}_TINT_TOP")
            if body_tint:
                self._tint_menu(combo, body_tint)
            if top_tint:
                self._add_face(combo, var, top_tint)

        # TOOLS block: the vertical gutter + the bordered checkbox box, its own
        # free-standing frame to the right of the selectors (natural width, so
        # the checkbox text isn't cramped). The three checkboxes ARE Anthropic
        # API server-side *tools* — named in the request's `tools` list.
        toolwrap = ttk.Frame(top)
        checks = ttk.Frame(toolwrap, borderwidth=1, relief="solid",
                           padding=(8, 3))
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
        self.update_idletasks()
        box_h = checks.winfo_reqheight()
        size_px = max(7, box_h // 6)
        tools_font = tkfont.Font(size=-size_px)
        while size_px > 6 and tools_font.metrics("linespace") * 5 > box_h:
            size_px -= 1
            tools_font.configure(size=-size_px)
        ttk.Label(toolwrap, text="T\nO\nO\nL\nS", justify="center",
                  font=tools_font, foreground="#000000").pack(side="left",
                                                              padx=(0, 5))
        checks.pack(side="left")
        # Drop the box down one selector row, so its top lines up with Model
        # rather than Demos (row height = the combobox plus its 1px pads).
        # expand=True gives it the middle cavity between the (left) selectors
        # and the (right) button stack, so it stays centred as the window
        # widens; the selectors hold the left edge.
        row_h = self.demos_box.winfo_reqheight() + 2
        toolwrap.pack(side="left", anchor="n", expand=True, pady=(row_h, 0))

        # Action buttons stacked vertically, pinned to the RIGHT edge so they
        # follow it as the window widens (like Send/Save/Quit below). New on
        # top (green, the primary action), then Help / API / Me.
        bstack = ttk.Frame(top)
        bstack.pack(side="right", anchor="n", padx=(8, 0))
        BTN_W = 6
        help_btn = ttk.Button(bstack, text="Help", width=BTN_W,
                              command=self._show_help)
        self.update_idletasks()
        new_font = tkfont.nametofont("TkDefaultFont").copy()
        new_font.configure(weight="bold")
        RoundButton(bstack, text="New", command=self._reset, font=new_font,
                    width=help_btn.winfo_reqwidth(),
                    height=help_btn.winfo_reqheight(),
                    v_inset=3).pack(pady=1)
        help_btn.pack(pady=1)
        ttk.Button(bstack, text="API", width=BTN_W,
                   command=self._load_api_key).pack(pady=1)
        ttk.Button(bstack, text="Me", width=BTN_W,
                   command=self._load_me).pack(pady=1)

        # Transcript and entry share a vertical PanedWindow: the sash
        # between them can be dragged to grow the input area for long,
        # multi-line prompts. Native (thin) ttk sash, plus a small drag TAB
        # added below (self.grip) so the thin line is easy to catch.
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
        # "Prompt" header sits at the top of this pane, above the input box.
        self._section_label(bottom, "Prompt").pack(side="top", fill="x")
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
        # Entry + an auto-hiding vertical scrollbar, gridded so the scrollbar
        # can be shown/removed in place (grid_remove keeps its cell) without
        # disturbing the entry. The box is a fixed height; a long prompt
        # scrolls here instead of growing the window (see _prompt_scroll_set).
        entry_area = ttk.Frame(bottom)
        entry_area.pack(side="left", fill="both", expand=True)
        entry_area.rowconfigure(0, weight=1)
        entry_area.columnconfigure(0, weight=1)
        self.entry = tk.Text(entry_area, height=PROMPT_MIN_LINES, wrap="word",
                             width=10, padx=8, pady=6)
        self.prompt_scroll = ttk.Scrollbar(entry_area, orient="vertical",
                                           command=self.entry.yview)
        self.entry.configure(yscrollcommand=self._prompt_scroll_set)
        self.entry.grid(row=0, column=0, sticky="nsew")
        self.prompt_scroll.grid(row=0, column=1, sticky="ns")
        self.prompt_scroll.grid_remove()   # hidden until the prompt overflows
        self.entry.bind("<Return>", self._on_return)       # Enter = Send
        self.entry.bind("<KP_Enter>", self._on_return)
        # Shift+Enter inserts a newline. Bind it explicitly (returning None
        # lets the default insertion through) instead of testing event.state,
        # which macOS reports unreliably for a plain Return — that misread was
        # sending Enter to the newline path instead of Send.
        self.entry.bind("<Shift-Return>", lambda e: None)
        self.entry.bind("<Shift-KP_Enter>", lambda e: None)
        # Enter also sends when focus is on a selector or button — e.g. right
        # after picking a Demo, when the cursor never touched the prompt box.
        # Bound window-wide; the prompt's binding above returns "break", so a
        # focused prompt sends once, not twice. (An OPEN dropdown grabs Enter
        # for its own item-select, so this doesn't interfere with picking.)
        self.bind("<Return>", self._on_return)
        self.bind("<KP_Enter>", self._on_return)

        self.status = tk.StringVar(
            value=f"ready — {self._money(0.0)} this session")
        status_bar = ttk.Label(self, textvariable=self.status, anchor="w",
                               relief="sunken", padding=3)

        # Pack order = squeeze priority: the cost meter claims its space
        # FIRST, then the PanedWindow absorbs the rest — transcript above
        # (weight 1: it takes any height surplus), entry row below. The
        # sash between the panes is the drag handle.
        status_bar.pack(fill="x", side="bottom")
        # "Response" header above the transcript (sits in the grey gap under
        # the top bar). Packed before the paned window so it lands above it.
        self._section_label(self, "Response").pack(side="top", fill="x",
                                                   padx=12)
        self.paned.pack(fill="both", expand=True, padx=12, pady=(2, 0))
        self.paned.add(self.view, weight=1)
        self.paned.add(bottom, weight=0)
        # A small raised "drag tab" centred on the sash. The native sash stays
        # its normal thin self; this little square just gives the mouse a
        # bigger target. It floats on top of the sash (placed at its current
        # y) and drags it; it follows the sash when the window or the sash
        # itself moves (via the <Configure> bindings below).
        self.grip = tk.Frame(self.paned, width=24, height=11, bg="#9a9a9a",
                             relief="raised", borderwidth=2,
                             cursor="sb_v_double_arrow")
        self.grip.bind("<B1-Motion>", self._on_grip_drag)
        self.paned.bind("<Configure>", lambda e: self._place_grip())
        self.view.bind("<Configure>", lambda e: self._place_grip())
        self.after(200, self._place_grip)

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

    def _prompt_scroll_set(self, first, last):
        """yscrollcommand for the fixed-height prompt box: drive the scrollbar
        and auto-hide it. When the whole prompt fits, the scrollbar is removed
        and the entry uses the full width; when it overflows, the scrollbar
        reappears in its grid cell so a long prompt scrolls in place instead of
        growing the window. Enlarge the box by dragging the sash above it."""
        if not hasattr(self, "prompt_scroll"):
            return
        self.prompt_scroll.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.prompt_scroll.grid_remove()
        else:
            self.prompt_scroll.grid()

    def _place_grip(self):
        """Keep the drag tab centred on the sash. Called whenever the sash can
        move — window resize, a sash drag, or a tab drag — so the tab follows
        it. Guarded: before the panes are laid out sashpos isn't meaningful."""
        if not hasattr(self, "grip"):
            return
        try:
            y = self.paned.sashpos(0)
        except tk.TclError:
            return
        if y > 0:
            self.grip.place(in_=self.paned, relx=0.5, y=y, anchor="center")

    def _on_grip_drag(self, event):
        """Drag the sash from the tab: move it to the pointer's y within the
        paned window (clamped so neither pane can be dragged shut), then
        reposition the tab onto the sash's new spot."""
        try:
            h = self.paned.winfo_height()
            y = self.grip.winfo_pointery() - self.paned.winfo_rooty()
            self.paned.sashpos(0, max(60, min(h - 60, y)))
        except tk.TclError:
            return
        self._place_grip()

    def _welcome(self):
        """Greet the otherwise-empty Response area at startup with a few ways
        to begin, in the grey-italic 'meta' style (like the other annotations).
        Display only — not logged, and cleared by New like anything else."""
        self._append(
            "1. Press Send to get a fast, cheap response.\n"
            "2. Change menu entries (Model, Persona, Curiosity) then "
            "press Send.\n"
            "3. Select a Demo, press Send.\n",
            "meta")

    def _apply_settings(self):
        """Apply settings.json — pre-loaded preferences at startup."""
        settings, notes = load_settings()
        self._apply_prefs(settings, notes)

    def _show_help(self):
        """Help button: show the same text the CLI's --help prints (usage,
        options, and the project link) as a monospace terminal block, prefixed
        with the command that produces it — reads like a real shell session,
        not italic prose. Logged as code so a saved transcript reproduces it."""
        block = ("$ python3 ./bin/curiosity-lab.py --help\n"
                 + make_parser().format_help().rstrip("\n"))
        self._append("\n", "meta")
        self._append("\n", "md_codeblock")   # blank line inside the gray: top pad
        self._append_code_block(block)
        self._append("\n", "md_codeblock")   # ...and bottom pad, easier to eyeball
        self.log.append({"kind": "code", "lang": "", "text": block})
        # Repeat the project URL below as a real clickable link — taken from
        # the last http(s) field in the help text.
        urls = re.findall(r"https?://\S+", block)
        if urls:
            self._append("\n", "meta")
            self._append_link(urls[-1], "curious")
            self._append("\n", "meta")

    def _on_demo_selected(self, event=None):
        """Demos combobox: the picked tag names a bundle in self.demo_bundles;
        load it. Values are refreshed from demos.json by New, like the other
        selectors (see _reload_choices)."""
        bundle = self.demo_bundles.get(self.demo_choice.get())
        if bundle:
            self._apply_demo(bundle)

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
            tag = match_tag(settings["persona"],
                            list(self.personas) + [NO_PERSONAS])
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
            if re.fullmatch(r"(\d+x\d+)?[+-]\d+[+-]\d+", geo):
                self.geometry(geo)   # size + position, both restored
            else:
                notes.append(f"{source}: unreadable geometry "
                             f"{settings['geometry']!r} — expected "
                             f"'WxH+x+y'")
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
                # Strip inline emphasis inside the heading (e.g. the model
                # writes "## **Title**"): the heading is already styled, so a
                # raw ** / * / ` would otherwise render as literal characters.
                content = INLINE_MD.sub(
                    lambda m: next(g for g in m.groups() if g is not None),
                    heading.group(2))
                self._append(content + nl, f"md_h{level}")
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

    def _reset_menus(self):
        """Reset button: return the selectors to a blank slate without touching
        the conversation. Demos / Persona / Curiosity go to (none) and the
        TOOLS boxes clear; the prompt text, transcript, Model and Size are all
        left as they are (that whole-conversation wipe is what New does)."""
        self.demo_choice.set(NO_DEMO)
        self.persona.set(NO_PERSONAS)
        self.curiosity.set(NO_CURIOSITY)
        self.fetch.set(False)
        self.search.set(False)
        self.sandbox.set(False)
        self.status.set("selectors reset to (none); prompt and chat kept")

    def _reset(self):
        if self.streaming:
            return
        self.history.clear()
        self._web_used_last_turn = False   # New clears the whole conversation
        self.fetch.set(False)              # web access is per-conversation:
        self.search.set(False)             # a clean run starts offline
        self.sandbox.set(False)            # ...and without the sandbox
        self.demo_choice.set(NO_DEMO)      # forget the last preset
        if self.log and self.log[-1]["kind"] != "divider":
            self.log.append({"kind": "divider"})   # Save keeps the whole session
        self.view.configure(state="normal")
        self.view.delete("1.0", "end")
        self.view.configure(state="disabled")
        self._welcome()   # don't leave a blank Response area after New either
        self._reload_choices()
        self.status.set(f"new conversation, personas, curiosities & demos "
                        f"reloaded — session total: {self._money(self.spend)}")

    def _reload_choices(self):
        """Re-read the JSON files (wired to New). A selection whose tag
        survived the edit stays; a vanished tag falls back to the default.
        Parse errors are shown in the transcript."""
        self.personas, err_p = load_choices(PERSONAS_FILE, DEFAULT_PERSONAS)
        self.curiosities, err_c = load_choices(CURIOSITIES_FILE,
                                               DEFAULT_CURIOSITIES)
        demos_now, err_d = load_bundles(DEMOS_FILE, DEFAULT_DEMOS)
        for err in (err_p, err_c, err_d):
            if err:
                self._append(f"[{err}]\n", "note")
        self.demo_bundles = {str(b["tag"]): b for b in demos_now}
        cur_values = [NO_CURIOSITY] + list(self.curiosities)
        self.demos_box["values"] = [NO_DEMO] + list(self.demo_bundles)
        self.demos_box["height"] = len(self.demo_bundles) + 1
        self.persona_box["values"] = [NO_PERSONAS] + list(self.personas)
        self.persona_box["height"] = len(self.personas) + 1
        if (self.persona.get() != NO_PERSONAS
                and self.persona.get() not in self.personas):
            self.persona.set(NO_PERSONAS)
        self.curiosity_box["values"] = cur_values
        self.curiosity_box["height"] = len(self.curiosities) + 1
        if (self.curiosity.get() != NO_CURIOSITY
                and self.curiosity.get() not in self.curiosities):
            self.curiosity.set(NO_CURIOSITY)
        # Keep the four menus the same (capped) width after a reload, too.
        menu_w = min(18, max(self._fit_width(list(PRICING)),
                             self._fit_width(self.personas),
                             self._fit_width(cur_values),
                             self._fit_width(list(self.demo_bundles)
                                             + [NO_DEMO])))
        for box in (self.demos_box, self.model_box, self.persona_box,
                    self.curiosity_box):
            box["width"] = menu_w

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
        if persona == NO_PERSONAS:
            self._append("  [Persona “(none)” — no persona in the system "
                         "prompt]\n", "meta")
        else:
            self._append(f"  [Persona “{persona}”: {self.personas[persona]}]"
                         "\n", "meta")
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
        persona = self.persona.get()
        system = "" if persona == NO_PERSONAS else self.personas.get(persona, "")
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

    def _on_return(self, event=None):
        """Enter sends. Shift+Enter is bound separately to a newline, so there's
        no fragile event.state check here."""
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
        if not getattr(self.client, "api_key", None):
            self._append(
                "\n[No API key set. Click API to load a key file, put "
                "apikey.txt beside the app, or export ANTHROPIC_API_KEY "
                "before launching — then Send again.]\n", "note")
            self.status.set("no API key — see transcript")
            return   # prompt is kept in the entry box

        # The prompt stays in the box while the reply streams (and after), so
        # there's something to read besides a blank field and re-asking with
        # tweaked knobs is one click. It also appears in the transcript's You:
        # block. Clear it yourself, or press New, to start a fresh question.
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
        # Heads-up: the prompt asks to run code but the Sandbox tool is off, so
        # nothing will actually execute — the model will imagine the output.
        # This is the "imagined vs actual execution" lesson, surfaced before the
        # reply rather than left for the user to catch. Keyword-based; tune
        # EXEC_INTENT_RE.
        warned = not self.sandbox.get() and bool(EXEC_INTENT_RE.search(text))
        if warned:
            # Print the note, then hold 3s BEFORE anything else prints, so it
            # sits alone at the bottom and is read before the You: block and
            # streaming reply scroll it up. streaming=True blocks a second Send
            # during the pause; the rest of the turn runs in _proceed_turn.
            warn = ("Sandbox is off — the model can't run code, so any "
                    "“output” is imagined by the LLM.")
            self._append(f"\n[{warn}]\n", "note")
            self.log.append({"kind": "note", "text": warn})
            self.streaming = True
            self.status.set("Sandbox off — hold on, read the note… (3s)")
            self.after(3000,
                       lambda: self._proceed_turn(text, suffix, persona_hint))
        else:
            self._proceed_turn(text, suffix, persona_hint)

    def _proceed_turn(self, text, suffix, persona_hint):
        """The rest of a turn, after any sandbox-warning pause: assemble the
        tools, record and show the user turn, and start streaming. The knobs
        are read live, so ticking a tool box during the pause takes effect."""
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
        self._begin_turn(self.model.get(), self._current_system(),
                         list(self.history), tools)

    def _begin_turn(self, model, system, messages, tools):
        """Start the streaming turn: flip Send→Stop, spin the meter, and launch
        the worker thread. Split out of _send so the sandbox warning can pause
        before it (see _send)."""
        self.streaming = True
        self._cancel = False
        self._gen += 1                       # this turn's id; older output is dropped
        self._sent_model = model
        self.send_btn.set_mode("Stop", "#e0a84e", "#c8923a", self._stop)
        self._spinning = True
        self._turn_start = time.monotonic()
        self._spin()
        threading.Thread(
            target=self._worker,
            args=(self._gen, self._sent_model, system, messages, tools),
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
            # Any files the sandbox wrote are downloaded HERE, on the worker
            # thread — never in _note_code_activity, which runs on the UI
            # thread and would freeze the window mid-download. The rendering
            # step later touches only bytes already in hand.
            files = self._download_output_files(final)
            # Carry the send-time model with the result: the combobox may
            # have been switched mid-stream, and the bill belongs to the
            # model that actually served the request.
            self.q.put((gen, "done", (model, final, files)))
        except Exception as exc:   # noqa: BLE001 — surface everything to the user
            self.q.put((gen, "error", f"{type(exc).__name__}: {exc}"))
        finally:
            if self._active_stream is stream:   # don't clobber a newer turn
                self._active_stream = None

    def _download_output_files(self, msg):
        """Worker-thread only. Scan the final message's code-execution result
        blocks for output files (each carries a `file_id`) and download the
        bytes via the beta Files API, so the UI thread later renders from
        bytes already fetched — no network call while the window is live.

        Returns {file_id: {name, mime, size, data}} on success, or
        {file_id: {error}} for a file that couldn't be retrieved; either way
        the turn survives. An empty dict when the sandbox wrote nothing (or
        wasn't used) costs a cheap walk of msg.content and no API calls."""
        files: dict = {}
        client = self.client
        if client is None:
            return files
        for b in getattr(msg, "content", None) or []:
            if getattr(b, "type", "") != "bash_code_execution_tool_result":
                continue
            result = getattr(b, "content", None)      # may be an error block
            for out in getattr(result, "content", None) or []:
                fid = getattr(out, "file_id", None)
                if not fid or fid in files:
                    continue
                try:
                    meta = client.beta.files.retrieve_metadata(
                        fid, betas=[FILES_BETA])
                    raw = client.beta.files.download(
                        fid, betas=[FILES_BETA]).read()
                    files[fid] = {"name": getattr(meta, "filename", "output"),
                                  "mime": getattr(meta, "mime_type", "") or "",
                                  "size": getattr(meta, "size_bytes", None)
                                  or len(raw),
                                  "data": raw}
                except Exception as exc:   # noqa: BLE001 — surface, don't crash
                    files[fid] = {"error": f"{type(exc).__name__}: {exc}"}
        return files

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
                                    f"session total: {self._money(self.spend)}")
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
                 f"Tokens: In {self._thousands(in_tok)} "
                 f"({self._money(in_cost)}) / Out {self._thousands(out_tok)} "
                 f"({self._money(out_cost)})"]
        if searches:
            parts.append(f"Searches: {searches} ({self._money(search_cost)})")
        if fetches:
            parts.append(f"Fetches: {fetches} (tokens only)")
        if runs:
            parts.append(f"Code runs: {runs} (free tier / $0.05/hr)")
        self.status.set("    |    ".join(parts)
                        + f"    |    Session total: {self._money(self.spend)}"
                        + " (est)")

    def _on_done(self, model, msg, files=None):
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
            self._note_web_activity(msg, files)
            searches, fetches = self._web_usage(msg)
            self._web_used_last_turn = bool(searches or fetches)
        self._add_cost(model, msg)
        self._append("\n", "assistant")
        self._finish()

    def _note_web_activity(self, msg, files=None):
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
        self._note_code_activity(msg, files)

    def _note_code_activity(self, msg, files=None):
        """Post-hoc notes for the Linux sandbox, read from the final message.
        This is the 'it really executed' evidence — the live stream only
        pauses. Two sub-tools surface here: bash (commands + stdout/stderr,
        non-zero exit in red) and the file editor (create/edit — the actual
        file the model WROTE, e.g. the script itself, shown regardless of
        whether the model bothered to narrate it).

        v2.9.0: a bash result also carries any OUTPUT files the run produced
        (a matplotlib PNG, a CSV) as output blocks with a `file_id`. `files`
        is the {file_id: info} map the worker already downloaded; images
        render inline, other files get a Save-me note."""
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
                # Output files the run wrote (the v2.9.0 payoff): images
                # render inline, everything else gets a Save-me note.
                for out in getattr(c, "content", None) or []:
                    fid = getattr(out, "file_id", None)
                    if fid:
                        self._emit_output_file(fid, (files or {}).get(fid))

    def _emit_output_file(self, fid, info):
        """Render or note one sandbox output file from bytes the worker already
        downloaded. `info` is {name, mime, size, data}, or {error}, or None
        when the file wasn't retrieved. Images show inline; other files (and
        undecodable images) get a bracketed Save-me note. An `image`/`file`
        log entry carries the bytes so Save can write them as a sidecar."""
        if not info:
            self._append(f"\n  [sandbox wrote a file (id {fid}) — "
                         "not retrieved]\n", "note")
            return
        if info.get("error"):
            self._append(f"\n  [output file download failed: "
                         f"{info['error']}]\n", "note")
            return
        name = info.get("name") or "output"
        mime = info.get("mime") or ""
        raw = info.get("data") or b""
        size = info.get("size") or len(raw)
        if mime.startswith("image/"):
            photo, dims = self._make_photo(raw, mime)
            if photo is not None:
                self._append(f"\n  [rendered {name} "
                             f"({dims[0]}×{dims[1]})]\n", "curious")
                self.view.configure(state="normal")
                self.view.image_create("end", image=photo)
                self.view.insert("end", "\n")
                self.view.see("end")
                self.view.configure(state="disabled")
                self._images.append(photo)   # keep a ref or Tk GCs the picture
                self.log.append({"kind": "image", "name": name,
                                 "mime": mime, "data": raw})
                return
            # Undecodable here (e.g. JPEG/WebP with no Pillow) — fall through.
        self._append(f"\n  [wrote {name} ({self._thousands(size)} bytes) "
                     "— Save to keep]\n", "curious")
        self.log.append({"kind": "file", "name": name, "mime": mime,
                         "data": raw})

    def _make_photo(self, raw, mime):
        """Return (PhotoImage, (orig_w, orig_h)) scaled to <= IMAGE_MAX_WIDTH,
        or (None, None) if the bytes can't be decoded here. Pillow (optional)
        gives smooth downscaling and JPEG/WebP; without it, native Tk handles
        PNG/GIF with integer subsampling only. Reported dims are the ORIGINAL
        size, so the note reflects what the sandbox produced, not the thumbnail."""
        cap = IMAGE_MAX_WIDTH
        if Image is not None and ImageTk is not None:
            try:
                im = Image.open(io.BytesIO(raw))
                im.load()
                ow, oh = im.size
                if ow > cap:
                    try:
                        resample = Image.Resampling.LANCZOS
                    except AttributeError:       # Pillow < 9.1
                        resample = Image.LANCZOS
                    im = im.resize((cap, max(1, round(oh * cap / ow))),
                                   resample)
                return ImageTk.PhotoImage(im), (ow, oh)
            except Exception:   # noqa: BLE001 — fall back to native Tk
                pass
        try:
            img = tk.PhotoImage(data=raw)   # Tk 8.6 decodes PNG/GIF from bytes
        except tk.TclError:
            return None, None
        ow, oh = img.width(), img.height()
        if ow > cap:
            img = img.subsample((ow // cap) + 1)   # integer-only, but crisp
        return img, (ow, oh)

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
                 f"Tokens: In {self._thousands(u.input_tokens)} "
                 f"({self._money(in_cost)}) / Out "
                 f"{self._thousands(u.output_tokens)} "
                 f"({self._money(out_cost)})"]
        if searches:
            parts.append(f"Searches: {searches} ({self._money(search_cost)})")
        if fetches:
            parts.append(f"Fetches: {fetches} (tokens only)")
        runs = self._code_runs(msg)
        if runs:
            # Container-time isn't in `usage`; count only, not a dollar
            # figure — the one lab mechanism the meter cannot price.
            parts.append(f"Code runs: {runs} (free tier / $0.05/hr)")
        self.status.set("    |    ".join(parts)
                        + f"    |    Session total: {self._money(self.spend)}")

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
        # The prompt was never cleared on Send, so it's still in the box —
        # nothing to restore here.

    # ---- quit / window position -------------------------------------------
    def _quit(self):
        self._save_geometry()
        self.destroy()

    def _save_geometry(self):
        """Remember the window SIZE and POSITION in settings.json — written on
        Quit or window close, applied at next startup. The published file
        ships without a Geometry item; it is seeded here on first save. A
        missing settings file is created; a broken one is left alone."""
        geo = self.geometry()   # "WxH+x+y" — size and position
        if not re.fullmatch(r"\d+x\d+[+-]\d+[+-]\d+", geo):
            return
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
            # Images/files ride out as sidecars next to the .md (a data-URI
            # would bloat the file); each gets a relative filename the
            # Markdown then links to. Written first so the links resolve.
            assets = self._write_assets(path)
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._session_markdown())
            # Confirm in the transcript, not the status bar — the meter's
            # last cost reading stays put where you can still read it.
            extra = (f"  (+{assets} sidecar file{'s' if assets != 1 else ''}; "
                     "images also embedded inline)" if assets else "")
            self._append(f"\n[session saved — {path}{extra}]\n", "note")
        except OSError as exc:
            self._append(f"\n[save failed: {exc}]\n", "note")

    @staticmethod
    def _mime_ext(mime):
        """A file extension for a sidecar whose logged name lacks one."""
        return {"image/png": ".png", "image/gif": ".gif",
                "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime, "")

    def _write_assets(self, md_path):
        """Write every logged image/file as a sidecar beside the .md and stamp
        each entry with the relative filename _session_markdown links to.
        Returns the count written. Names are '<md-stem>.assetN.<ext>', unique
        and free of the spaces a model's own filename might carry."""
        stem = os.path.splitext(os.path.basename(md_path))[0]
        outdir = os.path.dirname(md_path) or "."
        seq = written = 0
        for entry in self.log:
            if entry.get("kind") not in ("image", "file"):
                continue
            data = entry.get("data")
            if not data:
                entry.pop("savename", None)
                continue
            seq += 1
            ext = (os.path.splitext(entry.get("name") or "")[1]
                   or self._mime_ext(entry.get("mime", "")))
            fname = f"{stem}.asset{seq}{ext}"
            try:
                with open(os.path.join(outdir, fname), "wb") as f:
                    f.write(data)
                entry["savename"] = fname
                written += 1
            except OSError:
                entry.pop("savename", None)
        return written

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
            elif kind == "image":
                alt = entry.get("name") or "image"
                data = entry.get("data")
                if data:
                    # Embed inline as a base64 data-URI so the .md shows the
                    # image with nothing else needed (opens self-contained in
                    # Typora / VS Code / a browser). The sidecar written by
                    # _write_assets is the standalone copy, linked below.
                    mime = entry.get("mime") or "image/png"
                    b64 = base64.b64encode(data).decode("ascii")
                    lines += [f"![{alt}](data:{mime};base64,{b64})", ""]
                    if entry.get("savename"):
                        lines += [f"*(also saved beside this file as "
                                  f"[{entry['savename']}]({entry['savename']}))*",
                                  ""]
                elif entry.get("savename"):
                    lines += [f"![{alt}]({entry['savename']})", ""]
                else:
                    lines += [f"> [image: {alt} — not saved]", ""]
            elif kind == "file":
                name = entry.get("name") or "file"
                if entry.get("savename"):
                    lines += [f"> [file: [{name}]({entry['savename']})]", ""]
                else:
                    lines += [f"> [file: {name} — not saved]", ""]
            elif kind == "divider":
                lines += ["*(new conversation)*", ""]
        return "\n".join(lines)


def make_parser():
    """The CLI parser, shared by `--help` on the command line and the Help
    button in the window, so both show the exact same text."""
    import argparse

    class _HelpFormat(argparse.RawDescriptionHelpFormatter):
        """Titled help: a version banner on top, capitalized section headers
        (Usage / Options), the hand-wrapped description, trailing blank line."""
        def format_help(self):
            text = super().format_help()
            text = text.replace("usage:", "Usage:", 1)
            text = text.replace("\noptions:", "\nOptions:", 1)
            return f"Curiosity Lab for Claude v{__version__}\n\n" + text + "\n"

    parser = argparse.ArgumentParser(
        prog="curiosity-lab",
        formatter_class=_HelpFormat,
        description="Curiosity Lab for Claude — a small chat window where "
                    "you pick a persona,\nadd curiosity to your questions, "
                    "and watch what every answer costs.",
        epilog=f"More info here:\n  {GITHUB_URL}")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="keep stderr visible (don't suppress Tk "
                             "file-dialog diagnostics)")
    parser.add_argument("-V", "--version", action="version",
                        version=f"Curiosity Lab for Claude v{__version__}")
    return parser


if __name__ == "__main__":
    args = make_parser().parse_args()
    Workbench(verbose=args.verbose).mainloop()
