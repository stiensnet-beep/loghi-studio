# Loghi-studio in server mode

Server mode is for showing books to other people over the internet: looking and searching behind a login.
Loghi itself does not run there, so the tabs Add, Queue and Collections are hidden and their requests are
refused. The login is home-made (see `auth.py`); judge for yourself whether that is enough for your use.

## What you need
- A machine with Docker and a reverse proxy that gives HTTPS (Caddy, nginx, Traefik).
- A host name of its own for the site. A path such as `example.org/loghi/` does not work.
- The books: per collection a folder with one folder per book (scans, `page/*.xml`, `txt/*.txt`).

## Step by step
1. Put this repository on the server and go to the folder `deploy/`.
2. Make three folders there: `data/`, `settings/` and `cache/`. The container runs as user 10001 and must be
   able to write the last two: `chown -R 10001:10001 settings cache`.
3. Copy the books to `data/<collection>/<book>/`. They are mounted read-only.
4. Make `settings/collections.json`. The roots are paths inside the container:

       {"collections": [
         {"id": "bildt", "title": "Het Bildt", "description": "", "years": "1591-1811",
          "root": "/data/bildt", "log": "/data/bildt/batch.log", "hits": []}
       ]}

5. In `compose.yml`, change `loghistudio.example.org` to your own host name (`LOGHI_HOSTS`; more names
   separated by commas). Requests for another host name are refused.
6. Make an account first; without one the server stops as soon as it starts. The image is built from the
   files in the folder above, and the password is printed once:

       docker compose build
       docker compose run --rm loghistudio python auth.py /settings <name> <admin|corrector|viewer>

   An account made later, while the server runs, works at once; no restart is needed.
7. Start it: `docker compose up -d`.
8. Let the reverse proxy pass `https://<your host name>` on to `127.0.0.1:8771`, with the `Host` header
   unchanged and the address of the visitor added to `X-Forwarded-For`.

## Good to know
- `compose.yml` opens port 8771 on this machine only. Never open it to the internet: Loghi-studio speaks
  plain HTTP there, and it believes the address in `X-Forwarded-For`, which only your proxy should set.
- Roles: an admin may change the lists of names and check lines; a corrector may check lines; a viewer may
  look and search.
- Wrong passwords: an address with five wrong attempts waits 15 minutes. A name with five wrong attempts,
  from whatever address, is slowed down to one attempt in five seconds, but it is never locked out.
  A session ends after 12 hours without use, or after 7 days. There is no registration and no "forgot
  password".
- A collection marked as scans for own use only (the field `private` in `collections.json`) does not exist
  in server mode: it is not listed, not searched and its scans are not given. Better still: do not copy
  such scans to the server at all.
- Searching with a regular expression is switched off in server mode.
- The lists of names of the name finder (`settings/names.json`) can be read by every account, with the
  relatives and references in them. Do not put people in them who may still be alive.
  `tools_gedcom_names.py` leaves those out by itself.
- `cache/` may be deleted; it is made again.

## Rights
Put only scans online that you may show.
