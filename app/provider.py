"""SportsLineup v0.4: schedule + dated US TV feed + bounded per-event TV lookup.

Uses documented TheSportsDB v1 endpoints. Free key `123` is for limited testing
only. Missing broadcaster data stays missing; 'provider_listed' is not a
verified streaming entitlement or market-specific antenna availability.
"""
from __future__ import annotations
import asyncio
import os
from collections import deque
from time import monotonic
from datetime import date, timedelta, datetime, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from .normalization import (SPORTS, records, tv_records, broadcast_rows,
                            normalize_event, merge_channels, coverage_breakdown, is_on_local_day)
from .broadcast_intelligence import annotate_events, distribution_summary
from .licensed_feed import licensed_listings

BASE = 'https://www.thesportsdb.com/api/v1/json'
SOURCE_SPORTS = ('Basketball', 'Baseball', 'American Football', 'Ice Hockey', 'Golf', 'Fighting', 'Martial Arts')

# Per-process best-effort provider protection; external persistent cache/rate limiting
# recommended before wide-scale rollout or multi-instance Render scaling.
RAW_CACHE: dict[tuple, tuple[float, dict]] = {}
RAW_CACHE_TTL = 900
_REQUEST_TIMES: deque[float] = deque()
_REQUEST_LOCK = asyncio.Lock()

class ProviderUnavailable(RuntimeError):
    pass


def _lookup_limit(is_free: bool) -> int:
    default = 3 if is_free else 16
    try:
        return min(48, max(0, int(os.getenv('SPORTSDB_LOOKUP_LIMIT', default))))
    except ValueError:
        return default


def _client_key() -> str:
    return os.getenv('SPORTSDB_API_KEY', '123').strip() or '123'


async def fetch_schedule(day: date, tz_name: str = 'America/Chicago', transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Query two UTC source dates to cover a full US-local day.

    A 9 pm Central game can be dated *tomorrow* in UTC, so joining only the
    requested date silently loses late games. A valid IANA timezone is required.
    """
    try:
        tz = ZoneInfo(tz_name)
    except (KeyError, ValueError):
        raise ValueError('Unsupported IANA timezone') from None
    key = _client_key()
    is_free = key == '123'
    # Positive UTC offsets need the previous UTC source day; US-negative offsets
    # need the following date for evening games.
    offset = datetime(day.year, day.month, day.day, 12, tzinfo=tz).utcoffset()
    ahead = -1 if offset and offset.total_seconds() > 0 else 1
    source_days = (day, day + timedelta(days=ahead))
    timeout = httpx.Timeout(14.0, connect=6.0)
    async with httpx.AsyncClient(timeout=timeout, transport=transport, headers={'Accept': 'application/json'}) as client:
        async def get_json(path: str, params: dict[str, str]) -> dict[str, Any]:
            # Secret key is never returned in an exception or exposed to the app.
            url = f'{BASE}/{quote(key, safe="")}/{path}'
            cache_key = (key, path, tuple(sorted(params.items())))
            if transport is None:
                previous = RAW_CACHE.get(cache_key)
                if previous and monotonic() - previous[0] < RAW_CACHE_TTL:
                    return previous[1]
                async with _REQUEST_LOCK:
                    moment = monotonic()
                    while _REQUEST_TIMES and moment - _REQUEST_TIMES[0] > 60:
                        _REQUEST_TIMES.popleft()
                    limit = 27 if is_free else 88
                    # Leave headroom below documented provider request ceilings.
                    if len(_REQUEST_TIMES) >= limit:
                        raise ProviderUnavailable('Provider request budget reached; retry later')
                    _REQUEST_TIMES.append(moment)
            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
                parsed = response.json()
                if not isinstance(parsed, dict) or ('Message' in parsed and 'Invalid' in str(parsed['Message'])):
                    raise ProviderUnavailable('Unexpected or unauthorized sports provider response')
                if transport is None:
                    RAW_CACHE[cache_key] = (monotonic(), parsed)
                return parsed
            except (httpx.HTTPError, ValueError):
                raise ProviderUnavailable('Sports provider request failed') from None

        requests = []
        for src_day in source_days:
            for sport in SOURCE_SPORTS:
                requests.append(('schedule', src_day.isoformat(), sport,
                                 get_json('eventsday.php', {'d': src_day.isoformat(), 's': sport})))
            requests.append(('tv', src_day.isoformat(), 'US',
                             get_json('eventstv.php', {'d': src_day.isoformat(), 'a': 'United_States'})))
        raw = await asyncio.gather(*(r[3] for r in requests), return_exceptions=True)
        successes = [(req, result) for req, result in zip(requests, raw) if isinstance(result, dict)]
        if not any(req[0] == 'schedule' for req, _ in successes):
            raise ProviderUnavailable('All sports schedule requests failed')
        errors = len(requests) - len(successes)
        tv: dict[str, list[dict[str, Any]]] = {}
        schedule_rows: dict[str, dict[str, Any]] = {}
        seen_tv_rows: dict[str, dict[str, Any]] = {}
        for (kind, src_day, _sport, _), result in successes:
            if kind == 'tv':
                rows = tv_records(result)
                channels = broadcast_rows(rows, allow_missing_country=True)
                for eid, items in channels.items():
                    merge_channels(tv.setdefault(eid, []), items)
                # TV-first recovery when an event was truncated by daily schedule limits.
                for row in rows:
                    eid = str(row.get('idEvent') or '')
                    if eid and eid in channels:
                        seen_tv_rows[eid] = row
            else:
                for row in records(result, 'events'):
                    eid = str(row.get('idEvent') or '').strip()
                    if eid:
                        schedule_rows[eid] = row
        # Resolve TV-first records to full event metadata, but cap traffic.
        orphans = sorted(set(seen_tv_rows) - set(schedule_rows))
        orphan_limit = min(2 if is_free else 6, _lookup_limit(is_free))
        if orphans and orphan_limit:
            ids = orphans[:orphan_limit]
            details = await asyncio.gather(
                *(get_json('lookupevent.php', {'id': eid}) for eid in ids),
                return_exceptions=True)
            for eid, payload in zip(ids, details):
                if isinstance(payload, dict):
                    items = records(payload, 'events')
                    if items:
                        schedule_rows[eid] = items[0]
                else:
                    errors += 1
        # Bounded lookups enrich the main schedule when the truncated free daily
        # TV listing omitted events. Never infer broadcasters from rights deals.
        lookup_budget = _lookup_limit(is_free)
        absent_tv_ids = sorted(set(schedule_rows) - set(tv))[:lookup_budget]
        if absent_tv_ids:
            semaphore = asyncio.Semaphore(6)
            async def lookup(eid: str):
                async with semaphore:
                    return await get_json('lookuptv.php', {'id': eid})
            responses = await asyncio.gather(*(lookup(eid) for eid in absent_tv_ids), return_exceptions=True)
            for eid, payload in zip(absent_tv_ids, responses):
                if isinstance(payload, dict):
                    for match, items in broadcast_rows(tv_records(payload)).items():
                        if match == eid:
                            merge_channels(tv.setdefault(eid, []), items)
                else:
                    errors += 1

    # Optional licensed supplier file can add ONLY explicitly sourced TV listings
    # to existing provider event IDs; never fabricate missing schedule events.
    partner = licensed_listings(day)
    partner_matches = 0
    for eid, entries in partner.items():
        if eid in schedule_rows:
            before = len(tv.get(eid, []))
            merge_channels(tv.setdefault(eid, []), entries)
            partner_matches += len(tv[eid]) - before

    unique = {}
    for eid, row in schedule_rows.items():
        event = normalize_event(row, tv)
        if event and is_on_local_day(event, day, tz):
            # Keep internal identities until multi-source reconciliation.
            # Removed before returning HTTP JSON to clients.
            unique[event['id']] = event
    events = sorted(unique.values(), key=lambda e: (e['startTime'] or '9999', e['sport'], e['title']))
    annotate_events(events)
    coverage = coverage_breakdown(events)
    distributions = distribution_summary(events)
    note = ('Provider supplied actual event times and channel names; empty channels mean not available from this source, '
            'not necessarily untelevised. This guide is not exhaustive.')
    if is_free:
        note = 'Public preview key heavily limits daily event and TV results. ' + note
    if partner_matches:
        note += f' {partner_matches} additional channel listings from configured licensed supplier data.'
    if errors:
        note += f' {errors} provider requests failed or returned unavailable data.'
    return {
        'date': day.isoformat(),
        'timezone': tz_name,
        'fetchedAt': datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
        'source': 'TheSportsDB v1 (limited public preview)' if is_free else 'TheSportsDB v1 (premium)',
        'partial': True,  # Not certified exhaustive, even with premium key.
        'coverage_note': note,
        'coverage': coverage,
        'distributionCoverage': distributions,
        'guideVersion': '0.3',
        'disclaimer': 'US source listings, not verified market availability, OTA reception, package carriage, or account entitlement.',
        'diagnostics': {'providerRequestFailures': errors, 'utcDaysQueried': len(source_days),
                        'tvDetailLookups': len(absent_tv_ids), 'tvOrphanLookups': min(len(orphans), orphan_limit),
                        'freePreview': is_free, 'licensedMatches': partner_matches},
        'events': events,
    }
