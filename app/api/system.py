"""
api/system.py — System health API blueprint.

GET /api/system
    CPU, memory, disk, load, uptime, service status, PPS devices.
    Includes temperature/frequency correlation history for dual-axis chart.
"""

import time
from flask import Blueprint, jsonify

import config
import database
import poller

bp = Blueprint("system_api", __name__)


@bp.route("/api/system")
def system_health():
    latest = poller.get_latest()
    system = latest.get("system") or {}

    now      = time.time()
    hour_ago = now - 3600

    def _hist(metric, max_pts=60):
        pts = database.query_history("system", metric,
                                     from_ts=hour_ago, to_ts=now,
                                     max_points=max_pts)
        return [{"t": p["timestamp"], "v": p["value"]} for p in pts]

    # Temperature/frequency correlation — fetch both on same time axis
    temp_hist = _hist("cpu_temp_c")
    freq_hist = database.query_history(
        "chrony", "freq_ppm",
        from_ts=hour_ago, to_ts=now,
        max_points=60
    )

    # Threshold values for frontend to draw warning/critical lines
    thresholds = {
        "cpu_temp_warning_c":  config.get("cpu_temp_warning_c", 70),
        "cpu_temp_critical_c": config.get("cpu_temp_critical_c", 80),
        "disk_warning_pct":    config.get("disk_warning_pct", 80),
        "disk_critical_pct":   config.get("disk_critical_pct", 90),
    }

    return jsonify({
        "cpu": {
            "temp_c":       system.get("cpu_temp_c"),
            "usage_pct":    system.get("cpu_usage_pct"),
            "per_core_pct": system.get("cpu_per_core_pct", []),
            "freq_mhz":     system.get("cpu_freq_mhz"),
            "core_count":   system.get("cpu_core_count"),
        },
        "memory": {
            "total_mb":     system.get("mem_total_mb"),
            "used_mb":      system.get("mem_used_mb"),
            "available_mb": system.get("mem_available_mb"),
            "usage_pct":    system.get("mem_usage_pct"),
        },
        "disk": {
            "total_gb":  system.get("disk_total_gb"),
            "used_gb":   system.get("disk_used_gb"),
            "free_gb":   system.get("disk_free_gb"),
            "usage_pct": system.get("disk_usage_pct"),
        },
        "load": {
            "load_1":  system.get("load_1"),
            "load_5":  system.get("load_5"),
            "load_15": system.get("load_15"),
        },
        "uptime": {
            "seconds": system.get("uptime_seconds"),
            "str":     system.get("uptime_str"),
        },
        "network": {
            "interface":   system.get("net_interface"),
            "bytes_sent":  system.get("net_bytes_sent"),
            "bytes_recv":  system.get("net_bytes_recv"),
            "hostname":    system.get("hostname"),
            "ip_address":  system.get("ip_address"),
        },
        "services":    system.get("services", {}),
        "pps_devices": system.get("pps_devices", []),

        "thresholds": thresholds,

        "charts": {
            "cpu_temp":  temp_hist,
            "cpu_usage": _hist("cpu_usage_pct"),
            "mem_usage": _hist("mem_usage_pct"),
            "freq_ppm":  [{"t": p["timestamp"], "v": p["value"]}
                          for p in freq_hist],
        },

        "cycle_time": latest.get("cycle_time"),
        "errors":     latest.get("errors") or [],
    })
