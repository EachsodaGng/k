"""FastAPI server that boots the Discord bot in background and exposes basic status."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI
from motor.motor_asyncio import AsyncIOMotorClient
from starlette.middleware.cors import CORSMiddleware

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

mongo_url = os.environ["MONGO_URL"]
mongo_client = AsyncIOMotorClient(mongo_url)
db = mongo_client[os.environ["DB_NAME"]]

# Bot handle, populated on startup
bot_state: dict = {"bot": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure indexes
    await db.monitors.create_index([("guild_id", 1), ("artist_key", 1)], unique=True)
    await db.processed.create_index([("guild_id", 1), ("artist_key", 1), ("asset_id", 1)], unique=True)
    await db.processed.create_index([("processed_at", -1)])

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if token:
        from bot import start_bot
        poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
        concurrency = int(os.environ.get("ROBLOX_CONCURRENCY", "25"))
        bot_state["bot"] = await start_bot(db, token, poll_interval, concurrency)
        logger.info("Discord bot launch task scheduled")
    else:
        logger.warning("DISCORD_BOT_TOKEN not set; bot disabled")

    yield

    bot = bot_state.get("bot")
    if bot:
        try:
            await bot.close()
        except Exception:  # noqa: BLE001
            pass
    mongo_client.close()


app = FastAPI(lifespan=lifespan)
api_router = APIRouter(prefix="/api")


@api_router.get("/")
async def root():
    return {"service": "roblox-artist-monitor", "ok": True}


@api_router.get("/status")
async def status():
    bot = bot_state.get("bot")
    monitors_total = await db.monitors.count_documents({})
    processed_total = await db.processed.count_documents({})
    guilds = 0
    user = None
    ready = False
    if bot is not None:
        try:
            guilds = len(bot.guilds)
            user = str(bot.user) if bot.user else None
            ready = bot.is_ready()
        except Exception:  # noqa: BLE001
            pass
    return {
        "bot": {
            "ready": ready,
            "user": user,
            "guilds": guilds,
            "poll_interval_seconds": int(os.environ.get("POLL_INTERVAL_SECONDS", "30")),
        },
        "monitors_total": monitors_total,
        "processed_total": processed_total,
    }


@api_router.get("/monitors")
async def list_monitors():
    """Aggregate count by guild (no secrets returned)."""
    pipeline = [
        {"$group": {"_id": "$guild_id", "artists": {"$addToSet": "$artist_name"}}},
        {"$project": {"_id": 0, "guild_id": "$_id", "artists": 1, "count": {"$size": "$artists"}}},
    ]
    rows = await db.monitors.aggregate(pipeline).to_list(length=1000)
    return {"guilds": rows, "total_guilds": len(rows)}


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
