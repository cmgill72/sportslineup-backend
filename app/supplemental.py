"""Opt-in, best-effort schedule adapters for exploratory single-user v0.4 builds.

MLB and NHL feeds are publicly reachable league endpoints, ESPN is undocumented.
Public reachability is NOT a redistributable license. All are OFF until separately
explicitly enabled by environment variable and reviewed for permitted usage.
No channel assignment is fabricated. Each provider independently fails open.
"""
from __future__ import annotations
import asyncio
import os
from datetime import date, timedelta, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo
import httpx

SOURCES = ('mlb', 'nhl', 'espn')
ESPN_LEAGUES = {
    'nfl': ('football', 'nfl'),
    'nba': ('basketball', 'nba'),
    'mlb': ('baseball', 'mlb'),
    'ncaa_football': ('football', 'college-football'),
    'ncaa_basketball': ('basketball', 'mens-college-basketball'),
    'ncaa_womens_basketball': ('basketball', 'womens-college-basketball'),
    'nhl': ('hockey', 'nhl'),
    'pga': ('golf', 'pga'),
    'wnba': ('basketball', 'wnba'),
    'ufc': ('mma', 'ufc'),
}


def enabled_providers() -> list[str]:
    return [p for p in SOURCES if os.getenv(f'SPORTSLINEUP_ENABLE_{p.upper()}', '').lower() in ('1','true','yes')]


def _utc_datetime(value: Any) -> str | None:
    try:
        if not isinstance(value, str) or not value:
            return None
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            return None  # Never guess an implicit timezone from another feed.
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    except (TypeError, ValueError, OverflowError):
        return None


def _day_matches(utc_stamp: str | None, day: date, zone: ZoneInfo, source_date: str | None = None) -> bool:
    if utc_stamp:
        return datetime.fromisoformat(utc_stamp.replace('Z', '+00:00')).astimezone(zone).date() == day
    return source_date == day.isoformat()


def _event(source: str, identifier: Any, sport: str, league: str, title: str, start: str | None,
           status: str = 'scheduled', participants: list[str] | None = None,
           broadcasts: list[dict[str, Any]] | None = None, source_date: str | None = None) -> dict[str, Any] | None:
    if not str(identifier).strip() or not title.strip():
        return None
    return {
        'id': f'{source}-{sport}-{identifier}', 'sport': sport, 'league': league,
        'title': title.strip(), 'startTime': start,
        'status': status, 'venue': None,
        'broadcasts': broadcasts or [], 'scheduleSources': [source.upper() if source != 'espn' else 'ESPN (experimental)'],
        '_participants': participants or [], '_sourceDay': source_date or '',
    }


def _status(value: str | None) -> str:
    state = (value or '').lower()
    if state in ('final','off','completed','game over','post','postgame'): return 'final'
    if state in ('live','in','in progress','inprogress','mid','intermission','halftime'): return 'live'
    if state in ('postponed','post','delay','delayed'): return 'postponed' if state != 'post' else 'final'
    if state in ('cancelled','canceled'): return 'cancelled'
    return 'scheduled'


def parse_mlb(payload: dict[str, Any], day: date, tz: str) -> list[dict[str, Any]]:
    zone = ZoneInfo(tz)
    result = []
    for block in payload.get('dates', []) if isinstance(payload.get('dates'), list) else []:
        for game in block.get('games', []) if isinstance(block, dict) else []:
            if not isinstance(game, dict): continue
            teams = game.get('teams') or {}
            away = ((teams.get('away') or {}).get('team') or {}).get('name') or ''
            home = ((teams.get('home') or {}).get('team') or {}).get('name') or ''
            if not (away and home): continue
            start = _utc_datetime(game.get('gameDate'))
            if not _day_matches(start, day, zone, block.get('date')): continue
            raw_state = game.get('status') or {}
            status = _status(str(raw_state.get('abstractGameState') or raw_state.get('detailedState') or ''))
            item = _event('mlb', game.get('gamePk'), 'mlb', 'MLB', f'{away} at {home}', start,
                          status=status, participants=[away, home], source_date=block.get('date'))
            if item: result.append(item)
    return result


def _nhl_tv_broadcasts(game: dict[str, Any]) -> list[dict[str, Any]]:
    """Use only actual US network names on this exact NHL game record.

    For example the NHL game feed may contain tvBroadcasts with countryCode,
    network and market. We must not interpret Canadian/international channels
    as US options, or a regional broadcast as available to every ZIP code.
    """
    result: list[dict[str, Any]] = []
    for item in game.get('tvBroadcasts', []) if isinstance(game.get('tvBroadcasts'), list) else []:
        if not isinstance(item, dict): continue
        if str(item.get('countryCode') or '').upper() != 'US': continue
        network = str(item.get('network') or '').strip()
        if not network or len(network) > 90 or any(r['channel'].casefold() == network.casefold() for r in result):
            continue
        result.append({
            'id': f"nhl-tv-{game.get('id')}-{len(result)}", 'channel': network,
            'type': 'source_listing', 'confidence': 'provider_listed',
            'source': 'NHL (opt-in)', 'country': 'US', 'url': None,
            'marketScope': str(item.get('market') or 'unspecified'),
        })
    return result


def parse_nhl(payload: dict[str, Any], day: date, tz: str) -> list[dict[str, Any]]:
    zone = ZoneInfo(tz)
    result = []
    for block in payload.get('gameWeek', []) if isinstance(payload.get('gameWeek'), list) else []:
        for game in block.get('games', []) if isinstance(block, dict) else []:
            if not isinstance(game, dict): continue
            def name(team: dict[str, Any]) -> str:
                place = ((team.get('placeName') or {}).get('default') or '').strip()
                common = ((team.get('commonName') or {}).get('default') or '').strip()
                return ' '.join(filter(None, [place, common])) or (team.get('abbrev') or '')
            away = name(game.get('awayTeam') or {})
            home = name(game.get('homeTeam') or {})
            if not (away and home): continue
            start = _utc_datetime(game.get('startTimeUTC'))
            if not _day_matches(start, day, zone, block.get('date')): continue
            state = str(game.get('gameState') or '').upper()
            status = 'final' if state in ('FINAL','OFF') else 'live' if state in ('LIVE','CRIT') else 'scheduled'
            item = _event('nhl', game.get('id'), 'nhl', 'NHL', f'{away} at {home}', start,
                          status=status, participants=[away, home], source_date=block.get('date'),
                          broadcasts=_nhl_tv_broadcasts(game))
            if item: result.append(item)
    return result


def _espn_broadcasts(comp: dict[str, Any], event_id: str) -> list[dict[str, Any]]:
    """Only literal network names in that game's own broadcasts list; no mapping of rights deals.
    Never convert a geoBroadcast region to a nationwide entitlement.
    """
    out = []
    for item in comp.get('broadcasts', []) if isinstance(comp.get('broadcasts'), list) else []:
        if not isinstance(item, dict): continue
        names = item.get('names') or []
        if not isinstance(names, list): continue
        for raw in names:
            name = str(raw).strip()
            if not name or len(name) > 90 or any(r['channel'].casefold() == name.casefold() for r in out): continue
            out.append({
                'id': f'espn-broadcast-{event_id}-{len(out)}',
                'channel': name, 'type': 'source_listing', 'confidence': 'provider_listed',
                'source': 'ESPN (experimental)', 'country': 'US (market not verified)', 'url': None,
                'marketScope': str(item.get('market') or 'unspecified'),
            })
    return out


def parse_espn(payload: dict[str, Any], sport: str, day: date, tz: str) -> list[dict[str, Any]]:
    zone = ZoneInfo(tz)
    result = []
    for row in payload.get('events', []) if isinstance(payload.get('events'), list) else []:
        if not isinstance(row, dict): continue
        comps = row.get('competitions') or []
        competition = comps[0] if isinstance(comps, list) and comps and isinstance(comps[0], dict) else {}
        start = _utc_datetime(row.get('date') or competition.get('date'))
        if not _day_matches(start, day, zone): continue
        competitors = competition.get('competitors') or []
        names = [str((c.get('team') or {}).get('displayName') or (c.get('athlete') or {}).get('displayName') or '').strip()
                 for c in competitors if isinstance(c, dict)]
        names = [x for x in names if x]
        # Avoid interpreting golf event competitors as a 2-team matchup.
        title = str(row.get('name') or row.get('shortName') or '').strip()
        if not title and len(names) == 2: title = f'{names[0]} vs {names[1]}'
        if not title: continue
        st = competition.get('status') or row.get('status') or {}
        status_type = st.get('type') or {}
        status = 'final' if status_type.get('completed') is True else _status(status_type.get('state') or status_type.get('name'))
        # Only take competition-specific broadcasts for this particular event.
        eid = str(row.get('id') or '')
        broadcasts = _espn_broadcasts(competition, eid)
        item = _event('espn', eid, sport, sport.upper(), title, start,
                      status=status, participants=names if len(names) == 2 else [], broadcasts=broadcasts)
        if item: result.append(item)
    return result


async def _fetch(client: httpx.AsyncClient, url: str, params: dict | None = None) -> dict:
    response = await client.get(url, params=params)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict): raise ValueError('Provider returned invalid JSON shape')
    return data


async def supplemental_schedule(day: date, tz: str, transport: httpx.AsyncBaseTransport | None = None,
                                providers: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Fetch optional supplementals independently; report provider status per league.
    Returns (events, source_status). No invented schedule rows on failure.
    """
    configured = enabled_providers() if providers is None else providers
    configured = [x for x in configured if x in SOURCES]
    events: list[dict] = []
    health: list[dict] = []
    if not configured: return events, health
    async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=5), transport=transport,
                                 follow_redirects=True, headers={'Accept': 'application/json', 'User-Agent': 'SportsLineup/0.4 personal-prototype'}) as client:
        requests: list[tuple[str, str, Any]] = []
        if 'mlb' in configured:
            requests.append(('MLB StatsAPI', 'mlb', _fetch(client, 'https://statsapi.mlb.com/api/v1/schedule',
                                                          {'sportId': 1, 'startDate': (day - timedelta(days=1)).isoformat(),
                                                           'endDate': (day + timedelta(days=1)).isoformat()})))
        if 'nhl' in configured:
            requests.append(('NHL Web API', 'nhl', _fetch(client, f'https://api-web.nhle.com/v1/schedule/{day.isoformat()}')))
        if 'espn' in configured:
            for sport, (group, league) in ESPN_LEAGUES.items():
                params: dict[str, str] = {'dates': day.strftime('%Y%m%d'), 'limit': '500'}
                if sport == 'ncaa_football': params['groups'] = '80'  # FBS only; not full NCAA.
                if sport in ('ncaa_basketball', 'ncaa_womens_basketball'): params['groups'] = '50'  # D-I group, source-dependent.
                requests.append(('ESPN (experimental)', sport, _fetch(
                    client, f'https://site.api.espn.com/apis/site/v2/sports/{group}/{league}/scoreboard', params)))
        responses = await asyncio.gather(*(x[2] for x in requests), return_exceptions=True)
        for (name, sport, _), response in zip(requests, responses):
            if isinstance(response, Exception):
                health.append({'provider': name, 'sport': sport, 'status': 'failed', 'events': 0})
                continue
            try:
                if name == 'MLB StatsAPI': rows = parse_mlb(response, day, tz)
                elif name == 'NHL Web API': rows = parse_nhl(response, day, tz)
                else: rows = parse_espn(response, 'ncaa_basketball' if sport == 'ncaa_womens_basketball' else sport, day, tz)
                if sport == 'ncaa_womens_basketball':
                    for row in rows: row['league'] = "NCAA Women's Basketball"
                events.extend(rows)
                health.append({'provider': name, 'sport': sport, 'status': 'ok' if rows else 'empty', 'events': len(rows)})
            except (KeyError, ValueError, TypeError, AttributeError):
                health.append({'provider': name, 'sport': sport, 'status': 'invalid_response', 'events': 0})
    return events, health
