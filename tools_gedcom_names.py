"""Make an import file for the name finder from a GEDCOM file.

    python tools_gedcom_names.py <family tree.ged> <output.json> [people.json] [--all]

Without people.json every person in the GEDCOM is written. With people.json (a list of objects with an
"id" such as "@I11@") only those people are written. People who may still be alive are left out, also
as relatives of others: a person counts as dead with a death record, or with a first year more than 100
years ago. A person without any year and without a death record is left out too. With --all everybody is
written; use that only for a list that stays on your own computer. Per person: name, given names, surname, sex, other
names, the first year, a reference (WeRelate page title if there is one, otherwise the GEDCOM id) and the
names of the relatives (partners, parents and children). The name finder uses the relatives to tell
which hits are likely to be about this person.
"""
import json
import re
import sys
import time


def read(path):
    recs, cur = {}, None
    for line in open(path, encoding="utf-8-sig", errors="replace"):
        line = line.rstrip("\r\n")
        if line.startswith("0 "):
            parts = line.split()
            cur = []
            if len(parts) >= 3:
                recs[parts[1]] = (parts[2], cur)
        elif cur is not None:
            cur.append(line)
    return recs


def person(lines):
    p = {"aka": [], "fams": [], "famc": [], "years": []}
    name_no = 0
    for l in lines:
        if l.startswith("1 NAME"):
            name_no += 1
            n = l[7:].replace("/", " ")
            if name_no == 1:
                p["name"] = " ".join(n.split())
            else:
                p["aka"].append(" ".join(n.split()))
        elif l.startswith("2 GIVN") and name_no == 1:
            p["given"] = l[7:].strip()
        elif l.startswith("2 SURN") and name_no == 1:
            p["surname"] = l[7:].strip()
        elif l.startswith("1 SEX"):
            p["sex"] = l[6:].strip()[:1]
        elif l.startswith("1 _UID") and "Person:" in l:
            p["uid"] = l.split("Person:")[-1].strip()
        elif l.startswith("1 FAMS"):
            p["fams"].append(l.split()[2])
        elif l.startswith("1 FAMC"):
            p["famc"].append(l.split()[2])
        elif l.startswith(("1 DEAT", "1 BURI", "1 CREM")):
            p["dead"] = True
        elif l.startswith("2 DATE"):
            p["years"] += [int(y) for y in re.findall(r"\b(1[0-9]\d\d|20\d\d)\b", l)]
    return p


def gone(p, year_now=None):
    """True when the person is certainly not alive: a death record, or a first year more than 100 years ago."""
    return bool(p.get("dead")) or bool(p["years"]) and min(p["years"]) < (year_now or time.localtime().tm_year) - 100


def main(ged, out, only=None, everybody=False):
    recs = read(ged)
    people = {k: person(v) for k, (t, v) in recs.items() if t == "INDI"}
    fams = {}
    for k, (t, lines) in recs.items():
        if t == "FAM":
            fams[k] = ([l.split()[2] for l in lines if l.startswith(("1 HUSB", "1 WIFE"))],
                       [l.split()[2] for l in lines if l.startswith("1 CHIL")])
    ids = [p["id"] for p in json.load(open(only, encoding="utf-8"))] if only else list(people)
    result, left_out = [], 0
    for i in ids:
        p = people.get(i)
        if not p or not p.get("name"):
            continue
        if not everybody and not gone(p):
            left_out += 1
            continue
        rel = []
        for f in p["fams"]:
            parents, children = fams.get(f, ([], []))
            rel += [x for x in parents if x != i] + children
        for f in p["famc"]:
            rel += fams.get(f, ([], []))[0]
        names = []
        for r in rel:
            n = people.get(r, {}).get("name")
            if n and n not in names and (everybody or gone(people[r])):
                names.append(n)
        item = {"name": p["name"], "uid": p.get("uid") or i, "relatives": names}
        for k in ("given", "surname", "sex"):
            if p.get(k):
                item[k] = p[k]
        if p["aka"]:
            item["aka"] = p["aka"]
        if p["years"]:
            item["year"] = min(p["years"])
        result.append(item)
    json.dump(result, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{len(result)} people written to {out}" + (f"; {left_out} left out because they may be alive (--all writes them too)" if left_out else ""))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    args = [a for a in sys.argv[1:] if a != "--all"]
    main(*args[:3], everybody="--all" in sys.argv)
