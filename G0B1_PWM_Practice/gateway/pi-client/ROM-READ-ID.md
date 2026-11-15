# Supervised ROM identification (not firmware installation)

`rom_read_id.py` is independent of `client.py` and `firmware.py`. It does not
read cloud configuration, open MQTT connections, access staged firmware, control
GPIO or change STM32 options. `stage_firmware` remains staging-only.

## Allowed wire transactions

Only these bytes can be transmitted by the normal probe flow:

| Request | Bytes sent | Expected reply |
| --- | --- | --- |
| Synchronize | `7F` | `79` ACK |
| Get | `00 FF` | ACK, N, N+1 bytes (version then commands), ACK |
| Get Version | `01 FE` | ACK, version plus two status bytes, ACK |
| Get ID | `02 FD` | ACK, N=1, two big-endian PID bytes, ACK |

No command is selected dynamically from the advertised list. Only 00/01/02
are allowlisted; no read-memory, GO, erase, write or protection command exists
in the helper. No automatic retries. A timeout/NACK/bad ACK/malformed response
aborts; manually reset before starting another attempt. The expected PID is
0x0467 (STM32G0B/G0C family); this does not uniquely identify a specific board.
Version is reported from the reply, not assumed to be 9.2. The two status bytes
are recorded raw, not interpreted as a verified FLASH_OPTR/RDP value.

## Dependencies and future supervised execution

Separate optional dependency: `requirements-hardware.txt` pins pyserial 3.5.
It does not alter the cloud requirements. Install this into the Pi's existing
`/home/pi/aws-iot/.venv` at deployment time, not into system Python. Deploy only
the helper, this guide, tests and hardware requirements from the existing laptop
source directory. Nothing has been deployed or run on hardware during preparation.

Future command from `/home/pi/aws-iot` in an interactive SSH terminal:

```sh
.venv/bin/python rom_read_id.py
```

Defaults: `/dev/serial0`, 115200 baud, 8 data bits, EVEN parity, 1 stop bit;
no software/hardware flow control. Port must resolve to `/dev/ttyAMA0` on this
Pi3 setup. `--baud` permits 9600/19200/38400/57600/115200;
`--timeout` permits 0.1–10 seconds, default 2. Every response has one shared
deadline, so partial bytes cannot indefinitely extend a command. Writes have
their own timeout; there are four transactions, no retries. Human prompts are
intentionally unbounded and can be cancelled with Ctrl+C. No blocking drain call.

Type READY after checking physical prerequisites; this opens/configures the
port but sends no protocol bytes. The helper clears stale input, then prompts
you to press/release B2 and type RESET. Only then is 7F transmitted. Any other
answer aborts. Exceptions/cancellation close the port. DTR/RTS are set false
before open; neither is wired to reset or BOOT0. pySerial exclusive=True uses
an advisory Linux lock: it blocks cooperating openers, NOT every program.
Close serial terminals and inspect ownership before running, including root users.

## Physical prerequisites — separate supervised step

- NUCLEO-G0B1RE: nBOOT_SEL=0 already verified; do not program it again.
- Both CN4 caps removed. No SWD reconnect after a full target power cycle.
- Manual BOOT0 on PA14/CN7 pin7 held at target 3.3 V, not 5 V. No Pi GPIO17.
- No external load on PB14; ROM repurposes it. ST-LINK VCP must not transmit.
- Common ground; TX->PA10 and RX<-PA9. Confirm exact wiring before use.
- Nucleo powered by laptop USB, Pi by separate adapter; do not join power rails.
  UART TX/RX stay disconnected until both boards are powered. Disconnect TX/RX
  before either board is powered off. Change CN4/BOOT0 jumper only with target
  unpowered and UART disconnected. Never connect a rail jumper against SWCLK.

After the probe the STM32 stays in ROM; the helper does not switch it back.
For application boot, disconnect UART, power target off, move its BOOT0 jumper
from 3.3 V to target GND (never both), then power it back on with CN4 still off.
Check application behavior separately. Reconnect UART only with both boards
powered, if needed. Do not claim update success from ROM identification alone.

## References and verification limits

- AN2606 Rev61, Table95 p220 and Fig55 p223: G0B1 USART pins, 8E1, 7F selection
  (previously verified local PDF in the STM32 reference folder).
- Intended protocol reference is ST AN3155, USART protocol used in the STM32
  bootloader. ST's current and legacy PDF download attempts failed during this
  work. No downloaded AN3155 or page citations are claimed.
  https://www.st.com/resource/en/application_note/cd00264342-usart-protocol-used-in-the-stm32-bootloader-stmicroelectronics.pdf
- Framing was cross-checked against the public stm32flash implementation's
  command constants and stm32_init/get-ID parsing; no source copied:
  https://github.com/stm32duino/stm32flash/blob/main/stm32.c
- Synthetic V9.2 test responses are fixtures, NOT observations of this board.
- Linux pseudo-terminal tests exercise real pySerial reads/writes, timeouts and
  advisory exclusive locking. PTYs lack real parity and use 8N1 in those tests
  only; a separate configuration test checks the helper requests 8E1. No real
  electrical parity, ROM response, flash operation or application health is
  verified until supervised hardware testing.

Tests: `python -m unittest -v test_rom_read_id test_client test_firmware`.
Use a Python environment containing both dependency files' pinned packages.