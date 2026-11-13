"""Collecte de métriques système du VPS (RAM, Swap, CPU, disque, uptime)."""

from __future__ import annotations

import os
import platform
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import Settings


def format_duration(seconds: float) -> str:
    """Formate une durée en secondes sous forme lisible (ex: 1j 04h12m)."""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days > 0:
        return f"{days}j {hours:02d}h{minutes:02d}m"
    if hours > 0:
        return f"{hours}h{minutes:02d}m"
    if minutes > 0:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


@dataclass(frozen=True)
class SystemMetrics:
    """Instantané des ressources système du VPS et du processus."""

    hostname: str
    os_label: str
    python_version: str
    cpu_count: int
    load_avg: tuple[float, float, float] | None
    ram_total_mb: float | None
    ram_used_mb: float | None
    ram_available_mb: float | None
    swap_total_mb: float | None
    swap_used_mb: float | None
    bot_rss_mb: float | None
    disk_total_gb: float | None
    disk_used_gb: float | None
    disk_free_gb: float | None
    system_uptime_seconds: float | None
    db_size_kb: float | None


def _parse_proc_meminfo(meminfo_path: Path) -> dict[str, int]:
    """Lit /proc/meminfo et retourne les valeurs en KiB."""
    values: dict[str, int] = {}
    if not meminfo_path.is_file():
        return values
    for line in meminfo_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        parts = rest.strip().split()
        if parts and parts[0].isdigit():
            values[key.strip()] = int(parts[0])
    return values


def _parse_bot_rss_mb(status_path: Path) -> float | None:
    """Lit VmRSS dans /proc/self/status (en Mo)."""
    if not status_path.is_file():
        return None
    for line in status_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split(":", 1)[1].strip().split()
            if parts and parts[0].isdigit():
                return round(int(parts[0]) / 1024.0, 1)
    return None


def _parse_uptime_seconds(uptime_path: Path) -> float | None:
    """Lit la durée depuis démarrage OS dans /proc/uptime."""
    if not uptime_path.is_file():
        return None
    first = uptime_path.read_text(encoding="utf-8", errors="ignore").split()
    if first:
        try:
            return float(first[0])
        except ValueError:
            return None
    return None


def collect_system_metrics(
    db_path: Path | None = None,
    *,
    proc_dir: Path = Path("/proc"),
) -> SystemMetrics:
    """Collecte les métriques du serveur sans dépendance externe."""
    hostname = socket.gethostname()
    os_label = f"{platform.system()} {platform.machine()}"
    python_version = platform.python_version()
    cpu_count = os.cpu_count() or 1

    load_avg: tuple[float, float, float] | None = None
    if hasattr(os, "getloadavg"):
        try:
            la = os.getloadavg()
            load_avg = (round(la[0], 2), round(la[1], 2), round(la[2], 2))
        except OSError:
            load_avg = None

    mem = _parse_proc_meminfo(proc_dir / "meminfo")
    ram_total_mb = ram_used_mb = ram_available_mb = None
    swap_total_mb = swap_used_mb = None
    if "MemTotal" in mem:
        total_kb = mem["MemTotal"]
        avail_kb = mem.get("MemAvailable", mem.get("MemFree", 0))
        used_kb = max(0, total_kb - avail_kb)
        ram_total_mb = round(total_kb / 1024.0, 1)
        ram_available_mb = round(avail_kb / 1024.0, 1)
        ram_used_mb = round(used_kb / 1024.0, 1)
    if "SwapTotal" in mem:
        st_kb = mem["SwapTotal"]
        sf_kb = mem.get("SwapFree", 0)
        swap_total_mb = round(st_kb / 1024.0, 1)
        swap_used_mb = round(max(0, st_kb - sf_kb) / 1024.0, 1)

    bot_rss_mb = _parse_bot_rss_mb(proc_dir / "self" / "status")
    uptime_s = _parse_uptime_seconds(proc_dir / "uptime")

    disk_total_gb = disk_used_gb = disk_free_gb = None
    disk_target = db_path.parent if db_path is not None and db_path.parent.exists() else Path(".")
    try:
        usage = shutil.disk_usage(disk_target)
        gib = 1024 ** 3
        disk_total_gb = round(usage.total / gib, 1)
        disk_used_gb = round(usage.used / gib, 1)
        disk_free_gb = round(usage.free / gib, 1)
    except OSError:
        pass

    db_size_kb: float | None = None
    if db_path is not None and db_path.is_file():
        try:
            db_size_kb = round(db_path.stat().st_size / 1024.0, 1)
        except OSError:
            db_size_kb = None

    return SystemMetrics(
        hostname=hostname,
        os_label=os_label,
        python_version=python_version,
        cpu_count=cpu_count,
        load_avg=load_avg,
        ram_total_mb=ram_total_mb,
        ram_used_mb=ram_used_mb,
        ram_available_mb=ram_available_mb,
        swap_total_mb=swap_total_mb,
        swap_used_mb=swap_used_mb,
        bot_rss_mb=bot_rss_mb,
        disk_total_gb=disk_total_gb,
        disk_used_gb=disk_used_gb,
        disk_free_gb=disk_free_gb,
        system_uptime_seconds=uptime_s,
        db_size_kb=db_size_kb,
    )


def format_vps_status(
    metrics: SystemMetrics,
    *,
    bot_uptime_seconds: float,
    started_at_str: str,
    settings: "Settings",
    stats: dict[str, int],
) -> str:
    """Met en forme le rapport /status complet pour Telegram."""
    lines = [
        "📊 **État du secrétaire & VPS**",
        f"• **Bot actif depuis :** {format_duration(bot_uptime_seconds)} (démarré le {started_at_str})",
        f"• **Serveur :** `{metrics.hostname}` ({metrics.os_label}, Python {metrics.python_version})",
    ]
    if metrics.system_uptime_seconds is not None:
        lines.append(
            f"• **Uptime VPS :** {format_duration(metrics.system_uptime_seconds)}"
        )

    if metrics.ram_total_mb is not None and metrics.ram_used_mb is not None:
        pct = (
            round(metrics.ram_used_mb / metrics.ram_total_mb * 100, 1)
            if metrics.ram_total_mb > 0
            else 0.0
        )
        lines.append(
            f"• **RAM VPS :** {metrics.ram_used_mb:.0f} / {metrics.ram_total_mb:.0f} Mo "
            f"({pct:.1f} %, libre : {metrics.ram_available_mb:.0f} Mo)"
        )

    if metrics.swap_total_mb is not None and metrics.swap_used_mb is not None:
        if metrics.swap_total_mb > 0:
            lines.append(
                f"• **Swap :** {metrics.swap_used_mb:.0f} / {metrics.swap_total_mb:.0f} Mo"
            )
        else:
            lines.append("• **Swap :** non configuré (0 Mo)")

    if metrics.bot_rss_mb is not None:
        lines.append(f"• **RAM processus bot :** {metrics.bot_rss_mb:.1f} Mo")

    if metrics.load_avg is not None:
        l1, l5, l15 = metrics.load_avg
        lines.append(
            f"• **CPU / Charge (1/5/15m) :** {l1:.2f} / {l5:.2f} / {l15:.2f} ({metrics.cpu_count} vCPU)"
        )
    else:
        lines.append(f"• **CPU :** {metrics.cpu_count} cœur(s)")

    if metrics.disk_total_gb is not None and metrics.disk_used_gb is not None:
        dpct = (
            round(metrics.disk_used_gb / metrics.disk_total_gb * 100, 1)
            if metrics.disk_total_gb > 0
            else 0.0
        )
        lines.append(
            f"• **Disque :** {metrics.disk_used_gb:.1f} / {metrics.disk_total_gb:.1f} Go "
            f"({dpct:.1f} %, libre : {metrics.disk_free_gb:.1f} Go)"
        )

    db_info = f" ({metrics.db_size_kb:.1f} Ko)" if metrics.db_size_kb is not None else ""
    lines.extend(
        [
            "",
            "📬 **Activité messagerie**",
            f"• **Modèle IA :** `{settings.gemini_model}`",
            f"• **Mails analysés en base :** {stats['total_processed']}{db_info}",
            f"• **En attente du récap ({settings.digest_hour:02d}h{settings.digest_minute:02d}) :** {stats['pending_digest']}",
        ]
    )
    return "\n".join(lines)
