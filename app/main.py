"""SportsLineup v0.4 guide endpoints with marked-stale last-good fallback."""
from __future__ import annotations
from datetime import date, datetime
from time import monotonic
from zoneinfo import ZoneInfo
from fastapi import FastAPI, HTTPException, Query
from .normalization import SPORTS
from .aggregation import fetch_schedule
from .provider import ProviderUnavailable, credential_mode, probe_provider
from .supplemental import enabled_providers
from os import getenv

app = FastAPI(title='SportsLineup API', version='0.5.0')
CACHE: dict[str, tuple[float, dict]] = {}
CACHE_SECONDS = 600
STALE_MAX_SECONDS = 7200


@app.get('/health')
async def health():
    return {'status': 'ok', 'app': 'SportsLineup', 'version': '0.5.0',
            'supplementalProvidersEnabled': enabled_providers(),
            'sportsdbKeyMode': credential_mode(),
            'sportsdbKeyConfigured': credential_mode() == 'configured',
            'authorizedEventFeedConfigured': bool(getenv('SPORTSLINEUP_AUTHORIZED_EVENTS_PATH'))}


@app.get('/v1/diagnostics/provider')
async def provider_diagnostics():
    # One cached request against TheSportsDB. Status and HTTP code only; never the API key.
    return await probe_provider()


@app.get('/v1/sports')
async def sports():
    return {'sports': SPORTS}


@app.get('/v1/providers')
async def providers():
    return {'version': '0.5.0', 'enabled': ['TheSportsDB'] + enabled_providers() + (['Authorized Event Feed'] if getenv('SPORTSLINEUP_AUTHORIZED_EVENTS_PATH') else []),
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
    except (ProviderUnavailable, TimeoutError) as failure:
        if cached and age <= STALE_MAX_SECONDS:
            snapshot = dict(cached[1])
            snapshot['stale'] = True
            snapshot['partial'] = True
            if isinstance(snapshot.get('quality'), dict):
                snapshot['quality'] = dict(snapshot['quality'])
                snapshot['quality']['warnings'] = list(snapshot['quality'].get('warnings') or []) + [
                    'Showing cached data after provider refresh failure; verify all broadcasts.']
            snapshot['coverage_note'] = ('LAST KNOWN DATA — provider refresh failed; '
                                         f'data is {int(age // 60)} minutes old. ' + snapshot.get('coverage_note', ''))
            return snapshot
        code = failure.code if isinstance(failure, ProviderUnavailable) else 'timeout'
        raise HTTPException(status_code=502, detail={
            'message': 'Sports data sources are unavailable. Confirm the Render SPORTSDB_API_KEY value and inspect the safe provider diagnostic.',
            'errorCode': code,
            'diagnosticPath': '/v1/diagnostics/provider',
        }) from None
    CACHE[key] = (monotonic(), result)
    if len(CACHE) > 100:
        CACHE.pop(next(iter(CACHE)))
    return result




@app.get('/v1/diagnostics/broadcast')
async def broadcast_diagnostics(day: date | None = Query(default=None, alias='date'),
                                tz: str = Query(default='America/Chicago', min_length=3, max_length=64)):
    """Report coverage/missing-listing causes without exposing provider keys.

    This endpoint does not reveal account credentials, full upstream URLs,
    customer subscriptions, or fabricated viewing channels.
    """
    guide_data = await schedule(day=day, tz=tz)
    diagnostics = guide_data.get('diagnostics') or {}
    return {
        'date': guide_data.get('date'), 'timezone': guide_data.get('timezone'),
        'fetchedAt': guide_data.get('fetchedAt'), 'stale': guide_data.get('stale', False),
        'coverage': guide_data.get('coverage'),
        'sourceHealth': guide_data.get('sourceHealth', []),
        'quality': guide_data.get('quality', {}),
        'tvLookupSummary': {key: diagnostics.get(key, 0) for key in (
            'tvDetailLookups', 'tvLookupEligible', 'tvLookupSkippedByBudget',
            'tvLookupWithUSListings', 'tvLookupOnlyNonUS',
            'tvLookupNoListings', 'tvLookupFailed', 'providerRequestFailures')},
        'explanation': 'No US broadcast listing is not proof that a game is untelevised. Verify through an authorized broadcast source.',
    }


@app.get('/v1/coverage/gaps')
async def coverage_gaps(day: date | None = Query(default=None, alias='date'),
                        tz: str = Query(default='America/Chicago', min_length=3, max_length=64)):
    """Return actual reported gaps; never guess broadcaster assignments."""
    data = await schedule(day=day, tz=tz)
    quality = dict(data.get('quality') or {})
    quality.update({'date': data.get('date'), 'timezone': data.get('timezone'),
                    'fetchedAt': data.get('fetchedAt'), 'stale': data.get('stale', False),
                    'sourceHealth': data.get('sourceHealth', [])})
    return quality


@app.get('/v1/guide')
async def guide(day: date | None = Query(default=None, alias='date'),
                tz: str = Query(default='America/Chicago', min_length=3, max_length=64)):
    return await schedule(day=day, tz=tz)
