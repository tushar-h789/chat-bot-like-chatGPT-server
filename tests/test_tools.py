from datetime import UTC, datetime

from app.services.ai.tools import run_tool


def test_calculate_evaluates_arithmetic() -> None:
    outcome = run_tool("calculate", {"expression": "(17 + 3) * 2"})

    assert outcome.is_error is False
    assert outcome.result == "40"


def test_calculate_rejects_names_and_calls() -> None:
    for expression in ("os.system('rm')", "__import__('os')", "2 ** 8", "nine"):
        outcome = run_tool("calculate", {"expression": expression})
        assert outcome.is_error is True
        assert outcome.result == "The expression could not be calculated."


def test_current_time_uses_the_injected_clock() -> None:
    moment = datetime(2026, 10, 7, 1, 2, 3, tzinfo=UTC)

    outcome = run_tool("current_time", {}, now=moment)

    assert outcome.result == "2026-10-07T01:02:03Z"
    assert outcome.is_error is False


def test_unknown_tool_is_an_error_result() -> None:
    outcome = run_tool("delete_files", {"path": "/"})

    assert outcome.is_error is True
    assert outcome.result == "That tool is not available."
