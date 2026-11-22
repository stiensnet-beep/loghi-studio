"""Fetch scans and PageXML of chronicles from the Kronieken corpus (kronieken.transkribus.eu).

The corpus (project Chronicling Novelty, Leiden and VU) was typed by volunteers of Vele Handen in
Transkribus. The site gives per page, open and without login, the scan (IIIF) and the PageXML with line
outline, baseline and text. For every page with text this writes <out>/<call>_<page>.jpg and
<out>/page/<call>_<page>.xml, the layout Loghi's cutting tool wants (tools_train_set.py). A scan larger
than --max dots on its longest side is fetched smaller and the line positions are scaled to fit (the site
refuses a made-smaller copy of more than 10 million dots, the full scan it does give).

Usage: python -I tools_kronieken_fetch.py <out folder> <call number> [<call number> ...] [--max 6000] [--threads 3]
       python -I tools_kronieken_fetch.py --list            all documents on the site (id, pages, title)
"""
import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tools_public_scans import jpeg_size, scale_xml  # noqa: E402

API = "https://api-read.transkribus.eu"
COLLECTION = 175721
AGENT = {"User-Agent": "Mozilla/5.0 (training set check)", "Content-Type": "application/json"}


def get(url, body=None, tries=4):
    for n in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=AGENT)
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.read()
        except OSError as e:
            if n == tries - 1 or getattr(e, "code", 500) < 500:     # a refusal (4xx) does not get better by asking again
                raise
            time.sleep(5 * (n + 1))


def documents():
    body = json.dumps({"facets": ["f_title"], "filterFacets": [], "range": [], "groupDocId": 1,
                       "collections": [COLLECTION], "offset": 0, "limit": 1000}).encode()
    return {it["title"].split(" - ")[0].strip(): it for it in json.loads(get(f"{API}/facets", body))["items"]}


def lines_with_text(xml):
    return len(re.findall(r"<TextLine\b.*?<Unicode>\s*[^<\s]", xml, flags=re.S))


def page(out, call, doc, n, longest):
    stem = f"{call}_{n:04d}"
    jpg, xml_out, skip = out / f"{stem}.jpg", out / "page" / f"{stem}.xml", out / "_empty" / stem
    if (jpg.exists() and xml_out.exists()) or skip.exists():
        return "had"
    info = json.loads(get(f"{API}/documents/{doc}/pages/{n}"))
    text = get(info["content"]).decode("utf-8")
    if lines_with_text(text) < 3:
        skip.write_text("", encoding="utf-8")
        return "empty"
    w0, h0 = (int(re.search(rf'image{k}="(\d+)"', text).group(1)) for k in ("Width", "Height"))
    f = min(1.0, longest / max(w0, h0))
    if f < 1.0:
        f = min(f, (9_900_000 / (w0 * h0)) ** 0.5)          # maxArea of the image server for scaled copies
    size = "full" if f == 1.0 else f"{round(w0 * f)},"
    data = get(info["image"].rsplit("/", 1)[0] + f"/full/{size}/0/default.jpg")
    got = jpeg_size(data)
    if not got:
        return "bad image"
    if got != (w0, h0):
        ratio = (got[0] / w0) / (got[1] / h0)
        if not 0.98 < ratio < 1.02:
            return f"other shape {(w0, h0)} {got}"
        text = scale_xml(text, got[0] / w0, got[1] / h0, *got)
    text = re.sub(r'imageFilename="[^"]*"', f'imageFilename="{stem}.jpg"', text, count=1)
    xml_out.write_text(text, encoding="utf-8", newline="\n")
    jpg.write_bytes(data)                     # the scan last: both files present means the page is complete
    return "new"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    docs = documents()
    if "--list" in sys.argv:
        for call, it in sorted(docs.items()):
            print(it["id"], call, "|", it["title"])
        return

    def opt(name, default):
        return int(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default
    longest, threads = opt("--max", 6000), opt("--threads", 3)
    args = [a for i, a in enumerate(sys.argv[1:], 1) if not a.startswith("--") and not sys.argv[i - 1].startswith("--")]
    out = Path(args[0])
    for d in ("page", "_empty"):
        (out / d).mkdir(parents=True, exist_ok=True)
    for call in args[1:]:
        if call not in docs:
            print("not on the site:", call)
            continue
        doc = docs[call]["id"]
        count = json.loads(get(f"{API}/documents/{doc}"))["pageCount"]
        tally = {}

        def one(n):
            try:
                return page(out, call, doc, n, longest)
            except Exception as e:      # one page that fails must not stop the rest
                return f"failed: {type(e).__name__}"
        with ThreadPoolExecutor(threads) as pool:
            for i, res in enumerate(pool.map(one, range(1, count + 1)), 1):
                tally[res] = tally.get(res, 0) + 1
                if i % 25 == 0:
                    print(f"  {call} {i} of {count}", flush=True)
        print(f"{call}: {count} pages on the site | {tally}", flush=True)


if __name__ == "__main__":
    main()
