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
                "fields": "summary,description,status,priority,assignee,reporter,issuetype,created,updated,labels,comment,parent",
            },
        )
        response.raise_for_status()
        return _extract_full_ticket(response.json())

    def transition_ticket(self, key: str, status_name: str) -> None:
        response = httpx.get(
            f"{self.base_url}/rest/api/3/issue/{key}/transitions",
            auth=self.auth,
            headers=self.headers,
        )
        response.raise_for_status()
        transitions = response.json().get("transitions", [])
        match = next(
            (t for t in transitions if t["name"].lower() == status_name.lower()),
            None,
        )
        if match is None:
            available = [t["name"] for t in transitions]
            raise ValueError(f"No transition named {status_name!r} for {key}. Available: {available}")
        response = httpx.post(
            f"{self.base_url}/rest/api/3/issue/{key}/transitions",
            auth=self.auth,
            headers={**self.headers, "Content-Type": "application/json"},
            json={"transition": {"id": match["id"]}},
        )
        response.raise_for_status()

    def post_comment(self, key: str, lines: list[str], mention: tuple[str, str] | None = None) -> None:
        response = httpx.post(
            f"{self.base_url}/rest/api/3/issue/{key}/comment",
            auth=self.auth,
            headers={**self.headers, "Content-Type": "application/json"},
            json={"body": _build_comment_adf(lines, mention)},
        )
        response.raise_for_status()

    def add_label(self, key: str, label: str) -> None:
        response = httpx.put(
            f"{self.base_url}/rest/api/3/issue/{key}",
            auth=self.auth,
            headers={**self.headers, "Content-Type": "application/json"},
            json={"update": {"labels": [{"add": label}]}},
        )
        response.raise_for_status()

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
        "reporter": (
            {"account_id": fields["reporter"]["accountId"], "display_name": fields["reporter"]["displayName"]}
            if fields.get("reporter") else None
        ),
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


def _build_comment_adf(lines: list[str], mention: tuple[str, str] | None = None) -> dict:
    content = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]}
        for line in lines
    ]
    if mention:
        account_id, display_name = mention
        content.append({
            "type": "paragraph",
            "content": [
                {"type": "mention", "attrs": {"id": account_id, "text": f"@{display_name}"}},
                {"type": "text", "text": " please update the ticket and return it to Ready when complete."},
            ],
        })
    return {"version": 1, "type": "doc", "content": content}


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
