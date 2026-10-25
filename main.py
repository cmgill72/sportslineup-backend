"""SportsLineup v0.7 premium sports identity, scores, and licensed local-TV lineup integration and resilient schedule API."""
from __future__ import annotations
from datetime import date, datetime
from time import monotonic
from zoneinfo import ZoneInfo
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from . import push_delivery
from contextlib import asynccontextmanager
import asyncio
from .normalization import SPORTS
from .live_scores import premium_enabled, allow_team_badges, fetch_livescores
from .team_badges import badge_lookup
from .aggregation import fetch_schedule
from .provider import ProviderUnavailable, credential_mode, probe_provider
from .supplemental import enabled_providers
from .gracenote import enabled as gracenote_enabled, configured as gracenote_configured, lineups_for_zip, GracenoteUnavailable, ZIP_RE
from os import getenv
from .network_logos import catalog_response, resolve_network_name

@asynccontextmanager
async def push_lifespan(app: FastAPI):
    worker = None
    if push_delivery.configured() and getenv('SPORTSLINEUP_PUSH_WORKER_ENABLED','false').lower() == 'true':
        worker = asyncio.create_task(push_delivery.worker_loop())
    yield
    if worker:
        worker.cancel()
        try: await worker
        except asyncio.CancelledError: pass

app = FastAPI(title='SportsLineup API', version='0.8.2', lifespan=push_lifespan)
CACHE: dict[str, tuple[float, dict]] = {}
CACHE_SECONDS = 600
STALE_MAX_SECONDS = 7200


@app.get('/health')
async def health():
    return {'status': 'ok', 'app': 'SportsLineup', 'version': '0.8.2',
            'supplementalProvidersEnabled': enabled_providers(),
            'sportsdbKeyMode': credential_mode(),
            'sportsdbKeyConfigured': credential_mode() == 'configured',
            'authorizedEventFeedConfigured': bool(getenv('SPORTSLINEUP_AUTHORIZED_EVENTS_PATH') or getenv('SPORTSLINEUP_AUTHORIZED_FEED_URL')),
            'liveScoresEnabled': premium_enabled(), 'teamBadgesEnabled': allow_team_badges(),
            'gracenoteEnabled': gracenote_enabled(), 'gracenoteKeyConfigured': gracenote_configured()}



@app.get('/v1/teams/badge')
async def team_badge(name: str = Query(min_length=2, max_length=72)):
    """Optional exact team-name lookup; no user key and no copyright grant."""
    return await badge_lookup(name)

@app.get('/v1/scores')
async def scores():
    """No provider token or artwork URLs returned; event IDs from TheSportsDB only."""
    values, health = await fetch_livescores()
    return {'source': 'TheSportsDB V2', 'status': health['status'],
            'fetchedAt': datetime.now(ZoneInfo('UTC')).replace(microsecond=0).isoformat(),
            'scores': [{'id': event_id, 'home': row['home'], 'away': row['away'],
                        'progress': row['progress'], 'state': row['state'], 'source': row['source'],
                        'homeTeam': row.get('homeTeam'), 'awayTeam': row.get('awayTeam')}
                       for event_id, row in values.items()]}


@app.get('/v1/diagnostics/scores')
async def diagnostics_scores():
    # Explicitly distinguishes a running provider from one supplying no live games.
    rows, health = await fetch_livescores()
    return {
        'version': '0.8.2',
        'enabled': premium_enabled(),
        'status': health.get('status', 'unknown'),
        'feedRecords': len(rows),
        'note': ('No matching live score is not a zero score. TheSportsDB does not '
                 'guarantee every NCAA/PGA/UFC event in the premium livescore feed.'),
        'exampleEventIds': list(rows.keys())[:5],
    }


@app.get('/v1/diagnostics/provider')
async def provider_diagnostics():
    # One cached request against TheSportsDB. Status and HTTP code only; never the API key.
    return await probe_provider()



@app.get('/v1/artwork/status')
async def artwork_status():
    """Safe diagnostic for what this deployment permits (never reveals API credentials)."""
    catalog = await catalog_response()
    entries = catalog.get('networks', [])
    return {
        'version': '0.8.2',
        'teamBadgesEnabled': allow_team_badges(),
        'teamBadgesReason': ('enabled; individual badges depend on TheSportsDB artwork' if allow_team_badges()
                             else 'disabled_on_render; set SPORTSLINEUP_ENABLE_TEAM_BADGES=true if rights allow'),
        'networkMode': catalog['mode'],
        'networkEntries': len(entries),
        'networkImageReferences': sum(bool(row.get('imageURL')) for row in entries),
        'networkFallbackEntries': sum(not row.get('imageURL') for row in entries),
        'networkImagesLiveVerified': False,
        'note': 'Image references may fail to download. Trademark rights must be reviewed before public distribution.'
    }


@app.get('/v1/networks/logos')
async def network_logo_library():
    """Curated, cacheable network image registry; no keys or protected originals."""
    from fastapi.responses import JSONResponse
    return JSONResponse(content=await catalog_response(),headers={'Cache-Control':'public, max-age=3600'})


@app.get('/v1/diagnostics/network-images')
async def diagnostics_network_images(probe: bool = False):
    """Reports configured artwork and optionally tests real upstream image responses.

    The probe only accesses approved catalog URLs. It never trusts a user-supplied URL.
    It is limited to seven requests in a single diagnostic run and isn't part of
    normal lineup traffic. This is a diagnostic, not proof of image usage rights.
    """
    catalog = await catalog_response()
    items = catalog.get('networks', [])
    results = [{'name': item['name'], 'imageConfigured': bool(item.get('imageURL')),
                'fallback': not bool(item.get('imageURL'))} for item in items]
    if probe:
        import asyncio
        import httpx
        # Bounded by a strict curated allowlist in the catalog validator.
        images = [(i, item['imageURL']) for i, item in enumerate(items) if item.get('imageURL')][:7]
        async with httpx.AsyncClient(timeout=httpx.Timeout(6, connect=3), follow_redirects=False) as client:
            async def check(i, url):
                try:
                    async with client.stream('GET', url,
                                             headers={'Accept': 'image/png,image/jpeg,image/webp'}) as response:
                        content_type = response.headers.get('content-type','').lower()
                        results[i]['httpStatus'] = response.status_code
                        results[i]['imageUsable'] = (response.status_code == 200 and content_type.startswith('image/'))
                except httpx.HTTPError:
                    results[i]['imageUsable'] = False
                    results[i]['error'] = 'upstream_unreachable'
            await asyncio.gather(*(check(i,url) for i,url in images))
    return {'version': '0.8.2', 'mode': catalog.get('mode'), 'networks': results,
            'remoteImagesVerified': probe,
            'note': 'Probe checks image HTTP responses, not artwork rights. Missing/failed images use text badges.'}


class DeviceRegistration(BaseModel):
    installationId: str = Field(min_length=20,max_length=64)
    installationKey: str = Field(min_length=64,max_length=64)
    apnsToken: str = Field(min_length=32,max_length=400)

class AlertSubscription(BaseModel):
    installationId: str
    installationKey: str
    eventId: str
    title: str
    sport: str
    expires: int
    alerts: dict[str,bool]
    activityToken: str | None = None

@app.get('/v1/push/status')
async def push_status():
    return {'configured':push_delivery.configured(),
            'workerEnabled':getenv('SPORTSLINEUP_PUSH_WORKER_ENABLED','false').lower()=='true',
            'mode':'APNs token delivery; requires always-on Render with persistent disk',
            'liveActivityRequires':'Widget extension, ActivityKit permission, APNs signing and active score feed'}

@app.post('/v1/push/register')
async def push_register(body:DeviceRegistration):
    if not push_delivery.configured(): raise HTTPException(status_code=503,detail='Push not configured on backend')
    if not push_delivery.register_device(body.installationId,body.installationKey,body.apnsToken):
        raise HTTPException(status_code=400,detail='Device registration rejected')
    return {'status':'registered'}

@app.post('/v1/push/subscribe')
async def push_subscribe(body:AlertSubscription):
    if not push_delivery.configured(): raise HTTPException(status_code=503,detail='Push not configured on backend')
    if not push_delivery.subscribe(body.installationId,body.installationKey,body.eventId,body.title,
                                 body.sport,body.expires,body.alerts,body.activityToken):
        raise HTTPException(status_code=400,detail='Subscription rejected; register device and confirm permissions')
    return {'status':'saved'}


@app.get('/v1/sports')
async def sports():
    return {'sports': SPORTS}


@app.get('/v1/providers')
async def providers():
    return {'version': '0.8.2', 'enabled': ['TheSportsDB'] + (['Gracenote (licensed)'] if gracenote_enabled() and gracenote_configured() else []) + enabled_providers() + (['Authorized Event Feed'] if getenv('SPORTSLINEUP_AUTHORIZED_EVENTS_PATH') else []),
            'note': 'Gracenote requires a licensed contract and selected local TV lineup. Extra publicly reachable sports feeds are opt-in research connectors. Verify API rights before distribution; no source certifies subscriber-specific viewing eligibility.'}


@app.get('/v1/schedule')
async def schedule(day: date | None = Query(default=None, alias='date'),
                   tz: str = Query(default='America/Chicago', min_length=3, max_length=64),
                   zip_code: str = Query(default='', alias='zip', max_length=5),
                   lineup_id: str = Query(default='', alias='lineupId', max_length=80)):
    try:
        zone = ZoneInfo(tz)
    except (KeyError, ValueError):
        raise HTTPException(status_code=400, detail='Invalid IANA timezone') from None
    today = datetime.now(zone).date()
    day = day or today
    if abs((day - today).days) > 45:
        raise HTTPException(status_code=400, detail='Date must be within 45 days of today')
    if zip_code and not ZIP_RE.fullmatch(zip_code):
        raise HTTPException(status_code=400, detail='ZIP must contain five digits')
    if lineup_id and not zip_code:
        raise HTTPException(status_code=400, detail='ZIP required for a local lineup')
    key = f'{day.isoformat()}:{tz}:{zip_code}:{lineup_id}:{gracenote_enabled()}:{",".join(enabled_providers())}' 
    cached = CACHE.get(key)
    age = monotonic() - cached[0] if cached else float('inf')
    if cached and age < CACHE_SECONDS:
        return cached[1]
    try:
        if lineup_id:
            result = await fetch_schedule(day, tz, zip_code=zip_code, lineup_id=lineup_id)
        else:
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




@app.get('/v1/lineups')
async def local_lineups(zip_code: str = Query(alias='zip', min_length=5, max_length=5)):
    if not ZIP_RE.fullmatch(zip_code):
        raise HTTPException(status_code=400, detail='ZIP must contain five digits')
    if not gracenote_enabled():
        return {'provider':'Gracenote', 'status':'disabled', 'lineups':[], 'note':'A licensed Gracenote contract and explicit enablement are required.'}
    if not gracenote_configured():
        return {'provider':'Gracenote', 'status':'not_configured', 'lineups':[], 'note':'Configure GRACENOTE_API_KEY on Render.'}
    try:
        lineups = await lineups_for_zip(zip_code)
    except GracenoteUnavailable as problem:
        # No credentials, endpoint URLs, or ZIP printed in error details.
        return {'provider':'Gracenote', 'status':'failed', 'errorCode':problem.code, 'lineups':[]}
    return {'provider':'Gracenote', 'status':'ok', 'lineups':lineups,
            'note':'Choose a real TV lineup; station listings are not a subscription, reception, or blackout guarantee.'}


@app.get('/v1/diagnostics/broadcast')
async def broadcast_diagnostics(day: date | None = Query(default=None, alias='date'),
                                tz: str = Query(default='America/Chicago', min_length=3, max_length=64),
                                zip_code: str = Query(default='', alias='zip', max_length=5),
                                lineup_id: str = Query(default='', alias='lineupId', max_length=80)):
    """Report coverage/missing-listing causes without exposing provider keys.

    This endpoint does not reveal account credentials, full upstream URLs,
    customer subscriptions, or fabricated viewing channels.
    """
    guide_data = await schedule(day=day, tz=tz, zip_code=zip_code if isinstance(zip_code,str) else '', lineup_id=lineup_id if isinstance(lineup_id,str) else '')
    diagnostics = guide_data.get('diagnostics') or {}
    return {
        'date': guide_data.get('date'), 'timezone': guide_data.get('timezone'),
        'fetchedAt': guide_data.get('fetchedAt'), 'stale': guide_data.get('stale', False),
        'coverage': guide_data.get('coverage'),
        'sourceHealth': guide_data.get('sourceHealth', []),
        'quality': guide_data.get('quality', {}),
        'verifiedCorrections': guide_data.get('verifiedCorrections', {'matchedEvents':0,'channelsAdded':0}),
        'tvLookupSummary': {key: diagnostics.get(key, 0) for key in (
            'tvDetailLookups', 'tvLookupEligible', 'tvLookupSkippedByBudget',
            'tvLookupWithUSListings', 'tvLookupOnlyNonUS',
            'tvLookupNoListings', 'tvLookupFailed', 'providerRequestFailures')},
        'gracenoteStatus': diagnostics.get('gracenoteStatus', 'disabled'),
        'gracenoteEvents': diagnostics.get('gracenoteEvents', 0),
        'explanation': 'No US broadcast listing is not proof that a game is untelevised. Verify through an authorized broadcast source.',
    }


@app.get('/v1/coverage/gaps')
async def coverage_gaps(day: date | None = Query(default=None, alias='date'),
                        tz: str = Query(default='America/Chicago', min_length=3, max_length=64),
                        zip_code: str = Query(default='', alias='zip', max_length=5),
                        lineup_id: str = Query(default='', alias='lineupId', max_length=80)):
    """Return actual reported gaps; never guess broadcaster assignments."""
    data = await schedule(day=day, tz=tz, zip_code=zip_code if isinstance(zip_code,str) else '', lineup_id=lineup_id if isinstance(lineup_id,str) else '')
    quality = dict(data.get('quality') or {})
    quality.update({'date': data.get('date'), 'timezone': data.get('timezone'),
                    'fetchedAt': data.get('fetchedAt'), 'stale': data.get('stale', False),
                    'sourceHealth': data.get('sourceHealth', [])})
    return quality


@app.get('/v1/guide')
async def guide(day: date | None = Query(default=None, alias='date'),
                tz: str = Query(default='America/Chicago', min_length=3, max_length=64),
                zip_code: str = Query(default='', alias='zip', max_length=5),
                lineup_id: str = Query(default='', alias='lineupId', max_length=80)):
    return await schedule(day=day, tz=tz, zip_code=zip_code if isinstance(zip_code,str) else '', lineup_id=lineup_id if isinstance(lineup_id,str) else '')
