# Structured Logging Reference

## JSON Logging Format

Transition from plain text to structured JSON logs for automated parsing:

```python
import logging, json

class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": getattr(record, "trace_id", None),
        })
```

## Correlation IDs

Every user request should carry a unique `TraceID` through all related log entries. This enables tracing a single request across tools, callbacks, and sub-agents.

## MAPE-K Self-Healing Loop

1. **Monitor**: Watch `data/agent.log` for ERROR/CRITICAL entries.
2. **Analyze**: Extract stack traces, identify root cause file and line.
3. **Plan**: Draft a fix, check git history for prior attempts.
4. **Execute**: Stage -> Verify -> Commit via evolution tools.
5. **Knowledge**: Record the fix pattern to avoid repeating analysis.

## Circuit Breaker Pattern

If the same tool fails 3 times within 5 minutes:
- Stop calling that tool.
- Log a CRITICAL entry.
- Report the failure to the admin rather than burning tokens on retries.

## Rootless Logging

- **`journald` driver**: In rootless Docker, logs integrate into user-space systemd journal (`journalctl --user`).
- **Decoupled ingestion**: Use sidecar collectors (e.g., Fluent Bit) if volume mounting is restricted.
