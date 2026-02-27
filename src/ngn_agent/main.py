import argparse
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import anthropic
import httpx

from ngn_agent.coder import implement_ticket
from ngn_agent.git import clone_repo, find_resume_branch
from ngn_agent.jira import JiraClient, validate_setup
from ngn_agent.validator import validate_ticket

_REQUIRED_ENV = ("ANTHROPIC_API_KEY", "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_FILTER_ID")
_API_UNAVAILABLE = "Anthropic API was unavailable for an extended period — please retry later."

# Minimum number of seconds to wait between Jira polls so we don't spin.
_POLL_INTERVAL = 30

# Pattern that every Jira ticket key must match before we use it to construct
# a filesystem path.  Prevents directory-traversal via a crafted key such as
# "../../tmp/evil".
_TICKET_KEY_RE = re.compile(r"^[A-Z]+-\d+$")

log = logging.getLogger(__name__)


def main() -> None:
    """Entry point for ngn-agent.

    Parses CLI arguments.  When ``--validate`` is supplied, runs pre-flight
    checks against the configured JIRA instance, prints a pass/fail report to
    stdout, and exits with code 0 (all checks passed) or 1 (any check failed).
    The polling loop is never started in this mode.

    Without ``--validate``, configures logging, validates required environment
    variables, then loops forever polling Jira for new work.  Each iteration is
    rate-limited to at most one poll every _POLL_INTERVAL seconds so the agent
    doesn't spin.  The loop runs until the process is killed (e.g. SIGINT /
    SIGTERM).
    """
    parser = argparse.ArgumentParser(
        prog="ngn-agent",
        description="Autonomous coding agent powered by Claude.",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Check JIRA configuration and report whether all requirements are met, then exit.",
    )
    # Use parse_known_args so that any unrecognised arguments (e.g. pytest
    # passes its own argv when calling main() in tests) are silently ignored
    # rather than causing argparse to exit with an error.
    args, _ = parser.parse_known_args()

    if args.validate:
        _run_validate()
        # _run_validate always calls sys.exit; this line is unreachable but
        # makes the control flow explicit.
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)

    missing = [v for v in _REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        for var in missing:
            log.error("Environment variable is not set: %s", var)
        sys.exit(1)

    jira = JiraClient()
    claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    log.info("ngn-agent started — polling every %ss. Press Ctrl-C to stop.", _POLL_INTERVAL)

    # Infinite polling loop — runs until the process is killed.
    while True:
        iteration_start = time.monotonic()

        try:
            poll_once(jira, claude)
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            # Transient failures — network errors (DNS, connection refused, timeout)
            # and HTTP error responses (4xx/5xx) from Jira — are caught here so the
            # loop continues rather than crashing the agent.
            log.warning("Error during poll — will retry: %s", exc)

        # Sleep for the remainder of the interval so we poll at most once per
        # _POLL_INTERVAL seconds regardless of how long the work took.
        elapsed = time.monotonic() - iteration_start
        wait = _POLL_INTERVAL - elapsed
        if wait > 0:
            log.info("Sleeping %.1fs until next poll...", wait)
            time.sleep(wait)


def _run_validate() -> None:
    """Run pre-flight JIRA validation checks and print a formatted report.

    Calls ``validate_setup()`` to obtain a list of check results, prints each
    result with a ``✓`` (pass), ``✗`` (fail), or ``⚠`` (warning) prefix, then
    appends a static reminder about workflow transitions (which cannot be
    verified via the API without a live ticket).

    Exits with code 0 when all checks passed, or 1 if any check failed.
    """
    results = validate_setup()

    for result in results:
        symbol = "✓" if result["passed"] else "✗"
        name = result["name"]
        detail = result["detail"]

        # Format the line based on check name so the output matches the
        # documented format (detail is shown in parentheses for auth / filter,
        # or appended with a colon for issue types / statuses).
        if name == "Environment variables":
            if result["passed"]:
                print(f"{symbol} {name}")
            else:
                print(f"{symbol} {name}: {detail}")
        elif name == "JIRA authentication":
            if result["passed"]:
                print(f"{symbol} {name} ({detail})")
            else:
                print(f"{symbol} {name}: {detail}")
        elif name == "Filter accessible":
            if result["passed"]:
                print(f"{symbol} {name} ({detail})")
            else:
                print(f"{symbol} {name}: {detail}")
        elif name in ("Issue types", "Statuses"):
            print(f"{symbol} {name}: {detail}")
        else:
            # Fallback for any future check names.
            print(f"{symbol} {name}: {detail}")

    # Transitions can never be verified without a live ticket — always shown
    # as a warning regardless of the other results.
    print(
        "\u26a0 Transitions: cannot be verified without a live ticket"
        " \u2014 confirm IN PROGRESS, IN REVIEW, and BLOCKED transitions"
        " exist in your project workflow manually"
    )

    all_passed = all(r["passed"] for r in results)
    sys.exit(0 if all_passed else 1)


def _find_open_pr(workspace: Path, branch: str) -> str | None:
    """Check whether an open pull request already exists for *branch* in the workspace.

    Runs ``gh pr list --head <branch> --state open --json url --jq '.[0].url'``
    inside *workspace* and returns the URL string if one is found, or ``None``
    when no open PR exists or the command fails.

    Args:
        workspace: Path to the cloned repository where the gh command is run.
        branch: Branch name to look up (e.g. ``ngn/PROJ-42``).

    Returns:
        The PR URL string, or ``None`` if no open PR was found.
    """
    try:
        result = subprocess.run(
            [
                "gh", "pr", "list",
                "--head", branch,
                "--state", "open",
                "--json", "url",
                "--jq", ".[0].url",
            ],
            capture_output=True,
            text=True,
            cwd=str(workspace),
        )
        url = result.stdout.strip()
        return url if url else None
    except Exception:
        # Any subprocess error (e.g. gh not installed, not authenticated) is
        # treated as "no open PR found" so the agent falls back to normal flow.
        return None


def poll_once(jira: JiraClient, claude: anthropic.Anthropic) -> None:
    """Poll Jira for a single candidate ticket and attempt to implement it.

    This function encapsulates one full iteration of the agent loop: it fetches
    the top-priority ticket from the configured Jira filter, validates it with
    Claude, and drives the implementation through to a pull request (or marks
    the ticket as BLOCKED on failure).  It is a no-op when no candidate tickets
    are found.

    Ticket key validation is applied before constructing a workspace path.
    Keys that do not match ``[A-Z]+-\\d+`` (e.g. ``../../tmp/evil``) are
    rejected immediately: the ticket is transitioned to BLOCKED and a comment
    is posted before returning.

    Args:
        jira: Authenticated Jira client.
        claude: Anthropic API client used for validation and implementation.
    """
    log.info("Polling JIRA filter %s...", os.environ["JIRA_FILTER_ID"])
    tickets = jira.get_tickets_from_filter(os.environ["JIRA_FILTER_ID"])

    if not tickets:
        log.info("No candidate tickets found.")
        return

    top = tickets[0]
    log.info("Top ticket: %s [%s] (%s) %s", top["key"], top["issue_type"], top["priority"], top["summary"])

    log.info("Fetching full ticket details...")
    ticket = jira.get_ticket(top["key"])

    # Guard against malformed ticket keys before using the key to build a
    # filesystem path.  A crafted key like "../../tmp/evil" would otherwise
    # resolve to an arbitrary directory outside the workspace root.
    if not _TICKET_KEY_RE.match(ticket["key"]):
        log.error("Ticket key '%s' does not match expected pattern — blocking.", ticket["key"])
        jira.transition_ticket(ticket["key"], "BLOCKED")
        reporter = ticket.get("reporter")
        mention = (reporter["account_id"], reporter["display_name"]) if reporter else None
        jira.post_comment(
            ticket["key"],
            [
                "This ticket has been blocked by Agent ngn.",
                f"The ticket key '{ticket['key']}' is not a valid Jira key (expected format: PROJECT-123).",
            ],
            mention=mention,
        )
        return

    ancestors = []
    current = ticket
    while current.get("parent"):
        parent_key = current["parent"]["key"]
        log.info("Fetching ancestor ticket %s...", parent_key)
        parent = jira.get_ticket(parent_key)
        ancestors.insert(0, parent)
        current = parent

    log.info("Validating ticket with Claude...")
    try:
        result = validate_ticket(ticket, claude, ancestors=ancestors or None)
    except anthropic.APIStatusError:
        log.error(_API_UNAVAILABLE)
        reporter = ticket.get("reporter")
        mention = (reporter["account_id"], reporter["display_name"]) if reporter else None
        jira.transition_ticket(ticket["key"], "BLOCKED")
        jira.post_comment(ticket["key"], [_API_UNAVAILABLE], mention=mention)
        return

    if result["valid"]:
        repo_url = result.get("repo_url", "")
        workspace = (Path(os.environ.get("WORKSPACE_DIR", "workspaces")) / ticket["key"]).resolve()
        log.info("Ticket is valid. Repo: %s  Workspace: %s", repo_url, workspace)
        log.info("Cloning repository...")
        try:
            clone_repo(repo_url, workspace)
        except (RuntimeError, ValueError) as exc:
            # The repo URL is invalid, uses a disallowed scheme, or is
            # inaccessible — block the ticket and resume the outer polling loop
            # rather than crashing the agent.
            log.error("Failed to clone repository: %s", exc)
            log.info("Transitioning %s to BLOCKED...", ticket["key"])
            jira.transition_ticket(ticket["key"], "BLOCKED")
            reporter = ticket.get("reporter")
            mention = (reporter["account_id"], reporter["display_name"]) if reporter else None
            lines = [
                "This ticket has been blocked by Agent ngn.",
                f"The repository could not be cloned: {exc}",
            ]
            jira.post_comment(ticket["key"], lines, mention=mention)
            return

        # Check whether a prior attempt already pushed a branch for this ticket.
        # If so, check it out and pass the name through to implement_ticket so
        # the agent knows to resume rather than start from scratch.
        ticket_branch = f"ngn/{ticket['key']}"
        resume_branch: str | None = None
        existing_pr_url: str | None = None
        if find_resume_branch(repo_url, ticket_branch):
            log.info("Resuming from existing branch %s...", ticket_branch)
            subprocess.run(
                ["git", "-C", str(workspace), "checkout", ticket_branch],
                check=True,
            )
            resume_branch = ticket_branch

            # Check for an open PR on this branch so the agent can address
            # review feedback rather than attempting to open a duplicate PR.
            existing_pr_url = _find_open_pr(workspace, ticket_branch)
            if existing_pr_url:
                log.info("Found existing open PR for %s: %s", ticket_branch, existing_pr_url)

        log.info("Transitioning %s to IN PROGRESS...", ticket["key"])
        jira.transition_ticket(ticket["key"], "IN PROGRESS")
        log.info("Labelling %s as ngn-handled...", ticket["key"])
        jira.add_label(ticket["key"], "ngn-handled")

        log.info("Implementing ticket...")
        impl = implement_ticket(
            ticket,
            workspace,
            claude,
            ancestors=ancestors or None,
            resume_branch=resume_branch,
            pr_url=existing_pr_url,
        )

        if impl["success"]:
            log.info("Implementation complete. PR: %s", impl["pr_url"])
            log.info("Transitioning %s to IN REVIEW...", ticket["key"])
            jira.transition_ticket(ticket["key"], "IN REVIEW")
        else:
            log.error("Implementation blocked: %s", impl["blocked_reason"])
            log.info("Transitioning %s to BLOCKED...", ticket["key"])
            jira.transition_ticket(ticket["key"], "BLOCKED")
            reporter = ticket.get("reporter")
            mention = (reporter["account_id"], reporter["display_name"]) if reporter else None
            lines = [
                "Implementation was blocked by Agent ngn.",
                f"Reason: {impl['blocked_reason']}",
            ]
            jira.post_comment(ticket["key"], lines, mention=mention)
    else:
        log.error("Ticket is missing required information: %s", ", ".join(result["missing"]))
        log.info("Transitioning %s to BLOCKED...", ticket["key"])
        jira.transition_ticket(ticket["key"], "BLOCKED")

        reporter = ticket.get("reporter")
        mention = (reporter["account_id"], reporter["display_name"]) if reporter else None
        lines = [
            "This ticket has been blocked by Agent ngn.",
            "The following required information was not found:",
        ] + [f"\u2022 {item}" for item in result["missing"]]
        jira.post_comment(ticket["key"], lines, mention=mention)

    log.info("Done.")
