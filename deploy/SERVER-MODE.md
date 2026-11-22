# Loghi-studio in server mode

Server mode is for showing books to other people over the internet: looking and searching behind a login.
Loghi itself does not run there, so the tabs Add, Queue and Collections are hidden and their requests are
refused. The login is home-made (see `auth.py`); judge for yourself whether that is enough for your use.

## What you need
- A machine with Docker and a reverse proxy that gives HTTPS (Caddy, nginx, Traefik). Loghi-studio itself
  speaks plain HTTP on port 8771 and must not be reachable without the proxy.
- A folder per collection with the books (scans, `page/*.xml`, `txt/*.txt`), mounted read-only.

## Folders next to `compose.yml`
- `app/`: `Dockerfile` and the files it copies (`server.py`, `auth.py`, `names.py`, `books.py`,
  `readqueue.py`, `corrections.py`, `reading_errors.json`, `index.html`, `login.html`).
- `data/<collection>/<book>/`: the books. Mounted read-only.
- `settings/`: `collections.json` (the roots are paths inside the container, `/data/<collection>`),
  `names.json`, `users.json` and `corrections/` (lines checked online). Must be writable for user 10001.
- `cache/`: search index and hits of the name finder; may be deleted, it is made again.

## Settings
- `LOGHI_MODE=server` switches server mode on (set in the Dockerfile).
- `LOGHI_HOSTS`: the host names the site is reached by, separated by commas. Requests for another host
  are refused. Change `loghistudio.example.org` in `compose.yml` to your own name.
- `LOGHI_STUDIO_SETTINGS`, `LOGHI_STUDIO_CACHE`: where the two folders are (set in the Dockerfile).

## Accounts
    python auth.py <settings folder> <name> <admin|corrector|viewer>

makes or replaces an account in `users.json` and prints the password once. An admin may change the lists of
names and check lines; a corrector may check lines; a viewer may look and search. Five wrong passwords give
a pause of 15 minutes for that name and that address. A session ends after 12 hours without use, or after
7 days. There is no registration and no "forgot password".

## Rights
Put only scans online that you may show. A collection whose scans are for your own use only (the field
`private` of the collection in `collections.json`) should not be copied to the server.
