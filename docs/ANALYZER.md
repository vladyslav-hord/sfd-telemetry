# SFD hourly analyzer

Requirements: Python 3.10+ and packages from `requirements.txt`.

Copy `analyzer/config.example.json` to `analyzer/config.json`. Paths are relative to the project root. The analyzer opens telemetry SQLite in read-only mode and writes only `data/sfd_analytics.sqlite3` and `data/analysis`.

Run hourly:

```bat
py -3 -m analyzer.main hourly --config analyzer\config.json
```

Use `run_hourly_analysis.bat` in Windows Task Scheduler. It appends output to `data/analysis/analyzer.log` and returns a non-zero code on failure. The live analyzer remains running while hourly recomputation writes its report.

Useful commands:

```bat
py -3 -m analyzer.main rebuild --from 2026-08-01 --to 2026-08-24
py -3 -m analyzer.main export --date 2026-08-24
py -3 -m analyzer.main validate
```

`dashboard --episode <source-event-id>` rebuilds one local scene replay page.
The regular `dashboard` and `hourly` commands rebuild compact processed
summaries with sections for KPIs, timeline, players, maps, combat, network,
scene, patterns, AI status and storage. Raw telemetry, chat and LLM response
envelopes are not embedded in day HTML. The replay is a sampled
reconstruction, not a physics-authoritative recording.

`OPENAI_API_KEY` is optional. Without it, deterministic metrics and JSON reports still complete with `llm=disabled`. Raw chat is never written into daily JSON. Public names/chat are queued only when OpenAI is enabled; persistent identifiers, profiles and IP data are never sent.

With `OPENAI_API_KEY`, queued chat work is uploaded as a JSONL Batch for `/v1/responses`; `sync-llm` imports completed output on the next run. API failures leave the deterministic report complete and the LLM status `partial`.

## Windows Task Scheduler

Create an hourly task in the `Europe/Warsaw` reporting timezone. Program: `cmd.exe`. Arguments: `/c "C:\path\to\sfd-telemetry\run_hourly_analysis.bat"`. Start in: `C:\path\to\sfd-telemetry`. Run it under an account that can read the telemetry database and set `OPENAI_API_KEY` only in that account's environment.

The batch launcher records output in `data/analysis/analyzer.log`. A non-zero exit code means the deterministic job failed; inspect that log, then run `py -3 -m analyzer.main validate --config analyzer\config.json`.

## Privacy, cost and limits

The analyzer never reads `ConnectionIP`; gameplay requests replace player/session identifiers with ephemeral labels and send only justified evidence event IDs. If OpenAI is enabled it sends only public chat/name fields allowed by config and compact aggregate daily metrics. Set `send_public_names` to `false` to omit names.

Batch requests use the configured `openai_model` (default `gpt-5-nano`), `store=false`, minimal reasoning and a durable request hash. Token limits in config defer excess work; they do not delete it. `omni-moderation-latest` labels chat only for analytics priority and never kicks, bans or changes gameplay.

High-coverage anomalous movement and scene candidates can be queued as compact
`gameplay_analysis_v2` requests. They contain only derived features, source
event IDs and the local candidate window—never the full telemetry database.

Telemetry limitations are reflected in reports: packet loss is unavailable, jitter is estimated, team chat/whispers are unavailable, identities are host-scoped, and killer/assist/round result may be unknown or inferred. Pattern candidates are observations only, never evidence of cheating or misconduct.

`open_dashboard.bat` starts a local HTTP server on port 8765. This enables live
`live.json` polling in browsers that block `fetch()` from `file://` URLs. The
dashboard also contains an embedded snapshot as a direct-open fallback.

## Dashboard views

The dashboard has two deliberately different views:

- **Live Operations**: a rolling three-hour window with active sessions,
  mapped unique identities, events/minute, histogram-based ping p95/p99 and
  observed max. `backlog_lag_seconds` (and compatibility `lag_seconds`) means
  source events not yet processed and is zero when the checkpoint reaches the
  source max event. `source_idle_seconds` is separate neutral source freshness;
  an idle source does not create a pipeline-backlog incident.
- **Day Research**: the completed report with unique player entities separated
  from human/bot session counts, round timeline, top-N tables, scene heatmap,
  grouped patterns, AI status/usage and quality definitions.

Tables are compact top-N views and display `showing X of Y`; filtering and
column sorting happen in the browser. The JSON contract is schema version 3.
When a legacy report has no explicit population contract, `players` means the
number of rows in its player array; it is never derived from session counts.
With the explicit contract, `Unique players` is the sum of distinct human and
bot identities; `Observed entities` additionally includes sessions with no
stable identity. Those values are intentionally separate from session totals.
Unresolved network rows receive a neutral `Network session NNN` label and are
not presented as named players. Cached-token and cost values remain `unknown`
when the provider did not report them.
