#include "Adafruit_PM25AQI.h"
#include <SoftwareSerial.h>

// RX, TX
SoftwareSerial pmSerial(2, 3);

Adafruit_PM25AQI aqi = Adafruit_PM25AQI();

void setup() {

  Serial.begin(115200);
  pmSerial.begin(9600);

  while (!Serial) delay(10);

  Serial.println();
  Serial.println("========================================");
  Serial.println("        AIR QUALITY MONITOR");
  Serial.println("      PM2.5 / PARTICLE DATA");
  Serial.println("========================================");
  Serial.println();

  Serial.println("Initializing PM sensor...");

  if (!aqi.begin_UART(&pmSerial)) {
    Serial.println("ERROR: Could not find PM2.5 sensor");
    while (1);
  }

  Serial.println("PM2.5 sensor connected!");
  Serial.println();

  delay(3000); // allow fan + laser to stabilize
}

void loop() {

  PM25_AQI_Data data;

  if (!aqi.read(&data)) {
    Serial.println("WARNING: Could not read PM data");
    delay(1000);
    return;
  }

  Serial.println();
  Serial.println("========================================");
  Serial.println("           NEW SENSOR READING");
  Serial.println("========================================");

  // STANDARD CONCENTRATIONS
  Serial.println();
  Serial.println("STANDARD PARTICLE CONCENTRATION (ug/m3)");
  Serial.println("----------------------------------------");

  Serial.print("PM1.0 : ");
  Serial.println(data.pm10_standard);

  Serial.print("PM2.5 : ");
  Serial.println(data.pm25_standard);

  Serial.print("PM10  : ");
  Serial.println(data.pm100_standard);


  // ENVIRONMENTAL CONCENTRATIONS
  Serial.println();
  Serial.println("ENVIRONMENTAL PARTICLE CONCENTRATION (ug/m3)");
  Serial.println("---------------------------------------------");

  Serial.print("PM1.0 : ");
  Serial.println(data.pm10_env);

  Serial.print("PM2.5 : ");
  Serial.println(data.pm25_env);

  Serial.print("PM10  : ");
  Serial.println(data.pm100_env);


  // PARTICLE SIZE DISTRIBUTION
  Serial.println();
  Serial.println("PARTICLE COUNT (# per 0.1L air)");
  Serial.println("--------------------------------");

  Serial.print(">0.3 µm : ");
  Serial.println(data.particles_03um);

  Serial.print(">0.5 µm : ");
  Serial.println(data.particles_05um);

  Serial.print(">1.0 µm : ");
  Serial.println(data.particles_10um);

  Serial.print(">2.5 µm : ");
  Serial.println(data.particles_25um);

  Serial.print(">5.0 µm : ");
  Serial.println(data.particles_50um);

  Serial.print(">10  µm : ");
  Serial.println(data.particles_100um);


  // AQI VALUES
  Serial.println();
  Serial.println("AIR QUALITY INDEX (US EPA)");
  Serial.println("---------------------------");

  Serial.print("PM2.5 AQI : ");
  Serial.println(data.aqi_pm25_us);

  Serial.print("PM10  AQI : ");
  Serial.println(data.aqi_pm100_us);

  Serial.println();
  Serial.println("========================================");

  delay(2000);
}