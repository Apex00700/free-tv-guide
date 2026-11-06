"""Rebuild one combined channel list and one 2-day TV guide from free, public sources.

Input : channels.csv  (channel number, group, display name, source match key, My Channels flag)
Output: site/shield-tv.m3u, site/shield-tv-guide.xml.gz, site/report.txt, site/health.json, site/artsizes.json, site/tvmaze.json
Runs on GitHub's servers every 2 hours (see .github/workflows/refresh.yml).
Only public URLs are used. No accounts, keys or passwords.
Show pictures are links only (never copied to GitHub). TVmaze pictures need the credit "TVmaze" with a link (in Apex Live).

Extras:
 - My Channels: channels flagged in channels.csv are ALSO in a "My Channels" group (group-title "My Channels;<group>").
 - Guide gaps: where a channel has no listing, a "Live: <channel>" block is added so no guide row is blank.
   Channels without any guide source get these blocks for the whole window.
 - Auto-hide: every stream is checked each run (Plex is skipped: it rate-limits GitHub's servers).
   A channel is hidden after failing 3 runs in a row and comes back automatically after one good run.
   Hidden channels stay in channels.csv and are listed in report.txt with the reason.
"""
import csv, gzip, io, json, os, re, sys, time, urllib.request, urllib.error, urllib.parse, datetime as dt
import concurrent.futures as cf
from xml.etree.ElementTree import iterparse, tostring, SubElement
from xml.sax.saxutils import escape, quoteattr, unescape

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
# Made by tools/make_backdrops.py on the PC. (The Shield's own logo files are no longer linked: see logos_local.txt below.)
SHIELD_MEDIA = "special://profile/media/"
BACKDROPS = {"seattle-area": 4, "us-local-news": 3, "news": 3, "sports": 3, "movies": 3, "drama": 2, "crime-mystery": 3,
             "comedy": 2, "classic-tv": 1, "reality": 2, "game-shows": 2, "entertainment": 3, "kids-family": 2,
             "science-history": 3, "food-travel": 2, "music": 3, "relax": 3, "gaming-anime": 3, "more-channels": 2}
CARD_W, CARD_H = 1280, 720  # 720p cards: sharp when shown as the big background on a 4K TV
# Apex Live's own logo cards (site/live-cards/<number>.jpg): sized for its tiles (~392x220 px at 1080p), empty padding
# trimmed and every logo fitted into the same box (60% of the width x 50% of the height). Kodi's cards/ are unchanged.
LIVE_W, LIVE_H = 480, 270
LIVE_BOX = (0.60, 0.50)
# Channels whose feed sends the wrong logo: source key (channels.csv "key") -> public logo to use instead.
# Used for Apex Live's card (live-cards/) and the playlist/guide logo. Kodi's cards/ keep the feed's logo (unchanged).
# If the logo can't be downloaded, the channel simply keeps its usual logo.
LOGO_FIX = {
    # FOX 13 Seattle (KCPQ): Local Now sends the "FOX LOCAL Seattle" logo. Public-domain logo (Wikimedia Commons).
    "LN_991C9E73-687C-4B95-AD6A-1ED471747562":
        "https://upload.wikimedia.org/wikipedia/commons/thumb/3/3c/KCPQ_Fox_13_2021.svg/1280px-KCPQ_Fox_13_2021.svg.png",
}
# Sharper logos for Apex Live's card only (live-cards/). Kodi's cards/ and the playlist/guide logo stay exactly as before.
# 1) LOGO_SHARP: channels whose feed logo is tiny; source key -> a bigger original of the SAME logo.
#    ("url", "light") = a dark drawing shown light, like the channel's own logo on its dark tile.
#    ("url", ..., "always") = use it even when today's logo isn't enlarged or has a different outline (chosen by hand).
#    Licences and credits: CREDITS.md. All are public-domain text logos (trademarks of their owners).
# 2) BIGGER_LOGO: feeds that serve the same logo bigger (size in the address), used when the card would enlarge the logo.
# Either one is only used if it loads and has the same shape as the feed's logo (so a channel that changes its logo
# never keeps an old one); otherwise the card is made exactly as before.
LOGO_SHARP = {
    "400000287": "https://upload.wikimedia.org/wikipedia/commons/3/32/Jubilee_logo.png",                     # Tubi: Jubilee
    "400000304": ("https://upload.wikimedia.org/wikipedia/commons/thumb/b/b3/UEFA_Tv_logo.svg/1280px-UEFA_Tv_logo.svg.png", "light"),  # Tubi: UEFA.tv
    "World Chase Tag": ("https://upload.wikimedia.org/wikipedia/commons/1/1a/World_Chase_Tag_logo.png", "light"),  # FreeLiveSports
    # Local Now: KGW 8 Portland. 2026-11-05: use the sharpest version (vector drawing; it has no "PORTLAND, OR" line,
    # so "always" skips the size and same-shape checks for this one channel).
    "LN_CA8A62A2-B954-44F0-B9CF-96AB6417F5D7": ("https://upload.wikimedia.org/wikipedia/commons/thumb/5/5a/KGW_Logo_2014.svg/1280px-KGW_Logo_2014.svg.png", "light", "always"),
}
BIGGER_LOGO = [  # (address pattern, same logo at 1024 px)
    (re.compile(r"^(https://image\.xumo\.com/v1/channels/channel/\d+/)\d+x\d+(\.png.*)$"), r"\g<1>1024x1024\2"),
    (re.compile(r"^(https://images\.vizio\.com/v4/btv/\d+/\d+/\d+/)\d+/\d+/"), r"\g<1>1024/576/"),
]
LIVE_SHARPER = {}         # live-cards/ path -> "feed" or "sharp" (for report.txt)

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

# Guide placeholders some feeds send while the channel is actually streaming (Xumo listed "SIGN OFF"
# 7 PM-2:30 AM for FOX LOCAL Seattle on 2026-11-03 while it showed live election coverage).
# Also "no listing" texts sent as if they were shows (guide check 2026-11-06: Tubi listed "Program information
# currently unavailable." for 14 hours on Paws & Claws; Plex/Roku/Xumo/Local Now send "To Be Announced"/"TBD").
PLACEHOLDER_RE = re.compile(r"^\s*(sign[\s-]*off|sign[\s-]*on|off[\s-]*air|station\s+off[\s-]*air|no\s+programming"
                            r"|to\s+be\s+announced|tba|tbd|dynamic\s+placeholder\b.*"
                            r"|program(me)?\s+info(rmation)?\s+(is\s+)?(currently\s+)?(un\s*available|not\s+available))\s*[.!]*\s*$", re.I)

# Channels whose feed guide was compared with the picture and belongs to something else (guide check 2026-11-06).
# Their listings are left out, so the guide shows honest "Live: <channel>" blocks instead. Key = channels.csv "key".
# Remove a line to try the feed's guide again.
GUIDE_DROP = {
    "US1700030R0": "Daily Wire TV (Samsung): listed The Isabel Brown Show while the stream showed The Ben Shapiro Show",
    "400000008": "NHL (Tubi): listed a Maple Leafs debut, then the 2023 Heritage Classic; the stream showed other games",
}

MAX_URL = 1000   # IPTV Simple cuts stream URLs at ~1024 characters; cut URLs fail (HTTP 400). Tested 2026-11-01.

# Show pictures: a listing gets the first SHARP one (at least 720 px tall and wider than tall) of
# 1. the feed's own picture, 2. Pluto movies' widescreen poster16_9, 3. Xumo's 1920x1080 picture by id,
# 4. TVmaze's wide picture (same name + year/channel check), 5. the themed backdrop or channel card (no_art_tag).
SHARP_H = 720
TVMAZE = "https://api.tvmaze.com"   # free, no key. Pictures CC BY-SA: credit "TVmaze" with a link (tvmaze.com/api)
TVMAZE_UA = "free-tv-guide/1.0 (personal TV guide, refreshed every 2 hours)"
TVMAZE_GAP = 0.55      # seconds between calls: at most 19 in any 10 s (TVmaze allows 20)
TVMAZE_BUDGET = 240    # seconds of TVmaze calls per run; the rest is asked next run (answers kept in tvmaze.json)

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

def gradient(color, size=None):
    """Static diagonal gradient: group color at top-left fading to a dark shade at bottom-right."""
    from PIL import Image
    a = hex_rgb(color); b = tuple(int(v * 0.30) for v in a)
    small = Image.new("RGB", (32, 18))
    for y in range(18):
        for x in range(32):
            t = (x / 31 * 0.6 + y / 17 * 0.4)
            small.putpixel((x, y), tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)))
    return small.resize(size or (CARD_W, CARD_H), Image.BILINEAR)

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

def name_card(name, color, out_path, size=None):
    """For channels whose feed has no logo (or a broken one): the channel name in white (stroked = bold look) on the group's
    gradient, so no tile is ever blank. Uses Pillow's built-in font (no font files needed)."""
    from PIL import ImageDraw, ImageFont
    W, H = size or (CARD_W, CARD_H); k = W / CARD_W     # k = 1 for Kodi's cards (unchanged)
    card = gradient(color, (W, H)); d = ImageDraw.Draw(card)
    words = name.replace(".US", "").split()
    for fsize in (120, 104, 90, 78, 66, 56):
        font = ImageFont.load_default(size=max(10, int(fsize * k))); lines, cur = [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if d.textlength(t, font=font) <= W * 0.80 or not cur: cur = t
            else: lines.append(cur); cur = w
        lines.append(cur)
        if len(lines) <= 3 and max(d.textlength(l, font=font) for l in lines) <= W * 0.84: break
    lh = int(fsize * k * 1.18); y = (H - lh * len(lines)) // 2
    sh, st = max(1, round(3 * k)), max(1, round(2 * k))
    for l in lines:
        x = (W - d.textlength(l, font=font)) // 2
        d.text((x + sh, y + round(4 * k)), l, font=font, fill=(0, 0, 0))          # soft shadow
        d.text((x, y), l, font=font, fill=(255, 255, 255), stroke_width=st, stroke_fill=(255, 255, 255)); y += lh
    card.save(out_path, "JPEG", quality=85, optimize=True)
    return True

def trim_logo(logo):
    """Apex Live cards: cut empty edges. Transparent logos: the transparent border. Fully opaque logos (e.g. a logo on
    its own white plate): the flat border in the corner color."""
    from PIL import Image, ImageChops
    a = logo.getchannel("A")
    if a.getextrema()[0] < 250:
        box = a.point(lambda v: 255 if v > 16 else 0).getbbox()
    else:
        rgb = logo.convert("RGB")
        diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, rgb.getpixel((0, 0))))
        box = diff.convert("L").point(lambda v: 255 if v > 24 else 0).getbbox()
    return logo.crop(box) if box else logo

def shape_mask(im):
    """Where the logo is (transparent logos: their alpha; opaque ones: what differs from the corner color), 64x32."""
    from PIL import Image, ImageChops
    a = im.getchannel("A")
    if a.getextrema()[0] >= 250:
        rgb = im.convert("RGB")
        a = ImageChops.difference(rgb, Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))).convert("L")
    return a.resize((64, 32), Image.BILINEAR).point(lambda v: 255 if v > 64 else 0)

def same_shape(a, b):
    """True if two trimmed logos have the same outline (same proportions, >= 60% overlap)."""
    from PIL import ImageChops
    ra, rb = a.width / a.height, b.width / b.height
    if abs(ra - rb) / ra > 0.12: return False
    ma, mb = shape_mask(a), shape_mask(b)
    inter, union = ImageChops.multiply(ma, mb).histogram()[255], ImageChops.lighter(ma, mb).histogram()[255]
    return union == 0 or inter / union >= 0.6

def lighten(im):
    """Dark drawing -> light: grey/black parts inverted, colored parts kept (like the channel's own logo on dark tiles)."""
    from PIL import Image, ImageChops, ImageOps
    r, g, b, a = im.split()
    sat = ImageChops.subtract(ImageChops.lighter(ImageChops.lighter(r, g), b), ImageChops.darker(ImageChops.darker(r, g), b))
    rgb = im.convert("RGB")
    out = Image.composite(ImageOps.invert(rgb), rgb, sat.point(lambda v: 255 if v < 48 else 0))
    out.putalpha(a)
    return out

def sharper(logo, url, sharp, live_path):
    """Apex Live's card only: a bigger original of the same logo (LOGO_SHARP, else the feed's own bigger size), used
    when the card would enlarge today's logo. None = keep today's logo."""
    t = trim_logo(logo)
    flags = sharp[1:] if isinstance(sharp, tuple) else ()
    always = "always" in flags
    if not always and min(LIVE_W * LIVE_BOX[0] / t.width, LIVE_H * LIVE_BOX[1] / t.height) <= 1: return None   # already sharp
    src, light, kind = None, False, "sharp"
    if sharp:
        src, light = (sharp[0] if isinstance(sharp, tuple) else sharp), "light" in flags
    else:
        kind = "feed"
        for pat, rep in BIGGER_LOGO:
            if url and pat.match(url): src = pat.sub(rep, url); break
    big = load_logo(src) if src else None
    if big is None: return None
    if light: big = lighten(big)
    bt = trim_logo(big)
    if not always and (bt.width < t.width * 1.5 or not same_shape(t, bt)): return None
    LIVE_SHARPER[live_path] = kind
    return big

def live_card(logo, color, out_path, name):
    """Apex Live's 480x270 card: the trimmed logo fitted into LIVE_BOX on the group gradient (or a name card)."""
    from PIL import Image
    if logo is None:
        return name_card(name, color, out_path, (LIVE_W, LIVE_H)) if name else False
    logo = trim_logo(logo)
    sc = min(LIVE_W * LIVE_BOX[0] / logo.width, LIVE_H * LIVE_BOX[1] / logo.height)
    logo = logo.resize((max(1, round(logo.width * sc)), max(1, round(logo.height * sc))), Image.LANCZOS)
    card = gradient(color, (LIVE_W, LIVE_H))
    card.paste(logo, ((LIVE_W - logo.width) // 2, (LIVE_H - logo.height) // 2), logo)
    card.save(out_path, "JPEG", quality=88, optimize=True)
    return True

def make_card(candidates, color, out_path, name="", live_path=None, live_logo=None, sharp=None):
    """Channel logo centered on its group's gradient. Uses the first logo unless it is white/monochrome
    and another candidate is clearly more colorful. No usable logo: a name card instead.
    live_path: also write Apex Live's card from the same logo (no second download).
    live_logo: a LOGO_FIX logo for Apex Live's card only (Kodi's card is made exactly as before).
    sharp: a LOGO_SHARP entry (Apex Live's card only; see sharper()).
    Returns (written, used_other_logo)."""
    from PIL import Image
    first = load_logo(candidates[0]) if candidates else None
    logo, changed, logo_url = first, False, candidates[0] if candidates else None
    if first is None or colorfulness(first) < 0.08:
        best, best_c = first, colorfulness(first) if first else -1
        for url in candidates[1:4]:
            alt = load_logo(url)
            if alt is not None and colorfulness(alt) > max(best_c + 0.10, 0.15):
                best, best_c, logo_url = alt, colorfulness(alt), url
        changed = best is not first and best is not None
        logo = best
    if live_path:
        fixed = load_logo(live_logo) if live_logo else None
        if fixed is None and logo is not None:
            try: fixed = sharper(logo, logo_url, sharp, live_path)
            except Exception: fixed = None
        try: live_card(fixed or logo, color, live_path, name)
        except Exception: pass                                   # never let Apex Live's card break Kodi's
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

def show_key(s):
    """Show name for matching: lower case, no leading 'The', '&' = 'and', letters and digits only."""
    s = re.sub(r"^the\s+", "", s.lower().replace("&", " and ").strip())
    return re.sub(r"[\W_]+", "", s)

def tvmaze_get(path, log):
    """One TVmaze call, paced by TVMAZE_GAP and retried after HTTP 429 ("too many requests").
    Returns the answer, None for "not found"; other trouble raises. log = start time of every call."""
    for tries in range(3):
        if log: time.sleep(max(0, TVMAZE_GAP - (time.time() - log[-1])))
        log.append(time.time())
        try: return json.loads(fetch(TVMAZE + path, timeout=10, ua=TVMAZE_UA))
        except urllib.error.HTTPError as e:
            if e.code == 404: return None
            if e.code != 429 or tries == 2: raise
            time.sleep(10)

COMMON = set("""that this with from have will been were they them their there what when where which while about after
before again also back best both could does each even every first great into just know last life like little long made
make many more most much must never next night only other over part people really right same should since some still
such take than then these thing think those three through time today under until very want well would year years your
series season episode episodes story stories family home live show shows world""".split())
def hint_words(text, n=4):
    """Words that can tell one show apart from another (common English words left out)."""
    return set(re.findall(rf"[a-z]{{{n},}}", text.lower())) - COMMON

def show_info(r):
    """TVmaze show (with cast and images): [wide picture URL or "", words that tell it apart: cast and character
    names, words from its summary]."""
    e = (r or {}).get("_embedded") or {}
    names = " ".join(f'{(c.get("person") or {}).get("name", "")} {(c.get("character") or {}).get("name", "")}' for c in e.get("cast") or [])
    summary = re.sub(r"<[^>]+>", " ", (r or {}).get("summary") or "")
    return [wide_picture(e.get("images")), sorted(hint_words(names) | hint_words(summary, 5))[:80]]

def wide_picture(images):
    """TVmaze's best background: at least SHARP_H tall and wider than tall; main one first, then closest to 16:9."""
    best = []
    for i in images or []:
        r = (i.get("resolutions") or {}).get("original") or {}
        w, h = r.get("width") or 0, r.get("height") or 0
        if i.get("type") == "background" and r.get("url") and h >= SHARP_H and w > h:
            best.append((bool(i.get("main")), -abs(w / h - 16 / 9), w * h, r["url"]))
    return max(best)[3] if best else ""

def tvmaze_pictures(wanted, cache, today):
    """Step 4: TVmaze pictures for shows still without a sharp one. wanted: {(show key, year or 0, channel names):
    [title as listed, listings, words in their descriptions]}. Returns ({same key: picture URL}, report line).
    Only sure matches: exactly one TVmaze show with the same name that also passes a check: its years cover the
    listing's year; else its network's name is in the channel's name; else the channel's name contains the show's name
    AND 2+ of the show's cast/character names or summary words are in the listings' descriptions (a channel can share
    a show's name and be something else, e.g. the music channel "Hit List").
    cache = site/tvmaze.json (links, TVmaze ids and those few words; no pictures): "q" = same-name shows per name,
    "s" = wide picture + words per show, each with the date to ask again (misses too, so they aren't asked every run).
    It never stops the build: after 3 failed calls in a row or TVMAZE_BUDGET seconds it stops asking and uses what it
    already knows."""
    log, t0, out, state = [], time.time(), {}, {"fails": 0, "stop": ""}
    later = lambda days: (dt.date.fromisoformat(today) + dt.timedelta(days=days)).isoformat()
    year = lambda d: int(d[:4]) if d else 0
    words = lambda s: set(re.findall(r"[\w&+']+", s.lower()))
    def ask(path):
        if not state["stop"] and time.time() - t0 > TVMAZE_BUDGET: state["stop"] = "time limit reached"
        if state["stop"]: return False
        try: r = tvmaze_get(path, log); state["fails"] = 0; return r
        except Exception as e:
            state["fails"] += 1
            if state["fails"] >= 3: state["stop"] = f"TVmaze not answering ({type(e).__name__})"
            return False
    def known(part, k, path, make):
        """Remembered answer, or ask TVmaze when it's due. Can't ask: the old answer (if any) is used."""
        old = cache[part].get(k)
        if old and old[0] > today: return old[1]
        r = ask(path)
        if r is False: return old[1] if old else None
        v = make(r)
        cache[part][k] = [later(30 if (v[0] if part == "s" else v) else 14), v]
        return v
    per_name = {}
    for (k, _, _), (_, n, _) in wanted.items(): per_name[k] = per_name.get(k, 0) + n
    for (k, yr, chans), (title, _, dw) in sorted(wanted.items(), key=lambda w: -per_name[w[0][0]]):   # most-listed first
        shows = known("q", k, "/search/shows?q=" + urllib.parse.quote(title), lambda r: [
            [s["id"], year(s.get("premiered")), year(s.get("ended")), [n["name"] for n in (s.get("network"), s.get("webChannel")) if n]]
            for s in (x.get("show") or {} for x in r or []) if show_key(s.get("name") or "") == k])
        if not shows: continue
        cw, ck = set().union(*map(words, chans)), [show_key(c) for c in chans]
        def check(s):
            """Why this same-name show is the one listed: "year", "network", "channel" or "" (not sure)."""
            if yr: return "year" if s[1] and s[1] - 1 <= yr <= (s[2] or int(today[:4])) + 1 else ""
            if any(words(n) and words(n) <= cw for n in s[3]): return "network"
            return "channel" if len(k) >= 4 and any(k in c for c in ck) else ""
        ok = [(s, why) for s in shows for why in [check(s)] if why]
        if len(ok) != 1: continue                       # none passed the check, or two did: unsure, skip
        (s, why), = ok
        info = known("s", str(s[0]), f"/shows/{s[0]}?embed[]=cast&embed[]=images", show_info)
        if info and info[0] and (why != "channel" or len(set(info[1]) & dw) >= 2): out[(k, yr, chans)] = info[0]
    peak = max((sum(1 for u in log if t <= u < t + 10) for t in log), default=0)
    return out, (f"TVmaze: {len(log)} calls this run (most in any 10 s: {peak}); {len(out)} of {len(wanted)} show/channel pairs matched"
                 + (f"; stopped early: {state['stop']}" if state["stop"] else ""))

def main():
    all_rows = list(csv.DictReader(open("channels.csv", encoding="utf-8")))
    picks = [r for r in all_rows if (r.get("include") or "yes").strip().lower() != "no"]   # include=no: kept in the CSV, left out of playlist + guide
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    owner, _, name = repo.partition("/")
    base = f"https://{owner.lower()}.github.io/{name}/" if repo else ""
    os.makedirs("site", exist_ok=True)
    if os.path.exists("CREDITS.md"):                 # logo sources and licences, also published next to the cards
        open("site/CREDITS.md", "w", encoding="utf-8").write(open("CREDITS.md", encoding="utf-8").read())

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
        if e["src_id"] and e["p"]["key"] not in GUIDE_DROP: wanted.setdefault(e["svc"], {})[e["src_id"]] = e["gid"]
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
                    # PLACEHOLDER_RE: "SIGN OFF" etc. while the stream is live -> dropped, the gap gets a "Live:" block
                    if a and b > lo and a < hi and title and not title.startswith("NOT_PUBLISHED") and not PLACEHOLDER_RE.match(title):
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
        os.makedirs("site/live-cards", exist_ok=True)
        def cands(e):
            out = [u for u in [e["c"]["logo"], guide_icon.get(e["gid"])] if u]
            for key_name in (e["p"]["name"], e["p"].get("original_name", ""), e["c"]["name"]):
                out += alt_logos.get(norm_name(key_name), []) if key_name else []
            return list(dict.fromkeys(out))
        todo = list(visible)   # every channel gets a card (name card when no logo can be loaded)
        with cf.ThreadPoolExecutor(32) as ex:
            res = list(ex.map(lambda e: make_card(cands(e), style(e)[0], f'site/cards/{e["num"]}.jpg', e["p"]["name"],
                                                  live_path=f'site/live-cards/{e["num"]}.jpg',
                                                  live_logo=LOGO_FIX.get(e["p"]["key"]),
                                                  sharp=LOGO_SHARP.get(e["p"]["key"])), todo))
        for e, (good, _) in zip(todo, res):
            if good and base:
                logo_for[e["gid"]] = f'{base}cards/{e["num"]}.jpg'; cards += 1
        changed = [e["p"]["name"] for e, (good, ch) in zip(todo, res) if good and ch is True]
        named = [e["p"]["name"] for e, (good, ch) in zip(todo, res) if good and ch == "name"]
        report.append(f"Logo cards: {sum(g for g, _ in res)} of {len(todo)} made" + ("" if base else " (local test: not linked)"))
        report.append(f"Apex Live logo cards (live-cards/, {LIVE_W}x{LIVE_H}): {len(os.listdir('site/live-cards'))}")
        sharp_by = {k: [e["p"]["name"] for e in todo if LIVE_SHARPER.get(f'site/live-cards/{e["num"]}.jpg') == k] for k in ("feed", "sharp")}
        report.append(f"Sharper Apex Live logos (live-cards/ only): {len(sharp_by['feed'])} from the feed's bigger size, "
                      f"{len(sharp_by['sharp'])} from LOGO_SHARP ({', '.join(sharp_by['sharp']) or 'none'})")
        # logos_local.txt is no longer used: its special://.../apex-logos/ files exist only on the Shield, so Kodi on any
        # other device showed no logo. Every channel now gets its GitHub card (Apex Live shows live-cards/ either way).
        report.append("Playlist logos: GitHub cards for every channel (logos_local.txt not used)")
        fixed = [e for e in visible if e["p"]["key"] in LOGO_FIX and os.path.exists(f'site/live-cards/{e["num"]}.jpg')]
        for e in fixed:                                  # corrected logo: playlist + guide point at Apex Live's card
            if base: logo_for[e["gid"]] = f'{base}live-cards/{e["num"]}.jpg'
        report.append(f"Corrected logos (LOGO_FIX, playlist + live-cards/): {', '.join(e['p']['name'] for e in fixed) or 'none'}")
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
    # sizes are remembered in site/artsizes.json between runs. Only sharp pictures are used (SHARP_H = 720 px tall or
    # more, and wider than tall). The others (Pluto movie posters are 694x1000, some Roku pictures 800x450, a Pluto
    # resize server 330x330, broken links) go to steps 2-5. A picture server that doesn't answer this run (timeout,
    # busy) isn't remembered, so that picture is measured again next run.
    icon_re = re.compile(r'<icon src="([^"]+)"\s*/>')
    def final_xml(x): return pluto_re.sub(to_wide, x) if "images.pluto.tv/series/" in x else x
    for gid in progs: progs[gid] = [(a, b, final_xml(x)) for a, b, x in progs[gid]]   # Pluto widescreen swap
    urls = {m.group(1) for items in progs.values() for _, _, x in items for m in icon_re.finditer(x)}
    # Pluto makes its widescreen picture at whatever size is asked (also from a small original), so those are
    # measured at their original size: the address without the "?..." part.
    real_url = lambda u: u.split("?")[0] if "featuredImage.jpg" in u else u
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
        except urllib.error.HTTPError as e: return None if 400 <= e.code < 500 and e.code != 429 else False
        except Exception: return False                   # no answer this time
        return None
    def measure(us, threads=32):
        """Measure the pictures not measured before. Returns how many that was."""
        todo_urls = sorted({real_url(u) for u in us} - set(sizes))
        with cf.ThreadPoolExecutor(threads) as ex:
            for u, sz in zip(todo_urls, ex.map(img_size, todo_urls)):
                if sz is not False: sizes[u] = sz
        return len(todo_urls)
    def sharp(u):
        sz = sizes.get(real_url(u)); return bool(sz) and sz[1] >= SHARP_H and sz[0] > sz[1]
    new = measure(urls)
    low = {u for u in urls if not sharp(u)}

    # Steps 2-4 for listings without a sharp picture of their own (step 5, the themed backdrop, is no_art_tag below).
    # 2. Pluto movies (and some episodes) send a 694x1000 poster; the same title has a widescreen poster16_9.jpg.
    # 3. Xumo's guide has no pictures, but Xumo's picture server makes a 1920x1080 one from the id in its guide.
    # Missing ones answer "not found". Both are measured like the feed's own pictures before they are used.
    ep_re = re.compile(r'https://images\.pluto\.tv/episodes/([0-9a-f]{24})/')
    xumo_re = re.compile(r'<episode-num system="dd_(?:assetid|progid)">(\w+)</episode-num>')
    def alt_pics(x):
        return ([f"https://images.pluto.tv/episodes/{m.group(1)}/poster16_9.jpg" for m in ep_re.finditer(x)][:1]
                + [f"https://image.xumo.com/v1/assets/asset/{m.group(1)}/1920x1080.jpg" for m in xumo_re.finditer(x)][:1])
    def own_ok(x):
        m = icon_re.search(x); return "<icon" in x and not (m and m.group(1) in low)
    need = [(e["gid"], x) for e in visible for _, _, x in progs[e["gid"]] if not own_ok(x)]
    alt_urls = {u for _, x in need for u in alt_pics(x)}
    new += measure(alt_urls)
    # 4. TVmaze for what is still missing (not for movies: TVmaze only has TV shows). Matched by name + a check.
    chan_names = {e["gid"]: tuple(n for n in (e["p"]["name"], e["p"].get("original_name", ""), e["c"]["name"]) if n) for e in visible}
    def tv_key(gid, x):
        """((show name key, year or 0, channel names), title as listed), or None for movies."""
        m = re.search(r"<title[^>]*>([^<]+)</title>", x)
        if not m or re.search(r"<category[^>]*>Movies?</category>", x): return None
        t = unescape(m.group(1)).strip(); ty = re.search(r"\s*\(((?:19|20)\d\d)\)$", t); dy = re.search(r"<date>(\d{4})", x)
        if ty: t = t[:ty.start()]
        return ((show_key(t), int((ty or dy).group(1)) if ty or dy else 0, chan_names[gid]), t) if show_key(t) else None
    wanted = {}
    for gid, x in need:
        k = None if any(sharp(u) for u in alt_pics(x)) else tv_key(gid, x)
        if k:
            w = wanted.setdefault(k[0], [k[1], 0, set()]); w[1] += 1
            d = re.search(r"<desc[^>]*>([^<]+)</desc>", x); w[2] |= hint_words(unescape(d.group(1))) if d else set()
    tvc, tv = {"q": {}, "s": {}}, {}
    try: tvc.update(json.loads(fetch(base + "tvmaze.json", timeout=30).decode()) if base else json.load(open("site/tvmaze.json")))
    except Exception: pass
    try:
        tv, line = tvmaze_pictures(wanted, tvc, f"{now:%Y-%m-%d}")
        new += measure(set(tv.values()), 4)              # TVmaze's picture server: gently
    except Exception as ex_: line = f"TVmaze: skipped ({type(ex_).__name__}: {ex_})"
    report.append(line)
    try:                                                 # links only; answers more than 30 days overdue are dropped
        old = f"{now - dt.timedelta(days=30):%Y-%m-%d}"
        json.dump({p: {k: v for k, v in tvc[p].items() if v[0] >= old} for p in ("q", "s")}, open("site/tvmaze.json", "w"), separators=(",", ":"))
    except Exception: pass
    def better(gid, x):
        """Steps 2-4: (step, URL) of the first sharp picture, or None."""
        for u in alt_pics(x):
            if sharp(u): return (2 if "pluto" in u else 3), u
        k = tv_key(gid, x); u = tv.get(k[0]) if k else None
        return (4, u) if u and sharp(u) else None
    used = {real_url(u) for u in urls} | alt_urls | set(tv.values())
    sizes = {u: sizes[u] for u in used if u in sizes}    # keep only pictures still in use
    json.dump(sizes, open("site/artsizes.json", "w"), separators=(",", ":"))
    report.append(f"Show pictures checked: {len(used)} ({new} new); feed pictures not sharp (under {SHARP_H} px tall, portrait or broken): {len(low)}")

    gz = gzip.open("site/shield-tv-guide.xml.gz", "wt", encoding="utf-8", compresslevel=9)
    gz.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="free-tv-guide">\n')
    for e in visible:
        logo = logo_for[e["gid"]]
        gz.write(f'<channel id={quoteattr(e["gid"])}><display-name>{escape(e["p"]["name"])}</display-name>'
                 + (f"<icon src={quoteattr(logo)}/>" if logo else "") + "</channel>\n")
    fill_count, with_real_now, with_art, replaced = 0, 0, 0, 0
    steps, svc_pics = [0] * 6, {}                        # listings per picture step (1-5); per service: [sharp, all]
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
            has_art, feed_xml = "<icon" in xml, xml; with_art += has_art
            m = icon_re.search(xml) if has_art else None
            if m and m.group(1) in low:                  # never stretch a small/portrait/broken picture
                xml = xml[:m.start()] + xml[m.end():]; has_art = False; with_art -= 1; replaced += 1
            alt = None if has_art else better(gid, feed_xml)
            if not has_art:
                tag = f"<icon src={quoteattr(alt[1])}/>" if alt else no_art_tag(a)
                if tag: xml = xml[:xml.rindex("</programme>")] + tag + "</programme>"
            src = 1 if has_art else alt[0] if alt else 5
            steps[src] += 1; with_art += src in (2, 3, 4)
            sv = svc_pics.setdefault(e["svc"], [0, 0]); sv[0] += src < 5; sv[1] += 1
            gz.write(xml + "\n")
        for ga, gb in gaps:
            for a, b, title, desc in fillers(ga, gb, f"Live: {nm}", f"Live programming on {nm}.", step):
                gz.write(f'<programme start="{fmt(a)}" stop="{fmt(b)}" channel={quoteattr(gid)}><title>{escape(title)}</title><desc>{escape(desc)}</desc><category>{escape(genre_for[gid])}</category>{no_art_tag(a)}</programme>\n')
                fill_count += 1
    gz.write("</tv>\n"); gz.close()

    # ---------- report
    guide_src = [e for e in visible if e["src_id"] and e["p"]["key"] not in GUIDE_DROP]
    report.append(f"Guide: {real} real listings + {fill_count} 'Live:' fill-in blocks; "
                  f"{with_real_now} of {len(guide_src)} channels with a guide source have a real listing on now")
    # How far ahead each service's real listings go (median of its channels). Under ~2 h, the guide turns into "Live:"
    # blocks before the next run (guide check 2026-11-06: Local Now's feed only reaches ~2-3 h ahead; LG's had run out).
    reach = {}
    for e in guide_src:
        ends = [b for _, b, _ in progs.get(e["gid"], [])]
        reach.setdefault(e["svc"], []).append(max(0.0, (max(ends) - now).total_seconds() / 3600) if ends else 0.0)
    report.append("Guide reach (hours of real listings ahead, median per service): " + ", ".join(
        f"{s} {sorted(v)[len(v) // 2]:.1f} h" + (f" (STALE: {sum(h == 0 for h in v)} of {len(v)} channels have nothing from now on)"
                                              if sum(h == 0 for h in v) > len(v) / 2 else "")
        for s, v in sorted(reach.items())))
    dropped = [e["p"]["name"] for e in visible if e["p"]["key"] in GUIDE_DROP]
    if dropped: report.append(f"Feed guide left out (GUIDE_DROP, wrong listings): {', '.join(dropped)}")
    report.append(f"Program artwork: {with_art} of {real} real listings have a sharp image ({100 * with_art // max(real, 1)}%); "
                  f"{replaced} small/portrait/broken pictures swapped for a sharper one or a themed backdrop")
    report.append("Show pictures by source: " + ", ".join(f"{n} {steps[i]}" for i, n in enumerate(
        ["feed's own", "Pluto movie 16:9", "Xumo by id", "TVmaze", "themed backdrop/card (no sharp picture)"], 1)))
    report.append("Sharp show pictures per service: " + ", ".join(
        f"{s} {a}/{n} ({100 * a // max(n, 1)}%)" for s, (a, n) in sorted(svc_pics.items(), key=lambda kv: -kv[1][1])))
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
