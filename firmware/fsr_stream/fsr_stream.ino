/* ============================================================================
   fsr_stream.ino  -  Adquisicion de 2 sensores FSR402 en ESP32 DevKit
   ----------------------------------------------------------------------------
   Proyecto: calibracion de FSR contra bascula (gramos).

   CONEXIONES (topologia "FSR arriba"):
       3V3 --- [ FSR ] ---+--- [ R_FIXED ] --- GND
                          |
                        GPIO (ADC)

     R_FIXED = 1666.7 ohm (10k en paralelo con 2k) para llegar a 5 kg sin
     que el ADC recorte. Si usas otro valor, cambia R_FIXED_OHMS mas abajo.

     Al presionar, R_FSR baja  ->  V_nodo SUBE.
     R_FSR = R_FIXED * (VCC - V_nodo) / V_nodo

   CANALES (igual que el sketch de prueba original):
       S3 -> GPIO35   (ADC1_CH7)
       S4 -> GPIO34   (ADC1_CH6)

   POR QUE ESTOS PINES:
     GPIO32..GPIO39 son ADC1. ADC1 no comparte recursos con el radio WiFi,
     asi que la lectura no se corrompe aunque el WiFi este activo.
     GPIO34..GPIO39 son solo-entrada: perfecto para ADC (no hacen falta
     resistencias pull-up ni salidas digitales).

   SALIDA (115200 baudios, CSV plano, 25 Hz):
       # ch0=S3 ch1=S4 r_fixed=1666.70 vcc=3.30 topologia=fsr_to_vcc oversample=32 hz=25 fw=1.0
       # ms,raw0,mv0,raw1,mv1
       12345,1534,1234.5,1480,1190.2
       ...

     Las lineas que empiezan con '#' son metadatos/comentarios: el programa
     del PC las ignora. Si ves el CSV en el Monitor Serie, pon el baudrate
     correcto (115200) o veras caracteres basura.

   POR QUE EL FIRMWARE NO CALCULA GRAMOS:
     Calibrar exige probar decenas de combinaciones de modelo. Hacerlo en el
     PC evita reflashear en cada iteracion. Cuando el modelo este elegido,
     pc/fit.py genera calibration.h con los coeficientes para el ESP32.
   ========================================================================== */

// ---------------------------------------------------------------------------
// 1. CONFIGURACION  (lo unico que deberias tocar)
// ---------------------------------------------------------------------------

#define FW_VERSION "1.0"

// Cuantos sensores y en que pines (orden = orden de las columnas del CSV)
static const uint8_t N_CH = 2;
static const uint8_t PIN_ADC[N_CH] = { 35, 34 };          // S3, S4
static const char*   CH_NAME[N_CH] = { "S3", "S4" };

// Resistencia fija del divisor, en ohmios. Mide la tuya con el multimetro
// si quieres precision. 1666.7 = 10k || 2k: 5 kg cae en ~2.57 V, dentro de
// la zona fiable del ADC. Solo es informativo: la conversion real la hace
// el PC con --r-fixed.
static const float R_FIXED_OHMS = 1666.7f;

// Voltaje real del riel de 3V3, medido con multimetro.
// OJO: no pongas 3.30 "porque si". Mide el pin 3V3 de la placa con el USB
// conectado; tipicamente da 3.25..3.35 V y de ahi depende el calculo de R.
static const float VCC_VOLTS = 3.30f;

// Topologia del divisor:
//   true  -> 3V3 -> FSR -> nodo -> R_FIXED -> GND     (V sube al presionar)
//   false -> 3V3 -> R_FIXED -> nodo -> FSR -> GND     (V baja al presionar)
static const bool FSR_TO_VCC = true;

// Promedio movil exponencial aplicado a la tension (0..1).
// Mas pequeno = mas suave pero mas lento. 0.30 ~ 4-5 muestras de constante.
static const float EMA_ALPHA = 0.30f;

// Muestreo
static const uint16_t OVERSAMPLE = 32;   // lecturas ADC promediadas por muestra
static const uint8_t  REPORT_HZ  = 25;   // lineas por segundo enviadas al PC

// Reimprimir la cabecera cada N segundos (util si reconectas el monitor
// sin resetear la placa). 0 = solo al arrancar.
static const uint16_t HEADER_EVERY_S = 30;

// ---------------------------------------------------------------------------
// 2. ESTADO INTERNO
// ---------------------------------------------------------------------------

static uint32_t next_report_us = 0;
static uint32_t last_header_ms = 0;
static float    ema_mv[N_CH];
static bool     ema_ready = false;

// ---------------------------------------------------------------------------
// 3. UTILIDADES
// ---------------------------------------------------------------------------

static void print_header() {
  Serial.print(F("# "));
  for (uint8_t i = 0; i < N_CH; i++) {
    Serial.print(F("ch"));
    Serial.print(i);
    Serial.print('=');
    Serial.print(CH_NAME[i]);
    Serial.print(' ');
  }
  Serial.print(F("r_fixed="));
  Serial.print(R_FIXED_OHMS, 2);
  Serial.print(F(" vcc="));
  Serial.print(VCC_VOLTS, 2);
  Serial.print(F(" topologia="));
  Serial.print(FSR_TO_VCC ? F("fsr_to_vcc") : F("fsr_to_gnd"));
  Serial.print(F(" oversample="));
  Serial.print(OVERSAMPLE);
  Serial.print(F(" hz="));
  Serial.print(REPORT_HZ);
  Serial.print(F(" fw="));
  Serial.println(F(FW_VERSION));

  Serial.print(F("# ms"));
  for (uint8_t i = 0; i < N_CH; i++) {
    Serial.print(F(",raw"));
    Serial.print(i);
    Serial.print(F(",mv"));
    Serial.print(i);
  }
  Serial.println();
}

// Lee una rafaga de OVERSAMPLE muestras y devuelve:
//   - el promedio crudo del ADC (counts, 0..4095)
//   - el promedio en milivoltios YA CALIBRADO por la fabrica del chip
//
// analogReadMilliVolts() usa la curva de calibracion que Espressif graba en
// la eFuse de cada ESP32. Es bastante mejor que hacer raw*3300/4095, sobre
// todo cerca de los extremos del rango.

static void read_burst(uint8_t pin, float& raw_avg, float& mv_avg) {
  uint32_t raw_sum = 0;
  uint32_t mv_sum  = 0;

  // Descartar la primera conversion: tras cambiar de canal el sample&hold
  // del ADC necesita asentarse.
  (void)analogRead(pin);
  (void)analogReadMilliVolts(pin);

  for (uint16_t i = 0; i < OVERSAMPLE; i++) {
    raw_sum += (uint32_t)analogRead(pin);
    mv_sum  += analogReadMilliVolts(pin);
  }

  raw_avg = (float)raw_sum / (float)OVERSAMPLE;
  mv_avg  = (float)mv_sum / (float)OVERSAMPLE;
}

// ---------------------------------------------------------------------------
// 4. SETUP
// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  // En placas con USB-CDC nativo esto evita perder las primeras lineas.
  delay(300);

  analogReadResolution(12);           // 0..4095
  for (uint8_t i = 0; i < N_CH; i++) {
    pinMode(PIN_ADC[i], INPUT);       // GPIO34/35 ya son solo-entrada
    // Atenuacion 11 dB: rango nominal 0..3.1 V (zona fiable ~0.15..2.45 V).
    analogSetPinAttenuation(PIN_ADC[i], ADC_11db);
  }

  print_header();

  Serial.print(F("# listo: "));
  Serial.print(N_CH);
  Serial.println(F(" canales"));
}

// ---------------------------------------------------------------------------
// 5. LOOP
// ---------------------------------------------------------------------------

void loop() {
  // Planificacion no bloqueante: el periodo se mantiene aunque el bucle
  // tarde algo mas en una iteracion (delay() acumularia error).
  const uint32_t period_us = 1000000UL / REPORT_HZ;
  uint32_t now_us = micros();
  if ((int32_t)(now_us - next_report_us) < 0) {
    return;
  }
  next_report_us = now_us + period_us;

  float raw[N_CH];
  float mv[N_CH];

  for (uint8_t i = 0; i < N_CH; i++) {
    read_burst(PIN_ADC[i], raw[i], mv[i]);

    if (!ema_ready) {
      ema_mv[i] = mv[i];
    } else {
      ema_mv[i] = EMA_ALPHA * mv[i] + (1.0f - EMA_ALPHA) * ema_mv[i];
    }
  }
  ema_ready = true;

  // Cabecera periodica (opcional)
  if (HEADER_EVERY_S > 0) {
    uint32_t now_ms = millis();
    if (now_ms - last_header_ms >= (uint32_t)HEADER_EVERY_S * 1000UL) {
      last_header_ms = now_ms;
      print_header();
      return;
    }
  }

  // ---- linea CSV ----
  Serial.print(millis());
  for (uint8_t i = 0; i < N_CH; i++) {
    Serial.print(',');
    Serial.print((int)(raw[i] + 0.5f));
    Serial.print(',');
    Serial.print(ema_mv[i], 1);   // 1 decimal ~ 0.1 mV
  }
  Serial.println();
}
