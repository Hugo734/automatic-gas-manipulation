"""
PC-side control for the S4 modulating valve through a TM221ME16R + TM3AQ2.

Talks Modbus TCP directly (no external libraries). Register map must match
plc/README.md:

    %MW0  setpoint      0..10000  (0.00..100.00 % open  ==  4.00..20.00 mA)
    %MW1  heartbeat     PC increments it; PLC closes the valve if it stops
    %MW2  enable        1 = PC controls the valve, 0 = force closed (4 mA)
    %MW10 output        value actually written to %QW1.0 (read only)
    %MW11 feedback raw  %IW0.0, 0..1000 (read only)
    %MW12 feedback      0..10000 = 0.00..100.00 % actual position (read only)
    %MW13 status        bit0 watchdog, bit1 position error, bit2 feedback fault,
                        bit3 in position, bit4 limit CLOSED, bit5 limit OPEN
    %MW14 deviation     %MW10 - %MW12 (read only)

Usage:
    python valve_control.py --ip 192.168.1.50 interactive
    python valve_control.py --ip 192.168.1.50 sweep --timeout 60
"""

import argparse
import socket
import struct
import threading
import time

REG_SETPOINT = 0
REG_HEARTBEAT = 1
REG_ENABLE = 2
REG_STATUS_BLOCK = 10  # %MW10..%MW14

HEARTBEAT_PERIOD_S = 0.5

STATUS_BITS = {
    0: "WATCHDOG",
    1: "ERROR DE POSICION",
    2: "FALLA SEÑAL FEEDBACK",
}


class ModbusTcp:
    """Minimal Modbus TCP client: FC03 read holding registers, FC06 write single register."""

    def __init__(self, host, port=502, unit=1, timeout=2.0):
        self.host, self.port, self.unit, self.timeout = host, port, unit, timeout
        self.sock = None
        self.tid = 0
        self.lock = threading.Lock()

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def _request(self, pdu):
        with self.lock:
            self.tid = (self.tid + 1) & 0xFFFF
            mbap = struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, self.unit)
            self.sock.sendall(mbap + pdu)
            header = self._recv_exact(7)
            tid, _, length, _ = struct.unpack(">HHHB", header)
            body = self._recv_exact(length - 1)
            if tid != self.tid:
                raise IOError(f"Transaction id mismatch ({tid} != {self.tid})")
            if body[0] & 0x80:
                raise IOError(f"Modbus exception code {body[1]} for function {body[0] & 0x7F}")
            return body

    def _recv_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("El PLC cerró la conexión")
            data += chunk
        return data

    def read_holding(self, address, count):
        body = self._request(struct.pack(">BHH", 3, address, count))
        return list(struct.unpack(f">{count}h", body[2:2 + 2 * count]))

    def write_register(self, address, value):
        self._request(struct.pack(">BHH", 6, address, value & 0xFFFF))


def ma_to_counts(ma):
    if not 4.0 <= ma <= 20.0:
        raise ValueError("la corriente debe estar entre 4 y 20 mA")
    return round((ma - 4.0) / 16.0 * 10000)


def pct_to_counts(pct):
    if not 0.0 <= pct <= 100.0:
        raise ValueError("el porcentaje debe estar entre 0 y 100")
    return round(pct * 100)


def counts_to_ma(counts):
    return 4.0 + counts / 10000 * 16.0


class Valve:
    def __init__(self, plc):
        self.plc = plc
        self._stop = threading.Event()
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)

    def start(self):
        self.plc.write_register(REG_SETPOINT, 0)
        self.plc.write_register(REG_ENABLE, 1)
        self._hb_thread.start()

    def stop(self):
        """Close the valve (4 mA) and hand control back to the PLC."""
        self._stop.set()
        try:
            self.plc.write_register(REG_SETPOINT, 0)
            self.plc.write_register(REG_ENABLE, 0)
        except OSError:
            pass  # PLC watchdog closes the valve anyway

    def _heartbeat_loop(self):
        counter = 0
        while not self._stop.is_set():
            counter = (counter + 1) % 30000
            try:
                self.plc.write_register(REG_HEARTBEAT, counter)
            except OSError as exc:
                print(f"\n[heartbeat] error de comunicación: {exc}")
            self._stop.wait(HEARTBEAT_PERIOD_S)

    def set_counts(self, counts):
        self.plc.write_register(REG_SETPOINT, counts)

    def status(self):
        out, fb_raw, fb, st, dev = self.plc.read_holding(REG_STATUS_BLOCK, 5)
        return {
            "output_ma": counts_to_ma(out),
            "output_pct": out / 100,
            "feedback_raw": fb_raw,
            "feedback_ma": fb_raw / 50,  # 500 ohm: 1 count = 10 mV = 0.02 mA
            "feedback_pct": fb / 100,
            "deviation_pct": dev / 100,
            "alarms": [name for bit, name in STATUS_BITS.items() if st & (1 << bit)],
            "in_position": bool(st & (1 << 3)),
            "limit_closed": bool(st & (1 << 4)),
            "limit_open": bool(st & (1 << 5)),
        }


def print_status(valve):
    s = valve.status()
    flags = []
    if s["in_position"]:
        flags.append("EN POSICION")
    if s["limit_closed"]:
        flags.append("FC CERRADA")
    if s["limit_open"]:
        flags.append("FC ABIERTA")
    flags += [f"ALARMA {a}" for a in s["alarms"]]
    print(f"  orden {s['output_ma']:5.2f} mA ({s['output_pct']:6.2f} %) | "
          f"posición {s['feedback_pct']:6.2f} % ({s['feedback_ma']:5.2f} mA) | "
          f"desviación {s['deviation_pct']:+6.2f} %  {' '.join(flags)}")
    return s


def run_interactive(valve):
    print("Comandos:  ma <4..20>   pct <0..100>   abrir   cerrar   estado   "
          "leer <n° %MW> [cantidad]   salir")
    while True:
        try:
            line = input("valvula> ").strip().lower()
        except EOFError:
            break
        if not line:
            continue
        cmd, *args = line.split()
        try:
            if cmd in ("salir", "quit", "exit", "q"):
                break
            elif cmd == "ma":
                valve.set_counts(ma_to_counts(float(args[0])))
            elif cmd == "pct":
                valve.set_counts(pct_to_counts(float(args[0])))
            elif cmd in ("abrir", "open"):
                valve.set_counts(10000)
            elif cmd in ("cerrar", "close"):
                valve.set_counts(0)
            elif cmd in ("leer", "read"):
                start = int(args[0])
                count = int(args[1]) if len(args) > 1 else 1
                for i, value in enumerate(valve.plc.read_holding(start, count)):
                    print(f"  %MW{start + i} = {value}")
                continue
            elif cmd not in ("estado", "status"):
                print("Comando desconocido")
                continue
            print_status(valve)
        except (ValueError, IndexError) as exc:
            print(f"Valor inválido: {exc}")


def run_sweep(valve, steps_ma, timeout_s):
    """Step through the list and measure how long the valve takes to reach each position."""
    results = []
    for ma in steps_ma:
        print(f"-> {ma:.1f} mA")
        valve.set_counts(ma_to_counts(ma))
        time.sleep(2.0)  # let the PLC see the new setpoint before checking "in position"
        start = time.time() - 2.0
        while True:
            s = print_status(valve)
            elapsed = time.time() - start
            if s["in_position"]:
                break
            if elapsed > timeout_s:
                print("  ¡No llegó a la posición dentro del tiempo límite!")
                break
            time.sleep(1.0)
        results.append((ma, s["output_pct"], s["feedback_pct"], s["deviation_pct"], elapsed))

    print("\nRESUMEN")
    print("  mA     orden %   posición %   desviación %   tiempo s")
    for ma, out, fb, dev, t in results:
        print(f"  {ma:5.1f}  {out:7.2f}   {fb:9.2f}   {dev:+11.2f}   {t:8.1f}")


def main():
    ap = argparse.ArgumentParser(description="Control de la válvula S4 4-20 mA vía Modbus TCP del TM221")
    ap.add_argument("--ip", required=True, help="dirección IP del TM221ME16R")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--unit", type=int, default=1, help="Modbus unit id")
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("interactive", help="escribir comandos en mA / %% a mano")
    sw = sub.add_parser("sweep", help="prueba automática 4-8-12-16-20-12-4 mA")
    sw.add_argument("--timeout", type=float, default=60.0,
                    help="segundos máximos por paso (carrera completa ~11-40 s)")
    args = ap.parse_args()

    plc = ModbusTcp(args.ip, args.port, args.unit)
    plc.connect()
    valve = Valve(plc)
    valve.start()
    print(f"Conectado a {args.ip}. Válvula habilitada en 4 mA (cerrada).")
    try:
        if args.mode == "interactive":
            run_interactive(valve)
        else:
            run_sweep(valve, [4, 8, 12, 16, 20, 12, 4], args.timeout)
    except KeyboardInterrupt:
        print()
    finally:
        valve.stop()
        plc.close()
        print("Válvula enviada a cerrar (4 mA). Desconectado.")


if __name__ == "__main__":
    main()
