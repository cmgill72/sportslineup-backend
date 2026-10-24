import httpx
import pytest
from app import team_badges
from app.main import app
from app.licensed_feed import licensed_listings


def test_disabled_team_badges(monkeypatch):
    monkeypatch.delenv('SPORTSLINEUP_ENABLE_TEAM_BADGES', raising=False)
    assert not team_badges.allow_team_badges()


def test_exact_team_name_prevents_wrong_badge(monkeypatch):
    monkeypatch.setenv('SPORTSLINEUP_ENABLE_TEAM_BADGES','true')
    url='https://r2.thesportsdb.com/images/media/team/badge/example.png'
    response={'teams':[{'strTeam':'San Antonio Spurs','strBadge':url}]}
    assert team_badges.select_badge('San Antonio Spurs',response) == url
    assert team_badges.select_badge('Houston Rockets',response) is None


def test_logo_rejects_untrusted_hosts(monkeypatch):
    monkeypatch.setenv('SPORTSLINEUP_ENABLE_TEAM_BADGES','true')
    assert team_badges.select_badge('Spurs',{'teams':[{'strTeam':'Spurs','strBadge':'https://evil.example/logo.png'}]}) is None


@pytest.mark.asyncio
async def test_team_badge_route_uses_disabled_fallback(monkeypatch):
    monkeypatch.setenv('SPORTSLINEUP_ENABLE_TEAM_BADGES','false')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        result=await client.get('/v1/teams/badge',params={'name':'San Antonio Spurs'})
    assert result.status_code==200
    assert result.json()['status']=='disabled'
    assert result.json()['badgeURL'] is None


@pytest.mark.asyncio
async def test_cached_team_badge_lookup(monkeypatch):
    monkeypatch.setenv('SPORTSLINEUP_ENABLE_TEAM_BADGES','true')
    team_badges.CACHE.clear()
    async def mock_send(request):
        return httpx.Response(200, json={'teams':[{'strTeam':'San Antonio Spurs','strBadge':'https://r2.thesportsdb.com/images/media/team/badge/allowed.png'}]})
    result = await team_badges.badge_lookup('San Antonio Spurs', transport=httpx.MockTransport(mock_send))
    assert result['status']=='found'
    assert result['badgeURL'].startswith('https://r2.thesportsdb.com')
    assert 'key' not in result


def test_licensed_logo_host_is_opt_in(tmp_path, monkeypatch):
    import json
    from datetime import date
    day=date(2026,10,24)
    path=tmp_path/'licensed.json'
    path.write_text(json.dumps([{'providerEventId':'1234567','date':'2026-10-24',
        'channel':'ABC','country':'US','source':'Licensed test',
        'logoURL':'https://assets.partner.example/abc.png'}]))
    monkeypatch.setenv('SPORTSLINEUP_LICENSED_LISTINGS_PATH',str(path))
    monkeypatch.delenv('SPORTSLINEUP_LICENSED_LOGO_HOSTS',raising=False)
    assert 'logoURL' not in licensed_listings(day)['1234567'][0]
    monkeypatch.setenv('SPORTSLINEUP_LICENSED_LOGO_HOSTS','assets.partner.example')
    assert licensed_listings(day)['1234567'][0]['logoURL'].endswith('abc.png')
