"""Opt-in HTTPS authorized all-sport event + broadcaster manifest.

Feed is never discovered via web scraping or a client-supplied URL. Operator must
configure a contracted HTTPS feed endpoint on Render. No assumption that any
channel is watchable in the current US market.
"""
from __future__ import annotations
import asyncio, json, os, time
from datetime import date
from urllib.parse import urlsplit
import httpx
from .authorized_events import parse_authorized_rows

_CACHE: dict[str, tuple[float, list, dict]] = {}
_lock = asyncio.Lock()


def configured() -> bool:
    return bool(os.getenv('SPORTSLINEUP_AUTHORIZED_FEED_URL', '').strip())


def _trusted_url() -> str | None:
    value = os.getenv('SPORTSLINEUP_AUTHORIZED_FEED_URL', '').strip()
    if not value: return None
    try:
        parsed = urlsplit(value)
        # Reject localhost, credentials, fragments, plain HTTP and nonstandard ports;
        # the URL is server-operator configured only, never client controlled.
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            return None
        if parsed.port not in (None, 443): return None
        if parsed.hostname.lower() in {'localhost', '127.0.0.1', '::1'}: return None
        return value
    except ValueError:
        return None


async def remote_authorized_events(day: date, tz: str) -> tuple[list, dict | None]:
    if not configured(): return [], None
    status = {'provider': 'Authorized HTTPS Feed', 'sport': 'all', 'status': 'failed', 'events': 0}
    url = _trusted_url()
    if not url: return [], {**status, 'errorCode':'invalid_configuration'}
    key = f'{url}|{day}|{tz}'
    async with _lock:
        current = _CACHE.get(key)
        if current and time.monotonic() - current[0] < 600:
            return current[1], current[2]
    try:
        headers = {'Accept':'application/json'}
        if os.getenv('SPORTSLINEUP_AUTHORIZED_FEED_TOKEN'):
            headers['Authorization'] = 'Bearer ' + os.environ['SPORTSLINEUP_AUTHORIZED_FEED_TOKEN']
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            response = await client.get(url, params={'date':str(day)}, headers=headers)
        response.raise_for_status()
        if len(response.content) > 4_000_000:
            return [], {**status, 'errorCode':'payload_too_large'}
        raw = response.json()
        # Versioned object envelope also supported for licensed vendors.
        if isinstance(raw, dict): raw = raw.get('events')
        if not isinstance(raw, list) or len(raw) > 10000:
            return [], {**status, 'errorCode':'invalid_payload'}
        rows = parse_authorized_rows(raw, day, tz)
        status.update(status='ok' if rows else 'empty', events=len(rows))
        async with _lock:
            _CACHE[key] = (time.monotonic(), rows, status)
        return rows, status
    except (httpx.HTTPError, ValueError, json.JSONDecodeError):
        # Fail independently. Existing primary/other sources remain usable.
        return [], {**status, 'errorCode':'upstream_unavailable'}
