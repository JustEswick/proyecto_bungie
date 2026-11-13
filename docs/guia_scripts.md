# Guía de scripts: qué hace cada uno y cómo usarlo

Este documento es el **manual de referencia** de las herramientas del
proyecto. Responde a "¿qué script uso, con qué opciones y qué va a producir?".

| Documento | Responde a |
|---|---|
| [`README.md`](../README.md) | "Quiero empezar ya" — instalación y comandos básicos |
| [`docs/procedimiento_calibracion.md`](procedimiento_calibracion.md) | "¿Cómo monto y mido bien?" — mecánica y método |
| **`docs/guia_scripts.md`** (este) | **"¿Qué hace este script y qué parámetro le paso?"** |

Todos los comandos se ejecutan desde la raíz del proyecto:

```powershell
cd "C:\ruta\a\proyecto_bungie"
.\.venv\Scripts\Activate.ps1
```

Si no activas el entorno, sustituye `python` por `.\.venv\Scripts\python.exe`.

---

## 1. El pipeline de un vistazo

```
firmware/fsr_stream.ino          ESP32: solo adquiere y envía CSV
        │  ms,raw0,mv0,raw1,mv1  (25 Hz)
        ▼
pc/probe.py                      ¿el enlace funciona? veredicto
        │
        ▼
pc/capture.py  ──────────────►   data/raw/<sesion>.csv
        │                               │
        │                               ▼
        │                        pc/fit.py  ──────►  data/models/<sesion>_<canal>.json
        │                               │             data/models/<sesion>_<canal>.png
        │                               ▼
        │                        firmware/fsr_stream/calibration.h
        │                               │
        ▼                               ▼
pc/live.py  ◄──────────────────  modelo en gramos (validación en vivo)
```

Regla mental: **el firmware no convierte a gramos**. Todo el cálculo vive en el
PC para poder iterar sin reflashear. `calibration.h` solo se usa si más adelante
quieres que el ESP32 entregue gramos.

---

## 2. Tabla resumen

| Script | Función | Entrada | Salida | Cuándo |
|---|---|---|---|---|
| `selftest.py` | Verifica entorno y matemática | — | informe OK/FALLO | siempre, antes de empezar |
| `probe.py` | Diagnostica el puerto y el firmware | COM | veredicto legible | cuando "no llegan datos" |
| `capture.py` | Captura puntos y mide el rango | COM + gramos | CSV | durante la calibración |
| `fit.py` | Ajusta y evalúa modelos | CSV | JSON + PNG + `.h` | después de capturar |
| `live.py` | Monitor en gramos | COM + JSON | pantalla | para validar |
| `fsr.py` | Constantes y conversiones | — | — | configuración (se edita) |
| `link.py` | Puerto serie + simulador | — | — | interno |
| `model.py` | Carga/evalúa modelos JSON | — | — | interno |

Los tres últimos no se invocan a mano: son módulos que importan los demás.

---

## 3. `selftest.py` — comprobación del entorno

**Qué hace:** comprueba que están `pyserial`, `numpy`, `scipy`, `matplotlib` y
`pandas`; que las conversiones mV ↔ ohmios son coherentes; que el simulador
produce datos; y que el ajuste recupera una ley conocida. **No toca hardware**
y **no escribe** en `data/`.

**Uso:**

```powershell
python pc\selftest.py
```

**Salida esperada:**

```
...
Todo correcto: el entorno esta listo para calibrar.
```

Si algo falla, imprime `FALLO` con el motivo exacto. Código de salida 0 = todo
bien, 1 = algún fallo.

**Cuándo:** después de instalar dependencias, antes de una sesión de banco, y
cada vez que toques `fsr.py`.

---

## 4. `probe.py` — diagnóstico del enlace

**Qué hace:** abre el puerto, escucha unos segundos y clasifica lo que llega.
Es la respuesta a "la placa no me registra nada". No crea ningún archivo.

**Uso:**

```powershell
python pc\probe.py --port COM4
python pc\probe.py --port COM4 --seconds 6 --raw
python pc\probe.py                      # autodetecta el puerto
```

**Opciones:**

| Opción | Defecto | Para qué |
|---|---|---|
| `--port` | autodetecta | puerto serie, p. ej. `COM4` |
| `--baud` | `115200` | debe coincidir con `Serial.begin()` |
| `--seconds` | `4` | cuánto escuchar |
| `--channels` | `2` | canales esperados en el CSV |
| `--raw` | no | muestra las líneas recibidas tal cual |

**Salida (caso correcto):**

```
====================================================================
  DIAGNOSTICO DEL ENLACE SERIE
====================================================================
  Puerto   : COM4 @ 115200 baudios
  Duracion : 4.0 s
  Lineas   : 103 total (101 validas, 0 formato antiguo, 2 cabeceras)
  Frecuencia: 25.2 muestras/s
  Cabecera : # ch0=S3 ch1=S4 r_fixed=1637.00 vcc=3.27 ...
  Muestra  : 12345,1534,1234.5,1480,1190.2

  VEREDICTO: OK - el firmware responde en el formato esperado
  Puedes calibrar: python pc/capture.py --port COM4 --session <nombre>
```

**Veredictos posibles:**

| Veredicto | Significado | Acción |
|---|---|---|
| `OK` | CSV válido | puedes calibrar |
| `FIRMWARE ANTIGUO` | llega `S3: 0 (Sin presion)` | sube `firmware/fsr_stream.ino` |
| `PUERTO OCUPADO` | otro programa usa el COM | cierra el Monitor Serie del IDE |
| `SIN DATOS` | el puerto abre pero no llega nada | revisa alimentación y baudrate |
| `BYTES NO LEGIBLES` | baudrate incorrecto | prueba `--baud 9600` / `57600` |
| `SOLO CABECERAS` | llegan `#` pero no muestras | vuelve a subir el firmware |
| `FORMATO DESCONOCIDO` | hay texto, pero no es el CSV esperado | compara con `ms,raw0,mv0,raw1,mv1` |

Código de salida: `0` si el enlace está bien, `1` si hay problema, `2` si no
pudo abrir el puerto.

---

## 5. `capture.py` — captura de puntos y comprobación de rango

Es el script que más vas a usar. Tiene tres modos:

1. **Interactivo** (por defecto): tú escribes el peso de la báscula.
2. **Barrido** (`--sweep`): recorre una lista y espera estabilidad.
3. **Comprobación de rango** (`--range-check`): mide el pico y recomienda
   `R_fija`. No escribe CSV.

Antes de crear el CSV hace un **preflight**: si el firmware es antiguo o el
puerto está ocupado, aborta sin dejar un archivo vacío.

**Uso básico:**

```powershell
python pc\capture.py --port COM4 --session fsr5kg
python pc\capture.py --port COM4 --session fsr5kg --sweep 0,500,1000,2000,5000
python pc\capture.py --port COM4 --range-check --target 5000
python pc\capture.py --simulate --session prueba
```

**Opciones:**

| Opción | Defecto | Para qué |
|---|---|---|
| `--port` | autodetecta | puerto serie |
| `--baud` | `115200` | baudrate |
| `--session` | `sesion_AAAAMMDD_HHMMSS` | nombre del CSV en `data/raw/` |
| `--hold` | `3.0` | segundos promediados por punto |
| `--vcc` | `3.27` | tensión real del riel |
| `--r-fixed` | `1637.0` | resistencia fija; admite `1633,1641` por canal |
| `--topology` | `fsr_to_vcc` | orientación del divisor |
| `--simulate` | no | datos sintéticos, sin hardware |
| `--sweep` | — | lista de pesos a recorrer |
| `--reps` | `1` | capturas por peso en `--sweep` |
| `--pause` | `10.0` | pausa antes de cada punto de `--sweep` para quitar/aplicar la carga (`0` = sin pausa) |
| `--settle` | `1.0` | espera tras aplicar el peso |
| `--stab-window` | `1.0` | ventana para medir estabilidad |
| `--stab-mv` | `3.0` | desviación máxima para considerar estable |
| `--stab-timeout` | `15.0` | espera máxima por estabilidad |
| `--range-check` | no | solo medir pico y recomendar `R_fija` |
| `--target` | `5000` | carga objetivo de `--range-check`, en gramos |
| `--range-seconds` | `10` | duración de `--range-check` |
| `--yes` | no | no pedir confirmación al guardar |

### 5.1 Modo interactivo

```
  Lectura en vivo (ventana 3 s):
    S3  mv=   412.3 +/-  1.9  R=    10.0 kohm  n=75
    S4  mv=   398.0 +/-  2.2  R=    10.5 kohm  n=75

  gramos en la bascula >
```

| Entrada | Efecto |
|---|---|
| un número (`1000`) | mide y pide confirmación `[S/n/r]` |
| ENTER vacío | solo refresca la lectura, **no guarda** |
| `u` | deshace el último punto |
| `l` | lista los puntos guardados |
| `q` | termina y guarda |
| `?` | ayuda |

La confirmación `[S/n/r]` evita guardar un punto malo: `S` guarda, `n` descarta,
`r` repite la medida.

### 5.2 Modo barrido

```powershell
python pc\capture.py --port COM4 --session fsr5kg `
    --sweep 0,100,200,300,500,750,1000,1500,2000,3000,4000,5000 `
    --reps 2 --hold 2.0 --vcc 3.27 --r-fixed 1633,1641
```

Antes de cada punto muestra una cuenta atrás de `--pause` segundos (10 por
defecto): es el tiempo para **quitar la carga anterior y aplicar la siguiente**.
Puedes terminar la espera antes pulsando ENTER. Con `--pause 0` el barrido
avanza sin pausa (útil solo con pesos automáticos).

Después espera a que la desviación baje de `--stab-mv` durante `--stab-window`.
Si no lo consigue en `--stab-timeout`, captura igual y avisa. **Revisa el
`mv_std` de ese punto antes de fiarte.**

### 5.3 Comprobación de rango

```powershell
python pc\capture.py --port COM4 --range-check --target 5000
```

Muestra el pico de cada canal durante `--range-seconds` y recomienda
`R_fija ≈ 2,3 · R_fsr(5 kg)`:

```
  canal  mV pico   R a esa carga   R_fija recomendada
     S3   2530.4        514 ohm              1.18 kohm
          -> OK: por debajo de 2450 mV
          -> para 5000 g en ~2.3 V usa R_fija ~= 1.18 kohm (maximo 1.48 kohm)
          -> tu R_fija actual (1637 ohm) es adecuada
```

No crea CSV. Es el paso que decide si necesitas `1k` en vez de `1,67k`.

### 5.4 El CSV que genera

`data/raw/<sesion>.csv`. Cabeceras:

```
# calibracion FSR - puntos de referencia
# vcc=3.27
# r_fixed=1633/1641
# topologia=fsr_to_vcc
# canales=S3,S4
# creado=2026-11-11T18:22:01
timestamp,session,ch,nombre,gramos,mv_mean,mv_std,raw_mean,raw_std,n
```

| Columna | Significado |
|---|---|
| `timestamp` | instante del punto |
| `session` | nombre de la sesión |
| `ch`, `nombre` | índice y nombre del canal (`S3`, `S4`) |
| `gramos` | referencia de la báscula |
| `mv_mean`, `mv_std` | tensión promediada y su dispersión |
| `raw_mean`, `raw_std` | cuentas del ADC (por si quieres rehacer la conversión) |
| `n` | muestras promediadas (`--hold` × 25 Hz) |

Si reutilizas una sesión con otro `vcc`, `r_fixed`, topología o número de
canales, el script se niega: usa otra `--session` para no mezclar
configuraciones.

**Una fila por canal:** cada peso genera dos filas (una de S3 y otra de S4),
aunque solo estés cargando un sensor. Es intencionado: así puedes calibrar los
dos a la vez si comparten carga, y el sensor en reposo sirve de control.
`fit.py` omite el canal sin señal; no hace falta borrar filas del CSV.

---

## 6. `fit.py` — ajuste y evaluación

**Qué hace:** convierte mV → ohmios con la `R_fija` de cada canal, ajusta tres
modelos por sensor y los compara con validación cruzada (LOOCV).

**Uso:**

```powershell
python pc\fit.py --csv data\raw\fsr5kg.csv
python pc\fit.py --csv data\raw\fsr5kg.csv --show
python pc\fit.py --csv data\raw\fsr5kg.csv --vcc 3.27 --r-fixed 1633,1641
```

**Opciones:**

| Opción | Defecto | Para qué |
|---|---|---|
| `--csv` | obligatorio | CSV de `capture.py` |
| `--out-dir` | `data/models` | dónde guardar JSON/PNG |
| `--vcc` | del CSV | sobrescribe la tensión |
| `--r-fixed` | del CSV | sobrescribe la resistencia; admite `1633,1641` |
| `--topology` | del CSV | sobrescribe el divisor |
| `--model` | `auto` | fuerza `power_law`, `quad_log` o `interp` |
| `--drop-saturated` | no | descarta puntos > 2450 mV |
| `--only` | todos con señal | ajusta solo `S3`, `S4` o `0,1` |
| `--show` | no | abre las gráficas |
| `--no-header` | no | no genera `calibration.h` |

**Canales sin señal:** si calibras un sensor por sesión, el otro canal queda en
reposo (~0–1 mV). `fit.py` lo detecta (< 30 mV), lo omite y avisa:

```text
  [ ] S4: sin senal (max 1.1 mV < 30 mV); se omite del ajuste.
```

No genera un modelo basura para ese canal. Para forzar uno concreto:
`--only S3` o `--only S4`.

**Salida por canal:**

```
  --- S3 (11 puntos, 100..5000 g) ---
  modelo                RMSE(g)       R2   max|err|(g)  LOOCV(g)
  M1 ley potencial         18.4  0.99873         41.0       22.1   <== elegido
  M2 cuadratica log        15.2  0.99912         33.4       27.6
  M3 interpolacion         11.9  0.99938         24.0        --
  ecuacion: gramos = 3.2451e+07 * R^(-2.1873)
  firmware: A=3.2451e+07  B=-2.1873   (M1, LOOCV 22.1 g)
  modelo  -> data/models/fsr5kg_S3.json
  grafica -> data/models/fsr5kg_S3.png
```

**Qué genera:**

- `<sesion>_<canal>.json` → modelo elegido + hardware + métricas + `embedded`.
- `<sesion>_<canal>.png` → log-log, residuales y mV vs gramos.
- `firmware/fsr_stream/calibration.h` → `CAL_A[]`, `CAL_B[]` y
  `CAL_R_FIXED_OHMS[]` por canal.

**Cómo elegir el modelo:** `fit.py` usa el **menor LOOCV**. M1 es el único que
cabe en el firmware; el campo `embedded` del JSON guarda siempre M1 aunque el
mejor sea M2/M3. Si M1 tiene un LOOCV pésimo, no lo metas en el ESP32: usa el
modelo en el PC.

---

## 7. `live.py` — monitor en gramos

**Qué hace:** carga uno o varios JSON y muestra `mV`, `ohmios` y `gramos` en
vivo. Sirve para validar con un peso conocido.

**Uso:**

```powershell
python pc\live.py --port COM4 --models data\models
python pc\live.py --simulate --models data\models --weight 1200
```

| Opción | Defecto | Para qué |
|---|---|---|
| `--port` | autodetecta | puerto serie |
| `--baud` | `115200` | baudrate |
| `--models` | `data/models` | un JSON o un directorio |
| `--simulate` | no | sin hardware |
| `--weight` | `0` | peso simulado, en gramos |
| `--window` | `0.5` | ventana de promediado |
| `--hz` | `4` | refresco de pantalla |

Cada canal usa la `R_fija` y el `VCC` guardados en su propio modelo, así que
puedes mezclar sensores con resistencias distintas.

---

## 8. Configuración: `pc/fsr.py`

No se ejecuta. Contiene las constantes que afectan a todos los scripts y las
conversiones. Lo que se toca:

| Constante | Defecto | Cuándo cambiarla |
|---|---|---|
| `R_FIXED_OHMS` | `1637.0` | si tu resistencia real difiere |
| `VCC_VOLTS` | `3.27` | **mídela** con multímetro |
| `FSR_TO_VCC` | `True` | si inviertes el divisor |
| `ADC_TRUST_V` | `2.45` | techo fiable del ADC |
| `ADC_MAX_V` | `3.10` | techo físico |
| `ADC_MIN_V` | `0.03` | por debajo se considera sin carga |
| `BAUDRATE` | `115200` | debe coincidir con el firmware |

También define `CHANNELS = ("S3", "S4")`, el orden exacto del firmware.

---

## 9. El firmware: constantes y formato

Archivo `firmware/fsr_stream/fsr_stream.ino`.

| Constante | Defecto | Para qué |
|---|---|---|
| `R_FIXED_OHMS` | `1637.0f` | informativo; la conversión real la hace el PC |
| `VCC_VOLTS` | `3.27f` | informativo |
| `FSR_TO_VCC` | `true` | orientación del divisor |
| `OVERSAMPLE` | `32` | lecturas ADC por muestra; súbelo si hay ruido |
| `REPORT_HZ` | `25` | líneas por segundo |
| `EMA_ALPHA` | `0.30f` | suavizado de la tensión |
| `HEADER_EVERY_S` | `30` | reimprime la cabecera cada 30 s |

**Formato de línea:**

```
# ch0=S3 ch1=S4 r_fixed=1637.00 vcc=3.27 topologia=fsr_to_vcc oversample=32 hz=25 fw=1.0
# ms,raw0,mv0,raw1,mv1
12345,1534,1234.5,1480,1190.2
```

Las líneas `#` son metadatos: el PC las ignora. Si ves basura en el Monitor
Serie, el baudrate no es 115200.

---

## 10. Recetas de trabajo

### 10.1 Primera vez

```powershell
python pc\selftest.py
# flashea firmware/fsr_stream/fsr_stream.ino desde el Arduino IDE
python pc\probe.py --port COM4
python pc\capture.py --port COM4 --range-check --target 5000
```

Decide la resistencia con el resultado del `--range-check` (ver
[procedimiento, sección 3](procedimiento_calibracion.md#3-comprobar-el-rango-antes-de-calibrar)).

### 10.2 Calibrar un sensor (uno por sesión)

```powershell
python pc\capture.py --port COM4 --session fsr5kg_S3 `
    --sweep 0,100,200,300,500,750,1000,1500,2000,3000,4000,5000 `
    --reps 2 --hold 2.0 --vcc 3.27 --r-fixed 1633,1641
python pc\fit.py --csv data\raw\fsr5kg_S3.csv --only S3
python pc\live.py --port COM4 --models data\models
```

Repite con `--session fsr5kg_S4` y `fit.py ... --only S4` para el otro sensor.
El CSV guarda las dos filas por peso; `fit.py` omite el canal que no cargaste.

Valida con un peso conocido, no con los mismos puntos del ajuste.

### 10.3 Dos sensores a la vez (misma carga)

Si ambos FSR reciben la misma carga, no uses `--only`: se ajustan los dos en la
misma sesión.

```powershell
python pc\capture.py --port COM4 --session mixto --r-fixed 1633,1641 ...
```

El CSV guarda `r_fixed=1633/1641`. Después:

```powershell
python pc\fit.py --csv data\raw\mixto.csv          # lo lee del CSV
python pc\fit.py --csv data\raw\mixto.csv --r-fixed 1633,1641   # o lo fuerzas
```

`fit.py` convierte cada fila con la resistencia de su canal.

### 10.4 Recalibrar tras cambiar `R_fija`

Empieza una **sesión nueva** (`--session otro_nombre`). El CSV guarda su propia
configuración y `fit.py` la usa; mezclar puntos de dos divisores en el mismo
archivo se rechaza a propósito.

### 10.5 Auditar una sesión

Dentro de `capture.py`: `l` lista los puntos, `u` deshace el último. Después
mira `mv_std` en el CSV (criterio < 5 mV) y prueba:

```powershell
python pc\fit.py --csv data\raw\fsr5kg.csv --drop-saturated
```

### 10.6 Pasar el modelo al ESP32

Copia o incluye `firmware/fsr_stream/calibration.h`:

```cpp
#include "calibration.h"
float ohms = CAL_R_FIXED_OHMS[i] * (CAL_VCC_VOLTS - v) / v;
float gramos = CAL_A[i] * powf(ohms, CAL_B[i]);
```

`CAL_R_FIXED_OHMS` es un array si los canales usan resistencias distintas.

---

## 11. Referencia de formatos

### 11.1 CSV de captura

Ver [5.4](#54-el-csv-que-genera). La unidad de `gramos` es el gramo, `mv_*` va
en milivoltios, `raw_*` en cuentas del ADC (0–4095) y `n` es un número de
muestras.

### 11.2 JSON del modelo

```json
{
  "session": "fsr5kg",
  "ch": 0,
  "nombre": "S3",
  "model": "power_law",
  "params": {"A": 3.2e7, "B": -2.19},
  "hardware": {"vcc": 3.27, "r_fixed": 1633.0, "topologia": "fsr_to_vcc"},
  "metrics": {"rmse_g": 18.4, "r2": 0.9987,
              "max_abs_g": 41.0, "loocv_rmse_g": 22.1},
  "embedded": {"model": "power_law", "params": {"A": 3.2e7, "B": -2.19},
               "loocv_rmse_g": 22.1},
  "n_points": 11,
  "range_g": [100.0, 5000.0],
  "range_ohms": [510.0, 38000.0],
  "created": "2026-11-11T18:22:01"
}
```

- `model`/`params` → el mejor modelo.
- `embedded` → M1, el único embebible. Puede ser `null` si M1 no se pudo ajustar.
- `hardware.r_fixed` es la resistencia de **ese** canal.

### 11.3 `calibration.h`

```cpp
#pragma once

#define CAL_N_CH 2
static const float CAL_R_FIXED_OHMS[CAL_N_CH] = { 1633.0f, 1641.0f };
static const float CAL_VCC_VOLTS    = 3.270f;
static const bool  CAL_FSR_TO_VCC   = true;

static const float CAL_A[] = { 3.2e+07f, 2.9e+07f };
static const float CAL_B[] = { -2.19f, -2.11f };
```

### 11.4 Convenciones

| Magnitud | Unidad | Nota |
|---|---|---|
| Resistencia | ohmios (Ω) | `kohm` solo en pantalla |
| Tensión | milivoltios (mV) | el ADC entrega mV calibrados |
| Peso | gramos (g) | la báscula en gramos |
| Tiempo | segundos (s) | `--hold`, `--settle`, `--range-seconds` |
| Frecuencia | Hz | 25 muestras/s del firmware |

La consola imprime ASCII sin acentos a propósito (compatibilidad con consolas
Windows); los documentos sí usan UTF-8.

---

## 12. Chuleta: todas las opciones

**`capture.py`**

```
--port COM4            puerto
--baud 115200          baudrate
--session NOMBRE       nombre del CSV
--hold 3.0             s promediados por punto
--vcc 3.27             tensión del riel
--r-fixed 1633,1641    o un único valor
--topology fsr_to_vcc
--simulate             sin hardware
--sweep 0,500,1000     barrido
--reps 2               repeticiones por peso
--pause 10             pausa antes de cada punto (0 = sin pausa)
--settle 1.0           espera tras aplicar peso
--stab-window 1.0      ventana de estabilidad
--stab-mv 3.0          umbral de estabilidad
--stab-timeout 15      espera máxima
--range-check          medir pico y recomendar R_fija
--target 5000          carga objetivo
--range-seconds 10     duración
--yes                  sin confirmación
```

**`fit.py`**

```
--csv ARCHIVO          obligatorio
--out-dir data/models
--vcc 3.27
--r-fixed 1633,1641    o un único valor
--topology fsr_to_vcc
--model auto           power_law | quad_log | interp
--drop-saturated
--only S3              S3 | S4 | 0,1
--show
--no-header
```

**`probe.py`**

```
--port COM4
--baud 115200
--seconds 4
--channels 2
--raw
```

**`live.py`**

```
--port COM4
--baud 115200
--models data/models
--simulate
--weight 1200
--window 0.5
--hz 4
```

---

## 13. Problemas frecuentes (con el mensaje exacto)

| Mensaje / síntoma | Causa | Solución |
|---|---|---|
| `FIRMWARE ANTIGUO` | la placa ejecuta el sketch de prueba | sube `fsr_stream.ino` desde el IDE |
| `PUERTO OCUPADO` | Monitor Serie del IDE abierto | ciérralo; solo un programa usa el COM |
| `No existe COM4 o esta desconectado.` | cable o número de puerto | revisa Administrador de dispositivos |
| `No llegan muestras validas del ESP32.` | preflight de `capture.py` | ejecuta `probe.py --raw` |
| `muy pocas muestras en la ventana` | `--hold` muy corto o enlace caído | sube `--hold` o revisa el cable |
| `--r-fixed tiene N valores pero hay 2 canales` | lista mal formada | pasa 1 o 2 valores: `1637` o `1633,1641` |
| `ya existe con r_fixed=... y ahora usas ...` | sesión reutilizada con otro hardware | usa otra `--session` |
| `el CSV no tiene datos` | captura interrumpida antes de guardar | repite la captura |
| `solo N punto(s) util(es); necesito al menos 2` | demasiados puntos a 0 g | añade puntos con peso |
| `no encontre modelos en data\models` | aún no ejecutaste `fit.py` | ajusta primero |
| RMSE enorme | mecánica inconsistente o saturación | mira el PNG de residuales y `--drop-saturated` |

Para el detalle físico (por qué la mecánica manda, cómo elegir `R_fija`, cómo
leer las gráficas) sigue en
[`docs/procedimiento_calibracion.md`](procedimiento_calibracion.md).
