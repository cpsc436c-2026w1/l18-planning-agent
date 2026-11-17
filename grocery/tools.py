"""The four grocery tools of the L17 slides, as plain functions over a store (grocery/store.py).

    search_products(query, filters)             read
    get_product(code)                           read
    get_list(shopper)                           read
    add_to_list(shopper, code, request_id)      write: changes the shopper's data, once per request_id

Every result is a small dict that goes to the LLM as the tool result, so it is capped: at most MAX_RESULTS
products per search, without their ingredients, and at most MAX_TEXT characters of ingredients in get_product.
A result that was cut says so ("more": the number of matches left out).
"""
from __future__ import annotations

import re

MAX_RESULTS = 5      # products in one search result (ours): 5 short rows cost about 250 input tokens
MAX_TEXT = 300       # characters of ingredients in get_product (ours)
STOP_WORDS = {"a", "an", "the", "and", "or", "for", "with", "without", "of", "no", "some", "dinner", "dish"}
WRITE_TOOLS = {"add_to_list"}

# What the LLM sees: names, descriptions and argument schemas in the OpenAI tool format.
TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "search_products",
        "description": "Search the grocery catalogue by one or two plain words (pasta, tomato sauce, tuna). "
                       f"Returns at most {MAX_RESULTS} products, cheapest first, with code, name and price_usd.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "one or two words for the product, in English"},
            "filters": {"type": "object", "properties": {
                "no_palm_oil": {"type": "boolean", "description": "true: only products whose ingredients "
                                                                  "hold no palm oil"},
                "max_price_usd": {"type": "number"}}}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_product",
        "description": "One product by its code: name, brand, quantity, price_usd, palm_oil, ingredients.",
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}},
    {"type": "function", "function": {
        "name": "get_list",
        "description": "The shopper's shopping list with its total in USD.",
        "parameters": {"type": "object", "properties": {"shopper": {"type": "string"}}, "required": ["shopper"]}}},
    {"type": "function", "function": {
        "name": "add_to_list",
        "description": "Add one product to the shopper's shopping list. The shopper is asked to approve first.",
        "parameters": {"type": "object", "properties": {"shopper": {"type": "string"},
                                                        "code": {"type": "string"}},
                       "required": ["shopper", "code"]}}},
]


def query_words(query: str) -> list[str]:
    """'Tomato sauces' -> ['tomato', 'sauce']: lower case, no stop words, no plural s."""
    words = [w for w in re.findall(r"[a-zà-ÿ]+", str(query).lower()) if w not in STOP_WORDS]
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words]


class GroceryTools:
    def __init__(self, store):
        self.store = store

    def search_products(self, query: str, filters: dict | None = None) -> dict:
        filters = filters if isinstance(filters, dict) else {}
        words = query_words(query)
        rows = self.store.candidates(words, no_palm_oil=bool(filters.get("no_palm_oil")),
                                     max_price_usd=filters.get("max_price_usd"))
        # products that hold more of the words come first, then the cheapest
        rows.sort(key=lambda r: (-sum(w in r["search_text"] for w in words), r["price_usd"], r["code"]))
        top = [_short(r) for r in rows[:MAX_RESULTS]]
        return {"products": top, "more": max(0, len(rows) - len(top))}

    def get_product(self, code: str) -> dict:
        row = self.store.product(str(code))
        if row is None:
            return {"error": f"no product with code {code}"}
        return {**_short(row), "category": row["category"], "ingredients": row["ingredients"][:MAX_TEXT]}

    def get_list(self, shopper: str) -> dict:
        items: dict[str, dict] = {}
        for row in self.store.list_items(shopper):
            product = self.store.product(row["code"]) or {"name": "?", "price_usd": 0.0}
            item = items.setdefault(row["code"], {"code": row["code"], "name": product["name"],
                                                  "price_usd": product["price_usd"], "quantity": 0})
            item["quantity"] += 1
        total = round(sum(i["price_usd"] * i["quantity"] for i in items.values()), 2)
        return {"shopper": shopper, "items": list(items.values()), "total_usd": total}

    def add_to_list(self, shopper: str, code: str, request_id: str) -> dict:
        if not request_id:
            return {"error": "add_to_list needs a request_id: app code sets it, one per approved write"}
        product = self.store.product(str(code))
        if product is None:
            return {"error": f"no product with code {code}"}
        row, created = self.store.put_list_item(shopper, request_id, product["code"])
        # created is False when this request_id was written before: nothing was added a second time
        return {"added": created, "duplicate_request": not created, "request_id": row["request_id"],
                "code": row["code"], "name": product["name"], "price_usd": product["price_usd"]}

    def categories(self) -> list[str]:
        """The kinds of product the shop sells: the words a search can use."""
        return self.store.categories()

    def call(self, name: str, args: dict) -> dict:
        """Run one tool call that the LLM asked for. A wrong name or wrong arguments become a tool result."""
        tool = {"search_products": self.search_products, "get_product": self.get_product,
                "get_list": self.get_list, "add_to_list": self.add_to_list}.get(name)
        if tool is None:
            return {"error": f"unknown tool {name}"}
        try:
            return tool(**args)
        except TypeError as e:
            return {"error": f"wrong arguments for {name}: {e}"}


def _short(row: dict) -> dict:
    return {"code": row["code"], "name": row["name"], "brand": row["brand"], "quantity": row["quantity"],
            "price_usd": row["price_usd"], "palm_oil": row["palm_oil"]}
