"""
config.py — Configuration management for the Stratum-1 dashboard.

Loads config.json from the project directory, merges over defaults,
validates values, and writes back a complete config.json.

Exposes:
    get(key)        — return current value for a config key
    get_all()       — return a copy of the full config dict
    save(updates)   — merge updates dict into config and write to disk
    reload()        — re-read config.json from disk if mtime has changed
    needs_reload()  — True if config.json has been modified since last load

Usage (standalone test):
    python3 config.py
"""

import json
import os
import socket
import logging

logger = logging.getLogger(__name__)

# ── Path resolution ───────────────────────────────────────────────────────────
# All paths are provided by paths.py. config.py does not construct
# any paths independently.

from paths import CONFIG_FILE as _CONFIG_PATH, DATA_DIR as _DATA_DIR

# ── Defaults ──────────────────────────────────────────────────────────────────
# Every key the application may read must have a default here.
# These are the values used if config.json does not exist or is missing a key.

DEFAULTS = {
    # ── Data collection ───────────────────────────────────────────────────────
    "poll_interval_seconds": 10,        # How often the poller collects data. Min 5.
    "retention_days": 90,               # How many days of raw samples to keep in SQLite.
    "auto_purge": True,                 # Automatically delete rows older than retention_days.
    "db_max_size_mb": 500,              # Warn (but don't purge) if DB exceeds this size.

    # ── Display / smoothing ───────────────────────────────────────────────────
    "smoothing_window_minutes": 5,      # Rolling average window for stat card primary value.
    "chart_show_raw": True,             # Show raw trace alongside smoothed on time-series charts.
    "chart_default_window_hours": 1,    # Default time window loaded on detail pages.

    # ── Alert thresholds ──────────────────────────────────────────────────────
    "offset_warning_ns": 100,           # PPS offset amber threshold (nanoseconds).
    "offset_critical_ns": 500,          # PPS offset red threshold (nanoseconds).
    "jitter_warning_ns": 50,            # Jitter amber threshold (nanoseconds).
    "jitter_critical_ns": 200,          # Jitter red threshold (nanoseconds).
    "dispersion_warning_us": 15,        # Root dispersion amber threshold (microseconds).
                                        # Default 15us — healthy PowerPi shows ~11us.
    "min_satellites_warning": 4,        # Fewer sats in fix triggers amber.
    "min_satellites_critical": 3,       # Fewer sats in fix triggers red.
    "cpu_temp_warning_c": 70,           # CPU temperature amber threshold (degrees C).
    "cpu_temp_critical_c": 80,          # CPU temperature red threshold (degrees C).
    "disk_warning_pct": 80,             # Disk usage amber threshold (percent).
    "disk_critical_pct": 90,            # Disk usage red threshold (percent).
    "offset_display_unit": "ns",        # Units for offset display: "ns" or "us".

    # ── gpsd / GNSS ───────────────────────────────────────────────────────────
    "gpsd_host": "localhost",
    "gpsd_port": 2947,
    "pps_device": "/dev/pps0",          # GPIO 12 hardware PPS — confirmed chrony reference.
    "elevation_mask_degrees": 10,       # Satellites below this elevation shown as inactive.
    "expected_constellations": "GPS,BeiDou",  # GLONASS monitored but not in default expected set.

    # ── Chrony / NTP ──────────────────────────────────────────────────────────
    "chronyc_path": "/usr/bin/chronyc",
    "reference_label": "GPS PPS",       # Friendly name for PPS source shown on all pages.
    "highlighted_peers": "",            # Comma-separated IPs/hostnames to highlight in sources table.

    # ── UI / display ──────────────────────────────────────────────────────────
    "site_name": "",                    # Shown in browser tab and nav bar. Auto-set from hostname if blank.
    "hostname": "",                     # Auto-populated from socket.gethostname() if blank.
    "default_theme": "system",          # "light", "dark", or "system".
    "default_landing_page": "overview",
    "show_position": True,              # If False, lat/lon/altitude hidden on GNSS page.
    "ui_refresh_interval_seconds": 10,  # How often browser pages poll the API. Min 5.

    # ── Reserved for v2 ───────────────────────────────────────────────────────
    "notifications": {},                # Placeholder — populated in v2 without schema migration.
}

# ── Validation rules ──────────────────────────────────────────────────────────
# Each entry: (type_or_types, min, max, allowed_values)
# None means no constraint on that dimension.

_VALIDATORS = {
    "poll_interval_seconds":      (int, 5, 3600, None),
    "retention_days":             (int, 1, 3650, None),
    "auto_purge":                 (bool, None, None, None),
    "db_max_size_mb":             (int, 10, 10000, None),
    "smoothing_window_minutes":   (int, 1, 60, None),
    "chart_show_raw":             (bool, None, None, None),
    "chart_default_window_hours": (int, 1, 720, None),
    "offset_warning_ns":          (int, 1, 1000000, None),
    "offset_critical_ns":         (int, 1, 1000000, None),
    "jitter_warning_ns":          (int, 1, 1000000, None),
    "jitter_critical_ns":         (int, 1, 1000000, None),
    "dispersion_warning_us":      ((int, float), 0, 1000, None),
    "min_satellites_warning":     (int, 1, 50, None),
    "min_satellites_critical":    (int, 1, 50, None),
    "cpu_temp_warning_c":         ((int, float), 0, 100, None),
    "cpu_temp_critical_c":        ((int, float), 0, 100, None),
    "disk_warning_pct":           (int, 1, 99, None),
    "disk_critical_pct":          (int, 1, 100, None),
    "offset_display_unit":        (str, None, None, ["ns", "us"]),
    "gpsd_port":                  (int, 1, 65535, None),
    "elevation_mask_degrees":     (int, 0, 45, None),
    "default_theme":              (str, None, None, ["light", "dark", "system"]),
    "ui_refresh_interval_seconds":(int, 5, 3600, None),
}

# ── Internal state ────────────────────────────────────────────────────────────
_config = {}
_mtime = 0.0


def _auto_populate(cfg):
    """Fill in values that are derived from the environment when left blank."""
    if not cfg.get("hostname"):
        cfg["hostname"] = socket.gethostname()
    if not cfg.get("site_name"):
        cfg["site_name"] = f"{cfg['hostname']} Stratum-1"
    return cfg


def _validate(key, value):
    """
    Validate a single key/value pair against _VALIDATORS.
    Returns (True, value) if valid (possibly coerced),
    or (False, reason_string) if invalid.
    """
    if key not in _VALIDATORS:
        return True, value  # Unknown keys pass through unchanged.

    expected_type, vmin, vmax, allowed = _VALIDATORS[key]

    # Type check — attempt coercion for common mismatches (e.g. JSON int as float).
    if not isinstance(value, expected_type):
        if isinstance(expected_type, tuple):
            primary_type = expected_type[0]
        else:
            primary_type = expected_type
        try:
            value = primary_type(value)
        except (ValueError, TypeError):
            return False, f"expected {expected_type}, got {type(value).__name__}"

    # Range check.
    if vmin is not None and value < vmin:
        value = vmin
        logger.warning("Config key '%s' below minimum %s — clamped to %s", key, vmin, vmin)
    if vmax is not None and value > vmax:
        value = vmax
        logger.warning("Config key '%s' above maximum %s — clamped to %s", key, vmax, vmax)

    # Allowed values check.
    if allowed is not None and value not in allowed:
        return False, f"must be one of {allowed}, got '{value}'"

    return True, value


def _load_from_disk():
    """
    Read config.json, merge over defaults, validate, auto-populate,
    and write back the complete merged config.
    Returns the merged config dict.
    """
    global _mtime

    cfg = dict(DEFAULTS)  # Start from a fresh copy of defaults.

    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, "r") as f:
                on_disk = json.load(f)
            # Merge file values over defaults, skipping unknown keys.
            for key, value in on_disk.items():
                if key in DEFAULTS:
                    ok, coerced = _validate(key, value)
                    if ok:
                        cfg[key] = coerced
                    else:
                        logger.warning(
                            "Config key '%s' invalid (%s) — using default %s",
                            key, coerced, DEFAULTS[key]
                        )
                elif key == "notifications":
                    # Preserve v2 placeholder dict as-is.
                    cfg["notifications"] = value
                else:
                    logger.debug("Unknown config key '%s' ignored", key)
            _mtime = os.path.getmtime(_CONFIG_PATH)
            logger.debug("config.json loaded from %s", _CONFIG_PATH)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to read config.json (%s) — using defaults", exc)
            _mtime = 0.0
    else:
        logger.info("config.json not found — creating with defaults at %s", _CONFIG_PATH)
        _mtime = 0.0

    cfg = _auto_populate(cfg)

    # Always write back so config.json is complete with all keys.
    _write_to_disk(cfg)

    return cfg


def _write_to_disk(cfg):
    """Write the config dict to config.json, pretty-printed."""
    global _mtime
    try:
        with open(_CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        _mtime = os.path.getmtime(_CONFIG_PATH)
        logger.debug("config.json written to %s", _CONFIG_PATH)
    except OSError as exc:
        logger.error("Failed to write config.json: %s", exc)


# ── Public API ────────────────────────────────────────────────────────────────

def load():
    """
    Load (or reload) configuration from disk.
    Called once at startup; subsequent calls via reload() are mtime-gated.
    """
    global _config
    _config = _load_from_disk()


def get(key, default=None):
    """Return the current value for a config key, or default if not found."""
    if not _config:
        load()
    return _config.get(key, default)


def get_all():
    """Return a copy of the full config dict (safe to pass to JSON responses)."""
    if not _config:
        load()
    return dict(_config)


def save(updates):
    """
    Merge updates dict into the current config and write to disk.
    Validates each key before accepting. Returns a dict of
    { key: "ok" | "invalid: reason" | "unknown" } for each key in updates.
    """
    if not _config:
        load()

    results = {}
    for key, value in updates.items():
        if key == "notifications":
            _config["notifications"] = value
            results[key] = "ok"
        elif key not in DEFAULTS:
            results[key] = "unknown"
        else:
            ok, coerced = _validate(key, value)
            if ok:
                _config[key] = coerced
                results[key] = "ok"
            else:
                results[key] = f"invalid: {coerced}"

    _auto_populate(_config)
    _write_to_disk(_config)
    return results


def needs_reload():
    """
    Return True if config.json has been modified on disk since last load.
    Called by the poller on each cycle — cheap (stat call only).
    """
    try:
        current_mtime = os.path.getmtime(_CONFIG_PATH)
        return current_mtime > _mtime
    except OSError:
        return False


def reload():
    """Re-read config.json from disk. Called by the poller when needs_reload() is True."""
    logger.info("Config file changed — reloading")
    load()


def get_expected_constellations():
    """
    Return the expected_constellations value as a list of stripped strings.
    e.g. "GPS,BeiDou" -> ["GPS", "BeiDou"]
    """
    raw = get("expected_constellations", "GPS,BeiDou")
    return [c.strip() for c in raw.split(",") if c.strip()]


def get_highlighted_peers():
    """
    Return the highlighted_peers value as a list of stripped strings.
    e.g. "192.168.1.1,pool.ntp.org" -> ["192.168.1.1", "pool.ntp.org"]
    """
    raw = get("highlighted_peers", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import pprint

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(levelname)s  %(message)s"
    )

    print("=" * 60)
    print("config.py — standalone test")
    print("=" * 60)
    from paths import summary as path_summary, ROOT
    print("Paths:")
    import pprint
    pprint.pprint(path_summary())
    print()

    load()

    print("Loaded config:")
    pprint.pprint(_config, width=60)
    print()
    print(f"hostname        : {get('hostname')}")
    print(f"site_name       : {get('site_name')}")
    print(f"poll_interval   : {get('poll_interval_seconds')}s")
    print(f"constellations  : {get_expected_constellations()}")
    print(f"needs_reload()  : {needs_reload()}")
    print()

    # Test validation — try saving a bad value and a good value.
    print("Testing save() with mixed valid/invalid values:")
    results = save({
        "poll_interval_seconds": 3,       # Below minimum — should clamp to 5.
        "offset_display_unit": "invalid", # Not in allowed list — should reject.
        "site_name": "Test Site",          # Valid string — should accept.
        "unknown_key_xyz": "foo",          # Unknown — should report unknown.
    })
    pprint.pprint(results)
    print(f"poll_interval after clamped save: {get('poll_interval_seconds')}s")
    print()
    print("config.json written to disk. Run: cat config.json to verify.")
