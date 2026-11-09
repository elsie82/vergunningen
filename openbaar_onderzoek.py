"""Scrape running public inquiries (openbaar onderzoek) from the commune websites.

fetch_all() returns ({projectnummer: {"oo_van", "oo_tot", "oo_url", "oo_titel", "commune"}}, status).
The sources are configured in sources.json (edit that file when a commune changes its website).
Two site types are supported:
  - remmicom (consultatieomgeving.net): JSON API, OMV number in title or detail text
  - lcp (tervuren.be, overijse.be): HTML list -> detail page -> PDF affiche with OMV number.
    If the list page has moved, a new one is looked up via the site's sitemap and homepage.
PDF results are cached in oo_cache.json (keyed by detail URL) so they are only downloaded once.
status is one entry per commune; entries with "errors" mean the source needs attention.
"""
import io, re, json, time, html, pathlib, datetime as dt
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
import pypdf

HERE = pathlib.Path(__file__).parent
SOURCES = HERE / "sources.json"
CACHE = HERE / "oo_cache.json"
STATE = HERE / "oo_state.json"  # last_ok / last_found per commune, kept between runs
STALE_DAYS = 30                 # no inquiries found for this long -> probably a broken source
UA = {"User-Agent": "omv-viewer/1.0 (personal project)"}
OMV = re.compile(r"OMV[_ ]?(\d{10})")
NOTICE = re.compile(r"(bekendmaking )?openbaar onderzoek", re.I)
CANDIDATE = re.compile(r"openba(?:re|ar)[-_ ]?onderzoek", re.I)  # urls with openbare-onderzoek(en) or openbaar-onderzoek
MONTHS = {m: i + 1 for i, m in enumerate("januari februari maart april mei juni juli augustus "
                                         "september oktober november december".split())}

def get(url, polite=False, timeout=60):
    if polite:
        time.sleep(1)  # LCP sites ask for Crawl-delay: 1 in robots.txt
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r

def text(markup):
    return " ".join(BeautifulSoup(markup or "", "html.parser").get_text(" ").split())

def parse_period(t):
    """First 'van <date> tot (en met) <date>' in the text, as ISO dates; handles 18/10/2026 and 8 oktober 2026."""
    d = r"(\d{1,2}/\d{1,2}/\d{4}|\d{1,2} (?:%s) \d{4})" % "|".join(MONTHS)
    m = re.search(rf"van {d} (?:tot en met|tot|t\.e\.m\.?) {d}", t, re.I)
    if not m:
        return None, None
    def iso(s):
        if "/" in s:
            day, mon, yr = map(int, s.split("/"))
        else:
            day, mon, yr = s.split()
            day, mon, yr = int(day), MONTHS[mon.lower()], int(yr)
        return dt.date(yr, mon, day).isoformat()
    return iso(m[1]), iso(m[2])

def remmicom(commune, base, warnings):
    out = {}
    for it in get(f"{base}/web/burger/openbare-onderzoeken").json():
        title, guid = text(it.get("titel")), it["externalIdentifier"]
        m = OMV.search(title)
        if not m:  # Huldenberg puts the number only in the body
            det = get(f"{base}/web/burger/openbare-onderzoeken/{guid}").json()
            m = OMV.search(text(json.dumps(det, ensure_ascii=False)))
        if not m:
            warnings.append(f"geen OMV-nummer gevonden in '{title[:60]}'")
            continue
        out[m[1]] = {"commune": commune, "oo_titel": title, "oo_van": (it.get("datumVan") or "")[:10] or None,
                     "oo_tot": (it.get("datumTot") or "")[:10] or None,
                     "oo_url": f"{base}/burger/nl/openbare-onderzoeken/{guid}"}
    return out

def notice_links(url):
    """Links on a page whose text starts with 'openbaar onderzoek' (the individual notices)."""
    soup = BeautifulSoup(get(url, polite=True).text, "html.parser")
    links = {urljoin(url, a["href"]): " ".join(a.get_text(" ").split()) for a in soup.select("a[href]")}
    return soup, {u: t for u, t in links.items() if NOTICE.match(t)}

def discover(url, max_pages=10):
    """The list page has moved or is empty: find a page listing notices via the sitemap and homepage."""
    root = "{0.scheme}://{0.netloc}".format(urlparse(url))
    queue = []
    try:
        queue += [html.unescape(l) for l in re.findall(r"<loc>([^<]+)</loc>", get(root + "/sitemap.xml", polite=True).text)]
    except requests.RequestException:
        pass
    try:
        queue += [urljoin(root, a["href"]) for a in BeautifulSoup(get(root, polite=True).text, "html.parser").select("a[href]")]
    except requests.RequestException:
        pass
    seen, host = {url}, urlparse(root).netloc
    queue = [u for u in dict.fromkeys(queue) if CANDIDATE.search(u) and urlparse(u).netloc == host]
    # list pages ("openbare-onderzoeken") before single notices ("openbaar-onderzoek-bollestraat-30")
    queue.sort(key=lambda u: not re.search(r"openbare[-_ ]?onderzoeken", u, re.I))
    while queue and len(seen) <= max_pages:
        cand = queue.pop(0)
        if cand in seen:
            continue
        seen.add(cand)
        try:
            soup, links = notice_links(cand)
        except requests.RequestException:
            continue
        if links:
            return cand, links
        # e.g. a theme page that links on to the actual list
        queue += [h for h in (urljoin(cand, a["href"]) for a in soup.select("a[href]"))
                  if CANDIDATE.search(h) and urlparse(h).netloc == host and h not in seen]
    return None

def lcp(commune, urls, cache, errors):
    """Inquiries from one or more list pages; a page that no longer works is looked up again via discover()."""
    links = {}
    for url in urls:
        try:
            links.update(notice_links(url)[1])
        except requests.RequestException as e:
            found = discover(url)
            if not found:
                errors.append(f"{url} werkt niet ({e}) en er is geen nieuwe pagina gevonden")
                continue
            errors.append(f"pagina verhuisd: vervang in sources.json {url} door {found[0]}")
            links.update(found[1])
    out = {}
    for link, title in links.items():
        if link not in cache:
            det = BeautifulSoup(get(link, polite=True).text, "html.parser")
            body = " ".join(det.get_text(" ").split())
            for a in det.select("a[href*='/file/download/']"):
                r = get(urljoin(link, a["href"]), polite=True, timeout=120)
                if r.content[:4] == b"%PDF":
                    body += " " + " ".join(" ".join(p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(r.content)).pages).split())
            m = OMV.search(body)
            van, tot = parse_period(body)
            cache[link] = {"nr": m[1] if m else None, "oo_van": van, "oo_tot": tot}
        c = cache[link]
        if not c["nr"]:  # e.g. inquiries on plans (MAP7, RUP) that are not permit files
            continue
        out[c["nr"]] = {"commune": commune, "oo_titel": title, "oo_van": c["oo_van"], "oo_tot": c["oo_tot"], "oo_url": link}
    return out

def load(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

def fetch_all():
    cache, state = load(CACHE), load(STATE)
    today = dt.date.today().isoformat()
    stale = (dt.date.today() - dt.timedelta(days=STALE_DAYS)).isoformat()
    out, status = {}, []
    for commune, src in load(SOURCES).items():
        st = {"commune": commune, "type": src["type"], "url": src["url"], "count": 0, "errors": [], "warnings": []}
        status.append(st)
        if src["type"] == "none":
            st["warnings"].append("geen bron ingesteld in sources.json")
            continue
        prev = state.setdefault(commune, {"last_found": today})  # new source: start the stale clock today
        urls = src["url"] if isinstance(src["url"], list) else [src["url"]]
        try:
            if src["type"] == "remmicom":
                found = remmicom(commune, urls[0], st["warnings"])
            else:
                found = lcp(commune, urls, cache, st["errors"])
            out.update(found)
            st["count"] = len(found)
            if not st["errors"]:
                prev["last_ok"] = today
            if found:
                prev["last_found"] = today
            elif prev["last_found"] < stale:
                msg = f"al sinds {prev['last_found']} geen openbare onderzoeken gevonden: controleer de bron"
                hint = discover(urls[0]) if src["type"] == "lcp" else None  # page still loads but was replaced?
                st["errors"].append(msg + (f"; mogelijk nieuwe pagina: {hint[0]}" if hint else ""))
        except Exception as e:  # one broken site must not break the rest of the update
            st["errors"].append(f"bron werkt niet: {e}")
        st.update({k: prev.get(k) for k in ("last_ok", "last_found")})
        print(f"  {commune}: {st['count']} openbare onderzoeken" + "".join(f"\n    FOUT: {m}" for m in st["errors"])
              + "".join(f"\n    let op: {m}" for m in st["warnings"]))
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return out, status

if __name__ == "__main__":
    for nr, v in fetch_all()[0].items():
        print(nr, v["commune"], v["oo_van"], v["oo_tot"], v["oo_titel"][:60])
