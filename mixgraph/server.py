#!/usr/bin/env python3
"""mixgraph console — serve the underground mix-series graph to the frontend.

  python3 mixgraph/server.py    ->  http://127.0.0.1:8770

Loads rosters.json + the derived indexes, builds a per-artist dated event list
(the trajectory), and exposes search / artist-dossier / series endpoints.
"""
import os, re, json
from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse
from collections import defaultdict, Counter
import uvicorn
from graph import split_artists, norm

ROOT = os.path.dirname(os.path.abspath(__file__))
ROSTERS = json.load(open(os.path.join(ROOT, 'rosters.json')))
AI = json.load(open(os.path.join(ROOT, 'artist_index.json')))
SI = json.load(open(os.path.join(ROOT, 'series_index.json')))

app = FastAPI(title='mixgraph')

_MONTHS = {m: i + 1 for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'])}


def parse_date(s):
    """-> (iso 'YYYY-MM-DD' or None, year or None) across ISO / RSS / loose formats."""
    s = (s or '').strip()
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return s[:10], int(m.group(1))
    m = re.search(r'(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})', s)        # "11 Jun 2026"
    if m and m.group(2).lower()[:3] in _MONTHS:
        return f'{m.group(3)}-{_MONTHS[m.group(2).lower()[:3]]:02d}-{int(m.group(1)):02d}', int(m.group(3))
    m = re.search(r'\b(19|20)\d{2}\b', s)
    if m:
        return f'{m.group(0)}-01-01', int(m.group(0))
    return None, None


# ---- build per-artist dated events (the trajectory) -------------------------
EVENTS = defaultdict(list)           # akey -> [{date, year, series, title, url}]
SERIES_META = {}
for rec in ROSTERS:
    s = rec['series']
    SERIES_META[s] = {'category': rec.get('category'), 'method': rec.get('method'),
                      'count': rec.get('count', 0)}
    for ep in rec.get('episodes', []):
        if not ep.get('artist'):
            continue
        iso, yr = parse_date(ep.get('date'))
        for a in split_artists(ep['artist']):
            EVENTS[norm(a)].append({'date': iso, 'year': yr, 'series': s,
                                    'title': ep.get('title'), 'url': ep.get('url')})
for k in EVENTS:
    EVENTS[k].sort(key=lambda e: e['date'] or '0000')

STATS = {'artists': len(AI), 'series': len([s for s in SI if SI[s].get('roster_size')]),
         'mixes': sum(r.get('count', 0) for r in ROSTERS)}


def recommend(akey, n=14):
    a = AI.get(akey)
    if not a:
        return []
    own = set(a['series'])
    rec = Counter()
    for p in a.get('top_peers', []):
        pa = AI.get(norm(p['artist']))
        if not pa:
            continue
        for s in pa['series']:
            if s not in own:
                rec[s] += p['shared_series']
    out = []
    for s, score in rec.most_common(n):
        out.append({'series': s, 'signal': score,
                    'category': SERIES_META.get(s, {}).get('category'),
                    'roster': SI.get(s, {}).get('roster_size', 0)})
    return out


@app.get('/api/stats')
def stats():
    return STATS


@app.get('/api/search')
def search(q: str = ''):
    qn = norm(q)
    if len(qn) < 2:
        return []
    hits = []
    for k, a in AI.items():
        nm = a['name']
        nn = norm(nm)
        if nn == qn:
            score = 0
        elif nn.startswith(qn):
            score = 1
        elif qn in nn:
            score = 2
        else:
            continue
        hits.append((score, -a['n_series'], k, nm, a['n_series']))
    hits.sort()
    return [{'key': k, 'name': nm, 'n_series': ns} for _, _, k, nm, ns in hits[:14]]


@app.get('/api/artist/{key}')
def artist(key: str):
    a = AI.get(key)
    if not a:
        return JSONResponse({'error': 'not found'}, status_code=404)
    ev = EVENTS.get(key, [])
    years = [e['year'] for e in ev if e['year']]
    return {
        'name': a['name'], 'n_series': a['n_series'],
        'first': a.get('first_seen'), 'last': a.get('last_seen'),
        'year_min': min(years) if years else None, 'year_max': max(years) if years else None,
        'series': [{'series': s, 'plays': c, 'category': SERIES_META.get(s, {}).get('category')}
                   for s, c in a['series'].items()],
        'peers': a.get('top_peers', []),
        'recommend': recommend(key),
        'events': ev,
    }


@app.get('/api/series/{name}')
def series(name: str):
    info = SI.get(name)
    if not info:
        return JSONResponse({'error': 'not found'}, status_code=404)
    roster = sorted(
        ((AI[k]['name'], AI[k]['series'][name], AI[k]['n_series'])
         for k in AI if name in AI[k]['series']),
        key=lambda x: -x[1])
    return {'name': name, **info,
            'roster': [{'name': n, 'plays': p, 'n_series': ns} for n, p, ns in roster[:60]]}


@app.get('/')
def index():
    return FileResponse(os.path.join(ROOT, 'static', 'index.html'))


if __name__ == '__main__':
    print('mixgraph console -> http://127.0.0.1:8770')
    uvicorn.run(app, host='127.0.0.1', port=8770, log_level='warning')
