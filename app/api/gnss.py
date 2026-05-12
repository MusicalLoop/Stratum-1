"""
api/gnss.py — GNSS status API blueprint.

GET /api/gnss
    Fix quality, position, constellation breakdown, NMEA health.
"""

import time
from flask import Blueprint, jsonify

import config
import database
import poller

bp = Blueprint("gnss_api", __name__)


@bp.route("/api/gnss")
def gnss_status():
    latest = poller.get_latest()
    gnss   = latest.get("gnss") or {}
    tpv    = gnss.get("tpv") or {}
    sky    = gnss.get("sky") or {}

    show_position = config.get("show_position", True)

    # Position — respect show_position config
    if show_position:
        position = {
            "latitude":   tpv.get("latitude"),
            "longitude":  tpv.get("longitude"),
            "altitude_m": tpv.get("altitude_m"),
            "eph":        tpv.get("eph"),
            "epv":        tpv.get("epv"),
        }
    else:
        position = {
            "latitude":   None,
            "longitude":  None,
            "altitude_m": tpv.get("altitude_m"),  # altitude less sensitive
            "eph":        None,
            "epv":        None,
            "hidden":     True,
        }

    # Elevation mask from config
    elev_mask = config.get("elevation_mask_degrees", 10)

    # Satellites above/below elevation mask
    satellites = sky.get("satellites") or []
    above_mask = [s for s in satellites
                  if s.get("elevation") is not None
                  and s["elevation"] >= elev_mask]
    below_mask = [s for s in satellites
                  if s.get("elevation") is not None
                  and s["elevation"] < elev_mask]

    # Expected vs seen constellation check
    expected = config.get_expected_constellations()
    seen     = set(sky.get("constellation_summary", {}).keys())
    missing_constellations = [c for c in expected if c not in seen]

    # Satellite count rolling means (5 min)
    sats_mean = poller.get_rolling_mean("gnss", "sats_used", minutes=5)

    # GNSS time vs system time delta
    gnss_time_utc = tpv.get("time_utc")
    cycle_time    = latest.get("cycle_time")

    return jsonify({
        "fix": {
            "mode":         tpv.get("fix_mode"),
            "mode_name":    tpv.get("fix_mode_name"),
            "has_fix":      tpv.get("has_fix"),
            "time_utc":     gnss_time_utc,
        },
        "position": position,
        "dop": {
            "hdop": sky.get("hdop"),
            "vdop": sky.get("vdop"),
            "pdop": sky.get("pdop"),
        },
        "satellites": {
            "visible":         sky.get("sats_visible"),
            "used":            sky.get("sats_used"),
            "used_mean_5m":    round(sats_mean, 1) if sats_mean else None,
            "above_mask":      len(above_mask),
            "below_mask":      len(below_mask),
            "elevation_mask":  elev_mask,
        },
        "constellations":  sky.get("constellation_summary", {}),
        "expected_constellations":  expected,
        "missing_constellations":   missing_constellations,

        "errors":     latest.get("errors") or [],
        "cycle_time": cycle_time,
    })
