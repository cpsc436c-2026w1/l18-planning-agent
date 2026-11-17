"""Build data/products.jsonl: the grocery subset the four tools read.

Sources (both ODbL, the same ones A3 used):
- A3's copies in the course bucket, read with the profile A3's kit names (`default`):
    s3://436c-2026w1/a3/full/lake/products/products.parquet   Open Food Facts, France subset, 1,258,581 products
    s3://436c-2026w1/a3/full/pg/prices.parquet                Open Prices, France, 198,956 price observations
- Open Food Facts' published food.parquet on Hugging Face, for the two columns A3 dropped:
  ingredients_text and ingredients_analysis_tags (the palm-oil status Open Food Facts derives from the ingredients).

Run once (about 20 minutes on the ops node: DuckDB reads three columns of the 7.7 GB file over HTTP; the result
is kept in data/src/ingredients.parquet, so a second run takes seconds):
    aws s3 cp s3://436c-2026w1/a3/full/lake/products/products.parquet data/src/ --profile default
    aws s3 cp s3://436c-2026w1/a3/full/pg/prices.parquet data/src/ --profile default
    uv run python data/build_subset.py

What it keeps, and why this size: the request is a week of dinners, so 7 dishes, each searched in one of a few
dinner categories. PER_CATEGORY products in each of the categories below gives every dish a real choice between
cheaper and dearer products and between products with and without palm oil, and the whole file stays under 1 MB:
small enough to commit, to load into DynamoDB in seconds and to read in one Scan page (1 MB).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
FOOD = "https://huggingface.co/datasets/openfoodfacts/product-database/resolve/main/food.parquet"
EUR_TO_USD = 1.10       # fixed for the demo (assumed): Open Prices rows are in euros, the lecture's budget is in USD
PER_CATEGORY = 24       # products kept per category, at most
PALM = 8                # of them, at most this many with palm oil
POOL = 80               # most-scanned priced products per category looked up for ingredients

# Open Food Facts category tag -> the English word a dinner search uses
CATEGORIES = {
    "en:pastas": "pasta", "en:tomato-sauces": "tomato sauce", "en:pestos": "pesto", "en:rices": "rice",
    "en:noodles": "noodles", "en:canned-tunas": "tuna", "en:lentils": "lentils", "en:chickpeas": "chickpeas",
    "en:soups": "soup", "en:pizzas": "pizza", "en:wraps": "wraps", "en:gnocchi": "gnocchi",
    "en:ravioli": "ravioli", "en:breaded-fish": "breaded fish", "en:sausages": "sausages",
    "en:chickens": "chicken", "en:eggs": "eggs", "en:frozen-vegetables": "frozen vegetables",
    "en:canned-vegetables": "canned vegetables", "en:mozzarella": "mozzarella", "en:quiches": "quiche",
    "en:curry-sauces": "curry sauce", "en:couscous": "couscous", "en:mashed-potatoes": "mashed potatoes",
    "en:salmons": "salmon", "en:hams": "ham",
}


def main() -> int:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs")
    con.execute("CREATE TABLE cats AS SELECT * FROM (VALUES " +
                ", ".join(f"('{k}', '{v}')" for k, v in CATEGORIES.items()) + ") t(tag, word)")
    # one price per product: the median of its Open Prices observations, and how many there were
    con.execute(f"""CREATE TABLE price AS
        SELECT product_code AS code, median(price) AS price_eur, count(*) AS price_n, max(date)::DATE AS price_last
        FROM '{SRC}/prices.parquet' WHERE price BETWEEN 0.3 AND 15 GROUP BY 1""")
    con.execute(f"""CREATE TABLE pool AS
        SELECT code, list_filter(product_name, lambda x: x.lang = 'main')[1].text AS name, brands, quantity,
               categories_tags, coalesce(scans_n, 0) AS scans_n, tag, word, price_eur, price_n, price_last
        FROM '{SRC}/products.parquet' JOIN price USING (code) JOIN cats ON list_contains(categories_tags, tag)
        WHERE name IS NOT NULL AND length(name) > 2
        QUALIFY row_number() OVER (PARTITION BY tag ORDER BY scans_n DESC, code) <= {POOL}""")
    print("pool:", con.execute("SELECT count(DISTINCT code) FROM pool").fetchall()[0][0], "codes", flush=True)
    cache = SRC / "ingredients.parquet"         # the slow step (about 20 minutes), kept for the next run
    if not cache.exists():
        con.execute(f"""COPY (
            SELECT code, list_filter(ingredients_text, lambda x: x.lang = 'main')[1].text AS ingredients,
                   ingredients_analysis_tags AS analysis
            FROM '{FOOD}' WHERE code IN (SELECT code FROM pool)) TO '{cache}' (FORMAT PARQUET)""")
    con.execute(f"CREATE TABLE ingr AS SELECT * FROM '{cache}'")
    print("ingredients found:", con.execute("SELECT count(*) FROM ingr WHERE ingredients IS NOT NULL").fetchall())
    rows = con.execute(f"""
        WITH x AS (
          SELECT p.*, i.ingredients,
                 CASE WHEN list_contains(i.analysis, 'en:palm-oil-free') THEN 'no'
                      WHEN list_contains(i.analysis, 'en:palm-oil') THEN 'yes' ELSE NULL END AS palm_oil
          FROM pool p JOIN ingr i USING (code)
          WHERE i.ingredients IS NOT NULL AND length(i.ingredients) > 10)
        SELECT code, name, brands, quantity, word AS category, round(price_eur * {EUR_TO_USD}, 2) AS price_usd,
               round(price_eur, 2) AS price_eur, price_n, price_last::VARCHAR, palm_oil, ingredients
        FROM x WHERE palm_oil IS NOT NULL
        -- a product in two categories is kept in one; within a category, at most PALM of the most scanned
        -- products with palm oil and at most PER_CATEGORY - PALM without, so the filter always leaves a choice
        QUALIFY row_number() OVER (PARTITION BY code ORDER BY tag) = 1
            AND row_number() OVER (PARTITION BY tag, palm_oil ORDER BY scans_n DESC, code)
                <= CASE WHEN palm_oil = 'yes' THEN {PALM} ELSE {PER_CATEGORY} - {PALM} END
        ORDER BY category, code""").fetchall()
    cols = ["code", "name", "brand", "quantity", "category", "price_usd", "price_eur", "price_n", "price_last",
            "palm_oil", "ingredients"]
    out = HERE / "products.jsonl"
    with out.open("w") as f:
        for r in rows:
            d = dict(zip(cols, r))
            d["brand"] = (d["brand"] or "").split(",")[0].strip()
            d["ingredients"] = " ".join(d["ingredients"].split())[:600]
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"wrote {out} : {len(rows)} products, {out.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
