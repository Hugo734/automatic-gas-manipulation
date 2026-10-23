"""
Web control panel for the S4 valve. Serves dashboard/valvula.html and a small
JSON API, and is the only process that writes to the PLC (same register map as
valve_control.py / plc/README.md).

Every page and API call needs a login (accounts: pc/usuarios.py). Only the
"operador" role may write; "lector" can only watch.

    GET  /login            login page          POST /login   form user + password
    POST /logout
    GET  /                 control page
    GET  /api/status       current state (polled by the page every second)
    GET  /api/history      last 10 minutes of setpoint / position
    POST /api/setpoint     {"pct": 0..100} or {"ma": 4..20}      (operador)
    POST /api/enable       {"on": true | false}                  (operador)
    POST /api/close        setpoint 0 (4 mA)                     (operador)

Usage:
    python pc/usuarios.py agregar juan --rol operador
    python pc/valve_web.py --plc 192.168.1.22
    then open http://<this PC's IP>:8080 from any computer on the network.
    With --cert/--key it serves https:// instead.
"""

import argparse
import collections
import http.cookies
import json
import os
import ssl
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from usuarios import Sessions, UserStore
from valve_control import (HEARTBEAT_PERIOD_S, REG_ENABLE, REG_HEARTBEAT, REG_SETPOINT,
                           ModbusTcp, Valve, counts_to_ma, ma_to_counts, pct_to_counts)

DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dashboard")
PAGE = os.path.join(DASHBOARD_DIR, "valvula.html")
LOGIN_PAGE = os.path.join(DASHBOARD_DIR, "login.html")
COOKIE = "valve_session"
HISTORY_POINTS = int(600 / HEARTBEAT_PERIOD_S)  # 10 minutes
LOG_LINES = 50


class Controller:
    """Keeps the PLC connection alive, sends the heartbeat and caches the latest status."""

    def __init__(self, host, port, unit):
        self.plc = ModbusTcp(host, port, unit)
        self.valve = Valve(self.plc)
        self.lock = threading.Lock()
        self.connected = False
        self.ever_connected = False
        self.attempts = 0
        self.last_try = 0.0
        self.last_error = ""
        self.state = {}
        self.history = collections.deque(maxlen=HISTORY_POINTS)
        self.log = collections.deque(maxlen=LOG_LINES)
        self._prev_alarms = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        """Close the valve and hand control back to the PLC."""
        self._stop.set()
        with self.lock:
            if self.connected:
                self.valve.stop()
            self.plc.close()

    def add_log(self, text):
        self.log.appendleft({"t": time.time(), "text": text})

    def _connect(self):
        """Try to open the PLC socket. Runs without the lock (can take the full timeout),
        which is safe because nothing else touches the socket while connected is False."""
        self.attempts += 1
        self.last_try = time.time()
        try:
            self.plc.connect()
        except OSError as exc:
            if self.last_error != str(exc):
                self.add_log(f"No responde el PLC: {exc}")
            self.last_error = str(exc)
            self.plc.close()
            self.history.append([time.time(), None, None])
            return
        with self.lock:
            self.connected = True
            self.ever_connected = True
            self.last_error = ""
            self.add_log("Conectado al PLC")

    def _loop(self):
        counter = 0
        while not self._stop.is_set():
            counter = (counter + 1) % 30000
            if not self.connected:
                self._connect()
            else:
                with self.lock:
                    try:
                        self._read_state()  # before the write, so %MW3 has had a full period to follow %MW1
                        self.plc.write_register(REG_HEARTBEAT, counter)
                    except OSError as exc:
                        self.add_log(f"Se perdió la comunicación con el PLC: {exc}")
                        self.connected = False
                        self.last_error = str(exc)
                        self.plc.close()
                        self.history.append([time.time(), None, None])
            self._stop.wait(HEARTBEAT_PERIOD_S)

    def _read_state(self):
        setpoint, heartbeat, enable, seen = self.plc.read_holding(REG_SETPOINT, 4)
        s = self.valve.status()
        s.update({
            "setpoint_pct": setpoint / 100,
            "setpoint_ma": counts_to_ma(setpoint),
            "enabled": enable == 1,
            "heartbeat_ok": seen == heartbeat,
        })
        self.state = s
        self.history.append([time.time(), s["output_pct"], s["feedback_pct"]])
        alarms = set(s["alarms"])
        for a in alarms - self._prev_alarms:
            self.add_log(f"ALARMA: {a}")
        for a in self._prev_alarms - alarms:
            self.add_log(f"Se quitó la alarma: {a}")
        self._prev_alarms = alarms

    def snapshot(self):
        with self.lock:
            return {
                "connected": self.connected,
                "ever_connected": self.ever_connected,
                "attempts": self.attempts,
                "last_try": self.last_try,
                "error": "" if self.connected else self.last_error,
                "plc": f"{self.plc.host}:{self.plc.port}",
                "state": self.state if self.connected else None,
                "log": list(self.log),
                "time": time.time(),
            }

    def write(self, action, counts=None, client=""):
        with self.lock:
            if not self.connected:
                raise ConnectionError("No hay comunicación con el PLC")
            if action == "enable":
                self.plc.write_register(REG_SETPOINT, 0)  # always take control from closed
                self.plc.write_register(REG_ENABLE, 1)
                self.add_log(f"Control habilitado por {client} (válvula en 4 mA)")
            elif action == "disable":
                self.plc.write_register(REG_SETPOINT, 0)
                self.plc.write_register(REG_ENABLE, 0)
                self.add_log(f"Control liberado por {client}: válvula cerrada")
            elif action == "close":
                self.valve.set_counts(0)
                self.add_log(f"CERRAR por {client}")
            else:
                self.valve.set_counts(counts)
                self.add_log(f"Orden {counts / 100:.1f} % ({counts_to_ma(counts):.2f} mA) por {client}")


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # plain http to an https port, dropped connections: nothing the operator can act on


def make_handler(ctrl, sessions, secure):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # keep the console for PLC events only

        def _send(self, code, body, ctype="application/json; charset=utf-8", headers=()):
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _file(self, path):
            with open(path, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")

        def _redirect(self, location, cookie=None):
            headers = [("Location", location)]
            if cookie is not None:
                headers.append(("Set-Cookie", cookie))
            self._send(303, b"", "text/plain", headers)

        def _cookie(self, token, max_age):
            c = f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
            return c + ("; Secure" if secure else "")

        def _token(self):
            jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            return jar[COOKIE].value if COOKIE in jar else ""

        def _who(self):
            """(user, role) of the logged-in session, or None."""
            return sessions.get(self._token())

        def _path(self):
            return urllib.parse.urlsplit(self.path).path

        def do_GET(self):
            path = self._path()
            if path == "/login":
                self._file(LOGIN_PAGE)
                return
            who = self._who()
            if path in ("/", "/index.html"):
                if who:
                    self._file(PAGE)
                else:
                    self._redirect("/login")
            elif not who:
                self._send(401, {"error": "Sesión vencida, vuelve a entrar"})
            elif path == "/api/status":
                snap = ctrl.snapshot()
                snap["user"], snap["role"] = who
                self._send(200, snap)
            elif path == "/api/history":
                with ctrl.lock:
                    self._send(200, list(ctrl.history))
            else:
                self._send(404, {"error": "No encontrado"})

        def _login(self):
            ip = self.client_address[0]
            if sessions.locked_out(ip):
                self._redirect("/login?error=bloqueado")
                return
            length = int(self.headers.get("Content-Length", 0))
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            name = form.get("usuario", [""])[0].strip().lower()
            token = sessions.login(ip, name, form.get("contrasena", [""])[0])
            if token:
                ctrl.add_log(f"Entró {name} ({ip})")
                self._redirect("/", self._cookie(token, 12 * 3600))
            else:
                ctrl.add_log(f"Intento de entrada fallido: '{name}' ({ip})")
                self._redirect("/login?error=" + ("bloqueado" if sessions.locked_out(ip) else "1"))

        def do_POST(self):
            path = self._path()
            if path == "/login":
                self._login()
                return
            if path == "/logout":
                sessions.logout(self._token())
                self._redirect("/login", self._cookie("", 0))
                return
            who = self._who()
            if not who:
                self._send(401, {"error": "Sesión vencida, vuelve a entrar"})
                return
            if who[1] != "operador":
                self._send(403, {"error": "Tu usuario solo puede ver, no operar la válvula"})
                return
            # A plain HTML form from another site cannot send JSON, so this blocks forged requests
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._send(415, {"error": "Se esperaba JSON"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                client = f"{who[0]} ({self.client_address[0]})"
                if path == "/api/setpoint":
                    if "ma" in body:
                        counts = ma_to_counts(float(body["ma"]))
                    else:
                        counts = pct_to_counts(float(body["pct"]))
                    ctrl.write("setpoint", counts, client)
                elif path == "/api/enable":
                    ctrl.write("enable" if body.get("on") else "disable", client=client)
                elif path == "/api/close":
                    ctrl.write("close", client=client)
                else:
                    self._send(404, {"error": "No encontrado"})
                    return
                self._send(200, {"ok": True})
            except (ValueError, KeyError, TypeError) as exc:
                self._send(400, {"error": f"Valor inválido: {exc}"})
            except OSError as exc:
                self._send(503, {"error": str(exc)})

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Panel web para controlar la válvula S4 desde el navegador")
    ap.add_argument("--plc", required=True, help="dirección IP del TM221ME16R")
    ap.add_argument("--port", type=int, default=502, help="puerto Modbus del PLC")
    ap.add_argument("--unit", type=int, default=1, help="Modbus unit id")
    ap.add_argument("--web-port", type=int, default=8080, help="puerto de la página web")
    ap.add_argument("--bind", default="0.0.0.0",
                    help="0.0.0.0 = cualquier PC de la red puede entrar; 127.0.0.1 = solo esta PC")
    ap.add_argument("--cert", help="certificado .pem para usar https")
    ap.add_argument("--key", help="llave privada .pem del certificado")
    args = ap.parse_args()

    store = UserStore()
    if not store.users:
        raise SystemExit("No hay usuarios. Crea uno primero:\n"
                         "  python pc/usuarios.py agregar <nombre> --rol operador")
    sessions = Sessions(store)

    ctrl = Controller(args.plc, args.port, args.unit)
    server = PanelServer((args.bind, args.web_port), make_handler(ctrl, sessions, bool(args.cert)))
    scheme = "http"
    if args.cert:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(args.cert, args.key)
        # Handshake on first read, inside each connection's thread, so a bad client cannot stall accept()
        server.socket = tls.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        scheme = "https"
    ctrl.start()
    print(f"PLC: {args.plc}:{args.port} (si no está conectado, el panel abre igual y sigue intentando)")
    print(f"Panel web: {scheme}://<IP de esta PC>:{args.web_port}   (Ctrl+C para salir)")
    if not args.cert:
        print("AVISO: sin --cert las contraseñas viajan sin cifrar por la red.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
        ctrl.stop()
        print("Válvula enviada a cerrar (4 mA). Desconectado.")


if __name__ == "__main__":
    main()
