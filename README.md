# SFD Telemetry

Passive telemetry for the Superfighters Deluxe 1.6 dedicated Server Tool. It uses
only the public ScriptAPI and SFD shared storage. It does not change gameplay,
inspect packets, expose real IP addresses, or reconstruct SteamIDs.

## Install and run

Requirements: SFD 1.6 Server Tool and Python 3.10+.

```powershell
pwsh -File .\tools\install_script.ps1
```

Enable `Custom\BotRotation.txt` once in Server Tool -> Scripts, then run:

```bat
start_all.bat
```

SQLite: `data\sfd_telemetry.sqlite3`.

## Live analyzer and storage limits

Run the collector and incremental analyzer as separate processes:

```powershell
python -m collector.main --config collector\config.json run
python -m analyzer.main live --config analyzer\config.json
```

Useful operational commands:

```powershell
python -m analyzer.main live --once --config analyzer\config.json
python -m analyzer.main reconcile --hours 2 --config analyzer\config.json
python -m analyzer.main maintenance --config analyzer\config.json
python -m analyzer.main hourly --config analyzer\config.json
python -m analyzer.main migrate --config analyzer\config.json
```

Build one episode page on demand with `python -m analyzer.main dashboard --episode <source-event-id> --config analyzer\config.json`.

The collector uses segmented gzip raw storage with a hard 1 GB quota. The analyzer
stores checkpoints and minute aggregates in analytics SQLite; telemetry plus WAL,
analytics SQLite plus WAL, and episode chunks are accounted against the configured
15 GB working quota. Raw segments are deleted only after they are marked processed.

## Dashboard

```bat
py -3 -m analyzer.main --config analyzer\config.json dashboard
open_dashboard.bat
```

The dashboard uses compact processed JSON summaries and polls `live.json`
without reloading the page. `open_dashboard.bat` starts a local HTTP server so
browser `fetch()` works; raw telemetry, chat and LLM response payloads are not
embedded in the dashboard. Scene replay data becomes available only for
rounds collected after script version 1.2.0 is applied in Server Tool.
Version 1.2.5 records bounded 4 Hz trajectories for active projectiles and a
5-second, 20 Hz in-memory ring for up to 64 relevant objects. Object and player
data remain adaptive to keep the telemetry budget bounded.

Scene pages are written under `data\dashboard\episodes`. They provide a local
canvas replay with scrubber, play speeds and independent layers for players,
objects, projectiles and exact/derived interactions. Old reports explicitly
show scene telemetry as unavailable rather than fabricating historical data.

## Verify

```bat
py -3 -m unittest discover -s tests -v
pwsh -File tools\compile_sfd_script.ps1
py -3 tools\analyze.py summary
```

See `docs\CAPABILITIES.md` for exact SFD 1.6 availability and privacy limits,
`docs\INSTALL.md` for operations, and `docs\SCHEMA.md` for tables/views.
