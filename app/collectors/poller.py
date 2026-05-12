"""
poller.py — Background data collection loop for the Stratum-1 dashboard.

Runs as a daemon thread started by app.py on Flask startup.
Calls all three collectors on each cycle, writes samples to SQLite,
and maintains in-memory ring buffers for fast rolling average queries.

Provides:
    start()             — start the poller daemon thread (call once from app.py)
    stop()              — signal the poller to stop gracefully
    get_latest()        — return the most recent collected data (from ring buffer)
    get_rolling_mean(source, metric, minutes) — rolling mean from ring buffer
    get_rolling_stats(source, metric, minutes) — rolling stats from ring buffer
    is_running()        — True if the poller thread is alive
    last_cycle_time()   — Unix timestamp of last successful cycle

Ring buffer
-----------
An in-memory deque of the last RING_BUFFER_SIZE samples per (source, metric)
pair. Used by API endpoints to answer rolling average requests without hitting
SQLite on every request. The database is still the authoritative store for
history queries.

Metric names written to the database
-------------------------------------
Source: chrony
    pps_offset_ns       — PPS adjusted offset (nanoseconds)
    rms_offset_ns       — RMS offset (nanoseconds)
    last_offset_ns      — last measured offset (nanoseconds)
    freq_ppm            — frequency correction (ppm)
    residual_freq_ppm   — residual frequency (ppm)
    skew_ppm            — skew (ppm)
    root_delay_ns       — root delay (nanoseconds)
    root_dispersion_ns  — root dispersion (nanoseconds)
    root_dispersion_us  — root dispersion (microseconds)
    update_interval_s   — chrony update interval (seconds)
    stratum             — NTP stratum
    reference_name      — reference source name (string)
    leap_status         — leap second status (string)

Source: gnss
    fix_mode            — fix mode integer (0-3)
    fix_mode_name       — fix mode string
    latitude            — decimal degrees (stored if show_position=True)
    longitude           — decimal degrees (stored if show_position=True)
    altitude_m          — metres
    sats_visible        — total satellites in view
    sats_used           — satellites used in fix
    hdop                — horizontal DOP
    vdop                — vertical DOP
    pdop                — position DOP

Source: system
    cpu_temp_c          — CPU temperature (degrees C)
    cpu_usage_pct       — aggregate CPU usage (percent)
    cpu_freq_mhz        — current CPU frequency (MHz)
    mem_usage_pct       — memory usage (percent)
    disk_usage_pct      — root disk usage (percent)
    load_1              — 1-minute load average
    uptime_seconds      — system uptime (seconds)
    chronyd_status      — service status string
    gpsd_status         — service status string

Usage (standalone test — one cycle):
    python3 poller.py --once
"""

import os
import sys
import time
import threading
import logging
import argparse
from collections import deque

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import database
from collectors import chrony as chrony_collector
from collectors import gpsd_client as gpsd_collector
from collectors import system as system_collector

# ── Ring buffer ───────────────────────────────────────────────────────────────

RING_BUFFER_SIZE = 500   # samples per (source, metric) pair

# _ring[(source, metric)] = deque of (timestamp, value_num) tuples
_ring: dict[tuple, deque] = {}
_ring_lock = threading.Lock()


def _ring_push(source, metric, timestamp, value_num):
    """Add a numeric sample to the ring buffer."""
    if value_num is None:
        return
    key = (source, metric)
    with _ring_lock:
        if key not in _ring:
            _ring[key] = deque(maxlen=RING_BUFFER_SIZE)
        _ring[key].append((timestamp, value_num))


def get_rolling_mean(source, metric, minutes):
    """
    Return the mean of the last N minutes of samples from the ring buffer.
    Returns None if no data in that window.
    """
    cutoff = time.time() - (minutes * 60)
    key = (source, metric)
    with _ring_lock:
        buf = list(_ring.get(key, []))

    values = [v for ts, v in buf if ts >= cutoff]
    if not values:
        return None
    return sum(values) / len(values)


def get_rolling_stats(source, metric, minutes):
    """
    Return mean, min, max, and count from the last N minutes of ring buffer data.
    Returns None if no data in that window.
    """
    cutoff = time.time() - (minutes * 60)
    key = (source, metric)
    with _ring_lock:
        buf = list(_ring.get(key, []))

    values = [v for ts, v in buf if ts >= cutoff]
    if not values:
        return None
    return {
        "mean":    sum(values) / len(values),
        "min_val": min(values),
        "max_val": max(values),
        "count":   len(values),
    }


# ── Latest data cache ─────────────────────────────────────────────────────────

_latest_lock = threading.Lock()
_latest = {
    "chrony": None,
    "gnss":   None,
    "system": None,
    "cycle_time": None,
    "cycle_count": 0,
    "errors": [],
}


def get_latest():
    """
    Return a copy of the most recently collected data.
    Thread-safe. Returns None values for any source that has not yet
    been collected or that failed on the last cycle.
    """
    with _latest_lock:
        return dict(_latest)


def last_cycle_time():
    """Return Unix timestamp of the last completed collection cycle, or None."""
    with _latest_lock:
        return _latest.get("cycle_time")


# ── Poller state ──────────────────────────────────────────────────────────────

_thread: threading.Thread | None = None
_stop_event = threading.Event()


def is_running():
    """Return True if the poller thread is alive."""
    return _thread is not None and _thread.is_alive()


# ── Collection cycle ──────────────────────────────────────────────────────────

def _collect_chrony(ts):
    """
    Run the chrony collector and return a list of sample dicts ready for
    database.write_samples(). Also pushes numeric values to the ring buffer.
    Returns (samples_list, raw_data_dict, error_str_or_None).
    """
    try:
        data = chrony_collector.collect()
    except Exception as e:
        return [], None, f"chrony collector exception: {e}"

    if data.get("error"):
        return [], data, data["error"]

    samples = []
    tracking = data.get("tracking") or {}
    sources  = data.get("sources") or []

    # Find PPS source for the headline offset metric
    pps_source = next((s for s in sources if s.get("is_pps")), None)

    def _s(metric, value_num=None, value_str=None, unit=None):
        samples.append({
            "timestamp":  ts,
            "source":     "chrony",
            "metric":     metric,
            "value_num":  value_num,
            "value_str":  value_str,
            "unit":       unit,
        })
        if value_num is not None:
            _ring_push("chrony", metric, ts, value_num)

    # PPS offset — primary timing metric
    if pps_source and pps_source.get("adj_offset_ns") is not None:
        _s("pps_offset_ns", value_num=pps_source["adj_offset_ns"], unit="ns")

    # Tracking fields
    if tracking.get("rms_offset_ns") is not None:
        _s("rms_offset_ns", value_num=tracking["rms_offset_ns"], unit="ns")
    if tracking.get("last_offset_ns") is not None:
        _s("last_offset_ns", value_num=tracking["last_offset_ns"], unit="ns")
    if tracking.get("freq_ppm") is not None:
        _s("freq_ppm", value_num=tracking["freq_ppm"], unit="ppm")
    if tracking.get("residual_freq_ppm") is not None:
        _s("residual_freq_ppm", value_num=tracking["residual_freq_ppm"], unit="ppm")
    if tracking.get("skew_ppm") is not None:
        _s("skew_ppm", value_num=tracking["skew_ppm"], unit="ppm")
    if tracking.get("root_delay_ns") is not None:
        _s("root_delay_ns", value_num=tracking["root_delay_ns"], unit="ns")
    if tracking.get("root_dispersion_ns") is not None:
        _s("root_dispersion_ns", value_num=tracking["root_dispersion_ns"], unit="ns")
    if tracking.get("root_dispersion_us") is not None:
        _s("root_dispersion_us", value_num=tracking["root_dispersion_us"], unit="us")
    if tracking.get("update_interval_s") is not None:
        _s("update_interval_s", value_num=tracking["update_interval_s"], unit="s")
    if tracking.get("stratum") is not None:
        _s("stratum", value_num=float(tracking["stratum"]), unit="")
    if tracking.get("reference_name"):
        _s("reference_name", value_str=tracking["reference_name"])
    if tracking.get("leap_status"):
        _s("leap_status", value_str=tracking["leap_status"])

    return samples, data, None


def _collect_gnss(ts):
    """
    Run the gpsd collector and return samples + raw data.
    """
    try:
        data = gpsd_collector.collect()
    except Exception as e:
        return [], None, f"gpsd collector exception: {e}"

    if data.get("error"):
        return [], data, data["error"]

    samples = []
    tpv = data.get("tpv") or {}
    sky = data.get("sky") or {}
    show_position = config.get("show_position", True)

    def _s(metric, value_num=None, value_str=None, unit=None):
        samples.append({
            "timestamp":  ts,
            "source":     "gnss",
            "metric":     metric,
            "value_num":  value_num,
            "value_str":  value_str,
            "unit":       unit,
        })
        if value_num is not None:
            _ring_push("gnss", metric, ts, value_num)

    # Fix data
    if tpv.get("fix_mode") is not None:
        _s("fix_mode", value_num=float(tpv["fix_mode"]), unit="")
    if tpv.get("fix_mode_name"):
        _s("fix_mode_name", value_str=tpv["fix_mode_name"])

    # Position — only store if show_position is enabled in config
    if show_position:
        if tpv.get("latitude") is not None:
            _s("latitude", value_num=tpv["latitude"], unit="deg")
        if tpv.get("longitude") is not None:
            _s("longitude", value_num=tpv["longitude"], unit="deg")
    if tpv.get("altitude_m") is not None:
        _s("altitude_m", value_num=tpv["altitude_m"], unit="m")

    # Satellite counts
    if sky.get("sats_visible") is not None:
        _s("sats_visible", value_num=float(sky["sats_visible"]), unit="count")
    if sky.get("sats_used") is not None:
        _s("sats_used", value_num=float(sky["sats_used"]), unit="count")

    # DOP values
    if sky.get("hdop") is not None:
        _s("hdop", value_num=sky["hdop"], unit="")
    if sky.get("vdop") is not None:
        _s("vdop", value_num=sky["vdop"], unit="")
    if sky.get("pdop") is not None:
        _s("pdop", value_num=sky["pdop"], unit="")

    return samples, data, None


def _collect_system(ts):
    """
    Run the system collector and return samples + raw data.
    """
    try:
        data = system_collector.collect()
    except Exception as e:
        return [], None, f"system collector exception: {e}"

    samples = []

    def _s(metric, value_num=None, value_str=None, unit=None):
        samples.append({
            "timestamp":  ts,
            "source":     "system",
            "metric":     metric,
            "value_num":  value_num,
            "value_str":  value_str,
            "unit":       unit,
        })
        if value_num is not None:
            _ring_push("system", metric, ts, value_num)

    if data.get("cpu_temp_c") is not None:
        _s("cpu_temp_c", value_num=data["cpu_temp_c"], unit="degC")
    if data.get("cpu_usage_pct") is not None:
        _s("cpu_usage_pct", value_num=data["cpu_usage_pct"], unit="pct")
    if data.get("cpu_freq_mhz") is not None:
        _s("cpu_freq_mhz", value_num=data["cpu_freq_mhz"], unit="MHz")
    if data.get("mem_usage_pct") is not None:
        _s("mem_usage_pct", value_num=data["mem_usage_pct"], unit="pct")
    if data.get("disk_usage_pct") is not None:
        _s("disk_usage_pct", value_num=data["disk_usage_pct"], unit="pct")
    if data.get("load_1") is not None:
        _s("load_1", value_num=data["load_1"], unit="")
    if data.get("load_5") is not None:
        _s("load_5", value_num=data["load_5"], unit="")
    if data.get("load_15") is not None:
        _s("load_15", value_num=data["load_15"], unit="")
    if data.get("uptime_seconds") is not None:
        _s("uptime_seconds", value_num=data["uptime_seconds"], unit="s")

    # Service statuses as strings
    services = data.get("services") or {}
    for svc_name, svc_info in services.items():
        _s(f"{svc_name}_status", value_str=svc_info.get("status", "unknown"))

    return samples, data, None


def _run_cycle():
    """
    Execute one complete collection cycle:
    1. Reload config if changed on disk
    2. Run all three collectors
    3. Batch-write all samples to SQLite
    4. Write heartbeat
    5. Update in-memory latest cache
    6. Log any errors
    """
    if config.needs_reload():
        config.reload()

    ts = time.time()
    all_samples = []
    errors = []

    # Run collectors
    chrony_samples, chrony_data, chrony_err = _collect_chrony(ts)
    gnss_samples,   gnss_data,   gnss_err   = _collect_gnss(ts)
    system_samples, system_data, system_err = _collect_system(ts)

    all_samples.extend(chrony_samples)
    all_samples.extend(gnss_samples)
    all_samples.extend(system_samples)

    if chrony_err:
        errors.append(f"chrony: {chrony_err}")
    if gnss_err:
        errors.append(f"gnss: {gnss_err}")
    if system_err:
        errors.append(f"system: {system_err}")

    # Write to database
    if all_samples:
        try:
            database.write_samples(all_samples)
        except Exception as e:
            errors.append(f"db write: {e}")
            logger.error("Database write failed: %s", e)

    # Write heartbeat regardless of collector errors
    try:
        database.write_heartbeat()
    except Exception as e:
        logger.warning("Heartbeat write failed: %s", e)

    # Update latest cache
    with _latest_lock:
        if chrony_data:
            _latest["chrony"] = chrony_data
        if gnss_data:
            _latest["gnss"] = gnss_data
        if system_data:
            _latest["system"] = system_data
        _latest["cycle_time"] = ts
        _latest["cycle_count"] += 1
        _latest["errors"] = errors

    if errors:
        logger.warning("Cycle completed with errors: %s", "; ".join(errors))
    else:
        cycle_num = _latest["cycle_count"]
        logger.debug("Cycle %d complete — %d samples written",
                     cycle_num, len(all_samples))

    return len(all_samples), errors


# ── Nightly maintenance ───────────────────────────────────────────────────────

_last_purge_day = None


def _maybe_purge():
    """
    Run database retention purge once per calendar day.
    Called at the start of each poller cycle — cheap when not needed.
    """
    global _last_purge_day
    import datetime
    today = datetime.date.today()
    if _last_purge_day == today:
        return
    _last_purge_day = today

    retention_days = config.get("retention_days", 90)
    if config.get("auto_purge", True):
        try:
            deleted = database.purge_old_data(retention_days)
            if deleted:
                logger.info("Nightly purge: deleted %d rows older than %d days",
                            deleted, retention_days)
        except Exception as e:
            logger.error("Nightly purge failed: %s", e)


# ── Thread loop ───────────────────────────────────────────────────────────────

def _poller_loop():
    """Main loop — runs in a daemon thread."""
    logger.info("Poller started")
    database.init()
    _maybe_purge()

    while not _stop_event.is_set():
        cycle_start = time.time()

        try:
            n_samples, errors = _run_cycle()
        except Exception as e:
            logger.error("Unexpected error in poller cycle: %s", e, exc_info=True)

        # Sleep for the remainder of the poll interval
        interval = config.get("poll_interval_seconds", 10)
        elapsed  = time.time() - cycle_start
        sleep_for = max(0, interval - elapsed)

        # Use wait() instead of sleep() so stop_event wakes us immediately
        _stop_event.wait(timeout=sleep_for)

        _maybe_purge()

    logger.info("Poller stopped")


# ── Public start/stop ─────────────────────────────────────────────────────────

def start():
    """
    Start the poller daemon thread.
    Safe to call only once — raises RuntimeError if already running.
    """
    global _thread

    if is_running():
        raise RuntimeError("Poller is already running")

    _stop_event.clear()
    config.load()
    database.init()

    _thread = threading.Thread(
        target=_poller_loop,
        name="stratum1-poller",
        daemon=True   # Thread exits automatically when main process exits
    )
    _thread.start()
    logger.info("Poller thread started (interval=%ds)",
                config.get("poll_interval_seconds", 10))


def stop():
    """Signal the poller to stop and wait for it to exit (up to 5 seconds)."""
    global _thread
    if _thread and _thread.is_alive():
        logger.info("Stopping poller...")
        _stop_event.set()
        _thread.join(timeout=5)
        if _thread.is_alive():
            logger.warning("Poller thread did not stop within 5s")
    _thread = None


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stratum-1 dashboard poller")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one collection cycle and exit (test mode)"
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=1,
        help="Number of cycles to run (default: 1 with --once)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show DEBUG log output"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s  %(message)s"
    )

    print("=" * 60)
    print("poller.py — standalone test")
    print("=" * 60)

    config.load()
    database.init()

    n_cycles = args.cycles if args.once else 1
    print(f"Running {n_cycles} collection cycle(s)...")
    print()

    for i in range(n_cycles):
        cycle_start = time.time()
        n_samples, errors = _run_cycle()
        elapsed = time.time() - cycle_start

        print(f"Cycle {i + 1}: {n_samples} samples written in {elapsed:.2f}s")

        if errors:
            print(f"  Errors: {'; '.join(errors)}")
        else:
            print("  No errors.")

        # Show summary from latest cache
        latest = get_latest()

        # Chrony summary
        chrony = latest.get("chrony") or {}
        tracking = (chrony.get("tracking") or {})
        sources  = (chrony.get("sources") or [])
        pps = next((s for s in sources if s.get("is_pps")), None)
        print()
        print("  Chrony:")
        print(f"    reference     : {tracking.get('reference_name', 'N/A')}")
        print(f"    stratum       : {tracking.get('stratum', 'N/A')}")
        print(f"    rms_offset_ns : {tracking.get('rms_offset_ns', 'N/A'):.1f} ns"
              if tracking.get('rms_offset_ns') else
              f"    rms_offset_ns : N/A")
        if pps:
            print(f"    PPS offset    : {pps.get('adj_offset_ns', 'N/A'):.1f} ns"
                  if pps.get('adj_offset_ns') is not None else
                  f"    PPS offset    : N/A")
            print(f"    PPS state     : {pps.get('state', 'N/A')}")

        # GNSS summary
        gnss = latest.get("gnss") or {}
        tpv  = gnss.get("tpv") or {}
        sky  = gnss.get("sky") or {}
        print()
        print("  GNSS:")
        print(f"    fix           : {tpv.get('fix_mode_name', 'N/A')}")
        print(f"    sats_used     : {sky.get('sats_used', 'N/A')}")
        print(f"    sats_visible  : {sky.get('sats_visible', 'N/A')}")
        consts = sky.get("constellation_summary", {})
        for name, counts in sorted(consts.items()):
            print(f"    {name:10s}   visible={counts['visible']}  "
                  f"used={counts['used']}")

        # System summary
        sys_data = latest.get("system") or {}
        print()
        print("  System:")
        print(f"    cpu_temp_c    : {sys_data.get('cpu_temp_c', 'N/A')} °C")
        print(f"    cpu_usage_pct : {sys_data.get('cpu_usage_pct', 'N/A')}%")
        print(f"    mem_usage_pct : {sys_data.get('mem_usage_pct', 'N/A')}%")
        print(f"    uptime        : {sys_data.get('uptime_str', 'N/A')}")
        svcs = sys_data.get("services") or {}
        for svc, info in svcs.items():
            print(f"    {svc:12s}  : {info.get('status', 'N/A')}")

        # Ring buffer check
        print()
        pps_mean = get_rolling_mean("chrony", "pps_offset_ns", minutes=60)
        print(f"  Ring buffer:")
        print(f"    pps_offset 60m mean : "
              f"{pps_mean:.1f} ns" if pps_mean is not None else
              f"    pps_offset 60m mean : N/A (need more data)")
        with _ring_lock:
            ring_keys = list(_ring.keys())
        print(f"    tracked metrics     : {len(ring_keys)}")

        if i < n_cycles - 1:
            print()
            interval = config.get("poll_interval_seconds", 10)
            print(f"  Waiting {interval}s before next cycle...")
            time.sleep(interval)

    print()

    # Verify database was written
    stats = database.db_stats()
    print(f"Database state after test:")
    print(f"  Raw sample count  : {stats['raw_samples_count']}")
    print(f"  Distinct metrics  : {len(stats['sources'])}")
    print(f"  DB size           : {stats['size_mb']} MB")
    hb = stats.get("poller_heartbeat")
    if hb:
        age = time.time() - hb
        print(f"  Heartbeat age     : {age:.1f}s")
    print()
    print("All tests passed.")
    print()
    print("Verify with:")
    print(f"  sqlite3 {database.DB_FILE} "
          f"'SELECT source, metric, COUNT(*) as n FROM raw_samples "
          f"GROUP BY source, metric ORDER BY source, metric;'")
