"""Premium V2 game details: source fidelity, auth safety, graceful no-data."""
import asyncio
import httpx
from fastapi.testclient import TestClient
from app import box_scores
from app.main import app


def provider(handler, event_id='tsdb-1937584'):
    return asyncio.run(box_scores.fetch_box_score(event_id, transport=httpx.MockTransport(handler)))


def test_provider_team_stats_and_competitor_results(monkeypatch):
    monkeypatch.setenv('SPORTSDB_API_KEY', 'mock-premium-key')
    def handler(request):
        assert request.headers['X-API-KEY'] == 'mock-premium-key'
        assert '/api/v2/json/lookup/' in str(request.url)
        if 'event_stats' in request.url.path:
            return httpx.Response(200,json={'lookup':[
                {'idEvent':'1937584','strStat':'Total Shots','intHome':'24','intAway':'3'},
                {'idEvent':'1937584','strStat':'Possession','intHome':0,'intAway':'100'},
                {'idEvent':'OTHER','strStat':'Wrong Event','intHome':'4','intAway':'6'},
            ]})
        return httpx.Response(200,json={'lookup':[
            {'idEvent':'1937584','idPlayer':'9','strPlayer':'Example Competitor','intPosition':1,'intPoints':0},
            {'idEvent':'1234567','idPlayer':'10','strPlayer':'Wrong athlete','intPosition':3},
        ]})
    result=provider(handler)
    assert result['status']=='available'
    assert result['teamStats']==[
        {'label':'Total Shots','home':'24','away':'3'},
        {'label':'Possession','home':'0','away':'100'},
    ]
    assert result['results']==[{'id':'1937584-9-0','name':'Example Competitor','position':'1','points':'0','result':None}]
    assert 'mock-premium-key' not in str(result)


def test_premium_missing_or_demo_key(monkeypatch):
    monkeypatch.delenv('SPORTSDB_API_KEY', raising=False)
    assert provider(lambda request: 1)['status']=='not_configured'
    monkeypatch.setenv('SPORTSDB_API_KEY','123')
    assert provider(lambda request: 1)['status']=='not_configured'


def test_reject_untrusted_ids_without_requests(monkeypatch):
    monkeypatch.setenv('SPORTSDB_API_KEY','mock-premium-key')
    def bad_handler(request):
        raise AssertionError('Do not fetch stats for unmatched or path-injected event IDs')
    for event_id in ['licensed-event-30','espn-5599','tsdb-../bad','tsdb-23','tsdb-1234567?x=1']:
        assert provider(bad_handler,event_id)['status']=='unsupported'


def test_empty_provider_rows_are_not_fake_box_scores(monkeypatch):
    monkeypatch.setenv('SPORTSDB_API_KEY','mock-premium-key')
    result=provider(lambda request: httpx.Response(200,json={'lookup':None}))
    assert result['status']=='no_data' and result['teamStats']==[] and result['results']==[]


def test_one_provider_partially_down_and_the_other_has_data(monkeypatch):
    monkeypatch.setenv('SPORTSDB_API_KEY','mock-premium-key')
    def handler(request):
        if 'event_stats' in request.url.path:
            return httpx.Response(502, json={'error':'unavailable'})
        return httpx.Response(200,json={'lookup':[{'idEvent':'1937584','strPlayer':'Runner',
                                                  'intPosition':'2','strResult':'2nd place'}]})
    result=provider(handler)
    assert result['status']=='available' and not result['teamStats']
    assert result['results'][0]['result']=='2nd place'


def test_auth_error_does_not_disclose_secret(monkeypatch):
    monkeypatch.setenv('SPORTSDB_API_KEY','SUPER_SECRET_KEY')
    result=provider(lambda request: httpx.Response(401,json={'error':'SUPER_SECRET_KEY'}))
    assert result['status']=='auth_failed'
    assert 'SUPER_SECRET_KEY' not in str(result)


def test_upstream_outage_is_gracefully_returned(monkeypatch):
    monkeypatch.setenv('SPORTSDB_API_KEY','mock-premium-key')
    result=provider(lambda request: httpx.Response(503,json={}))
    assert result['status']=='unavailable'


def test_fastapi_endpoint_exposes_only_safe_response(monkeypatch):
    async def mock_fetch(event_id):
        return box_scores._response(event_id,'no_data',note='No records')
    monkeypatch.setattr('app.main.fetch_box_score', mock_fetch)
    response=TestClient(app).get('/v1/events/tsdb-1234567/box-score')
    assert response.status_code==200 and response.json()['eventId']=='tsdb-1234567'
    assert response.json()['teamStats']==[]
