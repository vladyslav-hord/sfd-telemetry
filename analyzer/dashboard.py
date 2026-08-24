from __future__ import annotations

import html
import json
import re
from pathlib import Path


CSS = """*{box-sizing:border-box}body{margin:0;background:#0b1020;color:#e8edf7;font:15px system-ui,sans-serif}main{max-width:1280px;margin:auto;padding:28px}a{color:#83d4ff}h1,h2{margin:0 0 14px}.muted{color:#9ba8bc}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}.card,section{background:#141c31;border:1px solid #293657;border-radius:10px;padding:16px;margin:14px 0}.metric{font-size:25px;font-weight:700}.good{color:#8be9b0}.warn{color:#ffd48a}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px;border-bottom:1px solid #293657}canvas{width:100%;height:180px;background:#0b1020;border-radius:8px}@media(max-width:600px){main{padding:14px}table{font-size:12px}}"""
JS = """function drawTimeline(id,values){const c=document.getElementById(id);if(!c||!values.length)return;const x=c.getContext('2d'),w=c.width=c.clientWidth*devicePixelRatio,h=c.height=c.clientHeight*devicePixelRatio,m=Math.max(...values,1);x.strokeStyle='#83d4ff';x.lineWidth=2*devicePixelRatio;x.beginPath();values.forEach((v,i)=>{const px=i*w/Math.max(values.length-1,1),py=h-(v/m*h*.9+h*.05);i?x.lineTo(px,py):x.moveTo(px,py)});x.stroke()}"""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _page(title: str, content: str) -> str:
    return f"<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><link rel='stylesheet' href='assets/dashboard.css'><main>{content}</main></html>"


def _number(value: object) -> str:
    return f"{value:,.0f}" if isinstance(value, (int, float)) else "—"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-") or "player"


def build_dashboard(report_directory: str, report_date: str | None = None) -> Path:
    all_reports = sorted(Path(report_directory).glob("*.json"))
    dashboard = Path(report_directory).parent / "dashboard"
    assets, days, players_dir, episodes = dashboard / "assets", dashboard / "days", dashboard / "players", dashboard / "episodes"
    for directory in (assets, days, players_dir, episodes):
        directory.mkdir(parents=True, exist_ok=True)
    (assets / "dashboard.css").write_text(CSS, encoding="utf-8")
    (assets / "dashboard.js").write_text(JS, encoding="utf-8")
    reports = [path for path in all_reports if report_date is None or path.stem == report_date]
    links = []
    for path in reports:
        payload = json.loads(path.read_text(encoding="utf-8"))
        server, quality = payload["server"], payload["data_quality"]
        combat = server.get("combat", {})
        cards = [("Humans", server.get("humans")), ("Rounds", server.get("rounds")), ("Events", quality.get("event_count")), ("Damage", combat.get("damage")), ("Inferred kills", combat.get("inferred_kills")), ("Scene frames", quality.get("scene_frame_batches"))]
        card_html = "".join(f"<div class='card'><div class='muted'>{html.escape(label)}</div><div class='metric'>{_number(value)}</div></div>" for label, value in cards)
        player_rows = "".join(f"<tr><td><a href='../players/{_slug(player['player_identity_id'])}.html'>{html.escape(player.get('display_name', player['player_identity_id']))}</a></td><td>{_number(player.get('visits', player['sessions']))}</td><td>{_number(player['playtime_seconds']/60)} min</td><td>{_number(player['combat'].get('inferred_kill_credit'))}</td><td>{_number(player['combat'].get('inferred_assist_credit'))}</td></tr>" for player in payload["players"])
        maps = "".join(f"<tr><td>{html.escape(str(item.get('map_name') or 'Unknown'))}</td><td>{_number(item.get('rounds'))}</td><td>{_number(item.get('duration_seconds'))} s</td></tr>" for item in payload["maps"])
        scene_note = "Scene telemetry unavailable for this historical day." if not quality.get("scene_available") else "Scene telemetry is available; significant episodes appear below."
        content = f"<nav><a href='../index.html'>All days</a></nav><h1>SFD telemetry — {html.escape(payload['report_date'])}</h1><p class='muted'>{scene_note}</p><div class='grid'>{card_html}</div><section><h2>Data quality</h2><pre>{html.escape(json.dumps(quality, ensure_ascii=False, indent=2))}</pre></section><section><h2>Players</h2><table><tr><th>Player</th><th>Visits</th><th>Playtime</th><th>Kills*</th><th>Assists*</th></tr>{player_rows}</table></section><section><h2>Maps</h2><table><tr><th>Map</th><th>Rounds</th><th>Mean duration</th></tr>{maps}</table></section><section><h2>Combat</h2><p>* Kill and assist data are inferred from available telemetry, not authoritative game scoring.</p><pre>{html.escape(json.dumps(combat, ensure_ascii=False, indent=2))}</pre></section>"
        (days / f"{path.stem}.html").write_text(_page(f"SFD — {path.stem}", content).replace("assets/dashboard.css", "../assets/dashboard.css"), encoding="utf-8")
        links.append(f"<li><a href='days/{path.stem}.html'>{html.escape(path.stem)}</a></li>")
        for player in payload["players"]:
            identity = player["player_identity_id"]
            content = f"<nav><a href='../days/{path.stem}.html'>Day</a></nav><h1>{html.escape(player.get('display_name', identity))}</h1><section><pre>{html.escape(json.dumps(player, ensure_ascii=False, indent=2))}</pre></section>"
            (players_dir / f"{_slug(identity)}.html").write_text(_page("SFD player", content).replace("assets/dashboard.css", "../assets/dashboard.css"), encoding="utf-8")
    all_links = [f"<li><a href='days/{path.stem}.html'>{html.escape(path.stem)}</a></li>" for path in all_reports]
    index = f"<h1>SFD Telemetry Dashboard</h1><p class='muted'>Local static reports. No network requests are made.</p><section><h2>Days</h2><ul>{''.join(all_links) or '<li>No reports yet</li>'}</ul></section>"
    (dashboard / "index.html").write_text(_page("SFD Telemetry Dashboard", index), encoding="utf-8")
    return dashboard / "index.html"
