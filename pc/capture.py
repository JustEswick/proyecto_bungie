"""Captura de puntos de calibracion (gramos de la bascula <-> lectura del FSR).

Flujo pensado para el banco de trabajo:

   1. Apoyas el sensor sobre la bascula, pones encima el disco rigido y taras.
   2. Presionas hasta que la bascula marca un peso estable.
   3. Escribes ese peso y pulsas ENTER mientras sigues presionando.
   4. El programa promedia la ultima ventana de tiempo y guarda un punto.

ENTER en vacio NO captura: solo refresca la lectura en pantalla, para que
puedas mirar la senal mientras buscas el peso. Asi no se guardan puntos
basura por pulsar ENTER de mas.

Uso:
    python pc/capture.py --port COM5 --session fsr_lote1
    python pc/capture.py --port COM5 --session fsr_lote1 --sweep 0,50,100,200,500,1000,2000,3000
    python pc/capture.py --simulate --session prueba

Comandos en el prompt:
    <numero>  capturar un punto con ese peso en gramos
    ENTER     refrescar la lectura actual (no guarda)
    u         deshacer el ultimo punto guardado
    l         listar los puntos guardados
    q         terminar y guardar
    h         ayuda
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import sys
import time
from pathlib import Path

# Permite tanto `python pc/capture.py` como `python -m pc.capture`
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from pc import fsr
from pc.link import FsrSource, SerialSource, SimulatedSource, find_port

FIELDS = ["timestamp", "session", "ch", "nombre", "gramos",
          "mv_mean", "mv_std", "raw_mean", "raw_std", "n"]
META_KEYS = ["vcc", "r_fixed", "topologia", "canales"]


# ---------------------------------------------------------------------------
# Archivo CSV
# ---------------------------------------------------------------------------

def _read_meta(path: Path) -> dict[str, str]:
    meta: dict[str, str] = {}
    with path.open("r", encoding="utf-8", newline="") as fh:
        for line in fh:
            if not line.startswith("#"):
                break
            body = line[1:].strip()
            if "=" in body:
                k, v = body.split("=", 1)
                meta[k.strip()] = v.strip()
    return meta


def _init_file(path: Path, meta: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        old = _read_meta(path)
        for k in META_KEYS:
            if k in old and str(old[k]) != str(meta[k]):
                raise SystemExit(
                    f"ERROR: {path.name} ya existe con {k}={old[k]} y ahora usas "
                    f"{k}={meta[k]}.\n       Usa otra --session para no mezclar "
                    f"configuraciones distintas.")
        return
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write("# calibracion FSR - puntos de referencia\n")
        for k in META_KEYS:
            fh.write(f"# {k}={meta[k]}\n")
        fh.write(f"# creado={_dt.datetime.now().isoformat(timespec='seconds')}\n")
        csv.writer(fh).writerow(FIELDS)


def _append_rows(path: Path, rows: list[dict]) -> None:
    with path.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        for r in rows:
            w.writerow(r)


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        lines = [ln for ln in fh if not ln.startswith("#")]
    return list(csv.DictReader(lines))


def _rewrite_rows(path: Path, rows: list[dict]) -> None:
    """Reescribe el archivo conservando metadatos y cabecera de columnas."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    meta = [ln for ln in lines if ln.startswith("#")]
    if meta and not meta[-1].endswith("\n"):
        meta[-1] += "\n"
    path.write_text("".join(meta), encoding="utf-8")
    with path.open("a", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerow(FIELDS)
    _append_rows(path, rows)


# ---------------------------------------------------------------------------
# Presentacion
# ---------------------------------------------------------------------------

def snapshot_text(source: FsrSource, seconds: float) -> str:
    mv, raw = source.channels_window(seconds)
    if not mv or len(mv[0]) < 3:
        return "  (sin datos; espera un momento o revisa el puerto)"

    out = []
    for i in range(source.n_ch):
        m = np.asarray(mv[i], dtype=float)
        r = fsr.ohms_from_mv(float(m.mean()))
        out.append(
            f"  {fsr.CHANNELS[i] if i < len(fsr.CHANNELS) else f'ch{i}':>3}  "
            f"mv={m.mean():8.1f} +/-{m.std(ddof=1) if m.size > 1 else 0:5.2f}  "
            f"R={fsr.format_ohms(r):>10}  n={m.size}")
    return "\n".join(out)


def _warn_saturation(mv_mean: float, name: str) -> None:
    if mv_mean > fsr.ADC_TRUST_V * 1000.0:
        print(f"  [!] {name}: {mv_mean:.0f} mV > {fsr.ADC_TRUST_V * 1000:.0f} mV. "
              f"Estas en la zona NO caracterizada del ADC (posible saturacion).")
    elif mv_mean > fsr.ADC_MAX_V * 1000.0 * 0.985:
        print(f"  [!] {name}: {mv_mean:.0f} mV, practicamente en el techo del ADC.")


# ---------------------------------------------------------------------------
# Sesion de captura
# ---------------------------------------------------------------------------

class CaptureSession:
    def __init__(self, args, source: FsrSource, path: Path):
        self.args = args
        self.source = source
        self.path = path
        self.meta = {
            "vcc": f"{args.vcc:.2f}",
            "r_fixed": f"{args.r_fixed:.2f}",
            "topologia": fsr.topology_name(args.fsr_to_vcc),
            "canales": ",".join(fsr.CHANNELS[:source.n_ch]),
        }
        _init_file(path, self.meta)

    # -- utilidades ---------------------------------------------------------
    def _stability(self, seconds: float):
        mv, _ = self.source.channels_window(seconds)
        if not mv or min(len(x) for x in mv) < 3:
            return float("inf")
        return max(np.std(np.asarray(x, dtype=float)) for x in mv)

    def _wait_stable(self) -> bool:
        a = self.args
        t0 = time.monotonic()
        time.sleep(min(0.5, a.settle))
        while time.monotonic() - t0 < a.stab_timeout:
            if self._stability(a.stab_window) <= a.stab_mv:
                return True
            time.sleep(0.1)
        return False

    # -- captura ------------------------------------------------------------
    def take_point(self, grams: float, *, show: bool = True,
                   settle: bool = True) -> list[dict]:
        a = self.args
        if settle and isinstance(self.source, SimulatedSource):
            # El simulador necesita "sentir" el peso: se aplica y se espera
            # la ventana de promedio (con margen para que no queden muestras
            # del peso anterior dentro de la ventana).
            self.source.set_weight(grams)
            time.sleep(a.hold + 0.5)

        mv, raw = self.source.channels_window(a.hold)
        if not mv or min(len(x) for x in mv) < 5:
            print("  [!] muy pocas muestras en la ventana; aumenta --hold o "
                  "revisa la conexion. Punto NO guardado.")
            return []

        stamp = _dt.datetime.now().isoformat(timespec="seconds")
        rows: list[dict] = []
        if show:
            print("  --- punto capturado ---")
        for i in range(self.source.n_ch):
            m = np.asarray(mv[i], dtype=float)
            r = np.asarray(raw[i], dtype=float)
            name = fsr.CHANNELS[i] if i < len(fsr.CHANNELS) else f"ch{i}"
            row = {
                "timestamp": stamp,
                "session": a.session,
                "ch": i,
                "nombre": name,
                "gramos": f"{grams:.1f}",
                "mv_mean": f"{m.mean():.2f}",
                "mv_std": f"{(m.std(ddof=1) if m.size > 1 else 0.0):.3f}",
                "raw_mean": f"{r.mean():.2f}",
                "raw_std": f"{(r.std(ddof=1) if r.size > 1 else 0.0):.3f}",
                "n": int(m.size),
            }
            rows.append(row)
            if show:
                print(f"  {name:>3}  gramos={grams:7.1f}  mv={m.mean():8.1f} "
                      f"+/-{(m.std(ddof=1) if m.size > 1 else 0):5.2f}  n={m.size}")
                _warn_saturation(float(m.mean()), name)
        _append_rows(self.path, rows)
        return rows

    def undo(self) -> None:
        rows = _read_rows(self.path)
        if not rows:
            print("  (no hay puntos que deshacer)")
            return
        last_ts = rows[-1]["timestamp"]
        kept = [r for r in rows if r["timestamp"] != last_ts]
        _rewrite_rows(self.path, kept)
        print(f"  deshecho: {len(rows) - len(kept)} filas "
              f"({last_ts}). Quedan {len(kept) // max(1, self.source.n_ch)} puntos.")

    def list_points(self) -> None:
        rows = _read_rows(self.path)
        if not rows:
            print("  (todavia no hay puntos)")
            return
        print(f"  {'gramos':>8}  {'canal':>5}  {'mv':>9}  {'R':>10}")
        for r in rows:
            try:
                mv = float(r["mv_mean"])
                print(f"  {float(r['gramos']):8.1f}  {r['nombre']:>5}  "
                      f"{mv:9.2f}  {fsr.format_ohms(fsr.ohms_from_mv(mv)):>10}")
            except (ValueError, KeyError):
                continue
        print(f"  total: {len(rows)} filas en {self.path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Captura de puntos para calibrar FSR contra una bascula.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--port", help="puerto serie (ej. COM5). Si se omite, autodetecta")
    p.add_argument("--baud", type=int, default=fsr.BAUDRATE)
    p.add_argument("--session", help="nombre de la sesion (nombre del CSV)")
    p.add_argument("--hold", type=float, default=1.5,
                   help="segundos que se promedian por punto")
    p.add_argument("--vcc", type=float, default=fsr.VCC_VOLTS,
                   help="voltaje real del riel 3V3 (mideal con multimetro)")
    p.add_argument("--r-fixed", type=float, default=fsr.R_FIXED_OHMS,
                   help="resistencia fija del divisor, en ohmios")
    p.add_argument("--topology", choices=["fsr_to_vcc", "fsr_to_gnd"],
                   default=fsr.topology_name(), help="orientacion del divisor")
    p.add_argument("--simulate", action="store_true",
                   help="usar datos sinteticos (sin hardware)")
    p.add_argument("--sweep", help="lista de pesos a recorrer, ej. 0,100,500,1000")
    p.add_argument("--reps", type=int, default=1,
                   help="capturas por peso en modo --sweep")
    p.add_argument("--settle", type=float, default=1.0,
                   help="espera tras aplicar el peso antes de capturar")
    p.add_argument("--stab-window", type=float, default=1.0,
                   help="ventana para medir la estabilidad")
    p.add_argument("--stab-mv", type=float, default=3.0,
                   help="desviacion maxima (mV) para considerar la senal estable")
    p.add_argument("--stab-timeout", type=float, default=15.0,
                   help="espera maxima por estabilidad antes de capturar igual")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.fsr_to_vcc = fsr.parse_topology(args.topology)

    session = args.session or _dt.datetime.now().strftime("sesion_%Y%m%d_%H%M%S")
    session = "".join(c if c.isalnum() or c in "-_." else "_" for c in session)
    path = fsr.DATA_RAW / f"{session}.csv"

    if args.simulate:
        source: FsrSource = SimulatedSource(n_ch=len(fsr.CHANNELS),
                                            vcc=args.vcc,
                                            r_fixed=args.r_fixed,
                                            fsr_to_vcc=args.fsr_to_vcc)
    else:
        try:
            port = find_port(args.port)
        except RuntimeError as exc:
            raise SystemExit(f"ERROR: {exc}")
        try:
            source = SerialSource(port, args.baud, n_ch=len(fsr.CHANNELS))
        except Exception as exc:
            raise SystemExit(
                f"ERROR: no pude abrir {port}: {exc}\n"
                f"       Cierra el Monitor Serie del Arduino IDE (el puerto solo\n"
                f"       admite un programa a la vez) y vuelve a intentarlo.")

    sess = CaptureSession(args, source, path)

    print()
    print("=" * 68)
    print("  CALIBRACION FSR  -  captura de puntos")
    print("=" * 68)
    print(f"  origen    : {source.status}")
    print(f"  archivo   : {path}")
    print(f"  divisor   : R_fixed={args.r_fixed:.0f} ohm, VCC={args.vcc:.2f} V, "
          f"{args.topology}")
    print(f"  promediado: {args.hold:.1f} s por punto")
    print()
    print("  Recuerda: disco rigido del mismo diametro que el pad activo del FSR,")
    print("  siempre en el mismo sitio, y tara la bascula con el disco encima.")
    print()

    source.start()
    try:
        time.sleep(0.5)  # dejar llenar el buffer
        if args.sweep:
            _run_sweep(sess, args, source)
        else:
            _run_interactive(sess, args, source)
    except KeyboardInterrupt:
        print("\n  interrumpido por el usuario")
    finally:
        source.stop()

    print(f"\n  Datos en: {path}")
    print(f"  Siguiente paso:  python pc/fit.py --csv \"{path}\"")
    return 0


def _run_sweep(sess: CaptureSession, args, source: FsrSource) -> None:
    try:
        targets = [float(x) for x in args.sweep.replace(";", ",").split(",") if x.strip()]
    except ValueError:
        raise SystemExit("--sweep debe ser una lista de numeros: 0,100,500,1000")

    print(f"  Modo barrido: {len(targets)} pesos x {args.reps} repeticiones")
    for grams in targets:
        for rep in range(args.reps):
            print(f"\n  >>> Peso objetivo: {grams:g} g   (rep {rep + 1}/{args.reps})")
            if isinstance(source, SimulatedSource):
                # Aplicar el peso y dejar que el buffer se llene solo con el
                # valor nuevo, para que la lectura que se muestra sea la real.
                source.set_weight(grams)
                time.sleep(max(args.settle, args.hold) + 0.5)
                stable = True
            else:
                print(f"      Aplica la carga y mantenla. Esperando estabilidad "
                      f"(<={args.stab_mv:g} mV en {args.stab_window:g} s)...")
                stable = sess._wait_stable()
                time.sleep(0.2)
            if not stable:
                print("      [!] no se estabilizo a tiempo; capturo igual. "
                      "Revisa mv_std del punto.")
            print(snapshot_text(source, args.stab_window))
            sess.take_point(grams, settle=False)


def _run_interactive(sess: CaptureSession, args, source: FsrSource) -> None:
    print("  Escribe el peso en gramos y ENTER para capturar.")
    print("  ENTER solo = refrescar lectura | u = deshacer | l = listar | q = salir\n")
    while True:
        print(f"  lectura actual (ventana {args.hold:g} s):")
        print(snapshot_text(source, args.hold))
        try:
            raw = input("  gramos > ").strip()
        except EOFError:
            break
        if raw.lower() in ("q", "quit", "exit"):
            break
        if raw.lower() in ("h", "?", "help"):
            print("  <numero> captura | ENTER refresca | u deshace | "
                  "l lista | q sale")
            continue
        if raw.lower() in ("u", "undo"):
            sess.undo()
            continue
        if raw.lower() in ("l", "list"):
            sess.list_points()
            continue
        if not raw:
            continue
        try:
            grams = float(raw.replace(",", ".").replace("g", "").strip())
        except ValueError:
            print("  [!] no entiendo ese valor; escribe solo el numero")
            continue
        if grams < 0:
            print("  [!] el peso no puede ser negativo")
            continue
        if isinstance(source, SimulatedSource):
            print(f"  (simulador: aplicando {grams:g} g durante {args.hold:g} s...)")
        sess.take_point(grams)


if __name__ == "__main__":
    raise SystemExit(main())
