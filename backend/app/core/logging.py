from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import structlog

SECRET_KEYS = re.compile(r"(api[_-]?key|authorization|password|secret|token)", re.IGNORECASE)
BEARER_PATTERN = re.compile(r"(?i)(bearer\s+)[^\s,;]+")


def sanitize_url(value: str | None) -> str | None:
    if not value:
        return value
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return "<invalid-url>"


def redact_text(value: str, secrets: tuple[str, ...] = ()) -> str:
    result = BEARER_PATTERN.sub(r"\1[REDACTED]", value)
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    return result


def redact_secrets(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if SECRET_KEYS.search(str(key)):
                redacted[key] = "[REDACTED]"
            elif (
                key in {"api_base", "jwks_url", "url", "base_url"}
                or str(key).endswith(("_api_base", "_url"))
            ) and isinstance(item, str):
                redacted[key] = sanitize_url(item)
            else:
                redacted[key] = redact_secrets(item, secrets)
        return redacted
    if isinstance(value, (list, tuple)):
        return type(value)(redact_secrets(item, secrets) for item in value)
    if isinstance(value, str):
        return redact_text(value, secrets)
    return value


def redacting_processor(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return redact_secrets(event_dict)


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(format="%(message)s", level=level, force=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redacting_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
