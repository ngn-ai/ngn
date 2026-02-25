import logging
import os
import sys
from pathlib import Path

import anthropic

from ngn_agent.coder import implement_ticket
from ngn_agent.git import clone_repo
from ngn_agent.jira import JiraClient
from ngn_agent.validator import validate_ticket

_REQUIRED_ENV = ("ANTHROPIC_API_KEY", "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_FILTER_ID")
_API_UNAVAILABLE = "Anthropic API was unavailable for an extended period — please retry later."

log = logging.getLogger(__name__)


def main() -> None:
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

    log.info("Polling JIRA filter %s...", os.environ["JIRA_FILTER_ID"])
    tickets = jira.get_tickets_from_filter(os.environ["JIRA_FILTER_ID"])

    if not tickets:
        log.info("No candidate tickets found.")
        return

    top = tickets[0]
    log.info("Top ticket: %s [%s] (%s) %s", top["key"], top["issue_type"], top["priority"], top["summary"])

    log.info("Fetching full ticket details...")
    ticket = jira.get_ticket(top["key"])

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
        clone_repo(repo_url, workspace)
        log.info("Transitioning %s to IN PROGRESS...", ticket["key"])
        jira.transition_ticket(ticket["key"], "IN PROGRESS")
        log.info("Labelling %s as ngn-handled...", ticket["key"])
        jira.add_label(ticket["key"], "ngn-handled")

        log.info("Implementing ticket...")
        impl = implement_ticket(ticket, workspace, claude, ancestors=ancestors or None)

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


if __name__ == "__main__":
    main()
