"""v0.2 event/broadcast normalization. Never infer a broadcast from TV rights history.

TheSportsDB v1 TV endpoints use a *singular* `tvevent` payload key even when
returning an array. Keep this distinct from schedule `events` and TV-first rows.
"""
from __future__ import annotations
from datetime import datetime, timezone, date
from typing import Any
from zoneinfo import ZoneInfo

SPORTS = ('nba', 'mlb', 'ncaa_football', 'ncaa_basketball', 'nhl', 'pga', 'wnba', 'ufc')
COLLEGE_TERMS = ('ncaa', 'college', 'division i', 'ncaab', 'ncaaf', 'big ten', 'big 12', 'sec ', 'acc ', 'pac-12', 'pac-10', 'mountain west', 'atlantic 10', 'big east', 'american athletic', 'sun belt', 'conference usa')
US_NAMES = {'united states', 'united states of america', 'usa', 'us', 'u s a'}


def classify(raw: dict[str, Any]) -> str | None:
    sport = str(raw.get('strSport') or '').casefold()
    league = str(raw.get('strLeague') or '').casefold()
    title = str(raw.get('strEvent') or '').casefold()
    combined = f'{league} {title}'
    if 'basketball' in sport:
        if 'wnba' in combined or "women's national basketball association" in combined:
            return 'wnba'
        if any(k in combined for k in COLLEGE_TERMS):
            return 'ncaa_basketball'
        if ('nba' in combined or 'national basketball association' in combined) and 'g league' not in combined:
            return 'nba'
    if 'baseball' in sport and any(k in combined for k in ('mlb', 'major league baseball', 'world series')):
        return 'mlb'
    if ('american football' in sport or sport == 'football') and any(k in combined for k in COLLEGE_TERMS):
        return 'ncaa_football'
    if ('ice hockey' in sport or sport == 'hockey') and ('nhl' in combined or 'national hockey league' in combined):
        return 'nhl'
    if 'golf' in sport and any(k in combined for k in ('pga', 'fedexcup', 'fedex cup', 'the players championship')):
        return 'pga'
    if any(k in sport for k in ('fighting', 'martial arts', 'mixed martial arts', 'mma')) and any(k in combined for k in ('ufc', 'ultimate fighting championship')):
        return 'ufc'
    return None


def timestamp(raw: dict[str, Any]) -> str | None:
    value = str(raw.get('strTimestamp') or raw.get('strTimeStamp') or '').strip()
    if not value:
        date_value = str(raw.get('dateEvent') or '').strip()
        time_value = str(raw.get('strTime') or '').strip()
        if not date_value or not time_value or time_value in ('00:00', '00:00:00'):
            # Many sports feeds use midnight as an unknown-time placeholder.
            return None
        value = f'{date_value}T{time_value}'
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    except ValueError:
        return None


def is_on_local_day(event: dict[str, Any], day: date, tz: ZoneInfo) -> bool:
    if not event['startTime']:
        # Timeless event is only included on its provider-provided date (if present),
        # not assigned an imaginary start time.
        return event.get('_sourceDay') == day.isoformat()
    dt = datetime.fromisoformat(event['startTime'].replace('Z', '+00:00'))
    return dt.astimezone(tz).date() == day


def records(payload: Any, *preferred: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in preferred:
        value = payload.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    return []


def tv_records(payload: Any) -> list[dict[str, Any]]:
    # The actual documented/observed v1 format is {"tvevent": [...]}; retaining
    # plural fallback enables future compatible sources.
    return records(payload, 'tvevent', 'tvevents', 'tv', 'eventstv')


def broadcast_rows(raw: list[dict[str, Any]], source: str = 'TheSportsDB', allow_missing_country: bool = False) -> dict[str, list[dict[str, Any]]]:
    matches: dict[str, list[dict[str, Any]]] = {}
    for entry in raw:
        country = str(entry.get('strCountry') or '').casefold().replace('_', ' ').strip()
        if (country and country not in US_NAMES) or (not country and not allow_missing_country):
            continue
        event_id = str(entry.get('idEvent') or '').strip()
        channel = str(entry.get('strChannel') or '').strip()
        if not event_id or not channel:
            continue
        matches.setdefault(event_id, [])
        if any(x['channel'].casefold() == channel.casefold() for x in matches[event_id]):
            continue
        matches[event_id].append({
            'id': f'tv-{event_id}-{len(matches[event_id])}',
            'channel': channel,
            'type': 'source_listing',
            'confidence': 'provider_listed',
            'source': source,
            'country': country.upper() if country else 'US (date feed filtered)',
            'url': None,
        })
    return matches


def normalize_event(raw: dict[str, Any], broadcasts: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any] | None:
    sport = classify(raw)
    event_id = str(raw.get('idEvent') or '').strip()
    if not sport or not event_id:
        return None
    title = str(raw.get('strEvent') or '').strip()
    if not title:
        home = str(raw.get('strHomeTeam') or '').strip()
        away = str(raw.get('strAwayTeam') or '').strip()
        title = f'{away} vs. {home}' if home and away else 'Event (title unavailable)'
    state = str(raw.get('strStatus') or '').upper().strip()
    if state in ('FT', 'AET', 'PEN', 'AOT', 'AP', 'FINAL'):
        status = 'final'
    elif state in ('PST', 'POST', 'POSTPONED'):
        status = 'postponed'
    elif state in ('CANC', 'ABD', 'CANCELLED'):
        status = 'cancelled'
    elif state not in ('', 'NS', 'TBD', 'SCHEDULED'):
        status = 'live'
    else:
        status = 'scheduled'
    return {
        'id': f'tsdb-{event_id}',
        'sport': sport,
        'league': str(raw.get('strLeague') or sport.upper()),
        'title': title,
        'startTime': timestamp(raw),
        'status': status,
        'venue': str(raw.get('strVenue') or '').strip() or None,
        'broadcasts': list((broadcasts or {}).get(event_id, [])),
        '_sourceDay': str(raw.get('dateEvent') or '').strip(),
        '_participants': [str(raw.get('strAwayTeam') or '').strip(), str(raw.get('strHomeTeam') or '').strip()] if raw.get('strAwayTeam') and raw.get('strHomeTeam') else [],
        'scheduleSources': ['TheSportsDB'],
    }


def merge_channels(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve independent listing evidence instead of discarding it on de-dup.

    Matching is deliberately conservative: punctuation such as '+' remains part
    of a network name (ESPN and ESPN+ are NOT the same broadcaster). The number
    of named providers is not a claim of entitlement or local availability.
    """
    def key(channel: str) -> str:
        return ' '.join(str(channel).casefold().split())

    indexed = {key(row.get('channel','')): row for row in existing}
    for row in incoming:
        channel_key = key(row.get('channel',''))
        if not channel_key:
            continue
        source = str(row.get('source') or '').strip()
        if channel_key in indexed:
            saved = indexed[channel_key]
            evidence = list(saved.get('sources') or ([saved['source']] if saved.get('source') else []))
            extra = list(row.get('sources') or ([source] if source else []))
            saved['sources'] = list(dict.fromkeys(evidence + extra))
        else:
            copy = dict(row)
            copy['sources'] = list(dict.fromkeys(row.get('sources') or ([source] if source else [])))
            existing.append(copy)
            indexed[channel_key] = copy
    return existing


def coverage_breakdown(events: list[dict[str, Any]]) -> dict[str, Any]:
    sports = {}
    for name in SPORTS:
        subset = [e for e in events if e['sport'] == name]
        with_tv = sum(bool(e['broadcasts']) for e in subset)
        sports[name] = {'events': len(subset), 'withBroadcast': with_tv,
                        'withoutBroadcast': len(subset) - with_tv}
    return {'events': len(events), 'withBroadcast': sum(bool(e['broadcasts']) for e in events),
            'withoutBroadcast': sum(not e['broadcasts'] for e in events), 'sports': sports}
