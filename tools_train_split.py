"""Split the lines of one training set (lines.txt of tools_train_set.py) into train.txt and val.txt.

Whole pages go to one side, so the validation lines come from pages the model has not trained on.
Per source (the page name without its page number) every Nth page is kept for validation, and the
number of training lines can be capped: the pages are then taken evenly spread over the source.

Usage: python tools_train_split.py <data folder> <set name> [--val-every 12] [--val-max 300]
           [--cap SOURCE=LINES ...] [--cap-all LINES] [--add <other lines list> ...]
  --val-max   at most this many validation lines per source
  --cap       at most this many training lines of that source; --cap-all for every source without its own cap
  --add       lines of another list (for instance globalise_train lines) are added to train.txt as they are
Writes <data folder>/train.txt and val.txt and prints the numbers per source.
"""
import sys
from collections import defaultdict
from pathlib import Path


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    argv = sys.argv[1:]
    data, name = Path(argv[0]), argv[1]

    def opts(flag):
        return [argv[i + 1] for i, a in enumerate(argv) if a == flag]
    every = int((opts("--val-every") or [12])[0])
    val_max = int((opts("--val-max") or [300])[0])
    cap_all = int((opts("--cap-all") or [0])[0])
    caps = {k: int(v) for k, v in (c.split("=") for c in opts("--cap"))}
    pages = defaultdict(lambda: defaultdict(list))            # source -> page -> rows
    for row in (data / name / "lines.txt").read_text(encoding="utf-8").splitlines(keepends=True):
        page = row.split("\t")[0].split("/")[-2]
        pages[page.rsplit("_", 1)[0]][page].append(row)
    train, val = [], []
    for source in sorted(pages):
        names = sorted(pages[source])
        v, t = [], []
        for i, p in enumerate(names):
            (v if i % every == every // 2 else t).append(p)
        vrows = [r for p in v for r in pages[source][p]][:val_max]
        cap = caps.get(source, cap_all)
        total = sum(len(pages[source][p]) for p in t)
        if cap and total > cap:
            step = total / cap
            keep, nxt = [], 0.0
            for i, p in enumerate(t):                          # evenly spread: page i is kept when it passes the next mark
                if i >= nxt:
                    keep.append(p)
                    nxt += step
            t = keep
        trows = [r for p in t for r in pages[source][p]]
        train += trows
        val += vrows
        print(f"{source:20s} pages {len(names):4d} | train {len(t):4d} pages {len(trows):6d} lines | val {len(v):3d} pages {len(vrows):5d} lines")
    for extra in opts("--add"):
        rows = Path(extra).read_text(encoding="utf-8").splitlines(keepends=True)
        train += rows
        print(f"added {len(rows)} lines from {extra}")
    (data / "train.txt").write_text("".join(train), encoding="utf-8", newline="\n")
    (data / "val.txt").write_text("".join(val), encoding="utf-8", newline="\n")
    print(f"train.txt {len(train)} lines | val.txt {len(val)} lines")


if __name__ == "__main__":
    main()
