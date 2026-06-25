#!/usr/bin/env python3
"""Collapse mixgraph/rosters.json into the adjacency graph.

Outputs:
  graph.json         node-link: artist + series nodes, artist->series edges (networkx/viz-ready)
  artist_index.json  per artist: series played (counts, first/last date), top co-occurring peers
  series_index.json  per series: roster size + most-similar series (shared-roster Jaccard)

The artist<->artist projection (two artists who appear on the same series) is the
"played-alongside-on-mixes" graph — the Rephonic-style adjacency that powers matching.
"""
import json, os, re, itertools
from collections import defaultdict, Counter

ROOT = os.path.dirname(os.path.abspath(__file__))


def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


# split B2B / collab entries into individual artists
_SPLIT = re.compile(r'\s+(?:b2b2b|b2b|vs\.?|w/|&|\+|x|and|feat\.?|ft\.?|invites|presents|pres\.?)\s+|,\s+', re.I)


_MONTHS = r'jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec'


def clean_part(p):
    p = re.sub(r'^\s*#?\d{1,4}\s+', '', p)                 # "053 Roberto Rodriguez" -> "Roberto Rodriguez"
    p = re.sub(r'\s*\([^)]*\)\s*$', '', p).strip(" -–—.&")
    if re.match(r'^\d{1,2}[-/.]\d{1,2}', p):               # date "09-06-17"
        return None
    if re.match(rf"^\d{{1,2}}(st|nd|rd|th)?\s+({_MONTHS})", p, re.I):  # "21st April '26"
        return None
    return p if sum(c.isalpha() for c in p) >= 2 else None


def split_artists(name):
    raw = [p.strip(" -–—.&") for p in _SPLIT.split(name or '') if p.strip(" -–—.&")]
    out = [c for c in (clean_part(p) for p in raw) if c]
    return out or ([clean_part(name)] if (name and clean_part(name)) else [])


def build():
    db = json.load(open(os.path.join(ROOT, 'rosters.json')))

    artist_series = defaultdict(lambda: defaultdict(int))   # akey -> {series: plays}
    artist_dates = defaultdict(list)                        # akey -> [dates]
    display = {}                                            # akey -> most common display name
    name_votes = defaultdict(Counter)
    series_artists = defaultdict(set)                       # series -> {akey}
    series_meta = {}

    for rec in db:
        s = rec['series']
        if not rec.get('episodes'):
            continue
        series_meta[s] = {'category': rec.get('category'), 'method': rec.get('method'),
                          'mixes': rec.get('count', 0)}
        for ep in rec['episodes']:
            if not ep.get('artist'):
                continue
            for a in split_artists(ep['artist']):
                k = norm(a)
                if len(k) < 2:
                    continue
                artist_series[k][s] += 1
                if ep.get('date'):
                    artist_dates[k].append(ep['date'])
                name_votes[k][a.strip()] += 1
                series_artists[s].add(k)

    for k, votes in name_votes.items():
        display[k] = votes.most_common(1)[0][0]

    # ---- artist<->artist co-occurrence (shared series) ----
    cooc = defaultdict(Counter)
    for s, ks in series_artists.items():
        for a, b in itertools.combinations(sorted(ks), 2):
            cooc[a][b] += 1
            cooc[b][a] += 1

    # ---- series<->series similarity (Jaccard on rosters) ----
    series_list = list(series_artists)
    sim = defaultdict(list)
    for i in range(len(series_list)):
        for j in range(i + 1, len(series_list)):
            a, b = series_list[i], series_list[j]
            A, B = series_artists[a], series_artists[b]
            inter = len(A & B)
            if inter:
                jac = inter / len(A | B)
                sim[a].append((b, inter, round(jac, 3)))
                sim[b].append((a, inter, round(jac, 3)))

    # ---- artist index ----
    artist_index = {}
    for k, ser in artist_series.items():
        ds = sorted(d for d in artist_dates[k] if d)
        peers = cooc[k].most_common(15)
        artist_index[k] = {
            'name': display[k],
            'n_series': len(ser),
            'total_plays_meta': sum(ser.values()),
            'series': dict(sorted(ser.items(), key=lambda x: -x[1])),
            'first_seen': ds[0] if ds else None,
            'last_seen': ds[-1] if ds else None,
            'top_peers': [{'artist': display.get(p, p), 'shared_series': c} for p, c in peers],
        }

    # ---- series index ----
    series_index = {}
    for s, ks in series_artists.items():
        nearest = sorted(sim[s], key=lambda x: -x[2])[:10]
        series_index[s] = {
            **series_meta.get(s, {}),
            'roster_size': len(ks),
            'similar_series': [{'series': b, 'shared_artists': n, 'jaccard': j} for b, n, j in nearest],
        }

    # ---- node-link graph (bipartite, viz/networkx-ready) ----
    nodes = [{'id': 'artist:' + k, 'label': display[k], 'kind': 'artist',
              'n_series': len(ser)} for k, ser in artist_series.items()]
    nodes += [{'id': 'series:' + s, 'label': s, 'kind': 'series',
               'roster': len(series_artists[s])} for s in series_artists]
    links = [{'source': 'artist:' + k, 'target': 'series:' + s, 'weight': c}
             for k, ser in artist_series.items() for s, c in ser.items()]

    json.dump({'nodes': nodes, 'links': links}, open(os.path.join(ROOT, 'graph.json'), 'w'),
              ensure_ascii=False)
    json.dump(artist_index, open(os.path.join(ROOT, 'artist_index.json'), 'w'), ensure_ascii=False, indent=1)
    json.dump(series_index, open(os.path.join(ROOT, 'series_index.json'), 'w'), ensure_ascii=False, indent=1)

    return artist_index, series_index, links


if __name__ == '__main__':
    ai, si, links = build()
    print(f'artists: {len(ai)}  |  series: {len(si)}  |  bipartite edges: {len(links)}')
    hubs = sorted(ai.values(), key=lambda a: -a['n_series'])[:12]
    print('\nMost-connected artists (play the most distinct series):')
    for h in hubs:
        print(f'  {h["name"]:<26} {h["n_series"]:>2} series')
