import json

import pytest
from pytest_httpx import HTTPXMock

from ngn_agent.jira import JiraClient, _adf_to_text, _build_comment_adf, _extract_full_ticket, _extract_ticket, _sort_key

JIRA_URL = "https://test.atlassian.net"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", JIRA_URL)
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-token")
    return JiraClient()


def _make_issue(key, issue_type, priority, created, assignee=None):
    return {
        "key": key,
        "fields": {
            "summary": f"Summary for {key}",
            "issuetype": {"name": issue_type},
            "status": {"name": "READY"},
            "priority": {"name": priority} if priority else None,
            "assignee": {"displayName": assignee} if assignee else None,
            "created": created,
        },
    }


# --- _sort_key ---

def test_sort_bug_before_task():
    bug = {"issue_type": "Bug", "priority": "Medium", "created": "2024-01-01T00:00:00.000+0000"}
    task = {"issue_type": "Task", "priority": "Medium", "created": "2024-01-01T00:00:00.000+0000"}
    assert _sort_key(bug) < _sort_key(task)


def test_sort_bug_before_story():
    bug = {"issue_type": "Bug", "priority": "Medium", "created": "2024-01-01T00:00:00.000+0000"}
    story = {"issue_type": "Story", "priority": "Medium", "created": "2024-01-01T00:00:00.000+0000"}
    assert _sort_key(bug) < _sort_key(story)


def test_sort_high_priority_before_low():
    high = {"issue_type": "Task", "priority": "High", "created": "2024-01-01T00:00:00.000+0000"}
    low = {"issue_type": "Task", "priority": "Low", "created": "2024-01-01T00:00:00.000+0000"}
    assert _sort_key(high) < _sort_key(low)


def test_sort_oldest_first():
    old = {"issue_type": "Task", "priority": "Medium", "created": "2024-01-01T00:00:00.000+0000"}
    new = {"issue_type": "Task", "priority": "Medium", "created": "2024-06-01T00:00:00.000+0000"}
    assert _sort_key(old) < _sort_key(new)


def test_sort_type_beats_priority():
    # A low-priority bug should rank above a high-priority task
    bug_low = {"issue_type": "Bug", "priority": "Low", "created": "2024-01-01T00:00:00.000+0000"}
    task_high = {"issue_type": "Task", "priority": "High", "created": "2024-01-01T00:00:00.000+0000"}
    assert _sort_key(bug_low) < _sort_key(task_high)


def test_sort_priority_beats_age():
    # A high-priority newer ticket should rank above a low-priority older one of the same type
    high_new = {"issue_type": "Task", "priority": "High", "created": "2024-06-01T00:00:00.000+0000"}
    low_old = {"issue_type": "Task", "priority": "Low", "created": "2024-01-01T00:00:00.000+0000"}
    assert _sort_key(high_new) < _sort_key(low_old)


def test_sort_tasks_and_stories_interleaved_by_priority():
    # Tasks and Stories are equal rank — a high-priority Story beats a low-priority Task
    story_high = {"issue_type": "Story", "priority": "High", "created": "2024-01-01T00:00:00.000+0000"}
    task_low = {"issue_type": "Task", "priority": "Low", "created": "2024-01-01T00:00:00.000+0000"}
    assert _sort_key(story_high) < _sort_key(task_low)


# --- _extract_ticket ---

def test_extract_ticket_full():
    issue = _make_issue("PROJ-1", "Bug", "High", "2024-01-01T00:00:00.000+0000", assignee="Alice")
    ticket = _extract_ticket(issue)
    assert ticket == {
        "key": "PROJ-1",
        "summary": "Summary for PROJ-1",
        "issue_type": "Bug",
        "status": "READY",
        "priority": "High",
        "assignee": "Alice",
        "created": "2024-01-01T00:00:00.000+0000",
    }


def test_extract_ticket_null_assignee_and_priority():
    issue = _make_issue("PROJ-1", "Task", None, "2024-01-01T00:00:00.000+0000")
    ticket = _extract_ticket(issue)
    assert ticket["assignee"] is None
    assert ticket["priority"] is None


# --- get_tickets_from_filter ---

def test_get_tickets_sorted(client, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json={
            "issues": [
                _make_issue("PROJ-2", "Task", "High", "2024-01-01T00:00:00.000+0000"),
                _make_issue("PROJ-1", "Bug", "Low", "2024-06-01T00:00:00.000+0000"),
            ]
        }
    )
    tickets = client.get_tickets_from_filter("42")
    # Bug beats Task regardless of priority/age
    assert [t["key"] for t in tickets] == ["PROJ-1", "PROJ-2"]


def test_get_tickets_empty(client, httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"issues": []})
    assert client.get_tickets_from_filter("42") == []


def test_get_tickets_jql(client, httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"issues": []})
    client.get_tickets_from_filter("42")
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert "filter=42" in body["jql"]
    assert "issuetype in (Bug, Task, Story)" in body["jql"]
    assert "status = READY" in body["jql"]


def test_get_tickets_auth(client, httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"issues": []})
    client.get_tickets_from_filter("42")
    request = httpx_mock.get_requests()[0]
    assert request.headers["authorization"].startswith("Basic ")


# --- _adf_to_text ---

def test_adf_to_text_simple_paragraph():
    node = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Hello world"}]},
    ]}
    assert _adf_to_text(node) == "Hello world\n"


def test_adf_to_text_multiple_paragraphs():
    node = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "First"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "Second"}]},
    ]}
    assert _adf_to_text(node) == "First\nSecond\n"


def test_adf_to_text_hard_break():
    node = {"type": "paragraph", "content": [
        {"type": "text", "text": "Line one"},
        {"type": "hardBreak"},
        {"type": "text", "text": "Line two"},
    ]}
    assert _adf_to_text(node) == "Line one\nLine two\n"


def test_adf_to_text_none():
    assert _adf_to_text(None) == ""


# --- _extract_full_ticket ---

def _make_full_issue(key="PROJ-1", description=None, comments=None, parent=None, labels=None):
    return {
        "key": key,
        "fields": {
            "summary": f"Summary for {key}",
            "issuetype": {"name": "Task"},
            "status": {"name": "READY"},
            "priority": {"name": "High"},
            "assignee": {"displayName": "Alice"},
            "reporter": {"accountId": "acc-123", "displayName": "Bob"},
            "created": "2024-01-01T00:00:00.000+0000",
            "updated": "2024-06-01T00:00:00.000+0000",
            "labels": labels or [],
            "parent": parent,
            "description": description,
            "comment": {"comments": comments or []},
        },
    }


def test_extract_full_ticket_basic():
    ticket = _extract_full_ticket(_make_full_issue())
    assert ticket["key"] == "PROJ-1"
    assert ticket["summary"] == "Summary for PROJ-1"
    assert ticket["updated"] == "2024-06-01T00:00:00.000+0000"
    assert ticket["labels"] == []
    assert ticket["parent"] is None
    assert ticket["description"] == ""
    assert ticket["comments"] == []
    assert ticket["reporter"] == {"account_id": "acc-123", "display_name": "Bob"}


def test_extract_full_ticket_with_description():
    description = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Do the thing"}]},
    ]}
    ticket = _extract_full_ticket(_make_full_issue(description=description))
    assert ticket["description"] == "Do the thing\n"


def test_extract_full_ticket_with_parent():
    parent = {"key": "EPIC-1", "fields": {"summary": "The epic"}}
    ticket = _extract_full_ticket(_make_full_issue(parent=parent))
    assert ticket["parent"] == {"key": "EPIC-1", "summary": "The epic"}


def test_extract_full_ticket_with_comments():
    comments = [
        {
            "author": {"displayName": "Bob"},
            "created": "2024-03-01T00:00:00.000+0000",
            "body": {"type": "doc", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Looks good"}]},
            ]},
        }
    ]
    ticket = _extract_full_ticket(_make_full_issue(comments=comments))
    assert len(ticket["comments"]) == 1
    assert ticket["comments"][0] == {
        "author": "Bob",
        "created": "2024-03-01T00:00:00.000+0000",
        "body": "Looks good\n",
    }


def test_extract_full_ticket_labels():
    ticket = _extract_full_ticket(_make_full_issue(labels=["backend", "auth"]))
    assert ticket["labels"] == ["backend", "auth"]


# --- get_ticket ---

def test_get_ticket(client, httpx_mock: HTTPXMock):
    httpx_mock.add_response(json=_make_full_issue("PROJ-99"))
    ticket = client.get_ticket("PROJ-99")
    assert ticket["key"] == "PROJ-99"
    assert ticket["summary"] == "Summary for PROJ-99"


def test_get_ticket_url(client, httpx_mock: HTTPXMock):
    httpx_mock.add_response(json=_make_full_issue("PROJ-99"))
    client.get_ticket("PROJ-99")
    request = httpx_mock.get_requests()[0]
    assert request.url.path == "/rest/api/3/issue/PROJ-99"


# --- transition_ticket ---

def test_transition_ticket(client, httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"transitions": [
        {"id": "11", "name": "In Progress"},
        {"id": "21", "name": "Blocked"},
        {"id": "31", "name": "Done"},
    ]})
    httpx_mock.add_response(status_code=204)

    client.transition_ticket("PROJ-1", "Blocked")

    requests = httpx_mock.get_requests()
    assert requests[0].url.path == "/rest/api/3/issue/PROJ-1/transitions"
    assert requests[0].method == "GET"
    assert requests[1].url.path == "/rest/api/3/issue/PROJ-1/transitions"
    assert requests[1].method == "POST"
    assert json.loads(requests[1].content) == {"transition": {"id": "21"}}


def test_transition_ticket_case_insensitive(client, httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"transitions": [{"id": "21", "name": "Blocked"}]})
    httpx_mock.add_response(status_code=204)
    client.transition_ticket("PROJ-1", "blocked")
    assert json.loads(httpx_mock.get_requests()[1].content) == {"transition": {"id": "21"}}


def test_transition_ticket_not_found(client, httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"transitions": [
        {"id": "11", "name": "In Progress"},
        {"id": "31", "name": "Done"},
    ]})
    with pytest.raises(ValueError, match="No transition named 'Blocked'"):
        client.transition_ticket("PROJ-1", "Blocked")


# --- _build_comment_adf ---

def test_build_comment_adf_lines_only():
    adf = _build_comment_adf(["Line one", "Line two"])
    assert adf["type"] == "doc"
    assert len(adf["content"]) == 2
    assert adf["content"][0] == {"type": "paragraph", "content": [{"type": "text", "text": "Line one"}]}
    assert adf["content"][1] == {"type": "paragraph", "content": [{"type": "text", "text": "Line two"}]}


def test_build_comment_adf_with_mention():
    adf = _build_comment_adf(["Blocked."], mention=("acc-123", "Bob"))
    assert len(adf["content"]) == 2
    mention_para = adf["content"][1]
    assert mention_para["content"][0] == {"type": "mention", "attrs": {"id": "acc-123", "text": "@Bob"}}
    assert mention_para["content"][1]["text"].startswith(" please update")


# --- post_comment ---

def test_post_comment(client, httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=201, json={})
    client.post_comment("PROJ-1", ["Blocked by Agent ngn."])
    request = httpx_mock.get_requests()[0]
    assert request.url.path == "/rest/api/3/issue/PROJ-1/comment"
    assert request.method == "POST"
    body = json.loads(request.content)
    assert body["body"]["type"] == "doc"
    assert body["body"]["content"][0]["content"][0]["text"] == "Blocked by Agent ngn."


def test_post_comment_with_mention(client, httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=201, json={})
    client.post_comment("PROJ-1", ["Blocked."], mention=("acc-123", "Bob"))
    body = json.loads(httpx_mock.get_requests()[0].content)
    last_para = body["body"]["content"][-1]["content"]
    assert last_para[0]["type"] == "mention"
    assert last_para[0]["attrs"]["id"] == "acc-123"
