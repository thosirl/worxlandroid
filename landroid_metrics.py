#!/usr/bin/env python3
"""
landroid_metrics.py

Collects Worx Landroid mower metrics and ships them to Grafana Cloud
via Prometheus remote write (same method as solis_metrics.py).

Run every 10 minutes via cron:
  */10 * * * * /path/to/venv/bin/python /path/to/landroid_metrics.py

Requires system libsnappy:
  Amazon Linux: sudo dnf install snappy-devel   (or yum install snappy-devel)

Required environment variables:
    WORX_EMAIL               Worx account email
    WORX_PASSWORD            Worx account password
    GRAFANA_REMOTE_WRITE_URL Grafana Cloud Prometheus remote write URL
    GRAFANA_USERNAME         Grafana metrics instance ID (numeric)
    GRAFANA_API_KEY          Grafana Cloud access policy token

Optional:
    WORX_BRAND               worx | kress | landxcape  (default: worx)
    LOG_LEVEL                DEBUG | INFO | WARNING     (default: INFO)
"""

import logging
import os
import struct
import sys
import time

import requests
import snappy
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BRAND_CONFIG = {
    "worx": {
        "auth_url":  "https://id.worx.com/oauth/token",
        "api_url":   "https://api.worxlandroid.com/api/v2",
        "client_id": "150da4d2-bb44-433b-9429-3773adc70a2a",
    },
    "kress": {
        "auth_url":  "https://id.kress.com/oauth/token",
        "api_url":   "https://api.kress-robotik.com/api/v2",
        "client_id": "931d4bc4-3192-405a-be78-98e43486dc59",
    },
    "landxcape": {
        "auth_url":  "https://id.landxcape-services.com/oauth/token",
        "api_url":   "https://api.landxcape-services.com/api/v2",
        "client_id": "dec998a9-066f-433b-987a-f5fc54d3af7c",
    },
}

STATUS_NAMES = {
    0: "idle",           1: "home",            4: "following_wire",
    5: "searching_home", 7: "mowing",          8: "mowing",
    9: "trapped",        10: "blade_blocked",  30: "going_home",
    32: "cutting_edge",  33: "searching_area", 34: "paused",
    103: "searching_zone", 111: "exploring",
}

ERROR_NAMES = {
    0: "none",         1: "trapped",       2: "lifted",         3: "wire_missing",
    4: "outside_wire", 5: "rain_delay",    8: "blade_blocked",  9: "wheel_blocked",
    11: "upside_down", 12: "battery_low",  14: "charge_error",  16: "locked",
    17: "battery_temp_error", 100: "docking_error", 104: "excessive_slope",
}

GRAFANA_URL     = os.environ["GRAFANA_REMOTE_WRITE_URL"]
GRAFANA_USER    = os.environ["GRAFANA_USERNAME"]   # numeric Prometheus stack ID
GRAFANA_API_KEY = os.environ["GRAFANA_API_KEY"]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Worx API
# ---------------------------------------------------------------------------


def authenticate(brand_cfg: dict, email: str, password: str) -> str:
    resp = requests.post(
        brand_cfg["auth_url"],
        json={
            "grant_type": "password",
            "client_id":  brand_cfg["client_id"],
            "username":   email,
            "password":   password,
            "scope":      "*",
        },
        timeout=20,
    )
    resp.raise_for_status()
    log.info("Authenticated successfully")
    return resp.json()["access_token"]


def get_mowers(api_url: str, token: str) -> list:
    resp = requests.get(
        f"{api_url}/product-items",
        params={"status": "1"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    resp.raise_for_status()
    items = resp.json()
    log.info("Found %d mower(s)", len(items))
    return items


# ---------------------------------------------------------------------------
# Metric extraction
# ---------------------------------------------------------------------------


def _to_float(v) -> "float | None":
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def extract_metrics(mower: dict) -> "list[dict]":
    serial   = mower.get("serial_number", "unknown")
    raw_name = mower.get("name", serial)
    name_tag = "".join(c if c.isalnum() or c in "-_." else "_" for c in raw_name)

    last_status = mower.get("last_status")
    if not last_status:
        log.warning("Mower %s has no last_status payload — skipping", serial)
        return []

    payload = last_status.get("payload", {})
    log.debug("Mower %s raw payload: %s", serial, payload)

    dat  = payload.get("dat", {})
    bt   = dat.get("bt", {})
    st   = dat.get("st", {})
    rain = dat.get("rain", {})

    ts      = int(time.time() * 1000)
    labels  = {"serial": serial, "name": name_tag}
    out     = []

    def add(name: str, raw, extra: "dict | None" = None):
        v = _to_float(raw)
        if v is not None:
            out.append({
                "name":      name,
                "value":     v,
                "labels":    {**labels, **(extra or {})},
                "timestamp": ts,
            })

    # Battery
    add("landroid_battery_percent",     bt.get("p"))
    add("landroid_battery_voltage",     bt.get("v"))
    add("landroid_battery_temperature", bt.get("t"))
    add("landroid_battery_charging",    bt.get("c"))   # -1=unknown, 0=not charging, 1=charging, 2=error
    add("landroid_battery_cycles",      bt.get("nr"))

    # Status
    status_code = dat.get("ls")
    error_code  = dat.get("le")
    add("landroid_status_code",  status_code)
    add("landroid_error_code",   error_code)
    add("landroid_wifi_rssi",    dat.get("rsi"))
    add("landroid_locked",       int(bool(dat.get("lk", 0))))

    # Cumulative statistics
    add("landroid_distance_meters",  st.get("d"))
    add("landroid_worktime_minutes", st.get("wt"))
    add("landroid_blade_minutes",    st.get("b"))

    # Rain
    add("landroid_rain_active",             int(str(rain.get("s", "0")) == "1"))
    add("landroid_rain_countdown_seconds",  rain.get("cnt"))

    log.info(
        "Mower %-20s serial=%-15s status=%-16s error=%-20s battery=%s%%",
        name_tag, serial,
        STATUS_NAMES.get(status_code, str(status_code)),
        ERROR_NAMES.get(error_code,   str(error_code)),
        bt.get("p"),
    )
    return out


# ---------------------------------------------------------------------------
# Prometheus remote write  (same implementation as solis_metrics.py)
# ---------------------------------------------------------------------------


def _varint(value: int) -> bytes:
    out = []
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            break
    return bytes(out)


def _ldelim(field: int, data: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(data)) + data


def _pb_string(field: int, s: str) -> bytes:
    return _ldelim(field, s.encode())


def _encode_label(name: str, value: str) -> bytes:
    return _pb_string(1, name) + _pb_string(2, value)


def _encode_sample(value: float, ts_ms: int) -> bytes:
    return (
        _varint((1 << 3) | 1) + struct.pack("<d", value) +
        _varint((2 << 3) | 0) + _varint(ts_ms)
    )


def _encode_timeseries(name: str, labels: dict, value: float, ts_ms: int) -> bytes:
    all_labels = {"__name__": name, **{k: str(v) for k, v in labels.items()}}
    label_bytes = b"".join(
        _ldelim(1, _encode_label(k, v))
        for k, v in sorted(all_labels.items())
    )
    sample_bytes = _ldelim(2, _encode_sample(value, ts_ms))
    return label_bytes + sample_bytes


def _build_write_request(metrics: "list[dict]") -> bytes:
    return b"".join(
        _ldelim(1, _encode_timeseries(m["name"], m["labels"], m["value"], m["timestamp"]))
        for m in metrics
    )


def push_to_grafana(metrics: "list[dict]") -> None:
    payload    = _build_write_request(metrics)
    compressed = snappy.compress(payload)
    resp = requests.post(
        GRAFANA_URL,
        data=compressed,
        headers={
            "Content-Encoding": "snappy",
            "Content-Type":     "application/x-protobuf",
            "X-Prometheus-Remote-Write-Version": "0.1.0",
        },
        auth=(GRAFANA_USER, GRAFANA_API_KEY),
        timeout=30,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    log.info("Worx Landroid → Grafana Cloud")

    email    = os.environ.get("WORX_EMAIL", "").strip()
    password = os.environ.get("WORX_PASSWORD", "").strip()
    brand    = os.environ.get("WORX_BRAND", "worx").lower()

    if not email or not password:
        log.error("WORX_EMAIL and WORX_PASSWORD are required")
        sys.exit(1)

    if brand not in BRAND_CONFIG:
        log.error("Unknown WORX_BRAND '%s'. Valid: %s", brand, list(BRAND_CONFIG))
        sys.exit(1)

    brand_cfg = BRAND_CONFIG[brand]

    try:
        token  = authenticate(brand_cfg, email, password)
        mowers = get_mowers(brand_cfg["api_url"], token)
    except Exception as exc:
        log.error("Failed to fetch mowers: %s", exc)
        sys.exit(1)

    if not mowers:
        log.warning("No mowers found")
        sys.exit(0)

    all_metrics = []  # type: list
    for mower in mowers:
        serial = mower.get("serial_number", "?")
        try:
            metrics = extract_metrics(mower)
            all_metrics.extend(metrics)
            log.info("Mower %s: %d metrics collected", serial, len(metrics))
        except Exception as exc:
            log.error("Mower %s: %s", serial, exc)

    if not all_metrics:
        log.error("No metrics collected")
        sys.exit(1)

    try:
        push_to_grafana(all_metrics)
        log.info("Pushed %d metrics to Grafana Cloud", len(all_metrics))
    except Exception as exc:
        log.error("Push failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
