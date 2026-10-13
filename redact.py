"""Secrets aus Log- und Fehlermeldungen entfernen.

API-Keys von OpenAI und Anthropic beginnen mit ``sk-`` (``sk-proj-…``, ``sk-ant-api03-…``). Die APIs zitieren
den Key in Fehlermeldungen teils selbst – auch gekürzt (``sk-proj-abcd********wxyz``). Deshalb wird alles
maskiert, was wie ein solcher Key oder ein Fragment davon aussieht, unabhängig davon, ob es der konfigurierte
Key ist. ``RedactFilter`` hängt an den Log-Handlern (Cron und Web), ``redact`` wird zusätzlich dort benutzt,
wo Texte direkt in Dateien oder an den Chat gehen.
"""

from __future__ import annotations

import logging
import re

MASK = "sk-***"
# "sk-" am Wortanfang, dann Key-Zeichen; Auslassungen (*, ., …) und der Rest dahinter gehören dazu
_KEY_RE = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]*(?:[*.…]+[A-Za-z0-9_\-]*)*[A-Za-z0-9_\-*…]")


def redact(text: str) -> str:
    """Alles, was wie ein ``sk-…``-Key aussieht (auch gekürzt), durch ``sk-***`` ersetzen."""
    return _KEY_RE.sub(MASK, text) if "sk-" in text else text


class RedactFilter(logging.Filter):
    """Maskiert Keys in jeder Log-Zeile (Nachricht nach dem Einsetzen der Argumente, auch im Traceback)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 – kaputte Format-Argumente nicht verschlimmern
            return True
        clean = redact(msg)
        if clean != msg:
            record.msg, record.args = clean, None
        if record.exc_info and record.exc_info[1] is not None and "sk-" in str(record.exc_info[1]):
            # Traceback würde den Text der Exception noch einmal ausgeben → vorab formatieren und maskieren
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install(*handlers: logging.Handler) -> None:
    """Filter an Handler hängen (ohne Angabe: alle Handler des Root-Loggers), nie doppelt."""
    for h in handlers or tuple(logging.getLogger().handlers):
        if not any(isinstance(f, RedactFilter) for f in h.filters):
            h.addFilter(RedactFilter())
