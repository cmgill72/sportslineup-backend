import json
import os
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.main import app
from app import network_logos as catalog

class NetworkLogoTests(unittest.TestCase):
    def setUp(self):
        catalog._cache = None
        catalog._cache_time = 0.0

    def test_preview_delivers_reviewed_png_images(self):
        with patch.dict(os.environ, {'SPORTSLINEUP_NETWORK_LOGO_MODE': 'preview'}, clear=False):
            res=TestClient(app).get('/v1/networks/logos')
            self.assertEqual(res.status_code, 200)
            data=res.json()
            self.assertEqual(data['mode'],'preview')
            abc=catalog.resolve_network_name('ABC US',data['networks'])
            self.assertTrue(abc['imageURL'].startswith('https://commons.wikimedia.org/'))
            self.assertEqual(abc['displayStatus'],'image')
            self.assertIn('max-age=',res.headers.get('cache-control',''))

    def test_unknown_network_gets_fallback(self):
        rows=catalog.validate_catalog(json.loads(catalog.CATALOG_FILE.read_text()))
        self.assertIsNone(catalog.resolve_network_name('New future network 2030',rows))

    def test_espn_vs_espn_plus_vs_espn2(self):
        rows=catalog.validate_catalog(json.loads(catalog.CATALOG_FILE.read_text()))
        self.assertEqual(catalog.resolve_network_name('ESPN',rows)['id'],'espn')
        self.assertEqual(catalog.resolve_network_name('ESPN+',rows)['id'],'espnplus')
        self.assertEqual(catalog.resolve_network_name('ESPN2',rows)['id'],'espn2')
        self.assertEqual(catalog.resolve_network_name('CBS Sports Network',rows)['id'],'cbs_sports')
        self.assertEqual(catalog.resolve_network_name('CBS',rows)['id'],'cbs')
        self.assertEqual(catalog.resolve_network_name('FS1',rows)['id'],'foxsports1')
        self.assertEqual(catalog.resolve_network_name('FOX',rows)['id'],'fox')

    def test_disabled_and_licensed_hide_preview_assets(self):
        for mode in ('disabled','licensed'):
            catalog._cache=None
            with patch.dict(os.environ,{'SPORTSLINEUP_NETWORK_LOGO_MODE':mode},clear=False):
                data=TestClient(app).get('/v1/networks/logos').json()
                self.assertTrue(all(row['imageURL'] is None for row in data['networks']))

    def test_unsafe_urls_rejected_and_aliases_preserved(self):
        base={'networks':[{'id':'x','name':'Example','aliases':['Foo'],
                           'imageURL':'http://127.0.0.1/private','permission':'licensed'}]}
        row=catalog.validate_catalog(base)[0]
        self.assertIsNone(row['imageURL'])
        self.assertEqual(row['aliases'],['Foo'])
        self.assertFalse(catalog.safe_https_image('https://example.com/path.png'))
        self.assertFalse(catalog.safe_https_image('file:///etc/passwd'))
        self.assertTrue(catalog.safe_https_image('https://commons.wikimedia.org/wiki/Special:FilePath/ABC-2021-LOGO.png'))

    def test_rights_documentation_present_for_preview_image(self):
        rows=catalog.validate_catalog(json.loads(catalog.CATALOG_FILE.read_text()))
        preview=[r for r in rows if r['permission']=='development_preview']
        self.assertGreaterEqual(len(preview),5)
        for row in preview:
            self.assertTrue(row['sourceURL'].startswith('https://commons.wikimedia.org'))
            self.assertIn('trademark',row['licenseReview'].lower())

if __name__=='__main__': unittest.main()
