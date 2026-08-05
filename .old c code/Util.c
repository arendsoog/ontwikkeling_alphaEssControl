#include <stdio.h>
#include <errno.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <curl/curl.h>
#include "Util.h"

#define	MSG_LENGTH	(1024)

static	int		theDebugLevel = 0;

static	FILE	*fp = NULL;

void	CloseLogfile (void)
{
	if (fp != NULL)
	{
		fclose (fp);
	}
}

void	SetLogfileName (char *fileName)
{
	CloseLogfile ();
	if ((fp = fopen (fileName, "a+")) == NULL)
	{
		printf ("Cannot open logfile (%s), errno = %d\n", fileName, errno);
		exit (1);
	}
}

void	Log (char *fmt, ...)
{
	va_list	ap;
	char	msg [MSG_LENGTH];

	va_start (ap, fmt);
	vsnprintf (msg, sizeof (msg), fmt, ap);
	va_end (ap);

	printf ("%s", msg);		/* @@@ */

	if (fp != NULL)
	{
		fprintf (fp, msg, strlen (msg));
		fflush (fp);
	}
}

void	TimeLog (char *fmt, ...)
{
	va_list	ap;
	char	msg [MSG_LENGTH];

	va_start (ap, fmt);
	vsnprintf (msg, sizeof (msg), fmt, ap);
	va_end (ap);

	time_t now = time (NULL);
	char* p = ctime (&now);
	Log ("%24.24s: %s", p, msg);
}

void	Shutdown (int signal)
{
	static	int	first = 1;

	if (first)								/* avoid looping				*/
	{
		first = 0;
		TimeLog ("*** SHUTDOWN on signal %d ***\n", signal);
	}

	exit (0);
}

void	SetDebug (int debugLevel)
{
	theDebugLevel = debugLevel;
}

void	Debug (int debugLevel, char *fmt, ...)
{
	va_list	ap;
	char	msg [MSG_LENGTH];

	if (debugLevel > theDebugLevel)			/* check if debug is in level	 */
	{
		return;
	}

	va_start (ap, fmt);
	vsnprintf (msg, sizeof(msg), fmt, ap);
	va_end (ap);

	Log ((char*)"%s", msg);
}

// ISO8601 formaat: 2025-12-02T01:00:00.000Z or 2025-12-20T23:00:00Z
bool    parseISO8601 (const char *str, int* year, int* mon, int *day, int* hour, int* min)
{
    double sec;

    int r = sscanf (str, "%d-%d-%dT%d:%d:%lfZ", year, mon, day, hour, min, &sec);

    return (r == 6); // success
}

struct  tm UTC2CET (int year, int month, int day, int hour, int min)
{
    time_t  t_of_day;
    struct  tm tmUTC;

    tmUTC.tm_year = year - 1900;       // Year - 1900
    tmUTC.tm_mon = month - 1;          // Month, where 0 = jan
    tmUTC.tm_mday = day;               // Day of the month
    tmUTC.tm_hour = hour;    
    tmUTC.tm_min = min;
    tmUTC.tm_sec = 0;
    tmUTC.tm_isdst = -1;                // Is DST on? 1 = yes, 0 = no, -1 = unknown

    //putenv ("TZ=UTC");                 // info is delivered in UTC
    //t_of_day = mktime (&tmUTC);        // get seconds since 1970 for UTC
    //putenv ("TZ=CET");                 // restore my time zone

	//setenv ("TZ", "UTC", 1);
    //tzset ();
    //t_of_day = mktime (&tmUTC);

    // Zet TZ naar CET (Europa/Amsterdam)
    //setenv ("TZ", "Europe/Amsterdam", 1);
    //tzset ();
	
	t_of_day = timegm (&tmUTC); // direct van UTC naar time_t

	return (*localtime (&t_of_day));   // get time struct in CET, corrected for zone and DST
}

// Callback to store data received
size_t WriteMemoryCallback (void *contents, size_t size, size_t nmemb, void *userp)
{
    size_t realsize = size * nmemb;
    curlResponseBuffer_t* mem = (curlResponseBuffer_t*) userp;

    char *ptr = realloc (mem->memory, mem->size + realsize + 1);
    if (!ptr)
    {
        Log ((char*)"WriteMemoryCallback: out of memory\n");
        return (0);
    }

    mem->memory = ptr;
    memcpy (&(mem->memory [mem->size]), contents, realsize);
    mem->size += realsize;
    mem->memory [mem->size] = '\0';

    return (realsize);
}

// call URL en return response in allocated memory
char* CallURLGetResponse (const char *url, const char* acceptHeader, const char* contentHeader, const char *authHeader, const char *postField, long timeOut)
{
    CURL*       curl;
    CURLcode	res;

    if (!(curl = curl_easy_init ()))
    {
        return (NULL);
    }

    curlResponseBuffer_t chunk;
    chunk.memory = malloc (1);
    chunk.size = 0;

    struct curl_slist *headers = NULL;
    headers = curl_slist_append (headers, acceptHeader);
    if (contentHeader)
    {
        headers = curl_slist_append (headers, contentHeader);
    }
    if (authHeader)
    {
        headers = curl_slist_append (headers, authHeader);
    }
    curl_easy_setopt (curl, CURLOPT_HTTPHEADER, headers);
	
	if (postField)
	{
		curl_easy_setopt (curl, CURLOPT_POSTFIELDS, postField);
	}
    
    curl_easy_setopt (curl, CURLOPT_VERBOSE, 0); 
    curl_easy_setopt (curl, CURLOPT_URL, url);
    curl_easy_setopt (curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt (curl, CURLOPT_TIMEOUT, timeOut);
    curl_easy_setopt (curl, CURLOPT_WRITEFUNCTION, WriteMemoryCallback);
    curl_easy_setopt (curl, CURLOPT_WRITEDATA, (void *) &chunk);

    //Log ("Calling %s\n", url);
    //if (postField)
    //{
    //    Log ("with post %s\n", postField);
    //}

    if ((res = curl_easy_perform (curl)) != CURLE_OK)
    {
		Log ("Curl_easy_perform () failed: %s\n", curl_easy_strerror (res));
        free (chunk.memory);
        curl_slist_free_all (headers);
        curl_easy_cleanup (curl);
        return (NULL);
    }

    curl_slist_free_all (headers);
    curl_easy_cleanup (curl);

    //Log ("Received >>\n%s\n<<\n", chunk.memory);

    return (chunk.memory);
}
