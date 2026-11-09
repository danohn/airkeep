#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_sntp.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "ui.h"
#include "display_relay.h"

static EventGroupHandle_t network;
static const char *TAG = "airkeep";
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

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase()); ESP_ERROR_CHECK(nvs_flash_init());
    } else ESP_ERROR_CHECK(err);
    airkeep_ui_start();
    if (!strlen(CONFIG_AIRKEEP_WIFI_SSID) ||
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
    // Leave room for the TLS handshake while processing pairing or renewal.
    if (xTaskCreate(airkeep_relay_task, "airkeep_relay", 14336, network, 5, NULL) != pdPASS) abort();
}
