#!/usr/bin/env python3
"""Fill the SoundCloud-hosted series (not on Mixcloud) via the Apify SoundCloud
scraper (cryptosignals/soundcloud-scraper), and merge into mixgraph/rosters.json.

Keeps long-form uploads (>= MIN_MIN minutes) so short label-release previews are
dropped and we keep actual guest mixes / radio shows. Reuses crawl.parse_artist.
"""
import json, os, urllib.request, concurrent.futures as cf
from crawl import parse_artist, norm

ROOT = os.path.dirname(os.path.abspath(__file__))
ACTOR = 'cryptosignals~soundcloud-scraper'
MIN_MIN = 18                       # minutes; mixes are long, release previews aren't
MAX_ITEMS = 200


def token():
    for l in open(os.path.join(ROOT, '..', '.env')):
        if l.startswith('APIFY_API_TOKEN'):
            return l.split('=', 1)[1].strip().strip('"').strip("'")


def fetch_profile(url, tok, max_items=MAX_ITEMS):
    body = json.dumps({'action': 'user', 'url': url, 'maxItems': max_items}).encode()
    api = f'https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items?token={tok}'
    req = urllib.request.Request(api, data=body, headers={'Content-Type': 'application/json'})
    items = json.loads(urllib.request.urlopen(req, timeout=290).read())
    return items[0] if items else {}


def to_episodes(profile, series_name):
    eps = []
    for t in (profile.get('trackList') or []):
        dur_ms = t.get('duration') or 0
        if dur_ms and dur_ms < MIN_MIN * 60 * 1000:        # drop short non-mix uploads
            continue
        artist, raw = parse_artist(t.get('title', ''), series_name)
        eps.append({'title': raw, 'artist': artist,
                    'date': (t.get('createdAt') or '')[:10],
                    'url': t.get('url'), 'plays': t.get('plays')})
    return eps


def crawl_one(series_name, url, category, tok):
    try:
        prof = fetch_profile(url, tok)
        eps = to_episodes(prof, series_name)
        return {'series': series_name, 'category': category, 'method': 'soundcloud-apify',
                'handle': prof.get('username'), 'sc_url': url,
                'episodes': eps, 'count': len(eps),
                'parsed_artists': sum(1 for e in eps if e.get('artist'))}
    except Exception as e:
        return {'series': series_name, 'method': 'soundcloud-apify', 'sc_url': url,
                'error': f'{type(e).__name__}: {e}', 'episodes': [], 'count': 0}


if __name__ == '__main__':
    tok = token()
    sc_urls = json.load(open('/tmp/sc_urls.json'))
    catalog = {s['name']: s.get('category') for s in json.load(open(os.path.join(ROOT, 'series.json')))}

    results = []
    with cf.ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(crawl_one, name, url, catalog.get(name), tok): name
                for name, url in sc_urls.items()}
        for f in cf.as_completed(futs):
            r = f.result(); results.append(r)
            print(f'  {r["series"]:<24} -> {r.get("count",0):>3} mixes '
                  f'({r.get("parsed_artists",0)} named) {r.get("error") or r["handle"]}', flush=True)

    # merge into rosters.json: replace the placeholder (0-count) entries
    path = os.path.join(ROOT, 'rosters.json')
    db = json.load(open(path))
    by = {r['series']: r for r in results}
    merged, replaced = [], 0
    for rec in db:
        if rec['series'] in by and by[rec['series']].get('count', 0) > 0:
            merged.append(by.pop(rec['series'])); replaced += 1
        else:
            merged.append(rec)
    merged.extend(by.values())                            # any new series not already present
    json.dump(merged, open(path, 'w'), ensure_ascii=False, indent=1)
    print(f'\nmerged: replaced {replaced} series with SoundCloud data')
