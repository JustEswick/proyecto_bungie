# Calibración de sensores FSR402 con ESP32

Sistema para caracterizar dos FSR contra una báscula de cocina (5 kg) y obtener
un modelo que convierta la lectura del ADC en **gramos**, con métricas de error
reales.

Pensado para el montaje:

```
        3V3 ──[ FSR402 ]──┬──[ R 1,67 kΩ ]── GND
                          │
                          ├── GPIO35   (S3)
                          └── GPIO34   (S4)
```

`R 1,67 kΩ` se consigue con **10 kΩ en paralelo con 2 kΩ** (`10k ∥ 2k ≈
1667 Ω` nominal). Mide tu combinación real y pásala con `--r-fixed`: el modelo
usa ese valor. Al presionar, la resistencia del FSR baja y la tensión del nodo
sube: `R_fsr = R_fija · (VCC − V_nodo) / V_nodo`.

Ese valor es el que permite llegar a **5 kg** sin que el ADC del ESP32 entre en
la zona no caracterizada (por encima de ~2,45 V).

---

## 1. Estructura

```
proyecto_bungie/
├─ firmware/fsr_stream/
│  ├─ fsr_stream.ino        firmware: 2 canales, CSV a 25 Hz
│  └─ calibration.h         generado por el ajuste (no editar a mano)
├─ pc/
│  ├─ fsr.py                constantes del divisor y conversiones V <-> ohmios
│  ├─ link.py               puerto serie + simulador (misma interfaz)
│  ├─ diagnose.py           clasifica lo que llega por el puerto
│  ├─ probe.py              diagnóstico del enlace serie
│  ├─ capture.py            toma de puntos y --range-check
│  ├─ fit.py                ajuste de modelos, métricas y gráficas
│  ├─ model.py              modelos en JSON y evaluación
│  ├─ live.py               monitor en vivo en gramos
│  └─ selftest.py           autocomprobación del entorno (sin hardware)
├─ data/raw/                CSV de cada sesión de calibración
├─ data/models/             JSON + PNG de diagnóstico
└─ docs/
   ├─ procedimiento_calibracion.md   protocolo mecánico y físico
   └─ guia_scripts.md                referencia de cada script y sus opciones
```

Guía de referencia rápida: [`docs/guia_scripts.md`](docs/guia_scripts.md).

El `.venv` ya está creado con `--system-site-packages`: ve `numpy`, `scipy`,
`matplotlib` y `pandas` de tu Python global, y solo tiene instalado `pyserial`
de forma local (nada global).

---

## 2. Entorno

Todo listo. Para usar los scripts, activa el entorno en la terminal:

```powershell
cd "C:\ruta\a\proyecto_bungie"
.\.venv\Scripts\Activate.ps1
```

Si PowerShell bloquea el script de activación:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Y si prefieres no activar nada, llama al Python del entorno directamente:

```powershell
.\.venv\Scripts\python.exe pc\capture.py --simulate
```

Comprobar que todo importa:

```powershell
.\.venv\Scripts\python.exe -c "import serial, numpy, scipy, matplotlib, pandas; print('ok')"
```

Autocomprobación completa (dependencias + conversiones + simulador + ajuste):

```powershell
.\.venv\Scripts\python.exe pc\selftest.py
```

Debe terminar con `Todo correcto: el entorno esta listo para calibrar.`

---

## 3. Firmware

### Subir desde Arduino IDE

1. Abre `firmware/fsr_stream/fsr_stream.ino`.
2. *Tools → Board → ESP32 Arduino → **ESP32 Dev Module***.
3. *Tools → Port* → el puerto de la placa.
4. **Upload** (⭢).
5. Abre el *Serial Monitor* a **115200** baudios. Debes ver:

```
# ch0=S3 ch1=S4 r_fixed=1667.00 vcc=3.30 topologia=fsr_to_vcc oversample=32 hz=25 fw=1.1 modelo=tabla_loglog
# ms,raw0,mv0,raw1,mv1,g0,g1
12345,1534,1234.5,1480,1190.2,0.0,0.0
12385,1536,1235.1,1479,1190.0,0.0,0.0
```

> Si ves caracteres raros, el baudrate del monitor no es 115200.

La versión 1.1 usa `calibration.h` si existe y añade `g0,g1` al final de cada
línea. Ese archivo lo genera `fit.py`, es local y no se versiona: cada usuario
tiene su propia tabla. Si aún no lo has generado, el sketch compila igual y
emite solo `raw`/`mv`. Si un canal no tiene modelo, sus gramos salen como `nan`.

### Compilar desde consola (sin abrir el IDE)

```powershell
& "<RUTA_AL_ARDUINO_CLI>\arduino-cli.exe" `
    compile --fqbn esp32:esp32:esp32 firmware\fsr_stream
```

> `arduino-cli.exe` viene incluido con Arduino IDE 2.x; sustitúyelo por la ruta
> de tu instalación o usa `arduino-cli` si lo tienes en el `PATH`.

### Qué ajustar en el sketch

| Constante | Qué es | Cuándo cambiarla |
|---|---|---|
| `VCC_VOLTS` | tensión real del pin 3V3 | **Mídela con multímetro**; si no, el modelo arrastra un error de escala |
| `R_FIXED_OHMS` | resistencia fija real | Si mides 1710 Ω en vez de 1667 Ω |
| `FSR_TO_VCC` | orientación del divisor | Solo si cambias el cableado |
| `OVERSAMPLE` | lecturas por muestra | Subir si hay mucho ruido |
| `REPORT_HZ` | líneas por segundo | 25 va sobrado |

El modelo se elige en el PC para poder iterar sin reflashear. Cuando tengas la
tabla, `fit.py` genera `calibration.h` (local, no versionado) y el firmware la
usa para añadir `g0,g1` al final de cada línea. Mientras no exista la tabla, el
ESP32 sigue emitiendo `raw` y `mv`.

---

## 4. Calibrar

### 4.1 Sin hardware, para probar el flujo

```powershell
.\.venv\Scripts\python.exe pc\capture.py --simulate --session demo `
    --channel 0 --sweep 0,50,100,200,500,1000,2000,3000
.\.venv\Scripts\python.exe pc\fit.py --csv data\raw\demo.csv
```

Con datos sintéticos el ajuste debe recuperar la ley oculta con RMSE de pocos
gramos. Si esto funciona, la cadena está bien.

### 4.2 Con la placa: primero el diagnóstico

Cierra el Monitor Serie del Arduino IDE (ocupa el puerto) y comprueba el
enlace:

```powershell
.\.venv\Scripts\python.exe pc\probe.py --port COM5
```

Debe decir `VEREDICTO: OK`. Si dice `FIRMWARE ANTIGUO`, sube el sketch. Si dice
`PUERTO OCUPADO`, cierra el monitor.

Después decide la resistencia midiendo el pico con la carga real de 5 kg:

```powershell
.\.venv\Scripts\python.exe pc\capture.py --port COM5 --range-check --target 5000
```

Si el pico pasa de 2450 mV, baja `R_fija` (por ejemplo a `1k`) y repite.

### 4.3 Captura interactiva

```powershell
.\.venv\Scripts\python.exe pc\capture.py --port COM5 --session fsr5kg_S3 --channel 0
```

Flujo en el prompt:

```
  Lectura reciente (ventana 3 s):
    S3  mv=   412.3 +/-  1.9  R=    11.7 kohm  n=75

  ENTER para capturar / comando >
```

1. Apoya el sensor en la báscula con el **disco rígido** encima y tara a 0.
2. Pulsa **ENTER** y mantén la fuerza durante `--hold` segundos.
3. Al terminar, las muestras quedan congeladas: **suelta el dedo** y escribe el
   peso observado en la báscula. Mínimo y máximo son opcionales.
4. Confirma con `[S/n/r]`: `S` guarda, `n` descarta, `r` repite la captura.

Comandos: `u` deshacer último punto · `l` listar · `q` salir. Cada sesión
guarda **un solo sensor** (`--channel 0` = S3, `--channel 1` = S4).

### 4.4 Barrido guiado (recomendado para la sesión buena)

```powershell
.\.venv\Scripts\python.exe pc\capture.py --port COM5 --session fsr5kg_S3 `
    --channel 0 `
    --sweep 0,250,500,750,1000,1500,2000,2500,3000,3500,4000,4500 `
    --reps 2 --hold 3.0 --pause 10 --settle 1.5 `
    --vcc 3.30 --r-fixed 1667,1710
```

Espera a que la señal del **canal elegido** sea estable (desviación ≤ 3 mV
durante 1 s) antes de capturar, así no dependes de tu pulso. Después congela las
muestras y te pide el peso observado. La lista termina en **4500 g** porque la
báscula se reinicia a 5 kg; el último 10 % se extrapola y se declara como
limitación.

Para el protocolo completo, la elección de `R_fija` y la interpretación de las
gráficas, ver
[`docs/procedimiento_calibracion.md`](docs/procedimiento_calibracion.md).
Todas las opciones de cada script están en
[`docs/guia_scripts.md`](docs/guia_scripts.md).

---

## 5. Ajustar y evaluar

```powershell
.\.venv\Scripts\python.exe pc\fit.py --csv data\raw\fsr_lote1.csv --show
```

Salida por consola (por cada sensor):

```
  --- S3 (N puntos, min..max g) ---
  modelo                RMSE(g)       R2   max|err|(g)  LOOCV(g)
  M1 ley potencial        <rmse>     <r2>       <max>      <loocv>   <== elegido
  M2 cuadratica log       <rmse>     <r2>       <max>      <loocv>
  M3 interpolacion        <rmse>     <r2>       <max>        --
  ecuacion: gramos = A * R^B
```

- **RMSE(g)** es el error típico en gramos sobre los puntos medidos.
- **LOOCV** es el error esperado con un peso *no usado* para ajustar; es el
  número honesto. Se elige el modelo con menor LOOCV.
- **M3** (tabla log–log) es el que va al firmware: interpola los nodos medidos
  y satura fuera de rango. Es la misma tabla que evalúa `pc/model.py`.
- **Rango declarado en el ejemplo: 250–4000 g por sensor.** Por debajo de ese
  mínimo se reporta `0 g` (saturación por diseño), no un valor extrapolado.
  Ajusta `--min-g`/`--max-g` al rango que midas en tu montaje.
- **M1** se guarda como ecuación compacta, pero en el ejemplo no baja del 3 %.

Genera:

- `data/models/<sesion>_<canal>.json` → modelo + hardware + métricas + `embedded`
- `data/models/<sesion>_<canal>.png` → log-log, residuales, mV vs gramos
- `firmware/fsr_stream/calibration.h` → tabla `(logR, logg)` por canal + `cal_grams()`

Opciones útiles:

```powershell
--drop-saturated     # descarta puntos por encima de 2.45 V y compara
--model power_law    # fuerza un modelo concreto (auto, quad_log, interp)
--vcc 3.30           # si mides otra tensión de riel
--r-fixed 1667,1710  # si los dos canales usan resistencias distintas
--only S3            # ajusta solo un sensor (S3, S4 o 0,1)
```

El CSV guarda su propio `vcc`, `r_fixed` y topología, así que `fit.py` los
recupera sin que tengas que repetirlos.

Cada sesión nueva guarda **un solo canal** y una fila por punto. El CSV incluye
`canal=`, `gramos_min`, `gramos_max` y `captura_id` para trazabilidad; `fit.py`
sigue usando `gramos` y `mv_mean`. Si abres un CSV antiguo con los dos canales,
`fit.py` omite el que esté en reposo (≤ 30 mV) y lo indica en consola; con
`--only S3` o `--only S4` fuerzas cuál quieres ajustar.

---

## 6. Verificar en vivo

```powershell
.\.venv\Scripts\python.exe pc\live.py --port COM5 --models data\models
```

Muestra `mV`, `ohmios` y `gramos` para cada sensor con el modelo cargado.
Presiona con un objeto de peso conocido y comprueba el error.

---

## 7. Interpretación de resultados

| RMSE (% del fondo de escala) | Lectura |
|---|---|
| < 1 % | Excelente para un FSR |
| 1–3 % | Bueno; suficiente para detectar presencia y estimar peso |
| 3–8 % | Aceptable solo para cambios relativos |
| > 8 % | Revisa mecánica, contacto y saturación del ADC |

Un FSR402 **no es una célula de carga**: tiene histéresis del 10–25 %,
deriva bajo carga sostenida (creep) y su respuesta depende del área de
contacto. Sirve para detectar y estimar; para pesar con precisión hace falta
una célula de carga + HX711.

---

## 8. Problemas frecuentes

| Síntoma | Causa / solución |
|---|---|
| `No encontre la placa ESP32` | Driver CH340/CP210x sin instalar, o cable solo de carga. Prueba `--port COM5` explícito. |
| Todas las lecturas a 0 mV | Divisor mal cableado o GND no común. Con esta topología, en reposo el nodo debe estar cerca de 0 V y subir al presionar. |
| Lecturas ~3300 mV siempre | FSR o R fija en corto, o el nodo está midiendo el riel. |
| mV > 2450 con carga alta | Zona no caracterizada del ADC. Ejecuta `--range-check --target 5000`; si pasa de 2450, baja `R_fija` (p. ej. `1k`) y recalibra con sesión nueva. |
| Mucho ruido (±20 mV) | Cables largos. Añade 100 nF entre el nodo y GND; sube `OVERSAMPLE`; usa `--hold 2.5`. |
| El ajuste da RMSE enorme | Mezclaste puntos con el sensor sin apoyar bien, o cambió el área de contacto. Mira el PNG de residuales: un punto desviado se ve a simple vista. |
| `ya existe con vcc=... y ahora usas ...` | El CSV es de otra configuración. Usa otra `--session`. |

---

## 9. Nota sobre el rango 0–5 kg

Con `3V3 → FSR → 10k → GND`, un FSR402 a ~1 kg ya está en ~2,5 V, el techo
fiable del ADC del ESP32: con la 10 kΩ original **5 kg recorta** (~3,15 V).

Por eso el ejemplo usa **~1,67 kΩ** (nominal **10 kΩ ∥ 2 kΩ ≈ 1667 Ω**).
Mide tu combinación real y pásala con `--r-fixed`:

| `R_fija` | V a 5 kg | Zona fiable hasta | V a 100 g |
|---|---:|---:|---:|
| 10 kΩ | 3,15 V ❌ | ~0,9 kg | 690 mV |
| 2 kΩ | 2,67 V | ~3,8 kg | 170 mV |
| **1,67 kΩ** | **2,52 V** | **~4,5 kg** | **141 mV** |
| 1 kΩ | 2,24 V ✅ | ~7 kg | 85 mV |

La tabla usa un FSR402 típico; tu unidad variará. Decide con
`--range-check --target 5000` y no con la tabla. `fit.py` te avisa con `[!]`
cuando detecta puntos por encima de 2450 mV.
