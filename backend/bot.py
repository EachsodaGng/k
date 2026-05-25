"""Discord bot: /add, /list, /remove. Monitors Roblox Creator Store audio by artist name."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from motor.motor_asyncio import AsyncIOMotorDatabase

from audio_processor import make_tmp_dir, process_audio_bytes
from roblox_client import RobloxClient

logger = logging.getLogger("robloxbot")

# Discord normal upload limit (8MB). Skip attaching above this.
DISCORD_UPLOAD_MAX = 8 * 1024 * 1024

# How many audios to ingest on initial /add (most-recent first)
INITIAL_INGEST_LIMIT = 30

# How many to scan each poll cycle per artist
POLL_PAGE_LIMIT = 30


class RobloxArtistBot(discord.Client):
    def __init__(self, db: AsyncIOMotorDatabase, poll_interval: int, concurrency: int):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.db = db
        self.poll_interval = poll_interval
        self.concurrency = concurrency
        self.session: Optional[aiohttp.ClientSession] = None
        self.roblox: Optional[RobloxClient] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._post_sem = asyncio.Semaphore(4)  # Discord posting concurrency

    async def setup_hook(self):
        self.session = aiohttp.ClientSession(headers={"User-Agent": "RobloxArtistMonitorBot/1.0"})
        self.roblox = RobloxClient(self.session, concurrency=self.concurrency)
        register_commands(self.tree, self)
        # Sync slash commands globally
        try:
            synced = await self.tree.sync()
            logger.info("Synced %d slash commands globally", len(synced))
        except Exception as e:  # noqa: BLE001
            logger.warning("Slash sync failed: %s", e)
        self._poll_task = asyncio.create_task(self.poll_loop(), name="poll_loop")

    async def close(self):
        if self._poll_task:
            self._poll_task.cancel()
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self):
        logger.info("Bot ready as %s (id=%s) in %d guilds", self.user, self.user.id if self.user else "?", len(self.guilds))
        # Per-guild sync so commands appear instantly (global sync can take up to 1h)
        for g in self.guilds:
            try:
                self.tree.copy_global_to(guild=g)
                synced = await self.tree.sync(guild=g)
                logger.info("Synced %d cmds to guild %s (%s)", len(synced), g.name, g.id)
            except Exception as e:  # noqa: BLE001
                logger.warning("Per-guild sync failed for %s: %s", g.id, e)

    async def on_guild_join(self, guild: discord.Guild):
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Joined guild %s; commands synced", guild.id)
        except Exception as e:  # noqa: BLE001
            logger.warning("on_guild_join sync failed: %s", e)

    # ---------------- DB helpers ----------------

    async def add_monitor(self, guild_id: str, artist_name: str, channel_id: str) -> str:
        norm = artist_name.strip()
        key = norm.lower()
        existing = await self.db.monitors.find_one({"guild_id": guild_id, "artist_key": key})
        if existing:
            await self.db.monitors.update_one(
                {"_id": existing["_id"]},
                {"$set": {"channel_id": channel_id, "artist_name": norm}},
            )
            return "updated"
        await self.db.monitors.insert_one({
            "guild_id": guild_id,
            "artist_key": key,
            "artist_name": norm,
            "channel_id": channel_id,
            "added_at": datetime.now(timezone.utc).isoformat(),
        })
        return "added"

    async def remove_monitor(self, guild_id: str, artist_name: str) -> bool:
        res = await self.db.monitors.delete_one({"guild_id": guild_id, "artist_key": artist_name.lower()})
        return res.deleted_count > 0

    async def list_monitors(self, guild_id: str) -> list[dict]:
        cur = self.db.monitors.find({"guild_id": guild_id}, {"_id": 0})
        return await cur.to_list(length=2000)

    async def mark_processed(self, guild_id: str, artist_key: str, asset_id: int):
        await self.db.processed.update_one(
            {"guild_id": guild_id, "artist_key": artist_key, "asset_id": asset_id},
            {"$setOnInsert": {"processed_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True,
        )

    async def is_processed(self, guild_id: str, artist_key: str, asset_id: int) -> bool:
        doc = await self.db.processed.find_one(
            {"guild_id": guild_id, "artist_key": artist_key, "asset_id": asset_id},
            {"_id": 1},
        )
        return doc is not None

    # ---------------- Core processing ----------------

    async def process_and_post(self, channel: discord.abc.Messageable, asset: dict, artist_query: str) -> bool:
        """Fetch audio, generate waveform + LUFS, post to channel. Returns True on success."""
        a = asset.get("asset") or {}
        creator = asset.get("creator") or {}
        asset_id = a.get("id")
        if not asset_id:
            return False
        title = a.get("name") or "Untitled"
        details = a.get("audioDetails") or {}
        artist = details.get("artist") or artist_query
        duration = a.get("duration") or 0
        created = a.get("createdUtc") or ""
        creator_name = creator.get("name") or "?"
        genre = details.get("musicGenre") or "—"

        thumb_task = asyncio.create_task(self.roblox.get_thumbnail_url(asset_id))
        ogg_bytes = await self.roblox.download_audio(asset_id)
        if ogg_bytes is None:
            # Transient failure → don't post, caller will not mark processed → retry next cycle
            logger.warning("Skip post: transient download failure for %s", asset_id)
            try:
                await thumb_task
            except Exception:  # noqa: BLE001
                pass
            return False
        if ogg_bytes == b"":
            # Permanent failure (restricted/deleted) → return "ok" so caller marks as processed
            logger.info("Permanent-skip restricted asset %s", asset_id)
            try:
                await thumb_task
            except Exception:  # noqa: BLE001
                pass
            return True

        tmp = make_tmp_dir()
        try:
            processed = await process_audio_bytes(ogg_bytes, tmp, asset_id)
            thumb_url = await thumb_task

            embed = discord.Embed(
                title=title[:256],
                url=f"https://create.roblox.com/store/asset/{asset_id}",
                description=(
                    f"**Artist:** {artist}\n"
                    f"**Uploader:** {creator_name}\n"
                    f"**Duration:** {int(duration)//60}:{int(duration)%60:02d}\n"
                    f"**Genre:** {genre}\n"
                    f"**Asset ID:** `{asset_id}`\n"
                    f"**LUFS:** `{processed.lufs.short() if processed else 'N/A'}`\n"
                    f"**Size:** {len(ogg_bytes)/1024:.1f} KB"
                ),
                color=0x5865F2,
                timestamp=datetime.now(timezone.utc),
            )
            if thumb_url:
                embed.set_thumbnail(url=thumb_url)
            if created:
                embed.set_footer(text=f"Created {created[:10]}")

            files: list[discord.File] = []
            if processed and processed.waveform_path and os.path.exists(processed.waveform_path):
                files.append(discord.File(processed.waveform_path, filename=f"{asset_id}_waveform.png"))
                embed.set_image(url=f"attachment://{asset_id}_waveform.png")

            if len(ogg_bytes) <= DISCORD_UPLOAD_MAX:
                safe_title = "".join(c if c.isalnum() or c in "._- " else "_" for c in title)[:60].strip() or str(asset_id)
                files.append(discord.File(processed.ogg_path if processed else _write_tmp(ogg_bytes, tmp, asset_id),
                                          filename=f"{safe_title}.ogg"))
            else:
                embed.add_field(
                    name="Download",
                    value=f"[Direct OGG]({'https://assetdelivery.roblox.com/v1/asset/?id=' + str(asset_id)}) "
                          f"(file too large to attach: {len(ogg_bytes)/1024/1024:.1f} MB)",
                    inline=False,
                )

            async with self._post_sem:
                await channel.send(embed=embed, files=files)
            return True
        except Exception as e:  # noqa: BLE001
            logger.exception("post failed for %s: %s", asset_id, e)
            return False
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def scan_artist(self, guild_id: str, artist_name: str, channel_id: str,
                          limit: int, announce_summary: discord.abc.Messageable | None = None) -> int:
        """Scan an artist's most-recent audios, post any not yet processed."""
        artist_key = artist_name.lower()
        ids = await self.roblox.search_audio_by_artist(artist_name, limit=limit)
        if not ids:
            if announce_summary:
                await announce_summary.send(f"No audios found on Roblox for **{artist_name}**.")
            return 0

        # Filter unseen
        unseen: list[int] = []
        for aid in ids:
            if not await self.is_processed(guild_id, artist_key, aid):
                unseen.append(aid)
        if not unseen:
            if announce_summary:
                await announce_summary.send(f"Already up to date for **{artist_name}** ({len(ids)} audios checked).")
            return 0

        details = await self.roblox.get_audio_details(unseen)
        # Strict artist filter (keyword search can hit titles/descriptions too)
        target = artist_key
        details = [
            d for d in details
            if ((d.get("asset") or {}).get("audioDetails") or {}).get("artist", "").strip().lower() == target
        ]
        # Match details by id (preserve "recent-first" order from search)
        by_id = {d.get("asset", {}).get("id"): d for d in details}
        ordered = [by_id[i] for i in unseen if i in by_id]

        if not ordered:
            if announce_summary:
                await announce_summary.send(
                    f"Found {len(ids)} keyword match(es) for **{artist_name}** but none have that exact artist tag. "
                    f"Check the exact spelling on create.roblox.com/store/audio?artistName={artist_name}."
                )
            return 0

        channel = self.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.fetch_channel(int(channel_id))
            except Exception:  # noqa: BLE001
                logger.warning("Channel %s not accessible", channel_id)
                return 0

        # Post oldest-of-new first so newest ends up last in chat
        posted = 0
        for asset in reversed(ordered):
            ok = await self.process_and_post(channel, asset, artist_name)
            aid = asset.get("asset", {}).get("id")
            if ok and aid:
                # Only mark processed on success so transient failures retry next cycle
                await self.mark_processed(guild_id, artist_key, int(aid))
                posted += 1
        return posted

    async def poll_loop(self):
        await self.wait_until_ready()
        logger.info("Polling loop started, interval=%ss", self.poll_interval)
        while not self.is_closed():
            try:
                monitors = await self.db.monitors.find({}, {"_id": 0}).to_list(length=10000)
                if monitors:
                    # Fan out one task per monitor; RobloxClient semaphore caps concurrency
                    tasks = [
                        self.scan_artist(m["guild_id"], m["artist_name"], m["channel_id"], POLL_PAGE_LIMIT)
                        for m in monitors
                    ]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    new_total = sum(r for r in results if isinstance(r, int))
                    if new_total:
                        logger.info("Poll cycle: %d new audios across %d monitors", new_total, len(monitors))
            except Exception as e:  # noqa: BLE001
                logger.exception("poll cycle error: %s", e)
            await asyncio.sleep(self.poll_interval)


def _write_tmp(data: bytes, tmp_dir: str, asset_id: int) -> str:
    p = os.path.join(tmp_dir, f"{asset_id}.ogg")
    if not os.path.exists(p):
        with open(p, "wb") as f:
            f.write(data)
    return p


def register_commands(tree: app_commands.CommandTree, bot: RobloxArtistBot):

    @tree.command(name="add", description="Monitor a Roblox artist (Creator Store). Posts every audio to the channel.")
    @app_commands.describe(
        artist="Exact Roblox artistName (as in create.roblox.com/store/audio?artistName=...)",
        channel="Channel to post audios in",
    )
    async def add_cmd(interaction: discord.Interaction, artist: str, channel: discord.TextChannel):
        if interaction.guild_id is None:
            await interaction.response.send_message("Run this inside a server.", ephemeral=True)
            return
        # Permission check: require Manage Channels
        if not interaction.user.guild_permissions.manage_channels:  # type: ignore[union-attr]
            await interaction.response.send_message("You need **Manage Channels** to use this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        action = await bot.add_monitor(str(interaction.guild_id), artist, str(channel.id))
        await interaction.followup.send(
            f"{'Added' if action == 'added' else 'Updated'} **{artist}** → {channel.mention}.\n"
            f"Scanning current audios now (up to {INITIAL_INGEST_LIMIT})…",
            ephemeral=True,
        )
        posted = await bot.scan_artist(
            str(interaction.guild_id), artist, str(channel.id),
            limit=INITIAL_INGEST_LIMIT,
        )
        try:
            await interaction.followup.send(
                f"Initial scan complete for **{artist}** — posted **{posted}** audio(s) to {channel.mention}. "
                f"New uploads will be auto-posted as they appear.",
                ephemeral=True,
            )
        except Exception:  # noqa: BLE001
            pass

    @tree.command(name="list", description="List monitored Roblox artists for this server.")
    async def list_cmd(interaction: discord.Interaction):
        if interaction.guild_id is None:
            await interaction.response.send_message("Run this inside a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await bot.list_monitors(str(interaction.guild_id))
        if not rows:
            await interaction.followup.send("No artists monitored yet. Use `/add` to start.", ephemeral=True)
            return
        # Group by channel
        lines = []
        for r in sorted(rows, key=lambda x: x["artist_name"].lower()):
            lines.append(f"• **{r['artist_name']}** → <#{r['channel_id']}>")
        content = "**Monitored Roblox artists ({}):**\n".format(len(rows)) + "\n".join(lines)
        # Chunk if too long
        for chunk in _chunks(content, 1900):
            await interaction.followup.send(chunk, ephemeral=True)

    @tree.command(name="remove", description="Stop monitoring a Roblox artist.")
    @app_commands.describe(artist="Exact artistName previously added")
    async def remove_cmd(interaction: discord.Interaction, artist: str):
        if interaction.guild_id is None:
            await interaction.response.send_message("Run this inside a server.", ephemeral=True)
            return
        if not interaction.user.guild_permissions.manage_channels:  # type: ignore[union-attr]
            await interaction.response.send_message("You need **Manage Channels** to use this.", ephemeral=True)
            return
        ok = await bot.remove_monitor(str(interaction.guild_id), artist)
        await interaction.response.send_message(
            f"{'Removed' if ok else 'Not found'}: **{artist}**", ephemeral=True
        )

    @tree.command(name="status", description="Bot health + monitor counts.")
    async def status_cmd(interaction: discord.Interaction):
        total = await bot.db.monitors.count_documents({})
        guild_total = await bot.db.monitors.count_documents({"guild_id": str(interaction.guild_id)}) if interaction.guild_id else 0
        processed = await bot.db.processed.count_documents({})
        await interaction.response.send_message(
            f"**Roblox Artist Monitor**\n"
            f"Monitors total: `{total}` | This server: `{guild_total}`\n"
            f"Audios processed (all-time): `{processed}`\n"
            f"Poll interval: `{bot.poll_interval}s`",
            ephemeral=True,
        )


def _chunks(text: str, n: int):
    for i in range(0, len(text), n):
        yield text[i:i + n]


async def start_bot(db: AsyncIOMotorDatabase, token: str, poll_interval: int, concurrency: int) -> RobloxArtistBot:
    bot = RobloxArtistBot(db=db, poll_interval=poll_interval, concurrency=concurrency)
    asyncio.create_task(bot.start(token), name="discord_bot")
    return bot
