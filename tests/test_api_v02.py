import asyncio
import unittest
from datetime import date
from unittest.mock import patch
import httpx
from app.main import app, CACHE


class APIv02Tests(unittest.TestCase):
    def request(self, path: str) -> httpx.Response:
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                return await client.get(path)
        return asyncio.run(run())

    def test_health_contract(self):
        response = self.request('/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['version'], '0.8.3')

    def test_unknown_timezone_rejected(self):
        response = self.request(f'/v1/schedule?date={date.today().isoformat()}&tz=Nonexistent%2FOrbit')
        self.assertEqual(response.status_code, 400)

    def test_time_zone_forwarded_to_provider(self):
        CACHE.clear()
        fake = {'date': date.today().isoformat(), 'events': [], 'source': 'mock',
                'partial': True, 'coverage_note': 'fixture', 'coverage': None}
        async def provider(day, zone):
            self.assertEqual(zone, 'America/Chicago')
            return fake
        with patch('app.main.fetch_schedule', side_effect=provider):
            response = self.request(f'/v1/schedule?date={date.today().isoformat()}&tz=America%2FChicago')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['events'], [])
        CACHE.clear()

if __name__ == '__main__': unittest.main()
