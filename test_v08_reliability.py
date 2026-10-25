import asyncio, os, tempfile, time, unittest
from unittest.mock import patch
from datetime import date
from app.remote_authorized import _trusted_url, remote_authorized_events
from app.push_delivery import check_install_key, check_token, check_device_id, _milestones, register_device, subscribe, registered_event_ids, configured

class BroadcastFeedTests(unittest.TestCase):
    def test_https_only_no_loopback(self):
        for url in ['http://api.example.com','https://localhost/','https://foo:8080/events','https://u:p@foo/events']:
            with self.subTest(url=url), patch.dict(os.environ, {'SPORTSLINEUP_AUTHORIZED_FEED_URL':url}):
                self.assertIsNone(_trusted_url())
        with patch.dict(os.environ, {'SPORTSLINEUP_AUTHORIZED_FEED_URL':'https://feed.example.com/listings'}):
            self.assertEqual(_trusted_url(),'https://feed.example.com/listings')

    def test_disabled_returns_none(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(asyncio.run(remote_authorized_events(date(2026,10,24),'America/Chicago')),([],None))

class PushReliabilityTests(unittest.TestCase):
    def test_opaque_credentials(self):
        self.assertTrue(check_install_key('a'*64))
        self.assertFalse(check_install_key('bad'))
        self.assertTrue(check_device_id('xxxxxxxx-xxxxxxxx-xxxxxxxx'))
        self.assertTrue(check_token('1'*64))
        self.assertFalse(check_token('http://evil.com'))

    def test_milestones_only_verified_transitions(self):
        self.assertEqual(_milestones(None, {'state':'final','home':100,'away':98,'progress':'Final'}), [])
        old={'state':'live','home':100,'away':90,'progress':'Q3'}
        now={'state':'live','home':105,'away':103,'progress':'Q4'}
        self.assertIn('closeGame',_milestones(old,now))
        self.assertIn('interval:4',_milestones(old,now))
        self.assertIn('final',_milestones(now,{**now,'state':'final','progress':'Final'}))

    def test_db_registration_and_consent(self):
        with tempfile.TemporaryDirectory() as folder:
            env={'SPORTSLINEUP_PUSH_ENABLED':'true','SPORTSLINEUP_APNS_TEAM_ID':'TEAM',
                 'SPORTSLINEUP_APNS_KEY_ID':'KID','SPORTSLINEUP_APNS_PRIVATE_KEY':'key',
                 'SPORTSLINEUP_APNS_BUNDLE_ID':'com.example.app',
                 'SPORTSLINEUP_PUSH_DB_PATH':folder+'/state.sqlite'}
            with patch.dict(os.environ,env):
                self.assertTrue(configured())
                identifier='app-install-12345678901234567'
                key='b'*64
                self.assertTrue(register_device(identifier,key,'c'*64))
                self.assertFalse(register_device(identifier,'d'*64,'c'*64))
                self.assertTrue(subscribe(identifier,key,'tsdb-12345678','A vs B','nba',int(time.time()+3600), {'final':True}))
                self.assertIn('tsdb-12345678',registered_event_ids())
                self.assertFalse(subscribe(identifier,'d'*64,'tsdb-12345678','A vs B','nba',int(time.time()+3600), {'final':True}))
                self.assertTrue(subscribe(identifier,key,'tsdb-12345678','A vs B','nba',int(time.time()+3600), {'final':False}))
                self.assertNotIn('tsdb-12345678',registered_event_ids())

class MilestoneNoiseTests(unittest.TestCase):
    def test_quarter_clock_changes_not_duplicated(self):
        from app.push_delivery import _milestones
        self.assertEqual(_milestones({'state':'live','home':23,'away':20,'progress':'Q3 7:00'},
                                     {'state':'live','home':25,'away':22,'progress':'Q3 5:31'},'nba'),[])
        self.assertIn('interval:4',_milestones({'state':'live','home':23,'away':20,'progress':'Q3 7:00'},
                                               {'state':'live','home':30,'away':26,'progress':'Q4 11:00'},'nba'))
        self.assertNotIn('closeGame',_milestones({'state':'live','home':2,'away':1,'progress':'P3'},
                                                 {'state':'live','home':2,'away':1,'progress':'P3 01:00'},'pga'))

class RemoteLicensedFeedIntegration(unittest.TestCase):
    def test_all_eight_sports_read_from_authorized_feed(self):
        from app.remote_authorized import _CACHE
        import httpx
        _CACHE.clear()
        sports=['nba','mlb','ncaa_football','ncaa_basketball','nhl','pga','wnba','ufc']
        rows=[{'country':'US','sport':sport,'providerEventId':f'licensed-{ix}',
               'title':f'Team A vs Team B', 'source':'Licensed Test Provider',
               'startTime':'2026-10-24T17:00:00Z',
               'participants':['Team A','Team B'],
               'broadcasts':[{'channel':'Example Network'}]}
              for ix,sport in enumerate(sports)]
        class FakeClient:
            def __init__(self,**kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def get(self,url,params=None,headers=None):
                self.assertTokenAbsent = 'Authorization' not in headers
                return httpx.Response(200,json={'events':rows},request=httpx.Request('GET',url))
        with patch.dict(os.environ, {'SPORTSLINEUP_AUTHORIZED_FEED_URL':'https://vendor.example/feed'},clear=True):
            with patch('app.remote_authorized.httpx.AsyncClient', FakeClient):
                results, status = asyncio.run(remote_authorized_events(date(2026,10,24),'America/Chicago'))
        self.assertEqual(status['status'],'ok')
        self.assertEqual({r['sport'] for r in results},set(sports))
        self.assertTrue(all(r['broadcasts'][0]['channel']=='Example Network' for r in results))

    def test_push_is_disabled_by_default(self):
        from fastapi.testclient import TestClient
        from app.main import app
        with patch.dict(os.environ, {}, clear=True):
            client=TestClient(app)
            self.assertFalse(client.get('/v1/push/status').json()['configured'])
            response=client.post('/v1/push/register',json={'installationId':'x'*26,'installationKey':'a'*64,'apnsToken':'b'*64})
            self.assertEqual(response.status_code,503)
