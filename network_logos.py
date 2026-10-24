"""Curated, alias-safe network thumbnails. Never general-purpose internet scraping.

Preview artwork is never advertised as App Store-cleared. A remote manifest can
replace the catalog without an iOS rebuild, but entry URLs and rights are validated.
"""
import asyncio
import json
import os
import re
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

import httpx

CATALOG_FILE = Path(__file__).with_name('network_logo_catalog.json')
_cache = None
_cache_time = 0.0
_lock = asyncio.Lock()
TTL_SECONDS = 3600
ALLOWED_IMAGE_HOSTS = {'commons.wikimedia.org', 'upload.wikimedia.org'}


def normalized_name(value: str) -> str:
    # Punctuation is intentional: ESPN and ESPN+ are different services.
    raw = (value or '').casefold().replace('&', 'and')
    raw = re.sub(r'\s+', ' ', raw).strip()
    raw = re.sub(r'\b(?:hd|usa)\b$', '', raw).strip()
    return re.sub(r'[^a-z0-9+]', '', raw)


def safe_https_image(url: str, allowed: set[str] | None = None) -> bool:
    if not isinstance(url, str) or len(url)>1500: return False
    parsed = urlsplit(url)
    domains = allowed or ALLOWED_IMAGE_HOSTS
    return (parsed.scheme == 'https' and parsed.hostname in domains and
            not parsed.username and not parsed.password and parsed.port in (None, 443))


def validate_catalog(raw: dict) -> list[dict]:
    if not isinstance(raw, dict) or not isinstance(raw.get('networks'), list):
        raise ValueError('Network catalog must have networks array')
    if len(raw['networks']) > 300: raise ValueError('Network catalog is too large')
    approved = set(ALLOWED_IMAGE_HOSTS)
    custom = os.getenv('SPORTSLINEUP_ALLOWED_NETWORK_IMAGE_HOSTS', '')
    approved.update(x.strip().lower() for x in custom.split(',') if x.strip() and '.' in x.strip())
    result=[]; seen=set()
    for item in raw['networks']:
        if not isinstance(item,dict): continue
        ident = str(item.get('id') or '').strip().lower()
        name = str(item.get('name') or '').strip()
        if not ident or not name or ident in seen or len(name)>100: continue
        seen.add(ident)
        url = item.get('imageURL')
        if url is not None and not safe_https_image(url, approved): url=None
        aliases=[a.strip() for a in item.get('aliases', []) if isinstance(a,str) and 0<len(a)<100][:12]
        result.append({'id':ident,'name':name,'aliases':aliases,'imageURL':url,
            'sourceURL':item.get('sourceURL') if isinstance(item.get('sourceURL'),str) else None,
            'source':str(item.get('source') or 'Unknown'),
            'licenseReview':str(item.get('licenseReview') or 'Not reviewed'),
            'permission':str(item.get('permission') or 'none')})
    return result


async def catalog_response() -> dict:
    global _cache,_cache_time
    if _cache is not None and monotonic()-_cache_time < TTL_SECONDS: return _cache
    async with _lock:
        if _cache is not None and monotonic()-_cache_time < TTL_SECONDS: return _cache
        local = json.loads(CATALOG_FILE.read_text())
        url = os.getenv('SPORTSLINEUP_NETWORK_LOGO_MANIFEST_URL','').strip()
        if url and url.startswith('https://') and urlsplit(url).hostname:
            try:
                async with httpx.AsyncClient(timeout=9,follow_redirects=False) as client:
                    response=await client.get(url,headers={'Accept':'application/json'})
                    response.raise_for_status()
                    if len(response.content)>180_000: raise ValueError('manifest oversized')
                    remote=response.json()
                    validate_catalog(remote)
                    local=remote
            except (httpx.HTTPError,ValueError,TypeError):
                pass # Local known-good catalog is safe offline fallback.
        mode=os.getenv('SPORTSLINEUP_NETWORK_LOGO_MODE','preview').strip().lower()
        if mode not in ('disabled','preview','licensed'): mode='disabled'
        output=[]
        for item in validate_catalog(local):
            allowed = item['permission']=='licensed' or (mode=='preview' and item['permission']=='development_preview')
            # In licensed mode, only entries individually marked licensed are shown.
            item['imageURL']=item['imageURL'] if mode!='disabled' and allowed else None
            item['displayStatus']=('image' if item['imageURL'] else 'fallback')
            output.append(item)
        _cache={'version':str(local.get('version',1)),'mode':mode,'networks':output,
                'warning':'Preview logos are for development review, not an App Store rights clearance.' if mode=='preview' else ''}
        _cache_time=monotonic()
        return _cache


def resolve_network_name(name:str, rows:list[dict]):
    needle=normalized_name(name)
    if not needle: return None
    for row in rows:
        if needle in {normalized_name(v) for v in [row['name'],*row.get('aliases',[])]}:
            return row
    return None
