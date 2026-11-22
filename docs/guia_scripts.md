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
firmware/fsr_stream.ino          ESP32: adquiere y envía CSV
        │  ms,raw0,mv0,raw1,mv1[,g0,g1]  (25 Hz)
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

Regla mental: **el modelo se elige en el PC y luego se copia al firmware**.
`fit.py` genera `calibration.h` con una tabla log–log por canal; el ESP32 la usa
para añadir `g0,g1` al final de cada línea. Así puedes iterar el modelo (y las
capturas) sin reflashear, y al final el ESP32 entrega gramos por sí solo.

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
  Cabecera : # ch0=S3 ch1=S4 r_fixed=1667.00 vcc=3.30 ...
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

Es el script que más vas a usar. La captura funciona en este orden:

1. Pulsas ENTER y se captura un intervalo de `--hold` segundos.
2. Al terminar, las muestras quedan **congeladas**: ya puedes soltar el dedo.
3. Escribes **un solo peso observado/predominante** en la báscula.
4. Opcionalmente anotas el mínimo y el máximo vistos. Si los dejas vacíos,
   quedan iguales al peso observado y son solo anotación.

Ese peso observado es el valor que va a `gramos` y el que usa `fit.py`. **No se
calcula un punto medio ni una media inventada**: la referencia es la que tú
observaste.

Tiene tres modos:

1. **Interactivo** (por defecto): ENTER inicia cada intervalo y después anotas
   la referencia.
2. **Barrido** (`--sweep`): recorre una lista, espera estabilidad y captura el
   intervalo. También pide la referencia manual.
3. **Comprobación de rango** (`--range-check`): mide el pico y recomienda
   `R_fija`. No escribe CSV.

**Un solo sensor por sesión:** `--channel 0` es S3 y `--channel 1` es S4. Si no
lo pasas, el script lo pregunta al inicio. El CSV guarda solo ese canal y sus
metadatos (`# canal=`, `# canales=`). Si intentas reutilizar la misma sesión con
otro canal o con otra configuración, se niega para no mezclar curvas.

Antes de crear el CSV hace un **preflight**: si el firmware es antiguo o el
puerto está ocupado, aborta sin dejar un archivo vacío.

**Uso básico:**

```powershell
python pc\capture.py --port COM4 --session fsr5kg_S3 --channel 0
python pc\capture.py --port COM4 --session fsr5kg_S3 --channel 0 --sweep 0,500,1000,2000,4500
python pc\capture.py --port COM4 --range-check --target 5000
python pc\capture.py --simulate --session prueba --channel 0
```

**Opciones:**

| Opción | Defecto | Para qué |
|---|---|---|
| `--channel` | pregunta | sensor de la sesión: `0` = S3, `1` = S4 |
| `--port` | autodetecta | puerto serie |
| `--baud` | `115200` | baudrate |
| `--session` | `sesion_AAAAMMDD_HHMMSS` | nombre del CSV en `data/raw/` |
| `--hold` | `3.0` | duración del intervalo de captura, en segundos (0,5–300) |
| `--vcc` | `3.30` | tensión real del riel |
| `--r-fixed` | `1667.0` | resistencia fija; admite `1667,1710` por canal |
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
  Lectura reciente (ventana 3 s):
    S3  mv=   412.3 +/-  1.9  R=    10.0 kohm  n=75

  ENTER para capturar / comando >
```

| Entrada | Efecto |
|---|---|
| ENTER vacío | inicia el intervalo y **congela** las muestras |
| `u` | deshace el último punto completo |
| `l` | lista los puntos guardados |
| `q` | termina y guarda |
| `?` | ayuda |

Al terminar el intervalo aparece:

```
  FIN: muestras congeladas. Ya puedes soltar el dedo.
  Peso observado/predominante (g): 1000
  Peso MINIMO observado (g) [ENTER = mismo]:
  Peso MAXIMO observado (g) [ENTER = mismo]:
```

Después muestra el resultado y pide confirmación `[S/n/r]`: `S` guarda, `n`
descarta y `r` repite la captura.

En hardware real no escribas el peso antes de capturar: primero se mide y
después se anota. En `--simulate` puedes escribir un objetivo en gramos para
elegir el peso simulado; sin número usa 1000 g.

### 5.2 Modo barrido

```powershell
python pc\capture.py --port COM4 --session fsr5kg_S3 --channel 0 `
    --sweep 0,100,200,300,500,750,1000,1500,2000,3000,4000,4500 `
    --reps 2 --hold 3.0 --pause 10 --settle 1.5 `
    --vcc 3.30 --r-fixed 1667,1710
```

Antes de cada punto muestra una cuenta atrás de `--pause` segundos (10 por
defecto): es el tiempo para **quitar la carga anterior y aplicar la siguiente**.
Puedes terminar la espera antes pulsando ENTER. Con `--pause 0` el barrido
avanza sin pausa (útil solo con pesos automáticos).

Después espera a que la desviación del **canal elegido** baje de `--stab-mv`
durante `--stab-window`. Si no lo consigue en `--stab-timeout`, captura igual y
avisa. **Revisa el `mv_std` de ese punto antes de fiarte.**

Tras congelar las muestras, el barrido también pide la referencia manual y
confirma antes de guardar, salvo que uses `--yes` o `--simulate`.

La lista recomendada termina en **4500 g**, no en 5000 g: la báscula se
reinicia justo a 5 kg y el último 10 % se extrapola. Es una limitación
declarada, no un punto inventado.

### 5.3 Comprobación de rango

```powershell
python pc\capture.py --port COM4 --range-check --target 5000
```

Muestra el pico de cada canal durante `--range-seconds` y recomienda
`R_fija ≈ 2,3 · R_fsr(5 kg)`:

```
  canal  mV pico   R a esa carga   R_fija recomendada
     S3   2300.0        700 ohm              1.61 kohm
          -> OK: por debajo de 2450 mV
          -> para 5000 g en ~2.3 V usa R_fija ~= 1.61 kohm (maximo 2.01 kohm)
          -> tu R_fija actual (1667 ohm) es adecuada
```

No crea CSV. Es el paso que decide si necesitas `1k` en vez de `1,67k`.

### 5.4 El CSV que genera

`data/raw/<sesion>.csv`. Cabeceras:

```
# calibracion FSR - puntos de referencia
# vcc=3.30
# r_fixed=1667/1710
# topologia=fsr_to_vcc
# canal=0
# canales=S3
# creado=2026-11-11T18:22:01
timestamp,session,ch,nombre,gramos,mv_mean,mv_std,raw_mean,raw_std,n,gramos_min,gramos_max,gramos_predominante,referencia,captura_id,duracion_s,muestras_tramo_s
```

| Columna | Significado |
|---|---|
| `timestamp` | inicio del intervalo capturado |
| `session` | nombre de la sesión |
| `ch`, `nombre` | índice y nombre del canal (`S3`, `S4`) |
| `gramos` | peso observado/predominante; es el valor que usa `fit.py` |
| `mv_mean`, `mv_std` | tensión promediada y su dispersión |
| `raw_mean`, `raw_std` | cuentas del ADC (por si quieres rehacer la conversión) |
| `n` | muestras del intervalo (≈ `--hold` × 25 Hz) |
| `gramos_min`, `gramos_max` | rango observado; anotación opcional |
| `gramos_predominante` | copia del peso observado (trazabilidad) |
| `referencia` | `manual_estable` o `simulado` |
| `captura_id` | identificador único del punto; lo usa `undo` |
| `duracion_s` | duración configurada del intervalo |
| `muestras_tramo_s` | tiempo real cubierto por las muestras congeladas |

`gramos_min` y `gramos_max` **no se usan para ajustar**. Sirven para auditar la
variabilidad del pulso. `fit.py` lee `gramos` y `mv_mean`; no calcula medias ni
puntos medios a partir de ellos.

Si reutilizas una sesión con otro `vcc`, `r_fixed`, topología o canal, el script
se niega: usa otra `--session` para no mezclar configuraciones. Un CSV de una
versión anterior también se rechaza al capturar; `fit.py` sí puede leerlo para
auditar sesiones viejas.

**Una fila por punto y canal:** como ahora se guarda un solo canal por sesión,
cada peso genera **una fila**. `fit.py` no necesita `--only` para esa sesión,
aunque puedes pasarlo para dejar clara tu intención.

---

## 6. `fit.py` — ajuste y evaluación

**Qué hace:** convierte mV → ohmios con la `R_fija` de cada canal, ajusta tres
modelos por sensor y los compara con validación cruzada (LOOCV).

**Uso:**

```powershell
python pc\fit.py --csv data\raw\fsr5kg.csv
python pc\fit.py --csv data\raw\fsr5kg.csv --show
python pc\fit.py --csv data\raw\fsr5kg.csv --vcc 3.30 --r-fixed 1667,1710

# Un canal por CSV, una sola cabecera con los dos:
python pc\fit.py --csv data\raw\ejemplo_S3.csv `
                 --csv data\raw\ejemplo_S4.csv --max-g 4000
```

Repite `--csv` para juntar varios canales en la misma `calibration.h`. Si un
canal aparece en dos CSV, se queda el último y avisa.

**Opciones:**

| Opción | Defecto | Para qué |
|---|---|---|
| `--csv` | obligatorio | CSV de `capture.py`; **repetible**, uno por canal |
| `--out-dir` | `data/models` | dónde guardar JSON/PNG |
| `--vcc` | del CSV | sobrescribe la tensión |
| `--r-fixed` | del CSV | sobrescribe la resistencia; admite `1667,1710` |
| `--topology` | del CSV | sobrescribe el divisor |
| `--model` | `auto` | fuerza `power_law`, `quad_log` o `interp` |
| `--drop-saturated` | no | descarta puntos > 2450 mV |
| `--min-g` | 0 | ignora puntos con menos peso (recorta el ajuste) |
| `--max-g` | sin límite | ignora puntos con más peso, p. ej. `--max-g 4000` |
| `--only` | todos con señal | ajusta solo `S3`, `S4` o `0,1` |
| `--show` | no | abre las gráficas |
| `--no-header` | no | no genera `calibration.h` |

El recorte por rango (`--min-g` / `--max-g`) es la forma correcta de excluir un
punto dudoso: **no toca el CSV**, solo decide qué puntos entran en el ajuste.
Ojo: el rango es global a todos los `--csv`; como el modelo se cierra donde la
curva deja de ser fiable, se aplica a los dos canales a la vez.

**Canales sin señal:** en sesiones nuevas ya se guarda un solo canal. Si abres
un CSV antiguo que contiene los dos, `fit.py` detecta el que quedó en reposo
(~0–1 mV), lo omite y avisa:

```text
  [ ] S4: sin senal (max 1.1 mV < 30 mV); se omite del ajuste.
```

No genera un modelo basura para ese canal. Para forzar uno concreto:
`--only S3` o `--only S4`.

**Salida por canal:**

```
  --- S3 (N puntos, min..max g) ---
  modelo                RMSE(g)       R2   max|err|(g)  LOOCV(g)
  M1 ley potencial        <rmse>     <r2>       <max>      <loocv>
  M2 cuadratica log       <rmse>     <r2>       <max>      <loocv>   <== elegido
  M3 interpolacion        <rmse>     <r2>       <max>        --
  ecuacion: log10(g) = c0 + c1*log10(R) + c2*log10(R)^2
  firmware: tabla log-log de N nodos (LOOCV <loocv> g)
  modelo  -> data/models/ejemplo_S3_S3.json
  grafica -> data/models/ejemplo_S3_S3.png
```

**Qué genera:**

- `<sesion>_<canal>.json` → modelo elegido + hardware + métricas + `embedded`.
- `<sesion>_<canal>.png` → log-log, residuales y mV vs gramos.
- `firmware/fsr_stream/calibration.h` → una tabla `(log10 R, log10 g)` por
  canal, `CAL_R_FIXED_OHMS[]` y la función `cal_grams()`. Es local y está en
  `.gitignore`: no se sube al repositorio.

**Cómo elegir el modelo:** `fit.py` usa el **menor LOOCV** para el JSON y las
gráficas. Para el firmware guarda siempre la **tabla de interpolación lineal en
log–log** en `embedded`: es la misma que evalúa `pc/model.py`, así que PC y
ESP32 coinciden (verificado a < 0,02 g). Si un canal no tiene tabla (menos de 3
puntos), `cal_grams()` devuelve `NAN` para ese canal.

---

## 7. `live.py` — monitor en gramos

**Qué hace:** carga uno o varios JSON y muestra `mV`, `ohmios` y `gramos` en
vivo. Sirve para validar con un peso conocido.

Usa el campo `embedded` (la **tabla log–log**) y satura igual que el firmware:
en reposo muestra `0 g`, no un valor extrapolado. La columna final indica
`tabla` cuando usa la tabla, o el nombre del modelo si no hay tabla.

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
| `R_FIXED_OHMS` | `1667.0` | si tu resistencia real difiere |
| `VCC_VOLTS` | `3.30` | **mídela** con multímetro |
| `FSR_TO_VCC` | `True` | si inviertes el divisor |
| `ADC_TRUST_V` | `2.45` | techo fiable del ADC |
| `ADC_MAX_V` | `3.10` | techo físico |
| `ADC_MIN_V` | `0.03` | umbral histórico de "sin carga" (para diagnóstico) |
| `ADC_FLOOR_V` | `0.15` | piso típico: por debajo el ADC no distingue |
| `BAUDRATE` | `115200` | debe coincidir con el firmware |

También define `CHANNELS = ("S3", "S4")`, el orden exacto del firmware.

---

## 9. El firmware: constantes y formato

Archivo `firmware/fsr_stream/fsr_stream.ino`.

| Constante | Defecto | Para qué |
|---|---|---|
| `R_FIXED_OHMS` | `1667.0f` | informativo; la calibración real está en `calibration.h` |
| `VCC_VOLTS` | `3.30f` | informativo |
| `FSR_TO_VCC` | `true` | orientación del divisor |
| `OVERSAMPLE` | `32` | lecturas ADC por muestra; súbelo si hay ruido |
| `REPORT_HZ` | `25` | líneas por segundo |
| `EMA_ALPHA` | `0.30f` | suavizado de la tensión |
| `HEADER_EVERY_S` | `30` | reimprime la cabecera cada 30 s |
| `EMIT_GRAMS` | auto | añade `g0,g1` si existe `calibration.h`; `0` fuerza el CSV 1.0 |

Incluye `calibration.h` si existe. Ese archivo lo genera `fit.py`, es local y no
se versiona: contiene la `R_fija` y el `VCC` reales de cada canal. Los valores de
arriba son solo la información que el firmware escribe en la cabecera.

**Formato de línea:**

```
# ch0=S3 ch1=S4 r_fixed=1667.00 vcc=3.30 topologia=fsr_to_vcc oversample=32 hz=25 fw=1.1 modelo=tabla_loglog
# ms,raw0,mv0,raw1,mv1,g0,g1
12345,1534,1234.5,1480,1190.2,0.0,0.0
```

Los gramos van **al final** para que los parsers antiguos sigan aceptando la
línea (leen las primeras `1 + 2·N` columnas e ignoran el resto). Fuera del rango
calibrado la función satura: `0` por debajo del nodo de menos peso y el peso
máximo por encima. Un canal sin modelo imprime `nan`.

**Rango útil del ejemplo: 250–4000 g por sensor.** Por debajo de 250 g la tabla
no tiene nodos y se reporta `0 g` (no es un fallo ni un sensor roto: es
saturación por diseño). Por encima de 4000 g se satura en el peso máximo.
Ajusta `--min-g`/`--max-g` al rango que midas en tu montaje.

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
python pc\capture.py --port COM4 --session fsr5kg_S3 --channel 0 `
    --sweep 0,250,500,750,1000,1500,2000,2500,3000,3500,4000,4500 `
    --reps 2 --hold 3.0 --pause 10 --settle 1.5 `
    --vcc 3.30 --r-fixed 1667,1710
python pc\fit.py --csv data\raw\fsr5kg_S3.csv --only S3
python pc\live.py --port COM4 --models data\models
```

Al terminar cada intervalo sueltas el dedo y escribes el peso observado; mínimo
y máximo son opcionales. Repite con `--session fsr5kg_S4 --channel 1` y
`fit.py ... --only S4` para el otro sensor.

Valida con un peso conocido, no con los mismos puntos del ajuste.

### 10.3 Dos sensores en la misma campaña

No mezcles los dos sensores en una sola sesión. Aunque compartan carga, cada
FSR tiene su propia curva y su propio montaje: usa una sesión por sensor.

```powershell
python pc\capture.py --port COM4 --session campana_S3 --channel 0 ...
python pc\capture.py --port COM4 --session campana_S4 --channel 1 ...
```

El CSV de cada sesión guarda `canal=0` o `canal=1` y su `r_fixed` por canal. Si
intentas reutilizar una sesión con otro canal, el script se niega para no
mezclar curvas.

`fit.py` sigue leyendo `r_fixed=1667/1710` y convierte cada fila con la
resistencia de su canal. Para generar **una sola** `calibration.h` con los dos:

```powershell
python pc\fit.py --csv data\raw\campana_S3.csv --csv data\raw\campana_S4.csv --max-g 4000
```

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

// mv = tension del nodo en milivoltios (la que ya emite el firmware)
float gramos = cal_grams(i, mv);   // NAN si el canal no esta calibrado
```

`cal_grams()` convierte mV → ohmios con la `R_fija` de **ese** canal `i`,
interpola linealmente en log–log y devuelve gramos. Es exactamente el mismo
cálculo que `pc/model.py`, así que no hay que recalibrar la lógica del PC.

---

## 11. Referencia de formatos

### 11.1 CSV de captura

Ver [5.4](#54-el-csv-que-genera). La unidad de `gramos` es el gramo, `mv_*` va
en milivoltios, `raw_*` en cuentas del ADC (0–4095) y `n` es un número de
muestras.

### 11.2 JSON del modelo

```json
{
  "session": "ejemplo_S3",
  "ch": 0,
  "nombre": "S3",
  "model": "quad_log",
  "params": {"c2": 1.0, "c1": -8.0, "c0": 18.0},
  "hardware": {"vcc": 3.30, "r_fixed": 1667.0, "topologia": "fsr_to_vcc"},
  "metrics": {"rmse_g": 12.4, "r2": 0.9991,
              "max_abs_g": 31.0, "loocv_rmse_g": 15.2},
  "embedded": {"model": "interp",
               "params": {"log_r": [2.90, 2.95, "..."],
                          "log_g": [3.60, 3.54, "..."]},
               "loocv_rmse_g": 15.2},
  "n_points": 10,
  "range_g": [250.0, 4000.0],
  "range_ohms": [1500.0, 650.0],
  "created": "2026-01-01T00:00:00"
}
```

- `model`/`params` → el mejor modelo (JSON y gráficas).
- `embedded` → **tabla log–log** que va al firmware. Puede ser `null` si no hay
  puntos suficientes.
- `hardware.r_fixed` es la resistencia de **ese** canal.

### 11.3 `calibration.h`

```cpp
#pragma once
#include <math.h>
#include <stdint.h>

#define CAL_N_CH 2
static const float CAL_R_FIXED_OHMS[CAL_N_CH] = { 1667.0f, 1710.0f };
static const float CAL_VCC_VOLTS    = 3.300f;
static const bool  CAL_FSR_TO_VCC   = true;

static const uint8_t CAL_NODES[CAL_N_CH] = { 10, 11 };
static const float CAL_LOG_R_0[] = { 2.934371f, /* ... */ 3.714237f };
static const float CAL_LOG_G_0[] = { 3.602060f, /* ... */ 2.403121f };
/* ... un par de arrays por canal ... */

static inline float cal_grams(uint8_t ch, float mv);
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
--channel 0            sensor de la sesión (0=S3, 1=S4)
--port COM4            puerto
--baud 115200          baudrate
--session NOMBRE       nombre del CSV
--hold 3.0             duración del intervalo de captura (s)
--vcc 3.30             tensión del riel
--r-fixed 1667,1710    o un único valor
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
--vcc 3.30
--r-fixed 1667,1710    o un único valor
--topology fsr_to_vcc
--model auto           power_law | quad_log | interp
--drop-saturated
--min-g 0
--max-g 4000
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
| `Muy pocas muestras NUEVAS` | `--hold` muy corto o enlace caído | sube `--hold` o revisa el cable |
| `Frecuencia efectiva muy baja` | llegan muestras con huecos (< 10 Hz) | revisa el enlace y el Monitor Serie |
| `Faltaron datos durante el intervalo` | se perdió parte de la ventana | repite la captura |
| `CSV de otra version` | sesión creada con una versión anterior | usa otra `--session` |
| `--r-fixed tiene N valores pero hay 2 canales` | lista mal formada | pasa 1 o 2 valores: `1667` o `1667,1710` |
| `ya existe con canal=... y ahora usas ...` | sesión reutilizada con otro sensor | usa otra `--session` |
| `el CSV no tiene datos` | captura interrumpida antes de guardar | repite la captura |
| `solo N punto(s) util(es); necesito al menos 2` | demasiados puntos a 0 g | añade puntos con peso |
| `no encontre modelos en data\models` | aún no ejecutaste `fit.py` | ajusta primero |
| RMSE enorme | mecánica inconsistente o saturación | mira el PNG de residuales y `--drop-saturated` |

Para el detalle físico (por qué la mecánica manda, cómo elegir `R_fija`, cómo
leer las gráficas) sigue en
[`docs/procedimiento_calibracion.md`](procedimiento_calibracion.md).
