#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_sntp.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "relay_snapshot.h"
#include "ui.h"

static EventGroupHandle_t network;
static const char *TAG = "airkeep";
static int64_t logged_reading_at;


static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *event = data;
        ESP_LOGW(TAG, "Wi-Fi disconnected (reason %u); reconnecting", event->reason);
        xEventGroupClearBits(network, 1);
        airkeep_ui_status("Wi-Fi reconnecting");
        esp_wifi_connect();
    }
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = data;
        ESP_LOGI(TAG, "Wi-Fi ready, IP " IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(network, 1);
        airkeep_ui_status("Connecting to relay");
    }
}

#define MAX_SNAPSHOT_BYTES 16384

typedef struct {
    char body[MAX_SNAPSHOT_BYTES + 1];
    size_t length;
    bool overflow;
} snapshot_response_t;

static esp_err_t http_event(esp_http_client_event_t *event)
{
    snapshot_response_t *response = event->user_data;
    if (event->event_id == HTTP_EVENT_ON_DATA && event->data_len > 0) {
        if ((size_t)event->data_len > MAX_SNAPSHOT_BYTES - response->length) {
            response->overflow = true;
            return ESP_FAIL;
        }
        if (!response->overflow) {
            memcpy(response->body + response->length, event->data, event->data_len);
            response->length += event->data_len;
        }
    }
    return ESP_OK;
}

static void poll_relay(void *arg)
{
    (void)arg;
    snapshot_response_t *response = calloc(1, sizeof(*response));
    size_t token_length = strlen(CONFIG_AIRKEEP_RELAY_TOKEN);
    char *authorization = malloc(token_length + 8);
    if (!response || !authorization) abort();
    snprintf(authorization, token_length + 8, "Bearer %s", CONFIG_AIRKEEP_RELAY_TOKEN);
    char url[512];
    const char *base = CONFIG_AIRKEEP_RELAY_URL;
    snprintf(url, sizeof(url), "%s%sv1/snapshot", base, base[strlen(base)-1] == '/' ? "" : "/");
    esp_http_client_config_t config = {
        .url = url,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .event_handler = http_event,
        .user_data = response,
        .timeout_ms = 10000,
        .keep_alive_enable = true,
        // Never forward the bearer token to a redirect destination.
        .disable_auto_redirect = true,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) abort();
    ESP_ERROR_CHECK(esp_http_client_set_header(client, "Authorization", authorization));
    ESP_ERROR_CHECK(esp_http_client_set_header(client, "Accept", "application/json"));
    free(authorization);
    int previous_status = -1;
    for (;;) {
        xEventGroupWaitBits(network, 1, pdFALSE, pdTRUE, portMAX_DELAY);
        response->length = 0;
        response->overflow = false;
        esp_err_t err = esp_http_client_perform(client);
        int status = err == ESP_OK ? esp_http_client_get_status_code(client) : 0;
        if (status != previous_status) {
            ESP_LOGI(TAG, "Relay HTTPS status %d (%s)", status, esp_err_to_name(err));
            previous_status = status;
        }
        if (status == 200 && !response->overflow) {
            response->body[response->length] = '\0';
            struct timeval now;
            gettimeofday(&now, NULL);
            airkeep_reading_t reading;
            bool connected;
            if (airkeep_parse_snapshot(response->body, response->length, CONFIG_AIRKEEP_AMS_ID,
                    now.tv_sec + now.tv_usec / 1000000.0, esp_timer_get_time(), &reading, &connected)) {
                airkeep_ui_reading(&reading);
                airkeep_ui_status(connected ? "Relay connected" : "Printer offline");
                int64_t now_us = esp_timer_get_time();
                if (!logged_reading_at || now_us - logged_reading_at >= 30000000LL) {
                    ESP_LOGI(TAG, "Relay AMS %s: %d%% RH, %.1f C, sample age %lld s, printer %s",
                        CONFIG_AIRKEEP_AMS_ID, reading.humidity, reading.temperature,
                        (long long)((now_us-reading.sampled_us)/1000000), connected ? "online" : "offline");
                    logged_reading_at = now_us;
                }
            } else airkeep_ui_status("No valid AMS data");
        } else {
            airkeep_ui_status(status == 401 || status == 403 ? "Check relay token" :
                              response->overflow ? "Relay response too large" : "Relay unavailable");
            // Close the failed connection so the next poll can reconnect.
            esp_http_client_close(client);
        }
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase()); ESP_ERROR_CHECK(nvs_flash_init());
    } else ESP_ERROR_CHECK(err);
    airkeep_ui_start();
    if (!strlen(CONFIG_AIRKEEP_WIFI_SSID) || !strlen(CONFIG_AIRKEEP_RELAY_TOKEN) ||
        strncmp(CONFIG_AIRKEEP_RELAY_URL, "https://", 8) || strlen(CONFIG_AIRKEEP_RELAY_URL) > 480) {
        airkeep_ui_status("Configure connection in menuconfig");
        return;
    }
    network = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT,ESP_EVENT_ANY_ID,wifi_event,NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT,IP_EVENT_STA_GOT_IP,wifi_event,NULL));
    wifi_config_t config = {0};
    strlcpy((char *)config.sta.ssid,CONFIG_AIRKEEP_WIFI_SSID,sizeof(config.sta.ssid));
    strlcpy((char *)config.sta.password,CONFIG_AIRKEEP_WIFI_PASSWORD,sizeof(config.sta.password));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA,&config));
    ESP_ERROR_CHECK(esp_wifi_start());
    // Scan first to spot missing or mistyped SSIDs in the serial log.
    // Never log passwords.
    airkeep_ui_status("Scanning Wi-Fi");
    if (esp_wifi_scan_start(NULL, true) == ESP_OK) {
        wifi_ap_record_t *found = calloc(16, sizeof(*found));
        if (!found) abort();
        uint16_t count = 16;
        ESP_ERROR_CHECK(esp_wifi_scan_get_ap_records(&count, found));
        bool matched = false;
        for (int i = 0; i < count; i++)
            if (!strcmp((char *)found[i].ssid, CONFIG_AIRKEEP_WIFI_SSID)) matched = true;
        if (!matched) {
            ESP_LOGW(TAG, "Configured SSID not among nearby networks");
            for (int i = 0; i < count; i++)
                ESP_LOGI(TAG, "Nearby SSID: %s (channel %u, signal %d)", found[i].ssid, found[i].primary, found[i].rssi);
        }
        free(found);
    }
    ESP_ERROR_CHECK(esp_wifi_connect());
    xEventGroupWaitBits(network,1,pdFALSE,pdTRUE,portMAX_DELAY);
    // Wait for the correct time before checking the relay certificate.
    esp_sntp_setoperatingmode(SNTP_OPMODE_POLL);
    esp_sntp_setservername(0,"pool.ntp.org");
    esp_sntp_init();
    ESP_LOGI(TAG, "Waiting for NTP clock synchronization");
    airkeep_ui_status("Synchronizing clock");
    while (sntp_get_sync_status() == SNTP_SYNC_STATUS_RESET) vTaskDelay(pdMS_TO_TICKS(500));
    ESP_LOGI(TAG, "Clock synchronized; starting verified HTTPS relay polling");
    if (xTaskCreate(poll_relay, "airkeep_relay", 8192, NULL, 5, NULL) != pdPASS) abort();
}
