//
// Author: Jaco Melse (jjmelse@xs4all.nl)
// 2025 - 2026
//

#pragma once

#include <stdlib.h>
#include <stdbool.h>
#include "Data.h"
#include "Util.h"

//#define PROVIDER_USE_FEE        0.01815         // per kWh
//#define PROVIDER_RETURN_FEE     0.01815         // per kWh, same as per 1-8-2025

//#define BATTERY_CAPACITY        11520.0 * 0.95  // in W

#define SOC_MAX                 900
#define SOC_MAX_CHARGE_PROVIDER 200
#define SOC_MAX_BATTERY         1000
#define SOC_MAX_CHARGE_ON_GRID  SOC_MAX_BATTERY
#define SOC_MIN                 104
#define SOC_MIN_DISCHARGE_AFTER 200

#define MAX_NETWORK_NAME        50
#define MAX_API_TOKEN           70

typedef struct
{
    struct
    {
        int     debugLevel;
        char    AlphaESSNetworkName [MAX_NETWORK_NAME];
        int     AlphaESSPVPower;
        int     AlphaESSUsableBatteryCapacity;
        int     AlphaESSInverterNominalPower;
        double  ProviderUseFee;
        double  ProviderReturnFee;
        int     VAT;
        char    ShellyPMNetworkName [MAX_NETWORK_NAME];
        bool    ShellyPMActive;
        int     ShellyPMPVPower;
        char    HomeWizardP1NetworkName [MAX_NETWORK_NAME];
        bool    HomeWizardP1Active;
        char    EntsoeAPIToken [MAX_API_TOKEN];
        char    NEDAPIToken [MAX_API_TOKEN];
        char    SolcastAPIToken [MAX_API_TOKEN];
    } general;

    struct
    {
        double  profitMin;
        bool    allowProviderControlHour [MAX_HOURS];
    } daily;

} config_t;

config_t    GetConfig (void);

// Prices.c
double      MkReturnPrice (double price);
double      MkUsePrice (double price);
bool        ReadPrices (day_t* today);
bool        CalculatePredictedImbalancePrice ();

// Schedule.c
void        LogExecutionResult (int hour, hour_t theHour);
void        SetChargingMsg (char* msg, charge_t charge);
bool        SetSchedule (int curSOC, int curHour, day_t* today, day_t *tomorrow);
void        ShowSchedule (day_t theDay, int startHour, int curHour, bool hourOnly, char* startMsg);

// Solar.c
bool        ReadEstimatedSolarPower (day_t* today, int curHour);
bool        SetGaragePV (bool PVOn);
short       GetGaragePVPower (void);
short       GetActivePowerP1 (void);