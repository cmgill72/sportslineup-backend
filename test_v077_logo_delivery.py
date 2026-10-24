"""v0.7.7: Verify logo configuration and direct CDN host paths."""
import json
from pathlib import Path
import httpx
import pytest
from app import network_logos as cat, team_badges
from app.main import app

@pytest.mark.asyncio
async def test_artwork_status_reports_disabled_mode_without_secrets(monkeypatch):
    monkeypatch.setenv('SPORTSLINEUP_ENABLE_TEAM_BADGES', 'false')
    cat._cache = None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        res=await client.get('/v1/artwork/status')
    assert res.status_code == 200
    value=res.json()
    assert value['teamBadgesEnabled'] is False
    assert value['networkImageReferences'] >= 6
    assert value['networkFallbackEntries'] > 0
    assert value['networkImagesLiveVerified'] is False
    assert 'key' not in value


def test_direct_image_origins_are_allowlisted_and_not_redirects():
    manifest=json.loads(cat.CATALOG_FILE.read_text())
    rows=cat.validate_catalog(manifest)
    for item in rows:
        if item['permission']=='development_preview' and item['imageURL']:
            assert item['imageURL'].startswith('https://upload.wikimedia.org/wikipedia/commons/')
            assert cat.safe_https_image(item['imageURL'])
            assert 'Special:FilePath' not in item['imageURL']


def test_ambiguous_provider_school_logo_must_not_mismatch(monkeypatch):
    monkeypatch.setenv('SPORTSLINEUP_ENABLE_TEAM_BADGES','true')
    url='https://r2.thesportsdb.com/images/media/team/badge/one.png'
    rows={'teams':[{'strTeam':'Auburn Tigers','strBadge':url},
                   {'strTeam':'Auburn Tigers Women','strBadge':url}]}
    assert team_badges.select_badge('Auburn', rows) is None
    assert team_badges.select_badge('Auburn Tigers', rows) == url
