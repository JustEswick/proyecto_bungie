# Procedimiento de calibración FSR402 → gramos

Este documento es el protocolo de banco. La parte eléctrica ya está resuelta
por el firmware; aquí lo que decide la calidad del resultado es la **mecánica**
y la **metodología**, no el código.

---

## 0. Qué se está midiendo y por qué importa

Un FSR402 no mide peso: mide **presión media sobre su área activa** (un pad
circular de ~12,7 mm de diámetro). Su resistencia sigue, de forma aproximada,
una ley de potencias con la fuerza aplicada:

```
R_fsr ≈ R1 · (F / F1)^(-k)        k ≈ 1 a 1,3
```

Consecuencias directas para el montaje:

1. Si cambias **quién** presiona (dedo, punta de destornillador, disco, caja),
   cambia el área de contacto y la curva entera. Un dedo reparte la carga de
   forma irregular y además varía entre mediciones.
2. Si el objeto no está **centrado**, se genera un momento y la lectura cae.
3. El FSR tiene **histéresis** (la curva de bajada no coincide con la de
   subida) y **creep** (la lectura deriva si mantienes la carga mucho tiempo).
4. Dos FSR del mismo lote pueden diferir un 20–30 %. Por eso: **un modelo por
   sensor**, nunca compartir coeficientes.

Conclusión: define un "dedo mecánico" y no lo cambies nunca durante toda la
campaña de medidas.

---

## 1. Materiales

- ESP32 DevKit con el firmware `fsr_stream`.
- 2 × FSR402.
- Por cada sensor, una resistencia fija de **1667 Ω**. La forma barata de
  conseguirla es **10 kΩ en paralelo con 2 kΩ** (`10k ∥ 2k = 1666,7 Ω`).
  Mide la combinación con el multímetro si quieres precisión; dos resistencias
  del 5 % en paralelo suelen quedar dentro del 1–2 %.
- Báscula de cocina de 5 kg (resolución 1 g).
- **Disco rígido** de ~12–13 mm de diámetro: arandela gruesa, ficha de dominó
  recortada, taco de madera, pieza impresa en 3D. Debe ser plano y no
  deformarse. Que pese poco.
- Opcional: 2 × condensador de 100 nF (nodo → GND) si hay ruido.
- Multímetro (para medir el riel de 3V3 y las resistencias).
- Pesas conocidas si las tienes (mejor que apretar a mano).

---

## 2. Preparación eléctrica (10 min, una sola vez)

1. **Mide el 3V3 real** con el multímetro, con la placa alimentada por USB y
   el firmware corriendo. Anótalo. Suele dar 3,25–3,35 V. Ponlo en
   `VCC_VOLTS` del sketch **y** en `--vcc` de `capture.py`. Un error del 2 %
   aquí es un error del 2 % en `R` y por tanto en el modelo.
2. **Mide las resistencias fijas**. Para cada sensor, mide la combinación
   `10k ∥ 2k` ya soldada (debería dar ~1,67 kΩ). Usa ese valor real: si mides
   1710 Ω, pasa `--r-fixed 1710`.
   - Si solo tienes una resistencia de 2 kΩ, puedes usar **2 kΩ sola** en un
     sensor. Funciona, pero el techo fiable del ADC baja de 4,5 kg a ~3,8 kg.
   - **Nunca pongas resistencias en serie** para "subir": `10k + 2k = 12k`
     empeora el rango. La dirección correcta es siempre bajar `R_fija`.
3. Comprueba el reposo: en el Monitor Serie, sin tocar el sensor, `mv` debe
   estar cerca de 0 (esta topología). Si marca cientos de mV en reposo, hay
   una vía de corriente parásita o el divisor está invertido.
4. Comprueba el enlace y el ruido. Primero el diagnóstico (no escribe nada):

   ```powershell
   .\.venv\Scripts\python.exe pc\probe.py --port COM5
   ```

   Debe decir `VEREDICTO: OK`. Si dice `FIRMWARE ANTIGUO`, sube el sketch. Si
   dice `PUERTO OCUPADO`, cierra el Monitor Serie del Arduino IDE.

   Después mira el ruido en vivo:

   ```powershell
   .\.venv\Scripts\python.exe pc\capture.py --port COM5 --session ruido --hold 2.5
   ```

   Mira `+/-` en la línea de lectura sin presionar. Criterio: **< 3 mV**. Si
   es mayor, cables más cortos, 100 nF en el nodo y/o sube `OVERSAMPLE` a 64.
5. Deja la placa encendida **2 minutos** antes de empezar (el regulador y el
   ADC se estabilizan).

---

## 3. Comprobar el rango antes de calibrar

### 3.1 El problema del ADC

El ADC del ESP32 con atenuación de 11 dB **no es lineal en todo su rango**.
Espressif solo caracteriza de fábrica la zona aproximada de **150–2450 mV**;
por debajo de 150 mV la resolución efectiva se degrada y por encima de
2450 mV la lectura sigue subiendo pero pierde linealidad. El techo físico está
en ~3,1 V, donde el ADC recorta.

Por eso el objetivo del divisor es que **5 kg caigan por debajo de 2,45 V**.
Con `VCC = 3,3 V`:

```
V_nodo = 3,3 · R_fija / (R_fija + R_fsr)
```

### 3.2 Por qué 1667 Ω y no 10 kΩ

Con la `10 kΩ` original, un FSR402 típico llega a 2,45 V con solo ~750–1000 g:
todo lo que pase de ahí se mide mal. Con `10k ∥ 2k = 1666,7 Ω` el mismo sensor
reparte mejor el rango:

| Carga (g) | R_fsr típica | V_nodo con 1667 Ω | ¿Zona fiable? |
|---:|---:|---:|:--|
| 0 | abierto | 0,00 V | sí |
| 100 | ~38 kΩ | 0,14 V | sí (justo en el límite bajo) |
| 300 | ~11 kΩ | 0,42 V | sí |
| 500 | ~6,4 kΩ | 0,68 V | sí |
| 750 | ~4,1 kΩ | 0,95 V | sí |
| 1000 | ~3,0 kΩ | 1,18 V | sí |
| 2000 | ~1,4 kΩ | 1,79 V | sí |
| 3000 | ~0,9 kΩ | 2,15 V | sí |
| 4000 | ~0,65 kΩ | 2,37 V | sí |
| **5000** | **~0,51 kΩ** | **2,53 V** | **al límite** |

Los valores de `R_fsr` son orientativos (ley aproximada
`R ≈ 3000 · (g/1000)^(-1,1)`); **tu unidad variará**. Lo que importa es la
tensión, y esa se mide.

### 3.3 Comprobación real con `--range-check`

No confíes en la tabla: mide tu sensor. Pon la carga objetivo (5 kg) y ejecuta:

```powershell
.\.venv\Scripts\python.exe pc\capture.py --port COM5 --range-check --target 5000
```

Durante 10 s muestra el pico de cada canal y recomienda un valor de `R_fija`:

| Pico medido | Veredicto | Acción |
|---:|---|---|
| < 2450 mV | OK | calibra así |
| 2450–2900 mV | aceptable, pierdes precisión arriba | calibra asumiendo más error en el extremo alto |
| > 2900 mV | demasiado alto | baja `R_fija` (p. ej. `2k ∥ 2k = 1k`) y repite |

El script calcula `R_fija ≈ 2,3 · R_fsr(5 kg)` como valor recomendado. Si tu
unidad pide 1 kΩ en vez de 1,67 kΩ, necesitas dos resistencias de 2 kΩ en
paralelo por sensor. **Cada sensor necesita su propia resistencia.**

### 3.4 Compromiso

Cuanto menor es `R_fija`, más rango pero menos resolución en la zona baja:

| `R_fija` | 5 kg | Zona buena hasta | 100 g |
|---|---:|---:|---:|
| 10 kΩ | 3,15 V ❌ | ~0,9 kg | 690 mV |
| 2 kΩ | 2,67 V | ~3,8 kg | 170 mV |
| **1,67 kΩ** | **2,53 V** | **~4,5 kg** | **140 mV** |
| 1 kΩ | 2,24 V ✅ | ~7 kg | 85 mV |

Para 0–5 kg, **1,67 kΩ es el mejor compromiso con una sola 2 kΩ por sensor**.
Si tu `--range-check` sale por encima de 2,45 V, pasa a 1 kΩ.

---

## 4. Montaje mecánico (lo que más afecta al resultado)

1. Pega o fija el FSR sobre una superficie **plana y rígida** (no sobre la
   plataforma flexible de la báscula directamente si se comba).
2. Apoya esa base en el centro de la báscula.
3. **Tara la báscula con el disco encima.** Así el peso del disco no cuenta.
4. Coloca el disco siempre en el **centro** del pad activo.
5. La carga debe ser **vertical**: si empujas en ángulo, el disco desliza y
   aparece fricción lateral.
6. No dobles el FSR: el sustrato es frágil y la curvatura altera la lectura.
7. Marca la posición con cinta o rotulador. En la segunda sesión debes poder
   repetir la mecánica exacta.

---

## 5. Protocolo de medida

### 5.1 Puntos

Para un rango de 0–5 kg con `R_fija = 1667 Ω`:

```
0, 100, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 4000, 5000 g
```

Los primeros 100 g caen muy abajo (~140 mV), así que no añadas puntos por
debajo salvo que te interese esa zona. El punto de 5000 g está cerca del techo
fiable: si `--range-check` lo deja por encima de 2450 mV, marca los puntos
altos como "informativos" o pasa a 1 kΩ.

- **0 g** se captura en reposo (sin tocar). No entra en el ajuste logarítmico,
  pero es tu referencia de offset y sirve para verificar que no hay señal
  parásita.
- Espaciado aproximadamente logarítmico: más puntos donde la curva cambia
  rápido (carga baja).
- No uses solo 2–3 puntos aunque tengas prisa: con 3 puntos el ajuste es
  básicamente una interpolación y no sabrás si el modelo falla en medio.

### 5.2 Repeticiones y estabilidad

- **2–3 repeticiones por peso**, separadas por descarga completa. Sirven para
  estimar la repetibilidad real.
- Mantén la carga **2–3 segundos** antes de capturar. El FSR tiene un
  asentamiento inicial rápido; capturar en el primer instante da lecturas
  altas.
- Captura solo cuando la báscula esté quieta. El script avisa si `mv_std`
  es alto.

### 5.3 Barrido guiado (recomendado)

```powershell
.\.venv\Scripts\python.exe pc\capture.py --port COM5 --session fsr5kg `
    --sweep 0,100,200,300,500,750,1000,1500,2000,3000,4000,5000 `
    --reps 2 --hold 2.0 --r-fixed 1667
```

El script espera a que la desviación baje de 3 mV en 1 s. Si no lo consigue en
15 s, captura igual y te avisa: ese punto probablemente sea basura, revisa su
`mv_std` antes de aceptarlo.

### 5.4 Histéresis (opcional pero muy informativo)

Repite el barrido **de bajada** en una sesión distinta:

```powershell
--session fsr5kg_bajada --sweep 5000,4000,3000,2000,1500,1000,750,500,300,200,100
```

Ajusta las dos sesiones por separado y compara. La diferencia entre ambas
curvas es tu histéresis. Si es > 15 %, ningún modelo la va a arreglar: tendrás
que decidir en qué dirección vas a usar el sensor, o filtrar por velocidad de
cambio.

---

## 6. Registro

El CSV (`data/raw/<sesion>.csv`) guarda por punto:

| Columna | Para qué |
|---|---|
| `gramos` | referencia de la báscula |
| `mv_mean`, `mv_std` | lectura promediada y su dispersión (control de calidad) |
| `raw_mean`, `raw_std` | cuentas del ADC, por si quieres rehacer la conversión |
| `n` | muestras promediadas (ventana × 25 Hz) |
| `timestamp` | trazabilidad |

Las cabeceras `# vcc=`, `# r_fixed=` y `# topologia=` quedan grabadas en el
propio archivo: el ajuste usa esos valores, no los de por defecto. Así un CSV
antiguo sigue siendo reproducible. `r_fixed` puede ser un valor por canal
(`1667/10000`): cada fila se convierte con el de su sensor.

Anota aparte (cuaderno): fecha, temperatura ambiente, si cambiaste el disco,
y cualquier golpe o doblez del sensor durante la sesión.

---

## 7. Análisis

La referencia completa de cada opción está en
[`docs/guia_scripts.md`](guia_scripts.md). El comando básico:

```powershell
.\.venv\Scripts\python.exe pc\fit.py --csv data\raw\fsr5kg.csv --show
```

Qué mirar, en este orden:

1. **La gráfica log-log (panel 1).** Los puntos deben seguir una recta. Si ves
   un codo, hay dos regímenes y conviene M2 o M3. Si un punto se sale, ese
   punto fue mal medido: bórralo y vuelve a ajustar.
2. **Residuales (panel 2).** Deben estar repartidos alrededor de 0 sin
   tendencia. Una tendencia en U significa que el modelo es demasiado simple;
   un punto aislado muy alto, basura experimental.
3. **RMSE y LOOCV.** El LOOCV es el número que debes citar: mide el error con
   un peso que el modelo no vio.
4. **La gráfica mV vs gramos (panel 3).** Si los puntos altos se pegan a la
   línea vertical naranja de 2450 mV, confirma saturación: vuelve al paso 3.

Criterios de aceptación sugeridos:

| Métrica | Objetivo |
|---|---|
| `mv_std` por punto | < 5 mV |
| RMSE (LOOCV) | < 3 % del fondo de escala |
| Repetibilidad entre reps | < 2 % del fondo |
| Histéresis | anotarla; no se corrige |
| Puntos saturados | 0 (o excluidos explícitamente) |

---

## 8. Modelos

| Nombre | Forma | Ventajas | Límites |
|---|---|---|---|
| M1 ley potencial | `g = A·R^B` | 2 parámetros, va al firmware, extrapola con sentido físico | Rígido si la curva tiene codo |
| M2 cuadrática log | `log g = c0 + c1·logR + c2·logR²` | Absorbe curvatura | 3 parámetros, puede explotar fuera de rango |
| M3 interpolación | PCHIP monótona | La más exacta dentro de lo medido | No extrapola; necesita tabla en el firmware |

Recomendación: usa **M3** para validar que la toma de datos es buena (si M3
tampoco ajusta, el problema es experimental), y **M1** para producción si su
LOOCV está dentro del criterio. `fit.py` ya genera `calibration.h` con M1.

---

## 9. Errores típicos que arruinan la calibración

1. **Cambiar el disco o la posición** a mitad de campaña → dos curvas mezcladas
   en un CSV.
2. **Capturar antes de que la báscula se estabilice** → lecturas altas.
3. **Presionar con el dedo** en vez de con el disco → área variable.
4. **No medir el 3V3 real** → error de escala sistemático.
5. **Ignorar la saturación del ADC** → el modelo aprende una curva aplastada
   en el extremo alto y luego subestima pesos grandes.
6. **Usar un solo punto de referencia** → no es una calibración, es un factor
   de escala.
7. **Extrapolar fuera del rango medido** → un FSR es monótono, pero el error
   crece rápido fuera de la zona caracterizada.

---

## 10. Guion completo de una sesión (0 → 5 kg)

Resumen de extremo a extremo, con los comandos en orden:

```powershell
# 1. Entorno
.\.venv\Scripts\python.exe pc\selftest.py

# 2. Flashear firmware/fsr_stream/fsr_stream.ino en el Arduino IDE (115200)
# 3. Comprobar que el enlace responde
.\.venv\Scripts\python.exe pc\probe.py --port COM5

# 4. Decidir la resistencia con la carga real de 5 kg
.\.venv\Scripts\python.exe pc\capture.py --port COM5 --range-check --target 5000

# 5. Capturar el barrido (2 repeticiones por peso)
.\.venv\Scripts\python.exe pc\capture.py --port COM5 --session fsr5kg `
    --sweep 0,100,200,300,500,750,1000,1500,2000,3000,4000,5000 `
    --reps 2 --hold 2.0 --r-fixed 1667

# 6. Ajustar y evaluar
.\.venv\Scripts\python.exe pc\fit.py --csv data\raw\fsr5kg.csv --show

# 7. Validar con un peso conocido
.\.venv\Scripts\python.exe pc\live.py --port COM5 --models data\models
```

Si un sensor lleva una resistencia distinta (por ejemplo, solo tienes una 2 kΩ
y el otro sigue con 10 kΩ), pasa los dos valores:

```powershell
--r-fixed 1667,10000
```

El CSV guarda ese `1667/10000`, y `fit.py` lo recupera solo: no tienes que
repetir la opción al ajustar.

---

## 11. Siguiente paso en tu proyecto

Con `data/models/*.json` ya tienes `A` y `B` por sensor. Dos caminos:

- **En el PC** (lo que hace `live.py`): lees el puerto, conviertes y decides.
- **En el ESP32**: incluye `firmware/fsr_stream/calibration.h` y añade

  ```cpp
  float ohms = CAL_R_FIXED_OHMS[i] * (CAL_VCC_VOLTS - v) / v;
  float gramos = CAL_A[i] * powf(ohms, CAL_B[i]);
  ```

  Ten en cuenta que `powf` y el logaritmo cuestan, pero a 25 Hz sobra de todo.

Si el proyecto final necesita pesar de verdad (no solo detectar), el sustituto
correcto es una célula de carga + HX711: resolución de gramos, linealidad real
y sin histéresis apreciable. El FSR es excelente para *presencia*, *contacto* y
*cambios relativos*.
