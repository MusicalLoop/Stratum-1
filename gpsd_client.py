"""
collectors/gpsd_client.py — gpsd JSON protocol client.

Connects to gpsd on the configured host:port, reads one TPV and one SKY
message, and returns structured data for storage and display.

Provides:
    collect()       — read TPV + SKY, return combined dict
    read_tpv()      — read one TPV message, return parsed dict
    read_sky()      — read one SKY message, return parsed dict

gpsd JSON protocol classes used:
    TPV — Time, Position, Velocity fix data
    SKY — Satellite sky view with azimuth, elevation, SNR, used flag

Constellation mapping (gnssid from gpsd):
    0 = GPS
    1 = SBAS
    2 = Galileo
    3 = BeiDou
    5 = QZSS
    6 = GLONASS

Fix mode mapping (gpsd TPV mode field):
    0 = Unknown
    1 = No fix
    2 = 2D fix
    3 = 3D fix

Usage (standalone test):
    python3 collectors/gpsd_client.py
"""

import os
import sys
import logging
import socket
import json
import time

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import config
    def _get_host(): return config.get("gpsd_host", "localhost")
    def _get_port(): return config.get("gpsd_port", 2947)
except ImportError:
    def _get_host(): return "localhost"
    def _get_port(): return 2947

# ── Constants ─────────────────────────────────────────────────────────────────

GNSSID_NAMES = {
    0: "GPS",
    1: "SBAS",
    2: "Galileo",
    3: "BeiDou",
    5: "QZSS",
    6: "GLONASS",
}

FIX_MODE_NAMES = {
    0: "Unknown",
    1: "No fix",
    2: "2D",
    3: "3D",
}

# Timeout waiting for a specific gpsd message class (seconds)
_READ_TIMEOUT = 10


# ── Low-level gpsd socket reader ──────────────────────────────────────────────

class _GpsdReader:
    """
    Minimal gpsd JSON protocol client.

    Opens a TCP connection to gpsd, sends ?WATCH to start streaming,
    and reads newline-delimited JSON messages.

    Used as a context manager:
        with _GpsdReader(host, port) as r:
            msg = r.read_class("TPV", timeout=10)
    """

    def __init__(self, host, port, timeout=15):
        self._host = host
        self._port = port
        self._timeout = timeout
        self._sock = None
        self._buf = ""

    def __enter__(self):
        self._sock = socket.create_connection(
            (self._host, self._port),
            timeout=self._timeout
        )
        # Enable JSON streaming
        self._sock.sendall(b'?WATCH={"enable":true,"json":true};\n')
        return self

    def __exit__(self, *args):
        if self._sock:
            try:
                self._sock.sendall(b'?WATCH={"enable":false};\n')
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _readline(self):
        """Read one newline-terminated JSON line from the socket."""
        deadline = time.time() + self._timeout
        while "\n" not in self._buf:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"gpsd read timed out after {self._timeout}s"
                )
            self._sock.settimeout(min(remaining, 2.0))
            try:
                chunk = self._sock.recv(4096).decode("utf-8", errors="replace")
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("gpsd connection closed unexpectedly")
            self._buf += chunk

        line, self._buf = self._buf.split("\n", 1)
        return line.strip()

    def read_class(self, class_name, timeout=None):
        """
        Read messages until one with class == class_name is found.
        Returns the parsed dict, or raises TimeoutError.

        Skips VERSION, DEVICES, WATCH, and other non-data messages.
        """
        deadline = time.time() + (timeout or self._timeout)
        while time.time() < deadline:
            line = self._readline()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("gpsd non-JSON line: %s", line[:80])
                continue
            if msg.get("class") == class_name:
                return msg
            logger.debug("gpsd skipped class=%s", msg.get("class"))
        raise TimeoutError(
            f"No {class_name} message received within {timeout or self._timeout}s"
        )

    def read_classes(self, class_names, timeout=None):
        """
        Read messages until all classes in class_names have been seen.
        Returns a dict {class_name: message}.
        """
        results = {}
        deadline = time.time() + (timeout or self._timeout)
        remaining_classes = set(class_names)

        while remaining_classes and time.time() < deadline:
            line = self._readline()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            cls = msg.get("class")
            if cls in remaining_classes:
                results[cls] = msg
                remaining_classes.discard(cls)
                logger.debug("gpsd received %s", cls)

        if remaining_classes:
            raise TimeoutError(
                f"Missing gpsd messages: {remaining_classes}"
            )
        return results

    def read_best_sky(self, n_messages=3, timeout=None):
        """
        Read N SKY messages and return the one with the most satellites.

        The LC76G sends constellation data in two separate NMEA bursts per
        update cycle — GPS+BeiDou first, then GLONASS — which gpsd assembles
        into alternating SKY objects (13 sats, then 21 sats). Reading just the
        first SKY message misses GLONASS on every other poll cycle.

        By reading N messages and taking the largest, we always capture the
        full constellation picture regardless of which phase we land on.
        """
        best = None
        deadline = time.time() + (timeout or self._timeout)
        seen = 0

        while seen < n_messages and time.time() < deadline:
            line = self._readline()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("class") == "SKY":
                seen += 1
                n_sats = len(msg.get("satellites", []))
                if best is None or n_sats > len(best.get("satellites", [])):
                    best = msg
                    logger.debug("gpsd SKY %d/%d: %d sats (new best)",
                                 seen, n_messages, n_sats)
                else:
                    logger.debug("gpsd SKY %d/%d: %d sats (kept previous best)",
                                 seen, n_messages, n_sats)

        if best is None:
            raise TimeoutError(
                f"No SKY message received within {timeout or self._timeout}s"
            )
        return best


# ── TPV parser ────────────────────────────────────────────────────────────────

def _parse_tpv(msg):
    """
    Parse a gpsd TPV message dict into a structured result dict.

    Keys:
        fix_mode        int   — 0=Unknown, 1=NoFix, 2=2D, 3=3D
        fix_mode_name   str   — "No fix", "2D", "3D"
        latitude        float or None — decimal degrees
        longitude       float or None — decimal degrees
        altitude_m      float or None — metres above WGS84
        speed_ms        float or None — speed in m/s
        time_utc        str or None   — ISO 8601 UTC time string
        eph             float or None — horizontal position error (m)
        epv             float or None — vertical position error (m)
        has_fix         bool
    """
    mode = msg.get("mode", 0)
    has_fix = mode >= 2

    return {
        "fix_mode":      mode,
        "fix_mode_name": FIX_MODE_NAMES.get(mode, f"Mode{mode}"),
        "latitude":      msg.get("lat"),
        "longitude":     msg.get("lon"),
        "altitude_m":    msg.get("alt"),
        "speed_ms":      msg.get("speed"),
        "time_utc":      msg.get("time"),
        "eph":           msg.get("eph"),
        "epv":           msg.get("epv"),
        "has_fix":       has_fix,
    }


# ── SKY parser ────────────────────────────────────────────────────────────────

def _parse_sky(msg):
    """
    Parse a gpsd SKY message dict into a structured result dict.

    Keys:
        hdop            float or None
        vdop            float or None
        pdop            float or None
        satellites      list of satellite dicts (see below)
        sats_visible    int — total satellites in view
        sats_used       int — satellites used in fix
        constellation_summary  dict — {name: {visible, used}} per constellation

    Each satellite dict:
        prn             int   — satellite PRN/ID number
        gnssid          int   — constellation ID (0=GPS, 3=BeiDou, 6=GLONASS…)
        constellation   str   — constellation name
        azimuth         float or None — degrees 0-360
        elevation       float or None — degrees 0-90
        snr             float or None — signal/noise ratio dBHz
        used            bool  — used in current fix
        svid            int or None — satellite vehicle ID within constellation
    """
    raw_sats = msg.get("satellites", [])
    satellites = []

    for s in raw_sats:
        gnssid = s.get("gnssid", 0)
        constellation = GNSSID_NAMES.get(gnssid, f"GNSS{gnssid}")
        used = bool(s.get("used", False))

        # gpsd may return ss=0 for sats with no signal — treat as None
        snr_raw = s.get("ss")
        snr = float(snr_raw) if snr_raw and snr_raw > 0 else None

        satellites.append({
            "prn":          s.get("PRN", s.get("prn")),
            "gnssid":       gnssid,
            "constellation": constellation,
            "azimuth":      s.get("az"),
            "elevation":    s.get("el"),
            "snr":          snr,
            "used":         used,
            "svid":         s.get("svid"),
        })

    sats_used = sum(1 for s in satellites if s["used"])

    # Build per-constellation summary
    constellation_summary = {}
    for s in satellites:
        name = s["constellation"]
        if name not in constellation_summary:
            constellation_summary[name] = {"visible": 0, "used": 0}
        constellation_summary[name]["visible"] += 1
        if s["used"]:
            constellation_summary[name]["used"] += 1

    return {
        "hdop":                  msg.get("hdop"),
        "vdop":                  msg.get("vdop"),
        "pdop":                  msg.get("pdop"),
        "satellites":            satellites,
        "sats_visible":          len(satellites),
        "sats_used":             sats_used,
        "constellation_summary": constellation_summary,
    }


# ── Public API ────────────────────────────────────────────────────────────────

def read_tpv(host=None, port=None, timeout=_READ_TIMEOUT):
    """
    Connect to gpsd, read one TPV message, return parsed dict.
    Raises RuntimeError on connection failure or timeout.
    """
    host = host or _get_host()
    port = port or _get_port()
    try:
        with _GpsdReader(host, port, timeout=timeout + 2) as r:
            msg = r.read_class("TPV", timeout=timeout)
        return _parse_tpv(msg)
    except (OSError, ConnectionError) as e:
        raise RuntimeError(f"gpsd connection failed ({host}:{port}): {e}")
    except TimeoutError as e:
        raise RuntimeError(f"gpsd timeout: {e}")


def read_sky(host=None, port=None, timeout=_READ_TIMEOUT):
    """
    Connect to gpsd, read one SKY message, return parsed dict.
    Raises RuntimeError on connection failure or timeout.
    """
    host = host or _get_host()
    port = port or _get_port()
    try:
        with _GpsdReader(host, port, timeout=timeout + 2) as r:
            msg = r.read_class("SKY", timeout=timeout)
        return _parse_sky(msg)
    except (OSError, ConnectionError) as e:
        raise RuntimeError(f"gpsd connection failed ({host}:{port}): {e}")
    except TimeoutError as e:
        raise RuntimeError(f"gpsd timeout: {e}")


def collect(host=None, port=None, timeout=_READ_TIMEOUT):
    """
    Connect to gpsd once, read TPV and the best available SKY message.

    Reads 3 consecutive SKY messages and returns the one with the most
    satellites. This handles the LC76G's split NMEA transmission pattern
    where GPS+BeiDou and GLONASS are sent in alternating bursts, causing
    gpsd to emit alternating 13-sat and 21-sat SKY objects. Taking the
    largest of 3 ensures all constellations are always captured.

    Returns:
    {
        "tpv":   { fix_mode, latitude, longitude, ... }
        "sky":   { hdop, satellites, sats_used, ... }
        "error": None | str
    }
    """
    host = host or _get_host()
    port = port or _get_port()

    result = {"tpv": None, "sky": None, "error": None}

    try:
        with _GpsdReader(host, port, timeout=timeout + 2) as r:
            # Read TPV first (one message sufficient for position/fix)
            # Read 3 SKY messages and take the most complete one.
            # The LC76G sends GPS+BeiDou and GLONASS in alternating bursts,
            # producing alternating 13-sat and 21-sat SKY objects.
            # Taking the largest of 3 messages always captures all constellations.
            tpv_msg = r.read_class("TPV", timeout=timeout)
            sky_msg = r.read_best_sky(n_messages=3, timeout=timeout)
        result["tpv"] = _parse_tpv(tpv_msg)
        result["sky"] = _parse_sky(sky_msg)
    except (OSError, ConnectionError) as e:
        result["error"] = f"gpsd connection failed ({host}:{port}): {e}"
        logger.warning("gpsd collect failed: %s", e)
    except TimeoutError as e:
        result["error"] = f"gpsd timeout: {e}"
        logger.warning("gpsd collect timeout: %s", e)
    except Exception as e:
        result["error"] = f"gpsd unexpected error: {e}"
        logger.error("gpsd collect unexpected error: %s", e, exc_info=True)

    return result


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(levelname)s  %(message)s"
    )

    print("=" * 60)
    print("collectors/gpsd_client.py — standalone test")
    print("=" * 60)

    host = _get_host()
    port = _get_port()
    print(f"gpsd host : {host}:{port}")
    print()

    # Test connectivity
    print("── Testing gpsd connection ──")
    try:
        sock = socket.create_connection((host, port), timeout=3)
        sock.close()
        print(f"  Connection to {host}:{port} OK")
    except Exception as e:
        print(f"  Cannot connect to gpsd: {e}")
        print("  Is gpsd running? Check: sudo systemctl status gpsd")
        sys.exit(1)
    print()

    # Full collect
    print("── collect() — TPV + SKY in one connection ──")
    data = collect(host, port)

    if data["error"]:
        print(f"  ERROR: {data['error']}")
        sys.exit(1)

    # ── TPV results
    tpv = data["tpv"]
    print("TPV (position/fix):")
    print(f"  fix_mode      : {tpv['fix_mode']} ({tpv['fix_mode_name']})")
    print(f"  has_fix       : {tpv['has_fix']}")
    if tpv["latitude"] is not None:
        print(f"  latitude      : {tpv['latitude']:.6f}")
        print(f"  longitude     : {tpv['longitude']:.6f}")
        print(f"  altitude_m    : {tpv['altitude_m']}")
    else:
        print("  position      : (no fix)")
    print(f"  time_utc      : {tpv['time_utc']}")
    print(f"  eph (horiz err): {tpv['eph']}")
    print()

    # ── SKY results
    sky = data["sky"]
    print("SKY (satellites):")
    print(f"  sats_visible  : {sky['sats_visible']}")
    print(f"  sats_used     : {sky['sats_used']}")
    print(f"  hdop          : {sky['hdop']}")
    print(f"  vdop          : {sky['vdop']}")
    print(f"  pdop          : {sky['pdop']}")
    print()

    print("  Constellation summary:")
    for name, counts in sorted(sky["constellation_summary"].items()):
        print(f"    {name:10s}  visible={counts['visible']:2d}  "
              f"used={counts['used']:2d}")
    print()

    print(f"  Satellites ({sky['sats_visible']} total):")
    print(f"  {'PRN':>5}  {'Const':8}  {'Az':>5}  {'El':>5}  "
          f"{'SNR':>6}  {'Used'}")
    print(f"  {'-'*5}  {'-'*8}  {'-'*5}  {'-'*5}  {'-'*6}  {'-'*4}")
    for sat in sorted(sky["satellites"],
                      key=lambda s: (s["constellation"], s["prn"] or 0)):
        az  = f"{sat['azimuth']:5.1f}" if sat["azimuth"] is not None else "  N/A"
        el  = f"{sat['elevation']:5.1f}" if sat["elevation"] is not None else "  N/A"
        snr = f"{sat['snr']:6.1f}" if sat["snr"] is not None else "   N/A"
        used = "YES" if sat["used"] else "no"
        print(f"  {sat['prn'] or 0:5d}  {sat['constellation']:8}  "
              f"{az}  {el}  {snr}  {used}")
    print()

    # ── Assertions
    print("── Assertions ──")
    assert data["error"] is None,         f"collect() returned error: {data['error']}"
    assert tpv is not None,               "TPV is None"
    assert sky is not None,               "SKY is None"
    assert sky["sats_visible"] > 0,       f"No satellites visible: {sky['sats_visible']}"
    assert sky["sats_used"] > 0,          f"No satellites used: {sky['sats_used']}"
    assert tpv["fix_mode"] in (2, 3),     f"No fix: mode={tpv['fix_mode']}"

    # Verify expected constellations present
    # GPS and BeiDou always present; GLONASS present when read_best_sky captures full burst
    consts = set(sky["constellation_summary"].keys())
    assert "GPS" in consts, f"GPS not in constellations: {consts}"
    print(f"  Constellations seen: {sorted(consts)}")
    if "GLONASS" in consts:
        gl = sky["constellation_summary"]["GLONASS"]
        print(f"  GLONASS: {gl['visible']} visible, {gl['used']} used — read_best_sky working correctly")
    else:
        print("  GLONASS not in this snapshot — may need more poll cycles to appear")

    # Verify satellite data quality
    sats_with_snr = [s for s in sky["satellites"] if s["snr"] is not None]
    assert len(sats_with_snr) > 0, "No satellites with SNR data"

    sats_with_position = [s for s in sky["satellites"]
                          if s["azimuth"] is not None and s["elevation"] is not None]
    assert len(sats_with_position) > 0, "No satellites with azimuth/elevation"

    if tpv["has_fix"]:
        assert tpv["latitude"] is not None,  "Has fix but no latitude"
        assert tpv["longitude"] is not None, "Has fix but no longitude"

    print("  All assertions passed.")
    print()
    print("gpsd_client.py test complete.")
