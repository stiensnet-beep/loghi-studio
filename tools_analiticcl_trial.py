"""Trial: does analiticcl find more of the indexed names than our own rule for reading errors, and at what price?

The AlleFriezen index says which people are named in a deed and on which scan the deed starts. All those
people are given to the real name finder (names.NameSearch), with the spellings it would make for an import.
Per rule for reading errors this counts:

  found   the share of (person, deed) pairs with a hit on the scan of the deed or the next one
  noise   hits per name per 1000 pages in mortgage books of more than a century later, where none of these
          people can be named. A hit there is a namesake (also with exact spelling) or a wrong match.
  near    the same on a book of the same hands, 17 to 33 years away: people of 0090 on the pages of 0093 and
          the other way round. Some of those people really are named there, so this is an upper limit.

Rules: exact spelling only, our rule (names.max_edits), looser rules with plain letter distance, and the
score of analiticcl (https://github.com/proycon/analiticcl, GPL-3.0; optional, not part of Loghi-studio:
python -m pip install --target cache/pkgs analiticcl). All rules work on the folded spelling, so only the
part about reading errors differs.

The second table uses what the index teaches about letter changes: on the scan of the deed a name word
that is not there exactly is compared with the nearest word that is; the same is done on a late page, where
a likeness is chance. A change that is much more common on the right scan (u read as n, i as y, P as S) is a
reading error; a change that is as common on late pages is chance. That is learned on books 0090 and 0091
(late book 0102) and measured on book 0093 (late book 0103), which the learning has not seen.

Usage: python tools_analiticcl_trial.py                 the table of the plain rules
       python tools_analiticcl_trial.py --learned       the table of the rules with learned letter changes
       python tools_analiticcl_trial.py --changes       the learned letter changes
       python tools_analiticcl_trial.py --show RULE     also examples of what RULE gains and what noise it adds
"""
import hashlib
import json
import math
import random
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "cache" / "pkgs"))

import names  # noqa: E402
import server  # noqa: E402

INDEX = HERE / "cache" / "allefriezen" / "het_bildt.json"
WORK = HERE / "cache" / "trial_analiticcl"
CID = "bildt"
TRUE_BOOKS = ("0090", "0091", "0093")       # in the index, 1606-1639
FAR_BOOKS = ("0102", "0103")                # mortgage books 1740-1766
NEAR = {"0090": "0093", "0093": "0090"}
LEARN, LEARN_FAR = ("0090", "0091"), "0102"
TEST, TEST_FAR = ("0093",), "0103"
names.MAX_ROWS = 10 ** 9                    # the trial needs every hit


class Srv:
    """What the name finder needs of the server, without starting one."""
    norm = staticmethod(server.norm)
    lev_le = staticmethod(server.lev_le)
    CONF_UNSURE = server.CONF_UNSURE

    @staticmethod
    def vol_path(cid, vol):
        raise KeyError(vol)                 # no certainty per line in this trial


def root():
    cols = json.loads((HERE / "settings" / "collections.json").read_text(encoding="utf-8"))["collections"]
    return Path(next(c["root"] for c in cols if c["id"] == CID))


def read_book(base, book):
    pages = []
    for f in sorted((base / book / "txt").glob("*.txt")):
        raw = f.read_text(encoding="utf-8", errors="replace").splitlines()
        pages.append((f.stem, raw, [server.norm(x) for x in raw]))
    return pages


def people_of_index(ns, books):
    """([name as the name finder keeps it], [(name id, book, scan of the deed)])."""
    d = json.loads(INDEX.read_text(encoding="utf-8"))
    by, mentions = {}, []
    for x in d["deeds"].values():
        if not x["scans"] or len(x["people"]) < 2:
            continue
        book, scan = x["scans"][0]
        if book not in TRUE_BOOKS or scan not in books[book]:
            continue
        seen = set()
        for p in x["people"]:
            label = names.clean_name(p["name"])
            k = label.lower()
            if k in seen:
                continue
            n = by.get(k)
            if n is None:
                forms = names.spellings(p["name"], key=ns._key)
                if not forms:
                    continue                # only a first name: the name finder does not search for that
                n = by[k] = {"id": f"n{len(by)}", "label": label, "variants": forms, "mode": "fuzzy", "relatives": []}
            seen.add(k)
            mentions.append((n["id"], book, scan))
    return list(by.values()), mentions


def lev(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


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


class Changes:
    """How much more often a letter change is seen on the right scan than on a late page (the ratio)."""
    MIN = 20                                # fewer sightings than this: fall back on a more general count

    def __init__(self, trial, var):
        folded = {b: {p[0]: {trial.v["tokfold"][w] for x in p[2] for w in x.split()} for p in trial.pages[b]}
                  for b in LEARN + (LEARN_FAR,)}
        late = sorted(folded[LEARN_FAR])
        self.true, self.far, self.pairs = Counter(), Counter(), Counter()
        for k, (nid, book, scan) in enumerate(trial.mentions):
            if book not in LEARN:
                continue
            here = folded[book][scan] | folded[book].get(f"{int(scan) + 1:06d}", set())
            f0 = (k * 7919) % (len(late) - 1)
            there = folded[LEARN_FAR][late[f0]] | folded[LEARN_FAR][late[f0 + 1]]
            for w in set(trial.ns._words(trial.label[nid])):
                if len(w) < 4 or w.endswith("dr"):          # -dr (daughter) is written loose in the text
                    continue
                for have, cnt, tag in ((here, self.true, "true"), (there, self.far, "far")):
                    if w in have:
                        continue
                    best = max(((sc, x) for x, sc, d in var.get(w, ()) if x in have and d <= 2), default=None)
                    if best is None:
                        continue
                    self.pairs[tag] += 1
                    for kind, x, y, place in changes(w, best[1]):
                        cnt[kind, x, y, place] += 1
                        cnt[kind, x, y] += 1
                        cnt[kind,] += 1
        self.base = (self.pairs["true"] + 1) / (self.pairs["far"] + 1)

    def ratio(self, kind, x, y, place):
        for key in ((kind, x, y, place), (kind, x, y), (kind,)):
            t, f = self.true[key], self.far[key]
            if t + f >= self.MIN or len(key) == 1:
                return (t + 1) / (f + 1)

    def cost(self, a, lo, hi):
        """Function (kind, from, to, place) -> price of that change: 1 for a neutral one, less for a typical
        reading error, more for a change that is mostly chance."""
        memo = {}

        def price(*key):
            c = memo.get(key)
            if c is None:
                c = memo[key] = min(hi, max(lo, 1 - a * math.log2(self.ratio(*key))))
            return c
        return price

    def table(self, top=60):
        keys = sorted((k for k in set(self.true) | set(self.far) if len(k) == 3), key=lambda k: -(self.true[k] + self.far[k]))
        print(f"{self.pairs['true']} near words on the right scan, {self.pairs['far']} on late pages")
        print("change          right scan   late page   ratio")
        for k in keys[:top]:
            print(f"{k[0]} {k[1] or '-':>2s} > {k[2] or '-':<2s}   {self.true[k]:9d} {self.far[k]:11d} {(self.true[k] + 1) / (self.far[k] + 1):7.2f}")

    def confusables(self, step=0.07):
        """The same knowledge as a confusable list for analiticcl: [(pattern, weight)], weights near 1."""
        abc = "|".join(sorted({k[1] or k[2] for k in self.true if len(k) == 3} | {k[2] for k in self.far if len(k) == 3 and k[2]}))
        out = []
        for k in set(self.true) | set(self.far):
            if len(k) != 3 or self.true[k] + self.far[k] < self.MIN:
                continue
            w = round(1 + step * max(-3, min(3, math.log2(self.ratio(*k, "")))), 3)
            if k[0] == "sub":
                out.append((f"-[{k[1]}]+[{k[2]}]", w))
            elif k[0] == "del":
                out += [(f"-[{k[1]}]=[{abc}]", w), (f"-[{k[1]}]$", w)]
            else:
                out += [(f"=[{abc}]+[{k[2]}]", w), (f"^+[{k[2]}]", w)]
        return sorted(out)


def weighted(a, b, price, limit):
    """Sum of the prices of the cheapest letter changes from a to b, or None when above the limit."""
    n, m = len(a), len(b)
    prev = [0.0] * (m + 1)
    for j in range(1, m + 1):
        prev[j] = prev[j - 1] + price("ins", "", b[j - 1], "^")
    for i in range(1, n + 1):
        ca, place = a[i - 1], "^" if i == 1 else "$" if i == n else ""
        cur = [prev[0] + price("del", ca, "", place)] + [0.0] * m
        best = cur[0]
        for j in range(1, m + 1):
            cb = b[j - 1]
            c = min(prev[j] + price("del", ca, "", place), cur[j - 1] + price("ins", "", cb, "$" if i == n else ""),
                    prev[j - 1] + (0.0 if ca == cb else price("sub", ca, cb, place)))
            cur[j] = c
            if c < best:
                best = c
        if best > limit:
            return None
        prev = cur
    return prev[m] if prev[m] <= limit else None


def analiticcl_variants(words, freq, confusables=None, weights=None):
    """word -> [(word in the text, score of analiticcl, letter distance)], for every word within 3 letters."""
    key = hashlib.sha1(json.dumps([sorted(words), sorted(freq.items()), confusables, weights], ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    WORK.mkdir(parents=True, exist_ok=True)
    cache = WORK / f"variants_{key}.json"
    if cache.exists():
        return {w: [tuple(x) for x in v] for w, v in json.loads(cache.read_text(encoding="utf-8")).items()}
    from analiticcl import SearchParameters, VariantModel, Weights
    abc = Counter()
    for w, n in freq.items():
        for ch in w:
            abc[ch] += n
    for w in words:
        for ch in w:
            abc[ch] += 0
    (WORK / "alphabet.tsv").write_text("".join(ch + "\n" for ch, _ in abc.most_common()), encoding="utf-8", newline="\n")
    (WORK / "lexicon.tsv").write_text("".join(f"{w}\t{n}\n" for w, n in sorted(freq.items())), encoding="utf-8", newline="\n")
    model = VariantModel(str(WORK / "alphabet.tsv"), Weights(**weights) if weights else Weights(), debug=0)
    model.read_lexicon(str(WORK / "lexicon.tsv"))
    if confusables:
        (WORK / "confusables.tsv").write_text("".join(f"{p}\t{w}\n" for p, w in confusables), encoding="utf-8", newline="\n")
        model.read_confusablelist(str(WORK / "confusables.tsv"))
        model.set_confusables_before_pruning()
    model.build()
    params = SearchParameters(max_edit_distance=3, max_anagram_distance=3, score_threshold=0.3, cutoff_threshold=0, max_matches=0)
    out = {}
    for r in model.find_variants_par(sorted(words), params):
        out[r["input"]] = [(x["text"], round(x["score"], 4), lev(r["input"], x["text"])) for x in r["variants"] if x["text"] != r["input"]]
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


class Trial:
    def __init__(self):
        self.ns = names.NameSearch(Srv, tempfile.mkdtemp(), tempfile.mkdtemp())
        self.ns.prices = None               # the trial compares rules for the usual hits only
        base = root()
        self.pages = {b: read_book(base, b) for b in TRUE_BOOKS + FAR_BOOKS}
        scans = {b: {p[0] for p in self.pages[b]} for b in self.pages}
        self.people, self.mentions = people_of_index(self.ns, scans)
        self.label = {n["id"]: n["label"] for n in self.people}
        tokfold, freq = {}, Counter()
        for pages in self.pages.values():
            for _, _, nl in pages:
                for x in nl:
                    for w in x.split():
                        f = tokfold.get(w)
                        if f is None:
                            f = tokfold[w] = names.fold(w)
                        freq[f] += 1
        self.freq = freq
        self.v = {"stamp": 0, "tokfold": tokfold, "folds": set(freq), "alphabet": "".join(sorted(set("".join(freq)))),
                  "by_len": None, "cand": {}}
        self.words = {w for n in self.people for p in [n["label"]] + n["variants"] for w in self.ns._words(p)}
        self.n_far = sum(len(self.pages[b]) for b in FAR_BOOKS)
        self.in_books = {}
        in_books = defaultdict(set)
        for nid, book, _ in self.mentions:
            in_books[nid].add(book)
        self.in_books = in_books
        self.only = {b: {nid for nid, bs in in_books.items() if bs == {b}} for b in NEAR}
        self.near_cells = sum(len(self.only[b]) * len(self.pages[NEAR[b]]) for b in NEAR)

    def run(self, cand=None):
        """{name id: {(book, scan): [line numbers]}} and the same for hits with exact spelling."""
        ns, v = self.ns, dict(self.v, cand={}, by_len=None)
        ns.__dict__.pop("_candidates", None)
        if cand:
            ns._candidates = lambda vv, word, mode: cand(word)
        compiled = ns._compile(self.people, v)
        hits, exact = defaultdict(dict), defaultdict(set)
        for book, pages in self.pages.items():
            rows, _ = ns._search_volume(CID, book, pages, self.people, compiled, v)
            for nid, lst in rows.items():
                for r in lst:
                    hits[nid].setdefault((book, r[0]), []).append(r[1])
                    if r[2]:
                        exact[nid].add((book, r[0]))
        return hits, exact

    def found(self, hits):
        return {(nid, b, s) for nid, b, s in self.mentions if (b, s) in hits.get(nid, ()) or (b, f"{int(s) + 1:06d}") in hits.get(nid, ())}

    def far(self, hits):
        return sum(1 for h in hits.values() for b, _ in h if b in FAR_BOOKS)

    def near(self, hits):
        return sum(1 for b in NEAR for nid in self.only[b] for hb, _ in hits.get(nid, ()) if hb == NEAR[b])

    def line(self, hits, exact, label):
        f, fe = len(self.found(hits)), len(self.found(exact))
        m = len(self.mentions)
        print(f"{label:34s} found {100 * f / m:5.1f}%  (exact spelling {100 * fe / m:4.1f}%)   "
              f"noise {1000 * self.far(hits) / (len(self.people) * self.n_far):6.3f}   near {1000 * self.near(hits) / self.near_cells:6.3f}", flush=True)

    def part(self, hits, books, far_book):
        """(share found, noise) for the people of these books only, with one late book."""
        found = self.found(hits)
        m = [x for x in self.mentions if x[1] in books]
        ids = {nid for nid, bs in self.in_books.items() if bs & set(books)}
        far = sum(1 for nid in ids for b, _ in hits.get(nid, ()) if b == far_book)
        return 100 * sum(1 for x in m if x in found) / len(m), 1000 * far / (len(ids) * len(self.pages[far_book]))

    def split(self, hits, label):
        (fl, nl), (ft, nt) = self.part(hits, LEARN, LEARN_FAR), self.part(hits, TEST, TEST_FAR)
        print(f"{label:40s} learn: found {fl:5.1f}%  noise {nl:6.3f}     test: found {ft:5.1f}%  noise {nt:6.3f}", flush=True)


def rules(var, folds):
    """name -> function(word) -> the words in the text that count as this word."""
    def make(ok):
        def cand(word):
            out = {word} if word in folds else set()
            out.update(c for c, s, d in var.get(word, ()) if ok(word, s, d))
            return out
        return cand

    def by_len(*steps):                     # (up to this length, letters off), the last one for all longer words
        def k(n):
            for top, e in steps:
                if n <= top:
                    return e
            return steps[-1][1]
        return lambda w, s, d: d <= k(len(w))

    out = {"exact": make(lambda w, s, d: False),
           "ours via the same list": make(lambda w, s, d: d <= names.max_edits(w)),
           "letters: 1 from 4, 2 from 9": make(by_len((3, 0), (8, 1), (99, 2))),
           "letters: 1 from 5, 2 from 7": make(by_len((4, 0), (6, 1), (99, 2))),
           "letters: 1 from 4, 2 from 7": make(by_len((3, 0), (6, 1), (99, 2))),
           "letters: 1 from 4, 2 from 6, 3 from 10": make(by_len((3, 0), (5, 1), (9, 2), (99, 3)))}
    for t in (0.80, 0.75, 0.70, 0.65, 0.60, 0.55):
        out[f"analiticcl score {t:.2f}"] = make(lambda w, s, d, t=t: len(w) >= 4 and s >= t)
    for t in (0.70, 0.65, 0.60):
        out[f"ours + analiticcl {t:.2f}"] = make(lambda w, s, d, t=t: d <= names.max_edits(w) or (len(w) >= 4 and s >= t))
    return out


BUDGETS = {"A": (0.6, 1.0, 1.2, 2.0), "B": (0.8, 1.2, 1.5, 2.2), "C": (1.0, 1.4, 1.8, 2.5), "D": (1.0, 1.6, 2.0, 2.8),
           "E": (1.2, 1.8, 2.2, 3.0)}


def learned_rules(trial, var, ch):
    """Rules that know which letter changes are reading errors: our own weighing, and analiticcl with a
    confusable list made of the same counts."""
    folds = trial.v["folds"]
    out = {}

    def budget(steps):                      # allowed sum of prices for words of 4, 5-6, 7-8 and 9 or more letters
        return lambda n: 0 if n <= 3 else steps[0] if n == 4 else steps[1] if n <= 6 else steps[2] if n <= 8 else steps[3]

    for a in (0.25, 0.4):
        price = ch.cost(a, 0.3, 2.0)
        dist = {}
        for w, lst in var.items():
            top = BUDGETS["E"][3] if len(w) >= 9 else BUDGETS["E"][2] if len(w) >= 7 else BUDGETS["E"][1]
            dist[w] = [(c, weighted(w, c, price, top)) for c, s, d in lst]
        for name, steps in BUDGETS.items():
            b = budget(steps)

            def cand(word, dist=dist, b=b):
                o = {word} if word in folds else set()
                lim = b(len(word))
                o.update(c for c, wd in dist.get(word, ()) if wd is not None and wd <= lim)
                return o
            out[f"learned prices (a {a}), budget {name}"] = cand
    conf = analiticcl_variants(trial.words, trial.freq, confusables=ch.confusables())
    for t in (0.75, 0.70, 0.65, 0.60, 0.55):
        def cand(word, t=t):
            o = {word} if word in folds else set()
            if len(word) >= 4:
                o.update(c for c, s, d in conf.get(word, ()) if s >= t)
            return o
        out[f"analiticcl + learned list, score {t:.2f}"] = cand
    return out


def show(trial, base, new, title, n=25):
    """Examples: what the new rule finds on the right scan and ours does not, and the noise it adds."""
    rnd = random.Random(1)
    text = {(b, p[0]): p[1] for b, pages in trial.pages.items() for p in pages}
    gained = sorted(trial.found(new) - trial.found(base))
    print(f"\n{title}: {len(gained)} more found on the scan of the deed. Examples:")
    for nid, b, s in rnd.sample(gained, min(n, len(gained))):
        for scan in (s, f"{int(s) + 1:06d}"):
            for ln in new.get(nid, {}).get((b, scan), [])[:1]:
                raw = text[b, scan]
                nxt = raw[ln] if ln < len(raw) else ""
                print(f"  {trial.label[nid]:30s} {b}/{scan}:{ln:<3d} {raw[ln - 1].strip()[:70]} | {nxt.strip()[:25]}")
    extra = sorted((nid, b, s, ln) for nid, h in new.items() for (b, s), lns in h.items() if b in FAR_BOOKS
                   and (b, s) not in base.get(nid, {}) for ln in lns[:1])
    print(f"\n{title}: {len(extra)} more hits in the late books (noise). Examples:")
    for nid, b, s, ln in rnd.sample(extra, min(n, len(extra))):
        raw = text[b, s]
        nxt = raw[ln] if ln < len(raw) else ""
        print(f"  {trial.label[nid]:30s} {b}/{s}:{ln:<3d} {raw[ln - 1].strip()[:70]} | {nxt.strip()[:25]}")


def main():
    t0 = time.time()
    trial = Trial()
    print(f"{len(trial.people)} names, {len(trial.mentions)} person-deed pairs in {', '.join(TRUE_BOOKS)}; "
          f"{trial.n_far} late pages ({', '.join(FAR_BOOKS)}); {len(trial.v['folds'])} different words in the text; "
          f"{len(trial.words)} different words in the names", flush=True)
    var = analiticcl_variants(trial.words, trial.freq)
    print(f"analiticcl: {sum(len(v) for v in var.values())} pairs of words within 3 letters ({time.time() - t0:.0f} s)", flush=True)
    rs = rules(var, trial.v["folds"])
    want = sys.argv[sys.argv.index("--show") + 1] if "--show" in sys.argv else None
    if "--changes" in sys.argv:
        Changes(trial, var).table()
        return
    ours = trial.run()
    if "--learned" in sys.argv or (want and want not in rs):
        ch = Changes(trial, var)
        trial.split(ours[0], "ours (as the name finder does)")
        plain = [k for k in rs if k.startswith(("exact", "letters", "analiticcl"))]
        for label, cand in list((k, rs[k]) for k in plain) + list(learned_rules(trial, var, ch).items()):
            if want and label != want:
                continue
            res = trial.run(cand)
            trial.split(res[0], label)
            if label == want:
                show(trial, ours[0], res[0], label)
        print(f"{time.time() - t0:.0f} s")
        return
    trial.line(*ours, "ours (as the name finder does)")
    for label, cand in rs.items():
        if want and label not in (want, "ours via the same list"):
            continue
        res = trial.run(cand)
        trial.line(*res, label)
        if label == want:
            show(trial, ours[0], res[0], label)
    print(f"{time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
