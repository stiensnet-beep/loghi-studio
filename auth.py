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
import secrets
import sys
import threading
import time
from pathlib import Path

ROLES = ("admin", "corrector", "viewer")
SESSION_IDLE = 12 * 3600        # a session ends after 12 hours without a request
SESSION_MAX = 7 * 86400         # and after 7 days in any case
MAX_FAILS = 5                   # wrong passwords before a pause
FAIL_PAUSE = 15 * 60            # seconds
WORDS = ("anker boek brug dijk eiken fluit gouden haven iepen kade lente molen noord oever pomp roer schip terp "
         "veld wad zeil akker bries dorp eb vloed greide hoeve klif meer sloot turf wier zomer sluis toren reiger "
         "kievit grutto zwaan otter linde wilg riet klaver boter kaas vlas hooi oogst ploeg schuur stal weide").split()


def make_password():
    """Four everyday words and a number: easy to say and to type, about 46 bits."""
    return "-".join(secrets.choice(WORDS) for _ in range(4)) + "-" + str(secrets.randbelow(900) + 100)


def _hash(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


class Auth:
    def __init__(self, settings_dir):
        self.file = Path(settings_dir) / "users.json"
        self.lock = threading.Lock()
        self.sessions = {}          # sha256(token) -> {"user", "role", "seen", "made"}
        self.fails = {}             # "ip:<ip>" or "user:<name>" -> [count, time of the last failure]
        self.users = {}
        self.load()

    def load(self):
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            self.users = {u["name"].lower(): u for u in data.get("users", []) if u.get("role") in ROLES}
        except (OSError, ValueError, KeyError, AttributeError):
            self.users = {}

    def set_user(self, name, role, password):
        name = str(name).strip()
        if not name or role not in ROLES or len(password) < 12:
            raise ValueError("A name, a role (admin, corrector or viewer) and a password of at least 12 characters are needed.")
        salt = os.urandom(16)
        with self.lock:
            self.load()
            self.users[name.lower()] = {"name": name, "role": role, "salt": salt.hex(), "hash": _hash(password, salt).hex()}
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"users": list(self.users.values())}, indent=1), encoding="utf-8")
            os.replace(tmp, self.file)

    # ------------------------------------------------------------ logging in
    def _blocked(self, key, now):
        n, last = self.fails.get(key, (0, 0))
        return n >= MAX_FAILS and now - last < FAIL_PAUSE

    def login(self, name, password, ip):
        """(token, user) after a right password; (None, reason) otherwise. Reasons: "wrong", "wait"."""
        now = time.time()
        name = str(name or "").strip()
        keys = (f"ip:{ip}", f"user:{name.lower()}")
        with self.lock:
            if any(self._blocked(k, now) for k in keys):
                return None, "wait"
            user = self.users.get(name.lower())
        # always do the work of one hash, so the answer takes as long for an unknown name
        salt = bytes.fromhex(user["salt"]) if user else b"0" * 16
        good = hmac.compare_digest(_hash(str(password or ""), salt).hex(), user["hash"] if user else "")
        with self.lock:
            if not (user and good):
                for k in keys:
                    n, last = self.fails.get(k, (0, 0))
                    self.fails[k] = [1 if now - last > FAIL_PAUSE else n + 1, now]
                return None, "wrong"
            for k in keys:
                self.fails.pop(k, None)
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
            s = self.sessions.get(key)
            if not s:
                return None
            if now - s["seen"] > SESSION_IDLE or now - s["made"] > SESSION_MAX or s["user"].lower() not in self.users:
                del self.sessions[key]
                return None
            s["seen"] = now
            return {"user": s["user"], "role": s["role"]}

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
