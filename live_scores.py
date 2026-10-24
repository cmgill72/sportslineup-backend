"""Optional premium TheSportsDB v2 livescores. Does not infer TV rights or guess scores.

Match score feeds by stable TheSportsDB event ID only. Ignore ambiguous or
foreign provider identifiers. All external art is disabled until explicitly
opted in after rights review. Provider tokens never leave the backend.
"""
from __future__ import annotations
import os
import re
from datetime import datetime, timezone
from time import monotonic
from typing import Any
from urllib.parse import urlsplit
import httpx

URL = 'https://www.thesportsdb.com/api/v2/json/livescore/all'
CACHE: tuple[float, dict[str, Any]] | None = None
TTL = 105  # roughly two-minute updates, with a little headroom


def premium_enabled() -> bool:
    return bool(os.getenv('SPORTSDB_API_KEY', '').strip() not in ('', '123')) and os.getenv('SPORTSLINEUP_ENABLE_LIVE_SCORES', 'true').lower() == 'true'


def allow_team_badges() -> bool:
    return os.getenv('SPORTSLINEUP_ENABLE_TEAM_BADGES', 'false').lower() == 'true'


def safe_badge(value: Any) -> str | None:
    if not allow_team_badges() or not isinstance(value, str) or len(value) > 512:
        return None
    parsed = urlsplit(value)
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or host not in {'www.thesportsdb.com', 'thesportsdb.com', 'r2.thesportsdb.com'} or not parsed.path.startswith('/images/'):
        return None
    return value


def _score(value: Any) -> int | None:
    if value is None or value == '' or isinstance(value, bool):
        return None
    if isinstance(value, (int, str)) and re.fullmatch(r'\d{1,3}', str(value).strip()):
        return int(value)
    return None


def _rows(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        raise ValueError('not_json_object')
    for field in ('livescores', 'livescore', 'events', 'scores'):
        result = payload.get(field)
        if isinstance(result, list):
            return [row for row in result if isinstance(row, dict)]
        if isinstance(result, dict):
            return [result]
    # Some providers return a null field for an empty feed. Do not call it an error.
    if any(field in payload and payload[field] is None for field in ('livescores', 'livescore', 'events', 'scores')):
        return []
    raise ValueError('unknown_score_shape')


def score_map(payload: Any) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for item in _rows(payload):
        event_id = str(item.get('idEvent') or '').strip()
        if not re.fullmatch(r'\d{4,12}', event_id):
            continue
        home, away = _score(item.get('intHomeScore')), _score(item.get('intAwayScore'))
        # Scores always come as a pair. Do not invent a 0 for missing entries.
        if home is None or away is None:
            continue
        progress = str(item.get('strProgress') or '').strip()[:70]
        raw_status = str(item.get('strStatus') or '').strip().lower()
        final = progress.lower().startswith(('final','ft','full time')) or raw_status in ('match finished', 'final','finished')
        live = not final and (bool(progress) or raw_status in ('live','in progress','in_progress'))
        # Some feeds include unspecified/preview scores; only display known live or final rows.
        if not final and not live:
            continue
        mapping['tsdb-' + event_id] = {
            'home': home, 'away': away, 'progress': progress if progress else ('Final' if final else 'In progress'),
            'state': 'final' if final else 'live', 'source': 'TheSportsDB V2 livescore',
            'homeTeam': str(item.get('strHomeTeam') or '').strip()[:100],
            'awayTeam': str(item.get('strAwayTeam') or '').strip()[:100],
            'homeBadgeURL': safe_badge(item.get('strHomeTeamBadge')),
            'awayBadgeURL': safe_badge(item.get('strAwayTeamBadge')),
        }
    return mapping


def attach_scores(events: list[dict[str, Any]], scores: dict[str, dict[str, Any]]) -> int:
    attached = 0
    for event in events:
        event_id = str(event.get('id') or '')
        record = scores.get(event_id)
        if not record:
            continue
        participants = [re.sub(r'[^a-z0-9]', '', p.casefold()) for p in event.get('_participants', []) if p]
        # When schedule contains both teams, verify names from the score feed.
        if len(participants) == 2 and record.get('homeTeam') and record.get('awayTeam'):
            expected = {re.sub(r'[^a-z0-9]', '', name.casefold()) for name in (record['homeTeam'], record['awayTeam'])}
            if set(participants) != expected:
                continue
        event['score'] = {k: record[k] for k in ('home','away','progress','state','source')}
        event['score']['homeTeam'] = record.get('homeTeam') or None
        event['score']['awayTeam'] = record.get('awayTeam') or None
        if record['state'] == 'final':
            event['status'] = 'final'
        elif record['state'] == 'live':
            event['status'] = 'live'
        if allow_team_badges():
            event['teamIdentity'] = {
                'home': record.get('homeTeam') or '', 'away': record.get('awayTeam') or '',
                'homeBadgeURL': record.get('homeBadgeURL'), 'awayBadgeURL': record.get('awayBadgeURL'),
                'artworkSource': 'TheSportsDB; check third-party trademark rights',
            }
        attached += 1
    return attached


async def fetch_livescores(transport: httpx.AsyncBaseTransport | None = None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    global CACHE
    if not premium_enabled():
        return {}, {'status': 'disabled_or_no_premium_key', 'matched': 0}
    now = monotonic()
    if transport is None and CACHE and now - CACHE[0] < TTL:
        return CACHE[1]['scores'], CACHE[1]['health']
    key = os.environ['SPORTSDB_API_KEY'].strip()
    try:
        async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(9, connect=4), follow_redirects=False) as client:
            response = await client.get(URL, headers={'X-API-KEY': key, 'Accept': 'application/json'})
            if response.status_code == 429:
                return {}, {'status': 'rate_limited', 'matched': 0}
            if response.status_code in (401, 403):
                return {}, {'status': 'authentication_failed', 'matched': 0}
            if response.status_code != 200:
                return {}, {'status': 'upstream_error', 'matched': 0}
            scores = score_map(response.json())
            result = {'scores': scores, 'health': {'status': 'ok', 'feedRecords': len(scores)}}
            if transport is None:
                CACHE = (monotonic(), result)
            return scores, result['health']
    except (httpx.TimeoutException, httpx.HTTPError):
        return {}, {'status': 'network_error', 'matched': 0}
    except (ValueError, KeyError):
        return {}, {'status': 'invalid_response', 'matched': 0}
