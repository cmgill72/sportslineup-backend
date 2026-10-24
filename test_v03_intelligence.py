"""v0.3 contract checks: classify known types without guessing viewing rights."""
from __future__ import annotations
import asyncio
import json
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch
import httpx
from app.broadcast_intelligence import classify_channel, annotate_events, distribution_summary
from app.licensed_feed import licensed_listings
from app.main import app, CACHE
from app.provider import fetch_schedule

DAY = date(2026, 10, 23)

class ClassificationTests(unittest.TestCase):
    def test_network_distribution_is_not_entitlement(self):
        samples = [
            ('ABC', 'over_the_air_network', True),
            ('ESPN', 'cable_network', False),
            ('ESPN+', 'streaming_service', False),
            ('Prime Video', 'streaming_service', False),
            ('FanDuel Sports Network Southwest', 'regional_network', False),
            ('NESN', 'regional_network', False),
            ('Made-up Sports Channel', 'unknown', False),
        ]
        for name, category, ota in samples:
            with self.subTest(name=name):
                row = classify_channel(name)
                self.assertEqual(row['distribution'], category)
                self.assertEqual(row['potentialAntenna'], ota)
                self.assertIn('verif', row['accessNote'].lower())

    def test_espn_vs_espn_plus_never_conflated(self):
        self.assertEqual(classify_channel('ESPN')['serviceName'], 'ESPN')
        self.assertEqual(classify_channel('ESPN+')['serviceName'], 'ESPN+')

    def test_listing_source_not_promoted_to_verified_access(self):
        events = [{'id': 'tsdb-12', 'broadcasts': [{
            'id':'b1', 'channel':'NBC', 'source':'provider',
            'type':'source_listing', 'confidence':'provider_listed', 'url':None
        }]}, {'id': 'tsdb-15', 'broadcasts': []}]
        annotate_events(events)
        self.assertEqual(events[0]['broadcastStatus'], 'source_listed')
        self.assertEqual(events[1]['broadcastStatus'], 'not_listed_by_provider')
        self.assertEqual(events[0]['broadcasts'][0]['availability'], 'unverified_for_viewer')
        self.assertIsNone(events[0]['broadcasts'][0]['url'])
        self.assertEqual(distribution_summary(events)['over_the_air_network'], 1)
        self.assertEqual(distribution_summary(events)['streaming_service'], 0)

class LicensedFeedTests(unittest.TestCase):
    def with_feed(self, data):
        fd, name = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        with open(name, 'w', encoding='utf-8') as f: json.dump(data, f)
        return name

    def test_only_explicit_event_id_us_local_day_and_source(self):
        valid = {'providerEventId':'42', 'date':DAY.isoformat(), 'country':'US',
                 'channel':'Example Channel', 'source':'Licensed Provider'}
        invalid = [
            {**valid, 'date':'2026-10-24'},
            {**valid, 'country':'CA'},
            {**valid, 'providerEventId':'some-team-name'},
            {**valid, 'source':''},
            {**valid, 'channel':''},
        ]
        name = self.with_feed([valid, {**valid}, *invalid])
        try:
            with patch.dict(os.environ, {'SPORTSLINEUP_LICENSED_LISTINGS_PATH':name}):
                result = licensed_listings(DAY)
            self.assertEqual(set(result), {'42'})
            self.assertEqual(len(result['42']), 1)
            self.assertEqual(result['42'][0]['source'], 'Licensed Provider')
        finally:
            os.unlink(name)

    def test_disabled_without_authorized_feed(self):
        with patch.dict(os.environ, {'SPORTSLINEUP_LICENSED_LISTINGS_PATH':''}):
            self.assertEqual(licensed_listings(DAY), {})

    def test_overlay_cannot_create_events_from_unrelated_id(self):
        feed = [{'providerEventId':'999', 'date':DAY.isoformat(), 'country':'US',
                 'channel':'Authorized A', 'source':'Licensed Provider'}]
        name = self.with_feed(feed)
        def handler(req: httpx.Request):
            name = req.url.path.split('/')[-1]
            if name == 'eventsday.php' and req.url.params.get('s') == 'Basketball':
                return httpx.Response(200, json={'events':[{
                    'idEvent':'42', 'strSport':'Basketball', 'strLeague':'NBA',
                    'strEvent':'Example A vs B', 'strTimestamp':'2026-10-23T20:00:00Z'
                }]})
            if name == 'eventsday.php': return httpx.Response(200, json={'events':[]})
            if name == 'eventstv.php': return httpx.Response(200, json={'tvevent':[]})
            if name == 'lookuptv.php': return httpx.Response(200, json={'tvevent':[]})
            return httpx.Response(200, json={'events':[]})
        try:
            with patch.dict(os.environ, {'SPORTSLINEUP_LICENSED_LISTINGS_PATH':name}):
                result = asyncio.run(fetch_schedule(DAY, transport=httpx.MockTransport(handler)))
            self.assertEqual([e['id'] for e in result['events']], ['tsdb-42'])
            self.assertEqual(result['events'][0]['broadcasts'], [])
            self.assertEqual(result['diagnostics']['licensedMatches'], 0)
        finally:
            os.unlink(name)

    def test_overlay_joins_existing_event_only(self):
        feed = [{'providerEventId':'42', 'date':DAY.isoformat(), 'country':'US',
                 'channel':'Example Authorized Sports', 'source':'Licensed Provider'}]
        name = self.with_feed(feed)
        def handler(req):
            endpoint = req.url.path.split('/')[-1]
            if endpoint == 'eventsday.php' and req.url.params.get('s') == 'Basketball':
                return httpx.Response(200, json={'events':[{'idEvent':'42', 'strSport':'Basketball',
                    'strLeague':'NBA', 'strTimestamp':'2026-10-23T19:00:00Z', 'strEvent':'Demo teams'}]})
            if endpoint == 'eventsday.php': return httpx.Response(200, json={'events':[]})
            if endpoint in ('eventstv.php', 'lookuptv.php'): return httpx.Response(200, json={'tvevent':[]})
            return httpx.Response(200, json={'events':[]})
        try:
            with patch.dict(os.environ, {'SPORTSLINEUP_LICENSED_LISTINGS_PATH':name}):
                result = asyncio.run(fetch_schedule(DAY, transport=httpx.MockTransport(handler)))
            self.assertEqual(result['events'][0]['broadcasts'][0]['channel'], 'Example Authorized Sports')
            self.assertEqual(result['diagnostics']['licensedMatches'], 1)
            self.assertEqual(result['events'][0]['broadcasts'][0]['availability'], 'unverified_for_viewer')
        finally:
            os.unlink(name)

class EndpointTests(unittest.TestCase):
    def test_guide_contract_keeps_timezone_and_errors(self):
        CACHE.clear()
        async def provider(day, timezone):
            return {'date': day.isoformat(), 'timezone':timezone, 'events':[],
                    'source':'fixture', 'coverage_note':'fixture', 'partial':True,
                    'guideVersion':'0.3', 'distributionCoverage':{}}
        async def fetch(path):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url='http://local') as client:
                return await client.get(path)
        with patch('app.main.fetch_schedule', side_effect=provider):
            result = asyncio.run(fetch('/v1/guide?date=2026-10-23&tz=America%2FChicago'))
        self.assertIn(result.status_code, (200, 400))  # date horizon depends on day of execution
        if result.status_code == 200:
            self.assertEqual(result.json()['guideVersion'], '0.3')
            self.assertEqual(result.json()['timezone'], 'America/Chicago')
        CACHE.clear()

if __name__ == '__main__': unittest.main()
