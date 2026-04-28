#!/usr/bin/env python3
"""Ground-truth probe for the Keepa toolset against a real ASIN.

Runs every keepa_* tool on the given ASIN and dumps:
  1. Each tool's structured output
  2. A summary of the raw cached product CSV (last few entries of each
     index, plus non-CSV product fields) so we can verify whether each
     extract tool is decoding the underlying values correctly.

Usage:
    uv run python scripts/keepa_probe.py [ASIN]

Defaults to B0BTZ5HGMB. The script asks for KEEPA_API_KEY interactively
(input hidden) the first time; subsequent runs in the same shell reuse
the env var. Cost is ~5 tokens per ASIN (1 fetch + 1 finder + 1
categories + 1 bestsellers + 1 seller info). The 50-token /topseller
call is intentionally skipped.

Outputs (in scripts/, gitignored — do not commit):
  - keepa_probe_output_<ASIN>.json — tool outputs + raw_csv_summary
  - keepa_probe_raw_<ASIN>.json    — full raw cached product
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_ASIN = "B0BTZ5HGMB"


def _summarize_csv(csv_arrays: list) -> dict:
    """Keep only the last 4 entries of each CSV index — enough to verify
    current-value decoding without dumping hundreds of KB of history."""
    summary: dict = {}
    for i, arr in enumerate(csv_arrays):
        if not arr:
            summary[i] = None
            continue
        summary[i] = {
            "length": len(arr),
            "last_4": arr[-4:] if len(arr) >= 4 else arr,
        }
    return summary


async def main() -> None:
    asin = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ASIN).strip().upper()
    print(f"Probing ASIN: {asin}")

    if not os.environ.get("KEEPA_API_KEY"):
        key = getpass.getpass("Enter your KEEPA_API_KEY (input hidden): ").strip()
        if not key:
            print("No key provided — aborting.")
            sys.exit(1)
        os.environ["KEEPA_API_KEY"] = key

    # Import after env var is set so the module-level read picks it up
    from app.tools import keepa_api as k

    results: dict = {"asin": asin, "tools": {}}

    # Force a fresh fetch by clearing any stale cache for this ASIN
    cache_path = Path(k._CACHE_DIR) / f"{asin}.json"
    if cache_path.exists():
        print(f"Clearing stale cache: {cache_path}")
        cache_path.unlink()

    print("→ keepa_check_tokens (before)")
    results["tools"]["check_tokens_before"] = await k.keepa_check_tokens()
    print(f"   tokens left: {results['tools']['check_tokens_before'].get('tokens_left')}")

    print(f"→ keepa_fetch_product({asin})  [costs 3 tokens]")
    results["tools"]["fetch_product"] = await k.keepa_fetch_product(asin)

    # Capture raw cached payload structure for ground-truth verification
    if cache_path.exists():
        with open(cache_path) as f:
            raw = json.load(f)
        results["raw_csv_summary"] = _summarize_csv(raw.get("csv", []))
        results["raw_non_csv_fields"] = {
            field: raw.get(field)
            for field in [
                "title",
                "brand",
                "monthlySold",
                "monthlySoldHistory",
                "productType",
                "salesRankReference",
                "salesRanks",
                "listedSince",
                "trackingSince",
                "fbaFees",
                "coupon",
                "couponHistory",
                "promotions",
                "isSNS",
                "availabilityAmazon",
                "categoryTree",
                "buyBoxSellerIdHistory",
                "stats",
                "liveOffersOrder",
            ]
        }
        # Also save ALL keys at the top level so we can spot anything we
        # didn't think to look for (promo flags, badges, etc.)
        results["raw_top_level_keys"] = sorted(raw.keys())
    else:
        print("WARNING: cache file not found after fetch — fetch likely failed.")
        results["raw_csv_summary"] = None
        results["raw_non_csv_fields"] = None

    # Sync extract tools (cache reads, 0 tokens)
    print("→ keepa_extract_pricing")
    results["tools"]["extract_pricing"] = k.keepa_extract_pricing(asin)

    print("→ keepa_extract_stats")
    results["tools"]["extract_stats"] = k.keepa_extract_stats(asin)

    print("→ keepa_extract_offers")
    results["tools"]["extract_offers"] = k.keepa_extract_offers(asin)

    print("→ keepa_extract_competitors")
    results["tools"]["extract_competitors"] = k.keepa_extract_competitors(asin)

    for metric in ("rating", "sales_rank", "review_count", "buy_box", "amazon", "lightning_deal"):
        print(f"→ keepa_extract_history(metric={metric}, days=30)")
        results["tools"][f"extract_history_{metric}"] = k.keepa_extract_history(
            asin, metric=metric, days=30
        )

    print("→ keepa_extract_sales_analysis(days=30)")
    results["tools"]["extract_sales_analysis"] = k.keepa_extract_sales_analysis(
        asin, days=30
    )

    # Discovery tools (own API calls, ~1 token each)
    print("→ keepa_get_categories(domain=1)")
    results["tools"]["get_categories_root"] = await k.keepa_get_categories(domain=1)

    cat_id = None
    raw_cats = (results.get("raw_non_csv_fields") or {}).get("categoryTree") or []
    if raw_cats:
        cat_id = raw_cats[-1].get("catId")
    if cat_id:
        print(f"→ keepa_get_bestsellers(domain=1, category={cat_id})")
        results["tools"]["get_bestsellers"] = await k.keepa_get_bestsellers(
            domain=1, category=cat_id
        )

    seller_id = None
    offers_out = results["tools"].get("extract_offers") or {}
    if offers_out.get("status") == "success":
        seller_id = offers_out.get("buy_box_seller")
        if not seller_id:
            for o in offers_out.get("live_offers") or []:
                if o.get("seller_id"):
                    seller_id = o["seller_id"]
                    break
    if seller_id:
        print(f"→ keepa_get_seller_info(domain=1, seller_id={seller_id})")
        results["tools"]["get_seller_info"] = await k.keepa_get_seller_info(
            domain=1, seller_id=seller_id
        )

    print("→ keepa_product_finder (small smoke query)")
    finder_selection = json.dumps(
        {
            "title": "sheet set",
            "current_SALES_gte": 1,
            "current_SALES_lte": 5000,
            "perPage": 5,
        }
    )
    results["tools"]["product_finder"] = await k.keepa_product_finder(
        selection=finder_selection, domain=1
    )

    results["tools"]["get_top_sellers"] = (
        "SKIPPED — costs 50 tokens; uncomment in script to run"
    )

    print("→ keepa_check_tokens (after)")
    results["tools"]["check_tokens_after"] = await k.keepa_check_tokens()

    out_path = Path(__file__).resolve().parent / f"keepa_probe_output_{asin}.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nSaved → {out_path}")
    print(f"Size: {out_path.stat().st_size / 1024:.1f} KB")

    # Also keep a copy of the raw cache for direct inspection
    if cache_path.exists():
        copy_path = Path(__file__).resolve().parent / f"keepa_probe_raw_{asin}.json"
        shutil.copy2(cache_path, copy_path)
        print(f"Raw cache copy → {copy_path}")
        print(f"Raw size: {copy_path.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    asyncio.run(main())
