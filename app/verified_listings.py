"""Small manually verified game-specific corrections from official team pages.

NOT a general broadcast data feed. Only listed local dates, exact participants,
sport, and start timestamps can match; never infer a team's other games.
"""
from __future__ import annotations
from datetime import date, datetime
from pathlib import Path
import json, re
from .normalization import merge_channels

_SOURCE = Path(__file__).with_name('verified_fixtures.json')


def _normalize_name(value: str) -> str:
    return re.sub(r'[^a-z0-9]', '', str(value).casefold())


def _teams_from_title(title: str) -> set[str]:
    parts = re.split(r'\s+(?:vs\.?|versus|at|@)\s+', str(title), flags=re.IGNORECASE)
    return {_normalize_name(s) for s in parts} if len(parts) == 2 else set()


def _start_near(observed: str | None, official: str, tolerance_minutes: int = 60) -> bool:
    if not observed: return False
    try:
        left = datetime.fromisoformat(observed.replace('Z','+00:00'))
        right = datetime.fromisoformat(official.replace('Z','+00:00'))
        return abs((left - right).total_seconds()) <= tolerance_minutes * 60
    except (ValueError, TypeError):
        return False


def apply_verified_listings(events: list[dict], day: date) -> dict[str, int]:
    """Strict date/sport/team/time match. Never reassign or replace provider data."""
    try:
        rows = json.loads(_SOURCE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'matchedEvents': 0, 'channelsAdded': 0}
    matched = added = 0
    for row in rows:
        if row.get('date') != day.isoformat(): continue
        expected = {_normalize_name(s) for s in row.get('matchup', [])}
        if len(expected) != 2: continue
        for event in events:
            if event.get('sport') != row.get('sport'): continue
            if _teams_from_title(event.get('title') or '') != expected: continue
            if not _start_near(event.get('startTime'), row.get('utcStart')): continue
            fresh = []
            for i, entry in enumerate(row.get('broadcasts') or []):
                if not entry.get('source') or not entry.get('evidenceURL'): continue
                fresh.append({'id': f'official-{row["sport"]}-{i}',
                              'channel': entry['channel'], 'type': 'source_listing',
                              'confidence': 'provider_listed', 'source': entry['source'],
                              'sources': [entry['source']], 'country': 'US', 'url': None,
                              'marketScope': entry.get('marketScope'),
                              'evidenceURL': entry.get('evidenceURL'),
                              'verifiedOn': entry.get('verifiedOn')})
            if fresh:
                before = len(event.get('broadcasts') or [])
                merge_channels(event.setdefault('broadcasts', []), fresh)
                added += len(event['broadcasts']) - before
                matched += 1
            # Do not apply the same fixture to multiple potential duplicates.
            break
    return {'matchedEvents': matched, 'channelsAdded': added}
