"""The v0.4 tests use synthetic provider-shaped fixtures, never fake real schedules."""
from __future__ import annotations
import asyncio
from datetime import date
import unittest
from unittest.mock import patch
import httpx
from app.reconciliation import reconcile, strong_match
from app.supplemental import (parse_mlb, parse_nhl, parse_espn,
                              supplemental_schedule, enabled_providers)
from app.aggregation import fetch_schedule
from app.provider import ProviderUnavailable

DAY = date(2026,10,23)


def game(id, sport, away, home, utc='2026-10-23T23:00:00Z', broadcasts=None, source='tsdb'):
    return {'id':f'{source}-{id}', 'sport':sport, 'league':sport.upper(),
            'title': f'{away} at {home}', 'startTime':utc, 'status':'scheduled',
            'venue':None, 'broadcasts':broadcasts or [],
            '_participants':[away,home], 'scheduleSources':[source]}


class ParserTests(unittest.TestCase):
    def test_mlb_local_date_and_teams_and_no_fabricated_tv(self):
        payload={'dates':[{'date':'2026-10-23','games':[{
            'gamePk':345,'gameDate':'2026-10-24T02:45:00Z',
            'teams':{'away':{'team':{'name':'Houston Astros'}},'home':{'team':{'name':'Texas Rangers'}}},
            'status':{'abstractGameState':'Preview'}}]}]}
        events=parse_mlb(payload,DAY,'America/Chicago')
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['id'],'mlb-mlb-345')
        self.assertIsNone(events[0]['venue'])
        self.assertEqual(events[0]['broadcasts'],[])
        self.assertEqual(parse_mlb(payload,DAY,'Asia/Tokyo'),[])

    def test_mlb_doubleheader_two_events(self):
        payload={'dates':[{'date':'2026-10-23','games':[]}]}
        for pk,stamp in ((1,'2026-10-23T17:00:00Z'),(2,'2026-10-23T23:00:00Z')):
            payload['dates'][0]['games'].append({'gamePk':pk,'gameDate':stamp,'teams':{
                'away':{'team':{'name':'A'}},'home':{'team':{'name':'B'}}}})
        self.assertEqual(len(parse_mlb(payload,DAY,'America/Chicago')),2)

    def test_nhl_hockey_game_week(self):
        payload={'gameWeek':[{'date':'2026-10-23','games':[{'id':777,
            'startTimeUTC':'2026-10-24T00:00:00Z','gameState':'FUT',
            'awayTeam':{'placeName':{'default':'Dallas'},'commonName':{'default':'Stars'}},
            'homeTeam':{'placeName':{'default':'Colorado'},'commonName':{'default':'Avalanche'}}}]}]}
        events=parse_nhl(payload,DAY,'America/Chicago')
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['title'],'Dallas Stars at Colorado Avalanche')
        self.assertEqual(events[0]['broadcasts'],[])

    def test_espn_literal_channel_and_no_inferred_rights(self):
        payload={'events':[{'id':'567','date':'2026-10-24T00:00:00Z','name':'Demo v Demo',
            'competitions':[{'competitors':[{'team':{'displayName':'Demo Away'}},
                            {'team':{'displayName':'Demo Home'}}],
                'broadcasts':[{'names':['ESPN','ESPN+']}, {'names':['ESPN']}]}]}]}
        events=parse_espn(payload,'nba',DAY,'America/Chicago')
        self.assertEqual(len(events),1)
        self.assertEqual([r['channel'] for r in events[0]['broadcasts']],['ESPN','ESPN+'])
        self.assertEqual(events[0]['broadcasts'][1]['source'],'ESPN (experimental)')
        self.assertIsNone(events[0]['broadcasts'][0]['url'])

    def test_espn_no_unsupported_geo_rights(self):
        payload={'events':[{'id':'567','date':'2026-10-24T00:00:00Z','name':'A at B',
            'competitions':[{'broadcasts':[], 'geoBroadcasts':[
                {'media':{'shortName':'Regional Network'}}]}]}]}
        self.assertEqual(parse_espn(payload,'nba',DAY,'America/Chicago')[0]['broadcasts'],[])

    def test_zero_or_invalid_event_time_does_not_guess(self):
        payload={'events':[{'id':'5','date':'not-time','name':'Unknown start',
                            'competitions':[]}]}
        self.assertEqual(parse_espn(payload,'pga',DAY,'America/Chicago'),[])

class ReconciliationTests(unittest.TestCase):
    def test_strong_match_preserves_canonical_id_and_broadcasts(self):
        primary=game('33','nba','Dallas Mavericks','Houston Rockets',source='TheSportsDB')
        extra=game('2','nba','Dallas Mavericks','Houston Rockets',utc='2026-10-23T23:05:00Z',
                   broadcasts=[{'id':'b1','channel':'ABC'}],source='ESPN')
        events,meta=reconcile([primary],[extra])
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['id'],primary['id'])
        self.assertEqual(events[0]['broadcasts'][0]['channel'],'ABC')
        self.assertEqual(meta['channelsAdded'],1)
        self.assertEqual(meta['matched'],1)
        self.assertEqual(len(events[0]['scheduleSources']),2)

    def test_different_sport_never_merge(self):
        p=game('1','nba','A','B')
        s=game('2','wnba','A','B')
        self.assertEqual(len(reconcile([p],[s])[0]),2)

    def test_different_opponent_never_transfers_broadcast(self):
        p=game('1','nba','A','B')
        s=game('2','nba','A','C',broadcasts=[{'channel':'ABC','id':'z'}])
        events,_=reconcile([p],[s])
        self.assertEqual(len(events),2)
        self.assertEqual(events[0]['broadcasts'],[])

    def test_doubleheader_never_matches_same_teams_hours_apart(self):
        p=game('1','mlb','A','B','2026-10-23T18:00:00Z')
        s=game('2','mlb','A','B','2026-10-23T22:00:00Z')
        self.assertFalse(strong_match(p,s))

    def test_missing_time_does_not_join(self):
        p=game('1','mlb','A','B',None)
        s=game('2','mlb','A','B')
        self.assertFalse(strong_match(p,s))

    def test_multiple_candidate_match_does_not_transfer(self):
        p1=game('1','nba','A','B',source='TheSportsDB')
        p2=game('2','nba','A','B',source='Different')
        s=game('3','nba','A','B',broadcasts=[{'id':'ch','channel':'ESPN'}],source='ESPN')
        events,meta=reconcile([p1,p2],[s])
        self.assertEqual(len(events),3)
        self.assertEqual(meta['ambiguous'],1)
        self.assertEqual(events[0]['broadcasts'],[])

class AdapterTests(unittest.TestCase):
    def test_all_optional_are_disabled_by_default(self):
        with patch.dict('os.environ', {'SPORTSLINEUP_ENABLE_MLB':'',
                   'SPORTSLINEUP_ENABLE_NHL':'','SPORTSLINEUP_ENABLE_ESPN':''}):
            self.assertEqual(enabled_providers(),[])

    def test_one_provider_failure_does_not_hide_other(self):
        def handler(req):
            if req.url.host=='statsapi.mlb.com': return httpx.Response(503)
            if req.url.host=='api-web.nhle.com':
                return httpx.Response(200,json={'gameWeek':[{'date':'2026-10-23','games':[{
                    'id':1,'startTimeUTC':'2026-10-23T22:00:00Z',
                    'awayTeam':{'abbrev':'DAL'},'homeTeam':{'abbrev':'COL'}}]}]})
            return httpx.Response(500)
        events, health=asyncio.run(supplemental_schedule(DAY,'America/Chicago',
                      httpx.MockTransport(handler),providers=['mlb','nhl']))
        self.assertEqual(len(events),1)
        self.assertEqual([h['status'] for h in health],['failed','ok'])

    def test_all_espn_feeds_are_explicit(self):
        def handler(req): return httpx.Response(200,json={'events':[]})
        events,health=asyncio.run(supplemental_schedule(DAY,'America/Chicago',
                             httpx.MockTransport(handler),providers=['espn']))
        self.assertEqual(events,[])
        self.assertGreaterEqual(len(health),8)
        self.assertTrue(all(x['status']=='empty' for x in health))

class AggregationTests(unittest.TestCase):
    def test_primary_fails_supplemental_succeeds(self):
        item=game('4','mlb','A','B',source='MLB')
        async def primary(*args): raise ProviderUnavailable('fail')
        async def supplement(*args): return [item],[{'provider':'MLB StatsAPI','sport':'mlb',
                                                    'status':'ok','events':1}]
        with patch('app.aggregation.fetch_tsdb',side_effect=primary),\
             patch('app.aggregation.supplemental_schedule',side_effect=supplement):
            result=asyncio.run(fetch_schedule(DAY,'America/Chicago'))
        self.assertEqual(len(result['events']),1)
        self.assertTrue(result['partial'])
        self.assertEqual(result['guideVersion'],'0.7.3')
        self.assertEqual(result['diagnostics']['providerRequestFailures'],1)
        self.assertEqual(result['sourceHealth'][0]['status'],'failed')
        self.assertEqual(result['events'][0]['broadcastStatus'],'not_listed_by_provider')

    def test_all_sources_fail_raises(self):
        async def primary(*args): raise ProviderUnavailable('fail')
        async def supplement(*args): return [],[{'provider':'MLB','sport':'mlb',
                                                'status':'failed','events':0}]
        with patch('app.aggregation.fetch_tsdb',side_effect=primary),\
             patch('app.aggregation.supplemental_schedule',side_effect=supplement):
            with self.assertRaises(ProviderUnavailable):
                asyncio.run(fetch_schedule(DAY,'America/Chicago'))

if __name__=='__main__': unittest.main()

class AuthorizedFeedTests(unittest.TestCase):
    def test_licensed_source_can_add_missing_ncaa_and_pga_events_without_guessing(self):
        import tempfile,json,os
        from app.authorized_events import authorized_events
        rows=[
            {'source':'Authorized Example','providerEventId':'c101','date':DAY.isoformat(),
             'country':'US','sport':'ncaa_football','title':'Team A at Team B',
             'startTime':'2026-10-23T23:00:00Z','participants':['Team A','Team B'],
             'broadcasts':[{'channel':'Example Sports Channel'}]},
            {'source':'Authorized Example','providerEventId':'g101','date':DAY.isoformat(),
             'country':'US','sport':'pga','title':'Sample Tournament','broadcasts':[]},
            {'source':'Authorized Example','providerEventId':'c103','date':'2026-10-24',
             'country':'US','sport':'ufc','title':'Next Day UFC'},
            {'source':'Authorized Example','providerEventId':'c104','date':DAY.isoformat(),
             'country':'CA','sport':'wnba','title':'Unlicensed geography'},
        ]
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(rows,f)
            name=f.name
        try:
            with patch.dict(os.environ,{'SPORTSLINEUP_AUTHORIZED_EVENTS_PATH':name}):
                out,status=authorized_events(DAY,'America/Chicago')
            self.assertEqual(status['status'],'ok')
            self.assertEqual(status['events'],2)
            self.assertEqual({e['sport'] for e in out},{'ncaa_football','pga'})
            self.assertEqual(out[0]['broadcasts'][0]['channel'],'Example Sports Channel')
        finally: os.unlink(name)

    def test_licensed_source_missing_file_reports_failure(self):
        from app.authorized_events import authorized_events
        with patch.dict('os.environ',{'SPORTSLINEUP_AUTHORIZED_EVENTS_PATH':'/no/such/file.json'}):
            events,status=authorized_events(DAY,'America/Chicago')
        self.assertEqual(events,[])
        self.assertEqual(status['status'],'failed')

class StaleCacheTests(unittest.TestCase):
    def test_expired_provider_returns_marked_stale_previous_response(self):
        from app.main import app,CACHE
        from time import monotonic
        from datetime import datetime
        day=datetime.now().date()
        key=f'{day.isoformat()}:America/Chicago:::False:'
        old={'date':day.isoformat(),'timezone':'America/Chicago',
             'events':[],'source':'Earlier provider snapshot','coverage_note':'old',
             'partial':True,'stale':False}
        async def failed(*args): raise ProviderUnavailable('outage')
        async def fetch():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as cl:
                return await cl.get(f'/v1/schedule?date={day.isoformat()}&tz=America%2FChicago')
        CACHE.clear()
        try:
            CACHE[key]=(monotonic()-700,old)
            with patch('app.main.fetch_schedule',side_effect=failed),patch('app.main.enabled_providers',return_value=[]):
                response=asyncio.run(fetch())
            self.assertEqual(response.status_code,200)
            self.assertTrue(response.json()['stale'])
            self.assertIn('LAST KNOWN',response.json()['coverage_note'])
        finally: CACHE.clear()

    def test_no_cache_on_provider_failure_502(self):
        from app.main import app,CACHE
        from datetime import datetime
        day=datetime.now().date()
        async def failed(*args): raise ProviderUnavailable('outage')
        async def fetch():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as cl:
                return await cl.get(f'/v1/schedule?date={day.isoformat()}&tz=America%2FChicago')
        CACHE.clear()
        with patch('app.main.fetch_schedule',side_effect=failed):
            response=asyncio.run(fetch())
        self.assertEqual(response.status_code,502)
