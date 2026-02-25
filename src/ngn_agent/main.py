import os
import sys

import anthropic

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

    print("Validating ticket with Claude...")
    result = validate_ticket(ticket, claude)

    if result["valid"]:
        repo_url = result.get("repo_url", "(not extracted)")
        print(f"\n✓ Ticket is valid. Ready to proceed.")
        print(f"  Repo: {repo_url}")
    else:
        print(f"\n✗ Ticket is missing required information:")
        for item in result["missing"]:
            print(f"  - {item}")
        print(f"\nTransitioning {ticket['key']} to Blocked...")
        jira.transition_ticket(ticket["key"], "Blocked")

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
