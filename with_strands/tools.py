"""The four grocery tools as Strands tools. Each is the plain function of grocery/tools.py with the @tool
decorator, which builds the name, the description and the argument schema the LLM sees from the signature and
the docstring (Strands docs, "Create custom tools")."""
from __future__ import annotations

from strands import tool

from grocery.tools import GroceryTools


def make_tools(grocery: GroceryTools) -> dict:
    @tool
    def search_products(query: str, filters: dict | None = None) -> dict:
        """Search the grocery catalogue by one or two plain English words (pasta, tomato sauce, tuna).
        Returns at most 5 products, cheapest first, with code, name and price_usd.

        Args:
            query: one or two words for the product
            filters: optional, {"no_palm_oil": true} keeps only products whose ingredients hold no palm oil;
                {"max_price_usd": 3} keeps products at or under that price
        """
        return grocery.search_products(query, filters)

    @tool
    def get_product(code: str) -> dict:
        """One product by its code: name, brand, quantity, price_usd, palm_oil, ingredients."""
        return grocery.get_product(code)

    @tool
    def get_list(shopper: str) -> dict:
        """The shopper's shopping list with its total in USD."""
        return grocery.get_list(shopper)

    @tool
    def add_to_list(shopper: str, code: str, request_id: str = "") -> dict:
        """Add one product to the shopper's shopping list. The shopper is asked to approve first.

        Args:
            shopper: the shopper
            code: the product's code
            request_id: set by the app; leave it empty
        """
        return grocery.add_to_list(shopper, code, request_id)

    return {t.tool_name: t for t in (search_products, get_product, get_list, add_to_list)}
