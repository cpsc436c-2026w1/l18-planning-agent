"""plain/agent.py offline, with the stub model."""
from grocery.model import ModelReply, StubModel, ToolCall
from plain.agent import MAX_MODEL_CALLS, MAX_TOOL_CALLS, run_agent

REQUEST = "Which is cheaper, spaghetti or penne? Add the cheaper one to my list."
yes = lambda shopper, product: True      # noqa: E731
no = lambda shopper, product: False      # noqa: E731


def test_adds_the_cheaper_one_after_approval(duck_tools):
    asked = []
    run = run_agent(REQUEST, "sam", StubModel(), duck_tools, lambda s, p: asked.append((s, p["code"])) or True)
    assert run["status"] == "done" and run["model_calls"] == 4 and run["tool_calls"] == 3
    assert asked == [("sam", "1002")]                             # the shopper was asked once, about the penne
    assert [i["code"] for i in duck_tools.get_list("sam")["items"]] == ["1002"]
    assert "Penne" in run["reply"]


def test_a_declined_write_changes_nothing(duck_tools):
    run = run_agent(REQUEST, "sam", StubModel(), duck_tools, no)
    assert run["status"] == "done" and run["writes"][0]["declined"] is True
    assert duck_tools.get_list("sam")["items"] == []
    assert run["reply"] == "Nothing was added to your list."


def test_the_tool_call_cap_stops_a_runaway_loop(duck_tools):
    run = run_agent(REQUEST, "sam", StubModel(runaway=True), duck_tools, yes)
    assert run["status"] == "stopped" and run["stop_reason"] == f"cap of {MAX_TOOL_CALLS} tool calls"
    assert run["tool_calls"] == MAX_TOOL_CALLS and run["model_calls"] == MAX_TOOL_CALLS + 1


def test_the_model_call_cap_stops_a_model_that_never_replies(duck_tools):
    class AsksNothingUseful(StubModel):
        def chat(self, messages, tools=None):
            self.usage.append({"model": "stub", "tokens_in": 1, "tokens_out": 1})
            return ModelReply(None, [ToolCall("c", "get_list", {"shopper": "sam"})] if len(self.usage) % 2 else
                              [ToolCall("c", "get_product", {"code": "1001"})])

    import plain.agent as agent

    old = agent.MAX_TOOL_CALLS
    agent.MAX_TOOL_CALLS = 100
    try:
        run = run_agent(REQUEST, "sam", AsksNothingUseful(), duck_tools, yes)
    finally:
        agent.MAX_TOOL_CALLS = old
    assert run["status"] == "stopped" and run["stop_reason"] == f"cap of {MAX_MODEL_CALLS} model calls"
    assert run["model_calls"] == MAX_MODEL_CALLS


def test_a_write_retried_after_a_timeout_adds_the_item_once(duck_tools):
    real = duck_tools.store.put_list_item
    calls = []

    def writes_then_times_out(shopper, request_id, code):
        calls.append(request_id)
        result = real(shopper, request_id, code)
        if len(calls) == 1:
            raise TimeoutError("the write succeeded, the answer was lost")
        return result

    duck_tools.store.put_list_item = writes_then_times_out
    run = run_agent(REQUEST, "sam", StubModel(), duck_tools, yes)
    assert len(calls) == 2 and calls[0] == calls[1]               # the retry carried the same key
    assert run["writes"][0]["duplicate_request"] is True
    assert duck_tools.get_list("sam")["items"][0]["quantity"] == 1


def test_the_write_goes_to_the_signed_in_shopper_whatever_the_model_wrote(duck_tools):
    class WritesToSomeoneElse(StubModel):
        def _agent(self, messages, user):
            if any(m["role"] == "tool" for m in messages):
                return ModelReply("Done.")
            return ModelReply(None, [ToolCall("c1", "add_to_list", {"shopper": "alex", "code": "1001"})])

    run_agent(REQUEST, "sam", WritesToSomeoneElse(), duck_tools, yes)
    assert duck_tools.get_list("alex")["items"] == []
    assert [i["code"] for i in duck_tools.get_list("sam")["items"]] == ["1001"]


def test_the_same_request_id_run_twice_adds_once(duck_tools):
    run_agent(REQUEST, "sam", StubModel(), duck_tools, yes, request_id="req-1")
    again = run_agent(REQUEST, "sam", StubModel(), duck_tools, yes, request_id="req-1")
    assert again["writes"][0]["duplicate_request"] is True
    assert duck_tools.get_list("sam")["items"][0]["quantity"] == 1


def test_an_unknown_product_is_not_put_to_the_shopper(duck_tools):
    class WrongCode(StubModel):
        def _agent(self, messages, user):
            if any(m["role"] == "tool" for m in messages):
                return ModelReply("I could not find it.")
            return ModelReply(None, [ToolCall("c1", "add_to_list", {"shopper": "sam", "code": "0000"})])

    asked = []
    run = run_agent(REQUEST, "sam", WrongCode(), duck_tools, lambda s, p: asked.append(p) or True)
    assert asked == [] and "error" in run["writes"][0]
