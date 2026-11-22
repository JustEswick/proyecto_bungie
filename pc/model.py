"""Modelos calibrados: evaluacion y persistencia en JSON.

Un modelo guardado tiene esta forma (ver data/models/*.json):

    {
      "session": "fsr_lote1",
      "ch": 0,
      "nombre": "S3",
      "model": "power_law",              # power_law | quad_log | interp
      "params": {"A": 1.2e6, "B": -1.83},
      "hardware": {"vcc": 3.30, "r_fixed": 1667.0, "topologia": "fsr_to_vcc"},
      "metrics": {"rmse_g": 12.4, "r2": 0.9991, "max_abs_g": 31.0,
                  "loocv_rmse_g": 15.2},
      "n_points": 11,
      "range_g": [50.0, 4000.0],
      "range_ohms": [653.0, 38000.0],
      "created": "2026-11-11T18:22:01"
    }
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np


# ---------------------------------------------------------------------------
# Evaluacion
# ---------------------------------------------------------------------------

def predict_grams(model: dict, ohms, saturate: bool = False):
    """Gramos estimados a partir de la resistencia del FSR (ohmios).

    Con ``saturate=True`` la tabla de interpolacion imita al firmware:
    por debajo del nodo de menos peso devuelve 0 g, y por encima del nodo
    de mas peso devuelve el peso maximo. Sin saturar devuelve ``nan``
    fuera del rango medido (util para metricas y LOOCV).
    """
    kind = model["model"]
    p = model["params"]

    if kind == "power_law":
        r = np.asarray(ohms, dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            out = p["A"] * np.power(r, p["B"])
        return float(out) if np.isscalar(ohms) else out

    if kind == "quad_log":
        r = np.asarray(ohms, dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            lr = np.log10(r)
            out = np.power(10.0, p["c0"] + p["c1"] * lr + p["c2"] * lr * lr)
        return float(out) if np.isscalar(ohms) else out

    if kind == "interp":
        # Interpolacion LINEAL en log-log. Es la misma que hace el firmware
        # (tabla de nodos en calibration.h), asi que PC y ESP32 coinciden
        # exactamente. Fuera del rango medido devuelve nan: el modelo solo
        # es valido entre el primer y el ultimo nodo.
        r = np.asarray(ohms, dtype=float)
        lr_grid = np.asarray(p["log_r"], dtype=float)
        lg_vals = np.asarray(p["log_g"], dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            lx = np.log10(r)
            out = np.power(10.0, np.interp(lx, lr_grid, lg_vals,
                                           left=np.nan, right=np.nan))
            if saturate:
                # Mismo criterio que cal_grams(): R menor que el nodo de
                # mas peso -> peso maximo; R mayor que el de menos peso -> 0.
                out = np.where(lx <= lr_grid[0], 10.0 ** lg_vals[0], out)
                out = np.where(lx >= lr_grid[-1], 0.0, out)
        return float(out) if np.isscalar(ohms) else out

    raise ValueError(f"modelo desconocido: {kind!r}")


def predict_grams_from_mv(model: dict, mv, saturate: bool = False, **hardware):
    """Gramos estimados a partir de la lectura en milivoltios."""
    from pc import fsr
    hw = dict(model.get("hardware", {}))
    hw.update({k: v for k, v in hardware.items() if v is not None})
    ohms = fsr.ohms_from_mv(
        mv,
        vcc=hw.get("vcc", fsr.VCC_VOLTS),
        r_fixed=hw.get("r_fixed", fsr.R_FIXED_OHMS),
        fsr_to_vcc=str(hw.get("topologia", "fsr_to_vcc")) == "fsr_to_vcc")
    return predict_grams(model, ohms, saturate=saturate)


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------

def save_model(path: Path, model: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(model, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def load_model(path: str | Path) -> dict:
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_models(path: str | Path) -> dict[str, dict]:
    """Carga uno o varios JSON. Acepta un archivo o un directorio."""
    p = Path(path)
    files = [p] if p.is_file() else sorted(p.glob("*.json"))
    out: dict[str, dict] = {}
    for f in files:
        try:
            m = load_model(f)
            out[m.get("nombre", f.stem)] = m
        except (json.JSONDecodeError, KeyError):
            continue
    return out


def describe(model: dict) -> str:
    """Ecuacion legible, para comentarios y consola."""
    p = model["params"]
    if model["model"] == "power_law":
        return f"gramos = {p['A']:.6g} * R^({p['B']:.6g})"
    if model["model"] == "quad_log":
        return (f"log10(g) = {p['c0']:.6g} + {p['c1']:.6g}*log10(R) "
                f"+ {p['c2']:.6g}*log10(R)^2")
    if model["model"] == "interp":
        return (f"interpolacion lineal log-log sobre {len(p['log_r'])} nodos "
                f"(la misma tabla que usa el firmware)")
    return str(model["model"])


def is_finite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False
