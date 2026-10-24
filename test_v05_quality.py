"""v0.5 safety, evidence retention, and coverage-gap reporting regression tests."""
import asyncio
from datetime import date
from unittest.mock import patch
import unittest
from fastapi.testclient import TestClient
from app.main import app
from app.normalization import merge_channels
from app.broadcast_intelligence import annotate_events
from app.quality import quality_report
from app.supplemental import parse_nhl
from app.aggregation import fetch_schedule


class EvidenceTests(unittest.TestCase):
    def test_matching_channels_preserve_distinct_provider_evidence(self):
        channels = [{'id':'a','channel':'ESPN', 'source':'TheSportsDB', 'confidence':'provider_listed'}]
        merge_channels(channels, [{'id':'b','channel':' ESPN ', 'source':'Authorized Data Partner'}])
        self.assertEqual(len(channels),1)
        self.assertEqual(channels[0]['sources'],['TheSportsDB','Authorized Data Partner'])
        self.assertEqual(channels[0]['source'],'TheSportsDB')

    def test_espn_and_espn_plus_are_distinct(self):
        values=[]
        merge_channels(values, [{'channel':'ESPN','source':'A'}, {'channel':'ESPN+','source':'B'}])
        self.assertEqual(len(values),2)

    def test_annotate_keeps_access_unverified_for_multi_source(self):
        events=[{'broadcasts':[{'channel':'NBC', 'source':'A','sources':['A','B']}]}]
        annotate_events(events)
        self.assertEqual(events[0]['broadcasts'][0]['evidenceCount'],2)
        self.assertEqual(events[0]['broadcasts'][0]['availability'],'unverified_for_viewer')
        self.assertEqual(events[0]['broadcastStatus'],'source_listed')

    def test_quality_counts_only_returned_events(self):
        events=[{'sport':'nba','broadcasts':[{'channel':'NBC','sources':['A','B']}]},
                {'sport':'nba','broadcasts':[]},
                {'sport':'nhl','broadcasts':[{'channel':'ESPN','sources':['A']}]}]
        quality=quality_report(events, [{'provider':'test','status':'failed'}], {'tvLookupSkippedByBudget':2})
        self.assertEqual(quality['missingUSListings'],1)
        self.assertEqual(quality['eventsWithMultiSourceListing'],1)
        self.assertEqual(quality['bySport']['nba']['listingRatePercent'],50)
        self.assertIsNone(quality['bySport']['pga']['listingRatePercent'])
        self.assertEqual(len(quality['sourceFailures']),1)
        self.assertTrue(any('2 events' in x for x in quality['warnings']))

    def test_quality_zero_events_is_not_a_perfect_score(self):
        quality=quality_report([],[],{})
        self.assertIsNone(quality['bySport']['ufc']['listingRatePercent'])
        self.assertEqual(quality['returnedEvents'],0)

    def test_nhl_uses_only_explicit_us_game_broadcasts(self):
        payload={'gameWeek':[{'date':'2026-10-23','games':[
            {'id':204,'gameState':'FUT', 'startTimeUTC':'2026-10-24T01:00:00Z',
             'awayTeam':{'placeName':{'default':'Dallas'},'commonName':{'default':'Stars'}},
             'homeTeam':{'placeName':{'default':'Colorado'},'commonName':{'default':'Avalanche'}},
             'tvBroadcasts':[{'countryCode':'US','network':'ESPN','market':'N'},
                             {'countryCode':'CA','network':'TSN','market':'N'},
                             {'countryCode':'US','network':'Altitude','market':'H'}]}
        ]}]}
        got=parse_nhl(payload,date(2026,10,23),'America/Chicago')
        self.assertEqual(len(got),1)
        self.assertEqual([r['channel'] for r in got[0]['broadcasts']],['ESPN','Altitude'])
        self.assertEqual(got[0]['broadcasts'][1]['marketScope'],'H')

    def test_nhl_does_not_infer_us_when_country_missing(self):
        from app.supplemental import _nhl_tv_broadcasts
        self.assertEqual(_nhl_tv_broadcasts({'id':1,'tvBroadcasts':[{'network':'TNT'}]}),[])

    def test_gaps_endpoint_does_not_expose_events_or_secret(self):
        import app.main as main
        from time import monotonic
        day = date.today().isoformat()
        data={'date':day,'timezone':'America/Chicago','quality':quality_report([],[],{}),
              'fetchedAt':'2026-10-23T20:00:00Z', 'sourceHealth':[],
              'events':[{'secret':'MUST_NOT_RETURN'}], 'stale':False}
        with patch.object(main,'CACHE',{f'{day}:America/Chicago:::False:':(monotonic(),data)}):
            response=TestClient(app).get(f'/v1/coverage/gaps?date={day}&tz=America/Chicago')
        self.assertEqual(response.status_code,200)
        self.assertNotIn('events',response.json())
        self.assertNotIn('MUST_NOT_RETURN',response.text)
        self.assertEqual(response.json()['returnedEvents'],0)

    def test_failed_primary_does_not_appear_successful_in_quality(self):
        from app.provider import ProviderUnavailable
        supplements=([{'id':'mlb-1','sport':'mlb','title':'A at B','startTime':'2026-10-23T19:00:00Z',
                       'broadcasts':[],'scheduleSources':['MLB'], 'status':'scheduled', 'league':'MLB'}],
                     [{'provider':'MLB StatsAPI','sport':'mlb','events':1,'status':'ok'}])
        with patch('app.aggregation.fetch_tsdb', return_value=ProviderUnavailable('bad')), \
             patch('app.aggregation.supplemental_schedule', return_value=supplements), \
             patch('app.aggregation.authorized_events', return_value=([],None)):
            got=asyncio.run(fetch_schedule(date(2026,10,23),'America/Chicago'))
        self.assertEqual(got['quality']['returnedEvents'],1)
        self.assertEqual(got['quality']['missingUSListings'],1)
        self.assertTrue(got['partial'])


if __name__ == '__main__': unittest.main()
