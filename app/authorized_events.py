"""Optional, contract-authorized FULL event feed for all eight sport categories.

Unlike the v0.3 add-only TV file, these records may create their own events.
Requires the deployer to possess the appropriate schedule/broadcast data license.
Missing or invalid files fail loudly in source status; no simulated events.
"""
from __future__ import annotations
from datetime import date
import json
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from .normalization import SPORTS
from .supplemental import _utc_datetime, _day_matches, _event

MAX_BYTES = 4_000_000


def authorized_events(day: date, tz: str) -> tuple[list[dict[str, Any]], dict | None]:
    path_name = os.getenv('SPORTSLINEUP_AUTHORIZED_EVENTS_PATH', '').strip()
    if not path_name:
        return [], None
    status = {'provider':'Authorized Event Feed', 'sport':'all', 'status':'failed', 'events':0}
    path = Path(path_name)
    try:
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            return [], status
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return [], status
    if not isinstance(raw, list):
        return [], status
    result = parse_authorized_rows(raw, day, tz)
    status['status'] = 'ok' if result else 'empty'
    status['events'] = len(result)
    return result, status


def parse_authorized_rows(raw: list, day: date, tz: str) -> list[dict[str, Any]]:
    """Shared strict normalizer for locally licensed and remote HTTPS feed rows."""
    result = []
    zone = ZoneInfo(tz)
    for row in raw[:10000]:
        if not isinstance(row,dict) or row.get('country') != 'US': continue
        sport = str(row.get('sport') or '')
        source = str(row.get('source') or '').strip()
        identifier = str(row.get('providerEventId') or '').strip()
        title = str(row.get('title') or '').strip()
        stamp = _utc_datetime(row.get('startTime'))
        day_label = str(row.get('date') or '')
        if sport not in SPORTS or not (2<=len(source)<=100) or not (1<=len(identifier)<=70) or not (3<=len(title)<=200):
            continue
        if (day_label and day_label != day.isoformat()) or not _day_matches(stamp,day,zone,day_label) or (not stamp and day_label!=day.isoformat()):
            continue
        participants = row.get('participants')
        participants = [str(n)[:100] for n in participants] if isinstance(participants,list) and len(participants)==2 else []
        listings = []
        for bc in row.get('broadcasts',[]) if isinstance(row.get('broadcasts'),list) else []:
            if not isinstance(bc,dict): continue
            channel = str(bc.get('channel') or '').strip()
            if 1<=len(channel)<=90 and not any(x['channel'].casefold()==channel.casefold() for x in listings):
                listings.append({'id':f'authorized-{sport}-{identifier}-{len(listings)}',
                                 'channel':channel,'type':'source_listing','confidence':'provider_listed',
                                 'source':source,'country':'US (specific viewer market unverified)',
                                 'url':None})
        event = _event('authorized', f'{sport}-{identifier}', sport,
                       str(row.get('league') or sport.upper())[:100], title, stamp,
                       status=str(row.get('status') or 'scheduled').lower(),
                       participants=participants, broadcasts=listings, source_date=day_label)
        if event:
            event['scheduleSources'] = [source]
            result.append(event)
    return result
