from app.normalization import normalize_event


def game(status, home, away):
    return {'idEvent':'1234567', 'strSport':'Basketball','strLeague':'NBA',
            'strEvent':'Rockets vs Spurs','strStatus':status,
            'strHomeTeam':'Spurs','strAwayTeam':'Rockets',
            'intHomeScore':home,'intAwayScore':away}


def test_completed_results_only():
    final=normalize_event(game('Match Finished', 110, 105))
    assert final['score'] and final['score']['state'] == 'final'
    assert normalize_event(game('Not Started', 0, 0))['score'] is None


def test_incomplete_score_never_invents_zero():
    assert normalize_event(game('Match Finished', None, 105))['score'] is None


def test_no_badge_by_default(monkeypatch):
    monkeypatch.delenv('SPORTSLINEUP_ENABLE_TEAM_BADGES', raising=False)
    raw=game('Match Finished', 100, 98)
    raw['strHomeTeamBadge']='https://www.thesportsdb.com/images/media/team/badge/yes.png'
    assert normalize_event(raw)['teamIdentity'] is None
