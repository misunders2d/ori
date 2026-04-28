# Keepa Product Finder

`keepa_product_finder(selection, domain=1)` searches Keepa's database with structured filters and returns a list of matching ASINs. **Token cost: ~1.** It does not return product data — feed the ASINs into `keepa_fetch_product` for each one you want to analyze.

## Selection JSON

`selection` is a **JSON string** (not a dict — the tool will `json.loads` it). Almost any product field can be filtered or sorted on. The convention:

- **Filters:** `<field>_gte` (≥), `<field>_lte` (≤), or just `<field>` for exact match / membership.
- **Time-windowed metrics:** `current_<METRIC>`, `avg30_<METRIC>`, `avg90_<METRIC>`, `avg180_<METRIC>`, `avg365_<METRIC>`. METRIC is the CSV constant (e.g., `SALES`, `NEW`, `BUY_BOX_SHIPPING`, `COUNT_REVIEWS`, `RATING`, `MONTHLY_SOLD`).
- **List filters:** `categories_include`, `categories_exclude`, `productType`, `brand`, etc.
- **Sort:** `"sort": [["<field>", "asc"|"desc"], ...]`.
- **Pagination:** `"page": 0, "perPage": 50` (max 10,000 results across pages).

## Common filter patterns

**Find products in a category with a sales-rank window:**
```json
{
  "categoryIds": [1063252],
  "current_SALES_gte": 1,
  "current_SALES_lte": 5000,
  "productType": [0],
  "perPage": 50
}
```

**High-revenue opportunities (good rank, lots of reviews, decent price):**
```json
{
  "rootCategory": 1055398,
  "current_SALES_lte": 10000,
  "current_COUNT_REVIEWS_gte": 100,
  "current_BUY_BOX_SHIPPING_gte": 2000,
  "current_BUY_BOX_SHIPPING_lte": 10000,
  "productType": [0],
  "hasParentASIN": false,
  "sort": [["current_SALES", "asc"]],
  "perPage": 50
}
```

**Title keyword + recent rank trend:**
```json
{
  "title": "bed sheets",
  "avg30_SALES_lte": 3000,
  "avg90_SALES_gte": 5000,
  "perPage": 25
}
```
(Reads as: rank averaged better recently than over the longer window — a product trending up.)

**Filter by monthly-sold tier:**
```json
{
  "current_MONTHLY_SOLD_gte": 1000,
  "categoryIds": [1063252]
}
```

## Notes on units

- Prices in selection JSON are **integer cents** (`current_BUY_BOX_SHIPPING_gte: 2000` = $20.00).
- Rank fields are integers (lower = better selling).
- `RATING` is on the 0–50 scale (45 = 4.5★).
- `productType: [0]` filters to "standard products" — usually what you want. `[5]` would be variation parents (no useful data).
- `hasParentASIN: false` excludes variation children whose parent rolls them up — useful when you want unique buyable products and not 12 colorways.

## Generating selection JSON the easy way

If a user describes a complex filter set you can't translate confidently, the easiest path is to set the filters in [Keepa's web Product Finder UI](https://keepa.com/#!finder) and copy the JSON from the **"SHOW API QUERY"** link at the bottom. Suggest this to the user when their criteria are intricate (multi-category + brand exclusion + price + rank + review velocity).

## Returns

```python
{
  "status": "success",
  "tokens_left": 1230,
  "asins": ["B08...", "B09...", "B0C..."]
}
```

If `asins` is empty, your filter set was too tight or the category has no products matching. Loosen one constraint at a time.
