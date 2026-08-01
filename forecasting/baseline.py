"""
Seasonal-naive PV-generation forecast baseline, evaluated against real
InfluxDB history.

The model: predict each time-of-day bucket as the mean of that same bucket
over the trailing FORECAST_TRAILING_DAYS days. No weather or tariff data
involved - this establishes the accuracy floor achievable from the plant's
own PV history alone, before investing in a weather-driven model.

Evaluated via walk-forward validation: for each day in history, predict it
using only days strictly before it. Days with no prior history are skipped,
not silently scored as perfect.

Usage:
    python3 forecasting/baseline.py

Reads InfluxDB connection details from the repo's .env (INFLUXDB_TOKEN,
INFLUXDB_ORG, INFLUXDB_BUCKET). INFLUXDB_URL defaults to localhost:8086
since this runs on the host, not inside the Docker network.
"""
import os
import warnings
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from influxdb_client import InfluxDBClient
from influxdb_client.client.warnings import MissingPivotFunction

# We deliberately query a single field in long format - no pivot() needed.
warnings.simplefilter("ignore", MissingPivotFunction)

REPO_ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo(os.environ.get("FORECAST_TZ", "Europe/London"))
BUCKET_MINUTES = int(os.environ.get("FORECAST_BUCKET_MINUTES", 5))
TRAILING_DAYS = int(os.environ.get("FORECAST_TRAILING_DAYS", 14))
DAYTIME_THRESHOLD_KW = 0.05


def load_dotenv(dotenv_path: Path) -> dict:
    env = {}
    for line in dotenv_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def fetch_pv_history() -> pd.DataFrame:
    env = load_dotenv(REPO_ROOT / ".env")
    url = os.environ.get("INFLUXDB_URL", "http://localhost:8086")
    client = InfluxDBClient(url=url, token=env["INFLUXDB_TOKEN"], org=env["INFLUXDB_ORG"])
    query = f'''
    from(bucket: "{env["INFLUXDB_BUCKET"]}")
      |> range(start: 0)
      |> filter(fn: (r) => r._measurement == "sigenstor" and r._field == "pv_power_kw")
      |> aggregateWindow(every: {BUCKET_MINUTES}m, fn: mean, createEmpty: false)
      |> keep(columns: ["_time", "_value"])
    '''
    try:
        result = client.query_api().query_data_frame(query)
    finally:
        client.close()

    frames = result if isinstance(result, list) else [result]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=["time", "pv_power_kw"])

    df = pd.concat(frames, ignore_index=True).rename(columns={"_time": "time", "_value": "pv_power_kw"})
    df = df[["time", "pv_power_kw"]]
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(TZ)
    df["date"] = df["time"].dt.floor("D")
    minute_of_day = df["time"].dt.hour * 60 + df["time"].dt.minute
    df["bucket"] = (minute_of_day // BUCKET_MINUTES).astype(int)
    return df.sort_values("time").reset_index(drop=True)


def seasonal_naive_walk_forward(df: pd.DataFrame) -> pd.DataFrame:
    dates = sorted(df["date"].unique())
    scored_days = []
    for d in dates:
        history = df[(df["date"] < d) & (df["date"] >= d - pd.Timedelta(days=TRAILING_DAYS))]
        if history.empty:
            continue
        predicted = history.groupby("bucket")["pv_power_kw"].mean().rename("predicted_kw")
        actual = df[df["date"] == d].join(predicted, on="bucket").dropna(subset=["predicted_kw"])
        if not actual.empty:
            scored_days.append(actual)
    return pd.concat(scored_days, ignore_index=True) if scored_days else pd.DataFrame()


def score(evaluated: pd.DataFrame) -> dict:
    err = evaluated["predicted_kw"] - evaluated["pv_power_kw"]
    daytime = evaluated[evaluated["pv_power_kw"] > DAYTIME_THRESHOLD_KW]
    daytime_err = daytime["predicted_kw"] - daytime["pv_power_kw"]
    return {
        "n_points": len(evaluated),
        "n_days": evaluated["date"].nunique(),
        "mae_kw": err.abs().mean(),
        "rmse_kw": (err**2).mean() ** 0.5,
        "mae_kw_daytime_only": daytime_err.abs().mean() if not daytime.empty else float("nan"),
    }


def main() -> None:
    df = fetch_pv_history()
    if df.empty:
        print("No pv_power_kw data in InfluxDB yet.")
        return

    span = df["time"].max() - df["time"].min()
    n_days = df["date"].nunique()
    print(
        f"Loaded {len(df)} points spanning {span} across {n_days} distinct day(s) "
        f"({df['time'].min()} to {df['time'].max()})."
    )

    evaluated = seasonal_naive_walk_forward(df)
    if evaluated.empty:
        print(
            "\nNot enough history to walk-forward evaluate: every day needs at least one "
            f"PRIOR day of data to predict against, and there's currently only {n_days} "
            "day(s) total. Re-run once the collector has accumulated more days."
        )
        return

    metrics = score(evaluated)
    print(
        f"\nSeasonal-naive baseline (each time-of-day bucket predicted as its trailing "
        f"{TRAILING_DAYS}-day average), walk-forward evaluated day by day:"
    )
    for k, v in metrics.items():
        print(f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}")


if __name__ == "__main__":
    main()
