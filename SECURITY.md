# Curiosity Lab for Claude:<br/>SAST Scan and Security Assessment

**Author:** JohnB, with AI pair-programming support by Anthropic Claude<br/>
**Date:** 2026-07-16<br/>
**Target version:** `bin/curiosity-lab.py` v3.0.0 (2,901 lines; 2,392 LOC scanned)<br/>
**Methodology:** static analysis with Bandit and Semgrep, plus a pip-audit dependency<br/>
&nbsp; &nbsp; scan, triaged with the sibling Web-Print framework — accept-by-design / false-positive / remediate / accept-risk.

## 1. Result

**PASS. No medium- or high-severity findings.**

- **Bandit:**<br/>
5 low-severity findings, all one rule (`B110`, try/except/pass) and all intentional<br/>
&nbsp; &nbsp; graceful-degradation blocks — triaged **accept-by-design** in §4.

- **Semgrep** (`p/python`, `p/security-audit`, `p/command-injection`):<br/>
no findings across 200 rules.

- **pip-audit:**<br/>
no known vulnerabilities in the declared dependencies.

- **The v3.0.0 change introduces no scanner finding.**<br/>
A second vendor (Melious) behind a vendor abstraction: base-URL switching, per-vendor<br/>
&nbsp; &nbsp; API keys, a merged model catalog, per-vendor demos, a `count_tokens` input-cost<br/>
&nbsp; &nbsp; recovery, and the top-bar / Eco-row rework. Its one materially new security property<br/>
&nbsp; &nbsp; is a second egress destination (`api.melious.ai`), disclosed in §5.11.

## 2. Trust model (the frame for every verdict)

**Curiosity Lab is a single-user, local, desktop GUI tool.**<br/>
The user runs it on their own machine, with their own privileges, with their own API<br/>
&nbsp; &nbsp; key, against prompts they typed and files they chose.

There is no server, no multi-tenant surface, and no authentication to bypass.<br/>
The user is the trust boundary.

Three places cross that boundary, and as of v3.0.0 the outbound destination is **vendor-selected**, no longer Anthropic-only:

- **Inbound:**<br/>
the model's responses, which the tool renders into the transcript (§5.1), and — since<br/>
&nbsp; &nbsp; v2.9.0 — any image files the sandbox produced, downloaded and rendered (§5.6).

- **Outbound:**<br/>
everything the user configured — persona text, Me-file contents, and the full chat<br/>
&nbsp; &nbsp; history — is sent on every call (§5.2), plus any model-composed web-search query<br/>
&nbsp; &nbsp; when web access is on (§5.4). It goes to the **active vendor's endpoint**:<br/>
&nbsp; &nbsp; `api.anthropic.com` for Claude (the default), or `api.melious.ai` (an EU,<br/>
&nbsp; &nbsp; Anthropic-compatible router) when Melious is selected (§5.11).

- **Remote execution, not local:**<br/>
the sandbox runs code on the server, never on the user's machine (§5.5).<br/>
Nothing the model writes executes locally.

The vendor and its endpoint are the user's explicit choice in the Vendor selector; the<br/>
&nbsp; &nbsp; active endpoint is **named in the transcript** on every switch (fail-loud, not silent);<br/>
&nbsp; &nbsp; and per-vendor keys are isolated, so no vendor ever borrows another's key (§5.11).

## 3. Scope

### In scope

| Component | File | Tool |
|---|---|---|
| Main tool | `bin/curiosity-lab.py` (2,901 lines) | Bandit, Semgrep |
| Dependencies | `bin/requirements.txt` (`anthropic`, `pillow`) | pip-audit |

### Out of scope

| Item | Reason |
|---|---|
| `personas.json`<br/>`curiosities.json`<br/>`demos.json` · `demos-melious.json`<br/>`settings.json`<br/>`models.json` · `models-melious.json`<br/>`vendors.json`<br/>`me.template.md` | Data files. Parsed with `json.load` / read as text, never executed. A malformed file falls back to a built-in default without being overwritten. See §5.8 (catalogs) and §5.11 (`vendors.json` carries the per-vendor `base_url` — egress-controlling data, still user-owned local config). |
| Saved chat exports (`*.chat.md`) and their image sidecars | Output artifacts, not tool code. |

### Copy integrity check

The published `bin/curiosity-lab.py` should be compared byte-for-byte against the<br/>
&nbsp; &nbsp; development-tree copy before release: when identical, scanning one covers both.<br/>
&nbsp; &nbsp; This scan targets the v3.0.0 development-tree copy (2,901 lines), pending that step.

## 4. Findings

Toolchain: Bandit 1.9.4, Semgrep 1.161.0 (200 community rules), pip-audit 2.10.0; Python 3.13, on macOS.

```
$ bandit -f txt bin/curiosity-lab.py
$ semgrep --config p/python --config p/security-audit --config p/command-injection bin/curiosity-lab.py
```

**Semgrep: none.**<br/>
Zero findings across 200 rules; no suppressions (`#nosec`, `#nosemgrep`) exist anywhere.

**Bandit: five low-severity `B110` (try/except/pass), high confidence — all accept-by-design.**<br/>
Each is a deliberate "degrade, don't crash" block in a desktop GUI; none touches auth,<br/>
&nbsp; &nbsp; file paths, credentials, or execution. Two carry an explicit `# noqa: BLE001`.

| Line | What the block guards | Verdict |
|---|---|---|
| 613 | Menu-hover text function fails → show the raw tag instead. Cosmetic. | Accept — by design |
| 757 | Attaching the hover panel to a combobox popdown fails on some Tk builds → skip it. | Accept — by design |
| 1110 | Re-tinting/rebinding a dropdown list on `<Map>` fails on some Tk/aqua builds → skip it. | Accept — by design |
| 2208 | Best-effort close of an already-dead SDK stream on **Stop** → ignore the throw. | Accept — by design |
| 2464 | Pillow resize path fails → fall back to the native Tk image decoder. Robustness. | Accept — by design |

The tool's shape otherwise remains conservative: no `subprocess`, no `eval`/`exec`, no<br/>
&nbsp; &nbsp; shell, no `urllib`/`requests`, no temp-file creation, no hardcoded secrets.<br/>
All network traffic goes through the official `anthropic` SDK (TLS handled there) for<br/>
&nbsp; &nbsp; both vendors — Melious speaks the Anthropic Messages API and differs only by<br/>
&nbsp; &nbsp; `base_url`. The code-execution sandbox runs on the server, so no local command ever runs (§5.5).

## 5. Proactive disclosure (items SAST cannot flag)

1. **Model output is rendered inert.**<br/>
Replies stream into a read-only Tk text widget and are re-rendered with a small Markdown<br/>
&nbsp; &nbsp; subset mapped onto Tk *text tags* — styling only. There is no HTML engine, no<br/>
&nbsp; &nbsp; JavaScript, and no code path from model output to execution of any kind. The Markdown<br/>
&nbsp; &nbsp; regexes run per line with lazy quantifiers; no catastrophic-backtracking is present.

2. **Data egress is the product, and it is disclosed.**<br/>
The persona text, the loaded Me-file, and the full conversation history are sent to the<br/>
&nbsp; &nbsp; active vendor's endpoint on every call — that is how the API works, and making the<br/>
&nbsp; &nbsp; injections visible (transcript annotations, token estimates) is the teaching purpose.<br/>
&nbsp; &nbsp; The README tells users to load only Me-file content they are comfortable sending.<br/>
&nbsp; &nbsp; Nothing is sent anywhere the active vendor's endpoint isn't.

3. **The API key is handled minimally, and per vendor.**<br/>
Each vendor's key is resolved independently — from that vendor's key file<br/>
&nbsp; &nbsp; (`apikey-claude.txt`, `apikey-melious.txt`), then its env var, then a generic<br/>
&nbsp; &nbsp; `apikey.txt` fallback — and passed explicitly to that vendor's SDK client. Passing it<br/>
&nbsp; &nbsp; explicitly (even when empty) stops the SDK from silently auto-reading<br/>
&nbsp; &nbsp; `ANTHROPIC_API_KEY` for a non-Claude vendor, so **Melious never borrows Claude's key**<br/>
&nbsp; &nbsp; and vice-versa. A key is never logged, exported, or written to any file, and never<br/>
&nbsp; &nbsp; shown beyond its last four characters. Key files are `.gitignore`d (`apikey.txt`,<br/>
&nbsp; &nbsp; `apikey-*.txt`, `*.key`). The README documents per-window key scoping.

4. **Web access (v2.6.0) is model-driven and proxied.**<br/>
With the Web-search / Web-fetch boxes on, the tool declares Anthropic's server-side<br/>
&nbsp; &nbsp; `web_search` / `web_fetch` tools per request. Searches leave the server carrying only<br/>
&nbsp; &nbsp; the model-composed query (no identity, IP, or location); fetches retrieve pages<br/>
&nbsp; &nbsp; through the server proxy, not from the user's machine. Every search and fetch is<br/>
&nbsp; &nbsp; annotated in the transcript, and retrieved text is rendered inert like any reply<br/>
&nbsp; &nbsp; (§5.1). These built-in tools are **Claude-only**: the boxes are disabled on Melious,<br/>
&nbsp; &nbsp; which rejects the Anthropic built-in tool types.

5. **Sandbox code execution (v2.7.0) runs remotely, not locally.**<br/>
With the Linux-sandbox box on, the tool declares Anthropic's server-side<br/>
&nbsp; &nbsp; `code_execution` tool. Code the model writes runs in a server-hosted container with no<br/>
&nbsp; &nbsp; internet access — **nothing executes on the user's machine**, and there is no<br/>
&nbsp; &nbsp; client-side bash/exec tool in the code. The tool warns the user, at Send, when a<br/>
&nbsp; &nbsp; prompt asks for execution while the box is off (so a model that fabricates output can<br/>
&nbsp; &nbsp; be caught). Also Claude-only, disabled on Melious (§5.4).

6. **Sandbox image output (v2.9.0) is handled defensively.**<br/>
When the sandbox writes a file, the tool downloads the bytes via the beta Files API<br/>
&nbsp; &nbsp; (`files-api-2025-04-14`) on the worker thread and renders images inline — three safeguards then guard the untrusted bytes:

&nbsp; &nbsp; **(a)** they are decoded inside a guarded block — junk or a failed decode yields no<br/>
&nbsp; &nbsp; image, never a crash (the Pillow path falls back to the native Tk decoder, line 2464);<br/>
&nbsp; &nbsp; decoder-library CVEs are the one residual, out of the app's hands.

&nbsp; &nbsp; **(b)** sidecar filenames written on **Save** are fully synthesised by the app —<br/>
&nbsp; &nbsp; `"<md-stem>.assetN.<ext>"`, where the stem is the user's own chosen `.md` name and the<br/>
&nbsp; &nbsp; extension comes via `os.path.splitext` (which cannot contain a path separator) or a<br/>
&nbsp; &nbsp; MIME fallback. A model-chosen `filename` contributes only its extension, so it cannot traverse out of the save directory.

&nbsp; &nbsp; **(c)** images embedded in the exported Markdown are base64 `data:` URIs — inert text.

7. **`settings.json` write-back is bounded.**<br/>
On Quit the tool rewrites `settings.json` to remember the window position (the size is<br/>
&nbsp; &nbsp; recomputed at startup to fit the content). Guardrails: an unparseable file is never<br/>
&nbsp; &nbsp; overwritten (left in place for the user to fix), a missing file is created, and only the geometry entry is changed.

8. **The model catalogs (`models.json`, `models-melious.json`) are data, not code.**<br/>
The model menu and its prices live in editable JSON, parsed with `json.load`; the two are<br/>
&nbsp; &nbsp; merged into a per-vendor catalog. Each entry is read for its tag, model ID, prices,<br/>
&nbsp; &nbsp; brand, and note; nothing from the file is executed or used to build a path. A<br/>
&nbsp; &nbsp; malformed file falls back to the built-in defaults without being overwritten, and the<br/>
&nbsp; &nbsp; model ID is passed to the SDK as an opaque string.

9. **stderr is briefly redirected around native file dialogs.**<br/>
macOS AppKit prints a harmless one-time diagnostic when the save/open panel class first<br/>
&nbsp; &nbsp; loads; the tool silences stderr for the duration of the dialog (restored in a<br/>
&nbsp; &nbsp; `finally`). The `-v | --verbose` flag disables the suppression entirely — nothing is ever hidden from a user who wants it.

10. **All file reads are user-driven.**<br/>
Me-files and JSON configs are opened from paths the user picked in a dialog or wrote in<br/>
&nbsp; &nbsp; `settings.json` — no path is ever constructed from model output.

11. **Multi-vendor egress (v3.0.0) is explicit, isolated, and named.**<br/>
Selecting Melious rebuilds the SDK client with that vendor's `base_url`<br/>
&nbsp; &nbsp; (`https://api.melious.ai`) and its own key, so the same prompt / history that would<br/>
&nbsp; &nbsp; go to Anthropic instead goes to Melious — the user's deliberate choice, surfaced in<br/>
&nbsp; &nbsp; the Vendor selector and printed in the transcript on every switch:<br/>
&nbsp; &nbsp; `Vendor → Melious (€); endpoint https://api.melious.ai; key loaded`<br/>
&nbsp; &nbsp; The `base_url` is sourced from `vendors.json` or the built-in default, never from<br/>
&nbsp; &nbsp; model output or prompt input, so a reply cannot redirect egress. `vendors.json` is<br/>
&nbsp; &nbsp; egress-controlling local config, owned by the user like every other config file (§3).<br/>
&nbsp; &nbsp; Keys are per-vendor and non-transferable (§5.3). No telemetry, no third destination —<br/>
&nbsp; &nbsp; traffic goes to exactly the active vendor's endpoint, and nowhere else.

## 6. Dependencies

**pip-audit — PASS.**

```
$ pip-audit -r bin/requirements.txt
No known vulnerabilities found
```

The declared dependencies are the official `anthropic` SDK and **Pillow** (image decoding<br/>
&nbsp; &nbsp; and scaling for inline thumbnails), plus Tkinter from the Python standard library.<br/>
&nbsp; &nbsp; Both are pip-audited above. Pillow is a default dependency, not a runtime requirement:<br/>
&nbsp; &nbsp; it's installed so every user renders identically, but soft-imported, so if absent the<br/>
&nbsp; &nbsp; app still runs on a native-Tk fallback and says so. The Melious vendor adds **no** new<br/>
&nbsp; &nbsp; dependency — it reuses the `anthropic` SDK with a different `base_url`.

## 7. Recommended actions

No defect fixes required, and no outstanding hardening actions.

## 8. Triage framework

Verdicts available: **Accept — by design**, **False positive**, **Remediate**, **Accept risk**.<br/>
The five Bandit `B110` findings are all triaged **Accept — by design** (§4): each is a<br/>
&nbsp; &nbsp; deliberate graceful-degradation block, none reaching security-relevant state.<br/>
Semgrep and pip-audit produced nothing to triage.
