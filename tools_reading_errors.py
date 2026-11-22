"""Learn from an index made by people which letter changes are reading errors, for the name finder.

The AlleFriezen index (tools_allefriezen_index.py) says which people are named in a deed and on which scan
the deed starts. For every word of such a name that is not on that scan exactly, the nearest word on the
scan is taken (at most two letters off) and the letter changes between the two are counted. The same is
done on a page of more than a century later, where a likeness can only be chance. A change that is much
more common on the right scan (u read as n, i as y, P as S) belongs to reading errors; a change that is as
common on late pages (a dropped letter) is chance. The counts go to reading_errors.json, which names.py
uses for the hits with a possible reading error.

Usage: python tools_reading_errors.py              learn on all indexed books and write reading_errors.json
       python tools_reading_errors.py --measure    learn on two books, measure on the third: what the hits
                                                   with a possible reading error add, and what noise
       python tools_reading_errors.py --table      show the learned letter changes
"""
import json
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

import names
import server

HERE = Path(__file__).parent
INDEX = HERE / "cache" / "allefriezen" / "het_bildt.json"
CID = "bildt"
TRUE_BOOKS = ("0090", "0091", "0093")       # in the index, 1606-1639
FAR_BOOKS = ("0102", "0103")                # the same kind of book, 1740-1766
LEARN, LEARN_FAR = ("0090", "0091"), ("0102",)
TEST, TEST_FAR = ("0093",), ("0103",)
MIN = 20                                    # a change seen less often than this is not kept on its own
BUDGETS = {"D": ((4, 1.0), (6, 1.6), (8, 2.0), (99, 2.8)), "E": ((4, 1.2), (6, 1.8), (8, 2.2), (99, 3.0)),
           "F": ((4, 1.4), (6, 2.0), (8, 2.5), (99, 3.2)), "G": ((4, 1.6), (6, 2.3), (8, 2.8), (99, 3.5))}


class Srv:
    """What the name finder needs of the server, without starting one."""
    norm = staticmethod(server.norm)
    lev_le = staticmethod(server.lev_le)
    CONF_UNSURE = server.CONF_UNSURE

    @staticmethod
    def vol_path(cid, vol):
        raise KeyError(vol)                 # no certainty per line here


class Data:
    """The text of the books, the people of the index and where they must be."""

    def __init__(self):
        self.ns = names.NameSearch(Srv, tempfile.mkdtemp(), tempfile.mkdtemp())
        cols = json.loads((HERE / "settings" / "collections.json").read_text(encoding="utf-8"))["collections"]
        base = Path(next(c["root"] for c in cols if c["id"] == CID))
        self.pages = {}
        for b in TRUE_BOOKS + FAR_BOOKS:
            self.pages[b] = []
            for f in sorted((base / b / "txt").glob("*.txt")):
                raw = f.read_text(encoding="utf-8", errors="replace").splitlines()
                self.pages[b].append((f.stem, raw, [server.norm(x) for x in raw]))
        tokfold = {}
        for pages in self.pages.values():
            for _, _, nl in pages:
                for x in nl:
                    for w in x.split():
                        if w not in tokfold:
                            tokfold[w] = names.fold(w)
        folds = set(tokfold.values())
        self.v = {"stamp": 0, "tokfold": tokfold, "folds": folds, "alphabet": "".join(sorted(set("".join(folds)))),
                  "by_len": None, "cand": {}}
        self.words = {b: {p[0]: {tokfold[w] for x in p[2] for w in x.split()} for p in self.pages[b]} for b in self.pages}
        by, self.mentions = {}, []          # mentions: (name id, book, scan of the deed)
        for x in json.loads(INDEX.read_text(encoding="utf-8"))["deeds"].values():
            if not x["scans"] or len(x["people"]) < 2:
                continue
            book, scan = x["scans"][0]
            if book not in TRUE_BOOKS or scan not in self.words[book]:
                continue
            seen = set()
            for p in x["people"]:
                label = names.clean_name(p["name"])
                k = label.lower()
                if k in seen:
                    continue
                n = by.get(k)
                if n is None:
                    forms = names.spellings(p["name"], key=self.ns._key)
                    if not forms:
                        continue            # only a first name: the name finder does not search for that
                    n = by[k] = {"id": f"n{len(by)}", "label": label, "variants": forms, "mode": "fuzzy", "relatives": []}
                seen.add(k)
                self.mentions.append((n["id"], book, scan))
        self.people = list(by.values())
        self.label = {n["id"]: n["label"] for n in self.people}
        self.in_books = defaultdict(set)
        for nid, book, _ in self.mentions:
            self.in_books[nid].add(book)

    def run(self, prices):
        """({name id: {(book, scan)}} for the usual hits, the same for hits with a possible reading error)."""
        self.ns.prices = prices
        v = dict(self.v, cand={}, by_len=None)
        compiled = self.ns._compile(self.people, v)
        usual, loose = defaultdict(set), defaultdict(set)
        keep, names.MAX_ROWS = names.MAX_ROWS, 10 ** 9          # every hit counts here
        try:
            for book, pages in self.pages.items():
                rows, _ = self.ns._search_volume(CID, book, pages, self.people, compiled, v)
                for nid, lst in rows.items():
                    for r in lst:
                        (loose if r[2] == names.LOOSE else usual)[nid].add((book, r[0]))
        finally:
            names.MAX_ROWS = keep
        return usual, loose

    def found(self, hits, books):
        """Share of the (person, deed) pairs of these books with a hit on the scan of the deed or the next."""
        m = [x for x in self.mentions if x[1] in books]
        ok = sum(1 for nid, b, s in m if (b, s) in hits.get(nid, ()) or (b, f"{int(s) + 1:06d}") in hits.get(nid, ()))
        return 100 * ok / len(m)

    def noise(self, hits, books, far):
        """Hits per name per 1000 late pages, for the people of these books."""
        ids = [nid for nid, bs in self.in_books.items() if bs & set(books)]
        n = sum(1 for nid in ids for b, _ in hits.get(nid, ()) if b in far)
        return 1000 * n / (len(ids) * sum(len(self.pages[b]) for b in far))


def changes(a, b):
    """The letter changes that turn a (the name) into b (the word in the text): [(kind, from, to, place)].
    Place is ^ at the start of the word, $ at the end, otherwise empty."""
    n, m = len(a), len(b)
    D = [list(range(m + 1))] + [[i] + [0] * m for i in range(1, n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            D[i][j] = min(D[i - 1][j] + 1, D[i][j - 1] + 1, D[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
    out, i, j = [], n, m
    while i or j:
        if i and j and D[i][j] == D[i - 1][j - 1] + (a[i - 1] != b[j - 1]):
            if a[i - 1] != b[j - 1]:
                out.append(("sub", a[i - 1], b[j - 1], "^" if i == 1 else "$" if i == n else ""))
            i, j = i - 1, j - 1
        elif i and D[i][j] == D[i - 1][j] + 1:
            out.append(("del", a[i - 1], "", "^" if i == 1 else "$" if i == n else ""))
            i -= 1
        else:
            out.append(("ins", "", b[j - 1], "^" if i == 0 else "$" if i == n else ""))
            j -= 1
    return out


def nearest(word, have):
    """The word of `have` ([(word, mask)]) nearest to `word`, at most two letters off; None when there is none."""
    wm, n, best = names._pairs_mask(word), len(word), None
    for x, xm in have:
        if abs(len(x) - n) <= 2 and (xm ^ wm).bit_count() <= 8 and server.lev_le(word, x, 2):
            d = 1 if server.lev_le(word, x, 1) else 2
            same = next((i for i, (ca, cb) in enumerate(zip(word, x)) if ca != cb), min(n, len(x)))
            key = (d, -same, x)
            if best is None or key < best:
                best = key
    return best[2] if best else None


def learn(data, books, far):
    """The counts for reading_errors.json: {"kind|from|to|place": [on the right scan, on a late page]}."""
    masks = {}

    def pair(book, scan):
        key = (book, scan)
        if key not in masks:
            ws = data.words[book][scan] | data.words[book].get(f"{int(scan) + 1:06d}", set())
            masks[key] = (ws, [(w, names._pairs_mask(w)) for w in ws])
        return masks[key]

    late = [(b, p[0]) for b in far for p in data.pages[b]]
    true, chance, pairs = Counter(), Counter(), [0, 0]
    for k, (nid, book, scan) in enumerate(data.mentions):
        if book not in books:
            continue
        for w in set(data.ns._words(data.label[nid])):
            if len(w) < 4 or w.endswith("dr"):                  # -dr (daughter) is written loose in the text
                continue
            for which, (ws, have) in enumerate((pair(book, scan), pair(*late[(k * 7919) % len(late)]))):
                x = None if w in ws else nearest(w, have)
                if x is None:
                    continue
                pairs[which] += 1
                cnt = chance if which else true
                for kind, a, b, place in changes(w, x):
                    cnt[f"{kind}|{a}|{b}|{place or '-'}"] += 1
                    cnt[f"{kind}|{a}|{b}|*"] += 1
                    cnt[kind] += 1
    counts = {k: [true[k], chance[k]] for k in sorted(set(true) | set(chance))
              if true[k] + chance[k] >= MIN or "|" not in k}
    return {"about": "Letter changes between a name and the word Loghi read: how often on the scan where the name "
                     "must be, and how often on a page where a likeness is chance. Made by tools_reading_errors.py.",
            "learned": {"collection": "Het Bildt (Tresoar 13-07)", "index": "AlleFriezen through Open Archives (CC0)",
                        "books": list(books), "late books": list(far), "date": time.strftime("%Y-%m-%d"),
                        "near words on the right scan": pairs[0], "near words on late pages": pairs[1]},
            "min": MIN, "counts": counts}


def table(model, top=45):
    print("change                 right scan   late page   ratio")
    rows = [(k.split("|"), v) for k, v in model["counts"].items() if k.endswith("|*")]
    for (kind, a, b, _), (t, f) in sorted(rows, key=lambda r: -(r[1][0] + r[1][1]))[:top]:
        what = f"{a} read as {b}" if kind == "sub" else f"{a} missing" if kind == "del" else f"{b} too many"
        print(f"{what:20s} {t:12d} {f:11d} {(t + 1) / (f + 1):7.2f}")


def measure(data):
    model = learn(data, LEARN, LEARN_FAR)
    prices = names.Prices(model)
    usual, _ = data.run(None)
    print(f"{len(data.people)} names, {len(data.mentions)} person-deed pairs; learned on {', '.join(LEARN)}, "
          f"measured on {', '.join(TEST)} (noise: hits per name per 1000 pages of {', '.join(TEST_FAR)})")
    print(f"{'usual hits only':28s} found {data.found(usual, TEST):5.1f}%   noise {data.noise(usual, TEST, TEST_FAR):6.3f}")
    keep = names.LOOSE_BUDGET
    try:
        for label, budget in BUDGETS.items():
            names.LOOSE_BUDGET = budget
            t0 = time.time()
            u, loose = data.run(prices)
            both = {nid: u.get(nid, set()) | loose.get(nid, set()) for nid in set(u) | set(loose)}
            print(f"with reading errors, budget {label} found {data.found(both, TEST):5.1f}%   noise {data.noise(both, TEST, TEST_FAR):6.3f}"
                  f"   (the extra hits alone: noise {data.noise(loose, TEST, TEST_FAR):6.3f}; {time.time() - t0:.0f} s)", flush=True)
    finally:
        names.LOOSE_BUDGET = keep


def main():
    data = Data()
    if "--measure" in sys.argv:
        measure(data)
        return
    model = learn(data, TRUE_BOOKS, FAR_BOOKS)
    if "--table" in sys.argv:
        table(model)
        return
    names.ERRORS_FILE.write_text(json.dumps(model, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(model['counts'])} letter changes written to {names.ERRORS_FILE.name}")
    table(model, 20)


if __name__ == "__main__":
    main()
