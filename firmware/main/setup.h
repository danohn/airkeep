#pragma once

#include <stdbool.h>

typedef struct {
    char ssid[33];
    char password[65];
    char relay_url[481];
    char ams_id[33];
} airkeep_config_t;

extern airkeep_config_t airkeep_config;

bool airkeep_setup_start(void);
