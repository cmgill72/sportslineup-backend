#!/usr/bin/env python3
"""Run from a networked Mac after deploying: python3 Backend/scripts/check_artwork.py"""
from __future__ import annotations
import json, sys
from urllib.parse import quote
from urllib.request import urlopen, Request
BASE=(sys.argv[1] if len(sys.argv)>1 else 'https://sportslineup-api.onrender.com').rstrip('/')

def fetch(path):
    with urlopen(Request(BASE+path,headers={'User-Agent':'SportsLineup-QA/0.7.7'}),timeout=15) as reply:
        return json.loads(reply.read().decode())

for path in ('/health','/v1/artwork/status','/v1/teams/badge?name='+quote('San Antonio Spurs'),'/v1/networks/logos'):
    print('\nEndpoint:',path)
    try:
        result=fetch(path)
        if path.endswith('logos'):
            rows=result.get('networks',[])
            print('Catalog mode:',result.get('mode'))
            print('Image references:',[r['name'] for r in rows if r.get('imageURL')])
            print('Fallback count:',sum(not r.get('imageURL') for r in rows))
        else:
            print(json.dumps(result,indent=2,default=str))
    except Exception as ex:
        print('FAILED:',type(ex).__name__,str(ex))
print('\nDo not paste your provider API key into this script.')
