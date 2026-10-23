import json
import logging
import ssl
from paho.mqtt import client as mqtt
from config import Settings
from domain import parse_readings
from storage import Store

log = logging.getLogger("airkeep.mqtt")


class Collector:
    def __init__(self, settings: Settings, store: Store):
        self.settings, self.store = settings, store
        self.connected = False
        self.error: str | None = None
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.username_pw_set("bblp", settings.printer_code)
        context = ssl.create_default_context(cafile=str(settings.ca_path))
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False  # Printer cert CN is its serial, not its IP.
        self.client.tls_set_context(context)
        self.client.on_socket_open = self.verify_serial
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message
        self.client.reconnect_delay_set(2, 60)
        self.client.connect_timeout = 8

    def verify_serial(self, client, userdata, sock):
        subject = {k:v for rdn in sock.getpeercert()["subject"] for k,v in rdn}
        if subject.get("commonName") != self.settings.printer_serial:
            raise ssl.CertificateError("Printer certificate serial does not match configured printer")

    def on_connect(self, client, userdata, flags, reason, props):
        self.connected = not reason.is_failure
        self.error = None if self.connected else str(reason)
        if self.connected:
            client.subscribe(f"device/{self.settings.printer_serial}/report")

    def on_disconnect(self, client, userdata, flags, reason, props):
        self.connected = False
        self.error = "Printer disconnected; reconnecting"

    def on_message(self, client, userdata, message):
        try:
            payload = json.loads(message.payload)
            if isinstance(payload, dict):
                for reading in parse_readings(payload):
                    self.store.save_reading(reading)
        except (ValueError, TypeError):
            log.warning("Ignored malformed printer report")

    def start(self):
        self.client.connect_async(self.settings.printer_ip, 8883, 30)
        self.client.loop_start()

    def stop(self):
        self.client.disconnect()
        self.client.loop_stop()
