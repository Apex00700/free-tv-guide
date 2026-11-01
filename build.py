"""Rebuild one combined channel list and one 2-day TV guide from free, public sources.

Input : channels.csv  (channel number, group, display name, source match key, My Channels flag)
Output: site/shield-tv.m3u, site/shield-tv-guide.xml.gz, site/report.txt, site/health.json
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
from xml.etree.ElementTree import iterparse, tostring
from xml.sax.saxutils import escape, quoteattr

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
TEST_UA = "Kodi/21.2 (Linux; Android 11) Android/11 Sys_CPU/aarch64 App_Bitness/64 Version/21.2"

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

def main():
    picks = list(csv.DictReader(open("channels.csv", encoding="utf-8")))
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
    entries = []
    for svc, plist in by_service.items():
        pl_url, _, pfx = SOURCES[svc]
        try:
            live = parse_m3u(fetch(pl_url).decode("utf-8", "replace"))
        except Exception as e:
            report.append(f"{svc}: playlist download FAILED ({e}); channels skipped"); continue
        by_id = {c["id"]: c for c in live if c["id"]}
        by_name = {c["name"]: c for c in live}
        found = 0
        for p in plist:
            c = by_id.get(p["key"]) if p["match"] == "id" else by_name.get(p["key"])
            if not c: continue
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

    # ---------- playlist
    lines = [f'#EXTM3U url-tvg="{base}shield-tv-guide.xml.gz"']
    for e in visible:
        p, c = e["p"], e["c"]
        grp = f'My Channels;{p["group"]}' if p.get("mychannels") == "yes" else p["group"]
        lines.append(f'#EXTINF:-1 tvg-id="{q(e["gid"])}" tvg-chno="{e["num"]}" tvg-name="{q(p["name"])}" tvg-logo="{q(c["logo"])}" group-title="{q(grp)}",{p["name"]}')
        if c["ua"]: lines.append(f'#EXTVLCOPT:http-user-agent={c["ua"]}')
        if c["ref"]: lines.append(f'#EXTVLCOPT:http-referrer={c["ref"]}')
        lines.append(c["url"].strip())
    open("site/shield-tv.m3u", "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")

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
                    if a and b > lo and a < hi:
                        el.set("channel", gid); el.tail = None
                        progs[gid].append((a, b, tostring(el, encoding="unicode").strip())); n += 1
                el.clear()
            elif el.tag == "channel":
                el.clear()
        real += n
        report.append(f"{svc}: {n} guide entries")

    gz = gzip.open("site/shield-tv-guide.xml.gz", "wt", encoding="utf-8", compresslevel=9)
    gz.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="free-tv-guide">\n')
    for e in visible:
        logo = e["c"]["logo"]
        gz.write(f'<channel id={quoteattr(e["gid"])}><display-name>{escape(e["p"]["name"])}</display-name>'
                 + (f"<icon src={quoteattr(logo)}/>" if logo else "") + "</channel>\n")
    fill_count, with_real_now = 0, 0
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
        for a, b, xml in items:
            gz.write(xml + "\n")
        for ga, gb in gaps:
            for a, b, title, desc in fillers(ga, gb, f"Live: {nm}", f"Live programming on {nm}.", step):
                gz.write(f'<programme start="{fmt(a)}" stop="{fmt(b)}" channel={quoteattr(gid)}><title>{escape(title)}</title><desc>{escape(desc)}</desc></programme>\n')
                fill_count += 1
    gz.write("</tv>\n"); gz.close()

    # ---------- report
    guide_src = [e for e in visible if e["src_id"]]
    report.append(f"Guide: {real} real listings + {fill_count} 'Live:' fill-in blocks; "
                  f"{with_real_now} of {len(guide_src)} channels with a guide source have a real listing on now")
    report.append(f"Stream check: {len(to_test)} tested, {failed_now} failed this run"
                  + (" (too many failures, treated as a network problem: nothing newly hidden)" if trouble else "")
                  + "; Plex not tested")
    report.append(f"TOTAL channels: {len(visible)} shown, {len(hidden)} hidden, built {now:%Y-%m-%d %H:%M} UTC")
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
