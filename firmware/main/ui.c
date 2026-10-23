#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "bsp/esp-bsp.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "lvgl.h"
#include "ui.h"
#include "display.h"

LV_FONT_DECLARE(airkeep_digits_128);
static QueueHandle_t readings, statuses;
static lv_obj_t *value, *temperature, *status, *bar, *chart, *details, *detail_text, *age, *brightness_text;
static lv_chart_series_t *series;
static airkeep_reading_t current;
static bool has_reading;
static int64_t chart_at;
static int history[120], history_count, history_write;
static const uint32_t amber = 0xFFB72D, ivory = 0xF5F0E6, muted = 0x9B9A96;

static lv_obj_t *label(lv_obj_t *parent, const char *text, int x, int y, const lv_font_t *font, uint32_t color)
{
    lv_obj_t *obj = lv_label_create(parent);
    lv_label_set_text(obj,text);
    lv_obj_set_pos(obj,x,y);
    lv_obj_set_style_text_font(obj,font,0);
    lv_obj_set_style_text_color(obj,lv_color_hex(color),0);
    return obj;
}

static void divider(lv_obj_t *parent, int y)
{
    lv_obj_t *obj=lv_obj_create(parent);
    lv_obj_remove_style_all(obj);
    lv_obj_set_pos(obj,20,y); lv_obj_set_size(obj,328,1);
    lv_obj_set_style_bg_color(obj,lv_color_hex(0x333331),0);
    lv_obj_set_style_bg_opa(obj,LV_OPA_COVER,0);
}

static void toggle_details(lv_event_t *event)
{
    (void)event;
    lv_obj_set_hidden(details,!lv_obj_is_hidden(details));
    ESP_LOGI("airkeep_ui", "Touch: details %s", lv_obj_is_hidden(details) ? "closed" : "opened");
}

static void brightness(lv_event_t *event)
{
    int percent=lv_slider_get_value(lv_event_get_target(event));
    char text[40];
    snprintf(text,sizeof(text),"Brightness · %d%%",percent);
    lv_label_set_text(brightness_text,text);
    bsp_display_brightness_set(percent);
    static int logged=-1;
    if (logged<0 || abs(percent-logged)>=5) {
        ESP_LOGI("airkeep_ui", "Brightness %d%%",percent);
        logged=percent;
    }
}

static void timer(lv_timer_t *timer)
{
    (void)timer;
    char text[256], connection[80];
    if (xQueueReceive(readings,&current,0)==pdTRUE) {
        has_reading=true;
        snprintf(text,sizeof(text),"%d",current.humidity); lv_label_set_text(value,text);
        lv_bar_set_value(bar,current.humidity,LV_ANIM_OFF);
        if (isfinite(current.temperature)) snprintf(text,sizeof(text),"%.1f°C",current.temperature);
        else snprintf(text,sizeof(text),"--°C");
        lv_label_set_text(temperature,text);
        if (!chart_at || current.sampled_us-chart_at>=30000000LL) {
            history[history_write]=current.humidity;
            history_write=(history_write+1)%120;
            if (history_count<120) history_count++;
            int low=100, high=0;
            for (int i=0;i<history_count;i++) { if(history[i]<low) low=history[i]; if(history[i]>high) high=history[i]; }
            lv_chart_set_range(chart,LV_CHART_AXIS_PRIMARY_Y,low>5?low-5:0,high<95?high+5:100);
            lv_chart_set_next_value(chart,series,current.humidity);
            chart_at=current.sampled_us;
        }
    }
    if (xQueueReceive(statuses,connection,0)==pdTRUE) lv_label_set_text(status,connection);
    int seconds = has_reading ? (int)((esp_timer_get_time()-current.sampled_us)/1000000) : -1;
    int stale_after = current.stale_after_seconds > 0 ? current.stale_after_seconds : 90;
    bool stale = seconds<0 || seconds>stale_after;
    lv_obj_set_style_text_color(value,lv_color_hex(stale ? muted : ivory),0);
    if (seconds<0) snprintf(text,sizeof(text),"Waiting for first reading");
    else if (seconds>stale_after) snprintf(text,sizeof(text),"Updated %dm ago · stale",seconds/60);
    else snprintf(text,sizeof(text),"Updated %ds ago",seconds);
    lv_label_set_text(age,text);
    snprintf(text,sizeof(text),"Relay %s\nAMS ID %s\n\nAir humidity, not filament moisture.\n\nRaw humidity index: %d\n5 = dry end, 1 = wet end\n\nHistory collected since boot.",
        CONFIG_AIRKEEP_RELAY_URL,CONFIG_AIRKEEP_AMS_ID,current.raw_index);
    lv_label_set_text(detail_text,text);
}

void airkeep_ui_reading(const airkeep_reading_t *reading) { xQueueOverwrite(readings,reading); }
void airkeep_ui_status(const char *text) { char out[80]; strlcpy(out,text,sizeof(out)); xQueueOverwrite(statuses,out); }

void airkeep_ui_start(void)
{
    readings=xQueueCreate(1,sizeof(airkeep_reading_t)); statuses=xQueueCreate(1,80);
    if (!readings || !statuses || !airkeep_display_start()) abort();
    bsp_display_brightness_set(65);
    if (!bsp_display_lock(3000)) abort();
    lv_obj_t *screen=lv_screen_active();
    lv_obj_set_style_bg_color(screen,lv_color_black(),0);
    lv_obj_set_style_text_color(screen,lv_color_hex(ivory),0);
    lv_obj_set_scrollable(screen,false);
    label(screen,"P2S / AMS 1",20,20,&lv_font_montserrat_18,ivory);
    label(screen,"RELATIVE HUMIDITY",20,52,&lv_font_montserrat_14,muted);
    value=label(screen,"--",15,71,&airkeep_digits_128,ivory);
    label(screen,"% RH",265,137,&lv_font_montserrat_24,amber);
    bar=lv_bar_create(screen); lv_obj_set_pos(bar,20,189); lv_obj_set_size(bar,328,9);
    lv_obj_set_style_bg_color(bar,lv_color_hex(0x292927),LV_PART_MAIN);
    lv_obj_set_style_bg_color(bar,lv_color_hex(amber),LV_PART_INDICATOR);
    label(screen,"0",20,204,&lv_font_montserrat_14,muted);
    label(screen,"100",318,204,&lv_font_montserrat_14,muted);
    divider(screen,229);
    label(screen,"Last hour",20,242,&lv_font_montserrat_14,ivory);
    chart=lv_chart_create(screen); lv_obj_set_pos(chart,20,271); lv_obj_set_size(chart,328,48);
    lv_obj_set_style_bg_opa(chart,LV_OPA_TRANSP,0); lv_obj_set_style_border_width(chart,0,0);
    lv_obj_set_style_pad_all(chart,0,0); lv_chart_set_div_line_count(chart,0,0);
    lv_chart_set_range(chart,LV_CHART_AXIS_PRIMARY_Y,0,100); lv_chart_set_point_count(chart,120);
    lv_obj_set_style_size(chart,0,0,LV_PART_INDICATOR);
    lv_obj_set_style_line_width(chart,2,LV_PART_ITEMS);
    series=lv_chart_add_series(chart,lv_color_hex(amber),LV_CHART_AXIS_PRIMARY_Y);
    lv_chart_set_all_value(chart,series,LV_CHART_POINT_NONE);
    divider(screen,328);
    temperature=label(screen,"--°C",20,343,&lv_font_montserrat_24,ivory);
    status=label(screen,"Starting",181,347,&lv_font_montserrat_14,muted);
    lv_obj_set_width(status,167); lv_label_set_long_mode(status,LV_LABEL_LONG_DOT);
    age=label(screen,"Waiting for first reading",20,375,&lv_font_montserrat_14,muted);
    lv_obj_t *button=lv_button_create(screen); lv_obj_set_pos(button,20,393); lv_obj_set_size(button,328,48);
    lv_obj_set_ext_click_area(button,12);
    lv_obj_set_style_bg_color(button,lv_color_hex(0x252524),0); lv_obj_set_style_shadow_width(button,0,0);
    lv_obj_t *caption=label(button,"Details",0,0,&lv_font_montserrat_18,ivory); lv_obj_center(caption);
    lv_obj_add_event_cb(button,toggle_details,LV_EVENT_CLICKED,NULL);
    details=lv_obj_create(screen); lv_obj_set_pos(details,0,0); lv_obj_set_size(details,368,448);
    lv_obj_set_style_bg_color(details,lv_color_black(),0); lv_obj_set_style_border_width(details,0,0);
    lv_obj_set_style_pad_all(details,20,0); lv_obj_set_hidden(details,true);
    lv_obj_set_scrollable(details,false);
    label(details,"Connection details",0,0,&lv_font_montserrat_24,ivory);
    detail_text=label(details,"",0,45,&lv_font_montserrat_14,muted); lv_obj_set_width(detail_text,320);
    brightness_text=label(details,"Brightness · 65%",0,279,&lv_font_montserrat_18,ivory);
    lv_obj_t *slider=lv_slider_create(details); lv_obj_set_pos(slider,8,304); lv_obj_set_size(slider,300,24);
    lv_obj_set_ext_click_area(slider,20);
    lv_slider_set_range(slider,10,100); lv_slider_set_value(slider,65,LV_ANIM_OFF);
    lv_obj_set_style_bg_color(slider,lv_color_hex(amber),LV_PART_INDICATOR);
    lv_obj_add_event_cb(slider,brightness,LV_EVENT_VALUE_CHANGED,NULL);
    lv_obj_t *back=lv_button_create(details); lv_obj_set_pos(back,0,354); lv_obj_set_size(back,320,72);
    lv_obj_set_ext_click_area(back,2);
    lv_obj_set_style_bg_color(back,lv_color_hex(0x252524),0);
    caption=label(back,"Back",0,0,&lv_font_montserrat_18,ivory); lv_obj_center(caption);
    lv_obj_add_event_cb(back,toggle_details,LV_EVENT_CLICKED,NULL);
    lv_timer_create(timer,1000,NULL);
    bsp_display_unlock();
    ESP_LOGI("airkeep_ui", "LVGL dashboard created at 368x448");
}
