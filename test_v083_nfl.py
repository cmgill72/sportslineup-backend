"""NFL-specific regression coverage: ninth sport, score and source matching."""
from datetime import date
from app.normalization import SPORTS, normalize_event, normalize_status
from app.live_scores import score_map, attach_scores
from app.authorized_events import parse_authorized_rows
from app.gracenote import _sport
from app.supplemental import ESPN_LEAGUES, parse_espn
from app.push_delivery import _interval_key, _milestones
from app.quality import quality_report


def nfl_event(event_id='1234567', status='Not Started'):
    return {'idEvent': event_id, 'idLeague': '4391', 'strSport': 'American Football',
            'strLeague': 'NFL', 'strEvent': 'Dallas Cowboys at Philadelphia Eagles',
            'strAwayTeam': 'Dallas Cowboys', 'strHomeTeam': 'Philadelphia Eagles',
            'strTimestamp': '2026-10-26T00:15:00+00:00', 'dateEvent': '2026-10-26',
            'strStatus': status}


def test_nfl_is_ninth_filter_and_mixed_feeds_do_not_conflate():
    assert len(SPORTS) == 9
    assert SPORTS.count('nfl') == 1
    assert normalize_event(nfl_event())['sport'] == 'nfl'
    college = {**nfl_event(), 'strLeague': 'NCAA Division I', 'idLeague': ''}
    assert normalize_event(college)['sport'] == 'ncaa_football'


def test_nfl_live_scores_match_event_id_and_correct_teams():
    event = normalize_event(nfl_event(status='Q3'))
    score = score_map({'livescores':[{'idEvent':'1234567', 'strProgress':'Q3 07:11',
                 'strHomeTeam':'Eagles', 'strAwayTeam':'Cowboys',
                 'intHomeScore':21, 'intAwayScore':17}]})
    assert attach_scores([event], score) == 1
    assert event['score']['home'] == 21 and event['status'] == 'live'
    other = normalize_event(nfl_event())
    bad = score_map({'livescores':[{'idEvent':'7654321', 'strProgress':'Q3 07:11',
                 'strHomeTeam':'Eagles', 'strAwayTeam':'Cowboys',
                 'intHomeScore':21, 'intAwayScore':17}]})
    assert attach_scores([other], bad) == 0


def test_nfl_status_quarters_are_not_guessed():
    assert normalize_status('Q4 08:05') == 'live'
    assert normalize_status('Q4 TBD') == 'scheduled'
    assert normalize_status('Final') == 'final'


def test_nfl_score_change_alerts_are_supported():
    assert _interval_key('Q3 07:11','nfl') == '3'
    prior = {'home':10,'away':0,'state':'live','progress':'Q2 01:30'}
    after = {'home':17,'away':14,'state':'live','progress':'Q3 08:11'}
    milestones = _milestones(prior,after,'nfl')
    assert 'interval:3' in milestones and 'closeGame' in milestones


def test_nfl_espt_and_gracenote_categorization():
    assert ESPN_LEAGUES['nfl'] == ('football','nfl')
    assert _sport({'title':'NFL: Cowboys at Eagles'}) == 'nfl'
    assert _sport({'title':'NCAA Football: Texas at Oklahoma'}) == 'ncaa_football'
    payload = {'events':[{'id':'99','date':'2026-10-25T17:00:00Z','name':'Browns at Titans',
        'competitions':[{'competitors':[{'team':{'displayName':'Cleveland Browns'}},
                                           {'team':{'displayName':'Tennessee Titans'}}],
                         'broadcasts':[{'names':['CBS'],'market':'national'}],
                         'status':{'type':{'name':'pre'}}}]}]}
    events = parse_espn(payload,'nfl',date(2026,10,25),'America/Chicago')
    assert len(events) == 1 and events[0]['sport']=='nfl'
    assert events[0]['broadcasts'][0]['channel']=='CBS'


def test_authorized_nfl_listing_and_nfl_quality_bucket():
    rows = [{'country':'US','sport':'nfl','source':'Licensed Guide','providerEventId':'fixture01',
             'title':'New York Giants at Houston Texans','startTime':'2026-10-25T17:00:00Z',
             'date':'2026-10-25', 'participants':['New York Giants','Houston Texans'],
             'broadcasts':[{'channel':'CBS'}]}]
    parsed = parse_authorized_rows(rows,date(2026,10,25),'America/Chicago')
    assert len(parsed)==1 and parsed[0]['sport']=='nfl'
    assert parsed[0]['broadcasts'][0]['channel']=='CBS'
    coverage=quality_report(parsed,[],{})
    assert coverage['bySport']['nfl']['returnedEvents']==1
    assert coverage['bySport']['nfl']['withUSListing']==1


def test_nfl_viewing_services_remain_distinct():
    from app.broadcast_intelligence import classify_channel
    assert classify_channel('NFL Network')['distribution'] == 'cable_network'
    assert classify_channel('NFL RedZone')['distribution'] == 'cable_network'
    assert classify_channel('NFL+')['distribution'] == 'streaming_service'
    assert classify_channel('NFL Sunday Ticket')['distribution'] == 'streaming_service'
    assert classify_channel('CBS')['distribution'] == 'over_the_air_network'


def test_dedicated_nfl_league_request_recovers_missing_mixed_feed(monkeypatch):
    """When generic American Football is truncated, league 4391 still provides NFL."""
    import asyncio
    import httpx
    from app.provider import fetch_schedule
    monkeypatch.setenv('SPORTSDB_API_KEY', 'test-premium-key')
    requests = []
    fixture = {**nfl_event(event_id='1234567'),
               'strTimestamp':'2026-10-25T17:00:00Z', 'dateEvent':'2026-10-25',
               'strEvent':'Cleveland Browns at Tennessee Titans',
               'strAwayTeam':'Cleveland Browns', 'strHomeTeam':'Tennessee Titans'}

    def handler(request):
        requests.append((request.url.path.rsplit('/',1)[-1], dict(request.url.params)))
        if request.url.path.endswith('/eventsday.php'):
            # Source-wide sport feed omitted this NFL event; filtered league feed finds it.
            return httpx.Response(200,json={'events':[fixture] if request.url.params.get('l')=='4391' else []})
        return httpx.Response(200,json={'tvevent':[]})

    guide = asyncio.run(fetch_schedule(date(2026,10,25),'America/Chicago',
                                      transport=httpx.MockTransport(handler)))
    assert len(guide['events'])==1
    assert guide['events'][0]['sport']=='nfl'
    assert guide['events'][0]['startTime']=='2026-10-25T17:00:00Z'
    assert any(path=='eventsday.php' and args.get('l')=='4391' for path,args in requests)
    assert guide['coverage']['sports']['nfl']['events']==1
