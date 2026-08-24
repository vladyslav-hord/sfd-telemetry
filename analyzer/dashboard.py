from __future__ import annotations

import html
import json
import re
import sqlite3
from pathlib import Path


CSS = """*{box-sizing:border-box}body{margin:0;background:#0b1020;color:#e8edf7;font:15px system-ui,sans-serif}main{max-width:1280px;margin:auto;padding:28px}a{color:#83d4ff}h1,h2{margin:0 0 14px}.muted{color:#9ba8bc}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}.card,section{background:#141c31;border:1px solid #293657;border-radius:10px;padding:16px;margin:14px 0}.metric{font-size:25px;font-weight:700}.good{color:#8be9b0}.warn{color:#ffd48a}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px;border-bottom:1px solid #293657}canvas{width:100%;height:420px;background:#0b1020;border-radius:8px}button,label{margin:3px}pre{overflow:auto;white-space:pre-wrap}@media(max-width:600px){main{padding:14px}table{font-size:12px}canvas{height:300px}}"""
JS = """function drawTimeline(id,values){const c=document.getElementById(id);if(!c||!values.length)return;const x=c.getContext('2d'),w=c.width=c.clientWidth*devicePixelRatio,h=c.height=c.clientHeight*devicePixelRatio,m=Math.max(...values,1);x.strokeStyle='#83d4ff';x.lineWidth=2*devicePixelRatio;x.beginPath();values.forEach((v,i)=>{const px=i*w/Math.max(values.length-1,1),py=h-(v/m*h*.9+h*.05);i?x.lineTo(px,py):x.moveTo(px,py)});x.stroke()}"""
REPLAY_JS = """(()=>{
const node=document.getElementById('episode-data');if(!node)return;
const d=JSON.parse(node.textContent),c=document.getElementById('replay'),s=document.getElementById('scrubber'),label=document.getElementById('time'),details=document.getElementById('entity-details'),layers={players:1,static:1,dynamic:1,projectiles:1,explosions:1,exact:1,derived:1};
let playing=false,t=0,speed=1,latest=[];const all=[...d.samples,...d.players],range=all.length?all:[{game_ms:d.trigger_game_ms,x:0,y:0}],min=Math.min(...range.map(x=>x.game_ms),d.trigger_game_ms),max=Math.max(...range.map(x=>x.game_ms),d.trigger_game_ms);s.min=min;s.max=max;s.value=d.trigger_game_ms;
const visible=name=>layers[name]!==0;
function render(){t=+s.value;label.textContent=((t-d.trigger_game_ms)/1000).toFixed(2)+' s';const x=c.getContext('2d'),w=c.width=c.clientWidth*devicePixelRatio,h=c.height=c.clientHeight*devicePixelRatio,rows=all.filter(v=>Math.abs(v.game_ms-t)<120),xs=range.map(v=>v.x||0),ys=range.map(v=>v.y||0),loX=Math.min(...xs)-10,hiX=Math.max(...xs)+10,loY=Math.min(...ys)-10,hiY=Math.max(...ys)+10,px=v=>(v-loX)/(hiX-loX||1)*w,py=v=>h-(v-loY)/(hiY-loY||1)*h,scale=Math.abs(px(loX+1)-px(loX));latest=rows.map(v=>({...v,_px:px(v.x||0),_py:py(v.y||0)}));x.fillStyle='#0b1020';x.fillRect(0,0,w,h);
if(visible('static'))d.entities.forEach(e=>{try{const a=JSON.parse(e.manifest_json||'{}').aabb;if(a){x.strokeStyle='#596780';x.strokeRect(px(a.left),py(a.top),Math.max(1,px(a.right)-px(a.left)),Math.max(1,py(a.bottom)-py(a.top)))}}catch(_){}});
d.samples.filter(v=>v.game_ms<=t&&t-v.game_ms<1500).forEach(v=>{if(visible(v.is_missile?'projectiles':'dynamic')){x.fillStyle=v.is_missile?'#ff9f6655':'#83d4ff44';x.fillRect(px(v.x||0)-1,py(v.y||0)-1,2,2)}});
if(visible('explosions'))(d.explosions||[]).filter(v=>Math.abs(v.game_ms-t)<180).forEach(v=>{x.strokeStyle='#ff5f6d';x.beginPath();x.arc(px(v.x||0),py(v.y||0),Math.max(2,(v.radius||0)*scale),0,Math.PI*2);x.stroke()});
latest.forEach(v=>{const player=!!v.player_session_id,projectile=!!v.is_missile;if(player&&!visible('players')||projectile&&!visible('projectiles')||!player&&!projectile&&!visible('dynamic'))return;x.fillStyle=player?'#8be9b0':projectile?'#ff9f66':'#83d4ff';x.beginPath();x.arc(v._px,v._py,player?7:projectile?3:4,0,Math.PI*2);x.fill();if(player){x.fillStyle='#dbeafe';x.fillText(v.player_session_id.slice(0,5),v._px+8,v._py-8)}});
d.interactions.filter(v=>Math.abs(v.game_ms-t)<120).forEach(v=>{if(v.source_quality==='exact'&&!visible('exact')||v.source_quality!=='exact'&&!visible('derived'))return;x.fillStyle=v.source_quality==='exact'?'#ffd48a':'#f48fb1';x.fillRect(px(v.x||0)-3,py(v.y||0)-3,6,6)});if(playing){s.value=Math.min(max,t+50*speed);if(+s.value>=max)playing=false;requestAnimationFrame(render)}}
c.onclick=e=>{const r=c.getBoundingClientRect(),cx=(e.clientX-r.left)*c.width/r.width,cy=(e.clientY-r.top)*c.height/r.height;let best=null,dist=Infinity;latest.forEach(v=>{const dx=cx-v._px,dy=cy-v._py;if(dx*dx+dy*dy<dist){dist=dx*dx+dy*dy;best=v}});if(best)details.textContent=JSON.stringify(best,null,2)};
document.getElementById('play').onclick=()=>{playing=!playing;if(playing)render()};document.querySelectorAll('[data-speed]').forEach(b=>b.onclick=()=>speed=+b.dataset.speed);document.querySelectorAll('[data-layer]').forEach(b=>b.onchange=()=>{layers[b.dataset.layer]=+b.checked;render()});s.oninput=render;render()})();"""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _page(title: str, content: str, asset_prefix: str = "assets/") -> str:
    return f"<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><link rel='stylesheet' href='{asset_prefix}dashboard.css'><main>{content}</main></html>"


def _number(value: object) -> str:
    return f"{value:,.0f}" if isinstance(value, (int, float)) else "—"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-") or "player"


def _episode_page(episode: dict) -> str:
    toggles = " ".join(f"<label><input type='checkbox' data-layer='{key}' checked> {label}</label>" for key, label in (("players", "players"), ("static", "static objects"), ("dynamic", "dynamic objects"), ("projectiles", "projectiles"), ("explosions", "explosions"), ("exact", "exact interactions"), ("derived", "derived interactions")))
    content = f"<nav><a href='../index.html'>Dashboard</a></nav><h1>Scene episode: {html.escape(episode['trigger'])}</h1><p class='muted'>Coverage {episode['coverage']:.0%}. States are interpolated; this is not a physics-exact replay.</p><section><canvas id='replay'></canvas><p><button id='play'>Play / pause</button> <button data-speed='0.25'>0.25×</button> <button data-speed='0.5'>0.5×</button> <button data-speed='1'>1×</button> <button data-speed='2'>2×</button> <span id='time'></span></p><p>{toggles}</p><input id='scrubber' type='range' style='width:100%'></section><section><h2>Selected entity</h2><pre id='entity-details'>Click a visible entity in the replay.</pre></section><section><h2>Interaction timeline</h2><pre>{html.escape(json.dumps(episode['interactions'], ensure_ascii=False, indent=2))}</pre></section><section><h2>Limitations</h2><p>{html.escape(' '.join(episode['limitations']))}</p></section><script id='episode-data' type='application/json'>{_json(episode)}</script><script>{REPLAY_JS}</script>"
    return _page("SFD scene episode", content, "../assets/")


def build_dashboard(report_directory: str, report_date: str | None = None, telemetry_database: str | None = None, episode_id: int | None = None) -> Path:
    all_reports = sorted(Path(report_directory).glob("*.json"))
    dashboard = Path(report_directory).parent / "dashboard"
    assets, days, players_dir, episodes = dashboard / "assets", dashboard / "days", dashboard / "players", dashboard / "episodes"
    for directory in (assets, days, players_dir, episodes):
        directory.mkdir(parents=True, exist_ok=True)
    (assets / "dashboard.css").write_text(CSS, encoding="utf-8")
    (assets / "dashboard.js").write_text(JS, encoding="utf-8")
    reports = [path for path in all_reports if report_date is None or path.stem == report_date]
    if telemetry_database:
        from .scene import load_episode
        telemetry = sqlite3.connect(Path(telemetry_database).resolve().as_uri() + "?mode=ro", uri=True)
        telemetry.row_factory = sqlite3.Row
        try:
            ids = [episode_id] if episode_id is not None else []
            if not ids:
                for path in reports:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    ids.extend(item["source_event_id"] for item in payload.get("environment", {}).get("episodes", []))
            for source_id in dict.fromkeys(ids):
                episode = load_episode(telemetry, int(source_id))
                if episode:
                    (episodes / f"{source_id}.html").write_text(_episode_page(episode), encoding="utf-8")
        finally:
            telemetry.close()
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
        environment = payload.get("environment", {})
        episode_links = "".join(f"<li><a href='../episodes/{item['source_event_id']}.html'>{html.escape(item['trigger'])} — {html.escape(item['utc_timestamp'])}</a></li>" for item in environment.get("episodes", [])[:50])
        navigation = " ".join(f"<a href='#{anchor}'>{label}</a>" for anchor, label in (("overview", "Overview"), ("players", "Players"), ("maps", "Maps"), ("rounds", "Rounds"), ("combat", "Combat"), ("movement", "Movement"), ("environment", "Environment"), ("episodes", "Scene episodes"), ("network", "Network"), ("chat", "Chat"), ("patterns", "Patterns"), ("quality", "Data quality")))
        environment_summary = {key: environment.get(key, {} if key not in {"motifs", "trajectory_clusters", "object_usage", "interaction_graph", "barrel_boost_candidates"} else []) for key in ("lifecycle", "object_categories", "interactions_by_type", "object_usage", "interaction_graph", "motifs", "trajectory_clusters")}
        environment_summary["barrel_boost_candidates"] = environment.get("barrel_boost_candidates", [])[:20]
        movement_summary = [{"player": player.get("display_name", player["player_identity_id"]), **player.get("movement", {})} for player in payload["players"]]
        content = f"<nav><a href='../index.html'>All days</a> · {navigation}</nav><h1>SFD telemetry — {html.escape(payload['report_date'])}</h1><p class='muted'>{scene_note}</p><section id='overview'><div class='grid'>{card_html}</div></section><section id='players'><h2>Players</h2><table><tr><th>Player</th><th>Visits</th><th>Playtime</th><th>Kills*</th><th>Assists*</th></tr>{player_rows}</table></section><section id='maps'><h2>Maps</h2><table><tr><th>Map</th><th>Rounds</th><th>Mean duration</th></tr>{maps}</table></section><section id='rounds'><h2>Rounds</h2><pre>{html.escape(json.dumps(payload.get('rounds', [])[:100], ensure_ascii=False, indent=2))}</pre></section><section id='combat'><h2>Combat</h2><p>* Kill and assist data are inferred from available telemetry, not authoritative game scoring.</p><pre>{html.escape(json.dumps(combat, ensure_ascii=False, indent=2))}</pre></section><section id='movement'><h2>Movement</h2><pre>{html.escape(json.dumps(movement_summary, ensure_ascii=False, indent=2))}</pre></section><section id='environment'><h2>Environment</h2><pre>{html.escape(json.dumps(environment_summary, ensure_ascii=False, indent=2))}</pre></section><section id='episodes'><h2>Scene episodes</h2><ul>{episode_links or '<li>No scene episodes for this day.</li>'}</ul></section><section id='network'><h2>Network</h2><pre>{html.escape(json.dumps(payload.get('network', {}), ensure_ascii=False, indent=2))}</pre></section><section id='chat'><h2>Chat</h2><pre>{html.escape(json.dumps(payload.get('chat', {}), ensure_ascii=False, indent=2))}</pre></section><section id='patterns'><h2>Patterns</h2><pre>{html.escape(json.dumps(payload.get('patterns', [])[:100], ensure_ascii=False, indent=2))}</pre></section><section id='quality'><h2>Data quality</h2><pre>{html.escape(json.dumps(quality, ensure_ascii=False, indent=2))}</pre></section>"
        (days / f"{path.stem}.html").write_text(_page(f"SFD — {path.stem}", content, "../assets/"), encoding="utf-8")
        links.append(f"<li><a href='days/{path.stem}.html'>{html.escape(path.stem)}</a></li>")
        for player in payload["players"]:
            identity = player["player_identity_id"]
            content = f"<nav><a href='../days/{path.stem}.html'>Day</a></nav><h1>{html.escape(player.get('display_name', identity))}</h1><section><pre>{html.escape(json.dumps(player, ensure_ascii=False, indent=2))}</pre></section>"
            (players_dir / f"{_slug(identity)}.html").write_text(_page("SFD player", content, "../assets/"), encoding="utf-8")
    all_links = [f"<li><a href='days/{path.stem}.html'>{html.escape(path.stem)}</a></li>" for path in all_reports]
    index = f"<h1>SFD Telemetry Dashboard</h1><p class='muted'>Local static reports. No network requests are made.</p><section><h2>Days</h2><ul>{''.join(all_links) or '<li>No reports yet</li>'}</ul></section>"
    (dashboard / "index.html").write_text(_page("SFD Telemetry Dashboard", index), encoding="utf-8")
    return dashboard / "index.html"
