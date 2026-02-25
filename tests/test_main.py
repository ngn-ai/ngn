"""Tests for the ngn_agent.main module.

Focus on the polling loop behaviour: that poll_once is called repeatedly and
that the agent sleeps for the remainder of each interval.
"""

from unittest.mock import MagicMock, patch

import pytest

from ngn_agent.main import _POLL_INTERVAL, poll_once


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jira(tickets=None):
    """Return a mock JiraClient that returns *tickets* from get_tickets_from_filter."""
    jira = MagicMock()
    jira.get_tickets_from_filter.return_value = tickets or []
    return jira


def _make_claude():
    return MagicMock()


# ---------------------------------------------------------------------------
# poll_once — no tickets
# ---------------------------------------------------------------------------

def test_poll_once_no_tickets_does_not_call_get_ticket(monkeypatch):
    """When the filter returns no tickets, poll_once should be a no-op."""
    monkeypatch.setenv("JIRA_FILTER_ID", "99")
    jira = _make_jira(tickets=[])
    claude = _make_claude()

    poll_once(jira, claude)

    jira.get_ticket.assert_not_called()


# ---------------------------------------------------------------------------
# Polling loop — rate limiting
# ---------------------------------------------------------------------------

def test_main_loop_sleeps_for_remaining_interval(monkeypatch):
    """The loop should sleep for (_POLL_INTERVAL - elapsed) after each iteration.

    We let two iterations run before breaking out via a side-effect on sleep.
    """
    monkeypatch.setenv("JIRA_FILTER_ID", "99")
    monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "a@b.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")

    poll_call_count = 0

    def fake_poll_once(jira, claude):
        nonlocal poll_call_count
        poll_call_count += 1
        # Stop after the second poll by raising SystemExit from within the loop.
        if poll_call_count >= 2:
            raise SystemExit(0)

    sleep_calls = []

    def fake_sleep(duration):
        sleep_calls.append(duration)

    # monotonic returns a sequence: 0.0 (loop start), 5.0 (after poll) to
    # simulate that the poll itself took 5 seconds.
    monotonic_values = iter([0.0, 5.0, 0.0, 5.0, 0.0, 5.0])

    with patch("ngn_agent.main.poll_once", side_effect=fake_poll_once), \
         patch("ngn_agent.main.time.sleep", side_effect=fake_sleep), \
         patch("ngn_agent.main.time.monotonic", side_effect=monotonic_values), \
         patch("ngn_agent.main.JiraClient"), \
         patch("ngn_agent.main.anthropic.Anthropic"):
        from ngn_agent.main import main
        with pytest.raises(SystemExit):
            main()

    # Should have slept once (after the first poll; the second raises SystemExit).
    assert len(sleep_calls) == 1
    # Elapsed was 5.0 s, so sleep should be _POLL_INTERVAL - 5.0.
    assert sleep_calls[0] == pytest.approx(_POLL_INTERVAL - 5.0)


def test_main_loop_skips_sleep_when_iteration_exceeds_interval(monkeypatch):
    """If a poll takes longer than _POLL_INTERVAL, sleep is skipped (no negative sleep)."""
    monkeypatch.setenv("JIRA_FILTER_ID", "99")
    monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "a@b.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")

    call_count = 0

    def fake_poll_once(jira, claude):
        nonlocal call_count
        call_count += 1
        if call_count >= 1:
            raise SystemExit(0)

    sleep_calls = []

    # Simulate poll taking 60 s — longer than the 30 s interval.
    monotonic_values = iter([0.0, 60.0])

    with patch("ngn_agent.main.poll_once", side_effect=fake_poll_once), \
         patch("ngn_agent.main.time.sleep", side_effect=sleep_calls.append), \
         patch("ngn_agent.main.time.monotonic", side_effect=monotonic_values), \
         patch("ngn_agent.main.JiraClient"), \
         patch("ngn_agent.main.anthropic.Anthropic"):
        from ngn_agent.main import main
        with pytest.raises(SystemExit):
            main()

    # sleep should never have been called (wait would be negative).
    assert sleep_calls == []


def test_main_loop_calls_poll_once_repeatedly(monkeypatch):
    """main() should call poll_once on every iteration of the loop."""
    monkeypatch.setenv("JIRA_FILTER_ID", "99")
    monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "a@b.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")

    call_count = 0

    def fake_poll_once(jira, claude):
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            raise SystemExit(0)

    # Enough monotonic values for 3 iterations (start + end per iteration).
    monotonic_values = iter([0.0, 0.0] * 4)

    with patch("ngn_agent.main.poll_once", side_effect=fake_poll_once), \
         patch("ngn_agent.main.time.sleep"), \
         patch("ngn_agent.main.time.monotonic", side_effect=monotonic_values), \
         patch("ngn_agent.main.JiraClient"), \
         patch("ngn_agent.main.anthropic.Anthropic"):
        from ngn_agent.main import main
        with pytest.raises(SystemExit):
            main()

    assert call_count == 3
