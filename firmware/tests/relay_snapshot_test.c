#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"
#include "relay_snapshot.h"

static const char *fixture = "{\"serverTime\":1000,\"staleAfter\":90,\"printerConnected\":true,"
    "\"readings\":[{\"amsID\":\"0\",\"humidity\":25,\"temperature\":27.1,\"rawIndex\":4,\"observedAt\":990}]}";

static bool parse(cJSON *root, double utc, int64_t mono, airkeep_reading_t *reading, bool *connected)
{
    char *json = cJSON_PrintUnformatted(root);
    bool result = airkeep_parse_snapshot(json, strlen(json), "0", utc, mono, reading, connected);
    free(json);
    return result;
}

int main(void)
{
    airkeep_reading_t reading;
    bool connected;
    cJSON *root = cJSON_Parse(fixture);
    assert(parse(root, 1000, 1000000, &reading, &connected));
    assert(reading.humidity == 25 && fabs(reading.temperature-27.1) < 0.001 && reading.raw_index == 4);
    assert(connected && reading.stale_after_seconds == 90);
    // A sample older than uptime still has a valid negative monotonic timestamp.
    assert(reading.sampled_us == -9000000);
    // Even a replayed HTTP body must age against the synchronized local clock.
    assert(parse(root, 1100, 101000000, &reading, &connected));
    assert(reading.sampled_us == -9000000);
    assert((101000000-reading.sampled_us)/1000000 == 110);
    cJSON_ReplaceItemInObject(root, "serverTime", cJSON_CreateNumber(1100));
    assert(parse(root, 1100, 101000000, &reading, &connected));
    assert(reading.sampled_us == -9000000);
    cJSON_ReplaceItemInObject(root, "printerConnected", cJSON_CreateBool(false));
    assert(parse(root, 1100, 101000000, &reading, &connected) && !connected);
    cJSON *unit = cJSON_GetArrayItem(cJSON_GetObjectItem(root, "readings"), 0);
    cJSON_ReplaceItemInObject(unit, "temperature", cJSON_CreateNull());
    cJSON_ReplaceItemInObject(unit, "rawIndex", cJSON_CreateNull());
    assert(parse(root, 1100, 101000000, &reading, &connected));
    assert(isnan(reading.temperature) && reading.raw_index == 0);
    cJSON_ReplaceItemInObject(unit, "humidity", cJSON_CreateNumber(0));
    assert(!parse(root, 1100, 101000000, &reading, &connected));
    cJSON_ReplaceItemInObject(unit, "humidity", cJSON_CreateNumber(101));
    assert(!parse(root, 1100, 101000000, &reading, &connected));
    cJSON_ReplaceItemInObject(unit, "humidity", cJSON_CreateString("25"));
    assert(!parse(root, 1100, 101000000, &reading, &connected));
    cJSON_ReplaceItemInObject(unit, "humidity", cJSON_CreateNumber(25));
    cJSON_ReplaceItemInObject(unit, "observedAt", cJSON_CreateNumber(1200));
    assert(!parse(root, 1100, 101000000, &reading, &connected));
    cJSON_DeleteItemFromObject(unit, "observedAt");
    assert(!parse(root, 1100, 101000000, &reading, &connected));
    cJSON_Delete(root);
    root = cJSON_Parse(fixture);
    unit = cJSON_GetArrayItem(cJSON_GetObjectItem(root, "readings"), 0);
    cJSON_ReplaceItemInObject(unit, "amsID", cJSON_CreateString("1"));
    assert(!parse(root, 1000, 1000000, &reading, &connected));
    cJSON_Delete(root);
    root = cJSON_Parse(fixture);
    cJSON_ReplaceItemInObject(root, "readings", cJSON_CreateObject());
    assert(!parse(root, 1000, 1000000, &reading, &connected));
    cJSON_Delete(root);
    assert(!airkeep_parse_snapshot("{}", 2, "0", 1000, 1, &reading, &connected));
    assert(!airkeep_parse_snapshot("{", 1, "0", 1000, 1, &reading, &connected));
    assert(!airkeep_parse_snapshot("{}junk", 6, "0", 1000, 1, &reading, &connected));
    puts("Relay snapshot: schema, cached-age preservation, offline, nullable fields and malformed data passed");
    return 0;
}
