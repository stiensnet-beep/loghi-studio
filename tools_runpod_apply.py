"""Put pages that were read again on Runpod into their book, the same way the reading queue does.

For every page: the old page/<scan>.xml is kept as <scan>.xml.bak, the new PageXML (already scaled back to
the scan) is copied in, txt/<scan>.txt is written again and the marker file notes that the scan was read
enlarged. A page whose lines were found but never read is left as it is. Nothing happens when one of the
pages already has a .xml.bak (an older text would be lost); look at that book by hand first.
Only with the owner's yes for that book: this changes the text of a book.

Usage: python tools_runpod_apply.py <folder with the new page/*.xml> <book folder> [factor=2] [--dry-run]
"""
import os
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import readqueue


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    src, book = Path(args[0]), Path(args[1])
    factor = int(args[2]) if len(args) > 2 else 2
    dry = "--dry-run" in sys.argv
    scans = {p.stem for p in book.iterdir() if p.suffix.lower() in (".jpg", ".jpeg")}
    pages = sorted(p for p in (src / "page").glob("*.xml") if p.stem in scans)
    stray = sorted(p.name for p in (src / "page").glob("*.xml") if p.stem not in scans)
    if stray:
        sys.exit(f"{len(stray)} pages have no scan in {book} (first: {stray[0]}); wrong book?")
    old_bak = [p.stem for p in pages if (book / "page" / f"{p.stem}.xml.bak").exists()]
    if old_bak:
        sys.exit(f"{len(old_bak)} pages already have a .xml.bak (first: {old_bak[0]}); nothing changed.")
    good, bad = [], []
    for xml in pages:
        try:
            lines, read, text = readqueue.page_lines(xml)
        except (OSError, ET.ParseError):
            bad.append(xml.stem)
            continue
        if lines and not read:
            bad.append(xml.stem)
            continue
        good.append((xml, text))
    print(f"{book}: {len(scans)} scans, {len(pages)} new pages, {len(good)} to place, {len(bad)} left as they are"
          + (f" ({', '.join(bad[:8])})" if bad else ""))
    if dry:
        return
    (book / "page").mkdir(exist_ok=True)
    (book / "txt").mkdir(exist_ok=True)
    for xml, text in good:
        dest = book / "page" / xml.name
        if dest.exists():
            os.replace(dest, dest.with_suffix(".xml.bak"))
        tmp = dest.with_suffix(".xml.part")
        shutil.copyfile(xml, tmp)
        os.replace(tmp, dest)
        tmp = book / "txt" / f"{xml.stem}.txt.part"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, book / "txt" / f"{xml.stem}.txt")
    if factor > 1 and good:
        marker = readqueue.read_marker(book)
        marker.setdefault("enlarged", {}).update({xml.stem: factor for xml, _ in good})
        readqueue.write_marker(book, marker)
    print(f"placed {len(good)} pages; the old text is in page/<scan>.xml.bak")


if __name__ == "__main__":
    main()
