import logging
import time

import anthropic

log = logging.getLogger(__name__)

_MAX_RETRIES = 60
_RETRY_DELAY = 60

_SYSTEM_PROMPT = """You are validating a JIRA ticket to determine whether it contains sufficient information for an autonomous coding agent to implement it.

Check for ALL of the following required elements:

1. Repository URL — a Git URL or reference indicating where the code lives (may appear in any field or in the description text)
2. Current context/behavior — a description of how things work today (the "as-is" state or background)
3. Desired outcome — what the implementation should achieve, including any technical requirements
4. Test requirements — what tests are expected, or how the implementation should be verified

Call submit_validation with your findings."""

_VALIDATION_TOOL = {
    "name": "submit_validation",
    "description": "Submit the validation result for a JIRA ticket",
    "input_schema": {
        "type": "object",
        "properties": {
            "valid": {
                "type": "boolean",
                "description": "True only if all four required elements are present",
            },
            "repo_url": {
                "type": "string",
                "description": "The repository URL extracted from the ticket, if found",
            },
            "missing": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Names of required elements that are absent or insufficient",
            },
        },
        "required": ["valid", "missing"],
    },
}


def validate_ticket(ticket: dict, client: anthropic.Anthropic, ancestors: list[dict] | None = None) -> dict:
    content = _format_ticket(ticket)
    if ancestors:
        ancestor_sections = "\n\n---\n\n".join(_format_ticket(a) for a in ancestors)
        content = f"Ancestor ticket context (outermost to innermost):\n\n{ancestor_sections}\n\n---\n\nTicket to validate:\n{content}"
    for attempt in range(_MAX_RETRIES):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                tools=[_VALIDATION_TOOL],
                tool_choice={"type": "tool", "name": "submit_validation"},
                messages=[{"role": "user", "content": content}],
            )
            break
        except anthropic.APIStatusError as exc:
            if exc.status_code not in (429, 529):
                raise
            if attempt == _MAX_RETRIES - 1:
                raise
            log.warning("API unavailable (%s), will retry in %ss (attempt %s/%s)...", exc.status_code, _RETRY_DELAY, attempt + 1, _MAX_RETRIES)
            time.sleep(_RETRY_DELAY)
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_validation":
            return block.input
    raise RuntimeError("Claude did not return a validation result")


def _format_ticket(ticket: dict) -> str:
    lines = [
        f"Key: {ticket['key']}",
        f"Type: {ticket['issue_type']}",
        f"Summary: {ticket['summary']}",
        f"Priority: {ticket.get('priority') or 'none'}",
    ]
    if ticket.get("parent"):
        lines.append(f"Parent: {ticket['parent']['key']} — {ticket['parent']['summary']}")
    if ticket.get("labels"):
        lines.append(f"Labels: {', '.join(ticket['labels'])}")
    lines.append(f"\nDescription:\n{ticket.get('description') or '(none)'}")
    if ticket.get("comments"):
        lines.append("\nComments:")
        for c in ticket["comments"]:
            lines.append(f"  [{c['created']}] {c['author']}:\n  {c['body']}")
    return "\n".join(lines)
