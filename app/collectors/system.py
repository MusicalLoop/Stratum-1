"""
collectors/system.py — Raspberry Pi system metrics collector.

Collects hardware and OS metrics using psutil and /sys filesystem reads.
No external commands required — all data comes from Python stdlib or psutil.

Provides:
    collect()           — gather all system metrics, return combined dict
    read_cpu_temp()     — CPU temperature from /sys/class/thermal
    read_cpu()          — CPU usage per-core and aggregate
    read_memory()       — memory usage
    read_disk()         — disk usage for root partition
    read_load()         — system load averages
    read_uptime()       — system uptime in seconds
    read_network()      — network interface stats
    read_services()     — systemd service status for chronyd and gpsd

All values use consistent units documented in the returned dicts.
All functions handle missing data gracefully — returns None rather than
raising exceptions, so a single failing read does not block the poller.

Usage (standalone test):
    python3 collectors/system.py
"""

import os
import sys
import time
import logging
import subprocess

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import psutil
except ImportError:
    psutil = None
    logger.error("psutil not installed — system metrics unavailable. "
                 "Run: pip install psutil")

# ── CPU temperature ───────────────────────────────────────────────────────────

# Thermal zone paths to try in order — Pi 5 uses thermal_zone0
_THERMAL_PATHS = [
    "/sys/class/thermal/thermal_zone0/temp",
    "/sys/class/thermal/thermal_zone1/temp",
]

def read_cpu_temp():
    """
    Read CPU temperature from the sysfs thermal zone.

    Returns:
        float — temperature in degrees Celsius, or None if unavailable.

    The sysfs value is in millidegrees Celsius (e.g. 54321 = 54.321°C).
    """
    for path in _THERMAL_PATHS:
        try:
            with open(path) as f:
                millideg = int(f.read().strip())
            return round(millideg / 1000.0, 1)
        except (OSError, ValueError):
            continue

    # psutil fallback (less reliable on Pi)
    if psutil:
        try:
            temps = psutil.sensors_temperatures()
            for key in ("cpu_thermal", "cpu-thermal", "coretemp"):
                if key in temps and temps[key]:
                    return round(temps[key][0].current, 1)
        except Exception:
            pass

    return None


# ── CPU usage ─────────────────────────────────────────────────────────────────

def read_cpu():
    """
    Read CPU usage statistics.

    Returns a dict:
        usage_pct       float — aggregate CPU usage percentage (0-100)
        per_core_pct    list  — usage percentage per core
        core_count      int   — number of logical cores
        freq_mhz        float or None — current CPU frequency in MHz

    Uses a 0.1s interval for the psutil measurement — short enough not
    to block the poller meaningfully, long enough for an accurate reading.
    """
    if not psutil:
        return {
            "usage_pct": None, "per_core_pct": [],
            "core_count": os.cpu_count() or 1, "freq_mhz": None
        }

    try:
        per_core = psutil.cpu_percent(interval=0.1, percpu=True)
        aggregate = round(sum(per_core) / len(per_core), 1) if per_core else None
    except Exception as e:
        logger.warning("cpu_percent failed: %s", e)
        per_core = []
        aggregate = None

    freq_mhz = None
    try:
        freq = psutil.cpu_freq()
        if freq:
            freq_mhz = round(freq.current, 1)
    except Exception:
        pass

    return {
        "usage_pct":    aggregate,
        "per_core_pct": [round(p, 1) for p in per_core],
        "core_count":   len(per_core) if per_core else (os.cpu_count() or 1),
        "freq_mhz":     freq_mhz,
    }


# ── Memory ────────────────────────────────────────────────────────────────────

def read_memory():
    """
    Read system memory statistics.

    Returns a dict:
        total_mb        float — total physical memory in MB
        used_mb         float — used memory in MB
        available_mb    float — available memory in MB
        usage_pct       float — memory usage percentage (0-100)
    """
    if not psutil:
        return {
            "total_mb": None, "used_mb": None,
            "available_mb": None, "usage_pct": None
        }

    try:
        mem = psutil.virtual_memory()
        return {
            "total_mb":     round(mem.total / (1024 ** 2), 1),
            "used_mb":      round(mem.used / (1024 ** 2), 1),
            "available_mb": round(mem.available / (1024 ** 2), 1),
            "usage_pct":    round(mem.percent, 1),
        }
    except Exception as e:
        logger.warning("virtual_memory failed: %s", e)
        return {
            "total_mb": None, "used_mb": None,
            "available_mb": None, "usage_pct": None
        }


# ── Disk ──────────────────────────────────────────────────────────────────────

def read_disk(path="/"):
    """
    Read disk usage for the given path (default: root filesystem).

    Returns a dict:
        total_gb        float — total disk space in GB
        used_gb         float — used disk space in GB
        free_gb         float — free disk space in GB
        usage_pct       float — disk usage percentage (0-100)
        path            str   — the path checked
    """
    if not psutil:
        return {
            "total_gb": None, "used_gb": None,
            "free_gb": None, "usage_pct": None, "path": path
        }

    try:
        disk = psutil.disk_usage(path)
        return {
            "total_gb": round(disk.total / (1024 ** 3), 2),
            "used_gb":  round(disk.used  / (1024 ** 3), 2),
            "free_gb":  round(disk.free  / (1024 ** 3), 2),
            "usage_pct": round(disk.percent, 1),
            "path":     path,
        }
    except Exception as e:
        logger.warning("disk_usage(%s) failed: %s", path, e)
        return {
            "total_gb": None, "used_gb": None,
            "free_gb": None, "usage_pct": None, "path": path
        }


# ── Load average ──────────────────────────────────────────────────────────────

def read_load():
    """
    Read system load averages.

    Returns a dict:
        load_1          float — 1-minute load average
        load_5          float — 5-minute load average
        load_15         float — 15-minute load average
        core_count      int   — logical CPU count (for normalisation)
    """
    try:
        load1, load5, load15 = os.getloadavg()
        return {
            "load_1":   round(load1, 2),
            "load_5":   round(load5, 2),
            "load_15":  round(load15, 2),
            "core_count": os.cpu_count() or 1,
        }
    except Exception as e:
        logger.warning("getloadavg failed: %s", e)
        return {"load_1": None, "load_5": None, "load_15": None, "core_count": 1}


# ── Uptime ────────────────────────────────────────────────────────────────────

def read_uptime():
    """
    Read system uptime in seconds from /proc/uptime.

    Returns a dict:
        uptime_seconds  float — seconds since last boot
        uptime_str      str   — human-readable string, e.g. "3d 14h 22m"
    """
    try:
        with open("/proc/uptime") as f:
            uptime_s = float(f.read().split()[0])
    except Exception as e:
        logger.warning("read /proc/uptime failed: %s", e)
        if psutil:
            try:
                uptime_s = time.time() - psutil.boot_time()
            except Exception:
                return {"uptime_seconds": None, "uptime_str": "Unknown"}
        else:
            return {"uptime_seconds": None, "uptime_str": "Unknown"}

    return {
        "uptime_seconds": uptime_s,
        "uptime_str":     _format_uptime(uptime_s),
    }


def _format_uptime(seconds):
    """Format uptime seconds as a readable string. e.g. '3d 14h 22m'"""
    seconds = int(seconds)
    days    = seconds // 86400
    hours   = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60

    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m"


# ── Network ───────────────────────────────────────────────────────────────────

def read_network(interface=None):
    """
    Read network interface statistics.

    If interface is None, uses the first non-loopback interface found.

    Returns a dict:
        interface       str   — interface name (e.g. "eth0", "wlan0")
        bytes_sent      int   — total bytes sent since boot
        bytes_recv      int   — total bytes received since boot
        hostname        str   — system hostname
        ip_address      str or None — primary IP address
    """
    import socket as _socket

    hostname = _socket.gethostname()
    ip_address = None
    try:
        # Connect to a non-routable address to determine outbound interface IP
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip_address = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    if not psutil:
        return {
            "interface": interface or "unknown",
            "bytes_sent": None, "bytes_recv": None,
            "hostname": hostname, "ip_address": ip_address,
        }

    try:
        net_io = psutil.net_io_counters(pernic=True)

        # Pick interface: use provided name, or find first non-loopback
        if interface and interface in net_io:
            iface = interface
        else:
            iface = None
            for name, stats in net_io.items():
                if name != "lo" and not name.startswith("docker"):
                    iface = name
                    break

        if iface and iface in net_io:
            stats = net_io[iface]
            return {
                "interface":  iface,
                "bytes_sent": stats.bytes_sent,
                "bytes_recv": stats.bytes_recv,
                "hostname":   hostname,
                "ip_address": ip_address,
            }
    except Exception as e:
        logger.warning("net_io_counters failed: %s", e)

    return {
        "interface": interface or "unknown",
        "bytes_sent": None, "bytes_recv": None,
        "hostname": hostname, "ip_address": ip_address,
    }


# ── Service status ────────────────────────────────────────────────────────────

# Services to monitor — (display_name, systemd_unit_name)
_MONITORED_SERVICES = [
    ("chronyd",  "chronyd"),
    ("gpsd",     "gpsd"),
]

def read_services():
    """
    Check the status of monitored systemd services.

    Returns a dict keyed by display_name:
        {
            "chronyd": {"status": "active", "active": True},
            "gpsd":    {"status": "active", "active": True},
        }

    Status values: "active", "inactive", "failed", "unknown"
    Uses 'systemctl is-active' which is fast and requires no sudo.
    """
    results = {}
    for display_name, unit_name in _MONITORED_SERVICES:
        results[display_name] = _check_service(unit_name)
    return results


def _check_service(unit_name):
    """Run 'systemctl is-active <unit>' and return a status dict."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit_name],
            capture_output=True,
            text=True,
            timeout=3
        )
        status = result.stdout.strip()
        # systemctl is-active returns: active, inactive, failed, activating,
        # deactivating, or unknown — we normalise to just these four
        if status == "active":
            return {"status": "active", "active": True}
        elif status == "failed":
            return {"status": "failed", "active": False}
        elif status in ("inactive", "deactivating"):
            return {"status": "inactive", "active": False}
        else:
            return {"status": status, "active": False}
    except subprocess.TimeoutExpired:
        logger.warning("systemctl is-active %s timed out", unit_name)
        return {"status": "unknown", "active": False}
    except FileNotFoundError:
        # systemctl not available (non-systemd system or test environment)
        return {"status": "unknown", "active": False}
    except Exception as e:
        logger.warning("service check %s failed: %s", unit_name, e)
        return {"status": "unknown", "active": False}


# ── PPS device check ──────────────────────────────────────────────────────────

def read_pps_devices():
    """
    Check PPS device presence and identity via sysfs.

    Returns a list of dicts, one per /dev/ppsN found:
        {
            "device":  "/dev/pps0",
            "name":    "pps@12.-1",   (from sysfs name file)
            "present": True,
            "is_gpio": True,          (True if name starts with "pps@")
        }
    """
    devices = []
    for i in range(4):
        dev_path = f"/dev/pps{i}"
        sysfs_name = f"/sys/class/pps/pps{i}/name"

        if not os.path.exists(dev_path):
            break

        name = None
        try:
            with open(sysfs_name) as f:
                name = f.read().strip()
        except OSError:
            pass

        devices.append({
            "device":  dev_path,
            "name":    name,
            "present": True,
            "is_gpio": name.startswith("pps@") if name else False,
        })

    return devices


# ── collect ───────────────────────────────────────────────────────────────────

def collect():
    """
    Gather all system metrics and return a single combined dict.

    Returns:
    {
        "cpu_temp_c":       float or None
        "cpu_usage_pct":    float or None
        "cpu_per_core_pct": list
        "cpu_freq_mhz":     float or None
        "cpu_core_count":   int
        "mem_total_mb":     float or None
        "mem_used_mb":      float or None
        "mem_available_mb": float or None
        "mem_usage_pct":    float or None
        "disk_total_gb":    float or None
        "disk_used_gb":     float or None
        "disk_free_gb":     float or None
        "disk_usage_pct":   float or None
        "load_1":           float or None
        "load_5":           float or None
        "load_15":          float or None
        "uptime_seconds":   float or None
        "uptime_str":       str
        "net_interface":    str
        "net_bytes_sent":   int or None
        "net_bytes_recv":   int or None
        "hostname":         str
        "ip_address":       str or None
        "services":         dict  (see read_services())
        "pps_devices":      list  (see read_pps_devices())
        "timestamp":        float (Unix epoch)
    }
    """
    cpu    = read_cpu()
    mem    = read_memory()
    disk   = read_disk()
    load   = read_load()
    uptime = read_uptime()
    net    = read_network()

    return {
        "cpu_temp_c":       read_cpu_temp(),
        "cpu_usage_pct":    cpu["usage_pct"],
        "cpu_per_core_pct": cpu["per_core_pct"],
        "cpu_freq_mhz":     cpu["freq_mhz"],
        "cpu_core_count":   cpu["core_count"],
        "mem_total_mb":     mem["total_mb"],
        "mem_used_mb":      mem["used_mb"],
        "mem_available_mb": mem["available_mb"],
        "mem_usage_pct":    mem["usage_pct"],
        "disk_total_gb":    disk["total_gb"],
        "disk_used_gb":     disk["used_gb"],
        "disk_free_gb":     disk["free_gb"],
        "disk_usage_pct":   disk["usage_pct"],
        "load_1":           load["load_1"],
        "load_5":           load["load_5"],
        "load_15":          load["load_15"],
        "uptime_seconds":   uptime["uptime_seconds"],
        "uptime_str":       uptime["uptime_str"],
        "net_interface":    net["interface"],
        "net_bytes_sent":   net["bytes_sent"],
        "net_bytes_recv":   net["bytes_recv"],
        "hostname":         net["hostname"],
        "ip_address":       net["ip_address"],
        "services":         read_services(),
        "pps_devices":      read_pps_devices(),
        "timestamp":        time.time(),
    }


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(levelname)s  %(message)s"
    )

    print("=" * 60)
    print("collectors/system.py — standalone test")
    print("=" * 60)
    print(f"psutil available : {psutil is not None}")
    print()

    # Detect whether we are running on a Pi with real hardware
    on_pi = os.path.exists("/sys/class/thermal/thermal_zone0/temp")

    # ── CPU temperature
    print("── CPU temperature ──")
    temp = read_cpu_temp()
    print(f"  cpu_temp_c : {temp}")
    if on_pi:
        assert temp is not None, "cpu_temp_c is None — check /sys/class/thermal/"
        assert 20 < temp < 90,   f"cpu_temp_c out of plausible range: {temp}"
    else:
        print("  (skipping range assertion — not running on Pi hardware)")
    print()

    # ── CPU usage
    print("── CPU usage ──")
    cpu = read_cpu()
    print(f"  usage_pct     : {cpu['usage_pct']}%")
    print(f"  per_core_pct  : {cpu['per_core_pct']}")
    print(f"  core_count    : {cpu['core_count']}")
    print(f"  freq_mhz      : {cpu['freq_mhz']}")
    assert cpu["usage_pct"] is not None, "cpu usage_pct is None"
    assert 0 <= cpu["usage_pct"] <= 100, f"cpu_pct out of range: {cpu['usage_pct']}"
    assert cpu["core_count"] >= 1,       f"core_count: {cpu['core_count']}"
    print()

    # ── Memory
    print("── Memory ──")
    mem = read_memory()
    print(f"  total_mb      : {mem['total_mb']} MB")
    print(f"  used_mb       : {mem['used_mb']} MB")
    print(f"  available_mb  : {mem['available_mb']} MB")
    print(f"  usage_pct     : {mem['usage_pct']}%")
    assert mem["total_mb"] is not None,  "mem total_mb is None"
    assert mem["total_mb"] > 0,          f"mem total_mb: {mem['total_mb']}"
    assert 0 <= mem["usage_pct"] <= 100, f"mem_pct out of range: {mem['usage_pct']}"
    print()

    # ── Disk
    print("── Disk (/) ──")
    disk = read_disk()
    print(f"  total_gb      : {disk['total_gb']} GB")
    print(f"  used_gb       : {disk['used_gb']} GB")
    print(f"  free_gb       : {disk['free_gb']} GB")
    print(f"  usage_pct     : {disk['usage_pct']}%")
    assert disk["total_gb"] is not None,  "disk total_gb is None"
    assert disk["total_gb"] > 0,          f"disk total_gb: {disk['total_gb']}"
    assert 0 <= disk["usage_pct"] <= 100, f"disk_pct out of range: {disk['usage_pct']}"
    print()

    # ── Load average
    print("── Load average ──")
    load = read_load()
    print(f"  load_1  : {load['load_1']}")
    print(f"  load_5  : {load['load_5']}")
    print(f"  load_15 : {load['load_15']}")
    assert load["load_1"] is not None, "load_1 is None"
    assert load["load_1"] >= 0,        f"load_1 negative: {load['load_1']}"
    print()

    # ── Uptime
    print("── Uptime ──")
    up = read_uptime()
    print(f"  uptime_seconds : {up['uptime_seconds']:.0f}s")
    print(f"  uptime_str     : {up['uptime_str']}")
    assert up["uptime_seconds"] is not None, "uptime_seconds is None"
    assert up["uptime_seconds"] > 0,         "uptime_seconds is zero or negative"
    assert up["uptime_str"] != "Unknown",    "uptime_str is Unknown"
    print()

    # ── Network
    print("── Network ──")
    net = read_network()
    print(f"  interface  : {net['interface']}")
    print(f"  bytes_sent : {net['bytes_sent']}")
    print(f"  bytes_recv : {net['bytes_recv']}")
    print(f"  hostname   : {net['hostname']}")
    print(f"  ip_address : {net['ip_address']}")
    assert net["hostname"],              "hostname is empty"
    print()

    # ── Services
    print("── Service status ──")
    services = read_services()
    for name, info in services.items():
        status_str = "ACTIVE" if info["active"] else f"NOT ACTIVE ({info['status']})"
        print(f"  {name:12s} : {status_str}")
    assert "chronyd" in services, "chronyd not in services"
    assert "gpsd"    in services, "gpsd not in services"
    if on_pi:
        # On PowerPi both should be active
        assert services["chronyd"]["active"], \
            f"chronyd not active: {services['chronyd']['status']}"
        assert services["gpsd"]["active"], \
            f"gpsd not active: {services['gpsd']['status']}"
    else:
        print("  (skipping active assertions — not on Pi)")
    print()

    # ── PPS devices
    print("── PPS devices ──")
    pps = read_pps_devices()
    if pps:
        for dev in pps:
            gpio_str = " (GPIO PPS)" if dev["is_gpio"] else ""
            print(f"  {dev['device']:12s}  name={dev['name']}{gpio_str}")
        gpio_devices = [d for d in pps if d["is_gpio"]]
        if on_pi:
            assert len(gpio_devices) >= 1, "No GPIO PPS device found"
        if gpio_devices:
            print(f"  GPIO PPS device: {gpio_devices[0]['device']} "
                  f"({gpio_devices[0]['name']})")
    else:
        print("  No PPS devices found (expected on Pi, ok in dev environment)")
    print()

    # ── Full collect()
    print("── collect() ──")
    data = collect()
    print(f"  cpu_temp_c    : {data['cpu_temp_c']} °C")
    print(f"  cpu_usage_pct : {data['cpu_usage_pct']}%")
    print(f"  mem_usage_pct : {data['mem_usage_pct']}%")
    print(f"  disk_usage_pct: {data['disk_usage_pct']}%")
    print(f"  load_1        : {data['load_1']}")
    print(f"  uptime_str    : {data['uptime_str']}")
    print(f"  hostname      : {data['hostname']}")
    print(f"  ip_address    : {data['ip_address']}")
    print(f"  chronyd       : {data['services']['chronyd']['status']}")
    print(f"  gpsd          : {data['services']['gpsd']['status']}")
    if on_pi:
        assert data["cpu_temp_c"] is not None, "collect(): cpu_temp_c is None"
    assert data["hostname"],               "collect(): hostname is empty"
    print()

    print("All tests passed.")
