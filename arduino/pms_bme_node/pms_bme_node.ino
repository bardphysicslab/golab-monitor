#include <Wire.h>
#include <Adafruit_BME280.h>
#include "Adafruit_PM25AQI.h"
#include <SoftwareSerial.h>

#define DEVICE_UID "bb-0001"
#define FW_VERSION "1.0"

#define PMS_RX 2
#define PMS_TX 3
#define BME_ADDRESS 0x76

const unsigned long SAMPLE_INTERVAL_MS = 2000;

// =====================
// Objects
// =====================
SoftwareSerial pmSerial(PMS_RX, PMS_TX);
Adafruit_PM25AQI aqi;
Adafruit_BME280 bme;

// =====================
// State
// =====================
bool running = false;
bool bmeReady = false;
bool pmsReady = false;

unsigned long lastSampleTime = 0;
unsigned long sampleCount = 0;

// Cache most recent valid PM packet
PM25_AQI_Data lastPMSData;
bool hasValidPMS = false;

// =====================
// Serial Input Buffer
// =====================
String inputLine = "";

// =====================
// Helper Functions
// =====================
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

void handleCommand(const String &cmd) {
  if (cmd == "START") {
    running = true;
    sampleCount = 0;
    lastSampleTime = millis();
    Serial.println("OK START");
    sendHeader();
  }
  else if (cmd == "STOP") {
    running = false;
    Serial.println("OK STOP");
  }
  else if (cmd == "STATUS") {
    sendStatus();
  }
  else if (cmd == "PING") {
    Serial.println("PONG");
  }
  else if (cmd == "HEADER") {
    sendHeader();
  }
  else if (cmd == "INFO") {
    sendInfo();
  }
  else if (cmd.length() == 0) {
    // Ignore empty lines
  }
  else {
    Serial.println("ERR UNKNOWN_CMD");
  }
}

void checkSerial() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();

    if (c == '\r') {
      continue;
    }

    if (c == '\n') {
      inputLine.trim();
      handleCommand(inputLine);
      inputLine = "";
    } else {
      inputLine += c;
    }
  }
}

bool readPMS(PM25_AQI_Data &data) {
  if (!pmsReady) return false;
  return aqi.read(&data);
}

bool readBME(float &tempC, float &rhPct, float &pressPa) {
  if (!bmeReady) return false;

  tempC = bme.readTemperature();
  rhPct = bme.readHumidity();
  pressPa = bme.readPressure();

  if (isnan(tempC) || isnan(rhPct) || isnan(pressPa)) {
    return false;
  }

  return true;
}

void sendData() {
  PM25_AQI_Data pmsData;
  float tempC = 0.0;
  float rhPct = 0.0;
  float pressPa = 0.0;

  bool okPMS = readPMS(pmsData);
  bool okBME = readBME(tempC, rhPct, pressPa);

  // BME failure is a true error
  if (!okBME) {
    Serial.println("ERR SENSOR_FAIL");
    return;
  }

  // Update PM cache only when a fresh packet is available
  if (okPMS) {
    lastPMSData = pmsData;
    hasValidPMS = true;
  }

  // Don't send data until first valid PM packet exists
  if (!hasValidPMS) {
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

  Serial.print(lastPMSData.pm10_standard);
  Serial.print(",");
  Serial.print(lastPMSData.pm25_standard);
  Serial.print(",");
  Serial.print(lastPMSData.pm100_standard);
  Serial.print(",");

  Serial.print(lastPMSData.pm10_env);
  Serial.print(",");
  Serial.print(lastPMSData.pm25_env);
  Serial.print(",");
  Serial.print(lastPMSData.pm100_env);
  Serial.print(",");

  Serial.print(lastPMSData.particles_03um);
  Serial.print(",");
  Serial.print(lastPMSData.particles_05um);
  Serial.print(",");
  Serial.print(lastPMSData.particles_10um);
  Serial.print(",");
  Serial.print(lastPMSData.particles_25um);
  Serial.print(",");
  Serial.print(lastPMSData.particles_50um);
  Serial.print(",");
  Serial.println(lastPMSData.particles_100um);
}

// =====================
// Setup
// =====================
void setup() {
  Serial.begin(115200);
  pmSerial.begin(9600);
/Users/lyricmateo/Documents/Arduino/serial_sensor_node_v1.ino
  pmsReady = aqi.begin_UART(&pmSerial);
  bmeReady = bme.begin(BME_ADDRESS);
}

// =====================
// Main Loop
// =====================
void loop() {
  checkSerial();

  if (!running) {
    return;
  }

  unsigned long now = millis();
  if (now - lastSampleTime >= SAMPLE_INTERVAL_MS) {
    lastSampleTime = now;
    sendData();
  }
}