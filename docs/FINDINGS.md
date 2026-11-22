# What we measured on our own books

These are notes of one project, not a study. The books are court, orphan and mortgage books of Frisian
lower courts (Het Bildt and others, mostly 17th century), scans of about 2,000 dots wide from Tresoar. All
runs used Loghi 2.3.2. Read the reservations at the end before you lean on a number.

## The yardstick
The index of AlleFriezen (fetched through Open Archives) names the people in every deed of three mortgage
books of Het Bildt (1606 to 1639) and the scan on which the deed starts. Those names were read by people.
For every word of those names (4 letters or more) we count whether the text Loghi read on that scan and
the next one holds it: right (the same after folding the spelling), nearly (one or two letters off) or
missing. `tools_index_recall.py` does the counting.

The measure has a ceiling below 100 %: the index sometimes spells a name in a modern way, and a deed can
run on past the second scan.

## 1. Enlarging the scan before the line finder
On scans of a large folio with small writing, the line finder (Laypa, model baseline2) missed most lines:
one double page had 11 lines in the PageXML where the scan has about 100. In the three mortgage books
31 to 60 % of the scans had fewer than 25 lines; ordinary books have a median of 66 to 76 lines a scan.

Enlarging the scan twice (Lanczos) before the line finder and the cutting of the lines, and scaling the
positions back afterwards, repaired this:
- Four scans, 38 name words of the index: 4 right with the old lines; 24 right, 9 nearly and 6 missing
  after enlarging (91 to 106 lines a scan). A sharper copy of 4,000 dots from the archive gave the same
  (25 right), so enlarging the scan one already has is enough.
- A whole book of 239 scans read again enlarged: 18,068 lines against 15,357, and 545,163 letters against
  354,643, that is 54 % more text.

The reading queue of Loghi-studio can read a book enlarged.

## 2. Which reading model
Forty indexed scans (41 deeds, 401 name words), all read enlarged twice on the same lines:

| model | right | nearly | missing |
|---|---|---|---|
| generic_new_17 (2023-05-25) | 59 % | 19 % | 21 % |
| GLOBALISE (2023-08) | 66 % | 17 % | 17 % |

On one whole book (597 deeds) the difference was smaller: 59 % right for generic_new_17 and 61 % for
GLOBALISE, both enlarged. GLOBALISE read at the size of the scan gave 58 %: its gain only shows on the
enlarged line images.

## 3. Transkribus as a comparison
The 25 scans of that book on which our best reading found the fewest names (25 deeds, 296 name words) were
read with the free Dutch model of Transkribus, The Dutchess I, at the size of the scan:

| reading | right | nearly | missing |
|---|---|---|---|
| generic_new_17, enlarged | 33 % | 25 % | 42 % |
| GLOBALISE, enlarged | 34 % | 25 % | 41 % |
| Transkribus, The Dutchess I | 30 % | 22 % | 48 % |

The scans were chosen where GLOBALISE did badly, which works against GLOBALISE. The paid models of
Transkribus were not tried, and Transkribus did not get the enlarged scan.

## 4. Fine-tuning on public ground truth
- GLOBALISE ground truth (doi:10.34894/IQ0YMT; 2,938 lines, 3 epochs): 8.10 % of the letters wrong before
  and 8.11 % after on its validation lines. As expected: that set was used to make the model.
- Kronieken corpus (Chronicling Novelty, Leiden; text typed by volunteers; the site
  kronieken.transkribus.eu gives PageXML with baselines per page). Seven chronicles of 1600 to 1650,
  36,712 training lines with the GLOBALISE lines mixed in, 2 epochs on top of GLOBALISE: 8.58 % of the
  letters wrong before and 8.02 % after on 1,712 lines of chronicle pages that were not trained on. On
  the forty indexed scans of our own books: 63 % of the name words right, against 66 % for GLOBALISE
  itself. So the model learned the chronicles a little better and our books not at all.

Our conclusion so far: public sets do not lift the reading of these books; what is left is lines of the
books themselves, checked by people. The Correct tab is made for that. It has not yet been shown that
this works.

## 5. Time and money on a rented graphics card
Line finding, cutting, reading and merging for 239 scans (enlarged twice, 18,238 lines) took 16 minutes
of work on rented cards (line finding 0.57 seconds a scan on an RTX 4000 Ada; reading 30 lines a second on
an RTX 2000 Ada, 100 a second on an RTX 3090), about 0.2 dollar. Fine-tuning on 36,712 lines took 41
minutes an epoch on an A40. The scripts are in `runpod_jobs/` and `tools_runpod_*`.

## Reservations
- The samples are small. On 401 name words a difference of 3 points is 12 words.
- One collection, one region, mostly hands of 1606 to 1639.
- The measure counts names, not all letters, and has a ceiling that we did not measure.
- Everything was done by one person with an AI assistant; nobody else has repeated it.
