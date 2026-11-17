"""The week's menu as an email: one Jinja2 template, an HTML part and a plain-text part."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

PRICE_NOTE = ("Prices are illustrative: the median of Open Prices observations for each product, "
              "converted at a fixed 1.10 USD per EUR. Products: Open Food Facts (ODbL).")

_env = Environment(loader=FileSystemLoader(Path(__file__).parent), undefined=StrictUndefined,
                   autoescape=lambda name: bool(name) and name.endswith(".html.j2"))   # names are data: escaped in HTML
_env.filters["usd"] = lambda x: f"{float(x):.2f}"


def render(menu: dict) -> dict:
    """Returns {"subject", "html", "text"}. A value the template needs and the message lacks is an error."""
    found = [p for p in menu["picks"] if p.get("code")]
    view = {**menu, "left_usd": round(menu["budget_usd"] - menu["total_usd"], 2), "found": len(found),
            "price_note": PRICE_NOTE}
    subject = (f"Dinners for the week: {len(menu['picks'])} dishes, "
               f"{menu['total_usd']:.2f} of {menu['budget_usd']:.0f} USD")
    return {"subject": subject, "html": _env.get_template("menu.html.j2").render(view),
            "text": _env.get_template("menu.txt.j2").render(view)}
