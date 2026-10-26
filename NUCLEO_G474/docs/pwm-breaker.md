# Section 2.6: PWM breaker

## Wiring and threshold

- Signal generator output: **PA1 / COMP1 INP**. Connect generator ground to board GND.
- Keep the actual pin voltage between GND and VDDA. Do not apply a negative
  waveform or a 5 V signal. Set the generator load indication to high impedance
  and confirm the actual amplitude/offset with a scope before connecting it.
- COMP1 INM uses **DAC3 channel 1 internally**; no external DAC wire is needed.
- `PWM_BREAKER_THRESHOLD_MV` is 1700. `PWM_BREAKER_VREF_MV` defaults to 3300;
  set it to the measured VREF+ voltage for a more accurate threshold. Nominal DAC
  code is 2110. DAC error and comparator offset still affect the measured trip point.
- No comparator hysteresis is added. Noise near the threshold can cause repeated
  trips/recovery; use a clean test signal before considering hysteresis/filtering.

## Implementation

- `Core/Src/pwm_breaker.c` configures DAC3/COMP1 through CMSIS registers; these
  peripherals' HAL drivers are not present in this CubeMX export.
- RM0440 tables 199/200 select PA1 and DAC3_CH1 for COMP1. Table 84 routes COMP1
  directly to **HRTIM Fault 4**, not Fault 1. Table 101 assigns COMP1 to EXTI21.
- Both Timer D and Timer F accept active-high FLT4. TD1/TD2/TF1 are forced low
  asynchronously by hardware. Fault filtering, blanking and counting are disabled.
- Protection is configured before output enabling. A high input at power-up
  must hold all three outputs low, including during Startup Mode.
- HRTIM faults latch outputs off (RM0440 section 28.3.17). A COMP1 falling-edge
  interrupt re-enables them after checking the comparator level. Main-loop polling
  of the latched output-disabled status also catches short faults/missed edges.
- The fault remains enabled throughout recovery; a new high input overrides a
  concurrent output-enable write. No interrupt or polling is needed to shut down.
- Counters, startup sequencing, button handling and LED indication keep running.
  Recovery continues the current waveform; it does not restart Startup Mode.
- Recovery is immediate software re-enabling, not period-aligned. It has interrupt
  latency and can expose the remainder of a pulse already in progress. Later pulses
  retain the programmed width and phase. There is no hard real-time recovery bound
  verified yet.
- Two 1 ms settling delays run only at initialization, before PWM starts. There
  are no blocking delays in the breaker interrupt or main-loop service.
- The breaker owns output enabling until reset. Any future explicit PWM stop must
  disarm automatic recovery first. TF2 remains disabled; adding it requires updating
  the output mask and its status-mask assertion.

The CubeMX `.ioc` does not describe this user-code module. Its initialization runs
after generated HRTIM setup and is kept in a USER CODE section. Preserve the root
CMake source entry and the interrupt USER CODE additions when regenerating.

## Hardware acceptance checklist (not yet executed)

1. With input low, confirm the existing startup pattern, then enter Normal Mode.
2. At each of 10/50/100 kHz, raise the input above 1.7 V. Scope PA1 and each PWM
   output: all three must remain low while input is high, with no periodic leaks.
3. Lower the input below 1.7 V. Confirm automatic recovery without a button press;
   measure recovery latency and inspect the first pulse as well as later pulses.
4. Repeat with short over-threshold pulses and check that outputs do not remain
   latched off after the input returns low.
5. Reset while the input is high. Confirm no startup pulses escape; lowering the
   input must allow the current startup sequence to appear.
6. Trip during Startup Mode, during a frequency change and while pressing the
   button. Verify protection remains active and normal controls still work.
7. Slowly sweep around 1.7 V and record actual rising/falling thresholds.

Useful debugger checks after initialization: DAC3 DOR1 = 2110 (default reference),
DAC3 MCR MODE1 = 3 and HFSEL = 2, COMP1 CSR INMSEL = 4 and INPSEL = 0,
HRTIM FLTINR1 FLT4SRC low bit = 1, FLT4P/FLT4E = 1,
and Timer D/F FLTxR FLT4EN = 1. Do not use breakpoints for latency measurements.