"""Conservative multi-source event reconciliation.

False duplicate elimination is worse than a duplicate card: only merge when two
independent records have the same sport, same two participant names, and close
scheduled UTC starts. Individual tournaments/cards without teams are matched
only by exact normalized title and very close UTC starts.
"""
from __future__ import annotations
import re
from datetime import datetime
from typing import Any
from .normalization import merge_channels


def _clean(value: str) -> str:
    value = value.lower().replace('&', 'and')
    value = re.sub(r'\b(the)\b', '', value)
    value = re.sub(r'\bny\b', 'new york', value)
    value = re.sub(r'\bla\b', 'los angeles', value)
    return re.sub(r'[^a-z0-9]+', '', value)


def _participants(event: dict[str, Any]) -> tuple[str, ...] | None:
    values = event.get('_participants') or []
    if len(values) != 2 or not all(isinstance(x, str) and _clean(x) for x in values):
        return None
    norm = [_clean(x) for x in values]
    return tuple(sorted(norm)) if norm[0] != norm[1] else None


def _minutes(a: str | None, b: str | None) -> float:
    if not a or not b: return float('inf')
    try:
        first = datetime.fromisoformat(a.replace('Z', '+00:00'))
        second = datetime.fromisoformat(b.replace('Z', '+00:00'))
        return abs((first - second).total_seconds()) / 60
    except (ValueError, TypeError):
        return float('inf')


def strong_match(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get('sport') != b.get('sport'): return False
    teams_a, teams_b = _participants(a), _participants(b)
    if teams_a and teams_b:
        # Date/time matters: same MLB teams can play a doubleheader on a day.
        return teams_a == teams_b and _minutes(a.get('startTime'), b.get('startTime')) <= 90
    if teams_a or teams_b:
        # Cannot safely combine if one provider lacks precise identities.
        return False
    title_a, title_b = _clean(a.get('title') or ''), _clean(b.get('title') or '')
    return bool(title_a and title_b and title_a == title_b and
                _minutes(a.get('startTime'), b.get('startTime')) <= 90)


def reconcile(primary: list[dict], supplements: list[dict]) -> tuple[list[dict], dict[str, int]]:
    merged = [dict(e, broadcasts=[dict(b) for b in e.get('broadcasts', [])],
                   scheduleSources=list(e.get('scheduleSources') or ['TheSportsDB'])) for e in primary]
    status = {'sourceEvents': len(supplements), 'matched': 0, 'added': 0, 'ambiguous': 0, 'channelsAdded': 0}
    for added in supplements:
        candidates = [existing for existing in merged if strong_match(existing, added)]
        # Existing supplemental may represent the same event; never merge into
        # multiple ambiguous candidates even if both have close timestamps.
        if len(candidates) == 1:
            original = candidates[0]
            # Additional safety: if two candidates from the same provider are
            # within 90 minutes, suppress automatic merge.
            sources = added.get('scheduleSources', [])
            if any(s in original.get('scheduleSources', []) for s in sources):
                status['ambiguous'] += 1
                merged.append(added)
                continue
            before = len(original.get('broadcasts') or [])
            merge_channels(original['broadcasts'], added.get('broadcasts') or [])
            status['channelsAdded'] += len(original['broadcasts']) - before
            original['scheduleSources'] = list(dict.fromkeys(original['scheduleSources'] + sources))
            status['matched'] += 1
            # Preserve stable primary event ID. If primary time missing, strong_match
            # would not have happened, so we NEVER guess/replace time.
        else:
            if len(candidates) > 1: status['ambiguous'] += 1
            merged.append(added)
            status['added'] += 1
    for item in merged:
        item.pop('_sourceDay', None)
        item.pop('_participants', None)
    return sorted(merged, key=lambda e: (e.get('startTime') or '9999', e['sport'], e['title'])), status
