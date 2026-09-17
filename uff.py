#!/usr/bin/env python3
"""
uff.py - UK Fuel Finder Data Collector & Dumper
===================================================

A streamlined command-line tool for pulling UK fuel prices from the
Government Fuel Finder API. Integrates smart caching, auto-retries, rate-limiting,
and data error cleaning, and outputs `prices_YYYY-MM-DD.json` for consumption by
the True Cost Fuel Finder web application.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import requests

# --------------------- Debug & Defaults ---------------------

DEBUG = False


def debug_print(msg: str) -> None:
    """Print a timestamped diagnostic message to stderr if DEBUG is enabled."""
    if not DEBUG:
        return
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr)


BASE_URL = "https://www.fuel-finder.service.gov.uk"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

DEFAULTS = {
    "config_dir": "/config/.storage/uk_fuel_finder",
    "stations_baseline_days": 7,
    "stations_incremental_hours": 12,
    "prices_baseline_days": 2,
    "prices_incremental_hours": 1.0,
    "http_timeout": 60,
    "http_retries": 6,
    "http_backoff_base": 1.8,
    "http_backoff_jitter": 0.7,
    "batch_sleep_seconds": 4.0,
    "incremental_safety_minutes": 45,
    "prices_min_coverage_ratio": 0.5,
}


# --------------------- Time Helpers ---------------------


def utc_now() -> datetime:
    """Return current UTC datetime as a timezone-aware object."""
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    """Format a datetime as a UTC ISO 8601 string ending in 'Z'."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_dt_maybe(s: str | None) -> datetime | None:
    """Parse an ISO datetime string, returning None on failure."""
    if not s:
        return None
    s2 = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s2)
    except Exception:
        return None


def parse_price_dt(s: str | None) -> datetime | None:
    """Parse an API price timestamp, treating naive datetimes as UTC."""
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


# --------------------- Data Cleaning Helpers ---------------------


def format_address_line(location: dict[str, Any], station_name: str = "") -> str:
    """Extract a clean, concise location string (e.g. 'High Street, York') from raw API data."""
    NOISE_WORDS = {
        "GARAGE",
        "SERVICE",
        "STATION",
        "SERVICES",
        "FILLING",
        "PETROL",
        "FORECOURT",
    }
    ROAD_WORDS = {
        "ROAD",
        "LANE",
        "STREET",
        "AVENUE",
        "DRIVE",
        "WAY",
        "CLOSE",
        "GROVE",
        "PLACE",
        "COURT",
        "PARK",
        "HILL",
        "GREEN",
        "BOULEVARD",
        "SQUARE",
        "TERRACE",
        "CRESCENT",
        "HIGHWAY",
        "ROW",
        "VIEW",
        "WALK",
        "CIRCLE",
        "MOUNT",
        "STRAND",
        "WHARF",
        "QUAY",
        "PIKE",
        "GATE",
        "YARD",
        "ESTATE",
        "PARKWAY",
        "MEWS",
        "ALLEY",
        "MALL",
        "RING",
    }

    def is_road_number(s: str) -> bool:
        s_clean = s.strip().upper()
        if len(s_clean) < 2 or len(s_clean) > 6:
            return False
        return s_clean[0] in "ABM" and s_clean[1:].isdigit()

    def is_postcode(s: str) -> bool:
        s_clean = s.strip().replace(" ", "").upper()
        if not (5 <= len(s_clean) <= 8):
            return False
        return s_clean[-1].isalpha() and s_clean[-2].isalpha() and s_clean[-3].isdigit()

    def is_noise(s: str) -> bool:
        words = set(s.strip().upper().split())
        return bool(words) and words.issubset(
            NOISE_WORDS | {"&", "AND", "THE", "LTD", "LIMITED", "PLC", "LLP"}
        )

    parts = []
    addr1 = (location.get("address_line_1") or "").strip()

    if addr1.count(",") >= 2:
        chunks = [c.strip() for c in addr1.split(",") if c.strip()]
        while chunks:
            chunk_words = set(chunks[0].upper().split())
            has_noise = bool(
                chunk_words
                & (NOISE_WORDS | {"LTD", "LIMITED", "PLC", "LLP", "CO", "COMPANY"})
            )
            has_road = bool(chunk_words & ROAD_WORDS)
            if has_noise and not has_road:
                chunks = chunks[1:]
            else:
                break

        if station_name:
            name_upper = station_name.upper().strip()
            sig_words = [
                w for w in name_upper.split() if w not in NOISE_WORDS and len(w) > 2
            ]
            for i, chunk in enumerate(chunks):
                chunk_upper = chunk.upper()
                chunk_words = set(chunk_upper.split())
                if chunk_words & ROAD_WORDS:
                    break
                full_match = name_upper in chunk_upper
                word_match = sig_words and sum(
                    1 for w in sig_words if w in chunk_upper
                ) >= max(1, len(sig_words) // 2)
                if full_match or word_match:
                    chunks = chunks[i + 1 :]
                    break

        for chunk in chunks:
            if is_postcode(chunk) or is_road_number(chunk) or is_noise(chunk):
                continue
            parts.append(chunk)
            if len(parts) >= 2:
                break
    else:
        for field in ["address_line_1", "address_line_2", "city"]:
            val = (location.get(field) or "").strip()
            if val and val.lower() != "null":
                if not is_road_number(val) and not is_noise(val):
                    parts.append(val)
                    if len(parts) >= 2:
                        break

    return ", ".join(parts[:2]) if parts else ""


def _price_fix_to_pence(price_raw: Any) -> tuple[Any, int]:
    """Correct fuel prices submitted in pounds (e.g. 1.45) rather than pence (145.0)."""
    if price_raw in (None, ""):
        return price_raw, 0
    try:
        d = Decimal(str(price_raw))
    except (InvalidOperation, ValueError):
        return price_raw, 0

    if d < Decimal("2"):
        d = (d * Decimal("100")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        return float(d), 1
    try:
        return float(d), 0
    except Exception:
        return price_raw, 0


# --------------------- Filesystem & Locking ---------------------


@dataclass
class Paths:
    work_dir: Path
    state_file: Path
    token_file: Path
    lock_file: Path
    config_file: Path


def make_paths(work_dir: str) -> Paths:
    d = Path(work_dir)
    return Paths(
        work_dir=d,
        state_file=d / "state.json",
        token_file=d / "token.json",
        lock_file=d / "state.lock",
        config_file=d / "config.json",
    )


class FileLock:
    """Exclusive advisory file lock using fcntl for multi-process safety."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self.fd = None

    def __enter__(self) -> FileLock:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = open(self.lock_path, "w")
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(
        self, exc_type: type | None, exc: BaseException | None, tb: Any | None
    ) -> None:
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            try:
                self.fd.close()
            except Exception:
                pass


# --------------------- HTTP & Retries ---------------------


class AuthError(Exception):
    def __init__(self, message: str, response: requests.Response | None = None) -> None:
        super().__init__(message)
        self.response = response


def request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: int = DEFAULTS["http_timeout"],
    retries: int = DEFAULTS["http_retries"],
    backoff_base: float = DEFAULTS["http_backoff_base"],
    backoff_jitter: float = DEFAULTS["http_backoff_jitter"],
) -> requests.Response:
    """Execute HTTP request with exponential backoff retry for transient failures."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=timeout,
            )
            if resp.status_code in (401, 403):
                raise AuthError(f"Auth HTTP {resp.status_code}", response=resp)
            if resp.status_code == 404:
                raise requests.HTTPError("HTTP 404 Not Found", response=resp)
            if resp.status_code in RETRYABLE_STATUS:
                raise requests.HTTPError(
                    f"Retryable HTTP {resp.status_code}", response=resp
                )
            resp.raise_for_status()
            return resp
        except Exception as e:
            if isinstance(e, AuthError):
                raise
            if (
                isinstance(e, requests.HTTPError)
                and getattr(getattr(e, "response", None), "status_code", None) == 404
            ):
                raise
            last_exc = e
            if attempt == retries - 1:
                raise
            status = getattr(getattr(e, "response", None), "status_code", None)
            debug_print(
                f"HTTP retry {attempt + 1}/{retries - 1} for {method} {url} (status={status}): {e}"
            )
            sleep_s = (backoff_base**attempt) + (
                backoff_jitter * (0.5 + (attempt % 3) / 3)
            )
            time.sleep(min(30.0, sleep_s))
    if last_exc:
        raise last_exc
    raise RuntimeError("request_with_retry: unreachable")


# --------------------- JSON & Config Helpers ---------------------


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_config_from_dir(work_dir: str) -> dict[str, Any]:
    cfg_path = Path(work_dir) / "config.json"
    if not cfg_path.exists():
        return {}
    try:
        v = json.loads(cfg_path.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


# --------------------- OAuth Token Manager ---------------------


def get_access_token(
    paths: Paths, client_id: str, client_secret: str, *, force_refresh: bool = False
) -> str:
    """Obtain a valid OAuth access token, handling cached tokens and refresh tokens."""
    token = load_json(paths.token_file) or {}
    access = token.get("access_token")
    expires_at = parse_dt_maybe(token.get("expires_at"))
    refresh = token.get("refresh_token")

    if (
        (not force_refresh)
        and access
        and expires_at
        and utc_now() < (expires_at - timedelta(seconds=30))
    ):
        debug_print("OAuth: using cached access_token")
        return access

    if refresh:
        try:
            debug_print("OAuth: refreshing access_token using refresh_token")
            url = f"{BASE_URL}/api/v1/oauth/regenerate_access_token"
            payload = {"client_id": client_id, "refresh_token": refresh}
            resp = request_with_retry(
                "POST",
                url,
                headers={"accept": "application/json"},
                json_body=payload,
            )
            data = resp.json()
            token_data = data.get("data", data)
            access_token = token_data["access_token"]
            expires_in = int(token_data.get("expires_in", 3600))
            new_token = {
                "access_token": access_token,
                "refresh_token": refresh,
                "expires_at": iso_utc(utc_now() + timedelta(seconds=expires_in)),
                "token_type": token_data.get("token_type", "Bearer"),
                "updated_at": iso_utc(utc_now()),
            }
            save_json(paths.token_file, new_token)
            return access_token
        except Exception as e:
            debug_print(
                f"OAuth: refresh failed ({e}), falling back to full token generation"
            )

    debug_print("OAuth: generating new access_token")
    url = f"{BASE_URL}/api/v1/oauth/generate_access_token"
    payload = {"client_id": client_id, "client_secret": client_secret}
    resp = request_with_retry(
        "POST",
        url,
        headers={"accept": "application/json"},
        json_body=payload,
    )
    data = resp.json()
    token_data = data.get("data", data)
    access_token = token_data["access_token"]
    expires_in = int(token_data.get("expires_in", 3600))
    refresh_token = token_data.get("refresh_token")
    new_token = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": iso_utc(utc_now() + timedelta(seconds=expires_in)),
        "token_type": token_data.get("token_type", "Bearer"),
        "updated_at": iso_utc(utc_now()),
    }
    save_json(paths.token_file, new_token)
    return access_token


# --------------------- API Batch Fetcher ---------------------


def fetch_all_batches(
    token: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    batch_sleep: float = DEFAULTS["batch_sleep_seconds"],
    refresh_token_fn: Any | None = None,
) -> list[dict[str, Any]]:
    """Fetch all pages from a paginated API endpoint, returning combined rows."""
    t0 = time.time()
    debug_print(f"API fetch start: {path} params={params}")
    headers = {"accept": "application/json", "authorization": f"Bearer {token}"}
    out: list[dict[str, Any]] = []
    batch = 1
    while True:
        qp = dict(params or {})
        qp["batch-number"] = str(batch)
        url = f"{BASE_URL}{path}"
        try:
            resp = request_with_retry("GET", url, headers=headers, params=qp)
        except AuthError as e:
            if refresh_token_fn is None:
                raise
            debug_print(
                f"Auth failed ({getattr(e.response, 'status_code', None)}). "
                f"Forcing token refresh and retrying batch {batch}."
            )
            token = refresh_token_fn()
            headers = {"accept": "application/json", "authorization": f"Bearer {token}"}
            resp = request_with_retry("GET", url, headers=headers, params=qp)
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", None) == 404:
                debug_print(f"  batch {batch}: 404 received — end of results")
                break
            raise

        response_body = resp.json()
        if isinstance(response_body, dict) and "data" in response_body:
            data = response_body.get("data", [])
        elif isinstance(response_body, list):
            data = response_body
        else:
            data = []

        if not isinstance(data, list):
            data = []
        out.extend(data)
        debug_print(f"  batch {batch}: {len(data)} rows (total {len(out)})")
        if len(data) < 500:
            break
        batch += 1
        time.sleep(batch_sleep)
    debug_print(f"API fetch done: {path} rows={len(out)} in {time.time() - t0:.2f}s")
    return out


# --------------------- Cache State Model ---------------------


def empty_state() -> dict[str, Any]:
    return {
        "stations": {},
        "prices": {},
        "meta": {
            "stations_baseline_at": None,
            "stations_last_incremental_at": None,
            "prices_baseline_at": None,
            "prices_last_incremental_at": None,
            "prices_max_price_last_updated": None,
            "price_fix_count_total": 0,
            "price_fix_count_last_run": 0,
            "price_fix_last_run_at": None,
            "created_at": iso_utc(utc_now()),
            "updated_at": iso_utc(utc_now()),
        },
    }


def load_state(paths: Paths) -> dict[str, Any]:
    data = load_json(paths.state_file)
    if (
        isinstance(data, dict)
        and "stations" in data
        and "prices" in data
        and "meta" in data
    ):
        meta = data.get("meta", {}) or {}
        meta.setdefault("price_fix_count_total", 0)
        meta.setdefault("price_fix_count_last_run", 0)
        meta.setdefault("price_fix_last_run_at", None)
        data["meta"] = meta
        return data
    return empty_state()


def save_state(paths: Paths, state: dict[str, Any]) -> None:
    state["meta"]["updated_at"] = iso_utc(utc_now())
    save_json(paths.state_file, state)


def invalidate_cache(paths: Paths) -> None:
    paths.work_dir.mkdir(parents=True, exist_ok=True)
    if paths.state_file.exists():
        paths.state_file.unlink()
    save_state(paths, empty_state())


def cache_stats(state: dict[str, Any]) -> dict[str, Any]:
    stations = state.get("stations", {}) or {}
    prices = state.get("prices", {}) or {}
    fuel_types: set[str] = set()

    for _, p in prices.items():
        if isinstance(p, dict):
            for ft in p.keys():
                fuel_types.add(str(ft))

    meta = state.get("meta", {}) or {}
    return {
        "stations_count": len(stations),
        "prices_station_count": len(prices),
        "fuel_types": sorted(fuel_types),
        "stations_baseline_at": meta.get("stations_baseline_at"),
        "stations_last_incremental_at": meta.get("stations_last_incremental_at"),
        "prices_baseline_at": meta.get("prices_baseline_at"),
        "prices_last_incremental_at": meta.get("prices_last_incremental_at"),
        "prices_max_price_last_updated": meta.get("prices_max_price_last_updated"),
        "price_fix_count_total": int(meta.get("price_fix_count_total") or 0),
        "price_fix_count_last_run": int(meta.get("price_fix_count_last_run") or 0),
        "price_fix_last_run_at": meta.get("price_fix_last_run_at"),
        "state_file_bytes": None,
        "token_file_bytes": None,
    }


# --------------------- Transforms & Merging ---------------------


def stations_to_dict(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for it in items:
        sid = it.get("node_id")
        if not sid:
            continue
        out[str(sid)] = it
    return out


def merge_station_dict(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    base = dict(base or {})
    for sid, obj in (updates or {}).items():
        base[str(sid)] = obj
    return base


def prices_to_dict(
    items: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], str | None, int]:
    out: dict[str, dict[str, Any]] = {}
    max_dt: datetime | None = None
    now = utc_now()
    fix_count = 0

    for it in items:
        sid = it.get("node_id")
        if not sid:
            continue
        sid = str(sid)
        fp = it.get("fuel_prices", [])
        if not isinstance(fp, list):
            fp = []
        per_station: dict[str, Any] = out.get(sid, {})
        for row in fp:
            if not isinstance(row, dict):
                continue
            ft = row.get("fuel_type")
            if not ft:
                continue
            ft = str(ft)

            price_raw = row.get("price")
            plu = row.get("price_last_updated")
            pcet = row.get("price_change_effective_timestamp")

            price, fixed = _price_fix_to_pence(price_raw)
            if fixed:
                fix_count += 1
                debug_print(
                    f"Fixed price error: {price_raw} -> {price} (assumed *100) "
                    f"for {ft} at station {sid[:8]}..."
                )

            per_station[ft] = {
                "price": price,
                "price_last_updated": plu,
                "price_change_effective_timestamp": pcet,
            }

            dt_plu = parse_price_dt(plu)
            dt_pcet = parse_price_dt(pcet)
            candidates = [t for t in (dt_plu, dt_pcet) if t is not None]
            row_dt = min(max(candidates), now) if candidates else None

            if row_dt and (max_dt is None or row_dt > max_dt):
                max_dt = row_dt
        out[sid] = per_station

    return out, (iso_utc(max_dt) if max_dt else None), fix_count


def merge_price_dict(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    base = dict(base or {})
    for sid, fuels in (updates or {}).items():
        sid = str(sid)
        if sid not in base or not isinstance(base.get(sid), dict):
            base[sid] = {}
        for ft, row in (fuels or {}).items():
            base[sid][str(ft)] = row
    return base


# --------------------- Refresh Policy & Cache Assurance ---------------------


def needs_stations_baseline(state: dict[str, Any], days: int) -> bool:
    dt = parse_dt_maybe(state.get("meta", {}).get("stations_baseline_at"))
    if dt is None:
        return True
    return utc_now() >= (dt + timedelta(days=days))


def needs_prices_baseline(state: dict[str, Any], days: int) -> bool:
    dt = parse_dt_maybe(state.get("meta", {}).get("prices_baseline_at"))
    if dt is None:
        return True
    return utc_now() >= (dt + timedelta(days=days))


def needs_stations_incremental(state: dict[str, Any], hours: int) -> bool:
    dt = parse_dt_maybe(state.get("meta", {}).get("stations_last_incremental_at"))
    if dt is None:
        return True
    return utc_now() >= (dt + timedelta(hours=hours))


def needs_prices_incremental(state: dict[str, Any], hours: float) -> bool:
    dt = parse_dt_maybe(state.get("meta", {}).get("prices_last_incremental_at"))
    if dt is None:
        return True
    return utc_now() >= (dt + timedelta(hours=hours))


def ensure_cache(
    *,
    paths: Paths,
    client_id: str,
    client_secret: str,
    full_refresh: bool,
    prices_refresh: bool = False,
    stations_baseline_days: int,
    stations_incremental_hours: int,
    prices_baseline_days: int,
    prices_incremental_hours: float,
    batch_sleep_seconds: float = DEFAULTS["batch_sleep_seconds"],
    prices_min_coverage_ratio: float = DEFAULTS["prices_min_coverage_ratio"],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ensure local cache is up to date via baseline or incremental API pulls."""
    with FileLock(paths.lock_file):
        if full_refresh:
            debug_print("Cache: full refresh requested; invalidating state")
            invalidate_cache(paths)

        state = load_state(paths)
        debug_print(
            f"Cache: loaded stations={len(state.get('stations', {}) or {})} "
            f"prices={len(state.get('prices', {}) or {})}"
        )

        token = get_access_token(paths, client_id, client_secret)

        def force_refresh_access_token() -> str:
            return get_access_token(paths, client_id, client_secret, force_refresh=True)

        state["meta"]["price_fix_count_last_run"] = 0
        did_stations_baseline = False

        if needs_stations_baseline(state, stations_baseline_days):
            baseline_at = state.get("meta", {}).get("stations_baseline_at")
            debug_print(
                f"Stations: baseline refresh required (last_baseline={baseline_at or 'never'})"
            )
            items = fetch_all_batches(
                token,
                "/api/v1/pfs",
                refresh_token_fn=force_refresh_access_token,
                batch_sleep=batch_sleep_seconds,
            )
            state["stations"] = stations_to_dict(items)
            debug_print(
                f"Stations: baseline loaded {len(items)} rows; stations now {len(state['stations'])}"
            )
            now = iso_utc(utc_now())
            state["meta"]["stations_baseline_at"] = now
            state["meta"]["stations_last_incremental_at"] = now
            did_stations_baseline = True

        elif needs_stations_incremental(state, stations_incremental_hours):
            last = (
                parse_dt_maybe(state["meta"].get("stations_last_incremental_at"))
                or utc_now()
            )
            safe = last - timedelta(minutes=DEFAULTS["incremental_safety_minutes"])
            params = {"effective-start-timestamp": safe.strftime("%Y-%m-%d %H:%M:%S")}
            debug_print(
                f"Stations: incremental using last={state['meta'].get('stations_last_incremental_at')} safe={safe.isoformat()}"
            )

            items = fetch_all_batches(
                token,
                "/api/v1/pfs",
                params=params,
                refresh_token_fn=force_refresh_access_token,
                batch_sleep=batch_sleep_seconds,
            )

            if items:
                upd = stations_to_dict(items)
                state["stations"] = merge_station_dict(state["stations"], upd)
                debug_print(f"Stations: merged {len(upd)} updates")

            state["meta"]["stations_last_incremental_at"] = iso_utc(utc_now())
        else:
            debug_print("Stations: no refresh needed")

        if (
            did_stations_baseline
            or prices_refresh
            or needs_prices_baseline(state, prices_baseline_days)
        ):
            baseline_at = state.get("meta", {}).get("prices_baseline_at")
            debug_print(
                f"Prices: baseline refresh required (last_baseline={baseline_at or 'never'})"
            )
            items = fetch_all_batches(
                token,
                "/api/v1/pfs/fuel-prices",
                refresh_token_fn=force_refresh_access_token,
                batch_sleep=batch_sleep_seconds,
            )
            prices, max_plu, fix_count = prices_to_dict(items)

            station_count = len(state.get("stations") or {})
            min_expected = int(station_count * prices_min_coverage_ratio)
            if station_count > 0 and len(prices) < min_expected:
                debug_print(
                    f"Prices: baseline REJECTED — only {len(prices)} stations priced vs {station_count} known. Keeping existing cache."
                )
            else:
                state["prices"] = prices
                debug_print(
                    f"Prices: baseline accepted {len(items)} rows; prices now {len(state['prices'])}"
                )
                now = iso_utc(utc_now())
                state["meta"]["prices_baseline_at"] = now
                state["meta"]["prices_last_incremental_at"] = max_plu or now
                state["meta"]["prices_max_price_last_updated"] = max_plu

                state["meta"]["price_fix_count_last_run"] = int(fix_count)
                state["meta"]["price_fix_count_total"] = int(
                    state["meta"].get("price_fix_count_total") or 0
                ) + int(fix_count)
                state["meta"]["price_fix_last_run_at"] = iso_utc(utc_now())

                if did_stations_baseline:
                    station_ids = set((state.get("stations") or {}).keys())
                    price_ids = set((state.get("prices") or {}).keys())
                    orph_prices = sorted(price_ids - station_ids)
                    if orph_prices:
                        for sid in orph_prices:
                            del state["prices"][sid]
                        debug_print(
                            f"Cache: pruned {len(orph_prices)} orphan price entries"
                        )

        elif needs_prices_incremental(state, prices_incremental_hours):
            debug_print(f"Prices: incremental refresh (>{prices_incremental_hours}h)")
            last = (
                parse_dt_maybe(state["meta"].get("prices_last_incremental_at"))
                or utc_now()
            )
            safe = last - timedelta(minutes=DEFAULTS["incremental_safety_minutes"])
            params = {"effective-start-timestamp": safe.strftime("%Y-%m-%d %H:%M:%S")}
            items = fetch_all_batches(
                token,
                "/api/v1/pfs/fuel-prices",
                params=params,
                refresh_token_fn=force_refresh_access_token,
                batch_sleep=batch_sleep_seconds,
            )
            if items:
                upd, max_plu, fix_count = prices_to_dict(items)
                state["prices"] = merge_price_dict(state["prices"], upd)
                if max_plu:
                    state["meta"]["prices_last_incremental_at"] = max_plu
                    state["meta"]["prices_max_price_last_updated"] = max_plu
                else:
                    state["meta"]["prices_last_incremental_at"] = iso_utc(utc_now())

                state["meta"]["price_fix_count_last_run"] = int(fix_count)
                state["meta"]["price_fix_count_total"] = int(
                    state["meta"].get("price_fix_count_total") or 0
                ) + int(fix_count)
                state["meta"]["price_fix_last_run_at"] = iso_utc(utc_now())
            else:
                state["meta"]["prices_last_incremental_at"] = iso_utc(utc_now())
                state["meta"]["price_fix_count_last_run"] = 0
        else:
            debug_print("Prices: no refresh needed")
            state["meta"]["price_fix_count_last_run"] = 0

        save_state(paths, state)

    stats = cache_stats(state)
    try:
        stats["state_file_bytes"] = (
            paths.state_file.stat().st_size if paths.state_file.exists() else 0
        )
        stats["token_file_bytes"] = (
            paths.token_file.stat().st_size if paths.token_file.exists() else 0
        )
    except Exception:
        pass
    return state, stats


# --------------------- CLI & Exporter ---------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Define CLI arguments."""
    p = argparse.ArgumentParser(
        description="UK Fuel Finder API Data Collector & Dumper"
    )

    p.add_argument(
        "--config-dir",
        default=None,
        help="Working directory for config and cache files",
    )
    p.add_argument(
        "--debug", action="store_true", help="Enable debug logging to stderr"
    )

    p.add_argument("--client-id", default=None, help="OAuth client ID")
    p.add_argument("--client-secret", default=None, help="OAuth client secret")

    p.add_argument(
        "--full-refresh",
        action="store_true",
        help="Invalidate cache and rebuild baselines from scratch",
    )
    p.add_argument(
        "--prices-refresh",
        action="store_true",
        help="Force prices baseline refresh only",
    )
    p.add_argument(
        "--dump",
        action="store_true",
        default=True,
        help="Write all stations with fresh prices to prices_<date>.json in work dir",
    )
    p.add_argument(
        "--max-price-age-days",
        type=float,
        default=None,
        metavar="DAYS",
        help="Exclude fuel prices older than DAYS days. Stations with no valid prices are omitted.",
    )

    return p.parse_args(argv)


def resolve_work_dir(args: argparse.Namespace) -> str:
    """Resolve working directory from CLI, environment, or default."""
    return args.config_dir or os.environ.get("UFF_CONFIG_DIR") or DEFAULTS["config_dir"]


def dump_prices_json(
    state: dict[str, Any],
    stats: dict[str, Any],
    paths: Paths,
    max_price_age_days: float | None,
) -> tuple[Path, int]:
    """Write all stations (filtered by max_price_age_days if specified) to prices_YYYY-MM-DD.json."""
    cutoff_dt: datetime | None = (
        utc_now() - timedelta(days=max_price_age_days)
        if max_price_age_days is not None
        else None
    )
    all_stations = state.get("stations") or {}
    all_prices = state.get("prices") or {}
    dump_stations: list[dict[str, Any]] = []

    for st in all_stations.values():
        sid = str(st.get("node_id"))
        prices_raw = all_prices.get(sid) or {}
        price_out: dict[str, Any] = {}
        for ftype, row in prices_raw.items():
            if not isinstance(row, dict):
                continue
            if cutoff_dt is not None:
                plu = parse_price_dt(row.get("price_last_updated"))
                if plu is None or plu < cutoff_dt:
                    continue
            price_out[ftype] = row
        if cutoff_dt is not None and not price_out:
            continue

        st_clean = dict(st)
        st_clean["address_display"] = format_address_line(
            st.get("location") or {}, st.get("trading_name") or ""
        )
        st_clean["fuel_prices"] = price_out
        dump_stations.append(st_clean)

    dump_out = {
        "state": "ok",
        "generated_at": iso_utc(utc_now()),
        "cache": stats,
        "stations": dump_stations,
    }
    date_str = utc_now().strftime("%Y-%m-%d")
    dump_file = paths.work_dir / f"prices_{date_str}.json"
    dump_file.write_text(json.dumps(dump_out, ensure_ascii=False, indent=2))
    return dump_file, len(dump_stations)


def main(argv: list[str] | None = None) -> int:
    """Entry point: refresh cache and export prices_YYYY-MM-DD.json."""
    args = parse_args(argv or sys.argv[1:])

    global DEBUG
    DEBUG = bool(getattr(args, "debug", False))

    work_dir = resolve_work_dir(args)
    paths = make_paths(work_dir)

    debug_print(f"work_dir={work_dir}")

    cfg = dict(DEFAULTS)
    cfg.update(load_config_from_dir(work_dir))

    client_id = (
        args.client_id or cfg.get("client_id") or os.environ.get("UFF_CLIENT_ID")
    )
    client_secret = (
        args.client_secret
        or cfg.get("client_secret")
        or os.environ.get("UFF_CLIENT_SECRET")
    )

    if not client_id or not client_secret:
        err_msg = {
            "state": "error",
            "error": "Missing client_id/client_secret (CLI args, config.json, or env UFF_CLIENT_ID/UFF_CLIENT_SECRET)",
        }
        print(json.dumps(err_msg, ensure_ascii=False))
        return 2

    try:
        state, stats = ensure_cache(
            paths=paths,
            client_id=client_id,
            client_secret=client_secret,
            full_refresh=args.full_refresh,
            prices_refresh=args.prices_refresh,
            stations_baseline_days=int(
                cfg.get("stations_baseline_days", DEFAULTS["stations_baseline_days"])
            ),
            stations_incremental_hours=int(
                cfg.get(
                    "stations_incremental_hours", DEFAULTS["stations_incremental_hours"]
                )
            ),
            prices_baseline_days=int(
                cfg.get("prices_baseline_days", DEFAULTS["prices_baseline_days"])
            ),
            prices_incremental_hours=float(
                cfg.get(
                    "prices_incremental_hours", DEFAULTS["prices_incremental_hours"]
                )
            ),
            batch_sleep_seconds=float(
                cfg.get("batch_sleep_seconds", DEFAULTS["batch_sleep_seconds"])
            ),
            prices_min_coverage_ratio=float(
                cfg.get(
                    "prices_min_coverage_ratio", DEFAULTS["prices_min_coverage_ratio"]
                )
            ),
        )
    except Exception as e:
        err_msg = {
            "state": "error",
            "error": f"Cache refresh failed: {e}",
            "generated_at": iso_utc(utc_now()),
        }
        print(json.dumps(err_msg, ensure_ascii=False))
        return 2

    dump_file, count = dump_prices_json(state, stats, paths, args.max_price_age_days)
    debug_print(f"Dump: wrote {count} stations to {dump_file}")
    print(
        json.dumps(
            {"state": "ok", "dump_file": str(dump_file), "station_count": count},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
