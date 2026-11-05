"""v0.9.8: exact-ID, source-only event lineups and timelines.

This endpoint adds event-scoped detail to the existing verified team statistics
and competitor results. An event lineup is NOT a per-player statistical box score;
a provider timeline is NOT necessarily complete play-by-play. Missing values stay
missing; no inferred scoring or home/away attribution.
"""
from __future__ import annotations

import asyncio
from collections import deque
from time import monotonic
import os
from typing import Any

import httpx

from .box_scores import EVENT_ID, V2_BASE, _text, _number, _rows

CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
CACHE_TTL = 150
CACHE_MAX = 150
_CALL_TIMES: deque[float] = deque()
_CALL_LOCK = asyncio.Lock()
MAX_LOOKUPS_PER_MINUTE = 10


def _booleanish(value: Any) -> bool:
    return str(value).strip().lower() in {'yes', 'true', '1', 'y'}


def normalize_lineup(payload: Any, event_id: str) -> list[dict[str, str | bool | None]]:
    result: list[dict[str, str | bool | None]] = []
    seen = set()
    for row in _rows(payload, 'lookup', 'lineup', 'lineups')[:200]:
        if _text(row.get('idEvent')) != event_id:
            continue
        player = _text(row.get('strPlayer'), 80)
        if not player:
            continue
        team = _text(row.get('strTeam'), 70) or None
        team_id = _text(row.get('idTeam'), 24) or None
        position = _text(row.get('strPosition'), 40) or None
        number = _text(row.get('intSquadNumber'), 6) or None
        substitute = _booleanish(row.get('strSubstitute'))
        # No invented designation when substitute flag is missing.
        is_substitute = substitute if row.get('strSubstitute') not in (None, '') else None
        identity = (_text(row.get('idPlayer')), player.casefold(), team_id or team, position)
        if identity in seen:
            continue
        seen.add(identity)
        result.append({'id': f'{event_id}-lineup-{len(result)}', 'name': player,
                       'team': team, 'teamId': team_id, 'position': position,
                       'number': number, 'isSubstitute': is_substitute})
        if len(result) >= 100:
            break
    return result


# Classification only for explicitly named score actions. Other timeline rows
# remain game events, not fabricated scoring plays.
_SCORING_ACTIONS = {'goal', 'penalty goal', 'own goal', 'touchdown',
                    'field goal', 'safety', 'home run', 'run scored', 'try',
                    'two point conversion', '2-point conversion'}


def normalize_timeline(payload: Any, event_id: str) -> list[dict[str, str | bool | None]]:
    result: list[dict[str, str | bool | None]] = []
    seen = set()
    for row in _rows(payload, 'lookup', 'timeline', 'timelines')[:250]:
        if _text(row.get('idEvent')) != event_id:
            continue
        action = _text(row.get('strTimeline'), 64)
        if not action:
            continue
        detail = _text(row.get('strTimelineDetail'), 120) or _text(row.get('strComment'), 120) or None
        player = _text(row.get('strPlayer'), 80) or None
        team = _text(row.get('strTeam'), 70) or None
        period = _text(row.get('strPeriod'), 24) or None
        minute = _number(row.get('intTime')) or None
        identity = (_text(row.get('idTimeline')), action, minute, player, team, detail)
        if identity in seen:
            continue
        seen.add(identity)
        score_action = action.casefold().strip() in _SCORING_ACTIONS
        result.append({'id': f'{event_id}-timeline-{len(result)}', 'kind': action,
                       'detail': detail, 'player': player, 'team': team,
                       'period': period, 'time': minute, 'isScoring': score_action})
        if len(result) >= 100:
            break
    # Source order is retained: minute or period may not be comparable across sports.
    return result


def _empty_reply(event_id: str, status: str, note: str) -> dict[str, Any]:
    return {'eventId': event_id, 'source': 'TheSportsDB Premium V2', 'status': status,
            'teamStats': [], 'results': [], 'lineups': [], 'timeline': [],
            'coverage': {'teamStats': status, 'results': status,
                         'lineups': status, 'timeline': status}, 'note': note}


async def fetch_game_details(app_event_id: str, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    if not EVENT_ID.fullmatch(app_event_id):
        return _empty_reply(app_event_id, 'unsupported', 'Detailed game data require an exact TheSportsDB event ID.')
    key = os.getenv('SPORTSDB_API_KEY', '').strip()
    if not key or key == '123':
        return _empty_reply(app_event_id, 'not_configured', 'Premium game details require SPORTSDB_API_KEY on Render.')
    if transport is None:
        cached = CACHE.get(app_event_id)
        if cached and monotonic() - cached[0] < CACHE_TTL:
            return dict(cached[1])
        async with _CALL_LOCK:
            now = monotonic()
            while _CALL_TIMES and now - _CALL_TIMES[0] >= 60:
                _CALL_TIMES.popleft()
            if len(_CALL_TIMES) >= MAX_LOOKUPS_PER_MINUTE:
                return _empty_reply(app_event_id, 'rate_limited', 'Detailed-game requests are temporarily limited to protect your API quota.')
            _CALL_TIMES.append(now)

    event_id = EVENT_ID.fullmatch(app_event_id).group(1)  # type: ignore[union-attr]
    # Concurrent four provider lookups (two established + two new), with a
    # bounded request budget and fixed paths. Mock transport supports QA.
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(9, connect=4), transport=transport,
        headers={'X-API-KEY': key, 'Accept': 'application/json'},
        follow_redirects=False,
    ) as client:
        async def lookup(kind: str) -> tuple[dict[str, Any] | None, str]:
            try:
                reply = await client.get(f'{V2_BASE}/{kind}/{event_id}')
                if reply.status_code in (401, 403):
                    return None, 'auth_failed'
                if reply.status_code == 429:
                    return None, 'rate_limited'
                if reply.status_code != 200:
                    return None, 'unavailable'
                body = reply.json()
                return (body, 'ok') if isinstance(body, dict) else (None, 'unavailable')
            except (httpx.HTTPError, ValueError):
                return None, 'unavailable'

        (stats_payload, stats_status), (results_payload, results_status), \
        (lineup_payload, lineup_status), (timeline_payload, timeline_status) = await asyncio.gather(
            lookup('event_stats'), lookup('event_results'),
            lookup('event_lineup'), lookup('event_timeline'))

    from .box_scores import normalize_stats, normalize_results
    team_stats = normalize_stats(stats_payload, event_id)
    competitors = normalize_results(results_payload, event_id)
    lineups = normalize_lineup(lineup_payload, event_id)
    timeline = normalize_timeline(timeline_payload, event_id)
    statuses = (stats_status, results_status, lineup_status, timeline_status)
    sections = (team_stats, competitors, lineups, timeline)
    coverage = {label: 'available' if rows else 'no_data' if status == 'ok' else status
                for label, rows, status in zip(('teamStats', 'results', 'lineups', 'timeline'), sections, statuses)}
    if any(sections):
        status = 'available'
        note = ('Only source-reported event statistics, competitor results, lineups and timeline actions are shown. '
                'Lineups are not player box scores; timelines may be incomplete.')
    elif 'auth_failed' in statuses:
        status, note = 'auth_failed', 'TheSportsDB rejected the premium credentials configured on Render.'
    elif 'rate_limited' in statuses:
        status, note = 'rate_limited', 'TheSportsDB temporarily limited detailed-game requests.'
    elif all(s == 'ok' for s in statuses):
        status, note = 'no_data', 'TheSportsDB returned no detailed statistics, lineup or timeline for this game.'
    else:
        status, note = 'unavailable', 'Detailed-game data are temporarily unavailable or incomplete.'
    result: dict[str, Any] = {
        'eventId': app_event_id, 'source': 'TheSportsDB Premium V2',
        'status': status, 'teamStats': team_stats, 'results': competitors,
        'lineups': lineups, 'timeline': timeline, 'coverage': coverage,
        'note': note,
    }
    if transport is None and status in ('available', 'no_data'):
        if len(CACHE) >= CACHE_MAX:
            CACHE.pop(next(iter(CACHE)))
        CACHE[app_event_id] = (monotonic(), result)
    return result
