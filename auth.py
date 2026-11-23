"""Accounts and sessions for Loghi-studio when it is reachable from outside this computer (server mode).

On the own computer Loghi-studio needs no login: it only listens on 127.0.0.1. In server mode every request
needs a session. There are three roles: "admin" may change the lists of names and check lines, "corrector"
may check lines against the scan, "viewer" may only look and search. Accounts are in settings/users.json;
only a salted scrypt hash of a password is stored.

Make or change an account (the password is made here and shown once):
    python auth.py <settings folder> <name> <admin|corrector|viewer>
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path

ROLES = ("admin", "corrector", "viewer")       # from most to least
NAME_OK = r"[\w][\w .\-]{1,39}"
SESSION_IDLE = 12 * 3600        # a session ends after 12 hours without a request
SESSION_MAX = 7 * 86400         # and after 7 days in any case
MAX_FAILS = 5                   # wrong passwords before a pause
FAIL_PAUSE = 15 * 60            # seconds
NAME_GAP = 5                # seconds between attempts for a name that had that many wrong passwords
KNOWN_DAYS = 30             # an address is trusted for a name this long after a right password
WORDS = ("anker boek brug dijk eiken fluit gouden haven iepen kade lente molen noord oever pomp roer schip terp "
         "veld wad zeil akker bries dorp eb vloed greide hoeve klif meer sloot turf wier zomer sluis toren reiger "
         "kievit grutto zwaan otter linde wilg riet klaver boter kaas vlas hooi oogst ploeg schuur stal weide").split()


def make_password():
    """Four everyday words and a number: easy to say and to type, about 33 bits. Enough together with the limits on wrong
    attempts in Auth.login; not meant for a place without such limits."""
    return "-".join(secrets.choice(WORDS) for _ in range(4)) + "-" + str(secrets.randbelow(900) + 100)


def _hash(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def _loose(name):
    """A name as people hear it: "Jan Smit", "Jan  Smit" and "Jan.Smit" are the same."""
    return re.sub(r"[\s.\-]+", " ", str(name).lower()).strip()


class Auth:
    def __init__(self, settings_dir):
        self.file = Path(settings_dir) / "users.json"
        self.lock = threading.Lock()
        self.sessions = {}          # sha256(token) -> {"user", "role", "seen", "made"}
        self.fails = {}             # "ip:<ip>", "user:<name>", "own:<name>" or "ask:<ip>" -> [count, time of the last one]
        self.known = {}             # (name, ip) -> time of the last right password from that address
        self.users = {}
        self._stamp = None
        self._fresh()

    def load(self):
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            self.users = {u["name"].lower(): u for u in data.get("users", []) if u.get("role") in ROLES}
        except (OSError, ValueError, KeyError, AttributeError):
            self.users = {}

    def _save(self):
        """Write users.json (the caller holds the lock)."""
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"users": list(self.users.values())}, indent=1), encoding="utf-8")
        os.replace(tmp, self.file)

    def set_user(self, name, role, password):
        name = str(name).strip()
        if not name or role not in ROLES or len(password) < 12:
            raise ValueError("A name, a role (admin, corrector or viewer) and a password of at least 12 characters are needed.")
        salt = os.urandom(16)
        with self.lock:
            self.load()
            self.users[name.lower()] = {"name": name, "role": role, "salt": salt.hex(), "hash": _hash(password, salt).hex()}
            self._save()

    # ------------------------------------------------------------ accounts, managed by an admin in the page
    def accounts(self):
        """[{"name", "role", "asked"}]: asked is the time the person asked for a new password, or None."""
        with self.lock:
            self._fresh()
            return sorted(({"name": u["name"], "role": u["role"], "asked": u.get("asked")} for u in self.users.values()),
                          key=lambda u: u["name"].lower())

    def _admins(self):
        return [u for u in self.users.values() if u["role"] == "admin"]

    def _end_sessions(self, key, keep=None):
        """End every session of the account with this key, except the one with the token `keep` (caller holds the lock)."""
        mine = hashlib.sha256(keep.encode()).hexdigest() if keep else None
        self.sessions = {k: s for k, s in self.sessions.items() if s["user"].lower() != key or k == mine}

    def _still_admin(self, by):
        """The admin who asks must still be admin now, under the lock: the role may have changed since the request came in."""
        if by is not None and (self.users.get(str(by).lower()) or {}).get("role") != "admin":
            raise PermissionError("not allowed for this account")

    def create(self, name, role, by=None):
        """A new account; gives the password, which is kept nowhere."""
        name = str(name or "").strip()
        if not re.fullmatch(NAME_OK, name):
            raise ValueError("A name is 2 to 40 letters, digits, spaces, dots or dashes.")
        if role not in ROLES:
            raise ValueError("Choose a role: admin, corrector or viewer.")
        password, salt = make_password(), os.urandom(16)
        with self.lock:
            self._fresh()
            self._still_admin(by)
            if name.lower() in self.users:
                raise ValueError("There is an account with this name already.")
            near = next((u["name"] for u in self.users.values() if _loose(u["name"]) == _loose(name)), None)
            if near:
                raise ValueError(f"There is an account with nearly the same name already: {near}.")
            self.users[name.lower()] = {"name": name, "role": role, "salt": salt.hex(), "hash": _hash(password, salt).hex()}
            self._end_sessions(name.lower())      # a session left over from an earlier account with this name
            self._save()
        return password

    def set_role(self, name, role, by=None):
        if role not in ROLES:
            raise ValueError("Choose a role: admin, corrector or viewer.")
        with self.lock:
            self._fresh()
            self._still_admin(by)
            u = self.users.get(str(name or "").strip().lower())
            if not u:
                raise KeyError("This account does not exist.")
            if u["role"] == "admin" and role != "admin" and len(self._admins()) == 1:
                raise ValueError("This is the only admin. Make another account admin first.")
            u["role"] = role
            self._save()

    def new_password(self, name, keep=None, by=None):
        """Another password for an account; gives it. The sessions of that account end, except the one with
        the token `keep` (an admin who gives himself a new password stays logged in to read it)."""
        password, salt = make_password(), os.urandom(16)
        with self.lock:
            self._fresh()
            self._still_admin(by)
            key = str(name or "").strip().lower()
            u = self.users.get(key)
            if not u:
                raise KeyError("This account does not exist.")
            u.update(salt=salt.hex(), hash=_hash(password, salt).hex())
            u.pop("asked", None)
            self._save()
            self._end_sessions(key, keep)
            self.fails.pop(f"user:{key}", None)
        return password

    def remove(self, name, by=None):
        with self.lock:
            self._fresh()
            self._still_admin(by)
            key = str(name or "").strip().lower()
            u = self.users.get(key)
            if not u:
                raise KeyError("This account does not exist.")
            if u["role"] == "admin" and len(self._admins()) == 1:
                raise ValueError("This is the only admin. Make another account admin first.")
            del self.users[key]
            self._end_sessions(key)
            self._save()

    def drop_ask(self, name, by=None):
        """Set a request for a new password aside, without giving one."""
        with self.lock:
            self._fresh()
            self._still_admin(by)
            u = self.users.get(str(name or "").strip().lower())
            if not u:
                raise KeyError("This account does not exist.")
            if u.pop("asked", None):
                self._save()

    def ask_reset(self, name, ip):
        """Somebody who forgot the password asks the admin for a new one. Says nothing about whether the name
        exists; an address may ask MAX_FAILS times per FAIL_PAUSE."""
        now, key = time.time(), f"ask:{ip}"
        with self.lock:
            self._fresh()
            n, last = self.fails.get(key, (0, 0))
            if n >= MAX_FAILS and now - last < FAIL_PAUSE:
                return
            self.fails[key] = [1 if now - last > FAIL_PAUSE else n + 1, now]
            self._prune(now)
            u = self.users.get(str(name or "").strip().lower())
            if u and not u.get("asked"):
                u["asked"] = int(now)
                self._save()

    def verify(self, name, password):
        """Is this the password of this account? True or False; "out" after MAX_FAILS wrong ones in a row: the
        sessions of the account then end, and logging in again has its own limits. Asked again before an admin
        changes accounts. It has its own count per name, apart from logging in: a slip here must not keep
        the address from logging in, and guessing from many addresses with a stolen session must not go on."""
        key, now = str(name or "").strip().lower(), time.time()
        with self.lock:
            self._fresh()
            user = self.users.get(key)
        salt = bytes.fromhex(user["salt"]) if user else b"0" * 16
        good = hmac.compare_digest(_hash(str(password or ""), salt).hex(), user["hash"] if user else "")
        with self.lock:
            if user and good:
                self.fails.pop(f"own:{key}", None)
                return True
            n, last = self.fails.get(f"own:{key}", (0, 0))
            n = 1 if now - last > FAIL_PAUSE else n + 1
            if n >= MAX_FAILS:
                self.fails.pop(f"own:{key}", None)
                self._end_sessions(key)
                return "out"
            self.fails[f"own:{key}"] = [n, now]
            return False

    def act(self, token, role):
        """Look as a lower role for the rest of the session (an admin as corrector or viewer, a corrector as
        viewer), or go back to the own role. Never upwards."""
        if role not in ROLES:
            raise ValueError("Choose a role: admin, corrector or viewer.")
        with self.lock:
            s = self.sessions.get(hashlib.sha256((token or "").encode()).hexdigest())
            u = self.users.get(s["user"].lower()) if s else None
            if not u:
                raise KeyError("log in first")
            if ROLES.index(role) < ROLES.index(u["role"]):
                raise ValueError("This account may not take that role.")
            s["as"] = None if role == u["role"] else role

    # ------------------------------------------------------------ logging in
    def _fresh(self):
        """Read users.json again when it changed: an account made while the server runs works at once."""
        try:
            st = self.file.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp != self._stamp:
            self._stamp = stamp
            self.load()

    def _prune(self, now):
        """Forget the counts of long ago (the caller holds the lock)."""
        if len(self.fails) > 2000:
            self.fails = {k: v for k, v in self.fails.items() if now - v[1] < FAIL_PAUSE}

    def _blocked(self, key, now):
        n, last = self.fails.get(key, (0, 0))
        return n >= MAX_FAILS and now - last < FAIL_PAUSE

    def login(self, name, password, ip):
        """(token, user) after a right password; (None, reason) otherwise. Reasons: "wrong", "wait", "slow".

        An address with MAX_FAILS wrong attempts waits FAIL_PAUSE. A name with that many wrong attempts, from
        whatever address, is only slowed down (one attempt per NAME_GAP seconds): a stranger who knows a name
        must not be able to keep its owner out. An address from which that name logged in before (in the last
        KNOWN_DAYS days, since the server started) is not slowed down at all, so the owner gets in at home even
        while strangers keep guessing from many addresses."""
        now = time.time()
        name = str(name or "").strip()
        keys = (f"ip:{ip}", f"user:{name.lower()}")
        with self.lock:
            self._fresh()
            if self._blocked(keys[0], now):
                return None, "wait"
            n, last = self.fails.get(keys[1], (0, 0))
            at_home = now - self.known.get((name.lower(), ip), 0) < KNOWN_DAYS * 86400
            if n >= MAX_FAILS and now - last < NAME_GAP and not at_home:
                return None, "slow"
            user = self.users.get(name.lower())
        # always do the work of one hash, so the answer takes as long for an unknown name
        salt = bytes.fromhex(user["salt"]) if user else b"0" * 16
        good = hmac.compare_digest(_hash(str(password or ""), salt).hex(), user["hash"] if user else "")
        with self.lock:
            if not (user and good):
                for k in keys:
                    n, last = self.fails.get(k, (0, 0))
                    self.fails[k] = [1 if now - last > FAIL_PAUSE else n + 1, now]
                self._prune(now)
                return None, "wrong"
            for k in keys:
                self.fails.pop(k, None)
            self.known[(name.lower(), ip)] = now
            if len(self.known) > 2000:
                self.known = {k: t for k, t in self.known.items() if now - t < KNOWN_DAYS * 86400}
            token = secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = {"user": user["name"], "role": user["role"],
                                                                         "seen": now, "made": now}
            if len(self.sessions) > 500:        # drop the sessions that ended
                self.sessions = {k: s for k, s in self.sessions.items()
                                 if now - s["seen"] < SESSION_IDLE and now - s["made"] < SESSION_MAX}
            return token, {"user": user["name"], "role": user["role"]}

    def session(self, token):
        if not token:
            return None
        key, now = hashlib.sha256(token.encode()).hexdigest(), time.time()
        with self.lock:
            self._fresh()
            s = self.sessions.get(key)
            if not s:
                return None
            if now - s["seen"] > SESSION_IDLE or now - s["made"] > SESSION_MAX or s["user"].lower() not in self.users:
                del self.sessions[key]
                return None
            s["seen"] = now
            real = self.users[s["user"].lower()]["role"]            # the role of now, not of the login
            acting = s.get("as")
            if acting and ROLES.index(acting) < ROLES.index(real):  # the own role was lowered meanwhile
                acting = s["as"] = None
            return {"user": s["user"], "role": acting or real, "real": real}

    def logout(self, token):
        if token:
            with self.lock:
                self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)


def main():
    if len(sys.argv) != 4 or sys.argv[3] not in ROLES:
        raise SystemExit(__doc__)
    password = make_password()
    Auth(sys.argv[1]).set_user(sys.argv[2], sys.argv[3], password)
    print(f"Account {sys.argv[2]} ({sys.argv[3]}) saved. Password: {password}")


if __name__ == "__main__":
    main()
