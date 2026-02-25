import os

import httpx

_PRIORITY_ORDER = {"Highest": 0, "High": 1, "Medium": 2, "Low": 3, "Lowest": 4}


class JiraClient:
    def __init__(self) -> None:
        self.base_url = os.environ["JIRA_BASE_URL"].rstrip("/")
        self.auth = (os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])
        self.headers = {"Accept": "application/json"}

    def get_ticket(self, key: str) -> dict:
        response = httpx.get(
            f"{self.base_url}/rest/api/3/issue/{key}",
            auth=self.auth,
            headers=self.headers,
            params={
                "fields": "summary,description,status,priority,assignee,issuetype,created,updated,labels,comment,parent",
            },
        )
        response.raise_for_status()
        return _extract_full_ticket(response.json())

    def get_tickets_from_filter(self, filter_id: str) -> list[dict]:
        response = httpx.post(
            f"{self.base_url}/rest/api/3/search/jql",
            auth=self.auth,
            headers={**self.headers, "Content-Type": "application/json"},
            json={
                "jql": f"filter={filter_id} AND issuetype in (Bug, Task, Story) AND status = READY",
                "fields": ["summary", "status", "priority", "assignee", "issuetype", "created"],
                "maxResults": 50,
            },
        )
        response.raise_for_status()
        tickets = [_extract_ticket(issue) for issue in response.json().get("issues", [])]
        tickets.sort(key=_sort_key)
        return tickets


def _extract_full_ticket(issue: dict) -> dict:
    fields = issue["fields"]
    parent = fields.get("parent")
    return {
        "key": issue["key"],
        "summary": fields["summary"],
        "issue_type": fields["issuetype"]["name"],
        "status": fields["status"]["name"],
        "priority": (fields.get("priority") or {}).get("name"),
        "assignee": (fields.get("assignee") or {}).get("displayName"),
        "created": fields["created"],
        "updated": fields["updated"],
        "labels": fields.get("labels", []),
        "parent": {"key": parent["key"], "summary": parent["fields"]["summary"]} if parent else None,
        "description": _adf_to_text(fields.get("description")),
        "comments": [
            {
                "author": (c.get("author") or {}).get("displayName"),
                "created": c["created"],
                "body": _adf_to_text(c.get("body")),
            }
            for c in (fields.get("comment") or {}).get("comments", [])
        ],
    }


def _adf_to_text(node: dict | None) -> str:
    if not node:
        return ""
    node_type = node.get("type")
    if node_type == "text":
        return node.get("text", "")
    if node_type == "hardBreak":
        return "\n"
    children = "".join(_adf_to_text(c) for c in node.get("content", []))
    if node_type in ("paragraph", "heading", "listItem", "blockquote", "codeBlock"):
        return children + "\n"
    return children


def _extract_ticket(issue: dict) -> dict:
    fields = issue["fields"]
    return {
        "key": issue["key"],
        "summary": fields["summary"],
        "issue_type": fields["issuetype"]["name"],
        "status": fields["status"]["name"],
        "priority": (fields.get("priority") or {}).get("name"),
        "assignee": (fields.get("assignee") or {}).get("displayName"),
        "created": fields["created"],
    }


def _sort_key(ticket: dict) -> tuple:
    type_rank = 0 if ticket["issue_type"] == "Bug" else 1
    priority_rank = _PRIORITY_ORDER.get(ticket["priority"], 2)
    return (type_rank, priority_rank, ticket["created"])
