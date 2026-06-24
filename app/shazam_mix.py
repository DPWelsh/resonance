#!/usr/bin/env python3
"""Identify the tracks in a SoundCloud (or other) DJ mix by audio fingerprinting.

Pipeline:  download the mix audio (yt-dlp)  ->  slide a window across it and cut a
short segment (ffmpeg)  ->  recognise that segment with Shazam (shazamio)  ->  dedupe
->  ordered "Artist\\tTitle" lines that server.normalize_setlist() accepts verbatim.

Shazam recognises RELEASED tracks; unreleased / white-label "ID" / heavily-blended
cuts are missed by design. The audio is downloaded to a temp dir and deleted after.
This does not circumvent any access control — it recognises audio, like Shazam does.
"""
import os, time, asyncio, tempfile, subprocess, shutil

WINDOW = 12          # seconds of audio fed to each Shazam query
HOP = 60             # nominal seconds between window starts (a window every ~minute)
HEAD_SKIP = 15       # skip the first N seconds (intros / talking)
MAX_WINDOWS = 80     # cap total Shazam queries — long mixes get wider spacing, not 100s of calls
CONCURRENCY = 3      # recognise this many windows at once (gentle enough to avoid hard throttling)
RECOG_TIMEOUT = 15   # seconds per Shazam call before giving up on that window (bounds total time)


def _duration(path):
    out = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=nw=1:nk=1', path], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def download_audio(url, workdir, on_pct=None):
    """Download a SMALL audio stream (Shazam needs no fidelity) to workdir.
    A 90-min mix in best quality is 150MB+; a <=128k stream is a fraction of that."""
    import yt_dlp

    def hook(d):
        if not (on_pct and d.get('status') == 'downloading'):
            return
        fi, fc = d.get('fragment_index'), d.get('fragment_count')
        if fc:                                       # HLS: count fragments, not bytes
            on_pct(int(100 * (fi or 0) / fc))
        else:
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            if total:
                on_pct(int(100 * d.get('downloaded_bytes', 0) / total))

    # Prefer a PROGRESSIVE http stream (one file, fast) over HLS (hundreds of slow
    # fragments). Shazam needs no fidelity, so a low-bitrate stream is ideal.
    opts = {'format': 'bestaudio[protocol^=http]/bestaudio/best',
            'outtmpl': os.path.join(workdir, 'mix.%(ext)s'),
            'quiet': True, 'no_warnings': True, 'noprogress': True, 'noplaylist': True,
            'retries': 3, 'concurrent_fragment_downloads': 10,   # if HLS is the only option
            'progress_hooks': [hook]}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    if not os.path.exists(path):
        cands = [f for f in os.listdir(workdir) if f.startswith('mix.')]
        if not cands:
            raise ValueError('Could not download audio from that link.')
        path = os.path.join(workdir, cands[0])
    title = info.get('title') if isinstance(info, dict) else None
    return path, title


def _cut(path, start, workdir, idx):
    seg = os.path.join(workdir, f'seg{idx}.wav')
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', str(start), '-t', str(WINDOW),
                    '-i', path, '-ac', '1', '-ar', '16000', seg], capture_output=True)
    return seg if os.path.exists(seg) and os.path.getsize(seg) > 1000 else None


async def _recognize(shazam, seg):
    fn = getattr(shazam, 'recognize', None) or getattr(shazam, 'recognize_song', None)
    try:
        # hard timeout: when Shazam throttles, the request can otherwise hang forever
        res = await asyncio.wait_for(fn(seg), timeout=RECOG_TIMEOUT)
    except TypeError:                              # older API wants raw bytes
        try:
            with open(seg, 'rb') as f:
                res = await asyncio.wait_for(fn(f.read()), timeout=RECOG_TIMEOUT)
        except Exception:
            return None
    except Exception:                             # timeout, throttle, decode error -> skip window
        return None
    track = (res or {}).get('track') if isinstance(res, dict) else None
    if not track:
        return None
    artist = (track.get('subtitle') or '').strip()
    title = (track.get('title') or '').strip()
    if not title:
        return None
    return {'artist': artist, 'title': title, 'key': track.get('key') or f'{artist}|{title}'}


def _window_starts(dur):
    """Evenly spaced window start times, at most MAX_WINDOWS (long mixes -> wider gaps)."""
    span = max(WINDOW, int(dur) - WINDOW - HEAD_SKIP)
    n = max(1, min(MAX_WINDOWS, span // HOP + 1))
    hop = span / n if n > 1 else 0
    return [int(HEAD_SKIP + i * hop) for i in range(n)]


async def _scan(path, dur, progress):
    from shazamio import Shazam
    shazam = Shazam()
    workdir = os.path.dirname(path)
    loop = asyncio.get_event_loop()
    starts = _window_starts(dur)
    total = len(starts)
    results = [None] * total
    sem = asyncio.Semaphore(CONCURRENCY)
    done = [0]

    async def one(idx, t):
        async with sem:                                  # cap concurrent Shazam calls
            seg = await loop.run_in_executor(None, _cut, path, t, workdir, idx)  # ffmpeg off the loop
            rec = await _recognize(shazam, seg) if seg else None
            if seg:
                try:
                    os.remove(seg)
                except OSError:
                    pass
        results[idx] = (t, rec)
        done[0] += 1
        if progress:
            progress('Listening to the mix', done[0], total)

    await asyncio.gather(*(one(i, t) for i, t in enumerate(starts)))
    hits, last = [], None                                # ordered de-dupe of adjacent repeats
    for t, rec in results:
        if rec and rec['key'] != last:
            rec['at'] = t
            hits.append(rec)
        if rec:
            last = rec['key']
    return hits


def resolve_mix(url, progress=None):
    """SoundCloud (or other) mix URL -> "Artist\\tTitle" lines via audio Shazam."""
    if not shutil.which('ffmpeg'):
        raise ValueError('ffmpeg is required for audio recognition.')
    workdir = tempfile.mkdtemp(prefix='shazam_')
    try:
        if progress:
            progress('Downloading the mix', 0, 100)
        on_pct = (lambda p: progress('Downloading the mix', p, 100)) if progress else None
        path, _title = download_audio(url, workdir, on_pct=on_pct)
        dur = _duration(path)
        if dur < WINDOW:
            raise ValueError('That audio is too short to identify.')
        hits = asyncio.run(_scan(path, dur, progress))
        seen, ordered = set(), []                   # global de-dupe, keep first-heard order
        for h in hits:
            k = (h['artist'].lower(), h['title'].lower())
            if k not in seen:
                seen.add(k)
                ordered.append(h)
        if not ordered:
            raise ValueError("Couldn't identify any tracks (Shazam matches released "
                             "tracks; unreleased / ID / blended cuts won't be found).")
        return '\n'.join(f"{h['artist']}\t{h['title']}" for h in ordered)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# Run standalone (in the isolated venv) — emit JSON lines the server parses for SSE.
if __name__ == '__main__':
    import sys, json
    url = sys.argv[1] if len(sys.argv) > 1 else ''

    def emit(stage, i, total):
        print(json.dumps({'t': 'progress', 'stage': stage, 'i': i, 'total': total}), flush=True)

    try:
        text = resolve_mix(url, progress=emit)
        print(json.dumps({'t': 'done', 'tracks': text}), flush=True)
    except Exception as e:
        print(json.dumps({'t': 'error', 'message': str(e)}), flush=True)
        sys.exit(1)
