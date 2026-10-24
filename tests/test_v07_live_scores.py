import pytest
import httpx
from app.live_scores import score_map, attach_scores, safe_badge, fetch_livescores


def sample(home=95, away=90, progress='Q3 2:14'):
    return {'idEvent':'1234567', 'strSport':'Basketball', 'strHomeTeam':'San Antonio Spurs',
            'strAwayTeam':'Houston Rockets', 'intHomeScore':home, 'intAwayScore':away,
            'strProgress':progress, 'strHomeTeamBadge':'https://www.thesportsdb.com/images/media/team/badge/123.png'}


def test_parse_live_and_final():
    assert score_map({'livescores':[sample()]})['tsdb-1234567']['state'] == 'live'
    assert score_map({'livescores':[sample(progress='Final')]})['tsdb-1234567']['state'] == 'final'


def test_zero_is_real_score_but_unknown_is_not():
    assert score_map({'livescores':[sample(home=0, away=0)]})['tsdb-1234567']['home'] == 0
    assert score_map({'livescores':[sample(home=None)]}) == {}


def test_strict_id_no_collision_and_team_mismatch():
    e = {'id':'tsdb-1234567', 'status':'upcoming', '_participants':['Houston Rockets','San Antonio Spurs']}
    other = {'id':'licensed-1234567', 'status':'upcoming', '_participants':['Houston Rockets','San Antonio Spurs']}
    assert attach_scores([e,other], score_map({'livescores':[sample()]})) == 1
    assert 'score' not in other
    bad = {'id':'tsdb-1234567','status':'upcoming','_participants':['Golden State Warriors','Boston Celtics']}
    assert attach_scores([bad], score_map({'livescores':[sample()]})) == 0


def test_unknown_and_bad_shape():
    assert score_map({'livescores':None}) == {}
    with pytest.raises(ValueError): score_map({'surprise':[]})
    assert score_map({'events':[sample(progress='',home=20)]}) == {}


def test_no_remote_badges_without_permission(monkeypatch):
    monkeypatch.delenv('SPORTSLINEUP_ENABLE_TEAM_BADGES', raising=False)
    assert safe_badge('https://www.thesportsdb.com/images/media/team/badge/123.png') is None
    monkeypatch.setenv('SPORTSLINEUP_ENABLE_TEAM_BADGES','true')
    assert safe_badge('https://evil.example.com/images/logo.png') is None
    assert safe_badge('http://www.thesportsdb.com/images/a.png') is None
    assert safe_badge('https://www.thesportsdb.com/images/media/team/badge/123.png') is not None

@pytest.mark.asyncio
async def test_auth_header_not_public(monkeypatch):
    monkeypatch.setenv('SPORTSDB_API_KEY','secret-test-value')
    monkeypatch.setenv('SPORTSLINEUP_ENABLE_LIVE_SCORES','true')
    def handler(request):
        assert request.headers['X-API-KEY'] == 'secret-test-value'
        assert 'secret-test-value' not in str(request.url)
        return httpx.Response(200, json={'livescores':[sample()]})
    scores, status = await fetch_livescores(httpx.MockTransport(handler))
    assert status['status'] == 'ok'
    assert len(scores) == 1

@pytest.mark.asyncio
async def test_failures_do_not_break_schedule(monkeypatch):
    monkeypatch.setenv('SPORTSDB_API_KEY','secret-test-value')
    monkeypatch.setenv('SPORTSLINEUP_ENABLE_LIVE_SCORES','true')
    scores, status = await fetch_livescores(httpx.MockTransport(lambda req: httpx.Response(503)))
    assert not scores and status['status'] == 'upstream_error'


def test_allows_multiple_possible_response_keys():
    assert score_map({'livescore':[sample()]})
    assert score_map({'scores':[sample()]})
