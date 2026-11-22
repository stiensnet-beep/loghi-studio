"""The reading queue: books waiting to be read by Loghi, one at a time.

A book is read in small chunks of scans (default 20). Each chunk gets its own work folder
(<book>/.loghi-studio-work) and goes through the six Loghi steps in Docker; the finished PageXML and
text are then moved into <book>/page and <book>/txt. So:
- after a stop, a crash or a reboot, only the chunk that was running is read again (resume per scan);
- a scan that fails is tried once more on its own, and skipped after the second failure;
- progress and the expected finishing time are known while a book is being read.

Pausing freezes the running container (docker pause); resuming continues exactly where it was.
Containers started here are named loghi-studio-*; other containers are never touched. While another
Loghi run is active on this computer, the queue waits.

State is kept in settings/queue.json. Scans are never changed. An existing page/<scan>.xml is only
replaced when that scan is read again (after a failure, or on request); the old file is kept as
<scan>.xml.bak.

Reading again, enlarged: on a scan of a large page with small writing the line finder misses most lines.
A queue item with "enlarge": 2 reads all scans of the book again from a copy that is twice as large; the
coordinates in the PageXML are scaled back to the scan. The marker file lists the scans done this way.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

LOGHI_VERSION = "2.3.2"
IMG_LAYPA = f"loghi/docker.laypa:{LOGHI_VERSION}"
IMG_HTR = f"loghi/docker.htr:{LOGHI_VERSION}"
IMG_TOOL = f"loghi/docker.loghi-tooling:{LOGHI_VERSION}"
BIN = "/src/loghi-tooling/minions/target/appassembler/bin"
NS = "-use_2013_namespace"
MEM = ["-m", "16g", "--shm-size", "8g"]
PREFIX = "loghi-studio-"
WORK_DIR = ".loghi-studio-work"
MARKER = ".loghi-studio.json"
LINE_MODEL = "baseline2"
OPEN_STATES = ("waiting", "running")
NIGHT_HOURS = (23, 7)       # the night thread count applies from 23:00 to 07:00
STEPS = {1: "finding the lines on the page", 2: "extracting the baselines", 3: "cutting out the lines",
         4: "reading the lines", 5: "merging text and reading order", 6: "writing the text files"}


class Stopped(Exception):
    pass


class DockerGone(Exception):
    """Docker stopped answering while a step was running; nothing is wrong with the scans."""


# ---------------------------------------------------------------- PageXML helpers

def page_lines(xml):
    """(lines found, lines that went through the reading step, plain text) of a PageXML file.

    The text has the same line order as the old page2txt.py. A line that was read has a TextEquiv
    element, even when Loghi read nothing in it; a page with lines but no TextEquiv at all was not read."""
    root = ET.parse(xml).getroot()
    ns = root.tag.split("}")[0] + "}"
    out, lines, read = [], 0, 0
    for region in root.iter(ns + "TextRegion"):
        for line in region.findall(ns + "TextLine"):
            lines += 1
            eqs = line.findall(ns + "TextEquiv")
            read += bool(eqs)
            u = eqs[-1].find(ns + "Unicode") if eqs else None
            if u is not None and u.text:
                out.append(u.text)
        out.append("")
    return lines, read, "\n".join(out).strip() + "\n"


def read_marker(book):
    try:
        data = json.loads((Path(book) / MARKER).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_marker(book, data):
    p = Path(book) / MARKER
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def shrink_page(xml, factor):
    """Scale the coordinates of a PageXML made from an enlarged copy back to the scan itself."""
    def pts(m):
        out = []
        for p in m.group(1).split():
            x, _, y = p.partition(",")
            out.append(f"{round(float(x) / factor)},{round(float(y) / factor)}")
        return 'points="' + " ".join(out) + '"'
    s = Path(xml).read_text(encoding="utf-8")
    s = re.sub(r'points="([^"]*)"', pts, s)
    s = re.sub(r'(imageWidth|imageHeight)="(\d+)"', lambda m: f'{m.group(1)}="{round(int(m.group(2)) / factor)}"', s)
    Path(xml).write_text(s, encoding="utf-8", newline=chr(10))


def enlarge_scan(src, dst, factor):
    from PIL import Image
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.resize((im.width * factor, im.height * factor), Image.LANCZOS).save(dst, "JPEG", quality=92)


def scan_state(book, img_ext=(".jpg", ".jpeg"), enlarge=0):
    """What still has to be read in a book folder.

    Returns {"scans": {stem: file name}, "todo": [stems], "failed_before": [stems], "skipped": [stems]}.
    - todo: no text file yet, or an empty text file while the lines on the page never reached the reading
      step (a failed read);
    - failed_before: the part of todo that failed in an earlier run;
    - skipped: failed twice in this program and given up (listed in the marker file).
    """
    book = Path(book)
    scans = {e.name.rsplit(".", 1)[0]: e.name for e in os.scandir(book)
             if e.is_file() and e.name.lower().endswith(img_ext)}
    sizes = {}
    tdir = book / "txt"
    if tdir.is_dir():
        sizes = {e.name[:-4]: e.stat().st_size for e in os.scandir(tdir) if e.name.endswith(".txt")}
    marker = read_marker(book)
    given_up = marker.get("failed", {})
    todo, failed_before, skipped = [], [], []
    if enlarge:     # read again, enlarged: everything that was not yet read this way
        done = marker.get("enlarged", {})
        for stem in sorted(scans):
            (skipped if stem in given_up else todo if done.get(stem) != enlarge else []).append(stem)
        return {"scans": scans, "todo": todo, "failed_before": [], "skipped": skipped}
    for stem in sorted(scans):
        if stem in given_up:
            skipped.append(stem)
        elif stem not in sizes:
            todo.append(stem)
        elif sizes[stem] <= 2:
            xml = book / "page" / f"{stem}.xml"
            try:
                lines, read, _ = page_lines(xml) if xml.exists() else (0, 0, "")
            except ET.ParseError:
                lines, read = 1, 0
            if lines and not read:
                todo.append(stem)
                failed_before.append(stem)
    return {"scans": scans, "todo": todo, "failed_before": failed_before, "skipped": skipped}


# ---------------------------------------------------------------- Docker

class DockerEngine:
    """Thin layer over the docker command line; replaced by a fake in the tests."""

    def __init__(self):
        self.exe = shutil.which("docker") or r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
        self.flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def _call(self, *args, timeout=60):
        try:
            r = subprocess.run([self.exe, *args], capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL, creationflags=self.flags)
            return r.returncode, r.stdout
        except (OSError, subprocess.TimeoutExpired):
            return 1, ""

    def available(self):
        """None when Docker answers, otherwise a sentence for the user."""
        rc, _ = self._call("version", "--format", "{{.Server.Version}}", timeout=30)
        return None if rc == 0 else "Docker is not running. Start Docker Desktop; the queue continues by itself."

    def others(self):
        """True while a Loghi container that was not started by Loghi-studio is running."""
        rc, out = self._call("ps", "--format", "{{.Names}}\t{{.Image}}")
        if rc:
            return False
        for line in out.splitlines():
            name, _, image = line.partition("\t")
            if image.startswith("loghi/docker.") and not name.startswith(PREFIX):
                return True
        return False

    def run(self, name, image, opts, cmd, work, cpus, log):
        argv = [self.exe, "run", "--rm", "--name", name, f"--cpus={cpus}", *opts,
                "-v", f"{Path(work).as_posix()}:/work", image, *cmd]
        with open(log, "ab") as f:
            f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {name} ({cpus} threads)\n".encode())
            f.flush()
            try:
                return subprocess.call(argv, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                       creationflags=self.flags)
            except OSError as e:
                f.write(f"could not start docker: {e}\n".encode())
                return 1

    def pause(self, name):
        self._call("pause", name)

    def unpause(self, name):
        self._call("unpause", name)

    def set_cpus(self, name, cpus):
        self._call("update", "--cpus", str(cpus), name)

    def kill(self, name):
        self._call("rm", "-f", name)

    def cleanup(self):
        """Remove containers left behind by an earlier run of this program (names loghi-studio-*)."""
        rc, out = self._call("ps", "-a", "--filter", f"name=^{PREFIX}", "--format", "{{.Names}}")
        for name in out.split() if rc == 0 else []:
            if name.startswith(PREFIX):
                self.kill(name)


def pipeline(model, cpus, models_dir):
    """The Loghi steps for one work folder: (step number, container suffix, image, docker options, command)."""
    mod = ["-v", f"{Path(models_dir).as_posix()}:/models"]
    htr = (
        "find /work/tmp/imagesnippets -type f -name '*.png' > /work/tmp/lines.txt\n"
        "echo $(wc -l < /work/tmp/lines.txt) lines\n"
        "TF_USE_LEGACY_KERAS=0 LD_PRELOAD=/usr/lib/$(arch)-linux-gnu/libtcmalloc_minimal.so.4 "
        f"python3 /src/loghi-htr/src/main.py --model /models/{model} --batch_size 32 "
        "--inference_list /work/tmp/lines.txt --results_file /work/tmp/results.txt "
        "--gpu -1 --output /work/tmp/output/ --beam_width 1")
    return [
        (1, "lines", IMG_LAYPA, MEM + mod,
         ["python", "run.py", "-c", f"/models/{LINE_MODEL}/config.yaml", "-i", "/work", "-o", "/work", "--opts",
          "MODEL.WEIGHTS", "", "TEST.WEIGHTS", f"/models/{LINE_MODEL}/model_best_mIoU.pth", "MODEL.DEVICE", "cpu"]),
        (2, "baselines", IMG_TOOL, [],
         [f"{BIN}/MinionExtractBaselines", "-input_path_image", "/work", "-input_path_png", "/work/page/",
          "-input_path_page", "/work/page/", "-output_path_page", "/work/page/",
          "-recalculate_textline_contours_from_baselines", "-as_single_region", NS]),
        (3, "cut", IMG_TOOL, [],
         [f"{BIN}/MinionCutFromImageBasedOnPageXMLNew", "-input_path", "/work", "-outputbase",
          "/work/tmp/imagesnippets/", "-tmpdir", "/work/tmp", "-output_type", "png", "-channels", "4",
          "-threads", str(cpus), NS]),
        (4, "htr", IMG_HTR, MEM + mod, ["bash", "-c", htr]),
        (5, "merge", IMG_TOOL, mod,
         [f"{BIN}/MinionLoghiHTRMergePageXML", "-input_path", "/work/page", "-results_file",
          "/work/tmp/results.txt", "-config_file", f"/models/{model}/config.json",
          "-htr_code_config_file", "/work/tmp/output/config.json", NS]),
        (5, "order", IMG_TOOL, [],
         [f"{BIN}/MinionRecalculateReadingOrderNew", "-input_dir", "/work/page/", "-border_margin", "50",
          "-threads", str(cpus), NS]),
    ]


# ---------------------------------------------------------------- the queue

class Queue:
    def __init__(self, srv, settings_dir, cache_dir, engine=None):
        self.srv = srv
        self.file = Path(settings_dir) / "queue.json"
        self.log_dir = Path(cache_dir) / "logs"
        self.engine = engine or DockerEngine()
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.thread = None
        self.container = None       # name of the running container, if any
        self.cancelled = set()
        self._state_cache = {}
        self.poll = 30              # seconds between checks while waiting for Docker or another run
        self.settle = 60            # another run must be absent this long (it has short gaps between its steps)
        self._clear = False         # True once no other Loghi run was seen for `settle` seconds
        default_models = srv.PROJECTS / "loghi" / "models"
        self.data = {"paused": False, "cpus": max(1, (os.cpu_count() or 4) * 5 // 8), "cpus_night": 0,
                     "chunk": 20, "models_dir": str(default_models) if default_models.is_dir() else "",
                     "rate": None, "items": []}
        if self.file.exists():
            try:
                self.data.update(json.loads(self.file.read_text(encoding="utf-8")))
            except ValueError:
                pass
        for it in self.data["items"]:
            if it["state"] == "running":    # interrupted by a restart: continues with the scans still unread
                it.update(state="waiting", step=None, message="Interrupted; continues with the scans that are still unread.")

    # ------------------------------------------------------------ bookkeeping
    def save(self):
        with self.lock:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.file)

    def models(self):
        """Reading models in the models folder: sub-folders with config.json and a model file."""
        d = Path(self.data.get("models_dir") or "")
        out = []
        if d.is_dir():
            for e in sorted(os.scandir(d), key=lambda e: e.name):
                if e.is_dir() and re.fullmatch(self.srv.NAME_RE, e.name) and (Path(e.path) / "config.json").exists() \
                        and (Path(e.path) / "charlist.txt").exists():
                    out.append(e.name)
        return out

    def line_model_ok(self):
        d = Path(self.data.get("models_dir") or "") / LINE_MODEL
        return (d / "config.yaml").exists() and (d / "model_best_mIoU.pth").exists()

    def cpus_now(self):
        night = self.data.get("cpus_night") or 0
        h = time.localtime().tm_hour
        if night and (h >= NIGHT_HOURS[0] or h < NIGHT_HOURS[1]):
            return night
        return self.data["cpus"]

    def _get(self, qid):
        for it in self.data["items"]:
            if it["id"] == qid:
                return it
        raise KeyError("unknown queue item")

    def is_open(self, cid, book):
        with self.lock:
            return next((it["state"] for it in self.data["items"]
                         if it["collection"] == cid and it["book"] == book and it["state"] in OPEN_STATES), None)

    def public(self):
        with self.lock:
            rate = self.data.get("rate")
            t = time.time()
            items = []
            for it in self.data["items"]:
                d = dict(it)
                left = max(0, (it.get("total") or 0) - (it.get("done") or 0))
                d["left"] = left
                d["eta"] = None
                if it["state"] in OPEN_STATES and rate and not self.data["paused"]:
                    t += left * rate
                    d["eta"] = t
                items.append(d)
            models = self.models()
            problem = None
            if not self.data.get("models_dir") or not Path(self.data["models_dir"]).is_dir():
                problem = "The folder with the Loghi models is not set or does not exist. Set it under Settings below."
            elif not self.line_model_ok():
                problem = f"The line model ({LINE_MODEL}) is missing in the models folder."
            elif not models:
                problem = "There is no reading model in the models folder."
            last = self.data.get("last_model")
            default = last if last in models else next((m for m in models if m.startswith("republic-recommended")),
                                                       models[0] if models else None)
            return {"paused": self.data["paused"], "items": items, "models": models, "problem": problem,
                    "default_model": default,
                    "rate": rate, "cpus_now": self.cpus_now(), "cpu_count": os.cpu_count() or 1,
                    "settings": {k: self.data[k] for k in ("cpus", "cpus_night", "chunk", "models_dir")}}

    def books(self, cid):
        """Per book of a collection: what is still to be read (for the form 'add to the queue')."""
        root = self.srv.COLL.root(cid)
        out = []
        for v in self.srv.list_volumes(cid):
            if not v["scans"]:
                continue
            book = root / v["name"]
            key = tuple(self._mtime(p) for p in (book, book / "txt", book / "page", book / MARKER))
            hit = self._state_cache.get(str(book))
            if hit and hit[0] == key:
                st = hit[1]
            else:
                st = scan_state(book)
                self._state_cache[str(book)] = (key, st)
            out.append({"name": v["name"], "scans": v["scans"], "todo": len(st["todo"]),
                        "failed_before": len(st["failed_before"]), "skipped": len(st["skipped"]),
                        "queued": self.is_open(cid, v["name"]),
                        "other_run": bool(v["running"]) and not self.is_open(cid, v["name"])})
        return out

    @staticmethod
    def _mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return None

    # ------------------------------------------------------------ changes from the interface
    def add(self, cid, book, model=None, retry_skipped=False, enlarge=0):
        book_dir = self.srv.vol_path(cid, book)
        models = self.models()
        model = model or self.public()["default_model"]
        if model not in models:
            raise ValueError("This reading model is not in the models folder.")
        if self.is_open(cid, book):
            raise ValueError(f"Book {book} is already in the queue.")
        if retry_skipped:
            marker = read_marker(book_dir)
            if marker.pop("failed", None):
                write_marker(book_dir, marker)
        if enlarge not in (0, 2, 3):
            raise ValueError("enlarge must be 0, 2 or 3.")
        st = scan_state(book_dir, enlarge=enlarge)
        if not st["todo"]:
            raise ValueError(f"Book {book} has no unread scans.")
        item = {"id": uuid.uuid4().hex[:10], "collection": cid, "book": book, "model": model, "state": "waiting",
                "enlarge": enlarge,
                "total": len(st["todo"]), "done": 0, "failed": [], "step": None, "added": time.time(),
                "started": None, "finished": None,
                "message": f"Waiting. {len(st['todo'])} scan{'s' if len(st['todo']) != 1 else ''} to read"
                           + (" again, enlarged" if enlarge else "")
                           + (f", of which {len(st['failed_before'])} failed in an earlier run." if st["failed_before"] else ".")}
        with self.lock:
            self.data["items"].append(item)
            self.data["last_model"] = model
            self.save()
        self.srv._vol_cache.pop(cid, None)
        self.wake.set()
        return dict(item)

    def change(self, qid, data):
        with self.lock:
            it = self._get(qid)
            items = self.data["items"]
            if "model" in data:
                if it["state"] != "waiting":
                    raise ValueError("The model can only be changed while the book is waiting.")
                if data["model"] not in self.models():
                    raise ValueError("This reading model is not in the models folder.")
                it["model"] = data["model"]
            if "move" in data:
                if it["state"] != "waiting":
                    raise ValueError("Only a waiting book can be moved.")
                waiting = [x for x in items if x["state"] == "waiting"]
                i = waiting.index(it)
                j = {"up": i - 1, "down": i + 1, "top": 0, "bottom": len(waiting) - 1}.get(data["move"])
                if j is None:
                    raise ValueError("Unknown move.")
                j = max(0, min(len(waiting) - 1, j))
                waiting.insert(j, waiting.pop(i))
                rest = iter(waiting)
                self.data["items"] = [next(rest) if x["state"] == "waiting" else x for x in items]
            self.save()
            return dict(it)

    def remove(self, qid):
        """Stop a running book, or take a waiting or finished book off the list. Read scans are kept."""
        with self.lock:
            it = self._get(qid)
            if it["state"] == "running":
                self.cancelled.add(qid)
                name = self.container
                it["message"] = "Stopping."
            else:
                self.data["items"].remove(it)
                name = None
            self.save()
        if name:
            self.engine.kill(name)
        self.srv._vol_cache.pop(it["collection"], None)
        self.wake.set()
        return dict(it)

    def clear_finished(self):
        with self.lock:
            n = len(self.data["items"])
            self.data["items"] = [it for it in self.data["items"] if it["state"] in OPEN_STATES]
            self.save()
            return {"removed": n - len(self.data["items"])}

    def set_settings(self, data):
        with self.lock:
            ncpu = os.cpu_count() or 1
            for key, lo, hi in (("cpus", 1, ncpu), ("cpus_night", 0, ncpu), ("chunk", 1, 200)):
                if key in data:
                    try:
                        val = int(data[key])
                    except (TypeError, ValueError):
                        raise ValueError(f"{key} must be a whole number.")
                    if not lo <= val <= hi:
                        raise ValueError(f"{key} must be between {lo} and {hi}.")
                    self.data[key] = val
            if "models_dir" in data:
                d = str(data["models_dir"] or "").strip()
                if not d or not Path(d).is_dir():
                    raise ValueError(f"This folder does not exist: {d}")
                self.data["models_dir"] = str(Path(d))
            name = self.container
            if "paused" in data:
                self.data["paused"] = bool(data["paused"])
            self.save()
            paused, cpus = self.data["paused"], self.cpus_now()
        if name:
            if "paused" in data:
                (self.engine.pause if paused else self.engine.unpause)(name)
            if "cpus" in data or "cpus_night" in data:
                self.engine.set_cpus(name, cpus)
        self.wake.set()
        return self.public()

    # ------------------------------------------------------------ running
    def _set(self, it, **kw):
        with self.lock:
            it.update(kw)

    def _gate(self, it):
        """Block while paused; leave with Stopped when this book was taken off the queue."""
        while True:
            if it["id"] in self.cancelled:
                raise Stopped()
            if not self.data["paused"]:
                return
            self._set(it, message="Paused. Choose Resume to continue.")
            self.wake.wait(self.poll)
            self.wake.clear()

    def _ready(self, it):
        """Wait until Docker runs and no other Loghi run is active."""
        while True:
            self._gate(it)
            busy = "Waiting: another Loghi run (not started by Loghi-studio) is active on this computer."
            problem = self.engine.available()
            if not problem and self.engine.others():
                problem, self._clear = busy, False
            elif not problem and not self._clear:
                self._set(it, step=None, message="Checking that no other Loghi run is active on this computer.")
                end = time.time() + self.settle
                while time.time() < end:
                    self.wake.wait(min(1, max(0.01, end - time.time())))
                    self._gate(it)
                if self.engine.others():
                    problem = busy
                else:
                    self._clear = True
            if not problem:
                return
            self._set(it, step=None, message=problem)
            self.wake.wait(self.poll)
            self.wake.clear()

    def _read(self, it, book, stems, files, log, label):
        """Run the Loghi steps on these scans. Returns (good stems, bad stems)."""
        work = book / WORK_DIR
        shutil.rmtree(work, ignore_errors=True)
        (work / "tmp" / "imagesnippets").mkdir(parents=True)
        (work / "tmp" / "output").mkdir()
        factor = it.get("enlarge") or 0
        for stem in stems:
            src, dst = book / files[stem], work / files[stem]
            if factor:
                enlarge_scan(src, dst, factor)
                continue
            try:
                os.link(src, dst)
            except OSError:
                shutil.copyfile(src, dst)
        failed_step = None
        for num, suffix, image, opts, cmd in pipeline(it["model"], self.cpus_now(), self.data["models_dir"]):
            if num == 4 and not any((work / "tmp" / "imagesnippets").rglob("*.png")):
                break               # no lines at all on these scans: nothing to read
            self._gate(it)
            self._set(it, step=num, message=f"Step {num} of 6, {STEPS[num]}: {label}.")
            name = f"{PREFIX}{it['id']}-{suffix}"
            with self.lock:
                self.container = name
            rc = self.engine.run(name, image, opts, cmd, work, self.cpus_now(), log)
            with self.lock:
                self.container = None
            if it["id"] in self.cancelled:
                raise Stopped()
            if rc != 0:
                failed_step = num
                break
        good, bad = [], []
        if failed_step is None:
            self._set(it, step=6, message=f"Step 6 of 6, {STEPS[6]}: {label}.")
            (book / "page").mkdir(exist_ok=True)
            (book / "txt").mkdir(exist_ok=True)
            for stem in stems:
                xml = work / "page" / f"{stem}.xml"
                try:
                    if factor:
                        shrink_page(xml, factor)
                    lines, read, text = page_lines(xml)
                except (OSError, ET.ParseError):
                    bad.append(stem)
                    continue
                if lines and not read:
                    bad.append(stem)    # lines were found but they never reached the reading step
                    continue
                dest = book / "page" / f"{stem}.xml"
                if dest.exists():
                    os.replace(dest, dest.with_suffix(".xml.bak"))
                shutil.move(str(xml), str(dest))
                tmp = book / "txt" / f"{stem}.txt.part"
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, book / "txt" / f"{stem}.txt")
                good.append(stem)
            if factor and good:
                marker = read_marker(book)
                marker.setdefault("enlarged", {}).update({s: factor for s in good})
                write_marker(book, marker)
        shutil.rmtree(work, ignore_errors=True)
        if failed_step is not None:
            if self.engine.available():
                raise DockerGone()
            if len(stems) == 1:
                return [], list(stems), failed_step
            half = len(stems) // 2      # a step failed: find the scan that causes it by halving
            g1, b1, f1 = self._read(it, book, stems[:half], files, log, label)
            g2, b2, f2 = self._read(it, book, stems[half:], files, log, label)
            return g1 + g2, b1 + b2, (f1 or f2 or failed_step) if not (g1 or g2) else None
        return good, bad, None

    def _run_item(self, it):
        cid, book_name = it["collection"], it["book"]
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log = self.log_dir / f"{cid}_{book_name}_{it['id']}.log"
        self._set(it, state="running", started=it.get("started") or time.time(), finished=None)
        self.save()
        final, message = "done", ""
        try:
            book = self.srv.vol_path(cid, book_name)
            st = scan_state(book, enlarge=it.get("enlarge") or 0)
            files, todo = st["scans"], st["todo"]
            total = max(it.get("total") or 0, (it.get("done") or 0) + len(todo))
            self._set(it, total=total, done=total - len(todo))
            tries, given_up, streak = {}, [], 0
            while todo:
                self._ready(it)
                first = [s for s in todo if not tries.get(s)]
                chunk = (first or todo)[:self.data["chunk"]]
                n0 = it["done"] + 1
                label = (f"scans {n0} to {n0 + len(chunk) - 1} of {total}" if first
                         else f"second try for {len(chunk)} scan{'s' if len(chunk) != 1 else ''}")
                t0 = time.time()
                try:
                    good, bad, failed_step = self._read(it, book, chunk, files, log, label)
                except DockerGone:
                    continue            # _ready waits until Docker is back; the chunk is read again
                streak = streak + 1 if failed_step and not good else 0
                if streak >= 2:
                    raise RuntimeError(f"Loghi step {failed_step} ({STEPS[failed_step]}) keeps failing; no scan was "
                                       f"marked as skipped. The output of Loghi is in {log}")
                for s in bad:
                    tries[s] = tries.get(s, 0) + 1
                    if tries[s] >= 2:
                        given_up.append(s)
                todo = [s for s in todo if s not in good and tries.get(s, 0) < 2]
                if good:
                    per = (time.time() - t0) / len(chunk)
                    with self.lock:
                        old = self.data.get("rate")
                        self.data["rate"] = per if not old else 0.7 * old + 0.3 * per
                with self.lock:
                    it["done"] = total - len(todo)
                    it["failed"] = sorted(given_up)
                    self.save()
                self.srv._vol_cache.pop(cid, None)
            if given_up:
                marker = read_marker(book)
                marker.setdefault("failed", {}).update({s: time.strftime("%Y-%m-%d") for s in given_up})
                write_marker(book, marker)
            st = scan_state(book, enlarge=it.get("enlarge") or 0)
            if not st["todo"] and not st["skipped"]:
                (book / "txt").mkdir(exist_ok=True)
                (book / "txt" / ".done").touch()
            read = total - len(given_up)
            message = f"Done: {read} scan{'s' if read != 1 else ''} read."
            if given_up:
                message += (f" {len(given_up)} failed twice and {'were' if len(given_up) != 1 else 'was'} skipped: "
                            f"{', '.join(sorted(given_up)[:20])}.")
        except Stopped:
            final, message = "stopped", "Stopped at your request. The scans that were read are kept."
        except Exception as e:  # keep the queue alive
            final, message = "failed", f"Failed: {str(e).strip(chr(39) + chr(34))}"
        try:
            shutil.rmtree(self.srv.COLL.root(cid) / book_name / WORK_DIR, ignore_errors=True)
        except Exception:
            pass
        self.cancelled.discard(it["id"])
        self._set(it, state=final, step=None, message=message, finished=time.time())
        self.save()
        self.srv._vol_cache.pop(cid, None)
        if self.srv.NAMES is not None:
            self.srv.NAMES.trigger()    # search the new text for the names on the list

    def run_pending(self):
        while True:
            with self.lock:
                if self.data["paused"]:
                    return
                it = next((x for x in self.data["items"] if x["state"] == "waiting"), None)
            if it is None:
                return
            self._run_item(it)

    def _loop(self):
        self.engine.cleanup()
        while True:
            self.wake.wait(60)
            self.wake.clear()
            try:
                self.run_pending()
            except Exception:
                time.sleep(5)

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
        self.wake.set()
