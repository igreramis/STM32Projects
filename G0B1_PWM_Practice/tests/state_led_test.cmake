# Compile the actual firmware service with a fake GPIO sink on the host.
find_program(HOST_CC NAMES cc gcc REQUIRED NO_CMAKE_FIND_ROOT_PATH)
file(READ "${SOURCE_DIR}/Core/Src/main.c" source)
string(REGEX MATCH "static void State_LED_Service\\(uint32_t now\\)[\r\n]+\\{[^~]*[\r\n]\\}[\r\n]+#endif" service "${source}")
# Restrict extraction to the first following preprocessor boundary.
string(FIND "${service}" "#endif" end)
if(end LESS 0)
    message(FATAL_ERROR "Cannot locate the firmware LED service")
endif()
string(SUBSTRING "${service}" 0 ${end} service)
set(harness [=[
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#define LED_GREEN_GPIO_Port 0
#define LED_GREEN_Pin 5
#define GPIO_PIN_SET 1
#define GPIO_PIN_RESET 0
static uint8_t operating_state;
static int led;
static void HAL_GPIO_WritePin(int port, int pin, int value)
{
  assert(port == 0 && pin == 5);
  led = value;
}
@SERVICE@
static void expect(uint32_t tick, int value)
{
  State_LED_Service(tick);
  assert(led == value);
}
int main(void)
{
  const uint32_t periods[] = {2000, 1000, 500, 0, 100};
  for (uint8_t state = 0; state < 5; ++state) {
    operating_state = state;
    uint32_t start = 10000U * (state + 1U);
    uint32_t period = periods[state];
    expect(start, 1);
    if (!period) {
      expect(start + 1U, 1);
      expect(start + 100000U, 1);
      continue;
    }
    expect(start + period / 2U - 1U, 1);
    expect(start + period / 2U, 0);
    expect(start + period - 1U, 0);
    expect(start + period, 1);
    /* A late service call retains phase rather than drifting. */
    expect(start + period * 7U + period / 2U, 0);
    expect(start + period * 8U, 1);
  }
  /* All blinking states remain correct across uint32_t tick rollover. */
  for (uint8_t state = 0; state < 5; ++state) {
    if (!periods[state]) continue;
    operating_state = 3;
    expect(UINT32_MAX - 40U, 1);
    operating_state = state;
    uint32_t start = UINT32_MAX - 25U;
    expect(start, 1);
    expect(start + periods[state] / 2U, 0);
    expect(start + periods[state], 1);
  }
  operating_state = 3;
  expect(10000, 1);
  operating_state = 4;
  expect(10001, 1);
  expect(10051, 0);
  operating_state = 3;
  expect(10052, 1);
  puts("PASS all five states, boundaries, transitions, late calls and tick rollover");
  return 0;
}
]=])
string(REPLACE "@SERVICE@" "${service}" harness "${harness}")
file(MAKE_DIRECTORY "${BINARY_DIR}/host-tests")
file(WRITE "${BINARY_DIR}/host-tests/state_led.c" "${harness}")
execute_process(COMMAND "${HOST_CC}" -std=c11 -Wall -Wextra -Werror
    "${BINARY_DIR}/host-tests/state_led.c" -o "${BINARY_DIR}/host-tests/state_led"
    RESULT_VARIABLE result)
if(NOT result EQUAL 0)
    message(FATAL_ERROR "Host LED test compilation failed: ${result}")
endif()
execute_process(COMMAND "${BINARY_DIR}/host-tests/state_led" RESULT_VARIABLE result)
if(NOT result EQUAL 0)
    message(FATAL_ERROR "LED timing assertions failed: ${result}")
endif()