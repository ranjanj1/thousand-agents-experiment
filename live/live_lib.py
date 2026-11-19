"""OpenRouter credit check (a free call). The key is read from video-maker/.env and never printed or logged."""
import json, urllib.request
from pathlib import Path

ENV = Path.home() / 'Documents/Projects/video-maker/.env'


def credit():
    """Dollars left: the smaller of the account balance and the key's own limit."""
    k = [l.split('=', 1)[1].strip().strip('"\'') for l in ENV.read_text().splitlines() if l.startswith('OPENROUTER_API_KEY=')][0]
    h = {'Authorization': 'Bearer ' + k}
    c = json.load(urllib.request.urlopen(urllib.request.Request('https://openrouter.ai/api/v1/credits', headers=h), timeout=30))['data']
    kk = json.load(urllib.request.urlopen(urllib.request.Request('https://openrouter.ai/api/v1/key', headers=h), timeout=30))['data']
    acct = c['total_credits'] - c['total_usage']
    left = kk.get('limit_remaining')
    return round(min(acct, left) if left is not None else acct, 4)
