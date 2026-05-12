"""
app.py — Flask application entry point for the Stratum-1 dashboard.

Starts the poller as a daemon thread on startup, registers all API
blueprints, and serves static files and HTML templates from the web/
directory.

Run directly for development:
    python3 app/app.py

In production, started by systemd using the venv Python:
    /opt/stratum1-dashboard/venv/bin/python app/app.py
"""

import os
import sys
import logging

# Ensure app/ is on the path regardless of working directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, send_from_directory, render_template
from paths import TEMPLATES_DIR, STATIC_DIR, ROOT
import config
import poller
import database

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(
    __name__,
    template_folder=TEMPLATES_DIR,
    static_folder=STATIC_DIR,
    static_url_path="/static",
)

# ── Register API blueprints ───────────────────────────────────────────────────

from api.overview   import bp as overview_bp
from api.chrony     import bp as chrony_bp
from api.gnss       import bp as gnss_bp
from api.satellites import bp as satellites_bp
from api.system     import bp as system_bp
from api.history    import bp as history_bp
from api.config_api import bp as config_bp

app.register_blueprint(overview_bp)
app.register_blueprint(chrony_bp)
app.register_blueprint(gnss_bp)
app.register_blueprint(satellites_bp)
app.register_blueprint(system_bp)
app.register_blueprint(history_bp)
app.register_blueprint(config_bp)

# ── Page routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    landing = config.get("default_landing_page", "overview")
    return render_template(f"{landing}.html", **_template_context())

@app.route("/overview")
def page_overview():
    return render_template("overview.html", **_template_context())

@app.route("/chrony")
def page_chrony():
    return render_template("chrony.html", **_template_context())

@app.route("/gnss")
def page_gnss():
    return render_template("gnss.html", **_template_context())

@app.route("/satellites")
def page_satellites():
    return render_template("satellites.html", **_template_context())

@app.route("/system")
def page_system():
    return render_template("system.html", **_template_context())

@app.route("/history")
def page_history():
    return render_template("history.html", **_template_context())

@app.route("/config")
def page_config():
    return render_template("config.html", **_template_context())


def _template_context():
    """Variables available to all templates."""
    return {
        "site_name":    config.get("site_name", "Stratum-1 Dashboard"),
        "hostname":     config.get("hostname", ""),
        "default_theme": config.get("default_theme", "system"),
        "refresh_interval": config.get("ui_refresh_interval_seconds", 10),
    }


# ── Health check ──────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    from flask import jsonify
    import time
    return jsonify({
        "status":         "ok",
        "poller_running": poller.is_running(),
        "last_cycle":     poller.last_cycle_time(),
        "uptime_s":       round(time.time() - _start_time, 1),
    })


# ── Startup ───────────────────────────────────────────────────────────────────

_start_time = None


def create_app():
    """
    Initialise the application.
    Separated from module-level code so it can be called cleanly
    by tests or a WSGI server without running the poller.
    """
    global _start_time
    import time
    _start_time = time.time()

    config.load()
    database.init()

    site_name = config.get("site_name", "Stratum-1 Dashboard")
    logger.info("Starting %s", site_name)
    logger.info("Templates : %s", TEMPLATES_DIR)
    logger.info("Static    : %s", STATIC_DIR)

    poller.start()
    logger.info("Poller started")

    return app


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Stratum-1 Dashboard")
    parser.add_argument("--host",  default="0.0.0.0",
                        help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port",  default=8080, type=int,
                        help="Bind port (default: 8080)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable Flask debug mode (do not use in production)")
    args = parser.parse_args()

    create_app()

    logger.info("Dashboard listening on http://%s:%d", args.host, args.port)
    logger.info("Local URL: http://localhost:%d", args.port)

    app.run(
        host=args.host,
        port=args.port,
        debug=args.debug,
        use_reloader=False,   # Reloader conflicts with the poller thread
    )
