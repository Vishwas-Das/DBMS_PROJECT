"""Capture the benchmark environment (host, software, database) without any secrets."""

from __future__ import annotations

import ctypes
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

import psycopg

REPO = Path(__file__).resolve().parents[1]
PG_SETTINGS = (
    "shared_buffers",
    "work_mem",
    "maintenance_work_mem",
    "effective_cache_size",
    "max_connections",
    "synchronous_commit",
    "fsync",
    "random_page_cost",
    "jit",
    "default_transaction_isolation",
)
PACKAGES = (
    "lae",
    "sqlalchemy",
    "psycopg",
    "sqlglot",
    "networkx",
    "alembic",
    "fastapi",
    "pydantic",
    "cryptography",
)


def _run(cmd: list[str], *, strip: bool = True) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd, capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() if strip else out.stdout


def _cpu_model() -> str:
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as k:
                return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    if sys.platform == "darwin":
        return _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor()
    return platform.processor() or "unknown"


def _ram_bytes() -> int | None:
    if sys.platform == "win32":

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MemoryStatus()
        st.dwLength = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):  # type: ignore[attr-defined]
            return int(st.ullTotalPhys)
        return None
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    return None


def _physical_cores() -> int | None:
    if sys.platform == "win32":
        out = _run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_Processor | Measure-Object NumberOfCores -Sum).Sum",
            ]
        )
        return int(out) if out and out.isdigit() else None
    out = _run(["sh", "-c", "lscpu -p=core,socket | grep -v '^#' | sort -u | wc -l"])
    return int(out) if out and out.isdigit() and int(out) > 0 else None


def _git() -> dict[str, Any]:
    commit = _run(["git", "-C", str(REPO), "rev-parse", "HEAD"])
    # porcelain lines are "XY path": never strip, the leading status column is significant
    status = _run(["git", "-C", str(REPO), "status", "--porcelain"], strip=False)
    return {
        "git_commit": commit,
        "git_dirty": bool(status),
        "git_dirty_paths": sorted({line[3:] for line in (status or "").splitlines()})[:50],
    }


def _docker() -> dict[str, Any]:
    exe = "docker"
    win_default = Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")
    if sys.platform == "win32" and win_default.exists():
        exe = str(win_default)
    version = _run([exe, "version", "--format", "{{.Server.Version}}"])
    info = _run([exe, "info", "--format", "{{.NCPU}}|{{.MemTotal}}|{{.OperatingSystem}}"])
    out: dict[str, Any] = {"docker_server_version": version}
    if info and info.count("|") == 2:
        ncpu, mem, osname = info.split("|")
        out.update(docker_ncpu=int(ncpu), docker_mem_bytes=int(mem), docker_os=osname)
    return out


def _packages() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for p in PACKAGES:
        try:
            out[p] = metadata.version(p)
        except metadata.PackageNotFoundError:
            out[p] = None
    return out


def database_environment(url: str) -> dict[str, Any]:
    """Server version, extension versions and the settings that matter (no credentials)."""
    with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT version(), current_setting('server_version')")
        full, short = cur.fetchone() or ("", "")
        cur.execute("SELECT extname, extversion FROM pg_extension ORDER BY extname")
        ext = {str(n): str(v) for n, v in cur.fetchall()}
        settings = {}
        for name in PG_SETTINGS:
            cur.execute("SELECT current_setting(%s, true)", (name,))
            row = cur.fetchone()
            settings[name] = row[0] if row else None
        cur.execute("SELECT current_database()")
        db = (cur.fetchone() or ("",))[0]
    return {
        "postgres_version": short,
        "postgres_version_full": full,
        "pgvector_version": ext.get("vector"),
        "pgcrypto_version": ext.get("pgcrypto"),
        "extensions": ext,
        "pg_settings": settings,
        "database": db,
    }


def capture(url: str) -> dict[str, Any]:
    env: dict[str, Any] = {
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "os": platform.platform(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "cpu_model": _cpu_model(),
        "logical_cores": os.cpu_count(),
        "physical_cores": _physical_cores(),
        "ram_bytes": _ram_bytes(),
        "python": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "packages": _packages(),
    }
    env.update(_git())
    env.update(_docker())
    env.update(database_environment(url))
    return env
