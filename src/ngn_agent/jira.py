import os

import httpx

_PRIORITY_ORDER = {"Highest": 0, "High": 1, "Medium": 2, "Low": 3, "Lowest": 4}

# Environment variables required for the agent to operate.
_REQUIRED_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "JIRA_BASE_URL",
    "JIRA_EMAIL",
    "JIRA_API_TOKEN",
    "JIRA_FILTER_ID",
)

# Issue types the agent must be able to process.
_REQUIRED_ISSUE_TYPES = {"Bug", "Task", "Story"}

# Workflow statuses the agent depends on.
_REQUIRED_STATUSES = {"READY", "IN PROGRESS", "IN REVIEW", "BLOCKED"}


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


def validate_setup() -> list[dict]:
    """Perform pre-flight checks against the configured JIRA instance.

    Runs a series of checks in order and returns a list of result dicts.
    Each dict contains:
        - ``name``   (str):  Human-readable name for the check.
        - ``passed`` (bool): Whether the check succeeded.
        - ``detail`` (str):  Additional information (e.g. authenticated user,
          filter name, or error description).

    Checks are performed in this order:

    1. **Environment variables** — verifies all five required env vars are set
       (no API call is made).
    2. **JIRA authentication** — ``GET /rest/api/3/myself``; reports the
       authenticated user's display name on success.
    3. **Filter accessible** — ``GET /rest/api/3/filter/{JIRA_FILTER_ID}``;
       reports the filter name on success.
    4. **Issue types** — ``GET /rest/api/3/issuetype``; confirms Bug, Task,
       and Story are present globally.
    5. **Statuses** — ``GET /rest/api/3/status``; confirms READY, IN PROGRESS,
       IN REVIEW, and BLOCKED are present globally.

    If the authentication check fails, all subsequent checks are skipped and
    marked with ``passed=False`` and a detail of ``"skipped"``.

    Returns:
        A list of result dicts, one per check.
    """
    results: list[dict] = []

    # ------------------------------------------------------------------
    # Check 1: Environment variables (no network call required)
    # ------------------------------------------------------------------
    missing_vars = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing_vars:
        results.append({
            "name": "Environment variables",
            "passed": False,
            "detail": f"missing: {', '.join(missing_vars)}",
        })
        # Without credentials we cannot make any API calls — skip everything.
        results.extend(_skipped_checks(["JIRA authentication", "Filter accessible", "Issue types", "Statuses"]))
        return results

    results.append({
        "name": "Environment variables",
        "passed": True,
        "detail": "all required variables are set",
    })

    base_url = os.environ["JIRA_BASE_URL"].rstrip("/")
    auth = (os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])
    headers = {"Accept": "application/json"}

    # ------------------------------------------------------------------
    # Check 2: JIRA authentication — GET /rest/api/3/myself
    # ------------------------------------------------------------------
    try:
        response = httpx.get(
            f"{base_url}/rest/api/3/myself",
            auth=auth,
            headers=headers,
        )
        response.raise_for_status()
        display_name = response.json().get("displayName", "unknown")
        results.append({
            "name": "JIRA authentication",
            "passed": True,
            "detail": display_name,
        })
    except httpx.HTTPStatusError as exc:
        results.append({
            "name": "JIRA authentication",
            "passed": False,
            "detail": f"HTTP {exc.response.status_code}",
        })
        # Cannot proceed with further API checks if auth failed.
        results.extend(_skipped_checks(["Filter accessible", "Issue types", "Statuses"]))
        return results
    except Exception as exc:
        results.append({
            "name": "JIRA authentication",
            "passed": False,
            "detail": str(exc),
        })
        results.extend(_skipped_checks(["Filter accessible", "Issue types", "Statuses"]))
        return results

    filter_id = os.environ["JIRA_FILTER_ID"]

    # ------------------------------------------------------------------
    # Check 3: Filter accessible — GET /rest/api/3/filter/{filter_id}
    # ------------------------------------------------------------------
    try:
        response = httpx.get(
            f"{base_url}/rest/api/3/filter/{filter_id}",
            auth=auth,
            headers=headers,
        )
        response.raise_for_status()
        filter_name = response.json().get("name", "unknown")
        results.append({
            "name": "Filter accessible",
            "passed": True,
            "detail": filter_name,
        })
    except httpx.HTTPStatusError as exc:
        results.append({
            "name": "Filter accessible",
            "passed": False,
            "detail": f"HTTP {exc.response.status_code}",
        })
    except Exception as exc:
        results.append({
            "name": "Filter accessible",
            "passed": False,
            "detail": str(exc),
        })

    # ------------------------------------------------------------------
    # Check 4: Issue types — GET /rest/api/3/issuetype
    # ------------------------------------------------------------------
    try:
        response = httpx.get(
            f"{base_url}/rest/api/3/issuetype",
            auth=auth,
            headers=headers,
        )
        response.raise_for_status()
        found_types = {t["name"] for t in response.json()}
        missing_types = sorted(_REQUIRED_ISSUE_TYPES - found_types)
        if missing_types:
            results.append({
                "name": "Issue types",
                "passed": False,
                "detail": (
                    ", ".join(
                        f"{t} {'found' if t in found_types else 'not found'}"
                        for t in sorted(_REQUIRED_ISSUE_TYPES)
                    )
                ),
            })
        else:
            results.append({
                "name": "Issue types",
                "passed": True,
                "detail": ", ".join(sorted(_REQUIRED_ISSUE_TYPES)),
            })
    except Exception as exc:
        results.append({
            "name": "Issue types",
            "passed": False,
            "detail": str(exc),
        })

    # ------------------------------------------------------------------
    # Check 5: Statuses — GET /rest/api/3/status
    # ------------------------------------------------------------------
    try:
        response = httpx.get(
            f"{base_url}/rest/api/3/status",
            auth=auth,
            headers=headers,
        )
        response.raise_for_status()
        # Status names are compared case-insensitively to be robust, but we
        # report the required names as specified (upper-case).
        found_names_upper = {s["name"].upper() for s in response.json()}
        status_details = []
        all_found = True
        for status in sorted(_REQUIRED_STATUSES):
            if status.upper() in found_names_upper:
                status_details.append(f"{status} found")
            else:
                status_details.append(f"{status} not found")
                all_found = False
        results.append({
            "name": "Statuses",
            "passed": all_found,
            "detail": ", ".join(status_details),
        })
    except Exception as exc:
        results.append({
            "name": "Statuses",
            "passed": False,
            "detail": str(exc),
        })

    return results


def _skipped_checks(names: list[str]) -> list[dict]:
    """Return a list of skipped check result dicts for the given check *names*.

    Used when an earlier check failure means subsequent checks cannot be run
    (e.g. authentication failed so API calls are pointless).

    Args:
        names: Ordered list of check names to mark as skipped.

    Returns:
        A list of result dicts with ``passed=False`` and ``detail="skipped"``.
    """
    return [{"name": name, "passed": False, "detail": "skipped"} for name in names]


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
