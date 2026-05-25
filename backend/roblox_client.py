"""Roblox Creator Store audio search + asset helpers (no auth required)."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

TOOLBOX_SEARCH = "https://apis.roblox.com/toolbox-service/v1/marketplace/audio"
TOOLBOX_DETAILS = "https://apis.roblox.com/toolbox-service/v1/items/details"
THUMBNAILS_API = "https://thumbnails.roblox.com/v1/assets"
ASSET_DELIVERY = "https://assetdelivery.roblox.com/v1/asset/"

# sortType=2 → most recent first
DEFAULT_SORT = 2


class RobloxClient:
    def __init__(self, session: aiohttp.ClientSession, concurrency: int = 25):
        self.session = session
        self.sem = asyncio.Semaphore(concurrency)

    async def _get_json(self, url: str, params: dict | None = None) -> dict | None:
        async with self.sem:
            try:
                async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        logger.warning("Roblox GET %s -> %s", url, r.status)
                        return None
                    return await r.json()
            except Exception as e:  # noqa: BLE001
                logger.warning("Roblox GET failed %s: %s", url, e)
                return None

    async def search_audio_by_artist(self, artist_name: str, limit: int = 30) -> list[int]:
        """Return list of asset IDs from the Creator Store filtered by artistName, most recent first."""
        params = {
            "artistName": artist_name,
            "limit": str(limit),
            "sortType": str(DEFAULT_SORT),
        }
        data = await self._get_json(TOOLBOX_SEARCH, params=params)
        if not data:
            return []
        return [int(item["id"]) for item in data.get("data", []) if "id" in item]

    async def get_audio_details(self, asset_ids: list[int]) -> list[dict]:
        """Resolve details for a batch of asset IDs."""
        if not asset_ids:
            return []
        # API accepts comma-separated assetIds
        results: list[dict] = []
        # Chunk to avoid extra-long URLs
        for i in range(0, len(asset_ids), 50):
            chunk = asset_ids[i:i + 50]
            params = {"assetIds": ",".join(str(x) for x in chunk)}
            data = await self._get_json(TOOLBOX_DETAILS, params=params)
            if data and isinstance(data.get("data"), list):
                results.extend(data["data"])
        return results

    async def get_thumbnail_url(self, asset_id: int) -> Optional[str]:
        params = {
            "assetIds": str(asset_id),
            "size": "420x420",
            "format": "Png",
            "isCircular": "false",
        }
        data = await self._get_json(THUMBNAILS_API, params=params)
        if not data:
            return None
        items = data.get("data") or []
        if not items:
            return None
        url = items[0].get("imageUrl")
        if url and "noFilter" not in url:
            return url
        # noFilter means default placeholder; still return it
        return url

    async def download_audio(self, asset_id: int) -> Optional[bytes]:
        """Download raw OGG bytes via assetdelivery (follows the CDN redirect)."""
        async with self.sem:
            try:
                async with self.session.get(
                    ASSET_DELIVERY,
                    params={"id": str(asset_id)},
                    allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as r:
                    if r.status != 200:
                        logger.warning("download_audio %s -> %s", asset_id, r.status)
                        return None
                    return await r.read()
            except Exception as e:  # noqa: BLE001
                logger.warning("download_audio failed %s: %s", asset_id, e)
                return None
