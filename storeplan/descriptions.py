"""What each table holds, written once. The catalog (storeplan/catalog.py) joins these words to the columns and
types it reads from the store itself: DuckDB offline, the Glue Data Catalog on AWS. Nothing here is generated
by a model, and nothing here changes at run time.
"""
from __future__ import annotations

TABLES = {
    "shopping_lists": {
        "store_type": "operational",
        "holds": "Each shopper's shopping list: one row per item, with the product's code and name.",
        "access": "key lookup",
        "key": "shopper",
        "use_for": "What is on one shopper's list. The shopper is the signed-in shopper: app code sets it.",
        "columns": {"shopper": "the shopper's name (the key)", "code": "product code", "name": "product name",
                    "quantity": "how many of this product are on the list"},
    },
    "prices_raw": {
        "store_type": "lake",
        "holds": "Raw zone: every price observation as it arrived, 2026-01-01 to 2026-10-31. About one in five arrived "
                 "twice, so a day can have two rows for the same product and store. The price is in the "
                 "store's own currency (column currency: CAD, or EUR for store online).",
        "access": "scan",
        "use_for": "Auditing what arrived (duplicates, currencies, receipts). Not for prices, counts or averages: "
                   "duplicates are counted twice and EUR is mixed with CAD; use prices_curated.",
        "columns": {"obs_id": "id of the observation (large, unique)", "product_code": "product code",
                    "store": "north, centre or online", "observed_on": "day of the observation (DATE)",
                    "price": "price in the store's currency", "currency": "CAD or EUR",
                    "source": "receipt reference (large, unique)"},
    },
    "prices_curated": {
        "store_type": "lake",
        "holds": "Curated table: one price per product, store and day, 2026-01-01 to 2026-10-31, in CAD: duplicates removed, "
                 "EUR converted at a fixed rate. Partitioned by month.",
        "access": "scan",
        "use_for": "Prices, price changes, averages, cheapest store, and counts of days or observations.",
        "columns": {"product_code": "product code", "store": "north, centre or online",
                    "observed_on": "day of the price (DATE)", "price_cad": "price in CAD",
                    "month": "partition column, YYYY-MM; app code derives it from a filter on observed_on"},
    },
}
