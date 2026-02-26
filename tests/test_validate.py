"""Tests for validate_setup() in ngn_agent.jira and the --validate CLI flag in main.

Uses pytest-httpx to mock JIRA API calls so no real network traffic is made.
"""

import pytest
from pytest_httpx import HTTPXMock

from ngn_agent.jira import validate_setup

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

JIRA_URL = "https://test.atlassian.net"
FILTER_ID = "42"


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    """Set all required environment variables before each test."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("JIRA_BASE_URL", JIRA_URL)
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-token")
    monkeypatch.setenv("JIRA_FILTER_ID", FILTER_ID)


def _myself_response(display_name: str = "Jason Smith") -> dict:
    """Return a minimal /rest/api/3/myself JSON payload."""
    return {"displayName": display_name, "accountId": "acc-123"}


def _filter_response(name: str = "NGN agent work") -> dict:
    """Return a minimal /rest/api/3/filter/{id} JSON payload."""
    return {"id": FILTER_ID, "name": name}


def _issuetype_response(types: list[str] | None = None) -> list[dict]:
    """Return a /rest/api/3/issuetype payload containing the given type names."""
    if types is None:
        types = ["Bug", "Task", "Story", "Epic", "Sub-task"]
    return [{"id": str(i), "name": name} for i, name in enumerate(types)]


def _status_response(statuses: list[str] | None = None) -> list[dict]:
    """Return a /rest/api/3/status payload containing the given status names."""
    if statuses is None:
        statuses = ["READY", "IN PROGRESS", "IN REVIEW", "BLOCKED", "Done", "To Do"]
    return [{"id": str(i), "name": name} for i, name in enumerate(statuses)]


# ---------------------------------------------------------------------------
# test_validate_setup_all_pass
# ---------------------------------------------------------------------------

def test_validate_setup_all_pass(httpx_mock: HTTPXMock):
    """All five checks should pass when every API endpoint returns valid data."""
    httpx_mock.add_response(
        url=f"{JIRA_URL}/rest/api/3/myself",
        json=_myself_response("Jason Smith"),
    )
    httpx_mock.add_response(
        url=f"{JIRA_URL}/rest/api/3/filter/{FILTER_ID}",
        json=_filter_response("NGN agent work"),
    )
    httpx_mock.add_response(
        url=f"{JIRA_URL}/rest/api/3/issuetype",
        json=_issuetype_response(),
    )
    httpx_mock.add_response(
        url=f"{JIRA_URL}/rest/api/3/status",
        json=_status_response(),
    )

    results = validate_setup()

    assert len(results) == 5
    for result in results:
        assert result["passed"] is True, (
            f"Expected check '{result['name']}' to pass, but got detail: {result['detail']!r}"
        )


# ---------------------------------------------------------------------------
# test_validate_setup_auth_failure_skips_remaining
# ---------------------------------------------------------------------------

def test_validate_setup_auth_failure_skips_remaining(httpx_mock: HTTPXMock):
    """When /myself returns 401, the auth check fails and later checks are skipped."""
    httpx_mock.add_response(
        url=f"{JIRA_URL}/rest/api/3/myself",
        status_code=401,
        json={"errorMessages": ["Unauthorized"]},
    )

    results = validate_setup()

    # There should be 5 results total: env vars + auth + 3 skipped.
    assert len(results) == 5

    env_result = results[0]
    assert env_result["name"] == "Environment variables"
    assert env_result["passed"] is True

    auth_result = results[1]
    assert auth_result["name"] == "JIRA authentication"
    assert auth_result["passed"] is False

    # Checks after auth must be skipped (not attempted — no further HTTP requests
    # should have been made).
    skipped_names = [r["name"] for r in results[2:]]
    assert "Filter accessible" in skipped_names
    assert "Issue types" in skipped_names
    assert "Statuses" in skipped_names

    for r in results[2:]:
        assert r["passed"] is False
        assert r["detail"] == "skipped"

    # Confirm no extra requests were made beyond the /myself call.
    requests_made = httpx_mock.get_requests()
    assert len(requests_made) == 1
    assert "/myself" in str(requests_made[0].url)


# ---------------------------------------------------------------------------
# test_validate_setup_filter_not_found
# ---------------------------------------------------------------------------

def test_validate_setup_filter_not_found(httpx_mock: HTTPXMock):
    """When the filter endpoint returns 404, the filter check should fail."""
    httpx_mock.add_response(
        url=f"{JIRA_URL}/rest/api/3/myself",
        json=_myself_response(),
    )
    httpx_mock.add_response(
        url=f"{JIRA_URL}/rest/api/3/filter/{FILTER_ID}",
        status_code=404,
        json={"errorMessages": ["Filter not found"]},
    )
    # Issue type and status checks still run after a filter failure.
    httpx_mock.add_response(
        url=f"{JIRA_URL}/rest/api/3/issuetype",
        json=_issuetype_response(),
    )
    httpx_mock.add_response(
        url=f"{JIRA_URL}/rest/api/3/status",
        json=_status_response(),
    )

    results = validate_setup()

    filter_result = next(r for r in results if r["name"] == "Filter accessible")
    assert filter_result["passed"] is False
    assert "404" in filter_result["detail"]


# ---------------------------------------------------------------------------
# test_validate_setup_missing_issue_type
# ---------------------------------------------------------------------------

def test_validate_setup_missing_issue_type(httpx_mock: HTTPXMock):
    """When Story is absent from /issuetype, the check should fail and mention Story."""
    httpx_mock.add_response(
        url=f"{JIRA_URL}/rest/api/3/myself",
        json=_myself_response(),
    )
    httpx_mock.add_response(
        url=f"{JIRA_URL}/rest/api/3/filter/{FILTER_ID}",
        json=_filter_response(),
    )
    # Return types that include Bug and Task but NOT Story.
    httpx_mock.add_response(
        url=f"{JIRA_URL}/rest/api/3/issuetype",
        json=_issuetype_response(types=["Bug", "Task", "Epic"]),
    )
    httpx_mock.add_response(
        url=f"{JIRA_URL}/rest/api/3/status",
        json=_status_response(),
    )

    results = validate_setup()

    issuetype_result = next(r for r in results if r["name"] == "Issue types")
    assert issuetype_result["passed"] is False
    assert "Story" in issuetype_result["detail"]


# ---------------------------------------------------------------------------
# test_validate_setup_missing_status
# ---------------------------------------------------------------------------

def test_validate_setup_missing_status(httpx_mock: HTTPXMock):
    """When BLOCKED is absent from /status, the check should fail and mention BLOCKED."""
    httpx_mock.add_response(
        url=f"{JIRA_URL}/rest/api/3/myself",
        json=_myself_response(),
    )
    httpx_mock.add_response(
        url=f"{JIRA_URL}/rest/api/3/filter/{FILTER_ID}",
        json=_filter_response(),
    )
    httpx_mock.add_response(
        url=f"{JIRA_URL}/rest/api/3/issuetype",
        json=_issuetype_response(),
    )
    # Return statuses that are missing BLOCKED.
    httpx_mock.add_response(
        url=f"{JIRA_URL}/rest/api/3/status",
        json=_status_response(statuses=["READY", "IN PROGRESS", "IN REVIEW", "Done"]),
    )

    results = validate_setup()

    status_result = next(r for r in results if r["name"] == "Statuses")
    assert status_result["passed"] is False
    assert "BLOCKED" in status_result["detail"]


# ---------------------------------------------------------------------------
# test_validate_setup_missing_env_vars
# ---------------------------------------------------------------------------

def test_validate_setup_missing_env_vars(monkeypatch):
    """When a required env var is absent, the env check fails and all API checks are skipped."""
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    monkeypatch.delenv("JIRA_FILTER_ID", raising=False)

    results = validate_setup()

    assert len(results) == 5

    env_result = results[0]
    assert env_result["name"] == "Environment variables"
    assert env_result["passed"] is False
    # Both missing vars should appear in the detail.
    assert "JIRA_API_TOKEN" in env_result["detail"]
    assert "JIRA_FILTER_ID" in env_result["detail"]

    # All subsequent checks are skipped.
    for r in results[1:]:
        assert r["passed"] is False
        assert r["detail"] == "skipped"


# ---------------------------------------------------------------------------
# CLI --validate integration tests
# ---------------------------------------------------------------------------

def test_cli_validate_exits_zero_on_all_pass(httpx_mock: HTTPXMock, capsys):
    """--validate should exit 0 and print ✓ lines when all checks pass."""
    httpx_mock.add_response(url=f"{JIRA_URL}/rest/api/3/myself", json=_myself_response("Jason Smith"))
    httpx_mock.add_response(url=f"{JIRA_URL}/rest/api/3/filter/{FILTER_ID}", json=_filter_response("NGN agent work"))
    httpx_mock.add_response(url=f"{JIRA_URL}/rest/api/3/issuetype", json=_issuetype_response())
    httpx_mock.add_response(url=f"{JIRA_URL}/rest/api/3/status", json=_status_response())

    from ngn_agent.main import _run_validate
    with pytest.raises(SystemExit) as exc_info:
        _run_validate()

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "✓ Environment variables" in out
    assert "✓ JIRA authentication (Jason Smith)" in out
    assert "✓ Filter accessible (NGN agent work)" in out
    assert "⚠" in out  # transitions warning always present


def test_cli_validate_exits_one_on_failure(httpx_mock: HTTPXMock, capsys):
    """--validate should exit 1 when any check fails."""
    httpx_mock.add_response(url=f"{JIRA_URL}/rest/api/3/myself", status_code=401)

    from ngn_agent.main import _run_validate
    with pytest.raises(SystemExit) as exc_info:
        _run_validate()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "✗" in out


def test_cli_validate_transitions_warning_always_printed(httpx_mock: HTTPXMock, capsys):
    """The transitions warning line should always appear, even when all checks pass."""
    httpx_mock.add_response(url=f"{JIRA_URL}/rest/api/3/myself", json=_myself_response())
    httpx_mock.add_response(url=f"{JIRA_URL}/rest/api/3/filter/{FILTER_ID}", json=_filter_response())
    httpx_mock.add_response(url=f"{JIRA_URL}/rest/api/3/issuetype", json=_issuetype_response())
    httpx_mock.add_response(url=f"{JIRA_URL}/rest/api/3/status", json=_status_response())

    from ngn_agent.main import _run_validate
    with pytest.raises(SystemExit):
        _run_validate()

    out = capsys.readouterr().out
    assert "⚠ Transitions:" in out
    assert "IN PROGRESS" in out
    assert "IN REVIEW" in out
    assert "BLOCKED" in out
