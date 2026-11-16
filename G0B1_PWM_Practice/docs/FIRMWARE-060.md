# Firmware 0.6.0 — state LED

The local release folders and cloud release records were checked on 2026-11-16:
0.5.0 was the latest release. The 0.6.0 entry in the isolated web demo is fixture
data, not a published release.

The PA5 green user LED now follows the existing B1-driven operating state:

| State | Full blink cycle | On / off |
| --- | --- | --- |
| idle | 2 seconds | 1000 / 1000 ms |
| ready | 1 second | 500 / 500 ms |
| running | 0.5 seconds | 250 / 250 ms |
| paused | Solid green | Always on |
| fault | 0.1 seconds | 50 / 50 ms |

Each state starts with the LED on. The tick-based service is non-blocking and
handles tick rollover. Normal loop/UART scheduling can introduce small jitter;
these are application indicators, not precision timer outputs or hardware fault
detection. Telemetry, state order, button debounce, and PWM behavior are unchanged.

CMake enables `APP_STATE_LED` for **every release target >= 0.6.0**. Add future
versions to the existing version list; the LED behavior is inherited automatically.
Older release targets retain their previous behavior. The unversioned practice
target remains the legacy application; use `firmware_0_6_0` for this release.

Build output: `build/Debug/versions/0.6.0/firmware.bin`.
Delivery location: `~/Downloads/resumes/plasmaleap/aws/releases/stm32g0b1/0.6.0/firmware.bin`.
Building/copying does not upload, approve, or install firmware.

## Build validation

- Image: 20,324 bytes; 156 bytes remain in the current 20 KiB OTA slot.
- SHA-256: `b962e8bf7b3ed139919bc06fb67fcd34f23258d47c06c1d8e8f9b118e9ce5a29`.
- Gateway image/vector/version validation passed.
- Host CTest passed against the actual LED service: all five states, half-cycle
	boundaries, state transitions, delayed service calls, and tick rollover.
- Rebuilt 0.4.0 and 0.5.0 binaries match the saved release binaries exactly.
- No physical LED timing test or firmware installation was performed.