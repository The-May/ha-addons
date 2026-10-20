import os
import signal
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from werkzeug.serving import make_server

import webui

sys.stdout.reconfigure(line_buffering=True)   # show log lines immediately in the add-on log


# ============================================================================
# Configuration
# ============================================================================

IMAGE_DIR = os.environ.get("IMAGE_DIR", "/media/db_history_plotter")
DEFAULT_REFRESH_MINUTES = 10


def refresh_minutes(opt):
    try:
        return max(1, int(opt.get("refresh_minutes", DEFAULT_REFRESH_MINUTES)))
    except (TypeError, ValueError):
        return DEFAULT_REFRESH_MINUTES


# ============================================================================
# Startup diagnostics
# ============================================================================

def check_directory(path):
    """Basic access check for an expected mounted directory."""
    print(f"[FS] Checking: {path}")
    if not os.path.exists(path):
        print("[FS]   EXISTS: NO")
        return
    if not os.path.isdir(path):
        print("[FS]   EXISTS: YES, DIRECTORY: NO")
        return
    try:
        print(f"[FS]   OK, {len(os.listdir(path))} entries")
    except Exception as e:
        print(f"[FS]   ACCESS: NO ({e})")


def startup_log(opt):
    tz_label = opt.get("timezone", "UTC")
    now = datetime.now(ZoneInfo(tz_label))
    db = opt["db_type"] + (f" @ {opt['db_host']}" if opt["db_type"] == "mariadb" else "")

    print(f"{'─' * 60}")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S')} {tz_label}")
    print(f"  DB:       {db}")
    print(f"  Sensors:  {len(opt['sensors'])}")
    print(f"  Views:    {webui.VIEWS_FILE} "
          f"({'found' if os.path.isfile(webui.VIEWS_FILE) else 'not found, using defaults'})")
    print(f"  Images:   {IMAGE_DIR}")
    print(f"{'─' * 60}")

    for path in ("/data", "/media", IMAGE_DIR):
        check_directory(path)

    if opt["db_type"] == "sqlite":
        p = os.path.abspath(opt["sqlite_file"])
        size = f"{os.path.getsize(p):,} bytes" if os.path.isfile(p) else "n/a"
        print(f"[FS] SQLite: {p} (exists: {os.path.isfile(p)}, size: {size})")
    print(f"{'─' * 60}")


# ============================================================================
# Web UI (started once, before the loop)
# ============================================================================

def start_webui(port):
    try:
        server = make_server("0.0.0.0", port, webui.app, threaded=True)
    except (OSError, SystemExit) as e:
        # werkzeug calls sys.exit() when the port is taken; keep writing PNGs
        # even if the UI cannot start, but make it obvious in the log
        print(f"[UI] ERROR: cannot listen on port {port} ({e or 'port in use?'})")
        return
    threading.Thread(target=server.serve_forever, name="webui", daemon=True).start()
    print(f"[UI] Web UI listening on port {port}")


# ============================================================================
# One render pass
# ============================================================================

def render_sensor(conn, opt, i, sensor, views, glob, tz, tz_label):
    """Fetch + draw + save one sensor. Returns True if an image was written."""
    sid = sensor["sensor_id"]
    view = webui.get_view(sid, views)               # saved view, else defaults
    _, eff = webui.resolve(sensor, view, glob)      # title / y label / hours
    hours = eff["hours_back"]

    print()
    print(f"[{i}] {sid} (last {hours}h, {'saved view' if sid in views else 'default view'})")

    t = time.monotonic()
    seed = webui.fetch_seed(conn, opt, sid, hours) if view["fill_gaps"] else None
    df = webui.fetch_df(conn, opt, sid, hours)
    note = f", last value before window: {seed:g}" if seed is not None else ""
    print(f"[{i}] Fetched {len(df)} rows ({(time.monotonic() - t) * 1000:.0f} ms){note}")

    if df.empty and seed is None:
        print(f"[{i}] No data for {sid}, skipping plot.")
        return False

    t = time.monotonic()
    png = webui.render_plot(df, {**eff, "sensor_id": sid}, view, tz, tz_label, seed)
    plot_ms = (time.monotonic() - t) * 1000

    path = os.path.join(IMAGE_DIR, f"{i}.png")
    tmp = os.path.join(IMAGE_DIR, f".{i}.png.tmp")
    with open(tmp, "wb") as f:
        f.write(png)
    os.replace(tmp, path)       # atomic: HA never serves a half-written image

    if not df.empty:
        first = datetime.fromtimestamp(float(df["timestamp"].iloc[0]), tz).strftime("%H:%M:%S")
        last = datetime.fromtimestamp(float(df["timestamp"].iloc[-1]), tz).strftime("%H:%M:%S")
        print(f"[{i}] Values: min={df['value'].min():.2f}  max={df['value'].max():.2f}  "
              f"range={first}–{last}")
    print(f"[{i}] Saved → {path} ({plot_ms:.0f} ms)")
    return True


def render_pass():
    opt = webui.load_options()          # re-read: picks up changes
    views = webui.load_views()          # {} if views.json does not exist yet
    glob = views.get(webui.GLOBAL_KEY, {})
    tz_label = opt.get("timezone", "UTC")
    tz = ZoneInfo(tz_label)
    sensors = opt["sensors"]

    started = datetime.now(tz)
    print()
    print(f"{'─' * 60}")
    print(f"  Render pass {started.strftime('%Y-%m-%d %H:%M:%S')} {tz_label}")
    print(f"{'─' * 60}")

    try:
        conn = webui.connect(opt)
    except Exception as e:
        print(f"[DB] ERROR: {e}")
        return
    print(f"[DB] Connected ({opt['db_type']})")

    processed = 0
    try:
        for i, sensor in enumerate(sensors):
            try:
                if render_sensor(conn, opt, i, sensor, views, glob, tz, tz_label):
                    processed += 1
            except Exception as e:      # one broken sensor must not stop the others
                print(f"[{i}] ERROR for {sensor.get('sensor_id')}: {e}")
    finally:
        conn.close()

    elapsed = (datetime.now(tz) - started).total_seconds()
    print()
    print(f"{'─' * 60}")
    print(f"  Done. {processed}/{len(sensors)} sensor(s) processed in {elapsed:.1f}s")
    print(f"{'─' * 60}")


# ============================================================================
# Main
# ============================================================================

def main():
    # Home Assistant stops the add-on with SIGTERM: exit right away instead of
    # waiting for the kill timeout (the process is PID 1 in the container).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    opt = webui.load_options()
    os.makedirs(IMAGE_DIR, exist_ok=True)

    startup_log(opt)
    start_webui(webui.get_port(opt))        # once, outside of the loop

    while True:
        try:
            render_pass()
        except Exception as e:              # never let the loop die (the web UI keeps running)
            print(f"[LOOP] ERROR: {e}")

        minutes = refresh_minutes(webui.load_options())
        print(f"[LOOP] Next render pass in {minutes} min")
        time.sleep(minutes * 60)


if __name__ == "__main__":
    main()