# Universal Commerce Protocol (UCP) Reference

UCP standardizes the shopping lifecycle into modular capabilities through strongly typed schemas.

## Implementation Pattern (Python SDK)

```python
import httpx, uuid
from ucp_sdk.models.discovery.profile_schema import UcpDiscoveryProfile
from ucp_sdk.models.schemas.shopping.checkout_create_req import CheckoutCreateRequest
from ucp_sdk.models.schemas.shopping.types.line_item_create_req import LineItemCreateRequest

# 1. DISCOVER: Parse the profile
async with httpx.AsyncClient() as c:
    profile = UcpDiscoveryProfile.model_validate(
        (await c.get("http://supplier-url:8182/.well-known/ucp")).json()
    )

# 2. ORDER: Build a typed request
checkout_req = CheckoutCreateRequest(
    currency="USD",
    line_items=[
        LineItemCreateRequest(quantity=10, item=ItemCreateRequest(id="salmon")),
    ],
    payment=PaymentCreateRequest(),
)

# 3. SEND: Create checkout with required UCP headers
headers = {
    "UCP-Agent": 'profile="https://ori.example/agent"',
    "Idempotency-Key": str(uuid.uuid4()), 
    "Request-Id": str(uuid.uuid4())
}

async with httpx.AsyncClient() as c:
    checkout = (await c.post(
        "http://supplier-url:8182/checkout-sessions",
        json=checkout_req.model_dump(), 
        headers=headers
    )).json()
```

## Protocol Discovery
All UCP-compliant endpoints MUST serve their capability profile at:
`GET /.well-known/ucp`
