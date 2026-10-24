#!/usr/bin/env python3
"""SportsLineup v0.5 non-secret live coverage audit.

Usage: python3 Backend/scripts/audit_v05.py --url https://sportslineup-api.onrender.com
       python3 Backend/scripts/audit_v05.py --date 2026-10-23 --days 1

Reports RETURNED event counts and broadcaster gaps, never asserts full coverage.
"""
import argparse
import json
from datetime import date, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


def get(base, path, query=None):
    suffix = ('?' + urlencode(query)) if query else ''
    request = Request(base.rstrip('/') + path + suffix, headers={'Accept':'application/json'})
    with urlopen(request, timeout=65) as response:
        return json.load(response)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--url', default='https://sportslineup-api.onrender.com')
    parser.add_argument('--date', default=date.today().isoformat())
    parser.add_argument('--days', type=int, default=1)
    parser.add_argument('--tz', default='America/Chicago')
    args=parser.parse_args()
    if not args.url.startswith('https://'):
        parser.error('The public backend address must begin with https://')
    if args.days not in range(1,8):
        parser.error('--days must be between 1 and 7')
    try: start=date.fromisoformat(args.date)
    except ValueError: parser.error('Use YYYY-MM-DD')
    try:
        health=get(args.url,'/health')
        print(f"Backend: {health.get('status')} | version {health.get('version')}")
        for i in range(args.days):
            day=(start+timedelta(days=i)).isoformat()
            gap=get(args.url, '/v1/coverage/gaps',{'date':day,'tz':args.tz})
            print(f"\n{day} | returned={gap.get('returnedEvents')} | unlisted={gap.get('missingUSListings')} | corroborated={gap.get('eventsWithMultiSourceListing')} | stale={gap.get('stale')}")
            for sport, metrics in (gap.get('bySport') or {}).items():
                print(f"  {sport:18s} {metrics.get('withUSListing',0):3d}/{metrics.get('returnedEvents',0):3d} listed  ({metrics.get('listingRatePercent')}%)")
            for warning in gap.get('warnings') or []:
                print('  !', warning)
    except (URLError, HTTPError, TimeoutError, ValueError) as exc:
        print(f'Unable to complete audit: {type(exc).__name__}: {exc}')
        raise SystemExit(1)


if __name__=='__main__': main()
