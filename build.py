"""Rebuild one combined channel list and one 2-day TV guide from free, public sources.

Input : channels.csv  (which channels to keep, their channel number and group)
Output: site/shield-tv.m3u and site/shield-tv-guide.xml.gz
Runs on GitHub's servers every 4 hours (see .github/workflows/refresh.yml).
Only public URLs are used. No accounts, keys or passwords.
"""
import csv, gzip, io, os, re, sys, urllib.request, datetime as dt
from xml.etree.ElementTree import iterparse, tostring
from xml.sax.saxutils import escape, quoteattr

DAYS = 2
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

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (free-tv-guide builder)"})
    with urllib.request.urlopen(req, timeout=120) as r:
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

def q(s): return (s or "").replace('"', "'")

def main():
    picks = list(csv.DictReader(open("channels.csv", encoding="utf-8")))
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    owner, _, name = repo.partition("/")
    base = f"https://{owner.lower()}.github.io/{name}/" if repo else ""
    os.makedirs("site", exist_ok=True)

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    lo, hi = now - dt.timedelta(hours=2), now + dt.timedelta(days=DAYS)
    lines = [f'#EXTM3U url-tvg="{base}shield-tv-guide.xml.gz"']
    guide_ids, report = {}, []

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
            gid = f'{pfx}.{c["id"]}' if p["match"] == "id" and c["id"] else ""
            if gid: guide_ids.setdefault(svc, set()).add(c["id"])
            entries.append((int(p["number"]), p, c, gid))
        report.append(f"{svc}: {found}/{len(plist)} channels found")

    entries.sort(key=lambda e: e[0])
    for n, p, c, gid in entries:
        lines.append(f'#EXTINF:-1 tvg-id="{q(gid)}" tvg-chno="{n}" tvg-name="{q(p["name"])}" tvg-logo="{q(c["logo"])}" group-title="{q(p["group"])}",{p["name"]}')
        if c["ua"]: lines.append(f'#EXTVLCOPT:http-user-agent={c["ua"]}')
        if c["ref"]: lines.append(f'#EXTVLCOPT:http-referrer={c["ref"]}')
        lines.append(c["url"].strip())
    open("site/shield-tv.m3u", "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")

    names = {(e[1]["service"], e[2]["id"]): (e[1]["name"], e[2]["logo"]) for e in entries if e[3]}
    gz = gzip.open("site/shield-tv-guide.xml.gz", "wt", encoding="utf-8", compresslevel=9)
    gz.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="free-tv-guide">\n')
    for (svc, cid), (nm, logo) in names.items():
        gz.write(f'<channel id={quoteattr(SOURCES[svc][2] + "." + cid)}><display-name>{escape(nm)}</display-name>'
                 + (f"<icon src={quoteattr(logo)}/>" if logo else "") + "</channel>\n")
    for svc, ids in guide_ids.items():
        gurl, pfx = SOURCES[svc][1], SOURCES[svc][2]
        try:
            raw = fetch(gurl)
            if raw[:2] == b"\x1f\x8b": raw = gzip.decompress(raw)
        except Exception as e:
            report.append(f"{svc}: guide download FAILED ({e})"); continue
        n = 0
        for ev, el in iterparse(io.BytesIO(raw)):
            if el.tag == "programme":
                cid = el.get("channel")
                if cid in ids:
                    try: a, b = parse_time(el.get("start")), parse_time(el.get("stop"))
                    except Exception: a = b = None
                    if a and b > lo and a < hi:
                        el.set("channel", f"{pfx}.{cid}"); el.tail = None
                        gz.write(tostring(el, encoding="unicode").strip() + "\n"); n += 1
                el.clear()
            elif el.tag == "channel":
                el.clear()
        report.append(f"{svc}: {n} guide entries")
    gz.write("</tv>\n"); gz.close()

    report.append(f"TOTAL channels: {len(entries)}  built {now:%Y-%m-%d %H:%M} UTC")
    open("site/report.txt", "w", encoding="utf-8").write("\n".join(report) + "\n")
    print("\n".join(report))
    if len(entries) < len(picks) * 0.5:
        sys.exit("Too many channels missing; not publishing (keeps the previous good version online).")

if __name__ == "__main__":
    main()
