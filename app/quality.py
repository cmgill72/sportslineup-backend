"""Evidence-aware coverage reporting. Counts apply to returned events only."""
from __future__ import annotations
from typing import Any
from .normalization import SPORTS


def quality_report(events: list[dict[str, Any]], health: list[dict[str, Any]],
                   diagnostics: dict[str, Any], stale: bool = False) -> dict[str, Any]:
    by_sport = {}
    missing = 0
    corroborated = 0
    for sport in SPORTS:
        subset = [row for row in events if row.get('sport') == sport]
        listed = sum(bool(row.get('broadcasts')) for row in subset)
        multi = sum(any(len(set(b.get('sources') or ([b.get('source')] if b.get('source') else []))) >= 2
                        for b in row.get('broadcasts', [])) for row in subset)
        by_sport[sport] = {
            'returnedEvents': len(subset), 'withUSListing': listed,
            'missingUSListing': len(subset) - listed, 'multiSourceListing': multi,
            'listingRatePercent': round(100 * listed / len(subset)) if subset else None,
        }
        missing += len(subset) - listed
        corroborated += multi
    failures = [f"{row.get('provider','Unknown')}: {row.get('status')}" for row in health
                if row.get('status') in ('failed','invalid_response')]
    warnings = []
    if missing: warnings.append(f'{missing} returned events lack US broadcaster listings.')
    skipped = int(diagnostics.get('tvLookupSkippedByBudget') or 0)
    if skipped: warnings.append(f'{skipped} events were not individually checked due to lookup limits.')
    if failures: warnings.append('One or more schedule sources failed; returned events may be incomplete.')
    if stale: warnings.append('The response contains last known data; check current information.')
    warnings.append('Source listings do not prove local carriage, subscription access or blackout eligibility.')
    return {
        'definition': 'Broadcast coverage of returned events, not all scheduled or televised events',
        'returnedEvents': len(events), 'missingUSListings': missing,
        'eventsWithMultiSourceListing': corroborated,
        'bySport': by_sport, 'sourceFailures': failures, 'warnings': warnings,
    }
