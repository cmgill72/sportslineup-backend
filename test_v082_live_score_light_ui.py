from app.normalization import normalize_event
from app.live_scores import attach_scores, score_map
from fastapi.testclient import TestClient
from app.main import app


def event(status='Q3', home=88, away=81):
    return {'idEvent': '1234567', 'strSport': 'Basketball', 'strLeague': 'NBA',
            'strEvent': 'Houston Rockets vs. San Antonio Spurs', 'strStatus': status,
            'strAwayTeam': 'Houston Rockets', 'strHomeTeam': 'San Antonio Spurs',
            'intAwayScore': away, 'intHomeScore': home}


def test_source_record_live_score_is_visible():
    record=normalize_event(event())
    assert record['status']=='live'
    assert record['score']['state']=='live'
    assert record['score']['home']==88 and record['score']['away']==81


def test_future_game_zero_zero_not_a_score():
    assert normalize_event(event('Not Started', 0, 0))['score'] is None


def test_missing_numeric_live_score_stays_missing():
    assert normalize_event(event('Q3', None, 81))['score'] is None


def test_short_and_long_team_names_match_exact_event_id():
    raw=normalize_event(event('Not Started', None, None))
    score=score_map({'livescores': [{
        'idEvent':'1234567', 'strProgress':'Q3 6:25',
        'strHomeTeam':'Spurs', 'strAwayTeam':'Rockets',
        'intHomeScore':'88', 'intAwayScore':'81'}]})
    assert attach_scores([raw],score)==1
    assert raw['score']['home']==88


def test_mismatched_teams_still_rejected():
    raw=normalize_event(event('Not Started', None, None))
    score=score_map({'livescores': [{
        'idEvent':'1234567', 'strProgress':'Q3 6:25',
        'strHomeTeam':'Boston Celtics', 'strAwayTeam':'Golden State Warriors',
        'intHomeScore':'88', 'intAwayScore':'81'}]})
    assert attach_scores([raw],score)==0


def test_score_and_network_diagnostics_are_secret_free(monkeypatch):
    monkeypatch.delenv('SPORTSDB_API_KEY',raising=False)
    client=TestClient(app)
    sc=client.get('/v1/diagnostics/scores')
    assert sc.status_code==200 and 'feedRecords' in sc.json()
    nw=client.get('/v1/diagnostics/network-images')
    assert nw.status_code==200 and nw.json()['remoteImagesVerified'] is False
