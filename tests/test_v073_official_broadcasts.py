"""v0.7.3: immutable official-schedule matching and safety regression tests."""
from datetime import date
from unittest.mock import patch
import asyncio
from app.verified_listings import apply_verified_listings
from app.aggregation import fetch_schedule
from app.broadcast_intelligence import annotate_events

DAY = date(2026,10,24)

def game(name, timestamp='2026-10-24T16:00:00Z', sport='ncaa_football', channel=None):
    return {'id': 'demo-'+name, 'sport':sport,'title':name,
            'startTime':timestamp, 'broadcasts': ([{'id':'0','channel':channel,'source':'TheSportsDB',
                'confidence':'provider_listed'}] if channel else []),
            'status':'scheduled','league':'college football','scheduleSources':['TheSportsDB']}

def test_lsu_auburn_abc_both_orders_and_school_aliases():
    for name in ('Auburn vs LSU','LSU at Auburn','LSU Tigers @ Auburn Tigers'):
        ev=game(name)
        result=apply_verified_listings([ev],DAY)
        assert result == {'matchedEvents':1,'channelsAdded':1}
        assert ev['broadcasts'][0]['channel']=='ABC'
        assert 'abc.com/news' in ev['broadcasts'][0]['evidenceURL']
        assert ev['broadcasts'][0]['listingAuthority']=='official_published_schedule'

def test_other_verified_abc_games():
    evs=[game('Texas A&M Aggies vs Alabama Crimson Tide','2026-10-24T19:30:00Z'),
         game('Ole Miss Rebels vs Texas Longhorns','2026-10-24T23:30:00Z')]
    result=apply_verified_listings(evs,DAY)
    assert result['matchedEvents']==2
    assert all(e['broadcasts'][0]['channel']=='ABC' for e in evs)

def test_abc_first_on_compact_row_and_other_sources_preserved():
    ev=game('Auburn vs LSU', channel='Some Regional Listing')
    result=apply_verified_listings([ev],DAY)
    assert result['channelsAdded']==1
    assert [b['channel'] for b in ev['broadcasts']]==['ABC','Some Regional Listing']

def test_dedupe_abc_keeps_provenance():
    ev=game('Auburn vs LSU', channel='ABC')
    result=apply_verified_listings([ev],DAY)
    assert result['channelsAdded']==0
    assert len(ev['broadcasts'])==1
    assert ev['broadcasts'][0]['evidenceURL'].startswith('https://abc.com')
    assert len(ev['broadcasts'][0]['sources'])==2

def test_never_attach_wrong_day_sport_opponent_or_time():
    cases=[(game('Auburn vs LSU'),date(2026,10,25)),
           (game('Auburn vs Alabama'),DAY),
           (game('Auburn vs LSU', sport='nba'),DAY),
           (game('Auburn vs LSU','2026-10-24T18:00:00Z'),DAY),
           (game('Auburn vs LSU',None),DAY)]
    for ev,day in cases:
        assert apply_verified_listings([ev],day)['matchedEvents']==0
        assert ev['broadcasts']==[]

def test_2026_10_31_schedule_is_date_scoped():
    game31=game('Florida at Georgia','2026-10-31T19:30:00Z')
    assert apply_verified_listings([game31],date(2026,10,31))['channelsAdded']==1
    assert apply_verified_listings([game('Florida at Georgia','2026-10-31T19:30:00Z')],DAY)['channelsAdded']==0

def test_aggegrator_keeps_abc_and_coverage_coherent():
    first=game('Auburn vs LSU')
    primary={'events':[first],'coverage_note':'','diagnostics':{},'source':'TheSportsDB'}
    with patch('app.aggregation.fetch_tsdb',return_value=primary), \
         patch('app.aggregation.supplemental_schedule',return_value=([],[])), \
         patch('app.aggregation.sports_airings',return_value=([],{'provider':'Gracenote','sport':'all','status':'disabled','events':0})), \
         patch('app.aggregation.authorized_events',return_value=([],None)), \
         patch('app.aggregation.fetch_livescores',return_value=({}, {'status':'disabled'})):
        result=asyncio.run(fetch_schedule(DAY,'America/Chicago'))
    assert result['verifiedCorrections']['channelsAdded']==1
    assert result['events'][0]['broadcasts'][0]['channel']=='ABC'
    assert result['coverage']['withBroadcast']==1
    assert result['events'][0]['broadcastStatus']=='source_listed'

def test_configurable_editor_supplied_manifest(tmp_path,monkeypatch):
    import json
    path=tmp_path/'official.json'
    path.write_text(json.dumps([{'date':'2026-10-24','sport':'ncaa_football',
                                 'matchup':['Brown','Cornell'],'utcStart':'2026-10-24T15:00:00Z',
                                 'broadcasts':[{'channel':'Example Test Channel',
                                                'source':'Authorized Example Fixture',
                                                'evidenceURL':'https://example.org/example',
                                                'verifiedOn':'2026-10-24'}]}]))
    monkeypatch.setenv('SPORTSLINEUP_VERIFIED_LISTINGS_PATH',str(path))
    ev=game('Brown vs Cornell','2026-10-24T15:00:00Z')
    assert apply_verified_listings([ev],DAY)['channelsAdded']==1
    assert ev['broadcasts'][0]['channel']=='Example Test Channel'
