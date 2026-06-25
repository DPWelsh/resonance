#!/usr/bin/env python3
"""Re-derive the artist field on every stored episode using the (improved)
parser — no network. Scrubs leftover episode numbers / date-like junk. Then
re-run graph.py to rebuild the adjacency graph from clean data."""
import json, os
from crawl import parse_artist

ROOT = os.path.dirname(os.path.abspath(__file__))
db = json.load(open(os.path.join(ROOT, 'rosters.json')))

changed = 0
for rec in db:
    for ep in rec.get('episodes', []):
        new, _ = parse_artist(ep.get('title', ''), rec['series'])
        if new != ep.get('artist'):
            changed += 1
        ep['artist'] = new
    rec['parsed_artists'] = sum(1 for e in rec.get('episodes', []) if e.get('artist'))

json.dump(db, open(os.path.join(ROOT, 'rosters.json'), 'w'), ensure_ascii=False, indent=1)
print(f'cleaned {changed} artist fields across {len(db)} series')
