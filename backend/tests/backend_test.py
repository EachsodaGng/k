"""Backend tests: HTTP endpoints + RobloxClient + audio_processor + Mongo indexes/uniqueness."""
import os
import asyncio
import pytest
import requests
import aiohttp
from pathlib import Path
from dotenv import load_dotenv

# Load backend .env so MONGO_URL/DB_NAME are available
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from roblox_client import RobloxClient  # noqa: E402
from audio_processor import process_audio_bytes, make_tmp_dir  # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402
from pymongo.errors import DuplicateKeyError  # noqa: E402

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://instant-audio-vault.preview.emergentagent.com").rstrip("/")

# ---------------- HTTP endpoints ----------------

class TestHttpEndpoints:
    def test_root(self):
        r = requests.get(f"{BASE_URL}/api/", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data == {"service": "roblox-artist-monitor", "ok": True}

    def test_status(self):
        r = requests.get(f"{BASE_URL}/api/status", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert "bot" in d and "monitors_total" in d and "processed_total" in d
        assert isinstance(d["monitors_total"], int)
        assert isinstance(d["processed_total"], int)
        b = d["bot"]
        assert b["ready"] is True, f"bot not ready: {b}"
        assert b["user"], "bot.user empty"
        assert isinstance(b["guilds"], int) and b["guilds"] >= 1, f"guilds={b['guilds']}"

    def test_monitors_shape(self):
        r = requests.get(f"{BASE_URL}/api/monitors", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert "guilds" in d and isinstance(d["guilds"], list)
        assert "total_guilds" in d and isinstance(d["total_guilds"], int)


# ---------------- Roblox client + audio pipeline ----------------

@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="module")
async def roblox_ctx():
    session = aiohttp.ClientSession(headers={"User-Agent": "RobloxArtistMonitorBot/1.0"})
    client = RobloxClient(session, concurrency=10)
    yield client
    await session.close()


@pytest.mark.asyncio
async def test_search_audio_by_artist():
    async with aiohttp.ClientSession(headers={"User-Agent": "RobloxArtistMonitorBot/1.0"}) as session:
        client = RobloxClient(session, concurrency=5)
        ids = await client.search_audio_by_artist("Kevin MacLeod", limit=5)
        assert isinstance(ids, list) and len(ids) > 0, f"got {ids}"
        assert all(isinstance(x, int) for x in ids)
        # stash for next test via module attr
        pytest._kml_ids = ids  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_get_audio_details():
    ids = getattr(pytest, "_kml_ids", None)
    assert ids, "search test must run first"
    async with aiohttp.ClientSession(headers={"User-Agent": "RobloxArtistMonitorBot/1.0"}) as session:
        client = RobloxClient(session, concurrency=5)
        details = await client.get_audio_details(ids)
        assert details and len(details) > 0
        item = details[0]
        a = item.get("asset") or {}
        assert "id" in a and "name" in a
        assert "audioDetails" in a
        assert "artist" in (a.get("audioDetails") or {})
        creator = item.get("creator") or {}
        assert "name" in creator


@pytest.mark.asyncio
async def test_get_thumbnail_url():
    ids = getattr(pytest, "_kml_ids", None)
    assert ids
    async with aiohttp.ClientSession(headers={"User-Agent": "RobloxArtistMonitorBot/1.0"}) as session:
        client = RobloxClient(session, concurrency=5)
        url = await client.get_thumbnail_url(ids[0])
        assert isinstance(url, str) and url.startswith("http"), f"thumb url: {url}"
        assert "rbxcdn" in url or "roblox" in url


@pytest.mark.asyncio
async def test_download_audio_and_process():
    ids = getattr(pytest, "_kml_ids", None)
    assert ids
    async with aiohttp.ClientSession(headers={"User-Agent": "RobloxArtistMonitorBot/1.0"}) as session:
        client = RobloxClient(session, concurrency=5)
        # try each id until one downloads >100KB (some may be gated)
        chosen_id, ogg = None, None
        for aid in ids:
            data = await client.download_audio(aid)
            if data and len(data) > 100 * 1024:
                chosen_id, ogg = aid, data
                break
        assert ogg is not None, "no downloadable audio >100KB"
        assert len(ogg) > 100 * 1024

        tmp = make_tmp_dir()
        try:
            processed = await process_audio_bytes(ogg, tmp, chosen_id)
            assert processed is not None
            assert processed.waveform_path and os.path.exists(processed.waveform_path)
            assert os.path.getsize(processed.waveform_path) > 0
            lufs = processed.lufs
            assert lufs.integrated_lufs is not None and isinstance(lufs.integrated_lufs, float)
            assert lufs.lra_lu is not None and isinstance(lufs.lra_lu, float)
            assert lufs.true_peak_dbfs is not None and isinstance(lufs.true_peak_dbfs, float)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------- Mongo indexes ----------------

@pytest.mark.asyncio
async def test_mongo_indexes_and_uniqueness():
    mc = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = mc[os.environ["DB_NAME"]]

    # Ensure indexes exist (server lifespan should have created these on bot start)
    mon_idx = await db.monitors.index_information()
    proc_idx = await db.processed.index_information()

    def find_unique(idx_info, keys):
        for name, meta in idx_info.items():
            key = [(k, v) for k, v in meta.get("key", [])]
            if key == keys and meta.get("unique"):
                return name
        return None

    assert find_unique(mon_idx, [("guild_id", 1), ("artist_key", 1)]), f"monitors idx missing: {mon_idx}"
    assert find_unique(proc_idx, [("guild_id", 1), ("artist_key", 1), ("asset_id", 1)]), f"processed idx missing: {proc_idx}"

    # Uniqueness: inserting twice into monitors with same key MUST raise DuplicateKeyError
    test_guild = "TEST_GUILD_pytest"
    await db.monitors.delete_many({"guild_id": test_guild})
    try:
        await db.monitors.insert_one({"guild_id": test_guild, "artist_key": "kevin macleod",
                                      "artist_name": "Kevin MacLeod", "channel_id": "1"})
        with pytest.raises(DuplicateKeyError):
            await db.monitors.insert_one({"guild_id": test_guild, "artist_key": "kevin macleod",
                                          "artist_name": "Kevin MacLeod", "channel_id": "2"})
    finally:
        await db.monitors.delete_many({"guild_id": test_guild})
        mc.close()
