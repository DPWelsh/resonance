#!/usr/bin/env python3
"""Resolve a pasted URL into a plain-text setlist the app already understands.

Supported:
  - a 1001tracklists.com tracklist page  -> parse its schema.org microdata
  - a SoundCloud mix link                -> read the DJ's posted tracklist from the
                                            track description (via oEmbed); if there
                                            is none, best-effort look the mix up on
                                            1001tracklists with a headless browser.

Everything returns lines of "Artist\\tTitle" (or "Artist - Title"), which
server.normalize_setlist() already accepts verbatim.
"""
import re, html, json, urllib.request, urllib.parse

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")


def _fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept': 'text/html,application/json,*/*',
        'Accept-Language': 'en-US,en;q=0.9',
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace')


# ---- 1001tracklists ----------------------------------------------------------
# Each track is a schema.org/MusicRecording block introduced by itemprop="tracks".
# The first byArtist meta in a block is the primary artist; the first name meta is
# the full "Artist - Title" credit. (Parser hardened against 5 real pages / 227
# tracks: featured artists, remixes, multi-artist, working-title, entities.)
_TRACK_SPLIT = re.compile(r'itemprop="tracks"')
_NAME_RE = re.compile(r'itemprop="name"\s+content="([^"]*)"')
_ARTIST_RE = re.compile(r'itemprop="byArtist"\s+content="([^"]*)"')


def parse_1001(page_html):
    """Return ordered [(artist, title), ...] from a 1001tracklists HTML page."""
    out = []
    for blk in _TRACK_SPLIT.split(page_html)[1:]:      # [0] is page-head meta -> drop
        nm = _NAME_RE.search(blk)
        if not nm:
            continue
        am = _ARTIST_RE.search(blk)
        artist = html.unescape(am.group(1)).strip() if am else ''
        name = html.unescape(nm.group(1)).strip()
        title = name
        if artist and name.startswith(artist + ' - '):
            title = name[len(artist) + 3:].strip()     # strip duplicated "Artist - " prefix
        else:
            parts = name.split(' - ', 1)               # ft.-credit case: split on first " - "
            if len(parts) == 2:
                if not artist:
                    artist = parts[0].strip()
                title = parts[1].strip()
        if artist or title:
            out.append((artist, title))
    return out


def resolve_1001(url):
    # Plain fetch works when 1001tracklists isn't showing its bot-gate. We do NOT
    # try to defeat that gate — when it's up, we ask the user to open the page in
    # their own browser (where it passes) and paste the tracks.
    rows = parse_1001(_fetch(url))
    if not rows:
        raise ValueError("1001tracklists is showing its bot-gate for that page right "
                         "now. Open it in your browser (it passes there) and paste the "
                         "tracks — or paste them as text.")
    return '\n'.join(f'{a}\t{t}' for a, t in rows)


# ---- SoundCloud --------------------------------------------------------------
def _soundcloud_oembed(url):
    api = 'https://soundcloud.com/oembed?format=json&url=' + urllib.parse.quote(url, safe='')
    try:
        return json.loads(_fetch(api, timeout=20))
    except Exception:
        return {}


# numbered ("1. ", "01) ") or timestamped ("00:00 ", "[1:23] ") leading index
_LEAD = re.compile(r'^\s*(?:\[?\d{1,2}:\d{2}(?::\d{2})?\]?|\d{1,3}[.)\]]|\d{1,3}\s*[-–])\s+')
_LABEL_TAIL = re.compile(r'\s*\[[^\[\]]*\]\s*$')   # trailing "[Label]" (SoundCloud convention)


def parse_desc_tracklist(desc):
    """Pull an ordered tracklist out of a free-form SoundCloud description."""
    lines = []
    for raw in (desc or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _LEAD.match(line)
        if not m:
            continue
        body = line[m.end():].strip()
        body = _LABEL_TAIL.sub('', body).strip()       # drop a trailing "[Label]"
        # only trust lines that look like a track ("Artist - Title")
        if ' - ' in body or ' – ' in body:
            lines.append(body)
    return lines


def resolve_soundcloud(url):
    meta = _soundcloud_oembed(url)
    tracks = parse_desc_tracklist(meta.get('description', ''))
    if len(tracks) >= 3:
        return '\n'.join(tracks)                        # "Artist - Title" lines

    # No tracklist in the description (the common case). We use 1001tracklists'
    # (un-gated) SEARCH to find the right set, then try a plain read — which works
    # only when their bot-gate is down. We do NOT defeat the gate; when it's up we
    # hand the user the link to open in their own browser.
    title = (meta.get('title') or '').strip()
    author = (meta.get('author_name') or '').strip()
    found = _search_1001_url(f'{author} {title}'.strip()) if title else None
    if found:
        try:
            rows = parse_1001(_fetch(found))
        except Exception:
            rows = []
        if rows:
            return '\n'.join(f'{a}\t{t}' for a, t in rows)
        raise ValueError(
            "Found this set on 1001tracklists, but it's behind their bot-gate. "
            "Open it in your browser and paste the tracks here:\n" + found)

    raise ValueError(
        "No tracklist in the SoundCloud description, and I couldn't find this mix "
        "on 1001tracklists. Paste the tracks as text, or paste a 1001tracklists link.")


# ---- headless 1001tracklists (search + some pages are JS/AJAX/bot-gated) ------
def _with_page(action, timeout_ms=30000):
    """Run action(page) in a fresh headless Chromium. Returns None on any failure
    (Playwright missing, launch error, bot challenge) so callers degrade cleanly."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(args=['--use-gl=swiftshader', '--no-sandbox'])
            try:
                pg = b.new_page(user_agent=UA)
                pg.set_default_timeout(timeout_ms)
                return action(pg)
            finally:
                b.close()
    except Exception:
        return None


def _search_1001_url(query):
    """Drive the JS-rendered 1001tracklists search and return the top tracklist
    URL. The search endpoint passes for headless; the tracklist pages don't."""
    def act(pg):
        pg.goto('https://www.1001tracklists.com/', wait_until='domcontentloaded', timeout=30000)
        box = pg.query_selector('#sBoxInput') or pg.query_selector("input[name='main_search']")
        if not box:
            return None
        box.click(); box.fill(query); pg.keyboard.press('Enter')
        try:
            pg.wait_for_selector("a[href*='/tracklist/']", timeout=12000)
        except Exception:
            pass
        pg.wait_for_timeout(800)
        hrefs = pg.eval_on_selector_all(
            "a[href*='/tracklist/']",
            "els=>els.map(e=>e.getAttribute('href')).filter(h=>/\\/tracklist\\/[a-z0-9]+\\//i.test(h))")
        if not hrefs:
            return None
        h = hrefs[0]
        return h if h.startswith('http') else 'https://www.1001tracklists.com' + h
    return _with_page(act)


# ---- dispatch ----------------------------------------------------------------
def resolve_url(url):
    url = (url or '').strip()
    host = urllib.parse.urlparse(url).netloc.lower()
    if '1001tracklists' in host:
        return resolve_1001(url)
    if 'soundcloud.com' in host or 'snd.sc' in host:
        return resolve_soundcloud(url)
    raise ValueError('Paste a SoundCloud or 1001tracklists link (or a text setlist).')


def looks_like_url(text):
    """A single line that's just a supported link -> treat as an import, not a setlist."""
    t = (text or '').strip()
    if '\n' in t or not re.match(r'^https?://', t, re.I):
        return False
    host = urllib.parse.urlparse(t).netloc.lower()
    return any(s in host for s in ('1001tracklists', 'soundcloud.com', 'snd.sc'))
