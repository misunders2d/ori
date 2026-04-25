"""CLI transport package — terminal fallback when no messenger configured."""
# No re-exports here; importing chat.py triggers app.runtime.executor which
# requires Phase G. Callers do `from app.transports.cli.chat import ...`.
