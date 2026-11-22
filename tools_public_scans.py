"""Fetch the scans that belong to public ground truth of the VOC archive (Nationaal Archief, 1.04.02).

The public sets hold only PageXML; the scan has the same name and is on the site of the Nationaal Archief.
For every PageXML this writes <out>/<name>.jpg and <out>/page/<name>.xml, the layout Loghi's cutting tool
wants. When the scan on the site has another size than the PageXML says, the PageXML copy is scaled to fit.
The folder with the ground truth is only read.

Usage: python -I tools_public_scans.py <folder with PageXML> <out folder> [--limit N]
"""
import json
import re
import struct
import sys
import time
import urllib.request
from pathlib import Path

PAGE = "https://www.nationaalarchief.nl/onderzoeken/archief/1.04.02/invnr/{inv}/file/{name}"
AGENT = {"User-Agent": "Mozilla/5.0 (training set check)"}


def get(url, tries=3):
    for n in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=180) as r:
                return r.read()
        except OSError:
            if n == tries - 1:
                raise
            time.sleep(5 * (n + 1))


def scans_of(inv, name, cache):
    """Name of the scan -> address of its image, for one inventory number (kept in a small file)."""
    f = cache / f"{inv}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    html = get(PAGE.format(inv=inv, name=name)).decode("utf-8", "replace")
    q = r"\\u0022"
    found = {}
    for m in re.finditer(q + "label" + q + ":" + q + r"([^\\]+?)\.(?:tif|jpg)" + q + r".{0,900}?IIIF=(.+?\.jp2)", html):
        found[m.group(1)] = m.group(2).replace("\\/", "/")
    f.write_text(json.dumps(found), encoding="utf-8")
    return found


def jpeg_size(data):
    """Width and height of a JPEG, read from its header (no extra packages needed)."""
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


def scale_xml(text, fx, fy, w, h):
    def pts(m):
        out = []
        for p in m.group(2).split():
            x, y = p.split(",")
            out.append(f"{round(float(x) * fx)},{round(float(y) * fy)}")
        return m.group(1) + " ".join(out) + '"'
    text = re.sub(r'(points=")([^"]*)"', pts, text)
    text = re.sub(r'imageWidth="\d+"', f'imageWidth="{w}"', text, count=1)
    return re.sub(r'imageHeight="\d+"', f'imageHeight="{h}"', text, count=1)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    src, out = Path(args[0]), Path(args[1])
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
    (out / "page").mkdir(parents=True, exist_ok=True)
    cache = out / "_addresses"
    cache.mkdir(exist_ok=True)
    files = sorted(p for p in src.rglob("*.xml")
                   if p.name not in ("mets.xml", "metadata.xml") and not p.name.startswith("._"))
    if limit:
        files = files[:limit]
    done = scaled = missing = 0
    for p in files:
        stem = p.stem
        name = re.sub(r"-groot$", "", stem)
        m = re.match(r"NL-HaNA_1\.04\.02_([0-9A-Za-z]+)_\d+$", name)
        jpg = out / f"{stem}.jpg"
        xml_out = out / "page" / f"{stem}.xml"
        if jpg.exists() and xml_out.exists():
            done += 1
            continue
        if not m:
            print("name not understood:", stem)
            missing += 1
            continue
        try:
            address = scans_of(m.group(1), name, cache).get(name)
            if not address:
                print("not on the site:", name)
                missing += 1
                continue
            data = get(f"https://service.archief.nl/iip/iipsrv?IIIF={address}/full/full/0/default.jpg")
        except OSError as e:
            print("failed:", name, e)
            missing += 1
            continue
        size = jpeg_size(data)
        text = p.read_text(encoding="utf-8")
        mw = re.search(r'imageWidth="(\d+)"', text)
        mh = re.search(r'imageHeight="(\d+)"', text)
        if not size or not mw or not mh:
            print("size unknown:", name)
            missing += 1
            continue
        w0, h0 = int(mw.group(1)), int(mh.group(1))
        if (w0, h0) != size:
            ratio = (size[0] / w0) / (size[1] / h0)
            if not 0.98 < ratio < 1.02:
                print("other shape, left out:", name, (w0, h0), size)
                missing += 1
                continue
            text = scale_xml(text, size[0] / w0, size[1] / h0, *size)
            scaled += 1
        text = re.sub(r'imageFilename="[^"]*"', f'imageFilename="{stem}.jpg"', text, count=1)
        jpg.write_bytes(data)
        xml_out.write_text(text, encoding="utf-8", newline="\n")
        done += 1
        if done % 10 == 0:
            print(done, "of", len(files), flush=True)
        time.sleep(0.3)
    print("scans ready:", done, "| PageXML scaled to the scan:", scaled, "| missing:", missing)


if __name__ == "__main__":
    main()
