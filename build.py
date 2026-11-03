"""Rebuild one combined channel list and one 2-day TV guide from free, public sources.

Input : channels.csv  (channel number, group, display name, source match key, My Channels flag)
Output: site/shield-tv.m3u, site/shield-tv-guide.xml.gz, site/report.txt, site/health.json, site/artsizes.json
Runs on GitHub's servers every 2 hours (see .github/workflows/refresh.yml).
Only public URLs are used. No accounts, keys or passwords.

Extras:
 - My Channels: channels flagged in channels.csv are ALSO in a "My Channels" group (group-title "My Channels;<group>").
 - Guide gaps: where a channel has no listing, a "Live: <channel>" block is added so no guide row is blank.
   Channels without any guide source get these blocks for the whole window.
 - Auto-hide: every stream is checked each run (Plex is skipped: it rate-limits GitHub's servers).
   A channel is hidden after failing 3 runs in a row and comes back automatically after one good run.
   Hidden channels stay in channels.csv and are listed in report.txt with the reason.
"""
import csv, gzip, io, json, os, re, sys, urllib.request, urllib.error, urllib.parse, datetime as dt
import concurrent.futures as cf
from xml.etree.ElementTree import iterparse, tostring, SubElement
from xml.sax.saxutils import escape, quoteattr

# Per group: card color (logo background) and the genre word given to shows that have none.
# The genre word drives the TV guide colors in Arctic Fuse 3. No purple/pink and no blue (the accent is royal blue #0063E5).
GROUP_STYLE = {
    "All Channels":      ("#263238", "Entertainment"),   # only used for the home-screen group tile
    "My Channels":       ("#B8860B", "News"),
    "Seattle Area":      ("#00897B", "News"),
    "US Local News":     ("#00838F", "News"),
    "News":              ("#D84315", "News"),
    "Sports":            ("#2E7D32", "Sports"),
    "Movies":            ("#B71C1C", "Movie"),
    "Drama":             ("#8E3B1F", "Drama"),
    "Crime & Mystery":   ("#37474F", "Crime"),
    "Comedy":            ("#9E9D24", "Comedy"),
    "Classic TV":        ("#6D4C41", "Comedy"),
    "Reality":           ("#A1663A", "Reality"),
    "Game Shows":        ("#F57F17", "Game Show"),
    "Entertainment":     ("#455A64", "Entertainment"),
    "Kids & Family":     ("#FF8F00", "Kids"),
    "Science & History": ("#5D4037", "Science"),
    "Food & Travel":     ("#827717", "Food"),
    "Music":             ("#9C6B30", "Music"),
    "Relax":             ("#2E7D6B", "Lifestyle"),
    "Gaming & Anime":    ("#DD2C00", "Entertainment"),
    "Español":           ("#E64A19", "Entertainment"),
    "More Channels":     ("#424242", "Entertainment"),
}
# Pictures stored on the Shield itself (personal use; the images are never on GitHub — only these paths are).
# Made by tools/make_local_logos.py and tools/make_backdrops.py on the PC.
SHIELD_MEDIA = "special://profile/media/"
BACKDROPS = {"seattle-area": 4, "us-local-news": 3, "news": 3, "sports": 3, "movies": 3, "drama": 2, "crime-mystery": 3,
             "comedy": 2, "classic-tv": 1, "reality": 2, "game-shows": 2, "entertainment": 3, "kids-family": 2,
             "science-history": 3, "food-travel": 2, "music": 3, "relax": 3, "gaming-anime": 3, "more-channels": 2}
CARD_W, CARD_H = 1280, 720  # 720p cards: sharp when shown as the big background on a 4K TV

DAYS = 2
HIDE_AFTER = 3            # consecutive failed runs before a channel is hidden
NETWORK_TROUBLE = 0.40    # if more than this share fails in one run, the run is ignored for hiding
BCC = "https://raw.githubusercontent.com/BuddyChewChew"
SOURCES = {  # service: (playlist URL, guide URL or None, id prefix)
    "Pluto TV":         (f"{BCC}/app-m3u-generator/main/playlists/plutotv_us.m3u", "https://i.mjh.nz/PlutoTV/us.xml.gz", "pluto"),
    "Samsung TV Plus":  (f"{BCC}/app-m3u-generator/main/playlists/samsungtvplus_us.m3u", "https://i.mjh.nz/SamsungTVPlus/us.xml.gz", "samsung"),
    "Roku Channel":     (f"{BCC}/app-m3u-generator/main/playlists/roku_all.m3u", "https://i.mjh.nz/Roku/all.xml.gz", "roku"),
    "Plex":             (f"{BCC}/app-m3u-generator/main/playlists/plex_us.m3u", "https://i.mjh.nz/Plex/us.xml.gz", "plex"),
    "Tubi":             (f"{BCC}/app-m3u-generator/main/playlists/tubi_all.m3u", f"{BCC}/app-m3u-generator/main/playlists/tubi_epg.xml", "tubi"),
    "Xumo":             (f"{BCC}/xumo-playlist-generator/main/playlists/xumo_playlist.m3u", f"{BCC}/xumo-playlist-generator/main/playlists/xumo_epg.xml.gz", "xumo"),
    "Local Now":        ("https://www.apsattv.com/localnow.m3u", f"{BCC}/localnow-playlist-generator/main/epg.xml", "localnow"),
    "LG Channels":      ("https://raw.githubusercontent.com/mchardage-sys/EPG-LG-Channels/main/lg_channels_us.m3u",
                         "https://raw.githubusercontent.com/mchardage-sys/EPG-LG-Channels/main/lg_epg_us.xml", "lg"),
    "Vizio WatchFree+": ("https://www.apsattv.com/vizio.m3u", None, "vizio"),
    "DistroTV":         ("https://www.apsattv.com/distro.m3u", None, "distro"),
    "TCL tv+":          ("https://www.apsattv.com/tcl.m3u", None, "tcl"),
    "FreeLiveSports":   ("https://www.apsattv.com/freelivesports.m3u", None, "fls"),
}
# Services whose streams inputstream.adaptive gets refused on (HTTP 400) and Kodi's internal demuxer
# fails on via IPTV Simple, but which play in inputstream.ffmpegdirect (official Kodi add-on, already
# installed). Tested on the Shield 2026-11-01 with FOX 13 Seattle: picture in ~14 s.
BUILTIN_PLAYER = {"Local Now"}
TEST_UA ="Kodi/21.2 (Linux; Android 11) Android/11 Sys_CPU/aarch64 App_Bitness/64 Version/21.2"

MAX_URL = 1000   # IPTV Simple cuts stream URLs at ~1024 characters; cut URLs fail (HTTP 400). Tested 2026-11-01.

def shorten_url(url):
    """Make very long stream URLs fit: drop empty query values, then trailing extras (ad-tracking) until short enough."""
    url = url.strip()
    if len(url) <= MAX_URL or "?" not in url: return url
    base, query = url.split("?", 1)
    pairs = [p for p in query.split("&") if p and not p.endswith("=")]
    while pairs and len(base) + 1 + len("&".join(pairs)) > MAX_URL:
        pairs.pop()
    return base + ("?" + "&".join(pairs) if pairs else "")

def fetch(url, timeout=120, ua="Mozilla/5.0 (free-tv-guide builder)"):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def attr(line, key):
    m = re.search(key + r'="([^"]*)"', line)
    return m.group(1) if m else ""

def parse_m3u(text):
    out, cur, opts = [], None, {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            cur, opts = line, {}
        elif line.startswith("#EXTVLCOPT:") and cur:
            k, _, v = line[11:].partition("="); opts[k] = v
        elif line and not line.startswith("#") and cur:
            out.append(dict(name=cur.rsplit(",", 1)[-1].strip(), id=attr(cur, "tvg-id"), logo=attr(cur, "tvg-logo"),
                            url=line, ua=attr(cur, "http-user-agent") or opts.get("http-user-agent", ""),
                            ref=attr(cur, "http-referrer") or opts.get("http-referrer", "")))
            cur = None
    return out

def parse_time(s):
    s = s.strip(); t = dt.datetime.strptime(s[:14], "%Y%m%d%H%M%S")
    m = re.search(r"([+-])(\d\d)(\d\d)$", s)
    if m:
        off = dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        t = t - off if m.group(1) == "+" else t + off
    return t

def fmt(t): return t.strftime("%Y%m%d%H%M%S +0000")
def q(s): return (s or "").replace('"', "'")

def check_stream(c):
    """Return '' if the stream answers with a playlist/video, else a short reason."""
    try:
        h = {"User-Agent": c["ua"] or TEST_UA}
        if c["ref"]: h["Referer"] = c["ref"]
        url = urllib.parse.quote(c["url"].strip(), safe=":/?&=%#+;,@!$'()*~[]")  # spaces/accents, as players send them
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=12) as r:
            body = r.read(4096)
        if b"#EXTM3U" in body[:300] or body[:1] == b"\x47" or b"ftyp" in body[:16]:
            return ""
        return "answer was not a stream"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:
        return type(e).__name__

def fillers(start, end, title, desc, step_hours=1):
    """'Live: <name>' blocks covering [start, end), split on whole hours."""
    out, t = [], start
    while t < end:
        nxt = (t.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=step_hours))
        nxt = min(nxt, end)
        if (nxt - t).total_seconds() >= 60:
            out.append((t, nxt, title, desc))
        t = nxt
    return out

def hex_rgb(h): return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))

def gradient(color):
    """Static diagonal gradient: group color at top-left fading to a dark shade at bottom-right."""
    from PIL import Image
    a = hex_rgb(color); b = tuple(int(v * 0.30) for v in a)
    small = Image.new("RGB", (32, 18))
    for y in range(18):
        for x in range(32):
            t = (x / 31 * 0.6 + y / 17 * 0.4)
            small.putpixel((x, y), tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)))
    return small.resize((CARD_W, CARD_H), Image.BILINEAR)

def colorfulness(img):
    """0 = white/grey/black only, higher = more color. Looks only at visible pixels."""
    from PIL import Image
    small = img.convert("RGBA").resize((48, 48))
    hsv = small.convert("RGB").convert("HSV").getdata(); alpha = small.getchannel("A").getdata()
    vals = [s * v / 65025 for (h, s, v), a in zip(hsv, alpha) if a > 128 and v > 40]
    return sum(vals) / len(vals) if vals else 0.0

def load_logo(url):
    from PIL import Image
    try: return Image.open(io.BytesIO(fetch(url, timeout=20))).convert("RGBA")
    except Exception: return None

def name_card(name, color, out_path):
    """For channels whose feed has no logo (or a broken one): the channel name in white (stroked = bold look) on the group's
    gradient, so no tile is ever blank. Uses Pillow's built-in font (no font files needed)."""
    from PIL import ImageDraw, ImageFont
    card = gradient(color); d = ImageDraw.Draw(card)
    words = name.replace(".US", "").split()
    for size in (120, 104, 90, 78, 66, 56):
        font = ImageFont.load_default(size=size); lines, cur = [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if d.textlength(t, font=font) <= CARD_W * 0.80 or not cur: cur = t
            else: lines.append(cur); cur = w
        lines.append(cur)
        if len(lines) <= 3 and max(d.textlength(l, font=font) for l in lines) <= CARD_W * 0.84: break
    lh = int(size * 1.18); y = (CARD_H - lh * len(lines)) // 2
    for l in lines:
        x = (CARD_W - d.textlength(l, font=font)) // 2
        d.text((x + 3, y + 4), l, font=font, fill=(0, 0, 0))          # soft shadow
        d.text((x, y), l, font=font, fill=(255, 255, 255), stroke_width=2, stroke_fill=(255, 255, 255)); y += lh
    card.save(out_path, "JPEG", quality=85, optimize=True)
    return True

def make_card(candidates, color, out_path, name=""):
    """Channel logo centered on its group's gradient. Uses the first logo unless it is white/monochrome
    and another candidate is clearly more colorful. No usable logo: a name card instead.
    Returns (written, used_other_logo)."""
    from PIL import Image
    first = load_logo(candidates[0]) if candidates else None
    logo, changed = first, False
    if first is None or colorfulness(first) < 0.08:
        best, best_c = first, colorfulness(first) if first else -1
        for url in candidates[1:4]:
            alt = load_logo(url)
            if alt is not None and colorfulness(alt) > max(best_c + 0.10, 0.15):
                best, best_c = alt, colorfulness(alt)
        changed = best is not first and best is not None
        logo = best
    if logo is None:
        return (name_card(name, color, out_path), "name") if name else (False, False)
    card = gradient(color)
    box = logo.getchannel("A").getbbox()                  # trim empty transparent border
    if box: logo = logo.crop(box)
    sc = min(CARD_W * 0.70 / logo.width, CARD_H * 0.58 / logo.height)   # fit (also enlarges small logos)
    logo = logo.resize((max(1, int(logo.width * sc)), max(1, int(logo.height * sc))), Image.LANCZOS)
    card.paste(logo, ((CARD_W - logo.width) // 2, (CARD_H - logo.height) // 2), logo)
    card.save(out_path, "JPEG", quality=85, optimize=True)
    return True, changed

def norm_name(n):
    n = re.sub(r"\((?:[^)]*)\)|\b(hd|tv|channel|network|us|usa)\b", " ", n.lower())
    return re.sub(r"[^a-z0-9]+", "", n)

def main():
    all_rows = list(csv.DictReader(open("channels.csv", encoding="utf-8")))
    picks = [r for r in all_rows if (r.get("include") or "yes").strip().lower() != "no"]   # include=no: kept in the CSV, left out of playlist + guide
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    owner, _, name = repo.partition("/")
    base = f"https://{owner.lower()}.github.io/{name}/" if repo else ""
    os.makedirs("site", exist_ok=True)

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    lo, hi = now - dt.timedelta(hours=2), now + dt.timedelta(days=DAYS)
    report = []

    # ---------- match picks to today's source playlists
    by_service = {}
    for p in picks: by_service.setdefault(p["service"], []).append(p)
    entries, alt_logos = [], {}
    for svc, plist in by_service.items():
        pl_url, _, pfx = SOURCES[svc]
        try:
            live = parse_m3u(fetch(pl_url).decode("utf-8", "replace"))
        except Exception as e:
            report.append(f"{svc}: playlist download FAILED ({e}); channels skipped"); continue
        by_id = {c["id"]: c for c in live if c["id"]}
        by_name = {c["name"]: c for c in live}
        for c in live:
            if c["logo"]: alt_logos.setdefault(norm_name(c["name"]), []).append(c["logo"])
        found = 0
        for p in plist:
            c = by_id.get(p["key"]) if p["match"] == "id" else by_name.get(p["key"])
            if not c: continue
            c = dict(c, url=shorten_url(c["url"]))   # long URLs break in IPTV Simple
            found += 1
            num = int(p["number"])
            gid = f'{pfx}.{c["id"]}' if p["match"] == "id" and c["id"] else f"live.{num}"
            entries.append(dict(num=num, p=p, c=c, gid=gid, svc=svc, src_id=c["id"] if p["match"] == "id" else ""))
        report.append(f"{svc}: {found}/{len(plist)} channels found")
    entries.sort(key=lambda e: e["num"])

    # ---------- stream health (auto-hide)
    prev = {}
    if base:
        try: prev = json.loads(fetch(base + "health.json", timeout=30).decode())
        except Exception: prev = {}
    elif os.path.exists("site/health.json"):
        prev = json.load(open("site/health.json", encoding="utf-8"))
    key = lambda e: f'{e["svc"]}|{e["p"]["key"]}'
    to_test = [e for e in entries if e["svc"] != "Plex"]
    with cf.ThreadPoolExecutor(32) as ex:
        results = dict(zip([key(e) for e in to_test], ex.map(lambda e: check_stream(e["c"]), to_test)))
    failed_now = sum(1 for r in results.values() if r)
    trouble = to_test and failed_now / len(to_test) > NETWORK_TROUBLE
    health = {}
    for e in entries:
        k = key(e); old = prev.get(k, {})
        if k not in results:                       # not tested (Plex)
            if old: health[k] = old
            continue
        if not results[k]:                         # played: clears any failures (hidden channels come back)
            continue
        fails = old.get("fails", 0) + (0 if trouble else 1)
        health[k] = {"name": e["p"]["name"], "number": e["num"], "fails": fails,
                     "first_failed": old.get("first_failed", f"{now:%Y-%m-%d %H:%M} UTC"), "reason": results[k]}
    hidden = {k for k, v in health.items() if v.get("fails", 0) >= HIDE_AFTER}
    visible = [e for e in entries if key(e) not in hidden]
    json.dump(health, open("site/health.json", "w", encoding="utf-8"), indent=1, ensure_ascii=False)

    style = lambda e: GROUP_STYLE.get(e["p"]["group"], ("#424242", "Entertainment"))
    genre_for = {e["gid"]: style(e)[1] for e in visible}
    guide_icon = {}

    # ---------- guide: real listings first, then fill gaps
    progs = {e["gid"]: [] for e in visible}
    wanted = {}
    for e in visible:
        if e["src_id"]: wanted.setdefault(e["svc"], {})[e["src_id"]] = e["gid"]
    real = 0
    for svc, ids in wanted.items():
        gurl = SOURCES[svc][1]
        if not gurl: continue
        try:
            raw = fetch(gurl)
            if raw[:2] == b"\x1f\x8b": raw = gzip.decompress(raw)
        except Exception as ex_:
            report.append(f"{svc}: guide download FAILED ({ex_})"); continue
        n = 0
        for ev, el in iterparse(io.BytesIO(raw)):
            if el.tag == "programme":
                gid = ids.get(el.get("channel"))
                if gid:
                    try: a, b = parse_time(el.get("start")), parse_time(el.get("stop"))
                    except Exception: a = b = None
                    title = (el.findtext("title") or "").strip()
                    if a and b > lo and a < hi and title and not title.startswith("NOT_PUBLISHED"):
                        el.set("channel", gid); el.tail = None
                        for d in el.findall("date"):          # some feeds send 1970-01-01 as "unknown" (shows as 12/31/1969)
                            if (d.text or "0")[:4] < "1971": el.remove(d)
                        if el.find("category") is None:        # gives the guide its genre color
                            SubElement(el, "category").text = genre_for[gid]
                        progs[gid].append((a, b, tostring(el, encoding="unicode").strip())); n += 1
                el.clear()
            elif el.tag == "channel":
                gid = ids.get(el.get("id")); ic = el.find("icon")
                if gid and ic is not None and ic.get("src"): guide_icon[gid] = ic.get("src")
                el.clear()
        real += n
        report.append(f"{svc}: {n} guide entries")

    # ---------- group-colored logo cards (needs Pillow; falls back to the plain logos)
    logo_for = {e["gid"]: e["c"]["logo"] for e in visible}
    cards = 0
    try:
        import PIL  # noqa: F401
        os.makedirs("site/cards", exist_ok=True)
        def cands(e):
            out = [u for u in [e["c"]["logo"], guide_icon.get(e["gid"])] if u]
            for key_name in (e["p"]["name"], e["p"].get("original_name", ""), e["c"]["name"]):
                out += alt_logos.get(norm_name(key_name), []) if key_name else []
            return list(dict.fromkeys(out))
        todo = list(visible)   # every channel gets a card (name card when no logo can be loaded)
        with cf.ThreadPoolExecutor(32) as ex:
            res = list(ex.map(lambda e: make_card(cands(e), style(e)[0], f'site/cards/{e["num"]}.jpg', e["p"]["name"]), todo))
        for e, (good, _) in zip(todo, res):
            if good and base:
                logo_for[e["gid"]] = f'{base}cards/{e["num"]}.jpg'; cards += 1
        changed = [e["p"]["name"] for e, (good, ch) in zip(todo, res) if good and ch is True]
        named = [e["p"]["name"] for e, (good, ch) in zip(todo, res) if good and ch == "name"]
        report.append(f"Logo cards: {sum(g for g, _ in res)} of {len(todo)} made" + ("" if base else " (local test: not linked)"))
        local = {l.strip() for l in open("logos_local.txt", encoding="utf-8") if l.strip() and not l.startswith("#")} if os.path.exists("logos_local.txt") else set()
        for e in visible:
            if str(e["num"]) in local: logo_for[e["gid"]] = f'{SHIELD_MEDIA}apex-logos/{e["num"]}.jpg'
        report.append(f"High-res logo cards stored on the Shield: {sum(1 for e in visible if str(e['num']) in local)} channels")
        report.append(f"Name cards (feed has no logo or a broken one): {len(named)} channels"
                      + (f" (e.g. {', '.join(named[:8])})" if named else ""))
        report.append(f"Full-color logo used instead of a white/monochrome one: {len(changed)} channels"
                      + (f" (e.g. {', '.join(changed[:12])})" if changed else ""))
    except ImportError:
        report.append("Logo cards: skipped (Pillow not installed); plain logos used")

    # ---------- playlist
    lines = [f'#EXTM3U url-tvg="{base}shield-tv-guide.xml.gz"']
    for e in visible:
        p, c = e["p"], e["c"]
        grp = f'My Channels;{p["group"]}' if p.get("mychannels") == "yes" else p["group"]
        lines.append(f'#EXTINF:-1 tvg-id="{q(e["gid"])}" tvg-chno="{e["num"]}" tvg-name="{q(p["name"])}" tvg-logo="{q(logo_for[e["gid"]])}" group-title="{q(grp)}",{p["name"]}')
        if e["svc"] in BUILTIN_PLAYER:
            lines += ["#KODIPROP:inputstream=inputstream.ffmpegdirect", "#KODIPROP:inputstream.ffmpegdirect.is_realtime_stream=true"]
        if c["ua"]: lines.append(f'#EXTVLCOPT:http-user-agent={c["ua"]}')
        if c["ref"]: lines.append(f'#EXTVLCOPT:http-referrer={c["ref"]}')
        lines.append(c["url"].strip())
    open("site/shield-tv.m3u", "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")

    # Pluto sends 900x900 square "tile" pictures; most series also have a 1920x1080 widescreen "featuredImage".
    # Use it when it exists (checked once per series), so the 4K TV's big background isn't a stretched square.
    pluto_re = re.compile(r'https://images\.pluto\.tv/series/([0-9a-f]{24})/[^"]+')
    sids = {m.group(1) for items in progs.values() for _, _, x in items for m in pluto_re.finditer(x)}
    PLUTO_PLACEHOLDER = "4c39c83e568a723a1da327b6640835a2"   # md5 of Pluto's generic "pluto tv" picture (sent when a series has no widescreen art)
    def has_wide(sid):
        import hashlib
        try:
            req = urllib.request.Request(f"https://images.pluto.tv/series/{sid}/featuredImage.jpg?w=64&h=36", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r: return hashlib.md5(r.read()).hexdigest() != PLUTO_PLACEHOLDER
        except Exception: return False
    with cf.ThreadPoolExecutor(32) as ex:
        wide = {s for s, ok in zip(sids, ex.map(has_wide, list(sids))) if ok}
    def to_wide(m):
        return f"https://images.pluto.tv/series/{m.group(1)}/featuredImage.jpg?fm=jpg&amp;q=80&amp;w=1920&amp;h=1080" if m.group(1) in wide else m.group(0)
    report.append(f"Pluto widescreen 1080p pictures: {len(wide)} of {len(sids)} series")

    # No stretched low-res pictures: every show picture is measured once (only the first bytes are read) and the
    # sizes are remembered in site/artsizes.json between runs. Pictures under 700 px tall, portrait posters and
    # broken links are replaced by the group's themed 1080p backdrop (Pluto episode posters are 694x1000,
    # some Roku pictures 800x450, a Pluto resize server 330x330).
    icon_re = re.compile(r'<icon src="([^"]+)"\s*/>')
    def final_xml(x): return pluto_re.sub(to_wide, x) if "images.pluto.tv/series/" in x else x
    urls = {m.group(1) for items in progs.values() for _, _, x in items for m in icon_re.finditer(final_xml(x))
            if "featuredImage.jpg" not in m.group(1)}
    sizes = {}
    try: sizes = json.loads(fetch(base + "artsizes.json", timeout=30).decode()) if base else json.load(open("site/artsizes.json"))
    except Exception: sizes = {}
    def img_size(u):
        from PIL import ImageFile
        try:
            req = urllib.request.Request(u.replace("&amp;", "&"), headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                p = ImageFile.Parser()
                for _ in range(16):                      # up to 256 KB, usually the first 4 KB is enough
                    chunk = r.read(16384)
                    if not chunk: break
                    p.feed(chunk)
                    if p.image: return list(p.image.size)
        except Exception: pass
        return None
    todo_urls = sorted(u for u in urls if u not in sizes)
    with cf.ThreadPoolExecutor(32) as ex:
        for u, sz in zip(todo_urls, ex.map(img_size, todo_urls)): sizes[u] = sz
    sizes = {u: sizes[u] for u in urls}                  # keep only pictures still in use
    json.dump(sizes, open("site/artsizes.json", "w"), separators=(",", ":"))
    low = {u for u, sz in sizes.items() if not sz or sz[1] < 700 or sz[1] > sz[0]}
    report.append(f"Show pictures checked: {len(urls)} ({len(todo_urls)} new); low-res/portrait/broken replaced by themed backdrops: {len(low)}")

    gz = gzip.open("site/shield-tv-guide.xml.gz", "wt", encoding="utf-8", compresslevel=9)
    gz.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="free-tv-guide">\n')
    for e in visible:
        logo = logo_for[e["gid"]]
        gz.write(f'<channel id={quoteattr(e["gid"])}><display-name>{escape(e["p"]["name"])}</display-name>'
                 + (f"<icon src={quoteattr(logo)}/>" if logo else "") + "</channel>\n")
    fill_count, with_real_now, with_art, replaced = 0, 0, 0, 0
    for e in visible:
        gid, nm = e["gid"], e["p"]["name"]
        items = sorted(progs.get(gid, []), key=lambda x: x[0])
        if any(a <= now < b for a, b, _ in items): with_real_now += 1
        t, gaps = lo, []
        for a, b, _ in items:
            if a > t + dt.timedelta(minutes=10): gaps.append((t, a))
            t = max(t, b)
        if t < hi: gaps.append((t, hi))
        step = 1 if items else 2
        card = logo_for[gid] if "/cards/" in (logo_for[gid] or "") else ""
        slug = re.sub(r"[^a-z0-9]+", "-", e["p"]["group"].lower()).strip("-")
        def no_art_tag(start):
            """Picture for a show that has none: the group's themed backdrop stored on the Shield (rotates hourly),
            else the channel's colored card. Apex TV shows the channel's logo on the tile in that case."""
            if slug in BACKDROPS:
                k = (int(start.timestamp() // 3600) + e["num"]) % BACKDROPS[slug]
                return f'<icon src="{SHIELD_MEDIA}apex-backdrops/{slug}/{k}.jpg"/>'
            return f"<icon src={quoteattr(card)}/>" if card else ""
        for a, b, xml in items:
            has_art = "<icon" in xml; with_art += has_art
            if "images.pluto.tv/series/" in xml: xml = pluto_re.sub(to_wide, xml)
            m = icon_re.search(xml) if has_art else None
            if m and m.group(1) in low:                  # never stretch a small/portrait/broken picture
                xml = xml[:m.start()] + xml[m.end():]; has_art = False; with_art -= 1; replaced += 1
            if not has_art:
                tag = no_art_tag(a)
                if tag: xml = xml[:xml.rindex("</programme>")] + tag + "</programme>"
            gz.write(xml + "\n")
        for ga, gb in gaps:
            for a, b, title, desc in fillers(ga, gb, f"Live: {nm}", f"Live programming on {nm}.", step):
                gz.write(f'<programme start="{fmt(a)}" stop="{fmt(b)}" channel={quoteattr(gid)}><title>{escape(title)}</title><desc>{escape(desc)}</desc><category>{escape(genre_for[gid])}</category>{no_art_tag(a)}</programme>\n')
                fill_count += 1
    gz.write("</tv>\n"); gz.close()

    # ---------- report
    guide_src = [e for e in visible if e["src_id"]]
    report.append(f"Guide: {real} real listings + {fill_count} 'Live:' fill-in blocks; "
                  f"{with_real_now} of {len(guide_src)} channels with a guide source have a real listing on now")
    report.append(f"Program artwork: {with_art} of {real} real listings have a sharp image ({100 * with_art // max(real, 1)}%); "
                  f"{replaced} small/portrait/broken pictures swapped for themed backdrops")
    report.append(f"Stream check: {len(to_test)} tested, {failed_now} failed this run"
                  + (" (too many failures, treated as a network problem: nothing newly hidden)" if trouble else "")
                  + "; Plex not tested")
    report.append(f"TOTAL channels: {len(visible)} shown, {len(hidden)} hidden, {len(all_rows) - len(picks)} excluded in channels.csv, built {now:%Y-%m-%d %H:%M} UTC")
    if hidden:
        report.append("HIDDEN (failed %d runs in a row; they come back automatically once they play again):" % HIDE_AFTER)
        for k in sorted(hidden, key=lambda k: health[k]["number"]):
            v = health[k]
            report.append(f'  {v["number"]:>5}  {v["name"]}  [{k.split("|")[0]}]  reason: {v["reason"]}  failing since {v["first_failed"]}')
    watching = [v for k, v in health.items() if k not in hidden and v.get("fails")]
    if watching:
        report.append(f"Failing but not hidden yet ({len(watching)}): " + ", ".join(f'{v["name"]} ({v["fails"]}x)' for v in watching[:40]))
    open("site/report.txt", "w", encoding="utf-8").write("\n".join(report) + "\n")
    print("\n".join(report))
    if len(entries) < len(picks) * 0.5:
        sys.exit("Too many channels missing; not publishing (keeps the previous good version online).")

if __name__ == "__main__":
    main()
