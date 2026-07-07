#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# pylint: disable=logging-fstring-interpolation,protected-access,line-too-long
"""
logging_setup.py — Logging-Infrastruktur für Solar Batterie Manager
=====================================================================
Ausgelagert aus battery_manager.py ab v3.0.10.2.

Enthält:
  - DeduplicatingFilter : logging.Filter mit Heartbeat-Mechanismus
  - setup_logging()     : Logger, FileHandler, StreamHandler konfigurieren

Importiert von: battery_manager.py, dashboard.py
"""

import logging
import logging.handlers
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────
# Deduplizierungs-Filter
# ─────────────────────────────────────────────

class DeduplicatingFilter(logging.Filter):
    """
    Deduplizierungs-Filter mit Heartbeat (v3.0.9.19).

    Unterdrückt aufeinanderfolgende identische Log-Nachrichten.
    Erzwingt trotzdem alle N Minuten einen Heartbeat-Log, damit man
    sieht dass das Programm noch lebt  -  auch wenn sich der Zustand
    nicht geändert hat.

    Der Heartbeat-Marker wird direkt ins record.msg geschrieben,
    kein separater Formatter nötig.

    Konfigurierbar via config.yaml:
      logging:
        dedup_heartbeat_minutes: 20   # Default: 20 Minuten
        dedup_enabled: true           # Default: true
    """
    def __init__(self, heartbeat_minutes: float = 20.0, enabled: bool = True):
        super().__init__()
        self._enabled = enabled
        self._heartbeat_s = heartbeat_minutes * 60.0
        self._last_msg: str = ""
        self._last_ts: float = 0.0
        self._lock = threading.Lock()
        # Handler-Referenz für emit_heartbeat_if_due(); wird von
        # setup_logging() nach Handler-Erstellung gesetzt.
        self._handler: Optional[logging.Handler] = None

    def _normalize(self, msg: str) -> str:
        """
        Normalisiert Log-Nachrichten für den Vergleich.
        Entfernt variable Teile (Datum, Zeit, IP, Pfad) aus bekannten
        Log-Mustern, damit z.B. alle Dashboard-HTTP-Requests (GET /,
        GET /api/state, ...) als identisch erkannt werden.

        Behandelte Muster:
        1. Werkzeug Access-Log (normales Format):
           '192.168.168.60 - - [06/Jun/2026 13:04:19] "GET /api/state HTTP/1.1" 200 -'
        2. Werkzeug Access-Log mit %s-Platzhaltern (internes Format, noch
           nicht durch getMessage() expandiert  -  tritt auf wenn record.args
           nicht None ist):
           '"%s" %s %s'  ->  normalisiert auf "HTTP_ACCESS"
        """
        if re.match(
                r'^[\d\.]+\s+-\s+-\s+\[.+?\]\s+"(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+\S+\s+HTTP/\d\.\d"\s+\d+',
                msg):
            return "HTTP_ACCESS"
        if re.match(r'^"%s"\s+%s\s+%s', msg):
            return "HTTP_ACCESS"
        return msg

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._enabled:
            return True
        formatted = record.getMessage()
        msg = self._normalize(formatted)
        now = time.monotonic()

        with self._lock:
            if msg == self._last_msg:
                if (now - self._last_ts) >= self._heartbeat_s:
                    self._last_ts = now
                    record.msg = formatted + " (Heartbeat)"
                    record.args = None
                    return True
                return False
            self._last_msg = msg
            return True

    def emit_heartbeat_if_due(self) -> None:
        """
        Vom Hintergrund-Thread aufgerufen (alle 60s). Prüft ob der
        Heartbeat fällig ist und schreibt einen synthetischen LogRecord
        direkt an self._handler  -  ohne erneut durch filter() zu laufen.
        Dadurch wird _last_msg nicht durch einen Fremd-String korrumpiert,
        und der nächste echte Log-Eintrag wird weiterhin korrekt dedupliziert.
        """
        if not self._enabled or self._handler is None:
            return
        now = time.monotonic()
        with self._lock:
            if not self._last_msg:
                return
            # v3.0.11.3: Nur fuer HTTP_ACCESS feuern. Fuer alle anderen
            # Nachrichten ist filter() zustaendig (schreibt "(Heartbeat)"
            # direkt ins record.msg). Beide Pfade gleichzeitig auszuloesen
            # erzeugte Doppel-Heartbeats im Journal (Race Condition).
            if self._last_msg != "HTTP_ACCESS":
                return
            if (now - self._last_ts) < self._heartbeat_s:
                return
            self._last_ts = now
        text = "- (Heartbeat: kein Browser-Request seit 20min)"
        r = logging.LogRecord(
            name="heartbeat", level=logging.INFO,
            pathname="", lineno=0, msg=text, args=None, exc_info=None)
        self._handler.emit(r)


# ─────────────────────────────────────────────
# Logging einrichten
# ─────────────────────────────────────────────

def setup_logging(cfg: dict) -> tuple[logging.Logger, Optional[DeduplicatingFilter], Optional[DeduplicatingFilter]]:
    """Konfiguriert Logger mit RotatingFileHandler und StreamHandler inkl. Deduplizierung."""
    log_cfg = cfg.get("logging", {})
    log_file = log_cfg.get("file", "battery_manager.log")
    log_level = getattr(logging, log_cfg.get("level", "INFO"))
    max_bytes = log_cfg.get("max_size_mb", 10) * 1024 * 1024
    backup = log_cfg.get("backup_count", 3)
    dedup_enabled   = log_cfg.get("dedup_enabled", True)
    dedup_heartbeat = log_cfg.get("dedup_heartbeat_minutes", 20.0)

    logger = logging.getLogger("BatteryManager")
    if logger.handlers:
        return logger, None, None
    logger.setLevel(log_level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    is_systemd = bool(os.environ.get("INVOCATION_ID"))

    # Je eine eigene Dedup-Instanz pro Handler (v3.0.9.19).
    # FileHandler und StreamHandler duerfen NICHT dieselbe Instanz teilen:
    # der FileHandler wuerde bei jedem [IDLE]-Durchlauf _last_ts aktualisieren
    # und damit den Heartbeat-Timer des StreamHandlers (Journal) stoeren.
    dedup_file   = DeduplicatingFilter(heartbeat_minutes=dedup_heartbeat, enabled=dedup_enabled)
    dedup_stream = DeduplicatingFilter(heartbeat_minutes=dedup_heartbeat, enabled=dedup_enabled)

    if is_systemd:
        if log_file and log_file.lower() not in ("", "null", "/dev/null", "journal"):
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=max_bytes, backupCount=backup)
            fh.setFormatter(fmt)
            fh.addFilter(dedup_file)
            dedup_file._handler = fh
            logger.addHandler(fh)
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        ch.addFilter(dedup_stream)
        dedup_stream._handler = ch
        logger.addHandler(ch)
    else:
        if log_file:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=max_bytes, backupCount=backup)
            fh.setFormatter(fmt)
            fh.addFilter(dedup_file)
            dedup_file._handler = fh
            logger.addHandler(fh)
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        ch.addFilter(dedup_stream)
        dedup_stream._handler = ch
        logger.addHandler(ch)

    if dedup_enabled:
        logger.info(
            f"Deduplizierung aktiv: identische Zeilen werden unterdrueckt, "
            f"Heartbeat alle {dedup_heartbeat:.0f} Minuten")

    logger.propagate = False
    return logger, dedup_file, dedup_stream
