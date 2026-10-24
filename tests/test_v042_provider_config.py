"""Regression suite for safe credentials, provider probes, and actionable 502s."""
from __future__ import annotations
import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
import httpx
from app.main import app, CACHE
from app.provider import probe_provider, credential_mode, ProviderUnavailable

ROOT = Path(__file__).resolve().parents[2]

class BlueprintSecurityTests(unittest.TestCase):
    def test_both_blueprints_keep_key_on_render(self):
        for path in [ROOT/'render.yaml', ROOT/'Backend/render.yaml']:
            txt = path.read_text()
            self.assertIn('- key: SPORTSDB_API_KEY\n        sync: false', txt)
            self.assertNotIn('- key: SPORTSDB_API_KEY\n        value:', txt)

    def test_credential_mode_from_environment(self):
        with patch.dict(os.environ, {'SPORTSDB_API_KEY': '123'}):
            self.assertEqual(credential_mode(), 'public_preview')
        with patch.dict(os.environ, {'SPORTSDB_API_KEY': 'PRIVATE-NEVER-LEAK'}):
            self.assertEqual(credential_mode(), 'configured')

class ProviderProbeTests(unittest.TestCase):
    def _run_probe(self, status=200, payload=None, key='SENSITIVE-PREMIUM-KEY'):
        seen = []
        def handler(req):
            seen.append(str(req.url))
            return httpx.Response(status, json=payload if payload is not None else {'events': []})
        with patch.dict(os.environ, {'SPORTSDB_API_KEY': key}):
            result = asyncio.run(probe_provider(transport=httpx.MockTransport(handler)))
        # Confirm the credential IS sent to provider but NEVER exposed in diagnostic JSON.
        self.assertIn(key, seen[0])
        self.assertNotIn(key, json.dumps(result))
        return result

    def test_ok_even_if_zero_events(self):
        result = self._run_probe(200, {'events': None})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['credentialMode'], 'configured')

    def test_provider_rejects_key_json(self):
        result = self._run_probe(400, {'Message': 'Invalid Premium API key'})
        self.assertEqual(result['status'], 'authentication_failed')
        self.assertEqual(result['upstreamStatus'], 400)

    def test_provider_200_invalid_key_message(self):
        self.assertEqual(self._run_probe(200, {'Message': 'Invalid Premium API key'})['status'], 'authentication_failed')

    def test_rate_limit_and_upstream_failure(self):
        self.assertEqual(self._run_probe(429)['status'], 'rate_limited')
        self.assertEqual(self._run_probe(503)['status'], 'upstream_error')

class ScheduleErrorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        CACHE.clear()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    async def asyncTearDown(self):
        await self.client.aclose()
        CACHE.clear()

    async def test_health_flags_configuration_not_secret(self):
        key='SENSITIVE-PREMIUM-KEY'
        with patch.dict(os.environ, {'SPORTSDB_API_KEY': key}):
            response = await self.client.get('/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['sportsdbKeyMode'], 'configured')
        self.assertTrue(response.json()['sportsdbKeyConfigured'])
        self.assertNotIn(key, response.text)

    async def test_schedule_preserves_safe_provider_error_code(self):
        secret='SENSITIVE-PREMIUM-KEY'
        with patch.dict(os.environ, {'SPORTSDB_API_KEY':secret}):
            with patch('app.main.fetch_schedule', new_callable=AsyncMock,
                       side_effect=ProviderUnavailable('SECRET STRING', 'authentication_failed')):
                result = await self.client.get('/v1/schedule?tz=America%2FChicago')
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.json()['detail']['errorCode'], 'authentication_failed')
        self.assertNotIn(secret, result.text)
        self.assertNotIn('SECRET STRING', result.text)

if __name__ == '__main__': unittest.main()
