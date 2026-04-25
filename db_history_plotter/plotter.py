import os
import csv
import json
import time
import matplotlib
matplotlib.use("Agg")   # headless backend — no display needed
import matplotlib.pyplot as plt
import pandas as pd
from datetime import datetime, timezone, UTC
from zoneinfo import ZoneInfo

# Read config from environment (setup in Addon/App Configuration Tab)
with open("/data/options.json") as f:
    OPT = json.load(f)

DB_TYPE  = OPT["db_type"]
DB_HOST  = OPT["db_host"]
DB_NAME  = OPT["db_name"]
DB_USER  = OPT["db_user"]
DB_PASSWORD = OPT["db_password"]
TIMEZONE_NAME = OPT.get("timezone", "UTC")
SENSORS  = OPT["sensors"]

# Paths inside the container
CSV_DIR   = "/tmp/db_history_plotter"
IMAGE_DIR = "/media/db_history_plotter"

TZ       = ZoneInfo(TIMEZONE_NAME)
TZ_LABEL = TIMEZONE_NAME

os.makedirs(CSV_DIR,   exist_ok=True)
os.makedirs(IMAGE_DIR, exist_ok=True)

# Startup log
run_start = datetime.now(TZ)
print(f"{'─'*60}")
print(f"  {run_start.strftime('%Y-%m-%d %H:%M:%S')} {TZ_LABEL}")
print(f"  DB:       {DB_TYPE}  {'@ ' + DB_HOST if DB_TYPE == 'mariadb' else ''}")
print(f"  Sensors:  {len(SENSORS)}")
print(f"{'─'*60}")

# DB connection
try:
    if DB_TYPE == "sqlite":
        import sqlite3
        SQLITE_FILE = "/config/home-assistant_v2.db"
        conn = sqlite3.connect(SQLITE_FILE)
        print(f"[DB] Connected to SQLite: {SQLITE_FILE}")
    elif DB_TYPE == "mariadb":
        import MySQLdb
        conn = MySQLdb.connect(
            host=DB_HOST,
            user=DB_USER,
            passwd=DB_PASSWORD,
            db=DB_NAME,
        )
        print(f"[DB] Connected to MariaDB: {DB_USER}@{DB_HOST}/{DB_NAME}")
    else:
        raise ValueError(f"Unknown DB_TYPE '{DB_TYPE}'. Choose 'sqlite' or 'mariadb'.")
except Exception as e:
    print(f"[DB] ERROR: {e}")
    exit(1)

# Per-sensor loop
for i, sensor in enumerate(SENSORS):
    sensor_id  = sensor["sensor_id"]
    hours_back = int(sensor["hours_back"])
    y_label    = sensor["y_label"]
    plot_title = sensor["plot_title"]
    csv_file   = os.path.join(CSV_DIR,   f"{i}.csv")
    image_file = os.path.join(IMAGE_DIR, f"{i}.png")

    print(f"\n[{i}] {sensor_id}  (last {hours_back}h)")

    # Build query
    if DB_TYPE == "sqlite":
        sql_query = f"""
        SELECT states.last_updated_ts, states.state, states_meta.entity_id
        FROM states
        JOIN states_meta ON states.metadata_id = states_meta.metadata_id
        WHERE states_meta.entity_id = '{sensor_id}'
          AND states.state != 'unavailable'
          AND states.last_updated_ts >= strftime('%s', 'now', '-{hours_back} hours')
        ORDER BY states.state_id ASC;
        """
    else:
        cutoff_ts = datetime.now(UTC).timestamp() - hours_back * 3600
        cutoff_local = datetime.fromtimestamp(cutoff_ts, tz=TZ).strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{i}] Query range: {cutoff_local} → now  ({TZ_LABEL})")
        sql_query = f"""
        SELECT states.last_updated_ts, states.state, states_meta.entity_id
        FROM states
        JOIN states_meta ON states.metadata_id = states_meta.metadata_id
        WHERE states_meta.entity_id = '{sensor_id}'
          AND states.state != 'unavailable'
          AND states.last_updated_ts >= {cutoff_ts}
        ORDER BY states.state_id ASC;
        """

    # Fetch
    t_query = time.monotonic()
    try:
        cursor = conn.cursor()
        cursor.execute(sql_query)
        rows = cursor.fetchall()
        cursor.close()
    except Exception as e:
        print(f"[{i}] ERROR querying {sensor_id}: {e}")
        continue
    elapsed_ms = (time.monotonic() - t_query) * 1000

    print(f"[{i}] Fetched {len(rows)} rows  ({elapsed_ms:.0f} ms)")

    if not rows:
        print(f"[{i}] No data for {sensor_id}, skipping plot.")
        continue

    # Write CSV
    with open(csv_file, "w", newline="") as f:
        csv.writer(f).writerows(rows)

    time.sleep(0.5)

    # Load DataFrame
    df = pd.read_csv(
        csv_file,
        names=["timestamp", "value", "entity_id"],
        dtype={"timestamp": object, "value": float, "entity_id": object},
    )

    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"])
    df["timestamp"] = pd.to_datetime(
        df["timestamp"].astype("float64"), unit="s", utc=True, errors="raise"
    ).dt.tz_convert(TZ)

    # Plot
    current_time_local = datetime.now(TZ)
    t_plot = time.monotonic()
    plt.figure(figsize=(12, 6))
    plt.plot(df["timestamp"], df["value"], marker="o", linestyle="-", linewidth=2)
    plt.title(
        f"{plot_title}\nGenerated: {current_time_local.strftime('%Y-%m-%d %H:%M:%S')} {TZ_LABEL}"
    )
    plt.xlabel(f"Timestamp ({TZ_LABEL})")
    plt.ylabel(y_label)
    plt.grid(True)
    plt.xticks(rotation=45)
    plt.tight_layout()
    plt.savefig(image_file, format="png")
    plt.close()
    plot_ms = (time.monotonic() - t_plot) * 1000

    v_min, v_max = df["value"].min(), df["value"].max()
    ts_first = df["timestamp"].iloc[0].strftime('%H:%M:%S')
    ts_last  = df["timestamp"].iloc[-1].strftime('%H:%M:%S')
    print(f"[{i}] Values: min={v_min:.2f}  max={v_max:.2f}  range={ts_first}–{ts_last}")
    print(f"[{i}] Saved → {image_file}  ({plot_ms:.0f} ms)")

conn.close()

# Summary log
run_end = datetime.now(TZ)
elapsed_total = (run_end - run_start).total_seconds()
print(f"\n{'─'*60}")
print(f"  Done. {len(SENSORS)} sensor(s) processed in {elapsed_total:.1f}s")
print(f"{'─'*60}\n")