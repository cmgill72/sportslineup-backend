"""v0.4.4 only tests documented behavior; not claims of live provider completeness."""
import asyncio
from datetime import date, datetime
from unittest.mock import patch
import unittest
import httpx
from fastapi.testclient import TestClient
from app.main import app, CACHE
from app.provider import fetch_schedule

DAY = date(2026, 10, 23)

def make_event(eid, timestamp, league='NBA', sport='Basketball'):
    return {'idEvent': eid, 'strSport': sport, 'strLeague': league,
            'strEvent': f'Team {eid} vs Other', 'dateEvent': timestamp[:10],
            'strTimestamp': timestamp, 'strStatus': 'NS'}

class BroadcastCoverageFixTests(unittest.TestCase):
    def test_local_date_lookups_not_wasted_on_adjacent_utc_games(self):
        queried=[]
        def handler(request):
            path=request.url.path.rsplit('/',1)[-1]
            args=dict(request.url.params)
            if path == 'eventsday.php':
                # ID 001 sorts first, but is TOMORROW local Chicago time.
                return httpx.Response(200, json={'events': [
                    make_event('001', '2026-10-24T16:00:00Z'),
                    make_event('900', '2026-10-24T01:30:00Z'),
                ] if args['s']=='Basketball' and args['d']=='2026-10-24' else []})
            if path=='eventstv.php': return httpx.Response(200,json={'tvevent':[]})
            if path=='lookuptv.php':
                queried.append(args['id'])
                return httpx.Response(200,json={'tvevent':[{'idEvent': args['id'], 'strCountry':'USA', 'strChannel':'NBC'}]})
            return httpx.Response(200,json={'events':[]})
        with patch.dict('os.environ',{'SPORTSDB_API_KEY':'premium-fixture', 'SPORTSDB_LOOKUP_LIMIT':'1'}):
            answer=asyncio.run(fetch_schedule(DAY,'America/Chicago',httpx.MockTransport(handler)))
        self.assertEqual(queried,['900'])
        self.assertEqual(answer['coverage']['withBroadcast'],1)
        self.assertEqual(answer['diagnostics']['tvLookupEligible'],1)
        self.assertEqual(answer['diagnostics']['tvLookupWithUSListings'],1)

    def test_round_robin_spreads_lookups_across_sports(self):
        looked=[]
        def handler(req):
            endpoint=req.url.path.rsplit('/',1)[-1]
            args=dict(req.url.params)
            if endpoint=='eventsday.php':
                if args['d']=='2026-10-23' and args['s']=='Basketball':
                    return httpx.Response(200,json={'events':[make_event('10','2026-10-23T19:00:00Z'), make_event('11','2026-10-23T20:00:00Z')]})
                if args['d']=='2026-10-23' and args['s']=='Ice Hockey':
                    return httpx.Response(200,json={'events':[make_event('12','2026-10-23T21:00:00Z','NHL','Ice Hockey')]})
                return httpx.Response(200,json={'events':[]})
            if endpoint=='eventstv.php':return httpx.Response(200,json={'tvevent':[]})
            if endpoint=='lookuptv.php':
                looked.append(args['id'])
                return httpx.Response(200,json={'tvevent':[]})
            return httpx.Response(200,json={'events':[]})
        with patch.dict('os.environ',{'SPORTSDB_API_KEY':'premium-fixture','SPORTSDB_LOOKUP_LIMIT':'2'}):
            answer=asyncio.run(fetch_schedule(DAY,'America/Chicago',httpx.MockTransport(handler)))
        self.assertEqual(looked, ['10','12'])
        self.assertEqual(answer['diagnostics']['tvLookupSkippedByBudget'],1)
        self.assertEqual(answer['diagnostics']['tvLookupNoListings'],2)
        self.assertEqual(answer['coverage']['withBroadcast'],0)

    def test_non_us_tv_is_not_misrepresented_as_us(self):
        def handler(req):
            endpoint=req.url.path.rsplit('/',1)[-1]
            if endpoint == 'eventsday.php':
                return httpx.Response(200,json={'events':[make_event('100','2026-10-23T18:00:00Z')]
                    if req.url.params.get('s')=='Basketball' and req.url.params.get('d')=='2026-10-23' else []})
            if endpoint=='eventstv.php':return httpx.Response(200,json={'tvevent':[]})
            if endpoint=='lookuptv.php': return httpx.Response(200,json={'tvevent':[
                {'idEvent':'100','strCountry':'Canada','strChannel':'TSN'}]})
            return httpx.Response(200,json={'events':[]})
        with patch.dict('os.environ',{'SPORTSDB_API_KEY':'premium-fixture'}):
            answer=asyncio.run(fetch_schedule(DAY,'America/Chicago',httpx.MockTransport(handler)))
        self.assertEqual(answer['diagnostics']['tvLookupOnlyNonUS'],1)
        self.assertEqual(answer['coverage']['withBroadcast'],0)

    def test_safe_broadcast_diagnostics_no_key_or_full_events(self):
        import app.main as main
        from time import monotonic
        today=date.today().isoformat()
        sample={'date':today,'timezone':'America/Chicago','coverage':{'events':4,'withBroadcast':1},
                'sourceHealth':[], 'diagnostics':{'tvLookupEligible':3, 'tvLookupNoListings':2},
                'events':[{'title':'Example'}], 'fetchedAt':'2026-10-23T22:00:00Z'}
        with patch.object(main,'CACHE',{f'{today}:America/Chicago:': (monotonic(), sample)}):
            r=TestClient(app).get(f'/v1/diagnostics/broadcast?date={today}&tz=America/Chicago')
        self.assertEqual(r.status_code,200)
        self.assertNotIn('events',r.json())
        self.assertIn('tvLookupSummary',r.json())
