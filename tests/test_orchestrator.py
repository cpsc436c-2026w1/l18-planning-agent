"""plain/orchestrator.py offline, with the stub model."""
import json

from grocery.model import ModelReply, StubModel
from plain.orchestrator import BUDGET_USD, MAX_DISHES, plan_week

REQUEST = "Plan dinners for a week under $60, no palm oil."


def test_a_plan_longer_than_seven_dishes_is_cut(duck_tools):
    worker = StubModel()
    week = plan_week(REQUEST, StubModel(dishes=9), worker, duck_tools)
    assert week["dishes_planned"] == 9 and week["dishes_kept"] == MAX_DISHES == len(week["picks"])
    assert len(worker.usage) == MAX_DISHES + 1            # one worker call per kept dish, and the merge call


def test_one_planner_call_and_the_small_calls_go_to_the_worker_model(duck_tools):
    planner, worker = StubModel(dishes=3), StubModel()
    plan_week(REQUEST, planner, worker, duck_tools)
    assert len(planner.usage) == 1                        # routing: only the plan goes to the planner model
    assert len(worker.usage) == 3 + 1                     # three dishes and the merge call


def test_the_filter_reaches_every_search(duck_tools):
    week = plan_week(REQUEST, StubModel(), StubModel(), duck_tools)
    assert week["filters"] == {"no_palm_oil": True}
    assert all(p["palm_oil"] == "no" for p in week["picks"])
    # without the filter the cheapest pasta is the one with palm oil
    week = plan_week("Plan dinners for a week under $60.", StubModel(), StubModel(), duck_tools)
    assert week["picks"][0]["code"] == "1003"


def test_the_total_is_added_by_app_code_from_catalogue_prices(duck_tools):
    week = plan_week(REQUEST, StubModel(), StubModel(), duck_tools)
    prices = [duck_tools.get_product(p["code"])["price_usd"] for p in week["picks"]]
    assert week["total_usd"] == round(sum(prices), 2) == 28.45
    assert week["over_budget"] is False and "within" in week["reply"]


def test_a_total_over_the_budget_is_reported(duck_tools, monkeypatch):
    monkeypatch.setattr("plain.orchestrator.BUDGET_USD", 20.0)
    week = plan_week(REQUEST, StubModel(), StubModel(), duck_tools)
    assert week["total_usd"] == 28.45 and week["over_budget"] is True and "over" in week["reply"]


def test_a_pick_outside_the_search_result_counts_as_nothing_found(duck_tools):
    class MakesUpACode(StubModel):
        def _worker(self, messages, user):
            return ModelReply(json.dumps({"code": "9999999", "price_usd": 0.01}))

    week = plan_week(REQUEST, StubModel(dishes=2), MakesUpACode(), duck_tools)
    assert [p["code"] for p in week["picks"]] == [None, None] and week["total_usd"] == 0


def test_a_planner_reply_that_is_not_json_gives_an_empty_week(duck_tools):
    class Chatty(StubModel):
        def _planner(self, messages, user):
            return ModelReply("Sure! Here are some ideas.")

    week = plan_week(REQUEST, Chatty(), StubModel(), duck_tools)
    assert week["dishes_kept"] == 0 and week["total_usd"] == 0 and BUDGET_USD == 60
