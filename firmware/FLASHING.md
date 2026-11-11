# Airkeep factory firmware

Use this image only on the **Waveshare ESP32-S3-Touch-AMOLED-1.8 V2** with 16 MB flash and 8 MB octal PSRAM. The factory install replaces the current firmware and clears saved Wi-Fi settings and pairing.

Download `airkeep-factory.bin` and `SHA256SUMS.txt` from the same release. Verify the checksum with `sha256sum -c SHA256SUMS.txt` on Linux or `shasum -a 256 -c SHA256SUMS.txt` on macOS.

Connect the board over USB and close any serial monitors. Replace `YOUR_SERIAL_PORT` with the board's serial port.

```sh
uvx --from esptool==5.6.0 esptool --chip esp32s3 --port YOUR_SERIAL_PORT write-flash --erase-all 0x0 airkeep-factory.bin
```

If the board does not enter download mode, hold BOOT while resetting it, then release BOOT. Reset it normally after flashing. Open the [Airkeep installer](https://airkeep.io/install/) in desktop Chrome or Edge and continue at **Configure over USB**. When the display connects, tap **Pair display** and approve it on your relay.

For a setup using only the terminal, [build and configure the firmware with ESP-IDF](https://github.com/danohn/airkeep/blob/main/firmware/README.md#build-and-configure).
