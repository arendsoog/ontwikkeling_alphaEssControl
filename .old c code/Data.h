#pragma once
#include <stdbool.h>

typedef enum
{
    NO_EARNING,
    EARNING_ON_RETURN,
    EARNING_ON_USE
} earning_t;

#define MAX_CHARGE  5
typedef enum
{
    CHARGING_ON_PV = 0,
    NO_CHARGING,
    NO_DISCHARGING,
    CHARGING_ON_GRID,
    CHARGING_DISCHARGE
} charge_t;

#define MAX_FIVE_MINS   12
typedef struct
{
    double  realSolarPowerRoof;
    double  realSolarPowerGarage;
    double  realHouseLoad;
    double  totalActivePower;
} fiveMin_t;

#define MAX_QUARTERS    4
typedef struct
{
    bool        valid;
    double      price;
    bool        highest;
    bool        lowest;
    bool        earningOnLoadHighest;
    int         cutoffSOC;
    earning_t   earning;
    charge_t    charge;
    charge_t    realCharge;
    bool        feedIn;
    int         estimatedStartSOC;
    int         realStartSOC;
    double      estimatedSolarPercentageNED;
    int         estimatedSolarPower;
    double      estimatedSolarPowerRoofCorrectionFactor;
    double      estimatedSolarPowerRoofCorrectionOffset;
    double      estimatedSolarPowerGarageCorrectionFactor;
    double      estimatedSolarPowerGarageCorrectionOffset;
    int         realSolarPowerRoof;
    int         realSolarPowerGarage;
    int         estimatedHouseLoad;
    double      estimatedHouseLoadSigma;
    int         realHouseLoad;
    int         totalActivePower;
    double      estimatedResult;
    double      realResult;
    int         fiveMinCount;
    struct
    {
        bool    valid;
        int     powerSetting;
    } providerOverride [MAX_QUARTERS];
    fiveMin_t   fiveMin [MAX_FIVE_MINS];
} hour_t;

#define MAX_HOURS   24
typedef struct
{
    bool        valid;
    int         year;
    int         mon;
    int         day;
    bool        earningOnReturnAllDay;
    int         indexHighest;
    int         indexLowest;
    int         indexCharge;
    int         indexDischarge;
    bool        chargeOnGridUsed;
    bool        dischargeUsed;
    hour_t      hour [MAX_HOURS];
} day_t;

typedef struct
{
    short   year;
    short   mon;
    short   day;
    short   hour;
    short   min;
    short   houseLoad;
    double  estimatedSolarPercentageNED;
    short   solarPowerRoof;
    short   solarPowerGarage;
    short   feedIn;
    double  price;
    double  useFee;
    double  returnFee;
} homeEnergyData_t;

#define MAX_WEEK_DAYS   7
typedef struct
{
    struct
    {
        double  houseLoad;
        double  houseLoadSigma;
        double  solarPowerRoof;
        double  solarPowerGarage;
        double  estimatedSolarPowerRoofCorrectionFactor;
        double  estimatedSolarPowerRoofCorrectionOffset;
        double  estimatedSolarPowerGarageCorrectionFactor;
        double  estimatedSolarPowerGarageCorrectionOffset;
    } hour [MAX_HOURS];
} homeEnergyWeekDayMean_t;

#define     MIN_EXPECTED_SOLAR_POWER    150.0

bool    StoreHourData (day_t theDay, int hour);
bool    RetrieveHourData (homeEnergyData_t* qData);
bool    RetrieveMeanData (int month, int wday, homeEnergyWeekDayMean_t* theData);
bool    CalculateAndStoreMeanData (void);


