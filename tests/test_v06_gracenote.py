"""v0.6 licensed broadcaster adapter: offline, repeatable fixtures. No real API keys."""
from datetime import date, datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch
import asyncio
import os
import unittest
from fastapi.testclient import TestClient
from app import gracenote
from app.main import app, CACHE
from app.reconciliation import reconcile
from app.broadcast_intelligence import annotate_events

DAY = date(2026, 10, 23)
ZIP = '78258'  # Synthetic fixture value; not a user setting.
LINEUP = 'USA-OTA78258'


def sample(sport='NBA Basketball', label='Indiana Pacers at Dallas Mavericks', title='NBA Basketball',
           tms='SP003376400000', station='KFAA', tv_start='2026-10-23T23:45Z',
           game_start='18:00:00-05:00', game_day='2026-10-23'):
    return dict(startTime=tv_start, qualifiers=['Live', 'Subject to blackout'],
                stationId='321', station={'callSign':station, 'channel':'29', 'stationId':'321'},
                program={'tmsId':tms, 'title':title, 'eventTitle':label,
                         'longDescription':sport, 'gameDate':game_day, 'gameTime':game_start,
                         'teams':[{'name':'Indiana Pacers'},{'name':'Dallas Mavericks'}]})


class GracenoteTests(unittest.TestCase):
    def setUp(self):
        CACHE.clear()
        gracenote._LINEUP_CACHE.clear()

    def test_fails_without_enabled_flag_or_credentials(self):
        with patch.dict(os.environ, {'SPORTSLINEUP_ENABLE_GRACENOTE':'false','GRACENOTE_API_KEY':''}):
            rows, status = asyncio.run(gracenote.sports_airings(DAY, 'America/Chicago',LINEUP,ZIP))
        self.assertEqual(status['status'], 'disabled')
        self.assertEqual(rows, [])

    def test_only_five_digit_zip_accepted(self):
        with self.assertRaises(ValueError):
            asyncio.run(gracenote.lineups_for_zip('78258&api_key=leak'))
        response = TestClient(app).get('/v1/lineups?zip=123AB')
        self.assertEqual(response.status_code,400)

    def test_no_lineup_without_license(self):
        with patch.dict(os.environ, {'SPORTSLINEUP_ENABLE_GRACENOTE':'true','GRACENOTE_API_KEY':''}):
            result = TestClient(app).get('/v1/lineups?zip='+ZIP).json()
        self.assertEqual(result['status'],'not_configured')
        self.assertEqual(result['lineups'], [])

    def test_lineups_schema_from_documented_payload(self):
        raw=[{'lineupId':LINEUP,'name':'Over the Air','type':'OTA'},
             {'lineupId':'USA-TX24423-X','name':'Cable Plan','type':'CABLE'},
             {'lineupId':'../../invalid','name':'oops','type':'OTA'}]
        with patch.dict(os.environ, {'SPORTSLINEUP_ENABLE_GRACENOTE':'true','GRACENOTE_API_KEY':'dummy'}):
            with patch.object(gracenote,'_get',new_callable=AsyncMock,return_value=raw) as get:
                first=asyncio.run(gracenote.lineups_for_zip(ZIP))
                second=asyncio.run(gracenote.lineups_for_zip(ZIP))
        self.assertEqual(len(first),2)
        self.assertEqual(first, second)
        get.assert_awaited_once()

    def test_rejects_mismatched_selected_lineup(self):
        raw=[{'lineupId':LINEUP,'name':'Antenna','type':'OTA'}]
        with patch.dict(os.environ, {'SPORTSLINEUP_ENABLE_GRACENOTE':'true','GRACENOTE_API_KEY':'dummy'}):
            with patch.object(gracenote,'_get',new_callable=AsyncMock,return_value=raw) as get:
                events,health=asyncio.run(gracenote.sports_airings(DAY,'America/Chicago','USA-OTA00000',ZIP))
        self.assertEqual(events,[])
        self.assertEqual(health['errorCode'],'lineup_not_available_for_zip')
        get.assert_awaited_once()

    def test_source_event_and_local_station_metadata(self):
        events,stats=gracenote.parse_airings([sample()],DAY,'America/Chicago',LINEUP,'OTA')
        self.assertEqual(stats['supportedAirings'],1)
        self.assertEqual(len(events),1)
        event=events[0]
        self.assertEqual(event['sport'],'nba')
        self.assertEqual(event['timeBasis'],'game')
        self.assertEqual(event['startTime'],'2026-10-23T23:00:00Z')
        self.assertEqual(event['status'],'scheduled')  # Gracenote qualifier Live != real-time game status.
        bc=event['broadcasts'][0]
        self.assertEqual(bc['channel'],'KFAA')
        self.assertEqual(bc['channelNumber'],'29')
        self.assertTrue(bc['blackoutWarning'])
        self.assertEqual(bc['lineupId'],LINEUP)
        self.assertNotIn('api_key',str(bc))
        annotate_events(events)
        self.assertEqual(bc['marketScope'],'selected_lineup')
        self.assertEqual(events[0]['broadcasts'][0]['distribution'],'local_lineup')
        self.assertTrue(events[0]['broadcasts'][0]['potentialAntenna'])
        self.assertEqual(events[0]['broadcasts'][0]['availability'],'unverified_for_viewer')

    def test_multiple_airings_same_event_are_one_event(self):
        a=sample()
        b=sample(station='KENS',tv_start='2026-10-24T00:00Z')
        c=sample(station='KFAA')
        result,_=gracenote.parse_airings([a,b,c],DAY,'America/Chicago',LINEUP)
        self.assertEqual(len(result),1)
        self.assertEqual({r['channel'] for r in result[0]['broadcasts']},{'KFAA','KENS'})

    def test_program_without_explicit_league_does_not_guess(self):
        row=sample(sport='Professional Basketball',title='Basketball',label='Mysterious Game')
        row['program']['longDescription']='Basketball'
        events,stats=gracenote.parse_airings([row],DAY,'America/Chicago',LINEUP)
        self.assertEqual(events,[])
        self.assertEqual(stats['unclassified'],1)

    def test_all_eight_supported_categories(self):
        pairs={'nba':'NBA Basketball','mlb':'MLB Baseball','ncaa_football':'College Football',
               'ncaa_basketball':'College Basketball','nhl':'NHL Hockey',
               'pga':'PGA TOUR','wnba':'WNBA Basketball','ufc':'UFC Fight Night'}
        for expected,title in pairs.items():
            with self.subTest(expected=expected):
                self.assertEqual(gracenote._sport({'title':title}),expected)

    def test_airing_day_not_moved_by_utc_boundary(self):
        row=sample(tv_start='2026-10-24T01:15Z') # Oct 23 8:15 PM in Chicago.
        a,_=gracenote.parse_airings([row],DAY,'America/Chicago',LINEUP)
        self.assertEqual(len(a),1)
        b,_=gracenote.parse_airings([row],date(2026,10,24),'America/Chicago',LINEUP)
        self.assertEqual(b,[])

    def test_does_not_create_station_when_only_channel_number_known(self):
        row=sample(); row['station'].pop('callSign')
        result,stats=gracenote.parse_airings([row],DAY,'America/Chicago',LINEUP)
        self.assertEqual(result,[])
        self.assertEqual(stats['noStation'],1)

    def test_strict_opponent_match_blocks_wrong_network(self):
        gn,_=gracenote.parse_airings([sample()],DAY,'America/Chicago',LINEUP)
        fixture={'id':'tsdb-77','sport':'nba','title':'Indiana Pacers at Houston Rockets',
                 'startTime':'2026-10-23T23:00:00Z','broadcasts':[],
                 '_participants':['Indiana Pacers','Houston Rockets']}
        merged, report=reconcile([fixture],gn)
        self.assertEqual(report['matched'],0)
        self.assertEqual(len(merged),2)

    def test_strict_time_match_blocks_doubleheader(self):
        gn,_=gracenote.parse_airings([sample()],DAY,'America/Chicago',LINEUP)
        fixture={'id':'tsdb-78','sport':'nba','title':'Indiana Pacers at Dallas Mavericks',
                 'startTime':'2026-10-24T04:00:00Z','broadcasts':[],
                 '_participants':['Indiana Pacers','Dallas Mavericks']}
        merged, report=reconcile([fixture],gn)
        self.assertEqual(report['matched'],0)

    def test_eligible_match_adds_station_but_not_subscriber_certification(self):
        gn,_=gracenote.parse_airings([sample()],DAY,'America/Chicago',LINEUP)
        fixture={'id':'tsdb-79','sport':'nba','title':'Indiana Pacers at Dallas Mavericks',
                 'startTime':'2026-10-23T23:00:00Z','broadcasts':[],
                 '_participants':['Indiana Pacers','Dallas Mavericks']}
        merged, report=reconcile([fixture],gn)
        self.assertEqual(report['matched'],1)
        self.assertEqual(merged[0]['broadcasts'][0]['channel'],'KFAA')
        annotate_events(merged)
        self.assertEqual(merged[0]['broadcasts'][0]['availability'],'unverified_for_viewer')

    def test_bad_provider_host_rejected(self):
        with patch.dict(os.environ, {'GRACENOTE_API_BASE_URL':'http://attacker.example/gracenote'}):
            with self.assertRaises(gracenote.GracenoteUnavailable) as ctx:
                gracenote._root()
        self.assertEqual(ctx.exception.code,'invalid_provider_host')

    def test_health_does_not_expose_key(self):
        with patch.dict(os.environ, {'GRACENOTE_API_KEY':'INTERNAL-NOT-FOR-APP','SPORTSLINEUP_ENABLE_GRACENOTE':'true'}):
            response=TestClient(app).get('/health')
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json()['gracenoteKeyConfigured'])
        self.assertNotIn('INTERNAL-NOT-FOR-APP',response.text)

    def test_cache_separates_lineups(self):
        from app.main import schedule
        async def perform():
            with patch('app.main.fetch_schedule', new_callable=AsyncMock,
                       return_value={'date':DAY.isoformat(),'events':[],'source':'test','coverage_note':'','partial':True}) as mock:
                with patch('app.main.gracenote_enabled',return_value=True):
                    # Call with valid IDs, no live provider; isolate cache behavior.
                    await schedule(day=DAY,tz='America/Chicago',zip_code=ZIP,lineup_id=LINEUP)
                    await schedule(day=DAY,tz='America/Chicago',zip_code=ZIP,lineup_id='USA-TX20000-X')
                self.assertEqual(mock.await_count,2)
        asyncio.run(perform())

    def test_sanitized_provider_error_never_contains_url_or_secret(self):
        async def failing_get(*args,**kwargs):
            raise gracenote.GracenoteUnavailable('provider_http_error')
        with patch.dict(os.environ, {'GRACENOTE_API_KEY':'secret','SPORTSLINEUP_ENABLE_GRACENOTE':'true'}):
            with patch.object(gracenote,'_get',side_effect=failing_get):
                rows,health=asyncio.run(gracenote.sports_airings(DAY,'America/Chicago',LINEUP,ZIP))
        self.assertEqual(rows,[])
        self.assertEqual(health['errorCode'],'provider_http_error')
        self.assertNotIn('secret',str(health))


if __name__=='__main__': unittest.main()

class LiveConnectorContractTests(unittest.TestCase):
    def setUp(self):
        gracenote._LINEUP_CACHE.clear()
        gracenote._AIRINGS_CACHE.clear()

    def test_end_to_end_mock_connector_two_half_day_requests_and_cache(self):
        response=[{'lineupId':LINEUP,'name':'Local OTA','type':'OTA'}]
        tasks=[]
        async def mocked(path, params):
            tasks.append((path,dict(params)))
            if path == '/lineups': return response
            if path == '/sports/all/events/airings': return [sample()]
            raise AssertionError('unrecognized endpoint')
        with patch.dict(os.environ, {'GRACENOTE_API_KEY':'dummy','SPORTSLINEUP_ENABLE_GRACENOTE':'true'}):
            with patch.object(gracenote,'_get',side_effect=mocked):
                first, health=asyncio.run(gracenote.sports_airings(DAY,'America/Chicago',LINEUP,ZIP))
                next_result, next_health=asyncio.run(gracenote.sports_airings(DAY,'America/Chicago',LINEUP,ZIP))
        self.assertEqual(health['status'],'ok')
        self.assertEqual(len(first),1)
        self.assertEqual(len(tasks),3) # ZIP lineup + two 12-hour sports queries
        self.assertEqual(first,next_result)
        self.assertEqual(health,next_health)
        self.assertEqual({x[0] for x in tasks}, {'/lineups','/sports/all/events/airings'})

    def test_provider_local_request_budget_does_not_leak_key(self):
        now=gracenote.monotonic()
        gracenote._REQUEST_TIMES[:] = [now]*gracenote.MAX_PROVIDER_REQUESTS_PER_HOUR
        try:
            with patch.dict(os.environ, {'GRACENOTE_API_KEY':'THE-SECRET'}):
                with self.assertRaises(gracenote.GracenoteUnavailable) as err:
                    asyncio.run(gracenote._get('/lineups', {'postalCode':ZIP}))
            self.assertEqual(err.exception.code, 'local_hourly_request_budget_exhausted')
            self.assertNotIn('THE-SECRET',str(err.exception))
        finally:
            gracenote._REQUEST_TIMES.clear()
