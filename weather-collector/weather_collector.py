"""
Polls the Open-Meteo forecast API for this site's coordinates and writes
hourly weather data to InfluxDB, for pairing with PV history in forecasting
work (see forecasting/).

Open-Meteo's forecast endpoint returns both recent past (via `past_days`,
model-reanalyzed - the closest thing to "actuals" this free API offers) and
future forecast (via `forecast_days`) in one response, so a single poll
covers both. Points are written keyed by their own timestamp, not poll time,
so re-polling naturally refines past hours toward reanalysis and future
hours toward a shorter, more accurate forecast horizon - last write wins per
timestamp in InfluxDB.

No API key needed - Open-Meteo's non-commercial tier is unauthenticated.
"""
import logging
import os
import time

import requests
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("weather-collector")

WEATHER_LATITUDE = float(os.environ["WEATHER_LATITUDE"])
WEATHER_LONGITUDE = float(os.environ["WEATHER_LONGITUDE"])
# Open-Meteo's forecast endpoint caps past_days at 92 and forecast_days at 16.
WEATHER_PAST_DAYS = int(os.environ.get("WEATHER_PAST_DAYS", 92))
WEATHER_FORECAST_DAYS = int(os.environ.get("WEATHER_FORECAST_DAYS", 16))
WEATHER_POLL_SECONDS = int(os.environ.get("WEATHER_POLL_SECONDS", 3600))

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_FIELDS = ["cloud_cover", "shortwave_radiation", "direct_radiation", "diffuse_radiation", "temperature_2m"]
FIELD_NAMES = {
    "cloud_cover": "cloud_cover_pct",
    "shortwave_radiation": "shortwave_radiation_wm2",
    "direct_radiation": "direct_radiation_wm2",
    "diffuse_radiation": "diffuse_radiation_wm2",
    "temperature_2m": "temperature_c",
}

INFLUXDB_URL = os.environ.get("INFLUXDB_URL", "http://influxdb:8086")
INFLUXDB_TOKEN = os.environ["INFLUXDB_TOKEN"]
INFLUXDB_ORG = os.environ["INFLUXDB_ORG"]
INFLUXDB_BUCKET = os.environ["INFLUXDB_BUCKET"]


def fetch_hourly() -> dict:
    params = {
        "latitude": WEATHER_LATITUDE,
        "longitude": WEATHER_LONGITUDE,
        "hourly": ",".join(HOURLY_FIELDS),
        "timezone": "UTC",
        "past_days": WEATHER_PAST_DAYS,
        "forecast_days": WEATHER_FORECAST_DAYS,
    }
    response = requests.get(OPEN_METEO_URL, params=params, timeout=30)
    response.raise_for_status()
    return response.json()["hourly"]


def to_points(hourly: dict) -> list:
    points = []
    times = hourly["time"]
    for i, iso_time in enumerate(times):
        point = Point("weather").tag("source", "open-meteo")
        for field in HOURLY_FIELDS:
            value = hourly[field][i]
            if value is None:
                continue
            point = point.field(FIELD_NAMES[field], float(value))
        # Open-Meteo gives naive "YYYY-MM-DDTHH:MM" already in UTC (timezone=UTC above).
        point = point.time(f"{iso_time}:00Z", WritePrecision.S)
        points.append(point)
    return points


def main() -> None:
    influx = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    write_api = influx.write_api(write_options=SYNCHRONOUS)

    while True:
        started_at = time.monotonic()
        try:
            hourly = fetch_hourly()
            points = to_points(hourly)
            write_api.write(bucket=INFLUXDB_BUCKET, record=points)
            log.info("wrote %d hourly weather points (%s to %s)", len(points), hourly["time"][0], hourly["time"][-1])
        except Exception:
            log.exception("weather poll failed, will retry next tick")

        elapsed = time.monotonic() - started_at
        time.sleep(max(0.0, WEATHER_POLL_SECONDS - elapsed))


if __name__ == "__main__":
    main()
