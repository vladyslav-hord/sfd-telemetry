# SFD daily analyzer

Requirements: Python 3.10+ and packages from `requirements.txt`.

Copy `analyzer/config.example.json` to `analyzer/config.json`. Paths are relative to the project root. The analyzer opens telemetry SQLite in read-only mode and writes only `data/sfd_analytics.sqlite3` and `data/analysis`.

Run daily:

```bat
py -3 -m analyzer.main daily --config analyzer\config.json
```

Use `run_daily_analysis.bat` in Windows Task Scheduler once per day. It appends output to `data/analysis/analyzer.log` and returns a non-zero code on failure.

Useful commands:

```bat
py -3 -m analyzer.main rebuild --from 2026-08-01 --to 2026-08-24
py -3 -m analyzer.main export --date 2026-08-24
py -3 -m analyzer.main validate
```

`OPENAI_API_KEY` is optional. Without it, deterministic metrics and JSON reports still complete with `llm=disabled`. Raw chat is never written into daily JSON. Public names/chat are queued only when OpenAI is enabled; persistent identifiers, profiles and IP data are never sent.

With `OPENAI_API_KEY`, queued chat work is uploaded as a JSONL Batch for `/v1/responses`; `sync-llm` imports completed output on the next run. API failures leave the deterministic report complete and the LLM status `partial`.

## Windows Task Scheduler

Create a daily task after midnight in the `Europe/Warsaw` reporting timezone. Program: `cmd.exe`. Arguments: `/c "C:\path\to\sfd-telemetry\run_daily_analysis.bat"`. Start in: `C:\path\to\sfd-telemetry`. Run it under an account that can read the telemetry database and set `OPENAI_API_KEY` only in that account's environment.

The batch launcher records output in `data/analysis/analyzer.log`. A non-zero exit code means the deterministic job failed; inspect that log, then run `py -3 -m analyzer.main validate --config analyzer\config.json`.

## Privacy, cost and limits

The analyzer never reads `ConnectionIP` and never sends `player_identity_id`, AccountID, UserID, profiles or the telemetry database to OpenAI. If OpenAI is enabled it sends only public chat/name fields allowed by config and aggregate daily metrics. Set `send_public_names` to `false` to omit names.

Batch requests use the pinned `gpt-5-nano-2025-08-07` model, `store=false`, minimal reasoning and a durable request hash. Token limits in config defer excess work; they do not delete it. `omni-moderation-latest` labels chat only for analytics priority and never kicks, bans or changes gameplay.

Telemetry limitations are reflected in reports: packet loss is unavailable, jitter is estimated, team chat/whispers are unavailable, identities are host-scoped, and killer/assist/round result may be unknown or inferred. Pattern candidates are observations only, never evidence of cheating or misconduct.
