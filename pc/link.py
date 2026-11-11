"""Fuentes de datos: puerto serie real o simulador.

Ambas exponen la misma interfaz, asi que ``capture.py`` y ``live.py`` no
saben (ni les importa) si los datos vienen del ESP32 o del simulador.

Formato esperado de cada linea del firmware:
    ms,raw0,mv0,raw1,mv1
Las lineas que empiezan por '#' se ignoran.
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque

from pc import fsr

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    serial = None
    list_ports = None


# ---------------------------------------------------------------------------
# Deteccion automatica del puerto
# ---------------------------------------------------------------------------

# VID de los chips USB-serie mas comunes en placas ESP32.
_KNOWN_VIDS = {
    0x1A86,  # CH340 / CH341 (NodeMCU, muchas DevKit baratas)
    0x10C4,  # CP210x  (DevKitC oficial)
    0x303A,  # Espressif nativo (ESP32-S2/S3/C3)
    0x0403,  # FTDI
}


def find_port(preferred: str | None = None) -> str:
    """Devuelve el puerto a usar. Si ``preferred`` es None, autodetecta."""
    if preferred:
        return preferred
    if list_ports is None:
        raise RuntimeError("pyserial no esta instalado en este entorno")

    candidates = []
    for p in list_ports.comports():
        if p.vid in _KNOWN_VIDS:
            candidates.append(p.device)
    if not candidates:
        devices = [p.device for p in list_ports.comports()]
        if len(devices) == 1:
            return devices[0]
        msg = ", ".join(devices) if devices else "ninguno"
        raise RuntimeError(
            f"No encontre la placa ESP32. Puertos disponibles: {msg}")
    if len(candidates) > 1:
        # Preferir el primero, pero avisar: casi siempre es el correcto.
        print(f"[aviso] varios puertos candidatos: {candidates}; uso {candidates[0]}")
    return candidates[0]


# ---------------------------------------------------------------------------
# Fuente base
# ---------------------------------------------------------------------------

class FsrSource:
    """Interfaz comun. Cada muestra es (t, [raw...], [mv...])."""

    n_ch: int
    status: str = "sin iniciar"

    def __init__(self, n_ch: int = 2, maxlen: int = 20000):
        self.n_ch = n_ch
        self._buf: deque[tuple[float, tuple[int, ...], tuple[float, ...]]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_line = ""

    # -- ciclo de vida ------------------------------------------------------
    def start(self) -> "FsrSource":
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"{type(self).__name__}-reader")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- datos --------------------------------------------------------------
    def _push(self, raw, mv) -> None:
        with self._lock:
            self._buf.append((time.monotonic(), tuple(raw), tuple(mv)))

    def snapshot(self):
        with self._lock:
            return list(self._buf)

    def window(self, seconds: float):
        """Muestras de los ultimos ``seconds`` segundos.

        Devuelve una lista de (t, raw, mv). Si aun no hay datos, lista vacia.
        """
        if seconds <= 0:
            return self.snapshot()
        with self._lock:
            if not self._buf:
                return []
            t_end = self._buf[-1][0]
            t_min = t_end - float(seconds)
            out = []
            for item in reversed(self._buf):
                if item[0] < t_min:
                    break
                out.append(item)
            out.reverse()
            return out

    def channels_window(self, seconds: float):
        """Devuelve (mv_por_canal, raw_por_canal) como listas de listas."""
        data = self.window(seconds)
        mv = [[] for _ in range(self.n_ch)]
        raw = [[] for _ in range(self.n_ch)]
        for _t, r, m in data:
            for i in range(self.n_ch):
                raw[i].append(r[i])
                mv[i].append(m[i])
        return mv, raw

    # -- a sobrescribir -----------------------------------------------------
    def _run(self) -> ...:  # pragma: no cover
        raise NotImplementedError

    def set_weight(self, grams: float) -> None:
        """Solo el simulador lo usa."""


# ---------------------------------------------------------------------------
# Puerto serie real
# ---------------------------------------------------------------------------

class SerialSource(FsrSource):
    def __init__(self, port: str, baud: int = fsr.BAUDRATE, n_ch: int = 2):
        super().__init__(n_ch=n_ch)
        if serial is None:
            raise RuntimeError("pyserial no esta instalado en este entorno")
        self.port = port
        self.baud = baud
        self._ser = serial.Serial(port=port, baudrate=baud, timeout=0.2)
        self.status = f"conectado a {port} @ {baud}"

    def stop(self) -> None:
        super().stop()
        try:
            self._ser.close()
        except Exception:
            pass

    def _run(self) -> None:
        expected = 1 + 2 * self.n_ch
        while not self._stop.is_set():
            try:
                line = self._ser.readline()
            except Exception as exc:  # desconexion en caliente
                self.status = f"error de serie: {exc}"
                break
            if not line:
                continue
            try:
                text = line.decode(errors="ignore").strip()
            except Exception:
                continue
            if not text:
                continue
            self.last_line = text
            if text.startswith("#"):
                continue

            parts = text.split(",")
            if len(parts) != expected:
                continue
            try:
                int(parts[0])                       # ms del dispositivo
                raw = [int(parts[1 + 2 * i]) for i in range(self.n_ch)]
                mv = [float(parts[2 + 2 * i]) for i in range(self.n_ch)]
            except ValueError:
                continue
            self._push(raw, mv)


# ---------------------------------------------------------------------------
# Simulador (para probar el pipeline sin hardware)
# ---------------------------------------------------------------------------

class SimulatedSource(FsrSource):
    """Genera lecturas de un FSR402 inventado pero realista.

    Ley oculta por canal:  R = R1000 * (gramos / 1000) ** (-k)
    La calibracion debe recuperar aproximadamente esos parametros.
    """

    def __init__(self, n_ch: int = 2, hz: int = 25, seed: int | None = 1234,
                 vcc: float = fsr.VCC_VOLTS,
                 r_fixed: float = fsr.R_FIXED_OHMS,
                 fsr_to_vcc: bool = True):
        super().__init__(n_ch=n_ch)
        self.hz = hz
        self.vcc = vcc
        self.r_fixed = r_fixed
        self.fsr_to_vcc = fsr_to_vcc
        self._rng = random.Random(seed)
        # (R a 1000 g, exponente). Dos canales ligeramente distintos para
        # imitar la variacion unidad-a-unidad de los FSR reales.
        self._law = [(3000.0, 1.10), (3300.0, 1.05)]
        self._weight = 0.0
        self._noise_mv = 1.6
        self._drift = 0.0
        self.status = "simulador"

    def set_weight(self, grams: float) -> None:
        self._weight = max(0.0, float(grams))

    def _r_for(self, ch: int, grams: float) -> float:
        r1000, k = self._law[ch % len(self._law)]
        if grams <= 1.0:
            return 1e8
        return r1000 * (grams / 1000.0) ** (-k)

    def _run(self) -> None:
        period = 1.0 / float(self.hz)
        next_t = time.monotonic()
        while not self._stop.is_set():
            raw, mv = [], []
            for ch in range(self.n_ch):
                r_true = self._r_for(ch, self._weight)
                v = fsr.volts_from_ohms(r_true, vcc=self.vcc,
                                        r_fixed=self.r_fixed,
                                        fsr_to_vcc=self.fsr_to_vcc)
                # deriva lenta (creep del FSR y estabilidad del riel)
                self._drift += self._rng.gauss(0.0, 0.02)
                self._drift *= 0.999
                v_noisy = v + self._rng.gauss(0.0, self._noise_mv / 1000.0)
                # no linealidad suave tipica del ADC del ESP32 (~0.5 %)
                v_noisy += 0.005 * v * (v - self.vcc / 2.0)
                v_meas = min(max(v_noisy, 0.0), fsr.ADC_MAX_V)
                m = round(v_meas * 1000.0, 1)
                mv.append(m)
                raw.append(int(round(v_meas * 4095.0 / fsr.ADC_MAX_V)))
            self._push(raw, mv)

            next_t += period
            delay = next_t - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_t = time.monotonic()
