"""
api/chrony.py — Chrony detail API blueprint.

GET /api/chrony
    Full chrony tracking and sourcestats data with history arrays
    for time-series charts.
"""

import time
from flask import Blueprint, jsonify, request

import config
import database
import poller

bp = Blueprint("chrony_api", __name__)


@bp.route("/api/chrony")
def chrony_detail():
    latest   = poller.get_latest()
    chrony   = latest.get("chrony") or {}
    tracking = chrony.get("tracking")  or {}
    sources  = chrony.get("sources")   or []
    sourcestats = chrony.get("sourcestats") or []

    # Time window for charts — default 1hr, override via ?range=Nh
    range_hours = _parse_range(request.args.get("range", "1h"))
    now      = time.time()
    from_ts  = now - (range_hours * 3600)
    unit     = config.get("offset_display_unit", "ns")
    divisor  = 1000.0 if unit == "us" else 1.0

    def _hist(source, metric, max_pts=300):
        pts = database.query_history(source, metric,
                                     from_ts=from_ts, to_ts=now,
                                     max_points=max_pts)
        return [{"t": p["timestamp"],
                 "v": round(p["value"] / divisor, 3)} for p in pts]

    # Merge sourcestats into sources list by name
    ss_by_name = {s["name"]: s for s in sourcestats}
    sources_out = []
    for src in sources:
        name = src.get("name", "")
        ss = ss_by_name.get(name, {})
        sources_out.append({
            "name":          name,
            "mode":          src.get("mode"),
            "state":         src.get("state"),
            "state_char":    src.get("state_char"),
            "stratum":       src.get("stratum"),
            "poll":          src.get("poll"),
            "reach":         src.get("reach"),
            "last_rx":       src.get("last_rx"),
            "last_rx_seconds": src.get("last_rx_seconds"),
            "adj_offset_ns": src.get("adj_offset_ns"),
            "meas_offset_ns":src.get("meas_offset_ns"),
            "error_ns":      src.get("error_ns"),
            "is_pps":        src.get("is_pps"),
            "is_gps":        src.get("is_gps"),
            "zero_samples":  src.get("zero_samples"),
            # From sourcestats
            "np":            ss.get("np"),
            "nr":            ss.get("nr"),
            "span":          ss.get("span"),
            "span_seconds":  ss.get("span_seconds"),
            "freq_ppm":      ss.get("freq_ppm"),
            "freq_skew_ppm": ss.get("freq_skew_ppm"),
            "offset_ns":     ss.get("offset_ns"),
            "std_dev_ns":    ss.get("std_dev_ns"),
        })

    return jsonify({
        "tracking": {
            "reference_id":       tracking.get("reference_id"),
            "reference_name":     tracking.get("reference_name"),
            "reference_label":    config.get("reference_label", "GPS PPS"),
            "stratum":            tracking.get("stratum"),
            "ref_time_utc":       tracking.get("ref_time_utc"),
            "system_offset_ns":   tracking.get("system_offset_ns"),
            "last_offset_ns":     tracking.get("last_offset_ns"),
            "rms_offset_ns":      tracking.get("rms_offset_ns"),
            "freq_ppm":           tracking.get("freq_ppm"),
            "freq_direction":     tracking.get("freq_direction"),
            "residual_freq_ppm":  tracking.get("residual_freq_ppm"),
            "skew_ppm":           tracking.get("skew_ppm"),
            "root_delay_ns":      tracking.get("root_delay_ns"),
            "root_dispersion_ns": tracking.get("root_dispersion_ns"),
            "root_dispersion_us": tracking.get("root_dispersion_us"),
            "update_interval_s":  tracking.get("update_interval_s"),
            "leap_status":        tracking.get("leap_status"),
        },
        "sources":      sources_out,
        "highlighted_peers": config.get_highlighted_peers(),

        # Chart data
        "charts": {
            "unit":       unit,
            "range_hours": range_hours,
            "pps_offset": _hist("chrony", "pps_offset_ns"),
            "rms_offset": _hist("chrony", "rms_offset_ns"),
            "freq_ppm":   _hist("chrony", "freq_ppm"),
            "skew_ppm":   _hist("chrony", "skew_ppm"),
        },

        "errors": latest.get("errors") or [],
        "cycle_time": latest.get("cycle_time"),
    })


def _parse_range(range_str):
    """Parse a range string like '1h', '6h', '24h', '7d' into hours."""
    try:
        if range_str.endswith("h"):
            return float(range_str[:-1])
        if range_str.endswith("d"):
            return float(range_str[:-1]) * 24
    except (ValueError, AttributeError):
        pass
    return 1.0
