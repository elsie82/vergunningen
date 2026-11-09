"""Fetch Omgevingsloket planning projects for selected communes -> data.json + data.xlsx"""
import json, re, sys, time, pathlib, datetime as dt
import requests
from shapely.geometry import shape
from shapely.prepared import prep
import pandas as pd
import openbaar_onderzoek

API = "https://www.mercator.vlaanderen.be/raadpleegdienstenmercatorpubliek/ogc/features/v1/collections/{}/items"
LAYERS = {"stedenbouwkundig": "lu:lu_omv_gd_v2", "verkaveling": "lu:lu_omv_vk_v2"}  # same fields; stored as "laag"
COMMUNES = ["Tervuren", "Hoeilaart", "Bertem", "Overijse", "Huldenberg"]
MIN_DATE = "2025-01-01"          # datum_huidige_toestand must be >= this
OVERLAP_DAYS = 7                 # incremental run: re-fetch from (previous run - this), catches late API changes
FULL_EVERY_DAYS = 7              # a full run from MIN_DATE at least this often, to drop files removed from the API
STUB = "Onbekend (niet in API)"  # huidige_toestand of inquiries that have no API record
HERE = pathlib.Path(__file__).parent
UA = {"User-Agent": "omv-viewer/1.0 (personal project)"}

def boundaries():
    """Commune polygons, cached in communes.geojson (fetched once from OSM Nominatim)."""
    cache = HERE / "communes.geojson"
    if cache.exists():
        return json.loads(cache.read_text())
    out = {}
    for name in COMMUNES:
        r = requests.get("https://nominatim.openstreetmap.org/search", headers=UA, timeout=60,
                         params={"q": f"{name}, Vlaams-Brabant, Belgium", "format": "jsonv2",
                                 "polygon_geojson": 1, "limit": 1})
        r.raise_for_status()
        g = r.json()[0]["geojson"]
        if g["type"] not in ("Polygon", "MultiPolygon"):
            sys.exit(f"No polygon for {name}")
        out[name] = g
        time.sleep(1.2)  # Nominatim usage policy
    cache.write_text(json.dumps(out))
    return out

def fetch(collection, bbox, since):
    params = {"f": "application/geo+json", "limit": 1000, "bbox": ",".join(map(str, bbox)),
              "filter": f"datum_huidige_toestand >= DATE('{since}')", "filter-lang": "cql2-text"}
    url, feats = API.format(collection), []
    while url:
        r = requests.get(url, params=params, headers=UA, timeout=120)
        if r.status_code >= 400 and "filter" in params:   # server rejects CQL2 -> filter locally
            print("Server-side filter rejected, filtering locally")
            params.pop("filter"); params.pop("filter-lang"); continue
        r.raise_for_status()
        d = r.json()
        feats += d["features"]
        url = next((l["href"] for l in d.get("links", []) if l.get("rel") == "next"), None)
        params = None  # next link already carries all params
    return feats

def load_previous():
    path = HERE / "data.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

def main(force_full=False):
    """Incremental by default: only files changed since the previous run (minus OVERLAP_DAYS) are fetched
    and merged into data.json. Every FULL_EVERY_DAYS (or with --full) everything from MIN_DATE is re-fetched."""
    prev, today = load_previous(), dt.date.today()
    last_full = (prev or {}).get("last_full")
    full = (force_full or not prev or not last_full or prev.get("layers") != list(LAYERS)  # new layer: fetch its history
            or last_full < (today - dt.timedelta(days=FULL_EVERY_DAYS)).isoformat())
    since = MIN_DATE if full else max(MIN_DATE, (dt.date.fromisoformat(prev["generated"][:10])
                                                 - dt.timedelta(days=OVERLAP_DAYS)).isoformat())
    print(f"{'Volledige' if full else 'Incrementele'} update: dossiers gewijzigd vanaf {since}")
    polys = {n: shape(g) for n, g in boundaries().items()}
    prepared = {n: prep(p) for n, p in polys.items()}
    minx, miny, maxx, maxy = [min(p.bounds[i] for p in polys.values()) if i < 2
                              else max(p.bounds[i] for p in polys.values()) for i in range(4)]
    rows = []
    for laag, f in ((laag, f) for laag, col in LAYERS.items() for f in fetch(col, (minx, miny, maxx, maxy), since)):
        p = f["properties"]
        in_progress = (p.get("huidige_toestand") or "").startswith("In behandeling")
        # For in-progress files the API date is just the extraction day, so it is not a decision date
        # (and they come back in every incremental run, so their status is always current)
        decision_date = None if in_progress else p.get("datum_huidige_toestand")
        if not in_progress and (not decision_date or decision_date < since):
            continue
        pt = shape(f["geometry"]).representative_point()
        commune = next((n for n, pp in prepared.items() if pp.contains(pt)), None)
        if not commune:
            continue
        nr = re.sub(r"\D", "", p.get("projectnummer") or "")
        rows.append({**{k: v for k, v in p.items() if k != "geom"},
                     "laag": laag, "commune": commune, "beslissingsdatum": decision_date, "lon": round(pt.x, 6), "lat": round(pt.y, 6),
                     "inzage_url": f"https://omgevingsloketinzage.omgeving.vlaanderen.be/{nr}/inhoud-aanvraag"})
    print(f"{len(rows)} dossiers opgehaald")
    # one row per file and layer (the paging can return duplicates; newer fetch wins over the previous data.json),
    # newest decision first
    old = [] if full else [r for r in prev["records"] if r.get("huidige_toestand") != STUB]
    rows = list({(r["projectnummer"], r.get("laag")): r for r in old + rows}.values())
    rows.sort(key=lambda r: r["beslissingsdatum"] or "", reverse=True)
    return save(*add_inquiries(rows), last_full=today.isoformat() if full else last_full)

def add_inquiries(rows):
    """Add the openbaar onderzoek period (oo_van/oo_tot) from the commune websites; not in the API."""
    rows = [{k: v for k, v in r.items() if not k.startswith("oo_")} for r in rows]
    by_nr = {}  # a project can be in both layers: give every row the inquiry
    for r in rows:
        by_nr.setdefault(re.sub(r"\D", "", r["projectnummer"] or ""), []).append(r)
    inquiries, status = openbaar_onderzoek.fetch_all()
    for nr, oo in inquiries.items():
        if nr in by_nr:
            for r in by_nr[nr]:
                r.update({k: v for k, v in oo.items() if k != "commune"})
        else:  # announced by the commune but not (yet) in the API extract: keep it visible
            rows.append({"projectnummer": f"OMV_{nr}", "commune": oo["commune"], "huidige_toestand": STUB,
                         "beslissingsdatum": None, "project_link": f"https://omgevingsloket.omgeving.vlaanderen.be?project={nr}",
                         "inzage_url": f"https://omgevingsloketinzage.omgeving.vlaanderen.be/{nr}/inhoud-aanvraag", **oo})
    print(f"{len(inquiries)} openbare onderzoeken, {sum(nr in by_nr for nr in inquiries)} gekoppeld aan API-dossiers")
    return rows, status

def save(rows, oo_status, last_full):
    """Write data.json/data.xlsx; returns 1 when a commune source needs attention (so the workflow fails and mails)."""
    out = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "last_full": last_full, "layers": list(LAYERS),
           "min_date": MIN_DATE, "count": len(rows), "oo_status": oo_status, "records": rows}
    (HERE / "data.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    pd.DataFrame(rows).to_excel(HERE / "data.xlsx", index=False)
    print(f"{len(rows)} records")
    broken = [s["commune"] for s in oo_status if s["errors"]]
    if broken:
        print(f"Bron(nen) openbaar onderzoek vragen aandacht: {', '.join(broken)} (zie hierboven)")
    return 1 if broken else 0

if __name__ == "__main__":
    if "--oo-only" in sys.argv:  # quick local refresh: reuse data.json, only re-scrape the commune websites
        prev = load_previous()
        sys.exit(save(*add_inquiries([r for r in prev["records"] if r.get("huidige_toestand") != STUB]),
                      last_full=prev.get("last_full")))
    else:
        sys.exit(main(force_full="--full" in sys.argv))
