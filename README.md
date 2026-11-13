# Calibración de sensores FSR402 con ESP32

Sistema para caracterizar dos FSR contra una báscula de cocina (5 kg) y obtener
un modelo que convierta la lectura del ADC en **gramos**, con métricas de error
reales.

Pensado para el montaje:

```
        3V3 ──[ FSR402 ]──┬──[ R 1,63 kΩ ]── GND
                          │
                          ├── GPIO35   (S3)
                          └── GPIO34   (S4)
```

`R 1,63 kΩ` se consigue con **10 kΩ en paralelo con 2 kΩ** (nominal). En este
montaje se midieron **1633 Ω (S3)** y **1641 Ω (S4)**, con un valor medio de
**1637 Ω**. Al presionar, la resistencia del FSR baja y la tensión del nodo sube:
`R_fsr = 1637 · (VCC − V_nodo) / V_nodo`.

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
# ch0=S3 ch1=S4 r_fixed=1637.00 vcc=3.27 topologia=fsr_to_vcc oversample=32 hz=25 fw=1.0
# ms,raw0,mv0,raw1,mv1
12345,1534,1234.5,1480,1190.2
12385,1536,1235.1,1479,1190.0
```

> Si ves caracteres raros, el baudrate del monitor no es 115200.

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
| `R_FIXED_OHMS` | resistencia fija real | Si mides 1710 Ω en vez de 1637 Ω |
| `FSR_TO_VCC` | orientación del divisor | Solo si cambias el cableado |
| `OVERSAMPLE` | lecturas por muestra | Subir si hay mucho ruido |
| `REPORT_HZ` | líneas por segundo | 25 va sobrado |

El firmware **no** calcula gramos a propósito: calibrar en el PC evita
reflashear en cada prueba. Cuando tengas el modelo, `fit.py` genera
`calibration.h` con los coeficientes por si quieres que el ESP32 entregue
gramos.

---

## 4. Calibrar

### 4.1 Sin hardware, para probar el flujo

```powershell
.\.venv\Scripts\python.exe pc\capture.py --simulate --session demo `
    --sweep 0,50,100,200,500,1000,2000,3000
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
.\.venv\Scripts\python.exe pc\capture.py --port COM5 --session fsr5kg
```

Flujo en el prompt:

```
  Lectura en vivo (ventana 3 s):
    S3  mv=   412.3 +/-  1.9  R=    11.7 kohm  n=75
    S4  mv=   398.0 +/-  2.2  R=    12.2 kohm  n=75
  gramos en la bascula >
```

1. Apoya el sensor en la báscula con el **disco rígido** encima y tara a 0.
2. Presiona hasta que la báscula marque un peso estable.
3. Escribe el número y pulsa ENTER **mientras sigues presionando**: el programa
   promedia la ventana anterior al ENTER y te pide confirmación `[S/n/r]`.
4. ENTER en vacío solo **refresca** la lectura, no guarda nada.

Comandos: `u` deshacer último punto · `l` listar · `q` salir.

### 4.4 Barrido guiado (recomendado para la sesión buena)

```powershell
.\.venv\Scripts\python.exe pc\capture.py --port COM5 --session fsr5kg `
    --sweep 0,100,200,300,500,750,1000,1500,2000,3000,4000,5000 `
    --reps 2 --hold 2.0 --vcc 3.27 --r-fixed 1633,1641
```

Espera a que la señal sea estable (desviación ≤ 3 mV durante 1 s) antes de
capturar, así no dependes de tu pulso. Para el protocolo completo, la elección
de `R_fija` y la interpretación de las gráficas, ver
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
  --- S3 (11 puntos, 50..4000 g) ---
  modelo                RMSE(g)       R2   max|err|(g)  LOOCV(g)
  M1 ley potencial         14.2  0.99873         31.0       17.9   <== elegido
  M2 cuadratica log        11.8  0.99912         26.4       21.3
  M3 interpolacion          9.9  0.99938         22.1        -- 
  ecuacion: gramos = 3.2451e+07 * R^(-2.1873)
```

- **RMSE(g)** es el error típico en gramos sobre los puntos medidos.
- **LOOCV** es el error esperado con un peso *no usado* para ajustar; es el
  número honesto. Se elige el modelo con menor LOOCV.
- **M3** suele dar el mejor ajuste dentro del rango medido, pero no extrapola.
- **M1** es el que se puede meter en el firmware (2 coeficientes).

Genera:

- `data/models/<sesion>_<canal>.json` → modelo + hardware + métricas
- `data/models/<sesion>_<canal>.png` → log-log, residuales, mV vs gramos
- `firmware/fsr_stream/calibration.h` → `CAL_A[]`, `CAL_B[]`

Opciones útiles:

```powershell
--drop-saturated     # descarta puntos por encima de 2.45 V y compara
--model power_law    # fuerza el modelo embebible
--vcc 3.27           # si mides otra tensión de riel
--r-fixed 1633,1641  # si los dos canales usan resistencias distintas
--only S3            # ajusta solo un sensor (S3, S4 o 0,1)
```

El CSV guarda su propio `vcc`, `r_fixed` y topología, así que `fit.py` los
recupera sin que tengas que repetirlos.

Si calibras **un sensor por sesión**, el CSV seguirá teniendo una fila por
canal (el otro sensor aparecerá en reposo). `fit.py` omite automáticamente los
canales sin señal (≤ 30 mV) y lo indica en consola; con `--only S3` o
`--only S4` fuerzas cuál quieres ajustar.

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

Por eso el montaje por defecto usa **~1637 Ω** (nominal **10 kΩ ∥ 2 kΩ**; en
este montaje se midieron 1633 Ω y 1641 Ω):

| `R_fija` | V a 5 kg | Zona fiable hasta | V a 100 g |
|---|---:|---:|---:|
| 10 kΩ | 3,15 V ❌ | ~0,9 kg | 690 mV |
| 2 kΩ | 2,67 V | ~3,8 kg | 170 mV |
| **1,63 kΩ** | **2,50 V** | **~4,5 kg** | **140 mV** |
| 1 kΩ | 2,24 V ✅ | ~7 kg | 85 mV |

La tabla usa un FSR402 típico; tu unidad variará. Decide con
`--range-check --target 5000` y no con la tabla. `fit.py` te avisa con `[!]`
cuando detecta puntos por encima de 2450 mV.
