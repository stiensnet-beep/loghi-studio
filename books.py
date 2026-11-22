"""Adding books (volumes) to a collection, as background jobs.

Two kinds of job:
- "folder":  copy the scans (*.jpg) from a folder into a new book folder of the collection;
- "tresoar": download the scans of one inventory number from the Tresoar image server.

Jobs run one at a time in a background thread and are kept in settings/jobs.json, so that an
interrupted job continues after a restart (files that are already there are skipped).
Only new book folders are created; existing files are never changed or deleted.
"""
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MIN_FREE_GB = 25            # stop before the disk is full
USER_AGENT = "Loghi-studio (local archive research)"
TRESOAR = "https://tresoar-images.memorix.nl/frl/thumb/{size}x{size}/"
SIZES = (1600, 2000, 3000)
IMG_EXT = (".jpg", ".jpeg")
OPEN_STATES = ("waiting", "running")


def tresoar_url(access, inv, scan, size, digits=6):
    """Scan numbers have 6 digits in most finding aids, 5 in some (e.g. 13-23 Kollumerland)."""
    n = (f"NL-0400410000/NL-0400410000_{access}/NL-0400410000_{access}_{inv:04d}/"
         f"NL-0400410000_{access}_{inv:04d}_{scan:0{digits}d}.jp2")
    return TRESOAR.format(size=size) + urllib.parse.quote(n, safe="")


def http_get(url, timeout=120):
    """Bytes of the url, False when it does not exist (404), None on network trouble."""
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            return data if len(data) > 200 else False
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            time.sleep(2 * (attempt + 1))
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None


class Stopped(Exception):
    pass


class Jobs:
    def __init__(self, srv, settings_dir, fetch=http_get, min_free_gb=MIN_FREE_GB):
        self.srv = srv
        self.file = Path(settings_dir) / "jobs.json"
        self.fetch = fetch
        self.min_free_gb = min_free_gb
        self.lock = threading.RLock()
        self.jobs = []
        self.wake = threading.Event()
        self.thread = None
        self.cancelled = set()
        if self.file.exists():
            try:
                self.jobs = json.loads(self.file.read_text(encoding="utf-8")).get("jobs", [])
            except ValueError:
                self.jobs = []
            for j in self.jobs:
                if j["state"] == "running":  # interrupted by a restart
                    j["state"] = "waiting"

    # ------------------------------------------------------------ bookkeeping
    def save(self):
        with self.lock:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"jobs": self.jobs[-200:]}, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.file)

    def public(self):
        with self.lock:
            return [dict(j) for j in reversed(self.jobs)]

    def _book_dir(self, cid, book):
        if not isinstance(book, str) or not re.fullmatch(self.srv.NAME_RE, book) or book in (".", "..") \
                or book in self.srv.SKIP_DIRS or len(book) > 60:
            raise ValueError("The name of the book may only contain letters, digits, dot, hyphen and underscore.")
        root = self.srv.COLL.root(cid)
        if not root.is_dir():
            raise ValueError("The folder of this collection does not exist.")
        return root / book

    def _check_new(self, cid, book):
        d = self._book_dir(cid, book)
        with self.lock:
            if any(j["collection"] == cid and j["book"] == book and j["state"] in OPEN_STATES for j in self.jobs):
                raise ValueError(f"Book {book} is already being added.")
        if d.exists() and any(e.name.lower().endswith(IMG_EXT) for e in os.scandir(d)):
            raise ValueError(f"Book {book} already exists in this collection. Choose another name.")
        return d

    def _new(self, kind, cid, book, **extra):
        job = {"id": uuid.uuid4().hex[:10], "kind": kind, "collection": cid, "book": book, "state": "waiting",
               "done": 0, "total": None, "message": "Waiting for its turn.", "created": time.time(), **extra}
        with self.lock:
            self.jobs.append(job)
            self.save()
        self.wake.set()
        return dict(job)

    def add_folder(self, cid, book, path, read_model=None):
        src = Path(path or "")
        if not path or not src.is_dir():
            raise ValueError(f"This folder does not exist: {path}")
        files = [e for e in os.scandir(src) if e.is_file() and e.name.lower().endswith(IMG_EXT)]
        if not files:
            raise ValueError("There are no scans (jpg files) in this folder.")
        dest = self._check_new(cid, book)
        if dest.resolve() == src.resolve():
            raise ValueError("This folder already is a book in this collection.")
        need = sum(e.stat().st_size for e in files) / 1e9
        free = shutil.disk_usage(dest.parent).free / 1e9
        if free - need < self.min_free_gb:
            raise ValueError(f"Not enough disk space: {need:.1f} GB needed, {free:.0f} GB free ({self.min_free_gb} GB must stay free).")
        return self._new("folder", cid, book, source=str(src), total=len(files), read_model=read_model)

    def add_tresoar(self, cid, access, inv, book=None, size=2000, read_model=None):
        if not isinstance(access, str) or not re.fullmatch(r"\d{1,3}(-\d{1,3}[a-z]?)?", access.strip()):
            raise ValueError("The finding aid number (toegang) must look like 13-07.")
        try:
            inv = int(str(inv).strip())
        except ValueError:
            raise ValueError("The inventory number must be a number.")
        if not 1 <= inv <= 9999:
            raise ValueError("The inventory number must be between 1 and 9999.")
        if int(size) not in SIZES:
            raise ValueError("Unknown scan size.")
        book = (book or "").strip() or f"{inv:04d}"
        self._check_new(cid, book)
        return self._new("tresoar", cid, book, access=access.strip(), inv=inv, size=int(size), read_model=read_model)

    def cancel(self, jid):
        with self.lock:
            job = self._get(jid)
            if job["state"] not in OPEN_STATES:
                raise ValueError("This job is no longer running.")
            self.cancelled.add(jid)
            if job["state"] == "waiting":
                job.update(state="stopped", message="Stopped before the job began.")
                self.save()
            return dict(job)

    def _get(self, jid):
        for j in self.jobs:
            if j["id"] == jid:
                return j
        raise KeyError("unknown job")

    # ------------------------------------------------------------ running
    def _set(self, job, **kw):
        with self.lock:
            job.update(kw)

    def _guard(self, job, dest):
        if job["id"] in self.cancelled:
            raise Stopped("Stopped at your request. What has arrived is kept.")
        if shutil.disk_usage(dest.parent).free / 1e9 < self.min_free_gb:
            raise Stopped(f"Stopped: less than {self.min_free_gb} GB of disk space is free.")

    def _run_folder(self, job, dest):
        src = Path(job["source"])
        files = sorted(e.name for e in os.scandir(src) if e.is_file() and e.name.lower().endswith(IMG_EXT))
        self._set(job, total=len(files))
        dest.mkdir(exist_ok=True)
        for i, name in enumerate(files):
            self._guard(job, dest)
            target = dest / name
            if not target.exists() or target.stat().st_size != (src / name).stat().st_size:
                tmp = dest / (name + ".part")
                shutil.copyfile(src / name, tmp)
                os.replace(tmp, target)
            self._set(job, done=i + 1, message=f"Copying: {i + 1} of {len(files)} scans.")
        return f"Done: {len(files)} scans copied. The original folder was not changed."

    def _exists(self, job, scan):
        return self.fetch(tresoar_url(job["access"], job["inv"], scan, 80, job.get("digits", 6)), 30)

    def _count(self, job):
        """Number of scans online (binary search with tiny images); 0 = not online, None = network trouble."""
        first = None
        for digits in (6, 5, 4):
            job["digits"] = digits
            first = self._exists(job, 1)
            if first is None:
                return None
            if first:
                break
        if first is False:
            return 0
        lo, hi = 1, 2
        while self._exists(job, hi):
            lo, hi = hi, hi * 2
            if hi > 20000:
                break
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self._exists(job, mid):
                lo = mid
            else:
                hi = mid
        return lo

    def _run_tresoar(self, job, dest):
        self._set(job, message="Counting the scans that are online.")
        total = self._count(job)
        if total is None:
            raise RuntimeError("The image server of Tresoar cannot be reached. Try again later.")
        if total == 0:
            raise RuntimeError(f"Finding aid {job['access']}, inventory number {job['inv']} is not online at Tresoar.")
        self._set(job, total=total)
        dest.mkdir(exist_ok=True)
        failed = []

        def one(scan):
            target = dest / f"{scan:06d}.jpg"
            if target.exists() and target.stat().st_size > 1000:
                return True
            data = self.fetch(tresoar_url(job["access"], job["inv"], scan, job["size"], job.get("digits", 6)))
            if not data:
                return False
            tmp = dest / f"{scan:06d}.jpg.part"
            tmp.write_bytes(data)
            os.replace(tmp, target)
            return True

        with ThreadPoolExecutor(4) as ex:
            for start in range(1, total + 1, 8):
                self._guard(job, dest)
                batch = list(range(start, min(start + 8, total + 1)))
                for scan, ok in zip(batch, ex.map(one, batch)):
                    if not ok:
                        failed.append(scan)
                self._set(job, done=batch[-1], message=f"Downloading: {batch[-1]} of {total} scans.")
        if failed:
            raise RuntimeError(f"{len(failed)} of {total} scans failed (first: scan {failed[0]}). "
                               "Add the book again to fetch only the missing scans.")
        (dest / ".dl_done").write_text(str(total))
        return f"Done: {total} scans downloaded."

    def _run(self, job):
        self._set(job, state="running", message="Running.")
        try:
            dest = self._book_dir(job["collection"], job["book"])
            msg = self._run_folder(job, dest) if job["kind"] == "folder" else self._run_tresoar(job, dest)
            self.srv._vol_cache.pop(job["collection"], None)
            if job.get("read_model"):
                try:
                    self.srv.QUEUE.add(job["collection"], job["book"], job["read_model"])
                    msg += " The book is now in the reading queue."
                except (KeyError, ValueError) as e:
                    msg += f" It could not be put in the reading queue: {str(e).strip(chr(39))}"
            else:
                msg += " The book is now in the list with the state to do."
            self._set(job, state="done", message=msg)
        except Stopped as e:
            self._set(job, state="stopped", message=str(e))
        except Exception as e:
            self._set(job, state="failed", message=str(e).strip("'\""))
        self.cancelled.discard(job["id"])
        self.srv._vol_cache.pop(job["collection"], None)
        self.save()

    def run_pending(self):
        while True:
            with self.lock:
                job = next((j for j in self.jobs if j["state"] == "waiting"), None)
            if job is None:
                return
            self._run(job)

    def _loop(self):
        while True:
            self.wake.wait(60)
            self.wake.clear()
            self.run_pending()

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
        self.wake.set()
