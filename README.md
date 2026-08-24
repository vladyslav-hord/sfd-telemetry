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

## Dashboard

```bat
py -3 -m analyzer.main --config analyzer\config.json dashboard
open_dashboard.bat
```

The dashboard is static and local. Scene replay data becomes available only for
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
