#pragma once
#include <stddef.h>
#include "ui.h"

// Translate original UTC sample time to the display's monotonic clock.
// Re-fetching a cached snapshot must never reset its sample age.
bool airkeep_parse_snapshot(const char *json, size_t length, const char *ams_id,
                            double now_unix, int64_t now_us,
                            airkeep_reading_t *reading, bool *printer_connected);
