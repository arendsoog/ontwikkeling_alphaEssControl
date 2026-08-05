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

static  bool    doAnalyseLog = false;

void    SetChargingMsg (char* msg, charge_t charge)
{
    switch (charge)
    {
        case CHARGING_ON_PV:
            sprintf (msg, "Charge-PV");
            break;
        case NO_CHARGING:
            sprintf (msg, "No-charging");
            break;
        case NO_DISCHARGING:
            sprintf (msg, "No-discharging");
            break;
        case CHARGING_ON_GRID:
            sprintf (msg, "Charge-grid");
            break;
        case CHARGING_DISCHARGE:
            sprintf (msg, "Discharge");
            break;
        default:
            sprintf (msg, "Unknown");
            break;
    }
}

static  void    SetProviderOverrideMsg (char *msg, hour_t theHour)
{
    msg [0] = '\0';
    for (int i = 0; i < MAX_QUARTERS; i ++)
    {
        if (theHour.providerOverride [i].valid)
        {
            strcat (msg, theHour.providerOverride [i].powerSetting > 0.0 ? "C" : "D");
        }
        else
        {
            strcat (msg, "-");
        }
    }
}

static  void    SetTotalPowerMsg (char *msg, int pos, int power, bool showZero, bool showUnits)
{
    char    format [20];

    if (pos < 2)
    {
        return;
    }
    if (power == 0 && !showZero)
    {
        sprintf (msg, " kWh");
        return;
    }
    //if (power < 1000)
    //{
    //    sprintf (format, "%%%dd%s", pos, showUnits ? "Wh" : "");
    //    sprintf (msg, format, power);
    //}
    //else
    //{
        double kwhPower = (double) power / 1000.0;
        sprintf (format, "%%%d.1f%s", showUnits ? pos - 1 : pos, showUnits ? "kWh" : "");
        sprintf (msg, format, kwhPower);
    //}
}

void    ShowSchedule (day_t theDay, int startHour, int curHour, bool hourOnly, char* startMsg)
{
    char earnMsg [10] = {0}, chargeMsg [20] = {0}, providerMsg [20] = {0}, tmp [20] = {0};
    int  i;

    if (!theDay.valid || startHour < 0 || curHour > MAX_HOURS || curHour < startHour)
    {
        return;
    }

    Log ("%s ", startMsg);
    if (!hourOnly)
    {
        Log ("%02d-%02d-%04d:\n", theDay.day, theDay.mon, theDay.year);
    }
    
    int totalSolarEst = 0, totalSolarReal = 0, totalHouseLoadEst = 0, totalHouseLoadReal = 0, totalActivePower = 0;
    double totalResultEst = 0, totalResultReal = 0;
    for (i = startHour; i < MAX_HOURS; i++)
    {
        hour_t  theHour = theDay.hour [i];
        bool    showReal = (i < curHour);

        if (theHour.valid)
        {
            switch (theHour.earning)
            {
                case NO_EARNING:
                    sprintf (earnMsg, "---");
                    break;
                case EARNING_ON_RETURN:
                    sprintf (earnMsg, "Pos");
                    break;
                case EARNING_ON_USE:
                    sprintf (earnMsg, "Neg");
                    break;
            }
            Log ("%02d:00 - %02d:59 -> %8.5f %s Solar: ", i, i, theHour.price, earnMsg);
            SetChargingMsg (chargeMsg, showReal ? theHour.realCharge : theHour.charge);
            SetProviderOverrideMsg (providerMsg, theHour);
            if (i + 1 < MAX_HOURS)
            {
                sprintf (tmp, "%5.1f%%", showReal ? (double) theDay.hour [i + 1].realStartSOC / 10.0 : (double) theDay.hour [i + 1].estimatedStartSOC / 10);    
            }
            else
            {
                sprintf (tmp, "-----%%");
            }
            if (showReal)
            {
                Log (" %4d/%4dWh Load: %4d/%4dWh %s %6dWh %-14.14s Provider: %s ",
                        theHour.estimatedSolarPower, theHour.realSolarPowerRoof + theHour.realSolarPowerGarage, theHour.estimatedHouseLoad, theHour.realHouseLoad,
                        theHour.feedIn ? "Feed-in" : "No-feed", -theHour.totalActivePower, chargeMsg, providerMsg);
                Log ("SOC (%5.1f%%): %5.1f%%/%5.1f%% -> %s",
                        (double) theHour.cutoffSOC / 10.0, (double) theHour.estimatedStartSOC / 10.0, (double) theHour.realStartSOC / 10.0, tmp);
                Log (" %5.2f/%5.2f", theHour.estimatedResult, theHour.realResult);
            }
            else
            {
                Log (" %4d/    Wh Load: %4d/    Wh %s       Wh %-14.14s Provider:      ",
                    theHour.estimatedSolarPower, theHour.estimatedHouseLoad, theHour.feedIn ? "Feed-in" : "No-feed", chargeMsg);
                Log ("SOC (%5.1f%%): %5.1f%%/       -> %s",
                    (double) theHour.cutoffSOC / 10.0, (double) theHour.estimatedStartSOC / 10.0, tmp);
                Log (" %5.2f/     ", theHour.estimatedResult);
            }
            if (showReal)
            {
                int realSolarPower = theHour.realSolarPowerGarage + theHour.realSolarPowerRoof; 
                if (theHour.estimatedSolarPower > 0 && realSolarPower > 0 &&
                    (theHour.estimatedSolarPower >= MIN_EXPECTED_SOLAR_POWER || realSolarPower >= MIN_EXPECTED_SOLAR_POWER))
                {
                    Log (" Solar factor: %4.2f", theHour.estimatedSolarPower > 0 ? (double) realSolarPower / (double) theHour.estimatedSolarPower : 0.0);
                }
            }
            Log (" %s%s\n", theHour.highest ? "Highest" : "", theHour.lowest ? "Lowest" : "");
            totalSolarEst += theHour.estimatedSolarPower;
            totalSolarReal += theHour.realSolarPowerRoof + theHour.realSolarPowerGarage;
            totalHouseLoadEst += theHour.estimatedHouseLoad;
            totalHouseLoadReal += theHour.realHouseLoad;
            totalActivePower += -theHour.totalActivePower;
            totalResultEst += theHour.estimatedResult;
            totalResultReal += theHour.realResult;
        }
        if (hourOnly)
        {
            break;
        }
    }
    
    if (!hourOnly && startHour == 0)    // show totals on total day report
    {
        char solarEst [10], solarReal [10], houseEst [10], houseReal [10], active [10];
        SetTotalPowerMsg (solarEst, 5, totalSolarEst, true, false);
        SetTotalPowerMsg (solarReal, 5, totalSolarReal, false, true);
        SetTotalPowerMsg (houseEst, 5, totalHouseLoadEst, true, false);
        SetTotalPowerMsg (houseReal, 5, totalHouseLoadReal, false, true);
        SetTotalPowerMsg (active, 6, totalActivePower, false, true);
        Log ("%36.36s %5.5s/%7.7s     %5.5s/%7.7s        %9.9s %66.66s %5.2f/%5.2f\n",
             "Totals:", solarEst, solarReal, houseEst, houseReal, active, " ", totalResultEst, totalResultReal);
    }
}

static  void    FinaliseSchedule (day_t *theDay)
{
    int         i, indexOffset, checkIndex;

    if (!theDay->valid)
    {
        return;
    }

    theDay->indexCharge = -1; 
    for (i = 0; i < MAX_HOURS; i++)
    {
        if (theDay->hour [i].charge == CHARGING_ON_GRID)
        {
            theDay->indexCharge = i;
        }
    }

    if (theDay->indexCharge >= 0)
    {
        checkIndex = theDay->indexCharge;
    }
    else
    {
        checkIndex = theDay->indexLowest;
    }
        
    double highBefore = -1000, highAfter = -1000;
    for (i = 0; i < MAX_HOURS; i++)
    {
        theDay->hour [i].feedIn = (theDay->hour [i].earning == EARNING_ON_RETURN);
        switch (theDay->hour [i].charge)
        {
            case NO_CHARGING:
                theDay->hour [i].cutoffSOC = SOC_MIN;
                break;
            case CHARGING_DISCHARGE:
                theDay->hour [i].cutoffSOC = SOC_MIN_DISCHARGE_AFTER;
                break;
            case NO_DISCHARGING:
            case CHARGING_ON_PV:
                theDay->hour [i].cutoffSOC = SOC_MAX;
                break;
            case CHARGING_ON_GRID:
                if (theDay->hour [i].earning == EARNING_ON_USE)
                {
                    theDay->hour [i].cutoffSOC = SOC_MAX_CHARGE_ON_GRID;
                }
                else
                {
                    theDay->hour [i].cutoffSOC = SOC_MAX;
                }
                break;
        }
    }
}

static  double  ClampDouble (double val, double low, double high)
{
    if (val < low)
    {
        return (low);
    }
    if (val > high)
    {
        return (high);
    }
    return (val);
}

static  int  ClampInt (int val, int low, int high)
{
    if (val < low)
    {
        return (low);
    }
    if (val > high)
    {
        return (high);
    }
    return (val);
}

// version 1.0.2 - updated EvaluateHourAction () for inverter and battery efficiency

#define CHARGE_LIMIT    10000.0  // Wh
#define DISCHARGE_LIMIT 10000.0  // Wh

#define MIN_CHARGE_POWER        100.0
#define MIN_DISCHARGE_POWER     100.0

/* 
 ETA_GRID_TO_BATT

 Efficiency factor for charging the battery from the grid that accounts for additional conversion losses not covered by the inverter efficiency curve
 or the battery charge efficiency.

 It represents DC bus / power electronics losses in the AC → DC → battery charging path inside the hybrid inverter.

 Total grid → battery efficiency becomes:

 grid AC
   - inverter efficiency
   - internal DC bus / conversion (ETA_GRID_TO_BATT)
   - battery charge efficiency

 Typical value: 0.97 – 0.99
*/
#define ETA_GRID_TO_BATT        0.97

static  double  InverterEfficiency (double power, double inverterNominalPower)
{
    double load = power / inverterNominalPower;

    if (load < 0.02) return (0.85);
    if (load < 0.05) return (0.90);
    if (load < 0.10) return (0.93);
    if (load < 0.20) return (0.955);
    if (load < 0.50) return (0.97);
    return (0.975);
}

static  double  BatteryEfficiency (double power, double batteryCapacity)
{
    double cRate = power / batteryCapacity;

    if (cRate < 0.02) return (0.90);
    if (cRate < 0.05) return (0.93);
    if (cRate < 0.10) return (0.95);
    if (cRate < 0.20) return (0.965);
    return (0.975);
}

/*
 * EvaluateHourAction ():
 *  Simulates a single-hour battery action and its financial effect.
 *
 *  - applies the selected charge/discharge action for one hour
 *  - updates battery SOC within physical and policy limits
 *  - computes the immediate profit or cost for this hour
 *  - tracks whether daily grid-charge or discharge limits are consumed
 *
 *  Does NOT consider future hours; purely a local, single-step evaluation.
 */
static  void    EvaluateHourAction (charge_t action, hour_t theHour, double SOC_W, int chargeUsed, int dischargeUsed,
                                    double* newSOC_W, double* profit, int* nextChargeUsed, int* nextDischargeUsed)
{
    config_t theConfig = GetConfig ();

    double inverterNominalPower = theConfig.general.AlphaESSInverterNominalPower;
    double batteryCapacity = theConfig.general.AlphaESSUsableBatteryCapacity;

    double solarDC = theHour.estimatedSolarPower;       // Wh
    double houseLoad = theHour.estimatedHouseLoad;      // Wh

    earning_t earning = theHour.earning;

    double priceUse    = MkUsePrice (theHour.price) / 1000.0;    // EUR/Wh
    double priceReturn = MkReturnPrice (theHour.price) / 1000.0; // EUR/Wh

    // special case
    if (earning == EARNING_ON_USE)
    {
        solarDC = 0; // PV-panels turned off by AlphaESSControl in CheckAndSetChargingMode (). FeedIn always negative
    }

    double solarAC = solarDC * InverterEfficiency (fmin (solarDC, inverterNominalPower), inverterNominalPower);
    solarAC = fmin (solarAC, inverterNominalPower);

    double feedIn = solarAC - houseLoad;

    *newSOC_W = SOC_W;
    *profit = -INFINITY;
    *nextChargeUsed = chargeUsed;
    *nextDischargeUsed = dischargeUsed;

    double maxSOC = SOC_MAX / 1000.0 * batteryCapacity;                    // maximum SOC in Wh
    double maxSOCeou = SOC_MAX_CHARGE_ON_GRID / 1000.0 * batteryCapacity;  // maximum SOC in Wh
    double minSOC = SOC_MIN / 1000.0 * batteryCapacity;                    // minimum SOC in Wh
    double maxDischargeAfter = SOC_MIN_DISCHARGE_AFTER / 1000.0 * batteryCapacity;  // min SOC after discharge

    // on earning on use negative prices, no feedin and PV turned off, so no solar power
    switch (action)
    {
        // Action: Load battery on PV, house load is compensated by PV, if battery full, PV to grid. Never on EARNING_ON_USE
        case CHARGING_ON_PV:
        {
            if (earning != EARNING_ON_USE)
            {
                if (feedIn > 0.0)   // feedIn > 0: more solar power than house load, so battery will charge, rest to grid
                {
                    double spaceSOC = maxSOC - SOC_W;
                    double chargeAC = fmin (feedIn, CHARGE_LIMIT);
                    if (chargeAC >= MIN_CHARGE_POWER)
                    {
                        double invEff = InverterEfficiency (chargeAC, inverterNominalPower);
                        double battEff = BatteryEfficiency (chargeAC, batteryCapacity);
                        double chargeSOC = chargeAC * invEff * battEff;
                        chargeSOC = fmin (chargeSOC, spaceSOC);
                        chargeAC = chargeSOC / (invEff * battEff);
                        *newSOC_W = SOC_W + chargeSOC;
                        *profit = (feedIn - chargeAC) * priceReturn;    // PV overload returned to grid, positive profit
                    }
                }
                else    // feedIn <= 0: all solar power used for house load, remaining house load used from battery, so battery will discharge, rest from grid      
                {
                    double availableSOC = SOC_W - minSOC;
                    double dischargeAC = fmin (-feedIn, DISCHARGE_LIMIT);
                    if (dischargeAC < MIN_DISCHARGE_POWER)
                    {
                        dischargeAC = 0.0;
                    }
                    double invEff = InverterEfficiency (dischargeAC, inverterNominalPower);
                    double battEff = BatteryEfficiency (dischargeAC, batteryCapacity);
                    double dischargeSOC = dischargeAC / (invEff * battEff);
                    dischargeSOC = fmin (dischargeSOC, availableSOC);
                    dischargeAC = dischargeSOC * invEff * battEff;
                    *newSOC_W = SOC_W - dischargeSOC;
                    *profit = (feedIn + dischargeAC) * priceUse;    // remaining used from grid  (f.i.: feedIn -20, discharge: 10 -> use = -10). Negative prices are ok
                
                }
            }
        }
        break;

        // Action: No battery charge, house load is compensated first by PV, then by battery and then by grid
        case NO_CHARGING:
        {
            if (earning == EARNING_ON_USE)
            {
                *profit = houseLoad * -priceUse;    // compensate negative price to create profit
            }
            else
            {
                if (feedIn > 0.0)   // feedIn > 0: more solar power than house load, rest to grid
                {
                    *profit = feedIn * priceReturn;
                }
                else   // feedIn <= 0: all solar power used for house load, remaining house load used from battery, so battery will discharge, rest from grid 
                {
                    double availableSOC = SOC_W - minSOC;
                    double dischargeAC = fmin (-feedIn, DISCHARGE_LIMIT);
                    if (dischargeAC < MIN_DISCHARGE_POWER)
                    {
                        dischargeAC = 0.0;
                    }
                    double invEff = InverterEfficiency (dischargeAC, inverterNominalPower);
                    double battEff = BatteryEfficiency (dischargeAC, batteryCapacity);
                    double dischargeSOC = dischargeAC / (invEff * battEff);
                    dischargeSOC = fmin (dischargeSOC, availableSOC);
                    dischargeAC = dischargeSOC * invEff * battEff;
                    *newSOC_W = SOC_W - dischargeSOC;
                    // discharge always <= -feedIn
                    *profit = (feedIn + dischargeAC) * priceUse;    // remaining used from grid  (f.i.: feedIn -20, discharge: 10 -> use = -10). Negative prices are ok
                }
            }
        }
        break;

        // Action: Load battery on PV after house load compensation. House load not compensated by battery
        case NO_DISCHARGING:
        {
            if (feedIn > 0.0)   // feedIn > 0: more solar power than house load, so battery will charge, rest to grid
            {
                double spaceSOC = maxSOC - SOC_W;
                double chargeAC = fmin (feedIn, CHARGE_LIMIT);
                if (chargeAC >= MIN_CHARGE_POWER)
                {
                    double invEff = InverterEfficiency (chargeAC, inverterNominalPower);
                    double battEff = BatteryEfficiency (chargeAC, batteryCapacity);
                    double chargeSOC = chargeAC * invEff * battEff;
                    chargeSOC = fmin (chargeSOC, spaceSOC);
                    chargeAC = chargeSOC / (invEff * battEff);
                    *newSOC_W = SOC_W + chargeSOC;
                    *profit = (feedIn - chargeAC) * priceReturn;    // PV overload returned to grid, positive profit
                }
            }
            else    // feedIn <= 0: all solar power used for house load, remaining house load from grid. No SOC change   
            {
                *profit = feedIn * priceUse;  // remaining used from grid
            }
        }
        break;

        // Action: Charge from grid (max 1 time per day)
        case CHARGING_ON_GRID:
        {
            if (!chargeUsed)
            {
                double maxCapacity = (earning == EARNING_ON_USE ? maxSOCeou : maxSOC);
                double spaceSOC = maxCapacity - SOC_W;
                double chargeSOC = fmin (spaceSOC, CHARGE_LIMIT);
                if (chargeSOC > 0.0)
                {
                    double chargeAC_est = chargeSOC / ETA_GRID_TO_BATT;
                    double battEff = BatteryEfficiency (chargeAC_est, batteryCapacity);
                    double invEff = InverterEfficiency (chargeAC_est, inverterNominalPower);
                    double chargeAC = chargeSOC / (invEff * battEff * ETA_GRID_TO_BATT);

                    if (chargeAC >= MIN_CHARGE_POWER)
                    {
                        *newSOC_W = SOC_W + chargeSOC;
                        feedIn -= chargeAC;
                        *profit = feedIn * ((feedIn > 0.0) ? priceReturn : priceUse);

                        *nextChargeUsed = 1;
                    }
                }
            }
        }
        break;

        // Action: Discharge battery (max 1 time per day). Never on EARNING_ON_USE
        case CHARGING_DISCHARGE:
        {
            if (earning != EARNING_ON_USE && !dischargeUsed)
            {
                double dischargeSOC = SOC_W - fmax (minSOC, maxDischargeAfter);
                dischargeSOC = fmin (dischargeSOC, DISCHARGE_LIMIT);
                if (dischargeSOC > 0.0)
                {
                    double dischargeAC_est = dischargeSOC;
                    double battEff = BatteryEfficiency (dischargeAC_est, batteryCapacity);
                    double invEff = InverterEfficiency (dischargeAC_est, inverterNominalPower);
                    double dischargeAC = dischargeSOC * battEff * invEff;

                    if (dischargeAC >= MIN_DISCHARGE_POWER)
                    {
                        *newSOC_W = SOC_W - dischargeSOC;
                        feedIn += dischargeAC;

                        *profit = feedIn * ((feedIn > 0.0) ? priceReturn : priceUse);   // feedIn = negative = use = automatically negative profit on positive use price

                        *nextDischargeUsed = 1;
                    }
                }
            }
        }
        break;
    }
    *newSOC_W = ClampDouble (*newSOC_W, 0.0, (double) batteryCapacity);
}

#define SOC_STEPS   1000

typedef struct
{
    double      result;
    double      hourResult;
    charge_t    charge;
    int         nextSOCIndex;
    int         nextChargeUsed;
    int         nextDischargeUsed;
} dp_t;

static dp_t dp [MAX_HOURS * 2 + 1][SOC_STEPS + 1][2][2];

/*
 * CalculateBestSchedule ():
 *  Computes the optimal battery control schedule using dynamic programming.
 *
 *  - optimises total profit from startHour until the end of today (and tomorrow if available)
 *  - state dimensions:
 *      * hour
 *      * battery state of charge (SOC)
 *      * daily grid-charge-used flag
 *      * daily discharge-used flag
 *  - respects all physical constraints (SOC limits, charge/discharge limits)
 *  - enforces daily limits: grid charge and discharge are allowed at most once per day
 *  - optionally forces a specific action at startHour
 *
 *  Returns false if no valid schedule exists from the given state.
 *  On success, fills the per-hour schedule and cumulative result.
 */

/* inside this routine, SOC is actual battery power, not a percentage */
static  bool    CalculateBestSchedule (int curSOC_p, int startHour, day_t* today, day_t* tomorrow, bool chargeUsedBefore, bool dischargeUsedBefore,
                                        bool forcedActionActive, charge_t forcedAction, double *dpResult)
{
    if (forcedActionActive)
    {
        if (chargeUsedBefore && forcedAction == CHARGING_ON_GRID)
        {
            return (false);
        }
        if (dischargeUsedBefore && forcedAction == CHARGING_DISCHARGE)
        {
            return (false);
        }
    }

    // Initialise
    for (int h = 0; h <= MAX_HOURS * 2; h++)
    {
        for (int si = 0; si <= SOC_STEPS; si++)
        {
            for (int cu = 0; cu < 2; cu++)
            {
                for (int du = 0; du < 2; du++)
                {
                    dp_t* dp_entry = &dp [h][si][cu][du];
                    dp_entry->result = 0.0;
                    dp_entry->hourResult = 0.0;
                    dp_entry->charge = NO_CHARGING;
                    dp_entry->nextSOCIndex = si;
                    dp_entry->nextChargeUsed = cu;
                    dp_entry->nextDischargeUsed = du;
                }
            }
        }
    }

    // DP backwards
    hour_t* theHour;
    int     endHour = 0;
    if (today->valid)
    {
        endHour = MAX_HOURS;
    }
    if (tomorrow->valid)
    {
        endHour = MAX_HOURS * 2;
    }
    config_t theConfig = GetConfig ();
    double batteryCapacity = theConfig.general.AlphaESSUsableBatteryCapacity;
    for (int hour = endHour - 1; hour >= startHour; hour--)
    {
        if (hour >= MAX_HOURS)
        {
            theHour = &tomorrow->hour [hour - MAX_HOURS];
        }
        else
        {
            theHour = &today->hour [hour];
        }
        for (int si = 0; si <= SOC_STEPS; si++)
        {
            double SOC_W = si * (batteryCapacity / SOC_STEPS);  // convert index to Wh

            for (int cu = 0; cu < 2; cu++)
            {
                for (int du = 0; du < 2; du++)
                {
                    double  bestResult = -INFINITY;
                    double  bestHourProfit = -INFINITY;
                    int     bestAction = NO_CHARGING;
                    int     bestNextSOCIndex = si;
                    int     bestNextCU = cu;
                    int     bestNextDU = du;

                    for (charge_t action = 0; action < MAX_CHARGE; action++)
                    {
                        double newSOC_W = SOC_W;
                        double profit = 0.0;
                        int nextCU = cu;
                        int nextDU = du;

                        EvaluateHourAction (action, *theHour, SOC_W, cu, du, &newSOC_W, &profit, &nextCU, &nextDU);
                        if (forcedActionActive && hour == startHour && action != forcedAction)
                        {
                            profit = -INFINITY;   // so other actions are ignored
                        }

                        int ns = (int) (newSOC_W / (batteryCapacity / SOC_STEPS) + 0.5);
                        ns = ClampInt (ns, 0, SOC_STEPS);

                        // reset daily limits at day start **after evaluating action**
                        int resetCU = nextCU;
                        int resetDU = nextDU;
                        if ((hour + 1) % 24 == 0)
                        {
                            resetCU = 0;
                            resetDU = 0;
                        }

                        double result = profit + dp [hour + 1][ns][resetCU][resetDU].result;

                        if (result > bestResult)
                        {
                            bestResult = result;
                            bestHourProfit = profit;
                            bestAction = action;
                            bestNextSOCIndex = ns;
                            bestNextCU = nextCU;
                            bestNextDU = nextDU;
                        }
                    }

                    dp_t* dp_entry = &dp [hour][si][cu][du];
                    dp_entry->result = bestResult;
                    dp_entry->hourResult = bestHourProfit;
                    dp_entry->charge = bestAction;
                    dp_entry->nextSOCIndex = bestNextSOCIndex;
                    dp_entry->nextChargeUsed = bestNextCU;
                    dp_entry->nextDischargeUsed = bestNextDU;
                }
            }
        }
    }

    int si = curSOC_p;
    int cu = chargeUsedBefore ? 1 : 0;
    int du = dischargeUsedBefore ? 1 : 0;

    *dpResult = dp [startHour][si][cu][du].result;  // set result
    double calcResult = 0.0;
    for (int hour = startHour; hour < endHour; hour++)
    {
        if (hour >= MAX_HOURS)
        {
            theHour = &tomorrow->hour [hour - MAX_HOURS];
        }
        else
        {
            theHour = &today->hour [hour];
        }
        
        if (hour != startHour && hour % 24 == 0)  // reset daily limits at day start
        {
            cu = 0;
            du = 0;
        }

        theHour->estimatedStartSOC = si;

        dp_t* dp_entry = &dp [hour][si][cu][du];
        theHour->charge = dp_entry->charge;
        theHour->estimatedResult = dp_entry->hourResult;

        calcResult += dp_entry->hourResult;

        // update state for next hour
        si = dp_entry->nextSOCIndex;
        cu = dp_entry->nextChargeUsed;
        du = dp_entry->nextDischargeUsed;
    }

    //printf("TESTING: dpResult: %f calcResult: %f\n", *dpResult, calcResult);
    if (forcedActionActive && today->hour [startHour].charge != forcedAction)
    {
        return (false);
    }
    if (*dpResult < -100000)    // means no valid hour action found
    {
        return (false);
    }
    return (true);
}

typedef struct
{
    double solarFactor;   // multiplicative factor
    double loadFactor;
    double probability;
} scenario_t;

static const scenario_t scenarios [] =
{
    { 0.6, 1.1, 0.28 },   // less sun, higher load (pessimistic)
    { 0.85, 0.9, 0.27 },   // less sun, less load
    { 1.0, 1.0, 0.15 },   // nominal
    { 1.15, 1.1, 0.18 },   // more sun, higher load
    { 1.25, 0.9, 0.12 },   // more sun, less load (optimistic)
};

#define MAX_SCENARIOS (sizeof (scenarios) / sizeof (scenarios [0]))

typedef struct
{
    bool        valid;
    double      profit;
    double      probability;
} ps_t;

static int ComparePS (const void* a, const void* b)
{
    double da = ((ps_t*)a)->profit;
    double db = ((ps_t*)b)->profit;
    return (da < db) ? -1 : ((da > db) ? 1 : 0);
}

/*
 * Percentile ():
 *  Computes a probability-weighted profit percentile over all valid scenarios.
 *
 *  - filters out non-valid scenarios and scenarios with zero probability
 *  - renormalises remaining probabilities to sum to 1.0
 *  - sorts scenarios by profit (ascending)
 *  - returns the profit value at cumulative probability >= p
 *
 *  Returns false if no valid scenarios are available.
 */
static bool Percentile (ps_t* ps, int n, double p, double* perc)
{
    ps_t tmp [MAX_SCENARIOS];
    int m = 0;
    double probSum = 0.0;

    /* Filter valid scenarios */
    for (int i = 0; i < n; i++)
    {
        if (ps [i].valid && ps [i].probability > 0.0)
        {
            tmp [m++] = ps [i];
            probSum += ps [i].probability;
        }
    }

    /* No valid scenarios, that's extremely bad */
    if (m == 0)
    {
        *perc = 0.0;
        return (false);
    }

    /* Renormalise probabilities */
    for (int i = 0; i < m; i++)
    {
        tmp [i].probability /= probSum;
    }

    qsort (tmp, m, sizeof (ps_t), ComparePS);

    double cum = 0.0;
    for (int i = 0; i < m; i++)
    {
        cum += tmp [i].probability;
        if (cum >= p)
        {
            *perc = tmp [i].profit;
            return (true);
        }
    }

    *perc = tmp [m - 1].profit;
    return (true);
}

static void LogScenarioSpread (int hour, charge_t action, ps_t* ps, int n)
{
    double  min = 0.0;
    double  max = 0.0;
    double  sum = 0.0;
    bool    first = true;

    if (!doAnalyseLog)
    {
        return;
    }

    for (int i = 0; i < n; i++)
    {
        if (!ps [i].valid)
        {
            continue;
        }
        if (first)
        {
            min = max = ps [i].profit;
            first = false;
        }
        else
        {
            if (ps [i].profit < min) min = ps [i].profit;
            if (ps [i].profit > max) max = ps [i].profit;
        }
        sum += ps [i].profit * ps [i].probability;
        first = false;
    }

    double  p20;
    if (Percentile (ps, n, 0.20, &p20))
    {
        Log ("SCEN H=%02d action=%d exp=%.3f p20=%.3f min=%.3f max=%.3f\n", hour, action, sum, p20, min, max);
    }
    else
    {
        Log ("SCEN H=%02d action=%d non-valid\n", hour, action);
    }
}

void LogExecutionResult (int hour, hour_t theHour)
{
    if (!doAnalyseLog)
    {
        return;
    }

    Log ("EXEC H=%02d action=%d exp=%.3f real=%.3f provider: ", hour, theHour.charge, theHour.estimatedResult, theHour.realResult);
    for (int i = 0; i < 2; i ++)
    {
        if (i == 0)
        {
            Log ("%02d:00-%02d:29=", hour, hour);
        }
        else
        {
            Log (" %02d:30-%02d:59=", hour, hour);
        }
        if (theHour.providerOverride [i].valid)
        {
            Log ("%s", theHour.providerOverride [i].powerSetting > 0 ? "CHARGE" : "DISCHARGE");
        }
        else
        {
            Log ("NO_ACTION");
        }
    }
    Log ("\n");
}

/*
 * ApplyScenarioToDay ():
 *  Applies a forecast uncertainty scenario to today and tomorrow.
 *
 *  - blends scenario factors into the baseline forecast using exponential time decay (near-term hours are affected more than distant hours)
 *  - scales solar production only when PV is materially relevant
 *  - widens or narrows house-load deviations based on forecast uncertainty (sigma)
 *  - keeps all adjustments bounded to avoid extreme or unstable values
 */

#define SCENARIO_TAU_HOURS      6.0     // “for how long do I trust my current forecast more than my baseline”
#define SOLAR_SCENARIO_MIN_WH   300.0   // "Only use scenarios when PV is relevant factor" Rule of thumb: MIN_WH ≈ 5–10% of typical midday hour production
#define HOUSE_LOAD_SIGMA_GAIN   0.8     // tuning knob

static void ApplyScenarioToDay (day_t* today, day_t *tomorrow, const scenario_t* sc, int startHour)
{
    hour_t* theHour;
    int     endHour = 0;
    if (today->valid)
    {
        endHour = MAX_HOURS;
    }
    if (tomorrow->valid)
    {
        endHour = MAX_HOURS * 2;
    }
    for (int hour = 0; hour < endHour; hour++)
    {
        if (hour >= MAX_HOURS)
        {
            theHour = &tomorrow->hour [hour - MAX_HOURS];
        }
        else
        {
            theHour = &today->hour [hour];
        }
        double decay = 1.0;
        int dh = hour - startHour;
        if (dh < 0)
        {
            dh = 0;
        }
        decay = exp (-(double)dh / SCENARIO_TAU_HOURS);
        double solarFactor = 1.0 + decay * (sc->solarFactor - 1.0);
        double loadFactor = 1.0 + decay * (sc->loadFactor - 1.0);

        double solar = theHour->estimatedSolarPower;
        double solarWeight = solar / (solar + SOLAR_SCENARIO_MIN_WH);   // prevent PV-scenario-ruis on low solar
        double effectiveSolarFactor = 1.0 + solarWeight * (solarFactor - 1.0);
        theHour->estimatedSolarPower *= effectiveSolarFactor;

        /* Adaptive load uncertainty using sigma */

        double loadMean = theHour->estimatedHouseLoad;
        double loadSigma = theHour->estimatedHouseLoadSigma;

        /* Relative uncertainty (dimensionless) */
        double relSigma = 0.0;
        if (loadMean > 1.0)
        {
            relSigma = loadSigma / loadMean;
        }

        /*
        * Sigma amplifier:
        *  - relSigma ≈ 0.1  → almost no effect
        *  - relSigma ≈ 0.3  → clearly widen scenarios
        *  - relSigma ≥ 0.5  → cap to avoid explosion
        */
        double sigmaAmplifier = 1.0 + HOUSE_LOAD_SIGMA_GAIN * relSigma;
        sigmaAmplifier = ClampDouble (sigmaAmplifier, 1.0, 1.6);

        /* Pull scenario loadFactor further away from 1.0 when sigma is high */
        double effectiveLoadFactor = 1.0 + sigmaAmplifier * (loadFactor - 1.0);
        effectiveLoadFactor = ClampDouble (effectiveLoadFactor, 0.5, 2.0);
        theHour->estimatedHouseLoad *= effectiveLoadFactor;

        if (hour == startHour && doAnalyseLog)
        {
            Log ("H%02d solar=%.0fWh solarWeight=%.2f\n", startHour, solar, solarWeight);
            Log ("LOAD H%02d lf=%.2f eff=%.2f\n", startHour, loadFactor, effectiveLoadFactor);
            Log ("SIGMA H%02d mean=%.0f sigma=%.0f rel=%.2f amp=%.2f\n", startHour, loadMean, loadSigma, relSigma, sigmaAmplifier);
        }
    }
}

/*
 * SetSchedule ():
 *  Determines the optimal charge/discharge schedule starting at the current hour.
 *
 *  - computes a baseline schedule without discretionary grid charge/discharge
 *  - evaluates each possible charge action under multiple uncertainty scenarios
 *  - for each action:
 *      * runs a constrained DP optimisation
 *      * computes marginal profit vs baseline per scenario
 *      * aggregates expected profit and downside risk (P20)
 *  - selects the action that maximises:
 *        expectedProfit − λ · downsideRisk
 *  - falls back to the baseline schedule if:
 *      * no valid action exists
 *      * or the expected extra profit is below the configured minimum
 *
 *  Finalises and stores the selected schedule for today and tomorrow.
 */

#define RISK_PERCENTILE 0.20
#define LAMBDA          0.30    // determines impact of bad profit in mean profit. Lower lambda = more aggressive approach

bool    SetSchedule (int curSOC, int curHour, day_t* today, day_t *tomorrow)
{
    day_t   todayOpt, todayBL, tomorrowOpt, tomorrowBL;
    int     i;

    todayBL = *today;
    tomorrowBL = *tomorrow;
    double resultBL;

    if (!CalculateBestSchedule (curSOC, curHour, &todayBL, &tomorrowBL, true, true, false, -1, &resultBL))   // baseline is without charge & discharge
    {
        TimeLog ("SetSchedule failed, no baseline, setting Charge-PV\n");
        for (i = curHour; i < MAX_HOURS; i ++)
        {
            today->hour [i].charge = CHARGING_ON_PV;
            today->hour [i].estimatedResult = 0.0;
        }

        FinaliseSchedule (today);
        FinaliseSchedule (tomorrow);
        return (true);
    }

    if (today->chargeOnGridUsed && today->dischargeUsed)
    {
        Log ("Charge and discharge already used today. Selecting baseline\n");
        *today = todayBL;
        *tomorrow = tomorrowBL;

        FinaliseSchedule (today);
        FinaliseSchedule (tomorrow);
        return (true);
    }

    /* Scenario based charge action evaluation */

    double expectedProfit [MAX_CHARGE];
    ps_t   profits [MAX_CHARGE][MAX_SCENARIOS];

    for (int action = 0; action < MAX_CHARGE; action++)
    {
        expectedProfit [action] = 0.0;
        for (int s = 0; s < MAX_SCENARIOS; s++)
        {
            profits [action][s].profit = 0.0;
            profits [action][s].probability = 0.0;
            profits [action][s].valid = false;
        }
    }

    if (doAnalyseLog)
    {
        TimeLog ("*** SetSchedule logging start ***\n");
        for (i = 0; i < MAX_CHARGE; i ++)
        {
            char tmp [20];
            SetChargingMsg (tmp, i);
            Log ("action %d = %s\n", i, tmp);
        }
    }

    /* calculate total expectedProfit for each action with all scenario's applying the probability of each scenario */
    for (int s = 0; s < MAX_SCENARIOS; s++)
    {
        day_t   todaySc, tomorrowSc;

        todaySc = *today;
        tomorrowSc = *tomorrow;
        ApplyScenarioToDay (&todaySc, &tomorrowSc, &scenarios [s], curHour);

        for (charge_t action = 0; action < MAX_CHARGE; action++)
        {
            day_t   todayOpt = todaySc;
            day_t   tomorrowOpt = tomorrowSc;
            double  result;

            profits [action][s].valid = CalculateBestSchedule (curSOC, curHour, &todayOpt, &tomorrowOpt, today->chargeOnGridUsed, today->dischargeUsed, true, action, &result);
            
            if (profits [action][s].valid)
            {
                double marginal = result - resultBL;
                profits [action][s].profit = marginal;
                profits [action][s].probability = scenarios [s].probability;
                expectedProfit [action] += scenarios[s].probability * marginal;
            }
        }
    }

    if (doAnalyseLog)
    {
        Log ("H%02d houseLoad=%dWh houseLoadSigma=%.0fWh\n", curHour, today->hour [curHour].estimatedHouseLoad, today->hour [curHour].estimatedHouseLoadSigma);

        for (charge_t action = 0; action < MAX_CHARGE; action++)
        {
            LogScenarioSpread (curHour, action, profits [action], MAX_SCENARIOS);
        }
    }

    /* Charge action selection with P20 */

    double lambda = LAMBDA;
    bool haveValidAction = false;
    double bestScore = 0.0;
    charge_t bestAction = NO_CHARGING;
    for (charge_t action = 0; action < MAX_CHARGE; action++)
    {
        double p20;
        if (!Percentile (profits [action], MAX_SCENARIOS, RISK_PERCENTILE, &p20))
        {
            if (doAnalyseLog)
            {
                Log ("RISK H=%02d SOC=%d action=%d non-valid\n", curHour, curSOC, action);
            }
            continue;
        }

        double riskPenalty = fmax (0.0, -p20);
        double score = expectedProfit [action] - lambda * riskPenalty;

        if (doAnalyseLog)
        {
            Log ("RISK H=%02d SOC=%d action=%d exp=%.3f p20=%.3f lambda=%.2f score=%.3f\n", curHour, curSOC, action, expectedProfit [action], p20, lambda, score);
        }

        if (!haveValidAction || score > bestScore)  // first time always sets bestScore & bestAction
        {
            bestScore = score;
            bestAction = action;
        }
        haveValidAction = true;
    }

    if (!haveValidAction)
    {
        Log ("SetSchedule: no valid action/scenario found. Selecting baseline\n");
        *today = todayBL;
        *tomorrow = tomorrowBL;

        FinaliseSchedule (today);
        FinaliseSchedule (tomorrow);
        return (true);
    }

    if (doAnalyseLog)
    {
        Log ("SetSchedule: Selected action %d (score=%.2f, lambda=%.2f)\n", bestAction, bestScore, lambda);
        for (int s = 0; s < MAX_SCENARIOS; s++)
        {
            if (profits [bestAction][s].valid)
            {
                Log ("  scenario %d: marginal=%.2f (p=%.2f)\n", s, profits [bestAction][s].profit, scenarios [s].probability);
            }
            else
            {
                Log ("  scenario %d: non-valid\n", s);
            }
        }
        TimeLog ("*** SetSchedule logging end ***\n");
    }
    
    /* Final optimized schedule with selected charge action */

    todayOpt = *today;
    tomorrowOpt = *tomorrow;
    double resultOpt;

    if (CalculateBestSchedule (curSOC, curHour, &todayOpt, &tomorrowOpt, today->chargeOnGridUsed, today->dischargeUsed, true, bestAction, &resultOpt))
    {
        // on executed grid charge, take into account the difference in result between between charge (optimum) & no charge (baseline)
        if (today->chargeOnGridUsed && today->indexCharge >= 0 && today->indexCharge < curHour)
        {
            resultOpt += today->hour [today->indexCharge].realResult;
            resultBL += todayBL.hour [today->indexCharge].estimatedResult;
        }

        // on executed discharge, take into account the difference in result between between discharge (optimum) & no discharge (baseline)
        if (today->dischargeUsed && today->indexDischarge >= 0 && today->indexDischarge < curHour)
        {
            resultOpt += today->hour [today->indexDischarge].realResult;
            resultBL += todayBL.hour [today->indexDischarge].estimatedResult;
        }

        /* Minimum profit check */

        double optExtra = resultOpt - resultBL;
        config_t theConfig = GetConfig ();

        if (optExtra < theConfig.daily.profitMin)
        {
            Log ("Scenario-optimum: %.2f, baseline result: %.2f. Extra (%.2f) < minimum (%.2f). Selecting baseline\n",
                resultOpt, resultBL, optExtra, theConfig.daily.profitMin);
            *today = todayBL;
            *tomorrow = tomorrowBL;
        }
        else
        {
            Log ("Scenario-optimum: %.2f, baseline result: %.2f. Extra (%.2f) >= minimum (%.2f). Selecting optimum\n",
                resultOpt, resultBL, optExtra, theConfig.daily.profitMin);
            *today = todayOpt;
            *tomorrow = tomorrowOpt;
        }
    }
    else
    {
        TimeLog ("SetSchedule failed, selecting baseline\n");
        *today = todayBL;
        *tomorrow = tomorrowBL;
    }

    FinaliseSchedule (today);
    FinaliseSchedule (tomorrow);
    return (true);
}