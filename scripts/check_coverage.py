#!/usr/bin/env python3
"""Read-only coverage probe for a running SportsLineup v0.4 backend.

Usage: python Backend/scripts/check_coverage.py --url https://YOUR.onrender.com \
           --days 7 --timezone America/Chicago --output coverage_report.md

The printed counts are provider-returned events, not complete league schedules.
No API secrets or phone subscription preferences are uploaded by this script.
"""
import argparse
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SPORTS = [('nba','NBA'),('mlb','MLB'),('ncaa_football','NCAA Football'),
          ('ncaa_basketball','NCAA Basketball'),('nhl','NHL'),('pga','PGA Tour'),
          ('wnba','WNBA'),('ufc','UFC')]


def main():
    parser = argparse.ArgumentParser(description='SportsLineup real provider coverage check')
    parser.add_argument('--url', required=True, help='HTTPS deployed SportsLineup API base URL')
    parser.add_argument('--days', type=int, default=7, help='Number of days to inspect (1–14)')
    parser.add_argument('--timezone', default='America/Chicago', help='IANA timezone identifier')
    parser.add_argument('--output', default='coverage_report.md')
    args = parser.parse_args()
    if not 1 <= args.days <= 14: parser.error('--days must be 1–14')
    if not args.url.startswith('https://'): parser.error('--url must use HTTPS')
    start = datetime.now(ZoneInfo(args.timezone)).date()
    totals = {key: [0, 0] for key, _ in SPORTS}
    error_days = []
    source_failures = {}
    extra_added = extra_matched = extra_channels = 0
    stale_dates = []
    scanned_days = 0
    for n in range(args.days):
        day = (start + timedelta(days=n)).isoformat()
        params = urlencode({'date': day, 'tz': args.timezone})
        url = f"{args.url.rstrip('/')}/v1/schedule?{params}"
        try:
            with urlopen(Request(url, headers={'Accept': 'application/json'}), timeout=65) as res:
                data = json.load(res)
            cov = data.get('coverage', {}).get('sports', {})
            for sport, _ in SPORTS:
                row = cov.get(sport, {})
                totals[sport][0] += int(row.get('events', 0))
                totals[sport][1] += int(row.get('withBroadcast', 0))
            for row in data.get('sourceHealth', []):
                if row.get('status') in ('failed', 'invalid_response'):
                    key = f"{row.get('provider', 'unknown')} [{row.get('sport', 'all')}]"
                    source_failures[key] = source_failures.get(key, 0) + 1
            metrics = data.get('reconciliation') or {}
            extra_added += int(metrics.get('added', 0))
            extra_matched += int(metrics.get('matched', 0))
            extra_channels += int(metrics.get('channelsAdded', 0))
            if data.get('stale'): stale_dates.append(day)
            scanned_days += 1
            print(f"{day}: {len(data.get('events', []))} provider-returned events; "
                  f"{data.get('coverage', {}).get('withBroadcast', 0)} with TV channels")
        except Exception as exc:
            error_days.append(day)
            print(f'{day}: request unavailable ({type(exc).__name__})')
    lines = [f'# SportsLineup v0.4 — provider coverage audit ({start.isoformat()})', '',
             f'- Requested window: {args.days} day(s), starting {start.isoformat()}',
             f'- Days successfully scanned: {scanned_days}',
             f'- Device/timezone interpretation: `{args.timezone}`',
             '- Provider-returned subset ONLY: **not** total league schedule or complete TV coverage.',
             '', '| Sport | Returned events | With broadcaster listed | Without listing |',
             '|---|---:|---:|---:|']
    for sport, label in SPORTS:
        all_count, listed_count = totals[sport]
        lines.append(f'| {label} | {all_count} | {listed_count} | {all_count-listed_count} |')
    lines += ['', '## Multi-source diagnostics', '',
              f'- Supplemental returned events added: {extra_added}',
              f'- Strongly matched cross-source events: {extra_matched}',
              f'- Additional literal channel names: {extra_channels}',
              f'- Stale guide days: {", ".join(stale_dates) if stale_dates else "none"}',
              '', '| Unavailable source | Failed days |', '|---|---:|']
    for source, failed in sorted(source_failures.items()):
        lines.append(f'| {source} | {failed} |')
    if not source_failures:
        lines.append('| None reported | 0 |')
    if error_days:
        lines += ['', f"Days with provider errors: {', '.join(error_days)}"]
    lines += ['', '## Important limitations', '',
              'Zero is **not** evidence of no scheduled events. TheSportsDB public preview key truncates results; optional sources may be disabled, undocumented or have incomplete sports/broadcast coverage.',
              'Network names come from source data; no regional reception or subscription access verified.',
              'For publication, confirm source commercial licensing and attribution requirements.']
    Path(args.output).write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'Wrote {args.output}')


if __name__ == '__main__': main()
