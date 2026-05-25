"""ffmpeg-powered waveform image + EBU R128 (LUFS) analysis."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

EBU_I = re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?\d+\.\d+)\s*LUFS")
EBU_LRA = re.compile(r"Loudness range:\s*\n\s*LRA:\s*(-?\d+\.\d+)\s*LU")
EBU_TP = re.compile(r"True peak:\s*\n\s*Peak:\s*(-?\d+\.\d+)\s*dBFS")


@dataclass
class LufsReport:
    integrated_lufs: Optional[float]
    lra_lu: Optional[float]
    true_peak_dbfs: Optional[float]

    def short(self) -> str:
        parts = []
        if self.integrated_lufs is not None:
            parts.append(f"I: {self.integrated_lufs:.1f} LUFS")
        if self.lra_lu is not None:
            parts.append(f"LRA: {self.lra_lu:.1f} LU")
        if self.true_peak_dbfs is not None:
            parts.append(f"TP: {self.true_peak_dbfs:.1f} dBFS")
        return " | ".join(parts) if parts else "N/A"


async def _run(cmd: list[str], stdin_bytes: bytes | None = None) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=stdin_bytes)
    return proc.returncode or 0, stdout, stderr


async def analyze_lufs(ogg_path: str) -> LufsReport:
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", ogg_path,
        "-filter_complex", "ebur128=peak=true",
        "-f", "null", "-",
    ]
    rc, _, stderr = await _run(cmd)
    if rc != 0:
        logger.warning("ebur128 failed: %s", stderr.decode("utf-8", "ignore")[-400:])
        return LufsReport(None, None, None)
    text = stderr.decode("utf-8", "ignore")
    # Use the summary block (last occurrences)
    summary_idx = text.rfind("Summary:")
    body = text[summary_idx:] if summary_idx != -1 else text
    i = EBU_I.search(body)
    lra = EBU_LRA.search(body)
    tp = EBU_TP.search(body)
    return LufsReport(
        integrated_lufs=float(i.group(1)) if i else None,
        lra_lu=float(lra.group(1)) if lra else None,
        true_peak_dbfs=float(tp.group(1)) if tp else None,
    )


async def generate_waveform_png(ogg_path: str, out_path: str,
                                width: int = 800, height: int = 160,
                                color: str = "#5865F2") -> bool:
    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-i", ogg_path,
        "-filter_complex", f"showwavespic=s={width}x{height}:colors={color}",
        "-frames:v", "1",
        out_path,
    ]
    rc, _, stderr = await _run(cmd)
    if rc != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        logger.warning("waveform failed: %s", stderr.decode("utf-8", "ignore")[-300:])
        return False
    return True


@dataclass
class ProcessedAudio:
    ogg_path: str
    waveform_path: str
    lufs: LufsReport
    size_bytes: int


async def process_audio_bytes(ogg_bytes: bytes, tmp_dir: str, asset_id: int) -> Optional[ProcessedAudio]:
    """Write OGG to disk, generate waveform, analyze LUFS. Returns paths + report."""
    ogg_path = os.path.join(tmp_dir, f"{asset_id}.ogg")
    wave_path = os.path.join(tmp_dir, f"{asset_id}_wave.png")
    try:
        with open(ogg_path, "wb") as f:
            f.write(ogg_bytes)
        lufs_task = asyncio.create_task(analyze_lufs(ogg_path))
        wave_task = asyncio.create_task(generate_waveform_png(ogg_path, wave_path))
        lufs, ok = await asyncio.gather(lufs_task, wave_task)
        if not ok:
            wave_path = ""
        return ProcessedAudio(
            ogg_path=ogg_path,
            waveform_path=wave_path,
            lufs=lufs,
            size_bytes=len(ogg_bytes),
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("process_audio_bytes failed: %s", e)
        return None


def make_tmp_dir() -> str:
    return tempfile.mkdtemp(prefix="robloxbot_")
