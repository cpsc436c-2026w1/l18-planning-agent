from pathlib import Path

from behave import given, then, when

from grocery.model import ModelReply, StubModel, ToolCall
from grocery.store import DuckDBStore
from grocery.tools import GroceryTools
from plain.agent import run_agent
from plain.orchestrator import plan_week

FIXTURE = Path(__file__).resolve().parent.parent.parent / "tests" / "products.jsonl"


@given("the test catalogue in DuckDB")
def catalogue(context):
    context.tools = GroceryTools(DuckDBStore(FIXTURE))
    context.agent_model = StubModel()


# ---- orchestrator-workers ----

@given("a planner that returns {n:d} dishes")
def planner_returns(context, n):
    context.planner, context.worker = StubModel(dishes=n), StubModel()


@given("every product in the catalogue costs {price:f} USD")
def flat_price(context, price):
    context.tools.store.con.execute("UPDATE products SET price_usd = ?", [price])


@when('the shopper asks "{request}"')
def ask_plan(context, request):
    context.week = plan_week(request, context.planner, context.worker, context.tools)


@then("{kept:d} dishes are kept of the {planned:d} planned")
def kept_of_planned(context, kept, planned):
    assert context.week["dishes_planned"] == planned, context.week["dishes_planned"]
    assert context.week["dishes_kept"] == kept == len(context.week["picks"]), context.week["dishes_kept"]


@then("the worker model was called {n:d} times for dishes and once for the merge")
def worker_calls(context, n):
    assert len(context.worker.usage) == n + 1, len(context.worker.usage)


@then("the planner model was called once")
def planner_calls(context):
    assert len(context.planner.usage) == 1, len(context.planner.usage)


@then("no product in the week's list holds palm oil")
def no_palm_oil(context):
    for pick in context.week["picks"]:
        assert context.tools.get_product(pick["code"])["palm_oil"] == "no", pick


@then("the total is {total:f} USD")
def total_is(context, total):
    assert context.week["total_usd"] == total, context.week["total_usd"]


@then("the total is the sum of the catalogue prices of the picks")
def total_from_catalogue(context):
    prices = [context.tools.get_product(p["code"])["price_usd"] for p in context.week["picks"]]
    assert context.week["total_usd"] == round(sum(prices), 2)


@then("the week is reported as {verdict} the budget of {budget:d} USD")
def budget_verdict(context, verdict, budget):
    assert context.week["budget_usd"] == budget
    assert context.week["over_budget"] is (verdict == "over"), context.week


@then('the reply says "{word}"')
def reply_says(context, word):
    assert word in context.week["reply"], context.week["reply"]


# ---- bounded agent ----

@given("a shopper who {verb}")
def shopper_answers(context, verb):
    context.confirm = lambda shopper, product: context.asked.append(product["name"]) or verb == "approves"


@given("a store whose first write succeeds and then times out")
def flaky_store(context):
    real = context.tools.store.put_list_item

    def put(shopper, request_id, code):
        context.write_keys.append(request_id)
        result = real(shopper, request_id, code)
        if len(context.write_keys) == 1:
            raise TimeoutError("the write succeeded, the answer was lost")
        return result

    context.tools.store.put_list_item = put


@given("an LLM that asks for another search on every call")
def runaway(context):
    context.agent_model = StubModel(runaway=True)


@given('an LLM that asks to add product "{code}" to the list of "{other}"')
def writes_elsewhere(context, code, other):
    class Model(StubModel):
        def _agent(self, messages, user):
            if any(m["role"] == "tool" for m in messages):
                return ModelReply("Done.")
            return ModelReply(None, [ToolCall("c1", "add_to_list", {"shopper": other, "code": code})])

    context.agent_model = Model()


@when('shopper "{shopper}" asks "{request}"')
def ask_agent(context, shopper, request):
    context.run = run_agent(request, shopper, context.agent_model, context.tools, context.confirm)


@then('the shopper was asked once, about "{name}"')
def asked_once(context, name):
    assert context.asked == [name], context.asked


@then('the list of "{shopper}" holds {n:d} "{name}"')
def list_holds(context, shopper, n, name):
    items = context.tools.get_list(shopper)["items"]
    assert [(i["name"], i["quantity"]) for i in items] == [(name, n)], items


@then('the list of "{shopper}" is empty')
def list_empty(context, shopper):
    assert context.tools.get_list(shopper)["items"] == []


@then("the write was sent {n:d} times with the same request key")
def same_key(context, n):
    assert len(context.write_keys) == n and len(set(context.write_keys)) == 1, context.write_keys


@then("the run is stopped by the cap of {n:d} tool calls")
def stopped_by_cap(context, n):
    assert context.run["status"] == "stopped" and context.run["stop_reason"] == f"cap of {n} tool calls", context.run


@then("{n:d} tool calls ran")
def tool_calls_ran(context, n):
    assert context.run["tool_calls"] == n
