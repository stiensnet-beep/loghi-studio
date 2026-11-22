"""Enlarge the scans of one book into its work folder (the same way the reading queue does).

Usage: python enlarge.py <book folder> <work folder> [factor=2]
A scan that is already in the work folder is skipped, so the job can be started again.
"""
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from PIL import Image

Image.MAX_IMAGE_PIXELS = None


def one(args):
    src, dst, factor = args
    if dst.exists():
        return 0
    with Image.open(src) as im:
        im = im.convert("RGB")
        if factor > 1:
            im = im.resize((im.width * factor, im.height * factor), Image.LANCZOS)
        tmp = dst.with_suffix(".part")
        im.save(tmp, "JPEG", quality=92)
        tmp.rename(dst)
    return 1


def main():
    book, work = Path(sys.argv[1]), Path(sys.argv[2])
    factor = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    work.mkdir(parents=True, exist_ok=True)
    scans = sorted(p for p in book.iterdir() if p.suffix.lower() in (".jpg", ".jpeg"))
    with ProcessPoolExecutor(max_workers=8) as ex:
        made = sum(ex.map(one, [(p, work / (p.stem + ".jpg"), factor) for p in scans]))
    print(len(scans), "scans,", made, "enlarged", factor, "times into", work, flush=True)


if __name__ == "__main__":
    main()
