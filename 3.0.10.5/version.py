"""
version.py — Einzige Stelle für den Versionsstring.

Importiert von:
  battery_manager.py  (logger.info, sys.exit-Meldung)
  dashboard.py        (HTML-Titel, h1)
  controller.py       (optional, falls Versionierung in state.json gewünscht)

Versionsschema: MAJOR.MINOR.PATCH.BUILD
  MAJOR : Breaking changes / komplette Umstrukturierung
  MINOR : Neue Features / Module
  PATCH : Bugfixes, Refactoring
  BUILD : Inkrementell innerhalb eines Releases (Iterations-Suffix)
"""

VERSION = "3.0.11.4"
