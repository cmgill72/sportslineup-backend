"""Regression checks for the user-facing Render troubleshooting workflow."""
from __future__ import annotations
import unittest
from unittest.mock import patch, AsyncMock
from datetime import datetime, timezone
import httpx
from app.main import app, CACHE
from app.provider import ProviderUnavailable


class ConnectivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        CACHE.clear()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://sportslineup-api.onrender.com')

    async def asyncTearDown(self):
        await self.client.aclose()
        CACHE.clear()

    async def test_health_reports_version_and_provider_mode_without_key(self):
        response = await self.client.get('/health')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['version'], '0.4.4')
        self.assertIn(body['sportsdbKeyMode'], ('public_preview', 'configured'))
        self.assertNotIn('SPORTSDB_API_KEY', body)

    async def test_empty_success_keeps_zero_as_real_result(self):
        empty = {
            'date': '2026-10-23', 'timezone': 'America/Chicago',
            'fetchedAt': '2026-10-23T23:00:00Z', 'source': 'Mock',
            'partial': True, 'events': [], 'coverage_note': 'No verified events from this provider.',
            'coverage': {'events': 0, 'withBroadcast': 0, 'withoutBroadcast': 0, 'sports': {}},
        }
        with patch('app.main.fetch_schedule', new_callable=AsyncMock, return_value=empty):
            response = await self.client.get('/v1/schedule?date=2026-10-23&tz=America%2FChicago')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['events'], [])

    async def test_failed_data_sources_get_explanatory_502_not_fake_games(self):
        with patch('app.main.fetch_schedule', new_callable=AsyncMock, side_effect=ProviderUnavailable('provider unavailable')):
            response = await self.client.get('/v1/schedule?date=2026-10-23&tz=America%2FChicago')
        self.assertEqual(response.status_code, 502)
        self.assertIn('SPORTSDB_API_KEY', response.json()['detail']['message'])


if __name__ == '__main__':
    unittest.main()
