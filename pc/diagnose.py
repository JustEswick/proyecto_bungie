"""Diagnostico del enlace serie con el ESP32.

Centraliza la deteccion de problemas para que `probe.py` y `capture.py`
den el mismo veredicto y el mismo consejo. El caso mas frecuente con
diferencia: la placa sigue con el sketch de prueba y no con fsr_stream.

Veredictos posibles:
    ok            lineas CSV validas
    old_firmware  la placa envia "S3: 0 (Sin presion)" (sketch viejo)
    header_only   solo llegan lineas '#' (firmware nuevo pero sin muestras)
    silence       el puerto se abrio pero no llego nada
    garbage       bytes no imprimibles (baudrate incorrecto)
    unknown       llegan lineas pero no encajan en ningun formato
    busy          no se pudo abrir el puerto porque otro programa lo usa
"""

from __future__ import annotations

import re

OLD_PREFIX = re.compile(r"^\s*S\d+\s*:")
OLD_LEVELS = ("sin presion", "sin presion", "ligera", "media", "fuerte")

VERDICT_TITLES = {
    "ok": "OK - el firmware responde en el formato esperado",
    "old_firmware": "FIRMWARE ANTIGUO",
    "header_only": "SOLO CABECERAS",
    "silence": "SIN DATOS",
    "garbage": "BYTES NO LEGIBLES (posible baudrate incorrecto)",
    "unknown": "FORMATO DESCONOCIDO",
    "busy": "PUERTO OCUPADO",
}

VERDICT_ADVICE = {
    "ok": "Puedes calibrar: python pc/capture.py --port {port} --session <nombre>",
    "old_firmware": (
        "La placa sigue ejecutando el sketch de prueba (envia 'S3: ... (Sin presion)').\n"
        "  -> Abre firmware/fsr_stream/fsr_stream.ino en el Arduino IDE y pulsa Upload."
    ),
    "header_only": (
        "Llegan las cabeceras '#' pero ninguna muestra.\n"
        "  -> Sube de nuevo el firmware; parece una version a medias."
    ),
    "silence": (
        "El puerto se abrio pero no llego ninguna linea.\n"
        "  -> Revisa que la placa este alimentada, que el sketch tenga Serial.begin(115200)\n"
        "     y que el baudrate sea 115200."
    ),
    "garbage": (
        "Los bytes no son texto: casi seguro el baudrate no coincide.\n"
        "  -> Prueba --baud 9600 / 57600 / 115200 hasta ver texto legible."
    ),
    "unknown": (
        "Llegan lineas, pero no son CSV ni el formato antiguo.\n"
        "  -> Compara con el formato esperado: ms,raw0,mv0,raw1,mv1"
    ),
    "busy": (
        "Otro programa tiene el puerto abierto.\n"
        "  -> Cierra el Monitor Serie del Arduino IDE (solo un programa puede\n"
        "     usar el puerto a la vez) y reintenta."
    ),
}


def classify_line(text: str, n_ch: int = 2) -> str:
    """Clasifica una linea ya decodificada."""
    t = text.strip()
    if not t:
        return "empty"
    if t.startswith("#"):
        return "header"
    if OLD_PREFIX.match(t) or any(k in t.lower() for k in OLD_LEVELS):
        return "old"

    parts = [p.strip() for p in t.split(",")]
    if len(parts) == 1 + 2 * n_ch:
        try:
            int(parts[0])
            for i in range(n_ch):
                int(parts[1 + 2 * i])
                float(parts[2 + 2 * i])
            return "csv"
        except ValueError:
            pass

    printable = sum(ch.isprintable() for ch in t)
    if printable / max(1, len(t)) < 0.8:
        return "garbage"
    return "unknown"


def analyze(lines: list[str], n_ch: int = 2, seconds: float = 1.0) -> dict:
    """Resume un conjunto de lineas y decide el veredicto."""
    counts = {k: 0 for k in
              ("csv", "old", "header", "garbage", "unknown", "empty")}
    header = ""
    sample = ""
    old_sample = ""

    for line in lines:
        kind = classify_line(line, n_ch)
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "header" and not header:
            header = line.strip()
        elif kind == "csv" and not sample:
            sample = line.strip()
        elif kind == "old" and not old_sample:
            old_sample = line.strip()

    if counts["csv"] >= 2:
        verdict = "ok"
    elif counts["old"]:
        verdict = "old_firmware"
    elif counts["garbage"]:
        verdict = "garbage"
    elif counts["header"] and counts["csv"] == 0:
        verdict = "header_only"
    elif counts["unknown"]:
        verdict = "unknown"
    else:
        verdict = "silence"

    return {
        "verdict": verdict,
        "counts": counts,
        "header": header,
        "sample": sample,
        "old_sample": old_sample,
        "hz": (counts["csv"] / seconds) if seconds > 0 else 0.0,
        "n_lines": sum(counts.values()),
    }


def explain_serial_error(exc: BaseException, port: str) -> str:
    """Traduce la excepcion de pyserial a algo accionable."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(k in text for k in ("acceso", "access", "denied", "permission",
                               "permiso", "busy")):
        return VERDICT_ADVICE["busy"].format(port=port)
    if any(k in text for k in ("could not open", "filenotfound",
                               "no such file", "no existe", "not found")):
        return (f"No existe {port} o esta desconectado.\n"
                f"  -> Comprueba el cable USB y el numero de puerto "
                f"(Administrador de dispositivos > Puertos COM).")
    return f"No pude abrir {port}: {exc}"


def print_report(info: dict, port: str, baud: int, seconds: float,
                 n_ch: int = 2) -> None:
    """Imprime el bloque de diagnostico legible."""
    c = info["counts"]
    print(f"  Puerto   : {port} @ {baud} baudios")
    print(f"  Duracion : {seconds:.1f} s")
    print(f"  Lineas   : {info['n_lines']} total "
          f"({c['csv']} validas, {c['old']} formato antiguo, "
          f"{c['header']} cabeceras)")
    if info["hz"]:
        print(f"  Frecuencia: {info['hz']:.1f} muestras/s")
    if info["header"]:
        print(f"  Cabecera : {info['header'][:100]}")
    if info["sample"]:
        print(f"  Muestra  : {info['sample'][:100]}")
    if info["old_sample"]:
        print(f"  Recibido : {info['old_sample'][:100]}")

    verdict = info["verdict"]
    print()
    print(f"  VEREDICTO: {VERDICT_TITLES.get(verdict, verdict)}")
    advice = VERDICT_ADVICE.get(verdict, "")
    if advice:
        print(f"  {advice.format(port=port)}")


def ok(info: dict) -> bool:
    return info["verdict"] == "ok"
