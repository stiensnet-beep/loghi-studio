"""Look into a folder of public ground truth (PageXML) and say what is in it.

Counts pages, regions, lines, lines with text and letters; lists the characters the reading model does
not know; groups the pages by the start of their file name (archive and inventory number).
The folders are downloads from others: run with `python -I` and only read them.

Usage: python -I tools_public_gt.py <folder with PageXML> [charlist.txt of the model] [--groups N]
"""
import collections
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def local(tag):
    return tag.rsplit("}", 1)[-1]


def line_text(line):
    """Text of a TextLine: its own TextEquiv/Unicode (not that of its words)."""
    for child in line:
        if local(child.tag) == "TextEquiv":
            for u in child:
                if local(u.tag) == "Unicode":
                    return u.text or ""
    return None


def read_page(path):
    out = {"regions": 0, "lines": 0, "text_lines": 0, "chars": 0, "baselines": 0, "words": 0,
           "texts": [], "image": None, "size": None, "creator": None, "changed": None, "region_types": []}
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        out["broken"] = True
        return out
    for el in root.iter():
        name = local(el.tag)
        if name == "Page":
            out["image"] = el.get("imageFilename")
            out["size"] = (el.get("imageWidth"), el.get("imageHeight"))
        elif name == "Creator":
            out["creator"] = (el.text or "").strip()[:60]
        elif name == "LastChange":
            out["changed"] = (el.text or "")[:10]
        elif name == "TextRegion":
            out["regions"] += 1
            m = re.search(r"structure\s*\{[^}]*type:([^;}]+)", el.get("custom") or "")
            out["region_types"].append(m.group(1).strip() if m else (el.get("type") or "-"))
        elif name == "TextLine":
            out["lines"] += 1
            if any(local(c.tag) == "Baseline" for c in el):
                out["baselines"] += 1
            if any(local(c.tag) == "Word" for c in el):
                out["words"] += 1
            text = line_text(el)
            if text is not None and text.strip():
                out["text_lines"] += 1
                out["chars"] += len(text.strip())
                out["texts"].append(text.strip())
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    folder = Path(args[0])
    known = None
    if len(args) > 1:
        known = set(Path(args[1]).read_text(encoding="utf-8").replace("\n", "").replace("\r", ""))
        known.add(" ")
    groups_n = 12
    if "--groups" in sys.argv:
        groups_n = int(sys.argv[sys.argv.index("--groups") + 1])
    sys.stdout.reconfigure(encoding="utf-8")        # -I ignores PYTHONIOENCODING
    files = sorted(p for p in folder.rglob("*.xml")
                   if p.name not in ("mets.xml", "metadata.xml") and not p.name.startswith("._"))
    tot = collections.Counter()
    groups = collections.Counter()
    group_lines = collections.Counter()
    creators = collections.Counter()
    years = collections.Counter()
    types = collections.Counter()
    unknown = collections.Counter()
    lengths = []
    sizes = []
    samples = []
    for p in files:
        d = read_page(p)
        if d.get("broken"):
            tot["broken"] += 1
            continue
        tot["pages"] += 1
        for k in ("regions", "lines", "text_lines", "chars", "baselines", "words"):
            tot[k] += d[k]
        if d["text_lines"]:
            tot["pages_with_text"] += 1
        g = re.sub(r"[_-]\d+(-groot)?$", "", p.stem)
        g = re.sub(r"^(NL-HaNA_1\.04\.02)_.*", r"\1", g) if "--full" not in sys.argv else g
        groups[g] += 1
        group_lines[g] += d["text_lines"]
        creators[d["creator"]] += 1
        years[(d["changed"] or "")[:4]] += 1
        types.update(d["region_types"])
        if d["size"] and d["size"][0]:
            sizes.append((int(d["size"][0]), int(d["size"][1])))
        for t in d["texts"]:
            lengths.append(len(t))
            if known is not None:
                for ch in t:
                    if ch not in known:
                        unknown[ch] += 1
        if d["texts"] and len(samples) < 4 and tot["pages"] % 37 == 1:
            samples.append((p.name, d["image"], d["texts"][:3]))
    print("folder:", folder)
    print("pages:", tot["pages"], "| with text:", tot["pages_with_text"], "| broken:", tot["broken"])
    print("regions:", tot["regions"], "| lines:", tot["lines"], "| with baseline:", tot["baselines"],
          "| with words:", tot["words"])
    print("lines with text:", tot["text_lines"], "| letters:", tot["chars"],
          "| mean letters a line:", round(tot["chars"] / tot["text_lines"], 1) if tot["text_lines"] else 0)
    if sizes:
        ws = sorted(w for w, _ in sizes)
        print("scan width: median", ws[len(ws) // 2], "min", ws[0], "max", ws[-1])
    print("made with:", creators.most_common(4))
    print("year of last change:", sorted(years.items()))
    print("region types:", types.most_common(12))
    print("groups (pages, lines with text):")
    for g, n in groups.most_common(groups_n):
        print("  ", g, n, group_lines[g])
    print("   number of groups:", len(groups))
    if known is not None:
        print("letters the model does not know:", sum(unknown.values()), "in all;",
              [(c, hex(ord(c)), n) for c, n in unknown.most_common(25)])
    for name, image, texts in samples:
        print("sample", name, "| image:", image)
        for t in texts:
            print("    ", t[:110])


if __name__ == "__main__":
    main()
