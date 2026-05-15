"""v2 scheduler transport wiring (production seams).

The protocol-typed adapters under :mod:`app.v2.emit` keep
no vendor SDKs at module load. The production wiring lives
HERE -- separate namespace so the emit layer's import
hygiene pins stay tight while production can pull in
``httpx`` / Slack tokens / etc.
"""
