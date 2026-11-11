idf_component_register(SRCS "main.c" "setup.c" "relay_snapshot.c" "display_relay.c" "ui.c" "display.c" "font_digits.c"
    INCLUDE_DIRS "." REQUIRES nvs_flash esp_wifi esp_event esp_netif esp_http_client mbedtls json esp_timer
    esp_app_format esp_driver_usb_serial_jtag http_parser
    PRIV_REQUIRES espressif__esp_lcd_touch espressif__esp_lvgl_port)
