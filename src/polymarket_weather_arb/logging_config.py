"""Persistent rotating logs with secret redaction for Autopilot paths."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Bare 64-hex or 0x-prefixed private keys (Ethereum secret keys are 32 bytes).
PRIVATE_KEY_PATTERN = re.compile(r"(?i)\b(?:0x)?[0-9a-f]{64}\b")
# Common EVM addresses (40 hex after 0x) — apply after keys so 0x+64 is handled first.
ADDRESS_PATTERN = re.compile(r"(?i)\b0x[a-f0-9]{40}\b")
# Telegram bot tokens: <bot_id>:<secret>
TELEGRAM_TOKEN_PATTERN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
# Any env/config style secret assignment, arbitrary prefix before KEY/SECRET/TOKEN/...
# e.g. POLYMARKET_CLOB_API_KEY=..., MY_RELAYER_API_SECRET: xxx, builder_api_key=...
SECRET_ASSIGN_PATTERN = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*"
    r"(?:"
    r"api[_-]?key|api[_-]?secret|access[_-]?key|access[_-]?secret|"
    r"secret[_-]?key|private[_-]?key|client[_-]?secret|"
    r"(?<![A-Za-z0-9])secret(?![A-Za-z0-9])|"
    r"token|password|passwd|credential|auth[_-]?token|"
    r"relayer[_-]?(?:key|secret|token|api[_-]?key|api[_-]?secret|credential)|"
    r"builder[_-]?(?:key|secret|token|api[_-]?key)"
    r")"
    r"[A-Za-z0-9_.-]*)"
    r"\s*[=:]\s*"
    r"([^\s,;\"']+)"
)
# Authorization: Bearer <token>  or bare Bearer <token>
BEARER_HEADER_PATTERN = re.compile(
    r"(?i)\b(authorization\s*:\s*bearer)\s+(\S+)"
)
BEARER_INLINE_PATTERN = re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9\-._~+/]+=*)")


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


class ExpectedSdkAuthBootstrapFilter(logging.Filter):
    """Hide only the beta SDK's expected create-then-derive 400 control flow."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "httpx":
            return True
        message = record.getMessage()
        return not (
            "POST https://clob.polymarket.com/auth/api-key" in message
            and " 400 Bad Request" in message
        )


def redact_text(text: str) -> str:
    """Redact secrets from a log line (public helper for tests)."""
    message = SECRET_ASSIGN_PATTERN.sub(r"\1=[REDACTED]", text)
    message = BEARER_HEADER_PATTERN.sub(r"\1 [REDACTED_BEARER]", message)
    message = BEARER_INLINE_PATTERN.sub(r"\1 [REDACTED_BEARER]", message)
    message = PRIVATE_KEY_PATTERN.sub("[REDACTED_PK]", message)
    message = TELEGRAM_TOKEN_PATTERN.sub("[REDACTED_TELEGRAM_TOKEN]", message)
    message = ADDRESS_PATTERN.sub("[REDACTED_ADDRESS]", message)
    return message


def setup_persistent_logging(data_dir: Path) -> Path:
    """Attach a redacting rotating file handler under ``data_dir/logs/autopilot.log``.

    Safe to call multiple times; only one RotatingFileHandler is added.
    Returns the log file path.
    """
    log_dir = Path(data_dir).expanduser().resolve() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "autopilot.log"

    handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    formatter = RedactingFormatter(
        fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)
    handler.addFilter(ExpectedSdkAuthBootstrapFilter())

    root_logger = logging.getLogger()
    if not any(isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
        root_logger.addHandler(handler)
        if root_logger.level > logging.INFO or root_logger.level == logging.NOTSET:
            root_logger.setLevel(logging.INFO)
    return log_file
