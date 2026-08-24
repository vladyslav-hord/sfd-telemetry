from __future__ import annotations

import html
import json
import math
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .report import _atomic_write_text


MAX_PLAYERS = 100
MAX_ROUNDS = 100
MAX_NETWORK = 100
MAX_PATTERNS = 100
MAX_EPISODES = 100
MAX_HEATMAP = 250
MAX_OBJECTS = 50
MAX_MOTIFS = 25
MAX_TRAJECTORIES = 50


CSS = """*{box-sizing:border-box}body{margin:0;background:#0b1020;color:#e8edf7;font:15px system-ui,sans-serif}main{max-width:1440px;margin:auto;padding:24px}a{color:#83d4ff}h1,h2,h3{margin:0 0 12px}.muted{color:#9ba8bc}.hero{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;margin-bottom:18px}.hero p{margin:5px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:12px}.columns{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:14px}.card,section{background:#141c31;border:1px solid #293657;border-radius:10px;padding:16px;margin:14px 0}.card{margin:0}.metric{font-size:25px;font-weight:700}.good{color:#8be9b0}.warn{color:#ffd48a}.bad{color:#ff8e9e}.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.toolbar input,.toolbar select,button{background:#0b1020;color:#e8edf7;border:1px solid #405276;border-radius:6px;padding:7px 9px}button{cursor:pointer}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px;border-bottom:1px solid #293657;vertical-align:top}th{color:#aebbd0;font-size:12px;text-transform:uppercase;letter-spacing:.04em}tbody tr:hover{background:#1b2742}canvas{width:100%;height:260px;background:#0b1020;border-radius:8px}pre{overflow:auto;white-space:pre-wrap;max-height:360px}.chips{display:flex;gap:7px;flex-wrap:wrap}.chip{border:1px solid #405276;border-radius:999px;padding:4px 8px;color:#c7d4e8}.empty{color:#9ba8bc;padding:12px 0}.status{font-weight:600}.status-ok{color:#8be9b0}.status-warn{color:#ffd48a}.status-bad{color:#ff8e9e}.section-note{margin-top:-4px}.day-links a{display:inline-block;margin:4px 8px 4px 0}.progress{height:8px;background:#26324e;border-radius:999px;overflow:hidden}.progress>span{display:block;height:100%;background:#83d4ff}.small{font-size:12px}@media(max-width:700px){main{padding:14px}.hero{display:block}.columns{grid-template-columns:1fr}table{font-size:12px}canvas{height:220px}.card{padding:12px}}"""


JS = r"""(()=>{
  const app=document.getElementById('dashboard-app');
  if(!app)return;
  const fallback=window.__DASHBOARD_DATA__||{};
  const esc=value=>String(value??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num=value=>typeof value==='number'&&Number.isFinite(value)?value.toLocaleString(undefined,{maximumFractionDigits:1}):'—';
  const set=(id,value)=>{const node=document.getElementById(id);if(node)node.innerHTML=value};
  const rows=(id,head,body)=>set(id,`<table><thead><tr>${head.map(x=>`<th>${esc(x)}</th>`).join('')}</tr></thead><tbody>${body||`<tr><td colspan="${head.length}" class="empty">No data</td></tr>`}</tbody></table>`);
  const list=(items,empty='No data')=>items&&items.length?items.map(x=>`<li>${esc(x)}</li>`).join(''):`<li class="empty">${empty}</li>`;
  function cards(data){
    const k=data.kpi||{},items=[['Players',k.players],['Humans',k.humans],['Bots',k.bots],['Sessions',k.sessions],['Rounds',k.rounds],['Events',k.events],['Damage',k.damage],['Inferred kills',k.inferred_kills],['p95 ping',k.p95_ping],['Ping peak',k.ping_peak],['Scene interactions',k.scene_interactions],['LLM queued',k.llm_queued]];
    set('kpis',items.map(([label,value])=>`<div class="card"><div class="muted">${esc(label)}</div><div class="metric">${esc(value==null?'—':`${num(value)}${label.toLowerCase().includes('ping')?' ms':''}`)}</div></div>`).join(''));
  }
  function drawTimeline(series){
    const canvas=document.getElementById('timeline-chart');if(!canvas||!series?.length)return;
    const dpr=window.devicePixelRatio||1,w=canvas.clientWidth*dpr,h=canvas.clientHeight*dpr;canvas.width=w;canvas.height=h;
    const ctx=canvas.getContext('2d');ctx.clearRect(0,0,w,h);ctx.strokeStyle='#293657';ctx.lineWidth=1*dpr;
    for(let i=1;i<5;i++){const y=i*h/5;ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke()}
    const values=series.map(x=>Number(x.events||x.rounds||0)),max=Math.max(...values,1);ctx.strokeStyle='#83d4ff';ctx.lineWidth=2*dpr;ctx.beginPath();
    values.forEach((value,i)=>{const x=i*w/Math.max(values.length-1,1),y=h-(value/max*h*.84+h*.08);i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke();
    const labels=series.filter((_,i)=>i===0||i===series.length-1).map(x=>x.t||'').join(' → ');set('timeline-label',esc(labels||'Recent activity'));
  }
  function render(data){
    data=data||{};const f=data.freshness||{};set('title',esc(data.kind==='live'?'SFD live telemetry':`SFD telemetry — ${data.report_date||'unknown date'}`));
    set('freshness',`<span class="status">${esc(data.status?.deterministic||'unknown')}</span> · LLM: ${esc(data.status?.llm||data.ai?.status||'unknown')} · generated ${esc(f.generated_at||data.generated_at||'—')} · ${esc(f.cutoff||'')}`);
    cards(data);drawTimeline(data.timeline||data.series||[]);
    const players=(data.players||[]).map(x=>`<tr><td>${esc(x.player||x.label)}</td><td>${x.bot?'bot':'human'}</td><td>${num(x.sessions)}</td><td>${num(x.playtime_min)} min</td><td>${num(x.combat?.inferred_kills)}</td><td>${num(x.combat?.damage_dealt)}</td><td>${num(x.input?.apm)}</td><td>${num(x.network?.p95)} ms</td></tr>`).join('');
    rows('players-table',['Player','Type','Sessions','Playtime','Kills','Damage','APM','p95 ping'],players);
    const maps=(data.maps||[]).map(x=>`<tr><td>${esc(x.map)}</td><td>${num(x.rounds)}</td><td>${num(x.duration_min)} min</td></tr>`).join('');rows('maps-table',['Map','Rounds','Duration'],maps);
    const rounds=(data.rounds||[]).map(x=>`<tr><td>${num(x.round)}</td><td>${esc(x.map)}</td><td>${num(x.duration_s)} s</td><td>${num(x.players)}</td><td>${esc(x.quality)}</td></tr>`).join('');rows('rounds-table',['Round','Map','Duration','Players','Quality'],rounds);
    const weapons=(data.combat?.weapons||[]).map(x=>`<tr><td>${esc(x.weapon)}</td><td>${num(x.events)}</td><td>${num(x.damage)}</td></tr>`).join('');rows('weapons-table',['Weapon','Events','Damage'],weapons);
    const pairs=(data.combat?.pairs||[]).map(x=>`<tr><td>Pair ${num(x.rank)}</td><td>${num(x.events)}</td><td>${num(x.damage)}</td></tr>`).join('');rows('pairs-table',['Pair','Events','Damage'],pairs);
    const net=(data.network?.outliers||[]).map(x=>`<tr><td>${esc(x.player||x.label)}</td><td>${num(x.samples)}</td><td>${num(x.p95)} ms</td><td>${num(x.p99)} ms</td><td>${num(x.spikes)}</td><td>${num(x.seconds_above)}</td></tr>`).join('');rows('network-table',['Player','Samples','p95','p99','Spikes','Seconds above'],net);
    const pats=(data.patterns||[]).map(x=>`<tr><td>${esc(x.trigger||x.family)}</td><td>${esc(x.state)}</td><td>${num(x.confidence)}</td><td>${num(x.occurrences)}</td><td>${num(x.robust_z)}</td></tr>`).join('');rows('patterns-table',['Trigger','State','Confidence','Occurrences','Robust z'],pats);
    const episodes=(data.environment?.episodes||[]).map(x=>`<tr><td>${num(x.episode_id)}</td><td>${esc(x.trigger)}</td><td>${esc(x.utc_timestamp||x.created_at)}</td><td>${num(x.coverage)}</td><td>${num(x.entity_count)}</td></tr>`).join('');rows('episodes-table',['Episode','Trigger','Timestamp','Coverage','Entities'],episodes);
    const heat=(data.environment?.heatmap||[]).slice(0,30).map(x=>`<span class="chip">${num(x.x)}, ${num(x.y)}: ${num(x.count)}</span>`).join('');set('heatmap',heat||'<span class="empty">No scene heatmap</span>');
    const cats=Object.entries(data.environment?.interaction_types||{}).map(([k,v])=>`<span class="chip">${esc(k)}: ${num(v)}</span>`).join('');set('scene-types',cats||'<span class="empty">No scene interactions</span>');
    set('scene-objects',(data.environment?.object_usage||[]).map(x=>`<tr><td>${esc(x.name)}</td><td>${esc(x.category)}</td><td>${num(x.interactions)}</td></tr>`).join('')||'<tr><td colspan="3" class="empty">No object usage</td></tr>');
    set('scene-motifs',(data.environment?.motifs||[]).map(x=>`<span class="chip">${esc(x.motif)}: ${num(x.occurrences)}</span>`).join('')||'<span class="empty">No motifs</span>');
    set('barrel-candidates',(data.environment?.barrel_boost_candidates||[]).slice(0,20).map(x=>`<tr><td>${num(x.rank)}</td><td>${esc(x.object)}</td><td>${esc(x.confidence)}</td><td>${num(x.player_speed_gain)}</td><td>${esc(x.advantage)}</td></tr>`).join('')||'<tr><td colspan="5" class="empty">No high-confidence candidates</td></tr>');
    const eventTypes=Object.entries(data.combat?.events_by_type||{}).map(([k,v])=>`<span class="chip">${esc(k)}: ${num(v)}</span>`).join('');set('event-types',eventTypes||'<span class="empty">No combat event data</span>');
    const narrative=data.ai?.narrative||{};set('ai-content',narrative.headline?`<h3>${esc(narrative.headline)}</h3><div class="columns">${['server_health','player_experience','map_findings','network_findings','player_highlights','pattern_findings','chat_findings','possible_factors','limitations'].map(k=>`<div><h3>${esc(k.replaceAll('_',' '))}</h3><ul>${list(narrative[k])}</ul></div>`).join('')}</div>`:`<p class="empty">No narrative analysis available. Queue: ${num(data.ai?.queue?.queued)} queued, ${num(data.ai?.queue?.submitted)} submitted, ${num(data.ai?.queue?.complete)} complete.</p>`);
    const quality=data.quality||{};set('quality-content',`<div class="chips">${Object.entries(quality).map(([k,v])=>`<span class="chip">${esc(k)}: ${esc(typeof v==='number'?num(v):v)}</span>`).join('')||'<span class="empty">No quality data</span>'}</div>`);
    const storage=data.storage||[];set('storage-content',storage.map(x=>`<div class="card"><div>${esc(x.component)}</div><div class="metric">${esc(x.state)}</div><div class="small">${num(x.used_bytes)} / ${num(x.max_bytes)} bytes</div><div class="progress"><span style="width:${Math.min(100,Math.max(0,Number(x.watermark||0)*100))}%"></span></div></div>`).join('')||'<p class="empty">No storage data</p>');
    set('environment-content',`<div class="chips"><span class="chip">Damage: ${num(data.environment?.environmental_damage)}</span><span class="chip">Samples: ${num(data.environment?.scene_samples)}</span>${Object.entries(data.environment?.categories||{}).map(([k,v])=>`<span class="chip">${esc(k)}: ${num(v)}</span>`).join('')}</div>`);
    set('refresh-state',data.kind==='live'?'Live snapshot; polling every 60 seconds.':'Static day summary.');
  }
  let current=fallback;render(current);
  async function poll(){const url=app.dataset.dataUrl;if(!url)return;try{const response=await fetch(`${url}?v=${Date.now()}`,{cache:'no-store'});if(!response.ok)throw new Error(response.status);current=await response.json();render(current);set('refresh-state','Live snapshot loaded from live.json.')}catch(_){set('refresh-state','Using embedded snapshot; start the local dashboard server for live polling.')}}
  if(app.dataset.kind==='live'){poll();setInterval(poll,60000)}
  window.addEventListener('resize',()=>drawTimeline(current.timeline||current.series||[]));
})();"""


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
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _page(title: str, content: str, asset_prefix: str = "assets/") -> str:
    return f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><link rel='stylesheet' href='{asset_prefix}dashboard.css'></head><body><main>{content}</main></body></html>"


def _number(value: object) -> str:
    return f"{value:,.1f}" if isinstance(value, float) and not value.is_integer() else f"{value:,.0f}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "—"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-") or "player"


def _atomic_json(path: Path, value: object) -> Path:
    return _atomic_write_text(path, _json(value) + "\n")


def _artifact_family(path: Path) -> str:
    return path.stem.split(".fallback-", 1)[0]


def _select_report_artifacts(directory: Path) -> list[Path]:
    families: dict[str, list[Path]] = defaultdict(list)
    for path in directory.glob("*.json"):
        if path.is_file():
            families[_artifact_family(path)].append(path)
    return sorted((max(paths, key=lambda item: item.stat().st_mtime_ns) for paths in families.values()), key=_artifact_family)


def _metric_json(value: object) -> dict[str, Any]:
    try: parsed = json.loads(value or "{}") if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError): parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value); return number if math.isfinite(number) else default
    except (TypeError, ValueError): return default


def _row_dict(row: sqlite3.Row | dict) -> dict[str, Any]: return dict(row)


def _fetch(conn: sqlite3.Connection, query: str, args: tuple = ()) -> list[dict[str, Any]]:
    try: return [_row_dict(row) for row in conn.execute(query, args).fetchall()]
    except sqlite3.Error: return []


def _player_labels(players: list[dict], extra: list[str] | None = None) -> dict[str, str]:
    values = {str(item.get("player_identity_id")) for item in players if item.get("player_identity_id")}; values.update(str(item) for item in (extra or []) if item)
    return {value: f"Player {index:03d}" for index, value in enumerate(sorted(values), 1)}


def _compact_player(player: dict, label: str) -> dict:
    combat = player.get("combat") or {}; movement = player.get("movement") or {}; input_data = player.get("input") or {}; profile = player.get("skill_profile") or {}; statistics = player.get("statistics") or {}
    stat_keys = ("TotalDamageTaken", "TotalShotsFired", "TotalProjectilesHitBy", "TotalMeleeAttackHits", "TotalJumps", "TotalRolls", "TotalDives")
    return {"player": label, "bot": bool(player.get("is_bot")), "sessions": player.get("sessions", 0), "visits": player.get("visits", 0), "playtime_min": round(_float(player.get("playtime_seconds")) / 60, 1), "combat": {"events": combat.get("combat_events", 0), "damage_dealt": round(_float(combat.get("damage_dealt")), 1), "damage_received": round(_float(combat.get("damage_received")), 1), "inferred_kills": combat.get("inferred_kill_credit", 0), "inferred_assists": combat.get("inferred_assist_credit", 0)}, "movement": {"distance": round(_float(movement.get("distance")), 1), "active_distance": round(_float(movement.get("active_distance")), 1), "static_seconds": round(_float(movement.get("static_seconds")), 1), "coverage": movement.get("coverage", 0)}, "input": {"events": input_data.get("events", 0), "apm": round(_float(input_data.get("actions_per_minute")), 1)}, "skill": {"confidence": profile.get("confidence", 0), "percentiles": profile.get("percentiles", {})}, "statistics": {key: statistics[key] for key in stat_keys if key in statistics}, "network": {}}


def _compact_round(item: dict, index: int) -> dict:
    return {"round": index, "map": item.get("map_name") or "Unknown", "duration_s": round(_float(item.get("duration_seconds")), 1), "players": item.get("player_count", 0), "humans": item.get("human_count", 0), "bots": item.get("bot_count", 0), "quality": item.get("result_quality") or "unknown", "type": item.get("game_type") or item.get("map_type") or "unknown", "started_at": item.get("started_at")}


def _compact_environment(environment: dict) -> dict:
    candidates = []
    for index, item in enumerate(environment.get("barrel_boost_candidates", [])[:MAX_OBJECTS], 1):
        candidates.append({"rank": index, "confidence": item.get("confidence"), "coverage": item.get("coverage", 0), "object": item.get("object_name") or "unknown", "pattern": item.get("pattern_type") or item.get("subtype"), "advantage": item.get("observed_advantage"), "object_speed_gain": round(_float(item.get("object_speed_gain")), 2), "player_speed_gain": round(_float(item.get("player_speed_gain")), 2), "player_displacement": round(_float(item.get("player_displacement")), 2)})
    episodes = [{"episode_id": item.get("source_event_id"), "trigger": item.get("trigger"), "coverage": item.get("coverage", 0), "utc_timestamp": item.get("utc_timestamp"), "entity_count": item.get("entity_count", 0)} for item in environment.get("episodes", [])[:MAX_EPISODES]]
    return {"available": bool(environment.get("available")), "environmental_damage": environment.get("environmental_damage", 0), "scene_samples": environment.get("scene_samples", 0), "interaction_types": dict(environment.get("interactions_by_type", {})), "categories": dict(environment.get("object_categories", {})), "heatmap": environment.get("interaction_heatmap", [])[:MAX_HEATMAP], "object_usage": [{"name": item.get("name"), "category": item.get("category"), "interactions": item.get("interactions", 0)} for item in environment.get("object_usage", [])[:MAX_OBJECTS]], "motifs": environment.get("motifs", [])[:MAX_MOTIFS], "trajectory_clusters": environment.get("trajectory_clusters", [])[:MAX_TRAJECTORIES], "barrel_boost_candidates": candidates, "episodes": episodes}


def _compact_network(sessions: list[dict], labels: dict[str, str]) -> dict:
    compact = []
    for item in sorted(sessions, key=lambda value: (_float(value.get("p95"), -1), _float(value.get("p99"), -1)), reverse=True)[:MAX_NETWORK]:
        spikes = item.get("spikes", {}); above = item.get("seconds_above", {})
        compact.append({"player": labels.get(str(item.get("player_session_id")), "Player"), "samples": item.get("samples", 0), "mean": round(_float(item.get("mean")), 1), "median": round(_float(item.get("median")), 1), "p95": round(_float(item.get("p95")), 1), "p99": round(_float(item.get("p99")), 1), "max": round(_float(item.get("max")), 1), "jitter": round(_float(item.get("estimated_jitter")), 1), "spikes": spikes.get("50", 0) if isinstance(spikes, dict) else 0, "seconds_above": above.get("100", 0) if isinstance(above, dict) else 0})
    p95 = [_float(item.get("p95")) for item in sessions if item.get("p95") is not None]
    return {"sessions": len(sessions), "mean_p95": round(sum(p95) / len(p95), 1) if p95 else None, "max_p95": round(max(p95), 1) if p95 else None, "outliers": compact}


def _compact_patterns(patterns: list[dict]) -> list[dict]:
    result = []
    for item in patterns[:MAX_PATTERNS]:
        features = item.get("features") if isinstance(item.get("features"), dict) else {}
        result.append({"trigger": item.get("trigger") or item.get("pattern_family") or "pattern", "signature": item.get("signature"), "state": item.get("state", "candidate"), "confidence": item.get("confidence", 0), "occurrences": item.get("occurrences", 1), "robust_z": round(_float(item.get("robust_z")), 2), "features": {key: features[key] for key in ("coverage", "sample_count", "distance", "mean_speed", "max_speed", "object_speed_gain", "player_speed_gain") if key in features}})
    return result


def _narrative(value: object) -> dict | None:
    if not isinstance(value, dict) or not isinstance(value.get("headline"), str): return None
    keys = ("headline", "server_health", "player_experience", "map_findings", "network_findings", "player_highlights", "pattern_findings", "chat_findings", "possible_factors", "limitations")
    return {key: value.get(key, []) if key != "headline" else value.get(key, "") for key in keys}


def _compact_report(payload: dict) -> dict:
    players = payload.get("players") if isinstance(payload.get("players"), list) else []; labels = _player_labels(players)
    server = payload.get("server") if isinstance(payload.get("server"), dict) else {}; quality = payload.get("data_quality") if isinstance(payload.get("data_quality"), dict) else server.get("data_quality", {}); combat = server.get("combat") if isinstance(server.get("combat"), dict) else {}
    environment = _compact_environment(payload.get("environment") if isinstance(payload.get("environment"), dict) else server.get("environment", {})); network_raw = payload.get("network", {}).get("sessions", []) if isinstance(payload.get("network"), dict) else []
    network = _compact_network(network_raw, labels); player_rows = [_compact_player(item, labels.get(str(item.get("player_identity_id")), f"Player {index:03d}")) for index, item in enumerate(sorted(players, key=lambda value: _float(value.get("playtime_seconds")), reverse=True)[:MAX_PLAYERS], 1)]
    maps = [{"map": item.get("map_name") or "Unknown", "rounds": item.get("rounds", 0), "duration_min": round(_float(item.get("duration_seconds")) / 60, 1)} for item in (payload.get("maps") or [])[:MAX_OBJECTS]]; rounds = [_compact_round(item, index) for index, item in enumerate((payload.get("rounds") or [])[:MAX_ROUNDS], 1)]
    weapons = [{"weapon": item.get("weapon") or "Unknown", "events": item.get("events", 0), "damage": round(_float(item.get("damage")), 1)} for item in (payload.get("weapons") or [])[:MAX_OBJECTS]]; pairs = [{"rank": index, "events": item.get("events", 0), "damage": round(_float(item.get("damage")), 1)} for index, item in enumerate(sorted(payload.get("interactions") or [], key=lambda value: _float(value.get("damage")), reverse=True)[:MAX_OBJECTS], 1)]
    timeline: dict[str, dict[str, float]] = {}
    for item in payload.get("rounds") or []:
        stamp = str(item.get("started_at") or "unknown")[:13]; bucket = timeline.setdefault(stamp, {"t": stamp, "rounds": 0, "events": 0, "players": 0, "duration_s": 0}); bucket["rounds"] += 1; bucket["events"] += 1; bucket["players"] += item.get("player_count", 0) or 0; bucket["duration_s"] += _float(item.get("duration_seconds"))
    series = list(timeline.values())
    for item in series: item["duration_s"] = round(item["duration_s"], 1)
    return {"schema_version": 2, "kind": "day", "report_date": payload.get("report_date"), "freshness": {"generated_at": payload.get("generated_at"), "cutoff": payload.get("data_cutoff")}, "status": payload.get("status", {}), "kpi": {"players": server.get("humans", 0) + server.get("bots", 0), "humans": server.get("humans", 0), "bots": server.get("bots", 0), "sessions": server.get("sessions", 0), "rounds": server.get("rounds", 0), "events": quality.get("event_count", 0), "damage": combat.get("damage", 0), "inferred_kills": combat.get("inferred_kills", 0), "p95_ping": network.get("max_p95"), "scene_interactions": sum(environment["interaction_types"].values()), "llm_queued": None}, "timeline": series, "players": player_rows, "maps": maps, "rounds": rounds, "combat": {"events_by_type": server.get("events_by_type", {}), "damage": combat.get("damage", 0), "inferred_kills": combat.get("inferred_kills", 0), "weapons": weapons, "pairs": pairs}, "network": network, "environment": environment, "patterns": _compact_patterns(payload.get("patterns") or []), "ai": {"status": (payload.get("status") or {}).get("llm"), "narrative": _narrative(payload.get("narrative")), "queue": {}}, "quality": quality, "storage": []}


def _dashboard_shell(summary: dict, data_url: str, kind: str, title: str, asset_prefix: str = "assets/") -> str:
    content = f"""<div id='dashboard-app' data-kind='{html.escape(kind)}' data-data-url='{html.escape(data_url)}'><header class='hero'><div><h1 id='title'>{html.escape(title)}</h1><p id='freshness' class='muted'></p><p id='refresh-state' class='small muted'></p></div><div class='toolbar'><a href='{asset_prefix}../index.html'>All days</a>{" · <a href='live.html'>Live</a>" if kind != 'live' else " · <a href='index.html'>Dashboard</a>"}</div></header><section id='overview'><h2>Overview</h2><div id='kpis' class='grid'></div></section><section id='timeline'><h2>Timeline</h2><p id='timeline-label' class='muted section-note'></p><canvas id='timeline-chart'></canvas></section><div class='columns'><section id='players'><h2>Players</h2><div id='players-table'></div></section><section id='maps'><h2>Maps</h2><div id='maps-table'></div></section></div><div class='columns'><section id='rounds'><h2>Rounds</h2><div id='rounds-table'></div></section><section id='combat'><h2>Combat and weapons</h2><div id='event-types' class='chips'></div><div id='weapons-table'></div><h3>Top interactions</h3><div id='pairs-table'></div></section></div><section id='network'><h2>Network</h2><div id='network-table'></div></section><section id='environment'><h2>Environment and scene</h2><div id='environment-content'></div><h3>Interaction types</h3><div id='scene-types' class='chips'></div><h3>Heatmap cells</h3><div id='heatmap' class='chips'></div><h3>Object usage</h3><div><table><thead><tr><th>Object</th><th>Category</th><th>Interactions</th></tr></thead><tbody id='scene-objects'></tbody></table></div><h3>Motifs</h3><div id='scene-motifs' class='chips'></div><h3>Barrel boost candidates</h3><div><table><thead><tr><th>Rank</th><th>Object</th><th>Confidence</th><th>Player speed gain</th><th>Observation</th></tr></thead><tbody id='barrel-candidates'></tbody></table></div><h3>Episodes</h3><div id='episodes-table'></div></section><section id='patterns'><h2>Patterns and anomalies</h2><div id='patterns-table'></div></section><section id='ai'><h2>AI analysis</h2><div id='ai-content'></div></section><div class='columns'><section id='quality'><h2>Data quality</h2><div id='quality-content'></div></section><section id='storage'><h2>Storage health</h2><div id='storage-content' class='grid'></div></section></div><script>window.__DASHBOARD_DATA__={_json(summary)};</script><script src='{asset_prefix}dashboard.js' defer></script></div>"""
    return _page(title, content, asset_prefix)


def _episode_page(episode: dict) -> str:
    toggles = " ".join(f"<label><input type='checkbox' data-layer='{key}' checked> {label}</label>" for key, label in (("players", "players"), ("static", "static objects"), ("dynamic", "dynamic objects"), ("projectiles", "projectiles"), ("explosions", "explosions"), ("exact", "exact interactions"), ("derived", "derived interactions")))
    content = f"<nav><a href='../index.html'>Dashboard</a></nav><h1>Scene episode: {html.escape(str(episode.get('trigger', 'episode')))}</h1><p class='muted'>Coverage {_float(episode.get('coverage')):.0%}. States are interpolated; this is not a physics-exact replay.</p><section><canvas id='replay'></canvas><p><button id='play'>Play / pause</button> <button data-speed='0.25'>0.25×</button> <button data-speed='0.5'>0.5×</button> <button data-speed='1'>1×</button> <button data-speed='2'>2×</button> <span id='time'></span></p><p>{toggles}</p><input id='scrubber' type='range' style='width:100%'></section><section><h2>Selected entity</h2><pre id='entity-details'>Click a visible entity in the replay.</pre></section><section><h2>Interaction timeline</h2><pre>{html.escape(json.dumps(episode.get('interactions', []), ensure_ascii=False, indent=2))}</pre></section><section><h2>Limitations</h2><p>{html.escape(' '.join(episode.get('limitations', [])))}</p></section><script id='episode-data' type='application/json'>{_json(episode)}</script><script>{REPLAY_JS}</script>"
    return _page("SFD scene episode", content, "../assets/")


def _live_ai(analytics: sqlite3.Connection) -> dict:
    requests = _fetch(analytics, "SELECT source_type,status,COUNT(*) AS count FROM llm_requests GROUP BY source_type,status"); jobs = _fetch(analytics, "SELECT job_kind,status,COUNT(*) AS count FROM llm_jobs GROUP BY job_kind,status"); counts = defaultdict(int)
    for item in requests + jobs: counts[f"{item.get('source_type', item.get('job_kind'))}_{item['status']}"] += int(item.get("count", 0))
    return {"status": "pending" if counts.get("narrative_queued", 0) or counts.get("gameplay_queued", 0) else "complete", "queue": {"queued": sum(value for key, value in counts.items() if key.endswith("_queued")), "submitted": sum(value for key, value in counts.items() if key.endswith("_submitted")), "complete": sum(value for key, value in counts.items() if key.endswith("_complete")), "moderation": counts.get("moderation_complete", 0)}, "counts": dict(counts), "narrative": None}


def _live_summary(config, analytics: sqlite3.Connection) -> dict:
    health_rows = _fetch(analytics, "SELECT component,used_bytes,max_bytes,watermark,state,dropped_count,malformed_count,gap_count FROM storage_health ORDER BY component"); storage = [{key: item.get(key) for key in ("component", "used_bytes", "max_bytes", "watermark", "state", "dropped_count", "malformed_count", "gap_count")} for item in health_rows]
    checkpoint_rows = _fetch(analytics, "SELECT last_event_id,processed_at FROM processing_checkpoints WHERE consumer_name='live_analyzer' LIMIT 1"); checkpoint = checkpoint_rows[0] if checkpoint_rows else {}
    server_rows = _fetch(analytics, "SELECT minute_start,metrics_json FROM agg_server_minute ORDER BY minute_start DESC LIMIT 240"); player_rows = _fetch(analytics, "SELECT minute_start,player_session_id,metrics_json FROM agg_player_minute ORDER BY minute_start DESC LIMIT 500"); network_rows = _fetch(analytics, "SELECT minute_start,player_session_id,ping_count,ping_sum,ping_min,ping_max FROM agg_network_minute ORDER BY minute_start DESC LIMIT 500")
    series: dict[str, dict[str, Any]] = {}
    for item in server_rows:
        bucket = series.setdefault(str(item.get("minute_start")), {"t": item.get("minute_start"), "events": 0, "damage": 0, "kills": 0, "deaths": 0, "players": 0}); metrics = _metric_json(item.get("metrics_json")); bucket["events"] += int(_float(metrics.get("events"))); bucket["damage"] += round(_float(metrics.get("damage")), 2); bucket["kills"] += int(_float(metrics.get("kills"))); bucket["deaths"] += int(_float(metrics.get("deaths")))
    active: dict[str, set[str]] = defaultdict(set); players: dict[str, dict[str, Any]] = {}
    for item in player_rows:
        timestamp = str(item.get("minute_start")); identity = str(item.get("player_session_id") or "unknown"); active[timestamp].add(identity); row = players.setdefault(identity, {"events": 0, "damage": 0, "kills": 0, "deaths": 0}); metrics = _metric_json(item.get("metrics_json")); row["events"] += int(_float(metrics.get("events"))); row["damage"] += round(_float(metrics.get("damage")), 2); row["kills"] += int(_float(metrics.get("kills"))); row["deaths"] += int(_float(metrics.get("deaths")))
    for timestamp, values in active.items():
        if timestamp in series: series[timestamp]["players"] = len(values)
    labels = {identity: f"Player {index:03d}" for index, identity in enumerate(sorted(players, key=lambda key: (players[key]["damage"], players[key]["events"]), reverse=True), 1)}; compact_players = [{"player": labels[key], "sessions": 1, "playtime_min": None, "combat": {"damage_dealt": value["damage"], "inferred_kills": value["kills"]}, "input": {"apm": None}, "network": {}} for key, value in list(players.items())[:MAX_PLAYERS]]
    net_by_player: dict[str, dict[str, float]] = {}; net_series: dict[str, dict[str, float]] = {}
    for item in network_rows:
        identity = str(item.get("player_session_id") or "unknown"); count = int(item.get("ping_count") or 0); value = net_by_player.setdefault(identity, {"samples": 0, "sum": 0, "p95": 0, "p99": 0, "spikes": 0, "seconds_above": 0}); value["samples"] += count; value["sum"] += _float(item.get("ping_sum")); value["p95"] = max(value["p95"], _float(item.get("ping_max"))); value["p99"] = max(value["p99"], _float(item.get("ping_max"))); bucket = net_series.setdefault(str(item.get("minute_start")), {"count": 0, "sum": 0, "max": 0}); bucket["count"] += count; bucket["sum"] += _float(item.get("ping_sum")); bucket["max"] = max(bucket["max"], _float(item.get("ping_max")))
    network = {"sessions": len(net_by_player), "outliers": [{"player": labels.get(identity, "Player"), "samples": int(value["samples"]), "p95": round(value["p95"], 1), "p99": round(value["p99"], 1), "spikes": int(value["spikes"]), "seconds_above": round(value["seconds_above"], 1)} for identity, value in sorted(net_by_player.items(), key=lambda pair: pair[1]["p95"], reverse=True)[:MAX_NETWORK]]}
    for timestamp, value in net_series.items():
        if timestamp in series: series[timestamp]["ping_mean"] = round(value["sum"] / value["count"], 1) if value["count"] else None; series[timestamp]["ping_max"] = round(value["max"], 1)
    episodes = [{"episode_id": item.get("source_event_id"), "trigger": item.get("trigger"), "coverage": item.get("coverage", 0), "status": item.get("status"), "created_at": item.get("created_at")} for item in _fetch(analytics, "SELECT source_event_id,trigger,coverage,status,created_at FROM episode_catalog ORDER BY episode_id DESC LIMIT ?", (MAX_EPISODES,))]; patterns = _compact_patterns(_fetch(analytics, "SELECT signature,pattern_family,state,confidence,occurrences,last_seen_at FROM pattern_candidates ORDER BY last_seen_at DESC LIMIT ?", (MAX_PATTERNS,))); ai = _live_ai(analytics); ordered_series = sorted(series.values(), key=lambda item: str(item.get("t") or "")); last = ordered_series[-1] if ordered_series else {}; lag = None
    try:
        latest = analytics.execute("SELECT MAX(updated_at) FROM agg_server_minute").fetchone()[0]
        if latest: lag = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(str(latest).replace("Z", "+00:00"))).total_seconds())
    except (sqlite3.Error, TypeError, ValueError): pass
    now = datetime.now(timezone.utc).isoformat()
    return {"schema_version": 2, "kind": "live", "generated_at": now, "freshness": {"generated_at": now, "last_processed_event_id": checkpoint.get("last_event_id", 0), "processed_at": checkpoint.get("processed_at"), "lag_seconds": round(lag, 1) if lag is not None else None}, "status": {"deterministic": "complete", "llm": ai["status"]}, "kpi": {"players": last.get("players", 0), "humans": None, "bots": None, "sessions": None, "rounds": None, "events": sum(int(_float(item.get("events"))) for item in ordered_series), "damage": round(sum(_float(item.get("damage")) for item in ordered_series), 1), "inferred_kills": sum(int(_float(item.get("kills"))) for item in ordered_series), "ping_peak": max([_float(item.get("ping_max")) for item in ordered_series if item.get("ping_max") is not None], default=None), "scene_interactions": None, "llm_queued": ai["queue"]["queued"]}, "series": ordered_series, "players": compact_players, "maps": [], "rounds": [], "combat": {"events_by_type": {}, "damage": 0, "inferred_kills": 0, "weapons": [], "pairs": []}, "network": network, "environment": {"available": False, "interaction_types": {}, "categories": {}, "heatmap": [], "episodes": episodes}, "patterns": patterns, "ai": ai, "quality": {}, "storage": storage}


def build_dashboard(report_directory: str, report_date: str | None = None, telemetry_database: str | None = None, episode_id: int | None = None) -> Path:
    report_root = Path(report_directory)
    all_reports = _select_report_artifacts(report_root)
    dashboard = report_root.parent / "dashboard"
    assets, days, players_dir, episodes_dir = dashboard / "assets", dashboard / "days", dashboard / "players", dashboard / "episodes"
    for directory in (assets, days, players_dir, episodes_dir):
        directory.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(assets / "dashboard.css", CSS)
    _atomic_write_text(assets / "dashboard.js", JS)
    reports = [path for path in all_reports if report_date is None or _artifact_family(path) == report_date]
    if telemetry_database and episode_id is not None:
        from .scene import load_episode
        telemetry = sqlite3.connect(Path(telemetry_database).resolve().as_uri() + "?mode=ro", uri=True); telemetry.row_factory = sqlite3.Row
        try:
            episode = load_episode(telemetry, int(episode_id))
            if episode:
                _atomic_write_text(episodes_dir / f"{episode_id}.html", _episode_page(episode))
        finally: telemetry.close()
    day_outputs = []
    for path in reports:
        report_date_key = _artifact_family(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        summary = _compact_report(payload)
        summary_path = _atomic_json(days / f"{report_date_key}.json", summary)
        day_path = _atomic_write_text(days / f"{report_date_key}.html", _dashboard_shell(summary, f"../days/{summary_path.name}", "day", f"SFD telemetry — {report_date_key}", "../assets/"))
        day_outputs.append({"date": report_date_key, "href": f"days/{day_path.name}", "json": f"days/{summary_path.name}", "bytes": path.stat().st_size, "artifact": path.name})
        for player in summary["players"]:
            _atomic_write_text(players_dir / f"{_slug(player['player'])}.html", _dashboard_shell({**summary, "players": [player]}, f"../days/{summary_path.name}", "day", f"SFD — {player['player']}", "../assets/"))
    index_summary = {"schema_version": 2, "kind": "index", "generated_at": datetime.now(timezone.utc).isoformat(), "days": day_outputs}
    _atomic_json(dashboard / "index.json", index_summary)
    links = "".join(f"<li><a href='{item['href']}'>{html.escape(item['date'])}</a> <span class='muted small'>({item['bytes']:,} bytes source)</span></li>" for item in day_outputs)
    index = f"<h1>SFD Telemetry Dashboard</h1><p class='muted'>Processed summaries. Large telemetry and raw AI payloads stay outside the browser artifact.</p><p><a href='live.html'>Open live dashboard</a></p><section><h2>Days</h2><ul class='day-links'>{links or '<li class=empty>No reports yet</li>'}</ul></section>"
    from .storage import prune_dashboard_cache
    prune_dashboard_cache(episodes_dir, 100 * 1024 * 1024, 50)
    return _atomic_write_text(dashboard / "index.html", _page("SFD Telemetry Dashboard", index))


def build_live_dashboard(config, analytics: sqlite3.Connection) -> Path:
    dashboard = Path(config.report_directory).parent / "dashboard"
    assets = dashboard / "assets"
    dashboard.mkdir(parents=True, exist_ok=True)
    assets.mkdir(parents=True, exist_ok=True)
    summary = _live_summary(config, analytics)
    json_path = _atomic_json(dashboard / "live.json", summary)
    _atomic_write_text(assets / "dashboard.css", CSS)
    _atomic_write_text(assets / "dashboard.js", JS)
    return _atomic_write_text(dashboard / "live.html", _dashboard_shell(summary, json_path.name, "live", "SFD live telemetry", "assets/"))


# Dashboard v3 keeps this public module path stable for the live process and
# callers that import analyzer.dashboard directly.
from .dashboard_v3 import (  # noqa: E402,F401
    CSS,
    JS,
    _compact_environment,
    _compact_network,
    _compact_player,
    _compact_patterns,
    _compact_report,
    _live_summary,
    _population,
    _quality,
    build_dashboard,
    build_live_dashboard,
)
