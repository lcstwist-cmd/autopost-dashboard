"""IP geolocation client for the AutoPost dashboard.

Backed by ipstack.com (https://ipstack.com/documentation). Free tier:
  • 100 lookups / day
  • HTTP only (no HTTPS) — we use http://api.ipstack.com/...
  • IPv4 and IPv6 supported

Usage:

    from src.dashboard.geo import geolocate_ip, timezone_offset_hours

    geo = geolocate_ip("8.8.8.8")
    # {"country_code": "US", "country_name": "United States",
    #  "city": "Mountain View", "region_name": "California",
    #  "latitude": 37.4, "longitude": -122.0,
    #  "timezone_id": "America/Los_Angeles", "utc_offset_min": -480}

A 24-hour in-memory cache keyed on IP keeps the request volume low. We
also short-circuit private / loopback / multicast IPs without a network
call so local development doesn't burn quota.

The module never raises on lookup failure — it returns an empty dict so
the caller can decide whether to fall back to a default timezone.
"""
from __future__ import annotations

import ipaddress
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CACHE: dict[str, dict] = {}
_CACHE_TTL = 24 * 60 * 60   # 24 hours
_HTTP_TIMEOUT = 6.0          # seconds


def _api_key() -> str:
    """Read the ipstack API key from the environment lazily."""
    return (os.environ.get("IPSTACK_API_KEY") or "").strip()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def geolocate_ip(ip: str) -> dict[str, Any]:
    """Return a normalized geolocation dict for `ip`.

    Returns {} when:
      * the key is missing
      * the IP is private / loopback / not a real public IP
      * the API call fails or returns an error
    """
    ip = (ip or "").strip()
    if not ip:
        return {}

    # Strip IPv6-mapped IPv4 ("::ffff:1.2.3.4") and bracket IPv6.
    if ip.startswith("::ffff:"):
        ip = ip[len("::ffff:"):]
    if ip.startswith("[") and ip.endswith("]"):
        ip = ip[1:-1]

    # Skip lookup for non-routable addresses.
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return {}
    if (ip_obj.is_loopback or ip_obj.is_private or ip_obj.is_link_local
            or ip_obj.is_multicast or ip_obj.is_reserved or ip_obj.is_unspecified):
        return {}

    # Cache hit?
    now = time.time()
    cached = _CACHE.get(ip)
    if cached and (now - cached.get("_ts", 0)) < _CACHE_TTL:
        return cached.get("data", {})

    key = _api_key()
    if not key:
        return {}

    # ipstack only allows HTTPS on paid plans — free tier MUST use http://.
    url = (f"http://api.ipstack.com/{urllib.parse.quote(ip)}"
            f"?access_key={urllib.parse.quote(key)}")
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        print(f"[geo] lookup failed for {ip}: {exc}", file=sys.stderr)
        return {}

    if isinstance(raw, dict) and raw.get("error"):
        print(f"[geo] ipstack error for {ip}: {raw.get('error')}",
              file=sys.stderr)
        return {}

    tz = raw.get("time_zone") or {}
    data = {
        "ip":             raw.get("ip", ip),
        "country_code":   (raw.get("country_code") or "").upper(),
        "country_name":   raw.get("country_name") or "",
        "region_code":    raw.get("region_code") or "",
        "region_name":    raw.get("region_name") or "",
        "city":           raw.get("city") or "",
        "zip":            raw.get("zip") or "",
        "latitude":       raw.get("latitude"),
        "longitude":      raw.get("longitude"),
        "timezone_id":    tz.get("id") or "",
        "utc_offset_min": _parse_utc_offset(tz),
    }

    _CACHE[ip] = {"_ts": now, "data": data}
    return data


def _parse_utc_offset(time_zone: dict) -> int | None:
    """Best-effort conversion from ipstack's ``time_zone`` block to minutes.

    ipstack returns ``gmt_offset`` in seconds, e.g. 7200 for UTC+02:00.
    Older payloads may omit it; in that case we leave it None.
    """
    gmt = time_zone.get("gmt_offset")
    if isinstance(gmt, (int, float)):
        return int(gmt) // 60
    return None


def timezone_offset_hours(ip: str) -> float | None:
    """Convenience: returns UTC offset in hours (e.g. 2.0 for Bucharest in winter).

    Used by the signup flow to translate "08:00 local" into the equivalent
    UTC time that the scheduler stores. Returns None when unknown so the
    caller falls back to the configured default (08:00 server local).
    """
    geo = geolocate_ip(ip)
    off_min = geo.get("utc_offset_min")
    if off_min is None:
        return None
    return off_min / 60.0


def country_label(geo: dict) -> str:
    """Pretty short label like "Bucharest, RO" or "RO" or empty string."""
    if not geo:
        return ""
    parts = []
    if geo.get("city"):
        parts.append(geo["city"])
    cc = geo.get("country_code") or ""
    if cc:
        parts.append(cc)
    return ", ".join(parts)


def cache_size() -> int:
    return len(_CACHE)


def clear_cache() -> None:
    _CACHE.clear()
