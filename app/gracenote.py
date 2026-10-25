"""Gracenote OnConnect sports-airing integration for a *licensed* customer account.

Only server-side API access. Does not infer streaming rights or national coverage.
Available after the developer obtains Gracenote commercial credentials and enables
SPORTSLINEUP_ENABLE_GRACENOTE=true; the backend never ships a key.

Documented APIs: /v1.1/lineups and /v1.1/sports/all/events/airings.
See docs at developer.tmsapi.com; contract/new portal may require updates.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from time import monotonic
from os import getenv
import re
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import httpx
from .normalization import merge_channels

API_ROOT = 'https://data.tmsapi.com/v1.1'
ZIP_RE = re.compile(r'^\d{5}$')
LINEUP_RE = re.compile(r'^[A-Za-z0-9-]{4,80}$')
MAX_ITEMS = 3000
_LINEUP_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}
_AIRINGS_CACHE: dict[str, tuple[float, list[dict], dict]] = {}
_REQUEST_TIMES: list[float] = []
MAX_PROVIDER_REQUESTS_PER_HOUR = 60  # Protect an optional metered commercial API.

class GracenoteUnavailable(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def configured() -> bool:
    return bool(getenv('GRACENOTE_API_KEY', '').strip())


def enabled() -> bool:
    return getenv('SPORTSLINEUP_ENABLE_GRACENOTE', 'false').lower() == 'true'


def _root() -> str:
    # Limit outbound API traffic to the trusted documented host, not an arbitrary URL.
    root = getenv('GRACENOTE_API_BASE_URL', API_ROOT).rstrip('/')
    parsed = urlsplit(root)
    if parsed.scheme != 'https' or parsed.hostname != 'data.tmsapi.com' or parsed.username or parsed.password:
        raise GracenoteUnavailable('invalid_provider_host')
    return root


async def _get(path: str, params: dict) -> Any:
    key = getenv('GRACENOTE_API_KEY', '').strip()
    if not key:
        raise GracenoteUnavailable('credentials_not_configured')
    now = monotonic()
    _REQUEST_TIMES[:] = [t for t in _REQUEST_TIMES if now - t < 3600]
    if len(_REQUEST_TIMES) >= MAX_PROVIDER_REQUESTS_PER_HOUR:
        raise GracenoteUnavailable('local_hourly_request_budget_exhausted')
    _REQUEST_TIMES.append(now)
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            resp = await client.get(_root() + path, params={**params, 'api_key': key},
                                    headers={'Accept': 'application/json'})
        if resp.status_code in (401, 403):
            raise GracenoteUnavailable('credentials_or_entitlement_rejected')
        if resp.status_code == 429:
            raise GracenoteUnavailable('rate_limited')
        if resp.status_code != 200:
            raise GracenoteUnavailable('provider_http_error')
        return resp.json()
    except GracenoteUnavailable:
        raise
    except (httpx.HTTPError, ValueError):
        # NEVER return httpx's error message: request URLs contain api_key.
        raise GracenoteUnavailable('provider_unreachable_or_invalid_json') from None


async def lineups_for_zip(zip_code: str) -> list[dict[str, str]]:
    if not ZIP_RE.fullmatch(zip_code):
        raise ValueError('ZIP must contain five digits')
    if not enabled():
        raise GracenoteUnavailable('disabled')
    cached = _LINEUP_CACHE.get(zip_code)
    if cached and monotonic() - cached[0] < 3600:
        return list(cached[1])
    raw = await _get('/lineups', {'country': 'USA', 'postalCode': zip_code})
    if not isinstance(raw, list):
        raise GracenoteUnavailable('unexpected_lineup_response')
    choices: list[dict[str, str]] = []
    for row in raw[:300]:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get('lineupId') or '')
        name = str(row.get('name') or '').strip()
        kind = str(row.get('type') or '').upper()
        if LINEUP_RE.fullmatch(item_id) and 1 <= len(name) <= 150 and kind in ('OTA','CABLE','SATELLITE','MVPD'):
            choices.append({'id': item_id, 'name': name, 'type': kind})
    _LINEUP_CACHE[zip_code] = (monotonic(), choices)
    if len(_LINEUP_CACHE) > 60:
        _LINEUP_CACHE.pop(next(iter(_LINEUP_CACHE)))
    return choices


def _sport(program: dict) -> str | None:
    """Require explicit league/tour identity; never turn arbitrary basketball into NBA."""
    parts = [str(program.get(name) or '') for name in ('title', 'eventTitle', 'longDescription')]
    parts.extend(str(x) for x in program.get('genres', []) if isinstance(x, str))
    value = ' '.join(parts).lower()
    if re.search(r'\bwnba\b|women.s national basketball association', value): return 'wnba'
    if re.search(r'\bnba\b|national basketball association', value): return 'nba'
    if re.search(r'\bcollege basketball\b|\bncaa basketball\b|\bncaab\b|\bmen.s basketball\b|\bwomen.s basketball\b', value): return 'ncaa_basketball'
    if re.search(r'\bcollege football\b|\bncaa football\b|\bncaaf\b', value): return 'ncaa_football'
    if re.search(r'\bnfl\b|national football league', value): return 'nfl'
    if re.search(r'\bmajor league baseball\b|\bmlb\b', value): return 'mlb'
    if re.search(r'\bnhl\b|national hockey league', value): return 'nhl'
    if re.search(r'\bpga (?:tour|championship)\b|\bpga tour\b', value): return 'pga'
    if re.search(r'\bufc\b|ultimate fighting championship', value): return 'ufc'
    return None


def _stamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str): return None
    try:
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def parse_airings(rows: Any, day: date, tz: str, lineup_id: str, lineup_type: str = 'CABLE') -> tuple[list[dict], dict[str, int]]:
    """Parse only identifiable sports and genuine airing records; grouping by TMS ID.

    Game start time comes from program.gameDate/gameTime, not the TV airtime,
    whenever available; else explicitly tag the event time as an *airing* time.
    """
    zone = ZoneInfo(tz)
    result: dict[str, dict] = {}
    stats = {'airings': 0, 'supportedAirings': 0, 'unclassified': 0, 'noStation': 0}
    if not isinstance(rows, list):
        raise GracenoteUnavailable('unexpected_airings_response')
    for row in rows[:MAX_ITEMS]:
        if not isinstance(row, dict): continue
        stats['airings'] += 1
        program = row.get('program') if isinstance(row.get('program'), dict) else {}
        sport = _sport(program)
        if not sport:
            stats['unclassified'] += 1
            continue
        start_airing = _stamp(row.get('startTime'))
        if not start_airing or start_airing.astimezone(zone).date() != day:
            continue
        station = row.get('station') if isinstance(row.get('station'), dict) else {}
        callsign = str(station.get('callSign') or '').strip()
        number = str(station.get('channel') or '').strip()
        # A channel number is not a broadcaster identity: use only known call-sign.
        if not callsign or len(callsign) > 70:
            stats['noStation'] += 1
            continue
        tms_id = str(program.get('tmsId') or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9]{4,30}', tms_id):
            continue
        names = [str(t.get('name') or '').strip() for t in program.get('teams', [])
                 if isinstance(t, dict)] if isinstance(program.get('teams'), list) else []
        teams = names if len(names) == 2 and all(names) else []
        game_start = _stamp(str(program.get('gameDate') or '') + 'T' + str(program.get('gameTime') or ''))
        actual_start = game_start or start_airing
        # Preserve timezone-local market airing date; late-night game starts may be later.
        title = str(program.get('eventTitle') or program.get('title') or '').strip()
        if not title or len(title) > 200: continue
        key = '|'.join([sport, tms_id, str(program.get('gameDate') or day.isoformat()), title.casefold()])
        qualifiers = [str(q)[:60] for q in (row.get('qualifiers') or []) if isinstance(q, str)]
        restriction = any('blackout' in q.lower() for q in qualifiers)
        broadcast = {
            'id': 'gracenote-' + re.sub('[^a-zA-Z0-9]', '', tms_id) + '-' + str(len(result.get(key, {}).get('broadcasts', []))),
            'channel': callsign, 'type': 'source_listing', 'confidence': 'provider_listed',
            'source': 'Gracenote', 'country': 'US', 'url': None,
            'marketScope': 'selected_lineup', 'lineupId': lineup_id,
            'lineupType': lineup_type,
            'channelNumber': number or None, 'stationId': str(row.get('stationId') or station.get('stationId') or '') or None,
            'airingStartTime': start_airing.isoformat().replace('+00:00','Z'),
            'blackoutWarning': restriction,
        }
        if key not in result:
            item = {
                'id': 'gracenote-' + tms_id + '-' + day.isoformat(),
                'sport': sport, 'league': str(program.get('title') or sport.upper())[:100],
                'title': title, 'startTime': actual_start.isoformat().replace('+00:00','Z'),
                'status': 'scheduled', 'venue': None, 'broadcasts': [],
                '_participants': teams, 'scheduleSources': ['Gracenote'],
                'timeBasis': 'game' if game_start else 'airing',
            }
            result[key] = item
        merge_channels(result[key]['broadcasts'], [broadcast])
        stats['supportedAirings'] += 1
    return list(result.values()), stats


async def sports_airings(day: date, tz: str, lineup_id: str, zip_code: str = '') -> tuple[list[dict], dict]:
    if not enabled():
        return [], {'provider':'Gracenote', 'sport':'all', 'status':'disabled', 'events':0}
    if not configured():
        return [], {'provider':'Gracenote', 'sport':'all', 'status':'not_configured', 'events':0}
    if not lineup_id:
        return [], {'provider':'Gracenote', 'sport':'all', 'status':'awaiting_lineup', 'events':0}
    if not LINEUP_RE.fullmatch(lineup_id) or not ZIP_RE.fullmatch(zip_code):
        return [], {'provider':'Gracenote', 'sport':'all', 'status':'invalid_lineup', 'events':0}
    status = {'provider':'Gracenote', 'sport':'all', 'status':'failed', 'events':0}
    cache_key = f'{day.isoformat()}:{tz}:{zip_code}:{lineup_id}'
    cached = _AIRINGS_CACHE.get(cache_key)
    if cached and monotonic() - cached[0] < 1800:
        return cached[1], dict(cached[2])
    # Sports airings API documented as a max 24-hour request. Use 12-hour chunks,
    # including daylight-saving transition days without exceeding the limit.
    zone = ZoneInfo(tz)
    left = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
    right = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    rows: list = []
    current = left
    try:
        available = await lineups_for_zip(zip_code)
        if lineup_id not in {r['id'] for r in available}:
            raise GracenoteUnavailable('lineup_not_available_for_zip')
        while current < right:
            end = min(current + timedelta(hours=12), right)
            payload = await _get('/sports/all/events/airings', {
                'lineupId': lineup_id, 'startDateTime': current.isoformat().replace('+00:00','Z'),
                'endDateTime': end.isoformat().replace('+00:00','Z'), 'liveOnly':'false',
            })
            if not isinstance(payload, list):
                raise GracenoteUnavailable('unexpected_airings_response')
            rows.extend(payload)
            current = end
        lineup_type = next((r['type'] for r in available if r['id'] == lineup_id), 'CABLE')
        events, stats = parse_airings(rows, day, tz, lineup_id, lineup_type)
        status.update(status='ok' if events else 'empty', events=len(events),
                      airingsInspected=stats['airings'], unclassifiedAirings=stats['unclassified'])
        _AIRINGS_CACHE[cache_key] = (monotonic(), events, dict(status))
        if len(_AIRINGS_CACHE) > 100:
            _AIRINGS_CACHE.pop(next(iter(_AIRINGS_CACHE)))
        return events, status
    except GracenoteUnavailable as error:
        status['status'] = 'failed'
        status['errorCode'] = error.code  # Does not contain credentials or URLs.
        return [], status
