#!/usr/bin/env python3
"""Demo matcher: given an artist, recommend mix series to pitch — using the
played-alongside (co-occurrence) graph. Series that the artist's peers play
but the artist hasn't appeared on yet."""
import json, os, re, sys
from collections import Counter

ROOT = os.path.dirname(os.path.abspath(__file__))
AI = json.load(open(os.path.join(ROOT, 'artist_index.json')))


def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def find(name):
    return AI.get(norm(name))


def recommend(name, n=12):
    a = find(name)
    if not a:
        return None, []
    own = set(a['series'])
    rec = Counter()
    for p in a['top_peers']:
        pa = find(p['artist'])
        if not pa:
            continue
        for s in pa['series']:
            if s not in own:
                rec[s] += p['shared_series']
    return a, rec.most_common(n)


if __name__ == '__main__':
    who = ' '.join(sys.argv[1:]) or 'Yu Yang'
    a, recs = recommend(who)
    if not a:
        print(f'"{who}" not in the graph'); sys.exit()
    print(f'=== {a["name"]} ===')
    print('plays on:', ', '.join(a['series']))
    print('played-alongside peers:',
          ', '.join(f'{p["artist"]}({p["shared_series"]})' for p in a['top_peers'][:10]))
    print('\n>>> RECOMMENDED mix series to pitch:')
    for s, score in recs:
        print(f'   {s:<24} peer-signal {score}')
