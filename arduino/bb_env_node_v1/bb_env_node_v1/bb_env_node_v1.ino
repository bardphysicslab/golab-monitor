#include <Wire.h>
#include <Adafruit_BME280.h>
#include "Adafruit_PM25AQI.h"

#define DEVICE_UID "bb-0003"
#define FW_VERSION "1.3"

static const unsigned long SAMPLE_INTERVAL_MS = 2000;

Adafruit_BME280 bme;
Adafruit_PM25AQI pms;

bool running = false;
bool bmeReady = false;
bool pmsReady = false;
bool pmsHasData = false;

unsigned long lastSampleTime = 0;
unsigned long sampleCount = 0;

PM25_AQI_Data lastPmsData = {};

char inputLine[64];
size_t inputPos = 0;

bool initBME280() {
  if (bme.begin(0x77, &Wire)) return true;
  if (bme.begin(0x76, &Wire)) return true;
  return false;
}

bool initPMSA003I() {
  return pms.begin_I2C(&Wire);
}

void sendHeader() {
  Serial.println(
    "HDR,v1,sample_idx,temp_c,rh_pct,press_pa,"
    "pm1_std,pm25_std,pm10_std,"
    "pm1_env,pm25_env,pm10_env,"
    "c03,c05,c10,c25,c50,c100"
  );
}

void sendInfo() {
  Serial.print("OK INFO uid=");
  Serial.print(DEVICE_UID);
  Serial.print(" fw=");
  Serial.print(FW_VERSION);
  Serial.println(" sensors=PMS,BME280");
}

void sendStatus() {
  if (running) {
    Serial.println("OK STATUS RUNNING");
  } else {
    Serial.println("OK STATUS STOPPED");
  }
}

bool readBME(float &tempC, float &rhPct, float &pressPa) {
  if (!bmeReady) return false;

  tempC = bme.readTemperature();
  rhPct = bme.readHumidity();
  pressPa = bme.readPressure();

  if (isnan(tempC) || isnan(rhPct) || isnan(pressPa)) return false;
  return true;
}

void pollPMS() {
  if (!pmsReady) return;

  PM25_AQI_Data data;
  if (pms.read(&data)) {
    lastPmsData = data;
    pmsHasData = true;
  }
}

void sendData() {
  float tempC = 0.0f;
  float rhPct = 0.0f;
  float pressPa = 0.0f;

  if (!readBME(tempC, rhPct, pressPa)) {
    return;
  }

  if (!pmsHasData) {
    return;
  }

  sampleCount++;

  Serial.print("DAT,");
  Serial.print(sampleCount);
  Serial.print(",");

  Serial.print(tempC, 2);
  Serial.print(",");
  Serial.print(rhPct, 2);
  Serial.print(",");
  Serial.print(pressPa, 0);
  Serial.print(",");

  Serial.print(lastPmsData.pm10_standard);
  Serial.print(",");
  Serial.print(lastPmsData.pm25_standard);
  Serial.print(",");
  Serial.print(lastPmsData.pm100_standard);
  Serial.print(",");

  Serial.print(lastPmsData.pm10_env);
  Serial.print(",");
  Serial.print(lastPmsData.pm25_env);
  Serial.print(",");
  Serial.print(lastPmsData.pm100_env);
  Serial.print(",");

  Serial.print(lastPmsData.particles_03um);
  Serial.print(",");
  Serial.print(lastPmsData.particles_05um);
  Serial.print(",");
  Serial.print(lastPmsData.particles_10um);
  Serial.print(",");
  Serial.print(lastPmsData.particles_25um);
  Serial.print(",");
  Serial.print(lastPmsData.particles_50um);
  Serial.print(",");
  Serial.println(lastPmsData.particles_100um);
}

void handleCommand(const char *cmd) {
  if (strcmp(cmd, "START") == 0) {
    running = true;
    sampleCount = 0;
    lastSampleTime = millis();
    Serial.println("OK START");
    sendHeader();
    return;
  }

  if (strcmp(cmd, "STOP") == 0) {
    running = false;
    Serial.println("OK STOP");
    return;
  }

  if (strcmp(cmd, "STATUS") == 0) {
    sendStatus();
    return;
  }

  if (strcmp(cmd, "PING") == 0) {
    Serial.println("PONG");
    return;
  }

  if (strcmp(cmd, "HEADER") == 0) {
    sendHeader();
    return;
  }

  if (strcmp(cmd, "INFO") == 0) {
    sendInfo();
    return;
  }

  if (cmd[0] == '\0') return;

  Serial.println("ERR UNKNOWN_CMD");
}

void checkSerial() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();

    if (c == '\r') continue;

    if (c == '\n') {
      inputLine[inputPos] = '\0';
      handleCommand(inputLine);
      inputPos = 0;
      continue;
    }

    if (inputPos < sizeof(inputLine) - 1) {
      inputLine[inputPos++] = c;
    } else {
      inputPos = 0;
    }
  }
}

void setup() {
  Serial.begin(115200);
  while (!Serial) {
    delay(10);
  }

  Wire.begin();

  bmeReady = initBME280();
  pmsReady = initPMSA003I();
}

void loop() {
  checkSerial();
  pollPMS();

  if (!running) return;

  unsigned long now = millis();
  if (now - lastSampleTime >= SAMPLE_INTERVAL_MS) {
    lastSampleTime = now;
    sendData();
  }
}