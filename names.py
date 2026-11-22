"""Name finder of Loghi-studio.

Lists of names (settings/names.json) that are managed in the interface. Every name has a label, spelling
variants, a mode ("fuzzy" = also reading errors, "exact" = spelling variants only), the collections it
applies to (empty = all), the list it belongs to and optionally relatives and references to a family tree.
A background thread searches all read text for these names and stores the hits per collection in
cache/names/<collection>.json. Per book it remembers for which names it has searched, so after a change
only the changed names are searched again.

Every hit also gets the certainty Loghi gave that line. A hit is strong when the spelling matches exactly
or Loghi is sure of the line, otherwise weak. A hit is supported when a relative of that person is named
on the same scan.

Apart from these there are hits with a possible reading error ("loose"): one word of the name is there as
usual and the other one only fits when Loghi made its typical reading errors in it (u read as n, y as i,
P as S). Which letter changes are typical is in reading_errors.json, learned from an index made by people
(tools_reading_errors.py). These hits are kept apart: they are only shown when asked for, and they never
count as strong or as support for somebody else.
"""
import csv
import hashlib
import io
import itertools
import json
import math
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MODES = ("fuzzy", "exact")
STRENGTHS = ("best", "supported", "strong", "all", "loose", "loose_supported")
SIMILAR, EXACT, LOOSE = 0, 1, 2     # how the spelling of a hit fits the name (third field of a row)
MAX_ROWS = 300          # hits kept per name per book; the real number is remembered
COMMON = 100            # more strong hits than this in one collection: a very common name
MAX_VARIANTS = 60
DEFAULT_LIST = "mine"

_FOLD = (("ph", "f"), ("dt", "t"), ("uw", "w"), ("c", "k"), ("z", "s"), ("v", "f"))
_DOUBLE = re.compile(r"(\w)\1+")


def fold(word):
    """Extra spelling folding on top of server.norm: c=k, z=s, v=f, ph=f, dt=t, uw=w."""
    for a, b in _FOLD:
        if a in word:
            word = word.replace(a, b)
    return _DOUBLE.sub(r"\1", word)


def _pairs_mask(word):
    """One bit per pair of neighbouring letters (with the word edges), spread over 256 bits."""
    w, m = "^" + word + "$", 0
    for i in range(len(w) - 1):
        m |= 1 << ((ord(w[i]) * 31 + ord(w[i + 1]) * 7) & 255)
    return m


def max_edits(word):
    """Allowed reading errors per word; short words must match exactly (otherwise 'welke' becomes Eelck)."""
    n = len(word)
    return 0 if n <= 4 else 1 if n <= 8 else 2


# ---------------------------------------------------------------- typical reading errors

ERRORS_FILE = Path(__file__).resolve().parent / "reading_errors.json"
LOOSE_BUDGET = ((4, 1.4), (6, 2.0), (8, 2.5), (99, 3.2))    # (word length up to, allowed sum of prices)


class Prices:
    """What a letter change costs when a word of a name is compared with a word in the text.

    The counts say how often a change was seen on the scan where the name must be (first number) and how
    often on pages where a likeness can only be chance (second number). A change that belongs to reading
    errors is cheap, a change that is mostly chance (a dropped letter) is dear, a neutral one costs 1.
    Keys: kind|from|to|place with place ^ (start of the word), $ (end), - (in between) or * (anywhere),
    and the kind alone (sub, del, ins) for all changes of that kind together."""

    def __init__(self, data, a=0.25, low=0.3, high=2.0):
        self.counts = {tuple(k.split("|")): (v[0], v[1]) for k, v in data["counts"].items()}
        self.min = data.get("min", 20)
        self.a, self.low, self.high = a, low, high
        self._memo = {}
        key = json.dumps([sorted(data["counts"].items()), self.min, a, low, high, LOOSE_BUDGET])
        self.stamp = hashlib.sha1(key.encode("utf-8")).hexdigest()[:6]

    @classmethod
    def load(cls, path=None):
        try:
            return cls(json.loads(Path(path or ERRORS_FILE).read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError):
            return None                     # without the file there are no hits with a possible reading error

    def ratio(self, kind, x, y, place):
        for key in ((kind, x, y, place or "-"), (kind, x, y, "*"), (kind,)):
            t, f = self.counts.get(key, (0, 0))
            if t + f >= self.min or len(key) == 1:
                return (t + 1) / (f + 1)

    def price(self, kind, x, y, place):
        key = (kind, x, y, place)
        c = self._memo.get(key)
        if c is None:
            c = self._memo[key] = min(self.high, max(self.low, 1 - self.a * math.log2(self.ratio(*key))))
        return c

    @staticmethod
    def budget(n):
        """The sum of prices a word of n letters may cost; 0 = too short for a reading error."""
        if n < 4:
            return 0
        for top, b in LOOSE_BUDGET:
            if n <= top:
                return b
        return LOOSE_BUDGET[-1][1]


def weighted(a, b, price, limit):
    """Sum of the prices of the cheapest letter changes that turn a (word of the name) into b (word in the
    text), or None when that is above the limit."""
    n, m = len(a), len(b)
    prev = [0.0] * (m + 1)
    for j in range(1, m + 1):
        prev[j] = prev[j - 1] + price("ins", "", b[j - 1], "^")
    for i in range(1, n + 1):
        ca, place = a[i - 1], "^" if i == 1 else "$" if i == n else ""
        gone = price("del", ca, "", place)
        cur = [prev[0] + gone] + [0.0] * m
        best = cur[0]
        for j in range(1, m + 1):
            cb = b[j - 1]
            c = min(prev[j] + gone, cur[j - 1] + price("ins", "", cb, "$" if i == n else ""),
                    prev[j - 1] + (0.0 if ca == cb else price("sub", ca, cb, place)))
            cur[j] = c
            if c < best:
                best = c
        if best > limit:
            return None
        prev = cur
    return prev[m] if prev[m] <= limit else None


# ---------------------------------------------------------------- spellings of a person's name

PARTICLES = {"van", "de", "der", "den", "te", "ter", "ten", "'t", "t", "op", "la", "le", "du", "vander"}
GROUPS = (("dirk", "durk", "derk"),
          ("arjen", "arien", "ariaen", "adriaan", "arian"),
          ("cornelis", "kornelis", "krelis", "knelis"))
_PAT_END = re.compile(r"(sdr|dr|szn|sz|sen|zn|z|x|s)$")
_ABBREV = re.compile(r"(?:[A-Za-z]\.)+[A-Za-z]?\.?|[A-Za-z]")
_VOWEL_V = re.compile(r"(?<=[aeiouy])(?:uw|v|w)(?=[aeiouy])")


def clean_name(name):
    """A name as it comes out of a family tree, without placeholders, dots and loose initials."""
    toks = []
    for t in re.sub(r"[_?/,;()\[\]\"]+", " ", name or "").split():
        if _ABBREV.fullmatch(t):
            continue
        t = t.strip(".")
        if t:
            toks.append(t)
    return " ".join(toks)


def is_patronymic(tok):
    t = tok.lower()
    return len(t) > 3 and bool(re.search(r"(s|sz|sen|z|x|dr|zn)$", t))


def split_person(name, given=None, surname=None):
    """(first name, [middle words: patronymics and second first names], surname)."""
    if given is not None:
        g, s = clean_name(given).split(), clean_name(surname or "")
        return (g[0] if g else ""), g[1:], s
    toks = clean_name(name).split()
    if not toks:
        return "", [], ""
    rest = toks[1:]
    for i, t in enumerate(rest):
        if t.lower() in PARTICLES and i + 1 < len(rest):
            return toks[0], rest[:i], " ".join(rest[i:])
    if len(rest) >= 2 and not is_patronymic(rest[-1]):
        return toks[0], rest[:-1], rest[-1]
    return toks[0], rest, ""


def _subs(w):
    """Other old spellings of one word that the folding and the reading errors do not already cover."""
    out = [w]

    def add(x):
        if x and x not in out:
            out.append(x)

    key = w.replace("ck", "k")
    for g in GROUPS:
        if key in g or key.replace("c", "k") in g:
            for x in g:
                add(x)
    for x in list(out):
        add(re.sub(r"ie(?=[^aeiouy])", "y", x))                 # Pieter: Pyter, Pytter, Pijtter
        add(x.replace("ieuw", "yuw"))                            # Lieuwes: Lyuwes, Lijuwes
        add(re.sub(r"(?:ij|y)(?=[^aeiouy])", "ie", x))          # Sybren: Siebren
        add(re.sub(r"bran(?:dt|d|t)", "bren", x))               # Sybrant: Sybren
        add(x.replace("bren", "brant"))
    for x in list(out):
        if len(x) <= 5:                                          # short words get no reading errors, so:
            for y in (x, re.sub(r"^e(?=[^aeiouy])", "ei", x), re.sub(r"^e[iy]", "e", x)):   # Eves: Eives
                add(y)
                add(_VOWEL_V.sub("w", y))                        # Eves: Ewes, Eiwes
                add(_VOWEL_V.sub("v", y))
    return out


MALE = {"dirk", "durk", "derk", "willem", "cornelis", "kornelis", "pieter", "hendrik", "gerrit", "jacob", "klaas",
        "claas", "arjen", "arien", "simon", "symon", "steven", "leendert", "albert", "evert", "geert", "harmen",
        "lieuwe", "douwe", "sjoerd", "tjeerd", "wybren", "sybren", "sybrant", "reinder", "teunis", "johan", "frans"}


def _first_forms(w, sex, avoid=()):
    out = _subs(w)
    m = re.fullmatch(r"(.{4,}?)(tje|tie|tien|tjen|tgen|je|ie)", w)
    if m and m.group(1) not in MALE and m.group(1) not in avoid:  # Trijntje: Trijn; but not Dirkje: Dirk
        for x in _subs(m.group(1)):
            if x not in out:
                out.append(x)
    elif sex == "F" and len(w) <= 6 and re.search(r"[^aeiouy]$", w):
        for x in _subs(w):                                       # Lolck: Lolckie (and Lolckje, one letter off)
            out.append(x + "ie")
            if x[-1] in "nlr":
                out.append(x + "tie")                            # Styn: Styntie
    return out


def _pat_forms(w):
    """Hayes: Hayes, Hayesen, Hayesdr (and Hayesz, which the folding covers)."""
    m = _PAT_END.search(w)
    if not m or m.start() < 2:
        return _subs(w)
    stem = w[:m.start()]
    if m.group(1) == "sen" and stem.endswith("s"):               # Janssen
        stem = stem[:-1]
    bases = []
    for x in _subs(stem + "s") + [y + "s" for y in _subs(stem)]:
        if x not in bases:
            bases.append(x)
    return bases + [b + "en" for b in bases] + [b + "dr" for b in bases]


def word_forms(word):
    """Old spellings of one word of a name, for the plain search box: Pieter also Pyter and Pytter, Hayes also
    Hayesen and Hayesdr, Trijntje also Trijn. The word itself is always in the answer."""
    w = word.lower()
    out = set(_first_forms(w, None))
    if is_patronymic(w):
        out.update(_pat_forms(w))
    out.add(w)
    return out


def spellings(name, sex=None, given=None, surname=None, key=None, limit=MAX_VARIANTS, avoid=()):
    """Search forms for one person: first name + patronymic, first name + surname, in old spellings.

    `key` maps a phrase to what the search compares (norm + fold), so only forms that differ are kept.
    `avoid` holds first names of men: Willemtje is then not also searched as Willem.
    Returns [] when there is only a first name: that is too little to search for."""
    first, mids, sur = split_person(name, given, surname)
    first, mids, sur = first.lower(), [m.lower() for m in mids], sur.lower()
    if not first or not (mids or sur):
        return []
    F = _first_forms(first, sex, avoid)
    mid_forms = [_pat_forms(m) if is_patronymic(m) else _subs(m) for m in mids]
    fams = []
    if mids:
        if len(mids) == 1 or is_patronymic(mids[0]):
            fams.append([F, mid_forms[0]])                       # the longer forms are found by this one too
        else:
            fams.append([F] + mid_forms)
        for m, forms in zip(mids[1:], mid_forms[1:]):
            if is_patronymic(m):
                fams.append([F, forms])
    if sur:
        s = sur.split()
        last = _pat_forms(s[-1]) if len(s) == 1 and is_patronymic(s[-1]) else _subs(s[-1])   # a patronymic as surname
        fams.append([F] + [[x] for x in s[:-1]] + [last])
    streams = [(" ".join(p) for p in itertools.product(*f)) for f in fams]
    out, seen = [], set()
    for phrase in itertools.chain.from_iterable(itertools.zip_longest(*streams)):
        if phrase is None:
            continue
        k = key(phrase) if key else phrase
        if k and k not in seen:
            seen.add(k)
            out.append(phrase)
            if len(out) >= limit:
                break
    return out


def people_from_text(text):
    """People from an import: a JSON list (name, uid, sex, given, surname, relatives, year) or lines of text.

    A line is a name, optionally followed by a tab or a semicolon and a reference."""
    text = (text or "").lstrip("﻿").strip()
    if text[:1] in ("[", "{"):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("people") or data.get("names") or []
        if not isinstance(data, list) or not all(isinstance(p, dict) for p in data):
            raise ValueError("The file must hold a list of people.")
        return data
    out = []
    for line in text.splitlines():
        parts = [p.strip() for p in re.split(r"[\t;]", line)]
        if parts and parts[0]:
            out.append({"name": parts[0], "uid": parts[1] if len(parts) > 1 and parts[1] else None})
    return out


# ---------------------------------------------------------------- the name finder

class NameSearch:
    def __init__(self, srv, settings_dir, cache_dir):
        self.srv = srv
        self.file = Path(settings_dir) / "names.json"
        self.cache_dir = Path(cache_dir) / "names"
        self.conf_dir = Path(cache_dir) / "conf"
        self.lock = threading.RLock()
        self.names = []
        self.lists = []
        self.results = {}       # cid -> {"v": 3, "sigsets": {hash: {name id: sig}}, "vols": {vol: {...}}}
        self.rev = 0            # goes up with every change of names or results
        self._memo = {}
        self._vocab = {}
        self.prices = Prices.load()
        self.state = {"running": False, "collection": None, "done": 0, "total": 0, "last_run": None, "error": None}
        self.wake = threading.Event()
        self.thread = None
        self.load()

    # ------------------------------------------------------------ the lists of names
    def load(self):
        with self.lock:
            data = {}
            if self.file.exists():
                data = json.loads(self.file.read_text(encoding="utf-8"))
            self.names = data.get("names", [])
            self.lists = data.get("lists", [])
            if not any(l["id"] == DEFAULT_LIST for l in self.lists):
                self.lists.insert(0, {"id": DEFAULT_LIST, "title": "My names", "on": True, "link": ""})
            known = {l["id"] for l in self.lists}
            for n in self.names:
                if n.get("list") not in known:
                    n["list"] = DEFAULT_LIST
            self.rev += 1

    def save(self):
        with self.lock:
            self.rev += 1
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"lists": self.lists, "names": self.names}, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.file)

    def _list(self, lid):
        for l in self.lists:
            if l["id"] == lid:
                return l
        raise KeyError("unknown list")

    def _clean_list(self, data, partial=False):
        out = {}
        if "title" in data or not partial:
            title = data.get("title")
            if not isinstance(title, str) or not title.strip():
                raise ValueError("Give the list a name.")
            out["title"] = title.strip()[:60]
        if "on" in data:
            out["on"] = bool(data["on"])
        if "link" in data:
            link = (data["link"] or "").strip()
            if link and not re.match(r"https?://", link):
                raise ValueError("The link must start with http:// or https://.")
            out["link"] = link[:300]
        return out

    def add_list(self, data):
        with self.lock:
            new = self._clean_list(data)
            if any(l["title"].lower() == new["title"].lower() for l in self.lists):
                raise ValueError("There already is a list with this name.")
            item = {"id": uuid.uuid4().hex[:8], "title": new["title"], "on": new.get("on", True), "link": new.get("link", "")}
            self.lists.append(item)
            self.save()
            return dict(item)

    def update_list(self, lid, data):
        with self.lock:
            item = self._list(lid)
            new = self._clean_list(data, partial=True)
            if "title" in new and any(l is not item and l["title"].lower() == new["title"].lower() for l in self.lists):
                raise ValueError("There already is a list with this name.")
            item.update(new)
            self.save()
            return dict(item)

    def remove_list(self, lid):
        with self.lock:
            item = self._list(lid)
            if lid == DEFAULT_LIST:
                raise ValueError("This list cannot be removed; you can remove the names in it.")
            n = len(self.names)
            self.names = [x for x in self.names if x.get("list") != lid]
            self.lists.remove(item)
            self.save()
        self.trigger()
        return dict(item, removed=n - len(self.names))

    def _clean(self, data, partial=False):
        out = {}
        if "label" in data or not partial:
            label = data.get("label")
            if not isinstance(label, str) or not label.strip():
                raise ValueError("Give a name.")
            out["label"] = label.strip()[:80]
        for field, cap in (("variants", MAX_VARIANTS), ("relatives", 40)):
            if field in data:
                v = data[field]
                if isinstance(v, str):
                    v = re.split(r"[\n,;]+", v)
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    raise ValueError("Spellings and relatives must be text.")
                out[field] = [x.strip()[:80] for x in v if x.strip()][:cap]
        if "mode" in data:
            if data["mode"] not in MODES:
                raise ValueError("The search mode must be fuzzy or exact.")
            out["mode"] = data["mode"]
        if "collections" in data:
            c = data["collections"]
            if not isinstance(c, list) or not all(isinstance(x, str) for x in c):
                raise ValueError("Collections must be a list.")
            known = set(self.srv.COLL.ids())
            out["collections"] = [x for x in c if x in known]
        if "list" in data:
            self._list(data["list"])
            out["list"] = data["list"]
        return out

    def _check_searchable(self, item):
        if not any(self._words(p) for p in [item["label"]] + item.get("variants", [])):
            raise ValueError("This name has no letters to search for.")

    def add(self, data):
        with self.lock:
            new = self._clean(data)
            item = {"id": uuid.uuid4().hex[:10], "label": new["label"], "variants": new.get("variants", []),
                    "mode": new.get("mode", "fuzzy"), "collections": new.get("collections", []),
                    "list": new.get("list", DEFAULT_LIST), "relatives": new.get("relatives", [])}
            self._check_searchable(item)
            self.names.append(item)
            self.save()
        self.trigger()
        return item

    def update(self, nid, data):
        with self.lock:
            item = self._get(nid)
            changed = dict(item, **self._clean(data, partial=True))
            self._check_searchable(changed)
            item.update(changed)
            self.save()
        self.trigger()
        return item

    def remove(self, nid):
        with self.lock:
            item = self._get(nid)
            self.names.remove(item)
            self.save()
        self.trigger()
        return item

    def _get(self, nid):
        for n in self.names:
            if n["id"] == nid:
                return n
        raise KeyError("unknown name")

    def _key(self, phrase):
        return tuple(self._words(phrase))

    def import_people(self, data):
        """Add many people at once; the spellings to search for are made here."""
        people = data.get("people")
        if people is None:
            people = people_from_text(data.get("text"))
        if not isinstance(people, list) or not people:
            raise ValueError("No names were found in the import.")
        if len(people) > 5000:
            raise ValueError("At most 5000 people per import.")
        base = self._clean({"label": "x", "mode": data.get("mode", "fuzzy"), "collections": data.get("collections", [])})
        for p in people:
            if not isinstance(p, dict) or not isinstance(p.get("name"), str):
                raise ValueError("Every person needs a name.")
        with self.lock:
            if data.get("list"):
                lst = self._list(data["list"])
                if data.get("link"):
                    lst.update(self._clean_list({"link": data["link"]}, partial=True))
            else:
                lst = self._list(self.add_list({"title": data.get("title"), "link": data.get("link", "")})["id"])
            by_label = {n["label"].lower(): n for n in self.names if n["list"] == lst["id"]}
            added, merged, skipped = 0, 0, []
            men = {clean_name(p.get("given") or p["name"]).split(" ")[0].lower() for p in people if p.get("sex") == "M"}
            for p in people:
                label = clean_name(p["name"])[:80]
                sex = p.get("sex") if p.get("sex") in ("F", "M") else None
                forms = spellings(p["name"], sex, p.get("given"), p.get("surname"), key=self._key, avoid=men)
                for aka in p.get("aka") or []:
                    forms += [f for f in spellings(aka, sex, key=self._key, avoid=men) if f not in forms]
                if not forms:
                    skipped.append({"name": p["name"], "why": "only a first name, or no name"})
                    continue
                rel = [clean_name(r)[:80] for r in p.get("relatives") or [] if isinstance(r, str) and len(clean_name(r).split()) > 1]
                ref = str(p["uid"])[:200] if p.get("uid") else None
                old = by_label.get(label.lower())
                if old:                                          # two people with the same name: one search
                    merged += 1
                    if ref and ref not in old.setdefault("refs", []):
                        old["refs"].append(ref)
                    old["relatives"] = (old.get("relatives", []) + [r for r in rel if r not in old.get("relatives", [])])[:40]
                    old["variants"] = (old["variants"] + [f for f in forms if f not in old["variants"]])[:MAX_VARIANTS]
                    if isinstance(p.get("year"), int):
                        old["year"] = min(old.get("year") or p["year"], p["year"])
                    continue
                item = {"id": uuid.uuid4().hex[:10], "label": label, "variants": forms[:MAX_VARIANTS], "mode": base["mode"],
                        "collections": base["collections"], "list": lst["id"], "relatives": rel[:40],
                        "refs": [ref] if ref else []}
                if isinstance(p.get("year"), int):
                    item["year"] = p["year"]
                self.names.append(item)
                by_label[label.lower()] = item
                added += 1
            self.save()
        self.trigger()
        return {"list": dict(lst), "added": added, "merged": merged, "skipped": skipped}

    def for_collection(self, cid):
        with self.lock:
            return [dict(n) for n in self.names if not n.get("collections") or cid in n["collections"]]

    def _active(self, cid, lid=None):
        """Names whose hits are shown: those of lists that are switched on (or of one list)."""
        with self.lock:
            on = {l["id"] for l in self.lists if (l["id"] == lid if lid else l.get("on", True))}
            return {n["id"]: n for n in self.names
                    if n["list"] in on and (not n.get("collections") or cid in n["collections"])}

    def public(self, cid=None, lid=None):
        counts = self._counts(cid) if cid else {}
        with self.lock:
            per = {}
            for n in self.names:
                per[n["list"]] = per.get(n["list"], 0) + 1
            lists = [dict(l, on=l.get("on", True), names=per.get(l["id"], 0)) for l in self.lists]
            names = [dict(n, hits=counts.get(n["id"], {}).get("total", 0)) for n in self.names if not lid or n["list"] == lid]
            if getattr(self.srv.COLL, "hide_private", False):       # server mode: nothing of a collection that is not shown
                shown = set(self.srv.COLL.ids())
                names = [dict(n, collections=[c for c in n["collections"] if c in shown]) if n.get("collections") else n
                         for n in names if not n.get("collections") or shown.intersection(n["collections"])]
            return {"lists": lists, "names": names, "status": dict(self.state)}

    # ------------------------------------------------------------ matching
    def _words(self, phrase):
        return [fold(w) for w in self.srv.norm(phrase).split() if w]

    @staticmethod
    def _sig(n):
        key = json.dumps([n["label"], n.get("variants", []), n.get("mode", "fuzzy"), n.get("relatives", [])], ensure_ascii=False)
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]

    def _vocab_for(self, cid, ix):
        """Per collection: word -> folded word, folded words, and tables for the fuzzy comparison."""
        hit = self._vocab.get(cid)
        if hit and hit["stamp"] == ix.stamp:
            return hit
        tokfold, folds = {}, set()
        for w in ix.vocab:
            f = fold(w)
            tokfold[w] = f
            folds.add(f)
        v = {"stamp": ix.stamp, "tokfold": tokfold, "folds": folds, "alphabet": "".join(sorted(set("".join(folds)))),
             "by_len": None, "cand": {}}
        self._vocab = {cid: v}      # one collection at a time: this is big
        return v

    def _candidates(self, v, word, mode):
        """All folded words in the collection that count as this word."""
        hit = v["cand"].get((word, mode))
        if hit is not None:
            return hit
        folds = v["folds"]
        loose = {word} if word in folds else set()
        k = max_edits(word) if mode == "fuzzy" else 0
        if k == 1:
            abc = v["alphabet"]
            for i in range(len(word) + 1):
                head, tail = word[:i], word[i:]
                for ch in abc:
                    x = head + ch + tail
                    if x in folds:
                        loose.add(x)
                if tail:
                    x = head + tail[1:]
                    if x in folds:
                        loose.add(x)
                    for ch in abc:
                        x = head + ch + tail[1:]
                        if x in folds:
                            loose.add(x)
        elif k > 1:
            # One reading error changes at most two pairs of letters next to each other. So a word can only
            # be within k errors when at most 4k pairs differ; that is checked with one number per word.
            if v["by_len"] is None:
                by_len = {}
                for f in folds:
                    if len(f) >= 7:
                        by_len.setdefault(len(f), []).append((f, _pairs_mask(f)))
                v["by_len"] = by_len
            wm, lev, limit = _pairs_mask(word), self.srv.lev_le, 4 * k
            for n in range(len(word) - k, len(word) + k + 1):
                for f, m in v["by_len"].get(n, ()):
                    if (m ^ wm).bit_count() <= limit and lev(word, f, k):
                        loose.add(f)
        v["cand"][(word, mode)] = loose
        return loose

    def _compile(self, names, v):
        """Tables for the search.

        memb: folded word in the text -> {pattern word number: spelled exactly?}
        table: first pattern word -> second pattern word (or None) -> [(target, other pattern words)]
        splits: the two halves of a pattern word, for words that Loghi read as two words
        loose: for hits with a possible reading error (None without reading_errors.json): per pattern word
               (number, word, mask, budget, letters off at most), and per first word the second words that
               may follow it ("after") and the other way round ("before")
        A target is the index of a name, or len(names) + the number of a relative (see `uses`)."""
        pids, memb, table = {}, {}, {}

        def pid_for(word, mode):
            pid = pids.get((word, mode))
            if pid is None:
                pid = pids[(word, mode)] = len(pids)
                for f in self._candidates(v, word, mode):
                    memb.setdefault(f, {})[pid] = f == word
            return pid

        def add(target, phrases, mode):
            seen = set()
            for phrase in phrases:
                words = tuple(self._words(phrase))
                if not words or words in seen:
                    continue
                seen.add(words)
                p = [pid_for(w, mode) for w in words]
                table.setdefault(p[0], {}).setdefault(p[1] if len(p) > 1 else None, []).append((target, tuple(p[2:])))

        helpers, uses = {}, []      # label of a relative -> its number; per name the numbers of its relatives
        for i, n in enumerate(names):
            add(i, [n["label"]] + n.get("variants", []), n.get("mode", "fuzzy"))
            mine = []
            for r in n.get("relatives", []):
                h = helpers.get(r.lower())
                if h is None:
                    h = helpers[r.lower()] = len(helpers)
                    add(len(names) + h, [r] + spellings(r, key=self._key, limit=30), n.get("mode", "fuzzy"))
                mine.append(h)
            uses.append(mine)
        splits = {}                 # first half -> second half -> {pattern word number: False}
        for (word, mode), pid in pids.items():
            for cut in range(2, len(word) - 1):
                a, b = word[:cut], word[cut:]
                if a in v["folds"] and b in v["folds"]:
                    splits.setdefault(a, {}).setdefault(b, {})[pid] = False
        loose = None
        if self.prices:
            info, after, before = {}, {}, {}
            for (word, mode), pid in pids.items():
                b = self.prices.budget(len(word)) if mode == "fuzzy" else 0
                if b:
                    info[pid] = (pid, word, _pairs_mask(word), b, 2 if len(word) < 9 else 3)
            for p1, d2 in table.items():
                for p2, lst in d2.items():
                    if p2 is None or not any(t < len(names) for t, _ in lst):
                        continue            # relatives are never looked for with a reading error
                    if p2 in info:
                        after.setdefault(p1, []).append(info[p2])
                    if p1 in info:
                        before.setdefault(p2, []).append(info[p1])
            loose = {"info": info, "after": after, "before": before, "ok": {}, "seen": {}}
        return memb, table, splits, uses, loose

    def _search_volume(self, cid, vol, pages, names, compiled, v):
        """({name id: [[scan, line, how the spelling fits, certainty, [numbers of the relatives on this scan]], ...]},
        {name id: real count}). How the spelling fits: EXACT, SIMILAR or LOOSE (a possible reading error)."""
        memb, table, splits, uses, lo = compiled
        tokfold, R = v["tokfold"], len(names)
        conf_of = self._conf_reader(cid, vol)
        rows = {}
        found = own = helped = None
        if lo:
            info, after, before, ok, seen = lo["info"], lo["after"], lo["before"], lo["ok"], lo["seen"]
            price, lev = self.prices.price, self.srv.lev_le

        def wrong(cands, f, strict=None):
            """Those of `cands` (words of names) that the word `f` of the text can be when Loghi made its
            typical reading errors in it. Words that `f` already is by the usual rule (`strict`) do not count."""
            n, fm, out = len(f), 0, []
            for pid, word, wm, budget, k in cands:
                if abs(len(word) - n) > k or (strict and pid in strict):
                    continue
                fm = fm or _pairs_mask(f)
                if (fm ^ wm).bit_count() > 4 * k:
                    continue
                r = ok.get((pid, f))
                if r is None:
                    if len(ok) > 300000:
                        ok.clear()
                    r = ok[pid, f] = lev(word, f, k) and weighted(word, f, price, budget) is not None
                if r:
                    out.append(pid)
            return out

        def errors(key, cands, f, m):
            """The same as wrong(), remembered per word of the text. A word of the text that is exactly a
            word of some name (`m` says so) is not taken for a reading error of another word."""
            c = seen.get((key, f))
            if c is None:
                if len(seen) > 500000:
                    seen.clear()
                c = seen[key, f] = () if m and True in m.values() else wrong(cands, f, m)
            return c

        def rest(tail, q, ext, tok):
            """The further words of a name, from word q of `ext` on. None when they do not follow, otherwise
            (all spelled exactly?, one of them only as a reading error?). `tok` (the words themselves) is
            given when one reading error is allowed."""
            ex, err = True, False
            for q, pid in enumerate(tail, q):
                mq = ext[q] if q < len(ext) else None
                if mq and pid in mq:
                    ex = ex and mq[pid]
                elif tok and not err and q < len(tok) and tok[q] and pid in info and wrong((info[pid],), tok[q]):
                    err = True
                else:
                    return None
            return ex, err

        def note(i, p, target, how):                             # how: 2 exact, 1 similar, 0 a reading error
            if target < R:
                if how > found.get((i, target), -1):
                    found[(i, target)] = how
                own.setdefault(target, set()).add((i, p))
            else:
                helped.setdefault(target - R, set()).add((i, p))

        def scan_line(i, ext, L, tok=None):
            """Look for names that start in the first L words of `ext` (line i plus the start of the next line).
            With `tok` (the words themselves) one word of a name may be a reading error; the others must be
            there by the usual rule."""
            for p in range(L):
                m1 = ext[p]
                m2 = ext[p + 1] if p + 1 < len(ext) else None
                if m1:
                    t2 = tok[p + 1] if tok and p + 1 < len(tok) else None
                    for pid1, ex1 in m1.items():
                        d2 = table.get(pid1)
                        if not d2:
                            continue
                        for target, _ in d2.get(None, ()):
                            note(i, p, target, 2 if ex1 else 1)
                        if m2:
                            for pid2, ex2 in m2.items():
                                for target, tail in d2.get(pid2, ()):
                                    if not tail:
                                        note(i, p, target, 2 if ex1 and ex2 else 1)
                                        continue
                                    r = rest(tail, p + 2, ext, tok if target < R else None)
                                    if r:
                                        note(i, p, target, 0 if r[1] else 2 if ex1 and ex2 and r[0] else 1)
                        if t2 and pid1 in after:                 # the second word as a reading error
                            for pid2 in errors(pid1, after[pid1], t2, m2):
                                for target, tail in d2[pid2]:
                                    if target < R and (not tail or rest(tail, p + 2, ext, None)):
                                        note(i, p, target, 0)
                if tok and m2 and tok[p]:                        # the first word as a reading error
                    for pid2 in m2:
                        firsts = before.get(pid2)
                        if firsts:
                            for pid1 in errors(-1 - pid2, firsts, tok[p], m1):
                                for target, tail in table[pid1].get(pid2, ()):
                                    if target < R and (not tail or rest(tail, p + 2, ext, None)):
                                        note(i, p, target, 0)

        for scan, raw, nl in pages:
            found, own, helped = {}, {}, {}     # (line, name) -> how; name -> its positions; relative -> positions
            folds = [[tokfold.get(w) for w in x.split()] for x in nl]
            membs = [[memb.get(f) for f in fl] for fl in folds]
            for i, fl in enumerate(folds):
                if not fl:
                    continue
                j = i + 1
                while j < len(folds) and not folds[j]:
                    j += 1
                ext, L = (membs[i] + membs[j][:8], len(fl)) if j < len(folds) else (membs[i], len(fl))
                extf = (fl + folds[j][:8] if j < len(folds) else fl) if lo or splits else None
                if any(ext):
                    scan_line(i, ext, L, extf if lo else None)
                if splits:                                       # a word that Loghi read as two words: Sij brensz
                    alt, k, L2 = [], 0, L
                    while k < len(extf):
                        d = splits.get(extf[k]) if k + 1 < len(extf) else None
                        m = d.get(extf[k + 1]) if d else None
                        if m:
                            alt.append(m)
                            L2 -= k + 1 < L
                            k += 2
                        else:
                            alt.append(ext[k])
                            k += 1
                    if len(alt) < len(extf):
                        scan_line(i, alt, L2)
            if not found:
                continue
            for (i, t), how in sorted(found.items()):
                sup = [k for k, h in enumerate(uses[t]) if helped.get(h, set()) - own[t]] if helped else []
                rows.setdefault(names[t]["id"], []).append([scan, i + 1, (LOOSE, SIMILAR, EXACT)[how], None, sup])

        def top(lst):                                            # at most MAX_ROWS, those with a relative first
            keep = [r for r in lst if r[4]][:MAX_ROWS]
            return keep + [r for r in lst if not r[4]][:MAX_ROWS - len(keep)]

        cut = {}
        for nid, lst in rows.items():
            usual, loose = [r for r in lst if r[2] != LOOSE], [r for r in lst if r[2] == LOOSE]
            if len(usual) > MAX_ROWS or len(loose) > MAX_ROWS:
                if len(usual) > MAX_ROWS:
                    cut[nid] = len(usual)
                rows[nid] = sorted(top(usual) + top(loose), key=lambda r: (r[0], r[1]))
        conf = conf_of({r[0] for lst in rows.values() for r in lst})
        for lst in rows.values():
            for r in lst:
                r[3] = conf[r[0]].get(r[1])
        return rows, cut

    def _conf_reader(self, cid, vol):
        """Certainty per line of a scan, kept in cache/conf so a PageXML file is read only once.

        Call the result with a set of scans for {scan: {line number: certainty}}."""
        f = self.conf_dir / cid / f"{vol}.json"
        try:
            page = self.srv.vol_path(cid, vol) / "page"
            key = [page.stat().st_mtime, sum(1 for e in os.scandir(page) if e.name.endswith(".xml"))]
        except (OSError, KeyError, ValueError):
            return lambda scans: {s: {} for s in scans}
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("key") != key:
                raise ValueError
        except (OSError, ValueError):
            data = {"key": key, "scans": {}}
        known = data["scans"]

        def one(scan):
            try:
                return [[l["n"], l["conf"]] for l in self.srv.read_page(cid, vol, scan)["lines"]]
            except Exception:
                return []

        def read(scans):
            new = sorted(s for s in scans if s not in known)
            if new:
                with ThreadPoolExecutor(8) as pool:      # opening the files is the slow part, so several at once
                    known.update(zip(new, pool.map(one, new)))
                f.parent.mkdir(parents=True, exist_ok=True)
                tmp = f.with_suffix(".tmp")
                tmp.write_text(json.dumps(data), encoding="utf-8")
                os.replace(tmp, f)
            return {s: dict(known[s]) for s in scans}
        return read

    # ------------------------------------------------------------ results on disk
    def _result_file(self, cid):
        return self.cache_dir / f"{cid}.json"

    def _load_results(self, cid):
        with self.lock:
            if cid not in self.results:
                try:
                    res = json.loads(self._result_file(cid).read_text(encoding="utf-8"))
                    if res.get("v") != 3:
                        raise ValueError
                except (OSError, ValueError, AttributeError):
                    res = {"v": 3, "sigsets": {}, "vols": {}}
                self.results[cid] = res
            return self.results[cid]

    def _save_results(self, cid):
        with self.lock:
            res = self.results[cid]
            used = {e["sh"] for e in res["vols"].values()}
            res["sigsets"] = {k: s for k, s in res["sigsets"].items() if k in used}
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._result_file(cid).with_suffix(".tmp")
            tmp.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self._result_file(cid))

    # ------------------------------------------------------------ reading the hits
    def _strong(self, r):
        return r[2] == EXACT or (r[2] == SIMILAR and (r[3] is None or r[3] >= self.srv.CONF_UNSURE))

    def _counts(self, cid):
        """Per name in this collection: total, strong and supported hits, and whether the name is very common.
        Hits with a possible reading error are counted apart (loose) and are in none of the other numbers."""
        key = ("counts", cid)
        with self.lock:
            hit = self._memo.get(key)
            if hit and hit[0] == self.rev:
                return hit[1]
            res = self._load_results(cid)
            out = {}
            for e in res["vols"].values():
                for nid, lst in e["rows"].items():
                    c = out.setdefault(nid, {"total": 0, "strong": 0, "supported": 0, "cut": 0, "loose": 0})
                    n = sum(1 for r in lst if r[2] != LOOSE)
                    c["total"] += n
                    c["loose"] += len(lst) - n
                    c["strong"] += sum(1 for r in lst if self._strong(r))
                    c["supported"] += sum(1 for r in lst if r[4] and r[2] != LOOSE)
                    c["cut"] += e.get("cut", {}).get(nid, n) - n
            for c in out.values():
                c["common"] = c["strong"] > COMMON
            self._memo[key] = (self.rev, out)
            return out

    def _keep(self, r, strength, limited):
        if strength in ("loose", "loose_supported"):             # only the hits with a possible reading error
            return r[2] == LOOSE and (strength == "loose" or bool(r[4]))
        if r[2] == LOOSE:
            return False
        if strength == "all":
            return True
        if strength == "supported":
            return bool(r[4])
        if not self._strong(r):
            return False
        return strength == "strong" or bool(r[4]) or not limited

    def _selected(self, cid, strength="best", lid=None, vol=None, nid=None):
        """[(name, counts, vol, row)] for the hits that pass the filters, in the order of the books."""
        if strength not in STRENGTHS:
            raise ValueError("unknown strength")
        self.srv.COLL.get(cid)
        active, counts = self._active(cid, lid), self._counts(cid)
        res = self._load_results(cid)
        out = []
        with self.lock:
            for v in ([vol] if vol else sorted(res["vols"])):
                e = res["vols"].get(v)
                if not e:
                    continue
                for i, lst in ([(nid, e["rows"].get(nid, []))] if nid else e["rows"].items()):
                    n = active.get(i)
                    if not n or not lst:
                        continue
                    limited = counts[i]["common"] and bool(n.get("relatives"))
                    out += [(n, counts[i], v, r) for r in lst if self._keep(r, strength, limited)]
        return out

    def _links(self, n):
        with self.lock:
            link = next((l.get("link") for l in self.lists if l["id"] == n["list"]), "") or ""
        return [link.replace("{uid}", r) if link else None for r in n.get("refs", [])]

    def summary(self, cid, strength="best", lid=None, vol=None, q="", sort="name", offset=0, limit=50):
        """The people who have hits, with numbers; the hits themselves come from rows()."""
        key = ("summary", cid, strength, lid, vol)
        with self.lock:
            hit = self._memo.get(key)
            if hit and hit[0] == self.rev:
                people, hidden = hit[1]
            else:
                per = {}
                for n, c, v, r in self._selected(cid, strength, lid, vol):
                    p = per.get(n["id"])
                    if p is None:
                        p = per[n["id"]] = {"id": n["id"], "label": n["label"], "list": n["list"], "refs": n.get("refs", []),
                                             "links": self._links(n), "year": n.get("year"), "shown": 0, "supported": 0,
                                             "books": set(), "total": c["total"], "strong": c["strong"],
                                             "loose": c["loose"], "common": c["common"], "cut": c["cut"],
                                             "limited": strength == "best" and c["common"] and bool(n.get("relatives"))}
                    p["shown"] += 1
                    p["supported"] += bool(r[4])
                    p["books"].add(v)
                people = [dict(p, books=len(p["books"])) for p in per.values()]
                active, counts = self._active(cid, lid), self._counts(cid)
                hidden = sum(1 for i in active if i not in per and counts.get(i, {}).get("total"))
                if len(self._memo) > 40:
                    self._memo.clear()
                self._memo[key] = (self.rev, (people, hidden))
            active_n = len(self._active(cid, lid))
        ql = q.strip().lower()
        sel = [p for p in people if ql in p["label"].lower()] if ql else list(people)
        sel.sort(key=lambda p: p["label"].lower())
        if sort == "hits":
            sel.sort(key=lambda p: -p["shown"])
        elif sort == "supported":
            sel.sort(key=lambda p: (-p["supported"], -p["shown"]))
        return {"people": sel[offset:offset + limit], "count": len(sel), "hits": sum(p["shown"] for p in sel),
                "names": active_n, "without": active_n - len(people) - hidden, "filtered_out": hidden,
                "cut": sum(1 for p in people if p["cut"]), "offset": offset, "limit": limit}

    def _expand(self, n, vol, r, pages):
        lines = pages(vol).get(r[0], [])
        i = r[1] - 1
        text = lines[i] if i < len(lines) else ""
        before = next((lines[k] for k in range(min(i, len(lines)) - 1, -1, -1) if lines[k].strip()), "")
        after = next((lines[k] for k in range(i + 1, len(lines)) if lines[k].strip()), "")
        rel = n.get("relatives", [])
        return {"vol": vol, "scan": r[0], "n": r[1], "id": n["id"], "label": n["label"],
                "kind": "exact" if r[2] == EXACT else "loose" if r[2] == LOOSE else "similar",
                "conf": r[3], "sure": self.srv.conf_label(r[3]),
                "strong": self._strong(r), "relatives": [rel[k] for k in r[4] if k < len(rel)],
                "text": text, "before": before, "after": after}

    def _pages(self, cid):
        ix = self.srv.index_for(cid)
        ix.refresh()
        memo = {}

        def pages(vol):
            if vol not in memo:
                memo.clear()
                memo[vol] = {scan: raw for scan, raw, _ in ix.vols.get(vol, (0, 0, []))[2]}
            return memo[vol]
        return pages

    def rows(self, cid, nid=None, strength="best", lid=None, vol=None, scan=None, offset=0, limit=100):
        """Hits with their text, for one person or one book."""
        pages = self._pages(cid)
        sel = [x for x in self._selected(cid, strength, lid, vol, nid) if not scan or x[3][0] == scan]
        return {"count": len(sel), "offset": offset,
                "rows": [self._expand(n, v, r, pages) for n, c, v, r in sel[offset:offset + limit]]}

    def marks(self, cid, vol):
        """Short hits of one book, for the tags in the View tab."""
        return [{"scan": r[0], "n": r[1], "id": n["id"], "label": n["label"]} for n, c, v, r in self._selected(cid, "all", None, vol)]

    def hits(self, cid):
        """All hits of a collection with their text (for the tests and for small collections)."""
        return self.rows(cid, strength="all", limit=10 ** 9)["rows"]

    def export_csv(self, cid, strength="best", lid=None, vol=None):
        """All hits that pass the filters as CSV text, one hit per line."""
        pages = self._pages(cid)
        with self.lock:
            titles = {l["id"]: l["title"] for l in self.lists}
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["person", "uid", "link", "list", "book", "scan", "line", "strength", "spelling", "certainty",
                    "certainty_word", "relatives_on_scan", "common_name", "hits_of_person", "line_before", "text", "line_after"])
        rows = sorted(self._selected(cid, strength, lid, vol), key=lambda x: (x[2], x[0]["label"].lower(), x[3][0], x[3][1]))
        for n, c, v, r in rows:
            x = self._expand(n, v, r, pages)
            w.writerow([n["label"], " | ".join(n.get("refs", [])), " | ".join(l for l in self._links(n) if l),
                        titles.get(n["list"], ""), v, r[0], r[1], "strong" if x["strong"] else "weak", x["kind"],
                        "" if r[3] is None else f"{r[3]:.2f}", x["sure"] or "", " | ".join(x["relatives"]),
                        "yes" if c["common"] else "no", c["total"], x["before"], x["text"], x["after"]])
        return buf.getvalue()

    # ------------------------------------------------------------ the background search
    def run(self, cid):
        """Search one collection. Books that were searched with the same names are skipped; after a change of
        the lists only the changed names are searched again."""
        ix = self.srv.index_for(cid)
        ix.refresh()
        names = self.for_collection(cid)
        stamp = self.prices.stamp if self.prices else ""        # other reading errors: search again
        cur = {n["id"]: self._sig(n) + stamp for n in names}
        sh = hashlib.sha1(json.dumps(sorted(cur.items())).encode()).hexdigest()[:12]
        res = self._load_results(cid)
        vols = dict(ix.vols)
        with self.lock:
            for gone in set(res["vols"]) - set(vols):
                del res["vols"][gone]
                self.rev += 1
            todo = [v for v in sorted(vols)
                    if res["vols"].get(v, {}).get("key") != [vols[v][0], vols[v][1]] or res["vols"][v]["sh"] != sh]
            self.state.update(done=len(vols) - len(todo), total=len(vols))
            if todo:
                res["sigsets"][sh] = cur
        if not todo:
            return 0
        voc = self._vocab_for(cid, ix)
        compiled, last_save = {}, time.time()
        for v in todo:
            if self.wake.is_set():  # the lists changed while searching: start again
                break
            mtime, count, pages = vols[v]
            old = res["vols"].get(v)
            base = res["sigsets"].get(old["sh"]) if old and old.get("key") == [mtime, count] else None
            if base is None:
                need, keep_rows, keep_cut = names, {}, {}
            else:
                need = [n for n in names if base.get(n["id"]) != cur[n["id"]]]
                fresh = {n["id"] for n in need}
                keep_rows = {i: r for i, r in old["rows"].items() if i in cur and i not in fresh}
                keep_cut = {i: c for i, c in old.get("cut", {}).items() if i in keep_rows}
            ck = tuple(n["id"] for n in need)
            if need and ck not in compiled:
                compiled.clear()
                compiled[ck] = self._compile(need, voc)
            rows, cut = self._search_volume(cid, v, pages, need, compiled[ck], voc) if need else ({}, {})
            with self.lock:
                res["vols"][v] = {"key": [mtime, count], "sh": sh, "rows": dict(keep_rows, **rows), "cut": dict(keep_cut, **cut)}
                self.state["done"] += 1
                self.rev += 1
            if time.time() - last_save > 30:
                self._save_results(cid)
                last_save = time.time()
        self._save_results(cid)
        return len(todo)

    def run_all(self):
        for c in self.srv.COLL.public():
            if not c["exists"]:
                continue
            with self.lock:
                self.state.update(running=True, collection=c["title"], done=0, total=0, error=None)
            try:
                self.run(c["id"])
            except Exception as e:  # keep the thread alive
                with self.lock:
                    self.state["error"] = f"{c['title']}: {e}"
            if self.wake.is_set():
                return
        self._vocab = {}
        with self.lock:
            self.state.update(running=False, collection=None, last_run=time.time())

    def trigger(self):
        self.wake.set()

    def _loop(self, interval):
        while True:
            self.wake.wait(interval)
            self.wake.clear()
            self.run_all()
            with self.lock:
                if not self.wake.is_set():
                    self.state["running"] = False

    def start(self, interval=600):
        """Search now, after every change of the lists, and every `interval` seconds (for newly read books)."""
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, args=(interval,), daemon=True)
            self.thread.start()
        self.trigger()
