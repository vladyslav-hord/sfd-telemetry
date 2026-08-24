"""Compact dashboard contract and renderer.
This module deliberately keeps presentation concerns out of metrics/live/schema.
It accepts the explicit population contract when present and has a legacy
fallback that counts unique rows in ``players`` rather than sessions.
"""
from __future__ import annotations

import html
import json
import math
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
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
LIVE_WINDOW_MINUTES = 180

CSS = """*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#09101d;color:#e8edf7;font:15px/1.45 system-ui,sans-serif}main{max-width:1480px;margin:auto;padding:20px 24px 48px}a{color:#83d4ff}h1,h2,h3{margin:0 0 10px}h2{font-size:21px}h3{font-size:15px}.muted{color:#9ba8bc}.small{font-size:12px}.hero{position:sticky;top:0;z-index:5;display:flex;justify-content:space-between;gap:18px;align-items:flex-start;margin:0 -24px 14px;padding:16px 24px;background:#0d1628ee;backdrop-filter:blur(10px);border-bottom:1px solid #293657}.hero p{margin:4px 0}.toolbar,.summary-line{display:flex;gap:9px;flex-wrap:wrap;align-items:center}.mode{border:1px solid #405276;border-radius:999px;padding:4px 9px;color:#c7d4e8;font-size:12px}.section-nav{position:sticky;top:97px;z-index:4;display:flex;gap:6px;overflow:auto;padding:6px 0;margin-bottom:12px;background:#09101df5}.section-nav a{white-space:nowrap;border:1px solid #293657;border-radius:999px;padding:5px 9px;font-size:12px;text-decoration:none}.card,section{background:#131d32;border:1px solid #293657;border-radius:10px;padding:15px;margin:13px 0}.card{margin:0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.columns{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(320px,1fr);gap:13px}.metric{font-size:23px;font-weight:700;letter-spacing:-.02em}.kpi-note{margin-top:3px}.good{color:#8be9b0}.warn{color:#ffd48a}.bad{color:#ff8e9e}.toolbar input,.toolbar select,button{background:#0b1324;color:#e8edf7;border:1px solid #405276;border-radius:6px;padding:7px 9px}button{cursor:pointer}.table-wrap{overflow:auto;max-height:650px;border:1px solid #293657;border-radius:7px}.table-tools{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin:8px 0}.table-tools input{min-width:180px;background:#0b1324;color:#e8edf7;border:1px solid #405276;border-radius:6px;padding:6px 8px}table{width:100%;border-collapse:collapse;min-width:580px}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #293657;vertical-align:top}th{position:sticky;top:0;background:#131d32;color:#aebbd0;font-size:11px;text-transform:uppercase;letter-spacing:.04em;cursor:pointer;white-space:nowrap}tbody tr:hover{background:#1b2742}.empty{color:#9ba8bc;padding:12px}.status{font-weight:600}.chip{display:inline-flex;gap:5px;border:1px solid #405276;border-radius:999px;padding:4px 8px;color:#c7d4e8;margin:2px}.chips{display:flex;gap:4px;flex-wrap:wrap}.progress{height:8px;background:#26324e;border-radius:999px;overflow:hidden}.progress>span{display:block;height:100%;background:#83d4ff}.definition{color:#9ba8bc;font-size:12px;margin:-3px 0 11px}.incident-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:9px}.incident{border-left:3px solid #405276;padding:8px 10px;background:#10192b;border-radius:5px}.incident.warn{border-color:#ffd48a}.incident.bad{border-color:#ff8e9e}.incident.good{border-color:#8be9b0}canvas{display:block;width:100%;height:280px;background:#0b1324;border-radius:8px}#heatmap-canvas{height:360px}.legend{margin-top:7px}.count-note{color:#9ba8bc;font-size:12px;margin:5px 0}.quality-row{display:grid;grid-template-columns:minmax(150px,1fr) minmax(95px,.5fr) minmax(95px,.5fr) minmax(130px,1fr);gap:10px;padding:8px 0;border-bottom:1px solid #293657}@media(max-width:700px){main{padding:12px 12px 36px}.hero{display:block;margin:0 -12px 10px;padding:12px}.section-nav{top:108px}.columns{grid-template-columns:1fr}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.metric{font-size:20px}canvas{height:220px}#heatmap-canvas{height:260px}.quality-row{grid-template-columns:1fr 1fr}.table-wrap{max-height:520px}table{min-width:560px}}"""

JS = r"""(()=>{const app=document.getElementById('dashboard-app');if(!app)return;const fallback=window.__DASHBOARD_DATA__||{};let current=fallback;const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const number=v=>typeof v==='number'&&Number.isFinite(v)?v.toLocaleString(undefined,{maximumFractionDigits:1}):'—';const percent=v=>typeof v==='number'&&Number.isFinite(v)?`${(v*100).toLocaleString(undefined,{maximumFractionDigits:1})}%`:'—';const ms=v=>typeof v==='number'&&Number.isFinite(v)?`${number(v)} ms`:'—';const set=(id,v)=>{const n=document.getElementById(id);if(n)n.innerHTML=v};const arr=v=>Array.isArray(v)?v:[];const states={};function table(id,columns,data,total){const n=document.getElementById(id);if(!n)return;const rows=arr(data),s=states[id]||{q:'',i:-1,d:1};states[id]=s;const filtered=rows.filter(x=>!s.q||JSON.stringify(x).toLowerCase().includes(s.q.toLowerCase()));if(s.i>=0)filtered.sort((a,b)=>{const av=a[columns[s.i].key],bv=b[columns[s.i].key],c=typeof av==='number'&&typeof bv==='number'?av-bv:String(av??'').localeCompare(String(bv??''));return c*s.d});n.innerHTML=`<div class="table-tools"><input type="search" aria-label="Filter table" placeholder="Filter table" value="${esc(s.q)}"><span class="count-note">showing ${filtered.length} of ${number(total??rows.length)}</span></div><div class="table-wrap"><table data-sortable="true"><thead><tr>${columns.map((c,i)=>`<th data-sort="${i}">${esc(c.label)}</th>`).join('')}</tr></thead><tbody>${filtered.length?filtered.map(x=>`<tr>${columns.map(c=>`<td>${c.render?c.render(x):esc(x[c.key])}</td>`).join('')}</tr>`).join(''):`<tr><td colspan="${columns.length}" class="empty">No data</td></tr>`}</tbody></table></div>`;n.querySelector('input')?.addEventListener('input',e=>{s.q=e.target.value;table(id,columns,rows,total)});n.querySelectorAll('th[data-sort]').forEach(th=>th.addEventListener('click',()=>{const i=+th.dataset.sort;s.d=s.i===i?-s.d:1;s.i=i;table(id,columns,rows,total)}))}function drawLine(data){const c=document.getElementById('timeline-chart'),a=arr(data.timeline||data.series);if(!c||!a.length)return;const d=devicePixelRatio||1,w=c.clientWidth*d,h=c.clientHeight*d;c.width=w;c.height=h;const x=c.getContext('2d');x.clearRect(0,0,w,h);x.strokeStyle='#293657';for(let i=1;i<5;i++){x.beginPath();x.moveTo(0,i*h/5);x.lineTo(w,i*h/5);x.stroke()}const k=data.kind==='live'?'events':'rounds',v=a.map(q=>Number(q[k]||0)),m=Math.max(...v,1);x.strokeStyle='#83d4ff';x.lineWidth=2*d;x.beginPath();v.forEach((q,i)=>{const px=i*w/Math.max(v.length-1,1),py=h-(q/m*h*.82+h*.09);i?x.lineTo(px,py):x.moveTo(px,py)});x.stroke();set('timeline-label',`${esc(a[0]?.t||'')} → ${esc(a.at(-1)?.t||'')} · ${data.kind==='live'?'events per minute':'rounds per hour'} · peak ${number(m)}`)}function heatmap(data){const c=document.getElementById('heatmap-canvas'),a=arr(data.environment?.heatmap);if(!c||!a.length)return;const d=devicePixelRatio||1,w=c.clientWidth*d,h=c.clientHeight*d;c.width=w;c.height=h;const x=c.getContext('2d');x.fillStyle='#0b1324';x.fillRect(0,0,w,h);const xs=a.map(q=>+q.x||0),ys=a.map(q=>+q.y||0),lx=Math.min(...xs),hx=Math.max(...xs),ly=Math.min(...ys),hy=Math.max(...ys),m=Math.max(...a.map(q=>+q.count||0),1);a.forEach(q=>{const px=(q.x-lx)/(hx-lx||1)*w,py=h-(q.y-ly)/(hy-ly||1)*h,r=4+Math.sqrt(+q.count||0)/Math.sqrt(m)*18;x.fillStyle=`rgba(131,212,255,${.15+.8*(+q.count||0)/m})`;x.beginPath();x.arc(px,py,r*d,0,Math.PI*2);x.fill()});set('heatmap-legend',`interaction count · x ${number(lx)}…${number(hx)} · y ${number(ly)}…${number(hy)} · darker = higher count`)}function render(data){current=data||{};const f=current.freshness||{},p=current.population||{},k=current.kpi||{};set('title',current.kind==='live'?'SFD Live Operations':`SFD Day Research — ${current.report_date||'unknown'}`);set('mode',current.kind==='live'?'LIVE OPERATIONS':'DAY RESEARCH');set('freshness',`<span class="status">${esc(current.status?.deterministic||'unknown')}</span> · LLM ${esc(current.ai?.status||current.status?.llm||'unknown')} · as-of ${esc(f.as_of||f.cutoff||'—')} · generated ${esc(f.generated_at||current.generated_at||'—')} · timezone ${esc(f.timezone||'UTC')}${current.kind==='live'?` · backlog ${number(f.backlog_lag_seconds??k.backlog_lag_seconds)} · source idle ${number(f.source_idle_seconds)}s`:''}`);set('refresh-state',current.kind==='live'?`Polling every 60 seconds · window ${esc(f.window_start||'—')} → ${esc(f.window_end||f.as_of||'—')}`:'Static summary · cutoff is the data as-of time');set('definitions',current.kind==='live'?'Live: active players are distinct sessions in the latest minute; backlog is source events not yet processed; source idle is neutral freshness. p95/p99 use the stored 10 ms histogram.':'Day: players are unique identity entities; human sessions and bot sessions are separate counts.');const items=current.kind==='live'?[['Active players',p.active_now??k.active_now,'distinct sessions in latest minute'],['Unique players',p.unique_players??k.unique_players,'mapped identities; partial if unknown'],['Sessions in window',p.sessions_window??'—','last 3h'],['Unknown sessions',p.unknown_sessions??'—','not linked to identity'],['Unknown bot sessions',p.unknown_bot_sessions??'—','bot class known; identity missing'],['Events',number(k.events),'window total'],['Events/min',number(k.events_per_min),'window average'],['Ping p95',ms(k.p95_ping),'histogram estimate'],['Ping max',ms(k.ping_max),'observed'],['Backlog lag',k.backlog_lag_seconds==null?'—':`${number(k.backlog_lag_seconds)} s`,'source events not processed'],['LLM queued',number(k.llm_queued),'current queue']]:[['Unique players',k.players??'—','distinct identities'],['Observed entities',p.player_entities??'—','identified identities + null sessions'],['Human players',p.human_players??k.humans,'unique identities'],['Human sessions',p.human_sessions??'—','session count'],['Bot sessions',p.bot_sessions??'—','session count'],['Rounds',number(k.rounds),'observed'],['Events',number(k.events),'all telemetry'],['Damage',number(k.damage),'damage points'],['Max player p95',ms(k.max_player_p95),'not population p95'],['Scene interactions',number(k.scene_interactions),'derived events'],['LLM status',current.ai?.status||current.status?.llm||'unknown','see AI section']];set('kpis',items.map(x=>`<div class="card"><div class="muted">${esc(x[0])}</div><div class="metric">${esc(typeof x[1]==='string'?x[1]:number(x[1]))}</div><div class="kpi-note small muted">${esc(x[2])}</div></div>`).join(''));const inc=arr(current.incidents);set('incident-content',inc.length?`<div class="incident-grid">${inc.map(x=>`<div class="incident ${esc(x.severity||'')}"><b>${esc(x.title)}</b><div class="small muted">${esc(x.detail)}</div></div>`).join('')}</div>`:'<div class="incident good">No active incidents in the available summary.</div>');drawLine(current);heatmap(current);const playerCols=[{key:'player',label:'Player'},{key:'bot',label:'Type',render:x=>x.bot===true?'bot':x.bot===false?'human':'unknown'},{key:'sessions',label:'Sessions',render:x=>number(x.sessions)},{key:'playtime_min',label:'Playtime',render:x=>x.playtime_min==null?'—':`${number(x.playtime_min)} min`},{key:'kills',label:'Kills',render:x=>number(x.combat?.inferred_kills)},{key:'damage',label:'Damage',render:x=>number(x.combat?.damage_dealt)},{key:'apm',label:'APM',render:x=>number(x.input?.apm)},{key:'p95',label:'Ping p95',render:x=>ms(x.network?.p95)}];table('players-table',playerCols,current.players,current.counts?.players?.total);table('maps-table',[{key:'map',label:'Map'},{key:'rounds',label:'Rounds',render:x=>number(x.rounds)},{key:'duration_min',label:'Total duration',render:x=>`${number(x.duration_min)} min`}],current.maps,current.counts?.maps?.total);table('rounds-table',[{key:'round',label:'Round',render:x=>number(x.round)},{key:'map',label:'Map'},{key:'duration_s',label:'Duration',render:x=>`${number(x.duration_s)} s`},{key:'players',label:'Players',render:x=>number(x.players)},{key:'quality',label:'Quality'}],current.rounds,current.counts?.rounds?.total);table('weapons-table',[{key:'weapon',label:'Weapon'},{key:'events',label:'Events',render:x=>number(x.events)},{key:'damage',label:'Damage points',render:x=>number(x.damage)}],current.combat?.weapons,current.counts?.weapons?.total);table('pairs-table',[{key:'subject',label:'Interaction'},{key:'events',label:'Events',render:x=>number(x.events)},{key:'damage',label:'Damage points',render:x=>number(x.damage)}],current.combat?.pairs,current.counts?.pairs?.total);table('network-table',[{key:'player',label:'Session label'},{key:'mapping',label:'Mapping'},{key:'samples',label:'Samples',render:x=>number(x.samples)},{key:'p95',label:'p95 estimate',render:x=>ms(x.p95)},{key:'p99',label:'p99 estimate',render:x=>ms(x.p99)},{key:'max',label:'Max observed',render:x=>ms(x.max)},{key:'spikes',label:'Spikes',render:x=>number(x.spikes)}],current.network?.outliers,current.counts?.network?.total);table('patterns-table',[{key:'family',label:'Family'},{key:'state',label:'State'},{key:'confidence',label:'Confidence',render:x=>percent(x.confidence)},{key:'occurrences',label:'Occurrences',render:x=>number(x.occurrences)},{key:'evidence',label:'Evidence',render:x=>number(x.evidence)}],current.patterns,current.counts?.patterns?.total);table('episodes-table',[{key:'episode',label:'Episode',render:x=>number(x.episode)},{key:'trigger',label:'Trigger'},{key:'timestamp',label:'As-of'},{key:'coverage',label:'Coverage',render:x=>percent(x.coverage)},{key:'entities',label:'Entities',render:x=>number(x.entities)}],current.environment?.episodes,current.counts?.episodes?.total);table('scene-objects',[{key:'name',label:'Object'},{key:'category',label:'Category'},{key:'interactions',label:'Interactions',render:x=>number(x.interactions)},{key:'share',label:'Share',render:x=>percent(x.share)}],current.environment?.object_usage,current.counts?.objects?.total);set('event-types',Object.entries(current.combat?.events_by_type||{}).sort((a,b)=>b[1]-a[1]).slice(0,20).map(([x,v])=>`<span class="chip">${esc(x)}: ${number(v)}</span>`).join('')||'<span class="empty">No event volume</span>');set('scene-types',Object.entries(current.environment?.interaction_types||{}).sort((a,b)=>b[1]-a[1]).map(([x,v])=>`<span class="chip">${esc(x)}: ${number(v)}</span>`).join('')||'<span class="empty">No scene interactions</span>');set('scene-motifs',arr(current.environment?.motifs).map(x=>`<span class="chip">${esc(x.motif||x.family)}: ${number(x.occurrences)}</span>`).join('')||'<span class="empty">No motifs</span>');set('barrel-candidates',arr(current.environment?.barrel_boost_candidates).slice(0,20).map(x=>`<tr><td>${number(x.rank)}</td><td>${esc(x.object)}</td><td>${percent(x.confidence)}</td><td>${number(x.player_speed_gain)}</td><td>${esc(x.advantage||x.pattern||'—')}</td></tr>`).join('')||'<tr><td colspan="5" class="empty">No high-confidence candidates</td></tr>');const n=current.ai?.narrative;set('ai-content',n?.headline?`<h3>${esc(n.headline)}</h3><div class="columns">${['server_health','player_experience','map_findings','network_findings','player_highlights','pattern_findings','chat_findings','possible_factors','limitations'].map(key=>`<div><h3>${esc(key.replaceAll('_',' '))}</h3><ul>${arr(n[key]).map(x=>`<li>${esc(x)}</li>`).join('')||'<li class="empty">No findings</li>'}</ul></div>`).join('')}</div>`:`<p class="empty">No narrative in this snapshot. Queue ${number(current.ai?.queue?.queued)}, submitted ${number(current.ai?.queue?.submitted)}, complete ${number(current.ai?.queue?.complete)}, failed ${number(current.ai?.queue?.failed)}. Token/cost accounting: ${esc(current.ai?.usage?.cost_status||'unknown')}.</p>`);const u=current.ai?.usage||{};set('ai-usage',`<div class="chips"><span class="chip">Requests: ${number(u.requests)}</span><span class="chip">Input tokens: ${number(u.input_tokens)}</span><span class="chip">Output tokens: ${number(u.output_tokens)}</span><span class="chip">Cached tokens: ${number(u.cached_tokens)}</span><span class="chip">Estimated cost: ${esc(u.estimated_cost??'unknown')}</span></div>`);const q=arr(current.quality?.metrics);set('quality-content',q.length?q.map(x=>`<div class="quality-row"><div><b>${esc(x.label)}</b><div class="definition">${esc(x.definition)}</div></div><div><b class="quality-value">${esc(x.display)}</b></div><div>${esc(x.severity)}</div><div class="definition">${esc(x.denominator)}</div></div>`).join(''):'<p class="empty">No quality telemetry in this view.</p>');const st=arr(current.storage);set('storage-content',st.map(x=>`<div class="card"><div>${esc(x.component)}</div><div class="metric">${esc(x.state)}</div><div class="small">${number(x.used_bytes)} / ${number(x.max_bytes)} bytes</div><div class="progress"><span style="width:${Math.min(100,Math.max(0,Number(x.watermark||0)*100))}%"></span></div></div>`).join('')||'<p class="empty">No storage telemetry in this view.</p>');set('environment-content',`<div class="summary-line"><span class="chip">Damage: ${number(current.environment?.environmental_damage)}</span><span class="chip">Samples: ${number(current.environment?.scene_samples)}</span>${Object.entries(current.environment?.categories||{}).map(([x,v])=>`<span class="chip">${esc(x)}: ${number(v)}</span>`).join('')}</div>`)}render(current);async function poll(){const url=app.dataset.dataUrl;if(!url)return;try{const r=await fetch(`${url}?v=${Date.now()}`,{cache:'no-store'});if(!r.ok)throw new Error(r.status);current=await r.json();render(current);set('refresh-state','Live snapshot loaded; next poll in 60 seconds.')}catch(_){set('refresh-state','Using embedded snapshot; local HTTP server is required for polling.')}}if(app.dataset.kind==='live'){poll();setInterval(poll,60000)}window.addEventListener('resize',()=>{drawLine(current);heatmap(current)})})();"""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _page(title: str, content: str, asset_prefix: str = "assets/") -> str:
    return f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><link rel='stylesheet' href='{asset_prefix}dashboard.css'></head><body><main>{content}</main></body></html>"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-") or "player"


def _float(value: object, default: float = 0.0) -> float:
    try:
        n = float(value); return n if math.isfinite(n) else default
    except (TypeError, ValueError): return default


def _optional_float(value: object) -> float | None:
    try:
        n = float(value); return n if math.isfinite(n) else None
    except (TypeError, ValueError): return None


def _number(value: object) -> str:
    return f"{value:,.1f}" if isinstance(value, float) and not value.is_integer() else f"{value:,.0f}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "—"


def _metric_json(value: object) -> dict[str, Any]:
    try: parsed = json.loads(value or "{}") if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError): parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _fetch(conn: sqlite3.Connection, query: str, args: tuple = ()) -> list[dict[str, Any]]:
    try: return [dict(row) for row in conn.execute(query, args).fetchall()]
    except sqlite3.Error: return []


def _atomic_json(path: Path, value: object) -> Path:
    return _atomic_write_text(path, _json(value) + "\n")


def _artifact_family(path: Path) -> str:
    return path.stem.split(".fallback-", 1)[0]


def _select_report_artifacts(directory: Path) -> list[Path]:
    families: dict[str, list[Path]] = defaultdict(list)
    for path in directory.glob("*.json"):
        if path.is_file(): families[_artifact_family(path)].append(path)
    return sorted((max(paths, key=lambda p: p.stat().st_mtime_ns) for paths in families.values()), key=_artifact_family)


def _population(server: dict, players: list[dict]) -> dict[str, int | None]:
    humans = sum(not item.get("is_bot") for item in players); bots = sum(bool(item.get("is_bot")) for item in players)
    def get(key: str, fallback: int | None) -> int | None: return int(server[key]) if key in server and server[key] is not None else fallback
    human_sessions = get("human_sessions", server.get("humans")); bot_sessions = get("bot_sessions", server.get("bots")); sessions = get("sessions", (human_sessions or 0) + (bot_sessions or 0))
    return {"player_entities": get("player_entities", len(players)), "human_players": get("human_players", humans), "bot_players": get("bot_players", bots), "human_sessions": human_sessions, "bot_sessions": bot_sessions, "sessions": sessions}


def _player_labels(players: list[dict]) -> dict[str, str]:
    ids = sorted(str(x.get("player_identity_id")) for x in players if x.get("player_identity_id")); return {key: f"Player {i:03d}" for i, key in enumerate(ids, 1)}


def _compact_player(item: dict, label: str) -> dict:
    combat=item.get("combat") or {}; movement=item.get("movement") or {}; inp=item.get("input") or {}; profile=item.get("skill_profile") or {}; stats=item.get("statistics") or {}; net=item.get("network") if isinstance(item.get("network"),dict) else {}
    apm=round(_float(inp.get("actions_per_minute")),1) if inp.get("actions_per_minute") is not None else None
    return {"player":label,"bot":bool(item.get("is_bot")),"sessions":item.get("sessions",0),"visits":item.get("visits",0),"playtime_min":round(_float(item.get("playtime_seconds"))/60,1) if item.get("playtime_seconds") is not None else None,"combat":{"events":combat.get("combat_events",0),"damage_dealt":round(_float(combat.get("damage_dealt")),1),"damage_received":round(_float(combat.get("damage_received")),1),"inferred_kills":combat.get("inferred_kill_credit",0),"inferred_assists":combat.get("inferred_assist_credit",0)},"movement":{"distance":round(_float(movement.get("distance")),1),"active_distance":round(_float(movement.get("active_distance")),1),"coverage":movement.get("coverage",0)},"input":{"events":inp.get("events",0),"apm":apm},"skill":{"confidence":profile.get("confidence",0)},"statistics":{k:stats[k] for k in ("TotalDamageTaken","TotalShotsFired","TotalProjectilesHitBy","TotalMeleeAttackHits","TotalJumps","TotalRolls","TotalDives") if k in stats},"network":{"p95":_optional_float(net.get("p95")),"p99":_optional_float(net.get("p99")),"max":_optional_float(net.get("max")),"samples":net.get("samples")}}


def _compact_network(rows: list[dict], labels: dict[str,str]) -> dict:
    ordered=sorted(rows,key=lambda x:_float(x.get("p95"),-1),reverse=True)
    resolved_total=sum(1 for item in rows if (item.get("player_identity_id") or item.get("player_id")) and str(item.get("player_identity_id") or item.get("player_id")) in labels)
    out=[]
    for i,item in enumerate(ordered[:MAX_NETWORK],1):
        identity=item.get("player_identity_id") or item.get("player_id"); label=labels.get(str(identity)) if identity else None
        if not label: label=f"Network session {i:03d}"
        spikes=item.get("spikes",{}); above=item.get("seconds_above",{})
        out.append({"player":label,"mapping":"resolved" if identity and str(identity) in labels else "session only","samples":item.get("samples",0),"p95":_optional_float(item.get("p95")),"p99":_optional_float(item.get("p99")),"max":_optional_float(item.get("max")),"spikes":spikes.get("50",0) if isinstance(spikes,dict) else 0,"seconds_above":above.get("100",0) if isinstance(above,dict) else 0})
    vals=[_optional_float(x.get("p95")) for x in rows]; vals=[x for x in vals if x is not None]
    return {"sessions_total":len(rows),"resolved_sessions":resolved_total,"unresolved_sessions":len(rows)-resolved_total,"max_player_p95":max(vals) if vals else None,"outliers":out,"p95_method":"report percentile"}


def _compact_environment(env: dict) -> dict:
    episodes=[{"episode":i,"trigger":x.get("trigger"),"coverage":x.get("coverage",0),"timestamp":x.get("utc_timestamp") or x.get("created_at"),"entities":x.get("entity_count",0)} for i,x in enumerate((env.get("episodes") or [])[:MAX_EPISODES],1)]
    objects=env.get("object_usage") or []; total=sum(_float(x.get("interactions")) for x in objects)
    usage=[{"name":x.get("name") or "Unknown","category":x.get("category") or "other","interactions":x.get("interactions",0),"share":_float(x.get("interactions"))/total if total else 0} for x in objects[:MAX_OBJECTS]]
    candidates=[{"rank":i,"confidence":_optional_float(x.get("confidence")),"object":x.get("object_name") or "unknown","pattern":x.get("pattern_type") or x.get("subtype"),"advantage":x.get("observed_advantage"),"player_speed_gain":round(_float(x.get("player_speed_gain")),2)} for i,x in enumerate((env.get("barrel_boost_candidates") or [])[:MAX_OBJECTS],1)]
    heatmap=[{"x":_float(item.get("x")),"y":_float(item.get("y")),"count":int(_float(item.get("count")))} for item in (env.get("interaction_heatmap") or [])]
    motifs=[{"motif":str(item.get("motif") or "unknown"),"occurrences":int(_float(item.get("occurrences")))} for item in (env.get("motifs") or [])[:MAX_MOTIFS] if isinstance(item,dict)]
    return {"available":bool(env.get("available")),"environmental_damage":env.get("environmental_damage",0),"scene_samples":env.get("scene_samples",0),"interaction_types":dict(env.get("interactions_by_type",{})),"categories":dict(env.get("object_categories",{})),"heatmap":sorted(heatmap,key=lambda x:x["count"],reverse=True)[:MAX_HEATMAP],"object_usage":usage,"motifs":motifs,"barrel_boost_candidates":candidates,"episodes":episodes}


def _compact_patterns(items: list[dict]) -> list[dict]:
    grouped={}
    for item in items:
        key=(str(item.get("trigger") or item.get("pattern_family") or "pattern"),str(item.get("state") or "candidate"))
        row=grouped.setdefault(key,{"family":key[0],"state":key[1],"confidence":None,"occurrences":0,"evidence":0})
        confidence=_optional_float(item.get("confidence"))
        if confidence is not None: row["confidence"]=max(row["confidence"] or 0.0,confidence)
        row["occurrences"]+=int(_float(item.get("occurrences"),1))
        row["evidence"]+=1
    return sorted(grouped.values(),key=lambda x:(x["state"]!="confirmed",-x["occurrences"],x["family"]))[:MAX_PATTERNS]


def _quality(raw: dict) -> dict:
    defs={"source_coverage":("Source coverage","Observed events / observed + missing events"),"stable_identity_coverage":("Stable identity coverage","Sessions with a stable player identity"),"combat_identity_coverage":("Combat identity coverage","Combat events with resolved identity"),"round_completion_coverage":("Round completion coverage","Rounds with a completed result")}; metrics=[]
    for key,value in raw.items():
        label,definition=defs.get(key,(key.replace("_"," ").title(),"Telemetry quality metric")); ratio=key in defs and isinstance(value,(int,float)); warn=(ratio and value<.95) or (key in {"sequence_gaps","sequence_missing_events","incomplete_player_sessions"} and _float(value)>0); metrics.append({"key":key,"label":label,"value":value,"display":f"{value*100:.1f}%" if ratio else _number(value) if isinstance(value,(int,float)) else str(value),"severity":"warn" if warn else "good","definition":definition,"denominator":definition})
    return {"metrics":metrics,"event_count":raw.get("event_count",0)}


def _narrative(value: object) -> dict|None:
    if not isinstance(value,dict) or not isinstance(value.get("headline"),str): return None
    keys=("headline","server_health","player_experience","map_findings","network_findings","player_highlights","pattern_findings","chat_findings","possible_factors","limitations"); return {k:value.get(k,[]) if k!="headline" else value[k] for k in keys}


def _attach_player_network(summary: dict, payload: dict, server: dict) -> dict:
    """Join already-public identity labels to ping metrics without retaining IDs."""
    sessions=((payload.get("network") or {}).get("sessions") or []) if isinstance(payload.get("network"),dict) else []
    sessions += [item for item in (server.get("network_players") or []) if isinstance(item,dict)]
    labels=_player_labels(payload.get("players") or [])
    metrics={}
    for item in sessions:
        label=labels.get(str(item.get("player_identity_id")))
        if label:
            metrics[label]={"p95":_optional_float(item.get("p95")),"p99":_optional_float(item.get("p99")),"max":_optional_float(item.get("max")),"samples":item.get("samples")}
    for player in summary.get("players",[]):
        if player.get("player") in metrics: player["network"]=metrics[player["player"]]
    return summary


def _compact_report(payload: dict) -> dict:
    players=payload.get("players") if isinstance(payload.get("players"),list) else []; labels=_player_labels(players); server=payload.get("server") if isinstance(payload.get("server"),dict) else {}; population=_population(server,players); qraw=payload.get("data_quality") if isinstance(payload.get("data_quality"),dict) else server.get("data_quality",{}); quality=_quality(qraw); combat=server.get("combat") if isinstance(server.get("combat"),dict) else {}; env=_compact_environment(payload.get("environment") if isinstance(payload.get("environment"),dict) else server.get("environment",{})); network=_compact_network((payload.get("network") or {}).get("sessions",[]),labels) if isinstance(payload.get("network"),dict) else {"sessions_total":0,"resolved_sessions":0,"unresolved_sessions":0,"outliers":[]}; player_rows=[_compact_player(x,labels.get(str(x.get("player_identity_id")),f"Player {i:03d}")) for i,x in enumerate(sorted(players,key=lambda x:_float(x.get("playtime_seconds")),reverse=True)[:MAX_PLAYERS],1)]; rounds_raw=payload.get("rounds") or []; rounds=[{"round":i,"map":x.get("map_name") or "Unknown","duration_s":round(_float(x.get("duration_seconds")),1),"players":x.get("player_count",0),"quality":x.get("result_quality") or "unknown"} for i,x in enumerate(rounds_raw[:MAX_ROUNDS],1)]; maps=[{"map":x.get("map_name") or "Unknown","rounds":x.get("rounds",0),"duration_min":round(_float(x.get("duration_seconds"))/60,1)} for x in (payload.get("maps") or [])[:MAX_OBJECTS]]; weapons_raw=payload.get("weapons") or []; weapons=[{"weapon":x.get("weapon") or "Unknown","events":x.get("events",0),"damage":round(_float(x.get("damage")),1)} for x in weapons_raw[:MAX_OBJECTS]]; interactions=sorted(payload.get("interactions") or [],key=lambda x:_float(x.get("damage")),reverse=True); pairs=[{"rank":i,"subject":f"Interaction {i:02d} (participants unresolved)","events":x.get("events",0),"damage":round(_float(x.get("damage")),1)} for i,x in enumerate(interactions[:MAX_OBJECTS],1)]; timeline={}
    for x in rounds_raw:
        stamp=str(x.get("started_at") or "unknown")[:13]; row=timeline.setdefault(stamp,{"t":stamp,"rounds":0,"players":0,"duration_s":0}); row["rounds"]+=1; row["players"]+=x.get("player_count",0) or 0; row["duration_s"]+=_float(x.get("duration_seconds"))
    for x in timeline.values(): x["duration_s"]=round(x["duration_s"],1)
    patterns=_compact_patterns(payload.get("patterns") or []); ai={"status":(payload.get("status") or {}).get("llm","unknown"),"narrative":_narrative(payload.get("narrative")),"queue":{"queued":None,"submitted":None,"complete":None,"failed":None},"usage":{"requests":None,"input_tokens":None,"output_tokens":None,"cached_tokens":None,"estimated_cost":None,"cost_status":"unknown"}}; counts={"players":{"shown":len(player_rows),"total":len(players)},"maps":{"shown":len(maps),"total":len(payload.get("maps") or [])},"rounds":{"shown":len(rounds),"total":len(rounds_raw)},"weapons":{"shown":len(weapons),"total":len(weapons_raw)},"pairs":{"shown":len(pairs),"total":len(interactions)},"network":{"shown":len(network["outliers"]),"total":network["sessions_total"]},"patterns":{"shown":len(patterns),"total":len(patterns),"source_total":len(payload.get("patterns") or [])},"episodes":{"shown":len(env["episodes"]),"total":len((payload.get("environment") or {}).get("episodes",[]))},"objects":{"shown":len(env["object_usage"]),"total":len((payload.get("environment") or {}).get("object_usage",[]))}}
    summary={"schema_version":3,"kind":"day","report_date":payload.get("report_date"),"freshness":{"generated_at":payload.get("generated_at"),"cutoff":payload.get("data_cutoff"),"as_of":payload.get("data_cutoff"),"timezone":payload.get("timezone") or "UTC"},"status":payload.get("status",{}),"population":population,"kpi":{"players":population["player_entities"],"unique_players":population["player_entities"],"humans":population["human_players"],"bots":population["bot_players"],"human_sessions":population["human_sessions"],"bot_sessions":population["bot_sessions"],"sessions":population["sessions"],"rounds":server.get("rounds",len(rounds_raw)),"events":qraw.get("event_count",0),"damage":combat.get("damage",0),"inferred_kills":combat.get("inferred_kills",0),"max_player_p95":network.get("max_player_p95"),"scene_interactions":sum(env["interaction_types"].values())},"timeline":list(timeline.values()),"players":player_rows,"maps":maps,"rounds":rounds,"combat":{"events_by_type":server.get("events_by_type",{}),"damage":combat.get("damage",0),"inferred_kills":combat.get("inferred_kills",0),"weapons":weapons,"pairs":pairs},"network":network,"environment":env,"patterns":patterns,"ai":ai,"quality":quality,"storage":[],"counts":counts,"incidents":[]}
    identity_players = (population["human_players"] or 0) + (population["bot_players"] or 0) if population["human_players"] is not None and population["bot_players"] is not None else population["player_entities"]
    summary["kpi"]["players"] = identity_players
    summary["kpi"]["unique_players"] = identity_players
    summary["kpi"]["observed_entities"] = population["player_entities"]
    summary["counts"]["players"]["total"] = identity_players if identity_players is not None else len(players)
    player_p95=[_optional_float(item.get("p95")) for item in (server.get("network_players") or []) if isinstance(item,dict)]
    player_p95=[value for value in player_p95 if value is not None]
    if player_p95:
        network["max_player_p95"]=max(player_p95)
        summary["kpi"]["max_player_p95"]=network["max_player_p95"]
    _attach_player_network(summary, payload, server)
    summary["freshness"]["as_of"] = summary["freshness"].get("as_of") or summary["freshness"].get("generated_at") or payload.get("report_date")
    if network.get("unresolved_sessions"): summary["incidents"].append({"severity":"warn","title":"Network mapping incomplete","detail":f"{network['unresolved_sessions']} session labels are not linked to player identity"})
    return summary


def _hist_quantile(hist: dict, q: float) -> float|None:
    values=sorted((_float(k),int(_float(v))) for k,v in (hist or {}).items() if _float(v)>0); total=sum(v for _,v in values); target=max(1,math.ceil(total*q)); running=0
    for band,count in values:
        running+=count
        if running>=target:return band+5
    return values[-1][0]+5 if values else None


def _live_ai(analytics: sqlite3.Connection) -> dict:
    """Read compact lifecycle/token accounting without exporting prompts or responses."""
    rows=[]
    for table, columns in (
        ("llm_jobs", "status"),
        ("llm_requests", "status"),
    ):
        rows.extend(_fetch(analytics, f"SELECT {columns} FROM {table}"))
    statuses=[str(row.get("status") or "").lower() for row in rows]
    queued=sum(status in {"queued","pending","retry","retrying"} for status in statuses)
    submitted=sum(status in {"submitted","running","processing","in_progress"} for status in statuses)
    complete=sum(status in {"complete","completed","succeeded","success"} for status in statuses)
    failed=sum(status in {"failed","error","cancelled","canceled"} for status in statuses)
    ledger=_fetch(analytics, "SELECT input_tokens,output_tokens,cached_tokens,estimated_input_tokens,estimated_output_tokens FROM llm_cost_ledger")
    def total(key: str, fallback: str) -> int | None:
        values=[row.get(key) for row in ledger if row.get(key) is not None]
        if values: return int(sum(_float(value) for value in values))
        values=[row.get(fallback) for row in ledger if row.get(fallback) is not None]
        return int(sum(_float(value) for value in values)) if values else None
    usage={
        "requests": len(ledger) or len(rows) or None,
        "input_tokens": total("input_tokens", "estimated_input_tokens"),
        "output_tokens": total("output_tokens", "estimated_output_tokens"),
        "cached_tokens": total("cached_tokens", "cached_tokens"),
        "estimated_cost": None,
        "cost_status": "unknown",
    }
    state="active" if submitted else "queued" if queued else "partial" if failed and complete else "complete" if complete else "pending"
    return {"status": state, "queue": {"queued": queued, "submitted": submitted, "complete": complete, "failed": failed}, "narrative": None, "usage": usage}


def _live_identity_map(config, session_ids: set[str]) -> dict[str, dict[str, Any]]:
    """Resolve live session IDs from telemetry without treating sessions as identities."""
    database=getattr(config,"telemetry_database",None)
    if not database or not session_ids: return {}
    try:
        uri=Path(database).resolve().as_uri()+"?mode=ro"
        telemetry=sqlite3.connect(uri,uri=True); telemetry.row_factory=sqlite3.Row
        try:
            marks=",".join("?" for _ in session_ids)
            rows=telemetry.execute(f"SELECT player_session_id,player_identity_id,is_bot FROM player_sessions WHERE player_session_id IN ({marks})",tuple(session_ids)).fetchall()
            return {str(row["player_session_id"]): {"identity":row["player_identity_id"],"bot":bool(row["is_bot"])} for row in rows}
        finally: telemetry.close()
    except (OSError,sqlite3.Error):
        return {}


def _parse_utc(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _live_source_freshness(config, checkpoint_id: object, processed_at: object, legacy_lag: float | None) -> dict[str, Any]:
    """Separate source idleness from an actual unprocessed source backlog."""
    database = getattr(config, "telemetry_database", None)
    fallback = {"backlog_lag_seconds": legacy_lag, "source_idle_seconds": None, "source_max_event_id": None, "source_max_event_at": None}
    if not database:
        return fallback
    try:
        uri = Path(database).resolve().as_uri() + "?mode=ro"
        telemetry = sqlite3.connect(uri, uri=True)
        try:
            source = telemetry.execute("SELECT MAX(event_id), MAX(utc_timestamp) FROM events").fetchone()
            source_id, source_at = (source or (None, None))
            if source_id is None:
                return {**fallback, "backlog_lag_seconds": 0.0}
            source_time = _parse_utc(source_at)
            now = datetime.now(timezone.utc)
            source_idle = max(0.0, (now - source_time).total_seconds()) if source_time else None
            try:
                processed_id = int(checkpoint_id or 0)
            except (TypeError, ValueError):
                processed_id = 0
            if processed_id >= int(source_id):
                backlog = 0.0
            else:
                checkpoint_row = telemetry.execute("SELECT utc_timestamp FROM events WHERE event_id<=? ORDER BY event_id DESC LIMIT 1", (processed_id,)).fetchone()
                checkpoint_time = _parse_utc(checkpoint_row[0] if checkpoint_row else processed_at)
                backlog = max(0.0, (source_time - checkpoint_time).total_seconds()) if source_time and checkpoint_time else legacy_lag
            return {"backlog_lag_seconds": round(backlog, 1) if backlog is not None else None, "source_idle_seconds": round(source_idle, 1) if source_idle is not None else None, "source_max_event_id": int(source_id), "source_max_event_at": source_at}
        finally:
            telemetry.close()
    except (OSError, sqlite3.Error):
        return fallback


def _live_summary(config, analytics: sqlite3.Connection) -> dict:
    max_rows = _fetch(analytics, "SELECT MAX(minute_start) AS value FROM agg_server_minute")
    end = max_rows[0].get("value") if max_rows and max_rows[0].get("value") else None
    if end:
        try:
            start = (datetime.fromisoformat(str(end).replace("Z", "+00:00")) - timedelta(minutes=LIVE_WINDOW_MINUTES)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            start = end
    else:
        start = None
    args = (start, end) if start and end else ("", "0000")
    server_rows = _fetch(analytics, "SELECT minute_start,metrics_json FROM agg_server_minute WHERE minute_start>=? AND minute_start<=? ORDER BY minute_start", args)
    player_rows = _fetch(analytics, "SELECT minute_start,player_session_id,metrics_json FROM agg_player_minute WHERE minute_start>=? AND minute_start<=? ORDER BY minute_start", args)
    net_rows = _fetch(analytics, "SELECT minute_start,player_session_id,ping_count,ping_sum,ping_min,ping_max,histogram_json FROM agg_network_minute WHERE minute_start>=? AND minute_start<=? ORDER BY minute_start", args)

    # Aggregates are keyed by session. Resolve them through telemetry before
    # presenting identity counts; an unavailable mapping stays explicitly unknown.
    session_ids = {str(row.get("player_session_id") or "unknown") for row in player_rows + net_rows}
    identity_map = _live_identity_map(config, session_ids)

    def identity_for(session_id: str) -> str | None:
        value = identity_map.get(session_id, {}).get("identity")
        return str(value) if value else None

    def group_key(session_id: str) -> str:
        identity = identity_for(session_id)
        return f"identity:{identity}" if identity else f"session:{session_id}"

    groups: dict[str, dict[str, Any]] = {}
    for session_id in sorted(session_ids):
        key = group_key(session_id)
        group = groups.setdefault(key, {"damage": 0.0, "kills": 0, "events": 0, "sessions": set(), "bot": None})
        group["sessions"].add(session_id)
        if session_id in identity_map:
            group["bot"] = bool(identity_map[session_id].get("bot"))
    series: dict[str, dict[str, Any]] = {}
    active: defaultdict[str, set[str]] = defaultdict(set)
    for item in server_rows:
        row = series.setdefault(str(item["minute_start"]), {"t": item["minute_start"], "events": 0, "damage": 0, "kills": 0, "deaths": 0, "players": 0})
        metrics = _metric_json(item.get("metrics_json"))
        row["events"] += int(_float(metrics.get("events")))
        row["damage"] += _float(metrics.get("damage"))
        row["kills"] += int(_float(metrics.get("kills")))
        row["deaths"] += int(_float(metrics.get("deaths")))
    for item in player_rows:
        minute = str(item["minute_start"])
        session_id = str(item.get("player_session_id") or "unknown")
        active[minute].add(session_id)
        group = groups.setdefault(group_key(session_id), {"damage": 0.0, "kills": 0, "events": 0, "sessions": set(), "bot": None})
        group["sessions"].add(session_id)
        metrics = _metric_json(item.get("metrics_json"))
        group["events"] += int(_float(metrics.get("events")))
        group["damage"] += _float(metrics.get("damage"))
        group["kills"] += int(_float(metrics.get("kills")))
    for minute, session_set in active.items():
        series.setdefault(minute, {"t": minute, "events": 0, "damage": 0, "kills": 0, "deaths": 0, "players": 0})["players"] = len(session_set)

    ordered_groups = sorted(groups.items(), key=lambda item: (item[1]["damage"], item[1]["events"], item[0]), reverse=True)
    labels = {}
    for index, (key, _value) in enumerate(ordered_groups, 1):
        labels[key] = f"Player {index:03d}" if key.startswith("identity:") else f"Session {index:03d}"
    compact_players = [{"player": labels[key], "bot": value["bot"], "sessions": len(value["sessions"]), "playtime_min": None, "combat": {"damage_dealt": round(value["damage"], 1), "inferred_kills": value["kills"]}, "input": {"apm": None}, "network": {}} for key, value in ordered_groups[:MAX_PLAYERS]]

    net: dict[str, dict[str, Any]] = {}
    net_session_ids: set[str] = set()
    net_series: dict[str, dict[str, Any]] = {}
    for item in net_rows:
        session_id = str(item.get("player_session_id") or "unknown")
        net_session_ids.add(session_id)
        key = group_key(session_id)
        histogram = _metric_json(item.get("histogram_json"))
        row = net.setdefault(key, {"samples": 0, "sum": 0.0, "max": None, "hist": {}, "spikes": 0, "sessions": set(), "resolved": bool(identity_for(session_id))})
        row["sessions"].add(session_id)
        count = int(item.get("ping_count") or 0)
        row["samples"] += count
        row["sum"] += _float(item.get("ping_sum"))
        ping_max = _optional_float(item.get("ping_max"))
        row["max"] = ping_max if row["max"] is None else max(row["max"], ping_max or 0)
        row["spikes"] += sum(int(_float(value)) for band, value in histogram.items() if _float(band) >= 50)
        for band, value in histogram.items():
            row["hist"][band] = row["hist"].get(band, 0) + int(_float(value))
        minute_row = net_series.setdefault(str(item["minute_start"]), {"samples": 0, "sum": 0.0, "max": None, "hist": {}})
        minute_row["samples"] += count
        minute_row["sum"] += _float(item.get("ping_sum"))
        minute_max = _optional_float(item.get("ping_max"))
        minute_row["max"] = minute_max if minute_row["max"] is None else max(minute_row["max"], minute_max or 0)
        for band, value in histogram.items():
            minute_row["hist"][band] = minute_row["hist"].get(band, 0) + int(_float(value))
    network = []
    for index, (key, value) in enumerate(sorted(net.items(), key=lambda item: _hist_quantile(item[1]["hist"], .95) or -1, reverse=True)[:MAX_NETWORK], 1):
        network.append({"player": labels.get(key, f"Network session {index:03d}"), "mapping": "resolved" if value["resolved"] else "session only", "sessions": len(value["sessions"]), "samples": value["samples"], "p95": _hist_quantile(value["hist"], .95), "p99": _hist_quantile(value["hist"], .99), "max": value["max"], "spikes": value["spikes"], "seconds_above": 0})
    resolved_network_sessions = sum(1 for session_id in net_session_ids if identity_for(session_id))
    for minute, value in net_series.items():
        row = series.setdefault(minute, {"t": minute, "events": 0, "damage": 0, "kills": 0, "deaths": 0, "players": 0})
        row["ping_mean"] = round(value["sum"] / value["samples"], 1) if value["samples"] else None
        row["ping_p95"] = _hist_quantile(value["hist"], .95)
        row["ping_p99"] = _hist_quantile(value["hist"], .99)
        row["ping_max"] = value["max"]
    ordered = sorted(series.values(), key=lambda value: str(value["t"]))
    latest = ordered[-1] if ordered else {}
    legacy_lag = None
    try:
        updated = analytics.execute("SELECT MAX(updated_at) FROM agg_server_minute").fetchone()[0]
        legacy_lag = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(str(updated).replace("Z", "+00:00"))).total_seconds()) if updated else None
    except (sqlite3.Error, TypeError, ValueError):
        pass
    storage = [{key: item.get(key) for key in ("component", "used_bytes", "max_bytes", "watermark", "state", "dropped_count", "malformed_count", "gap_count")} for item in _fetch(analytics, "SELECT component,used_bytes,max_bytes,watermark,state,dropped_count,malformed_count,gap_count FROM storage_health ORDER BY component")]
    checkpoint_rows = _fetch(analytics, "SELECT last_event_id,processed_at FROM processing_checkpoints WHERE consumer_name='live_analyzer'")
    checkpoint = checkpoint_rows[0] if checkpoint_rows else {}
    source_freshness = _live_source_freshness(config, checkpoint.get("last_event_id"), checkpoint.get("processed_at"), legacy_lag)
    backlog_lag = source_freshness["backlog_lag_seconds"]
    ai = _live_ai(analytics)
    mapped_sessions = {session_id for session_id in session_ids if identity_for(session_id)}
    identities = {identity_for(session_id) for session_id in mapped_sessions}
    human_ids = {identity_for(session_id) for session_id in mapped_sessions if not identity_map[session_id].get("bot")}
    bot_ids = {identity_for(session_id) for session_id in mapped_sessions if identity_map[session_id].get("bot")}
    mapping_available = bool(identity_map)
    unknown_sessions = session_ids - mapped_sessions
    unknown_bot_sessions = sum(1 for session_id in unknown_sessions if identity_map.get(session_id, {}).get("bot")) if mapping_available else None
    population = {"player_entities": len(identities) if mapping_available else None, "unique_players": len(identities) if mapping_available else None, "identified_players": len(identities), "unknown_sessions": len(unknown_sessions), "unknown_bot_sessions": unknown_bot_sessions, "identified_sessions": len(mapped_sessions), "active_now": latest.get("players", 0), "sessions_window": len(session_ids), "human_players": len(human_ids) if mapping_available else None, "bot_players": len(bot_ids) if mapping_available else None, "human_sessions": sum(1 for session_id in mapped_sessions if not identity_map[session_id].get("bot")) if mapping_available else None, "bot_sessions": sum(1 for session_id in mapped_sessions if identity_map[session_id].get("bot")) if mapping_available else None, "sessions": len(session_ids)}
    generated_at = datetime.now(timezone.utc).isoformat()
    event_total = sum(int(_float(value.get("events"))) for value in ordered)
    summary = {"schema_version": 3, "kind": "live", "generated_at": generated_at, "freshness": {"generated_at": generated_at, "processed_at": checkpoint.get("processed_at"), "last_processed_event_id": checkpoint.get("last_event_id", 0), "lag_seconds": backlog_lag, "backlog_lag_seconds": backlog_lag, "source_idle_seconds": source_freshness["source_idle_seconds"], "source_max_event_id": source_freshness["source_max_event_id"], "source_max_event_at": source_freshness["source_max_event_at"], "lag_definition": "backlog lag; zero when the live checkpoint has caught up to the source max event", "window_start": start, "window_end": end, "as_of": end, "timezone": "UTC"}, "status": {"deterministic": "complete", "llm": ai["status"]}, "population": population, "kpi": {"players": latest.get("players", 0), "active_now": latest.get("players", 0), "unique_players": population["unique_players"], "events": event_total, "events_per_min": round(event_total / max(len(ordered), 1), 1), "damage": round(sum(_float(value.get("damage")) for value in ordered), 1), "p95_ping": next((value.get("ping_p95") for value in reversed(ordered) if value.get("ping_p95") is not None), None), "p99_ping": next((value.get("ping_p99") for value in reversed(ordered) if value.get("ping_p99") is not None), None), "ping_max": max([value.get("ping_max") for value in ordered if value.get("ping_max") is not None], default=None), "lag_seconds": backlog_lag, "backlog_lag_seconds": backlog_lag, "source_idle_seconds": source_freshness["source_idle_seconds"], "llm_queued": ai["queue"]["queued"]}, "timeline": ordered, "players": compact_players, "maps": [], "rounds": [], "combat": {"events_by_type": {}, "weapons": [], "pairs": []}, "network": {"sessions_total": len(net_session_ids), "resolved_sessions": resolved_network_sessions, "unresolved_sessions": len(net_session_ids) - resolved_network_sessions, "outliers": network, "p95_method": "10ms histogram estimate"}, "environment": {"available": False, "interaction_types": {}, "categories": {}, "heatmap": [], "episodes": []}, "patterns": [], "ai": ai, "quality": {"metrics": []}, "storage": storage, "counts": {"players": {"shown": len(compact_players), "total": len(groups)}, "network": {"shown": len(network), "total": len(net)}, "patterns": {"shown": 0, "total": 0}, "episodes": {"shown": 0, "total": 0}}, "incidents": []}
    if summary["network"]["unresolved_sessions"]:
        summary["incidents"].append({"severity": "warn", "title": "Network mapping incomplete", "detail": f"{summary['network']['unresolved_sessions']} network sessions are not linked to a player identity"})
    if population["unknown_sessions"]:
        summary["incidents"].append({"severity": "warn", "title": "Identity mapping incomplete", "detail": f"{population['unknown_sessions']} observed sessions remain session-only; unique player count is partial"})
    if backlog_lag is not None and backlog_lag > 120:
        summary["incidents"].append({"severity": "bad", "title": "Pipeline backlog", "detail": f"{_number(backlog_lag)} seconds of source events remain unprocessed"})
    return summary


def _shell(summary: dict, data_url: str, kind: str, title: str, asset_prefix: str) -> str:
    sections=[("overview","Overview"),("timeline","Timeline"),("players","Players"),("maps","Maps"),("rounds","Rounds"),("combat","Combat"),("network","Network"),("environment","Scene"),("patterns","Patterns"),("ai","AI"),("quality","Quality"),("storage","Storage")];nav="".join(f"<a href='#{k}'>{v}</a>" for k,v in sections);content=f"<div id='dashboard-app' data-kind='{html.escape(kind)}' data-data-url='{html.escape(data_url)}'><header class='hero'><div><div class='summary-line'><span id='mode' class='mode'>{'LIVE OPERATIONS' if kind=='live' else 'DAY RESEARCH'}</span><h1 id='title'>{html.escape(title)}</h1></div><p id='freshness' class='muted'></p><p id='refresh-state' class='small muted'></p></div><div class='toolbar'><a href='{asset_prefix}../index.html'>All days</a>{" · <a href='live.html'>Live</a>" if kind!='live' else " · <a href='index.html'>Dashboard</a>"}</div></header><nav class='section-nav' aria-label='Dashboard sections'>{nav}</nav><section id='incidents'><h2>Incidents and interpretation</h2><p id='definitions' class='definition'></p><div id='incident-content'></div></section><section id='overview'><h2>Overview</h2><div id='kpis' class='grid'></div></section><section id='timeline'><h2>Timeline</h2><p id='timeline-label' class='muted'></p><canvas id='timeline-chart'></canvas></section><div class='columns'><section id='players'><h2>Players</h2><div id='players-table'></div></section><section id='maps'><h2>Maps</h2><div id='maps-table'></div></section></div><div class='columns'><section id='rounds'><h2>Rounds</h2><div id='rounds-table'></div></section><section id='combat'><h2>Combat</h2><div id='event-types' class='chips'></div><h3>Weapons</h3><div id='weapons-table'></div><h3>Top interactions</h3><div id='pairs-table'></div></section></div><section id='network'><h2>Network</h2><p class='definition'>p95/p99 are estimates from stored histograms; max is separate.</p><div id='network-table'></div></section><section id='environment'><h2>Environment and scene</h2><div id='environment-content'></div><h3>Interaction types</h3><div id='scene-types' class='chips'></div><h3>Interaction heatmap</h3><canvas id='heatmap-canvas'></canvas><div id='heatmap-legend' class='legend small muted'></div><h3>Object usage</h3><div id='scene-objects'></div><h3>Motifs</h3><div id='scene-motifs' class='chips'></div><h3>Barrel candidates</h3><div class='table-wrap'><table><thead><tr><th>Rank</th><th>Object</th><th>Confidence</th><th>Player gain</th><th>Observation</th></tr></thead><tbody id='barrel-candidates'></tbody></table></div><h3>Episodes</h3><div id='episodes-table'></div></section><section id='patterns'><h2>Patterns and anomalies</h2><p class='definition'>Grouped family/state results; low-confidence signatures are collapsed.</p><div id='patterns-table'></div></section><section id='ai'><h2>AI analysis and usage</h2><div id='ai-content'></div><div id='ai-usage' class='chips'></div></section><div class='columns'><section id='quality'><h2>Data quality</h2><div class='quality-row'><b>Metric</b><b>Value</b><b>Severity</b><b>Definition</b></div><div id='quality-content'></div></section><section id='storage'><h2>Storage health</h2><div id='storage-content' class='grid'></div></section></div><script>window.__DASHBOARD_DATA__={_json(summary)};</script><script src='{asset_prefix}dashboard.js' defer></script></div>";return _page(title,content,asset_prefix)


def build_dashboard(report_directory: str, report_date: str | None = None, telemetry_database: str | None = None, episode_id: int | None = None) -> Path:
    root=Path(report_directory); dashboard=root.parent/"dashboard"; assets=dashboard/"assets"; days=dashboard/"days"; assets.mkdir(parents=True,exist_ok=True);days.mkdir(parents=True,exist_ok=True);(dashboard/"players").mkdir(parents=True,exist_ok=True);(dashboard/"episodes").mkdir(parents=True,exist_ok=True);_atomic_write_text(assets/"dashboard.css",CSS);_atomic_write_text(assets/"dashboard.js",JS);reports=[p for p in _select_report_artifacts(root) if report_date is None or _artifact_family(p)==report_date]; outputs=[]
    if telemetry_database and episode_id is not None:
        from .scene import load_episode
        from . import dashboard as legacy_dashboard
        telemetry=sqlite3.connect(Path(telemetry_database).resolve().as_uri()+"?mode=ro",uri=True); telemetry.row_factory=sqlite3.Row
        try:
            episode=load_episode(telemetry,int(episode_id))
            if episode: _atomic_write_text(dashboard/"episodes"/f"{episode_id}.html",legacy_dashboard._episode_page(episode))
        finally: telemetry.close()
    for report in reports:
        date=_artifact_family(report);summary=_compact_report(json.loads(report.read_text(encoding="utf-8")));jp=_atomic_json(days/f"{date}.json",summary);hp=_atomic_write_text(days/f"{date}.html",_shell(summary,f"../days/{jp.name}","day",f"SFD Day Research — {date}","../assets/"));outputs.append({"date":date,"href":f"days/{hp.name}","json":f"days/{jp.name}","bytes":jp.stat().st_size,"artifact":report.name})
    _atomic_json(dashboard/"index.json",{"schema_version":3,"kind":"index","generated_at":datetime.now(timezone.utc).isoformat(),"days":outputs});links="".join(f"<li><a href='{x['href']}'>{html.escape(x['date'])}</a> <span class='muted small'>({x['bytes']:,} bytes summary)</span></li>" for x in outputs);return _atomic_write_text(dashboard/"index.html",_page("SFD Telemetry Dashboard",f"<h1>SFD Telemetry Dashboard</h1><p class='muted'>Operational live snapshot and processed day research summaries.</p><p><a href='live.html'>Open Live Operations</a></p><section><h2>Day Research</h2><ul class='day-links'>{links or '<li class=empty>No reports yet</li>'}</ul></section>"))


def build_live_dashboard(config, analytics: sqlite3.Connection) -> Path:
    dashboard=Path(config.report_directory).parent/"dashboard";assets=dashboard/"assets";dashboard.mkdir(parents=True,exist_ok=True);assets.mkdir(parents=True,exist_ok=True);summary=_live_summary(config,analytics);jp=_atomic_json(dashboard/"live.json",summary);_atomic_write_text(assets/"dashboard.css",CSS);_atomic_write_text(assets/"dashboard.js",JS);return _atomic_write_text(dashboard/"live.html",_shell(summary,jp.name,"live","SFD Live Operations","assets/"))
