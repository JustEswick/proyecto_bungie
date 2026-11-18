"""Captura de puntos de calibracion (gramos de la bascula <-> lectura del FSR).

Flujo de captura (congelar primero, anotar despues):

  1. Eliges UN sensor por sesion (`--channel 0` = S3, `1` = S4).
  2. Pulsas ENTER y el programa captura un intervalo de `--hold` segundos.
  3. Al terminar, las muestras quedan congeladas: ya puedes soltar el dedo.
  4. Escribes UN peso observado/predominante en la bascula. Ese valor es el
     que usara fit.py.
  5. Opcionalmente anotas minimo y maximo del intervalo. Si los dejas vacios,
     quedan iguales al peso observado. Son solo anotacion/auditoria.

Dos modos:

  INTERACTIVO  (por defecto)
      ENTER inicia cada intervalo. Despues anotas la referencia y confirmas.

  BARRIDO  (--sweep)
      Recorre una lista de pesos. Antes de cada punto hace una pausa
      (`--pause`, 10 s por defecto) para que te de tiempo a quitar la carga
      anterior y aplicar la siguiente. Despues espera a que la senal sea
      estable y captura el intervalo. Tambien pide la referencia manual.

Con --simulate no se piden datos manuales: se usa el peso simulado.

Uso:
    python pc/capture.py --port COM4 --session fsr5kg
    python pc/capture.py --port COM4 --session fsr5kg --channel 0
    python pc/capture.py --port COM4 --session fsr5kg --channel 0 --sweep 0,100,500,1000,2000,3000,4000,5000
    python pc/capture.py --port COM4 --range-check --target 5000
    python pc/capture.py --simulate --session prueba --channel 0

Antes de crear el CSV se comprueba el enlace serie: si la placa tiene el
firmware antiguo o el puerto esta ocupado, se aborta diciendo exactamente
que pasa (en vez de dejar un CSV vacio).
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import math
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
          "mv_mean", "mv_std", "raw_mean", "raw_std", "n",
          "gramos_min", "gramos_max", "gramos_predominante",
          "referencia", "captura_id", "duracion_s", "muestras_tramo_s"]
META_KEYS = ["vcc", "r_fixed", "topologia", "canal", "canales"]
MIN_SAMPLE_HZ = 10.0


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
        with path.open("r", encoding="utf-8", newline="") as fh:
            header = next(csv.reader(ln for ln in fh if not ln.startswith("#")), [])
        if header != FIELDS:
            raise SystemExit("ERROR: CSV de otra version. Usa otra --session; "
                             "no se modifico el archivo anterior.")
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
                  r_fixed=None, channel: int | None = None) -> str:
    mv, _raw = source.channels_window(seconds)
    if not mv or len(mv[0]) < 3:
        return "  (sin datos; espera un momento o revisa el puerto)"

    out = []
    shown = range(source.n_ch) if channel is None else [channel]
    for i in shown:
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


def _countdown(seconds: float) -> None:
    """Cuenta atras por consola; ENTER la termina antes (solo Windows)."""
    if seconds <= 0:
        return
    msvcrt = None
    try:
        import msvcrt  # Windows: permite saltar con ENTER
    except ImportError:
        pass
    end = time.monotonic() + seconds
    while True:
        left = end - time.monotonic()
        if left <= 0:
            break
        sys.stdout.write(f"\r      esperando... {left:4.1f} s   "
                         f"(ENTER para medir ya)   ")
        sys.stdout.flush()
        if msvcrt is not None and msvcrt.kbhit():
            if msvcrt.getwch() in ("\r", "\n"):
                break
        time.sleep(0.1)
    sys.stdout.write("\r" + " " * 66 + "\r")
    sys.stdout.flush()


def _pause_before_point(grams: float, seconds: float) -> None:
    """Da tiempo a quitar la carga anterior y aplicar la del punto actual."""
    if seconds <= 0:
        return
    accion = "quita la carga" if grams <= 0 else f"aplica {grams:g} g"
    print(f"      preparate: {accion} y mantenlo.")
    _countdown(seconds)


def ask_grams(prompt: str, minimum: float = 0.0,
              maximum: float = float("inf"),
              *, optional: bool = False) -> float | None:
    """Pide un peso en gramos; ENTER vacio devuelve None si optional."""
    while True:
        try:
            text = input(prompt).strip().lower()
            if optional and not text:
                return None
            for suffix in ("gramos", "gr", "g"):
                if text.endswith(suffix):
                    text = text[:-len(suffix)].strip()
                    break
            value = float(text.replace(",", "."))
        except ValueError:
            print("  Escribe un numero en gramos, sin unidades ni separadores de miles.")
            continue
        if math.isfinite(value) and minimum <= value <= maximum:
            return value
        msg = f"  Valor invalido: debe ser finito y >= {minimum:g}"
        if math.isfinite(maximum):
            msg += f", <= {maximum:g}"
        print(msg + ".")


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
        if args.channel is None:
            canal = ""
            canales = ",".join(fsr.CHANNELS[:source.n_ch])
        else:
            canal = str(args.channel)
            canales = channel_name(args.channel)
        self.meta = {
            "vcc": f"{args.vcc:.2f}",
            "r_fixed": fsr.format_r_fixed(args.r_fixed),
            "topologia": fsr.topology_name(args.fsr_to_vcc),
            "canal": canal,
            "canales": canales,
        }
        _init_file(path, self.meta)

    # -- utilidades ---------------------------------------------------------
    def _stability(self, seconds: float) -> float:
        mv, _ = self.source.channels_window(seconds)
        if not mv:
            return float("inf")
        ch = self.args.channel
        channels = range(self.source.n_ch) if ch is None else [ch]
        if any(i >= len(mv) or len(mv[i]) < 3 for i in channels):
            return float("inf")
        return max(np.std(np.asarray(mv[i], dtype=float)) for i in channels)

    def _wait_stable(self) -> bool:
        a = self.args
        if a.settle > 0:
            time.sleep(a.settle)
        t0 = time.monotonic()
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
        """Captura un intervalo y congela las muestras antes de anotar nada."""
        a = self.args
        if isinstance(self.source, SimulatedSource):
            self.source.set_weight(grams)
            if not (settle and a.settle > 0):
                # Deja que el simulador genere muestras YA con el peso nuevo;
                # si no, la primera muestra del intervalo puede ser del peso
                # anterior y dispara mv_std.
                time.sleep(0.3)

        if settle and a.settle > 0:
            print(f"  Preparacion: {a.settle:g} s. Todavia NO se registra.")
            time.sleep(a.settle)

        print(f"  INICIO: observa la bascula durante {a.hold:g} s.")
        start = time.monotonic()
        end = start + a.hold
        while True:
            left = end - time.monotonic()
            if left <= 0:
                break
            sys.stdout.write(f"\r  CAPTURANDO: faltan {left:4.1f} s   ")
            sys.stdout.flush()
            time.sleep(min(0.1, left))

        # Filtra por reloj local de recepcion, no por la ultima muestra:
        # una desconexion no debe reutilizar lecturas antiguas del buffer.
        samples = [item for item in self.source.snapshot()
                   if start <= item[0] <= end]
        print("\r  FIN: muestras congeladas. Ya puedes soltar el dedo.   ")

        if len(samples) < 5:
            print("  [!] Muy pocas muestras NUEVAS. Punto NO guardado.")
            return []
        span = samples[-1][0] - samples[0][0]
        if span <= 0:
            print("  [!] Las muestras no tienen una ventana temporal valida.")
            return []
        hz = len(samples) / span
        if hz < MIN_SAMPLE_HZ:
            print(f"  [!] Frecuencia efectiva muy baja ({hz:.1f} Hz). "
                  f"Punto NO guardado; revisa el enlace.")
            return []
        edge = max(0.25, a.hold * 0.2)
        gap = max(0.5, a.hold * 0.2)
        if (samples[0][0] - start > edge
                or end - samples[-1][0] > edge
                or any(y[0] - x[0] > gap
                       for x, y in zip(samples, samples[1:]))):
            print("  [!] Faltaron datos durante el intervalo. Repite la captura.")
            return []

        stamp = _dt.datetime.now().isoformat(timespec="microseconds")
        channels = (range(self.source.n_ch) if a.channel is None
                    else [a.channel])
        rows: list[dict] = []
        for i in channels:
            m = np.asarray([item[2][i] for item in samples], dtype=float)
            r = np.asarray([item[1][i] for item in samples], dtype=float)
            if not (np.isfinite(m).all() and np.isfinite(r).all()):
                print("  [!] Muestras no finitas. Punto NO guardado.")
                return []
            rows.append({
                "timestamp": stamp,
                "session": a.session,
                "ch": i,
                "nombre": channel_name(i),
                "gramos": f"{grams:.3f}",
                "mv_mean": f"{m.mean():.2f}",
                "mv_std": f"{m.std(ddof=1):.3f}",
                "raw_mean": f"{r.mean():.2f}",
                "raw_std": f"{r.std(ddof=1):.3f}",
                "n": int(m.size),
                "gramos_min": "",
                "gramos_max": "",
                "gramos_predominante": "",
                "referencia": "",
                "captura_id": stamp,
                "duracion_s": f"{a.hold:.3f}",
                "muestras_tramo_s": f"{span:.3f}",
            })
        return rows

    def _reference(self, rows: list[dict]) -> float:
        """Pide el peso observado; minimo/maximo son anotacion opcional."""
        simulated = isinstance(self.source, SimulatedSource)
        if simulated:
            reference = float(rows[0]["gramos"])
            minimum = maximum = reference
            print(f"  Referencia simulada: {reference:g} g.")
        else:
            print("  Anota lo observado SOLO durante el intervalo de captura.")
            print("  Unidades: gramos. Sin separadores de miles; decimal con punto o coma.")
            reference = ask_grams("  Peso observado/predominante (g): ")
            minimum = ask_grams(
                "  Peso MINIMO observado (g) [ENTER = mismo]: ",
                maximum=reference, optional=True)
            maximum = ask_grams(
                "  Peso MAXIMO observado (g) [ENTER = mismo]: ",
                minimum=reference, optional=True)
            if minimum is None:
                minimum = reference
            if maximum is None:
                maximum = reference

        for row in rows:
            row.update(
                gramos=f"{reference:.3f}",
                gramos_min=f"{minimum:.3f}",
                gramos_max=f"{maximum:.3f}",
                gramos_predominante=f"{reference:.3f}",
                referencia="simulado" if simulated else "manual_estable",
            )
        if not simulated:
            print(f"  Referencia: {reference:g} g; rango anotado "
                  f"{minimum:g}..{maximum:g} g.")
            print("  fit.py usara el peso observado; el rango es solo anotacion.")
        return reference

    def _print_rows(self, rows: list[dict], grams: float) -> None:
        print("  --- punto medido ---")
        floor_mv = fsr.ADC_FLOOR_V * 1000.0
        resting = True
        for row in rows:
            mv = float(row["mv_mean"])
            sd = float(row["mv_std"])
            print(f"  {row['nombre']:>3}  gramos={grams:7.1f}  mv={mv:8.1f} "
                  f"+/-{sd:5.2f}  n={row['n']}")
            _warn_saturation(mv, row["nombre"])
            if mv > floor_mv:
                resting = False
        if grams > 0 and resting:
            print(f"  [!] La senal sigue en reposo (<{floor_mv:.0f} mV) con ese peso.")
            print("      Puede que el sensor no este presionando: repite la captura.")

    # -- captura ------------------------------------------------------------
    def take_point(self, grams: float, *, show: bool = True,
                   settle: bool = True, confirm: bool = False) -> list[dict]:
        while True:
            rows = self._measure(grams, settle)
            if not rows:
                return []
            reference = self._reference(rows)
            if show:
                self._print_rows(rows, reference)
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
        last_id = rows[-1].get("captura_id") or rows[-1].get("timestamp")
        kept = [r for r in rows
                if (r.get("captura_id") or r.get("timestamp")) != last_id]
        _rewrite_rows(self.path, kept)
        points_left = len({r.get("captura_id") or r.get("timestamp")
                           for r in kept})
        print(f"  deshecho: {len(rows) - len(kept)} filas "
              f"({last_id}). Quedan {points_left} puntos.")

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
    p.add_argument("--channel", type=int,
                   help="sensor de esta sesion: 0=S3, 1=S4")
    p.add_argument("--port", help="puerto serie (ej. COM4). Si se omite, autodetecta")
    p.add_argument("--baud", type=int, default=fsr.BAUDRATE)
    p.add_argument("--session", help="nombre de la sesion (nombre del CSV)")
    p.add_argument("--hold", type=float, default=3.0,
                   help="duracion del intervalo de captura, en segundos")
    p.add_argument("--vcc", type=float, default=fsr.VCC_VOLTS,
                   help="voltaje real del riel 3V3 (mideal con multimetro)")
    p.add_argument("--r-fixed", default=str(fsr.R_FIXED_OHMS),
                   help="resistencia fija en ohmios; acepta un valor por canal: "
                        "1633,1641")
    p.add_argument("--topology", choices=["fsr_to_vcc", "fsr_to_gnd"],
                   default=fsr.topology_name(), help="orientacion del divisor")
    p.add_argument("--simulate", action="store_true",
                   help="usar datos sinteticos (sin hardware)")
    p.add_argument("--sweep", help="lista de pesos a recorrer, ej. 0,100,500,1000")
    p.add_argument("--reps", type=int, default=1,
                   help="capturas por peso en modo --sweep")
    p.add_argument("--settle", type=float, default=1.0,
                   help="espera tras aplicar el peso antes de capturar")
    p.add_argument("--pause", type=float, default=10.0,
                   help="pausa antes de cada punto de --sweep, en segundos, "
                        "para quitar/aplicar la carga (0 = sin pausa)")
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
    if not math.isfinite(args.hold) or not 0.5 <= args.hold <= 300:
        raise SystemExit("--hold debe estar entre 0.5 y 300 segundos.")
    if not math.isfinite(args.settle) or not 0 <= args.settle <= 60:
        raise SystemExit("--settle debe estar entre 0 y 60 segundos.")
    if not math.isfinite(args.pause) or args.pause < 0:
        raise SystemExit("--pause debe ser un numero de segundos >= 0.")
    if args.reps < 1:
        raise SystemExit("--reps debe ser >= 1.")
    if args.channel is not None and not 0 <= args.channel < n_ch:
        raise SystemExit(f"--channel debe estar entre 0 y {n_ch - 1}.")

    try:
        args.r_fixed = fsr.parse_r_fixed(args.r_fixed, n_ch)
    except ValueError as exc:
        raise SystemExit(f"ERROR en --r-fixed: {exc}")
    args.fsr_to_vcc = fsr.parse_topology(args.topology)

    session = args.session or _dt.datetime.now().strftime("sesion_%Y%m%d_%H%M%S")
    session = "".join(c if c.isalnum() or c in "-_." else "_" for c in session)
    args.session = session
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
    print(f"  intervalo : {args.hold:.1f} s de captura por punto")
    print()
    print("  Tara la bascula con el sensor y su soporte; presiona solo sobre el FSR.")
    print("  Al terminar el intervalo puedes soltar el dedo y anotar el peso.")
    print("  Se guarda un solo canal por sesion.")
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

        if args.channel is None:
            print("  Elige UN sensor para esta sesion:")
            for i in range(n_ch):
                print(f"    {i} = {channel_name(i)}")
            while True:
                chosen = input("  Sensor (indice): ").strip()
                if chosen.isdigit() and 0 <= int(chosen) < n_ch:
                    args.channel = int(chosen)
                    break
                print("  Escribe uno de los indices indicados.")
        print(f"  canal     : {channel_name(args.channel)} "
              f"(indice {args.channel})")
        print()

        sess = CaptureSession(args, source, path)
        wrote_csv = True
        if args.sweep:
            _run_sweep(sess, args, source)
        else:
            _run_interactive(sess, args, source)
    except (KeyboardInterrupt, EOFError):
        print("\n  Finalizado. El punto pendiente no se guarda; "
              "los anteriores se conservan.")
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
    if args.pause > 0 and not isinstance(source, SimulatedSource):
        print(f"  Antes de cada punto: {args.pause:g} s para quitar/aplicar la "
              f"carga (ENTER salta la espera).")
    print("  Para cada peso: mantenlo; despues anota el peso observado.")
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
                _pause_before_point(grams, args.pause)
                stable = sess._wait_stable()
                time.sleep(0.2)
            if not stable:
                print("      [!] no se estabilizo a tiempo; capturo igual. "
                      "Revisa mv_std del punto.")
            print("      lectura:", end="")
            print("\n" + snapshot_text(source, args.stab_window, args.r_fixed,
                                       channel=args.channel))
            sess.take_point(grams, settle=False,
                            confirm=not args.yes
                            and not isinstance(source, SimulatedSource))


def _run_interactive(sess: CaptureSession, args, source: FsrSource) -> None:
    print("  COMO TOMAR UN PUNTO")
    print("    1. Tara la bascula con el sensor y su soporte.")
    print(f"    2. Pulsa ENTER y manten la fuerza durante {args.hold:g} s.")
    print("    3. Al terminar puedes soltar el dedo: las muestras ya estan congeladas.")
    print("    4. Escribe el peso observado; minimo y maximo son opcionales.")
    print("    5. Confirma con S (o repite con r).")
    print()
    print("  ENTER en vacio inicia una captura nueva.")
    print("  u = deshacer | l = listar | q = salir | ? = ayuda")
    prompt = ("\n  ENTER para capturar / objetivo simulado (g) / comando > "
              if args.simulate
              else "\n  ENTER para capturar / comando > ")
    while True:
        print(f"\n  Lectura reciente (ventana {args.hold:g} s):")
        print(snapshot_text(source, args.hold, args.r_fixed,
                            channel=args.channel))
        try:
            raw = input(prompt).strip()
        except EOFError:
            break
        low = raw.lower()
        if low in ("q", "quit", "exit"):
            break
        if low in ("h", "?", "help"):
            print("  ENTER: captura un intervalo -> peso observado -> min/max opcionales.")
            print("  u deshace el ultimo punto, l lista, q sale.")
            continue
        if low in ("u", "undo"):
            sess.undo()
            continue
        if low in ("l", "list"):
            sess.list_points()
            continue
        if raw and not args.simulate:
            print("  En hardware real se captura primero y se anota el peso despues.")
            print("  Pulsa ENTER para iniciar el intervalo.")
            continue
        try:
            grams = (float(raw.replace(",", ".").replace("g", "").strip())
                     if raw else (1000.0 if args.simulate else 0.0))
        except ValueError:
            print("  Escribe un numero, comando o ENTER.")
            continue
        if not math.isfinite(grams) or grams < 0:
            print("  El objetivo debe ser finito y no negativo.")
            continue
        if raw and args.simulate:
            print(f"  (simulador: aplicando {grams:g} g durante {args.hold:g} s...)")
        sess.take_point(grams, confirm=not args.yes
                        and not isinstance(source, SimulatedSource))


if __name__ == "__main__":
    raise SystemExit(main())
