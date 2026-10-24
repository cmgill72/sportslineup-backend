from unittest.mock import AsyncMock, patch
import os
from fastapi.testclient import TestClient
from app.main import app


def test_health_v07_and_no_key_leak(monkeypatch):
    monkeypatch.setenv('SPORTSDB_API_KEY', 'private-example')
    resp = TestClient(app).get('/health')
    assert resp.status_code == 200
    body = resp.json()
    assert body['version'] == '0.7.6'
    assert body['liveScoresEnabled'] is True
    assert 'private-example' not in str(body)


def test_scores_endpoint_source_failures_are_explicit():
    stub = ({'tsdb-1234567': {
        'home': 100, 'away': 95, 'progress':'Q4 2:25', 'state':'live',
        'source':'TheSportsDB V2 livescore', 'homeTeam':'Spurs','awayTeam':'Rockets'}},
        {'status':'ok'})
    with patch('app.main.fetch_livescores', AsyncMock(return_value=stub)):
        body = TestClient(app).get('/v1/scores').json()
    assert body['status'] == 'ok'
    assert body['scores'][0]['id'] == 'tsdb-1234567'
    assert body['scores'][0]['awayTeam'] == 'Rockets'
    assert 'key' not in str(body).lower()


def test_score_error_empty_not_502():
    with patch('app.main.fetch_livescores', AsyncMock(return_value=({}, {'status':'rate_limited'}))):
        response = TestClient(app).get('/v1/scores')
    assert response.status_code == 200
    assert response.json()['scores'] == []
    assert response.json()['status'] == 'rate_limited'
