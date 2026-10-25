"""Opt-in APNs delivery with durable local registry (use Render persistent disk).

SECURITY: no API keys or device tokens in public health responses; installation
secrets are hashed; event IDs must exist in authenticated provider scores; calls
are deduplicated. Deployment REQUIREMENTS: paid always-on Render web service,
persistent disk, APNs .p8 + bundle/team/key IDs, and consent by each user.
"""
from __future__ import annotations
import base64, hashlib, json, os, re, sqlite3, time, asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import httpx

_HEX = re.compile(r'^[0-9a-fA-F]+$')
_ALLOWED = {'interval','midpoint','closeGame','final'}


def configured() -> bool:
    return (os.getenv('SPORTSLINEUP_PUSH_ENABLED', '').lower() == 'true'
            and bool(os.getenv('SPORTSLINEUP_APNS_TEAM_ID'))
            and bool(os.getenv('SPORTSLINEUP_APNS_KEY_ID'))
            and bool(os.getenv('SPORTSLINEUP_APNS_PRIVATE_KEY'))
            and bool(os.getenv('SPORTSLINEUP_APNS_BUNDLE_ID'))
            and bool(os.getenv('SPORTSLINEUP_PUSH_DB_PATH')))


@contextmanager
def _db():
    path = os.getenv('SPORTSLINEUP_PUSH_DB_PATH', '')
    if not path: raise RuntimeError('Push database path not configured')
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # Device tokens and subscription preferences are sensitive. Do not create
    # a world-readable DB when deploying on shared hosts.
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute('PRAGMA busy_timeout=10000')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''CREATE TABLE IF NOT EXISTS devices (
        id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, token TEXT, updated INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS subscriptions (
        device_id TEXT NOT NULL, event_id TEXT NOT NULL, prefs TEXT NOT NULL,
        event_title TEXT NOT NULL, sport TEXT NOT NULL, expires INTEGER NOT NULL,
        activity_token TEXT, PRIMARY KEY(device_id,event_id));
    CREATE TABLE IF NOT EXISTS seen_scores (
        event_id TEXT PRIMARY KEY, score_json TEXT NOT NULL, changed INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS dispatched (
        device_id TEXT NOT NULL, event_id TEXT NOT NULL, milestone TEXT NOT NULL,
        PRIMARY KEY(device_id,event_id,milestone));''')
    conn.commit()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def check_device_id(value: str) -> bool:
    return bool(re.fullmatch(r'[a-zA-Z0-9-]{20,64}', value))


def check_install_key(value: str) -> bool:
    return len(value) == 64 and bool(_HEX.fullmatch(value))


def check_token(value: str) -> bool:
    return 32 <= len(value) <= 400 and bool(_HEX.fullmatch(value))


def register_device(device_id: str, install_key: str, apns_token: str) -> bool:
    if not configured() or not check_device_id(device_id) or not check_install_key(install_key) or not check_token(apns_token):
        return False
    digest = hashlib.sha256(install_key.encode()).hexdigest()
    with _db() as conn:
        previous = conn.execute('SELECT secret_hash FROM devices WHERE id=?',(device_id,)).fetchone()
        if previous and previous[0] != digest: return False
        conn.execute('INSERT INTO devices(id,secret_hash,token,updated) VALUES (?,?,?,?) '
                     'ON CONFLICT(id) DO UPDATE SET token=excluded.token, updated=excluded.updated',
                     (device_id,digest,apns_token,int(time.time())))
    return True


def authenticate(conn, device_id: str, install_key: str) -> bool:
    if not check_device_id(device_id) or not check_install_key(install_key): return False
    row = conn.execute('SELECT secret_hash FROM devices WHERE id=?',(device_id,)).fetchone()
    import hmac
    return bool(row and hmac.compare_digest(row[0], hashlib.sha256(install_key.encode()).hexdigest()))


def subscribe(device_id: str, install_key: str, event_id: str, title: str,
              sport: str, expires: int, prefs: dict, activity_token: str | None = None) -> bool:
    if not configured() or not 1 <= len(event_id) <= 150 or not 1 <= len(title) <= 180 or sport not in {
        'nfl','nba','mlb','ncaa_football','ncaa_basketball','nhl','pga','wnba','ufc'}: return False
    if not isinstance(prefs, dict) or set(prefs) - _ALLOWED or any(type(v) is not bool for v in prefs.values()): return False
    if expires < time.time() - 60 or expires > time.time() + 16*86400: return False
    if activity_token is not None and not check_token(activity_token): return False
    with _db() as conn:
        if not authenticate(conn,device_id,install_key): return False
        if not any(prefs.values()) and activity_token is None:
            conn.execute('DELETE FROM subscriptions WHERE device_id=? AND event_id=?',(device_id,event_id))
        else:
            conn.execute('INSERT INTO subscriptions(device_id,event_id,prefs,event_title,sport,expires,activity_token) '
                         'VALUES (?,?,?,?,?,?,?) ON CONFLICT(device_id,event_id) DO UPDATE SET '
                         'prefs=excluded.prefs,event_title=excluded.event_title,sport=excluded.sport,'
                         'expires=excluded.expires,activity_token=COALESCE(excluded.activity_token,subscriptions.activity_token)',
                         (device_id,event_id,json.dumps(prefs),title,sport,expires,activity_token))
    return True


def registered_event_ids() -> set[str]:
    if not configured(): return set()
    with _db() as conn:
        return {r[0] for r in conn.execute('SELECT DISTINCT event_id FROM subscriptions WHERE expires>?',(int(time.time()),))}


def _jwt() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    private = os.getenv('SPORTSLINEUP_APNS_PRIVATE_KEY', '').replace('\\n','\n')
    key = serialization.load_pem_private_key(private.encode(),password=None)
    if not isinstance(key,ec.EllipticCurvePrivateKey): raise ValueError('APNs key is not EC')
    def enc(obj): return base64.urlsafe_b64encode(json.dumps(obj,separators=(',',':')).encode()).rstrip(b'=').decode()
    header, payload = enc({'alg':'ES256','kid':os.environ['SPORTSLINEUP_APNS_KEY_ID']}), enc({'iss':os.environ['SPORTSLINEUP_APNS_TEAM_ID'],'iat':int(time.time())})
    signed = f'{header}.{payload}'.encode()
    der = key.sign(signed,ec.ECDSA(hashes.SHA256()))
    r,s = decode_dss_signature(der)
    sig = base64.urlsafe_b64encode(r.to_bytes(32,'big')+s.to_bytes(32,'big')).rstrip(b'=').decode()
    return f'{header}.{payload}.{sig}'


async def _send_apns(token: str, payload: dict, kind: str) -> bool:
    host = 'api.sandbox.push.apple.com' if os.getenv('SPORTSLINEUP_APNS_ENV','sandbox')=='sandbox' else 'api.push.apple.com'
    bundle = os.environ['SPORTSLINEUP_APNS_BUNDLE_ID']
    topic = bundle + ('.push-type.liveactivity' if kind=='liveactivity' else '')
    headers = {'authorization':'bearer '+_jwt(), 'apns-topic':topic,
               'apns-push-type':kind, 'apns-priority':'10' if kind=='alert' else '5',
               'content-type':'application/json'}
    async with httpx.AsyncClient(http2=True,timeout=12) as client:
        try:
            res = await client.post(f'https://{host}/3/device/{token}',headers=headers,json=payload)
            return res.status_code == 200
        except httpx.HTTPError:
            return False


def _interval_key(progress: str, sport: str) -> str | None:
    # Ignore clock-time changes within a period; only actual milestone
    # transitions produce alerts. No guessed progress for golf/MMA.
    if sport == 'mlb': pattern = r'(?:inning|inn|top|bottom)\s*(\d{1,2})'
    elif sport == 'nhl': pattern = r'(?:period|p)\s*(\d)'
    elif sport in {'nfl','nba','wnba','ncaa_football','ncaa_basketball'}:
        pattern = r'(?:quarter|q|half)\s*(\d)'
    else: return None
    match = re.search(pattern, progress,flags=re.IGNORECASE)
    return match.group(1) if match else None


def _milestones(before: dict | None, after: dict, sport: str = 'nba') -> list[str]:
    if not before: return [] # No fabricated transition on first worker poll.
    found = []
    prior = str(before.get('state','')).lower()
    now = str(after.get('state','')).lower()
    if now == 'final' and prior != 'final': found.append('final')
    prevprogress=str(before.get('progress','')).lower().strip()
    progress=str(after.get('progress','')).lower().strip()
    if progress != prevprogress:
        if any(x in progress for x in ['halftime','half time','intermission']) and not any(x in prevprogress for x in ['halftime','half time','intermission']):
            found.append('midpoint')
        elif now == 'live':
            prev_key, curr_key = _interval_key(prevprogress,sport), _interval_key(progress,sport)
            if prev_key and curr_key and curr_key != prev_key:
                found.append('interval:'+curr_key)
    if now == 'live' and sport in {'nfl','nba','wnba','ncaa_football','ncaa_basketball','nhl','mlb'}:
        threshold = {'nfl':7,'nba':5,'wnba':5,'ncaa_basketball':5,'ncaa_football':7,'nhl':1,'mlb':1}[sport]
        try:
            gap = abs(int(after['home'])-int(after['away']))
            oldgap = abs(int(before['home'])-int(before['away']))
            if gap <= threshold and oldgap > threshold: found.append('closeGame')
        except (KeyError,ValueError,TypeError): pass
    return found


async def dispatch_score_snapshot(scores: dict[str,dict]) -> dict:
    """Update activities and notify opted-in users only on confirmed transitions.
    Called from an always-on paid backend, NOT from client requests.
    """
    result = {'events':0,'notifications':0,'liveUpdates':0,'failed':0}
    if not configured(): return result
    with _db() as conn:
        now = int(time.time())
        conn.execute('DELETE FROM subscriptions WHERE expires < ?', (now,))
        targets = conn.execute('SELECT s.device_id,s.event_id,s.prefs,s.activity_token,d.token,s.event_title,s.sport '
             'FROM subscriptions s JOIN devices d ON s.device_id=d.id WHERE s.expires > ?', (now,)).fetchall()
        events = {r[1] for r in targets}
        for event_id in events:
            score = scores.get(event_id)
            if score is None: continue
            previous = conn.execute('SELECT score_json FROM seen_scores WHERE event_id=?',(event_id,)).fetchone()
            before = json.loads(previous[0]) if previous else None
            if before == score: continue
            conn.execute('INSERT INTO seen_scores(event_id,score_json,changed) VALUES (?,?,?) '
                         'ON CONFLICT(event_id) DO UPDATE SET score_json=excluded.score_json,changed=excluded.changed',
                         (event_id,json.dumps(score),now))
            result['events'] += 1
            current_sport = next((r[6] for r in targets if r[1] == event_id), 'nba')
            changes = _milestones(before,score,current_sport)
            for device_id, event, pref_json, activity_token, device_token, title, sport in targets:
                if event != event_id: continue
                prefs=json.loads(pref_json)
                for change in changes:
                    key = change.split(':',1)[0]
                    if not prefs.get(key): continue
                    if conn.execute('SELECT 1 FROM dispatched WHERE device_id=? AND event_id=? AND milestone=?',
                                    (device_id,event_id,change)).fetchone(): continue
                    payload={'aps':{'alert':{'title':title,'body':f'{change.replace("closeGame","Close game").title()}: score update available'},'sound':'default'},'eventId':event_id}
                    if await _send_apns(device_token,payload,'alert'):
                        conn.execute('INSERT OR IGNORE INTO dispatched VALUES (?,?,?)',(device_id,event_id,change))
                        result['notifications']+=1
                    else: result['failed']+=1
                if activity_token:
                    final = str(score.get('state','')).lower() == 'final'
                    aps={'timestamp':now, 'event':'end' if final else 'update',
                         'content-state':{'homeScore':int(score.get('home') or 0),
                                          'awayScore':int(score.get('away') or 0),
                                          'phase':str(score.get('progress') or '')[:30],
                                          'status':str(score.get('state') or 'scheduled')[:20]}}
                    if final: aps['dismissal-date']=now+300
                    if await _send_apns(activity_token,{'aps':aps},'liveactivity'):
                        result['liveUpdates']+=1
                    else: result['failed']+=1
    return result


async def worker_loop():
    from .live_scores import fetch_livescores
    while True:
        try:
            if configured() and registered_event_ids():
                scores, health = await fetch_livescores()
                if health.get('status')=='ok': await dispatch_score_snapshot(scores)
        except Exception as exc:
            # Never log device tokens or credentials.
            import logging
            logging.warning('SportsLineup APNs worker cycle failed (%s)', type(exc).__name__)
        await asyncio.sleep(max(60,int(os.getenv('SPORTSLINEUP_PUSH_POLL_SECONDS','120'))))
