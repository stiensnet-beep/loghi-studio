"""Make an enlarged work copy of one book for reading on Runpod.

The scans are enlarged (as the reading queue does) and the existing PageXML (lines already found) is scaled
up to match, so only the line snippets have to be cut again. The book folder itself is only read.

Usage: python tools_runpod_prep.py <book folder> <work folder> [factor=2]
"""
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import readqueue


def one(args):
    src, dst, factor = args
    if not dst.exists():
        readqueue.enlarge_scan(src, dst, factor)


def main():
    book, work = Path(sys.argv[1]), Path(sys.argv[2])
    factor = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    (work / "page").mkdir(parents=True, exist_ok=True)
    scans = sorted(p for p in book.iterdir() if p.suffix.lower() in (".jpg", ".jpeg"))
    for p in scans:
        xml = book / "page" / (p.stem + ".xml")
        if xml.exists():
            out = work / "page" / xml.name
            shutil.copyfile(xml, out)
            readqueue.shrink_page(out, 1 / factor)      # dividing by 1/factor scales up
    with ProcessPoolExecutor(max_workers=10) as ex:
        list(ex.map(one, [(p, work / p.name, factor) for p in scans]))
    print(len(scans), "scans enlarged", factor, "times into", work)


if __name__ == "__main__":
    main()
