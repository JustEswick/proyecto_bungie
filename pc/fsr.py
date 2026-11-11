"""Constantes del hardware y conversiones tension <-> resistencia.

Todo el proyecto asume un divisor de tension por sensor:

    topologia "fsr_to_vcc" (la que tienes cableada):
        3V3 --- FSR --- nodo --- R_FIXED --- GND
        Al presionar, R_fsr baja y V_nodo sube.

    topologia "fsr_to_gnd" (alternativa):
        3V3 --- R_FIXED --- nodo --- FSR --- GND
        Al presionar, V_nodo baja.

La conversion inversa es la que convierte una lectura del ADC en resistencia.
Se hace en el PC (no en el firmware) para no reflashear mientras se prueba.
"""

from __future__ import annotations

import math
from pathlib import Path

# ---------------------------------------------------------------- rutas
ROOT = Path(__file__).resolve().parent.parent
DATA_RAW = ROOT / "data" / "raw"
DATA_MODELS = ROOT / "data" / "models"
FIRMWARE_DIR = ROOT / "firmware" / "fsr_stream"

# ---------------------------------------------------------------- hardware
# Mide estos dos valores con el multimetro. Un 5 % de error en VCC se
# traduce en un error notable en la resistencia calculada.
R_FIXED_OHMS = 10000.0
VCC_VOLTS = 3.30
FSR_TO_VCC = True

CHANNELS = ("S3", "S4")      # orden identico al firmware (GPIO35, GPIO34)
BAUDRATE = 115200

# Limites utiles del ADC del ESP32 con atenuacion 11 dB.
# 2.45 V es el techo de la zona caracterizada por Espressif; por encima la
# lectura sigue siendo monotona pero pierde linealidad.
ADC_TRUST_V = 2.45
ADC_MAX_V = 3.10
ADC_MIN_V = 0.03             # por debajo, R_fsr es practicamente infinito


def topology_name(fsr_to_vcc: bool = FSR_TO_VCC) -> str:
    return "fsr_to_vcc" if fsr_to_vcc else "fsr_to_gnd"


def parse_topology(text: str) -> bool:
    t = str(text).strip().lower()
    if t in ("fsr_to_vcc", "vcc", "fsr-arriba", "arriba"):
        return True
    if t in ("fsr_to_gnd", "gnd", "fsr-abajo", "abajo"):
        return False
    raise ValueError(f"topologia desconocida: {text!r}")


def ohms_from_volts(volts, *, vcc: float = VCC_VOLTS,
                    r_fixed: float = R_FIXED_OHMS,
                    fsr_to_vcc: bool = FSR_TO_VCC):
    """Resistencia del FSR a partir de la tension del nodo (0 si no aplica).

    Devuelve ``math.inf`` cuando el FSR esta practicamente abierto (sin carga).
    Acepta escalares o arrays de numpy.
    """
    vcc = float(vcc)
    r_fixed = float(r_fixed)

    if hasattr(volts, "__len__"):
        import numpy as np
        v = np.asarray(volts, dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            if fsr_to_vcc:
                out = r_fixed * (vcc - v) / v
            else:
                out = r_fixed * v / (vcc - v)
        out[~np.isfinite(out)] = np.inf
        out[out < 0] = np.inf
        return out

    v = float(volts)
    if fsr_to_vcc:
        if v <= 1e-4:
            return math.inf
        return r_fixed * (vcc - v) / v
    if v >= vcc - 1e-4:
        return math.inf
    return r_fixed * v / (vcc - v)


def volts_from_ohms(ohms, *, vcc: float = VCC_VOLTS,
                    r_fixed: float = R_FIXED_OHMS,
                    fsr_to_vcc: bool = FSR_TO_VCC):
    """Tension del nodo esperada para una resistencia dada (para graficas)."""
    r = float(ohms)
    if not math.isfinite(r) or r <= 0:
        return 0.0 if fsr_to_vcc else vcc
    if fsr_to_vcc:
        return vcc * r_fixed / (r_fixed + r)
    return vcc * r / (r_fixed + r)


def ohms_from_mv(mv, **kwargs):
    """Igual que ohms_from_volts pero recibiendo milivoltios."""
    if hasattr(mv, "__len__"):
        import numpy as np
        return ohms_from_volts(np.asarray(mv, dtype=float) / 1000.0, **kwargs)
    return ohms_from_volts(float(mv) / 1000.0, **kwargs)


def format_ohms(ohms: float) -> str:
    """Formato legible: 12.3 kohm, 850 ohm, abierto."""
    if ohms is None or not math.isfinite(ohms) or ohms > 1e7:
        return "abierto"
    if ohms >= 1e6:
        return f"{ohms / 1e6:.2f} Mohm"
    if ohms >= 1e3:
        return f"{ohms / 1e3:.1f} kohm"
    return f"{ohms:.0f} ohm"
