#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dashboard.py — Web-Dashboard für Solar Batterie Manager
========================================================
Ausgelagert aus battery_manager.py ab v3.0.10.0.

Enthält:
  - DASHBOARD_HTML   : eingebettetes HTML/CSS/JS (Jinja-frei, plain str.replace)
  - _HeartbeatThread : Hintergrund-Thread für Dedup-Heartbeats
  - start_dashboard  : Flask-Server starten (Daemon-Thread)

Abhängigkeiten (aus battery_manager.py):
  - SystemState        (dataclass, wird per asdict() serialisiert)
  - DeduplicatingFilter (logging.Filter-Subklasse)

Import in battery_manager.py:
  from dashboard import start_dashboard
"""

import logging
import threading
from dataclasses import asdict

# TYPE_CHECKING-Guard verhindert zirkulären Import zur Laufzeit.
# Zur Laufzeit werden SystemState und DeduplicatingFilter als Parameter
# übergeben – kein echter Import nötig.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from models import SystemState
    from logging_setup import DeduplicatingFilter


# ─────────────────────────────────────────────
# Interner Heartbeat-Thread
# ─────────────────────────────────────────────

class _HeartbeatThread(threading.Thread):
    """
    Prüft alle check_interval_s Sekunden ob Heartbeats fällig sind.
    Überwacht alle übergebenen DeduplicatingFilter-Instanzen:
    sowohl Werkzeug-HTTP-Access als auch BatteryManager-StreamHandler.
    """
    def __init__(self, filters: list, check_interval_s: float = 60.0):
        super().__init__(daemon=True, name="DeduplicatingHeartbeat")
        self._filters = filters
        self._interval = check_interval_s
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            self._stop.wait(self._interval)
            if self._stop.is_set():
                break
            for f in self._filters:
                f.emit_heartbeat_if_due()

    def stop(self):
        self._stop.set()


# ─────────────────────────────────────────────
# HTML-Template
# ─────────────────────────────────────────────
# Platzhalter die zur Laufzeit ersetzt werden:
#   __VERSION__  -> VERSION aus battery_manager.py (z.B. "3.0.10.5")
#   __REFRESH__  -> refresh_interval_seconds aus config.yaml
#   __CAP__      -> battery.capacity_kwh aus config.yaml

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Solar Batterie Manager v__VERSION__</title>
<style>
:root{--bg:#0f1117;--bg2:#1a1d27;--bg3:#252836;
  --acc:#f59e0b;--grn:#22c55e;--red:#ef4444;--blu:#3b82f6;--vio:#a78bfa;
  --txt:#e2e8f0;--mut:#64748b;--brd:#2d3148}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:system-ui,sans-serif;min-height:100vh}
.hd{background:var(--bg2);border-bottom:1px solid var(--brd);
    padding:12px 18px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.hd h1{font-size:1.05rem;font-weight:700;color:var(--acc)}
.hdr{display:flex;gap:14px;flex-wrap:wrap;align-items:center}
.sp{display:flex;align-items:center;gap:5px;font-size:.78rem;color:var(--mut)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--red)}
.dot.ok{background:var(--grn)}.dot.warn{background:var(--acc)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:10px;padding:14px}
.card{background:var(--bg2);border:1px solid var(--brd);border-radius:10px;padding:14px}
.lbl{font-size:.68rem;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin-bottom:5px}
.val{font-size:1.65rem;font-weight:700}
.unt{font-size:.82rem;color:var(--mut);margin-left:3px}
.sub{font-size:.73rem;color:var(--mut);margin-top:3px}
.sb{background:var(--bg3);border-radius:5px;height:7px;margin-top:7px;overflow:hidden}
.sf{height:100%;border-radius:5px;transition:width .5s}
.bdg{display:inline-block;padding:3px 9px;border-radius:20px;font-size:.7rem;font-weight:600;text-transform:uppercase}
.bc{background:rgba(34,197,94,.15);color:var(--grn);border:1px solid var(--grn)}
.bi{background:rgba(100,116,139,.15);color:var(--mut);border:1px solid var(--brd)}
.bt{background:rgba(59,130,246,.15);color:var(--blu);border:1px solid var(--blu)}
.bf{background:rgba(245,158,11,.15);color:var(--acc);border:1px solid var(--acc)}
.be{background:rgba(167,139,250,.15);color:var(--vio);border:1px solid var(--vio)}
.bd{background:rgba(239,68,68,.1);color:var(--red);border:1px solid rgba(239,68,68,.4)}
.sec{padding:0 14px 14px}
.sec h2{font-size:.88rem;font-weight:600;margin-bottom:8px}
.pan{background:var(--bg2);border:1px solid var(--brd);border-radius:10px;padding:14px;overflow-x:auto}
.chart-svg{width:100%;overflow-x:auto}
.chart-svg svg{display:block;min-width:560px}
table{width:100%;border-collapse:collapse;font-size:.78rem;min-width:540px}
th{text-align:left;padding:6px 9px;color:var(--mut);font-weight:500;border-bottom:1px solid var(--brd)}
td{padding:6px 9px;border-bottom:1px solid var(--brd)}
tr.past td{opacity:.38}tr.now td{background:rgba(245,158,11,.05)}
.rea{background:var(--bg2);border:1px solid var(--brd);border-left:3px solid var(--acc);
     border-radius:8px;padding:11px 13px;margin:0 14px 14px;font-size:.8rem;line-height:1.6}
.rl{font-size:.66rem;text-transform:uppercase;color:var(--mut);margin-bottom:3px}
.evb{background:rgba(167,139,250,.08);border:1px solid var(--vio);border-radius:8px;
     padding:9px 13px;margin:0 14px 12px;font-size:.8rem;color:var(--vio)}

</style>
</head>
<body>
<div class="hd">
  <h1>&#9889; Solar Batterie Manager <span style="font-size:0.7rem;color:var(--mut);font-weight:400">v__VERSION__</span></h1>
  <div class="hdr">
    <div class="sp"><div class="dot" id="mdot"></div><span id="mst">Verbinde...</span></div>
    <div class="sp"><div class="dot" id="edot"></div><span id="est">evcc -</span></div>
    <div class="sp" id="ts"></div>
  </div>
</div>
<div id="evb" style="display:none"></div>
<div class="grid">
  <div class="card">
    <div class="lbl">Batterie SOC</div>
    <div class="val" id="soc">-<span class="unt">%</span></div>
    <div class="sb"><div class="sf" id="sbar" style="width:0"></div></div>
    <div class="sub" id="skwh">-</div>
  </div>
  <div class="card">
    <div class="lbl">Batterie</div>
    <div class="val" id="bv">-<span class="unt">V</span></div>
    <div class="sub" id="bi">Strom: - A</div>
    <div class="sub" id="bp">Leistung: - W</div>
  </div>
  <div class="card">
    <div class="lbl">Lademodus</div>
    <div style="margin:5px 0" id="mbdg">-</div>
    <div class="sub" id="ca">-</div>
    <div class="sub" id="dfc">-</div>
  </div>
  <div class="card">
    <div class="lbl">PV Leistung</div>
    <div class="val" id="pvw">-<span class="unt">W</span></div>
    <div class="sub" id="pvk">Heute: -</div>
  </div>
  <div class="card">
    <div class="lbl">Verbrauch</div>
    <div class="val" id="ldw">-<span class="unt">W</span></div>
    <div class="sub" id="ldk">Heute: -</div>
  </div>
  <div class="card">
    <div class="lbl">Netz</div>
    <div class="val" id="grw">-<span class="unt">W</span></div>
    <div class="sub" style="color:var(--mut)">+ Bezug / - Einspeisung</div>
  </div>
  <div class="card">
    <div class="lbl">PV Prognose heute</div>
    <div class="val" id="fcp">-<span class="unt">kWh</span></div>
    <div class="sub" id="fcr">Verbleibend: -</div>
    <div class="sub" id="fcs" style="color:var(--acc)"></div>
  </div>
  <div class="card">
    <div class="lbl">Nachtverbrauch</div>
    <div class="val" id="fcn">-<span class="unt">kWh</span></div>
    <div class="sub" id="fct">Ziel-SOC: -</div>
  </div>
</div>
<div class="rea"><div class="rl">Entscheidung</div><div id="rea">Lade...</div></div>
<div class="sec">
  <h2>PV &amp; Verbrauch heute (kWh/h)</h2>
  <div class="pan chart-svg"><svg id="chart" height="220"></svg></div>
</div>
<div class="sec">
  <h2>Stundlicher Ladeplan + projizierter SOC</h2>
  <div class="pan">
    <table>
      <thead><tr><th>Uhr</th><th>PV kWh</th><th>Last kWh</th><th>Ueberschuss</th>
        <th>Aktion</th><th>Strom</th><th>SOC %</th></tr></thead>
      <tbody id="tb"></tbody>
    </table>
  </div>
</div>
<script>
const REFRESH=__REFRESH__*1000, CAP=__CAP__;
function sc(s){return s>=80?'var(--grn)':s>=40?'var(--acc)':'var(--red)'}
function bdg(a){
  const m={charging:['Laden','bc'],trickle:['Sanft','bt'],full_charge:['Vollladung','bf'],
    idle:['Pause','bi'],discharging:['Entladen','bd']};
  const[l,c]=m[a]||['-','bi'];
  return`<span class="bdg ${c}">${l}</span>`;
}
async function refresh(){
  try{
    const d=await(await fetch('/api/state')).json();
    document.getElementById('ts').textContent=
      d.timestamp?new Date(d.timestamp).toLocaleTimeString('de'):'';
    const md=document.getElementById('mdot'),ms=document.getElementById('mst');
    if(d.modbus_connected){md.className='dot ok';ms.textContent='Modbus OK';}
    else{md.className='dot';ms.textContent='Modbus offline';}
    const ed=document.getElementById('edot'),es=document.getElementById('est');
    if(d.evcc_active){ed.className='dot warn';es.textContent='evcc '+d.evcc_mode;}
    else if(d.evcc_charge_power_w>0){ed.className='dot ok';es.textContent='evcc PV';}
    else{ed.className='dot';es.textContent='evcc -';}
    const soc=d.soc??0;
    document.getElementById('soc').innerHTML=`${soc.toFixed(1)}<span class="unt">%</span>`;
    const sb=document.getElementById('sbar');sb.style.width=soc+'%';sb.style.background=sc(soc);
    document.getElementById('skwh').textContent=`ca. ${(soc/100*CAP).toFixed(1)} kWh`;
    document.getElementById('mbdg').innerHTML=bdg(d.charge_mode);
    document.getElementById('ca').textContent=`Ladestrom: ${(d.charge_current_setpoint||0).toFixed(0)} A`;
    document.getElementById('dfc').textContent=`Vollladung vor ${d.days_since_full_charge||0} Tagen`;
    const bv=d.battery_voltage||0;
    const bi=d.battery_current||0;
    const bp=d.battery_power_w||0;
    document.getElementById('bv').innerHTML=`${bv.toFixed(2)}<span class="unt">V</span>`;
    document.getElementById('bi').textContent=`Strom: ${bi.toFixed(1)} A ${bi>0?'↑ laden':'↓ entladen'}`;
    document.getElementById('bp').textContent=`Leistung: ${bp.toFixed(0)} W`;
    document.getElementById('pvw').innerHTML=`${(d.pv_power_w||0).toFixed(0)}<span class="unt">W</span>`;
    document.getElementById('pvk').textContent=`Heute: ${(d.pv_energy_today_kwh||0).toFixed(2)} kWh`;
    document.getElementById('ldw').innerHTML=`${(d.load_power_w||0).toFixed(0)}<span class="unt">W</span>`;
    document.getElementById('ldk').textContent=`Heute: ${(d.load_energy_today_kwh||0).toFixed(2)} kWh`;
    const gw=d.grid_power_w||0;
    document.getElementById('grw').innerHTML=`${gw.toFixed(0)}<span class="unt">W</span>`;
    document.getElementById('grw').style.color=gw>50?'var(--red)':gw<-50?'var(--grn)':'var(--txt)';
    document.getElementById('fcp').innerHTML=`${(d.forecast_pv_today_kwh||0).toFixed(1)}<span class="unt">kWh</span>`;
    document.getElementById('fcr').textContent=`Verbleibend: ${(d.forecast_pv_remaining_kwh||0).toFixed(1)} kWh`;
    document.getElementById('fcn').innerHTML=`${(d.forecast_consumption_night_kwh||0).toFixed(1)}<span class="unt">kWh</span>`;
    document.getElementById('fct').textContent=`Ziel-SOC: ${(d.target_soc||80).toFixed(0)}%`;
    const srcMap={'vrm':'VRM ★','solcast':'Solcast','open_meteo':'Open-Meteo','dummy':'Dummy ⚠️','':`Open-Meteo`};
    document.getElementById('fcs').textContent=srcMap[d.forecast_source||'']||d.forecast_source||'';

    document.getElementById('rea').textContent=d.charge_reason||'-';
    const sc2=d.planned_charge_schedule||[],nH=new Date().getHours();
    (function buildChart(data,nowH){
      const svg=document.getElementById('chart');
      const W=Math.max(svg.parentElement.clientWidth-28,560);
      svg.setAttribute('width',W);
      const H=220,PAD={t:16,r:16,b:32,l:44};
      const iW=W-PAD.l-PAD.r, iH=H-PAD.t-PAD.b;
      const n=data.length||1;
      const grpW=iW/n;
      const barW=Math.max(2,grpW*0.38);
      const gap=Math.max(1,grpW*0.04);
      const maxY=Math.max(...data.map(s=>Math.max(s.pv_kwh||0,s.consumption_kwh||0)),0.5);
      // round up to nice number
      const niceMax=maxY<=1?1:maxY<=2?2:maxY<=3?3:maxY<=5?5:maxY<=6?6:Math.ceil(maxY);
      const sc=v=>iH*(1-v/niceMax);
      // Y gridlines & labels
      const ticks=[0,niceMax*0.25,niceMax*0.5,niceMax*0.75,niceMax];
      let out=`<g transform="translate(${PAD.l},${PAD.t})">`;
      // grid
      ticks.forEach(t=>{
        const y=sc(t);
        out+=`<line x1="0" y1="${y.toFixed(1)}" x2="${iW}" y2="${y.toFixed(1)}"
          stroke="rgba(255,255,255,0.07)" stroke-width="1"/>`;
        out+=`<text x="-6" y="${(y+4).toFixed(1)}" text-anchor="end"
          font-size="10" fill="#64748b">${t%1===0?t:t.toFixed(1)}</text>`;
      });
      // bars
      data.forEach((s,i)=>{
        const x=i*grpW;
        const cx=x+grpW/2;
        const isNow=(s.hour===nowH);
        const pvH=Math.max(1,iH-sc(s.pv_kwh||0));
        const ldH=Math.max(1,iH-sc(s.consumption_kwh||0));
        const pvY=sc(s.pv_kwh);
        const ldY=sc(s.consumption_kwh);
        // highlight current hour
        if(isNow) out+=`<rect x="${(x+1).toFixed(1)}" y="0" width="${(grpW-2).toFixed(1)}" height="${iH}"
          fill="rgba(245,158,11,0.07)" rx="2"/>`;
        // PV bar (left of pair)
        const pvOp=s.is_past?'0.35':'1';
        out+=`<rect x="${(cx-barW-gap/2).toFixed(1)}" y="${pvY.toFixed(1)}"
          width="${barW.toFixed(1)}" height="${pvH.toFixed(1)}"
          fill="#f59e0b" opacity="${pvOp}" rx="2">
          <title>${s.hour}:00 PV ${(s.pv_kwh||0).toFixed(2)} kWh</title></rect>`;
        // Consumption bar (right of pair)
        out+=`<rect x="${(cx+gap/2).toFixed(1)}" y="${ldY.toFixed(1)}"
          width="${barW.toFixed(1)}" height="${ldH.toFixed(1)}"
          fill="#ef4444" opacity="${s.is_past?'0.25':'0.65'}" rx="2">
          <title>${s.hour}:00 Verbrauch ${(s.consumption_kwh||0).toFixed(2)} kWh</title></rect>`;
        // hour label
        if(i%2===0||n<=14)
          out+=`<text x="${cx.toFixed(1)}" y="${(iH+18).toFixed(1)}"
            text-anchor="middle" font-size="10" fill="${isNow?'#f59e0b':'#64748b'}"
            font-weight="${isNow?'700':'400'}">${s.hour}h</text>`;
      });
      // SOC line (secondary axis, right side)
      const socData=data.filter(s=>s.projected_soc!==undefined);
      if(socData.length>1){
        const socY=v=>iH*(1-v/100);
        const pts=socData.map((s,i)=>{
          const x=i*grpW+grpW/2;
          return`${x.toFixed(1)},${socY(s.projected_soc||0).toFixed(1)}`;
        }).join(' ');
        out+=`<polyline points="${pts}" fill="none" stroke="#22c55e"
          stroke-width="1.5" stroke-dasharray="4,3" opacity="0.7"/>`;
        // SOC axis label on right
        out+=`<text x="${iW+4}" y="12" font-size="9" fill="#22c55e" opacity="0.7">SOC%</text>`;
        // SOC right axis ticks 0/50/100
        [0,50,100].forEach(t=>{
          const y=socY(t);
          out+=`<text x="${iW+4}" y="${(y+3).toFixed(1)}" font-size="9" fill="#22c55e" opacity="0.5">${t}</text>`;
        });
      }
      // Legend top-right
      out+=`<g transform="translate(${iW-160},0)">
        <rect width="10" height="10" fill="#f59e0b" rx="2" y="1"/>
        <text x="14" y="10" font-size="10" fill="#94a3b8">PV-Erzeugung</text>
        <rect x="90" width="10" height="10" fill="#ef4444" opacity="0.65" rx="2" y="1"/>
        <text x="104" y="10" font-size="10" fill="#94a3b8">Verbrauch</text>
      </g>`;
      out+=`</g>`;
      svg.innerHTML=out;
    })(sc2,nH);
    document.getElementById('tb').innerHTML=sc2.map(s=>{
      const cl=s.is_past?'past':s.hour===nH?'now':'';
      const sur=(s.surplus_kwh||0)>=0
        ?`<span style="color:var(--grn)">+${(s.surplus_kwh||0).toFixed(2)}</span>`
        :`<span style="color:var(--red)">${(s.surplus_kwh||0).toFixed(2)}</span>`;
      const ca=s.charge_current_a||0;
      const caColor=ca>0.5?'var(--grn)':ca<-0.5?'var(--red)':'var(--txt)';
      const caOpacity=s.is_past?'1':'0.6';
      const caFmt=s.is_past
        ?(ca>=0?`+${ca.toFixed(1)}`:`${ca.toFixed(1)}`)
        :`${Math.abs(ca).toFixed(1)}`;
      const caStr=ca!==0
        ?`<span style="color:${caColor};opacity:${caOpacity}">${caFmt} A</span>`
        :`<span style="opacity:0.35">0 A</span>`;
      return`<tr class="${cl}">
        <td>${String(s.hour).padStart(2,'0')}:00</td>
        <td>${(s.pv_kwh||0).toFixed(3)}</td><td>${(s.consumption_kwh||0).toFixed(3)}</td>
        <td>${sur}</td><td>${bdg(s.action)}</td>
        <td>${caStr}</td>
        <td><span style="color:${sc(s.projected_soc||0)}">${(s.projected_soc||0).toFixed(1)}%</span></td>
      </tr>`;
    }).join('');
  }catch(e){document.getElementById('rea').textContent='Fehler: '+e.message;}
}
refresh();setInterval(refresh,REFRESH);
</script>
</body></html>
"""


# ─────────────────────────────────────────────
# Dashboard starten
# ─────────────────────────────────────────────

def start_dashboard(cfg: dict, state, logger: logging.Logger,
                    dedup_stream, version: str = ""):
    """
    Flask-Dashboard als Daemon-Thread starten.

    Parameter:
        cfg          : gesamtes config-dict
        state        : SystemState-Instanz (wird per asdict() an /api/state geliefert)
        logger       : battery_manager logger
        dedup_stream : DeduplicatingFilter-Instanz des StreamHandlers
        version      : VERSION-String aus battery_manager.py (z.B. "3.0.10.0")
    """
    try:
        from flask import Flask, jsonify, Response
    except ImportError:
        logger.warning("Flask fehlt - Dashboard deaktiviert  (pip install flask)")
        return

    dash    = cfg["dashboard"]
    refresh = dash.get("refresh_interval_seconds", 30)
    cap     = cfg["battery"]["capacity_kwh"]
    html    = (DASHBOARD_HTML
               .replace("__VERSION__", version)
               .replace("__REFRESH__", str(refresh))
               .replace("__CAP__",     str(cap)))

    # Werkzeug Access-Logs deduplizieren (sonst alle 30s im journal)
    log_cfg = cfg.get("logging", {})
    dedup_enabled    = log_cfg.get("dedup_enabled", True)
    dedup_heartbeat  = log_cfg.get("dedup_heartbeat_minutes", 20.0)

    # Import hier um zirkulären Top-Level-Import zu vermeiden
    # DeduplicatingFilter-Klasse aus dem aufrufenden Modul holen
    # (wird als Instanz übergeben, Klasse für neues Objekt nötig)
    DeduplicatingFilter = type(dedup_stream)

    werkzeug_log = logging.getLogger('werkzeug')
    if hasattr(werkzeug_log, 'handlers') and werkzeug_log.handlers:
        werkzeug_log.handlers.clear()
    werkzeug_log.setLevel(logging.INFO)
    werkzeug_log.propagate = False

    dedup_werkzeug = DeduplicatingFilter(
        heartbeat_minutes=dedup_heartbeat, enabled=dedup_enabled)
    fmt_werkzeug = logging.Formatter("%(message)s")
    ch_werkzeug  = logging.StreamHandler()
    ch_werkzeug.setFormatter(fmt_werkzeug)
    ch_werkzeug.addFilter(dedup_werkzeug)
    dedup_werkzeug._handler = ch_werkzeug
    werkzeug_log.addHandler(ch_werkzeug)

    if dedup_enabled:
        logger.info(
            f"Werkzeug-Access-Log Deduplizierung aktiv: identische Requests "
            f"werden unterdrückt, Heartbeat alle {dedup_heartbeat:.0f} Minuten")

    heartbeat_thread = _HeartbeatThread(
        filters=[dedup_stream, dedup_werkzeug],
        check_interval_s=60.0)
    heartbeat_thread.start()

    app = Flask(__name__)

    @app.route("/")
    def index():
        return Response(html, mimetype="text/html")

    @app.route("/api/state")
    def api_state():
        return jsonify(asdict(state))

    host = dash.get("host", "0.0.0.0")
    port = dash.get("port", 5000)
    threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False),
        daemon=True
    ).start()
    logger.info(f"Dashboard: http://{host}:{port}")
