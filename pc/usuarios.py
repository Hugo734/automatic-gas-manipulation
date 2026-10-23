"""
User accounts and login sessions for the valve web panel.

Accounts live in pc/usuarios.json (never commit it). Passwords are stored as
PBKDF2-SHA256 hashes with a random salt.

Roles:
    operador  can see the panel and move the valve
    lector    can only see the panel

Usage (run on the server PC):
    python pc/usuarios.py agregar juan --rol operador
    python pc/usuarios.py contrasena juan
    python pc/usuarios.py rol juan lector
    python pc/usuarios.py quitar juan
    python pc/usuarios.py lista
"""

import argparse
import getpass
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usuarios.json")
ROLES = ("operador", "lector")
PBKDF2_ITERATIONS = 200_000
MIN_PASSWORD_LEN = 8
SESSION_IDLE_S = 8 * 3600      # one shift
MAX_FAILURES = 5
LOCKOUT_S = 300


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return salt.hex(), digest.hex()


class UserStore:
    def __init__(self, path=USERS_FILE):
        self.path = path
        self.lock = threading.Lock()
        self.users = {}
        self._mtime = None
        self.load()

    def load(self):
        """Re-read the file if it changed, so edits made with this script apply while the server runs."""
        mtime = os.path.getmtime(self.path) if os.path.exists(self.path) else None
        if mtime != self._mtime:
            self.users = {}
            if mtime is not None:
                with open(self.path, encoding="utf-8") as f:
                    self.users = json.load(f)
            self._mtime = mtime

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.users, f, indent=2)
        os.replace(tmp, self.path)

    def set_user(self, name, password, role):
        salt, digest = hash_password(password)
        self.users[name] = {"salt": salt, "hash": digest, "role": role}
        self.save()

    def check(self, name, password):
        """Return the role if the password is right, else None."""
        with self.lock:
            self.load()
            user = self.users.get(name)
        if not user:
            hash_password(password)  # same cost either way, so timing does not reveal valid names
            return None
        _, digest = hash_password(password, bytes.fromhex(user["salt"]))
        return user["role"] if hmac.compare_digest(digest, user["hash"]) else None

    def role_of(self, name):
        with self.lock:
            self.load()
            user = self.users.get(name)
        return user["role"] if user else None


class Sessions:
    """In-memory login sessions plus a per-IP lockout after repeated bad passwords."""

    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.sessions = {}   # token -> {"user", "last"}
        self.failures = {}   # ip -> [count, first_failure_time]

    def locked_out(self, ip):
        with self.lock:
            count, since = self.failures.get(ip, (0, 0))
            if count >= MAX_FAILURES and time.time() - since < LOCKOUT_S:
                return True
            if time.time() - since >= LOCKOUT_S:
                self.failures.pop(ip, None)
            return False

    def login(self, ip, name, password):
        """Return a session token, or None if the login failed."""
        if self.store.check(name, password) is None:
            with self.lock:
                count, since = self.failures.get(ip, (0, time.time()))
                self.failures[ip] = [count + 1, since]
            return None
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.failures.pop(ip, None)
            self.sessions[token] = {"user": name, "last": time.time()}
        return token

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)

    def get(self, token):
        """Return (user, role) for a valid session, or None."""
        with self.lock:
            s = self.sessions.get(token)
            if not s or time.time() - s["last"] > SESSION_IDLE_S:
                self.sessions.pop(token, None)
                return None
            s["last"] = time.time()
            user = s["user"]
        role = self.store.role_of(user)
        if role is None:  # account was deleted while logged in
            self.logout(token)
            return None
        return user, role


def ask_password():
    while True:
        p1 = getpass.getpass("Contraseña nueva: ")
        if len(p1) < MIN_PASSWORD_LEN:
            print(f"Debe tener al menos {MIN_PASSWORD_LEN} caracteres.")
            continue
        if getpass.getpass("Repetir contraseña: ") != p1:
            print("No coinciden, intenta de nuevo.")
            continue
        return p1


def main():
    ap = argparse.ArgumentParser(description="Usuarios del panel web de la válvula")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("agregar", help="crear un usuario")
    a.add_argument("nombre")
    a.add_argument("--rol", choices=ROLES, required=True)
    c = sub.add_parser("contrasena", help="cambiar la contraseña de un usuario")
    c.add_argument("nombre")
    r = sub.add_parser("rol", help="cambiar el rol de un usuario")
    r.add_argument("nombre")
    r.add_argument("rol", choices=ROLES)
    q = sub.add_parser("quitar", help="borrar un usuario")
    q.add_argument("nombre")
    sub.add_parser("lista", help="ver los usuarios")
    args = ap.parse_args()

    store = UserStore()
    users = store.users
    if args.cmd == "lista":
        if not users:
            print("No hay usuarios.")
        for name, u in sorted(users.items()):
            print(f"  {name:20s} {u['role']}")
        return
    name = args.nombre.strip().lower()
    if args.cmd == "agregar":
        if name in users:
            raise SystemExit(f"El usuario '{name}' ya existe.")
        store.set_user(name, ask_password(), args.rol)
        print(f"Usuario '{name}' creado como {args.rol}.")
    elif name not in users:
        raise SystemExit(f"El usuario '{name}' no existe.")
    elif args.cmd == "contrasena":
        store.set_user(name, ask_password(), users[name]["role"])
        print("Contraseña cambiada.")
    elif args.cmd == "rol":
        users[name]["role"] = args.rol
        store.save()
        print(f"'{name}' ahora es {args.rol}.")
    elif args.cmd == "quitar":
        del users[name]
        store.save()
        print(f"Usuario '{name}' borrado.")


if __name__ == "__main__":
    main()
