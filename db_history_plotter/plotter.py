import os
import csv
import json
import time
import matplotlib
matplotlib.use("Agg")   # headless backend — no display needed
import matplotlib.pyplot as plt
import pandas as pd
from datetime import datetime, UTC
from zoneinfo import ZoneInfo


# ============================================================================
# Configuration
# ============================================================================

# Read config from Home Assistant App Configuration
with open("/data/options.json") as f:
    OPT = json.load(f)

DB_TYPE       = OPT["db_type"]
DB_HOST       = OPT["db_host"]
DB_NAME       = OPT["db_name"]
DB_USER       = OPT["db_user"]
DB_PASSWORD   = OPT["db_password"]
SQLITE_FILE   = OPT["sqlite_file"]
TIMEZONE_NAME = OPT.get("timezone", "UTC")
SENSORS       = OPT["sensors"]

# Paths inside the container
CSV_DIR   = "/tmp/db_history_plotter"
IMAGE_DIR = "/media/db_history_plotter"

TZ       = ZoneInfo(TIMEZONE_NAME)
TZ_LABEL = TIMEZONE_NAME

os.makedirs(CSV_DIR, exist_ok=True)
os.makedirs(IMAGE_DIR, exist_ok=True)


# ============================================================================
# Minimal filesystem / mount diagnostics
# ============================================================================

print()
print(f"{'─' * 60}")
print("  Filesystem / mount diagnostics")
print(f"{'─' * 60}")

print(f"[FS] Current working directory: {os.getcwd()}")


def check_directory(path):
    """Basic access check for an expected mounted directory."""

    print(f"\n[FS] Checking: {path}")

    if not os.path.exists(path):
        print("[FS]   EXISTS: NO")
        return

    if not os.path.isdir(path):
        print("[FS]   EXISTS: YES")
        print("[FS]   DIRECTORY: NO")
        return

    try:
        entries = os.listdir(path)

        print("[FS]   EXISTS: YES")
        print("[FS]   DIRECTORY: YES")
        print("[FS]   ACCESS: YES")
        print(f"[FS]   ENTRIES: {len(entries)}")

    except Exception as e:
        print("[FS]   EXISTS: YES")
        print("[FS]   DIRECTORY: YES")
        print(f"[FS]   ACCESS: NO ({e})")


check_directory("/data")
check_directory("/homeassistant")
check_directory("/media")


# Check configured SQLite path
sqlite_path = os.path.abspath(SQLITE_FILE)

print()
print("[FS] SQLite configuration:")
print(f"[FS]   Configured path: {SQLITE_FILE}")
print(f"[FS]   Absolute path:   {sqlite_path}")
print(f"[FS]   Exists:          {os.path.exists(sqlite_path)}")
print(f"[FS]   Is file:         {os.path.isfile(sqlite_path)}")

if os.path.isfile(sqlite_path):
    print(
        f"[FS]   Size:            "
        f"{os.path.getsize(sqlite_path):,} bytes"
    )

print(f"{'─' * 60}")
print()


# ============================================================================
# Startup log
# ============================================================================

run_start = datetime.now(TZ)

print(f"{'─' * 60}")
print(f"  {run_start.strftime('%Y-%m-%d %H:%M:%S')} {TZ_LABEL}")
print(
    f"  DB:       {DB_TYPE}"
    f"{'  @ ' + DB_HOST if DB_TYPE == 'mariadb' else ''}"
)
print(f"  Sensors:  {len(SENSORS)}")
print(f"{'─' * 60}")


# ============================================================================
# Database connection
# ============================================================================

try:

    if DB_TYPE == "sqlite":

        import sqlite3

        if not os.path.isfile(SQLITE_FILE):
            raise FileNotFoundError(
                f"SQLite database does not exist: {SQLITE_FILE}"
            )

        db_size = os.path.getsize(SQLITE_FILE)

        if db_size == 0:
            raise RuntimeError(
                f"SQLite database is empty (0 bytes): {SQLITE_FILE}"
            )

        conn = sqlite3.connect(SQLITE_FILE)

        print(
            f"[DB] Connected to SQLite: "
            f"{SQLITE_FILE} ({db_size:,} bytes)"
        )

    elif DB_TYPE == "mariadb":

        import MySQLdb

        conn = MySQLdb.connect(
            host=DB_HOST,
            user=DB_USER,
            passwd=DB_PASSWORD,
            db=DB_NAME,
        )

        print(
            f"[DB] Connected to MariaDB: "
            f"{DB_USER}@{DB_HOST}/{DB_NAME}"
        )

    else:

        raise ValueError(
            f"Unknown DB_TYPE '{DB_TYPE}'. "
            f"Choose 'sqlite' or 'mariadb'."
        )

except Exception as e:

    print(f"[DB] ERROR: {e}")
    raise SystemExit(1)


# ============================================================================
# Per-sensor loop
# ============================================================================

processed_sensors = 0

for i, sensor in enumerate(SENSORS):

    sensor_id  = sensor["sensor_id"]
    hours_back = int(sensor["hours_back"])
    y_label    = sensor["y_label"]
    plot_title = sensor["plot_title"]

    csv_file = os.path.join(
        CSV_DIR,
        f"{i}.csv"
    )

    image_file = os.path.join(
        IMAGE_DIR,
        f"{i}.png"
    )

    print()
    print(
        f"[{i}] {sensor_id} "
        f"(last {hours_back}h)"
    )


    # ------------------------------------------------------------------------
    # Build query
    # ------------------------------------------------------------------------

    if DB_TYPE == "sqlite":

        sql_query = f"""
        SELECT
            states.last_updated_ts,
            states.state,
            states_meta.entity_id
        FROM states
        JOIN states_meta
            ON states.metadata_id = states_meta.metadata_id
        WHERE states_meta.entity_id = ?
          AND states.state NOT IN ('unavailable', 'unknown')
          AND states.last_updated_ts >=
              strftime('%s', 'now', ?)
        ORDER BY states.state_id ASC;
        """

        sql_params = (
            sensor_id,
            f"-{hours_back} hours",
        )

        print(
            f"[{i}] Query range: "
            f"last {hours_back}h (SQLite)"
        )

    else:

        cutoff_ts = (
            datetime.now(UTC).timestamp()
            - hours_back * 3600
        )

        cutoff_local = datetime.fromtimestamp(
            cutoff_ts,
            tz=TZ
        ).strftime("%Y-%m-%d %H:%M:%S")

        print(
            f"[{i}] Query range: "
            f"{cutoff_local} → now ({TZ_LABEL})"
        )

        sql_query = """
        SELECT
            states.last_updated_ts,
            states.state,
            states_meta.entity_id
        FROM states
        JOIN states_meta
            ON states.metadata_id = states_meta.metadata_id
        WHERE states_meta.entity_id = %s
          AND states.state NOT IN ('unavailable', 'unknown')
          AND states.last_updated_ts >= %s
        ORDER BY states.state_id ASC;
        """

        sql_params = (
            sensor_id,
            cutoff_ts,
        )


    # ------------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------------

    t_query = time.monotonic()

    try:

        cursor = conn.cursor()

        if DB_TYPE == "sqlite":
            cursor.execute(
                sql_query,
                sql_params
            )
        else:
            cursor.execute(
                sql_query,
                sql_params
            )

        rows = cursor.fetchall()
        cursor.close()

    except Exception as e:

        print(
            f"[{i}] ERROR querying "
            f"{sensor_id}: {e}"
        )

        continue

    elapsed_ms = (
        time.monotonic() - t_query
    ) * 1000

    print(
        f"[{i}] Fetched {len(rows)} rows "
        f"({elapsed_ms:.0f} ms)"
    )


    # ------------------------------------------------------------------------
    # No data
    # ------------------------------------------------------------------------

    if not rows:

        print(
            f"[{i}] No data for "
            f"{sensor_id}, skipping plot."
        )

        continue


    # ------------------------------------------------------------------------
    # Write CSV
    # ------------------------------------------------------------------------

    with open(
        csv_file,
        "w",
        newline=""
    ) as f:

        csv.writer(f).writerows(rows)


    # Keep this because the original working implementation
    # used a short delay before reading the CSV.
    time.sleep(0.5)


    # ------------------------------------------------------------------------
    # Load DataFrame
    # ------------------------------------------------------------------------

    df = pd.read_csv(
        csv_file,
        names=[
            "timestamp",
            "value",
            "entity_id"
        ],
        dtype={
            "timestamp": object,
            "value": object,
            "entity_id": object,
        },
    )


    # Convert timestamp/value to numeric
    df["timestamp"] = pd.to_numeric(
        df["timestamp"],
        errors="coerce"
    )

    df["value"] = pd.to_numeric(
        df["value"],
        errors="coerce"
    )

    df = df.dropna(
        subset=[
            "timestamp",
            "value"
        ]
    )


    # Convert Unix timestamp UTC → configured timezone
    df["timestamp"] = pd.to_datetime(
        df["timestamp"].astype("float64"),
        unit="s",
        utc=True,
        errors="raise"
    ).dt.tz_convert(TZ)


    # ------------------------------------------------------------------------
    # Plot
    # ------------------------------------------------------------------------

    current_time_local = datetime.now(TZ)

    t_plot = time.monotonic()

    plt.figure(
        figsize=(12, 6)
    )

    plt.plot(
        df["timestamp"],
        df["value"],
        marker="o",
        linestyle="-",
        linewidth=2
    )

    plt.title(
        f"{plot_title}\n"
        f"Generated: "
        f"{current_time_local.strftime('%Y-%m-%d %H:%M:%S')} "
        f"{TZ_LABEL}"
    )

    plt.xlabel(
        f"Timestamp ({TZ_LABEL})"
    )

    plt.ylabel(y_label)

    plt.grid(True)

    plt.xticks(
        rotation=45
    )

    plt.tight_layout()

    plt.savefig(
        image_file,
        format="png"
    )

    plt.close()

    plot_ms = (
        time.monotonic() - t_plot
    ) * 1000


    # ------------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------------

    v_min = df["value"].min()
    v_max = df["value"].max()

    ts_first = (
        df["timestamp"]
        .iloc[0]
        .strftime("%H:%M:%S")
    )

    ts_last = (
        df["timestamp"]
        .iloc[-1]
        .strftime("%H:%M:%S")
    )

    print(
        f"[{i}] Values: "
        f"min={v_min:.2f}  "
        f"max={v_max:.2f}  "
        f"range={ts_first}–{ts_last}"
    )

    print(
        f"[{i}] Saved → "
        f"{image_file} "
        f"({plot_ms:.0f} ms)"
    )

    processed_sensors += 1


# ============================================================================
# Close database
# ============================================================================

conn.close()


# ============================================================================
# Summary
# ============================================================================

run_end = datetime.now(TZ)

elapsed_total = (
    run_end - run_start
).total_seconds()

print()
print(f"{'─' * 60}")
print(
    f"  Done. "
    f"{processed_sensors}/{len(SENSORS)} sensor(s) "
    f"processed in {elapsed_total:.1f}s"
)
print(f"{'─' * 60}")
print()
