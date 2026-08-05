//
// Author: Jaco Melse (jjmelse@xs4all.nl)
// 2025 - 2026
//

#pragma once

#include <netdb.h>
#include <stdio.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>
#include <modbus/modbus.h>

#define DISPATCH_MODE_DEFAULT					0	
#define	DISPATCH_MODE_ONLY_CHARGE_FROM_PV		1	// In this mode, the battery is not allowed to discharge (Pdispatch < 32000). After the PV supplies the load, the excess energy is used to charge the battery. When the battery is charged, there is surplus power to the grid
#define	DISPATCH_MODE_STATE_OF_CHARGE_CONTROL	2	// Force charge mode. The charge or discharge process will be stopped until it reaches the SOC setting value
#define DISPATCH_MODE_LOAD_FOLLOWING			3	// The system will be self-consumption mode
#define	DISPATCH_MODE_MAXIMISE_OUTPUT			4	// If the current PV power can not meet the required inverter AC output power, the battery will also discharge
#define DISPATCH_MODE_NORMAL					5	// The system will be self-consumption mode
#define	DISPATCH_MODE_OPTIMISE_CONSUMPTION		6	// Currently PV will charge batteries firstly. If the PV power cannot meet the maximum battery charging power, it will also absorb electricity from the grid to charge the battery
#define DISPATCH_MODE_MAXIMISE_CONSUMPTION		7	// It will only absorb electricity from the grid to charge the battery 
#define	DISPATCH_MODE_ECO						8
#define	DISPATCH_MODE_FCAS						9
#define	DISPATCH_MODE_PV_POWER_SETTING			10
#define	DISPATCH_MODE_NO_BATTERY_CHARGE			19	// The system will be self-consumption mode, the charging power does not exceed the set power

typedef struct
{
	int		mode;
	bool	started;
	int		power;
	double	cutoffSOC;
	int		duration;
	int		para7;
	bool	PVOn;
} dispatch_t;

void			ClearBuf (uint16_t* dest, int l);
//double			ToWatt (uint16_t power);
//double			ToWatt2 (uint16_t powerHigh, uint16_t powerLow);
//unsigned int	ToUnsignedInt (uint16_t highByte, uint16_t lowByte);
//int				ToInt (uint16_t highByte, uint16_t lowByte);

bool    		AlphaESSConnect (char* ip);
int				GetDispatchParam (dispatch_t* dispatchParam);
int				SetDispatchParam (dispatch_t dispatchParam);
int				SetMaxFeedIntoGrid (uint16_t maxFeed);
uint16_t		GetMaxFeedIntoGrid (void);
int				GetSOC (void);
long			GetPVPower (void);
short			GetBatteryPower(void);
int				GetTotalActivePower (void);
unsigned int	GetTotalEnergyFeedToGrid (void);
unsigned int	GetTotalEnergyConsumeFromGrid (void);
unsigned int	GetPVTotalEnergyFeedToGrid (void);
unsigned int	GetPVTotalEnergyConsumeFromGrid (void);
int				SetTimePeriodControl (uint16_t startHour, uint16_t startMin, uint16_t stopHour, uint16_t stopMin, uint16_t cutSOC, bool charge);

//void			FormatWatts (int power, char* msg);
const	char*	GetDispatchMsg (uint16_t flag);
const	char*	GetDispatchPowerModeMsg (int power);