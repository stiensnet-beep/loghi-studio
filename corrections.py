"""Corrections: lines that a person checked against the scan, and training lines made from them.

A correction never changes the scans, the PageXML or the text files of Loghi. Every checked line is one
JSON line, appended to <settings>/corrections/<collection>/<book>.jsonl: the text Loghi read, the text as it
is written on the scan, who, when, and the place of the line on the scan. The last entry of a line counts;
older entries stay in the file, so nothing is ever lost.

States of an entry:
- "checked":    the text is what is written on the scan (it may be the same as Loghi read);
- "partial":    only some words were corrected; the rest of the line could not be read with certainty.
                The text is shown in View and Search, but the line gives no training line;
- "unreadable": the line cannot be read with certainty; it gives no training line;
- "undone":     the check was withdrawn.

View, Search and the name finder show the checked text; the text of Loghi stays in its own files.

A training set is made from the checked lines in the format Loghi asks for (docs/training.md and
scripts/create-train-data.sh of knaw-huc/loghi, README of loghi-htr): a ground-truth folder with scans and
PageXML that holds only the checked lines, cut into line images by Loghi's own
MinionCutFromImageBasedOnPageXMLNew, and list files with "<path of the line image><tab><text>" per line.
A scan that was read enlarged is put in the set enlarged in the same way, so a training line looks exactly
like the line the model gets when it reads.

An index of names made by people (the field "index" of a collection: a file made by
tools_allefriezen_index.py) helps the reader: the names of the deed that starts on a scan are shown beside
the line, and the order "names" goes to the lines that hold a word nearly, but not exactly, like a word of
such a name. The index may spell a name differently from the scan; the scan decides.

A second reading of the same scans helps too: where two readers differ, at least one of them is wrong. The
second reading is a folder <cache>/second/<collection>/<book>/page/ with PageXML (for instance the same lines
read by another model), else the text the book had before it was read again (page/<scan>.xml.bak). The order
"differ" goes through the lines the two read differently, the smallest difference first; the reader sees
both texts and decides on the scan. A second reading is never a training line by itself.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

STATES = ("checked", "partial", "unreadable", "undone")
MAX_TEXT = 500
MIN_LEN = 10                # "unsure lines first" passes over shorter lines (stains, loose marks)
WORD = re.compile(r"[^\W\d_]{4,}")  # words of a name that are long enough to compare
WORD_ANY = re.compile(r"[^\W\d_]+")
SAME_PLACE = 0.5            # overlap of the boxes of two lines to count as the same line after reading again
MOUNT = "/work"             # where the training folder is mounted in the Loghi containers
VAL_PART = 10               # one line in ten goes to the validation list
PAGE_NS = "http://schema.primaresearch.org/PAGE/gts/pagecontent/2013-07-15"
OWN_USE = "OWN-USE-ONLY.txt"
BACKUP_AFTER = 600          # seconds after the last check before the copy to git is made


def clean_text(text):
    """The text of a line as it is kept: one line, composed characters, single spaces."""
    if not isinstance(text, str):
        raise ValueError("The text of the line is missing.")
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()
    if not text:
        raise ValueError("The line is empty. Type what is written, or mark the line as unreadable.")
    if len(text) > MAX_TEXT:
        raise ValueError(f"The line is too long (more than {MAX_TEXT} characters).")
    if any(unicodedata.category(ch) == "Cc" for ch in text):
        raise ValueError("The line holds a character that cannot be saved.")
    return text


def bbox(points):
    """(x0, y0, x1, y1) of a PageXML points string, or None."""
    try:
        pts = [tuple(float(v) for v in p.split(",")) for p in (points or "").split()]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)
    except (ValueError, IndexError):
        return None


def overlap(a, b):
    """Shared part of two boxes, as a share of their joined area (0..1)."""
    if not a or not b:
        return 0.0
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    both = w * h
    return both / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - both)


def distance(a, b):
    """Number of letters to change, add or drop to make b from a."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def scale_points(points, factor):
    out = []
    for p in points.split():
        x, _, y = p.partition(",")
        out.append(f"{round(float(x) * factor)},{round(float(y) * factor)}")
    return " ".join(out)


class Corrections:
    def __init__(self, srv, settings_dir, training_dir):
        self.srv = srv
        self.dir = Path(settings_dir) / "corrections"
        self.training_dir = Path(training_dir)
        self.lock = threading.RLock()
        self._cache = {}            # (collection, book) -> (file stamp, {scan: {line id: entry}})
        self._lines = {}            # (collection, book, scan) -> (stamp of the PageXML, rows)
        self._sums = {}             # (collection, book) -> (file stamp, counts)
        self._idx = {}              # collection -> (file stamp, {book: {scan: [deed]}})
        self._all = {}              # collection -> all deeds of the index, also those without a scan
        self._near = {}             # (collection, book) -> (stamps, [(scan, line number, line id)])
        self.second_dir = Path(getattr(srv, "CACHE_DIR", Path(settings_dir).parent / "cache")) / "second"
        self._other = {}            # (collection, book, scan) -> (stamps, {line number: text of the second reading})
        self._diff = {}             # (collection, book) -> (stamps, [(share that differs, scan, line number, line id)])
        self._second_books = {}     # collection -> (time, {book: "other" or "before"})
        self.job = {"running": False, "collection": None, "state": None, "message": "", "finished": None}
        self.auto_backup = False    # switched on by server.main() on the own computer
        self.backup_state = {"time": None, "ok": None, "message": ""}
        self._timer = None
        self._git_lock = threading.Lock()

    # ------------------------------------------------------------ the files
    def _file(self, cid, vol):
        self.srv.COLL.get(cid)
        if not re.fullmatch(r"[\w\-]+", cid) or not re.fullmatch(self.srv.NAME_RE, vol or "") or vol in (".", ".."):
            raise KeyError("invalid book name")
        return self.dir / cid / f"{vol}.jsonl"

    @staticmethod
    def _stamp(f):
        try:
            st = f.stat()
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    def mtime(self, cid, vol):
        """Time of the last check in a book (0 when there is none)."""
        try:
            return self._file(cid, vol).stat().st_mtime
        except (OSError, KeyError):
            return 0.0

    def _load(self, cid, vol):
        """{scan: {line id: last entry}} of a book; lines whose check was withdrawn are left out."""
        f = self._file(cid, vol)
        stamp = self._stamp(f)
        if stamp is None:
            return {}
        with self.lock:
            hit = self._cache.get((cid, vol))
            if hit and hit[0] == stamp:
                return hit[1]
            scans = {}
            for raw in f.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(raw)
                    scan, line, state = r["scan"], r["line"], r["state"]
                except (ValueError, KeyError, TypeError):
                    continue        # a damaged line (a crash while writing) never blocks the rest
                if state == "undone":
                    scans.get(scan, {}).pop(line, None)
                    if scan in scans and not scans[scan]:
                        del scans[scan]
                elif state in STATES:
                    scans.setdefault(scan, {})[line] = r
            self._cache[(cid, vol)] = (stamp, scans)
            return scans

    def books(self, cid):
        """Books of a collection that have checked lines."""
        d = self.dir / cid
        if not re.fullmatch(r"[\w\-]+", cid) or not d.is_dir():
            return []
        return sorted(e.name[:-6] for e in os.scandir(d) if e.name.endswith(".jsonl"))

    # ------------------------------------------------------------ showing
    @staticmethod
    def _mark(line, rec):
        line["orig"] = line["text"]
        if rec["state"] == "unreadable":
            line["checked"] = "unreadable"
        elif rec["state"] == "partial":
            line["text"] = rec["text"]
            line["checked"] = "partial"
        else:
            line["text"] = rec["text"]
            line["checked"] = "corrected" if rec["text"] != line["orig"] else "confirmed"
        line["by"], line["at"] = rec.get("user"), rec.get("time")

    def apply(self, cid, vol, scan, lines):
        """Put the checks of a scan on its lines (as read from the PageXML).

        A line keeps its check when the scan is read again: the new line at the same place gets it."""
        try:
            recs = self._load(cid, vol).get(scan)
        except KeyError:
            return lines
        if not recs:
            return lines
        left = dict(recs)
        for l in lines:
            r = left.get(l.get("id"))
            if r is not None and r.get("coords") == l.get("poly"):
                self._mark(l, r)
                del left[l["id"]]
        if left:
            free = [(l, bbox(l.get("poly"))) for l in lines if "checked" not in l and l.get("poly")]
            for r in left.values():
                box = bbox(r.get("coords"))
                best, share = None, SAME_PLACE
                for i, (l, b) in enumerate(free):
                    s = overlap(box, b)
                    if s >= share:
                        best, share = i, s
                if best is not None:
                    self._mark(free.pop(best)[0], r)
        return lines

    def index_fix(self, cid, vol):
        """A function (scan, lines of the text file) -> lines with the checked text, or None without checks."""
        try:
            scans = self._load(cid, vol)
        except KeyError:
            return None
        if not scans:
            return None

        def fix(scan, raw):
            recs = scans.get(scan)
            if not recs:
                return raw
            out, slow = list(raw), False
            for r in recs.values():
                if r["state"] not in ("checked", "partial"):
                    continue
                i = r.get("n", 0) - 1
                if 0 <= i < len(out) and raw[i] == r.get("orig"):
                    out[i] = r["text"]
                else:
                    slow = True
            if slow:                    # the scan was read again: place the checks through the PageXML
                out = list(raw)
                try:
                    for l in self.srv.read_page(cid, vol, scan)["lines"]:
                        if l.get("checked") in ("corrected", "confirmed", "partial") and 0 < l["n"] <= len(out):
                            out[l["n"] - 1] = l["text"]
                except (KeyError, ValueError, OSError):
                    pass
            return out
        return fix

    # ------------------------------------------------------------ saving
    def save(self, cid, vol, scan, line_id, text, state, user):
        if state not in STATES:
            raise ValueError("Unknown state of a check.")
        if not re.fullmatch(self.srv.NAME_RE, scan or ""):
            raise KeyError("invalid scan name")
        f = self._file(cid, vol)
        page = self.srv.read_page(cid, vol, scan, raw=True)
        line = next((l for l in page["lines"] if l.get("id") and l["id"] == line_id), None)
        if line is None or not line.get("poly"):
            raise KeyError("This line is not on the scan. Was the scan read again? Open the scan once more.")
        rec = {"scan": scan, "line": line_id, "n": line["n"], "state": state,
               "user": str(user or "local")[:80], "time": int(time.time())}
        if state == "undone":
            if line_id not in self._load(cid, vol).get(scan, {}):
                raise ValueError("This line has not been checked.")
        else:
            rec.update(orig=line["text"], conf=line["conf"], coords=line["poly"], base=line.get("base"))
            if state in ("checked", "partial"):
                rec["text"] = clean_text(text)
        with self.lock:
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "a", encoding="utf-8", newline="\n") as out:
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                os.fsync(out.fileno())
        ix = self.srv._indexes.get(cid)
        if ix is not None:
            ix.checked = 0              # the next search looks at the files again
        if self.auto_backup:
            self.backup_later()
        shown = next((l for l in self.srv.read_page(cid, vol, scan)["lines"] if l.get("id") == line_id), None)
        return {"line": shown, "book": self.counts(cid, vol)}

    # ------------------------------------------------------------ a copy in git
    def _repo(self):
        """(folder of the git repository, path of the corrections in it), or (None, None)."""
        repo = Path(self.srv.HERE)
        try:
            rel = self.dir.resolve().relative_to(repo.resolve())
        except ValueError:
            return None, None
        return (repo, rel.as_posix()) if (repo / ".git").exists() else (None, None)

    def backup_later(self):
        with self.lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(BACKUP_AFTER, self.backup)
            self._timer.daemon = True
            self._timer.start()

    def backup(self):
        """Commit and push the folder with the checked lines, and nothing else. Returns the state."""
        repo, rel = self._repo()
        if repo is None or not self.dir.is_dir():
            return None

        def git(*args):
            r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=180,
                               stdin=subprocess.DEVNULL, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode:
                raise OSError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "git failed")
            return r.stdout
        with self._git_lock:
            state = {"time": time.time(), "ok": True, "message": ""}
            try:
                ignored = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--", rel], capture_output=True,
                                         stdin=subprocess.DEVNULL, timeout=60,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode == 0
                if ignored:
                    raise OSError("This folder is ignored by git here (.gitignore). Keep the copy in a private repository of your own; see the README.")
                git("add", "--", rel)
                if git("status", "--porcelain", "--", rel).strip():
                    git("commit", "-q", "-m", "Corrections: copy of the checked lines", "--only", "--", rel)
                try:
                    git("push", "-q")
                except (OSError, subprocess.SubprocessError) as e:
                    raise OSError(f"The copy is in git on this computer, but sending it on failed: {e}")
            except (OSError, subprocess.SubprocessError) as e:
                state.update(ok=False, message=str(e))
            self.backup_state = state
            return state

    # ------------------------------------------------------------ counting
    def counts(self, cid, vol):
        """Numbers of one book. "errors" and "chars" give the share of letters Loghi read wrong in the
        checked lines (letters to change, add or drop, per letter of the right text)."""
        f = self._file(cid, vol)
        stamp = self._stamp(f)
        with self.lock:
            hit = self._sums.get((cid, vol))
            if hit and hit[0] == stamp:
                return dict(hit[1])
        c = {"checked": 0, "corrected": 0, "partial": 0, "unreadable": 0, "chars": 0, "errors": 0, "scans": 0, "last": 0}
        for recs in self._load(cid, vol).values():
            c["scans"] += 1
            for r in recs.values():
                c["last"] = max(c["last"], r.get("time") or 0)
                if r["state"] in ("unreadable", "partial"):
                    c[r["state"]] += 1      # no training line, and not in the share of letters read wrong
                    continue
                c["checked"] += 1
                c["chars"] += len(r["text"])
                if r["text"] != r.get("orig"):
                    c["corrected"] += 1
                    c["errors"] += distance(r.get("orig") or "", r["text"])
        with self.lock:
            self._sums[(cid, vol)] = (stamp, c)
        return dict(c)

    def summary(self, cid):
        col = self.srv.COLL.get(cid)
        books, total = {}, {"checked": 0, "corrected": 0, "partial": 0, "unreadable": 0, "chars": 0, "errors": 0, "scans": 0, "last": 0}
        for vol in self.books(cid):
            c = self.counts(cid, vol)
            if c["checked"] or c["partial"] or c["unreadable"]:
                books[vol] = c
                for k in total:
                    total[k] = max(total[k], c[k]) if k == "last" else total[k] + c[k]
        return {"books": books, "total": total, "private": bool(col.get("private")), "charset": self.charset(),
                "index": bool(self._index(cid)), "second": self.second_books(cid),
                "backup": dict(self.backup_state) if self.auto_backup and self._repo()[0] else None}

    def charset(self):
        """The characters the reading model knows (its charlist), or None when no model is at hand."""
        q = self.srv.QUEUE
        try:
            models = q.models()
            name = q.data.get("last_model") if q.data.get("last_model") in models else models[0]
            chars = (Path(q.data["models_dir"]) / name / "charlist.txt").read_text(encoding="utf-8")
            return "".join(sorted(set(chars) | {" "}))
        except (AttributeError, IndexError, KeyError, OSError, UnicodeError):
            return None

    # ------------------------------------------------------------ what to check next
    def _scan_lines(self, cid, vol, scan):
        """[(line number, line id, certainty, length of the text, text Loghi read)] of a scan, kept until its
        PageXML changes."""
        try:
            stamp = self._stamp(self.srv.vol_path(cid, vol) / "page" / f"{scan}.xml")
        except KeyError:
            return []
        if stamp is None:
            return []
        key = (cid, vol, scan)
        hit = self._lines.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
        try:
            rows = [(l["n"], l.get("id"), l["conf"], len(l["text"]), l["text"])
                    for l in self.srv.read_page(cid, vol, scan, raw=True)["lines"] if l.get("id") and l.get("poly")]
        except Exception:               # a damaged PageXML: this scan has nothing to check
            rows = []
        self._lines[key] = (stamp, rows)
        return rows

    def _done(self, cid, vol):
        out = set()
        for scan, recs in self._load(cid, vol).items():
            ids = {r[1] for r in self._scan_lines(cid, vol, scan)}
            if all(lid in ids for lid in recs):
                out.update((scan, lid) for lid in recs)
            else:                       # read again since the check: the checks moved to the new lines
                try:
                    out.update((scan, l["id"]) for l in self.srv.read_page(cid, vol, scan)["lines"] if l.get("checked"))
                except (KeyError, ValueError, OSError):
                    pass
        return out

    def next(self, cid, vol, scan=None, n=0, order="page"):
        """The first line after (scan, n) that nobody checked yet, or None.

        order "page": in reading order through the book. order "unsure": the lines Loghi is least sure of
        first, through the whole book. order "names": lines with a name of the index that was nearly read right.
        order "differ": lines that the second reading reads differently, the smallest difference first."""
        if scan and not re.fullmatch(self.srv.NAME_RE, scan):
            raise KeyError("invalid scan name")
        scans = [s["id"] for s in self.srv.list_scans(cid, vol) if s["txt"]]
        done = self._done(cid, vol)
        if order == "unsure":
            rows = [(conf, s, ln, lid) for s in scans for ln, lid, conf, size, _ in self._scan_lines(cid, vol, s)
                    if conf is not None and size >= MIN_LEN]
            rows.sort()
            after = None
            if scan:
                cur = next((r for r in self._scan_lines(cid, vol, scan) if r[0] == n), None)
                after = (cur[2] if cur and cur[2] is not None else -1.0, scan, n)
            for conf, s, ln, lid in rows:
                if (s, lid) not in done and (after is None or (conf, s, ln) > after):
                    return {"scan": s, "n": ln}
            return None
        if order == "names":
            for s, ln, lid in self._near_names(cid, vol, scans):
                if (s, lid) not in done and (not scan or (s, ln) > (scan, n)):
                    return {"scan": s, "n": ln}
            return None
        if order == "differ":
            rows = self._differ(cid, vol, scans)
            after = None
            if scan:
                cur = next((r for r in rows if r[1] == scan and r[2] == n), None)
                after = (cur[0] if cur else -1.0, scan, n)
            for share, s, ln, lid in rows:
                if (s, lid) not in done and (after is None or (share, s, ln) > after):
                    return {"scan": s, "n": ln}
            return None
        if order != "page":
            raise ValueError("Unknown order.")
        start = scans.index(scan) if scan in scans else 0
        for s in scans[start:]:
            for ln, lid, conf, size, _ in self._scan_lines(cid, vol, s):
                if s == scan and ln <= n:
                    continue
                if (s, lid) not in done:
                    return {"scan": s, "n": ln}
        return None

    # ------------------------------------------------------------ the index of names made by people
    def _index(self, cid):
        """{book: {scan: [deed]}}: the deeds that start on a scan, with the people named in them."""
        path = self.srv.COLL.get(cid).get("index")
        if not path:
            return {}
        f = Path(path) if Path(path).is_absolute() else self.srv.HERE / path
        stamp = self._stamp(f)
        if stamp is None:
            return {}
        hit = self._idx.get(cid)
        if hit and hit[0] == stamp:
            return hit[1]
        import names
        out, flat = {}, []
        try:
            deeds = json.loads(f.read_text(encoding="utf-8")).get("deeds", {})
        except (OSError, ValueError, AttributeError):
            deeds = {}
        for d in deeds.values():
            people = [{"name": p["name"], "role": (p.get("role") or "").replace("other:", "") or None, "place": p.get("place")}
                      for p in d.get("people") or [] if p.get("name")]
            if len(people) < 2:         # a single entry is the description of the book, not a deed
                continue
            date = [str(x) for x in d.get("date") or [] if x]
            deed = {"date": "-".join(reversed(date)), "folio": d.get("folio"), "people": people}
            scans = [w for w in d.get("scans") or [] if len(w) == 2]
            for where in scans:
                out.setdefault(where[0], {}).setdefault(where[1], []).append(deed)
            part = str(d.get("part") or "")
            flat.append(dict(deed, kind=d.get("book"), part=part,
                             vol=scans[0][0] if scans else part.zfill(4) if part.isdigit() else None,
                             scan=scans[0][1] if scans else None,
                             sort=("-".join(x.zfill(2) for x in date), part),
                             keys=[{names.fold(self.srv.norm(w)) for w in WORD_ANY.findall(p["name"])} - {""} for p in people]))
        flat.sort(key=lambda x: x["sort"])
        self._idx[cid] = (stamp, out)
        self._all[cid] = flat
        return out

    def index_search(self, cid, q, vol=None, limit=40):
        """Deeds of the index that name the people asked for: every word of the question must be in the name of
        somebody in the deed, in any old spelling the name finder knows, or one or two letters off. Deeds in
        which one person carries all the words come first. None when the collection has no index."""
        if not self._index(cid):
            return None
        import names
        toks = WORD_ANY.findall((q or "").lower())
        toks = [t for t in toks if t not in names.PARTICLES] or toks
        tests = []
        for t in toks:
            base = names.fold(self.srv.norm(t))
            if base:
                tests.append(({names.fold(self.srv.norm(x)) for x in names.word_forms(t)}, base, names.max_edits(base)))
        if not tests:
            return {"total": 0, "results": []}

        def fits(w, test):
            forms, base, k = test
            return w in forms or bool(k and abs(len(w) - len(base)) <= k and self.srv.lev_le(base, w, k))
        found = []
        for d in self._all.get(cid, []):
            if vol and d["vol"] != vol:
                continue
            per = [[any(fits(w, t) for w in ks) for t in tests] for ks in d["keys"]]
            if all(any(p[i] for p in per) for i in range(len(tests))):
                found.append((not any(all(p) for p in per), d, [any(p) for p in per]))
        found.sort(key=lambda x: (x[0], x[1]["sort"]))
        return {"total": len(found), "results": [
            {"vol": d["vol"], "scan": d["scan"], "part": d["part"], "kind": d["kind"], "date": d["date"], "folio": d["folio"],
             "one": not apart, "people": [dict(p, hit=h) for p, h in zip(d["people"], hits)]}
            for apart, d, hits in found[:limit]]}

    @staticmethod
    def _before(scan):
        return f"{int(scan) - 1:0{len(scan)}d}" if scan.isdigit() and int(scan) > 0 else None

    def index_names(self, cid, vol, scan):
        """The deeds of the index that start on this scan, and on the scan before (a deed often runs on)."""
        book = self._index(cid).get(vol, {})
        return {"here": book.get(scan, []), "before": book.get(self._before(scan or ""), [])}

    def _near_names(self, cid, vol, scans):
        """Lines that hold a word nearly, but not exactly, like a word of a name the index gives for the scan."""
        book = self._index(cid).get(vol, {})
        if not book:
            return []
        try:
            stamps = (self._idx[cid][0], self._stamp(self.srv.vol_path(cid, vol) / "page"), len(scans))
        except KeyError:
            return []
        hit = self._near.get((cid, vol))
        if hit and hit[0] == stamps:
            return hit[1]
        import names

        def key(w):
            return names.fold(self.srv.norm(w))
        rows = []
        for s in scans:
            deeds = book.get(s, []) + book.get(self._before(s), [])
            keys = {key(w) for d in deeds for p in d["people"] for w in WORD.findall(p["name"])}
            keys.discard("")
            if not keys:
                continue
            for ln, lid, conf, size, text in self._scan_lines(cid, vol, s):
                words = {key(w) for w in WORD.findall(text)} - keys
                if any(abs(len(w) - len(k)) <= 2 and self.srv.lev_le(w, k, 1 if len(k) <= 6 else 2) for w in words for k in keys):
                    rows.append((s, ln, lid))
        self._near[(cid, vol)] = (stamps, rows)
        return rows

    # ------------------------------------------------------------ a second reading of the same scans
    def _second(self, cid, vol, scan):
        """(PageXML of the second reading of a scan, kind) or (None, None). kind "other": a file in
        <cache>/second/<collection>/<book>/page/. kind "before": the text the book had before it was read
        again (page/<scan>.xml.bak, left by the reading queue and by tools_runpod_apply.py)."""
        f = self.second_dir / cid / vol / "page" / f"{scan}.xml"
        if f.exists():
            return f, "other"
        f = self.srv.vol_path(cid, vol) / "page" / f"{scan}.xml.bak"
        return (f, "before") if f.exists() else (None, None)

    def second_books(self, cid):
        """{book: "other" or "before"} for the books of a collection that have a second reading."""
        hit = self._second_books.get(cid)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        out = {}
        try:
            vols = [v["name"] for v in self.srv.list_volumes(cid)]
        except (KeyError, OSError):
            vols = []
        for vol in vols:
            try:
                if (self.second_dir / cid / vol / "page").is_dir():
                    out[vol] = "other"
                else:
                    with os.scandir(self.srv.vol_path(cid, vol) / "page") as it:
                        if any(e.name.endswith(".xml.bak") for e in it):
                            out[vol] = "before"
            except (KeyError, OSError):
                pass
        self._second_books[cid] = (time.time(), out)
        return out

    def _other_lines(self, cid, vol, scan):
        """({line number in the book: text of the second reading}, kind) for a scan. A line of the second
        reading belongs to the line of the book with the same id at the same place, else to the line it
        overlaps most."""
        f, kind = self._second(cid, vol, scan)
        if f is None:
            return {}, None
        stamps = (self._stamp(f), self._stamp(self.srv.vol_path(cid, vol) / "page" / f"{scan}.xml"))
        key = (cid, vol, scan)
        hit = self._other.get(key)
        if hit and hit[0] == stamps:
            return hit[1], kind
        out = {}
        try:
            import xml.etree.ElementTree as ET
            root = ET.parse(f).getroot()
            ns = root.tag.split("}")[0] + "}"
            page = self.srv.read_page(cid, vol, scan, raw=True)
            el = root.find(ns + "Page")
            theirs_w = int(el.get("imageWidth", 0)) if el is not None else 0
            fx = page["width"] / theirs_w if page.get("width") and theirs_w else 1.0     # read at another size
            theirs = []
            for line in root.iter(ns + "TextLine"):
                eqs = line.findall(ns + "TextEquiv")
                u = eqs[-1].find(ns + "Unicode") if eqs else None
                c = line.find(ns + "Coords")
                box = bbox(c.get("points")) if c is not None else None
                if u is not None and u.text and u.text.strip() and box:
                    theirs.append((line.get("id"), tuple(v * fx for v in box), re.sub(r"\s+", " ", u.text).strip()))
            by_id = {t[0]: t for t in theirs}
            for l in page["lines"]:
                if not l.get("id") or not l.get("poly"):
                    continue
                box = bbox(l["poly"])
                t = by_id.get(l["id"])
                if not t or overlap(box, t[1]) < SAME_PLACE:
                    t = max(theirs, key=lambda x: overlap(box, x[1]), default=None)
                if t and overlap(box, t[1]) >= SAME_PLACE:
                    out[l["n"]] = t[2]
        except Exception:               # a damaged file: no second reading for this scan
            out = {}
        self._other[key] = (stamps, out)
        return out, kind

    def other(self, cid, vol, scan):
        """The second reading of the lines of a scan: {"kind": ..., "lines": {line number: text}}."""
        if not re.fullmatch(self.srv.NAME_RE, scan or ""):
            raise KeyError("invalid scan name")
        self._file(cid, vol)
        lines, kind = self._other_lines(cid, vol, scan)
        return {"kind": kind, "lines": {str(n): t for n, t in lines.items()}}

    def _differ(self, cid, vol, scans):
        """[(share of the letters that differ, scan, line number, line id)] for the lines the two readings
        read differently, the smallest difference first."""
        self._file(cid, vol)
        stamps = (self._stamp(self.second_dir / cid / vol / "page"), self._stamp(self.srv.vol_path(cid, vol) / "page"), len(scans))
        hit = self._diff.get((cid, vol))
        if hit and hit[0] == stamps:
            return hit[1]
        rows = []
        for s in scans:
            other, _ = self._other_lines(cid, vol, s)
            if not other:
                continue
            for ln, lid, conf, size, text in self._scan_lines(cid, vol, s):
                a, b = re.sub(r"\s+", " ", text).strip(), other.get(ln)
                if b is None or a == b or max(len(a), len(b)) < MIN_LEN:
                    continue
                i = 0                   # the same start and end need no comparing: most lines differ in a few letters
                while i < min(len(a), len(b)) and a[i] == b[i]:
                    i += 1
                j = 0
                while j < min(len(a), len(b)) - i and a[-1 - j] == b[-1 - j]:
                    j += 1
                d = distance(a[i:len(a) - j], b[i:len(b) - j])
                rows.append((d / max(len(a), len(b)), s, ln, lid))
        rows.sort()
        self._diff[(cid, vol)] = (stamps, rows)
        return rows

    # ------------------------------------------------------------ the training set
    def export_state(self, cid=None):
        with self.lock:
            job = dict(self.job)
        folder = self.training_dir / (cid or job["collection"] or "_")
        try:
            job["last"] = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            job["last"] = None
        job["folder"] = str(folder) if cid else None
        return job

    def start_export(self, cid):
        self.srv.COLL.get(cid)
        with self.lock:
            if self.job["running"]:
                raise ValueError("A training set is being made already. Wait until it is ready.")
            self.job = {"running": True, "collection": cid, "state": "running",
                        "message": "Collecting the checked lines.", "finished": None}
        threading.Thread(target=self._export_job, args=(cid,), daemon=True).start()
        return self.export_state(cid)

    def _export_job(self, cid):
        try:
            man = self.export(cid)
            state = "done" if man["cut"] else "no docker"
            msg = (f"Ready: {man['lines']} training lines from {man['scans']} scans." if man["cut"] else
                   f"The scans and texts of {man['checked']} checked lines are ready, but the lines were not cut out: "
                   f"{man['cut_error']}")
        except Exception as e:
            state, msg = "failed", f"Making the training set failed: {str(e).strip(chr(39) + chr(34))}"
        with self.lock:
            self.job.update(running=False, state=state, message=msg, finished=time.time())

    def _say(self, msg):
        with self.lock:
            self.job["message"] = msg

    def export(self, cid, cut=True):
        """Make the training set of a collection again from all its checked lines. Returns the manifest."""
        import readqueue
        col = self.srv.COLL.get(cid)
        out = self.training_dir / cid
        if out.exists() and any(out.iterdir()) and not (out / "manifest.json").exists():
            raise ValueError(f"The folder {out} holds other files; it was not made by Loghi-studio.")
        out.mkdir(parents=True, exist_ok=True)
        for sub in ("gt", "lines", "tmp"):
            shutil.rmtree(out / sub, ignore_errors=True)
        gt = out / "gt"
        (gt / "page").mkdir(parents=True)
        (out / "tmp").mkdir()
        man = {"collection": cid, "title": col.get("title"), "made": time.strftime("%Y-%m-%d %H:%M:%S"),
               "private": bool(col.get("private")), "loghi": readqueue.LOGHI_VERSION, "mount": MOUNT,
               "books": {}, "checked": 0, "scans": 0, "no_baseline": 0, "lines": 0, "train": 0, "validation": 0,
               "cut": False, "cut_error": None}
        (out / "manifest.json").write_text(json.dumps(man, indent=1), encoding="utf-8")
        own = out / OWN_USE
        if man["private"]:
            own.write_text("The scans of this collection are for own use only.\n"
                           "Do not share this folder, the line images or a model with these line images in it "
                           "without asking the owner of the scans.\n", encoding="utf-8")
        elif own.exists():
            own.unlink()
        for vol in self.books(cid):
            try:
                book = self.srv.vol_path(cid, vol)
            except KeyError:
                continue
            files = {s["id"]: s["file"] for s in self.srv.list_scans(cid, vol)}
            enlarged = readqueue.read_marker(book).get("enlarged", {})
            n_book = 0
            for scan in sorted(self._load(cid, vol)):
                if scan not in files:
                    continue
                page = self.srv.read_page(cid, vol, scan)
                lines = [l for l in page["lines"] if l.get("checked") in ("corrected", "confirmed") and l.get("poly")]
                man["no_baseline"] += sum(1 for l in lines if not l.get("base"))
                lines = [l for l in lines if l.get("base")]
                if not lines or not page["width"] or not page["height"]:
                    continue
                factor = int(enlarged.get(scan) or 1)
                stem = f"{vol}_{scan}"
                self._say(f"Collecting the checked lines: book {vol}, scan {scan}.")
                if factor > 1:
                    readqueue.enlarge_scan(book / files[scan], gt / f"{stem}.jpg", factor)
                else:
                    shutil.copyfile(book / files[scan], gt / f"{stem}.jpg")
                rows = "".join(
                    f'<TextLine id={quoteattr(l["id"])}><Coords points="{scale_points(l["poly"], factor)}"/>'
                    f'<Baseline points="{scale_points(l["base"], factor)}"/>'
                    f'<TextEquiv><Unicode>{escape(l["text"])}</Unicode></TextEquiv></TextLine>\n' for l in lines)
                (gt / "page" / f"{stem}.xml").write_text(
                    f'<?xml version="1.0" encoding="UTF-8"?>\n<PcGts xmlns="{PAGE_NS}">\n'
                    f'<Page imageFilename="{stem}.jpg" imageWidth="{page["width"] * factor}" '
                    f'imageHeight="{page["height"] * factor}">\n<TextRegion id="region_checked">\n'
                    f'<Coords points="0,0 {page["width"] * factor},0 {page["width"] * factor},{page["height"] * factor} '
                    f'0,{page["height"] * factor}"/>\n{rows}</TextRegion>\n</Page>\n</PcGts>\n', encoding="utf-8", newline="\n")
                man["scans"] += 1
                man["checked"] += len(lines)
                n_book += len(lines)
            if n_book:
                man["books"][vol] = n_book
        if cut and man["checked"]:
            self._cut(out, man)
        elif cut:
            man["cut_error"] = "There are no checked lines yet."
        (out / "README.txt").write_text(self._readme(man), encoding="utf-8", newline="\n")
        (out / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
        shutil.rmtree(out / "tmp", ignore_errors=True)
        return man

    def _cut(self, out, man):
        """Cut the line images with Loghi's own tool and write the list files."""
        import readqueue
        q = self.srv.QUEUE
        engine = q.engine if q else readqueue.DockerEngine()
        err = engine.available()
        if err:
            man["cut_error"] = err
            return
        channels = 4
        try:
            cfg = json.loads((Path(q.data["models_dir"]) / q.data["last_model"] / "config.json").read_text(encoding="utf-8"))
            channels = int(cfg.get("channels") or cfg.get("args", {}).get("channels") or 4)
        except (AttributeError, KeyError, OSError, ValueError, TypeError):
            pass
        cpus = max(1, min(4, q.cpus_now() if q else 2))
        self._say(f"Cutting out {man['checked']} lines with Loghi (Docker).")
        log = out / "cut.log"
        rc = engine.run(readqueue.PREFIX + "traincut", readqueue.IMG_TOOL, [],
                        [f"{readqueue.BIN}/MinionCutFromImageBasedOnPageXMLNew", "-input_path", f"{MOUNT}/gt",
                         "-outputbase", f"{MOUNT}/lines/", "-tmpdir", f"{MOUNT}/tmp", "-output_type", "png",
                         "-channels", str(channels), "-threads", str(cpus), "-write_text_contents", "-no_page_update",
                         "-no_text_line_contour_recalculation", "-skip_unclear", readqueue.NS],
                        out, cpus, log)
        if rc != 0:
            man["cut_error"] = f"The cutting step of Loghi failed; see {log}."
            return
        rows = []
        for png in sorted((out / "lines").rglob("*.png")):
            txt = png.with_suffix(".txt")
            if txt.exists():
                text = re.sub(r"\s+", " ", txt.read_text(encoding="utf-8")).strip()
                if text:
                    rows.append((png.relative_to(out).as_posix(), text))
        train, val = [], []
        for rel, text in rows:
            part = int(hashlib.sha1(rel.encode("utf-8")).hexdigest(), 16) % VAL_PART
            (val if part == 0 else train).append(f"{MOUNT}/{rel}\t{text}\n")
        for name, part in (("training_all.txt", sorted(train + val)), ("training_all_train.txt", train),
                           ("training_all_val.txt", val)):
            (out / name).write_text("".join(part), encoding="utf-8", newline="\n")
        man.update(cut=True, channels=channels, lines=len(rows), train=len(train), validation=len(val))

    @staticmethod
    def _readme(man):
        return (
            f"Training lines from Loghi-studio, collection {man['title']} ({man['collection']}), made {man['made']}.\n\n"
            "Every line was read on the scan by a person. Nothing here was guessed by the computer.\n\n"
            "gt/                      scans (enlarged in the same way as they were read) and gt/page/*.xml:\n"
            "                         PageXML with only the checked lines. This is the input of Loghi's\n"
            "                         scripts/create-train-data.sh.\n"
            "lines/                   line images cut by MinionCutFromImageBasedOnPageXMLNew of Loghi "
            f"{man['loghi']}, each with its text.\n"
            "training_all.txt         <path of the line image><tab><text>, all lines.\n"
            "training_all_train.txt   the lines to train on (about nine in ten).\n"
            "training_all_val.txt     the lines to validate on (about one in ten, always the same lines).\n\n"
            f"The paths in the lists start with {man['mount']}/: mount this folder there in the Loghi container\n"
            "(docker run -v <this folder>:/work ...), as scripts/htr-train-pipeline.sh does with its listdir.\n\n"
            f"Lines: {man['lines']} ({man['train']} to train, {man['validation']} to validate), from {man['scans']} scans.\n"
            + ("\nOWN USE ONLY: the scans of this collection may not be shared. See OWN-USE-ONLY.txt.\n" if man["private"] else ""))
