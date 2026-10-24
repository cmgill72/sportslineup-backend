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

# Public diagnostics must never include the credential or a full URL embedding it.
class ProviderUnavailable(RuntimeError):
    def __init__(self, message: str, code: str = 'upstream_unavailable'):
        super().__init__(message)
        self.code = code


def credential_mode() -> str:
    return 'public_preview' if _client_key() == '123' else 'configured'


PROBE_CACHE: dict[str, Any] = {}
PROBE_TTL = 300


async def probe_provider(transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """One short, cached diagnostic request; NEVER returns the provider key."""
    key = _client_key()
    cached = PROBE_CACHE.get('latest')
    if transport is None and cached and cached[0] == key and monotonic() - cached[1] < PROBE_TTL:
        return dict(cached[2])
    # Endpoint and query are innocuous. Avoid exposing a URL containing the key in logs/responses.
    path = f'{BASE}/{quote(key, safe="")}/eventsday.php'
    result: dict[str, Any] = {'provider': 'TheSportsDB', 'credentialMode': credential_mode(),
                              'credentialPresent': key != '123', 'status': 'unavailable',
                              'upstreamStatus': None}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5), transport=transport,
                                     headers={'Accept': 'application/json'}) as client:
            response = await client.get(path, params={'d': date.today().isoformat(), 's': 'Basketball'})
            result['upstreamStatus'] = response.status_code
            if response.status_code == 429:
                result['status'] = 'rate_limited'
            elif response.status_code in (401, 403):
                result['status'] = 'authentication_failed'
            elif response.status_code >= 500:
                result['status'] = 'upstream_error'
            elif response.status_code >= 400:
                # TheSportsDB v1 can reject an invalid premium key with HTTP 400.
                try:
                    body = response.json()
                    invalid_key = isinstance(body, dict) and any(
                        'invalid' in str(body.get(name, '')).lower() and
                        'key' in str(body.get(name, '')).lower()
                        for name in ('Message','message','error'))
                except ValueError:
                    invalid_key = False
                result['status'] = 'authentication_failed' if invalid_key else 'request_rejected'
            else:
                try:
                    payload = response.json()
                    if not isinstance(payload, dict):
                        result['status'] = 'invalid_response'
                    elif any('invalid' in str(payload.get(field, '')).lower()
                             for field in ('Message','message','error')):
                        result['status'] = 'authentication_failed'
                    elif 'events' in payload:
                        result['status'] = 'ok'
                    else:
                        result['status'] = 'unexpected_payload'
                except ValueError:
                    result['status'] = 'invalid_response'
    except httpx.TimeoutException:
        result['status'] = 'timeout'
    except httpx.HTTPError:
        result['status'] = 'network_error'
    if transport is None:
        PROBE_CACHE['latest'] = (key, monotonic(), dict(result))
    return result


def _lookup_limit(is_free: bool) -> int:
    default = 3 if is_free else 40
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
                if response.status_code == 429:
                    raise ProviderUnavailable('Sports data provider rate limited', 'rate_limited')
                if response.status_code in (401, 403):
                    raise ProviderUnavailable('Sports data provider rejected credentials', 'authentication_failed')
                if 400 <= response.status_code < 500:
                    try:
                        body = response.json()
                        invalid_key = isinstance(body, dict) and any(
                            'invalid' in str(body.get(name, '')).lower() and
                            'key' in str(body.get(name, '')).lower()
                            for name in ('Message', 'message', 'error'))
                    except ValueError:
                        invalid_key = False
                    if invalid_key:
                        raise ProviderUnavailable('Sports provider rejected the API key', 'authentication_failed')
                    raise ProviderUnavailable('Sports data provider rejected the request', 'request_rejected')
                if response.status_code >= 500:
                    raise ProviderUnavailable('Sports data provider upstream error', 'upstream_error')
                parsed = response.json()
                if not isinstance(parsed, dict):
                    raise ProviderUnavailable('Unexpected sports provider response', 'invalid_response')
                if any('invalid' in str(parsed.get(field, '')).lower() for field in ('Message', 'message', 'error')):
                    raise ProviderUnavailable('Sports provider rejected the API key', 'authentication_failed')
                if transport is None:
                    RAW_CACHE[cache_key] = (monotonic(), parsed)
                return parsed
            except httpx.TimeoutException:
                raise ProviderUnavailable('Sports provider request timed out', 'timeout') from None
            except httpx.HTTPError:
                raise ProviderUnavailable('Sports provider network error', 'network_error') from None
            except ValueError:
                raise ProviderUnavailable('Sports provider returned non-JSON data', 'invalid_response') from None

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
            failed = [item for item in raw if isinstance(item, ProviderUnavailable)]
            # Prioritize actionable provider errors, without echoing upstream URLs or keys.
            codes = [item.code for item in failed]
            priority = ('authentication_failed', 'rate_limited', 'request_rejected',
                        'timeout', 'upstream_error', 'network_error', 'invalid_response')
            code = next((value for value in priority if value in codes), 'upstream_unavailable')
            raise ProviderUnavailable('All sports schedule requests failed', code)
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
        # Coverage v0.4.4: previous builds selected only the first 16 IDs in
        # lexicographic order, including games outside the requested US-local
        # date. This starved the games users were actually looking at.
        # Build the eligible queue from THIS day's supported sports, then
        # round-robin sports while giving live/upcoming games priority.
        lookup_budget = _lookup_limit(is_free)
        candidates_by_sport: dict[str, list[tuple[str, dict[str, Any]]]] = {name: [] for name in SPORTS}
        for eid, row in schedule_rows.items():
            if eid in tv:
                continue
            event = normalize_event(row)
            if event and is_on_local_day(event, day, tz):
                candidates_by_sport[event['sport']].append((eid, event))

        def candidate_priority(item: tuple[str, dict[str, Any]]) -> tuple:
            _eid, event = item
            return (0 if event['status'] == 'live' else 1 if event['status'] == 'scheduled' else 2,
                    event.get('startTime') or '9999', _eid)

        for queue in candidates_by_sport.values():
            queue.sort(key=candidate_priority)
        eligible_tv_lookups = sum(map(len, candidates_by_sport.values()))
        chosen: list[str] = []
        while len(chosen) < lookup_budget:
            added = False
            for sport in SPORTS:
                queue = candidates_by_sport[sport]
                if queue and len(chosen) < lookup_budget:
                    chosen.append(queue.pop(0)[0])
                    added = True
            if not added:
                break
        absent_tv_ids = chosen
        tv_lookup_with_us = 0
        tv_lookup_with_foreign_only = 0
        tv_lookup_empty = 0
        tv_lookup_failed = 0
        if absent_tv_ids:
            semaphore = asyncio.Semaphore(6)
            async def lookup(eid: str):
                async with semaphore:
                    return await get_json('lookuptv.php', {'id': eid})
            responses = await asyncio.gather(*(lookup(eid) for eid in absent_tv_ids), return_exceptions=True)
            for eid, payload in zip(absent_tv_ids, responses):
                if isinstance(payload, dict):
                    all_rows = tv_records(payload)
                    us_rows = broadcast_rows(all_rows)
                    listed = us_rows.get(eid, [])
                    if listed:
                        before = len(tv.get(eid, []))
                        merge_channels(tv.setdefault(eid, []), listed)
                        if len(tv[eid]) > before:
                            tv_lookup_with_us += 1
                    elif all_rows:
                        tv_lookup_with_foreign_only += 1
                    else:
                        tv_lookup_empty += 1
                else:
                    tv_lookup_failed += 1
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
    if eligible_tv_lookups > len(absent_tv_ids):
        note += f' {eligible_tv_lookups - len(absent_tv_ids)} games not individually checked due to TV lookup budget.'
    if tv_lookup_empty or tv_lookup_with_foreign_only:
        note += f' {tv_lookup_empty + tv_lookup_with_foreign_only} event lookups returned no US listing.'
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
                        'tvDetailLookups': len(absent_tv_ids), 'tvLookupEligible': eligible_tv_lookups,
                        'tvLookupSkippedByBudget': eligible_tv_lookups - len(absent_tv_ids),
                        'tvLookupWithUSListings': tv_lookup_with_us,
                        'tvLookupOnlyNonUS': tv_lookup_with_foreign_only,
                        'tvLookupNoListings': tv_lookup_empty, 'tvLookupFailed': tv_lookup_failed,
                        'tvOrphanLookups': min(len(orphans), orphan_limit),
                        'freePreview': is_free, 'licensedMatches': partner_matches},
        'events': events,
    }
