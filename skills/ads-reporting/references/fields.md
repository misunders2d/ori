# Valid Fields and Filters

## Required Fields

- `dateRange.value` is required and must always be included in `fields`. The API requires exactly one time-related dimension.
- `budgetCurrency.value` is required and must always be included in `fields`.

## Dimensions

- `adBreak.id`
- `adProduct.value`
- `adSlot.id`
- `adSlot.type`
- `advertiserAccount.id`
- `advertiserAccount.managerAccounts`
- `advertiserAccount.name`
- `audienceSegment.id`
- `audienceSegment.name`
- `browserName.value`
- `budgetCurrency.value`
- `campaign.country`
- `campaign.globalCampaignId`
- `campaign.id`
- `campaign.name`
- `contentGenre.value`
- `contentRating.value`
- `contentTitle.value`
- `contentType.value`
- `convertedProduct.brand`
- `convertedProduct.category`
- `convertedProduct.id`
- `convertedProduct.parentProductId`
- `convertedProductMarketplace.value`
- `country.name`
- `dateRange.value`
- `deviceType.value`
- `environment.value`
- `event.id`
- `event.inventoryType`
- `event.name`
- `event.property`
- `operatingSystem.value`
- `region.name`
- `searchTerm.value`
- `siteApp.value`
- `supplySource.id`
- `supplySource.name`
- `target.matchType`
- `target.value`

## Metrics

- `metric.averageUserImpressionFrequency`
- `metric.clicks`
- `metric.costPerDetailPageView`
- `metric.costPerNewToBrandPurchase`
- `metric.costPerPurchase`
- `metric.costPerPurchasePromoted`
- `metric.cpc`
- `metric.ctr`
- `metric.detailPageViewRate`
- `metric.detailPageViews`
- `metric.impressions`
- `metric.impressionShare`
- `metric.impressionShareRank`
- `metric.longTermRoas`
- `metric.longTermSales`
- `metric.newToBrandPurchaseRate`
- `metric.newToBrandPurchases`
- `metric.newToBrandRoas`
- `metric.newToBrandSales`
- `metric.newToBrandUnitsSold`
- `metric.purchaseRate`
- `metric.purchaseRateOverClicks`
- `metric.purchaseRatePromoted`
- `metric.purchases`
- `metric.purchasesPromoted`
- `metric.roas`
- `metric.roasPromoted`
- `metric.sales`
- `metric.salesPromoted`
- `metric.supplyCost`
- `metric.topOfSearchImpressionShare`
- `metric.totalCost`
- `metric.unitsSold`
- `metric.unitsSoldPromoted`
- `metric.userReach`
- `metric.vctr`
- `metric.viewableImpressions`

## Filters

Only the following values are valid for `filters`:
- `campaign.id`
- `campaign.country`
- `adProduct.value`
- `campaignCountry.value`
