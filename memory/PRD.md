# Roblox Artist Discord Monitor — PRD

## Original problem statement
"make me a [bot] that monitors roblox artists when i use discord command /add make it scan and add all audios sent to discord with thumbnail ogg file download waveform lufs info and make sure it scans instant every artist i add use the command for new audios make it instant for over 1000 or more artists using discord token"

## Architecture
- FastAPI backend (supervisor-managed) runs the Discord bot inside the lifespan (asyncio task).
- discord.py 2.7 — slash commands /add, /list, /remove, /status, per-guild sync on ready for instant availability.
- Roblox Creator Store: `apis.roblox.com/toolbox-service/v1/marketplace/audio?artistName=...&sortType=2` (no auth) + details endpoint + assetdelivery for OGG.
- ffmpeg pipeline: `showwavespic` waveform PNG + `ebur128` integrated LUFS / LRA / true peak.
- MongoDB: `monitors` (guild_id+artist_key unique), `processed` (guild_id+artist_key+asset_id unique).
- Polling loop every 30s, asyncio.gather across all monitors, RobloxClient semaphore=25 → scales to 1000+ artists.
- Minimal React status page at `/` showing live bot/guild/monitor counts.

## Implemented (2026-02-25)
- /add artist channel — adds monitor, scans up to 30 most-recent audios, posts to chosen channel.
- /list — server-scoped list of monitored artists with channels.
- /remove artist — stop monitoring.
- /status — counts + poll interval.
- Discord post includes: embed (title, artist, uploader, duration, genre, asset ID, LUFS, size, thumbnail, creation date), attached waveform PNG, attached OGG file (≤8MB) or download link.
- Polling auto-posts new audios from existing monitors within ~30s.

## Backlog (P1/P2)
- P1: persistent OGG storage (S3/object storage) when files >8MB so users can still download.
- P1: dashboard with per-artist history (currently console-only).
- P2: per-artist polling interval override.
- P2: webhook posting instead of bot user, for higher message throughput.
