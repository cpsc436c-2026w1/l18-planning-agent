"""The four tools, on DuckDB and on DynamoDB (moto)."""
from grocery.tools import MAX_RESULTS, MAX_TEXT, TOOL_SPECS, query_words


def test_search_finds_by_category_word_cheapest_first(tools):
    found = tools.search_products("pasta")["products"]
    assert [p["code"] for p in found] == ["1003", "1002", "1001"]
    assert [p["price_usd"] for p in found] == sorted(p["price_usd"] for p in found)


def test_no_palm_oil_filter_uses_the_ingredients_flag(tools):
    codes = [p["code"] for p in tools.search_products("pasta", {"no_palm_oil": True})["products"]]
    assert codes == ["1002", "1001"]                      # 1003 holds palm oil
    assert [p["code"] for p in tools.search_products("pizza", {"no_palm_oil": True})["products"]] == ["4002"]


def test_max_price_filter(tools):
    assert [p["code"] for p in tools.search_products("pizza", {"max_price_usd": 5})["products"]] == ["4001"]


def test_more_matching_words_rank_first(tools):
    assert tools.search_products("penne pasta")["products"][0]["code"] == "1002"


def test_search_result_is_capped_and_says_so(tools):
    found = tools.search_products("pasta tuna soup pizza gnocchi wraps ravioli")
    assert len(found["products"]) == MAX_RESULTS and found["more"] == 5
    assert "ingredients" not in found["products"][0]      # the long text stays out of search results


def test_search_with_no_usable_word_returns_nothing(tools):
    assert tools.search_products("the and")["products"] == []


def test_query_words_drop_stop_words_and_plural():
    assert query_words("Tomato sauces for the dinner") == ["tomato", "sauce"]


def test_get_product_and_unknown_code(tools):
    p = tools.get_product("4001")
    assert p["palm_oil"] == "yes" and "palm oil" in p["ingredients"] and len(p["ingredients"]) <= MAX_TEXT
    assert "error" in tools.get_product("nope")


def test_add_to_list_and_get_list(tools):
    assert tools.add_to_list("sam", "1002", "r1")["added"] is True
    assert tools.add_to_list("sam", "2001", "r2")["added"] is True
    got = tools.get_list("sam")
    assert [i["code"] for i in got["items"]] == ["1002", "2001"] and got["total_usd"] == 3.95
    assert tools.get_list("alex") == {"shopper": "alex", "items": [], "total_usd": 0}


def test_a_retried_write_adds_the_item_once(tools):
    first = tools.add_to_list("sam", "1002", "r1")
    again = tools.add_to_list("sam", "1002", "r1")
    assert first["added"] is True and again["added"] is False and again["duplicate_request"] is True
    assert tools.get_list("sam")["items"][0]["quantity"] == 1


def test_a_new_request_for_the_same_product_adds_a_second_one(tools):
    tools.add_to_list("sam", "1002", "r1")
    tools.add_to_list("sam", "1002", "r2")
    assert tools.get_list("sam")["items"][0]["quantity"] == 2


def test_add_to_list_refuses_unknown_code_and_missing_request_id(tools):
    assert "error" in tools.add_to_list("sam", "nope", "r1")
    assert "error" in tools.add_to_list("sam", "1002", "")
    assert tools.get_list("sam")["items"] == []


def test_call_reports_unknown_tool_and_wrong_arguments(tools):
    assert tools.call("delete_everything", {}) == {"error": "unknown tool delete_everything"}
    assert "wrong arguments" in tools.call("get_product", {"sku": "1"})["error"]
    assert tools.call("get_product", {"code": "1001"})["name"] == "Spaghetti n5"


def test_categories(tools):
    assert "pasta" in tools.categories() and len(tools.categories()) == 9


def test_tool_specs_name_the_four_tools():
    assert [t["function"]["name"] for t in TOOL_SPECS] == ["search_products", "get_product", "get_list",
                                                           "add_to_list"]
