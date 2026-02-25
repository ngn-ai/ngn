import os
import sys
from pathlib import Path

import anthropic

from ngn_agent.git import clone_repo
from ngn_agent.jira import JiraClient
from ngn_agent.validator import validate_ticket

_REQUIRED_ENV = ("ANTHROPIC_API_KEY", "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_FILTER_ID")


def main() -> None:
    missing = [v for v in _REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        for var in missing:
            print(f"Error: {var} environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    jira = JiraClient()
    claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    print(f"Polling JIRA filter {os.environ['JIRA_FILTER_ID']}...")
    tickets = jira.get_tickets_from_filter(os.environ["JIRA_FILTER_ID"])

    if not tickets:
        print("No candidate tickets found.")
        return

    top = tickets[0]
    print(f"Top ticket: {top['key']} [{top['issue_type']}] ({top['priority']}) {top['summary']}")

    print("Fetching full ticket details...")
    ticket = jira.get_ticket(top["key"])

    ancestors = []
    current = ticket
    while current.get("parent"):
        parent_key = current["parent"]["key"]
        print(f"Fetching ancestor ticket {parent_key}...")
        parent = jira.get_ticket(parent_key)
        ancestors.insert(0, parent)
        current = parent

    print("Validating ticket with Claude...")
    result = validate_ticket(ticket, claude, ancestors=ancestors or None)

    if result["valid"]:
        repo_url = result.get("repo_url", "")
        workspace = Path(os.environ.get("WORKSPACE_DIR", "workspaces")) / ticket["key"]
        print(f"\n✓ Ticket is valid.")
        print(f"  Repo:      {repo_url}")
        print(f"  Workspace: {workspace}")
        print(f"\nCloning repository...")
        clone_repo(repo_url, workspace)
        print(f"Transitioning {ticket['key']} to IN PROGRESS...")
        jira.transition_ticket(ticket["key"], "IN PROGRESS")
        print(f"Labelling {ticket['key']} as ngn-handled...")
        jira.add_label(ticket["key"], "ngn-handled")
        print("Done.")
    else:
        print(f"\n✗ Ticket is missing required information:")
        for item in result["missing"]:
            print(f"  - {item}")
        print(f"\nTransitioning {ticket['key']} to BLOCKED...")
        jira.transition_ticket(ticket["key"], "BLOCKED")

        reporter = ticket.get("reporter")
        mention = (reporter["account_id"], reporter["display_name"]) if reporter else None
        lines = [
            "This ticket has been blocked by Agent ngn.",
            "The following required information was not found:",
        ] + [f"\u2022 {item}" for item in result["missing"]]
        jira.post_comment(ticket["key"], lines, mention=mention)
        print("Done.")


if __name__ == "__main__":
    main()
