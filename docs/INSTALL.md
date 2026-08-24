# Installation

Requirements: Superfighters Deluxe 1.6 Server Tool and Python 3.10+.
No Python packages are required.

1. Install the extension script:

   ```powershell
   pwsh -File .\tools\install_script.ps1
   ```

2. In Server Tool, open **Scripts**, enable `Custom\BotRotation.txt`, then apply
   the change. SFD persists enabled scripts in `HOST_GAME_ENABLED_SCRIPTS`, so it
   loads automatically on later server starts.

3. Copy `collector\config.example.json` to `collector\config.json` only if paths
   or retention settings need changing.

4. Start collector and Server Tool together:

   ```bat
   start_all.bat
   ```

   If Server Tool is already running, use `start_collector.bat`.

The database appears at `data\sfd_telemetry.sqlite3`. The extension's bounded
transport spool is at
`%USERPROFILE%\Documents\Superfighters Deluxe\Cache\ScriptData\Shared\CCC.txt`.
It is transport state, not the database.

Telemetry rates are compile-time constants at the top of `sfd\SFDTelemetry.txt`:
1 Hz persistent player state, 20 Hz/10-second RAM ring, 4 Hz change-based scene
objects, 5-second ping samples, and feature
switches for chat, inputs, projectiles, noisy object events and world context. Re-run the local compile
check after changing them. Collector polling, archive and independent chat/gameplay
retention are in `collector\config.json`.

Useful commands:

```bat
py -3 -m collector.main --config collector\config.json once
py -3 -m collector.main --config collector\config.json backup
py -3 -m collector.main --config collector\config.json maintenance
py -3 tools\analyze.py summary
py -3 tools\analyze.py ping
```

Run `maintenance --vacuum` only while Server Tool and collector are idle.
