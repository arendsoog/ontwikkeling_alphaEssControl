//
// Author: Jaco Melse (jjmelse@xs4all.nl)
// 2025 - 2026
//

#include <stddef.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <unistd.h>
#include <errno.h>
#include <string.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <modbus/modbus.h>
#include "Util.h"
#include "AlphaESS.h"

static uint32_t ToUnsignedInt (uint16_t highWord, uint16_t lowWord)
{
    return (((uint32_t) highWord << 16) | lowWord);
}

static int32_t  ToInt (uint16_t highWord, uint16_t lowWord)
{
    return ((int32_t) (((uint32_t) highWord << 16) | lowWord));
}

static int  CheckDispatchMode (uint16_t mode, uint16_t cutoffSOC)
{
    char tmp [40];

    sprintf (tmp, "with cut-off SOC: %1.1f%%", (double) cutoffSOC / 10.0);
    switch (mode)
    {
        case DISPATCH_MODE_DEFAULT:
            Debug (2, "Setting Default mode %s\n", tmp);
            break;
        case DISPATCH_MODE_ONLY_CHARGE_FROM_PV:
            Debug(2, "Setting Battery only Charges from PV mode %s\n", tmp);
            break;
        case DISPATCH_MODE_STATE_OF_CHARGE_CONTROL:
            Debug(2, "Setting State of Charge control %s\n", tmp);
            break;
        case DISPATCH_MODE_LOAD_FOLLOWING:
            Debug(2, "Setting Load Following mode %s\n", tmp);
            break;
        case DISPATCH_MODE_MAXIMISE_OUTPUT:
            Debug(2, "Setting Maximise Output mode %s\n", tmp);
            break;
        case DISPATCH_MODE_NORMAL:
            Debug(2, "Setting Normal mode %s\n", tmp);
            break;
        case DISPATCH_MODE_OPTIMISE_CONSUMPTION:
            Debug(2, "Setting Optimise Consumption mode %s\n", tmp);
            break;
        case DISPATCH_MODE_MAXIMISE_CONSUMPTION:
            Debug(2, "Setting Maximise Consumption mode %s\n", tmp);
            break;
        case DISPATCH_MODE_ECO:
            Debug(2, "Setting ECO mode %s\n", tmp);
            break;
        case DISPATCH_MODE_FCAS:
            Debug(2, "Setting FCAS mode %s\n", tmp);
            break;
        case DISPATCH_MODE_PV_POWER_SETTING:
            Debug(2, "Setting PV Power Setting mode %s\n", tmp);
            break;
        case DISPATCH_MODE_NO_BATTERY_CHARGE:
            Debug(2, "Setting No Battery Charge mode %s\n", tmp);
            break;
        default:
            Debug(2, "Illegal mode\n");
            return (-1);
    }
    return (0);
}

static struct
{
    modbus_t*   ctx;
    char        ip [64];
    int         port;
} theMB;

static int ModbusConnectSafe (void)
{
    theMB.ctx = modbus_new_tcp (theMB.ip, theMB.port);
    if (theMB.ctx == NULL)
    {
        return (-1);
    }

    modbus_set_debug (theMB.ctx, 0);

    int ret;
    if ((ret = modbus_set_slave (theMB.ctx, 85)) < 0)
    {
        TimeLog ("modbus_set_slave: %s\n", modbus_strerror (errno));
        return (-1);
    }

    if (modbus_connect (theMB.ctx) == -1)
    {
        modbus_free (theMB.ctx);
        theMB.ctx = NULL;
        return (-1);
    }

    // set TCP keepalive
    int sock = modbus_get_socket (theMB.ctx);

    int yes = 1;
    setsockopt (sock, SOL_SOCKET, SO_KEEPALIVE, &yes, sizeof (yes));

    int idle = 30;
    int intvl = 10;
    int cnt = 3;
    setsockopt (sock, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof (int));
    setsockopt (sock, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof (int));
    setsockopt (sock, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof (int));

    // set timeout for modbus response
    modbus_set_response_timeout (theMB.ctx, 5, 0);

    return (0);
}

static int ModbusReconnect (void)
{
    if (theMB.ctx)
    {
        modbus_close (theMB.ctx);
        modbus_free (theMB.ctx);
        theMB.ctx = NULL;
    }

    sleep (1);  // important to avoid TIME_WAIT

    return (ModbusConnectSafe ());
}

static int ModbusReadSafe (int addr, int nb, uint16_t *dest)
{
    int rc = modbus_read_registers (theMB.ctx, addr, nb, dest);

    if (rc == -1)
    {
        TimeLog ("ModbusReadSafe: %s. Reconnecting...", modbus_strerror (errno));

        if (ModbusReconnect () == 0)
        {
            Log ("succesful\n");
            // one retry
            rc = modbus_read_registers (theMB.ctx, addr, nb, dest);
        }
        else
        {
            Log ("failed\n");
        }
    }
    return (rc);
}

static  bool    ResolveHostname (const char *hostname, char *ipStr, size_t ipStrLen)
{
    struct addrinfo hints = {0}, *res;
    hints.ai_family = AF_INET;  // IPv4

    if (getaddrinfo (hostname, NULL, &hints, &res) != 0)
    {
        return (false);
    }

    struct sockaddr_in *addr = (struct sockaddr_in *) res->ai_addr;
    inet_ntop (AF_INET, &addr->sin_addr, ipStr, ipStrLen);

    freeaddrinfo (res);
    return (true);
}

bool    AlphaESSConnect (char* networkName)
{
    char    ipStr [80];

    if (!ResolveHostname (networkName, ipStr, sizeof (ipStr)))
    {
        Log ("AlphaESSConnect: could not resolve %s\n", networkName);
        return (false);
    }

    strncpy (theMB.ip, ipStr, sizeof (theMB.ip));
    theMB.port = 502;
    theMB.ctx = NULL;

    if (ModbusConnectSafe () != 0)
    {
        return (false);
    }

    return (true);
}

int GetDispatchParam (dispatch_t* dispatchParam)
{
    int      ret;
    uint16_t data [12];

    if ((ret = ModbusReadSafe (0x0880, 11, data)) < 0)
    {
        TimeLog ("GetDispatchParam: %s\n", modbus_strerror (errno));
        return (-1);
    }
    Debug (3, (char*) "Read dispatch: %s, power: %u W, discharge-cutoff-SOC: %u%%, mode: %u, cutoff-SOC: %u, dispatch-time: %us, p7: %u, PV-switch: %u\n",
        (data [0] == 1) ? "start" : "stop", ToUnsignedInt (data [1], data [2]), ToUnsignedInt (data [3], data [4]), data [5], data [6], ToUnsignedInt (data [7], data [8]), data [9], data [10]);

    uint16_t power = ToUnsignedInt (data [1], data [2]);
    dispatchParam->mode = data [5];
    dispatchParam->started = (data [0] == 1);
    dispatchParam->power = (power >= 32000) ? 0 - (power - 32000) : power;    // positive power = charge
    dispatchParam->cutoffSOC = (double) data [6] * 4;
    dispatchParam->duration = ToUnsignedInt (data [7], data [8]);
    dispatchParam->para7 = data[9];
    dispatchParam->PVOn = (data [10] == 1);

    return (0);
}

// cutoffSOC: 100% = 1000
int SetDispatchParam (dispatch_t dispatchParam)
{
    int      ret;
    uint16_t data [12];
    uint16_t power;

    if (dispatchParam.cutoffSOC > 1000)
    {
        dispatchParam.cutoffSOC = 1000;
    }

    if (CheckDispatchMode (dispatchParam.mode, dispatchParam.cutoffSOC) < 0)
    {
        return (-1);
    }

    data [5] = dispatchParam.mode;
    data [0] = dispatchParam.started ? 1 : 0;
    power = dispatchParam.power < 0 ? (32000 + abs (dispatchParam.power)) : dispatchParam.power;  // positive power = charge
    data [1] = (power / 256 / 256) & 0xffff;
    data [2] = power & 0xffff;
    data [3] = 0;
    data [4] = 0;
    data [6] = (uint16_t) (dispatchParam.cutoffSOC / 4);
    data [7] = (dispatchParam.duration / 256 / 256) & 0xffff;
    data [8] = dispatchParam.duration & 0xffff;
    data [9] = 255; // dispatchParam.para7;
    data [10] = dispatchParam.PVOn ? 1 : 2;

    Debug (3, (char*)"Write dispatch: %s, power: %u W, discharge-cutoff-SOC: %u%%, mode: %u, cutoff-SOC: %u, dispatch-time: %us, p7: %u, PV-switch: %u\n",
        (data[0] == 1) ? "start" : "stop", ToUnsignedInt (data[1], data[2]), ToUnsignedInt (data [3], data [4]), data [5], data [6], ToUnsignedInt (data [7], data [8]), data[9], data[10]);
    if ((ret = modbus_write_registers (theMB.ctx, 0x0880, 11, data)) < 0)
    {
        TimeLog ("SetDispatchParam: %s\n", modbus_strerror (errno));
        return (-1);
    }

    return (0);
}

// 1%
int SetMaxFeedIntoGrid (uint16_t maxFeed)
{
    int      ret;
    uint16_t data[3];

    if (maxFeed > 100)
    {
        maxFeed = 100;
    }
    Debug (2, (char*) "Setting Max Feed into Grid: %u\n", maxFeed);
    data[0] = maxFeed;
    if ((ret = modbus_write_registers (theMB.ctx, 0x0800, 1, data)) < 0)
    {
        TimeLog ("SetMaxFeedIntoGrid: %s\n", modbus_strerror (errno));
        return (-1);
    }
    return (0);
}

// 1%
uint16_t GetMaxFeedIntoGrid (void)
{
    int      ret;
    uint16_t data[3];

    if ((ret = ModbusReadSafe (0x0800, 1, data)) < 0)
    {
        TimeLog ("GetMaxFeedIntoGrid: %s\n", modbus_strerror (errno));
        return (-1);
    }
    return (data[0]);
}

// 0.1%
int GetSOC (void)
{
    int      ret;
    uint16_t data[1];

    if ((ret = ModbusReadSafe (0x0102, 1, data)) < 0)
    {
        TimeLog ("GetSOC: %s\n", modbus_strerror (errno));
        return (-1);
    }
    return (data[0]);
}

// Watts, PV generated power
long GetPVPower (void)
{
    int      ret;
    uint16_t data[7];

    if ((ret = ModbusReadSafe (0x041F, 6, data)) < 0)
    {
        TimeLog ("GetPVPower: %s\n", modbus_strerror (errno));
        return (-1);
    }
    uint16_t pv1 = ToUnsignedInt (data [0], data [1]);
    uint16_t pv2 = ToUnsignedInt (data [4], data [5]);

    return (pv1 + pv2);
}

// Watts, - = charge, + = discharge
short GetBatteryPower (void)
{
    int      ret;
    uint16_t data [2];

    if ((ret = ModbusReadSafe (0x0126, 1, data)) < 0)
    {
        TimeLog ("GetBatteryPower: %s\n", modbus_strerror (errno));
        return (-1);
    }
    return (data [0]);
}

// Watts, - = return to grid, + = use from grid
int GetTotalActivePower (void)
{
    int      ret;
    uint16_t data [3] = {0};

    // from PV Meter Running Data
    if ((ret = ModbusReadSafe (0x0021, 2, data)) < 0)
    {
        TimeLog ("GetTotalActivePower: %s\n", modbus_strerror (errno));
        return (-1);
    }
    
    return (ToInt (data [0], data [1]));
}

// 1 bit = 0.01 kWh
unsigned int    GetTotalEnergyFeedToGrid (void)
{
    int      ret;
    uint16_t data [3] = {0};

    // from Grid Meter Running Data
    if ((ret = ModbusReadSafe (0x0010, 2, data)) < 0)
    {
        TimeLog ("GetTotalEnergyFeedToGrid: %s\n", modbus_strerror (errno));
        return (-1);
    }
    
    return (ToUnsignedInt (data [0], data [1]));
}

// 1 bit = 0.01 kWh
unsigned int    GetTotalEnergyConsumeFromGrid (void)
{
    int      ret;
    uint16_t data [3] = {0};

    // from Grid Meter Running Data
    if ((ret = ModbusReadSafe (0x0012, 2, data)) < 0)
    {
        TimeLog ("GetTotalEnergyConsumeFromGrid: %s\n", modbus_strerror (errno));
        return (-1);
    }
    
    return (ToUnsignedInt (data [0], data [1]));
}

// 1 bit = 0.01 kWh
unsigned int	GetPVTotalEnergyFeedToGrid (void)
{
    int      ret;
    uint16_t data [3] = {0};

    // from Grid Meter Running Data
    if ((ret = ModbusReadSafe (0x0090, 2, data)) < 0)
    {
        TimeLog ("GetPVTotalEnergyFeedToGrid: %s\n", modbus_strerror (errno));
        return (-1);
    }
    
    return (ToUnsignedInt (data [0], data [1]));
}

// 1 bit = 0.01 kWh
unsigned int	GetPVTotalEnergyConsumeFromGrid (void)
{
    int      ret;
    uint16_t data [3] = {0};

    // from Grid Meter Running Data
    if ((ret = ModbusReadSafe (0x0092, 2, data)) < 0)
    {
        TimeLog ("GetPVTotalEnergyConsumeFromGrid: %s\n", modbus_strerror (errno));
        return (-1);
    }
    
    return (ToUnsignedInt (data [0], data [1]));
}

int SetTimePeriodControl (uint16_t startHour, uint16_t startMin, uint16_t stopHour, uint16_t stopMin, uint16_t cutSOC, bool charge)
{
    int      ret;
    uint16_t data [20] = {0};

    Debug (3, "Setting Time Period Control for %s: %02u:%02u - %02u:%02u until SOC=%u%%\n", charge ? "charging" : "discharging", startHour, startMin, stopHour, stopMin, cutSOC);
    data [0] = charge ? 1 : 2;
    data [1] = 10; // set default to 10%
    if (!charge)
    {
        data [1] = cutSOC;
        data [2] = startHour;
        data [3] = stopHour;
        data [11] = startMin;
        data [12] = stopMin;
    }
    else
    {
        data [6] = cutSOC;
        data [7] = startHour;
        data [8] = stopHour;
        data [15] = startMin;
        data [16] = stopMin;
    }
    if ((ret = modbus_write_registers (theMB.ctx, 0x084F, 17, data)) < 0)
    {
        TimeLog ("SetTimePeriodControl: %s\n", modbus_strerror (errno));
        return (-1);
    }
    return (0);
}

const   char* GetDispatchMsg (uint16_t flag)
{
    switch (flag)
    {
        case 0: return ("Default");
        case 1: return ("Battery only charges from PV");
        case 2: return ("State of Charge control");
        case 3: return ("Load Following");
        case 4: return ("Maximise Output");
        case 5: return ("Normal");
        case 6: return ("Optimise Consumption");
        case 7: return ("Maximise Consumption");
        case 19: return ("No Battery Charge");
        default: return ("Unknown");
    };
}

const   char* GetDispatchPowerModeMsg (int power)
{
    if (power == 0)
    {
        return ("no-charging");
    }
    if (power > 0)
    {
        return ("charging");
    }
    if (power < 0)
    {
        return ("discharging");
    }
    return ("unknown");
}