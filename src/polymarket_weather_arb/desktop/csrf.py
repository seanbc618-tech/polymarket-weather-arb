"""Per-process CSRF and exact-origin checks for dashboard POSTs.

Localhost alone is not enough against browser-originated requests. This is a
narrow request-integrity layer; remote user authentication remains owned by the
Cloudflare Access boundary.
"""

from __future__ import annotations

import hmac
import re
import secrets
from typing import Mapping
from urllib.parse import urlsplit

_PROCESS_TOKEN = secrets.token_urlsafe(32)
CSRF_FIELD = "csrf_token"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
_POST_FORM_RE = re.compile(
    r"(<form\b(?=[^>]*\bmethod\s*=\s*([\"'])post\2)[^>]*>)",
    re.IGNORECASE,
)


def csrf_token() -> str:
    return _PROCESS_TOKEN


def verify_csrf(form_token: str | None) -> bool:
    if not form_token:
        return False
    return hmac.compare_digest(str(form_token), _PROCESS_TOKEN)


def verify_local_host(host_header: str | None) -> bool:
    """Accept only expected local Host headers when a header is present."""
    if host_header is None or not str(host_header).strip():
        # Some test clients omit Host; do not hard-fail in that case.
        return True
    host = str(host_header).strip().lower()
    # Strip port
    if host.startswith("["):
        # [::1]:port
        end = host.find("]")
        hostname = host[: end + 1] if end != -1 else host
    else:
        hostname = host.split(":", 1)[0]
    return hostname in LOCAL_HOSTS


def _canonical_origin(value: str | None) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw.lower() == "null":
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if (
        parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    hostname = parsed.hostname.rstrip(".").lower()
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme.lower()}://{hostname}{suffix}"


def _canonical_host(value: str | None, *, scheme: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    try:
        parsed = urlsplit(f"{scheme}://{str(value).strip()}")
        port = parsed.port
    except ValueError:
        return None
    if not parsed.hostname or parsed.username or parsed.password:
        return None
    hostname = parsed.hostname.rstrip(".").lower()
    default_port = 443 if scheme == "https" else 80
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{hostname}{suffix}"


def require_sensitive_post(
    form: Mapping[str, str],
    *,
    host_header: str | None = None,
    origin_header: str | None = None,
    allowed_public_origin: str | None = None,
) -> None:
    """Raise ValueError when CSRF or local host checks fail."""
    if not verify_csrf(form.get(CSRF_FIELD)):
        raise ValueError("invalid or missing CSRF token")
    if allowed_public_origin:
        expected_origin = _canonical_origin(allowed_public_origin)
        if expected_origin is None:
            raise ValueError("invalid configured public dashboard origin")
        expected = urlsplit(expected_origin)
        expected_host = _canonical_host(expected.netloc, scheme=expected.scheme)
        request_host = _canonical_host(host_header, scheme=expected.scheme)
        if request_host != expected_host:
            raise ValueError("refusing unexpected Host header for sensitive action")
        if _canonical_origin(origin_header) != expected_origin:
            raise ValueError("refusing missing or unexpected Origin for sensitive action")
        return
    if not verify_local_host(host_header):
        raise ValueError("refusing non-local Host header for sensitive action")
    if origin_header:
        origin = _canonical_origin(origin_header)
        if origin is not None and urlsplit(origin).hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("refusing non-local Origin for sensitive action")


def csrf_hidden_input() -> str:
    from html import escape

    return f'<input type="hidden" name="{CSRF_FIELD}" value="{escape(csrf_token())}">'


def inject_csrf_into_post_forms(html: str) -> str:
    """Add one process token to every POST form rendered by the dashboard."""
    hidden = csrf_hidden_input()
    return _POST_FORM_RE.sub(lambda match: f"{match.group(1)}{hidden}", html)
