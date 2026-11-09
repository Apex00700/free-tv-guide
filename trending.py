#!/usr/bin/env python3
"""Daily "trending" file for the Apex TV front door's On Demand tile (Tony 2026-11-08).

Writes site/trending.json: 4 trending movies and 4 trending TV shows of the day (alternating movie, show, movie, show ...),
each with title, kind, year and the address of TMDB's own picture. Source: TMDB's "trending / day" lists (the same lists Kodi's
TMDb Helper uses). This product uses the TMDB API but is not endorsed or certified by TMDB.

THE KEY: read ONLY from the environment variable TMDB_API_KEY (a GitHub repository secret of that exact name). It is never printed,
logged, written to a file, put in an error message or echoed: requests go through send(), which hides the request address on any
error. If the variable is missing, or TMDB cannot be reached, NOTHING is written and the job still succeeds (the door then uses
its last good download, Kodi's pictures, or its still art).

Usage:  python trending.py            (the real thing: needs TMDB_API_KEY)
        python trending.py --fake     (sample data, no network, no key: for tests)
"""
import datetime as dt
import json
import os
import re
import sys
import urllib.request

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w780"          # 780 px wide backdrops: about 60-100 KB each, no key needed to download
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site", "trending.json")
PER_KIND = 4
SAFE_PATH = re.compile(r"^/[A-Za-z0-9_\-]{5,80}\.jpg$")


def send(path, key):
    """One TMDB call. The key goes in a header or the query string and is NEVER shown: errors carry only the kind of problem."""
    url = f"{API}{path}"
    headers = {"Accept": "application/json", "User-Agent": "apex-tv-trending/1"}
    if len(key) > 60:                              # a v4 "read access token" (long) goes in a header
        headers["Authorization"] = "Bearer " + key
    else:                                          # a v3 key (32 characters) goes in the query string
        url += ("&" if "?" in url else "?") + "api_key=" + key
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:                         # never include the URL (it may contain the key)
        raise RuntimeError("TMDB request failed (%s)" % type(e).__name__) from None


def pick(results, kind):
    """First PER_KIND results that have a wide picture, in TMDB's own trending order."""
    out = []
    for r in results:
        title = (r.get("title") if kind == "movie" else r.get("name")) or ""
        date = (r.get("release_date") if kind == "movie" else r.get("first_air_date")) or ""
        back = r.get("backdrop_path") or ""
        if r.get("adult") or not title.strip() or not SAFE_PATH.match(back):
            continue
        out.append({"kind": kind, "title": title.strip(), "year": date[:4] if re.match(r"^\d{4}", date) else "",
                    "pic": IMG + back})
        if len(out) == PER_KIND:
            break
    return out


def build(movies, shows, now=None):
    """The file: alternating movie, show, movie, show, ... ranks 1-8 (fewer if TMDB gave fewer pictures)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    items = []
    for i in range(PER_KIND):
        for lst in (movies, shows):
            if i < len(lst):
                items.append(dict(lst[i], rank=len(items) + 1))
    return {"v": 1, "date": now.strftime("%Y-%m-%d"), "generated": int(now.timestamp() * 1000), "items": items}


def fake():
    mv = [{"title": f"Fake Movie {i}", "release_date": "2026-01-0%d" % i, "backdrop_path": f"/fakemovie{i}abcde.jpg"} for i in range(1, 6)]
    tv = [{"name": f"Fake Show {i}", "first_air_date": "2025-02-0%d" % i, "backdrop_path": f"/fakeshow{i}abcdef.jpg"} for i in range(1, 6)]
    return mv, tv


def main(argv):
    if "--fake" in argv:
        mv, tv = fake()
    else:
        key = os.environ.get("TMDB_API_KEY", "").strip()
        if not key:
            print("TMDB_API_KEY is not set: trending skipped (nothing written)")
            return 0
        try:
            mv = send("/trending/movie/day?include_adult=false&language=en-US", key).get("results", [])
            tv = send("/trending/tv/day?include_adult=false&language=en-US", key).get("results", [])
        except RuntimeError as e:
            print("trending skipped:", e)
            return 0
    data = build(pick(mv, "movie"), pick(tv, "show"))
    if len(data["items"]) < 2:
        print("trending skipped: fewer than 2 usable titles")
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    print("trending.json written: %d titles (%s)" % (len(data["items"]), data["date"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
