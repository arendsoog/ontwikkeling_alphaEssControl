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

// price in EUR/kWh
double  MkReturnPrice (double price)
{
    config_t theConfig = GetConfig ();
    double VAT = (double) theConfig.general.VAT / 100.0;
    return (price * (1.0 + VAT) + theConfig.general.ProviderReturnFee);
}

// price in EUR/kWh
double  MkUsePrice (double price)
{
    config_t theConfig = GetConfig ();
    double VAT = (double) theConfig.general.VAT / 100.0;
    return (price * (1.0 + VAT) + theConfig.general.ProviderUseFee);
}

static  bool ExtractPricesFrankEnergie (char *response, hour_t* hours)
{
    int     year, mon, day, hour, min;

    // Parse JSON
    cJSON *root = cJSON_Parse ((const char*) response);
    if (!root)
    {
        Log ("ExtractPricesFrankEnergie: JSON parse error\n");
        return (false);
    }

    cJSON *errors = cJSON_GetObjectItem (root, "errors");
    if (cJSON_IsArray (errors))
    {
        cJSON_Delete (root);
        return (false);
    }

    // Pad: data → marketPrices → electricityPrices[]
    cJSON *data = cJSON_GetObjectItem (root, "data");
    cJSON *marketPrices = cJSON_GetObjectItem (data, "marketPrices");
    cJSON *electricityPrices = cJSON_GetObjectItem (marketPrices, "electricityPrices");

    if (!cJSON_IsArray (electricityPrices))
    {
        Log ("ExtractPricesFrankEnergie: electricityPrices is not an array\n");
        cJSON_Delete (root);
        return (false);
    }

    int count = cJSON_GetArraySize (electricityPrices);

    //printf ("Number of entries: %d\n\n", count);

    for (int i = 0; i < count; i++)
    {
        cJSON *item = cJSON_GetArrayItem (electricityPrices, i);

        cJSON *from        = cJSON_GetObjectItem (item, "from");
        cJSON *till        = cJSON_GetObjectItem (item, "till");
        cJSON *marketPrice = cJSON_GetObjectItem (item, "marketPrice");
        cJSON *perUnit     = cJSON_GetObjectItem (item, "perUnit");

        if (from && till && marketPrice && perUnit)
        {
            if (parseISO8601 (from->valuestring, &year, &mon, &day, &hour, &min))
            {
                struct tm mytm;
                //printf ("from: %s\n", from->valuestring);
                mytm = UTC2CET (year, mon, day, hour, min);
                //printf ("becomes: %04d-%02d-%02d %02d:%02d\n", mytm.tm_year + 1900, mytm.tm_mon + 1, mytm.tm_mday, mytm.tm_hour, mytm.tm_min);
                if (mytm.tm_hour >= 0 && mytm.tm_hour < MAX_HOURS)
                {
                    hours [mytm.tm_hour].price = marketPrice->valuedouble;
                    hours [mytm.tm_hour].valid = true;
                }
            }
        }
    }

    cJSON_Delete (root);

    //for (int i = 0; i < MAX_HOURS; i ++)
    //{
    //    if (!hours [i].valid)
    //    {
    //        return (false);
    //    }
    //}

    return (true);
}

static  bool ReadPricesFrankEnergie (day_t* today)
{
    char*   response;

    char query [250];
    sprintf (query, "{\"query\":\"query MarketPrices {marketPrices (date: \\\"%04d-%02d-%02d\\\") {electricityPrices {from till marketPrice perUnit}}}\"}", today->year, today->mon, today->day);

    if ((response = CallURLGetResponse ("https://graphql.frankenergie.nl", "Accept: application/json", "Content-Type: application/json", NULL, query, 20L)) == NULL)
    {
        Log ("ReadPricesFrankEnergie: CallURLGetResponse () failed\n");
        return (false);
    }

    if (!ExtractPricesFrankEnergie (response, today->hour))
    {
        free (response);
        return (false);
    }
    free (response);

    return (true);
}

static  bool ExtractPricesEntsoe (char *response, hour_t* hours)
{
    // Parse JSON
    cJSON *root = cJSON_Parse ((const char*) response);
    if (!root)
    {
        Log ("ExtractPricesEntsoe: JSON parse error\n");
        return (false);
    }
    
    cJSON *dto = cJSON_GetObjectItem (root, "queryDataDto");

    cJSON *instancePageInfo = cJSON_GetObjectItem (dto, "instancePageInfo");
    if (!cJSON_IsObject(instancePageInfo))
    {
        Log ("ExtractPricesEntsoe: instancePageInfo missing\n");
        cJSON_Delete (root);
        return (false);
    }
    cJSON *total = cJSON_GetObjectItem (instancePageInfo, "total");
    if (!cJSON_IsNumber (total))
    {
        Log ("ExtractPricesEntsoe: total not a number\n");
        cJSON_Delete (root);
        return (false);
    }
    if (total->valueint == 0)   // no price data available
    {
        cJSON_Delete (root);
        return (false);
    }

    cJSON *list = cJSON_GetObjectItem (dto, "dataItemExchangeList");

    if (!cJSON_IsArray (list))
    {
        Log ("ExtractPricesEntsoe: dataItemExchangeList is no array\n");
        cJSON_Delete (root);
        return (false);
    }

    // Only use ENERGY_PRICES items (usually index 0)
    cJSON *item = cJSON_GetArrayItem (list, 0);

    cJSON *dataArray = cJSON_GetObjectItem (item, "data");
    if (!cJSON_IsArray (dataArray))
    {
        Log ("ExtractPricesEntsoe: data is no array\n");
        cJSON_Delete (root);
        return (false);
    }

    // Eeach entry in dataArray = 1 day
    int numDays = cJSON_GetArraySize (dataArray);

    if (numDays < 1)   // we only read the first day
    {
        Log ("ExtractPricesEntsoe: less than one day\n");
        cJSON_Delete (root);
        return (false);
    }

    int year, mon, day, hour, min;

    cJSON *dayItem = cJSON_GetArrayItem (dataArray, 0);
    cJSON *curveData = cJSON_GetObjectItem (dayItem, "curveData");
    cJSON *periodList = cJSON_GetObjectItem (curveData, "periodList");
    cJSON *period0 = cJSON_GetArrayItem (periodList, 0);

    if (period0)
    {
        cJSON *periodTime = cJSON_GetObjectItem (period0, "timeInterval");
        if (periodTime)
        {
            cJSON *from = cJSON_GetObjectItem (periodTime, "from");
            if (cJSON_IsString (from))
            {
                //printf ("from: %s\n", from->valuestring);
                if (parseISO8601 (from->valuestring, &year, &mon, &day, &hour, &min))
                {
                    struct tm   mytm;
                    time_t      myTime;
                    mytm = UTC2CET (year, mon, day, hour, min);
                    //printf ("becomes: %04d-%02d-%02d %02d:%02d\n", mytm.tm_year + 1900, mytm.tm_mon + 1, mytm.tm_mday, mytm.tm_hour, mytm.tm_min);
                    if (mytm.tm_hour != 0)  // first hour should be zero
                    {
                        Log ("ExtractPricesEntsoe: first hour non-zero\n");
                        cJSON_Delete (root);
                        return (false);
                    }
                }
            }
        }

        cJSON *pointMap = cJSON_GetObjectItem (period0, "pointMap");

        // Array with 24 x 4 = 96 quarter prices
        struct
        {
            bool    inResponse;
            double  price;
        } q [MAX_HOURS * 4] = {0};

        // First read all indexes
        cJSON *pmEntry = NULL;
        cJSON_ArrayForEach (pmEntry, pointMap)
        {
            int idx = atoi (pmEntry->string); // "0" -> 0

            if (idx >= 0 && idx < (MAX_HOURS * 4))
            {
                cJSON *valueArray = pmEntry->child;

                if (valueArray && cJSON_IsNumber (valueArray))
                {
                    q [idx].inResponse = true;
                    q [idx].price = valueArray->valuedouble;
                }
            }
        }
        if (!q [0].inResponse)
        {
            Log ("ExtractPricesEntsoe: first price is missing\n");
            cJSON_Delete (root);
            return (false);
        }

        double  lastValidPrice = 0.0;
        for (int i = 0; i < MAX_HOURS * 4; i++)
        {
            if (q [i].inResponse)
            {
                lastValidPrice = q [i].price;
            }
            else
            {
                q [i].price = lastValidPrice;
            }
        }

        for (int h = 0; h < MAX_HOURS; h ++)    // invalidate hours
        {
            hours [h].valid = false;
        }
        // calculate hour prices
        for (int h = 0; h < MAX_HOURS; h++)
        {
            struct tm   mytm;
            mytm = UTC2CET (year, mon, day, hour + h, 0);
            int hCET = mytm.tm_hour;

            if (!hours [hCET].valid)    // avoid override on DST change day
            {
                int base = h * 4;
                hours [hCET].price = (q [base].price + q [base + 1].price + q [base + 2].price + q [base + 3].price) / 4.0 / 1000;  // eur/MWh to eur/kWh
                hours [hCET].valid = true;
                //printf ("%02d:00: %f ", hCET, hours [hCET].price);
            }
        }
    }

    cJSON_Delete (root);

    //for (int i = 0; i < MAX_HOURS; i ++)
    //{
    //    if (!hours [i].valid)
    //    {
    //        return (false);
    //    }
    //}
    
    return (true);
}

static  bool ReadPricesEntsoe (day_t* today)
{
    char*   response;
    int     i;

    config_t theConfig = GetConfig ();
    
    char url [600];
    snprintf (url, sizeof (url),
            "https://web-api.tp.entsoe.eu/api?documentType=A44&in_Domain=10YNL----------L&out_Domain=10YNL----------L"
            "&periodStart=%04d%02d%02d0000&periodEnd=%04d%02d%02d2359&securityToken=%s",
             today->year, today->mon, today->day, today->year, today->mon, today->day, theConfig.general.EntsoeAPIToken);

    if ((response = CallURLGetResponse (url, "Accept: application/json", NULL, NULL, NULL, 200L)) == NULL)
    {
        Log ("ReadPricesEntsoe: CallURLGetResponse () failed\n");
        return (false);
    }

    if (!ExtractPricesEntsoe (response, today->hour))
    {
        free (response);
        return (false);
    }
    free (response);

    return (true);
}

static  void    ReadPricesAfterMath (day_t *today)
{
    int     i;

    for (i = 0; i < MAX_HOURS; i++)
    {
        double profitOnReturn = MkReturnPrice (today->hour [i].price);
        double costOnUse = MkUsePrice (today->hour [i].price);

        //printf ("%02d: profitOnReturn: %.5f, costOnUse: %.5f\n", i, profitOnReturn, costOnUse);
        if (profitOnReturn > 0 && costOnUse < 0)
        {
            if (profitOnReturn >= (0 - costOnUse))  // choose highest, preferring profitOnReturn
            {
                costOnUse = 0;
            }
            else
            {
                profitOnReturn = 0;
            }
        }

        if (profitOnReturn > 0 || (profitOnReturn == 0 && costOnUse >= 0))   // above, it is lucrative to feed-in to the grid. In case of 0 we choose for EARNING_ON_RETURN for positive 'saldering' balance
        {
            //printf("totalPrice: %.5f + return fee: %.5f = %.5f -> setting EARNING_ON_RETURN\n", totalPrice, PROVIDER_RETURN_FEE, totalPrice + PROVIDER_RETURN_FEE);
            today->hour [i].earning = EARNING_ON_RETURN;
        }
        if (costOnUse < 0)  // below, it is lucrative to load from grid
        {
            //printf("totalPrice: %.5f + use fee: %.5f = %.5f -> setting EARNING_ON_USE\n", totalPrice, PROVIDER_USE_FEE, totalPrice + PROVIDER_USE_FEE);
            today->hour [i].earning = EARNING_ON_USE;
        }
    }

    today->earningOnReturnAllDay = true;
    double  lowest = 1000, highest = -1000;
    for (i = 0; i < MAX_HOURS; i++)
    {
        if (today->hour [i].earning != EARNING_ON_RETURN)
        {
            today->earningOnReturnAllDay = false;
        }
        if (today->hour [i].price < lowest)
        {
            lowest = today->hour [i].price;
            today->indexLowest = i;
        }
        if (today->hour [i].price > highest)
        {
            highest = today->hour [i].price;
            today->indexHighest = i;
        }
    }
    today->hour [today->indexLowest].lowest = true;
    today->hour [today->indexHighest].highest = true;
}

bool    ReadPrices (day_t* today)
{
    bool    resultEntsoe;

    if (!(resultEntsoe = ReadPricesEntsoe (today)))
    {
        Log ("No Entsoe prices for %02d-%02d-%04d\n", today->day, today->mon, today->year);
        if (!ReadPricesFrankEnergie (today))
        {
            Log ("No Frank Energie prices for %02d-%02d-%04d\n", today->day, today->mon, today->year);
            return (false);
        }
    }
    
    ReadPricesAfterMath (today);
    Log ("Found prices for %02d-%02d-%04d %s\n", today->day, today->mon, today->year, resultEntsoe ? "(using Entsoe.eu)" : "(using Frank Energie backup)");
    
    return (true);
}