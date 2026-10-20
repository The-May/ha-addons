import io
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, UTC
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
from matplotlib.figure import Figure   # Figure API (not pyplot) -> thread safe
import numpy as np
import pandas as pd
from flask import Flask, Response, abort, jsonify, render_template_string, request


# ============================================================================
# Configuration
# ============================================================================

OPTIONS_FILE = os.environ.get("OPTIONS_FILE", "/data/options.json")

VIEWS_FILE = os.environ.get(
    "VIEWS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(OPTIONS_FILE)), "views.json"),
)

DEFAULT_PORT = 8099
DEFAULT_HOURS = 24
GLOBAL_KEY = "_global"      # reserved key in views.json (HA entity ids can't start with "_")


def load_options():
    # Re-read on every request so config changes show up without a restart.
    with open(OPTIONS_FILE) as f:
        return json.load(f)


def get_port(opt=None):
    """Web UI port: env PORT (dev override) > options 'webui_port' > 8099."""
    opt = load_options() if opt is None else opt
    for candidate in (os.environ.get("PORT"), opt.get("webui_port")):
        try:
            port = int(candidate)
            if 1 <= port <= 65535:
                return port
        except (TypeError, ValueError):
            pass
    return DEFAULT_PORT


# ============================================================================
# Database
# ============================================================================

def connect(opt):
    db_type = opt["db_type"]

    if db_type == "sqlite":
        path = opt["sqlite_file"]
        if not os.path.isfile(path):
            raise FileNotFoundError(f"SQLite database does not exist: {path}")
        return sqlite3.connect(path)

    if db_type == "mariadb":
        import MySQLdb
        return MySQLdb.connect(
            host=opt["db_host"],
            user=opt["db_user"],
            passwd=opt["db_password"],
            db=opt["db_name"],
        )

    raise ValueError(f"Unknown db_type '{db_type}'. Choose 'sqlite' or 'mariadb'.")


def fetch_df(conn, opt, sensor_id, hours_back):
    """Same queries as the batch script, returned as a clean DataFrame."""
    hours_back = int(hours_back)

    if opt["db_type"] == "sqlite":
        sql = """
        SELECT states.last_updated_ts, states.state
        FROM states
        JOIN states_meta ON states.metadata_id = states_meta.metadata_id
        WHERE states_meta.entity_id = ?
          AND states.state NOT IN ('unavailable', 'unknown')
          AND states.last_updated_ts >= strftime('%s', 'now', ?)
        ORDER BY states.state_id ASC;
        """
        params = (sensor_id, f"-{hours_back} hours")
    else:
        sql = """
        SELECT states.last_updated_ts, states.state
        FROM states
        JOIN states_meta ON states.metadata_id = states_meta.metadata_id
        WHERE states_meta.entity_id = %s
          AND states.state NOT IN ('unavailable', 'unknown')
          AND states.last_updated_ts >= %s
        ORDER BY states.state_id ASC;
        """
        params = (sensor_id, datetime.now(UTC).timestamp() - hours_back * 3600)

    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()

    df = pd.DataFrame(rows, columns=["timestamp", "value"])
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    df["value"]     = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna(subset=["timestamp", "value"])


def fetch_seed(conn, opt, sensor_id, hours_back):
    """
    Last numeric state recorded BEFORE the window starts.
    HA only stores state changes, so this is the value that was still in
    effect when the window began (None if there is none / it is not numeric).
    """
    hours_back = int(hours_back)

    if opt["db_type"] == "sqlite":
        sql = """
        SELECT states.state
        FROM states
        JOIN states_meta ON states.metadata_id = states_meta.metadata_id
        WHERE states_meta.entity_id = ?
          AND states.state NOT IN ('unavailable', 'unknown')
          AND states.last_updated_ts < strftime('%s', 'now', ?)
        ORDER BY states.last_updated_ts DESC
        LIMIT 1;
        """
        params = (sensor_id, f"-{hours_back} hours")
    else:
        sql = """
        SELECT states.state
        FROM states
        JOIN states_meta ON states.metadata_id = states_meta.metadata_id
        WHERE states_meta.entity_id = %s
          AND states.state NOT IN ('unavailable', 'unknown')
          AND states.last_updated_ts < %s
        ORDER BY states.last_updated_ts DESC
        LIMIT 1;
        """
        params = (sensor_id, datetime.now(UTC).timestamp() - hours_back * 3600)

    cur = conn.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    cur.close()

    if not row:
        return None
    try:
        value = float(row[0])
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) else None


# ============================================================================
# View settings: schema, validation, persistence
# ============================================================================

def _sel(*pairs):
    return list(pairs)


GROUPS = [
    ("General", [
        dict(key="title", label="Title", type="text", default=None, wide=True),
        dict(key="y_label", label="Y label / unit", type="text", default=None),
        dict(key="hours_back", label="Range (hours)", type="number", default=None,
             min=1, max=8760, integer=True),
    ]),
    ("Style", [
        dict(key="chart_type", label="Chart type", type="select", default="line",
             options=_sel(("line", "Line"), ("step", "Step"), ("area", "Area"),
                          ("bar", "Bar"), ("stem", "Stem"), ("scatter", "Scatter"))),
        dict(key="theme", label="Theme", type="select", default="light",
             options=_sel(("light", "Light"), ("dark", "Dark"))),
        dict(key="color", label="Color", type="color", default="#03a9f4"),
        dict(key="line_width", label="Line width", type="range", default=2.0,
             min=0.5, max=6, step=0.5),
        dict(key="markers", label="Markers", type="select", default="auto",
             options=_sel(("auto", "Auto"), ("on", "On"), ("off", "Off"))),
        dict(key="marker_size", label="Marker size", type="range", default=5,
             min=2, max=14, step=1),
        dict(key="grid", label="Grid", type="select", default="y",
             options=_sel(("y", "Horizontal"), ("both", "Both"), ("none", "None"))),
        dict(key="aspect", label="Aspect ratio", type="select", default="2:1",
             options=_sel(("3:1", "3:1 (wide)"), ("2:1", "2:1"),
                          ("16:9", "16:9"), ("4:3", "4:3"))),
    ]),
    ("Data", [
        dict(key="fill_gaps", label="Fill gaps (carry last value forward)", type="bool",
             default=True, wide=True,
             hint="HA only stores a value when it changes, so hours without a change "
                  "have no data. This fills them with the last known value instead of "
                  "leaving a gap. For bars/stems it needs an aggregation interval or Max points."),
        dict(key="resample", label="Aggregate (mean)", type="select", default="",
             options=_sel(("", "Off"), ("1min", "1 min"), ("5min", "5 min"),
                          ("10min", "10 min"), ("15min", "15 min"), ("30min", "30 min"),
                          ("1h", "1 hour"), ("2h", "2 hours"), ("3h", "3 hours"),
                          ("4h", "4 hours"), ("6h", "6 hours"), ("12h", "12 hours"),
                          ("1D", "1 day"), ("2D", "2 days"), ("7D", "7 days"))),
        dict(key="max_points", label="Max points / bars", type="range", default=0,
             min=0, max=200, step=1, integer=True,
             hint="Picks the aggregation interval automatically so at most this "
                  "many points/bars are drawn. 0 = no limit."),
        dict(key="smooth", label="Smoothing (points)", type="range", default=0,
             min=0, max=30, step=1, integer=True),
    ]),
    ("Annotations", [
        dict(key="show_avg",    label="Average line", type="bool", default=False),
        dict(key="show_minmax", label="Min / max",    type="bool", default=False),
        dict(key="show_last",   label="Last value",   type="bool", default=True),
    ]),
    ("Axes", [
        dict(key="y_mode", label="Y axis range", type="select", default="absolute", wide=True,
             options=_sel(("absolute", "Fixed values (empty = auto)"),
                          ("relative", "Relative to data min / max"))),
        dict(key="y_min", label="Y min", type="number", default=None,
             show_if=("y_mode", "absolute")),
        dict(key="y_max", label="Y max", type="number", default=None,
             show_if=("y_mode", "absolute")),
        dict(key="y_pad_low", label="Below min (%)", type="number", default=5.0,
             min=-50, max=500, placeholder="5", show_if=("y_mode", "relative"),
             hint="Axis starts this % of the data span below the lowest value. 0 = exactly at the min."),
        dict(key="y_pad_high", label="Above max (%)", type="number", default=5.0,
             min=-50, max=500, placeholder="5", show_if=("y_mode", "relative"),
             hint="Axis ends this % of the data span above the highest value."),
        dict(key="date_format", label="Time format", type="select", default="auto",
             options=_sel(("auto", "Auto"), ("%H:%M", "14:30"),
                          ("%d.%m. %H:%M", "20.10. 14:30"), ("%d.%m.", "20.10."),
                          ("%a %H:%M", "Tue 14:30"))),
    ]),
]

FIELDS = [f for _, fields in GROUPS for f in fields]
FIELD_BY_KEY = {f["key"]: f for f in FIELDS}

ASPECTS = {"3:1": (12, 4), "2:1": (12, 6), "16:9": (12, 6.75), "4:3": (12, 9)}


def defaults():
    return {f["key"]: f["default"] for f in FIELDS}


def clean(f, value):
    """Coerce one incoming value for field `f`. Never trust client input."""
    ftype, default = f["type"], f["default"]
    try:
        if ftype == "select":
            allowed = [v for v, _ in f["options"]]
            return value if value in allowed else default
        if ftype == "text":
            value = None if value is None else str(value).strip()[:120]
            return value or None
        if ftype == "bool":
            return bool(value)
        if ftype == "color":
            return value if re.fullmatch(r"#[0-9a-fA-F]{6}", str(value)) else default
        if ftype == "range":
            value = min(max(float(value), f["min"]), f["max"])
            return int(round(value)) if f.get("integer") else value
        if ftype == "number":
            if value in (None, ""):
                return None
            value = float(value)
            if not np.isfinite(value):
                return None
            if "min" in f:
                value = max(value, f["min"])
            if "max" in f:
                value = min(value, f["max"])
            return int(round(value)) if f.get("integer") else value
    except (TypeError, ValueError):
        return default
    return default


def sanitize(raw):
    out = {f["key"]: clean(f, raw.get(f["key"], f["default"])) for f in FIELDS}
    if out["y_min"] is not None and out["y_max"] is not None and out["y_min"] >= out["y_max"]:
        out["y_min"] = out["y_max"] = None
    return out


_views_lock = threading.Lock()


def load_views():
    try:
        with open(VIEWS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"[views] could not read {VIEWS_FILE}: {e}")
        return {}


def save_views(views):
    os.makedirs(os.path.dirname(VIEWS_FILE) or ".", exist_ok=True)
    tmp = VIEWS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(views, f, indent=2, sort_keys=True)
    os.replace(tmp, VIEWS_FILE)          # atomic: never leaves a half-written file


def get_view(sensor_id, views=None):
    views = load_views() if views is None else views
    return sanitize(views.get(sensor_id, {}))


def resolve(sensor, view, glob):
    """
    Returns (inherited, effective) for the three 'identity' settings.
    `inherited` = what the entity falls back to when its own field is empty
    (shown as placeholder in the form).
    """
    inherited = {
        "hours_back": int(glob.get("hours_back") or sensor.get("hours_back") or DEFAULT_HOURS),
        "title":      sensor.get("plot_title") or sensor["sensor_id"],
        "y_label":    sensor.get("y_label") or "",
    }
    effective = {
        k: (view[k] if view[k] not in (None, "") else inherited[k]) for k in inherited
    }
    return inherited, effective


# ============================================================================
# Plotting
# ============================================================================

THEMES = {
    "light": dict(bg="#ffffff", fg="#1f2937", muted="#6b7280", grid="#e5e7eb", spine="#d1d5db"),
    "dark":  dict(bg="#161a22", fg="#e5e7eb", muted="#9ca3af", grid="#2a303c", spine="#3b4252"),
}

# "Nice" aggregation intervals used by the max-points cap (smallest first).
NICE_RULES = [
    (60, "1min"), (120, "2min"), (300, "5min"), (600, "10min"), (900, "15min"),
    (1800, "30min"), (3600, "1h"), (7200, "2h"), (10800, "3h"), (14400, "4h"),
    (21600, "6h"), (43200, "12h"), (86400, "1D"), (172800, "2D"), (604800, "7D"),
]
RULE_SECONDS = {rule: seconds for seconds, rule in NICE_RULES}


def fig_to_png(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    return buf.getvalue()


def _num(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _aggregate(s, rule, fill, window, seed):
    """Mean per `rule` bucket. With `fill`, empty buckets carry the last value forward."""
    if fill and window:
        # NaN sentinels at window start/end make resample create every bucket in between
        edges = pd.Series([np.nan, np.nan], index=pd.DatetimeIndex(list(window)))
        s = pd.concat([s, edges]).sort_index(kind="mergesort")
    r = s.resample(rule).mean()
    if fill:
        r = r.ffill()
        if seed is not None:
            r = r.fillna(seed)          # buckets before the first change in the window
    return r.dropna()


def _extend_raw(s, window, seed):
    """Raw (not aggregated) data: anchor the seed at the window start, carry the last value to 'now'."""
    start, end = window
    head = None
    if seed is not None and (s.empty or s.index[0] > start):
        head = pd.Series([seed], index=pd.DatetimeIndex([start]))
    last = s.iloc[-1] if len(s) else seed
    tail = None
    if last is not None and (s.empty or s.index[-1] < end):
        tail = pd.Series([last], index=pd.DatetimeIndex([end]))
    parts = [p for p in (head, s, tail) if p is not None and len(p)]
    return pd.concat(parts) if parts else s


def prepare_series(df, view, tz, seed=None, hours_back=None):
    """DataFrame -> tz-aware series: aggregate / fill gaps, cap points, smooth."""
    ts = pd.to_datetime(df["timestamp"].astype("float64"), unit="s", utc=True).dt.tz_convert(tz)
    s = pd.Series(df["value"].to_numpy(dtype=float), index=pd.DatetimeIndex(ts))
    s = s.sort_index(kind="mergesort")

    fill = bool(view["fill_gaps"])
    window = None
    if hours_back:
        end = pd.Timestamp.now(tz=tz)
        window = (end - pd.Timedelta(hours=int(hours_back)), end)

    rule = view["resample"] or None
    cap = view["max_points"]
    raw = s

    if rule:
        s = _aggregate(raw, rule, fill, window, seed)

    if cap > 0 and len(s) > cap:
        # coarsen to the smallest "nice" interval that stays within the cap
        span = (s.index[-1] - s.index[0]).total_seconds()
        floor = max(span / cap, RULE_SECONDS.get(rule, 0))
        for seconds, r in NICE_RULES:
            if seconds < floor:
                continue
            s = _aggregate(raw, r, fill, window, seed)
            if len(s) <= cap:
                break
    elif not rule and fill and window and view["chart_type"] in ("line", "step", "area"):
        s = _extend_raw(s, window, seed)

    if view["smooth"] > 1:
        s = s.rolling(view["smooth"], min_periods=1, center=True).mean()
    return s


def _bar_width(xs):
    """Bar width in days: 80% of the typical spacing between points."""
    diffs = np.diff(xs)
    diffs = diffs[diffs > 0]
    step = float(np.median(diffs)) if len(diffs) else 1 / 24
    return step * 0.8


def compute_ylim(view, ys):
    """(bottom, top) for the Y axis; None = let matplotlib decide."""
    if view["y_mode"] == "relative" and len(ys):
        lo, hi = float(np.min(ys)), float(np.max(ys))
        span = (hi - lo) or max(abs(hi) * 0.1, 1.0)
        pad_low  = 5.0 if view["y_pad_low"]  is None else view["y_pad_low"]
        pad_high = 5.0 if view["y_pad_high"] is None else view["y_pad_high"]
        bottom = lo - span * pad_low / 100
        top    = hi + span * pad_high / 100
        return (bottom, top) if bottom < top else (None, None)
    return view["y_min"], view["y_max"]


def render_plot(df, meta, view, tz, tz_label, seed=None):
    """meta = effective {'title', 'y_label', 'hours_back'} + 'sensor_id'."""
    th = THEMES[view["theme"]]
    s = prepare_series(df, view, tz, seed, meta["hours_back"])
    if s.empty:
        msg = f"No data for\n{meta.get('sensor_id', '')}\n(last {meta['hours_back']}h)"
        if seed is not None:
            msg += "\n\nNo state changes in this range.\nSet an aggregation interval to draw the last known value."
        return render_message(msg)
    xs = mdates.date2num(s.index.to_pydatetime())
    ys = s.to_numpy(dtype=float)
    n = len(ys)

    kind  = view["chart_type"]
    color = view["color"]
    lw    = view["line_width"]
    ms    = view["marker_size"]
    show_markers = view["markers"] == "on" or (view["markers"] == "auto" and n <= 50)
    marker = "o" if show_markers else None
    ybottom, ytop = compute_ylim(view, ys)

    fig = Figure(figsize=ASPECTS[view["aspect"]], facecolor=th["bg"])
    ax = fig.subplots()
    ax.set_facecolor(th["bg"])

    # ---- series --------------------------------------------------------
    if kind == "line":
        ax.plot(xs, ys, color=color, lw=lw, marker=marker, ms=ms, solid_joinstyle="round")
    elif kind == "step":
        ax.step(xs, ys, where="post", color=color, lw=lw, marker=marker, ms=ms)
    elif kind == "area":
        if ybottom is not None:
            floor = ybottom
        else:
            span = (ys.max() - ys.min()) or 1.0
            floor = ys.min() - 0.05 * span
        ax.fill_between(xs, ys, floor, color=color, alpha=0.18, lw=0)
        ax.plot(xs, ys, color=color, lw=lw, marker=marker, ms=ms)
    elif kind == "bar":
        ax.bar(xs, ys, width=_bar_width(xs), color=color, align="center")
    elif kind == "stem":
        ax.vlines(xs, 0, ys, color=color, lw=max(lw * 0.75, 1))
        if marker:
            ax.plot(xs, ys, linestyle="none", marker="o", ms=ms, color=color)
    elif kind == "scatter":
        ax.plot(xs, ys, linestyle="none", marker="o", ms=max(ms, 3), color=color, alpha=0.9)

    if ybottom is not None or ytop is not None:
        ax.set_ylim(bottom=ybottom, top=ytop)

    # ---- annotations ---------------------------------------------------
    if view["show_avg"]:
        avg = float(np.mean(ys))
        ax.axhline(avg, color=th["muted"], lw=1, ls=(0, (4, 3)), alpha=0.8, zorder=1)
        ax.annotate(f"avg {_num(avg)}", xy=(1, avg), xycoords=("axes fraction", "data"),
                    xytext=(-4, 4), textcoords="offset points",
                    ha="right", va="bottom", fontsize=9, color=th["muted"])

    if view["show_minmax"] and n:
        imax, imin = int(np.argmax(ys)), int(np.argmin(ys))
        for idx, off, va in ((imax, (0, 9), "bottom"), (imin, (0, -9), "top")):
            ax.plot([xs[idx]], [ys[idx]], "o", ms=7, color=color, mec=th["bg"], mew=1.5, zorder=5)
            ax.annotate(_num(ys[idx]), xy=(xs[idx], ys[idx]), xytext=off,
                        textcoords="offset points", ha="center", va=va,
                        fontsize=9, color=th["fg"])

    if view["show_last"] and n:
        ax.margins(x=0.06)
        ax.annotate(_num(ys[-1]), xy=(xs[-1], ys[-1]), xytext=(8, 0),
                    textcoords="offset points", ha="left", va="center",
                    fontsize=11, fontweight="bold", color=color)

    # ---- axes / look ---------------------------------------------------
    ax.xaxis_date(tz=tz)
    locator = mdates.AutoDateLocator(tz=tz)
    ax.xaxis.set_major_locator(locator)
    if view["date_format"] == "auto":
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz=tz))
    else:
        ax.xaxis.set_major_formatter(mdates.DateFormatter(view["date_format"], tz=tz))

    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(th["spine"])
    ax.tick_params(colors=th["muted"], labelsize=10, length=3)
    ax.xaxis.get_offset_text().set_color(th["muted"])
    ax.set_ylabel(meta["y_label"], color=th["muted"])

    if view["grid"] == "none":
        ax.grid(False)
    else:
        ax.grid(True, axis="y" if view["grid"] == "y" else "both", color=th["grid"], lw=0.8)
        ax.set_axisbelow(True)

    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    ax.set_title(meta["title"], loc="left", fontsize=15, fontweight="bold",
                 color=th["fg"], pad=24)
    ax.annotate(f"last {meta['hours_back']}h  ·  generated {now} {tz_label}",
                xy=(0, 1), xycoords="axes fraction", xytext=(0, 6),
                textcoords="offset points", va="bottom", fontsize=9, color=th["muted"])

    fig.tight_layout(pad=1.6)
    return fig_to_png(fig)


def render_message(text):
    """Placeholder image for 'no data' / error cases."""
    fig = Figure(figsize=(12, 6), facecolor="#ffffff")
    ax = fig.subplots()
    ax.axis("off")
    ax.text(0.5, 0.5, text, ha="center", va="center", fontsize=14, wrap=True, color="#374151")
    return fig_to_png(fig)


# ============================================================================
# Web app
# ============================================================================

app = Flask(__name__)

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DB History Plotter</title>
<style>
  :root { --bg:#f4f5f7; --card:#fff; --text:#1c1e21; --muted:#6b7280; --line:#e5e7eb; --accent:#03a9f4; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#111317; --card:#1c1f26; --text:#e6e8eb; --muted:#9aa1ad; --line:#2c303a; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  header { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap;
           padding:14px 20px; border-bottom:1px solid var(--line); background:var(--card); }
  h1 { font-size:18px; margin:0; }
  .meta { color:var(--muted); font-size:13px; }
  .tools { display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
  .gl { display:flex; align-items:center; gap:6px; font-size:13px; color:var(--muted); }
  .gl input { width:80px; }
  button { background:var(--accent); color:#fff; border:0; border-radius:6px;
           padding:8px 14px; font:inherit; cursor:pointer; }
  button.ghost { background:transparent; color:var(--muted); border:1px solid var(--line); padding:5px 10px; font-size:13px; }
  main { display:grid; grid-template-columns:repeat(auto-fill, minmax(520px, 1fr));
         gap:16px; padding:20px; align-items:start; }
  @media (max-width:600px) { main { grid-template-columns:1fr; padding:12px; } }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
  .head { display:flex; justify-content:space-between; gap:8px; padding:12px 14px 10px; }
  .card h2 { font-size:15px; margin:0; }
  .sub { color:var(--muted); font-size:12px; font-family:ui-monospace, monospace; }
  .badge { font-size:11px; padding:2px 8px; border-radius:99px; background:var(--accent);
           color:#fff; white-space:nowrap; align-self:flex-start; }
  .badge[hidden] { display:none; }
  .card img { display:block; width:100%; height:auto; background:var(--card); min-height:120px; transition:opacity .15s; }
  .card.loading img { opacity:.5; }

  details.panel { border-top:1px solid var(--line); }
  details.panel summary { cursor:pointer; padding:10px 14px; color:var(--muted); font-size:13px; user-select:none; }
  form.settings { padding:0 14px 14px; display:grid; gap:12px; }
  fieldset { border:1px solid var(--line); border-radius:8px; margin:0; padding:6px 12px 12px;
             display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:10px 14px; }
  legend { color:var(--muted); font-size:11px; padding:0 6px; text-transform:uppercase; letter-spacing:.05em; }
  label.f { display:flex; flex-direction:column; gap:4px; font-size:12px; color:var(--muted); }
  label.f[hidden] { display:none; }
  label.f.wide { grid-column:1 / -1; }
  label.f.check { flex-direction:row; align-items:center; gap:8px; }
  small.hint { color:var(--muted); font-size:11px; opacity:.85; line-height:1.3; }
  select, input[type=number], input[type=text] { width:100%; background:var(--bg); color:var(--text);
         border:1px solid var(--line); border-radius:6px; padding:5px 8px; font:inherit; font-size:13px; }
  input[type=color] { width:100%; height:30px; padding:0; border:1px solid var(--line);
         border-radius:6px; background:var(--bg); }
  .rng { display:flex; align-items:center; gap:8px; }
  .rng input { flex:1; min-width:0; }
  .rng output { min-width:2.4em; text-align:right; color:var(--text); font-size:12px; }
  .foot { display:flex; align-items:center; justify-content:space-between; gap:8px; }
  .status { color:var(--muted); font-size:12px; }
  .status.err, input.err { color:#e5484d; border-color:#e5484d; }
  .empty { padding:40px; text-align:center; color:var(--muted); }
</style>
</head>
<body>
<header>
  <div>
    <h1>DB History Plotter</h1>
    <div class="meta">{{ db }} &middot; {{ cards|length }} sensor(s) &middot; {{ tz }} &middot; {{ n_saved }} saved view(s)</div>
  </div>
  <div class="tools">
    <label class="gl" title="Applies to every graph without its own range. Empty = use the range from the add-on options (or {{ default_hours }}).">
      Default range
      <input type="number" id="globalHours" min="1" max="8760" step="1" placeholder="options" value="{{ glob_hours if glob_hours else '' }}">
      h
    </label>
    <button onclick="refreshAll()">Refresh</button>
  </div>
</header>

{% if cards %}
<main>
  {% for c in cards %}
  <section class="card" data-index="{{ c.i }}">
    <div class="head">
      <div>
        <h2 class="title">{{ c.eff.title }}</h2>
        <div class="sub">{{ c.sensor.sensor_id }} &middot; last <span class="hrs">{{ c.eff.hours_back }}</span>h &middot; #{{ c.i }}</div>
      </div>
      <span class="badge" title="A saved view exists for this entity" {% if not c.saved %}hidden{% endif %}>customized</span>
    </div>

    <img data-src="plot/{{ c.i }}.png" alt="{{ c.eff.title }}" loading="lazy">

    <details class="panel">
      <summary>Customize view</summary>
      <form class="settings" onsubmit="return false">
        {% for title, fields in groups %}
        <fieldset>
          <legend>{{ title }}</legend>
          {% for f in fields %}
            {% set v = c.view[f["key"]] %}
            {% set dep = f["show_if"] %}
            {% if f["type"] == "bool" %}
              <label class="f check{% if f['wide'] %} wide{% endif %}"><input type="checkbox" name="{{ f['key'] }}" {% if v %}checked{% endif %}> {{ f["label"] }}</label>
              {% if f["hint"] %}<small class="hint" style="grid-column:1 / -1">{{ f["hint"] }}</small>{% endif %}
            {% else %}
              <label class="f{% if f['wide'] %} wide{% endif %}" {% if dep %}data-dep="{{ dep[0] }}" data-dep-val="{{ dep[1] }}"{% endif %}>{{ f["label"] }}
                {% if f["type"] == "select" %}
                  <select name="{{ f['key'] }}">
                    {% for val, lab in f["options"] %}<option value="{{ val }}" {% if v == val %}selected{% endif %}>{{ lab }}</option>{% endfor %}
                  </select>
                {% elif f["type"] == "color" %}
                  <input type="color" name="{{ f['key'] }}" value="{{ v }}">
                {% elif f["type"] == "range" %}
                  <span class="rng"><input type="range" name="{{ f['key'] }}" min="{{ f['min'] }}" max="{{ f['max'] }}" step="{{ f['step'] }}" value="{{ v }}"><output>{{ v }}</output></span>
                {% elif f["type"] == "text" %}
                  <input type="text" name="{{ f['key'] }}" maxlength="120" placeholder="{{ c.inh.get(f['key'], '') }}" value="{{ '' if v is none else v }}">
                {% elif f["type"] == "number" %}
                  <input type="number" name="{{ f['key'] }}" step="{{ 1 if f['integer'] else 'any' }}"
                         {% if f['min'] is defined %}min="{{ f['min'] }}"{% endif %} {% if f['max'] is defined %}max="{{ f['max'] }}"{% endif %}
                         placeholder="{{ c.inh.get(f['key'], f.get('placeholder', 'auto')) }}" value="{{ '' if v is none else v }}">
                {% endif %}
                {% if f["hint"] %}<small class="hint">{{ f["hint"] }}</small>{% endif %}
              </label>
            {% endif %}
          {% endfor %}
        </fieldset>
        {% endfor %}
        <div class="foot">
          <span class="status" aria-live="polite"></span>
          <button type="button" class="ghost reset">Reset to default</button>
        </div>
      </form>
    </details>
  </section>
  {% endfor %}
</main>
{% else %}
<div class="empty">No sensors configured.</div>
{% endif %}

<script>
  function collect(form) {
    const out = {};
    form.querySelectorAll("[name]").forEach(el => {
      if (el.type === "checkbox") out[el.name] = el.checked;
      else if (el.type === "range") out[el.name] = parseFloat(el.value);
      else if (el.type === "number") out[el.name] = el.value === "" ? null : parseFloat(el.value);
      else out[el.name] = el.value;
    });
    return out;
  }

  function applyToForm(form, view) {
    Object.entries(view).forEach(([k, v]) => {
      const el = form.elements[k];
      if (!el) return;
      if (el.type === "checkbox") el.checked = !!v;
      else el.value = v === null ? "" : v;
    });
    syncOutputs(form);
    applyDeps(form);
  }

  function syncOutputs(form) {
    form.querySelectorAll("input[type=range]").forEach(r => { r.nextElementSibling.textContent = r.value; });
  }

  // Show a field only while the field it depends on has the expected value.
  function applyDeps(form) {
    form.querySelectorAll("[data-dep]").forEach(el => {
      const src = form.elements[el.dataset.dep];
      el.hidden = !src || src.value !== el.dataset.depVal;
    });
  }

  function updateHead(card, eff) {
    card.querySelector(".title").textContent = eff.title;
    card.querySelector(".hrs").textContent = eff.hours_back;
    card.querySelector("img").alt = eff.title;
  }

  function refreshImg(card) {
    const img = card.querySelector("img");
    card.classList.add("loading");
    img.src = img.dataset.src + "?t=" + Date.now();
  }

  function setStatus(card, text, isErr) {
    const el = card.querySelector(".status");
    el.textContent = text;
    el.classList.toggle("err", !!isErr);
  }

  async function save(card) {
    const form = card.querySelector("form");
    try {
      const res = await fetch("api/view/" + card.dataset.index, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(collect(form))
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      updateHead(card, data.effective);
      card.querySelector(".badge").hidden = false;
      setStatus(card, "Saved for this entity");
      refreshImg(card);
    } catch (e) {
      setStatus(card, "Could not save: " + e.message, true);
    }
  }

  async function reset(card) {
    const form = card.querySelector("form");
    try {
      const res = await fetch("api/view/" + card.dataset.index, { method: "DELETE" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      applyToForm(form, data.view);
      updateHead(card, data.effective);
      card.querySelector(".badge").hidden = true;
      setStatus(card, "Reset to default");
      refreshImg(card);
    } catch (e) {
      setStatus(card, "Could not reset: " + e.message, true);
    }
  }

  function refreshAll() {
    document.querySelectorAll(".card").forEach(refreshImg);
  }

  document.querySelectorAll(".card").forEach(card => {
    const form = card.querySelector("form");
    const img = card.querySelector("img");
    img.addEventListener("load",  () => card.classList.remove("loading"));
    img.addEventListener("error", () => card.classList.remove("loading"));

    let timer;
    const queueSave = () => { clearTimeout(timer); timer = setTimeout(() => save(card), 450); };
    form.addEventListener("input",  () => { syncOutputs(form); applyDeps(form); queueSave(); });
    form.addEventListener("change", () => { applyDeps(form); queueSave(); });
    card.querySelector(".reset").addEventListener("click", () => reset(card));
    applyDeps(form);
  });

  // Global default range: saved, then the page reloads so every inheriting card updates.
  const globalHours = document.getElementById("globalHours");
  if (globalHours) {
    let gtimer;
    globalHours.addEventListener("input", () => {
      clearTimeout(gtimer);
      globalHours.classList.remove("err");
      gtimer = setTimeout(async () => {
        try {
          const res = await fetch("api/global", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ hours_back: globalHours.value === "" ? null : parseFloat(globalHours.value) })
          });
          if (!res.ok) throw new Error("HTTP " + res.status);
          location.reload();
        } catch (e) {
          globalHours.classList.add("err");
        }
      }, 700);
    });
  }

  refreshAll();
</script>
</body>
</html>
"""


def _sensor_or_404(i):
    sensors = load_options()["sensors"]
    if not 0 <= i < len(sensors):
        abort(404)
    return sensors[i]


@app.get("/")
def index():
    opt = load_options()
    views = load_views()
    glob = views.get(GLOBAL_KEY, {})

    cards = []
    for i, s in enumerate(opt["sensors"]):
        view = get_view(s["sensor_id"], views)
        inh, eff = resolve(s, view, glob)
        cards.append({"i": i, "sensor": s, "view": view, "inh": inh, "eff": eff,
                      "saved": s["sensor_id"] in views})

    db = opt["db_type"] + (f" @ {opt['db_host']}" if opt["db_type"] == "mariadb" else "")
    return render_template_string(
        PAGE,
        cards=cards,
        groups=GROUPS,
        db=db,
        tz=opt.get("timezone", "UTC"),
        n_saved=len([k for k in views if k != GLOBAL_KEY]),
        glob_hours=glob.get("hours_back"),
        default_hours=DEFAULT_HOURS,
    )


@app.get("/plot/<int:i>.png")
def plot(i):
    opt = load_options()
    sensor = _sensor_or_404(i)
    views = load_views()
    view = get_view(sensor["sensor_id"], views)
    _, eff = resolve(sensor, view, views.get(GLOBAL_KEY, {}))

    tz_label = opt.get("timezone", "UTC")
    tz = ZoneInfo(tz_label)

    conn = None
    try:
        conn = connect(opt)
        seed = None
        if view["fill_gaps"]:
            seed = fetch_seed(conn, opt, sensor["sensor_id"], eff["hours_back"])
        df = fetch_df(conn, opt, sensor["sensor_id"], eff["hours_back"])
        if df.empty and seed is None:
            png = render_message(
                f"No data for\n{sensor['sensor_id']}\n(last {eff['hours_back']}h)"
            )
        else:
            meta = {**eff, "sensor_id": sensor["sensor_id"]}
            png = render_plot(df, meta, view, tz, tz_label, seed)
    except Exception as e:
        app.logger.exception("Plot %s failed", i)
        png = render_message(f"Error:\n{e}")
    finally:
        if conn is not None:
            conn.close()

    return Response(png, mimetype="image/png", headers={"Cache-Control": "no-store"})


@app.post("/api/view/<int:i>")
def api_save_view(i):
    sensor = _sensor_or_404(i)
    sensor_id = sensor["sensor_id"]
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400)

    with _views_lock:
        views = load_views()
        merged = {**views.get(sensor_id, {}), **payload}
        views[sensor_id] = sanitize(merged)
        try:
            save_views(views)
        except OSError as e:
            app.logger.exception("Could not write %s", VIEWS_FILE)
            return jsonify(error=str(e)), 500

    _, eff = resolve(sensor, views[sensor_id], views.get(GLOBAL_KEY, {}))
    return jsonify(view=views[sensor_id], effective=eff)


@app.delete("/api/view/<int:i>")
def api_reset_view(i):
    sensor = _sensor_or_404(i)
    sensor_id = sensor["sensor_id"]
    with _views_lock:
        views = load_views()
        if views.pop(sensor_id, None) is not None:
            try:
                save_views(views)
            except OSError as e:
                app.logger.exception("Could not write %s", VIEWS_FILE)
                return jsonify(error=str(e)), 500

    view = defaults()
    _, eff = resolve(sensor, view, views.get(GLOBAL_KEY, {}))
    return jsonify(view=view, effective=eff)


@app.post("/api/global")
def api_save_global():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400)

    hours = clean(FIELD_BY_KEY["hours_back"], payload.get("hours_back"))

    with _views_lock:
        views = load_views()
        glob = dict(views.get(GLOBAL_KEY, {}))
        if hours is None:
            glob.pop("hours_back", None)
        else:
            glob["hours_back"] = hours

        if glob:
            views[GLOBAL_KEY] = glob
        else:
            views.pop(GLOBAL_KEY, None)

        try:
            save_views(views)
        except OSError as e:
            app.logger.exception("Could not write %s", VIEWS_FILE)
            return jsonify(error=str(e)), 500
    return jsonify(glob)


if __name__ == "__main__":
    # standalone mode (development); in the add-on plotter.py starts the server
    app.run(host="0.0.0.0", port=get_port(), threaded=True)