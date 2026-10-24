"""Optional, bounded on-demand badge discovery for personal testing or cleared art.

TheSportsDB provides links to third-party team marks but *not* the associated
trademark or copyright licenses. Disabled until explicitly enabled in Render.
No image is downloaded/hosted here; only an allowlisted URL is returned.
"""
from __future__ import annotations
import asyncio
from time import monotonic
import re
from urllib.parse import quote
import httpx
from .live_scores import allow_team_badges, safe_badge
from .provider import _client_key

CACHE: dict[str, tuple[float, dict]] = {}
LOCK = asyncio.Lock()
TIMES: list[float] = []
TTL = 86400


def normalize_name(name: str) -> str:
    return re.sub(r'[^a-z0-9]', '', name.casefold())


def select_badge(name: str, data: object) -> str | None:
    if not isinstance(data, dict) or not isinstance(data.get('teams'), list):
        return None
    target = normalize_name(name)
    exact = [record for record in data['teams'] if isinstance(record, dict)
             and normalize_name(str(record.get('strTeam') or '')) == target]
    if len(exact) == 1:
        return safe_badge(exact[0].get('strBadge') or exact[0].get('strTeamBadge'))
    # TheSportsDB sometimes lists a school's mascot in the canonical name while
    # the schedule only lists the school. Never guess if multiple candidates match.
    safe_prefix = [record for record in data['teams'] if isinstance(record, dict)
                   and normalize_name(str(record.get('strTeam') or '')).startswith(target)
                   and 4 <= len(target)]
    if not exact and len(safe_prefix) == 1:
        return safe_badge(safe_prefix[0].get('strBadge') or safe_prefix[0].get('strTeamBadge'))
    return None


async def badge_lookup(name: str, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    name = name.strip()
    if not (2 <= len(name) <= 72) or not re.fullmatch(r"[\w .&'()+-]+", name, re.UNICODE):
        return {'status': 'invalid', 'badgeURL': None}
    if not allow_team_badges():
        return {'status': 'disabled', 'badgeURL': None}
    key = normalize_name(name)
    if not key:
        return {'status': 'invalid', 'badgeURL': None}
    cached = CACHE.get(key)
    now = monotonic()
    if cached and now - cached[0] < TTL:
        return cached[1]
    async with LOCK:
        now = monotonic()
        if key in CACHE and now - CACHE[key][0] < TTL:
            return CACHE[key][1]
        TIMES[:] = [t for t in TIMES if now-t < 60]
        if len(TIMES) >= 20:
            return {'status': 'rate_limited', 'badgeURL': None}
        TIMES.append(now)
    provider_key = quote(_client_key(), safe='')
    try:
        async with httpx.AsyncClient(transport=transport, timeout=8.0, follow_redirects=False) as client:
            response = await client.get(f'https://www.thesportsdb.com/api/v1/json/{provider_key}/searchteams.php',
                                        params={'t':name})
            if response.status_code != 200:
                return {'status':'unavailable', 'badgeURL':None}
            url = select_badge(name, response.json())
    except (ValueError, httpx.HTTPError):
        return {'status':'unavailable', 'badgeURL':None}
    result = {'status':'found' if url else 'not_found', 'badgeURL':url}
    if transport is None:
        CACHE[key] = (monotonic(), result)
        if len(CACHE) > 3000:
            CACHE.pop(next(iter(CACHE)))
    return result
