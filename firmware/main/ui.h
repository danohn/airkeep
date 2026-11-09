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

typedef enum { AIRKEEP_STARTING, AIRKEEP_UNPAIRED, AIRKEEP_PAIRING, AIRKEEP_RETRY, AIRKEEP_PAIRED } airkeep_pairing_t;
typedef enum { AIRKEEP_NO_ACTION, AIRKEEP_PAIR, AIRKEEP_CANCEL, AIRKEEP_DISCONNECT } airkeep_action_t;
void airkeep_ui_pairing(airkeep_pairing_t state, const char *message,
                        const char *code, const char *uri, const char *qr);
airkeep_action_t airkeep_ui_action(void);
