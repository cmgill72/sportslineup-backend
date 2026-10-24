#!/usr/bin/env python3
"""Troubleshoot connection separately from broadcast coverage. No API secrets."""
from __future__ import annotations
import argparse
import json
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


def check(url: str) -> dict:
    req = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'SportsLineupDiagnostics/0.4.2'})
    try:
        with urlopen(req, timeout=65) as resp:
            code = resp.status
            body = resp.read(524288).decode('utf8', 'replace')
    except HTTPError as err:
        code = err.code
        body = err.read(4096).decode('utf8', 'replace')
    except (URLError, TimeoutError) as err:
        print('ERROR: Could not connect to Render:', err)
        return {'error': str(err)}
    try:
        data = json.loads(body)
    except ValueError:
        data = {'rawMessage': body[:180]}
    print('HTTP', code, url)
    if code != 200:
        print('Server error:', data.get('detail', data.get('rawMessage', 'unknown')))
    return {'status': code, 'data': data}


def main():
    parser = argparse.ArgumentParser(description='Test Render /health and /v1/schedule from your Mac')
    parser.add_argument('--url', default='https://sportslineup-api.onrender.com')
    parser.add_argument('--date', help='YYYY-MM-DD, default is today in target time zone')
    parser.add_argument('--timezone', default='America/Chicago')
    args = parser.parse_args()
    zone = ZoneInfo(args.timezone)
    day = args.date or datetime.now(zone).date().isoformat()
    base = args.url.rstrip('/')
    print('\nSportsLineup connectivity diagnostic (date', day, ')')
    health = check(base + '/health')
    if health.get('status') == 200:
        print('Backend version:', health['data'].get('version', 'unknown'))
        print('Data key mode:', health['data'].get('sportsdbKeyMode', 'not available in deployed backend'))
    upstream = check(base + '/v1/diagnostics/provider')
    if upstream.get('status') == 200:
        info = upstream['data']
        print('TheSportsDB key mode:', info.get('credentialMode', 'unknown'))
        print('TheSportsDB diagnostic:', info.get('status', 'unknown'))
        print('TheSportsDB upstream HTTP:', info.get('upstreamStatus', 'no response'))
        if info.get('status') == 'authentication_failed':
            print('ACTION: Review the TheSportsDB premium key on Render; never share it in chat.')
        if info.get('status') == 'rate_limited':
            print('ACTION: Wait for the provider limit to reset, then retry.')
    result = check(base + '/v1/schedule?' + urlencode({'date': day, 'tz': args.timezone}))
    if result.get('status') == 200:
        data = result['data']
        events = data.get('events', [])
        print('Returned events:', len(events))
        print('Events with TV labels:', sum(bool(e.get('broadcasts')) for e in events))
        print('Data source:', data.get('source'))
        print('Provider summary:')
        for row in data.get('sourceHealth', []):
            print(' ', row.get('provider'), row.get('sport'), row.get('status'), row.get('events'))
        if not events:
            print('WARNING: Connected, but no source events were supplied. This is a data coverage/provider issue, not an iPhone network issue.')
    print('\nIf /health is 200 but /v1/schedule is 502: inspect Render logs and SPORTSDB_API_KEY.')
    print('If /health is 404: deploy the included Backend to the Render service.')


if __name__ == '__main__':
    main()
