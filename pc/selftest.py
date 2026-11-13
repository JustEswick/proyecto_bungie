"""Autocomprobacion del entorno y de la cadena de calculo.

No necesita hardware. Verifica:
  1. que pyserial/numpy/scipy/matplotlib importan,
  2. que la conversion tension <-> resistencia es consistente,
  3. que el simulador produce lecturas plausibles,
  4. que el ajuste recupera una ley de potencias conocida.

    python pc/selftest.py
"""

from __future__ import annotations

import contextlib
import io
import math
import shutil
import sys
import tempfile
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from pc import fit, fsr
from pc.link import SimulatedSource
from pc.model import predict_grams

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [ok]   {name}" + (f"  ({detail})" if detail else ""))
    else:
        print(f"  [FALLO] {name}" + (f"  ({detail})" if detail else ""))
        FAILS.append(name)


def test_imports() -> None:
    print("\n1. Dependencias")
    try:
        import serial
        check("pyserial", True, serial.__version__)
    except ImportError:
        check("pyserial", False, "instala con: .venv\\Scripts\\pip install pyserial")
    for mod in ("numpy", "scipy", "matplotlib", "pandas"):
        try:
            __import__(mod)
            check(mod, True)
        except ImportError:
            check(mod, False)


def test_conversions() -> None:
    print("\n2. Conversion tension <-> resistencia")
    worst = 0.0
    for r in (300.0, 1000.0, 5000.0, 20000.0, 100000.0):
        v = fsr.volts_from_ohms(r)
        r2 = fsr.ohms_from_volts(v)
        worst = max(worst, abs(r2 - r) / r)
    check("ida y vuelta fsr_to_vcc", worst < 1e-9, f"error relativo {worst:.2e}")

    v = fsr.volts_from_ohms(5000.0, fsr_to_vcc=False)
    r2 = fsr.ohms_from_volts(v, fsr_to_vcc=False)
    check("ida y vuelta fsr_to_gnd", abs(r2 - 5000.0) / 5000.0 < 1e-9)

    check("reposo = abierto", not math.isfinite(fsr.ohms_from_mv(0.0)))
    v_hi = fsr.VCC_VOLTS * 1000.0
    check("nodo a VCC = corto", fsr.ohms_from_mv(v_hi) < 1.0)


def test_simulator() -> None:
    print("\n3. Simulador")
    src = SimulatedSource(n_ch=2).start()
    src.set_weight(1000.0)
    time.sleep(1.6)
    mv, _raw = src.channels_window(1.0)
    src.stop()
    ok = all(len(x) > 10 for x in mv)
    check("genera muestras a 25 Hz", ok,
          f"{[len(x) for x in mv]} muestras por canal")
    means = [float(np.mean(x)) for x in mv] if ok else [0, 0]
    check("1000 g -> nodo en zona util", all(700 < m < 2000 for m in means),
          f"{means[0]:.0f} / {means[1]:.0f} mV")
    sd = [float(np.std(x)) for x in mv] if ok else [99, 99]
    check("ruido bajo", all(s < 10 for s in sd),
          f"{sd[0]:.2f} / {sd[1]:.2f} mV")

    # Con R_fija = 1637 ohm, 5 kg debe quedar en la parte alta pero sin recortar
    # (el techo fiable es 2.45 V; 5 kg puede quedar justo por encima en algunas
    # unidades, de ahi el margen hasta 2.9 V).
    src = SimulatedSource(n_ch=2).start()
    src.set_weight(5000.0)
    time.sleep(1.6)
    mv5, _ = src.channels_window(1.0)
    src.stop()
    means5 = [float(np.mean(x)) for x in mv5] if mv5 else [0, 0]
    check("5000 g -> medible sin recortar (< 2.9 V)",
          all(1800 < m < 2900 for m in means5),
          f"{means5[0]:.0f} / {means5[1]:.0f} mV")


def test_fit_recovers_law() -> None:
    print("\n4. El ajuste recupera una ley conocida")
    rng = np.random.default_rng(7)
    a_true, b_true = 2.0e6, -0.95
    grams = np.array([50, 100, 200, 300, 500, 750, 1000, 1500, 2000], dtype=float)
    ohms = (grams / a_true) ** (1.0 / b_true)
    ohms *= 1.0 + rng.normal(0.0, 0.003, ohms.size)   # 0.3 % de ruido

    fits = fit.fit_all(ohms, grams)
    m1 = next(f for f in fits if f["model"] == "power_law" and f.get("params"))
    a, b = m1["params"]["A"], m1["params"]["B"]
    check("A recuperada", abs(a - a_true) / a_true < 0.05,
          f"{a:.3g} vs {a_true:.3g}")
    check("B recuperado", abs(b - b_true) < 0.03, f"{b:.3f} vs {b_true:.3f}")
    check("RMSE < 2 % del fondo",
          m1["metrics"]["rmse_g"] < 0.02 * grams.max(),
          f"{m1['metrics']['rmse_g']:.1f} g")

    pred = predict_grams(m1, ohms)
    check("prediccion coherente", np.allclose(pred, grams, rtol=0.05))


def _write_capture(path: Path, rows: list[str]) -> None:
    header = [
        "# calibracion FSR - puntos de referencia",
        "# vcc=3.27",
        "# r_fixed=1633/1641",
        "# topologia=fsr_to_vcc",
        "# canales=S3,S4",
        "# creado=2026-11-12T18:00:00",
        "timestamp,session,ch,nombre,gramos,mv_mean,mv_std,raw_mean,raw_std,n",
    ]
    path.write_text("\n".join(header + rows) + "\n", encoding="utf-8")


def test_fit_channel_selection() -> None:
    """Un canal en reposo no debe generar un modelo de calibracion."""
    print("\n5. Seleccion de canales en el ajuste")
    tmp = Path(tempfile.mkdtemp(prefix="fsr_selftest_"))
    try:
        # S3 con senal realista; S4 en reposo (~1 mV): solo debe ajustarse S3.
        rows = []
        signal = [(100, 135.0), (500, 660.0), (1000, 1150.0),
                  (2000, 1762.0), (3000, 2117.0), (4000, 2345.0),
                  (5000, 2501.0)]
        for i, (g, mv) in enumerate(signal):
            rows.append(f"2026-11-12T18:00:{i:02d},uno,0,S3,{g}.0,{mv},1.0,1,1.0,75")
            rows.append(f"2026-11-12T18:00:{i:02d},uno,1,S4,{g}.0,0.9,0.8,1,0.9,75")
        csv = tmp / "uno.csv"
        _write_capture(csv, rows)
        out = tmp / "models_uno"
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            rc = fit.main(["--csv", str(csv), "--out-dir", str(out),
                           "--no-header"])
        names = sorted(p.name for p in out.glob("*.json"))
        check("canal en reposo se omite", names == ["uno_S3.json"],
              f"generados: {names}")
        check("se avisa del canal omitido", "S4" in buf.getvalue()
              and "sin senal" in buf.getvalue())

        # Los dos canales con senal + --only S4: solo debe ajustarse S4.
        rows2 = []
        for i, (g, mv) in enumerate(signal):
            rows2.append(f"2026-11-12T18:01:{i:02d},dos,0,S3,{g}.0,{mv},1.0,1,1.0,75")
            rows2.append(f"2026-11-12T18:01:{i:02d},dos,1,S4,{g}.0,{mv * 0.95},1.0,1,1.0,75")
        csv2 = tmp / "dos.csv"
        _write_capture(csv2, rows2)
        out2 = tmp / "models_dos"
        with contextlib.redirect_stdout(io.StringIO()):
            rc2 = fit.main(["--csv", str(csv2), "--out-dir", str(out2),
                            "--only", "S4", "--no-header"])
        names2 = sorted(p.name for p in out2.glob("*.json"))
        check("--only S4 ajusta solo ese canal", names2 == ["dos_S4.json"],
              f"generados: {names2}")
        check("ambos ajustes terminan bien", rc == 0 and rc2 == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_capture_pause() -> None:
    """La pausa entre puntos debe ser configurable y desactivable."""
    print("\n6. Pausa entre puntos de captura")
    from pc import capture
    parser = capture.build_parser()
    check("--pause por defecto = 10 s",
          parser.parse_args([]).pause == 10.0)
    check("--pause acepta otro valor",
          parser.parse_args(["--pause", "4.5"]).pause == 4.5)
    t0 = time.monotonic()
    capture._pause_before_point(1000.0, 0)
    check("--pause 0 no espera", time.monotonic() - t0 < 0.5)


def test_round_trip_models() -> None:
    print("\n7. Persistencia de modelos")
    tmp = Path(__file__).resolve().parent.parent / "data" / "models"
    jsons = sorted(tmp.glob("*.json")) if tmp.exists() else []
    if not jsons:
        print("  [info] no hay modelos guardados todavia (normal antes de calibrar)")
        return
    from pc.model import load_model
    m = load_model(jsons[0])
    required = {"model", "params", "hardware", "metrics", "nombre"}
    check("JSON con campos minimos", required.issubset(m.keys()),
          f"{jsons[0].name}")
    g = predict_grams(m, 10000.0)
    check("evalua a 10 kohm", math.isfinite(g), f"{g:.0f} g")


def main() -> int:
    print("=" * 62)
    print("  AUTOCOMPROBACION  -  entorno de calibracion FSR")
    print("=" * 62)
    test_imports()
    test_conversions()
    test_simulator()
    test_fit_recovers_law()
    test_fit_channel_selection()
    test_capture_pause()
    test_round_trip_models()

    print("\n" + "=" * 62)
    if FAILS:
        print(f"  {len(FAILS)} comprobacion(es) fallida(s): {', '.join(FAILS)}")
        return 1
    print("  Todo correcto: el entorno esta listo para calibrar.")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
