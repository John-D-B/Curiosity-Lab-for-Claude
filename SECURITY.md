# Curiosity Lab for Claude:<br/>SAST Scan and Security Assessment

**Author:** JohnB, with AI pair-programming support by Anthropic Claude<br/>
**Date:** 2026-07-14<br/>
**Target version:** `bin/curiosity-lab.py` v2.10.0 (2,437 lines)<br/>
**Methodology:** static analysis with Bandit and Semgrep, dependency audit with pip-audit,<br/>
&nbsp; &nbsp; and the same triage framework as the sibling Web-Print assessment<br/>
&nbsp; &nbsp; (accept-by-design / false-positive / remediate / accept-risk).

## 1. Result

**PASS. No medium- or high-severity findings.**

- Bandit: **5 low-severity findings**, all one rule (`B110`, try/except/pass) and all<br/>
&nbsp; &nbsp; intentional graceful-degradation blocks — triaged **accept-by-design** in §4.
- Semgrep (`p/python`, `p/security-audit`, `p/command-injection`): **no findings** (200 rules).
- pip-audit: **no known vulnerabilities** in dependencies.
- The five features added since the v2.4.0 scan — web access, sandbox code execution,<br/>
&nbsp; &nbsp; image output, and the `models.json` price catalog — introduce no scanner finding;<br/>
&nbsp; &nbsp; their security-relevant design points are disclosed in §5.

## 2. Trust model (the frame for every verdict)

**Curiosity Lab** is a **single-user, local, desktop GUI tool.**<br/>
The user runs it on their own machine, with their own privileges,<br/>
&nbsp; &nbsp; with their own API key, against prompts **they** typed and files **they** chose.

There is no server, no multi-tenant surface, and no authentication to bypass.<br/>
The user is the trust boundary.

Three places cross that boundary — and all three cross it to `api.anthropic.com`, nowhere else:

- **Inbound:** the model's responses, which the tool renders into the transcript (§5.1),<br/>
&nbsp; &nbsp; and — since v2.9.0 — any image files the sandbox produced, downloaded and rendered (§5.6).
- **Outbound:** everything the user configured — persona text, Me-file contents,<br/>
&nbsp; &nbsp; and the full chat history — sent on every call (§5.2), plus any model-composed<br/>
&nbsp; &nbsp; web-search query when the user enables web access (§5.4).
- **Remote execution, not local:** the sandbox runs code on Anthropic's servers, never on<br/>
&nbsp; &nbsp; the user's machine (§5.5). Nothing the model writes executes locally.

## 3. Scope

### In scope

| Component | File | Tool |
|---|---|---|
| Main tool | `bin/curiosity-lab.py` (2,437 lines) | Bandit, Semgrep |
| Dependencies | `bin/requirements.txt` (`anthropic`, `pillow`) | pip-audit |

### Out of scope

| Item | Reason |
|---|---|
| `personas.json`<br/>`curiosities.json`<br/>`demos.json`<br/>`settings.json`<br/>`models.json`<br/>`me.template.md` | Data files.<br/>Parsed with `json.load` / read as text, never executed. |
| Saved chat exports (`*.chat.md`) and their image sidecars | Output artifacts, not tool code. |

### Copy integrity check

The published `bin/curiosity-lab.py` was compared byte-for-byte against the<br/>
&nbsp; &nbsp; development tree's copy: **identical**. Scanning one covers both.

## 4. Findings

Toolchain: Bandit 1.9.4, Semgrep 1.161.0 (three community rulesets, 200 rules), pip-audit 2.10.0, Python 3.13, on macOS.

```bash
$ bandit -f txt bin/curiosity-lab.py
$ semgrep --config p/python --config p/security-audit --config p/command-injection bin/curiosity-lab.py
```

**Semgrep: none.** Zero findings across 200 rules; no suppressions (`#nosec`, `#nosemgrep`) exist anywhere in the file.

**Bandit: five low-severity `B110` (try/except/pass), high confidence — all accept-by-design.**<br/>
Each is a deliberate "degrade, don't crash" block in a desktop GUI; none touches auth,
file paths, credentials, or execution. Three already carry an explicit `# noqa: BLE001`.

| Line | What the block guards | Verdict |
|---|---|---|
| 507 | Menu-hover text function fails → show the raw tag instead. Cosmetic. | Accept — by design |
| 636 | Attaching the hover panel to a combobox popdown fails on some Tk builds → skip it. Cosmetic. | Accept — by design |
| 764 | Tinting a dropdown list background fails on some Tk/aqua builds → skip the tint. Cosmetic. | Accept — by design |
| 1771 | Best-effort close of an already-dead SDK stream on **Stop** → ignore the throw. | Accept — by design |
| 2026 | Pillow resize path fails → fall back to the native Tk image decoder. Robustness. | Accept — by design |

The tool's shape otherwise remains conservative: no `subprocess`, no `eval`/`exec`,<br/>
&nbsp; &nbsp; no shell, no `urllib`, no temp-file creation, no hardcoded secrets.<br/>
All network traffic goes through the official `anthropic` SDK (TLS handled there),<br/>
&nbsp; &nbsp; and the code-execution sandbox runs **on Anthropic's servers** — there is no<br/>
&nbsp; &nbsp; client-side execution tool, so no local command ever runs (§5.5).

## 5. Proactive disclosure (items SAST cannot flag)

1. **Model output is rendered inert.**<br/>
Replies stream into a read-only Tk text widget and are re-rendered with a small<br/>
&nbsp; &nbsp; Markdown subset mapped onto Tk *text tags* — styling only.<br/>
There is no HTML engine, no JavaScript, and no code path from model output to<br/>
&nbsp; &nbsp; execution of any kind. The Markdown regexes operate per line with<br/>
&nbsp; &nbsp; lazy quantifiers; no catastrophic-backtracking construction is present.

2. **Data egress is the product, and it is disclosed.**<br/>
The persona text, the loaded Me-file, and the full conversation history are sent<br/>
&nbsp; &nbsp; to `api.anthropic.com` with **every** call — that is how the API works,<br/>
&nbsp; &nbsp; and making the injections *visible* (transcript annotations, token estimates)<br/>
&nbsp; &nbsp; is the tool's teaching purpose.<br/>
The README's privacy note tells users to load only Me-file content they are<br/>
&nbsp; &nbsp; comfortable sending. Nothing is sent anywhere else.

3. **The API key is handled minimally.**<br/>
The key is resolved from the environment by the SDK (`anthropic.Anthropic()`),<br/>
&nbsp; &nbsp; or read once from a user-chosen key file (`apikey.txt`, auto-loaded, or any<br/>
&nbsp; &nbsp; file via the **API** button) and passed directly to the SDK constructor.<br/>
&nbsp; &nbsp; It is never logged, exported, or written to any file by Curiosity Lab, and<br/>
&nbsp; &nbsp; never displayed beyond its last four characters.<br/>
&nbsp; &nbsp; `apikey.txt` and `*.key` are `.gitignore`d against accidental publishing.<br/>
&nbsp; &nbsp; The README documents per-window key scoping so an environment key cannot<br/>
&nbsp; &nbsp; silently shadow a Max-plan login elsewhere.

4. **Web access (v2.6.0) is model-driven and proxied.**<br/>
With the Web-search / Web-fetch boxes on, the tool declares Anthropic's server-side<br/>
&nbsp; &nbsp; `web_search` / `web_fetch` tools per request. Searches leave Anthropic carrying only<br/>
&nbsp; &nbsp; the **model-composed query** (no identity, IP, or location); fetches retrieve pages<br/>
&nbsp; &nbsp; through Anthropic's proxy, not from the user's machine. Every search and fetch the<br/>
&nbsp; &nbsp; model performs is annotated in the transcript, and retrieved text is rendered inert<br/>
&nbsp; &nbsp; exactly like any other reply (§5.1).

5. **Sandbox code execution (v2.7.0) runs remotely, not locally.**<br/>
With the Linux-sandbox box on, the tool declares Anthropic's server-side<br/>
&nbsp; &nbsp; `code_execution` tool. Code the model writes runs in an **Anthropic-hosted**<br/>
&nbsp; &nbsp; container with no internet access — **nothing executes on the user's machine**,<br/>
&nbsp; &nbsp; and there is no client-side bash/exec tool in the code. The tool warns the user,<br/>
&nbsp; &nbsp; at Send, when a prompt asks for execution while the box is off (so a model that<br/>
&nbsp; &nbsp; fabricates output can be caught).

6. **Sandbox image output (v2.9.0) is handled defensively.**<br/>
When the sandbox writes a file, the tool downloads the bytes via the beta Files API<br/>
&nbsp; &nbsp; (`files-api-2025-04-14`) on the worker thread and renders images inline.<br/>
&nbsp; &nbsp; Three safeguards:<br/>
&nbsp; &nbsp; **(a)** untrusted image bytes are decoded inside a guarded block — junk or a failed<br/>
&nbsp; &nbsp; decode yields no image, never a crash (the Pillow path falls back to the native Tk<br/>
&nbsp; &nbsp; decoder, line 2026); decoder-library CVEs are the one residual, out of the app's hands.<br/>
&nbsp; &nbsp; **(b)** sidecar filenames written on **Save** are **fully synthesised by the app** —<br/>
&nbsp; &nbsp; `"<md-stem>.assetN.<ext>"`, where the stem is the user's own chosen `.md` name and<br/>
&nbsp; &nbsp; the extension is taken via `os.path.splitext` (which cannot contain a path separator)<br/>
&nbsp; &nbsp; or a MIME fallback. A model-chosen `filename` contributes **only its extension**, so<br/>
&nbsp; &nbsp; it cannot traverse out of the user's save directory.<br/>
&nbsp; &nbsp; **(c)** images embedded in the exported Markdown are base64 `data:` URIs — inert text.

7. **`settings.json` write-back is bounded.**<br/>
On Quit the tool rewrites the user's `settings.json` to remember the window<br/>
&nbsp; &nbsp; position (the size is recomputed at startup to fit the content). Guardrails: an<br/>
&nbsp; &nbsp; unparseable file is **never overwritten** (left in place for the user to fix),<br/>
&nbsp; &nbsp; a missing file is created, and only the geometry entry is modified.

8. **The `models.json` catalog (v2.10.0) is data, not code.**<br/>
The model menu and its prices moved out of the script into an editable `models.json`,<br/>
&nbsp; &nbsp; parsed with `json.load`. Each entry is read for its tag, model ID, prices, and note;<br/>
&nbsp; &nbsp; nothing from the file is executed or used to build a path. A malformed file falls back<br/>
&nbsp; &nbsp; to the built-in `DEFAULT_MODELS` without being overwritten, and the model ID is passed<br/>
&nbsp; &nbsp; to the SDK as an opaque string.

9. **stderr is briefly redirected around native file dialogs.**<br/>
macOS AppKit prints a harmless one-time diagnostic when the save/open panel<br/>
&nbsp; &nbsp; class first loads; the tool silences stderr for the duration of the dialog<br/>
&nbsp; &nbsp; (restored in a `finally`). The `-v | --verbose` flag disables the<br/>
&nbsp; &nbsp; suppression entirely, so nothing is ever hidden from a user who wants it.

10. **All file reads are user-driven.**<br/>
Me-files and JSON configs are opened from paths the user picked in a dialog or<br/>
&nbsp; &nbsp; wrote in `settings.json` — no path is ever constructed from model output.

## 6. Dependencies

**pip-audit — PASS.**

```bash
$ pip-audit -r bin/requirements.txt
No known vulnerabilities found
```

The declared dependencies are the official `anthropic` SDK and **Pillow**<br/>
&nbsp; &nbsp; (image decoding / scaling for inline thumbnails), plus Tkinter from the<br/>
&nbsp; &nbsp; Python standard library. Both are pip-audited above. Pillow is a default<br/>
&nbsp; &nbsp; dependency, not a runtime requirement — it's installed so every user renders<br/>
&nbsp; &nbsp; identically, but soft-imported, so if absent the app still runs on a<br/>
&nbsp; &nbsp; native-Tk fallback and says so.

## 7. Recommended actions

No defect fixes required. The one optional hardening item from the prior scan is now closed:

| ID | Action | Priority | Effort | Status |
|---|---|---|---|---|
| CL-SAST-01 | Pin a minimum SDK version in `bin/requirements.txt`<br/>(e.g. `anthropic>=0.116`)<br/>so fresh installs get a known-audited<br/> dependency baseline. | Low | Low | **Resolved 2026-07-14**<br/>`requirements.txt` now pins `anthropic>=0.116`<br/>pip-audit clean. |

## 8. Triage framework

Verdicts available: **Accept — by design**, **False positive**, **Remediate**, **Accept risk**.<br/>
The five Bandit `B110` findings are all triaged **Accept — by design** (§4): each is a<br/>
&nbsp; &nbsp; deliberate graceful-degradation block in the GUI, none reaches security-relevant state.<br/>
Semgrep and pip-audit produced nothing to triage.
