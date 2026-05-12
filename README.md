# Stratum-1 Dashboard — Operations Guide

**Version:** v1.0  
**Hardware:** Raspberry Pi 5 — PowerPi  
**Install path:** /opt/stratum1-dashboard  
**Dashboard URL:** http://PowerPi:8080/  
**Health endpoint:** http://PowerPi:8080/health  
**Config file:** /opt/stratum1-dashboard/data/config.json  
**Database:** /opt/stratum1-dashboard/data/stratum1.db  
**Logs:** `journalctl -u stratum1-dashboard`  

---

## 1. Quick Reference

| Task | Command |
|------|---------|
| Check service status | `sudo systemctl status stratum1-dashboard` |
| Start service | `sudo systemctl start stratum1-dashboard` |
| Stop service | `sudo systemctl stop stratum1-dashboard` |
| Restart service | `sudo systemctl restart stratum1-dashboard` |
| Live logs | `journalctl -u stratum1-dashboard -f` |
| Last 50 log lines | `journalctl -u stratum1-dashboard -n 50` |
| PPS offset now | `chronyc tracking \| grep 'System time'` |
| All chrony sources | `chronyc sources -v` |
| All services | `sudo systemctl status chronyd gpsd stratum1-dashboard` |
| Database size | `du -sh /opt/stratum1-dashboard/data/` |
| Database row count | `sqlite3 /opt/stratum1-dashboard/data/stratum1.db 'SELECT COUNT(*) FROM raw_samples;'` |
| Backup data | `tar -czf stratum1-data-$(date +%Y%m%d).tar.gz -C /opt/stratum1-dashboard data/` |
| Open config page | http://PowerPi:8080/config |

---

## 2. Reading the Dashboard

### 2.1 Overview page — status badge

| Badge | Meaning |
|-------|---------|
| ● OK (green) | All metrics within thresholds. Server well-disciplined. |
| ▲ WARN (amber) | One or more metrics above warning threshold. Functional but worth investigating. |
| ✕ ALERT (red) | One or more metrics above critical threshold. Action may be required. |
| — (grey) | No data yet — poller has not completed its first cycle. |

> On first start or after a reboot, the badge shows **ALERT** for 1–3 minutes while chrony re-acquires PPS lock. This is normal — the badge will transition to WARN then OK automatically.

### 2.2 NTP/Chrony page — sources table

| Symbol | Meaning |
|--------|---------|
| `#*` (green row) | PPS — current best reference. Should always have `*` state. |
| `#?` GPS row | NMEA source — always shows `?` state with large negative offset (~10–40ms). Normal — NMEA delay. |
| `^-` | NTP server peer — available but not current best. |
| `^?` fritz.box | Always `?` with dashes — home router NTP is unusable. Normal. |
| `*` | Current best source (should be PPS) |
| `+` | Combined — contributing to clock |
| `-` | Not combined — available but unused |
| `?` | Unusable |

### 2.3 Satellites page — sky plot

- **Filled circle + PRN:** satellite used in current fix
- **Hollow coloured circle:** visible but not used in fix
- **Grey circle at edge:** below elevation mask (10°)
- **Dashed red ring:** elevation mask boundary
- **Circle size:** proportional to SNR (larger = stronger signal)
- Green = GPS · Orange = BeiDou · Blue = GLONASS · Purple = Galileo

> GLONASS appears intermittently. BeiDou is often visible but shows 0 used — both are normal.

### 2.4 System Health — temp/frequency chart

The dual-axis chart shows CPU temperature (orange, left) and chrony frequency correction (blue dashed, right) together. These should correlate — when temperature rises, frequency correction changes in tandem. This is thermal drift: the crystal oscillator's frequency changes slightly with temperature. It's a diagnostic, not an alert.

---

## 3. Normal Operating Values

| Metric | Normal range |
|--------|-------------|
| PPS offset (disciplined) | ±10–300 ns |
| PPS offset (settling after reboot) | Up to ±1,000,000 ns — reduces over 1–3 min |
| Jitter (RMS offset) | 100–1,500 ns |
| Root dispersion | 8–15 µs |
| Frequency correction | 4.5–5.5 ppm slow (varies slowly with temperature) |
| Skew | 0.05–0.20 ppm |
| GPS source offset | –5 ms to –40 ms (NMEA delay — always negative, always large, always normal) |
| CPU temperature (idle) | 44–56 °C |
| CPU usage | 0–5% |
| Memory usage | 8–15% |
| Satellites visible | 6–20 (varies through day) |
| Satellites used | 4–12 |
| HDOP | 0.8–2.0 |

---

## 4. Common Situations

### 4.1 ALERT badge after power cut / reboot

**Expected.** Chrony needs 1–3 minutes to re-acquire PPS lock. Watch the PPS offset on the Overview page decrease. Badge will go WARN then OK automatically. No action required unless it does not settle within 5 minutes.

If not settling after 5 minutes:

```bash
chronyc sources -v         # Is PPS showing * state?
chronyc tracking           # Is system time offset decreasing?
sudo systemctl status gpsd # Is gpsd running?
ls -la /dev/pps*           # Are PPS devices present?
cat /sys/class/pps/pps0/assert  # Timestamp updating every second?
```

### 4.2 PPS offset stuck large

If offset is large and not reducing after 5 minutes:

```bash
# Check PPS device is firing
cat /sys/class/pps/pps0/assert
# Should show a timestamp updating every second

# Check GNSS fix
# Open http://PowerPi:8080/gnss
# Fix mode should show 3D
```

If fix mode shows "No fix", the antenna may be obstructed.

### 4.3 Status badge stays WARN

Possible causes and checks:
- **Poor satellite geometry today** — check Satellites page, count and sky plot
- **Temperature spike** — check System Health page, temp/frequency chart
- **Time of day** — geometry varies; some periods naturally produce wider offset

> A WARN badge with offset 100–300ns when PPS has `*` state and the server has been running for >10 minutes is not a problem. The threshold is intentionally sensitive.

### 4.4 Dashboard not loading after reboot

```bash
sudo systemctl status stratum1-dashboard
journalctl -u stratum1-dashboard -n 30

# Common causes:
# - gpsd not started yet (dependency timing)
# - Port 8080 in use
# - Python path issue

# Fix:
sudo systemctl restart stratum1-dashboard
```

### 4.5 No satellite data / sky plot empty

```bash
sudo systemctl status gpsd
gpspipe -w -n 5
# Should print TPV and SKY JSON messages
# If SKY shows 0 satellites: check antenna connection
```

### 4.6 History page shows sparse / no data

The History page requires accumulated data. The 24h view fills in after 24 hours; 7d after 7 days. If data stops at a point:

```bash
journalctl -u stratum1-dashboard --since '1 hour ago'
# Look for ERROR or WARNING lines

# Check heartbeat via config page:
# http://PowerPi:8080/config → Check database → Heartbeat age
```

---

## 5. Updating the Application

```bash
sudo systemctl stop stratum1-dashboard

# Copy changed files:
# Python files  → /opt/stratum1-dashboard/app/
# HTML templates → /opt/stratum1-dashboard/web/templates/
# Static assets  → /opt/stratum1-dashboard/web/static/

# If requirements.txt changed:
cd /opt/stratum1-dashboard
source venv/bin/activate
pip install -r requirements.txt

sudo systemctl start stratum1-dashboard
sudo systemctl status stratum1-dashboard
```

> **Never delete `data/stratum1.db` or `data/config.json`** when updating.

---

## 6. Configuration

All settings are at `http://PowerPi:8080/config`. Changes take effect within one poll cycle (default 10s) — no restart needed.

### 6.1 Adjusting alert thresholds

If the server consistently achieves ±50ns, tighten the warning threshold to 75ns for more meaningful alerts. Set via Configuration page → Alert Thresholds → Offset warning.

### 6.2 Hiding position

Set **show_position = Hidden** on the Configuration page. Altitude is still shown. The poller stops storing lat/lon in the database.

### 6.3 Poll interval

Default 10s is appropriate. Chrony's own update interval is 8s — polling faster gives diminishing returns. Increase to 30s to reduce database growth.

### 6.4 Config page test actions

| Test | Verifies |
|------|---------|
| Test chronyc | Binary path, permissions, raw output |
| Test gpsd | Connection to localhost:2947, fix mode, constellation summary |
| Test PPS devices | /dev/pps0–2 present, GPIO PPS badge (pps@12.-1) |
| Check database | File size, row counts, oldest/newest samples, heartbeat age |

---

## 7. Database Maintenance

### Check size

```bash
du -sh /opt/stratum1-dashboard/data/
du -sh /opt/stratum1-dashboard/data/stratum1.db

sqlite3 /opt/stratum1-dashboard/data/stratum1.db \
  'SELECT COUNT(*) FROM raw_samples;'

sqlite3 /opt/stratum1-dashboard/data/stratum1.db \
  'SELECT MIN(datetime(timestamp,"unixepoch")),
          MAX(datetime(timestamp,"unixepoch")) FROM raw_samples;'
```

### Expected growth

At 34 metrics × 10s interval: ~12,000 rows/hour, ~290,000 rows/day, ~26M rows over 90 days. Database stays well under 500MB for 90-day retention.

### Manual purge

```bash
cd /opt/stratum1-dashboard
source venv/bin/activate
python3 -c "
import sys; sys.path.insert(0, 'app')
import database, config
config.load()
database.init()
n = database.purge_old_data(config.get('retention_days', 90))
print(f'Deleted {n} rows')
"
```

### Backup

```bash
# Data only (most important)
tar -czf ~/stratum1-data-$(date +%Y%m%d).tar.gz \
  -C /opt/stratum1-dashboard data/

# Full project (excluding venv)
tar -czf ~/stratum1-full-$(date +%Y%m%d).tar.gz \
  -C /opt stratum1-dashboard/ --exclude=venv
```

### Restore

```bash
sudo systemctl stop stratum1-dashboard
tar -xzf stratum1-data-20260512.tar.gz -C /opt/stratum1-dashboard/
sudo systemctl start stratum1-dashboard
```

---

## 8. Log Reference

### Viewing logs

```bash
journalctl -u stratum1-dashboard -f              # Live stream
journalctl -u stratum1-dashboard -n 50           # Last 50 lines
journalctl -u stratum1-dashboard -b              # Since last boot
journalctl -u stratum1-dashboard \
  --since '2026-05-12 10:00' --until '2026-05-12 11:00'
```

### Log message reference

| Message | Meaning |
|---------|---------|
| `INFO  Database initialised. WAL mode enabled.` | Normal startup |
| `INFO  Poller thread started (interval=10s)` | Normal startup |
| `INFO  Dashboard listening on http://0.0.0.0:8080` | Flask started |
| `DEBUG  Cycle N complete — 34 samples written` | Normal poll cycle |
| `WARNING  chronyc tracking failed: ...` | chronyc command failed — check chrony running and chronyc_path |
| `WARNING  gpsd collect failed: Connection refused` | gpsd not running or wrong host:port |
| `WARNING  Cycle completed with errors: ...` | One or more collectors failed — data may be partial |
| `INFO  Config file changed — reloading` | config.json modified — new settings picked up |
| `INFO  Nightly purge: deleted N rows older than 90 days` | Normal maintenance |
| `ERROR  Database write failed: ...` | SQLite error — check disk space and permissions |

---

## 9. Hardware Notes

### PPS devices

| Device | Identity |
|--------|---------|
| /dev/pps0 | GPIO pin 12 hardware PPS. **Chrony reference source.** sysfs name: `pps@12.-1` |
| /dev/pps1 | ptp0 — Pi 5 network hardware clock. Unrelated to GNSS. |
| /dev/pps2 | ttyAMA0 — Serial line discipline PPS. Lower quality. Not used by chrony. |

> The dtoverlay entry in `/boot/firmware/config.txt` for pps-gpio appears as a bare filename rather than `dtoverlay=pps-gpio,gpiopin=12`. It is functional but should be corrected at next maintenance.

### GNSS module (LC76G / Waveshare Pico-GPS-L76X HAT)

- Command interface: **disabled** — module is read-only
- Active constellations: GPS, BeiDou, GLONASS (intermittent)
- gpsd: `/dev/serial0`, port 2947, flags `-G -n -b`

### Chrony

- Reference: PPS (0x50505300 = "PPS")
- Stratum: 1
- Update interval: 8s
- Typical PPS offset: ±10–300 ns when disciplined
- Typical root dispersion: 8–15 µs
- GPS source: coarse NMEA time only (~–30ms offset — expected)

---

## Revision History

| Version | Notes |
|---------|-------|
| v1.0 | Initial release. All seven dashboard pages verified. systemd service confirmed with auto-restart on reboot. PowerPi / Raspberry Pi 5 / LC76G / GPIO 12 PPS. |
