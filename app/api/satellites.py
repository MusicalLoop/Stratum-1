"""
api/satellites.py — Satellite sky view API blueprint.

GET /api/satellites
    Full satellite list with azimuth, elevation, SNR for sky plot rendering.
"""

import time
from flask import Blueprint, jsonify

import config
import database
import poller

bp = Blueprint("satellites_api", __name__)


@bp.route("/api/satellites")
def satellites():
    latest = poller.get_latest()
    gnss   = latest.get("gnss") or {}
    sky    = gnss.get("sky") or {}

    elev_mask  = config.get("elevation_mask_degrees", 10)
    satellites = sky.get("satellites") or []

    # Annotate each satellite with above/below mask flag
    annotated = []
    for sat in satellites:
        el = sat.get("elevation")
        annotated.append({
            **sat,
            "above_mask": el is not None and el >= elev_mask,
        })

    # Sort: used first, then by SNR descending
    annotated.sort(key=lambda s: (
        0 if s.get("used") else 1,
        -(s.get("snr") or 0)
    ))

    # Rolling constellation count history for the trend chart
    now     = time.time()
    hour_ago = now - 3600
    sats_hist = database.query_history(
        "gnss", "sats_used",
        from_ts=hour_ago, to_ts=now,
        max_points=60
    )
    sats_visible_hist = database.query_history(
        "gnss", "sats_visible",
        from_ts=hour_ago, to_ts=now,
        max_points=60
    )

    return jsonify({
        "satellites":       annotated,
        "sats_visible":     sky.get("sats_visible"),
        "sats_used":        sky.get("sats_used"),
        "elevation_mask":   elev_mask,
        "constellation_summary": sky.get("constellation_summary", {}),

        # History for the rolling count chart
        "history": {
            "sats_used":    [{"t": p["timestamp"], "v": p["value"]}
                             for p in sats_hist],
            "sats_visible": [{"t": p["timestamp"], "v": p["value"]}
                             for p in sats_visible_hist],
        },

        "cycle_time": latest.get("cycle_time"),
        "errors":     latest.get("errors") or [],
    })
