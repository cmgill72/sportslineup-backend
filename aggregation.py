"""SportsLineup v0.4 source orchestration and evidence-preserving merging."""
from __future__ import annotations
import asyncio
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any
from .provider import fetch_schedule as fetch_tsdb, ProviderUnavailable
from .supplemental import supplemental_schedule, enabled_providers
from .reconciliation import reconcile
from .authorized_events import authorized_events
from .remote_authorized import remote_authorized_events
from .broadcast_intelligence import annotate_events, distribution_summary
from .normalization import coverage_breakdown
from .quality import quality_report
from .verified_listings import apply_verified_listings
from .gracenote import sports_airings
from .live_scores import fetch_livescores, attach_scores, allow_team_badges


async def fetch_schedule(day: date, tz: str, zip_code: str = "", lineup_id: str = "") -> dict[str, Any]:
    enabled = enabled_providers()
    results = await asyncio.gather(fetch_tsdb(day, tz), supplemental_schedule(day, tz),
                                   sports_airings(day, tz, lineup_id, zip_code), remote_authorized_events(day, tz), return_exceptions=True)
    primary_raw, extra_raw, gracenote_raw, remote_raw = results
    gn_events, gn_status = gracenote_raw if isinstance(gracenote_raw, tuple) else (
        [], {'provider':'Gracenote','sport':'all','status':'failed','events':0,'errorCode':'adapter_error'})
    primary_ok = isinstance(primary_raw, dict)
    supplemental_ok = isinstance(extra_raw, tuple)
    events_from_extra, provider_health = extra_raw if supplemental_ok else ([], [])
    licensed_rows, licensed_health = authorized_events(day, tz)
    remote_rows, remote_health = remote_raw if isinstance(remote_raw, tuple) else (
        [], {'provider':'Authorized HTTPS Feed','sport':'all','status':'failed','events':0,'errorCode':'adapter_error'})
    has_other = any(r['status'] in ('ok','empty') for r in provider_health)
    has_licensed = any(health and health['status'] in ('ok','empty') for health in (licensed_health,remote_health))
    has_gracenote = gn_status['status'] in ('ok', 'empty')
    if not primary_ok and not has_other and not has_licensed and not has_gracenote:
        failure_code = primary_raw.code if isinstance(primary_raw, ProviderUnavailable) else 'upstream_unavailable'
        raise ProviderUnavailable('No enabled sports schedule provider returned a usable response', failure_code)
    primary = primary_raw['events'] if primary_ok else []
    extras = events_from_extra + licensed_rows + remote_rows + gn_events
    merged, reconciliation = reconcile(primary, extras)
    verified = apply_verified_listings(merged, day)
    # Livescores never change which broadcasters are available; ID-only reconciliation.
    if day == datetime.now(ZoneInfo(tz)).date():
        scores, score_health = await fetch_livescores()
        score_matches = attach_scores(merged, scores)
    else:
        scores, score_health, score_matches = {}, {'status': 'not_current_day'}, 0
    annotate_events(merged)
    coverage = coverage_breakdown(merged)
    status = [{'provider':'TheSportsDB', 'sport':'all', 'status': 'ok' if primary_ok else 'failed',
               'events': len(primary)}] + provider_health
    if licensed_health: status.append(licensed_health)
    if remote_health: status.append(remote_health)
    status.append(gn_status)
    if enabled and not supplemental_ok:
        status.extend({'provider': source.upper(), 'sport':'all', 'status':'failed', 'events':0} for source in enabled)
    failures = sum(x['status'] in ('failed','invalid_response') for x in status)
    data = dict(primary_raw) if primary_ok else {}
    source_names = [x['provider'] for x in status if x['status'] == 'ok']
    if not source_names:
        source_names = ['Partial provider responses']
    note = (data.get('coverage_note', '') + ' ' if primary_ok else 'TheSportsDB unavailable. ') + (
        f'Multi-source: {reconciliation["added"]} additional source events; '
        f'{reconciliation["matched"]} strongly matched; '
        f'{reconciliation["channelsAdded"]} additional literal broadcast labels; '
        f'{failures} provider failures. '
        f'{verified["matchedEvents"]} dated games checked against official team broadcaster pages. '
        f'Local lineup source: {gn_status["status"]}; {len(gn_events)} identifiable event(s). '
        'Event counts are returned-source counts, NOT proof of schedule completeness. '
        'No active subscription, local market availability or blackout rights are verified.'
    )
    data.update({
        'verifiedCorrections': verified,
        'date': day.isoformat(), 'timezone': tz,
        'fetchedAt': datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z'),
        'source': ' + '.join(dict.fromkeys(source_names)),
        'events': merged, 'partial': True, 'coverage_note': note,
        'guideVersion': '0.8.3', 'coverage': coverage,
        'distributionCoverage': distribution_summary(merged),
        'disclaimer': 'Provider-reported broadcast names only. Market coverage, rights, regional blocks, antenna reception, subscription eligibility and local game feeds are unverified.',
        'sourceHealth': status, 'reconciliation': reconciliation, 'stale': False,
    })
    diagnostics = data.get('diagnostics') or {
        'providerRequestFailures': 1, 'utcDaysQueried': 0, 'tvDetailLookups': 0,
        'tvOrphanLookups': 0, 'freePreview': False, 'licensedMatches': 0,
    }
    diagnostics['supplementalSourcesEnabled'] = enabled
    diagnostics['gracenoteStatus'] = gn_status['status']
    diagnostics['gracenoteEvents'] = len(gn_events)
    diagnostics['authorizedEventFeedEnabled'] = licensed_health is not None or remote_health is not None
    diagnostics['authorizedRemoteFeedStatus'] = (remote_health or {}).get('status', 'not_configured')
    diagnostics['supplementalFailures'] = failures
    data['diagnostics'] = diagnostics
    data['quality'] = quality_report(merged, status, diagnostics)
    data['guideVersion'] = '0.8.3'
    data['scoreCoverage'] = {'status': score_health['status'], 'feedRecords': score_health.get('feedRecords', 0),
                             'matchedEvents': sum(bool(event.get('score')) for event in merged), 'source': 'TheSportsDB event scores + V2 live feed',
                             'teamBadgesEnabled': allow_team_badges()}
    data['scoreFetchedAt'] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z') if score_health['status'] == 'ok' else None
    return data
