"""SportsLineup v0.4 guide endpoints with marked-stale last-good fallback."""
from __future__ import annotations
from datetime import date, datetime
from time import monotonic
from zoneinfo import ZoneInfo
from fastapi import FastAPI, HTTPException, Query
from .normalization import SPORTS
from .aggregation import fetch_schedule
from .provider import ProviderUnavailable
from .supplemental import enabled_providers
from os import getenv

app = FastAPI(title='SportsLineup API', version='0.4.0')
CACHE: dict[str, tuple[float, dict]] = {}
CACHE_SECONDS = 600
STALE_MAX_SECONDS = 7200


@app.get('/health')
async def health():
    return {'status': 'ok', 'app': 'SportsLineup', 'version': '0.4.0',
            'supplementalProvidersEnabled': enabled_providers(),
            'authorizedEventFeedConfigured': bool(getenv('SPORTSLINEUP_AUTHORIZED_EVENTS_PATH'))}


@app.get('/v1/sports')
async def sports():
    return {'sports': SPORTS}


@app.get('/v1/providers')
async def providers():
    return {'version': '0.4.0', 'enabled': ['TheSportsDB'] + enabled_providers() + (['Authorized Event Feed'] if getenv('SPORTSLINEUP_AUTHORIZED_EVENTS_PATH') else []),
            'note': 'Extra publicly reachable sports feeds are opt-in research connectors. Verify API rights before distribution; no source certifies subscriber-specific viewing eligibility.'}


@app.get('/v1/schedule')
async def schedule(day: date | None = Query(default=None, alias='date'),
                   tz: str = Query(default='America/Chicago', min_length=3, max_length=64)):
    try:
        zone = ZoneInfo(tz)
    except (KeyError, ValueError):
        raise HTTPException(status_code=400, detail='Invalid IANA timezone') from None
    today = datetime.now(zone).date()
    day = day or today
    if abs((day - today).days) > 45:
        raise HTTPException(status_code=400, detail='Date must be within 45 days of today')
    from .supplemental import enabled_providers
    key = f'{day.isoformat()}:{tz}:{",".join(enabled_providers())}'
    cached = CACHE.get(key)
    age = monotonic() - cached[0] if cached else float('inf')
    if cached and age < CACHE_SECONDS:
        return cached[1]
    try:
        result = await fetch_schedule(day, tz)
    except (ProviderUnavailable, TimeoutError):
        if cached and age <= STALE_MAX_SECONDS:
            snapshot = dict(cached[1])
            snapshot['stale'] = True
            snapshot['partial'] = True
            snapshot['coverage_note'] = ('LAST KNOWN DATA — provider refresh failed; '
                                         f'data is {int(age // 60)} minutes old. ' + snapshot.get('coverage_note', ''))
            return snapshot
        raise HTTPException(status_code=502, detail='No sports data available from configured sources') from None
    CACHE[key] = (monotonic(), result)
    if len(CACHE) > 100:
        CACHE.pop(next(iter(CACHE)))
    return result


@app.get('/v1/guide')
async def guide(day: date | None = Query(default=None, alias='date'),
                tz: str = Query(default='America/Chicago', min_length=3, max_length=64)):
    return await schedule(day=day, tz=tz)
