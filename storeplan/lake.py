"""Build the lake stand-in: price history in the raw zone and as a curated table as Parquet files, written by DuckDB.

    prices_raw     every observation as it arrived: some twice, in the store's own currency (EUR for online)
    prices_curated one row per product, store and day, in CAD; partitioned by month (month=2026-09/...)

The raw zone is built first and the curated table is derived from it (drop the duplicates, convert EUR to CAD), as a
pipeline would. Prices are illustrative (storeplan/fixtures.py). The build is deterministic: md5 of the key, no
random numbers, so the same products file gives the same lake.

    uv run python -m storeplan.lake              # data/lake/ from data/products.jsonl, about 10 s
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from grocery import config
from storeplan import fixtures as fx

LAKE_DIR = config.ROOT / "data" / "lake"


def _unit(expr: str) -> str:
    """A number in [0, 1) from the md5 of an SQL expression: deterministic noise."""
    return f"(('0x' || substr(md5({expr}), 1, 8))::BIGINT / 4294967296.0)"


def build(products_file: Path = config.DATA_FILE, out: Path = LAKE_DIR, extra: list[dict] | None = None) -> dict:
    import duckdb

    rows = [json.loads(line) for line in Path(products_file).read_text().splitlines() if line.strip()]
    rows += extra if extra is not None else [fx.INJECTED]
    if out.exists():
        shutil.rmtree(out)
    (out / "prices_raw").mkdir(parents=True)
    con = duckdb.connect()
    con.execute("CREATE TABLE products (code VARCHAR, price_usd DOUBLE)")
    con.executemany("INSERT INTO products VALUES (?, ?)", [(r["code"], r["price_usd"]) for r in rows])
    con.execute("CREATE TABLE stores (store VARCHAR, currency VARCHAR)")
    con.executemany("INSERT INTO stores VALUES (?, ?)", list(fx.STORES.items()))
    # true CAD price: base x a store factor of 0.94..1.06 per product and store x a yearly trend of -10..+10 %
    # x weekly noise of -3..+3 %
    con.execute(f"""
        CREATE TABLE truth AS
        SELECT p.code AS product_code, s.store, s.currency, d::DATE AS observed_on,
               round(p.price_usd * {fx.USD_TO_CAD} * (0.94 + {_unit("p.code || s.store")} * 0.12)
                     * (1 + ({_unit("p.code")} * 0.2 - 0.1) * datediff('day', DATE '{fx.FIRST_DAY}', d) / 300.0)
                     * (1 + {_unit("p.code || s.store || (datediff('day', DATE '" + fx.FIRST_DAY + "', d) // 7)::VARCHAR")} * 0.06 - 0.03),
                     2) AS price_cad
        FROM products p, stores s,
             range(DATE '{fx.FIRST_DAY}', DATE '{fx.AS_OF}' + INTERVAL 1 DAY, INTERVAL 1 DAY) t(d)""")
    # raw: about one observation in five arrives twice; prices in the store's own currency
    con.execute(f"""
        CREATE TABLE raw AS
        SELECT md5(product_code || store || observed_on::VARCHAR || n::VARCHAR) AS obs_id,
               product_code, store, observed_on,
               CASE WHEN currency = 'EUR' THEN round(price_cad / {fx.EUR_TO_CAD}, 2) ELSE price_cad END AS price,
               currency,
               'receipt:' || md5('r' || product_code || store || observed_on::VARCHAR || n::VARCHAR) AS source
        FROM truth, range(2) r(n)
        WHERE n = 0 OR {_unit("product_code || store || observed_on::VARCHAR")} < 0.2""")
    con.execute(f"COPY (SELECT * FROM raw ORDER BY product_code, store, observed_on) "
                f"TO '{out}/prices_raw/part-0.parquet' (FORMAT parquet)")
    # curated: one row per product, store and day; EUR converted at the fixed rate
    con.execute(f"""
        COPY (SELECT product_code, store, observed_on,
                     round(any_value(CASE WHEN currency = 'EUR' THEN price * {fx.EUR_TO_CAD} ELSE price END), 2)
                         AS price_cad,
                     strftime(observed_on, '%Y-%m') AS month
              FROM raw GROUP BY product_code, store, observed_on ORDER BY month, product_code, store, observed_on)
        TO '{out}/prices_curated' (FORMAT parquet, PARTITION_BY (month), FILENAME_PATTERN 'part-{{i}}')""")
    counts = {"prices_raw": con.execute("SELECT count(*) FROM raw").fetchone()[0],
              "prices_curated": con.execute("SELECT count(*) FROM truth").fetchone()[0]}
    return counts


def files(table: str, lake: Path = LAKE_DIR, months: list[str] | None = None) -> list[Path]:
    """The Parquet files of a table; for prices_curated only the given months' folders when months is set."""
    if table == "prices_curated" and months is not None:
        return sorted(p for m in months for p in (lake / table / f"month={m}").glob("*.parquet"))
    return sorted((lake / table).rglob("*.parquet"))


if __name__ == "__main__":
    n = build()
    size = {t: sum(f.stat().st_size for f in files(t)) for t in n}
    for t in n:
        print(f"{t}: {n[t]} rows, {size[t] / 1e6:.1f} MB")
