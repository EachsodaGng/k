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
            for attempt in range(6):
                try:
                    async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status == 200:
                            return await r.json()
                        if r.status == 429:
                            # Honor Retry-After when provided, else exponential up to 12s
                            retry = r.headers.get("Retry-After")
                            try:
                                wait = float(retry) if retry else (1.0 * (2 ** attempt))
                            except ValueError:
                                wait = 1.0 * (2 ** attempt)
                            wait = min(wait, 12.0)
                            await asyncio.sleep(wait)
                            continue
                        logger.warning("Roblox GET %s -> %s", url, r.status)
                        return None
                except Exception as e:  # noqa: BLE001
                    logger.warning("Roblox GET failed %s: %s", url, e)
                    return None
            logger.warning("Roblox GET %s -> 429 (gave up after 6 retries)", url)
            return None

    async def search_audio_by_artist(self, artist_name: str, limit: int = 30) -> list[int]:
        """Return list of asset IDs from the Creator Store filtered by artist keyword, most recent first.

        Note: the toolbox-service endpoint ignores `artistName` and only accepts `keyword`.
        Keyword can match title or description too, so callers must re-filter via details.
        """
        params = {
            "keyword": artist_name,
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
        """Download raw OGG bytes via assetdelivery (follows the CDN redirect).

        Returns bytes on success, b"" (empty) on permanent failure (401/403/404 → asset restricted/deleted),
        None on transient failure (timeout / 5xx) so the caller can retry next cycle.
        """
        async with self.sem:
            try:
                async with self.session.get(
                    ASSET_DELIVERY,
                    params={"id": str(asset_id)},
                    allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as r:
                    if r.status == 200:
                        return await r.read()
                    if r.status in (401, 403, 404, 410):
                        logger.warning("download_audio %s -> %s (permanent skip)", asset_id, r.status)
                        return b""
                    logger.warning("download_audio %s -> %s (will retry)", asset_id, r.status)
                    return None
            except Exception as e:  # noqa: BLE001
                logger.warning("download_audio failed %s: %s", asset_id, e)
                return None
