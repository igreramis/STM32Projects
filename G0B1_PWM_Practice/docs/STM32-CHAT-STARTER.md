# Starter prompt for a separate STM32/Pi chat

Copy the text below into a new chat in this same workspace and attach
`docs/OTA-HANDOVER.md`. Do not attach scratch notes or presigned URLs.

---

I want to PLAN connecting my Raspberry Pi 3 Model B to my NUCLEO-G0B1RE so the
Pi can program the STM32G0B1 using its built-in ROM UART bootloader, controlling
BOOT0/NRST as appropriate. Plan first; do not flash or change boot settings yet.

First read `/home/wh/STM32Projects/G0B1_PWM_Practice/docs/OTA-HANDOVER.md`, then
inspect the relevant current code. The existing AWS gateway source is OUTSIDE
the STM32 workspace at `/home/wh/Downloads/resumes/plasmaleap/aws/pi-client`.
Its deployment is `/home/pi/aws-iot`, accessed through SSH alias `pi`.

Cloud -> Pi firmware download is already verified. Preserve it. Reuse the staged
binary and approved metadata; do not create another MQTT client or gateway.
Do not assume the application USART2 pins are ROM bootloader pins. Research the
specific ST device boot table, boot option bytes and board schematic first.

For this first stage:
1. Summarize verified starting facts and unresolved hardware questions.
2. Produce a documentation-backed proposed wiring table with Pi physical/BCM
   pins and STM32 connector/MCU pins, power/reset safety and VCP conflicts.
3. Identify read-only checks for the board/option bytes and Pi UART mapping.
4. Propose a ROM handshake/read-ID test before any erase/write operation.
5. Plan a standalone flashing module that can later integrate with the existing
   staging flow, including exclusive access, byte/target revalidation, timeouts,
   readback verification, application boot checks and recovery behavior.

Ask for physical-board details/photos when needed. No option-byte writes,
erase/program operations, GPIO boot/reset changes, Pi serial configuration
changes, network disruptions or automatic cloud flashing without explicit
agreement. This is planning, not permission to perform those actions.

Keep explanations short. After each milestone update the shared handover with
verified results, files changed/deployed, wiring/configuration decisions, active
processes and remaining work. The cloud chat will read it when I return. Only
one chat should edit/deploy the gateway files at a time. Keep secrets out of all
handover records.

---

## Prompt to use when returning to the cloud chat

Read `docs/OTA-HANDOVER.md`, especially the hardware milestone entries, and
inspect the software changes listed there. Summarize what was actually tested,
what remains unverified and how to connect the flasher to the existing cloud
staging workflow. Plan the integration before enabling cloud-triggered writes.