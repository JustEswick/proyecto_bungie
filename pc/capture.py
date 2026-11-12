"""Captura de puntos de calibracion (gramos de la bascula <-> lectura del FSR).

Dos modos:

  INTERACTIVO  (por defecto)
      Muestra la lectura en vivo. Escribes el peso que marca la bascula,
      ENTER, y el programa promedia la ultima ventana (`--hold` segundos),
      te ensena el resultado y te pide confirmacion antes de guardar.
      ENTER en vacio NO captura: solo refresca la pantalla, para que puedas
      mirar la senal mientras buscas el peso.

  BARRIDO  (--sweep)
      Recorre una lista de pesos. Para cada uno espera a que la senal sea
      estable y captura solo, sin escribir nada. Ideal para una sesion buena.

Uso:
    python pc/capture.py --port COM4 --session fsr5kg
    python pc/capture.py --port COM4 --session fsr5kg --sweep 0,100,500,1000,2000,3000,4000,5000
    python pc/capture.py --port COM4 --range-check --target 5000
    python pc/capture.py --simulate --session prueba

Antes de crear el CSV se comprueba el enlace serie: si la placa tiene el
firmware antiguo o el puerto esta ocupado, se aborta diciendo exactamente
que pasa (en vez de dejar un CSV vacio).
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

from pc import diagnose, fsr
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

def channel_name(i: int) -> str:
    return fsr.CHANNELS[i] if i < len(fsr.CHANNELS) else f"ch{i}"


def snapshot_text(source: FsrSource, seconds: float,
                  r_fixed=None) -> str:
    mv, _raw = source.channels_window(seconds)
    if not mv or len(mv[0]) < 3:
        return "  (sin datos; espera un momento o revisa el puerto)"

    out = []
    for i in range(source.n_ch):
        m = np.asarray(mv[i], dtype=float)
        r = fsr.ohms_from_mv(float(m.mean()),
                             r_fixed=fsr.r_fixed_for(i, r_fixed))
        sd = m.std(ddof=1) if m.size > 1 else 0.0
        out.append(
            f"  {channel_name(i):>3}  mv={m.mean():8.1f} +/-{sd:5.2f}  "
            f"R={fsr.format_ohms(r):>10}  n={m.size}")
    return "\n".join(out)


def _warn_saturation(mv_mean: float, name: str) -> None:
    if mv_mean > fsr.ADC_TRUST_V * 1000.0:
        print(f"  [!] {name}: {mv_mean:.0f} mV > {fsr.ADC_TRUST_V * 1000:.0f} mV. "
              f"Zona NO caracterizada del ADC (posible saturacion).")
    elif mv_mean > fsr.ADC_MAX_V * 1000.0 * 0.985:
        print(f"  [!] {name}: {mv_mean:.0f} mV, practicamente en el techo del ADC.")


# ---------------------------------------------------------------------------
# Comprobacion previa del enlace
# ---------------------------------------------------------------------------

def preflight(source: FsrSource, args, seconds: float = 2.0) -> bool:
    """Verifica que llegan muestras validas ANTES de crear el CSV."""
    if isinstance(source, SimulatedSource):
        return True

    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        time.sleep(0.2)
        if len(source.snapshot()) >= 5:
            break

    n_valid = len(source.snapshot())
    info = diagnose.analyze(source.recent_raw(), n_ch=source.n_ch,
                            seconds=seconds)

    if n_valid >= 5:
        print(f"  [ok] enlace serie: {info['hz']:.1f} muestras/s validas")
        if info["header"]:
            print(f"       firmware: {info['header'][:96]}")
        return True

    print()
    print("  " + "-" * 62)
    print("  No llegan muestras validas del ESP32.")
    print("  " + "-" * 62)
    diagnose.print_report(info, getattr(source, "port", "?"), args.baud,
                          seconds, source.n_ch)
    print("  " + "-" * 62)
    print("  No se ha creado ningun archivo (no hay datos que guardar).")
    return False


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
            "r_fixed": fsr.format_r_fixed(args.r_fixed),
            "topologia": fsr.topology_name(args.fsr_to_vcc),
            "canales": ",".join(fsr.CHANNELS[:source.n_ch]),
        }
        _init_file(path, self.meta)

    # -- utilidades ---------------------------------------------------------
    def _stability(self, seconds: float) -> float:
        mv, _ = self.source.channels_window(seconds)
        if not mv or min(len(x) for x in mv) < 3:
            return float("inf")
        return max(np.std(np.asarray(x, dtype=float)) for x in mv)

    def _wait_stable(self) -> bool:
        a = self.args
        t0 = time.monotonic()
        time.sleep(min(0.3, a.settle))
        while True:
            elapsed = time.monotonic() - t0
            sd = self._stability(a.stab_window)
            if sd <= a.stab_mv:
                sys.stdout.write(
                    f"\r      estable: sd={sd:4.1f} mV <= {a.stab_mv:g} mV"
                    + " " * 24 + "\n")
                sys.stdout.flush()
                return True
            if elapsed >= a.stab_timeout:
                sys.stdout.write(
                    f"\r      sin estabilizar: sd={sd:4.1f} mV tras "
                    f"{elapsed:.0f} s" + " " * 18 + "\n")
                sys.stdout.flush()
                return False
            frac = min(1.0, elapsed / max(a.stab_timeout, 0.1))
            bar = "#" * int(frac * 20)
            sys.stdout.write(
                f"\r      estabilizando: sd={sd:5.1f} mV "
                f"[{bar:<20}] {elapsed:4.1f}s")
            sys.stdout.flush()
            time.sleep(0.15)

    # -- medidas ------------------------------------------------------------
    def _measure(self, grams: float, settle: bool) -> list[dict]:
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
        for i in range(self.source.n_ch):
            m = np.asarray(mv[i], dtype=float)
            r = np.asarray(raw[i], dtype=float)
            rows.append({
                "timestamp": stamp,
                "session": a.session,
                "ch": i,
                "nombre": channel_name(i),
                "gramos": f"{grams:.1f}",
                "mv_mean": f"{m.mean():.2f}",
                "mv_std": f"{(m.std(ddof=1) if m.size > 1 else 0.0):.3f}",
                "raw_mean": f"{r.mean():.2f}",
                "raw_std": f"{(r.std(ddof=1) if r.size > 1 else 0.0):.3f}",
                "n": int(m.size),
            })
        return rows

    def _print_rows(self, rows: list[dict], grams: float) -> None:
        print("  --- punto medido ---")
        resting = True
        for row in rows:
            mv = float(row["mv_mean"])
            sd = float(row["mv_std"])
            print(f"  {row['nombre']:>3}  gramos={grams:7.1f}  mv={mv:8.1f} "
                  f"+/-{sd:5.2f}  n={row['n']}")
            _warn_saturation(mv, row["nombre"])
            if mv > fsr.ADC_MIN_V * 1000.0:
                resting = False
        if grams > 0 and resting:
            print("  [!] La senal sigue en reposo (<30 mV) con ese peso puesto.")
            print("      Puede que el sensor no este presionando: usa 'r' para repetir.")

    # -- captura ------------------------------------------------------------
    def take_point(self, grams: float, *, show: bool = True,
                   settle: bool = True, confirm: bool = False) -> list[dict]:
        while True:
            rows = self._measure(grams, settle)
            if not rows:
                return []
            if show:
                self._print_rows(rows, grams)
            if not confirm:
                break
            ans = input("  Guardar este punto? [S/n/r] ").strip().lower()
            if ans in ("", "s", "si", "y", "yes"):
                break
            if ans.startswith("r"):
                print("  repitiendo la captura...")
                settle = True
                continue
            print("  punto descartado")
            return []
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
        r_list = fsr.parse_r_fixed(self.meta.get("r_fixed", fsr.R_FIXED_OHMS),
                                   self.source.n_ch)
        print(f"  {'gramos':>8}  {'canal':>5}  {'mv':>9}  {'R':>10}")
        for r in rows:
            try:
                mv = float(r["mv_mean"])
                rf = fsr.r_fixed_for(int(r["ch"]), r_list)
                print(f"  {float(r['gramos']):8.1f}  {r['nombre']:>5}  "
                      f"{mv:9.2f}  "
                      f"{fsr.format_ohms(fsr.ohms_from_mv(mv, r_fixed=rf)):>10}")
            except (ValueError, KeyError):
                continue
        print(f"  total: {len(rows)} filas en {self.path}")


# ---------------------------------------------------------------------------
# Comprobacion de rango
# ---------------------------------------------------------------------------

def range_check(source: FsrSource, args) -> None:
    """Mide el pico con la carga objetivo y recomienda R_fija."""
    n = source.n_ch
    target = args.target
    seconds = args.range_seconds

    print()
    print("=" * 68)
    print(f"  COMPROBACION DE RANGO  -  objetivo {target:g} g")
    print("=" * 68)
    print(f"  Aplica {target:g} g y mantenlos quietos durante {seconds:g} s.")
    print("  Se guardara el valor MAXIMO de cada canal.")
    print()

    if isinstance(source, SimulatedSource):
        source.set_weight(target)
        time.sleep(max(1.0, args.settle))

    peaks = [0.0] * n
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        mv, _ = source.channels_window(0.4)
        if mv and min(len(x) for x in mv) >= 3:
            live = []
            for i in range(n):
                m = float(np.mean(mv[i]))
                peaks[i] = max(peaks[i], m)
                live.append(f"{channel_name(i)}={peaks[i]:.0f}")
            sys.stdout.write("\r  midiendo pico... " + "  ".join(live) + " mV   ")
            sys.stdout.flush()
        time.sleep(0.2)
    sys.stdout.write("\r" + " " * 70 + "\r")
    print("  Resultado:")
    print(f"  {'canal':>6}{'mV pico':>10}{'R a esa carga':>17}"
          f"{'R_fija recomendada':>20}")
    for i in range(n):
        peak = peaks[i]
        rf = fsr.r_fixed_for(i, args.r_fixed)
        if peak < fsr.ADC_MIN_V * 1000.0:
            print(f"  {channel_name(i):>6}{peak:10.1f}{'--':>17}{'--':>20}   "
                  f"(no detecte presion)")
            continue
        r_target = fsr.ohms_from_mv(peak, r_fixed=rf)
        rec = 2.3 * r_target
        rmax = 2.882 * r_target
        print(f"  {channel_name(i):>6}{peak:10.1f}"
              f"{fsr.format_ohms(r_target):>17}"
              f"{rec / 1000.0:15.2f} kohm")

        if peak <= fsr.ADC_TRUST_V * 1000.0:
            verdict = "OK: por debajo de 2450 mV"
        elif peak <= 2900.0:
            verdict = "aceptable, pero pierdes precision arriba"
        else:
            verdict = "demasiado alto: recortara o perdera linealidad"
        print(f"          -> {verdict}")
        print(f"          -> para {target:g} g en ~2.3 V usa R_fija ~= "
              f"{rec / 1000.0:.2f} kohm (maximo {rmax / 1000.0:.2f} kohm)")
        if rf > rmax:
            print(f"          -> tu R_fija actual ({rf:.0f} ohm) es DEMASIADO ALTA")
        elif rf < rec * 0.5:
            print(f"          -> tu R_fija actual ({rf:.0f} ohm) es muy baja: "
                  f"perderas resolucion abajo")
        else:
            print(f"          -> tu R_fija actual ({rf:.0f} ohm) es adecuada")
    print()
    print(f"  Si cambias R_fija, pasala siempre: --r-fixed <ohmios>")
    print("  (y usa una sesion nueva: el CSV guarda su propio R_fija).")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Captura de puntos para calibrar FSR contra una bascula.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--port", help="puerto serie (ej. COM4). Si se omite, autodetecta")
    p.add_argument("--baud", type=int, default=fsr.BAUDRATE)
    p.add_argument("--session", help="nombre de la sesion (nombre del CSV)")
    p.add_argument("--hold", type=float, default=3.0,
                   help="segundos que se promedian por punto")
    p.add_argument("--vcc", type=float, default=fsr.VCC_VOLTS,
                   help="voltaje real del riel 3V3 (mideal con multimetro)")
    p.add_argument("--r-fixed", default=str(fsr.R_FIXED_OHMS),
                   help="resistencia fija en ohmios; acepta un valor por canal: "
                        "1667,10000")
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
    p.add_argument("--range-check", action="store_true",
                   help="solo medir el pico con --target y recomendar R_fija")
    p.add_argument("--target", type=float, default=5000.0,
                   help="carga objetivo (g) para --range-check")
    p.add_argument("--range-seconds", type=float, default=10.0,
                   help="duracion de --range-check")
    p.add_argument("--yes", action="store_true",
                   help="no pedir confirmacion al guardar (modo desatendido)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    n_ch = len(fsr.CHANNELS)

    try:
        args.r_fixed = fsr.parse_r_fixed(args.r_fixed, n_ch)
    except ValueError as exc:
        raise SystemExit(f"ERROR en --r-fixed: {exc}")
    args.fsr_to_vcc = fsr.parse_topology(args.topology)

    session = args.session or _dt.datetime.now().strftime("sesion_%Y%m%d_%H%M%S")
    session = "".join(c if c.isalnum() or c in "-_." else "_" for c in session)
    path = fsr.DATA_RAW / f"{session}.csv"

    if args.simulate:
        source: FsrSource = SimulatedSource(n_ch=n_ch,
                                            vcc=args.vcc,
                                            r_fixed=args.r_fixed,
                                            fsr_to_vcc=args.fsr_to_vcc)
    else:
        try:
            port = find_port(args.port)
        except RuntimeError as exc:
            raise SystemExit(f"ERROR: {exc}")
        try:
            source = SerialSource(port, args.baud, n_ch=n_ch)
        except Exception as exc:
            raise SystemExit(
                f"ERROR: no pude abrir {port}: {exc}\n"
                f"       {diagnose.explain_serial_error(exc, port)}")

    print()
    print("=" * 68)
    print("  CALIBRACION FSR  -  captura de puntos")
    print("=" * 68)
    print(f"  origen    : {source.status}")
    print(f"  archivo   : {path}")
    print(f"  divisor   : R_fixed={fsr.format_r_fixed(args.r_fixed)} ohm, "
          f"VCC={args.vcc:.2f} V, {args.topology}")
    print(f"  promediado: {args.hold:.1f} s por punto")
    print()
    print("  Recuerda: disco rigido del mismo diametro que el pad activo del FSR,")
    print("  siempre en el mismo sitio, y tara la bascula con el disco encima.")
    print()

    source.start()
    wrote_csv = False
    try:
        time.sleep(0.4)  # dejar llenar el buffer
        if not preflight(source, args):
            return 1

        if args.range_check:
            range_check(source, args)
            return 0

        sess = CaptureSession(args, source, path)
        wrote_csv = True
        if args.sweep:
            _run_sweep(sess, args, source)
        else:
            _run_interactive(sess, args, source)
    except KeyboardInterrupt:
        print("\n  interrumpido por el usuario")
    finally:
        source.stop()

    if wrote_csv:
        print(f"\n  Datos en: {path}")
        print(f"  Siguiente paso:  python pc/fit.py --csv \"{path}\"")
    return 0


def _run_sweep(sess: CaptureSession, args, source: FsrSource) -> None:
    try:
        targets = [float(x) for x in args.sweep.replace(";", ",").split(",")
                   if x.strip()]
    except ValueError:
        raise SystemExit("--sweep debe ser una lista de numeros: 0,100,500,1000")

    total = len(targets) * args.reps
    print(f"  Modo barrido: {len(targets)} pesos x {args.reps} repeticiones "
          f"({total} puntos)")
    print("  Para cada peso: aplicalo, mantenlo y espera. Se captura solo.")
    done = 0
    for grams in targets:
        for rep in range(args.reps):
            done += 1
            print(f"\n  >>> {done}/{total}  Peso objetivo: {grams:g} g"
                  f"   (rep {rep + 1}/{args.reps})")
            if isinstance(source, SimulatedSource):
                source.set_weight(grams)
                time.sleep(max(args.settle, 0.8) + 0.5)
                stable = True
            else:
                stable = sess._wait_stable()
                time.sleep(0.2)
            if not stable:
                print("      [!] no se estabilizo a tiempo; capturo igual. "
                      "Revisa mv_std del punto.")
            print("      lectura:", end="")
            print("\n" + snapshot_text(source, args.stab_window, args.r_fixed))
            sess.take_point(grams, settle=False)


def _run_interactive(sess: CaptureSession, args, source: FsrSource) -> None:
    print("  COMO TOMAR UN PUNTO")
    print("    1. Apoya el sensor y tara la bascula con el disco encima.")
    print("    2. Aplica el peso y MANTENLO.")
    print("    3. Escribe cuantos gramos marca la bascula y pulsa ENTER.")
    print("    4. Revisa la medida y confirma con S (o repite con r).")
    print()
    print("  ENTER en vacio solo refresca la lectura, no guarda nada.")
    print("  u = deshacer | l = listar | q = salir | ? = ayuda")
    while True:
        print(f"\n  Lectura en vivo (ventana {args.hold:g} s):")
        print(snapshot_text(source, args.hold, args.r_fixed))
        try:
            raw = input("\n  gramos en la bascula > ").strip()
        except EOFError:
            break
        low = raw.lower()
        if low in ("q", "quit", "exit"):
            break
        if low in ("h", "?", "help"):
            print("  Escribe el peso y ENTER para medir.")
            print("  ENTER vacio refresca | u deshace | l lista | q sale")
            continue
        if low in ("u", "undo"):
            sess.undo()
            continue
        if low in ("l", "list"):
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
        sess.take_point(grams, confirm=not args.yes)


if __name__ == "__main__":
    raise SystemExit(main())
