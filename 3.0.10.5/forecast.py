#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
forecast.py — PV-Prognose für Solar Batterie Manager
=====================================================
Ausgelagert aus battery_manager.py ab v3.0.10.3.

Enthält:
  - VrmForecastManager  : Victron VRM API (primär, anlagenspezifisch)
  - ForecastManager     : Prioritätskette VRM -> Solcast -> Open-Meteo -> Dummy
                          + dynamisches Nachtfenster + Sonnenzeiten-Berechnung

Importiert von: battery_manager.py (Instanziierung in main()),
                controller.py (self.forecast)
"""

import math
import logging
import time
from datetime import datetime, timedelta, date, timezone
from typing import Optional

import requests

from models import HourlyForecast


# ─────────────────────────────────────────────
# VRM API Prognose
# ─────────────────────────────────────────────

class VrmForecastManager:
    """
    Holt PV- und Verbrauchsprognose direkt von der Victron VRM API.

    Vorteile gegenueber Open-Meteo:
    - Solcast-Satellitendaten + ML-Modell auf deiner Anlagenhistorie
    - Anlagenspezifisch kalibriert (kennt deine realen Ertragsdaten)
    - Zusaetzlich: Verbrauchsprognose basierend auf historischem Muster
    - Rollierende Tagesprognose: Restwert + bereits erzeugte Energie

    Voraussetzungen:
    - Anlage muss in VRM registriert sein (mind. 30 Tage Daten)
    - VRM Access Token (VRM Portal -> Einstellungen -> Integrationen -> Access Tokens)
    - Installation ID (Zahl in der VRM-URL: vrm.victronenergy.com/installation/XXXXX)

    Fallback: bei Fehler wird Open-Meteo verwendet.
    """

    BASE_URL = "https://vrmapi.victronenergy.com/v2"

    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg    = cfg
        self.logger = logger
        vrm = cfg.get("vrm", {})
        self.enabled      = vrm.get("enabled", False)
        self.token        = vrm.get("access_token", "")
        self.install_id   = vrm.get("installation_id", "")
        self.timeout      = vrm.get("timeout_seconds", 10)
        self._cache: Optional[list] = None
        self._cache_ts: float = 0.0

    def _headers(self) -> dict:
        return {"x-authorization": f"Token {self.token}",
                "Content-Type": "application/json"}

    def fetch(self, force: bool = False) -> Optional[list]:
        """
        Gibt stundliche HourlyForecast-Liste zurueck oder None bei Fehler.
        Strategie: Ganzen heutigen Tag abfragen (00:00 bis 23:59),
        damit alle Stunden verfuegbar sind und Gesamttag korrekt summiert wird.
        """
        if not self.enabled or not self.token or not self.install_id:
            return None

        # v3.0.9.4: VRM-Prognosen ändern sich bei Wetterwechseln stündlich.
        # force=True überspringt den lokalen Cache und fragt VRM-Server neu.
        interval_s = self.cfg.get("forecast", {}).get(
            "update_interval_minutes", 60) * 60
        # Cache bei Tageswechsel immer verwerfen (neue VRM-Prognose verfügbar)
        cache_day = getattr(self, "_cache_day", None)
        today = datetime.now().date()
        if cache_day != today:
            force = True
            self._cache_day = today
        if not force and self._cache and (
                time.monotonic() - self._cache_ts) < interval_s:
            self.logger.debug("VRM: liefere gecachte Prognose")
            return self._cache

        try:
            now   = datetime.now()
            # Start: Mitternacht heute Lokalzeit (timestamp() berücksichtigt Zeitzone)
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            # End: 23:59 heute Lokalzeit
            end   = now.replace(hour=23, minute=59, second=59, microsecond=0)
            start_unix = int(start.timestamp())
            end_unix   = int(end.timestamp())
            avg_h = self.cfg["charging"].get(
                "avg_daily_consumption_kwh", 8.0) / 24

            url = (f"{self.BASE_URL}/installations/{self.install_id}/stats"
                   f"?type=forecast"
                   f"&start={start_unix}"
                   f"&end={end_unix}"
                   f"&interval=hours")

            self.logger.info(f"VRM API Request: {url}")
            self.logger.info(
                f"VRM Zeitbereich lokal: {start} bis {end}  |  "
                f"UTC: {datetime.fromtimestamp(start_unix, tz=timezone.utc)} bis "
                f"{datetime.fromtimestamp(end_unix, tz=timezone.utc)}"
            )

            resp = requests.get(url, headers=self._headers(),
                                timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()

            records = data.get("records", {})
            pv_raw   = records.get("solar_yield_forecast", [])
            cons_raw = records.get("vrm_consumption_fc",   [])

            self.logger.info(
                f"VRM API Eintraege: PV={len(pv_raw)}, Cons={len(cons_raw)}"
            )
            self.logger.debug(f"VRM API raw (trunc): {str(data)[:500]}")

            if not pv_raw:
                self.logger.warning("VRM API: keine PV-Prognosedaten")
                return None

            # KORREKTUR: Aggregation pro Stunde (mehrere Eintraege werden summiert)
            # Nur Eintraege des heutigen Tages verwenden  -  VRM liefert auch Eintraege
            # vom Vortag (letzte Stunden des UTC-Tages), die auf lokale Stunden 22/23
            # gemappt werden und die echten Tagesstunden ueberschreiben wuerden.
            pv_by_hour: dict = {}
            cons_by_hour: dict = {}
            today = datetime.now().date()

            for entry in pv_raw:
                if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                    continue
                ts_raw, wh = entry[0], entry[1]
                if wh is None:
                    continue
                ts_sec = ts_raw / 1000 if ts_raw > 1e10 else ts_raw
                dt = datetime.fromtimestamp(ts_sec)
                if dt.date() != today:
                    continue
                pv_by_hour[dt.hour] = pv_by_hour.get(dt.hour, 0.0) + float(wh)

            for entry in cons_raw:
                if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                    continue
                ts_raw, wh = entry[0], entry[1]
                if wh is None:
                    continue
                ts_sec = ts_raw / 1000 if ts_raw > 1e10 else ts_raw
                dt = datetime.fromtimestamp(ts_sec)
                if dt.date() != today:
                    continue
                cons_by_hour[dt.hour] = cons_by_hour.get(dt.hour, 0.0) + float(wh)

            # PV-Prognose in HourlyForecast umwandeln  -  alle 24h, nicht nur PV-Stunden.
            # VRM liefert PV-Daten nur fuer Tagesstunden; Nachtstunden fehlen in pv_by_hour.
            # Ohne range(24) wuerden Nachtstunden in night_consumption_kwh() fehlen -> zu niedriger Wert.
            out = []
            total_pv_kwh = 0.0
            for h in range(24):
                pv_kwh = pv_by_hour.get(h, 0.0) / 1000.0
                c_kwh  = cons_by_hour.get(h, avg_h * 1000) / 1000.0 if h in cons_by_hour else avg_h
                out.append(HourlyForecast(
                    hour=h,
                    pv_kwh=round(pv_kwh, 3),
                    consumption_kwh=round(c_kwh, 3),
                    net_kwh=round(pv_kwh - c_kwh, 3)
                ))
                total_pv_kwh += pv_kwh

            if not out:
                return None

            # DEBUG: Stunden 10-12 fuer Vergleich mit Portal loggen
            for hf in out:
                if 10 <= hf.hour <= 12:
                    self.logger.info(
                        f"VRM Stunde {hf.hour:02d}:00 -> PV={hf.pv_kwh:.3f} kWh, "
                        f"Cons={hf.consumption_kwh:.3f} kWh"
                    )

            self._cache    = out
            self._cache_ts = time.monotonic()
            self.logger.info(f"VRM-Prognose: {total_pv_kwh:.2f} kWh PV heute")
            return out

        except requests.exceptions.HTTPError as e:
            self.logger.warning(f"VRM API HTTP-Fehler: {e} – Fallback Open-Meteo")
        except requests.exceptions.ConnectionError:
            self.logger.warning("VRM API nicht erreichbar – Fallback Open-Meteo")
        except Exception as e:
            self.logger.warning(f"VRM API Fehler: {e} – Fallback Open-Meteo")
        return None


# ─────────────────────────────────────────────
# PV-Prognose Haupt-Manager
# ─────────────────────────────────────────────

class ForecastManager:
    """
    Holt stundliche PV-Prognose.
    Prioritaet: VRM API (anlagenspezifisch) -> Solcast -> Open-Meteo -> Dummy
    """

    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg = cfg
        self.logger = logger
        self.loc = cfg["location"]
        self.pv_cfg = cfg["pv"]
        self.fc_cfg = cfg["forecast"]
        self._cache: Optional[list] = None
        self._cache_ts: float = 0.0
        # VRM als primäre Prognose-Quelle
        self.vrm = VrmForecastManager(cfg, logger)
        # (-1, -1) erzwingt Log beim ersten Aufruf nach Programmstart
        self._last_night_window: tuple = (-1, -1)

    def get_forecast(self, force: bool = False) -> list:
        interval_s = self.fc_cfg.get("update_interval_minutes", 60) * 60
        # Cache bei Tageswechsel immer verwerfen
        cache_day = getattr(self, "_cache_day", None)
        today = datetime.now().date()
        if cache_day != today:
            force = True
            self._cache_day = today
        if not force and self._cache and (time.monotonic() - self._cache_ts) < interval_s:
            return self._cache

        # 1. VRM API (beste Qualitaet: Solcast + Anlagenhistorie)
        vrm_fc = self.vrm.fetch(force=force)
        if vrm_fc:
            # v3.0.9.4: Wenn VRM neu abgerufen wurde (force=True), auch den
            # eigenen ForecastManager-Cache aktualisieren. Sonst bleibt der
            # alte Wert im Dashboard stehen, obwohl VRM neue Daten liefert.
            self._cache    = vrm_fc
            self._cache_ts = time.monotonic()
            self.logger.info(
                f"VRM-Prognose aktualisiert: {sum(f.pv_kwh for f in vrm_fc):.1f} kWh heute")
            return vrm_fc  # forecast_source wird in main() gesetzt

        # 2. Fallback: Solcast oder Open-Meteo
        try:
            provider = self.fc_cfg.get("provider", "open_meteo")
            if provider == "solcast":
                fc = self._fetch_solcast()
            else:
                fc = self._fetch_open_meteo()
            self._cache    = fc
            self._cache_ts = time.monotonic()
            self.logger.info(
                f"Prognose ({provider}): {sum(f.pv_kwh for f in fc):.2f} kWh heute")
            return fc
        except Exception as e:
            self.logger.error(f"Prognose-Fehler: {e}")
            return self._cache or self._dummy()

    def _fetch_open_meteo(self) -> list:
        lat = self.loc["latitude"]
        lon = self.loc["longitude"]
        tz  = self.loc.get("timezone", "Europe/Berlin")
        url = (f"https://api.open-meteo.com/v1/forecast"
               f"?latitude={lat}&longitude={lon}"
               f"&hourly=shortwave_radiation,cloud_cover"
               f"&timezone={tz}&forecast_days=2")
        data = requests.get(url, timeout=15).json()
        times      = data["hourly"]["time"]
        radiation  = data["hourly"].get("shortwave_radiation", [0]*len(times))
        cloud      = data["hourly"].get("cloud_cover",         [0]*len(times))
        peak_kw    = self.pv_cfg["peak_power_kwp"]
        eff        = self.pv_cfg.get("efficiency_factor", 0.82)
        avg_h      = self.cfg["charging"].get("avg_daily_consumption_kwh", 8.0) / 24
        today      = datetime.now().date()
        out = []
        for i, t_str in enumerate(times):
            t = datetime.fromisoformat(t_str)
            if t.date() != today:
                continue
            rad  = radiation[i] or 0
            cl   = (cloud[i] or 0) / 100.0
            pv   = max(0.0, (rad / 1000.0) * peak_kw * eff * (1 - cl * 0.3))
            out.append(HourlyForecast(hour=t.hour, pv_kwh=round(pv,3),
                                      consumption_kwh=round(avg_h,3),
                                      net_kwh=round(pv - avg_h,3)))
        return out or self._dummy()

    def _fetch_solcast(self) -> list:
        key = self.fc_cfg.get("solcast_api_key", "")
        rid = self.fc_cfg.get("solcast_resource_id", "")
        if not key or not rid:
            self.logger.warning("Solcast: API-Key fehlt -> Open-Meteo als Fallback")
            return self._fetch_open_meteo()
        url = (f"https://api.solcast.com.au/rooftop_sites/{rid}"
               f"/forecasts?format=json&hours=24")
        data = requests.get(url, headers={"Authorization": f"Bearer {key}"},
                            timeout=15).json()
        avg_h = self.cfg["charging"].get("avg_daily_consumption_kwh", 8.0) / 24
        out = []
        for p in data.get("forecasts", []):
            t  = datetime.fromisoformat(p["period_end"].replace("Z", "+00:00"))
            pv = float(p.get("pv_estimate", 0))
            out.append(HourlyForecast(hour=t.hour, pv_kwh=round(pv,3),
                                      consumption_kwh=round(avg_h,3),
                                      net_kwh=round(pv - avg_h,3)))
        return out or self._dummy()

    def _dummy(self) -> list:
        self.logger.warning("Verwende Dummy-PV-Prognose")
        peak_kw = self.pv_cfg["peak_power_kwp"]
        eff     = self.pv_cfg.get("efficiency_factor", 0.82)
        avg_h   = self.cfg["charging"].get("avg_daily_consumption_kwh", 8.0) / 24
        out = []
        for h in range(24):
            pv = 0.0
            if 6 <= h <= 20:
                x  = (h - 13) / 4.0
                pv = peak_kw * eff * math.exp(-x * x) * 0.7
            out.append(HourlyForecast(hour=h, pv_kwh=round(pv,3),
                                      consumption_kwh=round(avg_h,3),
                                      net_kwh=round(pv - avg_h,3)))
        return out

    def pv_remaining_kwh(self) -> float:
        now_h = datetime.now().hour
        return sum(f.pv_kwh for f in self.get_forecast() if f.hour >= now_h)

    def pv_total_kwh(self) -> float:
        return sum(f.pv_kwh for f in self.get_forecast())

    def night_consumption_kwh(self) -> float:
        """Berechnet Nachtverbrauch aus stündlichen Forecast-Daten mit dynamischem Fenster."""
        fc_list = self.get_forecast()
        ns, ne = self._get_dynamic_night_window(fc_list)

        night_cons = 0.0
        for fc in fc_list:
            h = fc.hour
            if h >= ns or h < ne:
                night_cons += fc.consumption_kwh

        if night_cons > 0.0:
            return round(night_cons, 2)

        # Fallback: Durchschnitt
        avg = self.cfg["charging"].get("avg_daily_consumption_kwh", 8.0) / 24
        return round(avg * ((24 - ns) + ne), 2)

    def _calculate_sun_times(self, dt: date) -> tuple[float, float, float]:
        """
        Berechnet Sonnenaufgang und Sonnenuntergang in lokaler Dezimalzeit
        (z.B. 8.23 = 08:14). Vereinfachte NOAA-Formel, Fehler < 2 Minuten.
        Sommer-/Winterzeit wird über zoneinfo (Python 3.9+) korrekt berücksichtigt.
        """
        lat = self.loc["latitude"]
        lon = self.loc["longitude"]
        tz_name = self.loc.get("timezone", "UTC")

        # UTC-Offset für das konkrete Datum (Sommerzeit!)
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
            noon_local = datetime(dt.year, dt.month, dt.day, 12, 0, tzinfo=tz)
            utc_offset_h = noon_local.utcoffset().total_seconds() / 3600.0
        except Exception:
            utc_offset_h = 1.0  # Fallback CET

        n = dt.timetuple().tm_yday

        # Sonnendeklination (Approximation)
        decl = math.radians(-23.44 * math.cos(math.radians((360.0 / 365.0) * (n + 10))))

        # Zeitgleichung (Equation of Time) in Stunden
        B = math.radians((360.0 / 365.0) * (n - 81))
        eot = (9.87 * math.sin(2 * B) - 7.53 * math.cos(B) - 1.5 * math.sin(B)) / 60.0

        # Stundenwinkel: 90.833° = 90° + 50' (Sonnenradius 16' + Refraktion 34')
        lat_rad = math.radians(lat)
        cos_omega = (
            math.cos(math.radians(90.833))
            - math.sin(lat_rad) * math.sin(decl)
        ) / (math.cos(lat_rad) * math.cos(decl))
        cos_omega = max(-1.0, min(1.0, cos_omega))
        omega = math.degrees(math.acos(cos_omega)) / 15.0  # in Stunden

        solar_noon_utc = 12.0 - (lon / 15.0) + eot
        sunrise_utc    = solar_noon_utc - omega
        sunset_utc     = solar_noon_utc + omega

        sunrise_local    = (sunrise_utc    + utc_offset_h) % 24
        sunset_local     = (sunset_utc     + utc_offset_h) % 24
        solar_noon_local = (solar_noon_utc + utc_offset_h) % 24

        return sunrise_local, sunset_local, solar_noon_local

    def _get_dynamic_night_window(self, fc_list: list) -> tuple:
        """
        Bestimmt Nachtstart und -ende dynamisch aus dem PV/Verbrauchs-Forecast.
        Fallback (kein Forecast oder unplausible Werte): astronomische
        Dämmerungszeiten aus GPS + Datum. Keine statischen Config-Werte nötig.
        """
        sunrise, sunset, solar_noon = self._calculate_sun_times(date.today())
        ns_fallback = max(12, min(23, math.ceil(sunset)))
        ne_fallback = max(0,  min(11, math.floor(sunrise)))

        has_consumption = fc_list and any(fc.consumption_kwh > 0 for fc in fc_list)

        if not has_consumption:
            night_start = ns_fallback
            night_end   = ne_fallback
            dynamic     = False
        else:
            # Dynamischer Start (Abend): erste Stunde ab 12 Uhr, wo PV < Verbrauch
            night_start = ns_fallback
            for fc in fc_list:
                if fc.hour >= 12 and fc.pv_kwh < fc.consumption_kwh:
                    night_start = fc.hour
                    break

            # Dynamisches Ende (Morgen): erste Stunde ab 0 Uhr, wo PV > Verbrauch
            night_end = ne_fallback
            for fc in fc_list:
                if fc.hour < 12 and fc.pv_kwh > fc.consumption_kwh:
                    night_end = fc.hour
                    break

            # Weiche Clamps: dynamische Werte nicht sinnlos weit
            # vom astronomischen Ereignis abweichen lassen (z.B. Datenfehler)
            night_start = max(math.floor(sunset) - 1,
                              min(math.ceil(sunset) + 3, night_start))
            night_end   = max(math.floor(sunrise) - 3,
                              min(math.ceil(sunrise) + 1, night_end))
            dynamic     = True

        # Nur loggen bei Änderung oder erstem Aufruf
        window = (night_start, night_end)
        if window != self._last_night_window:
            self._last_night_window = window
            hours  = (24 - night_start) + night_end
            source = "dynamisch" if dynamic else "astronomisch"
            self.logger.info(
                f"[NIGHT_WINDOW] {night_start}:00–{night_end}:00 "
                f"({hours}h, {source} | "
                f"Sonnenaufgang {sunrise:.1f}h, Sonnenuntergang {sunset:.1f}h)"
            )

        return night_start, night_end
