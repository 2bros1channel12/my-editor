"""Best-effort local hardware and media-tool capability profile."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
from typing import Any, Callable


def _run(command: list[str], *, runner: Callable[..., Any], timeout: float = 3.0) -> Any:
    return runner(command, capture_output=True, text=True, timeout=timeout, check=False)


def _memory_bytes() -> tuple[int | None, int | None, str]:
    try:
        import ctypes
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys), int(status.ullAvailPhys), "windows_api"
    except (AttributeError, OSError):
        pass
    try:
        if Path("/proc/meminfo").exists():
            values = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, value = line.split(":", 1)
                if key in {"MemTotal", "MemAvailable"}:
                    values[key] = int(value.strip().split()[0]) * 1024
            return values.get("MemTotal"), values.get("MemAvailable"), "proc_meminfo"
    except (OSError, ValueError, IndexError):
        pass
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        total = os.sysconf("SC_PHYS_PAGES") * page_size
        available = os.sysconf("SC_AVPHYS_PAGES") * page_size
        return int(total), int(available), "sysconf"
    except (ValueError, OSError, AttributeError):
        return None, None, "unknown"


def _cpu_flags(runner: Callable[..., Any]) -> tuple[str, str]:
    candidates = [Path("/proc/cpuinfo")]
    for path in candidates:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            match = re.search(r"^(?:flags|Features)\s*:\s*(.+)$", content, flags=re.MULTILINE)
            if match:
                return "proc_cpuinfo", match.group(1).split()
        except OSError:
            pass
    if sys.platform == "darwin":
        try:
            result = _run(["sysctl", "-n", "machdep.cpu.features"], runner=runner)
            if result.returncode == 0:
                return "sysctl", result.stdout.split()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return "unknown", []


def _gpu_info(runner: Callable[..., Any]) -> list[dict[str, Any]]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return []
    try:
        result = _run([executable, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], runner=runner)
        if result.returncode != 0:
            return []
        gpus = []
        for line in result.stdout.splitlines():
            fields = [value.strip() for value in line.split(",", 1)]
            if len(fields) == 2:
                try:
                    vram = int(fields[1]) * 1024 * 1024
                except ValueError:
                    vram = None
                gpus.append({"name": fields[0], "vendor": "NVIDIA", "dedicatedVramBytes": vram, "source": "nvidia_smi", "confidence": "reported"})
        return gpus
    except (OSError, subprocess.TimeoutExpired):
        return []


def _tool_profile(name: str, configured: str | None, runner: Callable[..., Any]) -> dict[str, Any]:
    executable = configured or shutil.which(name)
    if not executable:
        return {"available": False, "path": None, "version": None, "encoders": [], "decoders": [], "hwaccels": [], "errors": [f"{name} not found"]}
    profile: dict[str, Any] = {"available": True, "path": executable, "version": None, "encoders": [], "decoders": [], "hwaccels": [], "errors": []}
    try:
        result = _run([executable, "-version"], runner=runner)
        if result.returncode == 0:
            profile["version"] = result.stdout.splitlines()[0] if result.stdout else None
        else:
            profile["errors"].append(f"{name} version check failed")
    except (OSError, subprocess.TimeoutExpired):
        profile["available"] = False
        profile["errors"].append(f"{name} could not start")
        return profile
    if name == "ffmpeg":
        for option, key in (("-encoders", "encoders"), ("-decoders", "decoders"), ("-hwaccels", "hwaccels")):
            try:
                listing = _run([executable, option], runner=runner, timeout=4)
                text = (listing.stdout or "") + "\n" + (listing.stderr or "")
                if listing.returncode == 0:
                    profile[key] = _parse_ffmpeg_listing(text, key)
                else:
                    profile["errors"].append(f"{option} listing failed")
            except (OSError, subprocess.TimeoutExpired):
                profile["errors"].append(f"{option} listing unavailable")
    return profile


def _parse_ffmpeg_listing(text: str, kind: str) -> list[str]:
    values = []
    for line in text.splitlines():
        match = re.match(r"\s*[\w.]{6}\s+([\w.-]+)", line)
        if match:
            values.append(match.group(1))
        elif kind == "hwaccels" and line.strip() and not line.lower().startswith(("hardware", "------", "configuration", "ffmpeg")):
            if re.fullmatch(r"[\w.-]+", line.strip()):
                values.append(line.strip())
    return sorted(set(values))


def collect_capabilities(data_dir: Path, *, ffmpeg_path: str | None = None, ffprobe_path: str | None = None, runner: Callable[..., Any] = subprocess.run, memory_provider: Callable[[], tuple[int | None, int | None, str]] = _memory_bytes, gpu_provider: Callable[[Callable[..., Any]], list[dict[str, Any]]] = _gpu_info, platform_provider: Callable[[], Any] = lambda: platform) -> dict[str, Any]:
    total_ram, available_ram, memory_source = memory_provider()
    try:
        disk = shutil.disk_usage(data_dir)
        disk_profile = {"totalBytes": disk.total, "freeBytes": disk.free, "source": "local_storage", "confidence": "reported"}
    except OSError:
        disk_profile = {"totalBytes": None, "freeBytes": None, "source": "unknown", "confidence": "unknown"}
    flag_source, flags = _cpu_flags(runner)
    try:
        gpus = gpu_provider(runner)
    except Exception:
        gpus = []
    ffmpeg = _tool_profile("ffmpeg", ffmpeg_path, runner)
    ffprobe_sibling = None
    if ffmpeg_path:
        sibling_name = "ffprobe.exe" if platform_provider().system() == "Windows" else "ffprobe"
        ffprobe_sibling = str(Path(ffmpeg_path).with_name(sibling_name))
    ffprobe = _tool_profile("ffprobe", ffprobe_path or ffprobe_sibling, runner)
    cores = os.cpu_count()
    recommendations = _recommend(cores, total_ram, gpus, disk_profile["freeBytes"])
    current_platform = platform_provider()
    return {
        "schemaVersion": 1,
        "collectedAt": datetime.now(timezone.utc).isoformat(),
        "platform": {"system": current_platform.system(), "release": current_platform.release(), "machine": current_platform.machine(), "pythonBits": platform.architecture()[0]},
        "cpu": {"logicalCores": cores, "brand": current_platform.processor() or None, "flags": flags, "source": flag_source, "confidence": "reported" if flag_source != "unknown" else "unknown"},
        "memory": {"totalBytes": total_ram, "availableBytes": available_ram, "source": memory_source, "confidence": "reported" if total_ram is not None else "unknown"},
        "gpus": gpus,
        "mediaTools": {"ffmpeg": ffmpeg, "ffprobe": ffprobe, "encoderListingSemantics": "advertised by build; device/driver encoding has not been verified"},
        "disk": disk_profile,
        "recommendations": recommendations,
    }


def _recommend(cores: int | None, ram: int | None, gpus: list[dict[str, Any]], free_disk: int | None) -> dict[str, Any]:
    vram = max((gpu.get("dedicatedVramBytes") or 0 for gpu in gpus), default=0)
    if (ram or 0) >= 32 * 1024**3 and (cores or 0) >= 8 and vram >= 8 * 1024**3 and (free_disk or 0) >= 100 * 1024**3:
        return {"modelSizeClass": "large", "proxyPreset": "high", "confidence": "estimated"}
    if (ram or 0) >= 16 * 1024**3 and (cores or 0) >= 4 and (free_disk or 0) >= 25 * 1024**3:
        return {"modelSizeClass": "small", "proxyPreset": "balanced", "confidence": "estimated"}
    return {"modelSizeClass": "small", "proxyPreset": "low", "confidence": "conservative"}
