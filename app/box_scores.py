"""On-demand TheSportsDB Premium V2 event statistics for Game Center.

This API supplies some event/team statistics and athlete event results, NOT a
contractual guarantee of a full NBA/NFL/MLB player-by-player box score. Never
invent absent fields, combine unrelated events, or expose the provider key.
"""
from __future__ import annotations

import asyncio
from collections import deque
import os
import re
from time import monotonic
from typing import Any

import httpx

V2_BASE = 'https://www.thesportsdb.com/api/v2/json/lookup'
EVENT_ID = re.compile(r'^tsdb-([0-9]{4,12})$')
CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
CACHE_TTL_SECONDS = 150
CACHE_MAX = 150
# Per-instance budget, before making two upstream calls per event. Production
# multi-instance scaling needs a shared rate limiter/counter.
_REQUEST_TIMES: deque[float] = deque()
_REQUEST_LOCK = asyncio.Lock()
MAX_FETCHES_PER_MINUTE = 10


def _text(value: Any, limit: int = 80) -> str:
    if value is None or isinstance(value, (dict, list, bool)):
        return ''
    return ' '.join(str(value).strip().split())[:limit]


def _number(value: Any) -> str:
    """Keep provider numeric formatting, including 0; no inferred units."""
    raw = _text(value, 24)
    return raw if re.fullmatch(r'-?\d+(?:\.\d+)?%?', raw) else ''


def _rows(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in keys:
        group = payload.get(key)
        if isinstance(group, list):
            return [row for row in group if isinstance(row, dict)]
    return []


def normalize_stats(payload: Any, event_id: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen = set()
    for row in _rows(payload, 'lookup', 'eventstats')[:100]:
        if _text(row.get('idEvent')) != event_id:
            continue
        label = _text(row.get('strStat'), 64)
        home = _number(row.get('intHome'))
        away = _number(row.get('intAway'))
        if not label or (not home and not away) or label.casefold() in seen:
            continue
        seen.add(label.casefold())
        result.append({'label': label, 'home': home or '—', 'away': away or '—'})
        if len(result) >= 70:
            break
    return result


def normalize_results(payload: Any, event_id: str) -> list[dict[str, str | None]]:
    result = []
    seen = set()
    for row in _rows(payload, 'lookup', 'results')[:150]:
        if _text(row.get('idEvent')) != event_id:
            continue
        name = _text(row.get('strPlayer'), 80)
        if not name:
            continue
        position = _number(row.get('intPosition'))
        points = _number(row.get('intPoints'))
        detail = _text(row.get('strResult'), 70) or _text(row.get('strDetail'), 70)
        identity = (_text(row.get('idPlayer')), name.casefold(), position)
        if identity in seen:
            continue
        seen.add(identity)
        result.append({'id': f"{event_id}-{_text(row.get('idResult')) or _text(row.get('idPlayer')) or len(result)}-{len(result)}",
                       'name': name, 'position': position or None,
                       'points': points or None, 'result': detail or None})
        if len(result) >= 100:
            break
    return result


def _response(event_id: str, status: str, *, team_stats=None, results=None, note: str = '') -> dict[str, Any]:
    return {'eventId': event_id, 'source': 'TheSportsDB Premium V2',
            'status': status, 'teamStats': team_stats or [], 'results': results or [],
            'note': note}


async def fetch_box_score(app_event_id: str, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    match = EVENT_ID.fullmatch(app_event_id)
    if not match:
        return _response(app_event_id, 'unsupported', note='Detailed statistics require an exact TheSportsDB event ID.')
    provider_id = match.group(1)
    key = os.getenv('SPORTSDB_API_KEY', '').strip()
    if not key or key == '123':
        return _response(app_event_id, 'not_configured', note='Premium event details require the existing SPORTSDB_API_KEY on Render.')
    if transport is None:
        cached = CACHE.get(app_event_id)
        if cached and monotonic() - cached[0] < CACHE_TTL_SECONDS:
            return dict(cached[1])

    if transport is None:
        async with _REQUEST_LOCK:
            now = monotonic()
            while _REQUEST_TIMES and now - _REQUEST_TIMES[0] >= 60:
                _REQUEST_TIMES.popleft()
            if len(_REQUEST_TIMES) >= MAX_FETCHES_PER_MINUTE:
                return _response(app_event_id, 'rate_limited',
                                 note='Detailed-stat requests are temporarily limited to protect your premium API quota.')
            _REQUEST_TIMES.append(now)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(9, connect=4), transport=transport,
        headers={'X-API-KEY': key, 'Accept': 'application/json'},
        follow_redirects=False,
    ) as client:
        async def lookup(which: str) -> tuple[dict[str, Any] | None, str]:
            # Path components are fixed or strictly digits, never user URLs.
            try:
                response = await client.get(f'{V2_BASE}/{which}/{provider_id}')
                if response.status_code in (401, 403):
                    return None, 'auth_failed'
                if response.status_code == 429:
                    return None, 'rate_limited'
                if response.status_code != 200:
                    return None, 'provider_unavailable'
                data = response.json()
                return (data, 'ok') if isinstance(data, dict) else (None, 'provider_unavailable')
            except (httpx.HTTPError, ValueError):
                return None, 'provider_unavailable'

        stats_reply, results_reply = await asyncio.gather(
            lookup('event_stats'), lookup('event_results'))
    stats, stats_status = stats_reply
    results, results_status = results_reply
    team_stats = normalize_stats(stats, provider_id)
    competitors = normalize_results(results, provider_id)
    if team_stats or competitors:
        result = _response(app_event_id, 'available', team_stats=team_stats, results=competitors,
                           note='Shows only statistics actually provided for this event. A complete player box score is not guaranteed.')
    elif stats_status == 'auth_failed' or results_status == 'auth_failed':
        result = _response(app_event_id, 'auth_failed', note='TheSportsDB rejected the premium credentials on Render.')
    elif stats_status == 'rate_limited' or results_status == 'rate_limited':
        result = _response(app_event_id, 'rate_limited', note='TheSportsDB temporarily limited detailed-stat requests.')
    elif stats_status == 'ok' and results_status == 'ok':
        result = _response(app_event_id, 'no_data', note='TheSportsDB returned no event statistics or competitor results for this game.')
    else:
        result = _response(app_event_id, 'unavailable', note='Detailed statistics are temporarily unavailable from TheSportsDB.')
    if transport is None and result['status'] in ('available', 'no_data'):
        if len(CACHE) >= CACHE_MAX:
            CACHE.pop(next(iter(CACHE)))
        CACHE[app_event_id] = (monotonic(), result)
    return result
