# Security Audit — ngn-agent (Part II)

**Date:** 2026-02-27
**Auditor:** ngn (automated, NGN-26)
**Scope:** Full codebase — `main.py`, `jira.py`, `validator.py`, `coder.py`, `git.py`
**Prior audit:** `docs/security-audit_2026_02_26.md` (NGN-22)
**Commit audited:** HEAD of `main` branch at time of audit (07e1514)

---

## Audit Context

A prior security audit was performed as part of NGN-22 and documented in
`docs/security-audit_2026_02_26.md`. That document identified 16 findings. Since
then, two dedicated hardening PRs have landed:

* **NGN-24** — path traversal protection and ticket-key input validation
* **NGN-25** — prompt injection mitigations and credential isolation in subprocesses

This second audit reviews the current state of every file, verifies which prior
findings have been remediated, notes any regressions or incomplete remediations, and
identifies any new findings not present in the first audit.

---

## Remediation Status of Prior Findings

| # | Title (NGN-22) | Status |
|---|----------------|--------|
| 1 | Shell Injection via LLM-Controlled Command String | Open (accepted risk; see Finding A) |
| 2 | Prompt Injection via Malicious Ticket Content | **Partially remediated** — see Finding B |
| 3 | Path Traversal via LLM-Controlled File Paths | **Remediated** |
| 4 | Credential Exposure via Exception Messages in JIRA Comments | Open — see Finding C |
| 5 | Unvalidated External Repo URL Passed to Git Subprocesses | **Remediated** (scheme allowlist added) |
| 6 | No Timeout on `git clone` or `git ls-remote` | Open — see Finding D |
| 7 | No Timeout on `git checkout` in `poll_once` | Open — see Finding D |
| 8 | Credential Exposure via Agent Logging | Open — see Finding E |
| 9 | `ANTHROPIC_API_KEY` Present in Agent Process Environment | **Remediated** (credential stripping in `_run_command`) |
| 10 | Workspace Directory Traversal via Ticket Key | **Remediated** (key regex guard added) |
| 11 | Filter ID Injected into JQL Query String | Open — see Finding F |
| 12 | JIRA Base URL Not Validated — Potential SSRF / Misconfiguration | Open — see Finding G |
| 13 | No Rate-Limit or Retry Handling for JIRA API Calls | Open — see Finding H |
| 14 | Dependency on `httpx` and `anthropic` — Supply-Chain Risk | Open (informational) |
| 15 | `subprocess.run` Without `check=True` in `find_resume_branch` | Open — see Finding I |
| 16 | Agent Comments May Expose Internal Implementation Details | Open — see Finding J |

---

## Findings

---

## Finding A: Shell Injection via LLM-Controlled Command String (Prior Finding 1 — Accepted Risk)

**File:** `src/ngn_agent/coder.py`, `_run_command()` (lines 299–322)
**Severity:** Critical
**Description:**
`_run_command` invokes `subprocess.run` with `shell=True` and a raw command string
supplied by Claude:

```python
result = subprocess.run(
    command,       # ← string from LLM tool call
    shell=True,
    capture_output=True,
    text=True,
    cwd=cwd,
    timeout=120,
    env=sanitized_env,
)
```

Because `shell=True` passes the string through `/bin/sh -c`, any shell metacharacters
the model inserts in `command` are interpreted by the shell. This gives Claude (or an
adversary who influences Claude via prompt injection — see Finding B) effective
arbitrary code execution on the agent host.

Since NGN-25, `_run_command` now strips all five credential environment variables
from the child environment (`_CREDENTIAL_ENV_VARS`). This meaningfully reduces the
blast radius of a malicious `env | curl …` command. However, the underlying ability
to execute arbitrary shell commands remains, as it is fundamental to the agent's
purpose. The `timeout=120` parameter prevents indefinite hangs from this call site.

**Recommendation:**
Accept the risk as inherent to the design. The credential-stripping and 120-second
timeout mitigations already in place are appropriate. Additional defence-in-depth
controls remain recommended:

1. Run the agent process inside a container or VM with minimal capabilities, no
   access to host SSH keys, and restricted network egress.
2. Consider an allowlist or human-in-the-loop gate for high-risk command patterns
   (e.g. `rm -rf`, `curl | bash`, writes outside the workspace).
3. Document the operating-environment isolation assumptions in README / CLAUDE.md.

---

## Finding B: Prompt Injection via Malicious Ticket Content (Prior Finding 2 — Partially Remediated)

**File:** `src/ngn_agent/coder.py`, `_format_ticket()` (lines 357–393); `src/ngn_agent/validator.py`, `_format_ticket()` (lines 72–86)
**Severity:** Critical
**Description:**
NGN-25 remediated the injection risk in `coder.py` by:

1. Wrapping all free-text ticket fields (summary, description, comment bodies, and
   comment author names) in `<untrusted-content>…</untrusted-content>` XML tags.
2. Adding an explicit instruction to the system prompt in `coder.py` directing
   Claude to treat content inside those tags as data and to ignore any embedded
   directives.

However, **`validator.py` was not updated**. Its `_format_ticket()` function still
inserts ticket content verbatim into the Claude context without any sanitisation or
trust-boundary marking:

```python
# validator.py — _format_ticket (current, unremediated)
lines.append(f"\nDescription:\n{ticket.get('description') or '(none)'}")
for c in ticket["comments"]:
    lines.append(f"  [{c['created']}] {c['author']}:\n  {c['body']}")
```

Similarly, `validator.py`'s `_SYSTEM_PROMPT` contains no instruction to ignore
directives embedded in ticket fields. A maliciously crafted ticket description could
therefore attempt to override the validator's logic — for example, instructing it to
return `valid=True` for an otherwise-incomplete ticket, or to extract a different
repo URL than the one actually present.

The validator controls which tickets the agent acts on and which repo URL it clones,
so a successful injection here could have consequences as serious as one in `coder.py`.

**Recommendation:**

1. Apply the same `_untrusted()` wrapping in `validator.py`'s `_format_ticket()` as
   was done in `coder.py`.
2. Add an explicit system-prompt instruction in `validator.py`'s `_SYSTEM_PROMPT`
   directing Claude to treat ticket content as untrusted data and to ignore any
   instructions embedded within it.

---

## Finding C: Credential Exposure via Exception Messages in JIRA Comments (Prior Finding 4 — Unchanged)

**File:** `src/ngn_agent/jira.py`, `validate_setup()` (lines 120–140, 170, 195); `src/ngn_agent/main.py`, `poll_once()` (lines 261–266)
**Severity:** High
**Description:**
Several error-handling paths convert raw exception objects to strings and include
them in output that may reach JIRA comments or the validation report printed to
stdout:

```python
# jira.py — validate_setup, generic exception handler
except Exception as exc:
    results.append({
        "name": "JIRA authentication",
        "passed": False,
        "detail": str(exc),   # ← raw exception string
    })
```

```python
# main.py — poll_once, clone failure
lines = [
    "This ticket has been blocked by Agent ngn.",
    f"The repository could not be cloned: {exc}",   # ← exc converted to str
]
jira.post_comment(ticket["key"], lines, ...)
```

If an exception message contains credential material — for example, the `httpx`
library sometimes includes the full request URL (which could contain query parameters
with token values) in `ConnectError` and similar exceptions — it will be surfaced to
any JIRA user who can view the ticket. This finding has not been addressed since the
prior audit.

**Recommendation:**

1. For the `clone` failure path, `RuntimeError` messages originate from git's stderr,
   which is unlikely to contain credentials; however, applying a truncation limit
   (e.g. 500 characters) is a safe precaution.
2. In `validate_setup`, replace `str(exc)` for generic exceptions with a safe
   summary and log the full exception internally:
   ```python
   except Exception:
       log.exception("Unexpected error during JIRA auth check")
       results.append({..., "detail": "unexpected error — see agent logs"})
   ```
3. Review the `httpx` exception hierarchy to confirm that request URLs are not
   included in exception string representations for the versions in use.

---

## Finding D: Missing Timeouts on Git Subprocess Calls (Prior Findings 6 & 7 — Unchanged)

**File:** `src/ngn_agent/git.py`, `clone_repo()` (line 49) and `find_resume_branch()` (line 67); `src/ngn_agent/main.py`, `poll_once()` (line 274)
**Severity:** Medium
**Description:**
Three subprocess calls still lack `timeout` parameters:

```python
# git.py — clone_repo
result = subprocess.run(
    ["git", "clone", repo_url, str(dest)],
    capture_output=True,
    text=True,
    # no timeout=
)

# git.py — find_resume_branch
result = subprocess.run(
    ["git", "ls-remote", "--heads", repo_url, f"refs/heads/{branch}"],
    capture_output=True,
    text=True,
    # no timeout=
)

# main.py — poll_once
subprocess.run(
    ["git", "-C", str(workspace), "checkout", ticket_branch],
    check=True,
    # no timeout=
)
```

If the remote host is slow or unreachable, any of these calls can block the agent
process indefinitely. `_run_command` in `coder.py` already sets `timeout=120`, which
demonstrates the correct pattern; the omission in `git.py` and `main.py` appears
inadvertent. This finding has not been addressed since the prior audit.

**Recommendation:**

```python
# clone_repo — allow up to 5 minutes for large repositories
subprocess.run([...], capture_output=True, text=True, timeout=300)

# find_resume_branch — quick remote query; 30 seconds is generous
subprocess.run([...], capture_output=True, text=True, timeout=30)

# poll_once git checkout — local operation; 60 seconds is generous
subprocess.run([...], check=True, timeout=60)
```

Catch `subprocess.TimeoutExpired` and convert it to an appropriate error at each
call site (e.g. raise `RuntimeError` in `clone_repo`, return `False` in
`find_resume_branch`, and transition the ticket to BLOCKED in `poll_once`).

---

## Finding E: Credential Exposure via Agent Logging (Prior Finding 8 — Unchanged)

**File:** `src/ngn_agent/main.py`, logging configuration (lines 63–65); `src/ngn_agent/jira.py`, `JiraClient.__init__()` (lines 23–25)
**Severity:** Medium
**Description:**
`main()` suppresses `httpx` and `anthropic` loggers to `WARNING` level:

```python
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("anthropic").setLevel(logging.WARNING)
```

This reduces the risk of DEBUG-level library output leaking credential values.
However, `JiraClient` stores credentials as instance attributes:

```python
self.auth = (os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])
```

If a future change inadvertently logs `str(self)` or the full `JiraClient` object,
credentials could be exposed. Additionally, there is no explicit suppression of
third-party loggers that may be added by future dependencies. This finding has not
been addressed since the prior audit.

**Recommendation:**

1. Verify (via a unit or integration test) that no `httpx` request headers appear
   in `WARNING`-level output.
2. Consider overriding `__repr__` on `JiraClient` to redact the `auth` tuple.
3. Suppress the `httpx` logger to `WARNING` in any integration test that exercises
   HTTP calls, to prevent accidental credential leakage in test output.

---

## Finding F: Filter ID Injected into JQL Query String (Prior Finding 11 — Unchanged)

**File:** `src/ngn_agent/jira.py`, `get_tickets_from_filter()` (lines 77–84)
**Severity:** Low
**Description:**
The JIRA filter ID — taken from the `JIRA_FILTER_ID` environment variable — is
interpolated directly into a JQL query string without format validation:

```python
"jql": f"filter={filter_id} AND issuetype in (Bug, Task, Story) AND status = READY",
```

If `JIRA_FILTER_ID` contains unexpected characters (e.g. a space, JQL keywords, or
quotes), the query may be corrupted or, in a worst-case scenario, allow JQL injection.
The filter ID is operator-controlled (set in the environment), so the practical risk
is low; however, no validation is applied. This finding has not been addressed since
the prior audit.

**Recommendation:**
Validate that `filter_id` matches the expected numeric format before use:

```python
import re
if not re.fullmatch(r"\d+", filter_id):
    raise ValueError(f"JIRA_FILTER_ID must be a numeric string, got: {filter_id!r}")
```

---

## Finding G: JIRA Base URL Not Validated — Potential SSRF / Misconfiguration (Prior Finding 12 — Unchanged)

**File:** `src/ngn_agent/jira.py`, `JiraClient.__init__()` (line 23); `validate_setup()` (line 105)
**Severity:** Low
**Description:**
`JIRA_BASE_URL` is accepted and used without scheme or hostname validation:

```python
self.base_url = os.environ["JIRA_BASE_URL"].rstrip("/")
```

All subsequent API calls construct URLs from this base. If the value is misconfigured
(e.g. uses `http://` instead of `https://`, or points to an internal or
attacker-controlled endpoint), the agent will forward JIRA credentials to that
address. This finding has not been addressed since the prior audit.

**Recommendation:**
Validate the scheme at startup (both in `JiraClient.__init__` and `validate_setup`):

```python
from urllib.parse import urlparse
parsed = urlparse(base_url)
if parsed.scheme != "https":
    raise ValueError("JIRA_BASE_URL must use https://")
```

---

## Finding H: No Rate-Limit or Retry Handling for JIRA API Calls (Prior Finding 13 — Unchanged)

**File:** `src/ngn_agent/jira.py`, all HTTP methods
**Severity:** Low
**Description:**
Every JIRA API call in `JiraClient` uses bare `httpx.get` / `httpx.post` /
`httpx.put` with no explicit `timeout` parameter and no retry logic. A `429 Too Many
Requests` response raises an unhandled `httpx.HTTPStatusError` rather than triggering
a retry. The default `httpx` timeout (5 seconds) may cause spurious failures on slow
networks.

By contrast, both `coder.py` and `validator.py` implement robust retry loops for
Anthropic API calls. This finding has not been addressed since the prior audit.

**Recommendation:**

1. Add `timeout=30` (or similar) to all `httpx` calls in `JiraClient`.
2. Implement a simple retry wrapper that retries on HTTP 429 and 5xx responses with
   exponential backoff, mirroring the pattern already in `coder.py` and
   `validator.py`.

---

## Finding I: Silent Failure in `find_resume_branch` When `git ls-remote` Fails (Prior Finding 15 — Unchanged)

**File:** `src/ngn_agent/git.py`, `find_resume_branch()` (lines 65–76)
**Severity:** Informational
**Description:**
`find_resume_branch` does not inspect the return code of `git ls-remote`:

```python
result = subprocess.run(
    ["git", "ls-remote", "--heads", repo_url, f"refs/heads/{branch}"],
    capture_output=True,
    text=True,
)
# A non-empty stdout means the ref was found on the remote.
return bool(result.stdout.strip())
```

If `git ls-remote` exits non-zero for any reason other than a Python exception (e.g.
authentication failure or network timeout), the function silently returns `False` and
the agent falls back to a fresh start. This is the safe fallback, but non-obvious
failures are swallowed without any diagnostic output for operators. This finding has
not been addressed since the prior audit.

**Recommendation:**
Log the return code and stderr at `DEBUG` level when the command fails:

```python
if result.returncode != 0:
    log.debug(
        "git ls-remote exited %d for %r: %s",
        result.returncode,
        branch,
        result.stderr.strip(),
    )
```

---

## Finding J: Agent Comments May Expose Internal Implementation Details (Prior Finding 16 — Unchanged)

**File:** `src/ngn_agent/main.py`, `poll_once()` (lines 298–304)
**Severity:** Informational
**Description:**
JIRA comments posted when an implementation is blocked include the full
`blocked_reason` string sourced from the LLM's `report_blocked` call or from
internal Python exception messages:

```python
lines = [
    "Implementation was blocked by Agent ngn.",
    f"Reason: {impl['blocked_reason']}",
]
jira.post_comment(ticket["key"], lines, mention=mention)
```

Depending on what the model or an exception handler places in `blocked_reason`,
these comments may expose internal file paths, stack traces, partial command output,
or other implementation details to any JIRA user who can view the ticket. This
finding has not been addressed since the prior audit.

**Recommendation:**
Truncate `blocked_reason` before including it in JIRA comments (e.g. 500 characters
maximum) and log the full text internally only:

```python
log.error("Full blocked reason: %s", impl["blocked_reason"])
short_reason = impl["blocked_reason"][:500]
if len(impl["blocked_reason"]) > 500:
    short_reason += " … (truncated — see agent logs)"
lines = ["Implementation was blocked by Agent ngn.", f"Reason: {short_reason}"]
```

---

## Finding K: `_find_open_pr` Subprocess Call Has No Timeout (New Finding)

**File:** `src/ngn_agent/main.py`, `_find_open_pr()` (lines 160–180)
**Severity:** Low
**Description:**
`_find_open_pr` invokes the GitHub CLI via `subprocess.run` with no `timeout`
parameter:

```python
result = subprocess.run(
    ["gh", "pr", "list", "--head", branch, "--state", "open",
     "--json", "url", "--jq", ".[0].url"],
    capture_output=True,
    text=True,
    cwd=str(workspace),
    # no timeout=
)
```

If the GitHub API is slow or unavailable, or the `gh` binary hangs waiting for
authentication, this call blocks the polling loop indefinitely. The function wraps
the call in a broad `except Exception` clause that would catch
`subprocess.TimeoutExpired` — but since no timeout is specified, `TimeoutExpired` is
never raised.

This function was added after the first audit and was not reviewed there.

**Recommendation:**
Add `timeout=30` to the call:

```python
result = subprocess.run(
    [...],
    capture_output=True,
    text=True,
    cwd=str(workspace),
    timeout=30,
)
```

The existing `except Exception` handler will then catch `subprocess.TimeoutExpired`
and return `None` (treated as "no open PR"), which is the correct safe fallback.

---

## Finding L: `_find_open_pr` Passes Untrusted Branch Name to `gh` CLI (New Finding)

**File:** `src/ngn_agent/main.py`, `_find_open_pr()` (lines 160–180)
**Severity:** Low
**Description:**
`_find_open_pr` receives a `branch` string and passes it directly as the `--head`
argument to `gh pr list`:

```python
result = subprocess.run(
    ["gh", "pr", "list", "--head", branch, "--state", "open", ...],
    ...
)
```

In `poll_once`, `branch` is always constructed as `f"ngn/{ticket['key']}"` after the
ticket key has been validated against `_TICKET_KEY_RE` (`^[A-Z]+-\d+$`), so the
actual risk at the current call site is low. The call uses a list argument (not
`shell=True`), so shell injection is not possible regardless.

However, `_find_open_pr` itself does not enforce this constraint; it accepts any
arbitrary string as `branch`. If the function were called from another context with
an unvalidated branch name, the argument could be misused.

**Recommendation:**
Document the expected format of the `branch` parameter in `_find_open_pr`'s
docstring and add an early-return guard for unexpected values:

```python
if not re.match(r"^ngn/[A-Z]+-\d+$", branch):
    log.warning("_find_open_pr called with unexpected branch name: %r", branch)
    return None
```

---

## Finding M: `_list_directory` Has No Workspace Boundary Check (New Finding)

**File:** `src/ngn_agent/coder.py`, `_list_directory()` (lines 277–285)
**Severity:** Low
**Description:**
`_read_file` and `_write_file` were both hardened in NGN-24 to enforce that the
resolved path falls within the workspace root. `_list_directory` was not updated with
a corresponding check:

```python
def _list_directory(path: str) -> str:
    try:
        entries = sorted(Path(path).iterdir(), key=lambda e: (e.is_file(), e.name))
        lines = [f"{'d' if e.is_dir() else 'f'}  {e.name}" for e in entries]
        return "\n".join(lines) or "(empty)"
    except Exception as exc:
        return f"Error listing {path}: {exc}"
```

The model can supply an arbitrary path — such as `/home`, `/etc`, `/root`, or
`/proc` — and the function will enumerate its contents and return the listing to the
model. While listing a directory does not directly expose file contents, it discloses
the existence and names of files and subdirectories outside the workspace, which
could aid further exploration or targeted reads via other tools.

The prior audit (Finding 3 / NGN-22) explicitly mentioned this gap; the NGN-24
remediation addressed `_read_file` and `_write_file` but not `_list_directory`.

**Recommendation:**
Add the same workspace-boundary check to `_list_directory` and thread `workspace`
through `_dispatch`:

```python
def _list_directory(path: str, workspace: Path) -> str:
    try:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(workspace.resolve()):
            return f"Error: path '{path}' is outside the workspace and cannot be listed"
        entries = sorted(resolved.iterdir(), key=lambda e: (e.is_file(), e.name))
        lines = [f"{'d' if e.is_dir() else 'f'}  {e.name}" for e in entries]
        return "\n".join(lines) or "(empty)"
    except Exception as exc:
        return f"Error listing {path}: {exc}"
```

---

## Finding N: `validator.py` Does Not Sanitise Ancestor Ticket Content (New Finding)

**File:** `src/ngn_agent/validator.py`, `validate_ticket()` (lines 57–64)
**Severity:** Medium
**Description:**
`validate_ticket` may include both the target ticket and ancestor tickets in the
content sent to Claude. Ancestor tickets are formatted by `validator.py`'s own
`_format_ticket()`, which — as noted in Finding B — does not apply
`<untrusted-content>` wrapping:

```python
if ancestors:
    ancestor_sections = "\n\n---\n\n".join(_format_ticket(a) for a in ancestors)
    content = (
        f"Ancestor ticket context (outermost to innermost):\n\n"
        f"{ancestor_sections}\n\n---\n\nTicket to validate:\n{content}"
    )
```

An attacker who controls an ancestor ticket (e.g. an Epic) could embed instructions
in its description or comments. These would be passed verbatim to the validator
Claude instance, which has no system-prompt instruction to treat them as untrusted
data.

The ancestor path is worth calling out separately from Finding B because ancestor
tickets (particularly Epics) are often authored by a wider set of team members and
may be sourced from external integration systems, increasing the realistic attack
surface compared with individual task tickets.

**Recommendation:**
Apply the same `<untrusted-content>` wrapping and system-prompt instruction to both
the target ticket and all ancestor tickets in `validator.py` (see Finding B for the
full remediation steps).

---

## Finding O: Unhandled `OSError` in `poll_once` Clone Step May Crash the Agent (New Finding)

**File:** `src/ngn_agent/main.py`, `poll_once()` (lines 239–253)
**Severity:** Informational
**Description:**
`poll_once` catches only `(RuntimeError, ValueError)` from the repository clone
step:

```python
try:
    clone_repo(repo_url, workspace)
except (RuntimeError, ValueError) as exc:
    ...
```

Other unexpected exceptions (e.g. `OSError` from `shutil.rmtree` when the workspace
directory cannot be deleted, or `PermissionError` when creating the parent directory)
are not caught here. They propagate to the outer polling loop in `main()`, which only
catches `httpx.RequestError` and `httpx.HTTPStatusError`. Any other exception will
crash the agent process, stopping all polling without posting a JIRA notification.

**Recommendation:**
Either:

1. Broaden the `except` clause to include `OSError`:
   ```python
   except (RuntimeError, ValueError, OSError) as exc:
       ...
   ```
2. Or add a catch-all in the `main()` polling loop that logs the full traceback and
   continues (with a sleep), ensuring the agent remains alive:
   ```python
   except Exception:
       log.exception("Unexpected error during poll iteration — continuing")
   ```

---

## Summary

### Remediated since NGN-22

| Finding # | Title |
|-----------|-------|
| 3  | Path traversal via LLM-controlled file paths (`_read_file`, `_write_file`) |
| 5  | Unvalidated external repo URL passed to git subprocesses |
| 9  | `ANTHROPIC_API_KEY` present in agent process environment |
| 10 | Workspace directory traversal via ticket key |

### Finding counts by severity

| Severity      | Count | Findings         |
|---------------|-------|------------------|
| Critical      | 2     | A, B             |
| High          | 1     | C                |
| Medium        | 3     | D, E, N          |
| Low           | 6     | F, G, H, K, L, M |
| Informational | 3     | I, J, O          |
| **Total**     | **15**|                  |

### Priority recommendations

1. **Remediate Findings B and N (Critical — prompt injection in `validator.py`).**
   The `coder.py` remediation from NGN-25 was not applied to `validator.py`. Applying
   `<untrusted-content>` wrapping and a system-prompt instruction to both the target
   ticket and ancestor tickets in `validator.py` is a low-effort change with high
   security value, and directly addresses an open Critical finding.

2. **Add the workspace boundary check to `_list_directory` (Finding M — Low).**
   The NGN-24 hardening round remediated `_read_file` and `_write_file` but left
   `_list_directory` open. This is a small, well-understood, one-function fix.

3. **Add timeouts to all remaining subprocess calls without them (Findings D, K).**
   `git clone`, `git ls-remote`, `git checkout`, and `gh pr list` all lack timeout
   parameters. Adding them prevents indefinite hangs and is a minimal code change.

4. **Sanitise exception messages before posting to JIRA (Finding C — High).**
   Replace `str(exc)` in generic exception handlers with safe summaries and log the
   full exception internally. Eliminates the risk of credential material appearing in
   publicly-visible JIRA comments.

5. **Validate `JIRA_BASE_URL` scheme and `JIRA_FILTER_ID` format at startup (Findings F, G).**
   Both are simple one-line validations that prevent misconfiguration from silently
   forwarding credentials to unintended endpoints or corrupting JQL queries.

6. **Isolate the agent process (Finding A — accepted risk by design).**
   Run ngn-agent inside a container or VM with minimal capabilities, restricted
   network egress, and no access to host credentials beyond those explicitly required.
   This remains the single most impactful control given the design intent of running
   arbitrary shell commands.
