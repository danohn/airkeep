#pragma once
#include <stdbool.h>
#include <stdint.h>
typedef struct {
    int humidity;
    float temperature;
    int raw_index;
    int64_t sampled_us;
    int stale_after_seconds;
} airkeep_reading_t;
void airkeep_ui_start(void);
void airkeep_ui_reading(const airkeep_reading_t *reading);
void airkeep_ui_status(const char *text);
