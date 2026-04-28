# Keepa `product_finder` selection JSON

## The exact `selection` argument

Pass this as a **JSON string** to `keepa_product_finder(selection=..., domain=1)` (the tool will `json.loads` it):

```json
{
  "categoryIds": [1063252],
  "current_SALES_gte": 1,
  "current_SALES_lte": 5000,
  "current_COUNT_REVIEWS_gte": 100,
  "current_BUY_BOX_SHIPPING_gte": 1500,
  "current_BUY_BOX_SHIPPING_lte": 5000,
  "productType": [0],
  "hasParentASIN": false,
  "sort": [["current_SALES", "asc"]],
  "perPage": 25
}
```

### Field-by-field mapping to the request

| User criterion | Selection key | Value |
|---|---|---|
| Category id 1063252 | `categoryIds` | `[1063252]` |
| Sales rank 1–5000 | `current_SALES_gte` / `current_SALES_lte` | `1` / `5000` |
| At least 100 reviews | `current_COUNT_REVIEWS_gte` | `100` |
| Standard products only (no variation parents) | `productType` | `[0]` |
| No variation parents (also exclude rolled-up children) | `hasParentASIN` | `false` |
| Buy box price $15–$50 | `current_BUY_BOX_SHIPPING_gte` / `_lte` | `1500` / `5000` |
| Sort by sales rank ascending | `sort` | `[["current_SALES", "asc"]]` |
| Return 25 results | `perPage` | `25` |

Notes on the choices above:

- `productType: [0]` is the "standard products" filter per the skill — `[5]` would be variation parents.
- `hasParentASIN: false` is added on top because "no variation parents" combined with the implicit goal of unique buyable products typically means you also don't want variation children that share their parent's BSR. If you specifically *do* want children, drop that one line.
- "Sort by sales rank ascending" maps to `current_SALES asc` because in Keepa, lower BSR = better-selling, so ascending sort puts the best sellers first.

## Real-world gotcha with the price units

**Prices in the selection JSON are integer cents, not dollars.** `current_BUY_BOX_SHIPPING` (and every other price field) expects an integer like `1500` for $15.00 and `5000` for $50.00. If you pass `15` and `50` thinking they're dollars, you will silently filter for products with a buy-box price between $0.15 and $0.50 and get either zero results or a list of pure junk SKUs — not an error, just a wrong answer. Same trap applies if you accidentally pass a float (`15.00`); stick to integer cents.

(Related but separate: the `RATING` field, if you ever add it, is on a 0–50 scale where 45 = 4.5 stars — another easy unit miss.)
