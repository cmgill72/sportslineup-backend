"""Regression protection for verified event-specific broadcasts and status safety."""
import unittest
from datetime import date
from app.normalization import normalize_status, normalize_event
from app.verified_listings import apply_verified_listings
from app.broadcast_intelligence import annotate_events


def event(sport, name, start):
    return {'id': f'test-{sport}-{name}', 'sport': sport, 'title': name,
            'startTime': start, 'status': 'scheduled', 'broadcasts': [], 'league': sport,
            'scheduleSources': ['fixture']}


class VerifiedListingIntegrity(unittest.TestCase):
    def test_unrecognized_status_never_live(self):
        for raw in ('Not Started', 'Unknown', 'foo', 'Scheduled', '', None, 'Kickoff'):
            self.assertEqual(normalize_status(raw), 'scheduled')

    def test_explicit_final_and_inprogress_states(self):
        for raw in ('Match Finished', 'FT', 'FINAL'):
            self.assertEqual(normalize_status(raw), 'final')
        for raw in ('LIVE', 'Q3', '2nd Quarter', 'HT'):
            self.assertEqual(normalize_status(raw), 'live')

    def test_normalize_event_status_unknown_is_not_live(self):
        raw={'idEvent':'321', 'strSport':'Basketball', 'strLeague':'NBA',
             'strHomeTeam':'Houston Rockets','strAwayTeam':'San Antonio Spurs',
             'strStatus':'Bad Status'}
        self.assertEqual(normalize_event(raw)['status'], 'scheduled')

    def test_exact_game_gets_official_prime_video(self):
        games=[event('nba','San Antonio Spurs vs Houston Rockets','2026-10-24T01:30:00Z')]
        change=apply_verified_listings(games,date(2026,10,23))
        self.assertEqual(change, {'matchedEvents':1,'channelsAdded':1})
        self.assertEqual(games[0]['broadcasts'][0]['channel'],'Prime Video')
        self.assertEqual(games[0]['broadcasts'][0]['marketScope'],'national_streaming')
        self.assertIn('nba.com/spurs',games[0]['broadcasts'][0]['evidenceURL'])

    def test_tulsa_cw_and_nhl_are_distinct_games(self):
        games=[event('ncaa_football','Tulsa vs Army','2026-10-24T00:30:00Z'),
               event('ncaa_football','Wyoming vs Air Force','2026-10-24T01:00:00Z'),
               event('nhl','Colorado Avalanche vs Tampa Bay Lightning','2026-10-24T01:00:00Z')]
        change=apply_verified_listings(games,date(2026,10,23))
        self.assertEqual(change['matchedEvents'],3)
        self.assertEqual([b['channel'] for b in games[0]['broadcasts']], ['ESPN2'])
        self.assertEqual([b['channel'] for b in games[1]['broadcasts']], ['The CW'])
        self.assertEqual({b['channel'] for b in games[2]['broadcasts']},
                         {'Altitude Sports','Altitude+','The Spot - Tampa Bay 66'})
        annotate_events(games)
        self.assertEqual(games[2]['broadcasts'][0]['distribution'],'regional_network')
        self.assertIn('Colorado', games[2]['broadcasts'][0]['accessNote'])

    def test_never_apply_wrong_date_or_opponent(self):
        games=[event('nba','San Antonio Spurs vs Dallas Mavericks','2026-10-24T01:30:00Z'),
               event('nba','San Antonio Spurs vs Houston Rockets','2026-10-25T01:30:00Z')]
        change=apply_verified_listings(games,date(2026,10,23))
        self.assertEqual(change['matchedEvents'],0)
        self.assertTrue(all(not g['broadcasts'] for g in games))
        previous=event('nba','San Antonio Spurs vs Houston Rockets','2026-10-24T01:30:00Z')
        self.assertEqual(apply_verified_listings([previous],date(2026,10,24))['matchedEvents'],0)

    def test_merge_existing_altitude_keeps_both_provenances_and_market(self):
        from app.normalization import merge_channels
        events=[event('nhl','Colorado Avalanche vs Tampa Bay Lightning','2026-10-24T01:00:00Z')]
        events[0]['broadcasts']=[{'id':'a','channel':'Altitude Sports','source':'TheSportsDB','confidence':'provider_listed'}]
        result=apply_verified_listings(events,date(2026,10,23))
        self.assertEqual(result['channelsAdded'],2)
        altitude=events[0]['broadcasts'][0]
        self.assertEqual(altitude['marketScope'],'Colorado Avalanche territory')
        self.assertEqual(len(altitude['sources']),2)

    def test_aggregator_applies_official_corrections_after_reconciliation(self):
        import asyncio
        from unittest.mock import patch
        from app.aggregation import fetch_schedule
        day = date(2026, 10, 23)
        main = event('nba', 'San Antonio Spurs vs Houston Rockets', '2026-10-24T01:30:00Z')
        primary = {'events':[main], 'coverage_note':'','diagnostics':{}, 'source':'TheSportsDB'}
        with patch('app.aggregation.fetch_tsdb', return_value=primary), \
             patch('app.aggregation.supplemental_schedule', return_value=([],[])), \
             patch('app.aggregation.authorized_events', return_value=([],None)):
            result=asyncio.run(fetch_schedule(day,'America/Chicago'))
        self.assertEqual(result['verifiedCorrections'],{'matchedEvents':1,'channelsAdded':1})
        self.assertEqual(result['events'][0]['broadcasts'][0]['channel'],'Prime Video')
        self.assertEqual(result['quality']['missingUSListings'],0)

    def test_late_night_utc_matching(self):
        games=[event('ncaa_football','Wyoming vs Air Force','2026-10-24T01:00:00Z')]
        self.assertEqual(apply_verified_listings(games,date(2026,10,23))['channelsAdded'],1)

if __name__ == '__main__': unittest.main()
