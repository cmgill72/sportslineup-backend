"""Optional, user-licensed local broadcast overlay, joined ONLY by provider event ID.

There is no scraped or unauthorized feed in this package. A deployer who owns
rights to a broadcast metadata feed can transform those records into this JSON
schema and provide its file path via SPORTSLINEUP_LICENSED_LISTINGS_PATH.

A feed row must include `providerEventId`, ISO `date` (local day in query TZ),
`channel`, `country` = US, and `source` identifying the authorized supplier.
The partner adds an airing LISTING, not a proof of subscriber availability.
"""
from __future__ import annotations
from datetime import date
import json
import os
from pathlib import Path
from typing import Any
from .normalization import merge_channels

MAX_BYTES = 2_000_000


def licensed_listings(day: date) -> dict[str, list[dict[str, Any]]]:
    path_value = os.getenv('SPORTSLINEUP_LICENSED_LISTINGS_PATH', '').strip()
    if not path_value:
        return {}
    path = Path(path_value)
    try:
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            return {}
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, list):
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for item in raw[:5000]:
        if not isinstance(item, dict):
            continue
        if item.get('date') != day.isoformat() or str(item.get('country', '')).upper() not in ('US', 'USA'):
            continue
        event_id = str(item.get('providerEventId', '')).strip()
        channel = str(item.get('channel', '')).strip()
        source = str(item.get('source', '')).strip()
        if not event_id.isdecimal() or not (1 <= len(channel) <= 90) or not (2 <= len(source) <= 90):
            continue
        item_row = {'id': f'licensed-{event_id}-{len(out.get(event_id, []))}',
                    'channel': channel, 'type': 'source_listing',
                    'confidence': 'provider_listed', 'source': source,
                    'country': 'US', 'url': None}
        merge_channels(out.setdefault(event_id, []), [item_row])
    return out
