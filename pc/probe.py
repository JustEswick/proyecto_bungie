"""Diagnostico del puerto serie: responde a "no me registra nada".

Abre el puerto, escucha unos segundos y dictamina que esta pasando:
firmware antiguo, puerto ocupado, baudrate incorrecto, silencio o todo OK.

Uso:
    python pc/probe.py --port COM4
    python pc/probe.py --port COM4 --seconds 6 --raw
    python pc/probe.py                  # autodetecta el puerto

Codigo de salida: 0 si el enlace esta bien, 1 si hay algun problema.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc import diagnose, fsr
from pc.link import find_port


def collect_lines(port: str, baud: int, seconds: float) -> list[str]:
    """Lee lineas crudas durante ``seconds`` y devuelve el texto decodificado."""
    import serial

    ser = serial.Serial(port, baud, timeout=0.2)
    try:
        ser.reset_input_buffer()
        time.sleep(0.2)          # deja que arranque si el puerto resetea la placa
        lines: list[str] = []
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            data = ser.readline()
            if not data:
                continue
            text = data.decode(errors="replace").rstrip("\r\n")
            if text:
                lines.append(text)
            if len(lines) >= 800:
                break
        return lines
    finally:
        ser.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Comprueba que el ESP32 envia el formato esperado por los scripts.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--port", help="puerto serie (ej. COM4). Si se omite, autodetecta")
    p.add_argument("--baud", type=int, default=fsr.BAUDRATE)
    p.add_argument("--seconds", type=float, default=4.0,
                   help="cuanto tiempo escuchar")
    p.add_argument("--channels", type=int, default=len(fsr.CHANNELS),
                   help="numero de canales del firmware")
    p.add_argument("--raw", action="store_true",
                   help="mostrar todas las lineas recibidas")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    try:
        port = find_port(args.port)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 2

    print()
    print("=" * 68)
    print("  DIAGNOSTICO DEL ENLACE SERIE")
    print("=" * 68)

    try:
        lines = collect_lines(port, args.baud, args.seconds)
    except Exception as exc:
        print(f"  Puerto   : {port} @ {args.baud} baudios")
        print()
        print("  VEREDICTO: PUERTO OCUPADO O NO DISPONIBLE")
        print(f"  {diagnose.explain_serial_error(exc, port)}")
        print()
        return 2

    info = diagnose.analyze(lines, n_ch=args.channels, seconds=args.seconds)

    if args.raw:
        print("  --- lineas crudas ---")
        for ln in lines[:60]:
            print(f"  | {ln[:110]}")
        print("  ---------------------")

    diagnose.print_report(info, port, args.baud, args.seconds, args.channels)
    print()
    return 0 if diagnose.ok(info) else 1


if __name__ == "__main__":
    raise SystemExit(main())
