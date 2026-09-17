#!/usr/bin/env python3
"""
uff.py - UK Fuel Finder Data Collector & Dumper
===================================================

Streamlined command-line tool for pulling UK fuel prices from the
Government Fuel Finder API. Handles caching, auto-retries, rate-limiting,
and exports `prices_YYYY-MM-DD.json` for web application consumption.
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
from pathlib import Path
from typing import Any

import requests

DEBUG = False


def debug_print(msg: str) -> None:
    if DEBUG:
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


# --------------------- Datetime Helpers ---------------------


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_dt_maybe(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


# --------------------- Price Cleaning ---------------------


def _price_fix_to_pence(price_raw: Any) -> float | None:
    """Correct fuel prices submitted in pounds (e.g. 1.45) to pence (145.0)."""
    if price_raw in (None, ""):
        return None
    try:
        p = float(price_raw)
        if 0 < p < 5.0:
            return round(p * 100.0, 1)
        return round(p, 1)
    except (ValueError, TypeError):
        return None


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
        self.fd: Any = None

    def __enter__(self) -> FileLock:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = open(self.lock_path, "w")
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: type | None, exc: BaseException | None, tb: Any | None) -> None:
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            if self.fd:
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
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.request(
                method, url, headers=headers, params=params, json=json_body, timeout=timeout
            )
            if resp.status_code in (401, 403):
                raise AuthError(f"Auth HTTP {resp.status_code}", response=resp)
            if resp.status_code == 404:
                raise requests.HTTPError("HTTP 404 Not Found", response=resp)
            if resp.status_code in RETRYABLE_STATUS:
                raise requests.HTTPError(f"Retryable HTTP {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp
        except Exception as e:
            if isinstance(e, AuthError):
                raise
            if isinstance(e, requests.HTTPError) and getattr(getattr(e, "response", None), "status_code", None) == 404:
                raise
            last_exc = e
            if attempt == retries - 1:
                raise
            status = getattr(getattr(e, "response", None), "status_code", None)
            debug_print(f"HTTP retry {attempt + 1}/{retries - 1} for {method} {url} (status={status}): {e}")
            sleep_s = (backoff_base**attempt) + (backoff_jitter * (0.5 + (attempt % 3) / 3))
            time.sleep(min(30.0, sleep_s))
    if last_exc:
        raise last_exc
    raise RuntimeError("request_with_retry unreachable")


# --------------------- JSON Helpers ---------------------


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


# --------------------- OAuth Token Manager ---------------------


def get_access_token(
    paths: Paths, client_id: str, client_secret: str, *, force_refresh: bool = False
) -> str:
    """Obtain a valid OAuth access token, caching token until near expiry."""
    token = load_json(paths.token_file) or {}
    access = token.get("access_token")
    expires_at = parse_dt_maybe(token.get("expires_at"))

    if not force_refresh and access and expires_at and utc_now() < (expires_at - timedelta(seconds=30)):
        debug_print("OAuth: using cached access_token")
        return access

    debug_print("OAuth: generating new access_token")
    url = f"{BASE_URL}/api/v1/oauth/generate_access_token"
    payload = {"client_id": client_id, "client_secret": client_secret}
    resp = request_with_retry("POST", url, headers={"accept": "application/json"}, json_body=payload)
    data = resp.json()
    token_data = data.get("data", data)
    access_token = token_data["access_token"]
    expires_in = int(token_data.get("expires_in", 3600))
    save_json(
        paths.token_file,
        {
            "access_token": access_token,
            "refresh_token": token_data.get("refresh_token"),
            "expires_at": iso_utc(utc_now() + timedelta(seconds=expires_in)),
            "token_type": token_data.get("token_type", "Bearer"),
            "updated_at": iso_utc(utc_now()),
        },
    )
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
        except AuthError:
            if refresh_token_fn is None:
                raise
            debug_print(f"Auth failed. Forcing token refresh and retrying batch {batch}.")
            token = refresh_token_fn()
            headers = {"accept": "application/json", "authorization": f"Bearer {token}"}
            resp = request_with_retry("GET", url, headers=headers, params=qp)
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", None) == 404:
                debug_print(f"  batch {batch}: 404 received — end of results")
                break
            raise

        response_body = resp.json()
        data = response_body.get("data", []) if isinstance(response_body, dict) else response_body
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


# --------------------- Cache State & Transforms ---------------------


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
            "created_at": iso_utc(utc_now()),
            "updated_at": iso_utc(utc_now()),
        },
    }


def load_state(paths: Paths) -> dict[str, Any]:
    data = load_json(paths.state_file)
    if isinstance(data, dict) and "stations" in data and "prices" in data and "meta" in data:
        return data
    return empty_state()


def save_state(paths: Paths, state: dict[str, Any]) -> None:
    state["meta"]["updated_at"] = iso_utc(utc_now())
    save_json(paths.state_file, state)


def stations_to_dict(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(it["node_id"]): it for it in items if it.get("node_id")}


def prices_to_dict(items: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], str | None]:
    out: dict[str, dict[str, Any]] = {}
    max_dt: datetime | None = None
    now = utc_now()

    for it in items:
        sid = it.get("node_id")
        if not sid:
            continue
        sid = str(sid)
        fp = it.get("fuel_prices", [])
        if not isinstance(fp, list):
            continue
        per_station: dict[str, Any] = out.get(sid, {})
        for row in fp:
            if not isinstance(row, dict):
                continue
            ft = row.get("fuel_type")
            if not ft:
                continue
            ft = str(ft)
            price = _price_fix_to_pence(row.get("price"))
            plu = row.get("price_last_updated")
            pcet = row.get("price_change_effective_timestamp")

            per_station[ft] = {
                "price": price,
                "price_last_updated": plu,
                "price_change_effective_timestamp": pcet,
            }

            dt_plu = parse_dt_maybe(plu)
            dt_pcet = parse_dt_maybe(pcet)
            candidates = [t for t in (dt_plu, dt_pcet) if t is not None]
            row_dt = min(max(candidates), now) if candidates else None
            if row_dt and (max_dt is None or row_dt > max_dt):
                max_dt = row_dt
        out[sid] = per_station

    return out, (iso_utc(max_dt) if max_dt else None)


def merge_price_dict(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    base = dict(base or {})
    for sid, fuels in (updates or {}).items():
        sid = str(sid)
        if sid not in base or not isinstance(base.get(sid), dict):
            base[sid] = {}
        for ft, row in (fuels or {}).items():
            base[sid][str(ft)] = row
    return base


def cache_stats(state: dict[str, Any]) -> dict[str, Any]:
    stations = state.get("stations", {}) or {}
    prices = state.get("prices", {}) or {}
    fuel_types = sorted({str(ft) for p in prices.values() if isinstance(p, dict) for ft in p.keys()})
    meta = state.get("meta", {}) or {}
    return {
        "stations_count": len(stations),
        "prices_station_count": len(prices),
        "fuel_types": fuel_types,
        "stations_baseline_at": meta.get("stations_baseline_at"),
        "stations_last_incremental_at": meta.get("stations_last_incremental_at"),
        "prices_baseline_at": meta.get("prices_baseline_at"),
        "prices_last_incremental_at": meta.get("prices_last_incremental_at"),
        "prices_max_price_last_updated": meta.get("prices_max_price_last_updated"),
    }


# --------------------- Refresh Policy & Cache Assurance ---------------------


def _needs_refresh(ts_str: str | None, interval: timedelta) -> bool:
    dt = parse_dt_maybe(ts_str)
    return dt is None or utc_now() >= (dt + interval)


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
            if paths.state_file.exists():
                paths.state_file.unlink()

        state = load_state(paths)
        token = get_access_token(paths, client_id, client_secret)

        def force_refresh_access_token() -> str:
            return get_access_token(paths, client_id, client_secret, force_refresh=True)

        did_stations_baseline = False

        # 1. Stations refresh
        if _needs_refresh(state["meta"].get("stations_baseline_at"), timedelta(days=stations_baseline_days)):
            debug_print("Stations: baseline refresh required")
            items = fetch_all_batches(
                token, "/api/v1/pfs", refresh_token_fn=force_refresh_access_token, batch_sleep=batch_sleep_seconds
            )
            state["stations"] = stations_to_dict(items)
            now = iso_utc(utc_now())
            state["meta"]["stations_baseline_at"] = now
            state["meta"]["stations_last_incremental_at"] = now
            did_stations_baseline = True
        elif _needs_refresh(state["meta"].get("stations_last_incremental_at"), timedelta(hours=stations_incremental_hours)):
            last = parse_dt_maybe(state["meta"].get("stations_last_incremental_at")) or utc_now()
            safe = last - timedelta(minutes=DEFAULTS["incremental_safety_minutes"])
            params = {"effective-start-timestamp": safe.strftime("%Y-%m-%d %H:%M:%S")}
            items = fetch_all_batches(
                token, "/api/v1/pfs", params=params, refresh_token_fn=force_refresh_access_token, batch_sleep=batch_sleep_seconds
            )
            if items:
                state["stations"].update(stations_to_dict(items))
            state["meta"]["stations_last_incremental_at"] = iso_utc(utc_now())

        # 2. Prices refresh
        needs_p_base = (
            did_stations_baseline
            or prices_refresh
            or _needs_refresh(state["meta"].get("prices_baseline_at"), timedelta(days=prices_baseline_days))
        )

        if needs_p_base:
            debug_print("Prices: baseline refresh required")
            items = fetch_all_batches(
                token, "/api/v1/pfs/fuel-prices", refresh_token_fn=force_refresh_access_token, batch_sleep=batch_sleep_seconds
            )
            prices, max_plu = prices_to_dict(items)
            station_count = len(state.get("stations") or {})
            min_expected = int(station_count * prices_min_coverage_ratio)

            if station_count > 0 and len(prices) < min_expected:
                debug_print(f"Prices: baseline REJECTED ({len(prices)} priced vs {station_count} known). Keeping existing cache.")
            else:
                state["prices"] = prices
                now = iso_utc(utc_now())
                state["meta"]["prices_baseline_at"] = now
                state["meta"]["prices_last_incremental_at"] = max_plu or now
                state["meta"]["prices_max_price_last_updated"] = max_plu

                if did_stations_baseline:
                    station_ids = set((state.get("stations") or {}).keys())
                    orphans = set((state.get("prices") or {}).keys()) - station_ids
                    for sid in orphans:
                        del state["prices"][sid]
        elif _needs_refresh(state["meta"].get("prices_last_incremental_at"), timedelta(hours=prices_incremental_hours)):
            debug_print("Prices: incremental refresh")
            last = parse_dt_maybe(state["meta"].get("prices_last_incremental_at")) or utc_now()
            safe = last - timedelta(minutes=DEFAULTS["incremental_safety_minutes"])
            params = {"effective-start-timestamp": safe.strftime("%Y-%m-%d %H:%M:%S")}
            items = fetch_all_batches(
                token, "/api/v1/pfs/fuel-prices", params=params, refresh_token_fn=force_refresh_access_token, batch_sleep=batch_sleep_seconds
            )
            if items:
                upd, max_plu = prices_to_dict(items)
                state["prices"] = merge_price_dict(state["prices"], upd)
                if max_plu:
                    state["meta"]["prices_last_incremental_at"] = max_plu
                    state["meta"]["prices_max_price_last_updated"] = max_plu
                else:
                    state["meta"]["prices_last_incremental_at"] = iso_utc(utc_now())
            else:
                state["meta"]["prices_last_incremental_at"] = iso_utc(utc_now())

        save_state(paths, state)

    return state, cache_stats(state)


# --------------------- CLI & Exporter ---------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="UK Fuel Finder API Data Collector & Dumper")
    p.add_argument("--config-dir", default=None, help="Working directory for config and cache files")
    p.add_argument("--debug", action="store_true", help="Enable debug logging to stderr")
    p.add_argument("--client-id", default=None, help="OAuth client ID")
    p.add_argument("--client-secret", default=None, help="OAuth client secret")
    p.add_argument("--full-refresh", action="store_true", help="Invalidate cache and rebuild baselines from scratch")
    p.add_argument("--prices-refresh", action="store_true", help="Force prices baseline refresh only")
    p.add_argument("--dump", action="store_true", default=True, help="Write all stations with fresh prices to prices_<date>.json")
    p.add_argument("--max-price-age-days", type=float, default=None, metavar="DAYS", help="Exclude fuel prices older than DAYS days")
    return p.parse_args(argv)


def dump_prices_json(
    state: dict[str, Any],
    stats: dict[str, Any],
    paths: Paths,
    max_price_age_days: float | None,
) -> tuple[Path, int]:
    """Write all stations with fresh prices to prices_YYYY-MM-DD.json."""
    cutoff_dt = utc_now() - timedelta(days=max_price_age_days) if max_price_age_days is not None else None
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
                plu = parse_dt_maybe(row.get("price_last_updated"))
                if plu is None or plu < cutoff_dt:
                    continue
            price_out[ftype] = row
        if cutoff_dt is not None and not price_out:
            continue

        st_clean = dict(st)
        st_clean["fuel_prices"] = price_out
        dump_stations.append(st_clean)

    dump_out = {
        "state": "ok",
        "generated_at": iso_utc(utc_now()),
        "cache": stats,
        "stations": dump_stations,
    }
    dump_file = paths.work_dir / f"prices_{utc_now().strftime('%Y-%m-%d')}.json"
    dump_file.write_text(json.dumps(dump_out, ensure_ascii=False, indent=2), encoding="utf-8")
    return dump_file, len(dump_stations)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    global DEBUG
    DEBUG = bool(getattr(args, "debug", False))

    work_dir = args.config_dir or os.environ.get("UFF_CONFIG_DIR") or DEFAULTS["config_dir"]
    paths = make_paths(work_dir)

    cfg = dict(DEFAULTS)
    cfg_file = load_json(paths.config_file)
    if isinstance(cfg_file, dict):
        cfg.update(cfg_file)

    client_id = args.client_id or cfg.get("client_id") or os.environ.get("UFF_CLIENT_ID")
    client_secret = args.client_secret or cfg.get("client_secret") or os.environ.get("UFF_CLIENT_SECRET")

    if not client_id or not client_secret:
        print(json.dumps({
            "state": "error",
            "error": "Missing client_id/client_secret (CLI args, config.json, or env UFF_CLIENT_ID/UFF_CLIENT_SECRET)",
        }, ensure_ascii=False))
        return 2

    try:
        state, stats = ensure_cache(
            paths=paths,
            client_id=client_id,
            client_secret=client_secret,
            full_refresh=args.full_refresh,
            prices_refresh=args.prices_refresh,
            stations_baseline_days=int(cfg.get("stations_baseline_days", DEFAULTS["stations_baseline_days"])),
            stations_incremental_hours=int(cfg.get("stations_incremental_hours", DEFAULTS["stations_incremental_hours"])),
            prices_baseline_days=int(cfg.get("prices_baseline_days", DEFAULTS["prices_baseline_days"])),
            prices_incremental_hours=float(cfg.get("prices_incremental_hours", DEFAULTS["prices_incremental_hours"])),
            batch_sleep_seconds=float(cfg.get("batch_sleep_seconds", DEFAULTS["batch_sleep_seconds"])),
            prices_min_coverage_ratio=float(cfg.get("prices_min_coverage_ratio", DEFAULTS["prices_min_coverage_ratio"])),
        )
    except Exception as e:
        print(json.dumps({
            "state": "error",
            "error": f"Cache refresh failed: {e}",
            "generated_at": iso_utc(utc_now()),
        }, ensure_ascii=False))
        return 2

    dump_file, count = dump_prices_json(state, stats, paths, args.max_price_age_days)
    debug_print(f"Dump: wrote {count} stations to {dump_file}")
    print(json.dumps({"state": "ok", "dump_file": str(dump_file), "station_count": count}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
