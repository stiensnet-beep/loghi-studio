"""Turn a folder with scans and PageXML (ground truth made by people) into training lines for Loghi-htr.

The folder holds <name>.jpg and page/<name>.xml (as tools_public_scans.py makes, or the gt folder of the
training set from the Correct tab). Loghi's own cutting tool (Docker) writes lines/<name>/<line>.png with
a .txt next to it; this script then writes lines.txt: "<path on the training machine><tab><text>".
Letters the base model does not know are replaced by their plain form (list below); a line that still
holds an unknown letter is left out and counted, because Loghi-htr would skip it without saying so.

Usage: python tools_train_set.py <folder> <set name> <model folder> [--cpus N] [--no-cut]
The paths in lines.txt start with /work/data/<set name>/.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import readqueue

# Plain forms for signs the GLOBALISE and generic models were not trained on.
PLAIN = {"’": "'", "‘": "'", "´": "'", "“": '"', "”": '"', "·": ".",
         "⁄": "/", "\t": " ", "æ": "ae"}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    folder, name, model = Path(args[0]).resolve(), args[1], Path(args[2])
    cpus = int(sys.argv[sys.argv.index("--cpus") + 1]) if "--cpus" in sys.argv else 4
    cfg = json.loads((model / "config.json").read_text(encoding="utf-8"))
    channels = int(cfg.get("model_channels") or cfg.get("args", {}).get("channels") or 4)
    tokens = json.loads((model / "tokenizer.json").read_text(encoding="utf-8"))
    known = set(tokens.values() if isinstance(tokens, dict) else tokens)      # {"0": "[PAD]", "3": " ", ...}
    known.add(" ")
    mount = f"/work/data/{name}"
    if "--no-cut" not in sys.argv:
        (folder / "tmp").mkdir(exist_ok=True)
        cmd = ["docker", "run", "--rm", "--name", f"trainset-cut-{name}", f"--cpus={cpus}",
               "-v", f"{folder.as_posix()}:{mount}", readqueue.IMG_TOOL,
               f"{readqueue.BIN}/MinionCutFromImageBasedOnPageXMLNew", "-input_path", mount,
               "-outputbase", f"{mount}/lines/", "-tmpdir", f"{mount}/tmp", "-output_type", "png",
               "-channels", str(channels), "-threads", str(cpus), "-write_text_contents", "-no_page_update",
               "-skip_unclear", readqueue.NS]
        with open(folder / "cut.log", "w", encoding="utf-8") as log:
            rc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
        if rc != 0:
            sys.exit(f"The cutting step failed; see {folder / 'cut.log'}")
    rows, unknown, empty = [], {}, 0
    for png in sorted((folder / "lines").rglob("*.png")):
        txt = png.with_suffix(".txt")
        text = txt.read_text(encoding="utf-8") if txt.exists() else ""
        for a, b in PLAIN.items():
            text = text.replace(a, b)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            empty += 1
            continue
        bad = set(text) - known
        if bad:
            for ch in bad:
                unknown[ch] = unknown.get(ch, 0) + 1
            continue
        rows.append(f"{mount}/{png.relative_to(folder).as_posix()}\t{text}\n")
    (folder / "lines.txt").write_text("".join(rows), encoding="utf-8", newline="\n")
    print(f"{name}: {len(rows)} lines in lines.txt | without text: {empty} | "
          f"left out for an unknown letter: {sum(unknown.values())} {sorted(unknown.items())}")


if __name__ == "__main__":
    main()
