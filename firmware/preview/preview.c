// Render firmware/main/ui.c into a host framebuffer.
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>
#include "lvgl.h"
#include "ui.h"

static uint32_t pixels[368 * 448], buffer[368 * 448];
static int64_t now_us=1000000;
static int brightness_value;
static lv_indev_data_t pointer;
static bool has_label(lv_obj_t *parent, const char *text)
{
    if (lv_obj_check_type(parent, &lv_label_class) && !strcmp(lv_label_get_text(parent), text)) return true;
    for (uint32_t i=0; i<lv_obj_get_child_count(parent); i++)
        if (has_label(lv_obj_get_child(parent, i), text)) return true;
    return false;
}
void bsp_display_brightness_set(int value) { brightness_value=value; }
static void read_pointer(lv_indev_t *input, lv_indev_data_t *data) { (void)input; *data=pointer; }
static void tap(lv_indev_t *input, int x, int y)
{
    pointer.point=(lv_point_t){x,y};
    pointer.state=LV_INDEV_STATE_PRESSED;
    lv_indev_read(input);
    lv_tick_inc(40); lv_timer_handler();
    pointer.state=LV_INDEV_STATE_RELEASED;
    lv_indev_read(input);
    lv_tick_inc(40); lv_timer_handler();
    lv_refr_now(NULL);
}
int64_t esp_timer_get_time(void) { return now_us; }
static void flush(lv_display_t *display,const lv_area_t *area,uint8_t *map)
{
    int width=area->x2-area->x1+1;
    for(int y=area->y1;y<=area->y2;y++)
        for(int x=area->x1;x<=area->x2;x++)
            pixels[y*368+x]=((uint32_t *)map)[(y-area->y1)*width+x-area->x1];
    lv_display_flush_ready(display);
}
lv_display_t *airkeep_display_start(void)
{
    lv_init();
    lv_display_t *display=lv_display_create(368,448);
    lv_display_set_color_format(display,LV_COLOR_FORMAT_XRGB8888);
    lv_display_set_buffers(display,buffer,NULL,sizeof(buffer),LV_DISPLAY_RENDER_MODE_FULL);
    lv_display_set_flush_cb(display,flush);
    return display;
}
int main(int argc,char **argv)
{
    airkeep_ui_start();
    if (argc>1 && !strcmp(argv[1],"--test-relay-state")) {
        airkeep_reading_t reading={.humidity=25,.temperature=27.1,.sampled_us=now_us-120000000,.stale_after_seconds=60};
        airkeep_ui_reading(&reading);
        airkeep_ui_status("Relay unavailable");
        lv_tick_inc(1000); lv_timer_handler();
        assert(has_label(lv_screen_active(),"Updated 2m ago · stale"));
        assert(has_label(lv_screen_active(),"Relay unavailable"));
        now_us+=60000000;
        airkeep_ui_reading(&reading); // A cached sample must continue aging.
        lv_tick_inc(1000); lv_timer_handler();
        assert(has_label(lv_screen_active(),"Updated 3m ago · stale"));
        reading.sampled_us=now_us;
        airkeep_ui_reading(&reading);
        airkeep_ui_status("Relay connected");
        lv_tick_inc(1000); lv_timer_handler();
        assert(has_label(lv_screen_active(),"Updated 0s ago"));
        assert(has_label(lv_screen_active(),"Relay connected"));
        puts("LVGL relay: old cached sample, outage status, aging and recovery passed");
        return 0;
    }
    if (argc>1 && !strcmp(argv[1],"--test-touch")) {
        lv_refr_now(NULL);
        lv_obj_t *details=lv_obj_get_child(lv_screen_active(),-1);
        lv_indev_t *input=lv_indev_create();
        lv_indev_set_type(input,LV_INDEV_TYPE_POINTER);
        lv_indev_set_read_cb(input,read_pointer);
        lv_indev_set_display(input,lv_display_get_default());
        assert(lv_obj_is_hidden(details));
        tap(input,232,447); // Actual bottom-edge coordinate seen on the board.
        assert(!lv_obj_is_hidden(details));
        tap(input,300,354); // Drag starts below the visual track on hardware.
        assert(brightness_value>=85);
        tap(input,232,447);
        assert(lv_obj_is_hidden(details));
        puts("LVGL touch: bottom-edge Details/Back and brightness slider passed");
        return 0;
    }
    airkeep_ui_status("Sample data");
    for(int n=1;n<=120;n++) {
        now_us+=30000000;
        airkeep_reading_t value={.humidity=23+(n%17==0?2:n%11==0?1:0),.temperature=25.6,.raw_index=4,.sampled_us=now_us};
        airkeep_ui_reading(&value);
        lv_tick_inc(1000); lv_timer_handler();
    }
    lv_refr_now(NULL);
    FILE *file=fopen(argc>1?argv[1]:"airkeep.ppm","wb");
    if(!file) return 1;
    fprintf(file,"P6\n368 448\n255\n");
    for(int i=0;i<368*448;i++) { uint32_t color=pixels[i]; fputc(color>>16,file); fputc(color>>8,file); fputc(color,file); }
    fclose(file);
    return 0;
}
