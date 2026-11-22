"""Put the translations of settings-independent interface texts into index.html.

English is the source language of the page. lang/translations.json holds, per English text, the Dutch (nl)
and Frisian (fy) text. This tool writes them into index.html between the markers /*I18N*/ and /*I18N-END*/,
so the page stays one file. It also checks that the placeholders ({0}, {1}, <kbd>, <strong>) are the same
in every language.

Usage: python tools_i18n_build.py
"""
import json
import re
from pathlib import Path

HERE = Path(__file__).parent


def marks(s):
    return sorted(re.findall(r"\{\w+\}|</?\w+>", s))


def main():
    data = json.loads((HERE / "lang" / "translations.json").read_text(encoding="utf-8"))
    out = {"nl": {}, "fy": {}}
    for key, tr in data.items():
        for lang in out:
            text = tr.get(lang)
            if not text:
                raise SystemExit(f"no {lang} text for: {key}")
            if marks(text) != marks(key):
                raise SystemExit(f"placeholders differ in {lang} for: {key}")
            out[lang][key] = text
    page = HERE / "index.html"
    s = page.read_text(encoding="utf-8")
    block = "/*I18N*/const I18N = " + json.dumps(out, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/") + ";/*I18N-END*/"
    new, n = re.subn(r"/\*I18N\*/.*?/\*I18N-END\*/", lambda m: block, s, count=1, flags=re.S)
    if n != 1:
        raise SystemExit("markers not found in index.html")
    page.write_text(new, encoding="utf-8", newline="\n")
    print(len(data), "texts in 2 languages written to index.html")


if __name__ == "__main__":
    main()
