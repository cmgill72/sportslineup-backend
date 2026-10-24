"""SportsLineup v0.4 source orchestration and evidence-preserving merging."""
from __future__ import annotations
import asyncio
from datetime import date, datetime, timezone
from typing import Any
from .provider import fetch_schedule as fetch_tsdb, ProviderUnavailable
from .supplemental import supplemental_schedule, enabled_providers
from .reconciliation import reconcile
from .authorized_events import authorized_events
from .broadcast_intelligence import annotate_events, distribution_summary
from .normalization import coverage_breakdown


async def fetch_schedule(day: date, tz: str) -> dict[str, Any]:
    enabled = enabled_providers()
    results = await asyncio.gather(fetch_tsdb(day, tz), supplemental_schedule(day, tz), return_exceptions=True)
    primary_raw, extra_raw = results
    primary_ok = isinstance(primary_raw, dict)
    supplemental_ok = isinstance(extra_raw, tuple)
    events_from_extra, provider_health = extra_raw if supplemental_ok else ([], [])
    licensed_rows, licensed_health = authorized_events(day, tz)
    has_other = any(r['status'] in ('ok','empty') for r in provider_health)
    has_licensed = bool(licensed_health and licensed_health['status'] in ('ok','empty'))
    if not primary_ok and not has_other and not has_licensed:
        raise ProviderUnavailable('No enabled sports schedule provider returned a usable response')
    primary = primary_raw['events'] if primary_ok else []
    extras = events_from_extra + licensed_rows
    merged, reconciliation = reconcile(primary, extras)
    annotate_events(merged)
    coverage = coverage_breakdown(merged)
    status = [{'provider':'TheSportsDB', 'sport':'all', 'status': 'ok' if primary_ok else 'failed',
               'events': len(primary)}] + provider_health
    if licensed_health: status.append(licensed_health)
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
        'Event counts are returned-source counts, NOT proof of schedule completeness. '
        'No active subscription, local market availability or blacklist/blackout rights are verified.'
    )
    data.update({
        'date': day.isoformat(), 'timezone': tz,
        'fetchedAt': datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z'),
        'source': ' + '.join(dict.fromkeys(source_names)),
        'events': merged, 'partial': True, 'coverage_note': note,
        'guideVersion': '0.4', 'coverage': coverage,
        'distributionCoverage': distribution_summary(merged),
        'disclaimer': 'Provider-reported broadcast names only. Market coverage, rights, regional blocks, antenna reception, subscription eligibility and local game feeds are unverified.',
        'sourceHealth': status, 'reconciliation': reconciliation, 'stale': False,
    })
    diagnostics = data.get('diagnostics') or {
        'providerRequestFailures': 1, 'utcDaysQueried': 0, 'tvDetailLookups': 0,
        'tvOrphanLookups': 0, 'freePreview': False, 'licensedMatches': 0,
    }
    diagnostics['supplementalSourcesEnabled'] = enabled
    diagnostics['authorizedEventFeedEnabled'] = licensed_health is not None
    diagnostics['supplementalFailures'] = failures
    data['diagnostics'] = diagnostics
    return data
