"""Ajuste del modelo de calibracion y diagnostico.

Toma el CSV generado por capture.py y, para cada sensor:
  * convierte mV -> resistencia del FSR,
  * ajusta tres modelos y los compara con validacion cruzada,
  * imprime metricas en gramos (no en ohmios, que no dicen nada util),
  * guarda un JSON con el modelo y una grafica de diagnostico,
  * opcionalmente genera firmware/fsr_stream/calibration.h.

Uso:
    python pc/fit.py --csv data/raw/fsr_lote1.csv
    python pc/fit.py --csv data/raw/fsr_lote1.csv --show
"""

from __future__ import annotations

import argparse
import datetime as _dt
import math
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from pc import fsr
from pc.model import describe, predict_grams, save_model

MODEL_LABELS = {
    "power_law": "M1 ley potencial",
    "quad_log": "M2 cuadratica log",
    "interp": "M3 interpolacion",
}


# ---------------------------------------------------------------------------
# Lectura y preparacion
# ---------------------------------------------------------------------------

def read_meta(path: Path) -> dict[str, str]:
    meta: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.startswith("#"):
                break
            body = line[1:].strip()
            if "=" in body:
                k, v = body.split("=", 1)
                meta[k.strip()] = v.strip()
    return meta


def load_capture(path: Path) -> tuple[pd.DataFrame, dict[str, str]]:
    meta = read_meta(path)
    df = pd.read_csv(path, comment="#")
    for col in ("gramos", "mv_mean", "mv_std", "raw_mean", "raw_std", "n", "ch"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df, meta


def add_resistance(df: pd.DataFrame, vcc: float, r_fixed,
                   fsr_to_vcc: bool) -> pd.DataFrame:
    """Convierte mV -> ohmios usando la R_fija de CADA canal.

    ``r_fixed`` puede ser un numero o una lista (p. ej. 1633 para S3 y 1641
    para S4). El CSV trae la columna ``ch`` y de ahi se elige el valor.
    """
    df = df.copy()
    mv = df["mv_mean"].to_numpy(dtype=float)
    if "ch" in df.columns:
        ch = df["ch"].fillna(0).to_numpy(dtype=int)
    else:
        ch = np.zeros(len(df), dtype=int)

    ohms = np.empty(len(df), dtype=float)
    for i in np.unique(ch):
        mask = ch == i
        ohms[mask] = fsr.ohms_from_mv(
            mv[mask], vcc=vcc, r_fixed=fsr.r_fixed_for(int(i), r_fixed),
            fsr_to_vcc=fsr_to_vcc)
    df["ohms"] = ohms
    return df


def parse_channel_selector(text: str | None) -> set[int] | None:
    """Convierte 'S3,S4' o '0,1' en indices de canal.

    Devuelve None si no se especifico nada (todos los canales).
    """
    if not text:
        return None
    out: set[int] = set()
    for token in text.replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        upper = token.upper()
        if upper in fsr.CHANNELS:
            out.add(fsr.CHANNELS.index(upper))
            continue
        try:
            idx = int(upper)
        except ValueError:
            raise ValueError(f"canal desconocido: {token!r} (usa S3, S4 o 0, 1)")
        if idx < 0:
            raise ValueError(f"indice de canal invalido: {idx}")
        out.add(idx)
    return out or None


def has_signal(sub: pd.DataFrame, min_mv: float | None = None) -> bool:
    """True si el canal supera el umbral de senal minima en algun punto."""
    if min_mv is None:
        min_mv = fsr.ADC_MIN_V * 1000.0
    if sub.empty or "mv_mean" not in sub.columns:
        return False
    peak = float(np.nanmax(sub["mv_mean"].to_numpy(dtype=float)))
    return math.isfinite(peak) and peak >= min_mv


def aggregate(df: pd.DataFrame) -> pd.DataFrame:
    """Una fila por (canal, gramos): media de las repeticiones."""
    g = df.groupby(["ch", "gramos"], as_index=False).agg(
        mv=("mv_mean", "mean"),
        mv_sd_within=("mv_std", "mean"),
        mv_sd_between=("mv_mean", "std"),
        ohms=("ohms", "mean"),
        reps=("mv_mean", "size"),
        nombre=("nombre", "first"),
    )
    g["mv_sd_between"] = g["mv_sd_between"].fillna(0.0)
    return g


# ---------------------------------------------------------------------------
# Ajuste de los tres modelos
# ---------------------------------------------------------------------------

def _fit_one(kind: str, ohms: np.ndarray, grams: np.ndarray) -> dict:
    lr = np.log10(ohms)
    lg = np.log10(grams)

    if kind == "power_law":
        if len(grams) < 2:
            raise ValueError("hacen falta >=2 puntos")
        b, log_a = np.polyfit(lr, lg, 1)
        return {"model": kind, "params": {"A": float(10.0 ** log_a), "B": float(b)}}

    if kind == "quad_log":
        if len(grams) < 4:
            raise ValueError("hacen falta >=4 puntos")
        c = np.polyfit(lr, lg, 2)
        return {"model": kind,
                "params": {"c2": float(c[0]), "c1": float(c[1]), "c0": float(c[2])}}

    if kind == "interp":
        if len(grams) < 3:
            raise ValueError("hacen falta >=3 puntos")
        order = np.argsort(lr)
        lr_s, lg_s = lr[order], lg[order]
        # PCHIP exige nodos estrictamente crecientes
        keep = np.concatenate(([True], np.diff(lr_s) > 1e-9))
        if keep.sum() < 3:
            raise ValueError("puntos demasiado repetidos en resistencia")
        return {"model": kind,
                "params": {"log_r": lr_s[keep].tolist(), "log_g": lg_s[keep].tolist()}}

    raise ValueError(kind)


def _metrics(y: np.ndarray, yhat) -> dict:
    yhat = np.asarray(yhat, dtype=float)
    ok = np.isfinite(yhat)
    if ok.sum() == 0:
        return {"rmse_g": float("nan"), "r2": float("nan"),
                "max_abs_g": float("nan"), "n_eval": 0}
    err = yhat[ok] - y[ok]
    ss_res = float(np.sum(err ** 2))
    ss_tot = float(np.sum((y[ok] - y[ok].mean()) ** 2))
    return {
        "rmse_g": float(np.sqrt(ss_res / ok.sum())),
        "r2": float(1.0 - ss_res / ss_tot) if ss_tot > 0 else float("nan"),
        "max_abs_g": float(np.max(np.abs(err))),
        "n_eval": int(ok.sum()),
    }


def _loocv(kind: str, ohms: np.ndarray, grams: np.ndarray) -> float:
    n = len(grams)
    if n < 4:
        return float("nan")
    preds = np.full(n, np.nan)
    for i in range(n):
        mask = np.ones(n, dtype=bool)
        mask[i] = False
        try:
            m = _fit_one(kind, ohms[mask], grams[mask])
            preds[i] = float(predict_grams(m, ohms[i]))
        except Exception:
            continue
    ok = np.isfinite(preds)
    if ok.sum() < 2:
        return float("nan")
    return float(np.sqrt(np.mean((preds[ok] - grams[ok]) ** 2)))


def fit_all(ohms: np.ndarray, grams: np.ndarray) -> list[dict]:
    out = []
    for kind in ("power_law", "quad_log", "interp"):
        try:
            m = _fit_one(kind, ohms, grams)
        except Exception as exc:
            out.append({"model": kind, "params": {}, "error": str(exc),
                        "metrics": {"rmse_g": float("nan"), "r2": float("nan"),
                                    "max_abs_g": float("nan"), "n_eval": 0},
                        "loocv_rmse_g": float("nan")})
            continue
        m["metrics"] = _metrics(grams, predict_grams(m, ohms))
        m["loocv_rmse_g"] = _loocv(kind, ohms, grams)
        out.append(m)
    return out


def pick_best(fits: list[dict]) -> dict:
    valid = [f for f in fits if f.get("params")]
    if not valid:
        raise SystemExit("No se pudo ajustar ningun modelo; revisa los datos.")
    def key(f):
        l = f["loocv_rmse_g"]
        return math.inf if not math.isfinite(l) else l
    return min(valid, key=key)


# ---------------------------------------------------------------------------
# Graficas
# ---------------------------------------------------------------------------

def plot_channel(agg: pd.DataFrame, fits: list[dict], best: dict,
                 path: Path, vcc: float, r_fixed: float, fsr_to_vcc: bool,
                 show: bool = False, resid_model: dict | None = None) -> None:
    import matplotlib
    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    name = agg["nombre"].iloc[0]
    r = agg["ohms"].to_numpy()
    g = agg["gramos"].to_numpy()
    mv = agg["mv"].to_numpy()

    ok = np.isfinite(r) & (g > 0)
    r_ok, g_ok = r[ok], g[ok]

    fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(15.5, 4.6))
    fig.suptitle(f"{name}  -  R_fixed={r_fixed:.0f} ohm, VCC={vcc:.2f} V", fontsize=12)

    # --- 1. log-log gramos vs R --------------------------------------
    ax1.loglog(r_ok, g_ok, "o", ms=6, label="medido", zorder=5)
    rr = np.logspace(np.log10(r_ok.min()) * 0.97, np.log10(r_ok.max()) * 1.03, 200)
    colors = {"power_law": "tab:orange", "quad_log": "tab:green", "interp": "tab:purple"}
    for f in fits:
        if not f.get("params"):
            continue
        gg = predict_grams(f, rr)
        ax1.loglog(rr, gg, "--", lw=1.6, color=colors[f["model"]],
                   label=MODEL_LABELS[f["model"]], alpha=0.9)
    ax1.set_xlabel("R del FSR (ohm)")
    ax1.set_ylabel("gramos")
    ax1.set_title("Ajuste log-log")
    ax1.grid(True, which="both", alpha=0.25)
    ax1.legend(fontsize=8)

    # --- 2. residuales del modelo embebible (M1) ----------------------
    # Si se usara M3, los residuales serian ~0 por construccion y no dirian
    # nada; con M1 se ve de verdad donde falla el modelo y que punto esta mal.
    rm = resid_model if (resid_model and resid_model.get("params")) else best
    pred = predict_grams(rm, r_ok)
    err = pred - g_ok
    ax2.axhline(0, color="0.4", lw=1)
    ax2.plot(g_ok, err, "o-", color="tab:blue")
    ax2.set_xlabel("gramos reales")
    ax2.set_ylabel("error (g)")
    ax2.set_title(f"Residuales {MODEL_LABELS[rm['model']]}\n"
                  f"RMSE={rm['metrics']['rmse_g']:.1f} g")
    ax2.grid(True, alpha=0.3)

    # --- 3. mV vs gramos con el modelo invertido ---------------------
    ax3.plot(mv[ok], g_ok, "o", color="tab:red", label="medido")
    w = np.linspace(max(mv[ok].min(), 1.0), min(mv[ok].max(), fsr.ADC_MAX_V * 1000), 300)
    r_from_mv = np.asarray(fsr.ohms_from_mv(w, vcc=vcc, r_fixed=r_fixed,
                                            fsr_to_vcc=fsr_to_vcc))
    gg = predict_grams(best, r_from_mv)
    ax3.plot(w, gg, "-", color="0.2", lw=1.5, label="modelo")
    ax3.axvline(fsr.ADC_TRUST_V * 1000, color="tab:orange", ls=":", lw=1.4,
                label=f"limite ADC {fsr.ADC_TRUST_V * 1000:.0f} mV")
    ax3.set_xlabel("tension del nodo (mV)")
    ax3.set_ylabel("gramos")
    ax3.set_title("Lectura directa")
    ax3.grid(True, alpha=0.3)
    ax3.legend(fontsize=8)

    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130)
    if show:
        plt.show()
    plt.close(fig)


# ---------------------------------------------------------------------------
# calibration.h para el firmware
# ---------------------------------------------------------------------------

def write_calibration_header(models: list[dict], path: Path,
                             vcc: float, r_fixed, fsr_to_vcc: bool) -> None:
    by_ch = {int(m.get("ch", i)): m for i, m in enumerate(models)}
    max_ch = max(by_ch) if by_ch else 0
    n_ch = max(len(fsr.CHANNELS), max_ch + 1)
    r_vals = [fsr.r_fixed_for(i, r_fixed) for i in range(n_ch)]
    lines = [
        "/* Generado automaticamente por pc/fit.py. NO EDITAR A MANO. */",
        f"/* Fecha: {_dt.datetime.now().isoformat(timespec='seconds')} */",
        "/*",
        "   Uso en el firmware:",
        "     ohms   = CAL_R_FIXED_OHMS[i] * (CAL_VCC - v) / v   (fsr_to_vcc)",
        "     gramos = CAL_A[i] * powf(ohms, CAL_B[i]);",
        "   Cada canal puede usar una resistencia fija distinta.",
        "*/",
        "#pragma once",
        "#include <cmath>   // nanf() para canales sin modelo",
        "",
        f"#define CAL_N_CH {n_ch}",
        "static const float CAL_R_FIXED_OHMS[CAL_N_CH] = { "
        + ", ".join(f"{v:.1f}f" for v in r_vals) + " };",
        f"static const float CAL_VCC_VOLTS    = {vcc:.3f}f;",
        f"static const bool  CAL_FSR_TO_VCC   = {'true' if fsr_to_vcc else 'false'};",
        "",
    ]
    a_vals, b_vals, ok, missing = [], [], True, []
    for i in range(n_ch):
        m = by_ch.get(i)
        emb = None
        if m:
            emb = m.get("embedded") or (
                m if m.get("model") == "power_law" else None)
        if not emb or not emb.get("params"):
            ok = False
            missing.append(i)
            a_vals.append(float("nan"))
            b_vals.append(float("nan"))
        else:
            a_vals.append(emb["params"]["A"])
            b_vals.append(emb["params"]["B"])

    if not ok:
        names = ", ".join(
            fsr.CHANNELS[i] if i < len(fsr.CHANNELS) else f"ch{i}"
            for i in missing)
        lines.append("/* AVISO: algun canal no se pudo ajustar con la ley potencial;")
        lines.append(f"   canales sin modelo: {names} (parametros nan).")
        lines.append("   Reajusta con --model power_law o completa la captura. */")
    else:
        lines.append("/* Coeficientes de la LEY POTENCIAL (M1), que es el modelo")
        lines.append("   embebible, aunque el mejor modelo del analisis sea otro. */")

    lines.append("static const float CAL_A[] = { "
                 + ", ".join(f"{v:.8g}f" for v in a_vals) + " };")
    lines.append("static const float CAL_B[] = { "
                 + ", ".join(f"{v:.8g}f" for v in b_vals) + " };")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Ajusta el modelo gramos <-> FSR y evalua su precision.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--csv", required=True, help="CSV generado por capture.py")
    p.add_argument("--out-dir", default=str(fsr.DATA_MODELS))
    p.add_argument("--vcc", type=float, help="sobrescribe el VCC del CSV")
    p.add_argument("--r-fixed",
                   help="sobrescribe R_fixed del CSV; acepta un valor por canal: "
                        "1633,1641")
    p.add_argument("--topology", choices=["fsr_to_vcc", "fsr_to_gnd"],
                   help="sobrescribe la topologia del CSV")
    p.add_argument("--model", choices=["auto", "power_law", "quad_log", "interp"],
                   default="auto", help="forzar un modelo en vez de elegir el mejor")
    p.add_argument("--drop-saturated", action="store_true",
                   help="descartar puntos por encima del limite fiable del ADC")
    p.add_argument("--only",
                   help="ajustar solo estos canales: S3, S4 o 0, 1 "
                        "(por defecto, todos los que tengan senal)")
    p.add_argument("--show", action="store_true", help="abrir las graficas")
    p.add_argument("--no-header", action="store_true",
                   help="no generar calibration.h")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise SystemExit(f"no existe {csv_path}")

    df, meta = load_capture(csv_path)
    if df.empty:
        raise SystemExit("el CSV no tiene datos")

    vcc = args.vcc if args.vcc is not None else float(meta.get("vcc", fsr.VCC_VOLTS))
    if "ch" in df.columns:
        ch_present = sorted(int(x) for x in df["ch"].dropna().unique())
    else:
        ch_present = [0]
    # El firmware define los canales; el CSV puede traer solo uno.
    n_ch = max(len(fsr.CHANNELS), max(ch_present) + 1)
    raw_r = args.r_fixed if args.r_fixed is not None else meta.get(
        "r_fixed", fsr.R_FIXED_OHMS)
    try:
        r_fixed = fsr.parse_r_fixed(raw_r, max(n_ch, 1))
    except ValueError as exc:
        raise SystemExit(f"ERROR en --r-fixed: {exc}")
    topo = args.topology or meta.get("topologia", fsr.topology_name())
    fsr_to_vcc = fsr.parse_topology(topo)

    try:
        only = parse_channel_selector(args.only)
    except ValueError as exc:
        raise SystemExit(f"ERROR en --only: {exc}")
    if only is not None:
        missing = sorted(only - set(ch_present))
        if missing:
            names = ", ".join(
                fsr.CHANNELS[i] if i < len(fsr.CHANNELS) else f"ch{i}"
                for i in missing)
            raise SystemExit(
                f"ERROR: --only pide canal(es) que no estan en el CSV: {names}")
        df = df[df["ch"].fillna(-1).astype(int).isin(only)]

    df = add_resistance(df, vcc, r_fixed, fsr_to_vcc)

    print()
    print("=" * 74)
    print(f"  AJUSTE DE CALIBRACION  -  {csv_path.name}")
    print("=" * 74)
    print(f"  divisor : R_fixed={fsr.format_r_fixed(r_fixed)} ohm  "
          f"VCC={vcc:.3f} V  ({topo})")
    print(f"  filas   : {len(df)}")
    if only is not None:
        names = ", ".join(
            fsr.CHANNELS[i] if i < len(fsr.CHANNELS) else f"ch{i}"
            for i in sorted(only))
        print(f"  canales : solo {names} (--only)")
    if meta.get("creado"):
        print(f"  creado  : {meta['creado']}")
    print()

    # --- avisos globales -------------------------------------------------
    sat = df[df["mv_mean"] > fsr.ADC_TRUST_V * 1000.0]
    if len(sat):
        chans = sorted(sat["nombre"].dropna().unique().tolist())
        print(f"  [!] {len(sat)} fila(s) por encima de {fsr.ADC_TRUST_V * 1000:.0f} mV "
              f"({', '.join(chans)}).")
        print("      Zona no caracterizada del ADC: el ajuste puede quedar sesgado.")
        if args.drop_saturated:
            df = df[df["mv_mean"] <= fsr.ADC_TRUST_V * 1000.0]
            print("      --> descartadas (--drop-saturated).")
        else:
            print("      --> se mantienen. Prueba --drop-saturated para comparar.")
        print()
    low = df[(df["mv_mean"] < fsr.ADC_MIN_V * 1000.0) & (df["gramos"] > 0)]
    if len(low):
        print(f"  [!] {len(low)} fila(s) con senal casi nula y peso > 0: "
              f"contacto malo o divisor desconectado.")

    out_dir = Path(args.out_dir)
    models_out: list[dict] = []
    min_mv = fsr.ADC_MIN_V * 1000.0

    for ch, sub in df.groupby("ch"):
        if "nombre" in sub.columns and not sub["nombre"].dropna().empty:
            name = str(sub["nombre"].dropna().iloc[0])
        else:
            name = f"ch{int(ch)}"
        if not has_signal(sub, min_mv):
            peak = float(np.nanmax(sub["mv_mean"].to_numpy(dtype=float)))
            print(f"  [ ] {name}: sin senal (max {peak:.1f} mV < "
                  f"{min_mv:.0f} mV); se omite del ajuste.")
            continue

        rf_ch = fsr.r_fixed_for(int(ch), r_fixed)
        agg = aggregate(sub)
        fit_rows = agg[(agg["gramos"] > 0) & np.isfinite(agg["ohms"])]
        if len(fit_rows) < 2:
            print(f"  [x] {name}: solo {len(fit_rows)} punto(s) util(es); "
                  f"necesito al menos 2 con peso > 0.")
            continue

        ohms = fit_rows["ohms"].to_numpy(dtype=float)
        grams = fit_rows["gramos"].to_numpy(dtype=float)

        order = np.argsort(grams)
        if np.any(np.diff(ohms[order]) > 0):
            print(f"  [!] {name}: la resistencia no baja de forma monotona al "
                  f"aumentar el peso (revisa contacto/histeresis).")

        fits = fit_all(ohms, grams)
        best = pick_best(fits)
        if args.model != "auto":
            forced = [f for f in fits if f["model"] == args.model and f.get("params")]
            if not forced:
                raise SystemExit(f"no se pudo ajustar --model {args.model}")
            best = forced[0]

        print(f"  --- {name} ({len(fit_rows)} puntos, "
              f"{grams.min():g}..{grams.max():g} g) ---")
        print(f"  {'modelo':<20}{'RMSE(g)':>10}{'R2':>9}"
              f"{'max|err|(g)':>13}{'LOOCV(g)':>11}")
        for f in fits:
            if not f.get("params"):
                print(f"  {MODEL_LABELS[f['model']]:<20}{'--':>10}{'--':>9}"
                      f"{'--':>13}{'--':>11}   ({f.get('error', '')})")
                continue
            m = f["metrics"]
            loo = f["loocv_rmse_g"]
            loo_s = f"{loo:11.1f}" if math.isfinite(loo) else f"{'--':>11}"
            print(f"  {MODEL_LABELS[f['model']]:<20}{m['rmse_g']:10.1f}"
                  f"{m['r2']:9.5f}{m['max_abs_g']:13.1f}{loo_s}"
                  + ("   <== elegido" if f is best else ""))
        print(f"  ecuacion: {describe(best)}")
        if best["model"] == "interp":
            print("  nota    : M3 interpola exactamente los nodos medidos (RMSE 0 por")
            print("            construccion); su LOOCV es el dato informativo.")
        m1 = next((f for f in fits
                   if f["model"] == "power_law" and f.get("params")), None)
        if m1 is not None:
            loo1 = m1["loocv_rmse_g"]
            loo1_s = f"{loo1:.1f} g" if math.isfinite(loo1) else "n/d"
            print(f"  firmware: A={m1['params']['A']:.6g}  B={m1['params']['B']:.6g}"
                  f"   (M1, LOOCV {loo1_s})")
        print()

        model = {
            "session": csv_path.stem,
            "captured_at": str(meta.get("creado", "")),
            "ch": int(ch),
            "nombre": name,
            "model": best["model"],
            "params": best["params"],
            "hardware": {"vcc": vcc, "r_fixed": rf_ch, "topologia": topo},
            "metrics": {
                "rmse_g": best["metrics"]["rmse_g"],
                "r2": best["metrics"]["r2"],
                "max_abs_g": best["metrics"]["max_abs_g"],
                "loocv_rmse_g": best["loocv_rmse_g"],
            },
            "embedded": (
                {
                    "model": "power_law",
                    "params": m1["params"],
                    "loocv_rmse_g": m1["loocv_rmse_g"],
                } if m1 is not None else None
            ),
            "n_points": int(len(fit_rows)),
            "range_g": [float(grams.min()), float(grams.max())],
            "range_ohms": [float(np.min(ohms)), float(np.max(ohms))],
            "created": _dt.datetime.now().isoformat(timespec="seconds"),
        }
        models_out.append(model)

        json_path = out_dir / f"{csv_path.stem}_{name}.json"
        save_model(json_path, model)
        print(f"  modelo  -> {json_path}")

        png_path = out_dir / f"{csv_path.stem}_{name}.png"
        plot_channel(fit_rows, fits, best, png_path, vcc, rf_ch, fsr_to_vcc,
                     show=args.show, resid_model=m1)
        print(f"  grafica -> {png_path}")
        print()

    if not models_out:
        raise SystemExit("no se genero ningun modelo")

    if not args.no_header:
        hdr = fsr.FIRMWARE_DIR / "calibration.h"
        write_calibration_header(models_out, hdr, vcc, r_fixed, fsr_to_vcc)
        print(f"  cabecera firmware -> {hdr}")

    print()
    print("  Criterio practico: para pesar en gramos con un FSR402, un RMSE")
    print("  por debajo del 3 % del fondo de escala ya es un buen resultado.")
    print("  La histeresis (carga/descarga) no la corrige ningun modelo: si")
    print("  mediste subida y bajada, comparalas por separado.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
