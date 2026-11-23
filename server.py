"""Loghi-studio: local web application around Loghi handwriting recognition.

Usage:  python server.py [port]      (default 8771)
Then open http://127.0.0.1:8771/

Server mode (reachable from outside, behind a web server that does https): set LOGHI_MODE=server and
LOGHI_HOSTS=<host name(s), comma separated>. The site must be at the root of its host name. Every request
then needs a login (auth.py); nothing is read, added or removed on the disk: only looking, searching, for
the admin the lists of names, and for the admin and correctors checking lines. Collections marked as
scans for own use only do not exist in server mode, and searching with a regular expression is off there.

Only the Python standard library is used (reading a book enlarged also needs Pillow). Collections live in
settings/collections.json (created on first start, editable in the interface).
A collection is a folder with one sub-folder per volume (book/film); each volume folder
holds the scans (*.jpg), page/*.xml (PageXML from Loghi) and txt/*.txt (plain text).

Existing files in the scan folders are never changed, moved or deleted by this server;
adding a book (books.py) only creates a new book folder, and reading a book (readqueue.py) only adds
page/*.xml and txt/*.txt. Lines checked by a person (corrections.py) are kept in the settings folder.
"""
import json
import os
import re
import string
import sys
import threading
import time
import traceback
import unicodedata
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

VERSION = "0.6.1-beta"
HERE = Path(__file__).resolve().parent
PROJECTS = HERE.parent
SETTINGS_DIR = Path(os.environ.get("LOGHI_STUDIO_SETTINGS", HERE / "settings"))
CACHE_DIR = Path(os.environ.get("LOGHI_STUDIO_CACHE", HERE / "cache"))
COLLECTIONS_FILE = SETTINGS_DIR / "collections.json"
TRAINING_DIR = Path(os.environ.get("LOGHI_STUDIO_TRAINING", HERE / "training"))

IMG_EXT = (".jpg", ".jpeg")
SKIP_DIRS = {"hits", "page", "txt", "tmp", "img", "_pages"}      # folders in a collection that are not books; so is every "hits_..."
NAME_RE = r"[\w.\-]+"

# Thresholds for the certainty Loghi gives per line (TextEquiv conf=, 0..1).
CONF_VERY_UNSURE = 0.35
CONF_UNSURE = 0.55


# ---------------------------------------------------------------- collections (settings file)

class Collections:
    """Thread-safe view of settings/collections.json."""

    def __init__(self, path=COLLECTIONS_FILE):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.items = []
        self.hide_private = False   # server mode: collections with scans for own use only do not exist there
        self.load()

    def _shown(self):
        return [c for c in self.items if not (self.hide_private and c.get("private"))]

    def load(self):
        with self.lock:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.items = data.get("collections", [])
            else:
                self.items = []     # a first start: the user adds collections in the tab Collections
                self.save()
            for c in self.items:        # settings from before this field: FamilySearch scans are for own use only
                c.setdefault("private", "familysearch" in str(c.get("title", "")).lower())

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"collections": self.items}, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    def get(self, cid):
        with self.lock:
            for c in self._shown():
                if c["id"] == cid:
                    return c
        raise KeyError(f"unknown collection: {cid}")

    def ids(self):
        with self.lock:
            return [c["id"] for c in self._shown()]

    def root(self, cid):
        return Path(self.get(cid)["root"])

    def public(self):
        with self.lock:
            return [dict(c, exists=Path(c["root"]).is_dir()) for c in self._shown()]

    @staticmethod
    def _clean(data, partial=False):
        out = {}
        for key in ("title", "description", "years", "root", "log", "index"):
            if key in data:
                val = data[key]
                if not isinstance(val, str):
                    raise ValueError(f"field {key} must be text")
                out[key] = val.strip()
        if "hits" in data:
            if not isinstance(data["hits"], list) or not all(isinstance(h, str) for h in data["hits"]):
                raise ValueError("field hits must be a list of folders")
            out["hits"] = [h.strip() for h in data["hits"] if h.strip()]
        if out.get("index") and not (Path(out["index"]) if Path(out["index"]).is_absolute() else HERE / out["index"]).is_file():
            raise ValueError(f"This file does not exist: {out['index']}")
        if "private" in data:           # scans for own use only: never in a training set to share
            out["private"] = bool(data["private"])
        if not partial or "title" in out:
            if not out.get("title"):
                raise ValueError("Give the collection a name.")
        if not partial or "root" in out:
            if not out.get("root"):
                raise ValueError("Give the folder that holds the books.")
            if not Path(out["root"]).is_dir():
                raise ValueError(f"This folder does not exist: {out['root']}")
            out["root"] = str(Path(out["root"]))
        return out

    def add(self, data):
        with self.lock:
            new = self._clean(data)
            base = slug(new["title"]) or "collection"
            cid, n = base, 2
            while cid in self.ids():
                cid, n = f"{base}-{n}", n + 1
            root = Path(new["root"])
            item = {"id": cid, "title": new["title"], "description": new.get("description", ""),
                    "years": new.get("years", ""), "root": new["root"],
                    "log": new.get("log") or str(root / "batch.log"),
                    "hits": new.get("hits") or [str(root)]}
            self.items.append(item)
            self.save()
            return item

    def update(self, cid, data):
        with self.lock:
            item = self.get(cid)
            item.update(self._clean(data, partial=True))
            self.save()
            _vol_cache.pop(cid, None)
            _indexes.pop(cid, None)
            return item

    def remove(self, cid):
        """Remove from the list only; the folder with scans and texts is left untouched."""
        with self.lock:
            item = self.get(cid)
            self.items.remove(item)
            self.save()
            _vol_cache.pop(cid, None)
            _indexes.pop(cid, None)
            return item


def slug(s):
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40]


COLL = None  # set in main() / tests through init()
NAMES = None
BOOKS = None
QUEUE = None
CORR = None


def init(path=None, engine=None):
    global COLL, NAMES, BOOKS, QUEUE, CORR
    COLL = Collections(path or COLLECTIONS_FILE)
    import names
    NAMES = names.NameSearch(sys.modules[__name__], Path(path).parent if path else SETTINGS_DIR, CACHE_DIR)
    import books
    BOOKS = books.Jobs(sys.modules[__name__], Path(path).parent if path else SETTINGS_DIR)
    import readqueue
    QUEUE = readqueue.Queue(sys.modules[__name__], Path(path).parent if path else SETTINGS_DIR, CACHE_DIR, engine)
    import corrections
    CORR = corrections.Corrections(sys.modules[__name__], Path(path).parent if path else SETTINGS_DIR,
                                   Path(path).parent.parent / "training" if path else TRAINING_DIR)
    _vol_cache.clear()
    _indexes.clear()
    _hits_cache.clear()
    return COLL


# ---------------------------------------------------------------- folder browser (to pick a folder)

def browse(path):
    """Sub-folders of a folder, so a folder can be chosen with the keyboard. Read-only."""
    if not path:
        drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.isdir(f"{d}:\\")] if os.name == "nt" else ["/"]
        return {"path": "", "parent": None, "folders": [{"name": d, "path": d} for d in drives], "images": 0}
    p = Path(path)
    if not p.is_dir():
        raise ValueError(f"This folder does not exist: {path}")
    folders, images = [], 0
    try:
        for e in os.scandir(p):
            if e.is_dir() and not e.name.startswith((".", "$")):
                folders.append({"name": e.name, "path": e.path})
            elif e.is_file() and e.name.lower().endswith(IMG_EXT):
                images += 1
    except OSError as err:
        raise ValueError(f"This folder cannot be read: {err}")
    folders.sort(key=lambda f: f["name"].lower())
    parent = "" if p.parent == p else str(p.parent)
    return {"path": str(p), "parent": parent, "folders": folders, "images": images}


# ---------------------------------------------------------------- volumes

def scan_volume(path: Path):
    imgs, txt = [], 0
    try:
        for e in os.scandir(path):
            if e.is_file() and e.name.lower().endswith(IMG_EXT):
                imgs.append(e.name)
    except OSError:
        return None
    tdir = path / "txt"
    if tdir.is_dir():
        txt = sum(1 for e in os.scandir(tdir) if e.name.endswith(".txt"))
    if not imgs and not txt:
        return None
    imgs.sort()
    return {
        "name": path.name,
        "scans": len(imgs),
        "txt": txt,
        "done": (tdir / ".done").exists(),
        "downloaded": (path / ".dl_done").exists(),
        "first": imgs[0] if imgs else None,
    }


_vol_cache = {}


def list_volumes(cid):
    root = COLL.root(cid)
    hit = _vol_cache.get(cid)
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    vols = []
    if root.is_dir():
        for e in sorted(os.scandir(root), key=lambda e: e.name):
            if e.is_dir() and e.name not in SKIP_DIRS and not e.name.startswith("hits_") and not e.name.startswith("."):
                v = scan_volume(Path(e.path))
                if v:
                    vols.append(v)
    running = running_volume(cid, [v["name"] for v in vols])
    for v in vols:
        q = QUEUE.is_open(cid, v["name"]) if QUEUE else None
        v["queued"] = q == "waiting"
        v["running"] = v["name"] == running or q == "running"
        v["state"] = "running" if v["running"] else "done" if v["done"] else "partial" if v["txt"] else "to do"
    _vol_cache[cid] = (time.time(), vols)
    return vols


def list_scans(cid, vol):
    p = vol_path(cid, vol)
    names = sorted(e.name for e in os.scandir(p) if e.is_file() and e.name.lower().endswith(IMG_EXT))
    tdir = p / "txt"
    have = set()
    if tdir.is_dir():
        have = {e.name[:-4] for e in os.scandir(tdir) if e.name.endswith(".txt")}
    return [{"file": n, "id": n.rsplit(".", 1)[0], "txt": n.rsplit(".", 1)[0] in have} for n in names]


def vol_path(cid, vol):
    if not re.fullmatch(NAME_RE, vol or "") or vol in (".", ".."):
        raise KeyError("invalid book name")
    p = COLL.root(cid) / vol
    if not p.is_dir():
        raise KeyError("this book does not exist")
    return p


# ---------------------------------------------------------------- batch log / status

# "20-11 13:38 weesboek_I2 start (282 scans)", "16-11 01:24 inv 0090 done, 370 txt",
# "12:21 inv 85 start (369 scans)", "14:16 inv 85 done, 134 hits"
LOG_RE = re.compile(r"^(?:(\d\d-\d\d) )?(\d\d:\d\d) (?:inv )?(\S+) (?:start \((\d+) scans\)|done, (\d+) (?:txt|hits))")


def match_volume(name, vols):
    """Log names are sometimes shorter than folder names ("85" for folder "0085")."""
    if name in vols:
        return name
    cand = [v for v in vols if v.endswith(name) or name.endswith(v)]
    return cand[0] if cand else name


def parse_log(cid):
    """Finished runs (vol, scans, seconds), the running volume (if any) and whether the batch is finished."""
    log = COLL.get(cid).get("log")
    runs, open_ = [], {}
    if not log or not Path(log).exists():
        return runs, None, None
    text = Path(log).read_text(encoding="utf-8", errors="replace")
    year = time.localtime().tm_year
    day_offset, last_minutes = 0, None
    for line in text.splitlines():
        m = LOG_RE.match(line)
        if not m:
            continue
        date, hm, vol, n_start = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            if date:
                t = time.mktime(time.strptime(f"{year}-{date} {hm}", "%Y-%d-%m %H:%M"))
            else:  # log without dates: count midnights
                minutes = int(hm[:2]) * 60 + int(hm[3:])
                if last_minutes is not None and minutes < last_minutes:
                    day_offset += 1
                last_minutes = minutes
                t = (day_offset * 1440 + minutes) * 60.0
        except ValueError:
            continue
        if n_start:
            open_[vol] = (t, int(n_start), bool(date))
        elif vol in open_:
            t0, n, _ = open_.pop(vol)
            runs.append({"vol": vol, "scans": n, "secs": t - t0})
    all_done = "ALL DONE" in text[-2000:]
    running = None
    if open_ and not all_done:
        vol, (t0, n, dated) = max(open_.items(), key=lambda kv: kv[1][0])
        running = {"vol": vol, "scans": n, "since": t0 if dated else None}
    return runs, running, all_done


def running_volume(cid, vols):
    _, running, _ = parse_log(cid)
    return match_volume(running["vol"], vols) if running else None


def status(cid):
    vols = list_volumes(cid)
    runs, running, all_done = parse_log(cid)
    scans = sum(v["scans"] for v in vols)
    done_scans = sum(v["scans"] for v in vols if v["done"])
    recent = [r for r in runs if r["scans"] >= 20 and r["secs"] > 0][-15:]
    rate = None
    if recent:
        rate = sum(r["scans"] for r in recent) / max(1, sum(r["secs"] for r in recent)) * 60
    left = scans - done_scans
    stage = None
    if running:
        running["vol"] = match_volume(running["vol"], [v["name"] for v in vols])
        root = COLL.root(cid)
        for p in (root / f"loghi_{running['vol']}.log", root.parent / f"loghi_{running['vol']}.log"):
            if p.exists():
                st = re.findall(r"^=== (\d/6 [^(]*)", p.read_text(encoding="utf-8", errors="replace"), re.M)
                stage = st[-1].strip() if st else None
                break
    return {
        "volumes": len(vols),
        "volumes_done": sum(1 for v in vols if v["done"]),
        "scans": scans,
        "scans_done": done_scans,
        "rate_per_min": rate,
        "eta_minutes": (left / rate) if rate and left and running else None,
        "running": running,
        "stage": stage,
        "all_done": all_done,
    }


# ---------------------------------------------------------------- PageXML

def conf_label(conf):
    """Certainty as a word, so it never depends on colour alone."""
    if conf is None:
        return None
    if conf < CONF_VERY_UNSURE:
        return "very unsure"
    if conf < CONF_UNSURE:
        return "unsure"
    return "sure"


def read_page(cid, vol, scan_id, raw=False):
    """Lines in the same order as page2txt.py, each with its 1-based line number in the .txt file.

    A line that a person checked (corrections.py) carries the checked text; "orig" is then what Loghi read
    and "checked" says what was done. With raw=True the lines are given as Loghi read them."""
    p = vol_path(cid, vol)
    xml = p / "page" / f"{scan_id}.xml"
    result = {"lines": [], "width": None, "height": None}
    if not xml.exists():
        txt = p / "txt" / f"{scan_id}.txt"
        if txt.exists():
            for i, t in enumerate(txt.read_text(encoding="utf-8").splitlines()):
                if t.strip():
                    result["lines"].append({"n": i + 1, "text": t, "conf": None, "sure": None, "poly": None})
        return result
    root = ET.parse(xml).getroot()
    ns = root.tag.split("}")[0] + "}"
    page = root.find(ns + "Page")
    if page is not None:
        result["width"] = int(page.get("imageWidth", 0)) or None
        result["height"] = int(page.get("imageHeight", 0)) or None
    out = []  # mirrors page2txt: text lines, None after every region
    for region in root.iter(ns + "TextRegion"):
        for line in region.findall(ns + "TextLine"):
            eqs = line.findall(ns + "TextEquiv")
            u = eqs[-1].find(ns + "Unicode") if eqs else None
            if u is not None and u.text:
                coords = line.find(ns + "Coords")
                base = line.find(ns + "Baseline")
                conf = eqs[-1].get("conf")
                conf = float(conf) if conf else None
                out.append({
                    "id": line.get("id"),
                    "text": u.text,
                    "conf": conf,
                    "sure": conf_label(conf),
                    "poly": coords.get("points") if coords is not None else None,
                    "base": base.get("points") if base is not None else None,
                })
        out.append(None)
    lead = 0
    while lead < len(out) and out[lead] is None:
        lead += 1
    for i, item in enumerate(out):
        if item is not None:
            item["n"] = i - lead + 1
            result["lines"].append(item)
    if not raw and CORR is not None:
        CORR.apply(cid, vol, scan_id, result["lines"])
    return result


# ---------------------------------------------------------------- hits from the name searches

_hits_cache = {}


def load_hits(cid):
    dirs = [Path(d) for d in COLL.get(cid).get("hits") or []]
    files = []
    for d in dirs:
        if d.is_dir():
            files += sorted(d.glob("hits*_*.md"))
    key = tuple((str(f), f.stat().st_mtime) for f in files)
    hit = _hits_cache.get(cid)
    if hit and hit[0] == key:
        return hit[1]
    vols = {v["name"] for v in list_volumes(cid)}
    rows, seen = [], set()
    for f in files:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith("| ") or line.startswith("| boek") or line.startswith("|---"):
                continue
            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cols) < 5 or not cols[2].isdigit():
                continue
            book = match_volume(cols[0], vols)
            k = (book, cols[1], cols[2], cols[3])
            if k in seen:
                continue
            seen.add(k)
            rows.append({"vol": book, "scan": cols[1], "n": int(cols[2]),
                         "label": cols[3], "ctx": " | ".join(cols[4:])})
    _hits_cache[cid] = (key, rows)
    return rows


# ---------------------------------------------------------------- full-text search

_NORM_PAIRS = (("ij", "y"), ("ck", "k"), ("gh", "g"), ("th", "t"), ("sz", "s"), ("ae", "aa"))
_PUNCT = re.compile(r"[^\w\s]")
_DOUBLE = re.compile(r"(\w)\1+")


def norm(s):
    """Loose spelling: lower case, no accents, ij=y, ck=k, gh=g, th=t, sz=s, ae=aa, doubled letters single."""
    s = s.lower()
    if not s.isascii():
        s = s.replace("ÿ", "y")
        s = unicodedata.normalize("NFKD", s)
        s = "".join(ch for ch in s if not unicodedata.combining(ch))
    for a, b in _NORM_PAIRS:
        if a in s:
            s = s.replace(a, b)
    return _DOUBLE.sub(r"\1", _PUNCT.sub("", s))


def lev_le(a, b, k):
    """True if Levenshtein distance(a, b) <= k."""
    if abs(len(a) - len(b)) > k:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        best = i
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            best = min(best, cur[j])
        if best > k:
            return False
        prev = cur
    return prev[-1] <= k


class TextIndex:
    """All txt lines of one collection in memory, refreshed when folders change."""

    CACHE_VERSION = 1

    def __init__(self, cid):
        self.cid = cid
        self.lock = threading.Lock()
        self.stamp = None
        self.vols = {}    # vol -> (txt dir mtime, file count, [(scan, [raw lines], [norm lines])])
        self.docs = []    # (vol, scan, [raw lines], [norm lines])
        self.vocab = set()
        self.fuzzy_cache = {}
        self.folded = None  # folded spelling -> words of the collection, made at the first search for names
        self.checked = 0
        self.cache_file = CACHE_DIR / f"{cid}.pkl"
        self._load_cache()

    def _load_cache(self):
        import pickle
        try:
            with open(self.cache_file, "rb") as f:
                ver, vols = pickle.load(f)
            if ver == self.CACHE_VERSION:
                self.vols = vols
        except Exception:
            pass

    def _save_cache(self):
        import pickle
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                pickle.dump((self.CACHE_VERSION, self.vols), f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, self.cache_file)
        except OSError:
            pass

    def _stamp(self):
        root = COLL.root(self.cid)
        st = []
        for v in list_volumes(self.cid):
            t = root / v["name"] / "txt"
            if t.is_dir() and v["txt"]:     # a check of a line counts as a change of the text of its book
                st.append((v["name"], max(t.stat().st_mtime, CORR.mtime(self.cid, v["name"]) if CORR else 0), v["txt"]))
        return tuple(st)

    def refresh(self):
        with self.lock:
            if time.time() - self.checked < 60 and self.stamp is not None:
                return
            stamp = self._stamp()
            self.checked = time.time()
            if stamp == self.stamp:
                return
            root = COLL.root(self.cid)
            changed = False
            keep = {}
            for vol, mtime, count in stamp:
                old = self.vols.get(vol)
                if old and old[0] == mtime and old[1] == count:
                    keep[vol] = old
                    continue
                changed = True
                tdir = root / vol / "txt"
                pages = []
                fix = CORR.index_fix(self.cid, vol) if CORR else None
                for e in sorted(os.scandir(tdir), key=lambda e: e.name):
                    if e.name.endswith(".txt"):
                        raw = Path(e.path).read_text(encoding="utf-8", errors="replace").splitlines()
                        if fix:
                            raw = fix(e.name[:-4], raw)
                        pages.append((e.name[:-4], raw, [norm(x) for x in raw]))
                keep[vol] = (mtime, count, pages)
            if set(keep) != set(self.vols):
                changed = True
            self.vols = keep
            docs, vocab = [], set()
            for vol, (_, _, pages) in keep.items():
                for scan, raw, nl in pages:
                    docs.append((vol, scan, raw, nl))
                    for x in nl:
                        vocab.update(x.split())
            self.docs, self.vocab, self.stamp = docs, vocab, stamp
            self.fuzzy_cache = {}
            self.folded = None
            if changed:
                self._save_cache()

    def fuzzy_words(self, w):
        """All words in the collection within edit distance k of w (k depends on word length)."""
        k = 0 if len(w) <= 3 else 1 if len(w) <= 6 else 2
        if w in self.fuzzy_cache:
            return self.fuzzy_cache[w]
        ws, lw = set(w), len(w)
        out = set()
        for v in self.vocab:
            if abs(len(v) - lw) > k:
                continue
            if len(ws.symmetric_difference(v)) > 2 * k:  # each edit changes the letter set by at most 2
                continue
            if lev_le(w, v, k):
                out.add(v)
        self.fuzzy_cache[w] = out
        return out

    def variant_words(self, word):
        """Words in the collection that are this name word in an old spelling (as the name finder makes them),
        or that are one or two letters off (reading errors; none for words of four letters or fewer)."""
        import names
        key = "names:" + word
        if key in self.fuzzy_cache:
            return self.fuzzy_cache[key]
        if self.folded is None:
            folded = {}
            for v in self.vocab:
                folded.setdefault(names.fold(v), []).append(v)
            self.folded = folded
        forms = {names.fold(norm(x)) for x in names.word_forms(word)} - {""}
        base = names.fold(norm(word))
        k = names.max_edits(base)
        out = set()
        for f in forms:
            out.update(self.folded.get(f, ()))
        if k and base:
            bs, lb = set(base), len(base)
            for f, vs in self.folded.items():
                if abs(len(f) - lb) > k or len(bs.symmetric_difference(f)) > 2 * k:
                    continue
                if lev_le(base, f, k):
                    out.update(vs)
        self.fuzzy_cache[key] = out
        return out

    def search(self, q, mode="loose", vol=None, window=1, limit=500):
        self.refresh()
        q = q.strip()
        if not q:
            return {"total": 0, "results": [], "per_vol": {}, "terms": [], "docs": len(self.docs)}
        if mode == "regex":
            if len(q) > 200:
                raise ValueError("The regular expression is too long (more than 200 characters).")
            rx = re.compile(q, re.I)
            test = lambda raw, nl: rx.search(raw)
        elif mode == "exact":
            ql = q.lower()
            test = lambda raw, nl: ql in raw.lower()
        else:
            words = [w for w in norm(q).split() if w]
            if not words:
                return {"total": 0, "results": [], "per_vol": {}, "terms": [], "docs": len(self.docs)}
            sets = None
            if mode == "names":
                toks = [t for t in re.findall(r"[^\W\d_]+", q.lower()) if norm(t)]
                sets = [self.variant_words(t) for t in toks]
            elif mode == "fuzzy":
                sets = [self.fuzzy_words(w) for w in words]
            if sets is not None:
                if not sets:
                    return {"total": 0, "results": [], "per_vol": {}, "terms": [], "docs": len(self.docs)}
                found = set().union(*sets)

                def test(raw, nl):
                    ws = set(nl.split())
                    return all(not ws.isdisjoint(s) for s in sets)
            else:
                def test(raw, nl):
                    return all(w in nl for w in words)
        total, results, per_vol = 0, [], {}
        for dvol, scan, raw, nl in self.docs:
            if vol and dvol != vol:
                continue
            for i in range(len(raw)):
                if not raw[i].strip():
                    continue
                if window > 1:
                    # line + next non-empty line; a hit that lies wholly in the next line is reported there
                    j = i + 1
                    while j < len(raw) and not raw[j].strip():
                        j += 1
                    hit = test(raw[i] + " " + (raw[j] if j < len(raw) else ""),
                               nl[i] + " " + (nl[j] if j < len(nl) else ""))
                    if hit and j < len(raw) and test(raw[j], nl[j]) and not test(raw[i], nl[i]):
                        hit = False
                else:
                    hit = test(raw[i], nl[i])
                if hit:
                    total += 1
                    per_vol[dvol] = per_vol.get(dvol, 0) + 1
                    if len(results) < limit:
                        ctx_before = next((raw[k] for k in range(i - 1, -1, -1) if raw[k].strip()), "")
                        ctx_after = next((raw[k] for k in range(i + 1, len(raw)) if raw[k].strip()), "")
                        results.append({"vol": dvol, "scan": scan, "n": i + 1, "text": raw[i],
                                        "before": ctx_before, "after": ctx_after})
                        if mode in ("fuzzy", "names"):      # the words that made the hit, to mark them
                            results[-1]["m"] = sorted(found.intersection(
                                (nl[i] + " " + (nl[j] if window > 1 and j < len(nl) else "")).split()))
        terms = norm(q).split() if mode in ("loose", "fuzzy", "names") else []
        return {"total": total, "results": results, "per_vol": per_vol, "terms": terms, "docs": len(self.docs)}


_indexes = {}
_index_lock = threading.Lock()


def index_for(cid):
    COLL.get(cid)
    with _index_lock:
        if cid not in _indexes:
            _indexes[cid] = TextIndex(cid)
        return _indexes[cid]


# ---------------------------------------------------------------- HTTP

def plain_error(e):
    """The message of a refused request, without the inside of Python."""
    s = str(e).strip("'\"")
    if isinstance(e, json.JSONDecodeError) or s.startswith(("invalid literal", "could not convert")):
        return "The request is not valid."
    if isinstance(e, KeyError) and re.fullmatch(r"\w{1,4}", s):       # a missing part of the address, such as c or v
        return f"The request misses a part: {s}."
    return s


class Handler(BaseHTTPRequestHandler):
    timeout = 60                # seconds a connection may stay silent while we wait for it

    def oops(self, e):
        """Something unforeseen (a damaged file, for instance): a plain answer, the cause in the log."""
        print(f"error in {self.command} {self.path.split('?')[0]}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        traceback.print_exc()
        try:
            self.send_json({"error": "Something went wrong on the server."}, 500)
        except OSError:
            pass

    server_version = "LoghiStudio/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        super().end_headers()

    def send_json(self, obj, code=200, headers=()):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, ctype, cache=True):
        try:
            data = path.read_bytes()
        except OSError:
            return self.send_json({"error": "file not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", ("private, " if self.server.auth else "") + "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(data)

    COOKIE = "ls_session"

    def token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == self.COOKIE:
                return v
        return None

    def client_ip(self):
        """The address of the visitor; behind the own web server that is the last address it added."""
        fwd = self.headers.get("X-Forwarded-For")
        if self.server.auth and fwd:
            return fwd.split(",")[-1].strip()
        return self.client_address[0]

    def guard(self, writing=False):
        """Who is asking. None when the request was refused (an answer has been sent).

        On the own computer: only this pc may talk to the server (foreign Host names and foreign web pages
        are refused); the answer is {"role": "admin", "local": True}. In server mode: the Host must be one of
        the known names and the answer is the session, or {"role": None} when nobody is logged in."""
        auth = self.server.auth
        if auth:
            ok_hosts = self.server.hosts
        else:
            port = self.server.server_address[1]
            ok_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if (self.headers.get("Host") or "").lower() not in ok_hosts:
            self.send_json({"error": "unknown host name" if auth else "only reachable from this computer"}, 403)
            return None
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc.lower() not in ok_hosts:
            self.send_json({"error": "request from another website refused"}, 403)
            return None
        if writing and not (self.headers.get("Content-Type") or "").strip().lower().startswith("application/json"):
            self.send_json({"error": "JSON only"}, 415)
            return None
        if not auth:
            return {"user": None, "role": "admin", "local": True}
        return auth.session(self.token()) or {"user": None, "role": None}

    def cookie(self, token, age):
        return ("Set-Cookie", f"{self.COOKIE}={token}; Path={self.server.base}; Max-Age={age}; HttpOnly; SameSite=Lax"
                + ("; Secure" if self.server.secure else ""))

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > 8_000_000:
            raise ValueError("too much data")
        data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("invalid data")
        return data

    def do_GET(self):
        who = self.guard()
        if who is None:
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = unquote(u.path)
        local = bool(who.get("local"))
        try:
            if who["role"] is None:         # server mode, nobody logged in: only the login page
                if path in ("/", "/index.html"):
                    return self.send_file(HERE / "login.html", "text/html; charset=utf-8", cache=False)
                return self.send_json({"error": "log in first"}, 401)
            if path in ("/", "/index.html"):
                return self.send_file(HERE / "index.html", "text/html; charset=utf-8", cache=False)
            if path == "/api/me":
                return self.send_json({"user": who["user"], "role": who["role"], "mode": "local" if local else "server"})
            if path == "/api/version":
                return self.send_json({"version": VERSION})
            if path == "/api/collections":
                cols = COLL.public()
                if not local:               # folders on the disk are nobody's business outside
                    cols = [{k: v for k, v in c.items() if k not in ("root", "log", "hits", "index")} for c in cols]
                return self.send_json(cols)
            if not local and path in ("/api/browse", "/api/jobs", "/api/queue", "/api/queue/books", "/api/corrections/export"):
                return self.send_json({"error": "not available here"}, 403)
            if path == "/api/browse":
                return self.send_json(browse(q.get("path", "")))
            if path == "/api/volumes":
                return self.send_json(list_volumes(q["c"]))
            if path == "/api/scans":
                return self.send_json(list_scans(q["c"], q["v"]))
            if path == "/api/page":
                if not re.fullmatch(NAME_RE, q.get("s", "")):
                    raise KeyError("invalid scan name")
                return self.send_json(read_page(q["c"], q["v"], q["s"]))
            if path == "/api/status":
                return self.send_json({c["id"]: status(c["id"]) for c in COLL.public() if c["exists"]})
            if path == "/api/jobs":
                return self.send_json(BOOKS.public())
            if path == "/api/queue":
                return self.send_json(QUEUE.public())
            if path == "/api/queue/books":
                return self.send_json(QUEUE.books(q["c"]))
            if path == "/api/names":
                return self.send_json(NAMES.public(q.get("c") or None, q.get("list") or None))
            if path == "/api/corrections":
                return self.send_json(CORR.summary(q["c"]))
            if path == "/api/corrections/next":
                return self.send_json({"next": CORR.next(q["c"], q["v"], q.get("s") or None, int(q.get("n") or 0),
                                                         q.get("order", "page"))})
            if path == "/api/corrections/export":
                return self.send_json(CORR.export_state(q["c"]))
            if path == "/api/corrections/index":
                return self.send_json(CORR.index_names(q["c"], q["v"], q["s"]))
            if path == "/api/corrections/other":
                return self.send_json(CORR.other(q["c"], q["v"], q["s"]))
            if path == "/api/hits" and q.get("src") == "old":
                rows = load_hits(q["c"])
                if q.get("v"):
                    rows = [r for r in rows if r["vol"] == q["v"]]
                if q.get("s"):
                    rows = [r for r in rows if r["scan"] == q["s"]]
                return self.send_json(rows)
            if path == "/api/hits":           # hits with their text: of one person, or of one book or scan
                if not (q.get("id") or q.get("v")):
                    raise ValueError("Give a person (id) or a book (v).")
                return self.send_json(NAMES.rows(q["c"], q.get("id") or None, q.get("strength", "best"), q.get("list") or None,
                                                 q.get("v") or None, q.get("s") or None, int(q.get("offset", 0)),
                                                 min(int(q.get("limit", 100)), 1000)))
            if path == "/api/hits/marks":
                return self.send_json(NAMES.marks(q["c"], q["v"]))
            if path == "/api/hits/summary":
                return self.send_json(NAMES.summary(q["c"], q.get("strength", "best"), q.get("list") or None, q.get("v") or None,
                                                    q.get("q", ""), q.get("sort", "name"), int(q.get("offset", 0)),
                                                    min(int(q.get("limit", 50)), 500)))
            if path == "/api/hits/export":
                body = NAMES.export_csv(q["c"], q.get("strength", "best"), q.get("list") or None, q.get("v") or None).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="hits_{q["c"]}_{time.strftime("%Y-%m-%d")}.csv"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/search":
                t0 = time.time()
                if self.server.auth and q.get("mode") == "regex":     # one pattern can keep a processor busy for minutes
                    raise ValueError("Searching with a regular expression is switched off on this server.")
                res = index_for(q["c"]).search(q.get("q", ""), q.get("mode", "loose"), q.get("v") or None,
                                               min(int(q.get("w", 1)), 5), min(int(q.get("limit", 500)), 1000))
                if q.get("mode", "loose") in ("loose", "fuzzy", "names"):       # names read by people, if there is an index
                    res["index"] = CORR.index_search(q["c"], q.get("q", ""), q.get("v") or None)
                res["secs"] = round(time.time() - t0, 2)
                return self.send_json(res)
            m = re.fullmatch(rf"/img/([\w\-]+)/({NAME_RE})/({NAME_RE}\.jpe?g)", path, re.I)
            if m:
                return self.send_file(vol_path(m.group(1), m.group(2)) / m.group(3), "image/jpeg")
            self.send_json({"error": "not found"}, 404)
        except re.error as e:
            self.send_json({"error": f"invalid regular expression: {e}"}, 400)
        except (KeyError, ValueError) as e:
            self.send_json({"error": plain_error(e)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.oops(e)

    def _write(self, method):
        who = self.guard(writing=method != "DELETE")
        if who is None:
            return
        path = unquote(urlparse(self.path).path)
        try:
            if not who.get("local"):
                auth = self.server.auth
                if path == "/api/login" and method == "POST":
                    d = self.read_body()
                    token, res = auth.login(d.get("user"), d.get("password"), self.client_ip())
                    if token:
                        return self.send_json(res, 200, [self.cookie(token, 7 * 86400)])
                    if res == "wait":
                        return self.send_json({"error": "too many wrong attempts; wait 15 minutes"}, 429)
                    if res == "slow":
                        return self.send_json({"error": "many wrong attempts for this name; wait a few seconds"}, 429, [("Retry-After", "5")])
                    return self.send_json({"error": "name or password is not right"}, 401)
                if path == "/api/logout" and method == "POST":
                    auth.logout(self.token())
                    return self.send_json({"ok": True}, 200, [self.cookie("", 0)])
                if who["role"] is None:
                    return self.send_json({"error": "log in first"}, 401)
                may = (who["role"] == "admin" and re.match(r"/api/(names|lists)(/|$)", path)) or \
                      (who["role"] in ("admin", "corrector") and path == "/api/corrections")
                if not may:
                    return self.send_json({"error": "not allowed for this account"}, 403)
            if path == "/api/corrections" and method == "POST":
                d = self.read_body()
                return self.send_json(CORR.save(d.get("collection"), d.get("book"), d.get("scan"), d.get("line"),
                                                d.get("text"), d.get("state") or "checked", who.get("user")), 201)
            if path == "/api/corrections/export" and method == "POST":
                return self.send_json(CORR.start_export(self.read_body().get("collection")), 202)
            if path == "/api/books" and method == "POST":
                d = self.read_body()
                read = d.get("read_model") or None
                if read and read not in QUEUE.models():
                    raise ValueError("This reading model is not in the models folder.")
                if d.get("kind") == "tresoar":
                    job = BOOKS.add_tresoar(d.get("collection"), d.get("access"), d.get("inv"), d.get("book"),
                                            d.get("size", 2000), read)
                else:
                    job = BOOKS.add_folder(d.get("collection"), d.get("book"), d.get("path"), read)
                return self.send_json(job, 201)
            if path == "/api/queue" and method == "POST":
                d = self.read_body()
                added, errors = [], []
                for b in d.get("books") or []:
                    try:
                        added.append(QUEUE.add(d.get("collection"), b, d.get("model") or None, bool(d.get("retry_skipped")), int(d.get("enlarge") or 0)))
                    except (KeyError, ValueError) as e:
                        errors.append(str(e).strip("'\""))
                if not added:
                    raise ValueError(" ".join(errors) or "Choose at least one book.")
                return self.send_json({"added": added, "errors": errors}, 201)
            if path == "/api/queue/settings" and method == "PUT":
                return self.send_json(QUEUE.set_settings(self.read_body()))
            if path == "/api/queue/clear" and method == "POST":
                return self.send_json(QUEUE.clear_finished())
            m = re.fullmatch(r"/api/queue/([0-9a-f]+)", path)
            if m and method == "PUT":
                return self.send_json(QUEUE.change(m.group(1), self.read_body()))
            if m and method == "DELETE":
                return self.send_json(QUEUE.remove(m.group(1)))
            m = re.fullmatch(r"/api/jobs/([\w\-]+)", path)
            if m and method == "DELETE":
                return self.send_json(BOOKS.cancel(m.group(1)))
            if path == "/api/names/import" and method == "POST":
                return self.send_json(NAMES.import_people(self.read_body()), 201)
            m = re.fullmatch(r"/api/lists(?:/([\w\-]+))?", path)
            if m:
                lid = m.group(1)
                if method == "POST" and not lid:
                    return self.send_json(NAMES.add_list(self.read_body()), 201)
                if method == "PUT" and lid:
                    return self.send_json(NAMES.update_list(lid, self.read_body()))
                if method == "DELETE" and lid:
                    return self.send_json(NAMES.remove_list(lid))
                return self.send_json({"error": "not allowed"}, 405)
            m = re.fullmatch(r"/api/names(?:/([\w\-]+))?", path)
            if m:
                nid = m.group(1)
                if method == "POST" and not nid:
                    return self.send_json(NAMES.add(self.read_body()), 201)
                if method == "PUT" and nid:
                    return self.send_json(NAMES.update(nid, self.read_body()))
                if method == "DELETE" and nid:
                    return self.send_json(NAMES.remove(nid))
                return self.send_json({"error": "not allowed"}, 405)
            m = re.fullmatch(r"/api/collections(?:/([\w\-]+))?", path)
            if not m:
                return self.send_json({"error": "not found"}, 404)
            cid = m.group(1)
            if method == "POST" and not cid:
                return self.send_json(COLL.add(self.read_body()), 201)
            if method == "PUT" and cid:
                return self.send_json(COLL.update(cid, self.read_body()))
            if method == "DELETE" and cid:
                return self.send_json(COLL.remove(cid))
            self.send_json({"error": "not allowed"}, 405)
        except (KeyError, ValueError) as e:
            self.send_json({"error": plain_error(e)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.oops(e)

    def do_POST(self):
        self._write("POST")

    def do_PUT(self):
        self._write("PUT")

    def do_DELETE(self):
        self._write("DELETE")


def warm_up():
    """Build the search indexes one by one in the background, so the first search does not have to wait."""
    for cid in COLL.ids():
        try:
            if COLL.root(cid).is_dir():
                index_for(cid).refresh()
        except Exception:
            pass
    NAMES.start()


def make_server(port=8771, auth=None, hosts=(), base="/", secure=True):
    """Without auth: the local application, on 127.0.0.1 only. With auth (auth.Auth): server mode."""
    if COLL is None:
        init()
    srv = ThreadingHTTPServer(("0.0.0.0" if auth else "127.0.0.1", port), Handler)
    srv.auth, srv.hosts, srv.base, srv.secure = auth, {h.strip().lower() for h in hosts if h.strip()}, base, secure
    return srv


def migrate_dutch_names():
    """Versions before 0.4 used Dutch file names and state words; convert them once."""
    old = HERE / "instellingen"
    if old.is_dir() and SETTINGS_DIR == HERE / "settings" and not SETTINGS_DIR.exists():
        os.rename(old, SETTINGS_DIR)
    for a, b in (("collecties.json", "collections.json"), ("namen.json", "names.json"), ("taken.json", "jobs.json")):
        if (SETTINGS_DIR / a).exists() and not (SETTINGS_DIR / b).exists():
            os.rename(SETTINGS_DIR / a, SETTINGS_DIR / b)
    words = {"ruim": "fuzzy", "precies": "exact", "wachten": "waiting", "bezig": "running", "klaar": "done",
             "mislukt": "failed", "gestopt": "stopped", "map": "folder"}
    for fn, key, fields in (("names.json", "names", ("mode",)), ("jobs.json", "jobs", ("state", "kind"))):
        p = SETTINGS_DIR / fn
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        changed = False
        for item in data.get(key, []):
            for f in fields:
                if item.get(f) in words:
                    item[f], changed = words[item[f]], True
            if "toegang" in item:
                item["access"], changed = item.pop("toegang"), True
        if changed:
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if (CACHE_DIR / "namen").is_dir() and not (CACHE_DIR / "names").exists():
        os.rename(CACHE_DIR / "namen", CACHE_DIR / "names")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8771
    migrate_dutch_names()
    if os.environ.get("LOGHI_MODE") == "server":
        import auth
        hosts = [h for h in os.environ.get("LOGHI_HOSTS", "").split(",") if h.strip()]
        accounts = auth.Auth(SETTINGS_DIR)
        if not hosts or not accounts.users:
            raise SystemExit("Server mode needs LOGHI_HOSTS and at least one account (python auth.py <settings> <name> <role>).")
        srv = make_server(port, accounts, hosts, os.environ.get("LOGHI_BASE", "/"), os.environ.get("LOGHI_INSECURE") != "1")
        COLL.hide_private = True    # scans for own use only are not shown to visitors (COLL exists after make_server)
        print(f"Loghi-studio, server mode, port {port}, for {', '.join(hosts)}", flush=True)
    else:                                   # the reading queue and adding books only exist on the own computer
        srv = make_server(port)
        print(f"Loghi-studio: http://127.0.0.1:{port}/")
        BOOKS.start()
        QUEUE.start()
        if (CORR.dir.parent / "git_backup.on").exists():    # asked for: the lines checked by people go to git by themselves
            CORR.auto_backup = True
            threading.Thread(target=CORR.backup, daemon=True).start()
    threading.Thread(target=warm_up, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
