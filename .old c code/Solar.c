//
// Author: Jaco Melse (jjmelse@xs4all.nl)
// 2025 - 2026
//

#include <stdio.h>
#include <stdlib.h>
#include <stdbool.h>
#include <string.h>
#include <math.h>
#include <errno.h>
#include <time.h>
#include <unistd.h>
#include <sys/wait.h>
#include <curl/curl.h>
#include <cjson/cJSON.h>
#include "Data.h"
#include "Util.h"
#include "AlphaESSControl.h"

static inline double PI (void)
{
    return acos (-1.0);
}

/*
 * OrientationFactor
 *
 * Smooth orientation correction relative to south-facing panels.
 * Calibrated for Dutch PV yield (diffuse-heavy climate).
 *
 * az_deg : panel azimuth relative to south
 *          (south = 0, west = +90, east = -90)
 *
 * return : factor [0.0 .. 1.0]
 */
static  double OrientationFactor (double azimuthDeg)
{
    double az = fabs (azimuthDeg);

    if (az >= 180.0)
    {
        return (0.55);  // practical lower bound for north-facing PV
    }

    double rad = az * (PI () / 180.0);

    /* exponent tuned for NL PV yield */
    double p = 1.3;

    double f = pow (cos (rad), p);

    /* clamp for numerical safety */
    if (f < 0.55)
    {
        f = 0.55;
    }

    return (f);
}

/*
 * TiltFactor
 *
 * Returns a smooth correction factor for PV panel tilt.
 * Calibrated for Dutch PV yield relative to the average panel tilt (≈35°).
 *
 * tilt_deg : panel tilt angle in degrees (0 = horizontal)
 *
 * return   : correction factor [0.85 .. 1.00]
 *
 * Notes:
 * - Maximum efficiency near 35° (typical Dutch roof).
 * - Slightly reduced for lower or higher tilts.
 * - Intended for adjusting NED.nl PV forecast percentages.
 */
static  double  TiltFactor (double tiltDeg)
{
    double d = fabs (tiltDeg - 35.0);
    return (1.0 - 0.0005 * d * d);
}

/* Calculate corrected Wh per hour. Normalise percentage if needed.
 * Note: percentage is expected as fraction (0..1) or percent (0..100).
 */
static double CalculateCorrectedWh (double rawPercentage, double myWp, int azimuthDeg, int tiltDeg)
{
    double percentage = rawPercentage;

    /* normalize percentage if API returns 0..100 */
    if (percentage > 1.0)
    {
        percentage /= 100.0;
    }
    if (percentage < 0.0) percentage = 0.0;
    if (percentage > 1.0) percentage = 1.0;

    double oFactor = OrientationFactor (azimuthDeg);
    double tFactor = TiltFactor (tiltDeg);
    double correctedWh = myWp * percentage * oFactor * tFactor;

    return (correctedWh);
}

static bool ExtractNEDReponse (const char *json, day_t* today)
{
    cJSON *root = cJSON_Parse (json);
    if (!root)
    {
        Log ("ExtractNEDResponse: JSON parse error\n");
        return (false);
    }

    /* hydra:member is een array */
    cJSON *members = cJSON_GetObjectItem (root, "hydra:member");
    if (!cJSON_IsArray (members))
    {
        Log ("ExtractNEDResponse: hydra:member not found or not array\n");
        cJSON_Delete (root);
        return (false);
    }

    config_t theConfig = GetConfig ();

    int count = cJSON_GetArraySize (members);
    //printf ("Aantal records: %d\n\n", count);

    struct  tm thetm;
    time_t  t_of_day;

    for (int i = 0; i < count; i++)
    {
        cJSON *item = cJSON_GetArrayItem (members, i);
        if (!cJSON_IsObject (item))
        {
            continue;
        }

        //cJSON *id = cJSON_GetObjectItem (item, "id");
        cJSON *validfrom = cJSON_GetObjectItem (item, "validfrom");
        //cJSON *validto = cJSON_GetObjectItem (item, "validto");
        //cJSON *capacity = cJSON_GetObjectItem (item, "capacity");
        //cJSON *volume = cJSON_GetObjectItem (item, "volume");
        cJSON *percentage = cJSON_GetObjectItem (item, "percentage");

        // 2025-12-23T14:00:00+00:00
        if (cJSON_IsString (validfrom))
        {
            int year, mon, day, hour;
            if (sscanf (validfrom->valuestring, "%d-%d-%dT%d:00", &year, &mon, &day, &hour) == 4)
            {
                struct tm mytm;
                mytm = UTC2CET (year, mon, day, hour, 0);
                if (mytm.tm_year == today->year - 1900 && mytm.tm_mon == today->mon - 1 && mytm.tm_mday == today->day)    // that's today
                {
                    if (cJSON_IsNumber (percentage))
                    {
                        today->hour [mytm.tm_hour].estimatedSolarPercentageNED = percentage->valuedouble;
                        //printf ("%02d:00 - %f\n", mytm.tm_hour, percentage->valuedouble);
                        
                        int roofPower = 0, garagePower = 0;
                        if (today->hour [mytm.tm_hour].estimatedSolarPowerRoofCorrectionFactor >= 0.0)  // if valid local correction factor, set corrected solar estimates
                        {
                            roofPower = (double) theConfig.general.AlphaESSPVPower * percentage->valuedouble * today->hour [mytm.tm_hour].estimatedSolarPowerRoofCorrectionFactor +
                                            today->hour [mytm.tm_hour].estimatedSolarPowerRoofCorrectionOffset;
                        }
                        else
                        {   // default: 17*430 Wp, 48 graden N-O, 25 graden helling
                            roofPower = CalculateCorrectedWh (percentage->valuedouble, theConfig.general.AlphaESSPVPower, 48, 25);
                        }

                        if (theConfig.general.ShellyPMActive)   // only in case garage is active
                        {
                            if (today->hour [mytm.tm_hour].estimatedSolarPowerGarageCorrectionFactor >= 0.0)
                            {
                                garagePower = (double) theConfig.general.ShellyPMPVPower * percentage->valuedouble * today->hour [mytm.tm_hour].estimatedSolarPowerGarageCorrectionFactor +
                                                today->hour [mytm.tm_hour].estimatedSolarPowerGarageCorrectionOffset;
                            }
                            else
                            {   // default: 18*185 Wp, 40 graden N-O, 10 graden helling
                                garagePower = CalculateCorrectedWh (percentage->valuedouble, theConfig.general.ShellyPMPVPower, 40, 10);
                            }
                        }
                        today->hour [mytm.tm_hour].estimatedSolarPower = roofPower + garagePower;
                    }
                }
            }
        }
        //if (cJSON_IsString (validto)) u.valid_to = validto->valuestring;
        //if (cJSON_IsNumber (capacity)) u.capacity = capacity->valuedouble;
        //if (cJSON_IsNumber (volume)) pv = volume->valuedouble;
    }

    cJSON_Delete (root);
    return (true);
}

static bool    ReadEstimatedSolarPowerNED (day_t* today)
{
    char*   response;
    time_t  t_of_day;
    struct  tm thetm;

    thetm.tm_year = today->year - 1900; // Year - 1900
    thetm.tm_mon = today->mon - 1;      // Month, where 0 = jan
    thetm.tm_mday = today->day;         // Day of the month
    thetm.tm_hour = 8;    
    thetm.tm_min = 0;
    thetm.tm_sec = 0;
    thetm.tm_isdst = 0;

    t_of_day = mktime (&thetm);
    t_of_day += 60*60*24;
    thetm = *localtime (&t_of_day);     // get next day

    //curl_easy_setopt (curl, CURLOPT_USERAGENT, " ned-c-client/1.0");

    char url [1024];
    // type 2 = solar, classification 1 = forecast, point 6 = Gelderland
    snprintf (url, sizeof (url), "https://api.ned.nl/v1/utilizations?point=6&type=2&granularity=5&granularitytimezone=1&classification=1&activity=1&validfrom[strictly_before]=%d-%02d-%02d&validfrom[after]=%d-%02d-%02d",
                                thetm.tm_year + 1900, thetm.tm_mon + 1, thetm.tm_mday, today->year, today->mon, today->day);
    char authHeader [256];
    config_t theConfig = GetConfig ();
    snprintf (authHeader, sizeof (authHeader), "X-AUTH-TOKEN: %s", theConfig.general.NEDAPIToken);
    if ((response = CallURLGetResponse (url, "Accept: application/ld+json", NULL, authHeader, NULL, 30L)) == NULL)
    {
        Log ("ReadEstimatedSolarPowerNED: CallURLGetResponse () failed\n");
        return (false);
    }

    if (strlen (response) == 0)
    {
        free (response);
        return (false);
    }

    bool result = ExtractNEDReponse (response, today);
    free (response);

    return (result);
}

static bool    ReadEstimatedSolarPowerSolcast (day_t* today)
{
    int         i;
    char    *response;

    const char *url = "https://api.solcast.com.au/rooftop_sites/66d3-ed89-ce67-49a9/forecasts?format=csv";
    char authHeader [256];
    config_t theConfig = GetConfig ();
    snprintf (authHeader, sizeof (authHeader), "Authorization: Bearer %s", theConfig.general.SolcastAPIToken);
    if ((response = CallURLGetResponse (url, "Accept: */*", NULL, authHeader, NULL, 20L)) == NULL)
    {
        Log ("ReadEstimatedSolarPowerSolcast: CallURLGetResponse () failed\n");
        return (false);
    }

    if (strlen (response) == 0)
    {
        free (response);
        return (false);
    }

    char*   endStr;
    char*   token = strtok_r (response, "\n", &endStr);

    while (token != NULL)
    {
        //printf ("found: %s\n", token);

        int     col = 0;
        char*   endToken;
        char*   item = strtok_r (token, ",", &endToken);
        while (item != NULL)
        {
            switch (col)
            {
                case 0:     // PV
                    double pv = (atof (item) * 1000.0);    // pv is in kW
                    //printf ("extracted pv: %f ", pv);
                    break;
                case 3:     // start time in UTC
                    int iyear, imon, iday, ihour, imin;

                    if (sscanf (item, "%d-%d-%dT%d:%d:00:00Z", &iyear, &imon, &iday, &ihour, &imin) == 5)
                    {
                        //printf ("extracted time: %d-%02d-%02d %02d:%02d ", iyear, imon, iday, ihour, imin);
                        struct tm mytm;
                        mytm = UTC2CET (iyear, imon, iday, ihour, imin);
                        if (iyear == today->year && imon == today->mon && iday == today->day)    // that's today
                        {
                            if (imin == 0)
                            {
                                today->hour [mytm.tm_hour].estimatedSolarPower = pv;
                            }
                            else
                            {
                                today->hour [mytm.tm_hour].estimatedSolarPower += pv;
                                today->hour [mytm.tm_hour].estimatedSolarPower /= 2;
                            }
                            //printf ("pv = %f, setting %02d:%02d to %d ", pv, mytm.tm_hour, mytm.tm_min, today->hour [mytm.tm_hour].estimatedSolarPower);
                        }
                    }
                    break;
            }
            item = strtok_r (NULL, ",", &endToken);
            col++;
        }
        //printf ("\n");
        token = strtok_r (NULL, "\n", &endStr);
    }

    free (response);
    return (true);
}

bool    ReadEstimatedSolarPower (day_t* today, int curHour)
{
    bool resultNED;

    if (!(resultNED = ReadEstimatedSolarPowerNED (today)))
    {
        Log ("No NED estimated solar power for %02d-%02d-%04d\n", today->day, today->mon, today->year);
        if (!ReadEstimatedSolarPowerSolcast (today))
        {
            Log ("No Solcast estimated solar power for %02d-%02d-%04d\n", today->day, today->mon, today->year);
            return (false);
        }
    }

    Log ("Found estimated solar power for %02d-%02d-%04d %s\n", today->day, today->mon, today->year, resultNED ? "(using NED.nl)" : "(using solcast.com backup)");
    return (true);
}

bool SetGaragePV (bool PVOn)
{
    char*   response;

    config_t theConfig = GetConfig ();
    if (!theConfig.general.ShellyPMActive)    // skip if no garage power configured
    {
        return (true);
    }

    char url [100];
    snprintf (url, sizeof (url), "http://%s/relay/0?turn=%s", theConfig.general.ShellyPMNetworkName, PVOn ? "on" : "off");
    if ((response = CallURLGetResponse (url, "Accept: application/json", NULL, NULL, NULL, 20L)) == NULL)
    {
        Log ("SetGaragePV: CallURLGetResponse () failed\n");
        return (false);
    }

    if (strlen (response) == 0)
    {
        free (response);
        return (false);
    }
    free (response);

    return (true);
}

// in Watts
short GetGaragePVPower (void)
{
    char*   response;

    config_t theConfig = GetConfig ();
    if (!theConfig.general.ShellyPMActive) // skip if no garage power configured
    {
        return (-1);
    }
    
    char url [100];
    snprintf (url, sizeof (url), "http://%s/rpc/Switch.GetStatus?id=0", theConfig.general.ShellyPMNetworkName);
    if ((response = CallURLGetResponse (url, "Accept: application/json", NULL, NULL, NULL, 20L)) == NULL)
    {
        Log ("GetGaragePVPower: CallURLGetResponse () failed\n");
        return (-1);
    }

    if (strlen (response) == 0)
    {
        free (response);
        return (-1);
    }

    // JSON structure: expected { "voltage":..., "current":..., "apower":..., ... }

    cJSON *root = cJSON_Parse (response);
    if (!root)
    {
        Log ("GetGaragePVPower: JSON parse error\n");
        free (response);
        return (-1);
    }

    // power is in field "apower"
    short power;
    cJSON *apower = cJSON_GetObjectItem (root, "apower");
    if (cJSON_IsNumber (apower))
    {
        power = apower->valuedouble; // in Watts
    }
    else
    {
        Log ("GetGaragePVPower: 'apower' not found in response\n");
        power = -1;
    }

    cJSON_Delete (root);
    free (response);

    return (power);
}

// in Watts
short GetActivePowerP1 (void)
{
    char*   response;

    config_t theConfig = GetConfig ();
    if (!theConfig.general.HomeWizardP1Active) // skip if no P1 dongle configured
    {
        return (0);
    }
    
    char url [100];
    snprintf (url, sizeof (url), "http://%s/api/v1/data", theConfig.general.HomeWizardP1NetworkName);
    if ((response = CallURLGetResponse (url, "Accept: application/json", NULL, NULL, NULL, 20L)) == NULL)
    {
        Log ("GetActivePowerP1: CallURLGetResponse () failed\n");
        return (false);
    }

    if (strlen (response) == 0)
    {
        free (response);
        return (0);
    }

    // JSON structure: expected { "voltage":..., "current":..., "apower":..., ... }

    cJSON *root = cJSON_Parse (response);
    if (!root)
    {
        Log ("GetActivePowerP1: JSON parse error\n");
        free (response);
        return (0);
    }

    // active power is in field "active_power_w"
    short power;
    cJSON *apower = cJSON_GetObjectItem (root, "active_power_w");
    if (cJSON_IsNumber (apower))
    {
        power = apower->valuedouble; // in Watts
    }
    else
    {
        Log ("GetActivePowerP1: 'active_power_w' not found in response\n");
        power = 0;
    }

    cJSON_Delete (root);
    free (response);

    return (power);
}
