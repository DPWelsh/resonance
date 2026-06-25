#!/usr/bin/env python3
"""mixgraph crawler — build an underground mix-series database.

For each series we resolve a Mixcloud account (or a SoundCloud podcast RSS feed),
enumerate its uploads, and record per mix: the guest ARTIST, the DATE, and the URL.
Output: mixgraph/rosters.json  (series -> [{title, artist, date, url}]).

Keyless: Mixcloud public REST API + iTunes lookup -> SoundCloud RSS. Be polite.
"""
import json, re, time, sys, os, urllib.request, urllib.parse, xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = {'User-Agent': 'mixgraph/0.1 (research)'}


def get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(url, timeout=20):
    return json.loads(get(url, timeout))


def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


# ---- artist extraction from a cloudcast / episode title --------------------
_EP_TOKEN = re.compile(
    r'^\s*(?:vol\.?|volume|episode|ep\.?|mix(?:tape)?|podcast|show|session[s]?|'
    r'radio|guest\s*mix|no\.?|n[°º]|part|pt\.?|#)?\s*[\#nNoO\.]*\s*\d{1,4}[a-z]?\s*',
    re.I)


def _episodey(p, series_name):
    # ignore parenthetical notes and 4-digit years when judging "is this an episode marker"
    q = re.sub(r'\([^)]*\)', '', p)
    q = re.sub(r'\b(19|20)\d{2}\b', '', q)
    return bool(re.search(r'\d', q)) or norm(p) == norm(series_name) \
        or (len(norm(p)) > 3 and norm(p) in norm(series_name))


def parse_artist(title, series_name):
    """Best-effort: strip the series name + episode code, return the guest.

    Handles the dominant title shapes: "MDC.327 Artist", "Crack Mix 124 - Artist",
    "Truancy Volume 275: Artist", "RA.577 Artist", "Artist - Series 93"."""
    raw = (title or '').strip()
    sn = series_name.strip()
    t = raw
    # 1) drop the series name wherever it sits (front/back), case-insensitive
    for variant in {sn, sn.replace(' ', ''),
                    re.sub(r'\s*(radio|fm|podcast|mix(?:es)?|series|show|sessions?)\b.*$', '', sn, flags=re.I).strip()}:
        if variant and len(variant) > 2:
            t = re.sub(re.escape(variant), ' ', t, flags=re.I)
    t = re.sub(r'\s{2,}', ' ', t).strip()

    chosen = None
    # 2) if a guest separator exists, keep the side that ISN'T an episode marker
    for sep in [' - ', ' – ', ' — ', ': ', ' w/ ', ' with ', ' invites ', ' pres. ', ' presents ']:
        if sep in t:
            parts = [p.strip() for p in t.split(sep) if p.strip()]
            clean = [p for p in parts if not _episodey(p, series_name)]
            chosen = clean[0] if clean else None
            break
    # 3) no usable separator -> strip a leading "<prefix><number> " episode code
    if chosen is None:
        m = re.match(r"^[\w .,&'’()/\-]*?\d{1,4}\s*[:\-–—.]*\s+(.+)$", t)
        chosen = m.group(1) if m else t

    chosen = re.sub(r'\s*\([^)]*\)\s*$', '', chosen)      # drop a trailing (label / live / note)
    chosen = re.sub(r'^\s*#?\d{1,4}[\s.:\-–—]+', '', chosen)  # leftover leading episode number
    chosen = re.sub(r'\b(19|20)\d{2}\b', '', chosen)
    chosen = re.sub(r'\s{2,}', ' ', chosen).strip(' -–—:#.|')
    # reject date-like / mostly-numeric junk that isn't a real artist name
    if re.match(r'^\d{1,4}[-/.]\d{1,2}', chosen):
        return None, raw
    alpha = sum(c.isalpha() for c in chosen)
    if not chosen or alpha < 2 or norm(chosen) == norm(series_name):
        return None, raw
    return chosen, raw


# ---- Mixcloud ---------------------------------------------------------------
def resolve_mixcloud(name, hint=None):
    """Return (username, matched_name, confidence) or (None, None, None)."""
    if hint:
        try:
            u = get_json(f'https://api.mixcloud.com/{hint}/')
            if u.get('username'):
                return u['username'], u.get('name'), 'hinted'
        except Exception:
            pass
    try:
        q = urllib.parse.quote(name)
        res = get_json(f'https://api.mixcloud.com/search/?q={q}&type=user&limit=5')
        cands = res.get('data', [])
    except Exception:
        return None, None, None
    nn = norm(name)
    for c in cands:                                   # exact normalized match first
        if norm(c.get('name')) == nn or norm(c.get('username')) == nn:
            return c['username'], c.get('name'), 'exact'
    for c in cands:                                   # contained match
        if nn in norm(c.get('name')) or norm(c.get('name')) in nn:
            return c['username'], c.get('name'), 'fuzzy'
    return None, None, None


def crawl_mixcloud(username, series_name, cap=120, pause=0.3):
    out, url = [], f'https://api.mixcloud.com/{username}/cloudcasts/?limit=50'
    while url and len(out) < cap:
        try:
            d = get_json(url)
        except Exception as e:
            break
        for x in d.get('data', []):
            artist, raw = parse_artist(x.get('name', ''), series_name)
            out.append({'title': raw, 'artist': artist,
                        'date': (x.get('created_time') or '')[:10],
                        'url': x.get('url'), 'plays': x.get('play_count')})
            if len(out) >= cap:
                break
        url = (d.get('paging') or {}).get('next')
        time.sleep(pause)
    return out


# ---- SoundCloud podcast via iTunes lookup -> RSS ---------------------------
def crawl_rss(apple_id, series_name, cap=400):
    try:
        look = get_json(f'https://itunes.apple.com/lookup?id={apple_id}')
        feed = look['results'][0]['feedUrl']
    except Exception:
        return [], None
    try:
        xml = get(feed, timeout=40)
        root = ET.fromstring(xml)
    except Exception:
        return [], feed
    out = []
    for item in root.iter('item'):
        title = (item.findtext('title') or '').strip()
        date = (item.findtext('pubDate') or '').strip()
        link = item.findtext('link') or ''
        artist, raw = parse_artist(title, series_name)
        out.append({'title': raw, 'artist': artist, 'date': date[:16], 'url': link})
        if len(out) >= cap:
            break
    return out, feed


def crawl_series(s):
    name = s['name']
    rec = {'series': name, 'category': s.get('category'), 'method': s['method'],
           'episodes': []}
    if s['method'] == 'rss':
        eps, feed = crawl_rss(s['apple_id'], name, cap=s.get('cap', 400))
        rec['feed'] = feed
        rec['episodes'] = eps
    else:
        uname, mname, conf = resolve_mixcloud(name, s.get('handle'))
        rec['handle'] = uname
        rec['resolved_name'] = mname
        rec['resolution'] = conf
        if uname:
            rec['episodes'] = crawl_mixcloud(uname, name, cap=s.get('cap', 120))
    rec['count'] = len(rec['episodes'])
    parsed = sum(1 for e in rec['episodes'] if e.get('artist'))
    rec['parsed_artists'] = parsed
    return rec


if __name__ == '__main__':
    series_file = os.path.join(ROOT, 'series.json')
    SERIES = json.load(open(series_file))
    only = sys.argv[1:] if len(sys.argv) > 1 else None
    if only:
        SERIES = [s for s in SERIES if s['name'] in only]
    results = []
    for i, s in enumerate(SERIES, 1):
        try:
            rec = crawl_series(s)
        except Exception as e:
            rec = {'series': s['name'], 'error': f'{type(e).__name__}: {e}', 'episodes': [], 'count': 0}
        results.append(rec)
        print(f'[{i}/{len(SERIES)}] {s["name"]:<28} -> {rec.get("count",0):>4} mixes '
              f'({rec.get("parsed_artists",0)} artists named) '
              f'{rec.get("resolution") or rec.get("method")}', flush=True)
        time.sleep(0.2)
    out_path = os.path.join(ROOT, 'rosters.json')
    json.dump(results, open(out_path, 'w'), ensure_ascii=False, indent=1)
    tot = sum(r.get('count', 0) for r in results)
    print(f'\nWROTE {out_path}: {len(results)} series, {tot} mixes total')
