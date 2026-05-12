"""
api/config_api.py — Configuration API blueprint.

GET  /api/config          — return current config as JSON
POST /api/config          — save updated config values
POST /api/config/test     — run a named test action
"""

import os
import time
import subprocess
import socket as _socket
from flask import Blueprint, jsonify, request

import config
import database
import poller

bp = Blueprint("config_api", __name__)


@bp.route("/api/config", methods=["GET"])
def get_config():
    """Return the current configuration as JSON."""
    return jsonify(config.get_all())


@bp.route("/api/config", methods=["POST"])
def save_config():
    """
    Accept a JSON body with config keys to update.
    Returns a result dict showing ok/invalid/unknown for each key.
    """
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object"}), 400

    results = config.save(data)
    return jsonify({
        "saved": True,
        "results": results,
    })


@bp.route("/api/config/test", methods=["POST"])
def run_test():
    """
    Run a named test action and return the result.

    Body: { "action": "chronyc" | "gpsd" | "pps" | "db" }

    Returns:
        { "action": str, "passed": bool, "output": str, "detail": dict }
    """
    data   = request.get_json(silent=True) or {}
    action = data.get("action", "").lower()

    if action == "chronyc":
        return jsonify(_test_chronyc())
    elif action == "gpsd":
        return jsonify(_test_gpsd())
    elif action == "pps":
        return jsonify(_test_pps())
    elif action == "db":
        return jsonify(_test_db())
    else:
        return jsonify({
            "action":  action or "(none)",
            "passed":  False,
            "output":  f"Unknown test action: '{action}'",
            "detail":  {},
        }), 400


# ── Test actions ──────────────────────────────────────────────────────────────

def _test_chronyc():
    """Run chronyc tracking and return the raw output."""
    chronyc = config.get("chronyc_path", "/usr/bin/chronyc")
    try:
        result = subprocess.run(
            [chronyc, "tracking"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return {
                "action":  "chronyc",
                "passed":  True,
                "output":  result.stdout,
                "detail":  {"path": chronyc, "returncode": 0},
            }
        else:
            return {
                "action":  "chronyc",
                "passed":  False,
                "output":  result.stderr or result.stdout,
                "detail":  {"path": chronyc, "returncode": result.returncode},
            }
    except FileNotFoundError:
        return {
            "action": "chronyc",
            "passed": False,
            "output": f"chronyc not found at {chronyc}",
            "detail": {"path": chronyc},
        }
    except subprocess.TimeoutExpired:
        return {
            "action": "chronyc",
            "passed": False,
            "output": "chronyc timed out after 5s",
            "detail": {"path": chronyc},
        }
    except Exception as e:
        return {
            "action": "chronyc",
            "passed": False,
            "output": str(e),
            "detail": {},
        }


def _test_gpsd():
    """Connect to gpsd, read one TPV and SKY message, return a summary."""
    host = config.get("gpsd_host", "localhost")
    port = config.get("gpsd_port", 2947)

    try:
        from collectors.gpsd_client import collect as gpsd_collect
        data = gpsd_collect(host=host, port=port, timeout=8)

        if data.get("error"):
            return {
                "action":  "gpsd",
                "passed":  False,
                "output":  data["error"],
                "detail":  {"host": host, "port": port},
            }

        tpv = data.get("tpv") or {}
        sky = data.get("sky") or {}
        lines = [
            f"Connected to gpsd at {host}:{port}",
            f"Fix mode    : {tpv.get('fix_mode_name', 'N/A')}",
            f"Satellites  : {sky.get('sats_used', 0)} used / "
            f"{sky.get('sats_visible', 0)} visible",
        ]
        for name, counts in sorted(
                (sky.get("constellation_summary") or {}).items()):
            lines.append(f"  {name:10s}: {counts['visible']} visible, "
                         f"{counts['used']} used")

        return {
            "action":  "gpsd",
            "passed":  True,
            "output":  "\n".join(lines),
            "detail":  {
                "host":          host,
                "port":          port,
                "fix_mode":      tpv.get("fix_mode"),
                "sats_used":     sky.get("sats_used"),
                "sats_visible":  sky.get("sats_visible"),
                "constellations": sky.get("constellation_summary", {}),
            },
        }
    except Exception as e:
        return {
            "action": "gpsd",
            "passed": False,
            "output": str(e),
            "detail": {"host": host, "port": port},
        }


def _test_pps():
    """Check PPS devices in /dev and /sys."""
    from collectors.system import read_pps_devices
    try:
        devices = read_pps_devices()

        if not devices:
            return {
                "action": "pps",
                "passed": False,
                "output": "No PPS devices found at /dev/pps*",
                "detail": {"devices": []},
            }

        lines = [f"Found {len(devices)} PPS device(s):"]
        for d in devices:
            gpio_note = " ← GPIO hardware PPS" if d["is_gpio"] else ""
            lines.append(f"  {d['device']:12s}  name={d['name']}{gpio_note}")

        gpio_devices = [d for d in devices if d["is_gpio"]]
        if gpio_devices:
            lines.append(
                f"\nGPIO PPS: {gpio_devices[0]['device']} "
                f"({gpio_devices[0]['name']})"
            )
            passed = True
        else:
            lines.append("\nNo GPIO PPS device found.")
            passed = False

        return {
            "action":  "pps",
            "passed":  passed,
            "output":  "\n".join(lines),
            "detail":  {"devices": devices},
        }
    except Exception as e:
        return {
            "action": "pps",
            "passed": False,
            "output": str(e),
            "detail": {},
        }


def _test_db():
    """Return database stats."""
    try:
        stats = database.db_stats()
        hb    = stats.get("poller_heartbeat")
        hb_age = round(time.time() - hb, 1) if hb else None

        lines = [
            f"Path             : {stats['path']}",
            f"Size             : {stats['size_mb']} MB",
            f"Schema version   : {stats['schema_version']}",
            f"Raw sample count : {stats['raw_samples_count']}",
            f"Poller heartbeat : "
            + (f"{hb_age}s ago" if hb_age is not None else "never"),
            "",
            "Metrics in database:",
        ]
        for s in stats["sources"]:
            lines.append(
                f"  {s['source']:8s} / {s['metric']:25s}  n={s['n']}"
            )

        return {
            "action":  "db",
            "passed":  True,
            "output":  "\n".join(lines),
            "detail":  stats,
        }
    except Exception as e:
        return {
            "action": "db",
            "passed": False,
            "output": str(e),
            "detail": {},
        }
