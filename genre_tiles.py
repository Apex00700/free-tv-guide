#!/usr/bin/env python3
"""Daily GENRE TILE pictures for the Apex On Demand home/hub (Tony 2026-11-09, option B baked): seven darkened tile pictures plus a small list file, next to trending.json.

For each of the seven genres (Action, Comedy, Drama, Sci-Fi, Thriller, Horror, Documentary) it looks at that genre's currently most popular movies on TMDB, takes a TEXTLESS backdrop (no title card or logo text),
checks that it reads as the genre (primary genre, no family/animation for the dark genres, brightness and colour rules per genre, a deny list), bakes it into a 392 x 220 tile picture (slightly desaturated, genre tint, darkened, dark
bottom-left corner for the name, a little grain) and writes site/genre/<slug>.jpg plus site/genre.json. The Apex skin draws the genre NAME, the red underline and the focus lift itself, so nothing readable is baked in.
This product uses the TMDB API but is not endorsed or certified by TMDB.

THE KEY: read ONLY from the environment variable TMDB_API_KEY (a GitHub repository secret of that exact name, the same one trending.py uses). It is never printed, logged, written to a file, put in an error message or echoed:
requests go through send(), which hides the request address on any error. If the variable is missing or TMDB cannot be reached, nothing new is written for a genre and yesterday's picture is carried forward
(only if it is under 72 hours old); the job still succeeds. The Shield shows its built-in illustrated scene for any tile without a picture.

Usage:  python genre_tiles.py            (the real thing: needs TMDB_API_KEY)
        python genre_tiles.py --fake     (sample data and generated pictures, no network, no key: for tests)
"""
import datetime as dt
import io
import json
import os
import random
import re
import sys
import urllib.request

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w1280"
HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(HERE, "site", "genre")
OUT = os.path.join(HERE, "site", "genre.json")
DENY = os.path.join(HERE, "genre_deny.txt")          # TMDB ids (one per line) of titles whose backdrop misleads: skipped
TILE = (392, 220)
SAFE_PATH = re.compile(r"/[A-Za-z0-9_\-]{5,80}\.jpg")
GENRES = [("Action", "action", 28, (142, 27, 27)), ("Comedy", "comedy", 35, (138, 90, 43)), ("Drama", "drama", 18, (93, 64, 55)), ("Sci-Fi", "scifi", 878, (18, 58, 107)),
          ("Thriller", "thriller", 53, (38, 50, 56)), ("Horror", "horror", 27, (51, 64, 42)), ("Documentary", "documentary", 99, (154, 60, 23))]
ANIMATION, FAMILY = 16, 10751
DARK_GENRES = {"horror", "thriller", "drama", "documentary", "action"}


def send(path, key):
    """One TMDB call. The key goes in a header or the query string and is NEVER shown: errors carry only the kind of problem."""
    url = f"{API}{path}"
    headers = {"Accept": "application/json", "User-Agent": "apex-tv-genre/1"}
    if len(key) > 60:
        headers["Authorization"] = "Bearer " + key
    else:
        url += ("&" if "?" in url else "?") + "api_key=" + key
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise RuntimeError("TMDB request failed (%s)" % type(e).__name__) from None


def fetch_image(path):
    """A backdrop from TMDB's public picture server (no key needed)."""
    if not SAFE_PATH.fullmatch(path or ""):
        raise RuntimeError("unsafe picture path")
    try:
        with urllib.request.urlopen(urllib.request.Request(IMG + path, headers={"User-Agent": "apex-tv-genre/1"}), timeout=30) as r:
            return Image.open(io.BytesIO(r.read(3_000_000))).convert("RGB")
    except Exception as e:
        raise RuntimeError("picture download failed (%s)" % type(e).__name__) from None


def deny_ids():
    try:
        return {int(x.split("#")[0].strip()) for x in open(DENY, encoding="utf-8") if x.split("#")[0].strip().isdigit()}
    except OSError:
        return set()


def stats(im):
    """mean brightness 0-255 and mean colour strength 0-1 of a small copy."""
    s = im.resize((64, 36))
    lum = sum(s.convert("L").getdata()) / (64 * 36)
    hsv = list(s.convert("HSV").getdata())
    sat = sum(p[1] for p in hsv) / (len(hsv) * 255.0)
    return lum, sat


def reads_as(slug, im):
    """Brightness/colour rules: does this picture look like the genre (and leave room for the name in the dark corner)?"""
    lum, sat = stats(im)
    if slug == "horror":
        return lum < 95 and sat < 0.50
    if slug == "thriller":
        return lum < 115
    if slug == "comedy":
        return lum > 65 and sat > 0.18
    if slug in ("action", "drama"):
        return lum < 150
    if slug == "documentary":
        return lum < 170 and sat < 0.7
    return lum < 170                                  # sci-fi


def textless_backdrop(images):
    """From TMDB's /images answer: the best-rated backdrop with NO language (so no title or logo text), wide enough."""
    best = None
    for b in images.get("backdrops", []):
        if b.get("iso_639_1") is not None or int(b.get("width", 0)) < 1280:
            continue
        if not SAFE_PATH.fullmatch(b.get("file_path", "") or ""):
            continue
        if best is None or (b.get("vote_average", 0), b.get("width", 0)) > (best.get("vote_average", 0), best.get("width", 0)):
            best = b
    return best


def candidates(results, gid, slug, deny):
    """Popular titles of the genre, best first: genre is the FIRST genre listed, no animation/family for the dark genres, not on the deny list."""
    out = []
    for r in results:
        ids = r.get("genre_ids") or []
        if r.get("adult") or r.get("id") in deny or not ids or ids[0] != gid:
            continue
        if slug in DARK_GENRES and (ANIMATION in ids or FAMILY in ids):
            continue
        if not (r.get("title") or "").strip():
            continue
        out.append(r)
    return out


def bake(im, tint, seed=7):
    """The tile picture: crop to 392:220, slightly desaturated, genre tint, darkened, dark bottom-left corner for the name, soft vignette, fine grain."""
    w, h = im.size
    tw, th = TILE
    r = tw / th
    if w / h > r:
        nw = int(h * r)
        im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    else:
        nh = int(w / r)
        im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    im = im.resize((tw * 2, th * 2), Image.LANCZOS)
    im = ImageEnhance.Color(im).enhance(0.78)
    tint_layer = Image.new("RGB", im.size, tuple(int(60 + c * 0.7) for c in tint))
    im = Image.blend(ImageChops.multiply(im, tint_layer.point(lambda v: min(255, int(v * 1.6)))), im, 0.55)
    im = ImageEnhance.Brightness(im).enhance(0.62)
    W, H = im.size
    # dark corner for the name: strongest bottom-left, fading to the top and to the right (smooth ramps, no seams)
    W, H = im.size
    gx = Image.new("L", (W, 1)); gx.putdata([int(255 * (1 - x / (W - 1))) for x in range(W)]); gx = gx.resize((W, H))      # left dark source -> right clear
    gy = Image.new("L", (1, H)); gy.putdata([int(255 * (y / (H - 1))) for y in range(H)]); gy = gy.resize((W, H))          # top clear -> bottom dark
    shade = ImageChops.multiply(gx.point(lambda v: int(40 + v * 0.75)), gy.point(lambda v: int(30 + v * 0.8)))
    im = Image.composite(Image.new("RGB", im.size, (3, 4, 6)), im, shade.point(lambda v: int(v * 0.62)))
    random.seed(seed)
    noise = Image.effect_noise(im.size, 14).convert("RGB")
    im = Image.blend(im, ImageChops.add(im, noise, 1.0, -128), 0.10)
    return im.resize(TILE, Image.LANCZOS)


def pick_for(genre, key, deny):
    name, slug, gid, tint = genre
    since = (dt.date.today() - dt.timedelta(days=3 * 365)).isoformat()
    res = send(f"/discover/movie?with_genres={gid}&sort_by=popularity.desc&vote_count.gte=300&primary_release_date.gte={since}&include_adult=false&language=en-US", key).get("results", [])
    for r in candidates(res, gid, slug, deny)[:12]:
        try:
            imgs = send(f"/movie/{r['id']}/images?include_image_language=null", key)
            b = textless_backdrop(imgs)
            if not b:
                continue
            pic = fetch_image(b["file_path"])
            if not reads_as(slug, pic):
                continue
            return {"genre": name, "slug": slug, "title": r["title"].strip(), "year": (r.get("release_date") or "")[:4], "tmdb_id": r["id"], "pic": f"genre/{slug}.jpg"}, bake(pic, tint)
        except RuntimeError:
            continue
    return None, None


def carry_forward(slug, name):
    """Yesterday's published picture and entry for this genre, only if the published list is under 72 hours old."""
    repo = os.environ.get("GITHUB_REPOSITORY", "apex00700/free-tv-guide")
    base = "https://%s.github.io/%s/" % tuple(repo.split("/", 1))
    try:
        with urllib.request.urlopen(urllib.request.Request(base + "genre.json", headers={"User-Agent": "apex-tv-genre/1"}), timeout=30) as r:
            old = json.loads(r.read(40000).decode("utf-8"))
        age = dt.datetime.now(dt.timezone.utc).timestamp() * 1000 - int(old["generated"])
        entry = next(t for t in old["tiles"] if t["slug"] == slug)
        if old.get("v") != 1 or not (0 <= age < 72 * 3600 * 1000):
            return None
        with urllib.request.urlopen(urllib.request.Request(base + entry["pic"], headers={"User-Agent": "apex-tv-genre/1"}), timeout=30) as r:
            data = r.read(400000)
        Image.open(io.BytesIO(data)).verify()
        with open(os.path.join(OUTDIR, slug + ".jpg"), "wb") as f:
            f.write(data)
        entry["carried"] = True
        return entry
    except Exception:
        return None


def fake_tile(genre):
    name, slug, gid, tint = genre
    im = Image.new("RGB", (1280, 720), tuple(int(c * 0.6) for c in tint))
    d = ImageDraw.Draw(im)
    d.ellipse([700, 80, 1100, 480], fill=tuple(min(255, c + 90) for c in tint))
    return {"genre": name, "slug": slug, "title": "Fake " + name, "year": "2026", "tmdb_id": 1, "pic": f"genre/{slug}.jpg"}, bake(im, tint)


def main(argv):
    os.makedirs(OUTDIR, exist_ok=True)
    fake = "--fake" in argv
    key = "" if fake else os.environ.get("TMDB_API_KEY", "").strip()
    if not fake and not key:
        print("TMDB_API_KEY is not set: genre pictures skipped (nothing new written; yesterday's are carried forward if recent)")
    deny = deny_ids()
    tiles = []
    for g in GENRES:
        entry = pic = None
        if fake:
            entry, pic = fake_tile(g)
        elif key:
            try:
                entry, pic = pick_for(g, key, deny)
            except RuntimeError as e:
                print(g[1], "skipped:", e)
        if entry and pic is not None:
            pic.save(os.path.join(OUTDIR, g[1] + ".jpg"), "JPEG", quality=82, optimize=True, progressive=True)
            tiles.append(entry)
            print("%s: %s (%s)" % (g[1], entry["title"], entry["year"]))
        else:
            old = carry_forward(g[1], g[0])
            if old:
                tiles.append(old)
                print(g[1], ": kept yesterday's picture")
            else:
                print(g[1], ": no picture today (the Shield shows its illustrated scene)")
    if not tiles:
        print("genre.json not written (no pictures at all)")
        return 0
    now = dt.datetime.now(dt.timezone.utc)
    data = {"v": 1, "date": now.strftime("%Y-%m-%d"), "generated": int(now.timestamp() * 1000), "size": list(TILE), "tiles": tiles,
            "credit": "This product uses the TMDB API but is not endorsed or certified by TMDB."}
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    print("genre.json written: %d tiles (%s)" % (len(tiles), data["date"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
