# Stratum-1 Dashboard — Requirements & Design Specification

**Version:** v0.5 — Final  
**Hardware:** Raspberry Pi 5 — PowerPi  
**GNSS module:** Waveshare Pico-GPS-L76X HAT (LC76G chip)  
**NTP daemon:** chrony  
**GNSS interface:** gpsd + NMEA  
**PPS:** GPIO pin 12 → /dev/pps0  
**Install path:** /opt/stratum1-dashboard  

---

## 1. Purpose and Scope

This document defines requirements, page structure, data sources, configurable parameters, and design assumptions for a self-hosted web dashboard reporting on a Raspberry Pi 5 Stratum-1 NTP time server. The dashboard is intended for home lab use, viewed on a mix of desktop and mobile browsers.

The system collects metrics from chrony (NTP discipline), gpsd / NMEA (GNSS receiver), and Pi system health. Data is polled on a configurable interval, stored in SQLite for historical trending, and served as JSON to static HTML pages rendered in the browser.

---

## 2. System Architecture

### 2.1 Components

| Component | Description |
|-----------|-------------|
| Backend poller | Python process running on the Pi. Polls chronyc, gpsd/NMEA, and system stats at a configurable interval. Writes raw samples to SQLite. |
| SQLite database | Single file on disk. Stores raw samples, pre-aggregated rolling averages, and configuration. Retention is configurable. |
| Web server | Flask on the Pi. Serves static HTML/CSS/JS and JSON API endpoints. |
| Frontend pages | Seven HTML pages using Tabler (Bootstrap 5-based). Charts via ApexCharts. Pages poll JSON endpoints via fetch() on a configurable auto-refresh interval. |
| Config store | config.json in data/. Human-editable. Loaded at poller startup and by the config UI page. |

### 2.2 Data flow

- Poller reads: chronyc tracking, chronyc sourcestats, gpsd socket, /proc and /sys for system stats
- Each poll cycle writes one raw sample row per source to SQLite
- SQLite views / ring buffer maintain rolling averages (1 min, 5 min, 1 hr windows)
- API endpoints query SQLite and return JSON — instantaneous latest, rolling averages, windowed history
- Browser pages call API endpoints every N seconds (configurable, default 10s) and update the DOM

### 2.3 Technology choices

| Concern | Choice |
|---------|--------|
| CSS framework | Tabler v1.0.0-beta17 (Bootstrap 5). Dashboard-first components, built-in dark mode, responsive grid. |
| Charting | ApexCharts v3.49.0. Real-time updates, polar charts, good mobile rendering, MIT licence. |
| Sky plot | Custom SVG rendered in-browser from azimuth/elevation JSON. No additional dependency. |
| Backend language | Python 3.11+ |
| Database | SQLite via Python sqlite3 stdlib. WAL mode enabled. |
| Config format | JSON file (data/config.json). Separate from metrics DB; human-editable. |

---

## 3. Data Collection and Smoothing

### 3.1 Rationale

Raw PPS offset values from chrony can vary between approximately 2 ns and 200 ns on a well-disciplined server. A rolling average provides a stable headline figure while the raw trace on charts preserves diagnostic value.

### 3.2 Storage

- In-memory ring buffer of last 500 raw samples per metric for fast rolling calculations
- Every raw sample persisted to SQLite for long-term trending
- Configurable retention policy (default 90 days). Deletion runs on poller startup and nightly.

### 3.3 Aggregation windows

| Window | Use |
|--------|-----|
| Instantaneous | Latest raw sample — diagnostic displays and raw chart trace |
| 1-minute rolling mean | Stat card secondary value |
| 5-minute rolling mean | Stat card primary display value (default) |
| Hourly / daily aggregates | History page charts |

### 3.4 Chart display

- Time-series charts show both the raw trace (thin, low opacity) and smoothed overlay (bold line)
- Configurable time windows: 1 hr / 6 hr / 24 hr / 7 d / 30 d
- Default chart window on first load is configurable (default 1 hr)

---

## 4. Page Specifications

### Page 1 — Overview

Single-glance health summary. Answers "is everything OK?" in under 3 seconds.

**Stat cards:** System status badge (green/amber/red), PPS offset (5-min mean + range), Jitter (RMS), GNSS fix type + satellite count, Stratum, CPU temp, Uptime  
**Sparklines:** PPS offset, RMS offset, CPU temp — last 60 minutes  
**Status detail panel:** Reference source, leap status, PPS mean, offset range, memory, disk  
**Active alerts panel:** Lists any amber/red threshold conditions  
**Auto-refresh:** Configurable interval (default 10s)

### Page 2 — NTP / Chrony Detail

Full chrony discipline picture.

**Tracking panel:** Reference ID, stratum, ref time UTC, system offset, last offset, RMS offset, frequency (ppm + direction), residual freq, skew, root delay, root dispersion (ns and µs), update interval, leap status  
**Sources table:** All peers/references — name, mode/state flags, stratum, poll, reach, last Rx, adjusted offset, std dev, samples. PPS row highlighted green. GPS row shows "NMEA" label with tooltip explaining expected ~30ms offset. Zero-sample peers (fritz.box) show dashes.  
**Charts:** PPS offset + RMS offset overlay, frequency correction (ppm). Range selector: 1h / 6h / 24h / 7d

### Page 3 — GNSS Status

Receiver health and fix quality.

**Fix status:** Mode badge (red=no fix, amber=2D, green=3D), UTC time  
**Satellite cards:** Used count, visible count, elevation mask  
**DOP cards:** HDOP, VDOP, PDOP — colour coded (green ≤1.5, amber ≤3.0, red >3.0)  
**Constellation panel:** Progress bars per constellation (visible/used ratio)  
**Position panel:** Lat/lon/alt/eph — hidden if show_position=false. "View on map" link to OpenStreetMap.  
**NMEA delay note:** Persistent footer note explaining GPS ~30ms offset in chrony sources

### Page 4 — Satellites

Dedicated sky picture and per-satellite signal data.

**Sky plot (custom SVG):**
- Polar chart: azimuth (0-360°) × elevation (0-90°)
- Filled circle with PRN label = satellite used in fix
- Hollow coloured circle = visible but not used
- Grey hollow circle = below elevation mask
- Circle size proportional to SNR
- Elevation mask ring (dashed red)
- Colour: GPS=green, BeiDou=orange, GLONASS=blue, Galileo=purple

**Satellite table:** PRN, constellation, azimuth, elevation, SNR bar, used indicator  
**Rolling count chart:** Satellites used and visible over last 1 hour (step-line)

### Page 5 — System Health

Pi hardware and OS metrics.

**Stat cards:** CPU temp (°C), CPU usage (%), memory (%), disk (%), load average, uptime  
**Services panel:** chronyd and gpsd — active/inactive badges  
**PPS devices panel:** Lists /dev/pps0-2 with sysfs names, GPIO PPS badge  
**Charts:** CPU temp vs frequency correction (dual-axis — thermal drift diagnostic), CPU + memory usage

### Page 6 — History & Trends

Long-term data from SQLite.

**Range selector:** 24h / 7d / 30d / 90d  
**Charts:** PPS offset, frequency correction (ppm), satellites used, CPU temperature  
**Daily summary table:** Date, mean absolute offset, min, max, sample count. CSV export button.

### Page 7 — Configuration

All configurable parameters with test actions.

**Sections:** Data collection, Alert thresholds, GNSS/Hardware, Chrony/NTP, UI/Display  
**Test actions:** Test chronyc, Test gpsd, Test PPS devices, Check database  
**Save/Reset:** Writes to data/config.json; poller reloads on next cycle (no restart required)

---

## 5. Configuration Schema Reference

| Key | Default | Notes |
|-----|---------|-------|
| poll_interval_seconds | 10 | Minimum 5 |
| retention_days | 90 | |
| auto_purge | true | |
| smoothing_window_minutes | 5 | Options: 1, 5, 10 |
| chart_show_raw | true | |
| chart_default_window_hours | 1 | |
| offset_warning_ns | 100 | |
| offset_critical_ns | 500 | |
| jitter_warning_ns | 50 | |
| jitter_critical_ns | 200 | |
| dispersion_warning_us | 15 | Revised from 10 — healthy PowerPi shows ~11µs |
| min_satellites_warning | 4 | |
| min_satellites_critical | 3 | |
| cpu_temp_warning_c | 70 | |
| cpu_temp_critical_c | 80 | |
| disk_warning_pct | 80 | |
| disk_critical_pct | 90 | |
| offset_display_unit | ns | ns or us |
| gpsd_host | localhost | |
| gpsd_port | 2947 | |
| pps_device | /dev/pps0 | GPIO 12 hardware PPS |
| elevation_mask_degrees | 10 | |
| expected_constellations | GPS,BeiDou | GLONASS monitored, Galileo not active |
| chronyc_path | /usr/bin/chronyc | |
| reference_label | GPS PPS | |
| highlighted_peers | | Comma-separated |
| site_name | PowerPi Stratum-1 | |
| hostname | | Auto from socket.gethostname() if blank |
| default_theme | system | light, dark, or system |
| default_landing_page | overview | |
| show_position | true | |
| ui_refresh_interval_seconds | 10 | Minimum 5 |

---

## 6. Assumptions and Constraints

- **Network access:** Trusted home LAN. No authentication or HTTPS required for v1.
- **Single user:** Config page has no locking.
- **Pi OS:** Raspberry Pi OS Bookworm (Debian-based). Python 3.11+.
- **chrony:** chronyc accessible without sudo (pi user in chrony group).
- **gpsd:** Running on localhost:2947. Started with `-G -n -b /dev/serial0 /dev/pps0`.
- **PPS:** GPIO PPS on pin 12. /dev/pps0 = `pps@12.-1` (confirmed via sysfs). pps_gpio module loaded. Note: dtoverlay entry in config.txt is malformed (bare filenames) but functional — recommend correcting to `dtoverlay=pps-gpio,gpiopin=12`.
- **Constellations:** GPS (gnssid 0) and BeiDou (gnssid 3) confirmed active. GLONASS intermittent. Galileo not active.
- **NMEA sentences:** $GNGGA, $GNGSA, $GNRMC, $GNVTG, $GNZDA, $GPGSV, $BDGSV, $GLGSV, $GPTXT. Accessed via gpsd JSON protocol, not raw parsing.
- **LC76G:** Command interface disabled. Dashboard is read-only with respect to receiver.
- **SQLite:** WAL mode enabled. Concurrent reads/writes handled cleanly.
- **HTTP port:** 8080 (fixed — not configurable via UI).
- **Chrony observed state:** Reference ID PPS (0x50505300), Stratum 1. PPS offset typically ±10–300ns. GPS source ~-30ms (NMEA delay — expected). fritz.box peer unreachable (0 samples — expected).
- **Root dispersion:** Healthy server shows ~11µs. Warning threshold set to 15µs.
- **Notifications:** Out of scope for v1.

---

## 7. Out of Scope (v1)

- Email or webhook alerting
- User authentication or access control
- HTTPS / TLS termination
- Multi-server / multi-instance support
- Sending configuration commands to the GNSS receiver
- Mobile app or PWA packaging

---

## 8. Open Questions — All Resolved

| # | Resolution |
|---|-----------|
| OQ-1 PPS GPIO pin | GPIO 12 confirmed. /dev/pps0 sysfs name = pps@12.-1. pps_gpio module loaded. |
| OQ-2 gpsd vs serial | gpsd is primary interface. Poller uses gpsd-py3 connecting to localhost:2947. |
| OQ-3 NMEA sentences | Confirmed: GGA, GSA, RMC, VTG, ZDA (multi), GPGSV, BDGSV, GLGSV, GPTXT. |
| OQ-4 Constellations | GPS + BeiDou active. GLONASS intermittent. Galileo not active. |
| OQ-5 Web server | Flask for v1. Single process, poller as daemon thread. |
| OQ-6 Sky plot library | Custom SVG in browser. Full control, no extra dependency. |

---

## 9. Project Structure

```
/opt/stratum1-dashboard/          ← INSTALL_DIR
├── app/                           ← Python source
│   ├── paths.py                   ← central path definitions
│   ├── app.py                     ← Flask entry point
│   ├── poller.py                  ← background collection loop
│   ├── config.py                  ← config management
│   ├── database.py                ← SQLite layer
│   ├── collectors/
│   │   ├── chrony.py
│   │   ├── gpsd_client.py
│   │   └── system.py
│   └── api/
│       ├── overview.py
│       ├── chrony.py
│       ├── gnss.py
│       ├── satellites.py
│       ├── system.py
│       ├── history.py
│       └── config_api.py
├── web/                           ← frontend assets
│   ├── templates/
│   │   ├── base.html
│   │   ├── overview.html
│   │   ├── chrony.html
│   │   ├── gnss.html
│   │   ├── satellites.html
│   │   ├── system.html
│   │   ├── history.html
│   │   └── config.html
│   └── static/
│       ├── css/tabler.min.css
│       ├── js/tabler.min.js
│       └── js/apexcharts.min.js
├── data/                          ← runtime data — back this up
│   ├── config.json
│   └── stratum1.db
├── docs/                          ← documentation
├── venv/
├── requirements.txt
└── stratum1-dashboard.service
```

### Build order

| Stage | Module | Test command |
|-------|--------|-------------|
| 0 | app/paths.py | `python3 app/paths.py` |
| 1 | app/config.py | `python3 app/config.py` → creates data/config.json |
| 2 | app/database.py | `python3 app/database.py` → creates data/stratum1.db |
| 3 | app/collectors/chrony.py | `python3 app/collectors/chrony.py` |
| 4 | app/collectors/gpsd_client.py | `python3 app/collectors/gpsd_client.py` |
| 5 | app/collectors/system.py | `python3 app/collectors/system.py` |
| 6 | app/poller.py | `python3 app/poller.py --once` |
| 7 | app/app.py + API | `python3 app/app.py` then `curl http://localhost:8080/api/overview` |
| 8 | base.html + overview.html | Open http://PowerPi:8080/ in browser |
| 9 | Remaining 6 pages | One at a time, verify each |

### Python dependencies (confirmed versions)

| Package | Version | Purpose |
|---------|---------|---------|
| flask | 3.1.3 | Web framework |
| gpsd-py3 | 0.3.0 | gpsd JSON protocol client |
| psutil | 7.2.2 | System stats |
| schedule | 1.2.2 | Poller job scheduler |

---

## 10. Database Metric Reference

### Source: chrony

| Metric | Unit | Description |
|--------|------|-------------|
| pps_offset_ns | ns | PPS adjusted offset — primary timing metric |
| rms_offset_ns | ns | RMS offset from chronyc tracking |
| last_offset_ns | ns | Last measured offset |
| freq_ppm | ppm | Frequency correction (positive = slow) |
| residual_freq_ppm | ppm | Residual frequency |
| skew_ppm | ppm | Frequency skew |
| root_delay_ns | ns | Root delay |
| root_dispersion_ns | ns | Root dispersion in nanoseconds |
| root_dispersion_us | µs | Root dispersion in microseconds (used for thresholds) |
| update_interval_s | s | Chrony update interval |
| stratum | — | NTP stratum (1.0) |
| reference_name | string | Reference source name (e.g. PPS) |
| leap_status | string | Leap second status (e.g. Normal) |

### Source: gnss

| Metric | Unit | Description |
|--------|------|-------------|
| fix_mode | — | gpsd mode: 0=Unknown, 1=NoFix, 2=2D, 3=3D |
| fix_mode_name | string | e.g. "3D" |
| latitude | deg | Only stored if show_position=true |
| longitude | deg | Only stored if show_position=true |
| altitude_m | m | Altitude above WGS84 |
| sats_visible | count | Satellites in view |
| sats_used | count | Satellites used in fix |
| hdop | — | Horizontal DOP |
| vdop | — | Vertical DOP |
| pdop | — | Position DOP |

### Source: system

| Metric | Unit | Description |
|--------|------|-------------|
| cpu_temp_c | °C | From /sys/class/thermal/thermal_zone0/temp |
| cpu_usage_pct | % | Aggregate CPU usage |
| cpu_freq_mhz | MHz | Current CPU frequency |
| mem_usage_pct | % | Memory usage |
| disk_usage_pct | % | Root filesystem usage |
| load_1 | — | 1-minute load average |
| load_5 | — | 5-minute load average |
| load_15 | — | 15-minute load average |
| uptime_seconds | s | System uptime |
| chronyd_status | string | active / inactive / failed / unknown |
| gpsd_status | string | active / inactive / failed / unknown |

---

## 11. Revision History

| Version | Notes |
|---------|-------|
| v0.1 Draft | Initial requirements from design discussion |
| v0.2 Draft | OQs resolved. gpsd confirmed. Constellation set: GPS+BeiDou, GLONASS monitored, Galileo not active. Sky plot = custom SVG. |
| v0.3 Draft | GPIO PPS pin 12 confirmed. Root dispersion threshold revised 10→15µs. Chrony live data captured. |
| v0.4 Draft | sqlite3 CLI added to prerequisites (not installed by default on Pi OS Bookworm) |
| v0.5 Final | Project complete. Directory structure updated to final layout. Build order updated with Stage 0. Confirmed package versions. Section 10 (metric reference) added. |
