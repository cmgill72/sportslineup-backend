"""V0.7.3: Verified broadcast manifest overlay.

No live website scraping. Bundled editor-reviewed fixtures are verified against
official published schedules, and an optional LOCAL JSON manifest can be
updated on Render from legally authorized sources. This feed never guesses
broadcasts from league rights, and it cannot replace the principal provider.
"""
from __future__ import annotations
from datetime import date, datetime
from pathlib import Path
import json, os, re
from .normalization import merge_channels

_SOURCE = Path(__file__).with_name('verified_fixtures.json')
# Only verified aliases for schools actually represented in our fixture manifests.
# Do not fuzzy-match across opponents, conferences, or partial names.
_ALIASES = {
    'lsutigers': 'lsu', 'louisianastate': 'lsu',
    'auburntigers': 'auburn', 'alabamacrimsontide': 'alabama',
    'texasamaggies': 'texasam', 'texasamaggiesfootball': 'texasam',
    'olemissrebels': 'olemiss', 'texaslonghorns': 'texas',
    'floridagators': 'florida', 'georgiabulldogs': 'georgia',
}


def _normalize_name(value: str) -> str:
    token = re.sub(r'[^a-z0-9]', '', str(value).casefold())
    return _ALIASES.get(token, token)


def _teams_from_title(title: str) -> set[str]:
    parts = re.split(r'\s+(?:vs\.?|versus|at|@)\s+', str(title), flags=re.IGNORECASE)
    return {_normalize_name(s) for s in parts} if len(parts) == 2 else set()


def _start_near(observed: str | None, official: str, tolerance_minutes: int = 45) -> bool:
    if not observed: return False
    try:
        left = datetime.fromisoformat(observed.replace('Z','+00:00'))
        right = datetime.fromisoformat(official.replace('Z','+00:00'))
        if left.tzinfo is None or right.tzinfo is None: return False
        return abs((left - right).total_seconds()) <= tolerance_minutes * 60
    except (ValueError, TypeError):
        return False


def _read_manifest(path: Path) -> list[dict]:
    try:
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            return []
        content = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(content, list) or len(content) > 1000: return []
        return [row for row in content if isinstance(row, dict)]
    except (OSError, ValueError):
        return []


def _manifest_rows() -> list[dict]:
    records = _read_manifest(_SOURCE)
    extra = os.getenv('SPORTSLINEUP_VERIFIED_LISTINGS_PATH','').strip()
    if extra:
        # File must already exist on the server. NEVER fetch a URL supplied by
        # the client or automatically scrape websites from API requests.
        records.extend(_read_manifest(Path(extra)))
    return records


def apply_verified_listings(events: list[dict], day: date) -> dict[str, int]:
    """Overlay editor-verified date/sport/team/time listings; no invented networks.

    Sports events missing a start timestamp are intentionally not eligible.
    Other providers' listings are retained for transparency (no overwrite).
    """
    matched = added = 0
    used: set[tuple[str,str]] = set()
    for row in _manifest_rows():
        if row.get('date') != day.isoformat(): continue
        sport = row.get('sport')
        matchup = row.get('matchup')
        expected = {_normalize_name(s) for s in matchup} if isinstance(matchup,list) and len(matchup)==2 else set()
        if len(expected) != 2 or not isinstance(row.get('utcStart'), str): continue
        for event in events:
            if event.get('sport') != sport: continue
            if _teams_from_title(event.get('title') or '') != expected: continue
            if not _start_near(event.get('startTime'), row['utcStart']): continue
            ident=(str(event.get('id')), str(row.get('date')))
            if ident in used: continue
            fresh = []
            for i, entry in enumerate(row.get('broadcasts') or []):
                if not isinstance(entry,dict): continue
                channel = str(entry.get('channel') or '').strip()
                evidence = str(entry.get('evidenceURL') or '')
                src = str(entry.get('source') or '').strip()
                if not channel or not src or not evidence.startswith('https://'): continue
                fresh.append({'id': f'official-{day.isoformat()}-{sport}-{i}',
                              'channel':channel,'type':'source_listing',
                              'confidence':'provider_listed',
                              'source':src,'sources':[src],'country':'US',
                              'url':None,'marketScope':entry.get('marketScope'),
                              'evidenceURL':evidence,'verifiedOn':entry.get('verifiedOn'),
                              'listingAuthority':'official_published_schedule'})
            if fresh:
                before = len(event.get('broadcasts') or [])
                existing = event.setdefault('broadcasts',[])
                merge_channels(existing,fresh)
                added += len(existing)-before
                # Put officially published broadcasts ahead of unverified
                # aggregator ones on compact single-channel game rows.
                confirmed={b['channel'].casefold().strip() for b in fresh}
                existing.sort(key=lambda b: 0 if str(b.get('channel','')).casefold().strip() in confirmed else 1)
                matched += 1
                used.add(ident)
            break  # Never attach a single fixture to multiple same-title events.
    return {'matchedEvents':matched,'channelsAdded':added}
