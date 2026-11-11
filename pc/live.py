"""Monitor en vivo: muestra mV, ohmios y gramos con el modelo ya calibrado.

Uso:
    python pc/live.py --port COM5 --models data/models
    python pc/live.py --simulate --models data/models --weight 1200

Es la forma rapida de comprobar si la calibracion "siente" bien antes de
integrarla en tu proyecto.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from pc import fsr
from pc.link import SerialSource, SimulatedSource, find_port
from pc.model import load_models, predict_grams_from_mv


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Lectura en vivo de FSR en gramos.",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--port")
    p.add_argument("--baud", type=int, default=fsr.BAUDRATE)
    p.add_argument("--models", default=str(fsr.DATA_MODELS),
                   help="JSON o directorio de modelos")
    p.add_argument("--simulate", action="store_true")
    p.add_argument("--weight", type=float, default=0.0,
                   help="peso simulado, en gramos (solo con --simulate)")
    p.add_argument("--window", type=float, default=0.5,
                   help="ventana de promediado, en segundos")
    p.add_argument("--hz", type=float, default=4.0, help="refresco de pantalla")
    args = p.parse_args(argv)

    models = load_models(args.models)
    if not models:
        raise SystemExit(f"no encontre modelos en {args.models}. "
                         f"Ejecuta primero pc/fit.py")

    if args.simulate:
        source = SimulatedSource(n_ch=len(fsr.CHANNELS))
        source.set_weight(args.weight)
    else:
        try:
            source = SerialSource(find_port(args.port), args.baud,
                                  n_ch=len(fsr.CHANNELS))
        except Exception as exc:
            raise SystemExit(f"ERROR: no pude abrir el puerto: {exc}\n"
                             f"       Cierra el Monitor Serie del Arduino IDE.")

    print(f"modelos: {', '.join(models)}  |  origen: {source.status}")
    print(f"{'canal':>6}{'mV':>11}{'ohmios':>14}{'gramos':>11}   modelo")
    print("-" * 62)

    source.start()
    try:
        while True:
            time.sleep(1.0 / max(args.hz, 0.5))
            mv, _raw = source.channels_window(args.window)
            if not mv or min(len(x) for x in mv) < 2:
                continue
            parts = []
            for i in range(source.n_ch):
                name = fsr.CHANNELS[i] if i < len(fsr.CHANNELS) else f"ch{i}"
                m = float(np.mean(mv[i]))
                ohms = fsr.ohms_from_mv(m)
                model = models.get(name)
                if model is None:
                    parts.append(f"{name:>6}{m:11.1f}{fsr.format_ohms(ohms):>14}"
                                 f"{'--':>11}   (sin modelo)")
                    continue
                grams = predict_grams_from_mv(model, m)
                parts.append(f"{name:>6}{m:11.1f}{fsr.format_ohms(ohms):>14}"
                             f"{grams:11.1f}   {model['model']}")
            sys.stdout.write("\r" * 200 + " | ".join(parts))
            sys.stdout.flush()
    except KeyboardInterrupt:
        print()
    finally:
        source.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
