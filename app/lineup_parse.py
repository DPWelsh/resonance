#!/usr/bin/env python3
"""Festival blokkenschema image -> structured lineup rows, via Claude vision.

A festival timetable image (stages as columns, days/time-slots as rows) is read
into rows of {artist, stage, day, start, end, live}. The graph only needs the
artist names; stage/day/time ride along as metadata for labels and tooltips.

Used by the web app's POST /api/lineup. Reuses the same ANTHROPIC_API_KEY the
Story panel already relies on.
"""
import os, re, json, base64

APP_DIR = os.path.dirname(os.path.abspath(__file__))

VISION_SYSTEM = (
    "You read festival timetable images (a 'blokkenschema': stages as columns, "
    "each column listing acts under day headers with time slots). Extract EVERY "
    "performance into a flat list. Be exhaustive and precise with artist names — "
    "preserve accents and '&'. A trailing '*' on a name usually means a live set; "
    "record that as live=true and strip the '*' from the name. Ignore non-acts like "
    "'a pausa' (a break/pause), legends, and footnotes. Return ONLY valid JSON."
)

VISION_INSTRUCTION = (
    "Return a JSON object: {\"festival\": <title or null>, \"acts\": [ {\"artist\": str, "
    "\"stage\": str, \"day\": str, \"start\": \"HH:MM\"|null, \"end\": \"HH:MM\"|null, "
    "\"live\": bool} ]}. One entry per performance. Use the stage names from the "
    "column headers and the day headers within each column. Do not invent acts; "
    "only what is printed. No prose, no markdown — just the JSON object."
)

# things that are slots/labels, not artists
_NON_ACTS = {'a pausa', 'pausa', 'tba', 'tbd', 'break'}


def _load_key():
    path = os.path.join(os.path.dirname(APP_DIR), '.env')
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            if '=' in line and not line.strip().startswith('#'):
                k, v = line.split('=', 1)
                if k.strip() == 'ANTHROPIC_API_KEY':
                    return v.strip().strip('"').strip("'")
    return os.environ.get('ANTHROPIC_API_KEY')


def _media_type(data: bytes, filename: str = '') -> str:
    ext = (os.path.splitext(filename)[1] or '').lower()
    if ext in ('.jpg', '.jpeg') or data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if ext == '.webp' or data[8:12] == b'WEBP':
        return 'image/webp'
    if ext == '.gif':
        return 'image/gif'
    return 'image/png'


def _clean_name(name: str):
    """Strip a trailing live-marker '*' and surrounding whitespace. Returns
    (clean_name, live_flag)."""
    n = (name or '').strip()
    live = n.endswith('*')
    n = n.rstrip('*').strip()
    # collapse internal whitespace/newlines from multi-line cells
    n = re.sub(r'\s+', ' ', n)
    return n, live


def parse_lineup_image(data: bytes, filename: str = '', model: str = 'claude-opus-4-8'):
    """Read a blokkenschema image into structured acts. Returns a dict
    {festival, acts:[...]}. Raises on missing key / SDK / bad response."""
    key = _load_key()
    if not key:
        raise RuntimeError('Add ANTHROPIC_API_KEY to your .env to read lineup images.')
    import anthropic  # imported lazily, same as the Story endpoint

    client = anthropic.Anthropic(api_key=key)
    b64 = base64.standard_b64encode(data).decode('ascii')
    msg = client.messages.create(
        model=model,
        max_tokens=8000,
        system=VISION_SYSTEM,
        messages=[{
            'role': 'user',
            'content': [
                {'type': 'image', 'source': {
                    'type': 'base64', 'media_type': _media_type(data, filename), 'data': b64}},
                {'type': 'text', 'text': VISION_INSTRUCTION},
            ],
        }],
    )
    text = ''.join(b.text for b in msg.content if getattr(b, 'type', None) == 'text').strip()
    # tolerate ```json fences or stray prose around the object
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if not m:
        raise ValueError('Could not read a lineup from that image.')
    obj = json.loads(m.group(0))

    acts, seen = [], set()
    for a in obj.get('acts') or []:
        raw = (a.get('artist') or '').strip()
        name, live_from_name = _clean_name(raw)
        if not name or name.lower() in _NON_ACTS:
            continue
        live = bool(a.get('live')) or live_from_name
        stage = (a.get('stage') or '').strip()
        day = (a.get('day') or '').strip()
        # de-dupe identical (artist, stage, day, start)
        sig = (name.lower(), stage.lower(), day.lower(), a.get('start') or '')
        if sig in seen:
            continue
        seen.add(sig)
        acts.append({'artist': name, 'stage': stage, 'day': day,
                     'start': a.get('start'), 'end': a.get('end'), 'live': live})
    return {'festival': (obj.get('festival') or None), 'acts': acts}


def acts_to_setlist(acts):
    """Collapse acts to one line per UNIQUE artist (the build path is artist-seeded;
    duplicates across stages/days would just inflate appearances)."""
    out, seen = [], set()
    for a in acts:
        nk = a['artist'].lower()
        if nk in seen:
            continue
        seen.add(nk)
        out.append(a['artist'])
    return '\n'.join(out)


if __name__ == '__main__':
    import sys
    path = sys.argv[1]
    res = parse_lineup_image(open(path, 'rb').read(), path)
    print(f"festival: {res['festival']}   acts: {len(res['acts'])}")
    for a in res['acts'][:12]:
        print(f"  {a['stage']:<12} {a['day']:<10} {a['start'] or '--':>5}  "
              f"{a['artist']}{' *live' if a['live'] else ''}")
    print('  ...')
    print('\n--- unique-artist setlist ---')
    print(acts_to_setlist(res['acts']))
