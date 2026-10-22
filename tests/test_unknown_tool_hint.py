"""The error a model gets for a call to a tool the agent doesn't have.

It names the tools the agent can call, closest names first and at most 20
of them, so a model that invented or misspelled a name can correct itself.
"""

from __future__ import annotations

from promptise.engine.nodes import _unknown_tool_message


class TestUnknownToolMessage:
    def test_close_name_first_with_a_suggestion(self) -> None:
        text = _unknown_tool_message("get_ordr", ["refund_order", "get_order"])
        assert text == (
            "Error: Unknown tool 'get_ordr'. Did you mean 'get_order'? "
            "Call one of the available tools: get_order, refund_order."
        )

    def test_suggests_a_misspelling_ignoring_case(self) -> None:
        for called in ("getOrder", "GET_ORDER", "get-order", "get_orders"):
            text = _unknown_tool_message(called, ["refund_order", "get_order"])
            assert "Did you mean 'get_order'?" in text, called

    def test_does_not_suggest_a_tool_that_only_shares_a_word(self) -> None:
        # cancel_order is not a misspelling of get_order: no suggestion, but
        # the closer names still come first.
        text = _unknown_tool_message("cancel_order", ["list_users", "get_order"])
        assert text == (
            "Error: Unknown tool 'cancel_order'. "
            "Call one of the available tools: get_order, list_users."
        )

    def test_caps_the_list(self) -> None:
        names = [f"tool_{i:02d}" for i in range(30)]
        text = _unknown_tool_message("zzz", names)
        assert "tool_19" in text and "tool_20" not in text
        assert text.endswith("tool_19 (and 10 more).")

    def test_a_close_name_past_the_cap_is_listed(self) -> None:
        names = [f"alpha_{i:02d}" for i in range(30)] + ["search_orders"]
        text = _unknown_tool_message("search_order", names)
        assert "Did you mean 'search_orders'?" in text
        assert "tools: search_orders, alpha_00" in text
        assert text.endswith("(and 11 more).")

    def test_no_tools(self) -> None:
        assert _unknown_tool_message("x", []) == (
            "Error: Unknown tool 'x'. No tools are available; answer without one."
        )
