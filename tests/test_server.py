"""Tests for server.py on a small fake collection in a temp folder (no real scans are touched).

Run:  python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

TMP = tempfile.TemporaryDirectory()
os.environ["LOGHI_STUDIO_SETTINGS"] = str(Path(TMP.name) / "settings")
os.environ["LOGHI_STUDIO_CACHE"] = str(Path(TMP.name) / "cache")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402
import readqueue  # noqa: E402


def page_xml(stem, text=None, lines=True):
    """PageXML as Loghi writes it: with a line and its text, a line without text, or no line at all."""
    line = ""
    if lines:
        eq = f'<TextEquiv conf="0.9"><Unicode>{text}</Unicode></TextEquiv>' if text else ""
        line = f'<TextRegion id="r1"><TextLine id="l1"><Coords points="1,1 9,1 9,9 1,9"/>{eq}</TextLine></TextRegion>'
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2013-07-15">'
            f'<Page imageFilename="{stem}.jpg" imageWidth="10" imageHeight="10">{line}</Page></PcGts>')


class FakeEngine:
    """Stands in for Docker: writes the files the Loghi steps would write."""

    def __init__(self):
        self.calls, self.events = [], []
        self.fail, self.blank, self.no_text = set(), set(), set()
        self.down = self.busy = False

    def available(self):
        return "Docker is not running." if self.down else None

    def others(self):
        return self.busy

    def run(self, name, image, opts, cmd, work, cpus, log):
        suffix, work = name.rsplit("-", 1)[1], Path(work)
        stems = sorted(f.stem for f in work.glob("*.jpg"))
        self.calls.append((suffix, stems, cpus))
        if self.down:
            return 1
        if suffix == "lines":
            if self.fail & set(stems):
                return 1
            (work / "page").mkdir(exist_ok=True)
            for s in stems:
                (work / "page" / f"{s}.xml").write_text(page_xml(s, lines=s not in self.blank), encoding="utf-8")
        elif suffix == "cut":
            for s in stems:
                if s not in self.blank:
                    (work / "tmp" / "imagesnippets" / s).mkdir()
                    (work / "tmp" / "imagesnippets" / s / "l1.png").write_bytes(b"png")
        elif suffix == "traincut":          # the training set: one line image and its text per line of the ground truth
            import re
            for xml in sorted((work / "gt" / "page").glob("*.xml")):
                d = work / "lines" / xml.stem
                d.mkdir(parents=True)
                for lid, text in re.findall(r'<TextLine id="([^"]+)">.*?<Unicode>(.*?)</Unicode>', xml.read_text(encoding="utf-8"), re.S):
                    (d / f"{xml.stem}-{lid}.png").write_bytes(b"png")
                    (d / f"{xml.stem}-{lid}.txt").write_text(text, encoding="utf-8")
        elif suffix == "merge":
            for s in stems:
                if s not in self.blank and s not in self.no_text:
                    (work / "page" / f"{s}.xml").write_text(page_xml(s, f"text of scan {s}"), encoding="utf-8")
        return 0

    def pause(self, name):
        self.events.append(("pause", name))

    def unpause(self, name):
        self.events.append(("unpause", name))

    def set_cpus(self, name, cpus):
        self.events.append(("cpus", cpus))

    def kill(self, name):
        self.events.append(("kill", name))

    def cleanup(self):
        self.events.append(("cleanup",))

PAGE = """<?xml version="1.0" encoding="UTF-8"?>
<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2013-07-15">
 <Page imageFilename="000001.jpg" imageWidth="1000" imageHeight="800">
  <TextRegion id="r1">
   <TextLine id="l1"><Coords points="10,10 500,10 500,40 10,40"/><Baseline points="10,35 500,35"/>
    <TextEquiv conf="0.91"><Unicode>Jan Haijes tot Sexbierum</Unicode></TextEquiv></TextLine>
   <TextLine id="l2"><Coords points="10,50 500,50 500,80 10,80"/>
    <TextEquiv conf="0.45"><Unicode>ende Aeffke Claeses</Unicode></TextEquiv></TextLine>
   <TextLine id="l3"><Coords points="10,90 500,90 500,120 10,120"/>
    <TextEquiv conf="0.1"><Unicode>BBrongersmallsce</Unicode></TextEquiv></TextLine>
  </TextRegion>
 </Page>
</PcGts>
"""


def build_collection(root: Path):
    for vol, done in (("0001", True), ("0002", False)):
        v = root / vol
        (v / "page").mkdir(parents=True)
        (v / "txt").mkdir()
        (v / "000001.jpg").write_bytes(b"\xff\xd8fakejpg")
        (v / "000002.jpg").write_bytes(b"\xff\xd8fakejpg")
        if done:
            (v / "page" / "000001.xml").write_text(PAGE, encoding="utf-8")
            (v / "txt" / "000001.txt").write_text("Jan Haijes tot Sexbierum\nende Aeffke Claeses\nBBrongersmallsce\n", encoding="utf-8")
            (v / "txt" / "000002.txt").write_text("welke voorschreven is\n", encoding="utf-8")
            (v / "txt" / ".done").touch()
    (root / "batch.log").write_text(
        "20-11 10:00 inv 0001 start (2 scans)\n20-11 10:40 inv 0001 done, 2 txt\n20-11 10:40 0002 start (2 scans)\n",
        encoding="utf-8")
    (root / "hits_0001.md").write_text(
        "| boek | scan | regel | doel | context |\n|---|---|---|---|---|\n| 1 | 000001 | 1 | Haijes (any) | Jan Haijes tot Sexbierum |\n",
        encoding="utf-8")


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(TMP.name) / "proef"
        build_collection(cls.root)
        cfg = Path(TMP.name) / "settings" / "collections.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"collections": [{
            "id": "proef", "title": "Proef", "description": "", "years": "1600", "root": str(cls.root),
            "log": str(cls.root / "batch.log"), "hits": [str(cls.root)]}]}), encoding="utf-8")
        cls.engine = FakeEngine()
        server.init(cfg, cls.engine)
        models = Path(TMP.name) / "models"
        for m in ("m1", "m2"):
            (models / m).mkdir(parents=True)
            (models / m / "config.json").write_text("{}")
            (models / m / "charlist.txt").write_text("abc")
        (models / "no_config").mkdir()
        (models / "baseline2").mkdir()
        (models / "baseline2" / "config.yaml").write_text("x")
        (models / "baseline2" / "model_best_mIoU.pth").write_text("x")
        cls.models = models
        cls.cfg = cfg
        cls.srv = server.make_server(0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def call(self, path, method="GET", body=None, headers=None):
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    # ---- pure functions
    def test_norm(self):
        self.assertEqual(server.norm("Haijes"), server.norm("Hayes"))
        self.assertEqual(server.norm("Eelck"), "elk")
        self.assertEqual(server.norm("Claeses,"), "clases")

    def test_lev(self):
        self.assertTrue(server.lev_le("sybrant", "isbrant", 2))
        self.assertFalse(server.lev_le("sybrant", "hendrik", 2))

    def test_conf_label(self):
        self.assertEqual(server.conf_label(0.9), "sure")
        self.assertEqual(server.conf_label(0.5), "unsure")
        self.assertEqual(server.conf_label(0.2), "very unsure")
        self.assertIsNone(server.conf_label(None))

    def test_slug(self):
        self.assertEqual(server.slug("Het Bildt (Tresoar 13-07)"), "het-bildt-tresoar-13-07")

    def test_log_formats(self):
        for line, vol in (("20-11 13:38 weesboek_I2 start (282 scans)", "weesboek_I2"),
                          ("16-11 01:24 inv 0090 done, 370 txt", "0090"),
                          ("12:21 inv 85 start (369 scans)", "85"),
                          ("14:16 inv 85 done, 134 hits", "85")):
            self.assertEqual(server.LOG_RE.match(line).group(3), vol)
        self.assertEqual(server.match_volume("85", ["0085", "0086"]), "0085")

    # ---- API
    def test_volumes_and_status(self):
        code, vols = self.call("/api/volumes?c=proef")
        self.assertEqual(code, 200)
        self.assertEqual([(v["name"], v["state"]) for v in vols], [("0001", "done"), ("0002", "running")])
        code, st = self.call("/api/status")
        s = st["proef"]
        self.assertEqual((s["scans"], s["scans_done"], s["volumes_done"]), (4, 2, 1))
        self.assertEqual(s["running"]["vol"], "0002")

    def test_page_with_certainty(self):
        code, page = self.call("/api/page?c=proef&v=0001&s=000001")
        self.assertEqual(code, 200)
        self.assertEqual([l["n"] for l in page["lines"]], [1, 2, 3])
        self.assertEqual([l["sure"] for l in page["lines"]], ["sure", "unsure", "very unsure"])
        self.assertEqual(page["width"], 1000)

    def test_search_modes(self):
        _, r = self.call("/api/search?c=proef&q=hayes&mode=loose")
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["results"][0]["scan"], "000001")
        _, r = self.call("/api/search?c=proef&q=Haijos&mode=fuzzy")
        self.assertEqual(r["total"], 1)
        _, r = self.call("/api/search?c=proef&q=haijes%20aeffke&mode=loose&w=2")
        self.assertEqual(r["total"], 1)
        code, r = self.call("/api/search?c=proef&q=(&mode=regex")
        self.assertEqual(code, 400)
        # names in old spellings: a patronymic in another form, c for k, a reading error; the words that hit are given
        _, r = self.call("/api/search?c=proef&q=Aefke%20Klaesen&mode=names")
        self.assertEqual((r["total"], r["results"][0]["n"], r["results"][0]["m"]), (1, 2, ["afke", "clases"]))
        self.assertEqual(self.call("/api/search?c=proef&q=Hayes&mode=names")[1]["total"], 1)
        self.assertEqual(self.call("/api/search?c=proef&q=Jan%20Pieters&mode=names")[1]["total"], 0)
        self.assertEqual(self.call("/api/search?c=proef&q=%2C&mode=names")[1]["total"], 0)
        self.assertNotIn("index", self.call("/api/search?c=proef&q=hayes&mode=exact")[1])
        self.assertIsNone(self.call("/api/search?c=proef&q=hayes&mode=names")[1]["index"])       # no index for this collection

    def test_hits(self):
        _, rows = self.call("/api/hits?c=proef&src=old")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["vol"], "0001")

    def test_image_and_traversal(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/img/proef/0001/000001.jpg") as r:
            self.assertEqual(r.headers["Content-Type"], "image/jpeg")
        self.assertEqual(self.call("/api/scans?c=proef&v=..")[0], 400)
        self.assertEqual(self.call("/api/page?c=proef&v=0001&s=..%2Fx")[0], 400)
        self.assertEqual(self.call("/api/volumes?c=bestaatniet")[0], 400)

    def test_local_only(self):
        self.assertEqual(self.call("/api/collections", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.call("/api/collections", "POST", {"title": "x", "root": str(self.root)},
                                   headers={"Origin": "https://evil.example"})[0], 403)

    def test_collections_crud_keeps_files(self):
        code, c = self.call("/api/collections", "POST", {"title": "Tweede proef", "root": str(self.root), "years": "1627"})
        self.assertEqual(code, 201)
        self.assertEqual(c["id"], "tweede-proef")
        code, c2 = self.call("/api/collections", "POST", {"title": "Tweede proef", "root": str(self.root)})
        self.assertEqual(c2["id"], "tweede-proef-2")
        code, err = self.call("/api/collections", "POST", {"title": "Fout", "root": str(self.root / "nee")})
        self.assertEqual(code, 400)
        self.assertIn("does not exist", err["error"])
        code, err = self.call("/api/collections", "POST", {"title": " ", "root": str(self.root)})
        self.assertEqual(code, 400)
        code, c = self.call("/api/collections/tweede-proef", "PUT", {"title": "Hernoemd", "description": "test"})
        self.assertEqual((code, c["title"], c["years"]), (200, "Hernoemd", "1627"))
        saved = json.loads(self.cfg.read_text(encoding="utf-8"))["collections"]
        self.assertIn("Hernoemd", [x["title"] for x in saved])
        for cid in ("tweede-proef", "tweede-proef-2"):
            self.assertEqual(self.call(f"/api/collections/{cid}", "DELETE")[0], 200)
        self.assertEqual([x["id"] for x in self.call("/api/collections")[1]], ["proef"])
        self.assertTrue((self.root / "0001" / "000001.jpg").exists())  # removing from the list deletes nothing

    def test_name_search(self):
        import names
        self.assertEqual(names.fold("claeses"), names.fold("klaeses"))
        self.assertEqual((names.max_edits("elk"), names.max_edits("sybrant"), names.max_edits("rollingswier")), (0, 1, 2))
        N = server.NAMES
        code, n1 = self.call("/api/names", "POST", {"label": "Jan Haijes", "variants": "Jan Hayes; Jan Haies", "mode": "fuzzy"})
        self.assertEqual(code, 201)
        self.assertEqual(n1["variants"], ["Jan Hayes", "Jan Haies"])
        _, n2 = self.call("/api/names", "POST", {"label": "Eelck", "mode": "fuzzy"})
        _, n3 = self.call("/api/names", "POST", {"label": "Aeffke Klaeses", "mode": "exact"})
        _, n4 = self.call("/api/names", "POST", {"label": "Brongersmallste", "mode": "fuzzy"})
        _, n5 = self.call("/api/names", "POST", {"label": "Sexbierum ende", "mode": "fuzzy", "collections": ["proef"]})
        self.assertEqual(self.call("/api/names", "POST", {"label": "  "})[0], 400)
        self.assertEqual(self.call("/api/names", "POST", {"label": "x", "mode": "raar"})[0], 400)
        N.wake.clear()
        self.assertEqual(N.run("proef"), 1)   # one volume has text
        self.assertEqual(self.call("/api/hits?c=proef")[0], 400)     # all hits at once is no longer possible
        rows = {r["label"]: r for r in self.call("/api/hits?c=proef&v=0001&strength=all")[1]["rows"]}
        self.assertEqual((rows["Jan Haijes"]["n"], rows["Jan Haijes"]["kind"], rows["Jan Haijes"]["strong"]), (1, "exact", True))
        self.assertNotIn("Eelck", rows)                      # 'welke' is not Eelck
        self.assertEqual(rows["Aeffke Klaeses"]["sure"], "unsure")   # c = k, exact spelling: strong although unsure
        self.assertTrue(rows["Aeffke Klaeses"]["strong"])
        weak = rows["Brongersmallste"]                       # looks like it, on a very unsure line
        self.assertEqual((weak["kind"], weak["sure"], weak["strong"]), ("similar", "very unsure", False))
        self.assertEqual(rows["Sexbierum ende"]["n"], 1)     # name that runs on into the next line
        self.assertEqual(N.run("proef"), 0)                  # nothing new: nothing searched again
        counts = {n["label"]: n["hits"] for n in self.call("/api/names?c=proef")[1]["names"]}
        self.assertEqual(counts["Jan Haijes"], 1)
        self.assertEqual(self.call(f"/api/names/{n1['id']}", "PUT", {"mode": "exact"})[1]["mode"], "exact")
        for n in (n1, n2, n3, n4, n5):
            self.assertEqual(self.call(f"/api/names/{n['id']}", "DELETE")[0], 200)
        self.assertEqual(self.call("/api/hits?c=proef&v=0001&strength=all")[1]["rows"], [])
        self.assertEqual(len(self.call("/api/hits?c=proef&src=old")[1]), 1)
        N.wake.clear()

    def test_spellings(self):
        import names
        key = server.NAMES._key
        forms = names.spellings("Pieter Hayes", key=key)
        for f in ("pieter hayes", "pyter hayes", "pieter hayesen", "pieter hayesdr"):
            self.assertIn(f, forms)
        self.assertEqual(names.spellings("Pieter _____", key=key), [])          # only a first name
        self.assertEqual(names.clean_name("Aaltje Boyen Abesdr. _____"), "Aaltje Boyen Abesdr")
        self.assertEqual(names.clean_name("Symen Arjens S.L.M."), "Symen Arjens")
        self.assertEqual(names.split_person("Tziedske Walings van der Mey"), ("Tziedske", ["Walings"], "van der Mey"))
        self.assertEqual(names.split_person("x", "Aafje Pieters", "Mug"), ("Aafje", ["Pieters"], "Mug"))
        forms = names.spellings("Aafje Pieters Mug", key=key)
        self.assertEqual(forms[:2], ["aafje pieters", "aafje mug"])
        self.assertIn("lolckie eiwes", names.spellings("Lolck Eves", "F", key=key))   # short words: other vowels, v or w
        self.assertNotIn("janie jans", names.spellings("Jan Jans", "M", key=key))
        self.assertIn("trijn jans", names.spellings("Trijntje Jans", "F", key=key))
        self.assertNotIn("willem jans", names.spellings("Willemtje Jans", "F", key=key))      # a woman is not searched as a man
        self.assertNotIn("wopk jans", names.spellings("Wopkje Jans", "F", key=key, avoid={"wopk"}))
        self.assertIn("haye sybrens", names.spellings("Haye Sybrandts", key=key))
        self.assertIn("durk krelis", names.spellings("Dirk Cornelis", key=key))
        self.assertIn("andries lyuwes", names.spellings("Andries Lieuwes", key=key))
        self.assertLessEqual(len(names.spellings("Trijntje Dirks de Jong", "F", key=key)), names.MAX_VARIANTS)
        # the mask for words with two reading errors never rejects a word that is within two errors
        for a, b in (("rollingswier", "rolingswir"), ("wassenaar", "wasenaer"), ("sybrandts", "sijbrants")):
            a, b = names.fold(server.norm(a)), names.fold(server.norm(b))
            if server.lev_le(a, b, 2):
                self.assertLessEqual((names._pairs_mask(a) ^ names._pairs_mask(b)).bit_count(), 8)
        self.assertEqual([x["name"] for x in names.people_from_text("Jan Hayes;Jan_(1)\n\nPiet Jans\n")], ["Jan Hayes", "Piet Jans"])
        self.assertEqual(names.people_from_text('[{"name": "Jan Hayes", "uid": "u"}]')[0]["uid"], "u")

    def test_possible_reading_errors(self):
        """One word of a name as a typical reading error: kept apart from the usual hits."""
        import names
        N = server.NAMES
        self.assertIsNotNone(N.prices)                                           # reading_errors.json is there
        price = N.prices.price
        self.assertLess(price("sub", "n", "u", ""), 0.6)                         # u for n: Loghi does that
        self.assertGreater(price("del", "y", "", ""), 1.3)                       # a dropped y is chance
        self.assertEqual((names.Prices.budget(3), names.Prices.budget(4) > 1, names.Prices.budget(12) > 3), (0, True, True))
        self.assertIsNotNone(names.weighted("hendriks", "heudryks", price, 2.5))
        self.assertIsNone(names.weighted("pyters", "ptrs", price, 2.0))
        raw = ["compareerde Jan Heudryks ende", "Trijn Jaus zijn wijff", "item Jau Heudryks voornoemt", "ende Jan Hedrs mede",
               "Pieter Jaus Monsma", "Pieter Jans Mousna", "noch Jan Hendriks"]
        words = {w for line in raw for w in server.norm(line).split()}
        tokfold = {w: names.fold(w) for w in words}
        folds = set(tokfold.values())
        voc = {"stamp": 0, "tokfold": tokfold, "folds": folds, "alphabet": "".join(sorted(set("".join(folds)))), "by_len": None, "cand": {}}
        people = [{"id": "a", "label": "Jan Hendriks", "variants": [], "mode": "fuzzy", "relatives": ["Trijn Jans"]},
                  {"id": "b", "label": "Trijn Jans", "variants": [], "mode": "fuzzy"},
                  {"id": "c", "label": "Jan Hendriks", "variants": [], "mode": "exact"},
                  {"id": "d", "label": "Pieter Jans Monsma", "variants": [], "mode": "fuzzy"}]
        page = [("000009", raw, [server.norm(x) for x in raw])]
        rows, cut = N._search_volume("proef", "0001", page, people, N._compile(people, voc), voc)
        got = {k: [r[1:3] for r in v] for k, v in rows.items()}
        # Heudryks for Hendriks (u for n, y for i) and Jaus for Jans: possible reading errors. Two words read
        # wrong (Jau Heudryks) or letters missing (Hedrs): nothing.
        self.assertEqual(got["a"], [[1, names.LOOSE], [7, names.EXACT]])
        self.assertEqual(got["b"], [[2, names.LOOSE]])
        self.assertEqual(got["c"], [[7, names.EXACT]])                           # exact spelling only: no reading errors
        self.assertEqual(got["d"], [[5, names.LOOSE], [6, names.LOOSE]])         # the second or the third word of a name
        self.assertEqual([r[4] for r in rows["a"]], [[], []])                    # a relative only supports with a usual hit
        self.assertEqual(cut, {})
        self.assertFalse(N._strong(rows["a"][0]))
        keep = N.prices
        try:
            N.prices = None                                                      # without the file: the usual hits only
            rows, _ = N._search_volume("proef", "0001", page, people, N._compile(people, voc), voc)
            self.assertEqual({k: [r[1:3] for r in v] for k, v in rows.items()}, {"a": [[7, names.EXACT]], "c": [[7, names.EXACT]]})
        finally:
            N.prices = keep
        # through the server: only shown when asked for, and counted apart
        extra = self.root / "0001" / "txt" / "000003.txt"
        extra.write_text("compareerde Jan Heudryks ende\nnoch Jan Hendriks\n", encoding="utf-8")
        try:
            _, n = self.call("/api/names", "POST", {"label": "Jan Hendriks", "mode": "fuzzy"})
            server.index_for("proef").checked = 0
            N.wake.clear()
            N.run("proef")
            usual = self.call(f"/api/hits?c=proef&id={n['id']}&strength=all")[1]["rows"]
            self.assertEqual([(r["scan"], r["n"], r["kind"]) for r in usual], [("000003", 2, "exact")])
            loose = self.call(f"/api/hits?c=proef&id={n['id']}&strength=loose")[1]["rows"]
            self.assertEqual([(r["scan"], r["n"], r["kind"], r["strong"]) for r in loose], [("000003", 1, "loose", False)])
            self.assertEqual(self.call(f"/api/hits?c=proef&id={n['id']}&strength=loose_supported")[1]["count"], 0)
            person = next(x for x in self.call("/api/hits/summary?c=proef&strength=all")[1]["people"] if x["id"] == n["id"])
            self.assertEqual((person["total"], person["loose"], person["shown"]), (1, 1, 1))
            person = next(x for x in self.call("/api/hits/summary?c=proef&strength=loose")[1]["people"] if x["id"] == n["id"])
            self.assertEqual(person["shown"], 1)
            self.assertEqual({x["label"]: x["hits"] for x in self.call("/api/names?c=proef")[1]["names"]}["Jan Hendriks"], 1)
            marks = [m for m in self.call("/api/hits/marks?c=proef&v=0001")[1] if m["id"] == n["id"]]
            self.assertEqual([(m["scan"], m["n"]) for m in marks], [("000003", 2)])   # no tags in View for reading errors
            self.assertEqual(self.call(f"/api/names/{n['id']}", "DELETE")[0], 200)
        finally:
            extra.unlink()
            server.index_for("proef").checked = 0
            N.wake.clear()
            N.run("proef")
            N.wake.clear()

    def test_split_word(self):
        """A name that Loghi read as two words (Sij brensz), also over the end of a line."""
        N = server.NAMES
        raw = ["vtekent haije Sij brensz ende", "Pieter Hay", "es voornoemt"]
        words = {w for line in raw for w in server.norm(line).split()}
        import names
        tokfold = {w: names.fold(w) for w in words}
        folds = set(tokfold.values())
        voc = {"stamp": 0, "tokfold": tokfold, "folds": folds, "alphabet": "".join(sorted(set("".join(folds)))), "by_len": None, "cand": {}}
        people = [{"id": "a", "label": "Haije Sijbrens", "variants": [], "mode": "fuzzy"},
                  {"id": "b", "label": "Pieter Hayes", "variants": [], "mode": "fuzzy"}]
        rows, cut = N._search_volume("proef", "0001", [("000009", raw, [server.norm(x) for x in raw])], people, N._compile(people, voc), voc)
        self.assertEqual({k: [r[:3] for r in v] for k, v in rows.items()}, {"a": [["000009", 1, 0]], "b": [["000009", 2, 0]]})
        self.assertEqual(cut, {})

    def test_name_lists_import_export(self):
        N = server.NAMES
        self.assertEqual([l["id"] for l in self.call("/api/names")[1]["lists"]], ["mine"])
        people = [{"name": "Jan Hayes", "uid": "Jan_Hayes_(1)", "sex": "M", "relatives": ["Aeffke Claeses", "Eelke _____"], "year": 1650},
                  {"name": "Jan Hayes", "uid": "Jan_Hayes_(2)", "year": 1611},
                  {"name": "Aeffke Claeses Mug", "given": "Aeffke Claeses", "surname": "Mug", "sex": "F", "relatives": ["Jan Hayes"]},
                  {"name": "Pieter _____", "uid": "Pieter_(9)"}]
        body = {"title": "Tree", "link": "https://example.org/wiki/{uid}", "collections": ["proef"], "people": people}
        self.assertEqual(self.call("/api/names/import", "POST", dict(body, link="ftp://x"))[0], 400)
        self.assertEqual(self.call("/api/names/import", "POST", dict(body, people=[]))[0], 400)
        code, r = self.call("/api/names/import", "POST", body)
        self.assertEqual((code, r["added"], r["merged"], [s["name"] for s in r["skipped"]]), (201, 2, 1, ["Pieter _____"]))
        lid = r["list"]["id"]
        self.assertEqual(self.call("/api/names/import", "POST", body)[0], 400)      # a list with this name exists
        got = self.call(f"/api/names?c=proef&list={lid}")[1]
        self.assertEqual({l["title"]: l["names"] for l in got["lists"]}, {"My names": 0, "Tree": 2})
        jan = next(n for n in got["names"] if n["label"] == "Jan Hayes")
        self.assertEqual((jan["refs"], jan["relatives"], jan["year"], jan["list"]), (["Jan_Hayes_(1)", "Jan_Hayes_(2)"], ["Aeffke Claeses"], 1611, lid))
        N.wake.clear()
        self.assertEqual(N.run("proef"), 1)
        s = self.call("/api/hits/summary?c=proef&strength=supported")[1]
        self.assertEqual((s["count"], s["hits"], s["names"]), (2, 2, 2))
        p = next(x for x in s["people"] if x["label"] == "Jan Hayes")
        self.assertEqual((p["shown"], p["supported"], p["books"], p["links"][1]), (1, 1, 1, "https://example.org/wiki/Jan_Hayes_(2)"))
        rows = self.call(f"/api/hits?c=proef&id={p['id']}&strength=supported")[1]
        self.assertEqual((rows["count"], rows["rows"][0]["relatives"], rows["rows"][0]["text"], rows["rows"][0]["after"]),
                         (1, ["Aeffke Claeses"], "Jan Haijes tot Sexbierum", "ende Aeffke Claeses"))
        self.assertEqual([m["label"] for m in self.call("/api/hits/marks?c=proef&v=0001")[1]], ["Jan Hayes", "Aeffke Claeses Mug"])
        self.assertEqual(self.call("/api/hits/summary?c=proef&q=aeff&sort=hits")[1]["count"], 1)
        self.assertEqual(self.call("/api/hits/summary?c=proef&strength=raar")[0], 400)
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/hits/export?c=proef&strength=all") as resp:
            self.assertIn("text/csv", resp.headers["Content-Type"])
            lines = resp.read().decode("utf-8").splitlines()
        self.assertTrue(lines[0].startswith("person,uid,link,list,book,scan,line,strength,spelling,certainty"))
        row = next(l for l in lines if l.startswith("Jan Hayes,"))
        for part in ("Jan_Hayes_(1) | Jan_Hayes_(2)", "https://example.org/wiki/Jan_Hayes_(1)", ",Tree,0001,000001,1,strong,exact,", "Aeffke Claeses", "Jan Haijes tot Sexbierum"):
            self.assertIn(part, row)
        # a new name: only that name is searched again, the other hits stay
        seen = []
        compile_ = N._compile
        N._compile = lambda people, voc: seen.append([x["label"] for x in people]) or compile_(people, voc)
        try:
            _, extra = self.call("/api/names", "POST", {"label": "Sexbierum", "list": "mine", "relatives": "Jan Haijes"})
            N.wake.clear()
            self.assertEqual(N.run("proef"), 1)
            self.assertEqual(seen, [["Sexbierum"]])
            self.assertEqual(N.run("proef"), 0)
        finally:
            N._compile = compile_
        self.assertEqual(self.call("/api/hits/summary?c=proef&strength=all")[1]["count"], 3)
        self.assertEqual(self.call(f"/api/hits?c=proef&id={extra['id']}&strength=supported")[1]["count"], 1)
        # a list that is switched off: hits hidden at once, nothing is searched again
        self.assertEqual(self.call(f"/api/lists/{lid}", "PUT", {"on": False})[1]["on"], False)
        self.assertEqual(self.call("/api/hits/summary?c=proef&strength=all")[1]["count"], 1)
        self.assertEqual(self.call(f"/api/hits/summary?c=proef&strength=all&list={lid}")[1]["count"], 2)   # asked for by name
        self.assertEqual(self.call("/api/hits/marks?c=proef&v=0001")[1][0]["label"], "Sexbierum")
        self.assertFalse(N.wake.is_set())
        self.assertEqual(N.run("proef"), 0)
        self.assertEqual(self.call(f"/api/lists/{lid}", "PUT", {"title": "My names"})[0], 400)
        self.assertEqual(self.call(f"/api/lists/{lid}", "PUT", {"title": "Family tree", "on": True})[1]["title"], "Family tree")
        self.assertEqual(self.call("/api/lists", "POST", {"title": " "})[0], 400)
        code, empty = self.call("/api/lists", "POST", {"title": "Empty"})
        self.assertEqual((code, empty["on"]), (201, True))
        # import into an existing list from pasted lines
        code, r = self.call("/api/names/import", "POST", {"list": empty["id"], "text": "Aeffke Claeses;ref 1\nJan\n"})
        self.assertEqual((r["added"], len(r["skipped"])), (1, 1))
        saved = json.loads((Path(TMP.name) / "settings" / "names.json").read_text(encoding="utf-8"))
        self.assertEqual([l["title"] for l in saved["lists"]], ["My names", "Family tree", "Empty"])
        self.assertEqual(self.call("/api/lists/mine", "DELETE")[0], 400)
        self.assertEqual(self.call(f"/api/lists/{lid}", "DELETE")[1]["removed"], 2)
        self.assertEqual(self.call(f"/api/lists/{empty['id']}", "DELETE")[1]["removed"], 1)
        self.assertEqual(self.call(f"/api/names/{extra['id']}", "DELETE")[0], 200)
        self.assertEqual(self.call("/api/names")[1]["names"], [])
        N.wake.clear()
        N.run("proef")
        self.assertEqual(self.call("/api/hits/summary?c=proef&strength=all")[1]["hits"], 0)

    def test_add_book_from_folder(self):
        B = server.BOOKS
        B.min_free_gb = 0
        src = Path(TMP.name) / "bron"
        src.mkdir(exist_ok=True)
        for i in (1, 2, 3):
            (src / f"{i:06d}.jpg").write_bytes(b"\xff\xd8" + bytes(2000))
        (src / "notitie.txt").write_text("geen scan")
        self.assertEqual(self.call("/api/books", "POST", {"collection": "proef", "kind": "folder", "book": "nieuw boek", "path": str(src)})[0], 400)
        self.assertEqual(self.call("/api/books", "POST", {"collection": "proef", "kind": "folder", "book": "0001", "path": str(src)})[0], 400)
        self.assertEqual(self.call("/api/books", "POST", {"collection": "proef", "kind": "folder", "book": "leeg", "path": str(self.root / "0001" / "txt")})[0], 400)
        self.assertEqual(self.call("/api/books", "POST", {"collection": "nee", "kind": "folder", "book": "x", "path": str(src)})[0], 400)
        code, job = self.call("/api/books", "POST", {"collection": "proef", "kind": "folder", "book": "uit_map", "path": str(src)})
        self.assertEqual((code, job["state"], job["total"]), (201, "waiting", 3))
        self.assertEqual(self.call("/api/books", "POST", {"collection": "proef", "kind": "folder", "book": "uit_map", "path": str(src)})[0], 400)
        B.run_pending()
        done = self.call("/api/jobs")[1][0]
        self.assertEqual((done["state"], done["done"]), ("done", 3))
        self.assertEqual(sorted(f.name for f in (self.root / "uit_map").iterdir()), ["000001.jpg", "000002.jpg", "000003.jpg"])
        self.assertEqual(len(list(src.iterdir())), 4)           # the source folder is untouched
        vols = {v["name"]: v for v in self.call("/api/volumes?c=proef")[1]}
        self.assertEqual((vols["uit_map"]["scans"], vols["uit_map"]["state"]), (3, "to do"))
        import shutil
        shutil.rmtree(self.root / "uit_map")
        server._vol_cache.clear()

    def test_add_book_from_tresoar(self):
        import books
        B = server.BOOKS
        B.min_free_gb = 0
        online, asked = 11, []

        def fake(url, timeout=120):
            asked.append(url)
            num = url.rsplit("_", 1)[1].split(".")[0]
            scan = int(num)
            if "_0077%2F" not in url or len(num) != 5:   # this finding aid numbers its scans with 5 digits
                return False
            return (b"\xff\xd8" + bytes(1500)) if scan <= online else False

        B.fetch = fake
        self.assertIn("13-07_0090_000012.jp2", books.tresoar_url("13-07", 90, 12, 2000).replace("%2F", "/"))
        self.assertEqual(self.call("/api/books", "POST", {"collection": "proef", "kind": "tresoar", "access": "dertien", "inv": 77})[0], 400)
        self.assertEqual(self.call("/api/books", "POST", {"collection": "proef", "kind": "tresoar", "access": "13-07", "inv": "x"})[0], 400)
        code, job = self.call("/api/books", "POST", {"collection": "proef", "kind": "tresoar", "access": "13-07", "inv": 77, "size": 2000})
        self.assertEqual((code, job["book"]), (201, "0077"))
        code, job2 = self.call("/api/books", "POST", {"collection": "proef", "kind": "tresoar", "access": "13-07", "inv": 78})
        code, job3 = self.call("/api/books", "POST", {"collection": "proef", "kind": "tresoar", "access": "13-07", "inv": 79})
        self.assertEqual(self.call(f"/api/jobs/{job3['id']}", "DELETE")[1]["state"], "stopped")   # cancelled while waiting
        B.run_pending()
        jobs = {j["book"]: j for j in self.call("/api/jobs")[1]}
        self.assertEqual((jobs["0077"]["state"], jobs["0077"]["total"]), ("done", 11))
        self.assertEqual(len(list((self.root / "0077").glob("*.jpg"))), 11)
        self.assertEqual((self.root / "0077" / ".dl_done").read_text(), "11")
        self.assertEqual(jobs["0078"]["state"], "failed")
        self.assertIn("is not online", jobs["0078"]["message"])
        self.assertFalse((self.root / "0078").exists())
        self.assertEqual(self.call(f"/api/jobs/{job['id']}", "DELETE")[0], 400)   # finished: cannot be stopped
        self.assertTrue(all("Loghi" not in u for u in asked))
        import shutil
        shutil.rmtree(self.root / "0077")
        server._vol_cache.clear()

    # ---- reading queue
    def make_book(self, name, n):
        d = self.root / name
        d.mkdir()
        for i in range(1, n + 1):
            (d / f"{i:06d}.jpg").write_bytes(b"\xff\xd8" + bytes(50))
        server._vol_cache.clear()
        return d

    def drop_book(self, name):
        import shutil
        shutil.rmtree(self.root / name)
        server._vol_cache.clear()
        self.call("/api/queue/clear", "POST", {})

    def queue_setup(self, chunk=2):
        E = self.engine
        E.calls.clear(); E.events.clear(); E.fail, E.blank, E.no_text = set(), set(), set(); E.down = E.busy = False
        server.QUEUE.settle = 0
        code, q = self.call("/api/queue/settings", "PUT", {"models_dir": str(self.models), "chunk": chunk, "cpus": 2, "cpus_night": 0, "paused": False})
        self.assertEqual((code, q["models"], q["problem"]), (200, ["m1", "m2"], None))
        return server.QUEUE

    def test_queue_reads_book_in_chunks(self):
        Q, E = self.queue_setup(), self.engine
        book = self.make_book("qbook", 5)
        E.blank, E.no_text = {"000002"}, {"000004"}
        info = {b["name"]: b for b in self.call("/api/queue/books?c=proef")[1]}
        self.assertEqual((info["qbook"]["todo"], info["qbook"]["queued"]), (5, None))
        body = {"collection": "proef", "books": ["qbook"], "model": "m1"}
        self.assertEqual(self.call("/api/queue", "POST", dict(body, model="no_config"))[0], 400)
        self.assertEqual(self.call("/api/queue", "POST", dict(body, books=["nope"]))[0], 400)
        self.assertEqual(self.call("/api/queue", "POST", dict(body, books=[]))[0], 400)
        code, r = self.call("/api/queue", "POST", body)
        self.assertEqual((code, r["added"][0]["state"], r["added"][0]["total"]), (201, "waiting", 5))
        self.assertEqual(self.call("/api/queue", "POST", body)[0], 400)          # already in the queue
        vols = {v["name"]: v for v in self.call("/api/volumes?c=proef")[1]}
        self.assertTrue(vols["qbook"]["queued"])
        Q.run_pending()
        it = self.call("/api/queue")[1]["items"][-1]
        self.assertEqual((it["state"], it["done"], it["failed"]), ("done", 5, ["000004"]))
        self.assertIn("000004", it["message"])
        self.assertEqual((book / "txt" / "000001.txt").read_text(encoding="utf-8"), "text of scan 000001\n")
        self.assertEqual((book / "txt" / "000002.txt").read_text(encoding="utf-8"), "\n")   # blank page: read, no text
        self.assertFalse((book / "txt" / "000004.txt").exists())                 # failed twice: skipped
        self.assertEqual(sorted(f.name for f in (book / "page").iterdir()), ["000001.xml", "000002.xml", "000003.xml", "000005.xml"])
        self.assertFalse((book / readqueue.WORK_DIR).exists())
        self.assertFalse((book / "txt" / ".done").exists())
        self.assertEqual(len(list(book.glob("*.jpg"))), 5)                       # scans untouched
        lines = [c[1] for c in E.calls if c[0] == "lines"]
        self.assertEqual(lines, [["000001", "000002"], ["000003", "000004"], ["000005"], ["000004"]])
        self.assertTrue(all(c[2] == 2 for c in E.calls))                         # the thread setting is used
        info = {b["name"]: b for b in self.call("/api/queue/books?c=proef")[1]}
        self.assertEqual((info["qbook"]["todo"], info["qbook"]["skipped"]), (0, 1))
        self.assertEqual(self.call("/api/queue", "POST", body)[0], 400)          # nothing left to read
        # the skipped scan again, on request; and a scan whose earlier read failed (lines but no text)
        E.no_text = set()
        (book / "txt" / "000001.txt").write_text("\n", encoding="utf-8")
        (book / "page" / "000001.xml").write_text(page_xml("000001"), encoding="utf-8")
        st = readqueue.scan_state(book)
        self.assertEqual((st["todo"], st["failed_before"], st["skipped"]), (["000001"], ["000001"], ["000004"]))
        (book / "txt" / "000003.txt").write_text("\n", encoding="utf-8")      # read, but Loghi saw no text: not a failure
        (book / "page" / "000003.xml").write_text(page_xml("000003", "x").replace("<Unicode>x<", "<Unicode><"), encoding="utf-8")
        self.assertEqual(readqueue.scan_state(book)["todo"], ["000001"])
        code, r = self.call("/api/queue", "POST", dict(body, retry_skipped=True))
        self.assertEqual((code, r["added"][0]["total"]), (201, 2))
        Q.run_pending()
        self.assertEqual(self.call("/api/queue")[1]["items"][-1]["state"], "done")
        self.assertEqual((book / "txt" / "000004.txt").read_text(encoding="utf-8"), "text of scan 000004\n")
        self.assertEqual((book / "txt" / "000001.txt").read_text(encoding="utf-8"), "text of scan 000001\n")
        self.assertTrue((book / "page" / "000001.xml.bak").exists())             # the replaced file is kept
        self.assertTrue((book / "txt" / ".done").exists())
        vols = {v["name"]: v for v in self.call("/api/volumes?c=proef")[1]}
        self.assertEqual((vols["qbook"]["state"], vols["qbook"]["txt"]), ("done", 5))
        self.drop_book("qbook")

    def test_queue_reads_again_enlarged(self):
        from PIL import Image
        Q, E = self.queue_setup(), self.engine
        book = self.make_book("big", 3)
        for f in book.glob("*.jpg"):
            Image.new("RGB", (10, 8), "white").save(f)
        body = {"collection": "proef", "books": ["big"], "model": "m1"}
        self.call("/api/queue", "POST", body)
        Q.run_pending()
        self.assertEqual(self.call("/api/queue", "POST", body)[0], 400)          # nothing left to read
        seen = []
        run = E.run
        def spy(name, image, opts, cmd, work, cpus, log):
            if name.endswith("-lines"):
                seen.extend(Image.open(f).size for f in Path(work).glob("*.jpg"))
            return run(name, image, opts, cmd, work, cpus, log)
        E.run = spy
        try:
            self.assertEqual(self.call("/api/queue", "POST", dict(body, enlarge=5))[0], 400)
            code, r = self.call("/api/queue", "POST", dict(body, enlarge=2))
            self.assertEqual((code, r["added"][0]["total"], r["added"][0]["enlarge"]), (201, 3, 2))
            Q.run_pending()
        finally:
            E.run = run
        self.assertEqual(seen, [(20, 16)] * 3)                                   # Loghi saw the enlarged copies
        self.assertEqual(Image.open(book / "000001.jpg").size, (10, 8))          # the scan itself is untouched
        xml = (book / "page" / "000001.xml").read_text(encoding="utf-8")
        self.assertIn('points="0,0 4,0 4,4 0,4"', xml)                           # coordinates scaled back to the scan
        self.assertIn('imageWidth="5"', xml)
        self.assertTrue((book / "page" / "000001.xml.bak").exists())
        self.assertEqual(readqueue.read_marker(book)["enlarged"], {"000001": 2, "000002": 2, "000003": 2})
        self.assertEqual(self.call("/api/queue", "POST", dict(body, enlarge=2))[0], 400)   # all done this way
        self.assertEqual(readqueue.scan_state(book)["todo"], [])
        self.drop_book("big")

    def test_queue_step_failure_is_narrowed_down(self):
        Q, E = self.queue_setup(chunk=4), self.engine
        book = self.make_book("qfail", 4)
        E.fail = {"000003"}
        self.call("/api/queue", "POST", {"collection": "proef", "books": ["qfail"], "model": "m2"})
        Q.run_pending()
        it = self.call("/api/queue")[1]["items"][-1]
        self.assertEqual((it["state"], it["failed"], it["model"]), ("done", ["000003"], "m2"))
        self.assertEqual(sorted(f.name for f in (book / "txt").iterdir()), ["000001.txt", "000002.txt", "000004.txt"])
        self.assertEqual(readqueue.read_marker(book)["failed"].keys(), {"000003"})
        # a step that fails on everything stops the book and marks nothing as skipped
        self.make_book("qbad", 3)
        E.fail = {"000001", "000002", "000003"}
        self.call("/api/queue", "POST", {"collection": "proef", "books": ["qbad"], "model": "m1"})
        Q.run_pending()
        it = self.call("/api/queue")[1]["items"][-1]
        self.assertEqual(it["state"], "failed")
        self.assertIn("keeps failing", it["message"])
        self.assertEqual(readqueue.read_marker(self.root / "qbad"), {})
        self.drop_book("qfail")
        self.drop_book("qbad")

    def test_queue_order_pause_and_waiting(self):
        import time
        Q, E = self.queue_setup(), self.engine
        for name in ("qa", "qb", "qc"):
            self.make_book(name, 1)
        self.assertTrue(self.call("/api/queue/settings", "PUT", {"paused": True})[1]["paused"])
        _, r = self.call("/api/queue", "POST", {"collection": "proef", "books": ["qa", "qb", "qc"], "model": "m1"})
        a, b, c = r["added"]
        Q.run_pending()                                                          # paused: nothing starts
        self.assertEqual(E.calls, [])
        self.assertEqual(self.call(f"/api/queue/{c['id']}", "PUT", {"move": "top"})[0], 200)
        self.assertEqual(self.call(f"/api/queue/{a['id']}", "PUT", {"move": "down"})[0], 200)
        self.assertEqual(self.call(f"/api/queue/{a['id']}", "PUT", {"move": "sideways"})[0], 400)
        self.assertEqual(self.call(f"/api/queue/{b['id']}", "PUT", {"model": "m2"})[1]["model"], "m2")
        self.assertEqual(self.call(f"/api/queue/{b['id']}", "PUT", {"model": "nope"})[0], 400)
        waiting = [i["book"] for i in self.call("/api/queue")[1]["items"] if i["state"] == "waiting"]
        self.assertEqual(waiting, ["qc", "qb", "qa"])
        self.assertEqual(self.call(f"/api/queue/{a['id']}", "DELETE")[0], 200)   # a waiting book is taken off
        self.assertEqual(self.call("/api/queue/settings", "PUT", {"cpus": 999})[0], 400)
        # Docker down, then another Loghi run active: the queue waits and continues by itself
        E.down, Q.poll = True, 0.02
        self.call("/api/queue/settings", "PUT", {"paused": False})
        th = threading.Thread(target=Q.run_pending)
        th.start()
        item = lambda: next(i for i in self.call("/api/queue")[1]["items"] if i["book"] == "qc")
        for _ in range(200):
            if "Docker is not running" in item()["message"]:
                break
            time.sleep(0.02)
        self.assertIn("Docker is not running", item()["message"])
        E.busy, E.down = True, False
        for _ in range(200):
            if "another Loghi run" in item()["message"]:
                break
            time.sleep(0.02)
        self.assertIn("another Loghi run", item()["message"])
        self.assertEqual([c for c in E.calls if c[0] == "lines"], [])
        E.busy = False
        Q.wake.set()
        th.join(20)
        done = [(i["book"], i["state"]) for i in self.call("/api/queue")[1]["items"]]
        self.assertEqual(done, [("qc", "done"), ("qb", "done")])
        self.assertEqual([c[1] for c in E.calls if c[0] == "lines"], [["000001"], ["000001"]])
        self.assertTrue((self.root / "qa").is_dir() and not (self.root / "qa" / "txt").exists())
        self.assertEqual(self.call("/api/queue/clear", "POST", {})[1]["removed"], 2)
        Q.poll = 30
        for name in ("qa", "qb", "qc"):
            self.drop_book(name)

    def test_queue_restart_and_add_then_read(self):
        Q = self.queue_setup()
        d = Path(TMP.name) / "restart"
        d.mkdir()
        (d / "queue.json").write_text(json.dumps({"paused": True, "items": [
            {"id": "ab12", "collection": "proef", "book": "0002", "model": "m1", "state": "running", "total": 2, "done": 1}]}))
        q2 = readqueue.Queue(server, d, d, self.engine)
        self.assertEqual((q2.data["items"][0]["state"], q2.data["paused"]), ("waiting", True))
        # a book that is added with "read it afterwards" goes into the queue by itself
        B = server.BOOKS
        B.min_free_gb = 0
        src = Path(TMP.name) / "bron2"
        src.mkdir()
        (src / "000001.jpg").write_bytes(b"\xff\xd8" + bytes(2000))
        self.assertEqual(self.call("/api/books", "POST", {"collection": "proef", "kind": "folder", "book": "qnew", "path": str(src), "read_model": "nope"})[0], 400)
        self.assertEqual(self.call("/api/books", "POST", {"collection": "proef", "kind": "folder", "book": "qnew", "path": str(src), "read_model": "m1"})[0], 201)
        B.run_pending()
        self.assertIn("reading queue", self.call("/api/jobs")[1][0]["message"])
        Q.run_pending()
        self.assertEqual((self.root / "qnew" / "txt" / "000001.txt").read_text(encoding="utf-8"), "text of scan 000001\n")
        self.drop_book("qnew")

    def test_migrate_dutch_names(self):
        d = Path(TMP.name) / "old_settings"
        d.mkdir()
        (d / "namen.json").write_text(json.dumps({"names": [{"id": "1", "label": "Jan", "mode": "ruim"}, {"id": "2", "label": "Piet", "mode": "precies"}]}))
        (d / "taken.json").write_text(json.dumps({"jobs": [{"id": "1", "kind": "map", "state": "klaar", "toegang": "13-07"}]}))
        (d / "collecties.json").write_text(json.dumps({"collections": []}))
        keep = server.SETTINGS_DIR
        server.SETTINGS_DIR = d
        try:
            server.migrate_dutch_names()
            server.migrate_dutch_names()    # a second time changes nothing
        finally:
            server.SETTINGS_DIR = keep
        self.assertEqual(sorted(f.name for f in d.iterdir()), ["collections.json", "jobs.json", "names.json"])
        self.assertEqual([n["mode"] for n in json.loads((d / "names.json").read_text())["names"]], ["fuzzy", "exact"])
        job = json.loads((d / "jobs.json").read_text())["jobs"][0]
        self.assertEqual((job["kind"], job["state"], job["access"]), ("folder", "done", "13-07"))

    def test_translations_are_complete_and_built(self):
        import tools_i18n_build
        here = Path(server.__file__).parent
        data = json.loads((here / "lang" / "translations.json").read_text(encoding="utf-8"))
        for key, tr in data.items():
            for lang in ("nl", "fy"):
                self.assertTrue(tr.get(lang), f"no {lang} text for {key}")
                self.assertEqual(tools_i18n_build.marks(tr[lang]), tools_i18n_build.marks(key), f"placeholders differ in {lang}: {key}")
        page = (here / "index.html").read_text(encoding="utf-8")
        block = page[page.index("/*I18N*/const I18N = ") + len("/*I18N*/const I18N = "):page.index(";/*I18N-END*/")]
        built = json.loads(block.replace("<\/", "</"))
        self.assertEqual(built["nl"], {k: v["nl"] for k, v in data.items()})     # run tools_i18n_build.py after a change
        self.assertEqual(built["fy"], {k: v["fy"] for k, v in data.items()})

    def test_server_mode_login_and_roles(self):
        import auth
        accounts = auth.Auth(Path(TMP.name) / "accounts")
        pw_admin, pw_viewer = auth.make_password(), auth.make_password()
        self.assertRegex(pw_admin, r"^([a-z]+-){4}\d{3}$")
        accounts.set_user("Beheer", "admin", pw_admin)
        accounts.set_user("kijker", "viewer", pw_viewer)
        self.assertRaises(ValueError, accounts.set_user, "x", "admin", "short")
        self.assertNotIn(pw_admin, accounts.file.read_text(encoding="utf-8"))      # only a hash is stored
        srv = server.make_server(0, accounts, ["loghi.example"], "/", secure=False)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def call(path, method="GET", body=None, cookie=None, host="loghi.example", origin=None):
            h = {"Host": host}
            if body is not None:
                h["Content-Type"] = "application/json"
            if cookie:
                h["Cookie"] = cookie
            if origin:
                h["Origin"] = origin
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h,
                                         data=json.dumps(body).encode() if body is not None else None)
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, r.read().decode("utf-8"), r.headers
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8"), e.headers
        try:
            # nobody logged in: the login page and nothing else
            code, body, head = call("/")
            self.assertEqual(code, 200)
            self.assertIn('id="password"', body)
            self.assertNotIn("search-form", body)
            self.assertEqual(head["X-Frame-Options"], "DENY")
            for path in ("/api/collections", "/api/me", "/api/search?c=proef&q=jan", "/img/proef/vol1/000001.jpg"):
                self.assertEqual(call(path)[0], 401, path)
            self.assertEqual(call("/api/names", "POST", {"label": "Jan Jansen"})[0], 401)
            self.assertEqual(call("/", host=f"127.0.0.1:{port}")[0], 403)             # unknown host name
            self.assertEqual(call("/api/login", "POST", {"user": "kijker", "password": pw_viewer}, origin="https://evil.example")[0], 403)
            # wrong password, unknown name: the same answer
            self.assertEqual(call("/api/login", "POST", {"user": "kijker", "password": "wrong"})[0], 401)
            self.assertEqual(call("/api/login", "POST", {"user": "nobody", "password": pw_viewer})[0], 401)
            # viewer: may look and search, not change; sees no folders
            code, body, head = call("/api/login", "POST", {"user": "Kijker", "password": pw_viewer})
            self.assertEqual((code, json.loads(body)["role"]), (200, "viewer"))
            self.assertIn("HttpOnly", head["Set-Cookie"])
            self.assertIn("SameSite=Lax", head["Set-Cookie"])
            viewer = head["Set-Cookie"].split(";")[0]
            self.assertIn("search-form", call("/", cookie=viewer)[1])
            self.assertEqual(json.loads(call("/api/me", cookie=viewer)[1]), {"user": "kijker", "role": "viewer", "mode": "server"})
            cols = json.loads(call("/api/collections", cookie=viewer)[1])
            self.assertEqual(cols[0]["id"], "proef")
            self.assertNotIn("root", cols[0])
            self.assertEqual(call("/api/search?c=proef&q=jan", cookie=viewer)[0], 200)
            for path in ("/api/browse", "/api/queue", "/api/jobs", "/api/queue/books?c=proef"):
                self.assertEqual(call(path, cookie=viewer)[0], 403, path)
            self.assertEqual(call("/api/lists", "POST", {"title": "x"}, cookie=viewer)[0], 403)
            self.assertEqual(call("/api/collections/proef", "DELETE", cookie=viewer)[0], 403)
            # admin: the lists of names, nothing on the disk
            code, body, head = call("/api/login", "POST", {"user": "beheer", "password": pw_admin})
            admin = head["Set-Cookie"].split(";")[0]
            code, body, _ = call("/api/lists", "POST", {"title": "Servertest"}, cookie=admin)
            self.assertEqual(code, 201)
            self.assertEqual(call(f"/api/lists/{json.loads(body)['id']}", "DELETE", cookie=admin)[0], 200)
            for path, method, data in (("/api/queue", "POST", {"collection": "proef", "books": ["vol1"]}),
                                       ("/api/collections", "POST", {"title": "x", "root": TMP.name}),
                                       ("/api/books", "POST", {"collection": "proef"}), ("/api/queue/settings", "PUT", {"cpus": 1})):
                self.assertEqual(call(path, method, data, cookie=admin)[0], 403, path)
            # checking lines: the admin and a corrector may, a viewer may not; a corrector may do nothing else
            accounts.set_user("Lezer", "corrector", pw_admin)
            corr = call("/api/login", "POST", {"user": "lezer", "password": pw_admin})[2]["Set-Cookie"].split(";")[0]
            check = {"collection": "proef", "book": "0001", "scan": "000001", "line": "l1", "text": "Jan Haijes tot Sexbierum"}
            self.assertEqual(call("/api/corrections", "POST", check, cookie=viewer)[0], 403)
            code, body, _ = call("/api/corrections", "POST", check, cookie=corr)
            self.assertEqual((code, json.loads(body)["line"]["by"]), (201, "Lezer"))
            self.assertEqual(call("/api/corrections", "POST", dict(check, state="undone"), cookie=admin)[0], 201)
            self.assertEqual(call("/api/lists", "POST", {"title": "x"}, cookie=corr)[0], 403)
            self.assertEqual(call("/api/corrections/export", "POST", {"collection": "proef"}, cookie=admin)[0], 403)
            self.assertEqual(call("/api/corrections/export?c=proef", cookie=admin)[0], 403)
            self.assertEqual(call("/api/corrections?c=proef", cookie=viewer)[0], 200)
            # logging out ends the session
            self.assertEqual(call("/api/logout", "POST", {}, cookie=viewer)[0], 200)
            self.assertEqual(call("/api/me", cookie=viewer)[0], 401)
            self.assertEqual(call("/api/me", cookie=admin)[0], 200)
            # five wrong passwords: a pause, also for the right password
            for _ in range(5):
                call("/api/login", "POST", {"user": "kijker", "password": "wrong"})
            self.assertEqual(call("/api/login", "POST", {"user": "kijker", "password": pw_viewer})[0], 429)
        finally:
            srv.shutdown()
            srv.server_close()
        # the local application still needs no login
        self.assertEqual(self.call("/api/me")[1], {"user": None, "role": "admin", "mode": "local"})

    # ---- lines checked by a person, and the training set made from them
    def correct(self, line, text=None, state="checked", scan="000001"):
        return self.call("/api/corrections", "POST", {"collection": "proef", "book": "0001", "scan": scan,
                                                      "line": line, "text": text, "state": state})

    def test_corrections(self):
        import corrections
        book = self.root / "0001"
        before = {p: p.read_bytes() for p in (book / "page" / "000001.xml", book / "txt" / "000001.txt")}
        _, page = self.call("/api/page?c=proef&v=0001&s=000001")
        self.assertEqual([l["id"] for l in page["lines"]], ["l1", "l2", "l3"])
        self.assertNotIn("checked", page["lines"][1])
        try:
            # a line that was read wrong, a line that was read right, a line nobody can read
            code, r = self.correct("l2", "  ende Aeffke  Claeses dochter ")
            self.assertEqual(code, 201)
            self.assertEqual((r["line"]["text"], r["line"]["orig"], r["line"]["checked"], r["line"]["by"]),
                             ("ende Aeffke Claeses dochter", "ende Aeffke Claeses", "corrected", "local"))
            self.assertEqual(self.correct("l1", "Jan Haijes tot Sexbierum")[1]["line"]["checked"], "confirmed")
            self.assertEqual(self.correct("l3", state="unreadable")[1]["line"]["checked"], "unreadable")
            for p, data in before.items():                      # Loghi's own files are never touched
                self.assertEqual(p.read_bytes(), data)
            store = Path(TMP.name) / "settings" / "corrections" / "proef" / "0001.jsonl"
            self.assertEqual(len(store.read_text(encoding="utf-8").splitlines()), 3)
            # refused: an empty line, a line that is not there, an unknown state, a wrong book name
            self.assertEqual(self.correct("l1", "   ")[0], 400)
            self.assertEqual(self.correct("l9", "x")[0], 400)
            self.assertEqual(self.correct("l1", "x", state="guess")[0], 400)
            self.assertEqual(self.call("/api/corrections", "POST", {"collection": "proef", "book": "..", "scan": "000001",
                                                                    "line": "l1", "text": "x"})[0], 400)
            self.assertEqual(len(store.read_text(encoding="utf-8").splitlines()), 3)
            # View and Search give the checked text; the unreadable line keeps what Loghi read
            _, page = self.call("/api/page?c=proef&v=0001&s=000001")
            self.assertEqual([l["text"] for l in page["lines"]],
                             ["Jan Haijes tot Sexbierum", "ende Aeffke Claeses dochter", "BBrongersmallsce"])
            self.assertEqual([l.get("checked") for l in page["lines"]], ["confirmed", "corrected", "unreadable"])
            self.assertEqual([l["text"] for l in server.read_page("proef", "0001", "000001", raw=True)["lines"]][1], "ende Aeffke Claeses")
            _, r = self.call("/api/search?c=proef&q=dochter&mode=loose")
            self.assertEqual((r["total"], r["results"][0]["n"]), (1, 2))
            # numbers: 7 letters added on 27 letters of right text in the line that was wrong
            _, s = self.call("/api/corrections?c=proef")
            b = s["books"]["0001"]
            self.assertEqual((b["checked"], b["corrected"], b["unreadable"], b["scans"]), (2, 1, 1, 1))
            self.assertEqual((b["errors"], b["chars"]), (8, 24 + 27))
            self.assertEqual(s["total"]["checked"], 2)
            self.assertFalse(s["private"])
            # what to check next: nothing is left; after withdrawing a check that line comes back
            self.assertIsNone(self.call("/api/corrections/next?c=proef&v=0001")[1]["next"])
            self.assertEqual(self.correct("l3", state="undone")[0], 201)
            self.assertEqual(self.correct("l3", state="undone")[0], 400)
            self.assertEqual(self.call("/api/corrections/next?c=proef&v=0001")[1]["next"], {"scan": "000001", "n": 3})
            self.assertEqual(self.call("/api/corrections/next?c=proef&v=0001&order=unsure")[1]["next"], {"scan": "000001", "n": 3})
            self.assertIsNone(self.call("/api/corrections/next?c=proef&v=0001&s=000001&n=3")[1]["next"])
            self.assertEqual(self.call("/api/corrections/next?c=proef&v=0001&order=x")[0], 400)
            self.assertEqual(self.call("/api/corrections/next?c=proef&v=0001&order=unsure&s=..%2F..%2Fx&n=1")[0], 400)

            # the scan is read again (new line ids, lines moved a little): the checks stay with their lines
            again = PAGE.replace('id="l1"', 'id="n1"').replace('id="l2"', 'id="n2"').replace('id="l3"', 'id="n3"') \
                        .replace("10,50 500,50 500,80 10,80", "12,51 498,51 498,82 12,82").replace("ende Aeffke Claeses", "ende Aeffke Cloeses")
            (book / "page" / "000001.xml").write_text(again, encoding="utf-8")
            _, page = self.call("/api/page?c=proef&v=0001&s=000001")
            self.assertEqual([(l["id"], l.get("checked")) for l in page["lines"]], [("n1", "confirmed"), ("n2", "corrected"), ("n3", None)])
            self.assertEqual((page["lines"][1]["text"], page["lines"][1]["orig"]), ("ende Aeffke Claeses dochter", "ende Aeffke Cloeses"))
            self.assertEqual(self.call("/api/corrections/next?c=proef&v=0001")[1]["next"], {"scan": "000001", "n": 3})
            fix = server.CORR.index_fix("proef", "0001")
            self.assertEqual(fix("000001", ["Jan Haijes tot Sexbierum", "ende Aeffke Cloeses", "x"])[1], "ende Aeffke Claeses dochter")
            (book / "page" / "000001.xml").write_bytes(before[book / "page" / "000001.xml"])

            # the training set: only checked lines, in Loghi's format; a scan read enlarged goes in enlarged
            self.assertEqual(self.call("/api/corrections/export?c=proef")[1]["last"], None)
            (book / readqueue.MARKER).write_text(json.dumps({"enlarged": {"000001": 2}}), encoding="utf-8")
            real, readqueue.enlarge_scan = readqueue.enlarge_scan, lambda src, dst, factor: Path(dst).write_bytes(b"x" * factor)
            try:
                man = server.CORR.export("proef")
            finally:
                readqueue.enlarge_scan = real
                (book / readqueue.MARKER).unlink()
            out = Path(TMP.name) / "training" / "proef"
            # line l2 has no baseline in the PageXML, so Loghi cannot cut it out: it is counted and left out
            self.assertEqual((man["lines"], man["scans"], man["cut"], man["books"]), (1, 1, True, {"0001": 1}))
            self.assertEqual((out / "gt" / "0001_000001.jpg").read_bytes(), b"xx")
            gt = (out / "gt" / "page" / "0001_000001.xml").read_text(encoding="utf-8")
            self.assertIn('imageWidth="2000" imageHeight="1600"', gt)
            self.assertIn('<TextLine id="l1"><Coords points="20,20 1000,20 1000,80 20,80"/><Baseline points="20,70 1000,70"/>', gt)
            self.assertNotIn('id="l2"', gt)
            self.assertNotIn('id="l3"', gt)
            cut = [c for c in self.engine.calls if c[0] == "traincut"]
            self.assertEqual(len(cut), 1)
            rows = (out / "training_all.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual(rows, ["/work/lines/0001_000001/0001_000001-l1.png\tJan Haijes tot Sexbierum"])
            self.assertEqual(man["no_baseline"], 1)
            both = (out / "training_all_train.txt").read_text(encoding="utf-8") + (out / "training_all_val.txt").read_text(encoding="utf-8")
            self.assertEqual(both.splitlines(), rows)
            self.assertFalse((out / corrections.OWN_USE).exists())
            self.assertIn("create-train-data.sh", (out / "README.txt").read_text(encoding="utf-8"))
            # scans for own use only: the set says so; without Docker the ground truth is still made
            self.assertEqual(self.call("/api/collections/proef", "PUT", {"private": True})[1]["private"], True)
            self.engine.down = True
            try:
                man = server.CORR.export("proef")
            finally:
                self.engine.down = False
                self.call("/api/collections/proef", "PUT", {"private": False})
            self.assertEqual((man["cut"], man["private"], man["checked"]), (False, True, 1))
            self.assertTrue((out / corrections.OWN_USE).exists())
            self.assertFalse((out / "lines").exists())
            code, job = self.call("/api/corrections/export", "POST", {"collection": "proef"})
            self.assertEqual(code, 202)
            for _ in range(100):
                job = self.call("/api/corrections/export?c=proef")[1]
                if not job["running"]:
                    break
                threading.Event().wait(0.05)
            self.assertEqual((job["state"], job["last"]["lines"], job["last"]["private"]), ("done", 1, False))
            self.assertFalse((out / corrections.OWN_USE).exists())
        finally:
            (book / "page" / "000001.xml").write_bytes(before[book / "page" / "000001.xml"])
            for lid in ("l1", "l2", "l3"):
                self.correct(lid, state="undone")
        _, page = self.call("/api/page?c=proef&v=0001&s=000001")
        self.assertEqual([l.get("checked") for l in page["lines"]], [None, None, None])
        self.assertEqual(self.call("/api/search?c=proef&q=dochter&mode=loose")[1]["total"], 0)
        self.assertEqual(self.call("/api/corrections?c=proef")[1]["books"], {})

    def test_corrections_partial(self):
        # only some words corrected: shown in View and Search, counted apart, never a training line
        try:
            code, r = self.correct("l2", "ende Aeffke Clases", state="partial")
            self.assertEqual(code, 201)
            self.assertEqual((r["line"]["text"], r["line"]["orig"], r["line"]["checked"]),
                             ("ende Aeffke Clases", "ende Aeffke Claeses", "partial"))
            self.assertEqual(self.correct("l1", "  ", state="partial")[0], 400)
            _, page = self.call("/api/page?c=proef&v=0001&s=000001")
            self.assertEqual([l.get("checked") for l in page["lines"]], [None, "partial", None])
            _, r = self.call("/api/search?c=proef&q=Clases&mode=exact")
            self.assertEqual((r["total"], r["results"][0]["n"]), (1, 2))
            fix = server.CORR.index_fix("proef", "0001")
            self.assertEqual(fix("000001", ["Jan Haijes tot Sexbierum", "ende Aeffke Claeses", "x"])[1], "ende Aeffke Clases")
            b = self.call("/api/corrections?c=proef")[1]["books"]["0001"]
            self.assertEqual((b["checked"], b["corrected"], b["partial"], b["chars"]), (0, 0, 1, 0))
            # the line counts as done when looking for the next line to check
            self.assertEqual(self.call("/api/corrections/next?c=proef&v=0001&s=000001&n=1")[1]["next"], {"scan": "000001", "n": 3})
            man = server.CORR.export("proef", cut=False)
            self.assertEqual((man["checked"], man["lines"]), (0, 0))
        finally:
            self.correct("l2", state="undone")
        self.assertEqual(self.call("/api/corrections?c=proef")[1]["books"], {})

    def test_corrections_index_of_names(self):
        # an index made by people: the names of the deed on a scan, and the lines with a name that was nearly read right
        index = Path(TMP.name) / "index.json"
        index.write_text(json.dumps({"deeds": {
            "a": {"date": ["1638", "12", "7"], "folio": "326", "scans": [["0001", "000001"]],
                  "people": [{"name": "Jan Haijes", "role": "other:Koper", "place": "Sexbierum"}, {"name": "Aeffke Claesen", "role": None, "place": None}]},
            "b": {"date": [], "scans": [["0001", "000001"]], "people": [{"name": "Nedergerecht proef", "role": "other:Vermeld"}]}}}), encoding="utf-8")
        self.assertFalse(self.call("/api/corrections?c=proef")[1]["index"])
        self.assertEqual(self.call("/api/corrections/index?c=proef&v=0001&s=000001")[1], {"here": [], "before": []})
        self.assertEqual(self.call("/api/collections/proef", "PUT", {"index": str(index) + ".missing"})[0], 400)
        self.assertEqual(self.call("/api/collections/proef", "PUT", {"index": str(index)})[0], 200)
        try:
            self.assertTrue(self.call("/api/corrections?c=proef")[1]["index"])
            _, r = self.call("/api/corrections/index?c=proef&v=0001&s=000001")
            self.assertEqual(r["before"], [])
            self.assertEqual(r["here"], [{"date": "7-12-1638", "folio": "326", "people": [
                {"name": "Jan Haijes", "role": "Koper", "place": "Sexbierum"}, {"name": "Aeffke Claesen", "role": None, "place": None}]}])
            self.assertEqual(len(self.call("/api/corrections/index?c=proef&v=0001&s=000002")[1]["before"]), 1)
            # the index as a search layer: found on the names people read, in any old spelling
            _, r = self.call("/api/search?c=proef&q=jan%20hayes&mode=names")
            self.assertEqual(r["index"]["total"], 1)
            d = r["index"]["results"][0]
            self.assertEqual((d["vol"], d["scan"], d["date"], d["one"], [p["hit"] for p in d["people"]]),
                             ("0001", "000001", "7-12-1638", True, [True, False]))
            _, r = self.call("/api/search?c=proef&q=Klaassen%20Hayes&mode=loose")       # two people of one deed
            self.assertEqual((r["index"]["total"], r["index"]["results"][0]["one"]), (1, False))
            self.assertEqual(self.call("/api/search?c=proef&q=jan%20pieters&mode=fuzzy")[1]["index"]["total"], 0)
            self.assertEqual(self.call("/api/search?c=proef&q=jan%20hayes&mode=names&v=0002")[1]["index"]["total"], 0)
            # "Haijes" and "Aeffke" are read right; "Claeses" is nearly "Claesen": line 2 is the one to look at
            self.assertEqual(self.call("/api/corrections/next?c=proef&v=0001&order=names")[1]["next"], {"scan": "000001", "n": 2})
            self.assertIsNone(self.call("/api/corrections/next?c=proef&v=0001&order=names&s=000001&n=2")[1]["next"])
        finally:
            self.call("/api/collections/proef", "PUT", {"index": ""})
        self.assertIsNone(self.call("/api/corrections/next?c=proef&v=0001&order=names")[1]["next"])

    def test_corrections_two_readers(self):
        # a second reading of the same lines: the lines the two read differently, the smallest difference first
        corr = server.CORR
        second = corr.second_dir / "proef" / "0001" / "page"
        corr._second_books.clear()
        self.assertEqual(self.call("/api/corrections?c=proef")[1]["second"], {})
        self.assertEqual(self.call("/api/corrections/other?c=proef&v=0001&s=000001")[1], {"kind": None, "lines": {}})
        self.assertIsNone(self.call("/api/corrections/next?c=proef&v=0001&order=differ")[1]["next"])
        second.mkdir(parents=True)
        (second / "000001.xml").write_text(PAGE.replace("Aeffke Claeses", "Aeffke Claesen").replace("BBrongersmallsce", "Brongersma state")
                                           .replace('id="l3"', 'id="other3"'), encoding="utf-8")
        corr._second_books.clear()
        try:
            self.assertEqual(self.call("/api/corrections?c=proef")[1]["second"], {"0001": "other"})
            _, r = self.call("/api/corrections/other?c=proef&v=0001&s=000001")       # l3 is found by its place, not its id
            self.assertEqual(r, {"kind": "other", "lines": {"1": "Jan Haijes tot Sexbierum", "2": "ende Aeffke Claesen", "3": "Brongersma state"}})
            self.assertEqual(self.call("/api/corrections/next?c=proef&v=0001&order=differ")[1]["next"], {"scan": "000001", "n": 2})
            self.assertEqual(self.call("/api/corrections/next?c=proef&v=0001&order=differ&s=000001&n=2")[1]["next"], {"scan": "000001", "n": 3})
            self.assertIsNone(self.call("/api/corrections/next?c=proef&v=0001&order=differ&s=000001&n=3")[1]["next"])
            self.assertNotEqual(self.call("/api/corrections/other?c=proef&v=0001&s=a%2Fb")[0], 200)
        finally:
            import shutil
            shutil.rmtree(corr.second_dir / "proef", ignore_errors=True)
            corr._second_books.clear()
            corr._diff.clear()
        # the text a book had before it was read again counts as a second reading too
        bak = self.root / "0001" / "page" / "000001.xml.bak"
        bak.write_text(PAGE.replace("Aeffke Claeses", "Aeffke Claesz"), encoding="utf-8")
        try:
            self.assertEqual(self.call("/api/corrections?c=proef")[1]["second"], {"0001": "before"})
            _, r = self.call("/api/corrections/other?c=proef&v=0001&s=000001")
            self.assertEqual((r["kind"], r["lines"]["2"]), ("before", "ende Aeffke Claesz"))
        finally:
            bak.unlink()
            corr._second_books.clear()

    def test_corrections_copy_in_git(self):
        import corrections
        import shutil
        import subprocess
        import types
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        repo = Path(TMP.name) / "repo"
        (repo / "settings" / "corrections" / "proef").mkdir(parents=True)
        (repo / "settings" / "names.json").write_text("{}")
        (repo / "other.txt").write_text("work in progress")

        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).stdout
        git("init", "-q")
        git("config", "user.name", "test")
        git("config", "user.email", "test@example.invalid")
        git("config", "commit.gpgsign", "false")
        (repo / ".gitignore").write_text("settings/*\n!settings/corrections/\n")
        git("add", ".gitignore", "other.txt")              # staged by somebody else: must stay out of the copy
        c = corrections.Corrections(types.SimpleNamespace(HERE=repo), repo / "settings", repo / "training")
        (c.dir / "proef" / "0001.jsonl").write_text('{"scan": "000001"}\n', encoding="utf-8")
        state = c.backup()
        self.assertEqual(git("log", "--name-only", "--format=%s").split(), ["Corrections:", "copy", "of", "the", "checked", "lines",
                                                                             "settings/corrections/proef/0001.jsonl"])
        self.assertIn("other.txt", git("status", "--porcelain"))          # still only staged
        self.assertNotIn("names.json", git("status", "--porcelain", "--untracked-files=all"))
        self.assertFalse(state["ok"])                                       # no place to push to: said, not hidden
        self.assertIn("sending it on failed", state["message"])
        self.assertEqual(len(git("log", "--format=%h").split()), 1)
        c.backup()                                                          # nothing new: no second commit
        self.assertEqual(len(git("log", "--format=%h").split()), 1)
        # outside a git repository nothing happens
        self.assertIsNone(server.CORR.backup())
        self.assertIsNone(self.call("/api/corrections?c=proef")[1]["backup"])

    def test_corrections_pure(self):
        import corrections
        self.assertEqual(corrections.distance("Cloeses", "Claeses"), 1)
        self.assertEqual(corrections.distance("", "abc"), 3)
        self.assertEqual(corrections.overlap((0, 0, 10, 10), (0, 0, 10, 10)), 1.0)
        self.assertEqual(corrections.overlap((0, 0, 10, 10), (20, 20, 30, 30)), 0.0)
        self.assertEqual(corrections.scale_points("1,2 3,4", 2), "2,4 6,8")
        self.assertEqual(corrections.clean_text("Jän  Jans"), "Jän Jans")
        self.assertRaises(ValueError, corrections.clean_text, "x" * 501)
        self.assertRaises(ValueError, corrections.clean_text, None)

    def test_browse(self):
        code, r = self.call("/api/browse?path=" + urllib.request.quote(str(self.root)))
        self.assertEqual(code, 200)
        self.assertEqual([f["name"] for f in r["folders"]], ["0001", "0002"])
        code, r = self.call("/api/browse?path=" + urllib.request.quote(str(self.root / "0001")))
        self.assertEqual(r["images"], 2)
        self.assertEqual(self.call("/api/browse?path=Q%3A%5Cbestaat%5Cniet")[0], 400)


if __name__ == "__main__":
    unittest.main()
