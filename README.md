# Calibración de sensores FSR402 con ESP32

Sistema para caracterizar dos FSR contra una báscula de cocina (5 kg) y obtener
un modelo que convierta la lectura del ADC en **gramos**, con métricas de error
reales.

Pensado para el montaje:

```
        3V3 ──[ FSR402 ]──┬──[ R 10k ]── GND
                          │
                          ├── GPIO35   (S3)
                          └── GPIO34   (S4)
```

Al presionar, la resistencia del FSR baja y la tensión del nodo sube:
`R_fsr = 10k · (VCC − V_nodo) / V_nodo`.

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
│  ├─ capture.py            toma de puntos (gramos + mV promediados)
│  ├─ fit.py                ajuste de modelos, métricas y gráficas
│  ├─ model.py              modelos en JSON y evaluación
│  ├─ live.py               monitor en vivo en gramos
│  └─ selftest.py           autocomprobación del entorno (sin hardware)
├─ data/raw/                CSV de cada sesión de calibración
├─ data/models/             JSON + PNG de diagnóstico
└─ docs/procedimiento_calibracion.md
```

El `.venv` ya está creado con `--system-site-packages`: ve `numpy`, `scipy`,
`matplotlib` y `pandas` de tu Python global, y solo tiene instalado `pyserial`
de forma local (nada global).

---

## 2. Entorno

Todo listo. Para usar los scripts, activa el entorno en la terminal:

```powershell
cd "C:\Users\Isaac\Documents\Sistemas embebidos\proyecto\proyecto_bungie"
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
# ch0=S3 ch1=S4 r_fixed=10000.00 vcc=3.30 topologia=fsr_to_vcc oversample=32 hz=25 fw=1.0
# ms,raw0,mv0,raw1,mv1
12345,1534,1234.5,1480,1190.2
12385,1536,1235.1,1479,1190.0
```

> Si ves caracteres raros, el baudrate del monitor no es 115200.

### Compilar desde consola (sin abrir el IDE)

```powershell
& "E:\Arduino\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe" `
    compile --fqbn esp32:esp32:esp32 firmware\fsr_stream
```

### Qué ajustar en el sketch

| Constante | Qué es | Cuándo cambiarla |
|---|---|---|
| `VCC_VOLTS` | tensión real del pin 3V3 | **Mídela con multímetro**; si no, el modelo arrastra un error de escala |
| `R_FIXED_OHMS` | resistencia fija real | Si mides 9.8k en vez de 10k |
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

### 4.2 Con la placa

```powershell
.\.venv\Scripts\python.exe pc\capture.py --port COM5 --session fsr_lote1
```

Flujo en el prompt:

```
  lectura actual (ventana 1.5 s):
    S3  mv=  412.3 +/-  1.9  R=    23.9 kohm  n=37
    S4  mv=  398.0 +/-  2.2  R=    25.1 kohm  n=37
  gramos > 1000
```

1. Apoya el sensor en la báscula con el **disco rígido** encima y tara a 0.
2. Presiona hasta que la báscula marque un peso estable.
3. Escribe el número y pulsa ENTER **mientras sigues presionando**: el programa
   promedia la ventana de 1.5 s anterior al ENTER.
4. ENTER en vacío solo **refresca** la lectura, no guarda nada.

Comandos: `u` deshacer último punto · `l` listar · `q` salir.

### 4.3 Barrido guiado (recomendado para la sesión buena)

```powershell
.\.venv\Scripts\python.exe pc\capture.py --port COM5 --session fsr_lote1 `
    --sweep 0,50,100,200,300,500,750,1000,1500,2000,3000,4000 --reps 2
```

Espera a que la señal sea estable (desviación ≤ 3 mV durante 1 s) antes de
capturar, así no dependes de tu pulso. Ver
`docs/procedimiento_calibracion.md` para el protocolo completo.

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
--vcc 3.28           # si mides otra tensión de riel
```

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
| mV > 2450 con carga alta | Zona no caracterizada del ADC. Calibra solo hasta ahí, o baja `R_FIXED` a 4.7k y recalibra. |
| Mucho ruido (±20 mV) | Cables largos. Añade 100 nF entre el nodo y GND; sube `OVERSAMPLE`; usa `--hold 2.5`. |
| El ajuste da RMSE enorme | Mezclaste puntos con el sensor sin apoyar bien, o cambió el área de contacto. Mira el PNG de residuales: un punto desviado se ve a simple vista. |
| `ya existe con vcc=... y ahora usas ...` | El CSV es de otra configuración. Usa otra `--session`. |

---

## 9. Nota sobre el rango

Con `3V3 → FSR → 10k → GND`, un FSR402 a ~1 kg baja a ~3 kΩ y el nodo ya está
en ~2.5 V, el techo fiable del ADC del ESP32. Para cargas mayores la lectura se
comprime y el modelo pierde precisión arriba. Si tu rango de interés es
0–2 kg, el montaje actual está bien; si necesitas 0–5 kg lineales, cambia la
resistencia fija a 4.7 kΩ y vuelve a calibrar. `fit.py` te avisa con `[!]`
cuando detecta puntos en esa zona.
