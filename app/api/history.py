"""
api/history.py — History and trends API blueprint.

GET /api/history?range=24h
    Long-range time-series data from SQLite for the history page.
    range param: 24h, 7d, 30d, 90d (default: 24h)
"""

import time
from flask import Blueprint, jsonify, request

import database

bp = Blueprint("history_api", __name__)

_RANGE_MAP = {
    "24h":  24,
    "7d":   168,
    "30d":  720,
    "90d":  2160,
}


@bp.route("/api/history")
def history():
    range_str = request.args.get("range", "24h")
    hours     = _RANGE_MAP.get(range_str, 24)
    now       = time.time()
    from_ts   = now - (hours * 3600)

    # More points for shorter ranges, fewer for longer
    if hours <= 24:
        max_pts = 300
    elif hours <= 168:
        max_pts = 400
    else:
        max_pts = 500

    def _h(source, metric):
        pts = database.query_history(source, metric,
                                     from_ts=from_ts, to_ts=now,
                                     max_points=max_pts)
        return [{"t": p["timestamp"], "v": p["value"]} for p in pts]

    # Daily summary table
    daily = _daily_summary(from_ts, now)

    return jsonify({
        "range":      range_str,
        "range_hours": hours,
        "from_ts":    from_ts,
        "to_ts":      now,

        "charts": {
            "pps_offset":   _h("chrony", "pps_offset_ns"),
            "rms_offset":   _h("chrony", "rms_offset_ns"),
            "freq_ppm":     _h("chrony", "freq_ppm"),
            "sats_used":    _h("gnss",   "sats_used"),
            "sats_visible": _h("gnss",   "sats_visible"),
            "cpu_temp":     _h("system", "cpu_temp_c"),
        },

        "daily_summary": daily,
    })


def _daily_summary(from_ts, to_ts):
    """
    Build a per-day summary table from raw_samples.
    Returns a list of dicts, one per calendar day, ordered newest first.
    """
    import datetime

    # Fetch raw PPS offset data for the range
    pts = database.query_range("chrony", "pps_offset_ns", from_ts, to_ts)

    if not pts:
        return []

    # Group by calendar day (UTC)
    days = {}
    for pt in pts:
        day = datetime.datetime.utcfromtimestamp(
            pt["timestamp"]
        ).strftime("%Y-%m-%d")
        if day not in days:
            days[day] = []
        if pt["value_num"] is not None:
            days[day].append(abs(pt["value_num"]))

    summary = []
    for day_str in sorted(days.keys(), reverse=True):
        values = days[day_str]
        if not values:
            continue
        summary.append({
            "date":       day_str,
            "mean_abs_ns": round(sum(values) / len(values), 1),
            "min_ns":     round(min(values), 1),
            "max_ns":     round(max(values), 1),
            "n_samples":  len(values),
        })

    return summary
