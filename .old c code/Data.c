#include <stdio.h>
#include <errno.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <db.h>
#include <math.h>
#include "AlphaESSControl.h"
#include "Data.h"
#include "Util.h"

#define MAX_MONTHS      12

typedef struct
{
    /* accumulation phase */
    double  sumW;               // sum of weights
    double  houseLoadSum;       // sum(w * x)
    double  houseLoadSum2;      // sum(w * x^2)

    /* final results */
    double  houseLoad;          // weighted mean
    double  houseLoadSigma;     // sqrt(weighted variance)

    double  solarPowerRoof;     // weighted mean
    double  solarPowerGarage;   // weighted mean
} homeEnergyDataMeanHour_t;

typedef struct
{
    double sumW;     // Σ(w)
    double sumWX;    // Σ(w*x)
    double sumWY;    // Σ(w*y)
    double sumWXX;   // Σ(w*x^2)
    double sumWXY;   // Σ(w*x*y)
    double weightedCount;
    double factor;
    double offset;
} solarRegressionHour_t;

typedef struct
{
    struct
    {
        bool    valid;
        struct
        {
            bool                        valid;
            homeEnergyDataMeanHour_t    hour [MAX_HOURS];
        } weekDay [MAX_WEEK_DAYS];
    } month [MAX_MONTHS];
    solarRegressionHour_t   solarRoofRegression [MAX_HOURS];
    solarRegressionHour_t   solarGarageRegression [MAX_HOURS];
} homeEnergyDataMean_t;

static  bool    HandlePowerDB (bool doStore, homeEnergyData_t* qData)
{
    DB *dbp;
    DBT key, data;
    int ret;

    // Open or create database
    if ((ret = db_create (&dbp, NULL, 0)) != 0)
    {
        TimeLog ("db_create error: %s\n", db_strerror (ret));
        return (false);
    }

    ret = dbp->open (dbp, NULL, "../data/PowerData.db", NULL, DB_BTREE, DB_CREATE, 0);
    if (ret != 0)
    {
        TimeLog ("db->open error: %s\n", db_strerror (ret));
        return (false);
    }
    
    // Make key
    char keyBuf [20];
    sprintf (keyBuf, "%04hd%02hd%02hd%02hd", qData->year, qData->mon, qData->day, qData->hour);

    memset (&key, 0, sizeof (DBT));
    memset (&data, 0, sizeof (DBT));

    key.data = keyBuf;
    key.size = strlen (keyBuf) + 1;

    if (doStore)
    {
        data.data = qData;
        data.size = sizeof (homeEnergyData_t);

        if ((ret = dbp->put (dbp, NULL, &key, &data, 0)) != 0)
        {
            TimeLog ("put-error: %s\n", db_strerror (ret));
            dbp->close (dbp, 0);
            return (false);
        }
    }
    else
    {
        if ((ret = dbp->get (dbp, NULL, &key, &data, 0)) != 0)
        {
            TimeLog ("get-error: %s\n", db_strerror (ret));
            dbp->close (dbp, 0);
            return (false);
        }
        
        *qData = *((homeEnergyData_t*) data.data);
    }
    
    dbp->close (dbp, 0);
    return (true);
}

bool    StoreHourData (day_t theDay, int hour)
{
    homeEnergyData_t    qData;
    hour_t              theHour;

    if (!theDay.valid || hour < 0 || hour >= MAX_HOURS)
    {
        return (false);
    }
    theHour = theDay.hour [hour];
    if (!theHour.valid)
    {
        return (false);
    }

    // version 1.1.1: in case of earning on use, AlphaESSControl turns off solar panels.
    // To avoid regression miscalculation, set all solar numbers to zero in that case
    if (theHour.earning == EARNING_ON_USE)
    {
        theHour.estimatedSolarPercentageNED = 0;
        theHour.realSolarPowerRoof = 0;
        theHour.realSolarPowerGarage = 0;
    }
    config_t theConfig = GetConfig ();

    qData.year = theDay.year;
    qData.mon = theDay.mon;
    qData.day = theDay.day;
    qData.hour = hour;
    qData.min = 0;
    qData.houseLoad = theHour.realHouseLoad;
    qData.estimatedSolarPercentageNED = theHour.estimatedSolarPercentageNED;
    qData.solarPowerRoof = theHour.realSolarPowerRoof;
    qData.solarPowerGarage = theHour.realSolarPowerGarage;
    qData.feedIn = theHour.totalActivePower;
    qData.price = theHour.price;
    qData.useFee = theConfig.general.ProviderUseFee;
    qData.returnFee = theConfig.general.ProviderReturnFee;

    //printf ("TESTING: Storing hour data %02d:00: houseLoad %d, estSolarPercentage %f solarPowerRoof %d, solarPowerGarage %d, feedIn %d, price %f, useFee %f, returnFee %f\n",
    //        qData.hour, qData.houseLoad, qData.estimatedSolarPercentageNED, qData.solarPowerRoof, qData.solarPowerGarage, qData.feedIn, qData.price, qData.useFee, qData.returnFee);

    if (qData.houseLoad == 0)
    {
        //TimeLog ("StoreHourData: houseLoad equals 0, skipped\n");
        return (true);
    }

    return (HandlePowerDB (true, &qData));
}

bool    RetrieveHourData (homeEnergyData_t* qData)
{
    if (qData->year < 2025 || qData->mon < 1 || qData->mon > MAX_MONTHS || qData->day < 1 || qData->day > 31 || qData->hour < 0 || qData->hour >= MAX_HOURS)
    {
        return (false);
    }

    return (HandlePowerDB (false, qData));
}

static  bool    HandleMeanPowerDB (bool doStore, homeEnergyDataMean_t* qData)
{
    DB *dbp;
    DBT key, data;
    int ret;

    // Open or create database
    if ((ret = db_create (&dbp, NULL, 0)) != 0)
    {
        TimeLog ("db_create error: %s\n", db_strerror (ret));
        return (false);
    }

    ret = dbp->open (dbp, NULL, "../data/PowerDataMean.db", NULL, DB_BTREE, DB_CREATE, 0);
    if (ret != 0)
    {
        TimeLog ((char*) "db->open error: %s\n", db_strerror (ret));
        return (false);
    }
    
    // Make key
    char keyBuf [20];
    sprintf (keyBuf, "MEANDATA");

    memset (&key, 0, sizeof (DBT));
    memset (&data, 0, sizeof (DBT));

    key.data = keyBuf;
    key.size = strlen (keyBuf) + 1;

    if (doStore)
    {
        data.data = qData;
        data.size = sizeof (homeEnergyDataMean_t);

        if ((ret = dbp->put (dbp, NULL, &key, &data, 0)) != 0)
        {
            TimeLog ("put-error: %s\n", db_strerror (ret));
            dbp->close (dbp, 0);
            return (false);
        }
    }
    else
    {
        if ((ret = dbp->get (dbp, NULL, &key, &data, 0)) != 0)
        {
            TimeLog ("get-error: %s\n", db_strerror (ret));
            dbp->close (dbp, 0);
            return (false);
        }
        
        *qData = *((homeEnergyDataMean_t*) data.data);
    }
    
    dbp->close (dbp, 0);
    return (true);
}

bool    RetrieveMeanData (int month, int wday, homeEnergyWeekDayMean_t* theData)
{
    homeEnergyDataMean_t    mData;

    if (month < 1 || month > MAX_MONTHS || wday < 0 || wday >= MAX_WEEK_DAYS)
    {
        return (false);
    }

    if (!HandleMeanPowerDB (false, &mData))
    {
        return (false);
    }

    for (int hour = 0; hour < MAX_HOURS; hour ++)
    {
        theData->hour [hour].houseLoad = mData.month [month - 1].weekDay [wday].hour [hour].houseLoad;
        theData->hour [hour].houseLoadSigma = mData.month [month - 1].weekDay [wday].hour [hour].houseLoadSigma;
        theData->hour [hour].solarPowerRoof = mData.month [month - 1].weekDay [wday].hour [hour].solarPowerRoof;
        theData->hour [hour].solarPowerGarage = mData.month [month - 1].weekDay [wday].hour [hour].solarPowerGarage;
        theData->hour [hour].estimatedSolarPowerRoofCorrectionFactor = mData.solarRoofRegression [hour].factor;
        theData->hour [hour].estimatedSolarPowerRoofCorrectionOffset = mData.solarRoofRegression [hour].offset;
        theData->hour [hour].estimatedSolarPowerGarageCorrectionFactor = mData.solarGarageRegression [hour].factor;
        theData->hour [hour].estimatedSolarPowerGarageCorrectionOffset = mData.solarGarageRegression [hour].offset;
    }
    return (true);
}

// recency tuning (in days)
#define LOAD_TAU_DAYS       21.0    // tunable: ~3 weeks memory

/* Other tuning parameters */
#define PRIOR_W             3.0     /* prior weight for shrinkage towards fallback */
#define DEFAULT_SIGMA_REL   0.25  /* fallback relative sigma when no variance info */
#define SMOOTH_KERNEL       1       /* if 1 apply 3-point smoothing across hours, else skip */

/* weekday classification
 * tm_wday: 0=Sun ... 6=Sat
 * return 0 = weekday (Mon-Fri), 1 = weekend (Sat-Sun)
 */
static inline int GetDayClass (int wday)
{
    /* 0 = weekday (Mon–Fri), 1 = weekend (Sat–Sun) */
    return ((wday == 0 || wday == 6) ? 1 : 0);
}

/* Exponential recency weight using LOAD_TAU_DAYS */
static double RecencyWeight (time_t now, time_t sample)
{
    double days = difftime (now, sample) / (24.0 * 3600.0);
    if (days < 0)
    {
        days = 0;
    }
    return (exp (-days / LOAD_TAU_DAYS));
}

/* Helper: check whether a month has at least one weekday or weekend partial sequence.
 * Returns true if the month contains at least one weekday (Mon-Fri) or one weekend day (Sat/Sun).
 */
static bool HasPartialWeekdayOrWeekend (int mon, homeEnergyDataMean_t* theData)
{
    bool hasWeekday = false;
    bool hasWeekend = false;

    /* Weekday: Mon..Fri -> tm_wday 1..5 */
    for (int wd = 1; wd <= 5; wd++)
    {
        if (theData->month[mon].weekDay[wd].valid)
        {
            hasWeekday = true;
            break;
        }
    }

    /* Weekend: Sat(6), Sun(0) */
    if (theData->month[mon].weekDay[6].valid || theData->month[mon].weekDay[0].valid)
    {
        hasWeekend = true;
    }

    return (hasWeekday || hasWeekend);
}

/* fills missing weekdays in a given month by copying from best matching existing weekday */
static void FillMonthFallback (int mon, homeEnergyDataMean_t* theData)
{
    for (int wday = 0; wday < MAX_WEEK_DAYS; wday++)
    {
        if (theData->month[mon].weekDay[wday].valid)
        {
            continue;
        }

        Log("Month %d: forward fallback filling for weekday %d\n", mon, wday);

        int targetClass = GetDayClass(wday);
        int baseMon = -1;
        int baseWday = -1;

        /* 1) Same month, same day class */
        for (int wd = 0; wd < MAX_WEEK_DAYS; wd++)
        {
            if (!theData->month[mon].weekDay[wd].valid)
            {
                continue;
            }
            if (GetDayClass(wd) != targetClass)
            {
                continue;
            }
            baseMon = mon;
            baseWday = wd;
            break;
        }

        /* 2) Same month, any day class */
        if (baseWday < 0)
        {
            for (int wd = 0; wd < MAX_WEEK_DAYS; wd++)
            {
                if (theData->month[mon].weekDay[wd].valid)
                {
                    baseMon = mon;
                    baseWday = wd;
                    break;
                }
            }
        }

        /* 3) Other months (already partially processed), same day class */
        if (baseWday < 0)
        {
            for (int m2 = 0; m2 < MAX_MONTHS; m2++)
            {
                if (m2 == mon)
                    continue;
                for (int wd = 0; wd < MAX_WEEK_DAYS; wd++)
                {
                    if (!theData->month[m2].weekDay[wd].valid)
                    {
                        continue;
                    }
                    if (GetDayClass(wd) != targetClass)
                    {
                        continue;
                    }
                    baseMon = m2;
                    baseWday = wd;
                    break;
                }
                if (baseWday >= 0)
                {
                    break;
                }
            }
        }

        /* 4) Other months, any day class */
        if (baseWday < 0)
        {
            for (int m2 = 0; m2 < MAX_MONTHS; m2++)
            {
                for (int wd = 0; wd < MAX_WEEK_DAYS; wd++)
                {
                    if (theData->month[m2].weekDay[wd].valid)
                    {
                        baseMon = m2;
                        baseWday = wd;
                        break;
                    }
                }
                if (baseWday >= 0)
                {
                    break;
                }
            }
        }

        /* Apply fallback by copying per-hour data */
        if (baseWday >= 0)
        {
            for (int hour = 0; hour < MAX_HOURS; hour++)
            {
                theData->month[mon].weekDay[wday].hour[hour] =
                    theData->month[baseMon].weekDay[baseWday].hour[hour];
            }
            theData->month[mon].weekDay[wday].valid = true;
        }
    }
}

/* Helper: try a candidate (cm, cwd) for a given hour; plain C version (no lambda).
 * Returns true if candidate has data and fills mean_out & sigma_out.
 */
static bool TryCandidate (const homeEnergyDataMean_t* theData, int cm, int cwd, int hour, double* mean_out, double* sigma_out)
{
    if (!theData->month [cm].weekDay [cwd].valid)
    {
        return (false);
    }

    const homeEnergyDataMeanHour_t* ch = &theData->month [cm].weekDay [cwd].hour [hour];
    if (ch->sumW > 0.0)
    {
        double mean = ch->houseLoadSum / ch->sumW;
        double var = (ch->houseLoadSum2 / ch->sumW) - (mean * mean);
        if (var < 0.0)
        {
            var = 0.0;
        }
        double sigma = sqrt (var);
        /* If computed sigma is zero (single value), provide a small relative sigma */
        if (sigma <= 0.0)
        {
            sigma = fabs (mean) * DEFAULT_SIGMA_REL + 0.1; /* avoid zero uncertainty */
        }
        *mean_out = mean;
        *sigma_out = sigma;
        return (true);
    }
    return (false);
}

/* Find a fallback mean & sigma for a given hour using prioritized search:
 * 1) same month, same dayclass, same hour
 * 2) same month, any weekday, same hour
 * 3) other months, same dayclass, same hour
 * 4) baseline month, same hour (baseline month must be provided)
 *
 * Returns true if found and sets *mean_out and *sigma_out.
 * If not found, returns false.
 */
static bool FindFallbackHourMean (const homeEnergyDataMean_t* theData, int mon, int wday, int hour, int bestMonth, double* mean_out, double* sigma_out)
{
    int targetClass = GetDayClass (wday);

    /* 1) same month, same dayclass, same hour */
    for (int wd = 0; wd < MAX_WEEK_DAYS; wd++)
    {
        if (!theData->month [mon].weekDay [wd].valid)
        {
            continue;
        }
        if (GetDayClass (wd) != targetClass)
        {
            continue;
        }
        if (TryCandidate (theData, mon, wd, hour, mean_out, sigma_out))
        {
            return (true);
        }
    }

    /* 2) same month, any dayclass */
    for (int wd = 0; wd < MAX_WEEK_DAYS; wd++)
    {
        if (!theData->month [mon].weekDay [wd].valid)
        {
            continue;
        }
        if (TryCandidate (theData, mon, wd, hour, mean_out, sigma_out))
        {
            return (true);
        }
    }

    /* 3) other months, same dayclass */
    for (int m2 = 0; m2 < MAX_MONTHS; m2++)
    {
        if (m2 == mon)
        {
            continue;
        }
        for (int wd = 0; wd < MAX_WEEK_DAYS; wd++)
        {
            if (!theData->month [m2].weekDay [wd].valid)
            {
                continue;
            }
            if (GetDayClass (wd) != targetClass)
            {
                continue;
            }
            if (TryCandidate (theData, m2, wd, hour, mean_out, sigma_out))
            {
                return (true);
            }
        }
    }

    /* 4) baseline month, same hour */
    if (bestMonth >= 0)
    {
        for (int wd = 0; wd < MAX_WEEK_DAYS; wd++)
        {
            if (!theData->month [bestMonth].weekDay [wd].valid)
            {
                continue;
            }
            if (TryCandidate (theData, bestMonth, wd, hour, mean_out, sigma_out))
            {
                return (true);
            }
        }
    }

    return (false);
}

#define SOLAR_TAU_DAYS  28.0

static double SolarRecencyWeight (time_t now, time_t sample)
{
    double days = difftime (now, sample) / (24.0 * 3600.0);
    if (days < 0)
    {
        days = 0;
    }

    /* Pure exponential decay: recent data dominates, old data fades out */
    return exp (-days / SOLAR_TAU_DAYS);
}

/* Accumulate weighted regression data per hour for solar production.
   Intercept model: y = a*x + b
   - x = expected power (Wp * NED fraction)
   - y = measured power
   - Filter on measured power (NOT expected power) to allow NED=0 learning */
static void CountRegressionData (homeEnergyData_t* hData, double wSolar, homeEnergyDataMean_t* theData)
{
    config_t theConfig = GetConfig ();
    double estFraction = hData->estimatedSolarPercentageNED;

    /* ===== ROOF ===== */
    double x = estFraction * theConfig.general.AlphaESSPVPower;   // expected power (W)
    double y = hData->solarPowerRoof;   // measured power (W)

    /* Filter noise: 2% of installed power (~146W for 7310Wp) */
    double minY = fmax (MIN_EXPECTED_SOLAR_POWER, 0.02 * theConfig.general.AlphaESSPVPower);

    if (y > minY)
    {
        solarRegressionHour_t* r = &theData->solarRoofRegression [hData->hour];

        /* Full WLS accumulators for intercept regression */
        r->sumW   += wSolar;
        r->sumWX  += wSolar * x;
        r->sumWY  += wSolar * y;
        r->sumWXX += wSolar * x * x;
        r->sumWXY += wSolar * x * y;
        r->weightedCount += wSolar;
    }

    /* ===== GARAGE ===== */
    x = estFraction * theConfig.general.ShellyPMPVPower;
    y = hData->solarPowerGarage;

    /* ~67W for 3330Wp */
    minY = fmax (MIN_EXPECTED_SOLAR_POWER, 0.02 * theConfig.general.ShellyPMPVPower);

    if (y > minY)
    {
        solarRegressionHour_t* r = &theData->solarGarageRegression [hData->hour];

        r->sumW   += wSolar;
        r->sumWX  += wSolar * x;
        r->sumWY  += wSolar * y;
        r->sumWXX += wSolar * x * x;
        r->sumWXY += wSolar * x * y;
        r->weightedCount += wSolar;
    }
}

/* Minimum reliability thresholds for regression factors */
#define MIN_SAMPLES        4.0
#define MIN_ENERGY_RATIO   0.15   // MUCH better than 2.5*WP^2

/* Check if the regression factor is statistically reliable.
   Uses weighted sample count and total signal energy (sumWXX). */
static  bool    IsFactorReliable (double weightedCount, double sumWXX, double factor, double WP)
{
    /* 1. Enough effective samples */
    if (weightedCount < MIN_SAMPLES)
    {
        //Log ("weightedCount not reliable: %f\n", weightedCount);
        return (false);
    }

    /* 2. Enough signal energy related to system capacity */
    double energyRatio = sumWXX / (WP * WP);
    if (energyRatio < MIN_ENERGY_RATIO)
    {
        //Log("energyRatio too low: %f\n", energyRatio);
        return (false);
    }

    /* 3. sanity check */
    if (factor < 0.2 || factor > 2.5)
    {
        //Log ("factor out of bounds: %f\n", factor);
        return (false);
    }

    return (true);
}

/* Compute hourly regression factors using intercept WLS: y = a*x + b */
static void CalculateRegression (homeEnergyDataMean_t* theData)
{
    config_t theConfig = GetConfig ();
    
    for (int hour = 0; hour < MAX_HOURS; hour++)
    {
        /* ===== ROOF ===== */
        solarRegressionHour_t* r = &theData->solarRoofRegression [hour];

        if (r->sumW > 0.0)
        {
            /* Solve weighted least squares with intercept */
            double denom = (r->sumW * r->sumWXX) - (r->sumWX * r->sumWX);

            if (fabs (denom) > 1e-9)
            {
                double a = ((r->sumW * r->sumWXY) - (r->sumWX * r->sumWY)) / denom;
                double b = ((r->sumWXX * r->sumWY) - (r->sumWX * r->sumWXY)) / denom;

                /* Clamp to physically realistic bounds */
                if (a < 0.1) a = 0.1;
                if (a > 2.5) a = 2.5;

                /* Intercept cannot exceed diffuse physical limits (~20% WP) */
                if (b < 0.0) b = 0.0;
                if (b > 0.20 * theConfig.general.AlphaESSPVPower) b = 0.20 * theConfig.general.AlphaESSPVPower;

                r->factor  = a;  // scale correction vs NED
                r->offset  = b;  // diffuse baseline
            }
            else
            {
                r->factor = 1.0;
                r->offset = 0.0;
            }
        }
        else
        {
            r->factor = 1.0;
            r->offset = 0.0;
        }

        if (!IsFactorReliable (r->weightedCount, r->sumWXX, r->factor, theConfig.general.AlphaESSPVPower))
        {
            r->factor = -1.0;
        }

        /* ===== GARAGE (same logic) ===== */
        r = &theData->solarGarageRegression [hour];

        if (r->sumW > 0.0)
        {
            double denom = (r->sumW * r->sumWXX) - (r->sumWX * r->sumWX);

            if (fabs (denom) > 1e-9)
            {
                double a = ((r->sumW * r->sumWXY) - (r->sumWX * r->sumWY)) / denom;
                double b = ((r->sumWXX * r->sumWY) - (r->sumWX * r->sumWXY)) / denom;

                if (a < 0.1) a = 0.1;
                if (a > 2.5) a = 2.5;

                if (b < 0.0) b = 0.0;
                if (b > 0.20 * theConfig.general.ShellyPMPVPower) b = 0.20 * theConfig.general.ShellyPMPVPower;

                r->factor = a;
                r->offset = b;
            }
            else
            {
                r->factor = 1.0;
                r->offset = 0.0;
            }
        }
        else
        {
            r->factor = 1.0;
            r->offset = 0.0;
        }

        if (!IsFactorReliable (r->weightedCount, r->sumWXX, r->factor, theConfig.general.ShellyPMPVPower))
        {
            r->factor = -1.0;
        }

        if (theData->solarRoofRegression [hour].factor >= 0.0 || theData->solarGarageRegression [hour].factor >= 0.0)
        {
            Log ("%02d:00 -> solar correction roof factor: %f offset: %f, garage factor: %f offset: %f\n",
                hour,
                theData->solarRoofRegression[hour].factor, theData->solarRoofRegression[hour].offset,
                theData->solarGarageRegression[hour].factor, theData->solarGarageRegression[hour].offset);
        }
    }
}

static  void    CountHomeLoadData (int mon, int wday, int hour, double w, homeEnergyData_t* hData, homeEnergyDataMean_t* theData)
{
    // mon  = thetm.tm_mon;   /* 0..11 after localtime_r */
    // wday = thetm.tm_wday;  /* 0..6 */
    // hour = thetm.tm_hour;  /* 0..23 */

    /* Mark month and weekday as present (final validation will decide completeness) */
    theData->month [mon].valid = true;
    theData->month [mon].weekDay[wday].valid = true;

    /* Weighted mean accumulation for this hour */
    homeEnergyDataMeanHour_t* h = &theData->month [mon].weekDay [wday].hour [hour];

    h->sumW += w;

    h->houseLoadSum  += w * hData->houseLoad;
    h->houseLoadSum2 += w * hData->houseLoad * hData->houseLoad;

    h->solarPowerRoof   += w * hData->solarPowerRoof;
    h->solarPowerGarage += w * hData->solarPowerGarage;
}

/* Calculate and store mean data
 * This reads records from the PowerData DB, applies recency weighting, computes weighted means
 * and variances per month/weekDay/hour, and then fills missing hours using per-hour fallback,
 * shrinkage toward fallback for small sample weights, and optional smoothing across hours.
 */
bool    CalculateAndStoreMeanData (void)
{
    DB*                     dbp = NULL;
    DBC*                    cursor = NULL;
    DBT                     key, data;
    int                     ret;
    homeEnergyDataMean_t    theData;
    homeEnergyData_t*       hData;

    time_t now = time (NULL);

    /* Clear data structure and DBTs */
    memset (&theData, 0, sizeof (theData));
    memset (&key, 0, sizeof (DBT));
    memset (&data, 0, sizeof (DBT));

    for (int hour = 0; hour < MAX_HOURS; hour++)    // set invalid data
    {
        theData.solarRoofRegression [hour].factor = -1.0;
        theData.solarGarageRegression [hour].factor = -1.0;
    }

    /* Open PowerData database */
    if ((ret = db_create (&dbp, NULL, 0)) != 0)
    {
        TimeLog("CalculateAndStoreMeanData: db_create error: %s\n", db_strerror (ret));
        return (false);
    }

    ret = dbp->open(dbp, NULL, "../data/PowerData.db", NULL, DB_BTREE, 0, 0);
    if (ret != 0)
    {
        TimeLog ("CalculateAndStoreMeanData: db->open error: %s\n", db_strerror (ret));
        dbp->close (dbp, 0);
        return (false);
    }

    if ((ret = dbp->cursor(dbp, NULL, &cursor, 0)) != 0)
    {
        TimeLog ("CalculateAndStoreMeanData: cursor creation failed: %s\n", db_strerror (ret));
        dbp->close (dbp, 0);
        return (false);
    }

    /* Position cursor at first record */
    if ((ret = cursor->get (cursor, &key, &data, DB_FIRST)) == DB_NOTFOUND)
    {
        cursor->close (cursor);
        dbp->close (dbp, 0);
        return (false);
    }

    /* Accumulate weighted sums */
    int count = 0;
    while (ret == 0)
    {
        count++;

        hData = (homeEnergyData_t*) data.data;

        struct tm thetm = {0};
        thetm.tm_year = hData->year - 1900;
        thetm.tm_mon  = hData->mon  - 1;
        thetm.tm_mday = hData->day;
        thetm.tm_hour = hData->hour;
        thetm.tm_isdst = -1;

        /* Convert sample timestamp to time_t */
        time_t sampleTime = mktime(&thetm);
        if (sampleTime == (time_t)-1)
        {
            ret = cursor->get(cursor, &key, &data, DB_NEXT);
            continue;
        }

        /* Normalize to local broken-down time (preserve original behavior) */
        localtime_r (&sampleTime, &thetm);

        /* calculate solarPowerFactors */
        double wSolar = SolarRecencyWeight (now, sampleTime);
        const double MIN_SOLAR_WEIGHT = 0.2;
        wSolar = fmax (MIN_SOLAR_WEIGHT, wSolar);   // Extra safety floor. Stabilizes regression on low irrediation (supports long term bias)
        CountRegressionData (hData, wSolar, &theData);

        /* Recency-based weighting of historical samples */
        double w = RecencyWeight (now, sampleTime);
        CountHomeLoadData (thetm.tm_mon, thetm.tm_wday, thetm.tm_hour, w, hData, &theData);

        ret = cursor->get (cursor, &key, &data, DB_NEXT);
    }

    cursor->close (cursor);
    dbp->close (dbp, 0);

    Log("Found %d PowerDB records\n", count);

    CalculateRegression (&theData);

    /* STEP: Determine baseline month (bestMonth) BEFORE finalization so per-hour fallback can use it.
     * Score months by number of valid weekdays and number of hours with sumW > 0.
     */
    int bestMonth = -1;
    int bestScore = -1;

    for (int mon = 0; mon < MAX_MONTHS; mon++)
    {
        int validDays = 0;
        int validHours = 0;

        for (int wd = 0; wd < MAX_WEEK_DAYS; wd++)
        {
            if (!theData.month [mon].weekDay [wd].valid)
            {
                continue;
            }

            validDays++;

            for (int hour = 0; hour < MAX_HOURS; hour++)
            {
                if (theData.month [mon].weekDay [wd].hour [hour].sumW > 0.0)
                {
                    validHours++;
                }
            }
        }

        /* Prefer months with more complete weekday/weekend coverage */
        int score = validDays * 24 + validHours;    /* 1 valid weekday weighs as 24 valid hours */
        if (score > bestScore)
        {
            bestScore = score;
            bestMonth = mon;
        }
    }

    if (bestMonth < 0)
    {
        Log ("No valid weekdays found. Not storing result\n");
        return (false);
    }

    Log ("Month %d: is baseline month (score=%d)\n", bestMonth, bestScore);

    /*
     * Finalize means and uncertainty with per-hour fallback, shrinkage toward fallback, and smoothing.
     *
     * Strategy:
     * - For each hour with sumW > 0: compute obs_mean and obs_var
     *   - Find fallback_mean & fallback_sigma
     *   - Shrink mean towards fallback using PRIOR_W:
     *       mean_final = (sumW * obs_mean + PRIOR_W * fallback_mean) / (sumW + PRIOR_W)
     *   - Combine variances similarly (simple weighted avg of variances)
     * - For each hour with sumW == 0:
     *   - Use fallback_mean directly (if found); otherwise leave zero and log
     * - After all hours are processed for a weekday, apply optional smoothing kernel across hours
     */

    for (int mon = 0; mon < MAX_MONTHS; mon++)
    {
        for (int wday = 0; wday < MAX_WEEK_DAYS; wday++)
        {
            /* skip weekdays that were never touched */
            if (!theData.month [mon].weekDay [wday].valid)
            {
                continue;
            }

            /* first pass: compute finalized mean & sigma for each hour */
            for (int hour = 0; hour < MAX_HOURS; hour++)
            {
                homeEnergyDataMeanHour_t* h = &theData.month [mon].weekDay [wday].hour [hour];

                /* Try to find fallback mean & sigma for this hour */
                double fallback_mean = 0.0;
                double fallback_sigma = 0.0;
                bool fallback_found = FindFallbackHourMean (&theData, mon, wday, hour, bestMonth, &fallback_mean, &fallback_sigma);

                if (h->sumW > 0.0)
                {
                    /* observed mean & variance */
                    double obs_mean = h->houseLoadSum / h->sumW;
                    double obs_ex2 = h->houseLoadSum2 / h->sumW;
                    double obs_var = obs_ex2 - obs_mean * obs_mean;
                    if (obs_var < 0.0)
                    {
                        obs_var = 0.0;
                    }
                    double obs_sigma = sqrt (obs_var);

                    if (!fallback_found)
                    {
                        /* No fallback available: keep observed mean, but ensure nonzero sigma */
                        h->houseLoad = obs_mean;
                        h->houseLoadSigma = (obs_sigma > 0.0) ? obs_sigma : (fabs(obs_mean) * DEFAULT_SIGMA_REL + 0.1);
                    }
                    else
                    {
                        /* Shrinkage toward fallback */
                        double wsum = h->sumW;
                        double mean_shrunk = (wsum * obs_mean + PRIOR_W * fallback_mean) / (wsum + PRIOR_W);

                        /* Combine variances (weighted average of observed and fallback variances) */
                        double sigma_sq_obs = obs_sigma * obs_sigma;
                        double sigma_sq_fbk = fallback_sigma * fallback_sigma;

                        double combined_var = (wsum * sigma_sq_obs + PRIOR_W * sigma_sq_fbk) / (wsum + PRIOR_W);

                        /* Add additional variance due to mean difference (between components) */
                        double delta = obs_mean - fallback_mean;
                        double mean_diff_var = (wsum * PRIOR_W) / ((wsum + PRIOR_W) * (wsum + PRIOR_W)) * (delta * delta);
                        combined_var += mean_diff_var;

                        if (combined_var < 0.0)
                        {
                            combined_var = 0.0;
                        }

                        h->houseLoad = mean_shrunk;
                        h->houseLoadSigma = sqrt (combined_var);
                    }

                    /* finalize solar / garage means with simple weighted average of sums */
                    h->solarPowerRoof   = h->solarPowerRoof   / h->sumW;
                    h->solarPowerGarage = h->solarPowerGarage / h->sumW;
                }
                else
                {
                    /* no observed data: use fallback if possible */
                    if (fallback_found)
                    {
                        h->houseLoad = fallback_mean;
                        h->houseLoadSigma = fallback_sigma;

                        /* For solar/garage we try to get fallback from same hour candidate:
                           reuse prioritized searches similar to FindFallbackHourMean.
                        */
                        bool solar_found = false;
                        /* prioritized search for solar means */
                        /* same month, same dayclass */
                        {
                            int targetClass = GetDayClass (wday);
                            for (int wd2 = 0; wd2 < MAX_WEEK_DAYS && !solar_found; wd2++)
                            {
                                if (!theData.month [mon].weekDay [wd2].valid)
                                {
                                    continue;
                                }
                                if (GetDayClass (wd2) != targetClass)
                                {
                                    continue;
                                }
                                homeEnergyDataMeanHour_t* ch = &theData.month [mon].weekDay [wd2].hour [hour];
                                if (ch->sumW > 0.0)
                                {
                                    h->solarPowerRoof   = ch->solarPowerRoof / ch->sumW;
                                    h->solarPowerGarage = ch->solarPowerGarage / ch->sumW;
                                    solar_found = true;
                                }
                            }
                        }
                        /* same month, any dayclass */
                        if (!solar_found)
                        {
                            for (int wd2 = 0; wd2 < MAX_WEEK_DAYS && !solar_found; wd2++)
                            {
                                if (!theData.month [mon].weekDay [wd2].valid)
                                {
                                    continue;
                                }
                                homeEnergyDataMeanHour_t* ch = &theData.month[mon].weekDay [wd2].hour [hour];
                                if (ch->sumW > 0.0)
                                {
                                    h->solarPowerRoof   = ch->solarPowerRoof / ch->sumW;
                                    h->solarPowerGarage = ch->solarPowerGarage / ch->sumW;
                                    solar_found = true;
                                }
                            }
                        }
                        /* other months same dayclass */
                        if (!solar_found)
                        {
                            int targetClass = GetDayClass (wday);
                            for (int m2 = 0; m2 < MAX_MONTHS && !solar_found; m2++)
                            {
                                if (m2 == mon)
                                {
                                    continue;
                                }
                                for (int wd2 = 0; wd2 < MAX_WEEK_DAYS && !solar_found; wd2++)
                                {
                                    if (!theData.month [m2].weekDay [wd2].valid)
                                    {
                                        continue;
                                    }
                                    if (GetDayClass (wd2) != targetClass)
                                    {
                                        continue;
                                    }
                                    homeEnergyDataMeanHour_t* ch = &theData.month [m2].weekDay [wd2].hour [hour];
                                    if (ch->sumW > 0.0)
                                    {
                                        h->solarPowerRoof   = ch->solarPowerRoof / ch->sumW;
                                        h->solarPowerGarage = ch->solarPowerGarage / ch->sumW;
                                        solar_found = true;
                                    }
                                }
                            }
                        }
                        /* baseline month */
                        if (!solar_found && bestMonth >= 0)
                        {
                            for (int wd2 = 0; wd2 < MAX_WEEK_DAYS && !solar_found; wd2++)
                            {
                                if (!theData.month [bestMonth].weekDay [wd2].valid)
                                {
                                    continue;
                                }
                                homeEnergyDataMeanHour_t* ch = &theData.month [bestMonth].weekDay [wd2].hour [hour];
                                if (ch->sumW > 0.0)
                                {
                                    h->solarPowerRoof   = ch->solarPowerRoof / ch->sumW;
                                    h->solarPowerGarage = ch->solarPowerGarage / ch->sumW;
                                    solar_found = true;
                                }
                            }
                        }
                        if (!solar_found)
                        {
                            h->solarPowerRoof = 0.0;
                            h->solarPowerGarage = 0.0;
                        }
                    }
                    else
                    {
                        /* No fallback found: leave zeros but set a conservative sigma to indicate high uncertainty */
                        h->houseLoad = 0.0;
                        h->houseLoadSigma = 1.0; /* large uncertainty */
                        h->solarPowerRoof = 0.0;
                        h->solarPowerGarage = 0.0;
                        Log ("Warning: no fallback found for mon=%d wday=%d hour=%d\n", mon, wday, hour);
                    }
                } /* end handling h->sumW == 0 or > 0 */

            } /* end per-hour loop */

            /* OPTIONAL smoothing across hours to avoid abrupt transitions */
/*#if SMOOTH_KERNEL
            {
                double tmp[MAX_HOURS];
                for (int hour = 0; hour < MAX_HOURS; hour++)
                {
                    double prev = theData.month[mon].weekDay[wday].hour[(hour + MAX_HOURS - 1) % MAX_HOURS].houseLoad;
                    double curr = theData.month[mon].weekDay[wday].hour[hour].houseLoad;
                    double next = theData.month[mon].weekDay[wday].hour[(hour + 1) % MAX_HOURS].houseLoad;
                    tmp[hour] = 0.25 * prev + 0.5 * curr + 0.25 * next;
                }
                for (int hour = 0; hour < MAX_HOURS; hour++)
                {
                    theData.month[mon].weekDay[wday].hour[hour].houseLoad = tmp[hour];
                }
            }
#endif*/
            /* After smoothing/filling, mark weekday valid so fallback steps keep it */
            theData.month [mon].weekDay [wday].valid = true;
        } /* end weekday loop */
    } /* end month loop */

    /*
     * Existing fallback strategy remains:
     * 1) Fill months with at least one partial or full sequence using normal fallback
     * 2) Fill all remaining missing weekdays/weekends using the baseline month
     *
     * FillMonthFallback uses per-weekday copying; since we've already attempted per-hour fallback,
     * those routines will act mainly on months with no partial sequences or where entire weekdays
     * were missing.
     */

    /* STEP 2: Fill months with at least one partial or full sequence using normal fallback */
    for (int mon = 0; mon < MAX_MONTHS; mon++)
    {
        if (!HasPartialWeekdayOrWeekend (mon, &theData))
        {
            continue; /* skip months without any sequence */
        }

        FillMonthFallback (mon, &theData); /* fills missing weekdays/weekends within this month */
    }

    /* STEP 3: Fill all remaining missing weekdays/weekends in all months using the baseline */
    for (int mon = 0; mon < MAX_MONTHS; mon++)
    {
        bool didLog = false;
        for (int wd = 0; wd < MAX_WEEK_DAYS; wd++)
        {
            if (theData.month [mon].weekDay [wd].valid)
            {
                continue; /* keep existing data */
            }

            if (!didLog)
            {
                Log ("Month %d: fallback filling with best month\n", mon);
                didLog = true;
            }

            /* Copy from baseline month */
            for (int hour = 0; hour < MAX_HOURS; hour++)
            {
                theData.month [mon].weekDay [wd].hour [hour] = theData.month [bestMonth].weekDay [wd].hour [hour];
            }
            theData.month [mon].weekDay [wd].valid = true;
        }
    }

    /* Store result */
    return (HandleMeanPowerDB (true, &theData));
}