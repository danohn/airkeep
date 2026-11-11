#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include "cJSON.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "nvs.h"
#include "display_relay.h"
#include "relay_snapshot.h"
#include "setup.h"
#include "ui.h"

#define SECOND 1000000LL
#define RESPONSE_LIMIT 16384
#define TOKEN_SIZE 44
#define ACCESS_TOKEN_SIZE 1536

static const char *TAG = "airkeep_relay";

typedef struct {
    char body[RESPONSE_LIMIT + 1];
    size_t size;
    bool overflow;
} response_t;

typedef struct {
    char access[ACCESS_TOKEN_SIZE];
    char refresh[TOKEN_SIZE];
    int64_t renew_at;
    bool revoked;
} credentials_t;

typedef struct {
    char device[TOKEN_SIZE];
    int interval;
    int64_t expires_at;
} pairing_t;

typedef struct {
    esp_http_client_handle_t http;
    response_t response;
    char origin[481];
    char url[520];
    char authorization[ACCESS_TOKEN_SIZE + 8];
} relay_t;

static esp_err_t receive(esp_http_client_event_t *event)
{
    response_t *response = event->user_data;
    if (event->event_id != HTTP_EVENT_ON_DATA || event->data_len <= 0) return ESP_OK;
    if ((size_t)event->data_len > RESPONSE_LIMIT - response->size) response->overflow = true;
    if (response->overflow) return ESP_FAIL;
    memcpy(response->body + response->size, event->data, event->data_len);
    response->size += event->data_len;
    return ESP_OK;
}

static int request(relay_t *relay, const char *path, const char *form, const char *access)
{
    snprintf(relay->url, sizeof(relay->url), "%s%s", relay->origin, path);
    relay->response.size = 0;
    relay->response.overflow = false;
    if (esp_http_client_set_url(relay->http, relay->url) != ESP_OK ||
        esp_http_client_set_method(relay->http, form ? HTTP_METHOD_POST : HTTP_METHOD_GET) != ESP_OK) return 0;
    if (access) {
        snprintf(relay->authorization, sizeof(relay->authorization), "Bearer %s", access);
        esp_err_t err = esp_http_client_set_header(relay->http, "Authorization", relay->authorization);
        memset(relay->authorization, 0, sizeof(relay->authorization));
        if (err != ESP_OK) return 0;
    } else {
        esp_http_client_delete_header(relay->http, "Authorization");
    }
    esp_err_t err = form ? esp_http_client_set_post_field(relay->http, form, strlen(form)) : ESP_OK;
    if (err == ESP_OK) err = esp_http_client_perform(relay->http);
    relay->response.body[relay->response.size] = 0;
    // Clear the form while its buffer is valid. A GET has no body to clear.
    if (form) esp_http_client_set_post_field(relay->http, NULL, 0);
    if (err != ESP_OK || relay->response.overflow) {
        ESP_LOGW(TAG, "%s failed: %s", path,
                 relay->response.overflow ? "response too large" : esp_err_to_name(err));
        esp_http_client_close(relay->http);
        return 0;
    }
    return esp_http_client_get_status_code(relay->http);
}

static bool copy_string(cJSON *json, const char *name, char *out, size_t size)
{
    const char *value = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(json, name));
    if (!value || !*value || strlen(value) >= size) return false;
    strlcpy(out, value, size);
    return true;
}

static bool url_token(const char *value, size_t length)
{
    return strlen(value) == length &&
        strspn(value, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-") == length;
}

static bool begin_pairing(relay_t *relay, const char *installation, pairing_t *pairing)
{
    char form[80];
    char code[10], uri[500], complete[520];
    snprintf(form, sizeof(form), "client_id=esp32&installation_id=%s", installation);
    if (request(relay, "/auth/device/code", form, NULL) != 200) return false;
    cJSON *json = cJSON_Parse(relay->response.body);
    const cJSON *interval_field = cJSON_GetObjectItemCaseSensitive(json, "interval");
    const cJSON *expiry = cJSON_GetObjectItemCaseSensitive(json, "expires_in");
    int interval = cJSON_IsNumber(interval_field) ? interval_field->valueint : 0;
    int lifetime = cJSON_IsNumber(expiry) ? expiry->valueint : 0;
    bool valid = copy_string(json, "device_code", pairing->device, sizeof(pairing->device)) &&
        url_token(pairing->device, 43) && copy_string(json, "user_code", code, sizeof(code)) &&
        url_token(code, 9) && code[4] == '-' &&
        copy_string(json, "verification_uri", uri, sizeof(uri)) &&
        copy_string(json, "verification_uri_complete", complete, sizeof(complete)) &&
        interval >= 5 && interval <= 60 && lifetime > 0 && lifetime <= 600;
    char expected[520];
    snprintf(expected, sizeof(expected), "%s/device", relay->origin);
    valid = valid && !strcmp(uri, expected);
    snprintf(expected, sizeof(expected), "%s/device#user_code=%s", relay->origin, valid ? code : "");
    valid = valid && !strcmp(complete, expected);
    cJSON_Delete(json);
    if (!valid) return false;
    pairing->interval = interval;
    pairing->expires_at = esp_timer_get_time() + lifetime * SECOND;
    ESP_LOGI(TAG, "Pairing ready");
    airkeep_ui_pairing(AIRKEEP_PAIRING, "Scan or visit the link", code, uri, complete);
    return true;
}

static bool save_tokens(relay_t *relay, nvs_handle_t nvs, credentials_t *creds, int64_t request_at)
{
    char refresh[TOKEN_SIZE];
    cJSON *json = cJSON_Parse(relay->response.body);
    const cJSON *expiry = cJSON_GetObjectItemCaseSensitive(json, "expires_in");
    int lifetime = cJSON_IsNumber(expiry) ? expiry->valueint : 0;
    bool valid = lifetime > 0 && lifetime <= 600 &&
        copy_string(json, "access_token", creds->access, sizeof(creds->access)) &&
        copy_string(json, "refresh_token", refresh, sizeof(refresh)) && url_token(refresh, 43);
    cJSON_Delete(json);
    char record[sizeof(relay->origin) + TOKEN_SIZE];
    if (valid) {
        snprintf(record, sizeof(record), "%s\n%s", relay->origin, refresh);
        valid = nvs_set_str(nvs, "session", record) == ESP_OK && nvs_commit(nvs) == ESP_OK;
    }
    if (!valid) return false;
    strlcpy(creds->refresh, refresh, sizeof(creds->refresh));
    creds->renew_at = request_at + (lifetime > 30 ? lifetime - 30 : lifetime) * SECOND;
    ESP_LOGI(TAG, "Display credential saved");
    return true;
}

static bool erase_session(nvs_handle_t nvs, credentials_t *creds)
{
    memset(creds, 0, sizeof(*creds));
    esp_err_t err = nvs_erase_key(nvs, "session");
    return err == ESP_ERR_NVS_NOT_FOUND || (err == ESP_OK && nvs_commit(nvs) == ESP_OK);
}

static void unpair(nvs_handle_t nvs, credentials_t *creds, const char *message)
{
    if (erase_session(nvs, creds)) airkeep_ui_pairing(AIRKEEP_UNPAIRED, message, NULL, NULL, NULL);
    else airkeep_ui_pairing(AIRKEEP_UNPAIRED, "Could not clear display credentials", NULL, NULL, NULL);
}

static void disconnect_display(relay_t *relay, nvs_handle_t nvs, credentials_t *creds, bool online)
{
    credentials_t old = *creds;
    unpair(nvs, creds, "Display disconnected");
    if (!online) return;
    if (old.access[0] && request(relay, "/auth/revoke", "", old.access) == 204) return;
    char form[112];
    snprintf(form, sizeof(form), "client_id=esp32&grant_type=refresh_token&refresh_token=%s", old.refresh);
    if (request(relay, "/auth/token", form, NULL) != 200) return;
    cJSON *json = cJSON_Parse(relay->response.body);
    if (copy_string(json, "access_token", old.access, sizeof(old.access)))
        request(relay, "/auth/revoke", "", old.access);
    cJSON_Delete(json);
}

static void poll_snapshot(relay_t *relay, credentials_t *creds)
{
    int status = request(relay, "/v1/snapshot", NULL, creds->access);
    if (status == 401 || status == 403) {
        creds->renew_at = 0;
        if (status == 403) creds->revoked = true;
        return;
    }
    if (status != 200) {
        airkeep_ui_status("Relay unavailable");
        return;
    }
    struct timeval now;
    gettimeofday(&now, NULL);
    airkeep_reading_t reading;
    bool connected;
    if (airkeep_parse_snapshot(relay->response.body, relay->response.size, airkeep_config.ams_id,
            now.tv_sec + now.tv_usec / 1000000.0, esp_timer_get_time(), &reading, &connected)) {
        static bool reported;
        if (!reported) {
            ESP_LOGI(TAG, "First relay reading received");
            reported = true;
        }
        airkeep_ui_reading(&reading);
        airkeep_ui_status(connected ? "Relay connected" : "Printer offline");
    } else {
        airkeep_ui_status("No valid AMS data");
    }
}

void airkeep_relay_task(void *network)
{
    relay_t *relay = calloc(1, sizeof(*relay));
    credentials_t *creds = calloc(1, sizeof(*creds));
    if (!relay || !creds) abort();
    strlcpy(relay->origin, airkeep_config.relay_url, sizeof(relay->origin));
    const esp_http_client_config_t config = {
        .url = relay->origin, .crt_bundle_attach = esp_crt_bundle_attach,
        .event_handler = receive, .user_data = &relay->response, .timeout_ms = 10000,
        .buffer_size_tx = ACCESS_TOKEN_SIZE + 512,
        .keep_alive_enable = true, .disable_auto_redirect = true,
    };
    relay->http = esp_http_client_init(&config);
    if (!relay->http) abort();

    nvs_handle_t nvs;
    ESP_ERROR_CHECK(nvs_open("airkeep_auth", NVS_READWRITE, &nvs));
    char installation[33];
    size_t size = sizeof(installation);
    if (nvs_get_str(nvs, "installation", installation, &size) != ESP_OK || !url_token(installation, 32)) {
        uint8_t random[16];
        esp_fill_random(random, sizeof(random));
        for (size_t i = 0; i < sizeof(random); i++) snprintf(installation + 2*i, 3, "%02x", random[i]);
        ESP_ERROR_CHECK(nvs_set_str(nvs, "installation", installation));
        ESP_ERROR_CHECK(nvs_commit(nvs));
    }
    char record[sizeof(relay->origin) + TOKEN_SIZE];
    size = sizeof(record);
    if (nvs_get_str(nvs, "session", record, &size) == ESP_OK) {
        char *separator = strchr(record, '\n');
        if (separator) {
            *separator++ = 0;
            if (!strcmp(record, relay->origin) && url_token(separator, 43))
                strlcpy(creds->refresh, separator, sizeof(creds->refresh));
        }
        if (!creds->refresh[0]) erase_session(nvs, creds);
    }

    pairing_t pairing = {0};
    int64_t next = 0;
    int network_delay = 5;
    airkeep_ui_pairing(creds->refresh[0] ? AIRKEEP_PAIRED : AIRKEEP_UNPAIRED,
                       creds->refresh[0] ? "Connecting to relay" : "Pair this display", NULL, NULL, NULL);
    for (;;) {
        bool online = (xEventGroupGetBits(network) & 1) != 0;
        airkeep_action_t action = airkeep_ui_action();
        if (action == AIRKEEP_CANCEL || action == AIRKEEP_DISCONNECT) {
            pairing = (pairing_t){0};
            if (creds->refresh[0]) disconnect_display(relay, nvs, creds, online);
            else airkeep_ui_pairing(AIRKEEP_UNPAIRED, "Pairing cancelled", NULL, NULL, NULL);
        } else if (action == AIRKEEP_PAIR && !creds->refresh[0] && !pairing.device[0]) {
            if (!online) {
                airkeep_ui_pairing(AIRKEEP_UNPAIRED, "Waiting for Wi-Fi", NULL, NULL, NULL);
            } else {
                airkeep_ui_pairing(AIRKEEP_PAIRING, "Starting pairing…", NULL, NULL, NULL);
                if (begin_pairing(relay, installation, &pairing)) {
                    next = esp_timer_get_time() + pairing.interval * SECOND;
                    network_delay = pairing.interval;
                } else {
                    pairing = (pairing_t){0};
                    airkeep_ui_pairing(AIRKEEP_RETRY, "Could not start pairing", NULL, NULL, NULL);
                }
            }
        }
        int64_t now = esp_timer_get_time();
        if (pairing.device[0] && now >= pairing.expires_at) {
            pairing = (pairing_t){0};
            airkeep_ui_pairing(AIRKEEP_RETRY, "Pairing code expired", NULL, NULL, NULL);
        }
        if (online && now >= next && (pairing.device[0] || creds->refresh[0])) {
            char form[160];
            bool pending = pairing.device[0];
            int delay = 5;
            if (!pending && creds->revoked) {
                unpair(nvs, creds, "Pair this display again");
            } else if (pending || now >= creds->renew_at) {
                if (pending) {
                    snprintf(form, sizeof(form), "client_id=esp32&grant_type=urn%%3Aietf%%3Aparams%%3Aoauth%%3Agrant-type%%3Adevice_code&device_code=%s", pairing.device);
                } else {
                    snprintf(form, sizeof(form), "client_id=esp32&grant_type=refresh_token&refresh_token=%s", creds->refresh);
                }
                int result = request(relay, "/auth/token", form, NULL);
                if (result == 200) {
                    pairing = (pairing_t){0};
                    if (save_tokens(relay, nvs, creds, now)) {
                        airkeep_ui_pairing(AIRKEEP_PAIRED, "Display paired", NULL, NULL, NULL);
                        network_delay = 5;
                        delay = 0;
                    } else {
                        if (creds->access[0]) request(relay, "/auth/revoke", "", creds->access);
                        unpair(nvs, creds, "Could not save display credentials");
                    }
                } else if (result == 400 || result == 401 || result == 403) {
                    cJSON *json = cJSON_Parse(relay->response.body);
                    const char *error = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(json, "error"));
                    if (pending && error && (!strcmp(error, "authorization_pending") || !strcmp(error, "slow_down"))) {
                        if (!strcmp(error, "slow_down")) pairing.interval += 5;
                        delay = network_delay = pairing.interval;
                    } else if (pending) {
                        pairing = (pairing_t){0};
                        airkeep_ui_pairing(AIRKEEP_RETRY, error && !strcmp(error, "expired_token") ? "Pairing code expired" :
                            error && !strcmp(error, "access_denied") ? "Pairing denied" : "Pairing failed", NULL, NULL, NULL);
                    } else {
                        unpair(nvs, creds, "Pair this display again");
                    }
                    cJSON_Delete(json);
                } else {
                    airkeep_ui_status("Relay unavailable");
                    if (network_delay < 60) network_delay = network_delay > 30 ? 60 : network_delay * 2;
                    delay = network_delay;
                }
            } else {
                poll_snapshot(relay, creds);
                if (!creds->renew_at) delay = 0;
            }
            next = esp_timer_get_time() + delay * SECOND;
        }
        vTaskDelay(pdMS_TO_TICKS(200));
    }
}
