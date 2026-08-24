# SFD 1.6 telemetry capabilities

Source of truth: local Steam build `24892602`, installed 2026-08-23 at
`D:\Programs\Steam\steamapps\common\Superfighters Deluxe`. API definitions were
checked against `Content\Data\Misc\ScriptAPI\html` and the public metadata of
`SFD.GameScriptInterface.dll`. Server Tool behavior was checked against the local
`DSServerHelp.html`, `Superfighters Deluxe Server.dll`, the running 64-bit/FNA
Server Tool, and the local `config.ini`.

`available` means available to an ordinary extension script. `derived` means the
value is explicitly marked as an inference in stored data.

| wanted datum | exact API/event | available in SFD 1.6 | limitations | collection strategy |
|---|---|---:|---|---|
| Extension lifecycle | `OnStartup`, `AfterStartup`, `OnShutdown` | yes | `OnShutdown` is before map restart/script deactivation, not process-crash notification | emit script/round start and shutdown records |
| Server process start/stop | no ScriptAPI event | no | lifecycle is script/map scoped; a crash cannot run `OnShutdown` | collector records its own start; infer server restart only from a new telemetry session |
| User join | `Game.Events.StartUserJoinCallback(Action<IUser[]>)` | yes | callback can contain several local users | one `player_session_id` per `IUser` |
| User leave | `Game.Events.StartUserLeaveCallback(Action<IUser[], DisconnectionType>)` | yes | reason is only `ConnectionLost` or `Left`; no kick/ban distinction | store exact enum, never invent a finer reason |
| Character created | `StartPlayerCreatedCallback(Action<IPlayer[]>)` | yes | player creation is not the same as network join | link by `UserIdentifier` |
| Character death/removal | `StartPlayerDeathCallback(Action<IPlayer, PlayerDeathArgs>)` | yes | args expose killed/removed, not killer | correlate with recent damage only as a separately marked inference |
| Authoritative killer/kill | no killer field on `PlayerDeathArgs` | no | `PlayerDamageArgs.SourceID` is event-type-specific and the final damage source may be environmental | store damage/death separately; do not label an inferred killer as authoritative |
| Public user message | `StartUserMessageCallback(Action<UserMessageCallbackArgs>)` | yes | receives the messages exposed by SFD; API has no whisper/team-channel field | store callback payload only |
| Commands | `UserMessageCallbackArgs.IsCommand`, `Command`, `CommandArguments`, `Message` | yes | `Command` is upper-case; arguments may be sensitive if server operators type secrets into public commands | configurable; raw public callback payload |
| Team/whisper chat | no channel field or dedicated callback | no | cannot prove or select these channels | do not attempt collection |
| Key actions | `StartPlayerKeyInputCallback(Action<IPlayer, VirtualKeyInfo[]>)` | yes | only SFD `VirtualKey`; network input events can be lost in extreme packet loss | store `Pressed`/`Released`, key and value; configurable off switch |
| User session handle | `IUser.UserIdentifier` | yes | unique only for the current game/server session | session linkage only |
| Legacy `UserID` / `UserId` | `IUser.UserID`, `IUser.UserId` | yes | undocumented aliases; local 1.6 implementation returns `UserIdentifier` widened to `long` | store only as legacy/session value, never as identity |
| Persistent account identity | `IUser.AccountID` | yes, host-scoped | SHA-256-derived, trimmed Base64 value salted with the host identifier; prefix `S0` for real users, `SF` for editor test accounts; empty for bots. It is not a SteamID and is not portable between hosts | use `S0...` as `player_identity_id` with `host_scoped_hash` confidence; reject `SF...` as real identity |
| SteamID | no ScriptAPI property | no | raw stable Steam identifier is intentionally not exposed | never reconstruct or enrich |
| Connection IP | `IUser.ConnectionIP` | obsolete/fake | since 1.5 it returns a fake IP, empty for bots and `localhost` for host | never collect as network identity or real IP |
| Account display name | `IUser.AccountName` | yes | mutable display value; empty for bots; not an ID | alias history only |
| Character name | `IUser.Name`, `IPlayer.Name`, `IProfile.Name` | yes | mutable | alias/profile snapshots |
| Local co-op index | `IUser.LocalUserIndex` | yes | 0 for bots; differentiates users under the same account | session context and account-hash sub-identity |
| Slot/team/roles | `GameSlotIndex`, `GetTeam()`, `IsHost`, `IsModerator` | yes | role/team can change | join plus state/checkpoint snapshots |
| Spectator state | `Spectating`, `JoinedAsSpectator`, `IsSpectator` | yes | `Spectating` includes waiting for next round | join/leave/state snapshots |
| Human/bot | `IUser.IsUser`, `IUser.IsBot`, `IPlayer.IsUser`, `IPlayer.IsBot` | yes | a bot has no account identity | store exact flags |
| Ping | `IUser.Ping` | yes | average RTT in ms as seen by server, not packet loss/jitter | raw configurable samples, default 5 s; derive aggregates offline |
| Lifetime public counters | `TotalGames`, `TotalWins`, `TotalLosses` | yes | semantics are SFD account counters, not telemetry-server-only counters | join/leave snapshots |
| Profile/avatar | `IUser.GetProfile()` / `IPlayer.GetProfile()`; `IProfile` | yes | selected game avatar only | JSON profile snapshots and normalized profile table |
| Profile gender | `IProfile.Gender`, `IUser.Gender` | yes | avatar selection, not real-world gender | label explicitly as `avatar_gender` |
| Clothing/colors | `IProfile.{Skin,ChestOver,ChestUnder,Hands,Waist,Legs,Feet,Accessory,Head}`; item `Name`, `Color1..3`, `ColorPalette` | yes | current selected outfit | serialize all layers |
| Map identity | `IGame.MapName`, `MapGUID`, `MapOriginalGUID`, `MapAuthor` | yes | GUIDs may be empty for unusual content | round start record |
| Map/game type | `GetMapType()`, `GetGameType()` | yes | no direct “FFA” property | `Independent` teams describe FFA; retain raw enums |
| Map round | `IGame.MapRound` | yes | starts at 1 and resets on map change | round context |
| Time limit | `TimeLimit`, `TimeLimitActive`, `GetRemainingTimeLimit()` | yes | remaining time is a live value | round start/end context |
| Sudden death | `SuddenDeathEnabled`, `SuddenDeathActive` | yes | no transition callback | persist in 1 Hz state/round end; emit transition detected by existing low-rate update |
| Round transition | script lifecycle | yes | no dedicated round callback | `OnStartup`/`OnShutdown`, no frame polling |
| Game-over transition | `IGame.IsGameOver` | yes, state only | no `Events` callback | detect transition in the existing 250 ms scheduler and finalize again on shutdown |
| Winner/draw | no winner API | derived only | API exposes game-over/alive/team state, not authoritative result object | infer only when exactly one alive team/player remains; store `result_source=inferred_alive_state`, otherwise unknown |
| Round player result | user totals, player statistics, alive/team state | partial | no authoritative per-round score/winner object | raw end checkpoints; derived views later |
| Player position/velocity | `GetWorldPosition()`, `GetLinearVelocity()` | yes | sampled, not continuous | 1 Hz persistent baseline; 4 Hz bounded RAM ring |
| Facing/aim | `FacingDirection`, `AimVector` | yes | aim vector may be zero/not applicable | state/event context |
| HP/energy | `GetHealth()`, `GetMaxHealth()`, `GetEnergy()`, `GetMaxEnergy()` | yes | death callback timing can expose zero/post-hit state | snapshot before/after only where actually observable |
| Movement/action flags | `IPlayer.IsBlocking`, `IsWalking`, `IsRunning`, `IsSprinting`, `IsCrouching`, `IsFalling`, `IsDiving`, `IsRolling`, `IsGrabbing`, `IsThrowing`, `IsStunned`, `IsTakingCover`, `IsDrawingWeapon`, `IsHipFiring`, `IsReloading`, and other documented flags | yes | instantaneous sampled state | compact baseline/high-resolution snapshots |
| Weapon loadout/ammo | current ranged/melee/thrown/powerup structs on `IPlayer` | yes | structs differ by slot; absent slot uses `WeaponItem.NONE`/default | state and event context |
| Damage | `StartPlayerDamageCallback(Action<IPlayer, PlayerDamageArgs>)` | yes | damage args are post-event and expose no HP-before value | store victim, amount/type/source and observed HP-after |
| Damage source | `PlayerDamageArgs.SourceID` | partial | projectile ID for Projectile, player ID for Melee, object ID for Missile, opaque explosion ID for Explosion, script-defined for Other | resolve only according to documented type |
| Projectile owner | `IProjectile.OwnerPlayerID`, `InitialOwnerPlayerID` | yes | current owner resets on bounce/deflection | capture both on projectile callbacks |
| Projectile created/hit | `StartProjectileCreatedCallback`, `StartProjectileHitCallback` | yes | hit callback gives projectile plus hit args; target may no longer exist | configurable, default enabled |
| Generic object created/damaged/terminated | corresponding object callbacks | yes | very noisy on destructible maps and not normally needed for player analytics | supported behind `ENABLE_OBJECT_EVENTS`, default off |
| Explosion hit | `StartExplosionHitCallback(Action<IExplosionData, ExplosionHitArg[]>)` | yes | explosion origin data available, but no persistent explosion object | store one event with hit list |
| Melee action/hits | `StartPlayerMeleeActionCallback(Action<IPlayer, PlayerMeleeHitArg[]>)` | yes | action type inferred from current `IsKicking`; empty hit list is still an action | store exact hit list and current flags |
| Weapon added/removed | corresponding player callbacks and args | yes | no dedicated weapon-switch callback | store inventory changes; switches observed in state/input |
| Reload/shot/empty attempt/block/grab/roll counters | `IPlayerStatistics` | yes | no direct callbacks for every action; some can be paired with key transitions/current state | full counter checkpoints and optional input events |
| Complete player statistics | `IPlayer.Statistics` | yes | exactly 23 canonical 1.6 properties; legacy typo alias `TotalEmptyGunsFireAttemts` duplicates `...Attempts` and is not stored twice | baseline, death, round end, leave raw JSON + deltas offline |
| Assist | no assist API | no authoritative value | damage history can support a configurable derived definition later | do not emit “assist” from the SFD script |
| Nearest players/world context | positions/team in samples | derived later | live full-world scans would add cost | calculate offline from synchronized state samples |
| Kick/ban/moderation | Server Tool has in-memory `Kick`, `BanAdded`, `BanRemoved` notifications and UI lists | not in ScriptAPI | no extension callback and no persistent Logs-file API; leave callback does not distinguish them | unavailable to this non-invasive collector; document gap |
| Server Tool Logs tab | in-memory `listViewLog`, `DSInfo.MessageLog` | UI only | local help calls it “certain key events”; implementation has no file logger for this list | not scraped or injected |
| Chat log file | `LOG_CHAT=1`, `LOG_CHAT_FOLDER`, `LOG_CHAT_LEVEL` | optional server feature | separate from Logs tab; contains chat/server/script chat messages, not general `WriteToConsole` output | not used as telemetry transport |
| `Game.WriteToConsole` | `IGame.WriteToConsole`, internal `ConsoleOutputType.Script` | yes, console only | routes to the in-memory SFD console; no documented durable server-log file; Server Tool only has a file writer for chat logs | diagnostics only, never the telemetry transport |
| Server stdout/stderr | GUI Server Tool process | not a telemetry source | no console protocol; redirecting process streams does not expose `ConsoleOutputType.Script` | unsupported; do not hook process internals |
| Automatic extension start | Server Tool Scripts tab / `HOST_GAME_ENABLED_SCRIPTS`; CLI `"Superfighters Deluxe Server.exe" -start` | yes | script must be installed and enabled once; scripts may be skipped while waiting if configured | installation instructions and optional all-in-one launcher |
| Durable script export | `IGame.GetSharedStorage("sfdtelemetry_v1")` | yes | practical storage limit about 10 MB; SFD rewrites an internal UTF-8 text store asynchronously | bounded rotating spool only; collector tails snapshots and deduplicates by session+sequence |
| Shared-storage disk path | local 1.6 `GameWorld.GetStorage` implementation | yes | Windows default is user Documents path | `Cache\ScriptData\Shared\CCC.txt` |
| Raw transport integrity | `IScriptStorage.SetItem(string, string[])` | yes | SFD storage escaping is ambiguous for literal backslashes (`\n`, `\r`) | Base64-encode UTF-8 `SFDTELEMETRY_V1|{json}` per spool entry; decode externally |

## Identity boundary

The only persistent ScriptAPI account value is `AccountID`, a host-specific salted
hash. It is suitable for repeat visits to the same server installation, but it is
not a SteamID and cannot be correlated across server owners. `ConnectionIP` is
explicitly fake. `UserID`/`UserId` are merely the session `UserIdentifier` in the
local 1.6 implementation. Bots and editor `SF...` test identities never create a
persistent player identity.

## Export decision

The collector does not tail the Server Tool Logs tab and does not use public chat.
The extension writes a bounded rotating spool to SFD shared storage. Each stored
value is Base64 transport data; after decoding it is exactly one line with the
prefix `SFDTELEMETRY_V1|` followed by a single-line JSON envelope. SQLite and the
optional raw JSONL archive remain the durable stores.
