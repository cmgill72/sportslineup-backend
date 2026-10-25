import unittest
from app.normalization import classify, normalize_event, broadcast_rows, timestamp, records

class NormalizationTests(unittest.TestCase):
    def test_sports(self):
        checks = [
            ('American Football', 'NFL', 'nfl'),
            ('Basketball', 'NBA', 'nba'),
            ('Basketball', 'WNBA', 'wnba'),
            ('Basketball', 'NCAA Division I', 'ncaa_basketball'),
            ('Baseball', 'MLB', 'mlb'),
            ('American Football', 'NCAA College Football', 'ncaa_football'),
            ('Ice Hockey', 'NHL', 'nhl'),
            ('Golf', 'PGA Tour', 'pga'),
            ('Fighting', 'UFC', 'ufc'),
        ]
        for sport, league, expected in checks:
            self.assertEqual(classify({'strSport': sport, 'strLeague': league}), expected)
    def test_nfl_is_separate_from_other_football_leagues(self):
        self.assertEqual(classify({'strSport':'American Football','strLeague':'NFL'}), 'nfl')
        self.assertEqual(classify({'strSport':'American Football','strLeague':'National Football League'}), 'nfl')
        self.assertEqual(classify({'strSport':'American Football','idLeague':'4391','strLeague':'Unknown'}), 'nfl')
        self.assertEqual(classify({'strSport':'American Football','strLeague':'NCAA Division I'}), 'ncaa_football')
        self.assertIsNone(classify({'strSport':'American Football','strLeague':'CFL'}))
        self.assertIsNone(classify({'strSport':'American Football','strLeague':'UFL'}))
        self.assertIsNone(classify({'strSport':'Fighting','strLeague':'Boxing'}))
    def test_us_only_dedupe(self):
        rows = broadcast_rows([
            {'idEvent':'42', 'strChannel':'NBC','strCountry':'United States'},
            {'idEvent':'42', 'strChannel':'NBC','strCountry':'USA'},
            {'idEvent':'42', 'strChannel':'TSN','strCountry':'Canada'},
        ])
        self.assertEqual(len(rows['42']), 1)
        self.assertEqual(rows['42'][0]['channel'], 'NBC')
    def test_timestamp_utc(self):
        self.assertEqual(timestamp({'strTimestamp':'2026-10-23T20:30:00'}), '2026-10-23T20:30:00Z')
    def test_missing_channels_not_fabricated(self):
        event = normalize_event({'idEvent':'123', 'strSport':'Basketball', 'strLeague':'NBA', 'strEvent':'A vs B', 'strTimestamp':'2026-10-23T22:00:00'}, {})
        self.assertEqual(event['broadcasts'], [])
        self.assertEqual(event['sport'], 'nba')
    def test_fallback_list_key(self):
        self.assertEqual(records({'tvevents':[{'id':'1'}]},'tvevents'), [{'id':'1'}])

if __name__ == '__main__': unittest.main()
