#pragma once
#include <stdbool.h>
#include <stdlib.h>

typedef struct
{
    char *memory;
    size_t size;
} curlResponseBuffer_t;

void	CloseLogfile (void);
void	SetLogfileName (char* fileName);
//void	ResetLogfileName (void);
void	Log (char* fmt, ...);
void	TimeLog (char* fmt, ...);
void	Shutdown (int signal);
void	SetDebug (int debugLevel);
void	Debug (int debugNo, char* fmt, ...);

bool    parseISO8601 (const char *str, int* year, int* mon, int *day, int* hour, int* min);
struct  tm UTC2CET (int year, int month, int day, int hour, int min);
size_t  WriteMemoryCallback (void *contents, size_t size, size_t nmemb, void *userp);
char*   CallURLGetResponse (const char *url, const char *acceptHeader, const char *contentHeader, const char *authHeader, const char *postField, long timeOut);