"""Process-wide logging safeguards for third-party HTTP clients.

The HTTP clients used by the controller put credentials in request headers,
query parameters, or Telegram URL paths.  Their INFO request logs are not
useful in production and can disclose those values, so this module both
raises their default level and sanitizes records before any handler sees them.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

_REDACTED = "[REDACTED]"
_MARKER = "__APEX_REDACTED__"
_SENSITIVE_VALUES: set[str] = set()

# Covers snake_case, kebab-case, and camelCase spellings used by the clients
# and common reverse proxies.  The expression is deliberately limited to
# credential-bearing names rather than every occurrence of the word "key".
_KEY = (
    r"[A-Za-z0-9_-]*?(?:api[-_]?key(?:[-_]?encrypted)?|"
    r"(?:wecom[-_]?)?encoding[-_]?aes[-_]?key|"
    r"corp[-_]?secret|access[-_]?token|secret(?:[-_]?key)?|"
    r"password(?:[-_]?hash)?|passwd|pw|private[-_]?key|authorization|"
    r"webhook[-_]?url|set[-_]?cookie|cookie|session[-_]?token|token)"
    r"[A-Za-z0-9_-]*"
)
_QUOTED_PAIR = re.compile(
    rf"(?i)(?P<key>\"?{_KEY}\"?)(?P<sep>\s*[:=]\s*)(?P<quote>[\"'])(?P<value>.*?)(?P=quote)"
)
_PAIR = re.compile(
    rf"(?i)(?P<key>\b{_KEY}\b)(?P<sep>\s*[:=]\s*)(?P<value>[^\s,;&}}\]>'\"]+)"
)
_SPACE_PAIR = re.compile(
    rf"(?i)(?P<key>\b{_KEY}\b)(?P<sep>\s+)(?P<value>[^\s,;&}}\]>'\"=:]+)"
)
_BEARER = re.compile(r"(?i)(\bAuthorization\b\s*[:=]\s*Bearer\s+)[^\s,;\]}]+")
_AUTH_DOUBLE = re.compile(r"(?i)(\bAuthorization\b\s*[:=]\s*)" + re.escape(_MARKER) + r"\s+" + re.escape(_MARKER))
_TELEGRAM_BOT = re.compile(r"(?i)(/bot)[^/\s?]+(?=/)")
_URL_QUERY = re.compile(r"(?P<url>https?://[^\s\"']+?)(?P<mark>[?#])(?P<query>[^\s\"']+)")
_URL_USERINFO = re.compile(r"(?i)(https?://)([^/@\s:]+)(?::([^/@\s]+))?@")
_HTTP_PAYLOAD = re.compile(
    r"(?i)(\b(?:request[_-]?)?(?:headers?|body|content)\b\s*[:=]\s*)"
    r"(?:\{[^}]*\}|\[[^\]]*\]|\"[^\"]*\"|'[^']*'|[^\s]+)"
)


def register_sensitive_values(*values: Any) -> None:
    """Register non-empty runtime credentials for exact-value redaction.

    Values are retained only in process memory.  This catches a credential
    appearing in an exception without a key name, while the key-based rules
    above cover logs emitted before a client has registered its values.
    """

    for value in values:
        text = str(value or "").strip()
        if text and len(text) >= 3:
            # Do not register a complete URL: replacing it wholesale would
            # remove the useful host/path from a diagnostic line.  Register
            # only userinfo and query values, preserving readable URL shape.
            if "://" in text:
                try:
                    parsed = urlsplit(text)
                except ValueError:
                    parsed = None
                if parsed is not None:
                    for candidate in (parsed.username, parsed.password):
                        if candidate and len(candidate) >= 3:
                            _SENSITIVE_VALUES.add(candidate)
                    for _name, candidate in parse_qsl(parsed.query, keep_blank_values=True):
                        if candidate and len(candidate) >= 3:
                            _SENSITIVE_VALUES.add(candidate)
                    continue
            _SENSITIVE_VALUES.add(text)


def _redact_url_query(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        query = match.group("query")
        # Preserve readable parameter names while removing every value.  This
        # is used only for HTTP client records; application audit details use
        # the more precise key-based substitutions below.
        parts = []
        for part in re.split(r"&", query):
            if "=" in part:
                name, _sep, _raw = part.partition("=")
                parts.append(f"{name}={_MARKER}")
            else:
                parts.append(_MARKER)
        return f"{match.group('url')}{match.group('mark')}{'&'.join(parts)}"

    return _URL_QUERY.sub(replace, value)


def _replace_quoted_pair(match: re.Match[str]) -> str:
    inner = match.group("value")
    safe = f"Bearer {_MARKER}" if re.match(r"(?i)^Bearer\s+", inner) else _MARKER
    return f"{match.group('key')}{match.group('sep')}{match.group('quote')}{safe}{match.group('quote')}"


def redact_text(value: Any, *, http_logger: bool = False) -> str:
    """Return a log-safe string without changing ordinary business wording."""

    text = str(value)
    # Apply credential-key patterns before exact-value replacement.  A weak
    # but valid configured secret such as ``secret`` may itself be part of a
    # key name (``secret_key``); replacing it first would destroy the key
    # boundary and leave the value after the equals sign visible.
    text = _TELEGRAM_BOT.sub(rf"\1{_MARKER}", text)
    text = _BEARER.sub(rf"\1{_MARKER}", text)
    text = _QUOTED_PAIR.sub(_replace_quoted_pair, text)
    text = _PAIR.sub(lambda m: f"{m.group('key')}{m.group('sep')}{_MARKER}", text)
    text = _SPACE_PAIR.sub(lambda m: f"{m.group('key')}{m.group('sep')}{_MARKER}", text)
    text = _AUTH_DOUBLE.sub(rf"\1Bearer {_MARKER}", text)
    # Exact replacement still catches credentials embedded in unstructured
    # exception text after structured key/value pairs have been sanitized.
    for secret in sorted(_SENSITIVE_VALUES, key=len, reverse=True):
        text = text.replace(secret, _MARKER)
    if http_logger:
        # HTTP transport diagnostics must never include header/body payloads;
        # remove those segments entirely and keep only request metadata.
        text = _HTTP_PAYLOAD.sub("", text)
    # Query values are never useful in a log line and can contain credentials
    # under arbitrary parameter names.  Redact them for both application and
    # third-party records, while retaining the URL host/path and parameter
    # names for troubleshooting.
    text = _redact_url_query(text)
    text = _URL_USERINFO.sub(rf"\1{_MARKER}@", text)
    return text.replace(_MARKER, _REDACTED)


class CredentialRedactionFilter(logging.Filter):
    """Sanitize a record in-place so every kind of handler sees safe data."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        logger_name = record.name.lower()
        is_http = logger_name == "httpx" or logger_name.startswith("httpx.") or logger_name == "httpcore" or logger_name.startswith("httpcore.")
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        safe = redact_text(rendered, http_logger=is_http)
        if safe != rendered:
            record.msg = safe
            record.args = ()

        if record.exc_info:
            exc_type = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            # Tracebacks include exception arguments, request URLs, headers,
            # and often bodies.  Keep only the exception class in the emitted
            # record; the message itself has already gone through redaction.
            record.exc_info = None
            record.exc_text = None
            record.msg = f"{safe} error_type={exc_type}"
            record.args = ()
        return True


_FILTER = CredentialRedactionFilter()
_CONFIGURED = False
_ORIGINAL_FACTORY: Any = None
_CURRENT_LEVEL = logging.WARNING


def set_log_level(level: str | int) -> None:
    """Apply the administrator-selected level to application loggers.

    Transport client loggers intentionally remain at WARNING so changing the
    application level cannot re-enable credential-bearing httpx/httpcore
    request diagnostics.
    """
    global _CURRENT_LEVEL
    if not _CONFIGURED:
        configure_logging()
    if isinstance(level, str):
        normalized = level.strip().upper()
        numeric = getattr(logging, normalized, None)
        if not isinstance(numeric, int):
            numeric = logging.WARNING
    else:
        numeric = int(level)
        normalized = logging.getLevelName(numeric)
    _CURRENT_LEVEL = numeric
    root = logging.getLogger()
    root.setLevel(numeric)
    # Uvicorn and APScheduler may install explicit levels on their child
    # loggers; align them with the runtime setting to suppress noisy INFO logs.
    for name in ("uvicorn.access", "uvicorn.error", "apscheduler", "apscheduler.scheduler", "apscheduler.executors.default"):
        logging.getLogger(name).setLevel(numeric)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def configure_logging() -> None:
    """Install safe defaults once for admin, portal, and combined startup."""

    global _CONFIGURED, _ORIGINAL_FACTORY
    if _CONFIGURED:
        return
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    for name in ("httpx", "httpcore"):
        client_logger = logging.getLogger(name)
        client_logger.setLevel(logging.WARNING)
        client_logger.addFilter(_FILTER)
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(_FILTER)

    _ORIGINAL_FACTORY = logging.getLogRecordFactory()

    def safe_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = _ORIGINAL_FACTORY(*args, **kwargs)
        _FILTER.filter(record)
        return record

    logging.setLogRecordFactory(safe_factory)
    _CONFIGURED = True
    set_log_level(_CURRENT_LEVEL)


__all__ = [
    "configure_logging",
    "set_log_level",
    "register_sensitive_values",
    "redact_text",
    "CredentialRedactionFilter",
]
