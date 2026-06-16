"""
resonance app pipeline — playlist CSV -> Discogs credit graph -> resonance scores.

A bounded, web-friendly version of the harvest+build scripts: fast enough for a
single web request (no big 1-hop, no alias-doc fetching). Bring-your-own-token.
"""
import csv, io, re, time, math
from collections import defaultdict
import requests
import igraph as ig
import leidenalg as la

DISCOGS = 'https://api.discogs.com'

ROLE_RULES = [
    (r'remix', 0.65), (r'produc', 1.00), (r'written|compos|songwriter|lyric', 0.90),
    (r'feat\.?|featuring', 0.85), (r'vocal|sung|voice', 0.80),
    (r'performer|instrument|synth|guitar|bass|drum|piano|keyboard|sax|percussion|horn|string', 0.80),
    (r'arrang', 0.70), (r'engineer|mix|record|dub', 0.40),
    (r'compiled|curat|selector|dj mix', 0.35), (r'master|lacquer|cut', 0.20),
    (r'design|photo|artwork|illustrat|layout|sleeve', 0.10),
]
def role_weight(role):
    if not role:
        return 1.0
    r = role.lower(); best = 0.0
    for pat, w in ROLE_RULES:
        if re.search(pat, r):
            best = max(best, w)
    return best or 0.5

SKIP_NAMES = {'various', 'no artist', 'unknown artist', '[no artist]', ''}


def parse_playlist_csv(text):
    """Accepts an Exportify-style CSV (or any CSV with track/artist columns)."""
    rows = []
    rdr = csv.DictReader(io.StringIO(text))
    # tolerant header matching
    fields = {f.lower().strip(): f for f in (rdr.fieldnames or [])}
    def pick(*cands):
        for c in cands:
            if c in fields:
                return fields[c]
        return None
    tcol = pick('track name', 'track', 'title', 'name')
    acol = pick('artist name(s)', 'artist name', 'artist', 'artists')
    if not acol:
        return rows
    for r in rdr:
        artist = (r.get(acol) or '').split(',')[0].strip()
        track = (r.get(tcol) or '').strip() if tcol else ''
        if artist:
            rows.append({'track': track, 'artist': artist})
    return rows


class Discogs:
    def __init__(self, token, ua='resonance-app/0.1 (+https://github.com/DPWelsh/resonance)'):
        self.s = requests.Session()
        self.s.headers.update({'Authorization': f'Discogs token={token}', 'User-Agent': ua})
        self._last = 0.0
    def get(self, path, params=None, _try=0):
        dt = time.time() - self._last
        if dt < 1.1:
            time.sleep(1.1 - dt)
        self._last = time.time()
        url = path if path.startswith('http') else DISCOGS + path
        try:
            r = self.s.get(url, params=params, timeout=30)
        except requests.RequestException:
            return None
        if r.status_code == 429 and _try < 6:
            time.sleep(int(r.headers.get('Retry-After', 5) or 5) + 1)
            return self.get(path, params, _try + 1)
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None
    def verify(self):
        return self.get('/oauth/identity')


def _resolve(dg, track, artist):
    d = dg.get('/database/search', {'type': 'release', 'artist': artist, 'track': track, 'per_page': 3})
    res = (d or {}).get('results') or []
    if not res:
        d = dg.get('/database/search', {'type': 'release', 'artist': artist, 'per_page': 3})
        res = (d or {}).get('results') or []
    return res[0]['id'] if res else None


def analyze(token, rows, progress=lambda *a: None, deep=False, max_tracks=80):
    """Returns {nodes, edges, top, discovery, stats}. progress(stage, done, total)."""
    rows = rows[:max_tracks]
    dg = Discogs(token)
    seed_names = {r['artist'].lower() for r in rows}

    # 1. resolve playlist tracks -> release ids
    rel_ids = []
    for i, r in enumerate(rows):
        rid = _resolve(dg, r['track'], r['artist'])
        if rid:
            rel_ids.append(rid)
        progress('resolve', i + 1, len(rows))
    rel_ids = list(dict.fromkeys(rel_ids))

    # 2. fetch full credits
    releases = []
    for i, rid in enumerate(rel_ids):
        rel = dg.get(f'/releases/{rid}')
        if rel:
            releases.append(rel)
        progress('credits', i + 1, len(rel_ids))

    # 2b. optional bounded 1-hop: pull other releases of the playlist's main artists
    if deep:
        main_ids = set()
        for rel in releases:
            for a in rel.get('artists', []):
                if a.get('name', '').lower() in seed_names and a.get('id'):
                    main_ids.add(a['id'])
        main_ids = list(main_ids)[:25]
        extra = []
        for i, aid in enumerate(main_ids):
            d = dg.get(f'/artists/{aid}/releases', {'sort': 'year', 'sort_order': 'desc', 'per_page': 12})
            for e in (d or {}).get('releases', [])[:12]:
                if e.get('role') == 'Main':
                    extra.append(str(e.get('main_release') or e.get('id')))
            progress('expand', i + 1, len(main_ids))
        for i, rid in enumerate(dict.fromkeys(extra)):
            if rid in (str(x) for x in rel_ids):
                continue
            rel = dg.get(f'/releases/{rid}')
            if rel:
                releases.append(rel)
            progress('deep', i + 1, len(extra))

    # 3. build graph
    nodes = {}
    edges = defaultdict(float)
    seed_ids = set()
    seen = set()
    for rel in releases:
        mid = rel.get('master_id') or 0
        if mid and mid in seen:
            continue
        if mid:
            seen.add(mid)
        rc = {}
        for a in rel.get('artists', []) + rel.get('extraartists', []):
            aid, nm = a.get('id'), (a.get('name') or '').strip()
            if not aid or aid == 0 or nm.lower() in SKIP_NAMES:
                continue
            is_main = a in rel.get('artists', [])
            w = 1.0 if is_main else role_weight(a.get('role'))
            n = nodes.setdefault(aid, {'id': aid, 'name': nm, 'in_playlist': nm.lower() in seed_names,
                                       'best_role': 0.0, 'releases': 0})
            n['best_role'] = max(n['best_role'], w)
            rc[aid] = max(rc.get(aid, 0), w)
            if nm.lower() in seed_names:
                seed_ids.add(aid)
        for aid in rc:
            nodes[aid]['releases'] += 1
        items = list(rc.items())
        for x in range(len(items)):
            for y in range(x + 1, len(items)):
                a, wa = items[x]; b, wb = items[y]
                edges[(min(a, b), max(a, b))] += min(wa, wb)

    if len(nodes) < 2:
        return {'nodes': [], 'edges': [], 'top': [], 'discovery': [], 'stats': {'tracks': len(rows), 'resolved': len(rel_ids), 'nodes': 0}}

    # 4. score: personalized PageRank seeded on playlist artists + Leiden clusters
    idx = {nid: i for i, nid in enumerate(nodes)}
    G = ig.Graph(n=len(nodes), directed=False)
    elist = [(idx[a], idx[b]) for (a, b) in edges]
    G.add_edges(elist)
    G.es['weight'] = [edges[e] for e in edges]
    reset = [1.0 if nid in seed_ids else 0.0 for nid in nodes]
    if sum(reset) == 0:
        reset = [1.0] * len(nodes)
    ppr = G.personalized_pagerank(reset=reset, weights='weight', damping=0.82)
    try:
        part = la.find_partition(G, la.RBConfigurationVertexPartition, weights='weight', seed=42)
        membership = part.membership
    except Exception:
        membership = [0] * len(nodes)
    mx = max(ppr) or 1.0
    for nid in nodes:
        i = idx[nid]
        nodes[nid]['resonance'] = round(100 * ppr[i] / mx, 2)
        nodes[nid]['cluster'] = membership[i]
        nodes[nid]['technical'] = nodes[nid]['best_role'] <= 0.4

    nl = list(nodes.values())
    top = sorted([n for n in nl if not n['technical']], key=lambda n: -n['resonance'])[:30]
    discovery = sorted([n for n in nl if not n['technical'] and not n['in_playlist']],
                       key=lambda n: -n['resonance'])[:30]
    out_nodes = [{'id': n['id'], 'label': n['name'], 'resonance': n['resonance'],
                  'cluster': n['cluster'], 'in_playlist': n['in_playlist'],
                  'technical': n['technical']} for n in nl]
    out_edges = [{'source': a, 'target': b, 'weight': round(w, 2)} for (a, b), w in edges.items()]
    slim = lambda L: [{'name': n['name'], 'resonance': n['resonance'], 'cluster': n['cluster'],
                       'in_playlist': n['in_playlist']} for n in L]
    return {'nodes': out_nodes, 'edges': out_edges, 'top': slim(top), 'discovery': slim(discovery),
            'stats': {'tracks': len(rows), 'resolved': len(rel_ids), 'nodes': len(nl), 'edges': len(edges)}}


if __name__ == '__main__':
    import sys, os
    token = os.environ.get('DISCOGS_TOKEN', '')
    rows = parse_playlist_csv(open(sys.argv[1]).read())
    print(f'parsed {len(rows)} tracks')
    res = analyze(token, rows, progress=lambda s, d, t: print(f'  {s}: {d}/{t}', end='\r'))
    print('\nstats:', res['stats'])
    print('top:', [f"{n['name']} ({n['resonance']})" for n in res['top'][:10]])
    print('discovery:', [n['name'] for n in res['discovery'][:10]])
