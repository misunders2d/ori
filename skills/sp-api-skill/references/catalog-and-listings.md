# Catalog & Listings Reference

Two related but distinct surfaces. **Catalog** = shared product data (title, images, bullets) that's the same for every seller of that ASIN. **Listing** = *your* seller-specific record for one of your SKUs (price, inventory, fulfillment channel, policy issues). Research/discovery uses catalog; inventory/pricing management uses listings.

## Tools

### `sp_get_catalog_item(asin, included_data=...)`
Returns one product's catalog data by ASIN. `included_data` is comma-separated — request only what you need because each section adds response size.

Options: `summaries, attributes, identifiers, images, productTypes, salesRanks, relationships, classifications, dimensions, vendorDetails`

### `sp_search_catalog(keywords="", identifiers="", identifier_type="ASIN", included_data=..., page_size=10)`
Search the catalog either by keywords (e.g. "bed sheets queen") or by identifiers (comma-separated, e.g. "B0123456789,B0987654321"). One of the two must be provided. `identifier_type` supports `ASIN, EAN, GTIN, ISBN, JAN, MINSAN, SKU, UPC`.

### `sp_get_listing(sku, included_data=...)`
Your seller-specific listing for a SKU — the "what does Amazon think about my listing" view. This is where you find out about issues, current offer price, fulfillment availability, etc.

Options: `summaries, attributes, issues, offers, fulfillmentAvailability, procurement, relationships, productTypes`

## When to use which

| Question | Tool |
|---|---|
| "What's this ASIN's title / images / brand?" | `sp_get_catalog_item` |
| "Find products matching 'memory foam pillow'" | `sp_search_catalog(keywords=...)` |
| "Look up ASINs by their UPCs" | `sp_search_catalog(identifiers=..., identifier_type='UPC')` |
| "What's wrong with my listing for SKU FOO-123?" | `sp_get_listing(sku='FOO-123', included_data='issues')` |
| "What's my current offer price and stock?" | `sp_get_listing(sku=..., included_data='offers,fulfillmentAvailability')` |

## Gotcha: SKU → ASIN translation

A SKU belongs to you; an ASIN is global. If the user gives you an ASIN and you need listing-level data, you need to know the seller's SKU for that ASIN. There's no direct SKU-from-ASIN lookup in these tools — pull it from a listings report (`GET_MERCHANT_LISTINGS_ALL_DATA` or `GET_FLAT_FILE_OPEN_LISTINGS_DATA`) or from `sp_list_reports` if one exists.

## Live references

- [Catalog Items API v2022-04-01](https://developer-docs.amazon.com/sp-api/docs/catalog-items-api-v2022-04-01-reference)
- [Listings Items API v2021-08-01](https://developer-docs.amazon.com/sp-api/docs/listings-items-api-v2021-08-01-reference)
- [Official model: catalog-items](https://github.com/amzn/selling-partner-api-models/tree/main/models/catalog-items-api-model)
- [Official model: listings-items](https://github.com/amzn/selling-partner-api-models/tree/main/models/listings-items-api-model)
