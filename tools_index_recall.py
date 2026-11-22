"""How many of the names in the AlleFriezen index does the Loghi text contain on the indexed scan?

The index (tools_allefriezen_index.py) says which people are named in a deed and on which scan it starts.
For every word of those names (4 letters or more) this counts: read right (same spelling after folding),
nearly (one or two letters off), or missing, in the text of that scan and the next one.

Usage: python tools_index_recall.py                 the text in the books, per book
       python tools_index_recall.py --pick [N]      choose N deeds (default 75) -> testset/sample_index.json
       python tools_index_recall.py --runs NAME ... compare runs of that sample (tools_testset_run.py NAME --sample sample_index.json)
"""
import json
import random
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

import names
import server

HERE = Path(__file__).parent
OUT = HERE / "testset"
INDEX = HERE / "cache" / "allefriezen" / "het_bildt.json"
CID = "bildt"
WORD = re.compile(r"[^\W\d_]{4,}")


def key(w):
    return names.fold(server.norm(w))


def root():
    cols = json.loads((HERE / "settings" / "collections.json").read_text(encoding="utf-8"))["collections"]
    return Path(next(c["root"] for c in cols if c["id"] == CID))


def deeds():
    d = json.loads(INDEX.read_text(encoding="utf-8"))
    return [x for x in d["deeds"].values() if x["scans"] and len(x["people"]) > 1]


def page_keys(xml):
    if not xml.exists():
        return None
    r = ET.parse(xml).getroot()
    ns = r.tag.split("}")[0] + "}"
    out = set()
    for line in r.iter(ns + "TextLine"):
        eqs = line.findall(ns + "TextEquiv")
        u = eqs[-1].find(ns + "Unicode") if eqs else None
        if u is not None and u.text:
            out.update(key(w) for w in WORD.findall(u.text))
    return out


def score(deed, have, c):
    for p in deed["people"]:
        for w in WORD.findall(p["name"]):
            k = key(w)
            if len(k) < 4:
                continue
            e = names.max_edits(k)
            if k in have:
                c["right"] += 1
            elif e and any(abs(len(h) - len(k)) <= e and server.lev_le(h, k, e) for h in have):
                c["near"] += 1
            else:
                c["missing"] += 1


def show(label, c):
    n = c["right"] + c["near"] + c["missing"]
    if n:
        print(f"{label:22s}{n:7d} name words: right {100 * c['right'] / n:3.0f}%  nearly {100 * c['near'] / n:3.0f}%  missing {100 * c['missing'] / n:3.0f}%")


def books():
    base = root()
    by = defaultdict(Counter)
    cache = {}
    for x in deeds():
        book, scan = x["scans"][0]
        have = set()
        for i in (0, 1):
            k = (book, f"{int(scan) + i:06d}")
            if k not in cache:
                cache[k] = page_keys(base / k[0] / "page" / f"{k[1]}.xml")
            have |= cache[k] or set()
        if cache[book, scan] is None:
            continue
        score(x, have, by[book])
        score(x, have, by["all"])
    for b in sorted(by):
        show(b, by[b])


def pick(n):
    rnd = random.Random(20261121)
    base = root()
    ok = [x for x in deeds() if len(x["people"]) >= 3 and all((base / x["scans"][0][0] / "page" / f"{int(x['scans'][0][1]) + i:06d}.xml").exists() for i in (0, 1))]
    rnd.shuffle(ok)
    sample, seen = [], set()
    for x in ok:
        book, scan = x["scans"][0]
        pair = [f"{int(scan) + i:06d}" for i in (0, 1)]
        if any((book, s) in seen for s in pair):
            continue
        seen.update((book, s) for s in pair)
        sample += [{"block": f"{CID}_{book}_{s}", "collection": CID, "book": book, "scan": s} for s in pair]
        if len(sample) >= 2 * n:
            break
    (OUT / "sample_index.json").write_text(json.dumps(sample, indent=1), encoding="utf-8", newline="\n")
    print(len(sample), "scans in testset/sample_index.json:", dict(Counter(s["book"] for s in sample)))


def runs(run_names):
    base = root()
    blocks = {(s["book"], s["scan"]) for s in json.loads((OUT / "sample_index.json").read_text(encoding="utf-8"))}
    table = {n: Counter() for n in ["books"] + run_names}
    used = 0
    for x in deeds():
        book, scan = x["scans"][0]
        pair = [f"{int(scan) + i:06d}" for i in (0, 1)]
        if not all((book, s) in blocks for s in pair):
            continue
        if not all((OUT / "runs" / n / "page" / f"{CID}_{book}_{s}.xml").exists() for n in run_names for s in pair):
            continue                    # a run that read only part of the sample: compare on the deeds every run has
        used += 1
        for n in table:
            have = set()
            for s in pair:
                xml = base / book / "page" / f"{s}.xml" if n == "books" else OUT / "runs" / n / "page" / f"{CID}_{book}_{s}.xml"
                have |= page_keys(xml) or set()
            score(x, have, table[n])
    print(used, "deeds")
    for n, c in table.items():
        show(n, c)


if __name__ == "__main__":
    if "--pick" in sys.argv:
        i = sys.argv.index("--pick")
        pick(int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 75)
    elif "--runs" in sys.argv:
        runs(sys.argv[sys.argv.index("--runs") + 1:])
    else:
        books()
