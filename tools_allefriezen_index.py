"""Fetch the AlleFriezen index of the lower courts (Nedergerechten) for one district through Open Archives.

The index gives, per deed: the date, the book (deel) and folio, the scan, and the names of the people in
the spelling of the deed. That makes it a source of names that are certain for a given scan.
Result: cache/allefriezen/<place>.json  (list of deeds; not in git). Can be stopped and started again.

Usage: python tools_allefriezen_index.py "Het Bildt"
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "cache" / "allefriezen"
API = "https://api.openarch.nl/"
UA = "Loghi-studio (local archive research)"
PAUSE = 0.35            # seconds between requests


def get(path, **params):
    url = API + path + "?" + urllib.parse.urlencode(params)
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
                data = json.load(r)
            time.sleep(PAUSE)
            return data
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (attempt + 1))
    raise SystemExit(f"no answer from Open Archives: {last}")


def val(d, *keys):
    for k in keys:
        d = d.get(k) if isinstance(d, dict) else None
    while isinstance(d, dict) and len(d) == 1:
        d = next(iter(d.values()))
    return d if isinstance(d, (str, int)) else None


def as_list(x):
    return x if isinstance(x, list) else [x] if x else []


def deed(rec):
    src = rec.get("a2a_Source", {})
    ref = src.get("a2a_SourceReference", {})
    roles = {}
    for rel in as_list(rec.get("a2a_RelationEP")):
        roles[val(rel, "a2a_PersonKeyRef")] = val(rel, "a2a_RelationType")
    people = []
    for p in as_list(rec.get("a2a_Person")):
        n = p.get("a2a_PersonName", {})
        parts = [val(n, k) for k in ("a2a_PersonNameFirstName", "a2a_PersonNamePatronym", "a2a_PersonNamePrefixLastName", "a2a_PersonNameLastName")]
        people.append({"name": " ".join(x for x in parts if x), "role": roles.get(p.get("pid")),
                       "place": val(p, "a2a_Residence", "a2a_Place"),
                       "remark": val(p, "a2a_PersonRemark", "a2a_Value")})
    scans = []
    for s in as_list(src.get("a2a_SourceAvailableScans", {}).get("a2a_Scan")):
        m = re.search(r"_(\d{4})_(\d{4,6})\.jp2", urllib.parse.unquote(val(s, "a2a_Uri") or ""))
        if m:
            scans.append([m.group(1), m.group(2)])
    d = src.get("a2a_SourceDate", {})
    remarks = {r.get("Key"): val(r, "a2a_Value") for r in as_list(src.get("a2a_SourceRemark"))}
    return {"access": val(ref, "a2a_Archive"), "part": val(ref, "a2a_RegistryNumber"), "book": val(ref, "a2a_Book"),
            "folio": val(ref, "a2a_Folio"), "collection": val(ref, "a2a_Collection"),
            "date": [val(d, "a2a_Year"), val(d, "a2a_Month"), val(d, "a2a_Day")],
            "scans": scans, "people": people, "remarks": remarks}


def main():
    place = sys.argv[1]
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / (re.sub(r"\W+", "_", place.lower()).strip("_") + ".json")
    data = json.loads(dest.read_text(encoding="utf-8")) if dest.exists() else {"place": place, "ids": None, "deeds": {}}
    if data["ids"] is None:
        ids = {}
        for year in range(1500, 1830, 10):      # per ten years: the search gives at most 10,000 results per question
            start = 0
            while True:
                r = get("1.1/records/search.json", name=f"* {year}-{year + 9}", archive="frl", sourcetype="Nedergerechten",
                        eventplace=place, number_show=100, start=start)["response"]
                for doc in r.get("docs", []):
                    ids[doc["identifier"]] = 1
                start += 100
                if start >= r["number_found"] or not r.get("docs"):
                    break
            print(f"list: {year}-{year + 9}: {r['number_found']} people, {len(ids)} deeds so far", flush=True)
        data["ids"] = list(ids)
        dest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    todo = [i for i in data["ids"] if i not in data["deeds"]]
    print(f"{len(data['ids'])} deeds, {len(todo)} still to fetch", flush=True)
    for n, i in enumerate(todo, 1):
        rec = get("1.0/records/show.json", archive="frl", identifier=i)
        data["deeds"][i] = deed(rec[0] if isinstance(rec, list) else rec)
        if n % 200 == 0 or n == len(todo):
            tmp = dest.with_suffix(".part")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(dest)
            print(f"deeds: {n} of {len(todo)}", flush=True)
    print("done:", dest)


if __name__ == "__main__":
    main()
