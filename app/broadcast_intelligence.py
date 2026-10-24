"""Conservative US broadcast classification for SportsLineup v0.3.

These values describe a named channel's *general distribution model*, not
rights to an individual event. A source-listed channel is not confirmed for the
user's postal code, package, antenna, or account. No inferred game channels.
"""
from __future__ import annotations
import re
from typing import Any

OTA_NETWORKS = {'ABC', 'CBS', 'NBC', 'FOX', 'THE CW', 'CW', 'PBS'}
STREAMING_LABELS = {
    'PEACOCK': 'Peacock', 'PARAMOUNT+': 'Paramount+', 'PRIME VIDEO': 'Prime Video',
    'AMAZON PRIME VIDEO': 'Prime Video', 'ESPN+': 'ESPN+', 'ESPN PLUS': 'ESPN+',
    'NETFLIX': 'Netflix', 'APPLE TV': 'Apple TV', 'APPLE TV+': 'Apple TV',
    'HBO MAX': 'HBO Max', 'MAX': 'HBO Max', 'DAZN': 'DAZN',
    'VICTORY+': 'Victory+', 'VICTORY PLUS': 'Victory+',
}
CABLE_LABELS = {
    'ESPN', 'ESPN2', 'ESPNU', 'ESPN U', 'ESPNEWS', 'TNT', 'TBS', 'TRUTV',
    'FS1', 'FS2', 'FOX SPORTS 1', 'FOX SPORTS 2', 'NBA TV', 'MLB NETWORK',
    'NHL NETWORK', 'GOLF CHANNEL', 'CBS SPORTS NETWORK', 'CBSSN',
    'BIG TEN NETWORK', 'BTN', 'SEC NETWORK', 'ACC NETWORK', 'NFL NETWORK',
    'USA NETWORK', 'TENNIS CHANNEL',
}
REGIONAL_LABELS = {
    'NESN', 'YES NETWORK', 'MSG', 'MSG NETWORK', 'SNY', 'MASN',
    'MARQUEE SPORTS NETWORK', 'ALTITUDE', 'ALTITUDE SPORTS',
    'SPACE CITY HOME NETWORK', 'SPORTSNET PITTSBURGH', 'ROOT SPORTS',
    'SPECTRUM SPORTSNET', 'SPECTRUM SPORTSNET LA', 'FANDUEL SPORTS NETWORK',
    'BALLY SPORTS', 'NBC SPORTS BOSTON', 'NBC SPORTS PHILADELPHIA',
    'NBC SPORTS CALIFORNIA', 'NBC SPORTS BAY AREA',
}
REGIONAL_PREFIXES = ('FANDUEL SPORTS NETWORK ', 'BALLY SPORTS ', 'NBC SPORTS REGIONAL ')
_ALLOWED_LINK_HOSTS: set[str] = set()  # Never synthesize broadcaster/watch URLs.


def clean_channel(channel: str) -> str:
    return re.sub(r'\s+', ' ', channel).strip()


def classify_channel(channel: str) -> dict[str, Any]:
    name = clean_channel(channel)
    key = name.upper()
    if key in OTA_NETWORKS:
        return dict(distribution='over_the_air_network', serviceName=key,
                    potentialAntenna=True, accessNote='Possible local antenna affiliate; this airing and reception are not verified.')
    if key in STREAMING_LABELS:
        return dict(distribution='streaming_service', serviceName=STREAMING_LABELS[key],
                    potentialAntenna=False, accessNote='Streaming subscription, plan and event rights are unverified; check with the service.')
    if key in REGIONAL_LABELS or key.startswith(REGIONAL_PREFIXES):
        return dict(distribution='regional_network', serviceName=name,
                    potentialAntenna=False, accessNote='Regional market rights, carriage and blackout rules are unverified.')
    if key in CABLE_LABELS:
        return dict(distribution='cable_network', serviceName=name,
                    potentialAntenna=False, accessNote='Requires the channel through an eligible TV package; carriage is unverified.')
    return dict(distribution='unknown', serviceName=name,
                potentialAntenna=False, accessNote='Distribution, market and access requirements are not verified.')


def annotate_broadcast(row: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(row)
    enriched.update(classify_channel(str(row.get('channel') or '')))
    enriched['availability'] = 'unverified_for_viewer'
    # 'confirmed' from another provider would misleadingly suggest personal access:
    # preserve reported provenance but never promote it to guaranteed access.
    return enriched


def annotate_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for event in events:
        event['broadcasts'] = [annotate_broadcast(b) for b in event.get('broadcasts', [])]
        event['broadcastStatus'] = 'source_listed' if event['broadcasts'] else 'not_listed_by_provider'
    return events


def distribution_summary(events: list[dict[str, Any]]) -> dict[str, int]:
    result = {k: 0 for k in ('over_the_air_network', 'cable_network', 'streaming_service', 'regional_network', 'unknown')}
    for event in events:
        for broadcast in event.get('broadcasts', []):
            key = broadcast.get('distribution', 'unknown')
            result[key if key in result else 'unknown'] += 1
    return result
