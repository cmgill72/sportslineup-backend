"""Deterministic v0.2 integration fixtures (not claims of live coverage)."""
from __future__ import annotations
import asyncio
import unittest
from datetime import date
from urllib.parse import parse_qs
from unittest.mock import patch
import httpx
from app.normalization import (tv_records, broadcast_rows, coverage_breakdown,
    is_on_local_day, normalize_event)
from app.provider import fetch_schedule, ProviderUnavailable
from zoneinfo import ZoneInfo

DAY = date(2026, 10, 23)


def sample_event(eid: str, stamp: str, sport: str = 'Basketball', league: str = 'NBA'):
    return {
        'idEvent': eid, 'strSport': sport, 'strLeague': league,
        'strEvent': 'Visitors vs Home', 'strTimestamp': stamp,
        'dateEvent': stamp[:10], 'strStatus': 'NS',
    }

class TVFixturesTests(unittest.TestCase):
    def test_real_v1_tv_payload_singular_key(self):
        payload = {'tvevent': [
            {'idEvent':'42','strChannel':'ABC','strCountry':'United States'},
            {'idEvent':'42','strChannel':'ABC','strCountry':'United States'},
            {'idEvent':'42','strChannel':'TSN','strCountry':'Canada'}
        ]}
        self.assertEqual(len(tv_records(payload)), 3)
        rows = broadcast_rows(tv_records(payload))
        self.assertEqual(len(rows['42']), 1)
        self.assertEqual(rows['42'][0]['confidence'], 'provider_listed')
        self.assertIsNone(rows['42'][0]['url'])

    def test_source_only_without_time_is_not_invented(self):
        e = normalize_event({'idEvent':'42','dateEvent':'2026-10-23',
                             'strSport':'Basketball','strLeague':'NBA','strEvent':'Test'})
        self.assertIsNone(e['startTime'])
        self.assertTrue(is_on_local_day(e, DAY, ZoneInfo('America/Chicago')))
        self.assertFalse(is_on_local_day(e, date(2026, 10, 24), ZoneInfo('America/Chicago')))

    def test_counts_describe_returned_data_not_season(self):
        ev = normalize_event(sample_event('42','2026-10-24T02:30:00Z'),
                             {'42': broadcast_rows([{'idEvent':'42','strChannel':'NBC','strCountry':'USA'}])['42']})
        matrix = coverage_breakdown([ev])
        self.assertEqual(matrix['sports']['nba']['withBroadcast'], 1)
        self.assertEqual(matrix['sports']['nhl']['events'], 0)

    def test_unknown_country_not_us_without_daily_filter(self):
        rows = broadcast_rows([{'idEvent':'18', 'strChannel':'Global Sports'}])
        self.assertEqual(rows, {})
        scoped = broadcast_rows([{'idEvent':'18', 'strChannel':'Global Sports'}], allow_missing_country=True)
        self.assertEqual(scoped['18'][0]['channel'], 'Global Sports')

    def test_midnight_fallback_is_tbd(self):
        e = normalize_event({'idEvent':'18','strSport':'Basketball','strLeague':'NBA',
                             'dateEvent':'2026-10-23','strTime':'00:00:00'})
        self.assertIsNone(e['startTime'])

    def test_source_day_shift(self):
        e = normalize_event(sample_event('42', '2026-10-24T02:30:00Z'))
        self.assertTrue(is_on_local_day(e, DAY, ZoneInfo('America/Chicago')))
        self.assertFalse(is_on_local_day(e, DAY, ZoneInfo('UTC')))


class PipelineTests(unittest.TestCase):
    def make_mock(self):
        requests = []
        def handler(request: httpx.Request):
            path = request.url.path.rsplit('/', 1)[-1]
            p = dict(request.url.params)
            requests.append((path, p))
            if path == 'eventsday.php':
                if p.get('s') != 'Basketball':
                    return httpx.Response(200, json={'events': None})
                if p['d'] == '2026-10-23':
                    events = [sample_event('77','2026-10-23T20:00:00Z')]
                elif p['d'] == '2026-10-24':
                    events = [sample_event('42','2026-10-24T02:30:00Z'),
                              sample_event('88','2026-10-24T22:00:00Z')]
                else: events = []
                return httpx.Response(200, json={'events': events})
            if path == 'eventstv.php':
                items = ([{'idEvent':'42','strChannel':'Peacock','strCountry':'United States'}]
                         if p['d'] == '2026-10-24' else [])
                # Actual provider structure uses `tvevent`, not `tvevents`.
                return httpx.Response(200, json={'tvevent': items})
            if path == 'lookuptv.php':
                eid = p['id']
                if eid == '77':
                    return httpx.Response(200, json={'tvevent': [
                        {'idEvent':'77','strChannel':'ESPN','strCountry':'USA'},
                        {'idEvent':'77','strChannel':'Sportsnet','strCountry':'Canada'}]})
                return httpx.Response(200, json={'tvevent': None})
            if path == 'lookupevent.php':
                return httpx.Response(200, json={'events': None})
            return httpx.Response(404, json={'error':'Unknown API route'})
        return httpx.MockTransport(handler), requests

    def test_actual_source_join_and_timezone(self):
        transport, requests = self.make_mock()
        data = asyncio.run(fetch_schedule(DAY, 'America/Chicago', transport=transport))
        self.assertEqual(data['timezone'], 'America/Chicago')
        self.assertTrue(data['partial'])
        ids = [e['id'] for e in data['events']]
        self.assertEqual(ids, ['tsdb-77','tsdb-42'])
        channels = {e['id']: [b['channel'] for b in e['broadcasts']] for e in data['events']}
        self.assertEqual(channels['tsdb-42'], ['Peacock'])
        self.assertEqual(channels['tsdb-77'], ['ESPN'])
        self.assertEqual(data['coverage']['withBroadcast'], 2)
        self.assertEqual(data['coverage']['sports']['nba']['events'], 2)
        self.assertIn(('eventstv.php', {'d':'2026-10-24','a':'United_States'}), requests)
        self.assertGreaterEqual(len([r for r in requests if r[0] == 'lookuptv.php']), 1)

    def test_no_unverified_rights_from_missing_lookups(self):
        transport, _ = self.make_mock()
        data = asyncio.run(fetch_schedule(date(2026, 10, 24), 'America/Chicago', transport=transport))
        missing = [e for e in data['events'] if e['id'] == 'tsdb-88']
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]['broadcasts'], [])

    def test_all_schedule_endpoints_down(self):
        def fail(_request): return httpx.Response(503, json={'error':'down'})
        with self.assertRaises(ProviderUnavailable):
            asyncio.run(fetch_schedule(DAY, transport=httpx.MockTransport(fail)))

    def test_tv_only_orphan_recovered_with_event_detail(self):
        def handler(req):
            q = dict(req.url.params)
            route = req.url.path.split('/')[-1]
            if route == 'eventstv.php':
                return httpx.Response(200, json={'tvevent': [
                    {'idEvent':'500','strChannel':'CBS','strCountry':'USA'}] if q['d']=='2026-10-23' else []})
            if route == 'eventsday.php': return httpx.Response(200, json={'events': []})
            if route == 'lookupevent.php':
                return httpx.Response(200, json={'events': [sample_event('500','2026-10-23T19:00:00Z','Ice Hockey','NHL')]})
            return httpx.Response(200, json={'tvevent': []})
        data = asyncio.run(fetch_schedule(DAY, transport=httpx.MockTransport(handler)))
        self.assertEqual(data['events'][0]['sport'], 'nhl')
        self.assertEqual(data['events'][0]['broadcasts'][0]['channel'], 'CBS')

if __name__ == '__main__': unittest.main()
