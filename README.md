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

## Verify

```bat
py -3 -m unittest discover -s tests -v
pwsh -File tools\compile_sfd_script.ps1
py -3 tools\analyze.py summary
```

See `docs\CAPABILITIES.md` for exact SFD 1.6 availability and privacy limits,
`docs\INSTALL.md` for operations, and `docs\SCHEMA.md` for tables/views.
