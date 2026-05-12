# Stratum-1 Dashboard — Setup & Deployment Guide

**Version:** v1.4 — Final  
**Hardware:** Raspberry Pi 5 — PowerPi  
**Install path:** /opt/stratum1-dashboard (INSTALL_DIR)  

| Variable | Value |
|----------|-------|
| INSTALL_DIR | /opt/stratum1-dashboard |
| VENV | /opt/stratum1-dashboard/venv |
| SERVICE_USER | pi |

---

## 1. Prerequisites

### 1.1 System packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git \
  gpsd gpsd-clients chrony sqlite3
```

> **Note:** `sqlite3` is a debugging/maintenance tool — not a runtime dependency. `python3-venv` may not be installed on minimal images.

### 1.2 Verify gpsd

```bash
sudo systemctl status gpsd.service gpsd.socket
gpspipe -w -n 5   # Should print TPV and SKY JSON messages
```

Expected: both services `active (running)`. If not:

```bash
sudo systemctl enable gpsd.socket gpsd.service
sudo systemctl start gpsd.socket gpsd.service
```

### 1.3 Verify chrony

```bash
sudo systemctl status chronyd
chronyc tracking   # Should show Reference ID: PPS, Stratum: 1
```

### 1.4 Verify PPS device

```bash
ls -la /dev/pps*
cat /sys/class/pps/pps0/name   # Should show: pps@12.-1
```

### 1.5 Verify chronyc permissions

```bash
chronyc tracking
# If permission denied:
sudo usermod -aG chrony pi
# Log out and back in
```

### 1.6 Verify gpsd access

```bash
gpspipe -w -n 3 2>&1
# If permission denied:
sudo usermod -aG dialout pi
```

---

## 2. Project Setup

> To install elsewhere (e.g. `/home/pi/stratum1-dashboard`), substitute your path wherever INSTALL_DIR appears. The application is path-agnostic — no code changes required.

### 2.1 Create project directory

```bash
sudo mkdir -p /opt/stratum1-dashboard
sudo chown pi:pi /opt/stratum1-dashboard
cd /opt/stratum1-dashboard
```

### 2.2 Create Python virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
# Prompt should show (venv) prefix
python --version   # Confirm Python 3.11+
```

> **Always activate the venv before running project Python commands:**  
> `source /opt/stratum1-dashboard/venv/bin/activate`

### 2.3 Upgrade pip

```bash
pip install --upgrade pip
```

### 2.4 Create requirements.txt

```
flask>=3.0
gpsd-py3>=0.3
psutil>=5.9
schedule>=1.2
```

### 2.5 Install Python dependencies

```bash
pip install -r requirements.txt
pip list | grep -E 'flask|gpsd|psutil|schedule'
# Should show all four packages with version numbers
```

### 2.6 Download frontend assets

```bash
cd /opt/stratum1-dashboard
mkdir -p web/static/css web/static/js

# Tabler CSS
wget -O web/static/css/tabler.min.css \
  https://cdn.jsdelivr.net/npm/@tabler/core@1.0.0-beta17/dist/css/tabler.min.css

# Tabler JS
wget -O web/static/js/tabler.min.js \
  https://cdn.jsdelivr.net/npm/@tabler/core@1.0.0-beta17/dist/js/tabler.min.js

# ApexCharts
wget -O web/static/js/apexcharts.min.js \
  https://cdn.jsdelivr.net/npm/apexcharts@3.49.0/dist/apexcharts.min.js
```

### 2.7 Create directory structure

```bash
cd /opt/stratum1-dashboard

mkdir -p app/collectors app/api app/tests
mkdir -p web/templates
mkdir -p data docs

touch app/__init__.py app/collectors/__init__.py app/api/__init__.py

# Verify
find . -not -path './venv/*' -type d | sort
```

Expected:

```
./app
./app/api
./app/collectors
./app/tests
./data
./docs
./web
./web/static
./web/static/css
./web/static/js
./web/templates
```

---

## 3. Build Stages

Work through each stage in order. Never skip a stage test.

### Stage 0 — paths.py

```bash
python3 app/paths.py
# Should print: ROOT, DATA_DIR, TEMPLATES_DIR all pointing to /opt/stratum1-dashboard/...
# data/ directory created automatically
```

### Stage 1 — config.py

```bash
python3 app/config.py
# Should print full config dict with all defaults
# data/config.json created
cat data/config.json
```

Expected: `hostname: PowerPi`, `site_name: PowerPi Stratum-1`, all keys present.

### Stage 2 — database.py

```bash
python3 app/database.py
# Should print: Database initialised. WAL mode enabled.

sqlite3 data/stratum1.db '.tables'
# Expected: config_meta  hourly_aggregates  raw_samples

sqlite3 data/stratum1.db 'PRAGMA journal_mode;'
# Expected: wal
```

### Stage 3 — collectors/chrony.py

```bash
python3 app/collectors/chrony.py
# Should print: tracking dict showing PPS reference, Stratum 1
# Verify: parse_tracking assertions passed
# Verify: parse_sourcestats assertions passed
# Verify: parse_sources assertions passed
# Verify: live collect() shows PPS state: best
```

### Stage 4 — collectors/gpsd_client.py

```bash
python3 app/collectors/gpsd_client.py
# Should print: fix_mode: 3 (3D), satellite list with GPS + BeiDou
# Verify: All assertions passed
```

### Stage 5 — collectors/system.py

```bash
python3 app/collectors/system.py
# Should print: cpu_temp_c ~45-55°C, chronyd: ACTIVE, gpsd: ACTIVE
# PPS devices: /dev/pps0 (GPIO PPS) pps@12.-1
# Verify: All tests passed
```

### Stage 6 — poller.py

```bash
python3 app/poller.py --once
# Should print: Cycle 1: ~34 samples written
# No errors

sqlite3 data/stratum1.db \
  'SELECT source, metric, COUNT(*) as n FROM raw_samples GROUP BY source, metric ORDER BY source, metric;'
# Should show ~34 distinct source/metric pairs
```

### Stage 7 — app.py + API

```bash
python3 app/app.py &
sleep 3

curl -s http://localhost:8080/api/overview | python3 -m json.tool | head -30
curl -s http://localhost:8080/health
# Should show: poller_running: true, status: ok

kill %1
```

### Stage 8 — base.html + overview.html

```bash
python3 app/app.py
```

Open `http://PowerPi:8080/` in browser. Verify:
- Sidebar with all page links
- Six stat cards with real values
- PPS sparkline rendering
- Status badge (will show ALERT for ~2 min then OK)
- Dark mode toggle works
- Mobile layout adapts

### Stage 9 — Remaining pages

Copy all templates to `web/templates/`. Test each page:

1. `http://PowerPi:8080/chrony` — tracking panel, sources table, charts
2. `http://PowerPi:8080/gnss` — fix badge, DOPs, constellation bars, position
3. `http://PowerPi:8080/satellites` — sky plot, satellite table, SNR bars
4. `http://PowerPi:8080/system` — stat cards, services, PPS devices, charts
5. `http://PowerPi:8080/history` — charts (fills in over time), daily summary
6. `http://PowerPi:8080/config` — settings form, test actions

For each page verify: no JS console errors, real values shown, auto-refresh working, mobile layout usable.

---

## 3b. Post-Deployment Verification

### Normal startup sequence after reboot

| Time | Expected state |
|------|---------------|
| t=0s | Dashboard starts. Poller first cycle. Chrony still acquiring. |
| t=0–30s | Overview shows **ALERT**. PPS offset may be very large (100,000+ ns). Normal. |
| t=30–90s | Badge transitions to **WARN**. Offset drops to low thousands of ns. |
| t=90–180s | Badge transitions to **OK**. Offset settles to sub-500ns. |
| t=5–10min | Full discipline. Offset typically 10–300ns. |

> **Do not adjust settings or restart if you see ALERT on first load.** Wait 2–3 minutes. It resolves automatically.

### Known normal values

| Metric | Healthy range |
|--------|--------------|
| PPS offset (disciplined) | ±10–300 ns |
| Jitter (RMS offset) | 200–1500 ns |
| Root dispersion | 8–15 µs |
| GPS source offset in chrony | –10 to –40 ms (NMEA delay — always large, always negative, always normal) |
| fritz.box in sources table | Always dashes / zero samples — expected |
| CPU temperature | 44–56 °C at idle |
| Satellites visible | 6–20 (varies through day) |
| GLONASS satellites | Intermittent — normal |
| BeiDou satellites | Often 0 used — normal when GPS provides sufficient fix |

---

## 4. systemd Service

### 4.1 Create service file

```bash
cat > /opt/stratum1-dashboard/stratum1-dashboard.service << 'EOF'
[Unit]
Description=Stratum-1 Dashboard
After=network.target gpsd.service chronyd.service
Wants=gpsd.service chronyd.service

[Service]
Type=simple
User=pi
WorkingDirectory=/opt/stratum1-dashboard
ExecStart=/opt/stratum1-dashboard/venv/bin/python app/app.py
Environment=STRATUM1_DIR=/opt/stratum1-dashboard
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
```

### 4.2 Install and enable

```bash
sudo cp stratum1-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable stratum1-dashboard
sudo systemctl start stratum1-dashboard
sudo systemctl status stratum1-dashboard
```

### 4.3 Verify logs

```bash
journalctl -u stratum1-dashboard -f
journalctl -u stratum1-dashboard -n 50
```

### 4.4 Service commands

| Command | Effect |
|---------|--------|
| `sudo systemctl start stratum1-dashboard` | Start |
| `sudo systemctl stop stratum1-dashboard` | Stop |
| `sudo systemctl restart stratum1-dashboard` | Restart |
| `sudo systemctl status stratum1-dashboard` | Status |
| `journalctl -u stratum1-dashboard -f` | Live logs |

---

## 5. Configuration

Config is managed via `http://PowerPi:8080/config` or by editing `data/config.json` directly.

Changes via the UI write to `data/config.json`. The poller detects the file change and reloads within one poll cycle — no restart required.

### Key settings to review on first run

| Key | Default | Action |
|-----|---------|--------|
| site_name | PowerPi Stratum-1 | Change if desired |
| show_position | true | Set false to hide lat/lon |
| default_theme | system | light / dark / system |
| offset_warning_ns | 100 | Tune to your server's performance |
| dispersion_warning_us | 15 | Based on observed ~11µs on PowerPi |
| expected_constellations | GPS,BeiDou | Add GLONASS if consistently visible |

---

## 6. Updating the Application

```bash
sudo systemctl stop stratum1-dashboard

# Copy changed files to correct locations:
# Python files  → app/
# HTML templates → web/templates/
# Static assets  → web/static/

# If requirements.txt changed:
source venv/bin/activate
pip install -r requirements.txt

sudo systemctl start stratum1-dashboard
sudo systemctl status stratum1-dashboard
```

> **Never delete `data/stratum1.db` or `data/config.json`** when updating. Only `app/` and `web/` change between versions.

---

## 7. Backup and Recovery

```bash
# Data backup (most important — config + database)
tar -czf stratum1-data-$(date +%Y%m%d).tar.gz \
  -C /opt/stratum1-dashboard data/

# Full project backup (excluding venv)
tar -czf stratum1-full-$(date +%Y%m%d).tar.gz \
  -C /opt stratum1-dashboard/ --exclude=venv

# Database size
du -sh /opt/stratum1-dashboard/data/stratum1.db

# Row count
sqlite3 /opt/stratum1-dashboard/data/stratum1.db \
  'SELECT COUNT(*) FROM raw_samples;'
```

---

## 8. Troubleshooting

| Symptom | Action |
|---------|--------|
| All dashes / no data | `sudo systemctl status stratum1-dashboard` → check logs |
| ALERT badge on startup | Normal — wait 2–3 min for chrony to acquire PPS |
| PPS offset not settling | `chronyc sources -v` — check PPS has `*` state. Check `/sys/class/pps/pps0/assert` updating. |
| Permission denied on chronyc | `sudo usermod -aG chrony pi` then re-login |
| Permission denied on gpsd | `sudo usermod -aG dialout pi` then re-login |
| No satellites on sky plot | `gpspipe -w -n 10` — check gpsd receiving SKY messages. Check antenna. |
| Dashboard not loading after reboot | `journalctl -u stratum1-dashboard -n 30` — check for startup errors |
| Config changes not taking effect | Poller reloads on next cycle (up to poll_interval_seconds). Restart if needed. |
| Database growing too large | Reduce `retention_days` in config. Check database size via Config → Check database. |

---

## 9. Quick-Start Checklist

- [ ] `sudo apt install -y python3 python3-venv python3-pip git gpsd gpsd-clients chrony sqlite3`
- [ ] gpsd and chronyd confirmed running. `chronyc tracking` shows Stratum 1 / Reference ID PPS
- [ ] `/dev/pps0` present. sysfs name = `pps@12.-1` (GPIO 12)
- [ ] `sudo mkdir -p /opt/stratum1-dashboard`
- [ ] `sudo chown pi:pi /opt/stratum1-dashboard && cd /opt/stratum1-dashboard`
- [ ] `python3 -m venv venv && source venv/bin/activate`
- [ ] `pip install --upgrade pip`
- [ ] `requirements.txt` created with four packages
- [ ] `pip install -r requirements.txt` — all four packages installed
- [ ] Tabler CSS, Tabler JS, ApexCharts downloaded to `web/static/`
- [ ] Directory structure created: `app/collectors/`, `app/api/`, `web/templates/`, `web/static/`, `data/`, `docs/`
- [ ] `python3 app/paths.py` — paths resolve correctly
- [ ] `python3 app/config.py` — `data/config.json` created with defaults
- [ ] `python3 app/database.py` — `data/stratum1.db` created, WAL mode confirmed
- [ ] `python3 app/collectors/chrony.py` — tracking dict shows PPS, Stratum 1
- [ ] `python3 app/collectors/gpsd_client.py` — satellite list shows GPS + BeiDou
- [ ] `python3 app/collectors/system.py` — cpu_temp_c reasonable, services active
- [ ] `python3 app/poller.py --once` — ~34 samples written to DB, no errors
- [ ] `python3 app/app.py` + `curl /api/overview` — returns populated JSON
- [ ] Open `http://PowerPi:8080/` — overview page renders with data
- [ ] All 6 remaining pages deployed and verified
- [ ] systemd service file created and installed
- [ ] `sudo systemctl enable stratum1-dashboard`
- [ ] `sudo systemctl start stratum1-dashboard` — shows active (running)
- [ ] Reboot Pi — dashboard comes back automatically
- [ ] Config reviewed: site_name, show_position, thresholds

---

## Revision History

| Version | Notes |
|---------|-------|
| v1.0 | Initial release. Install path /home/pi/stratum1-dashboard |
| v1.1 | Install path changed to /opt. INSTALL_DIR introduced. sudo mkdir+chown documented. |
| v1.2 | Project layout restructured: app/, web/, data/, docs/. paths.py added. |
| v1.3 | sqlite3 CLI added to prerequisites |
| v1.4 | Post-deployment verification section added. Known startup behaviours documented. systemd service file updated with Environment=STRATUM1_DIR. |
