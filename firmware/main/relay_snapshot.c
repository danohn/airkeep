#include <math.h>
#include <string.h>
#include "cJSON.h"
#include "relay_snapshot.h"

static bool finite_number(const cJSON *item)
{
    return cJSON_IsNumber(item) && isfinite(item->valuedouble);
}

bool airkeep_parse_snapshot(const char *json, size_t length, const char *ams_id,
                            double now_unix, int64_t now_us,
                            airkeep_reading_t *reading, bool *printer_connected)
{
    if (!json || !ams_id || !reading || !printer_connected || !isfinite(now_unix)) return false;
    cJSON *root = cJSON_ParseWithLengthOpts(json, length + 1, NULL, true);
    if (!root) return false;
    bool valid = false;
    const cJSON *server = cJSON_GetObjectItemCaseSensitive(root, "serverTime");
    const cJSON *stale = cJSON_GetObjectItemCaseSensitive(root, "staleAfter");
    const cJSON *connected = cJSON_GetObjectItemCaseSensitive(root, "printerConnected");
    const cJSON *units = cJSON_GetObjectItemCaseSensitive(root, "readings");
    if (!finite_number(server) || server->valuedouble <= 0 ||
        !finite_number(stale) || stale->valuedouble < 1 || stale->valuedouble > 86400 ||
        !cJSON_IsBool(connected) || !cJSON_IsArray(units)) goto done;
    const cJSON *unit;
    cJSON_ArrayForEach(unit, units) {
        const cJSON *id = cJSON_GetObjectItemCaseSensitive(unit, "amsID");
        if (!cJSON_IsString(id) || strcmp(id->valuestring, ams_id)) continue;
        const cJSON *humidity = cJSON_GetObjectItemCaseSensitive(unit, "humidity");
        const cJSON *observed = cJSON_GetObjectItemCaseSensitive(unit, "observedAt");
        if (!finite_number(humidity) || humidity->valuedouble < 1 || humidity->valuedouble > 100 ||
            !finite_number(observed) || observed->valuedouble <= 0 ||
            observed->valuedouble > server->valuedouble + 1) continue;
        double age = fmax(0, fmax(server->valuedouble, now_unix) - observed->valuedouble);
        if (age > 7 * 86400) continue;
        const cJSON *temp = cJSON_GetObjectItemCaseSensitive(unit, "temperature");
        const cJSON *index = cJSON_GetObjectItemCaseSensitive(unit, "rawIndex");
        *reading = (airkeep_reading_t){
            .humidity = (int)lround(humidity->valuedouble),
            .temperature = finite_number(temp) && temp->valuedouble >= 0 && temp->valuedouble <= 100
                ? (float)temp->valuedouble : NAN,
            .raw_index = finite_number(index) && index->valuedouble >= 1 && index->valuedouble <= 5
                && floor(index->valuedouble) == index->valuedouble ? index->valueint : 0,
            .sampled_us = now_us - (int64_t)(age * 1000000),
            .stale_after_seconds = (int)stale->valuedouble,
        };
        *printer_connected = cJSON_IsTrue(connected);
        valid = true;
        break;
    }
done:
    cJSON_Delete(root);
    return valid;
}
