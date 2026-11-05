"""v0.9.8: exact-ID event enrichments, no fabrications, safe partial outages."""
import asyncio
import httpx
from fastapi.testclient import TestClient
from app import game_details
from app.main import app

ID='tsdb-1937584'

def run(handler, event_id=ID):
    return asyncio.run(game_details.fetch_game_details(event_id, transport=httpx.MockTransport(handler)))


def responder(request):
    assert request.headers['X-API-KEY']=='test-key-do-not-emit'
    assert request.url.path.startswith('/api/v2/json/lookup/')
    if 'event_stats' in request.url.path:
        return httpx.Response(200,json={'lookup':[
            {'idEvent':'1937584','strStat':'Shots on goal','intAway':'8','intHome':'11'},
            {'idEvent':'0000000','strStat':'Fake shots','intAway':'99','intHome':'99'}]})
    if 'event_results' in request.url.path:
        return httpx.Response(200,json={'lookup':[{'idEvent':'1937584','idPlayer':'12',
          'strPlayer':'Athlete Alpha','intPoints':'0','intPosition':'2'}]})
    if 'event_lineup' in request.url.path:
        return httpx.Response(200,json={'lookup':[
            {'idEvent':'1937584','idPlayer':'12','idTeam':'10','strTeam':'Blue',
              'strPlayer':'Athlete Alpha','intSquadNumber':'4','strPosition':'Forward',
              'strSubstitute':'No'},
            {'idEvent':'1937584','idPlayer':'13','strPlayer':'Athlete Beta','strSubstitute':'Yes'},
            {'idEvent':'9999999','idPlayer':'14','strPlayer':'Other Match'}]})
    if 'event_timeline' in request.url.path:
        return httpx.Response(200,json={'lookup':[
            {'idEvent':'1937584','idTimeline':'1','strTimeline':'Goal',
              'strTimelineDetail':'Left-foot finish','intTime':12,'strPeriod':'1H',
              'strPlayer':'Athlete Alpha','strTeam':'Blue'},
            {'idEvent':'1937584','idTimeline':'2','strTimeline':'Yellow Card',
              'intTime':32,'strPlayer':'Athlete Beta'},
            {'idEvent':'9999999','strTimeline':'Goal','intTime':99}]})
    raise AssertionError(request.url.path)


def test_provider_enrichment_and_exact_event(monkeypatch):
    monkeypatch.setenv('SPORTSDB_API_KEY','test-key-do-not-emit')
    result=run(responder)
    assert result['status']=='available'
    assert result['coverage']=={'teamStats':'available','results':'available',
                                'lineups':'available','timeline':'available'}
    assert result['teamStats']==[{'label':'Shots on goal','home':'11','away':'8'}]
    assert len(result['results'])==1 and result['results'][0]['points']=='0'
    assert len(result['lineups'])==2
    assert result['lineups'][0]['number']=='4'
    assert result['lineups'][0]['isSubstitute'] is False
    assert result['lineups'][1]['isSubstitute'] is True
    assert result['lineups'][1]['number'] is None
    assert len(result['timeline'])==2
    assert result['timeline'][0]['isScoring'] is True
    assert result['timeline'][1]['isScoring'] is False
    assert 'test-key-do-not-emit' not in str(result)


def test_untrusted_event_ids_never_fetch(monkeypatch):
    monkeypatch.setenv('SPORTSDB_API_KEY','test-key-do-not-emit')
    def fail(_): raise AssertionError('Unexpected outgoing API request')
    for item in ['espn-11', 'tsdb-../etc', 'tsdb-22', 'tsdb-12345?a=2', 'tsdb-']:
        assert run(fail,item)['status']=='unsupported'


def test_not_configured(monkeypatch):
    monkeypatch.delenv('SPORTSDB_API_KEY',raising=False)
    assert run(lambda _: 1)['status']=='not_configured'
    monkeypatch.setenv('SPORTSDB_API_KEY','123')
    assert run(lambda _: 1)['status']=='not_configured'


def test_empty_provider_result_no_fabrication(monkeypatch):
    monkeypatch.setenv('SPORTSDB_API_KEY','test-key-do-not-emit')
    result=run(lambda _:httpx.Response(200,json={'lookup':None}))
    assert result['status']=='no_data'
    assert all(not result[key] for key in ('teamStats','results','lineups','timeline'))
    assert all(status=='no_data' for status in result['coverage'].values())


def test_partial_provider_outage_does_not_erase_data(monkeypatch):
    monkeypatch.setenv('SPORTSDB_API_KEY','test-key-do-not-emit')
    def handler(request):
        if 'event_timeline' in request.url.path:
            return httpx.Response(503,json={})
        return responder(request)
    result=run(handler)
    assert result['status']=='available' and len(result['lineups'])==2
    assert result['coverage']['timeline']=='unavailable'
    assert result['coverage']['lineups']=='available'


def test_provider_auth_error_key_not_exposed(monkeypatch):
    monkeypatch.setenv('SPORTSDB_API_KEY','VERY_PRIVATE_TEST_VALUE')
    result=run(lambda _:httpx.Response(401,json={'bad':'VERY_PRIVATE_TEST_VALUE'}))
    assert result['status']=='auth_failed'
    assert 'VERY_PRIVATE_TEST_VALUE' not in str(result)


def test_upstream_limits_and_failures(monkeypatch):
    monkeypatch.setenv('SPORTSDB_API_KEY','test-key-do-not-emit')
    result=run(lambda _:httpx.Response(429,json={}))
    assert result['status']=='rate_limited'
    result=run(lambda _:httpx.Response(503,json={}))
    assert result['status']=='unavailable'


def test_timeline_avoids_invented_scores_and_time_units():
    result=game_details.normalize_timeline({'lookup':[{'idEvent':'1937584','strTimeline':'Penalty',
        'intTime':'0','strPeriod':'Q1','strComment':'Reported foul'}]},'1937584')
    assert result[0]['isScoring'] is False
    assert result[0]['time']=='0'
    assert result[0]['period']=='Q1'
    assert 'score' not in result[0]


def test_scoring_only_explicit_action_labels():
    rows=[{'idEvent':'1937584','strTimeline':x,'intTime':i} for i,x in enumerate(
        ['Goal','Field Goal','Touchdown','Home Run','Yellow Card','Shot','Goalkeeper save','Points'])]
    result=game_details.normalize_timeline({'lookup':rows},'1937584')
    assert [v['isScoring'] for v in result]==[True,True,True,True,False,False,False,False]


def test_lineup_does_not_claim_starting_when_unknown():
    result=game_details.normalize_lineup({'lookup':[{'idEvent':'1937584',
        'strPlayer':'Player One','strPosition':'Defense'}]},'1937584')
    assert result[0]['isSubstitute'] is None
    assert 'points' not in result[0]


def test_injection_and_duplicate_filter():
    data={'lookup':[{'idEvent':'1937584','idTimeline':'11','strTimeline':'Goal'},
                    {'idEvent':'1937584','idTimeline':'11','strTimeline':'Goal'},
                    {'idEvent':'OTHER','idTimeline':'12','strTimeline':'Goal'}]}
    assert len(game_details.normalize_timeline(data,'1937584'))==1


def test_api_route_contract(monkeypatch):
    async def fake(event_id):
        assert event_id==ID
        return game_details._empty_reply(event_id,'no_data','Missing')
    monkeypatch.setattr('app.main.fetch_game_details',fake)
    response=TestClient(app).get(f'/v1/events/{ID}/game-details')
    assert response.status_code==200
    assert response.json()['timeline']==[]
    assert response.json()['lineups']==[]
    assert 'coverage' in response.json()


def test_v094_box_score_route_still_exists():
    paths={route.path for route in app.routes}
    assert '/v1/events/{event_id}/box-score' in paths
    assert '/v1/events/{event_id}/game-details' in paths


def test_timeline_requires_actual_action_not_match_title():
    rows={'lookup':[{'idEvent':'1937584','strEvent':'Home vs Away','intTime':'4'}]}
    assert game_details.normalize_timeline(rows,'1937584')==[]
