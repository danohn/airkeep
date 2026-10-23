#include "display.h"
#include "bsp/esp-bsp.h"
#include "bsp/display.h"
#include "esp_lcd_touch.h"
#include "bsp/touch.h"
#include "esp_lvgl_port.h"
#include "esp_check.h"
#include "esp_timer.h"

static void raw_touch(esp_lcd_touch_handle_t handle, uint16_t *x, uint16_t *y,
                      uint16_t *strength, uint8_t *count, uint8_t max_count)
{
    (void)handle; (void)strength; (void)max_count;
    static int64_t logged_at;
    if (*count && esp_timer_get_time() - logged_at > 250000) {
        ESP_LOGI("airkeep_ui", "Raw touch %u,%u", x[0], y[0]);
        logged_at = esp_timer_get_time();
    }
}

static void touch_pressed(lv_event_t *event)
{
    lv_point_t point;
    lv_indev_get_point(lv_event_get_target(event), &point);
    ESP_LOGI("airkeep_ui", "Touch at %d,%d", (int)point.x, (int)point.y);
}

static void round_area(lv_event_t *event)
{
    lv_area_t *area = lv_event_get_param(event);
    area->x1 &= ~1;
    area->y1 &= ~1;
    area->x2 |= 1;
    area->y2 |= 1;
}

lv_display_t *airkeep_display_start(void)
{
    // BSP 2.0.3's convenience initializer selects the RGB path for this QSPI
    // panel and a large non-DMA buffer. Use its panel/touch drivers with the
    // SPI LVGL port, a small internal DMA buffer, and transfer-done callbacks.
    lvgl_port_cfg_t port = ESP_LVGL_PORT_INIT_CONFIG();
    ESP_ERROR_CHECK(lvgl_port_init(&port));
    esp_lcd_panel_handle_t panel;
    esp_lcd_panel_io_handle_t io;
    esp_lcd_touch_handle_t touch;
    bsp_display_config_t panel_config = {0};
    ESP_ERROR_CHECK(bsp_display_new(&panel_config, &panel, &io));
    ESP_ERROR_CHECK(bsp_touch_new(NULL, &touch));
    touch->config.process_coordinates = raw_touch;
    // Match the V2 vendor example's periodic interrupt/reporting mode.
    const uint8_t periodic = 0x40;
    ESP_ERROR_CHECK(esp_lcd_panel_io_tx_param(touch->io, 0xFA, &periodic, 1));
    const lvgl_port_display_cfg_t config = {
        .io_handle = io, .panel_handle = panel,
        .buffer_size = BSP_LCD_H_RES * 20,
        .hres = BSP_LCD_H_RES, .vres = BSP_LCD_V_RES,
        .color_format = LV_COLOR_FORMAT_RGB565,
        .flags = {.buff_dma = true, .swap_bytes = true},
    };
    lv_display_t *display = lvgl_port_add_disp(&config);
    if (!display) return NULL;
    lv_display_add_event_cb(display, round_area, LV_EVENT_INVALIDATE_AREA, NULL);
    const lvgl_port_touch_cfg_t touch_config = {.disp = display, .handle = touch};
    lv_indev_t *input = lvgl_port_add_touch(&touch_config);
    if (!input) return NULL;
    // Periodic reads detect release even if the controller only interrupts on
    // contact. A complete press/release is needed for LV_EVENT_CLICKED.
    lv_indev_set_mode(input, LV_INDEV_MODE_TIMER);
    lv_indev_add_event_cb(input, touch_pressed, LV_EVENT_PRESSED, NULL);
    return display;
}
