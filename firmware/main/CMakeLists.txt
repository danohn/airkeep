idf_component_register(SRCS "main.c" "relay_snapshot.c" "ui.c" "display.c" "font_digits.c"
    INCLUDE_DIRS "." REQUIRES nvs_flash esp_wifi esp_event esp_netif esp_http_client mbedtls json esp_timer
    PRIV_REQUIRES espressif__esp_lcd_touch espressif__esp_lvgl_port)
