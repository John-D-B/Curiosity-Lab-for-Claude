# Curiosity Lab for Claude:<br/>SAST Scan and Security Assessment

**Author:** JohnB, with AI pair-programming support by Anthropic Claude<br/>
**Date:** 2026-07-11<br/>
**Target version:** `bin/curiosity-lab.py` v2.4.0 (1,041 lines)<br/>
**Methodology:** static analysis with Bandit and Semgrep, dependency audit with pip-audit,<br/>
&nbsp; &nbsp; and the same triage framework as the sibling Web-Print assessment<br/>
&nbsp; &nbsp; (accept-by-design / false-positive / remediate / accept-risk).

## 1. Result

**PASS. Zero findings from all three scanners.**

- Bandit: **no issues identified** (1'041 lines of code scanned, no `#nosec` suppressions).
- Semgrep (`p/python`, `p/security-audit`, `p/command-injection`): **no findings**.
- pip-audit: **no known vulnerabilities** in dependencies.
- The security-relevant design points a reviewer should know are therefore all in<br/>
&nbsp; &nbsp; the proactive-disclosure section (§5) — none is a scanner finding.

## 2. Trust model (the frame for every verdict)

**Curiosity Lab** is a **single-user, local, desktop GUI tool.**<br/>
The user runs it on their own machine, with their own privileges,<br/>
&nbsp; &nbsp; with their own API key, against prompts **they** typed and files **they** chose.

There is no server, no multi-tenant surface, and no authentication to bypass.<br/>
The user is the trust boundary.

Two places genuinely cross that boundary:

- **Inbound:** the model's responses, which the tool renders into the transcript (§5.1).
- **Outbound:** everything the user configured — persona text, Me-file contents,<br/>
&nbsp; &nbsp; and the full chat history — is sent to `api.anthropic.com` on every call (§5.2).

## 3. Scope

### In scope

| Component | File | Tool |
|---|---|---|
| Main tool | `bin/curiosity-lab.py` (1'041 lines) | Bandit, Semgrep |
| Dependencies | `bin/requirements.txt` (`anthropic`) | pip-audit |

### Out of scope

| Item | Reason |
|---|---|
| `personas.json`, `curiosities.json`, `demos.json`, `settings.json`, `me.template.md` | Data files; parsed with `json.load` / read as text, never executed. |
| Saved chat exports (`*.chat.md`) | Output artifacts, not tool code. |

### Copy integrity check

The published `bin/curiosity-lab.py` was compared byte-for-byte against the<br/>
&nbsp; &nbsp; development tree's copy: **identical**. Scanning one covers both.

## 4. Findings

Toolchain: Bandit 1.9.4, Semgrep 1.161.0 (three community rulesets, 200 rules), pip-audit 2.10.0, Python 3.13, on macOS.

```bash
$ bandit -f txt bin/curiosity-lab.py
$ semgrep --config p/python --config p/security-audit --config p/command-injection bin/curiosity-lab.py
```

**None.** Bandit and Semgrep both report zero issues.<br/>
No suppressions (`#nosec`, `#nosemgrep`) exist anywhere in the file.

This is consistent with the tool's shape: no `subprocess`, no `eval`/`exec`,<br/>
&nbsp; &nbsp; no shell, no `urllib`, no temp-file creation, no hardcoded secrets.<br/>
All network traffic goes through the official `anthropic` SDK (TLS handled there).

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
&nbsp; &nbsp; or — since v2.4.0 — read once from a user-chosen key file (`apikey.txt`,<br/>
&nbsp; &nbsp; auto-loaded, or any file via the **API** button) and passed directly to<br/>
&nbsp; &nbsp; the SDK constructor. It is never logged, exported, or written to any file<br/>
&nbsp; &nbsp; by Curiosity Lab, and never displayed beyond its last four characters.<br/>
&nbsp; &nbsp; `apikey.txt` and `*.key` are `.gitignore`d against accidental publishing.<br/>
&nbsp; &nbsp; The README documents per-window key scoping so an environment key cannot<br/>
&nbsp; &nbsp; silently shadow a Max-plan login elsewhere.

4. **`settings.json` write-back is bounded.**<br/>
On Quit the tool rewrites the user's `settings.json` to remember the window<br/>
&nbsp; &nbsp; position (the size is recomputed at startup to fit the content). Guardrails: an unparseable file is **never overwritten**<br/>
&nbsp; &nbsp; (left in place for the user to fix), a missing file is created,<br/>
&nbsp; &nbsp; and only the geometry entry is modified.

5. **stderr is briefly redirected around native file dialogs.**<br/>
macOS AppKit prints a harmless one-time diagnostic when the save/open panel<br/>
&nbsp; &nbsp; class first loads; the tool silences stderr for the duration of the dialog<br/>
&nbsp; &nbsp; (restored in a `finally`). The `-v | --verbose` flag disables the<br/>
&nbsp; &nbsp; suppression entirely, so nothing is ever hidden from a user who wants it.

6. **All file reads are user-driven.**<br/>
Me-files and JSON configs are opened from paths the user picked in a dialog or<br/>
&nbsp; &nbsp; wrote in `settings.json` — no path is ever constructed from model output.

## 6. Dependencies

**pip-audit — PASS.**

```bash
$ pip-audit -r bin/requirements.txt
No known vulnerabilities found
```

The sole runtime dependency is the official `anthropic` SDK<br/>
&nbsp; &nbsp; (plus Tkinter from the Python standard library).

## 7. Recommended actions

No defect fixes required. One optional hardening item:

| ID | Action | Priority | Effort |
|---|---|---|---|
| CL-SAST-01 | Pin a minimum SDK version in `bin/requirements.txt` (e.g. `anthropic>=0.116`)<br/>&nbsp; &nbsp; so fresh installs get a known-audited dependency baseline. | Low | Low |

## 8. Triage framework

Verdicts available: **Accept — by design**, **False positive**, **Remediate**, **Accept risk**.<br/>
No finding existed to triage; the framework is recorded here for parity with<br/>
&nbsp; &nbsp; the Web-Print assessment and for future re-scans.
