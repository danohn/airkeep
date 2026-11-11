#include <stdio.h>
#include <string.h>
#include "cJSON.h"
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "http_parser.h"
#include "nvs.h"
#include "sdkconfig.h"
#include "setup.h"

#define SETUP_NAMESPACE "airkeep_setup"
#define CONFIG_KEY "config_v1"
#define MAX_COMMAND 1536

airkeep_config_t airkeep_config = {
    .ssid = CONFIG_AIRKEEP_WIFI_SSID,
    .password = CONFIG_AIRKEEP_WIFI_PASSWORD,
    .relay_url = CONFIG_AIRKEEP_RELAY_URL,
    .ams_id = CONFIG_AIRKEEP_AMS_ID,
};

static bool normalize_config(airkeep_config_t *config)
{
    size_t ssid_len = strnlen(config->ssid, sizeof(config->ssid));
    size_t password_len = strnlen(config->password, sizeof(config->password));
    size_t url_len = strnlen(config->relay_url, sizeof(config->relay_url));
    size_t ams_len = strnlen(config->ams_id, sizeof(config->ams_id));
    if (!ssid_len || ssid_len >= sizeof(config->ssid) ||
        (password_len && password_len < 8) || password_len >= sizeof(config->password) ||
        url_len >= sizeof(config->relay_url) || strncmp(config->relay_url, "https://", 8) ||
        !ams_len || ams_len >= sizeof(config->ams_id)) return false;
    if (password_len == 64 && strspn(config->password, "0123456789abcdefABCDEF") != 64) return false;
    if (strspn(config->ams_id, "0123456789") != ams_len) return false;

    struct http_parser_url url = {0};
    if (strpbrk(config->relay_url, "?#@\\") ||
        http_parser_parse_url(config->relay_url, url_len, 0, &url) ||
        !(url.field_set & (1 << UF_HOST)) ||
        (url.field_set & ((1 << UF_USERINFO) | (1 << UF_QUERY) | (1 << UF_FRAGMENT))) ||
        ((url.field_set & (1 << UF_PORT)) && !url.port)) return false;
    if (url.field_set & (1 << UF_PATH)) {
        if (url.field_data[UF_PATH].len != 1 || config->relay_url[url_len - 1] != '/') return false;
        config->relay_url[url_len - 1] = '\0';
    }
    return true;
}

static bool copy_field(const cJSON *json, const char *name, char *target, size_t capacity)
{
    const char *value = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(json, name));
    return value && strlcpy(target, value, capacity) < capacity;
}

static void save_config(const char *payload)
{
    airkeep_config_t config = {0};
    cJSON *json = NULL;
    // cJSON exposes C strings. Reject a JSON NUL escape before validating lengths.
    for (const char *p = payload; *p; ++p) {
        if (*p != '\\' || !p[1]) continue;
        if (!strncmp(p + 1, "u0000", 5)) {
            puts("AIRKEEP_ERROR invalid-settings");
            return;
        }
        ++p;
    }
    json = cJSON_ParseWithOpts(payload, NULL, true);
    bool valid = cJSON_IsObject(json) &&
        copy_field(json, "ssid", config.ssid, sizeof(config.ssid)) &&
        copy_field(json, "password", config.password, sizeof(config.password)) &&
        copy_field(json, "relayUrl", config.relay_url, sizeof(config.relay_url)) &&
        copy_field(json, "amsId", config.ams_id, sizeof(config.ams_id)) &&
        normalize_config(&config);
    cJSON_Delete(json);
    if (!valid) {
        puts("AIRKEEP_ERROR invalid-settings");
        return;
    }

    nvs_handle_t nvs;
    esp_err_t result = nvs_open(SETUP_NAMESPACE, NVS_READWRITE, &nvs);
    if (result == ESP_OK) {
        result = nvs_set_blob(nvs, CONFIG_KEY, &config, sizeof(config));
        if (result == ESP_OK) result = nvs_commit(nvs);
        nvs_close(nvs);
    }
    if (result != ESP_OK) {
        puts("AIRKEEP_ERROR save-failed");
        return;
    }
    puts("AIRKEEP_OK saved");
    fflush(stdout);
    usb_serial_jtag_wait_tx_done(pdMS_TO_TICKS(500));
    esp_restart();
}

static void setup_task(void *arg)
{
    (void)arg;
    char line[MAX_COMMAND + 1];
    size_t used = 0;
    bool discard = false;
    for (;;) {
        char byte;
        if (usb_serial_jtag_read_bytes(&byte, 1, portMAX_DELAY) != 1) continue;
        if (byte == '\r') continue;
        if (byte == '\n') {
            line[used] = '\0';
            if (discard) puts("AIRKEEP_ERROR invalid-command");
            else if (!strcmp(line, "AIRKEEP_HELLO")) {
                printf("AIRKEEP_READY 1 %s\n", esp_app_get_description()->version);
            } else if (!strncmp(line, "AIRKEEP_CONFIG ", sizeof("AIRKEEP_CONFIG ") - 1)) {
                save_config(line + sizeof("AIRKEEP_CONFIG ") - 1);
            }
            fflush(stdout);
            memset(line, 0, used);
            used = 0;
            discard = false;
        } else if (used == MAX_COMMAND || !byte) discard = true;
        else if (!discard) line[used++] = byte;
    }
}

bool airkeep_setup_start(void)
{
    nvs_handle_t nvs;
    if (nvs_open(SETUP_NAMESPACE, NVS_READONLY, &nvs) == ESP_OK) {
        airkeep_config_t stored;
        size_t size = sizeof(stored);
        if (nvs_get_blob(nvs, CONFIG_KEY, &stored, &size) == ESP_OK &&
            size == sizeof(stored) && normalize_config(&stored)) airkeep_config = stored;
        nvs_close(nvs);
    }
    const usb_serial_jtag_driver_config_t usb = {.rx_buffer_size = 1024, .tx_buffer_size = 1024};
    if (usb_serial_jtag_driver_install(&usb) == ESP_OK) {
        usb_serial_jtag_vfs_use_driver();
        if (xTaskCreate(setup_task, "airkeep_setup", 6144, NULL, 4, NULL) != pdPASS) {
            ESP_LOGE("airkeep_setup", "Could not start USB setup");
        }
    } else ESP_LOGE("airkeep_setup", "Could not initialize USB setup");
    return normalize_config(&airkeep_config);
}
