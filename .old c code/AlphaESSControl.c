//
// Author: Jaco Melse (jjmelse@xs4all.nl)
// 2025 - 2026
//

#include <stdio.h>
#include <stdlib.h>
#include <stdbool.h>
#include <signal.h>
#include <string.h>
#include <math.h>
#include <errno.h>
#include <time.h>
#include <glib.h>
#include <curl/curl.h>
#include <cjson/cJSON.h>
#include <modbus/modbus.h>
#include "Data.h"
#include "Util.h"
#include "AlphaESS.h"
#include "AlphaESSControl.h"

#define SOFTWARE_VERSION    "V1.1.2"

static  void    InitHours (day_t* today)
{
    for (int i = 0; i < MAX_HOURS; i ++)
    {
        hour_t* theHour = &today->hour [i];
        theHour->valid = false;
        theHour->price = 0.0;
        theHour->highest = false;
        theHour->lowest = false;
        theHour->earningOnLoadHighest = false;
        theHour->cutoffSOC = SOC_MAX;
        theHour->earning = NO_EARNING;
        theHour->charge = NO_CHARGING;
        theHour->realCharge = NO_CHARGING;
        theHour->feedIn = false;
        theHour->estimatedStartSOC = 0;
        theHour->realStartSOC = 0;
        theHour->estimatedSolarPercentageNED = 0;
        theHour->estimatedSolarPower = 0;
        theHour->estimatedSolarPowerRoofCorrectionFactor = -1.0;
        theHour->estimatedSolarPowerRoofCorrectionOffset = 0.0;
        theHour->estimatedSolarPowerGarageCorrectionFactor = -1.0;
        theHour->estimatedSolarPowerGarageCorrectionOffset = 0.0;
        theHour->realSolarPowerRoof = 0;
        theHour->realSolarPowerGarage = 0;
        theHour->estimatedHouseLoad = 0;
        theHour->realHouseLoad = 0;
        theHour->totalActivePower = 0;
        theHour->estimatedResult = 0.0;
        theHour->realResult = 0.0;
        theHour->fiveMinCount = 0;
        for (int j = 0; j < MAX_QUARTERS; j ++)
        {
            theHour->providerOverride [0].valid = false;
            theHour->providerOverride [0].powerSetting = 0;
        }
        for (int j = 0; j < MAX_FIVE_MINS; j ++)
        {
            theHour->fiveMin [j].realSolarPowerRoof = 0.0;
            theHour->fiveMin [j].realSolarPowerGarage = 0.0;
            theHour->fiveMin [j].realHouseLoad = 0.0;
            theHour->fiveMin [j].totalActivePower = 0.0;
        }
    }
}

static  void    InitDay (int year, int mon, int day, day_t* today)
{
    today->day = day;
    today->mon = mon;
    today->year = year;
    today->valid = false;
    today->earningOnReturnAllDay = false;
    today->indexHighest = -1;
    today->indexLowest = -1;
    today->indexCharge = -1;
    today->indexDischarge = -1;
    today->chargeOnGridUsed = false;
    today->dischargeUsed = false;
    InitHours (today);
}

static  config_t    theConfig;

config_t    GetConfig (void)
{
    return (theConfig);
}

#define MIN_SIGMA_WH    200     // Wh
static  bool    ReadEstimates (day_t* today)
{
    homeEnergyWeekDayMean_t mData;

    // set weekday
    time_t  t_of_day;
    struct  tm thetm;

    thetm.tm_year = today->year - 1900;
    thetm.tm_mon = today->mon - 1;
    thetm.tm_mday = today->day;
    thetm.tm_hour = 5;  // avoid daylight saving time hassle    
    thetm.tm_min = 0;
    thetm.tm_sec = 0;
    thetm.tm_isdst = -1;                // Is DST on? 1 = yes, 0 = no, -1 = unknown
    t_of_day = mktime (&thetm);
    thetm = *localtime (&t_of_day);

    // set defaults in case of no database history
    for (int hour = 0; hour < MAX_HOURS; hour ++)
    {
        today->hour [hour].estimatedHouseLoad = MIN_SIGMA_WH;
        today->hour [hour].estimatedSolarPowerRoofCorrectionFactor = -1.0;
        today->hour [hour].estimatedSolarPowerGarageCorrectionFactor = -1.0;
    }

    if (RetrieveMeanData (today->mon, thetm.tm_wday, &mData))   // no mean data available is no error
    {
        for (int hour = 0; hour < MAX_HOURS; hour++)
        {
            today->hour [hour].estimatedHouseLoad = mData.hour [hour].houseLoad;
            // Apply minimum uncertainty to avoid overconfidence with sparse data
            if (today->hour [hour].estimatedHouseLoad < MIN_SIGMA_WH)
            {
                today->hour [hour].estimatedHouseLoad = MIN_SIGMA_WH;
            }

            today->hour [hour].estimatedSolarPowerRoofCorrectionFactor = mData.hour [hour].estimatedSolarPowerRoofCorrectionFactor;
            today->hour [hour].estimatedSolarPowerRoofCorrectionOffset = mData.hour [hour].estimatedSolarPowerRoofCorrectionOffset;
            today->hour [hour].estimatedSolarPowerGarageCorrectionFactor = mData.hour [hour].estimatedSolarPowerGarageCorrectionFactor;
            today->hour [hour].estimatedSolarPowerGarageCorrectionOffset = mData.hour [hour].estimatedSolarPowerGarageCorrectionOffset;
        }
    }

    return (true);
}

static  bool    ReadPricesAndEstimates (day_t *today, int curHour)
{
    if (!ReadEstimates (today)) // must be called before solar
    {
        return (false);
    }

    if (!ReadPrices (today))
    {
        return (false);
    }
    
    if (!ReadEstimatedSolarPower (today, curHour))
    {
        return (false);
    }

    today->valid = true;    // in case later on, after a valid cyle, price or solar power read fails, today remains valid

    return (true);
}

static  GKeyFile*   ReadConfigFile (void)
{
    static  GKeyFile *kf;
    GError *error = NULL;

    kf = g_key_file_new ();
    if (!g_key_file_load_from_file (kf, "../config/config.ini", G_KEY_FILE_NONE, &error))
    {
        Log ("Error reading config: %s\n", error->message);
        g_error_free (error);
        g_key_file_free (kf);
        return (NULL);
    }

    return (kf);
}

static  void    ReadConfigInit (void)
{
    GKeyFile *kf;
    GError *error = NULL;

    if ((kf = ReadConfigFile ()) == NULL)
    {
        exit (1);
    }

    // set defaults
    theConfig.general.debugLevel = 0;
    theConfig.daily.profitMin = 0.5;
    for (int i = 0; i < MAX_HOURS; i ++)
    {
        theConfig.daily.allowProviderControlHour [i] = true;
    }

    error = NULL;
    theConfig.general.debugLevel = g_key_file_get_integer (kf, "General", "debugLevel", &error);
    if (error != NULL)
    {
        Log ("Error reading config: %s\n", error->message);
        exit (1);
    }

    char* p;
    error = NULL;
    p = g_key_file_get_string (kf, "General", "AlphaESSInverter", &error);
    if (error != NULL)
    {
        Log ("Error reading config: %s\n", error->message);
        exit (1);
    }
    snprintf (theConfig.general.AlphaESSNetworkName, sizeof (theConfig.general.AlphaESSNetworkName), "%s", p);
    g_free (p);
    if (strlen (theConfig.general.AlphaESSNetworkName) == 0)
    {
        Log ("Config error: AlphaESSNetworkName missing\n");
        exit (1);
    }

    error = NULL;
    theConfig.general.AlphaESSPVPower = g_key_file_get_integer(kf, "General", "AlphaESSPVPower", &error);
    if (error != NULL)
    {
        Log ("Error reading config: %s\n", error->message);
        exit (1);
    }

    error = NULL;
    theConfig.general.AlphaESSUsableBatteryCapacity = g_key_file_get_integer(kf, "General", "AlphaESSUsableBatteryCapacity", &error);
    if (error != NULL)
    {
        Log ("Error reading config: %s\n", error->message);
        exit (1);
    }
    
    error = NULL;
    theConfig.general.AlphaESSInverterNominalPower = g_key_file_get_integer (kf, "General", "AlphaESSInverterNominalPower", &error);
    if (error != NULL)
    {
        Log ("Error reading config: %s\n", error->message);
        exit (1);
    }

    error = NULL;
    theConfig.general.ProviderUseFee = g_key_file_get_double (kf, "General", "ProviderUseFee", &error);
    if (error != NULL)
    {
        Log ("Error reading config: %s\n", error->message);
        exit (1);
    }

    error = NULL;
    theConfig.general.ProviderReturnFee = g_key_file_get_double (kf, "General", "ProviderReturnFee", &error);
    if (error != NULL)
    {
        Log ("Error reading config: %s\n", error->message);
        exit (1);
    }

    error = NULL;
    theConfig.general.VAT = g_key_file_get_integer (kf, "General", "VAT", &error);
    if (error != NULL)
    {
        Log ("Error reading config: %s\n", error->message);
        exit (1);
    }

    error = NULL;
    p = g_key_file_get_string (kf, "General", "ShellyPM", &error);
    if (error != NULL)
    {
        Log ("Error reading config: %s\n", error->message);
        exit (1);
    }
    snprintf (theConfig.general.ShellyPMNetworkName, sizeof (theConfig.general.ShellyPMNetworkName), "%s", p);
    g_free (p);
    theConfig.general.ShellyPMActive = (strlen (theConfig.general.ShellyPMNetworkName) != 0);
    if (theConfig.general.ShellyPMActive)
    {
        error = NULL;
        theConfig.general.ShellyPMPVPower = g_key_file_get_integer (kf, "General", "ShellyPMPVPower", &error);
        if (error != NULL)
        {
            Log ("Error reading config: %s\n", error->message);
            exit (1);
        }
    }

    error = NULL;
    p = g_key_file_get_string (kf, "General", "HomeWizardP1", &error);
    if (error != NULL)
    {
        Log ("Error reading config: %s\n", error->message);
        exit (1);
    }
    snprintf (theConfig.general.HomeWizardP1NetworkName, sizeof (theConfig.general.HomeWizardP1NetworkName), "%s", p);
    g_free (p);
    theConfig.general.HomeWizardP1Active = (strlen (theConfig.general.HomeWizardP1NetworkName) != 0);

    error = NULL;
    p = g_key_file_get_string (kf, "General", "EntsoeAPIToken", &error);
    if (error != NULL)
    {
        Log ("Error reading config: %s\n", error->message);
        exit (1);
    }
    snprintf (theConfig.general.EntsoeAPIToken, sizeof (theConfig.general.EntsoeAPIToken), "%s", p);
    g_free (p);

    error = NULL;
    p = g_key_file_get_string (kf, "General", "NEDAPIToken", &error);
    if (error != NULL)
    {
        Log ("Error reading config: %s\n", error->message);
        exit (1);
    }
    snprintf (theConfig.general.NEDAPIToken, sizeof (theConfig.general.NEDAPIToken), "%s", p);
    g_free (p);

    error = NULL;
    p = g_key_file_get_string (kf, "General", "SolcastAPIToken", &error);
    if (error != NULL)
    {
        Log ("Error reading config: %s\n", error->message);
        exit (1);
    }
    snprintf (theConfig.general.SolcastAPIToken, sizeof (theConfig.general.SolcastAPIToken), "%s", p);
    g_free (p);

    g_key_file_free (kf);

    Log ("Startup parameters:\n- AlphaESS : networkName: %s, PV power: %d, usable battery capacity: %d, inverter nominal power: %d\n",
        theConfig.general.AlphaESSNetworkName, theConfig.general.AlphaESSPVPower, theConfig.general.AlphaESSUsableBatteryCapacity,
        theConfig.general.AlphaESSInverterNominalPower);
    Log ("- provider use fee: %f, provider return fee: %f, VAT: %d%%\n", theConfig.general.ProviderUseFee, theConfig.general.ProviderReturnFee, theConfig.general.VAT);

    if (theConfig.general.ShellyPMActive)
    {
        Log ("- ShellyPM : networkName: %s, PV power: %d\n", theConfig.general.ShellyPMNetworkName, theConfig.general.ShellyPMPVPower);
    }
    if (theConfig.general.HomeWizardP1Active)
    {
        Log ("- HomeWizardP1 networkName: %s\n", theConfig.general.HomeWizardP1NetworkName);
    }
}

static  void    ReadConfigDaily (void)
{
    GKeyFile *kf;
    GError *error = NULL;

    if ((kf = ReadConfigFile ()) == NULL)
    {
        exit (1);
    }

    // set default
    theConfig.daily.profitMin = 0.5;

    error = NULL;
    theConfig.daily.profitMin = g_key_file_get_double (kf, "Daily", "minProfitOnChargeCycle", &error);
    if (error != NULL)
    {
        Log ("Error reading config: %s\n", error->message);
        exit (1);
    }

    g_key_file_set_list_separator (kf, ',');
    gint *values = NULL;
    gsize length = 0;
    error = NULL;
    values = g_key_file_get_integer_list (kf, "Daily", "allowProviderControlHour", &length, &error);
    if (error != NULL)
    {
        Log ("Error reading config: %s\n", error->message);
        exit (1);
    }

    // set default
    int     i;
    for (i = 0; i < MAX_HOURS; i++)
    {
        theConfig.daily.allowProviderControlHour [i] = false;
    }
    for (gsize i = 0; i < length; i++)
    {
        if (values [i] >= 0 && values [i] < MAX_HOURS)
        {
            theConfig.daily.allowProviderControlHour [values [i]] = true;
        }
    }
    g_free (values);

    g_key_file_free (kf);

    Log ("Daily settings : minProfit: %3.2f. Provider controlled hours: ", theConfig.daily.profitMin);
    bool first = true;
    for (i = 0; i < MAX_HOURS; i ++)
    {
        if (theConfig.daily.allowProviderControlHour [i])
        {
            if (!first)
            {
                Log (", ");
            }
            first = false;
            Log ("%02d", i);
        }
    }
    if (first)
    {
        Log ("none");
    }
    Log ("\n");
}

static  int     ReadConfigMinute (day_t today, int* charge, int* cutoff)
{
    GKeyFile *kf;
    GError *error = NULL;

    kf = ReadConfigFile ();

    error = NULL;
    char *cmd = g_key_file_get_string (kf, "Minute", "command", &error);
    if (error != NULL)
    {
        Log ("Error reading config: %s\n", error->message);
        g_error_free (error);
        g_free (cmd);
        g_key_file_free (kf);
        return (false);
    }

    char c = cmd [0];
    g_free (cmd);
    g_key_file_free (kf);

    switch (c)
    {
        case 'C':
            Log ("Config command: Charging on grid\n");
            *charge = CHARGING_ON_GRID;
            *cutoff = SOC_MAX_CHARGE_ON_GRID;
            return (true);
        case 'D':
            Log ("Config command: Discharge\n");
            *charge = CHARGING_DISCHARGE;
            *cutoff = SOC_MIN_DISCHARGE_AFTER;
            return (true);
        case 'P':
            Log ("Config command: Charging on PV\n");
            *charge = CHARGING_ON_PV;
            *cutoff = SOC_MAX;
            return (true);
        case 'N':
            Log ("Config command: No charging\n");
            *charge = NO_CHARGING;
            *cutoff = SOC_MAX;
            return (true);
        case 'S':
            Log ("Config command: Show schedule\n");
            ShowSchedule (today, 0, MAX_HOURS, false, "Schedule for today");
            return (false);
    }
    return (false);
}

static  bool Init (int year, int mon, int day)
{
    int     ret;

    char    logName [50];

    signal (SIGTERM, Shutdown);
    signal (SIGINT, Shutdown);

    sprintf (logName, "../logs/%04d-%02d-%02d.log", year, mon, day);
    SetLogfileName (logName);

    TimeLog ("*** Initialization started ***\n");

    ReadConfigInit ();
    
    if (!AlphaESSConnect (theConfig.general.AlphaESSNetworkName))
    {
        Log ("Modbus connection failed: %s\n", modbus_strerror (errno));
        return (false);
    }

    curl_global_init (CURL_GLOBAL_ALL);

    TimeLog ("*** Initialization finished ***\n");

    return (true);
}

static  bool DoDailyWork (int year, int mon, int day, day_t yesterday, day_t* today, day_t* tomorrow)
{
    char    logName [50];
    int     curSOC;

    sprintf (logName, "../logs/%04d-%02d-%02d.log", year, mon, day);
    SetLogfileName (logName);

    TimeLog ("*** AlphaESSControl %s - Daily work started ***\n", SOFTWARE_VERSION);

    ReadConfigDaily ();

    if (yesterday.valid)
    {
        // added for analysus
        LogExecutionResult (23, yesterday.hour [23]);
        //

        // show last (old) schedule on day change
        ShowSchedule (yesterday, 0, MAX_HOURS, false, "Reporting past day");
        // store last hour data
        StoreHourData (yesterday, 23);
    }

    // calculate mean power data once every day
    CalculateAndStoreMeanData ();

    InitDay (year, mon, day, today);
    time_t      now = time (NULL) + 60 * 60 * 24;
    struct  tm  now_tm = *localtime (&now);
    InitDay (now_tm.tm_year + 1900, now_tm.tm_mon + 1, now_tm.tm_mday, tomorrow);

    curSOC = GetSOC ();
    if (ReadPricesAndEstimates (today, 0))
    {
        SetSchedule (curSOC, now_tm.tm_hour, today, tomorrow);  // set schedule for today
        ShowSchedule (*today, now_tm.tm_hour, now_tm.tm_hour, false, "Schedule for today");
    }

    TimeLog ("*** Daily work finished ***\n");
    return (true);
}

static  bool IsEqualDispatchParam (dispatch_t dispatchParam1, dispatch_t dispatchParam2)
{
    return (dispatchParam1.started == dispatchParam2.started &&
            dispatchParam1.mode == dispatchParam2.mode &&
            dispatchParam1.duration == dispatchParam2.duration &&
            dispatchParam1.cutoffSOC == dispatchParam2.cutoffSOC &&
            dispatchParam1.power == dispatchParam2.power &&
            dispatchParam1.PVOn == dispatchParam2.PVOn);
}

static  void    CheckAndSetChargingMode (day_t* today, int hour, int min)
{
            uint16_t    curFeedInPercentage, curSOC, targetFeedInPercentage;
            dispatch_t  curDispatchParam, setDispatchParam;
    static  dispatch_t  lastReadDispatchParam = { -1, false }, lastSetDispatchParam = { -1, false };
    static  int         providerChargingPower = 0;
    static  int         lastHour = -1;
            bool        hourStart = false;

    Debug (3, "CheckAndSetChargingMode: feed-in %s, charging %s, cut-off SOC: %u\n",
              today->hour [hour].feedIn ? "enabled" : "disabled", today->hour[hour].charge != NO_CHARGING ? "enabled" : "disabled", today->hour[hour].cutoffSOC);

    if (lastHour != hour)
    {
        lastHour = hour;
        hourStart = true;
        today->hour [hour].providerOverride [0].valid = false;
        today->hour [hour].providerOverride [1].valid = false;
        today->hour [hour].providerOverride [2].valid = false;
        today->hour [hour].providerOverride [3].valid = false;
    }

    if (GetDispatchParam (&curDispatchParam) < 0)
    {
        return;
    }
    curFeedInPercentage = GetMaxFeedIntoGrid ();
    curSOC = GetSOC();

    switch (today->hour[hour].charge)
    {
        case CHARGING_ON_PV:    
            if (today->hour [hour].cutoffSOC == SOC_MAX_BATTERY || curSOC < today->hour [hour].cutoffSOC)                   // on 100% we keep on charging, otherwise stop when cutoffSOC reached
            {
                setDispatchParam.mode = DISPATCH_MODE_NORMAL;                   // PV to grid according to feed-in setting, charging, self compensation
            }
            else
            {
                setDispatchParam.mode = DISPATCH_MODE_NO_BATTERY_CHARGE;        // PV to grid, no charging, self compensation regulated by PVOn, feedIn blocked on negatice prices
            }
            break;

        case NO_CHARGING:
            if (today->hour [hour].earning == EARNING_ON_USE)
            {
                setDispatchParam.mode = DISPATCH_MODE_ONLY_CHARGE_FROM_PV;  // no discharge, no self compensation
            }
            else
            {
                setDispatchParam.mode = DISPATCH_MODE_NO_BATTERY_CHARGE;    // no charging, self compensation
            }
            break;

        case CHARGING_DISCHARGE:
            if (today->hour [hour].earning == EARNING_ON_USE)
            {
                Log ("Earning on use: Discharging not allowed, automatically set to No-charging\n");
                setDispatchParam.mode = DISPATCH_MODE_ONLY_CHARGE_FROM_PV;  // no discharge, no self compensation
            }
            else
            {
                //int diff = curSOC - today->hour [hour].cutoffSOC;
                //if (diff <= 0 || (diff > 0 && diff < 10 && GetBatteryPower () == 0))    // if in last percentage and no discharging anymore
                if (curSOC <= today->hour [hour].cutoffSOC)
                {
                    setDispatchParam.mode = DISPATCH_MODE_NO_BATTERY_CHARGE;    // no charging, self compensation
                }
                else
                {
                    setDispatchParam.mode = DISPATCH_MODE_STATE_OF_CHARGE_CONTROL;
                }
                today->hour [hour].feedIn = true;   // always feed-in on discharge
            }
            break;

        case CHARGING_ON_GRID:
            if (today->hour [hour].cutoffSOC == SOC_MAX_BATTERY || curSOC < today->hour [hour].cutoffSOC)                   // on 100% we keep on charging, otherwise stop when cutoffSOC reached
            {
                if (today->hour [hour].earning == EARNING_ON_USE)
                {
                    setDispatchParam.mode = DISPATCH_MODE_MAXIMISE_CONSUMPTION;             // max negative price, so load battery to highest possible
                }
                else
                {
                    setDispatchParam.mode = DISPATCH_MODE_STATE_OF_CHARGE_CONTROL;          // no negative prices
                }
            }
            else
            {
                if (today->hour [hour].earning == EARNING_ON_USE)
                {
                    setDispatchParam.mode = DISPATCH_MODE_ONLY_CHARGE_FROM_PV;              // no self compensation
                }
                else
                {
                    setDispatchParam.mode = DISPATCH_MODE_NO_BATTERY_CHARGE;
                }
            }
            break;
        case NO_DISCHARGING:
            if (today->hour [hour].cutoffSOC == SOC_MAX_BATTERY || curSOC < today->hour [hour].cutoffSOC)   // on 100% we keep on charging, otherwise stop when cutoffSOC reached
            {
                setDispatchParam.mode = DISPATCH_MODE_ONLY_CHARGE_FROM_PV;                  // no discharge, no self compensation
            }
            else
            {
                setDispatchParam.mode = DISPATCH_MODE_NO_BATTERY_CHARGE;                    // no charging, self compensation
            }
            break;
    }

    // security check
    if (today->hour [hour].earning == EARNING_ON_USE)
    {
        today->hour [hour].feedIn = false;   // never feedIn on negative prices
    }

    setDispatchParam.started = true;
    setDispatchParam.duration = 60 * 60;    // always set for 1 hour

    long power = (theConfig.general.AlphaESSUsableBatteryCapacity * (SOC_MAX_BATTERY - SOC_MIN) / 1000) + 500;
    switch (setDispatchParam.mode)
    {
        case DISPATCH_MODE_NO_BATTERY_CHARGE:
            setDispatchParam.power = 0;
            setDispatchParam.cutoffSOC = 0;
            setDispatchParam.PVOn = true;
            break;

        case DISPATCH_MODE_NORMAL:
        case DISPATCH_MODE_ONLY_CHARGE_FROM_PV:
            if (today->hour [hour].cutoffSOC == SOC_MAX_BATTERY || curSOC < today->hour [hour].cutoffSOC)
            {
                setDispatchParam.power = power;
            }
            else
            {
                setDispatchParam.power = 0;
            }
            setDispatchParam.cutoffSOC = 0;
            setDispatchParam.PVOn = true;
            break;

        case DISPATCH_MODE_STATE_OF_CHARGE_CONTROL:
            setDispatchParam.power = (today->hour [hour].charge == CHARGING_DISCHARGE) ? 0 - power : power;
            setDispatchParam.cutoffSOC = today->hour [hour].cutoffSOC;
            setDispatchParam.PVOn = true;
            break;

        case DISPATCH_MODE_MAXIMISE_CONSUMPTION:
            setDispatchParam.power = 0;
            setDispatchParam.cutoffSOC = 0;
            setDispatchParam.PVOn = false;          // maximise grid consumption
            break;

        default:
            Log ("Unsupported dispatch state %d\n", setDispatchParam.mode);
            return;
    }
    // security check
    if (today->hour [hour].earning == EARNING_ON_USE)
    {
        today->hour [hour].feedIn = false;   // never feedIn on negative prices
        setDispatchParam.PVOn = false;       // no PV on negative prices
    }
    targetFeedInPercentage = today->hour [hour].feedIn ? 100 : 0;
    
    Debug (3, "Currently: mode: %d, feed-in: %u%%, cut-off SOC: %1.1f%%, SOC: %1.1f%%, PV-power: %d\n", curDispatchParam.mode, curFeedInPercentage, (double) curDispatchParam.cutoffSOC / 10.0, (double)curSOC / 10.0, GetBatteryPower ());
    if (!hourStart && IsEqualDispatchParam (curDispatchParam, setDispatchParam) && curFeedInPercentage == targetFeedInPercentage)
    {
        if (curDispatchParam.mode != DISPATCH_MODE_STATE_OF_CHARGE_CONTROL || (curDispatchParam.mode == DISPATCH_MODE_STATE_OF_CHARGE_CONTROL && curDispatchParam.duration > 0))  // extra check on State of Charge
        {
            Debug (2, "Mode (%u), feed-in (%u%%) and cut-off SOC (%1.1f%%) already set as required\n", curDispatchParam.mode, curFeedInPercentage, (double) curDispatchParam.cutoffSOC / 10.0);
            return;
        }
    }

    Debug (2, "SOC = %1.1f%%, setting feed-in %s\n", (double) curSOC / 10.0, today->hour [hour].feedIn ? "enabled" : "disabled");

    bool allowProviderControl = theConfig.daily.allowProviderControlHour [hour];
    // skip provider controlled operation if: curSOC >= 20% AND hour is before GRID charge on negatice prices AND provider has set charging
    if (curSOC >= SOC_MAX_CHARGE_PROVIDER && today->indexCharge >= 0 && today->hour [today->indexCharge].earning == EARNING_ON_USE &&
        hour <= today->indexCharge && curDispatchParam.power >= 0)
    {
        allowProviderControl = false;
    }
    // skip provider controlled operation if we have scheduled our own GRID charge or discharge
    //if (today->hour [hour].charge == CHARGING_ON_GRID || today->hour [hour].charge == CHARGING_DISCHARGE)
    //{
    //    allowProviderControl = false;
    //}

    if (allowProviderControl)
    {
        // if in State of Charge with power set not started by us
        if (lastSetDispatchParam.mode != DISPATCH_MODE_STATE_OF_CHARGE_CONTROL && curDispatchParam.started && curDispatchParam.mode == DISPATCH_MODE_STATE_OF_CHARGE_CONTROL && curDispatchParam.power != 0)
        {
            if (hourStart || providerChargingPower != curDispatchParam.power)
            {
                providerChargingPower = curDispatchParam.power;
                TimeLog ("Provider controlled operation active: %d Watt (%s)\n", curDispatchParam.power, GetDispatchPowerModeMsg (curDispatchParam.power));
                // CalculatePredictedImbalancePrice ();
            }
            if (curDispatchParam.power < 0)                     // on provider controlled decharging, turn on PV to maximize revenue
            {
                curDispatchParam.PVOn = true;
                SetDispatchParam (curDispatchParam);
                SetGaragePV (true);
            }
            else                                                // on provider controlled charging, do not turn off PV, not sure if charging on best prices
            {
                SetGaragePV (today->hour [hour].feedIn);        // set GaragePV according to schedule
            }
            Debug (2, "State of Charge Control active, skipping charging mode change\n");
            
            // provider actions are per quarter of an hour
            int q = min / 15;
            today->hour [hour].providerOverride [q].valid = true;
            today->hour [hour].providerOverride [q].powerSetting = curDispatchParam.power;
            return;
        }
    }

    SetMaxFeedIntoGrid (targetFeedInPercentage);                // always set feed-in %. Feed-in always active in State of Charge mode
    SetGaragePV (today->hour [hour].feedIn);

    if (providerChargingPower != 0)                             // assure logging restart after provider initiated state of charge control
    {
        providerChargingPower = 0;
        lastReadDispatchParam.mode = -1;
        lastSetDispatchParam.mode = -1;
        TimeLog ("Provider controlled operation terminated\n");
    }

    if (hourStart || !IsEqualDispatchParam (lastReadDispatchParam, curDispatchParam) || !IsEqualDispatchParam (lastSetDispatchParam, setDispatchParam))  // check for a setting change
    {
        char extraCurMsg[20] = { '\0' };
        if (curDispatchParam.mode == DISPATCH_MODE_STATE_OF_CHARGE_CONTROL)
        {
            sprintf (extraCurMsg, " (%d Watt)", curDispatchParam.power);
        }
        char extraSetMsg[20] = { '\0' };
        if (setDispatchParam.mode == DISPATCH_MODE_STATE_OF_CHARGE_CONTROL)
        {
            sprintf (extraSetMsg, " (%d Watt)", setDispatchParam.power);
        }
        if (hourStart || curDispatchParam.mode != setDispatchParam.mode)    // only log on hour start or mode change
        {
            TimeLog ("Setting mode: %s%s (%d), previous mode: %s (%d)%s. SOC = %1.1f%%\n",
                    GetDispatchMsg (setDispatchParam.mode), extraSetMsg, setDispatchParam.mode,
                    GetDispatchMsg (curDispatchParam.mode), curDispatchParam.mode, extraCurMsg, (double) curSOC / 10.0);
        }
        
        //Log ((char*)"== Setting ");
        //Log ((char*)"dispatch %s, ", setDispatchParam.started ? "started" : "stopped");
        //Log ((char*)"power: %d Watt (%s), ", setDispatchParam.power, GetDispatchPowerModeMsg (setDispatchParam.power));
        //Log ((char*)"cut-off-SOC: %1.1f%%, ", setDispatchParam.cutoffSOC);
        //Log ((char*)"duration: %d sec, ", setDispatchParam.duration);
        //Log ((char*)"PVSwitch: %s, ", setDispatchParam.PVOn ? "on" : "off");
        //Log ((char*)"feedIn: %d%% ==\n", targetFeedInPercentage);
    }

    lastReadDispatchParam = curDispatchParam;
    lastSetDispatchParam = setDispatchParam;
    SetDispatchParam (setDispatchParam);
}

static  bool    IsChargeDiff (day_t day1, day_t day2, int curHour)
{
    if (!day1.valid && !day2.valid)
    {
        return (false);
    }
    if (day1.valid != day2.valid)
    {
        return (true);
    }
    for (int i = curHour; i < MAX_HOURS; i ++)
    {
        if (!day1.hour [i].valid || !day2.hour [i].valid)
        {
            return (true);
        }
        if (day1.hour [i].charge != day2.hour [i].charge)
        {
            return (true);
        }
    }
    return (false);
}

static  bool DoHourlyWork (int curHour, day_t yesterday, day_t* today, day_t* tomorrow)
{
    static  int lastHour = -1, curSOC;
    bool    hourStart = false;

    if (lastHour != curHour)
    {
        lastHour = curHour;
        hourStart = true;
    }

    Debug (3, "== Hourly work for %02d:00 hours\n", curHour);
    curSOC = GetSOC ();
    TimeLog ("*** Hourly work started, SOC = %.1f%%, provider control%sallowed ***\n",
             (double) curSOC / 10.0, theConfig.daily.allowProviderControlHour [curHour] ? " " : " not ");

    today->hour [curHour].realStartSOC = curSOC;

    // store last hour data, last hour (23:00) is stored in Daily work using yesterday
    if (hourStart && curHour > 0)
    {
        int execHour = curHour - 1;

        // added for analysis
        LogExecutionResult (execHour, today->hour [execHour]);
        //

        ShowSchedule (*today, execHour, curHour, true, "Past hour:");     // show results of last hour
        StoreHourData (*today, execHour);
    }

    if (!today->hour [curHour].valid)              // Daily work should repair
    {
        Log ("Setting state: no earnings (no valid price)\n");
        today->hour [curHour].earning = NO_EARNING;
        today->hour [curHour].feedIn = true;
        today->hour [curHour].charge = CHARGING_ON_PV;
        today->hour [curHour].cutoffSOC = SOC_MAX;
    }
    else
    {
        charge_t curCharge;

        curCharge = today->hour [curHour].charge;
        bool didShow = false;

        if (!tomorrow->valid)
        {
            if (ReadPricesAndEstimates (tomorrow, curHour))
            {
                SetSchedule (curSOC, curHour, today, tomorrow);    // set schedule for today and tomorrow
                ShowSchedule (*today, 0, curHour, false, "Schedule for today");          // show both schedules, since schedule for today may be changed due to tomorrow's prices
                ShowSchedule (*tomorrow, 0, 0, false, "Schedule for tomorrow");
                didShow = true;
            }
        }
        if (!didShow)
        {
            if (today->valid)
            {
                ReadEstimatedSolarPower (today, curHour);
            }
            if (tomorrow->valid)
            {
                ReadEstimatedSolarPower (tomorrow, curHour);
            }
            day_t todayOrg = *today;
            day_t tomorrowOrg = *tomorrow;
            SetSchedule (curSOC, curHour, today, tomorrow);
            if (IsChargeDiff (todayOrg, *today, curHour))   // show schedule on change
            {
                ShowSchedule (*today, 0, curHour, false, "Schedule for today");
            }
        }

        if (today->hour [curHour].charge != curCharge)
        { 
            char chargeMsgOld [20], chargeMsgNew [20];

            SetChargingMsg (chargeMsgOld, curCharge);
            SetChargingMsg (chargeMsgNew, today->hour [curHour].charge);
            TimeLog ("Charging mode changed due to current SOC: %s -> %s\n", chargeMsgOld, chargeMsgNew);
        }
    }

    if (today->hour [curHour].valid)
    {
        today->hour [curHour].realCharge = today->hour [curHour].charge;    // update realCharge
        ShowSchedule (*today, curHour, curHour, true, "Schedule:");
    }

    TimeLog ("*** Hourly work finished ***\n");
    return (true);
}

static  bool    CalculatePower (int hour, day_t* today)
{
    hour_t* h = &today->hour [hour];
    int count = h->fiveMinCount;

    if (count < 0 || count >= MAX_FIVE_MINS)
    {
        TimeLog ("CalculatePower: fiveMinCount out of bounds\n", count);
        return (true);  // no retry
    }

    double pvRoof = GetPVPower ();
    double pvGarage = GetGaragePVPower ();
    // it seems that sometimes - in cases of fast solar power fluctuation - the inverter grid meter reacts too slowly
    // therefore we use the power metering directly from the P1 port, through the Home Wizard dongle API, if present
    double totalActivePower;
    if (theConfig.general.HomeWizardP1Active)
    {
        totalActivePower = GetActivePowerP1 ();
    }
    else
    {
        totalActivePower = GetTotalActivePower ();
    }
    double batteryPower = (double) GetBatteryPower ();
    //int     totalFrom = GetTotalEnergyConsumeFromGrid () * 10.0;
    //int     totalFeed = GetTotalEnergyFeedToGrid () * 10.0;
    //Debug (2, "Consume: %d feedin: %d\n", totalFrom, totalFeed);

    Debug (2, "Battery: %f, PV roof: %f, PV garage: %f\n", batteryPower, pvRoof, pvGarage);
    Debug (2, "Total active power: %f\n", totalActivePower);

    // activePower: - = return to grid, + = use from grid
    // batteryPower: - = charge, + = discharge
    double realHouseLoad = pvRoof + pvGarage + totalActivePower + batteryPower;
    if (realHouseLoad < 0.0)
    {
        Log ("5-minute measurement ignored: negative houseLoad (%.0f = roof %.0f + garage %.0f + active %.0f + battery %.0f)\n",
                realHouseLoad, pvRoof, pvGarage, totalActivePower, batteryPower);
        return (false);
    }

    //TimeLog ("CalculatePower: count %d: houseLoad %.0f = roof %.0f + garage %.0f + active %.0f + battery %.0f\n",
    //                count, realHouseLoad, pvRoof, pvGarage, totalActivePower, batteryPower);

    fiveMin_t* s = &h->fiveMin [count];
    s->realSolarPowerRoof = pvRoof;
    s->realSolarPowerGarage = pvGarage;
    s->totalActivePower = totalActivePower;
    s->realHouseLoad = realHouseLoad;

    if (h->fiveMinCount < MAX_FIVE_MINS)
    {
        h->fiveMinCount++;
        count = h->fiveMinCount;
    }

    // Calculate mean
    double sumRoof = 0.0;
    double sumGarage = 0.0;
    double sumLoad = 0.0;
    double sumActive = 0.0;

    for (int i = 0; i < count; i++)
    {
        sumRoof += h->fiveMin [i].realSolarPowerRoof;
        sumGarage += h->fiveMin [i].realSolarPowerGarage;
        sumLoad += h->fiveMin [i].realHouseLoad;
        sumActive += h->fiveMin [i].totalActivePower;
    }

    h->realSolarPowerRoof = sumRoof / count;
    h->realSolarPowerGarage = sumGarage / count;
    h->realHouseLoad = sumLoad / count;
    h->totalActivePower = sumActive / count;

    // Cost calculation
    double activePower = h->totalActivePower;

    if (activePower > 0.0)  // + = use from grid
    {
        h->realResult = -activePower * MkUsePrice (h->price) / 1000.0;   // activePower in Watts, price in kW
    }
    else                    // - = return to grid
    {
        h->realResult = -activePower * MkReturnPrice (h->price) / 1000.0;
    }

    Debug (2, "realResult: %f -activePower: %f\n", h->realResult, -activePower);
    return (true);
}

static  bool DoMinuteWork (int hour, int min, day_t* today, day_t* tomorrow)
{
    static  int     lastMin, lastHour = -1;
    static  bool    halfHourCheckDone = false;

    Debug (3, "== Minute work for %02d:%02d hours\n", hour, min);
    Debug (2, "*** Minute work started ***\n");

    if (lastHour != hour)
    {
        lastHour = hour;
        lastMin = -10;  // force update
        halfHourCheckDone = false;
    }

    if (min >= lastMin + 5)    // each 5 minutes we calculate power
    {
        if (CalculatePower (hour, today))   // on failure we retry
        {
            lastMin = min;
        }
    }

    // register actually exectuted charge & discharge
    if (today->hour[hour].charge == CHARGING_ON_GRID)
    {
        today->chargeOnGridUsed = true;
        today->indexCharge = hour;
    }
    if (today->hour[hour].charge == CHARGING_DISCHARGE)
    {
        today->dischargeUsed = true;
        today->indexDischarge = hour;
    }

    CheckAndSetChargingMode (today, hour, min);

    Debug (2, "*** Minute work finished ***\n");
    return (true);
}

int main ()
{
    time_t      now, last = 0L;
    struct      tm  now_tm;
    int         lastHour = -1, lastYearDay = -1;
    day_t       yesterday, today, tomorrow;

    SetDebug (0);

    yesterday.valid = false;
    today.valid = false;
    tomorrow.valid = false;

    now = time (NULL);
    now_tm = *localtime (&now);
    if (!Init (now_tm.tm_year + 1900, now_tm.tm_mon + 1, now_tm.tm_mday))
    {
        exit (1);
    }

    last = 0;   // force first time execution
    for (;;)
    {
        now = time (NULL);
        if (now < last + 60L)
        {
            sleep (15);     // sleep to reduce external communication (both http and modbus)
        }
        last = now;
        now_tm = *localtime (&now);

        if (now_tm.tm_yday != lastYearDay)
        {
            yesterday = today;
            lastYearDay = now_tm.tm_yday;
            lastHour = -1;
            today.valid = false;
            tomorrow.valid = false;
        }

        if (!today.valid)
        {
            if (!DoDailyWork (now_tm.tm_year + 1900, now_tm.tm_mon + 1, now_tm.tm_mday, yesterday, &today, &tomorrow))
            {
                exit (1);
            }
        }
        
        if (now_tm.tm_hour != lastHour || !today.hour [now_tm.tm_hour].valid)
        {
            lastHour = now_tm.tm_hour;
            if (!DoHourlyWork (now_tm.tm_hour, yesterday, &today, &tomorrow))
            {
                exit (1);
            }
        }

        if (!DoMinuteWork (now_tm.tm_hour, now_tm.tm_min, &today, &tomorrow))
        {
            exit (1);
        }
    }
}
