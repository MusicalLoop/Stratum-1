"""
collectors/chrony.py — Parse chronyc output into structured dicts.

Provides:
    collect()           — run all three chronyc commands, return combined dict
    parse_tracking()    — parse 'chronyc tracking'
    parse_sourcestats() — parse 'chronyc sourcestats'
    parse_sources()     — parse 'chronyc sources -v'

All time/offset values are normalised to nanoseconds (float) for consistent
storage. Frequency values are in ppm. All other values keep their natural
units with the unit documented in the returned dict.

Truncated names (ending '>') are preserved as-is — they are the actual
chronyc output and match between sourcestats and sources tables.

Usage (standalone test):
    python3 collectors/chrony.py
"""

import os
import re
import subprocess
import logging
import sys

logger = logging.getLogger(__name__)

# Allow running directly from app/ or from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import config
    _get_chronyc = lambda: config.get("chronyc_path", "/usr/bin/chronyc")
except ImportError:
    _get_chronyc = lambda: "/usr/bin/chronyc"


# ── Unit conversion ───────────────────────────────────────────────────────────

# Multipliers to convert a value to nanoseconds
_TO_NS = {
    "ns":  1.0,
    "us":  1_000.0,
    "ms":  1_000_000.0,
    "s":   1_000_000_000.0,
}

# Multipliers to convert a value to microseconds
_TO_US = {
    "ns":  0.001,
    "us":  1.0,
    "ms":  1_000.0,
    "s":   1_000_000.0,
}


def _parse_value_with_unit(text):
    """
    Parse a chronyc value+unit string into (float_value, unit_str).

    Handles formats produced by chronyc:
        -0.000000653 seconds   →  (-653.0, "ns")     [tracking fields]
        -46ns                  →  (-46.0, "ns")       [sources last sample]
        -756us                 →  (-756.0, "us")      [sources last sample]
        +12ms                  →  (12.0, "ms")        [sources last sample]
        +0ns                   →  (0.0, "ns")         [fritz.box style]
        100ms                  →  (100.0, "ms")       [sources error]
        138ns                  →  (138.0, "ns")       [sources error]
        5.107 ppm slow         →  (5.107, "ppm")      [tracking frequency]
        -0.007 ppm             →  (-0.007, "ppm")     [sourcestats]
        0.130 ppm              →  (0.130, "ppm")      [sourcestats/skew]
        0.000000001 seconds    →  (1.0, "ns")         [root delay]

    Returns (None, None) if the string cannot be parsed.
    """
    if text is None:
        return None, None

    text = text.strip()

    # "X seconds [fast|slow] of NTP time" — tracking System time field
    m = re.match(r'^([+-]?\d+\.?\d*(?:e[+-]?\d+)?)\s+seconds\s+(?:fast|slow)\s+of', text, re.I)
    if m:
        val = float(m.group(1))
        return val * _TO_NS["s"], "ns"

    # "X seconds" — plain seconds (root delay, root dispersion etc.)
    m = re.match(r'^([+-]?\d+\.?\d*(?:e[+-]?\d+)?)\s+seconds$', text, re.I)
    if m:
        val = float(m.group(1))
        return val * _TO_NS["s"], "ns"

    # "X ppm slow|fast" — frequency with direction word
    m = re.match(r'^([+-]?\d+\.?\d*)\s+ppm\s+(?:slow|fast)$', text, re.I)
    if m:
        return float(m.group(1)), "ppm"

    # "X ppm" — plain ppm
    m = re.match(r'^([+-]?\d+\.?\d*)\s+ppm$', text, re.I)
    if m:
        return float(m.group(1)), "ppm"

    # Compact: "+12ms", "-46ns", "+0ns", "100ms", "4000ms" etc.
    m = re.match(r'^([+-]?\d+\.?\d*)(ns|us|ms|s)$', text, re.I)
    if m:
        val = float(m.group(1))
        unit = m.group(2).lower()
        return val, unit

    return None, None


def _to_ns(value, unit):
    """Convert a value in the given unit to nanoseconds. Returns None on failure."""
    if value is None or unit is None:
        return None
    mult = _TO_NS.get(unit.lower())
    if mult is None:
        return None
    return value * mult


def _to_us(value, unit):
    """Convert a value in the given unit to microseconds. Returns None on failure."""
    if value is None or unit is None:
        return None
    mult = _TO_US.get(unit.lower())
    if mult is None:
        return None
    return value * mult


# ── chronyc runner ────────────────────────────────────────────────────────────

def _run_chronyc(*args, timeout=5):
    """
    Run chronyc with the given arguments.
    Returns stdout as a string, or raises RuntimeError on failure.
    """
    chronyc = _get_chronyc()
    cmd = [chronyc] + list(args)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"chronyc {' '.join(args)} exited {result.returncode}: "
                f"{result.stderr.strip()}"
            )
        return result.stdout
    except FileNotFoundError:
        raise RuntimeError(f"chronyc not found at {chronyc}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"chronyc {' '.join(args)} timed out after {timeout}s")


# ── parse_tracking ────────────────────────────────────────────────────────────

def parse_tracking(raw=None):
    """
    Parse 'chronyc tracking' output.

    If raw is None, runs chronyc tracking and parses the result.
    If raw is provided (string), parses that instead (for testing).

    Returns a dict with keys:
        reference_id        str   — hex reference ID, e.g. "50505300"
        reference_name      str   — decoded name, e.g. "PPS"
        stratum             int
        ref_time_utc        str   — reference time as UTC string
        system_offset_ns    float — system time offset in nanoseconds
        last_offset_ns      float — last measured offset in nanoseconds
        rms_offset_ns       float — RMS offset in nanoseconds
        freq_ppm            float — frequency error (positive = slow)
        freq_direction      str   — "slow" or "fast"
        residual_freq_ppm   float
        skew_ppm            float
        root_delay_ns       float — root delay in nanoseconds
        root_dispersion_ns  float — root dispersion in nanoseconds
        root_dispersion_us  float — root dispersion in microseconds (for threshold checks)
        update_interval_s   float
        leap_status         str   — "Normal", "Insert second", "Delete second", "Not synchronised"
        raw_text            str   — original chronyc output
    """
    if raw is None:
        raw = _run_chronyc("tracking")

    result = {
        "reference_id":       None,
        "reference_name":     None,
        "stratum":            None,
        "ref_time_utc":       None,
        "system_offset_ns":   None,
        "last_offset_ns":     None,
        "rms_offset_ns":      None,
        "freq_ppm":           None,
        "freq_direction":     None,
        "residual_freq_ppm":  None,
        "skew_ppm":           None,
        "root_delay_ns":      None,
        "root_dispersion_ns": None,
        "root_dispersion_us": None,
        "update_interval_s":  None,
        "leap_status":        None,
        "raw_text":           raw,
    }

    for line in raw.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue

        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()

        if key == "Reference ID":
            # "50505300 (PPS)"  or  "7F7F0101 ()"
            m = re.match(r'^([0-9A-Fa-f]+)\s*\(([^)]*)\)', value)
            if m:
                result["reference_id"] = m.group(1).upper()
                name = m.group(2).strip()
                if not name:
                    # Decode hex to ASCII if no name given
                    try:
                        name = bytes.fromhex(m.group(1)).decode("ascii").rstrip("\x00")
                    except Exception:
                        name = m.group(1)
                result["reference_name"] = name

        elif key == "Stratum":
            try:
                result["stratum"] = int(value)
            except ValueError:
                pass

        elif key == "Ref time (UTC)":
            result["ref_time_utc"] = value

        elif key == "System time":
            # "0.000000000 seconds fast of NTP time"
            # Direction matters: fast = system is ahead = positive offset
            val, unit = _parse_value_with_unit(value)
            if val is not None:
                direction = "fast" if "fast" in value.lower() else "slow"
                ns = _to_ns(val, unit)
                result["system_offset_ns"] = ns if direction == "fast" else -ns

        elif key == "Last offset":
            val, unit = _parse_value_with_unit(value)
            result["last_offset_ns"] = _to_ns(val, unit)

        elif key == "RMS offset":
            val, unit = _parse_value_with_unit(value)
            result["rms_offset_ns"] = _to_ns(val, unit)

        elif key == "Frequency":
            # "5.107 ppm slow"
            val, unit = _parse_value_with_unit(value)
            if val is not None:
                result["freq_ppm"] = val
                result["freq_direction"] = "slow" if "slow" in value.lower() else "fast"

        elif key == "Residual freq":
            val, unit = _parse_value_with_unit(value)
            if unit == "ppm":
                result["residual_freq_ppm"] = val

        elif key == "Skew":
            val, unit = _parse_value_with_unit(value)
            if unit == "ppm":
                result["skew_ppm"] = val

        elif key == "Root delay":
            val, unit = _parse_value_with_unit(value)
            result["root_delay_ns"] = _to_ns(val, unit)

        elif key == "Root dispersion":
            val, unit = _parse_value_with_unit(value)
            ns = _to_ns(val, unit)
            result["root_dispersion_ns"] = ns
            result["root_dispersion_us"] = ns / 1000.0 if ns is not None else None

        elif key == "Update interval":
            m = re.match(r'^([\d.]+)\s+seconds', value)
            if m:
                result["update_interval_s"] = float(m.group(1))

        elif key == "Leap status":
            result["leap_status"] = value

    return result


# ── parse_sourcestats ─────────────────────────────────────────────────────────

def parse_sourcestats(raw=None):
    """
    Parse 'chronyc sourcestats' output.

    Returns a list of dicts, one per source. Keys:
        name            str
        np              int or None   — number of sample points
        nr              int or None   — number of residual runs
        span            str           — measurement span (raw string, e.g. "18h")
        span_seconds    int or None   — span converted to seconds
        freq_ppm        float or None
        freq_skew_ppm   float or None
        offset_ns       float or None — offset in nanoseconds
        std_dev_ns      float or None — std dev in nanoseconds
        zero_samples    bool          — True if NP=0 (e.g. fritz.box)
    """
    if raw is None:
        raw = _run_chronyc("sourcestats")

    sources = []
    in_table = False

    for line in raw.splitlines():
        line = line.rstrip()

        # Detect start of data (after the === separator)
        if line.startswith("=="):
            in_table = True
            continue

        if not in_table:
            continue

        # Skip blank lines and header
        if not line.strip():
            continue

        # Parse data row
        # Format: Name  NP  NR  Span  Frequency  FreqSkew  Offset  StdDev
        # Example:
        #   PPS                         6   3    40     -0.007      0.128    -46ns   499ns
        #   fritz.box                   0   0     0     +0.000   2000.000     +0ns  4000ms
        #   212-71-233-40.ip.linodeu>  64  33   18h     -0.096      0.013  +1838us   572us
        parts = line.split()
        if len(parts) < 8:
            continue

        name = parts[0]
        try:
            np_val = int(parts[1])
            nr_val = int(parts[2])
        except ValueError:
            continue

        span_raw = parts[3]
        span_seconds = _parse_span(span_raw)

        zero_samples = (np_val == 0)

        if zero_samples:
            # Don't parse meaningless statistics for zero-sample peers
            sources.append({
                "name":          name,
                "np":            0,
                "nr":            0,
                "span":          span_raw,
                "span_seconds":  0,
                "freq_ppm":      None,
                "freq_skew_ppm": None,
                "offset_ns":     None,
                "std_dev_ns":    None,
                "zero_samples":  True,
            })
            continue

        try:
            freq = float(parts[4])
            freq_skew = float(parts[5])
        except ValueError:
            freq = None
            freq_skew = None

        offset_val, offset_unit = _parse_value_with_unit(parts[6])
        stddev_val, stddev_unit = _parse_value_with_unit(parts[7])

        sources.append({
            "name":          name,
            "np":            np_val,
            "nr":            nr_val,
            "span":          span_raw,
            "span_seconds":  span_seconds,
            "freq_ppm":      freq,
            "freq_skew_ppm": freq_skew,
            "offset_ns":     _to_ns(offset_val, offset_unit),
            "std_dev_ns":    _to_ns(stddev_val, stddev_unit),
            "zero_samples":  False,
        })

    return sources


def _parse_span(span_str):
    """
    Convert a chronyc span string to seconds.
    Examples: "40" -> 40, "18h" -> 64800, "550m" -> 33000, "0" -> 0
    Returns None if unparseable.
    """
    if not span_str:
        return None
    span_str = span_str.strip()
    try:
        # Plain integer = seconds
        return int(span_str)
    except ValueError:
        pass
    m = re.match(r'^(\d+)([mhd])$', span_str, re.I)
    if m:
        val = int(m.group(1))
        unit = m.group(2).lower()
        return val * {"m": 60, "h": 3600, "d": 86400}[unit]
    return None


# ── parse_sources ─────────────────────────────────────────────────────────────

def parse_sources(raw=None):
    """
    Parse 'chronyc sources -v' output.

    Returns a list of dicts, one per source. Keys:
        name                str
        mode                str   — "server", "peer", or "local"
        state               str   — "best", "combined", "not_combined",
                                    "unusable", "error", "too_variable"
        state_char          str   — raw state character (* + - ? x ~)
        stratum             int or None
        poll                int or None   — log2 of poll interval
        reach               str           — octal reachability register
        last_rx             str or None   — "117" seconds, or "-" if never
        last_rx_seconds     int or None
        adj_offset_ns       float or None — adjusted offset in nanoseconds
        meas_offset_ns      float or None — measured offset in nanoseconds
        error_ns            float or None — estimated error in nanoseconds
        is_pps              bool          — True if name == "PPS"
        is_gps              bool          — True if name == "GPS"
        zero_samples        bool          — True if LastRx is "-"
    """
    if raw is None:
        raw = _run_chronyc("sources", "-v")

    sources = []
    in_table = False

    _MODE_MAP = {"^": "server", "=": "peer", "#": "local"}
    _STATE_MAP = {
        "*": "best",
        "+": "combined",
        "-": "not_combined",
        "?": "unusable",
        "x": "error",
        "~": "too_variable",
    }

    for line in raw.splitlines():
        line = line.rstrip()

        # Detect start of data (after the === separator)
        if line.startswith("====="):
            in_table = True
            continue

        if not in_table:
            continue

        if not line.strip():
            continue

        # Mode and state chars are the first two non-space characters
        # Format: MS Name  Stratum Poll Reach LastRx Last sample
        # Example:
        #   #* PPS             0   3   377    11   -446ns[-1047ns] +/-  138ns
        #   ^- 183.ip-51...    2  10   377   117   -838us[ -846us] +/- 8915us
        #   ^? fritz.box       0  10   377     -     +0ns[   +0ns] +/-    0ns

        if len(line) < 4:
            continue

        mode_char  = line[0]
        state_char = line[1]

        if mode_char not in _MODE_MAP:
            continue

        # Rest of the line after the two mode/state chars
        rest = line[2:].strip()

        # Split into fields — last sample field is complex so we split carefully
        # Fields: Name Stratum Poll Reach LastRx <last_sample_complex>
        # Last sample: "-446ns[-1047ns] +/- 138ns"  or  "+0ns[  +0ns] +/-  0ns"
        # We split on whitespace, then reassemble the last sample fields

        parts = rest.split()
        if len(parts) < 6:
            continue

        name       = parts[0]
        stratum    = _safe_int(parts[1])
        poll       = _safe_int(parts[2])
        reach      = parts[3]
        last_rx    = parts[4]   # seconds integer or "-"

        last_rx_seconds = None
        zero_samples = (last_rx == "-")
        if not zero_samples:
            last_rx_seconds = _safe_int(last_rx)

        # Remainder after name + 4 fixed fields = last sample section
        # "-446ns[-1047ns] +/- 138ns"
        sample_section = " ".join(parts[5:])
        adj_ns, meas_ns, err_ns = _parse_last_sample(sample_section)

        sources.append({
            "name":             name,
            "mode":             _MODE_MAP.get(mode_char, "unknown"),
            "state":            _STATE_MAP.get(state_char, "unknown"),
            "state_char":       state_char,
            "stratum":          stratum,
            "poll":             poll,
            "reach":            reach,
            "last_rx":          last_rx,
            "last_rx_seconds":  last_rx_seconds,
            "adj_offset_ns":    adj_ns,
            "meas_offset_ns":   meas_ns,
            "error_ns":         err_ns,
            "is_pps":           name.upper() == "PPS",
            "is_gps":           name.upper() == "GPS",
            "zero_samples":     zero_samples,
        })

    return sources


def _parse_last_sample(text):
    """
    Parse the 'Last sample' field from chronyc sources -v.

    Format: "adjoffset[measoffset] +/- error"
    Examples:
        "-446ns[-1047ns] +/-  138ns"
        "+12ms[  -10ms] +/-  100ms"
        "+0ns[   +0ns] +/-    0ns"
        "+2709us[+2703us] +/-   25ms"

    Returns (adj_offset_ns, meas_offset_ns, error_ns).
    All values as floats in nanoseconds, or None if unparseable.
    """
    # Match: value unit [ value unit ] +/- value unit
    # The spaces inside brackets are variable
    m = re.match(
        r'([+-]?\d+\.?\d*)(ns|us|ms|s)'
        r'\s*\[\s*([+-]?\d+\.?\d*)(ns|us|ms|s)\s*\]'
        r'\s*\+/-\s*([+-]?\d+\.?\d*)(ns|us|ms|s)',
        text.strip(), re.I
    )
    if not m:
        return None, None, None

    adj_ns  = _to_ns(float(m.group(1)), m.group(2))
    meas_ns = _to_ns(float(m.group(3)), m.group(4))
    err_ns  = _to_ns(float(m.group(5)), m.group(6))
    return adj_ns, meas_ns, err_ns


def _safe_int(s):
    """Return int(s) or None if conversion fails."""
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


# ── collect ───────────────────────────────────────────────────────────────────

def collect():
    """
    Run all three chronyc commands and return a combined dict.

    Returns:
    {
        "tracking":    { ... }      — from parse_tracking()
        "sourcestats": [ ... ]      — from parse_sourcestats()
        "sources":     [ ... ]      — from parse_sources()
        "error":       None | str   — set if any command failed
    }

    On partial failure (e.g. one command fails), the failed section is
    set to None and the error string is populated. The poller continues
    with whatever data is available.
    """
    result = {
        "tracking":    None,
        "sourcestats": None,
        "sources":     None,
        "error":       None,
    }
    errors = []

    try:
        result["tracking"] = parse_tracking()
    except Exception as e:
        errors.append(f"tracking: {e}")
        logger.warning("chronyc tracking failed: %s", e)

    try:
        result["sourcestats"] = parse_sourcestats()
    except Exception as e:
        errors.append(f"sourcestats: {e}")
        logger.warning("chronyc sourcestats failed: %s", e)

    try:
        result["sources"] = parse_sources()
    except Exception as e:
        errors.append(f"sources: {e}")
        logger.warning("chronyc sources failed: %s", e)

    if errors:
        result["error"] = "; ".join(errors)

    return result


# ── Standalone test ───────────────────────────────────────────────────────────

# Sample output captured from PowerPi — used when chronyc is not available
_SAMPLE_TRACKING = """\
Reference ID    : 50505300 (PPS)
Stratum         : 1
Ref time (UTC)  : Tue May 12 07:47:54 2026
System time     : 0.000000000 seconds fast of NTP time
Last offset     : -0.000000653 seconds
RMS offset      : 0.000001051 seconds
Frequency       : 5.107 ppm slow
Residual freq   : -0.007 ppm
Skew            : 0.130 ppm
Root delay      : 0.000000001 seconds
Root dispersion : 0.000013825 seconds
Update interval : 8.0 seconds
Leap status     : Normal
"""

_SAMPLE_SOURCESTATS = """\
Name/IP Address            NP  NR  Span  Frequency  Freq Skew  Offset  Std Dev
==============================================================================
GPS                         6   4    39   +417.523   4563.946    +12ms    17ms
PPS                         6   3    40     -0.007      0.128    -46ns   499ns
183.ip-51-89-151.eu        15  10  240m     -0.119      0.099   -756us   426us
pool.ntp3.cam.ac.uk        33  14  550m     -0.125      0.031   -368us   484us
212-71-233-40.ip.linodeu>  64  33   18h     -0.096      0.013  +1838us   572us
31.121.161.188             13   7  207m     +0.023      0.097   +263us   336us
fritz.box                   0   0     0     +0.000   2000.000     +0ns  4000ms
"""

_SAMPLE_SOURCES = """\
  .-- Source mode  '^' = server, '=' = peer, '#' = local clock.
 / .- Source state '*' = current best, '+' = combined, '-' = not combined,
| /             'x' = may be in error, '~' = too variable, '?' = unusable.
||                                                 .- xxxx [ yyyy ] +/- zzzz
||      Reachability register (octal) -.           |  xxxx = adjusted offset,
||      Log2(Polling interval) --.      |          |  yyyy = measured offset,
||                                \\     |          |  zzzz = estimated error.
||                                 |    |           \\
MS Name/IP address         Stratum Poll Reach LastRx Last sample               
===============================================================================
#? GPS                           0   3   377    11    -10ms[  -10ms] +/-  100ms
#* PPS                           0   3   377    11   -446ns[-1047ns] +/-  138ns
^- 183.ip-51-89-151.eu           2  10   377   117   -838us[ -846us] +/- 8915us
^- pool.ntp3.cam.ac.uk           2  10   377   212   -103us[ -122us] +/- 9208us
^- 212-71-233-40.ip.linodeu>     2  10   377   155  +2709us[+2703us] +/-   25ms
^- 31.121.161.188                4  10   377    95  -3958ns[  -11us] +/-   14ms
^? fritz.box                     0  10   377     -     +0ns[   +0ns] +/-    0ns
"""


if __name__ == "__main__":
    import pprint

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s  %(message)s")

    # Detect whether chronyc is actually available
    chronyc_path = "/usr/bin/chronyc"
    chronyc_available = os.path.exists(chronyc_path)

    print("=" * 60)
    print("collectors/chrony.py — standalone test")
    print("=" * 60)
    print(f"chronyc available : {chronyc_available}")
    if not chronyc_available:
        print("  (using captured sample data from PowerPi)")
    print()

    # ── Test parse_tracking
    print("── parse_tracking ──")
    t = parse_tracking(raw=_SAMPLE_TRACKING if not chronyc_available else None)
    print(f"  reference_id       : {t['reference_id']}")
    print(f"  reference_name     : {t['reference_name']}")
    print(f"  stratum            : {t['stratum']}")
    print(f"  system_offset_ns   : {t['system_offset_ns']:.4f} ns")
    print(f"  last_offset_ns     : {t['last_offset_ns']:.2f} ns")
    print(f"  rms_offset_ns      : {t['rms_offset_ns']:.2f} ns")
    print(f"  freq_ppm           : {t['freq_ppm']} ppm {t['freq_direction']}")
    print(f"  residual_freq_ppm  : {t['residual_freq_ppm']} ppm")
    print(f"  skew_ppm           : {t['skew_ppm']} ppm")
    print(f"  root_delay_ns      : {t['root_delay_ns']:.4f} ns")
    print(f"  root_dispersion_ns : {t['root_dispersion_ns']:.2f} ns")
    print(f"  root_dispersion_us : {t['root_dispersion_us']:.4f} us")
    print(f"  update_interval_s  : {t['update_interval_s']} s")
    print(f"  leap_status        : {t['leap_status']}")
    print()

    # Verify expected values from PowerPi sample
    assert t["reference_name"] == "PPS",         f"reference_name: {t['reference_name']}"
    assert t["stratum"] == 1,                    f"stratum: {t['stratum']}"
    assert t["freq_direction"] == "slow",        f"freq_direction: {t['freq_direction']}"
    assert t["leap_status"] == "Normal",         f"leap_status: {t['leap_status']}"
    assert t["root_dispersion_us"] is not None,  "root_dispersion_us is None"
    assert 0 < t["root_dispersion_us"] < 100, \
        "root_dispersion_us out of range: " + str(t["root_dispersion_us"])
    assert t["last_offset_ns"] is not None, "last_offset_ns is None"
    print("  parse_tracking assertions passed.")
    print()

    # ── Test parse_sourcestats
    print("── parse_sourcestats ──")
    ss = parse_sourcestats(
        raw=_SAMPLE_SOURCESTATS if not chronyc_available else None
    )
    print(f"  {len(ss)} sources parsed:")
    for s in ss:
        if s["zero_samples"]:
            print(f"  {s['name']:30s}  ZERO SAMPLES")
        else:
            print(f"  {s['name']:30s}  "
                  f"offset={s['offset_ns']:>10.1f}ns  "
                  f"stddev={s['std_dev_ns']:>8.1f}ns  "
                  f"span={s['span']}")
    print()

    # Verify fritz.box zero-sample handling
    fritz = next((s for s in ss if s["name"] == "fritz.box"), None)
    assert fritz is not None,          "fritz.box not found in sourcestats"
    assert fritz["zero_samples"],      "fritz.box should be zero_samples=True"
    assert fritz["offset_ns"] is None, "fritz.box offset_ns should be None"

    pps_ss = next((s for s in ss if s["name"] == "PPS"), None)
    assert pps_ss is not None,              "PPS not found in sourcestats"
    assert not pps_ss["zero_samples"],      "PPS should not be zero_samples"
    assert pps_ss["offset_ns"] is not None, "PPS offset_ns should not be None"
    assert pps_ss["std_dev_ns"] is not None,"PPS std_dev_ns should not be None"
    if not chronyc_available:
        # Sample-data only: verify exact parsed values from captured output
        assert abs(pps_ss["offset_ns"] - (-46.0)) < 0.1, \
            f"PPS offset_ns: {pps_ss['offset_ns']}"
        assert abs(pps_ss["std_dev_ns"] - 499.0) < 0.1, \
            f"PPS std_dev_ns: {pps_ss['std_dev_ns']}"
    print("  parse_sourcestats assertions passed.")
    print()

    # ── Test parse_sources
    print("── parse_sources ──")
    srcs = parse_sources(
        raw=_SAMPLE_SOURCES if not chronyc_available else None
    )
    print(f"  {len(srcs)} sources parsed:")
    for s in srcs:
        marker = "BEST  " if s["state"] == "best" else \
                 "ZERO  " if s["zero_samples"] else \
                 "      "
        adj = f"{s['adj_offset_ns']:>10.1f}ns" \
              if s["adj_offset_ns"] is not None else "         N/A"
        print(f"  {marker}{s['mode'][0].upper()}{s['state_char']} "
              f"{s['name']:30s}  "
              f"adj={adj}")
    print()

    # Verify PPS is identified as best
    pps_src = next((s for s in srcs if s["is_pps"]), None)
    assert pps_src is not None,             "PPS not found in sources"
    assert pps_src["state"] == "best",      f"PPS state: {pps_src['state']}"
    assert pps_src["adj_offset_ns"] is not None, "PPS adj_offset_ns is None"
    # PPS offset varies — verify it is within a sane range (±10us)
    assert abs(pps_src["adj_offset_ns"]) < 10_000, \
        f"PPS adj_offset_ns out of range: {pps_src['adj_offset_ns']}"
    if not chronyc_available:
        assert abs(pps_src["adj_offset_ns"] - (-446.0)) < 0.1, \
            f"PPS adj_offset_ns sample check: {pps_src['adj_offset_ns']}"

    # Verify GPS has large negative offset (NMEA delay — always several ms)
    gps_src = next((s for s in srcs if s["is_gps"]), None)
    assert gps_src is not None, "GPS not found in sources"
    assert gps_src["adj_offset_ns"] is not None, "GPS adj_offset_ns is None"
    # GPS NMEA offset is always negative and in the range -5ms to -500ms
    assert -500_000_000 < gps_src["adj_offset_ns"] < -1_000, \
        f"GPS adj_offset_ns unexpected: {gps_src['adj_offset_ns']}"
    if not chronyc_available:
        assert abs(gps_src["adj_offset_ns"] - (-10_000_000.0)) < 100, \
            f"GPS adj_offset_ns sample check: {gps_src['adj_offset_ns']}"

    # Verify fritz.box zero sample handling
    fritz_src = next((s for s in srcs if s["name"] == "fritz.box"), None)
    assert fritz_src is not None,           "fritz.box not found in sources"
    assert fritz_src["zero_samples"],       "fritz.box should be zero_samples"
    assert fritz_src["last_rx"] == "-",     f"fritz.box last_rx: {fritz_src['last_rx']}"
    print("  parse_sources assertions passed.")
    print()

    # ── Test collect() — live or sample
    print("── collect() ──")
    if chronyc_available:
        print("  Running live chronyc collect()...")
        data = collect()
        if data["error"]:
            print(f"  ERROR: {data['error']}")
        else:
            print(f"  tracking reference  : {data['tracking']['reference_name']}")
            print(f"  tracking stratum    : {data['tracking']['stratum']}")
            print(f"  sourcestats count   : {len(data['sourcestats'])}")
            print(f"  sources count       : {len(data['sources'])}")
            pps = next((s for s in data["sources"] if s["is_pps"]), None)
            if pps:
                print(f"  PPS adj offset      : {pps['adj_offset_ns']:.1f} ns")
                print(f"  PPS state           : {pps['state']}")
    else:
        print("  chronyc not available — skipping live collect() test.")
    print()

    print("All tests passed.")
