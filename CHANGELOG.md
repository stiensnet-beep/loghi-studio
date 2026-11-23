# Changes

Version numbers: 0.x is beta (trial version). 1.0 comes when adding, the queue and automatic starting work and have been tested.

## Unreleased

## 0.6.1-beta (23 Nov 2026)
- Correct: what was saved last stays in sight below the buttons ("Saved just now"), with a button to
  withdraw that save (Alt+Z) and one to go back to that line. Saving always goes on to the next line; that
  was easy to miss.
- Correct: a line in which nothing was changed can no longer be saved as partly corrected (it happened by
  pressing the button twice). The buttons are now called "Save, only some words corrected" and "Next line,
  without saving".

## 0.6.0-beta (22 Nov 2026)
- After a second look by the other model: an address from which a name logged in before is not slowed down
  when strangers guess at that name; a changed role counts at once, not after the next login; in server mode
  the lists of names show nothing of a collection that is hidden; an unforeseen error is written to the log
  with the place in the code; after saving in Correct the confirmation and the next line are one message.
- After a review by another model (Claude Opus 5.5), server mode: collections with scans for own use only do
  not exist there; searching with a regular expression is off there; many wrong passwords for a name slow that
  name down for everyone but no longer lock it out (an address with five wrong attempts still waits 15
  minutes); an account made while the server runs works at once.
- A damaged PageXML file gives a plain message instead of a dropped connection; messages no longer show
  the inside of Python; a request with a negative length is refused and a silent connection ends after a minute.
- Docker containers are named after the copy that started them (`loghi-studio-<code>-...`), so two copies on
  one computer leave each other's containers alone.
- `tools_gedcom_names.py` leaves out people who may still be alive, also as relatives (`--all` writes everybody).
- The README no longer claims a light theme or a contrast of 12:1 without the button High contrast, and names
  Pillow (needed for reading a book enlarged and for the tests).
- Made ready to be shared: a licence (MIT, Johan Dijkstra), a README for others with pictures, `docs/FINDINGS.md` (what we
  measured), `deploy/SERVER-MODE.md`, and a tool in the working repository that makes the public copy and
  stops when a file still holds a private detail.
- A first start no longer looks for the folders of the first user: the list of collections starts empty
  and the tab Collections opens.
- On a first visit the page follows the language of the browser (Dutch, Frisian, otherwise English).
- The copy of the checked lines to git is now something you switch on, with the file
  `settings/git_backup.on`. Without that file nothing is committed or pushed.
- Correct: a new order, "Lines that two readers read differently", for a book that has a second reading
  (a folder `cache/second/<collection>/<book>/page/` with PageXML of another model, or the text the book had
  before it was read again). The smallest differences come first. The screen shows both texts with the
  words that differ in bold and underlined, says the difference in words, and Alt+O puts the text of the
  other reader in the field. Every line is still decided on the scan by a person.
- Correct: new button **Only some words corrected** (Alt + W), for a line where you can correct one or a
  few words but cannot read the rest with certainty. The correction is shown in View and Search and in
  the name finder, the line is counted apart ("partly corrected") and it never becomes a training line.
- Search: new mode **Names in old spellings**. Every word is also searched in the old spellings the name
  finder makes (Pieter, Pyter, Pytter; Hayes, Hayesen, Hayesdr; c or k, z or s, v or f), and one or two
  letters may differ. The words that made the hit are marked.
- Search: when the collection has an **index of names made by people**, the deeds that name the person
  are listed above the text hits, with date, folio, the people of the deed and a button to the scan.
  Those names were read by people, so a person is found where Loghi read the name wrong.
- The lines checked by people can be kept in git as a copy (`settings/corrections/`): the server on the own
  computer then commits and pushes that folder, ten minutes after the last check. Since the entry above
  this is something you switch on.
- View: the text beside the scan can be made wider or narrower: drag the bar between scan and text, use
  the buttons Text wider and Text narrower, or the arrow keys on the bar. The width is remembered.
- View: **select a piece** of the scan. Drag a box over the scan (button Select a piece, the key S, or
  drag with Shift held) and the text pane shows only the lines in the box; the scan zooms to the piece.
  With the keyboard: Shift + up or down from the chosen line. Copy the piece; Esc shows all lines again.
- New tab **Correct**: check a line against the scan. The line is cut from the scan and shown very large
  above the field with the text; Enter saves and goes to the next line that nobody checked yet (in reading
  order, or unsure lines first). A line can be right as read, corrected or unreadable; a check can be
  withdrawn. Loghi's own files are never changed: checks are kept in `settings/corrections/`. View, Search
  and the name finder show the checked text with a tag in words. Works with the keyboard alone and with a
  screen reader; three languages; Lighthouse accessibility 100.
- **Training set** from the checked lines, in the format Loghi trains on: ground truth, line images cut by
  Loghi's own tool, and list files with path, tab, text. Collections with scans for own use only
  (FamilySearch) are marked, and so is their training set.
- A table shows per book how many lines were checked and how many letters Loghi read wrong in them.
- With an index of names made by people (AlleFriezen) the Correct tab shows the names of the deed on the
  scan as a help, and can go straight to the lines with a name that was nearly read right.
- Server mode: new role `corrector` (may check lines, nothing else).
- Fixed: the Log out button was visible on the own computer; the header no longer sticks out of a narrow
  screen with large text.
- Name finder: hits with a **possible reading error**. One word of the name is there as usual and the other
  only fits when Loghi made its typical reading errors in it (u for n, y for i, S for P). Which letter
  changes are typical is learned from the AlleFriezen index (`reading_errors.json`, made by
  `tools_reading_errors.py`). These hits are kept apart: two new choices under Which hits show them, with
  or without a relative on the same scan; they never count as strong, as a tag in View or as support for
  somebody else. Measured on a book the learning did not see: people of the index found on their scan
  62 % -> 70 %; the separate list holds about half as much noise as the usual hits.
- New look: one calm dark blue theme, thinner lines, softer
  contrast (still far above the accessibility minimum). The light theme and the theme chooser are gone.
- Progress: one block per book, as in the old viewer. Each state has its own shape (filled, half, round,
  empty) and a count in words; arrow keys move between blocks, Enter opens the book.
- Reading queue: a book can be read again from copies of the scans that are twice as large. On large
  pages with small writing the line finder then finds about 100 lines per scan instead of 10 to 30. The
  scans themselves are not changed; the old PageXML is kept as .xml.bak. (For now only through the API:
  POST /api/queue with "enlarge": 2.)
- Home page as the first tab: slogan, a search box, a short text about Loghi-studio, numbers and the
  collections. The page now presents itself as a reader of old Frisian documents, with a small pompeblêd.
- Three languages: Dutch (default), Frisian and English, chosen at the top right. All fixed texts are
  translated, and the changing texts of Home, View, Search, Progress and the hits of the Name finder.
  Still English: changing texts of Add, Queue, Collections and the lists of names, and messages that come
  from the server. Translations are in lang/translations.json; tools_i18n_build.py writes them into the page.
- High contrast switch for low vision (black, white, yellow, thicker lines); follows the system setting
  until it is chosen by hand. Lighthouse accessibility audit: 100 on Home, View and Queue.

## 0.5.0-beta (20 Nov 2026)
- Name finder: several lists of names. Each list can be switched on and off, renamed and removed.
- Import many names at once, from a file or pasted text. Loghi-studio makes the search forms itself
  (first name with patronymic, first name with surname, old spellings) and keeps the reference to the
  family tree, so a hit links back to the page of that person. `tools_gedcom_names.py` makes an import
  file with relatives from a GEDCOM file.
- Hits are shown per person, collapsed, with numbers; filters on list, strength, book and name; 50 people
  per page. All hits can be downloaded as a CSV file.
- A hit is marked "with a relative" when a partner, parent or child is named on the same scan. For a
  very common name (more than 100 strong hits) only those hits are shown by default.
- A name that Loghi read as two words ("Sij brensz"), also across the end of a line, is found now.
- Much faster: about 90 seconds for 718 names in 27,500 scans (the old code needed 80 seconds for 89
  names). After a change only the changed names are searched again.
- The limit of 5,000 hits per book, which dropped hits without telling, is gone. The limit is now 300
  hits per name per book, and the page tells when it was reached.

## 0.4.0-beta (20 Nov 2026)
- New tab Queue: books are read by Loghi from the page, one at a time. Change the order, pause and resume,
  reading model per book, number of threads (by day and at night), expected finishing time.
- A book is read in chunks of 20 scans, so after a stop or a restart only the running chunk is read again.
- A scan that fails is tried once more and then skipped; skipped scans can be tried again on request.
  Scans whose read failed in an earlier run (lines found, but never read) are found automatically.
- The queue waits while Docker is not running or while another Loghi run is active, and continues by itself.
- Add: a new book can go straight into the reading queue.
- The whole application is in English now (interface, messages, file names in `settings/`).
  Existing Dutch settings files are converted automatically on the first start.

## 0.3.0-beta (20 Nov 2026)
- New tab Add: add a book from a folder with scans (a copy; the source is left untouched)
  or download it from Tresoar with finding aid and inventory number.
- Jobs run in the background, one at a time, with progress and a Stop button; an interrupted job continues
  after a restart. 25 GB of disk space always stays free.
- The downloader detects whether a finding aid uses scan numbers of 6, 5 or 4 digits.
- Shortcut on the desktop and an explanation about a bookmark in the footer.

## 0.2.0-beta (20 Nov 2026)
- The name finder is now part of Loghi-studio: manage the list of names in the page (name, other spellings,
  search mode fuzzy or exact, per collection or everywhere).
- Searches in the background: at start, after every change of the list and every 10 minutes for newly read books.
- Every hit is strong or weak, based on the spelling and on how sure Loghi is of the line.
- Short names (4 letters or fewer) must match exactly; "welke" is no longer found as Eelck.
- The hits of the old search scripts can still be seen through Source: Old search scripts.

## 0.1.0-beta (20 Nov 2026)
- First version: View, Search, Name finder, Progress, Collections.
- Collections in a settings file instead of in the code.
- Accessible page: large text, high contrast, keyboard, labels for the screen reader.
- Unsure lines marked with [?] and [??].
