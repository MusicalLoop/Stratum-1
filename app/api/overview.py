"""
api/overview.py — Overview API blueprint.

GET /api/overview
    Returns the single-glance summary used by the overview page.
    Combines the 5-minute rolling mean of PPS offset with fix status,
    satellite count, system health, and sparkline data for the last hour.
"""

import time
from flask import Blueprint, jsonify

import config
import database
import poller

bp = Blueprint("overview", __name__)


def _status_badge(chrony_data, gnss_data, system_data):
    """
    Derive the overall status badge state from current metrics.
    Returns "green", "amber", or "red".
    Priority: any red condition → red; any amber → amber; else green.
    """
    conditions = []

    # PPS offset (use ring buffer rolling mean)
    pps_mean = poller.get_rolling_mean("chrony", "pps_offset_ns", minutes=5)
    if pps_mean is not None:
        pps_abs = abs(pps_mean)
        warn  = config.get("offset_warning_ns", 100)
        crit  = config.get("offset_critical_ns", 500)
        if pps_abs >= crit:
            conditions.append("red")
        elif pps_abs >= warn:
            conditions.append("amber")

    # Root dispersion
    disp = poller.get_rolling_mean("chrony", "root_dispersion_us", minutes=5)
    if disp is not None:
        disp_warn = config.get("dispersion_warning_us", 15)
        if disp >= disp_warn:
            conditions.append("amber")

    # Satellite count
    sats = poller.get_rolling_mean("gnss", "sats_used", minutes=5)
    if sats is not None:
        sats_warn = config.get("min_satellites_warning", 4)
        sats_crit = config.get("min_satellites_critical", 3)
        if sats <= sats_crit:
            conditions.append("red")
        elif sats <= sats_warn:
            conditions.append("amber")

    # CPU temperature
    temp = poller.get_rolling_mean("system", "cpu_temp_c", minutes=5)
    if temp is not None:
        temp_warn = config.get("cpu_temp_warning_c", 70)
        temp_crit = config.get("cpu_temp_critical_c", 80)
        if temp >= temp_crit:
            conditions.append("red")
        elif temp >= temp_warn:
            conditions.append("amber")

    # Disk usage
    disk = poller.get_rolling_mean("system", "disk_usage_pct", minutes=5)
    if disk is not None:
        disk_warn = config.get("disk_warning_pct", 80)
        disk_crit = config.get("disk_critical_pct", 90)
        if disk >= disk_crit:
            conditions.append("red")
        elif disk >= disk_warn:
            conditions.append("amber")

    # No data at all — show amber
    if pps_mean is None and sats is None:
        return "amber"

    if "red" in conditions:
        return "red"
    if "amber" in conditions:
        return "amber"
    return "green"


@bp.route("/api/overview")
def overview():
    latest   = poller.get_latest()
    chrony   = latest.get("chrony") or {}
    gnss     = latest.get("gnss")   or {}
    system   = latest.get("system") or {}
    tracking = (chrony.get("tracking") or {})
    sources  = (chrony.get("sources")  or [])
    tpv      = (gnss.get("tpv")        or {})
    sky      = (gnss.get("sky")        or {})

    pps_src = next((s for s in sources if s.get("is_pps")), None)

    # Rolling stats for PPS offset (5-minute window)
    smooth_mins = config.get("smoothing_window_minutes", 5)
    pps_stats = poller.get_rolling_stats("chrony", "pps_offset_ns",
                                         minutes=smooth_mins)

    # Sparkline data — last 1 hour, up to 60 points each
    now     = time.time()
    hour_ago = now - 3600
    unit    = config.get("offset_display_unit", "ns")
    divisor = 1000.0 if unit == "us" else 1.0

    def _sparkline(source, metric):
        pts = database.query_history(source, metric,
                                     from_ts=hour_ago, to_ts=now,
                                     max_points=60)
        return [{"t": p["timestamp"], "v": p["value"]} for p in pts]

    cycle_time = latest.get("cycle_time")
    data_age_s = round(now - cycle_time, 1) if cycle_time else None

    return jsonify({
        "status_badge":     _status_badge(chrony, gnss, system),
        "reference_label":  config.get("reference_label", "GPS PPS"),
        "stratum":          tracking.get("stratum"),
        "leap_status":      tracking.get("leap_status"),

        # PPS offset — primary metric
        "pps_offset": {
            "value":    round(pps_stats["mean"] / divisor, 2)
                        if pps_stats else None,
            "min":      round(pps_stats["min_val"] / divisor, 2)
                        if pps_stats else None,
            "max":      round(pps_stats["max_val"] / divisor, 2)
                        if pps_stats else None,
            "unit":     unit,
            "window_minutes": smooth_mins,
            "n_samples": pps_stats["count"] if pps_stats else 0,
        },

        # Jitter (RMS offset from tracking)
        "jitter": {
            "value": round(tracking.get("rms_offset_ns", 0) / divisor, 2)
                     if tracking.get("rms_offset_ns") is not None else None,
            "unit":  unit,
        },

        # GNSS fix
        "fix": {
            "mode":         tpv.get("fix_mode"),
            "mode_name":    tpv.get("fix_mode_name"),
            "sats_used":    sky.get("sats_used"),
            "sats_visible": sky.get("sats_visible"),
            "hdop":         sky.get("hdop"),
        },

        # System
        "system": {
            "cpu_temp_c":    system.get("cpu_temp_c"),
            "cpu_usage_pct": system.get("cpu_usage_pct"),
            "mem_usage_pct": system.get("mem_usage_pct"),
            "disk_usage_pct":system.get("disk_usage_pct"),
            "uptime_str":    system.get("uptime_str"),
            "hostname":      system.get("hostname"),
            "ip_address":    system.get("ip_address"),
        },

        # Sparklines
        "sparklines": {
            "pps_offset": _sparkline("chrony", "pps_offset_ns"),
            "rms_offset": _sparkline("chrony", "rms_offset_ns"),
            "cpu_temp":   _sparkline("system", "cpu_temp_c"),
        },

        # Metadata
        "data_age_s":   data_age_s,
        "cycle_time":   cycle_time,
        "errors":       latest.get("errors") or [],
        "refresh_interval_s": config.get("ui_refresh_interval_seconds", 10),
    })
