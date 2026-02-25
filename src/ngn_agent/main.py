import os
import sys

from ngn_agent.jira import JiraClient

_REQUIRED_ENV = ("ANTHROPIC_API_KEY", "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_FILTER_ID")


def main() -> None:
    missing = [v for v in _REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        for var in missing:
            print(f"Error: {var} environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    filter_id = os.environ["JIRA_FILTER_ID"]
    client = JiraClient()

    print(f"Polling JIRA filter {filter_id}...")
    tickets = client.get_tickets_from_filter(filter_id)

    if not tickets:
        print("No candidate tickets found.")
        return

    print(f"\nFound {len(tickets)} ticket(s):\n")
    for t in tickets:
        assignee = t["assignee"] or "unassigned"
        priority = t["priority"] or "none"
        print(f"  {t['key']}  [{t['issue_type']}]  [{t['status']}]  ({priority})  {t['summary']}  — {assignee}")


if __name__ == "__main__":
    main()
