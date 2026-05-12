"""
paths.py — Central path definitions for the Stratum-1 dashboard.

Every module that needs a file path imports from here.
No other module constructs paths independently.

Project root is determined by (in priority order):
  1. STRATUM1_DIR environment variable  — set in the systemd service file
  2. Parent directory of this file      — works for any install location

Layout:
    INSTALL_DIR/
    ├── app/          ← Python source (this file lives here)
    ├── web/          ← Frontend assets
    │   ├── templates/
    │   └── static/
    ├── data/         ← Runtime data (config + database)
    ├── docs/         ← Documentation
    └── venv/

Usage:
    from paths import CONFIG_FILE, DB_FILE, TEMPLATES_DIR, STATIC_DIR
"""

import os

# ── Project root ──────────────────────────────────────────────────────────────
# app/ is one level below the project root, so we go up one directory.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get(
    "STRATUM1_DIR",
    os.path.dirname(_APP_DIR)
)

# ── Subdirectories ────────────────────────────────────────────────────────────
APP_DIR       = _APP_DIR
DATA_DIR      = os.path.join(ROOT, "data")
WEB_DIR       = os.path.join(ROOT, "web")
TEMPLATES_DIR = os.path.join(WEB_DIR, "templates")
STATIC_DIR    = os.path.join(WEB_DIR, "static")
DOCS_DIR      = os.path.join(ROOT, "docs")

# ── Runtime files ─────────────────────────────────────────────────────────────
CONFIG_FILE   = os.path.join(DATA_DIR, "config.json")
DB_FILE       = os.path.join(DATA_DIR, "stratum1.db")

# ── Ensure data/ directory exists ─────────────────────────────────────────────
# Called once at import time. Safe to call multiple times.
os.makedirs(DATA_DIR, exist_ok=True)


def summary():
    """Return a dict of all paths — useful for diagnostics and testing."""
    return {
        "ROOT":         ROOT,
        "APP_DIR":      APP_DIR,
        "DATA_DIR":     DATA_DIR,
        "WEB_DIR":      WEB_DIR,
        "TEMPLATES_DIR": TEMPLATES_DIR,
        "STATIC_DIR":   STATIC_DIR,
        "DOCS_DIR":     DOCS_DIR,
        "CONFIG_FILE":  CONFIG_FILE,
        "DB_FILE":      DB_FILE,
        "STRATUM1_DIR_env": os.environ.get("STRATUM1_DIR", "(not set — using parent of app/)"),
    }


# ── Standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import pprint
    print("=" * 60)
    print("paths.py — standalone test")
    print("=" * 60)
    pprint.pprint(summary())
    print()

    # Verify data/ was created
    print(f"data/ exists : {os.path.isdir(DATA_DIR)}")
    print(f"web/  exists : {os.path.isdir(WEB_DIR)}")
    print()

    # Warn about any expected directories that are missing
    expected = [WEB_DIR, TEMPLATES_DIR, STATIC_DIR]
    missing = [p for p in expected if not os.path.isdir(p)]
    if missing:
        print("WARNING — these directories do not exist yet (run setup first):")
        for p in missing:
            print(f"  {p}")
    else:
        print("All expected directories present.")
