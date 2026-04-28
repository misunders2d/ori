#!/usr/bin/env python3
"""Quick ground-truth test for keepa_bulk_query on a small ASIN set.

Asks for KEEPA_API_KEY interactively, runs bulk_query, also pulls
the raw csv[17] (review count) and csv[3] (BSR) from each cached
product so we can see whether the data is in Keepa or whether our
decoder is dropping it.

Usage:
    uv run python scripts/keepa_bulk_test.py [ASIN,ASIN,...]
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_ASINS = "B07PSK5GG2,B07GQ38TP8,B07MS7QWTG"


async def main() -> None:
    asins = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ASINS).strip()
    asin_list = [a.strip().upper() for a in asins.split(",") if a.strip()]
    print(f"Bulk-querying {len(asin_list)} ASINs: {asin_list}")

    if not os.environ.get("KEEPA_API_KEY"):
        key = getpass.getpass("Enter your KEEPA_API_KEY (input hidden): ").strip()
        if not key:
            print("No key provided — aborting.")
            sys.exit(1)
        os.environ["KEEPA_API_KEY"] = key

    from app.tools import keepa_api as k

    # Clear stale cache so we get fresh data
    for a in asin_list:
        cp = Path(k._CACHE_DIR) / f"{a}.json"
        if cp.exists():
            cp.unlink()

    fields = "asin,title,product_type,review_count,rating,sales_rank,monthly_sold,buy_box,active_deal,parent_asin,variation_asins,variation_count"

    print("→ keepa_bulk_query")
    result = await k.keepa_bulk_query(
        asins=asins,
        fields=fields,
        with_offers=False,
        domain=1,
    )

    print("\n=== bulk_query result ===")
    print(json.dumps(result, indent=2, default=str))

    # Now inspect each cached product directly
    print("\n=== raw csv[3]/csv[16]/csv[17] for each ASIN (ground truth) ===")
    for a in asin_list:
        cp = Path(k._CACHE_DIR) / f"{a}.json"
        if not cp.exists():
            print(f"  {a}: NO CACHE FILE (probably not returned by Keepa)")
            continue
        raw = json.loads(cp.read_text())
        csv = raw.get("csv") or []
        print(f"  {a}:")
        print(f"    productType: {raw.get('productType')}")
        print(f"    title: {(raw.get('title') or '')[:80]}")
        print(f"    csv length: {len(csv)}")
        print(f"    csv[3] (SALES) last 4: {csv[3][-4:] if len(csv) > 3 and csv[3] else None}")
        print(f"    csv[16] (RATING) last 4: {csv[16][-4:] if len(csv) > 16 and csv[16] else None}")
        print(f"    csv[17] (COUNT_REVIEWS) last 4: {csv[17][-4:] if len(csv) > 17 and csv[17] else None}")
        print(f"    monthlySold (raw): {raw.get('monthlySold')}")
        v = raw.get("variations")
        if v:
            print(f"    variations (raw): list len={len(v)}, asins={[x.get('asin') for x in v]}")
        else:
            print(f"    variations (raw): {v}")
        print(f"    variationCSV (raw): {raw.get('variationCSV')}")
        print(f"    parentAsin: {raw.get('parentAsin')}")

    out_path = Path(__file__).resolve().parent / "keepa_bulk_test_output.json"
    with open(out_path, "w") as f:
        json.dump({"asins": asin_list, "bulk_result": result}, f, indent=2, default=str)
    print(f"\nSaved → {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
