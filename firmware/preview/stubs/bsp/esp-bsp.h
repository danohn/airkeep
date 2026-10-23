#pragma once
#include "lvgl.h"
lv_display_t *bsp_display_start(void);
static inline int bsp_display_lock(int timeout) { (void)timeout; return 1; }
static inline void bsp_display_unlock(void) {}
void bsp_display_brightness_set(int value);
