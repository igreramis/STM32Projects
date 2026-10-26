/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : Main program body
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2026 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "main.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */

/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */
typedef enum
{
  APP_MODE_STARTUP,
  APP_MODE_NORMAL
} AppMode;

typedef enum
{
  TD1_SEQUENCE_IDLE,
  TD1_SEQUENCE_QUEUE_20_PERCENT,
  TD1_SEQUENCE_QUEUE_30_PERCENT,
  TD1_SEQUENCE_QUEUE_OFF,
  TD1_SEQUENCE_WAIT_OFF,
  TD1_SEQUENCE_GAP
} TD1_SequenceState;

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */
#define PB14_SCOPE_TEST 0 /* 1: GPIO scope test; 0: HRTIM TD1 sequence. */
#define TD1_GAP_PERIODS 1U /* Minimum 1: 100 us between pulse groups. */
#define HRTIM_TICKS_PER_US 340U /* 170 MHz HRTIM clock, MUL2. */
#define NORMAL_PULSE_TICKS 170U /* 500 ns, independent of period. */
#define NORMAL_REQUESTED_DELTA_US 100U
#define NORMAL_PHASE_SLOTS 4U /* Reserve the fourth phase even without TF2. */
#define BUTTON_DEBOUNCE_MS 20U
#define BUTTON_SHORT_PRESS_MAX_MS 1000U

/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/

COM_InitTypeDef BspCOMInit;
HRTIM_HandleTypeDef hhrtim1;

/* USER CODE BEGIN PV */
static volatile AppMode app_mode = APP_MODE_STARTUP;
static volatile uint32_t startup_abort_requested;
static volatile TD1_SequenceState td1_sequence_state = TD1_SEQUENCE_IDLE;
static uint32_t td1_gap_remaining;
/* Ordered outputs: TD1 (PB14), TD2 (PB15), TF1 (PC6). */
static const struct
{
  uint32_t timer_index;
  uint32_t compare_unit;
} sequence_channels[] = {
  {HRTIM_TIMERINDEX_TIMER_D, HRTIM_COMPAREUNIT_1},
  {HRTIM_TIMERINDEX_TIMER_D, HRTIM_COMPAREUNIT_3},
  {HRTIM_TIMERINDEX_TIMER_F, HRTIM_COMPAREUNIT_1}
};
static uint32_t active_channel;
static const uint32_t normal_period_ticks[] = {34000U, 6800U, 3400U};
static volatile uint32_t normal_frequency_index;
/* Half-periods for 1/2/4 Hz LD2 blinking at 10/50/100 kHz PWM. */
static const uint32_t led_half_period_ms[] = {500U, 250U, 125U};
static uint32_t led_active_half_period_ms;
static uint32_t led_last_toggle_at;
static uint32_t button_raw;
static uint32_t button_stable;
static uint32_t button_changed_at;
static uint32_t button_pressed_at;
static uint32_t button_press_armed;

/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
static void MX_GPIO_Init(void);
static void MX_HRTIM1_Init(void);
/* USER CODE BEGIN PFP */
static void TD1_SequenceStart(void);
static void NormalMode_Enter(void);
static void NormalMode_QueueWaveform(uint32_t frequency_index);
static void Button_Poll(void);
static void Button_OnShortPress(void);
static void LED_Poll(void);

/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */

#if PB14_SCOPE_TEST
static void PB14_ScopeTest(void)
{
  GPIO_InitTypeDef gpio = {0};
  __HAL_RCC_GPIOB_CLK_ENABLE();
  HAL_GPIO_WritePin(GPIOB, GPIO_PIN_14, GPIO_PIN_RESET);
  gpio.Pin = GPIO_PIN_14;
  gpio.Mode = GPIO_MODE_OUTPUT_PP;
  gpio.Pull = GPIO_NOPULL;
  gpio.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(GPIOB, &gpio);

  /* Toggle every 5 ms: approximately 100 Hz, 50% duty.
   * Standalone scope test; HRTIM and BSP initialization are bypassed.
   */
  uint32_t last_toggle = HAL_GetTick();
  while (1)
  {
    uint32_t now = HAL_GetTick();
    if ((uint32_t)(now - last_toggle) >= 5U)
    {
      last_toggle = now;
      HAL_GPIO_TogglePin(GPIOB, GPIO_PIN_14);
    }
  }
}
#endif

/* USER CODE END 0 */

/**
  * @brief  The application entry point.
  * @retval int
  */
int main(void)
{

  /* USER CODE BEGIN 1 */

  /* USER CODE END 1 */

  /* MCU Configuration--------------------------------------------------------*/

  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */
  HAL_Init();

  /* USER CODE BEGIN Init */

  /* USER CODE END Init */

  /* Configure the system clock */
  SystemClock_Config();

  /* USER CODE BEGIN SysInit */
#if PB14_SCOPE_TEST
  PB14_ScopeTest(); /* Intentional standalone test; does not return. */
#endif

  /* USER CODE END SysInit */

  /* Initialize all configured peripherals */
  MX_GPIO_Init();
  MX_HRTIM1_Init();
  /* USER CODE BEGIN 2 */

  /* USER CODE END 2 */

  /* Initialize led */
  BSP_LED_Init(LED_GREEN);

  /* Poll PC13 for debounced press/release pairs; no raw-edge actions. */
  BSP_PB_Init(BUTTON_USER, BUTTON_MODE_GPIO);

  /* Initialize COM1 port (115200, 8 bits (7-bit data + 1 stop bit), no parity */
  BspCOMInit.BaudRate   = 115200;
  BspCOMInit.WordLength = COM_WORDLENGTH_8B;
  BspCOMInit.StopBits   = COM_STOPBITS_1;
  BspCOMInit.Parity     = COM_PARITY_NONE;
  BspCOMInit.HwFlowCtl  = COM_HWCONTROL_NONE;
  if (BSP_COM_Init(COM1, &BspCOMInit) != BSP_ERROR_NONE)
  {
    Error_Handler();
  }

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  /* Start after the button is configured. A held-at-reset button must first
   * be released, then pressed, to produce a short-press event.
   */
  button_raw = (BSP_PB_GetState(BUTTON_USER) != 0) ? 1U : 0U;
  button_stable = button_raw;
  button_changed_at = HAL_GetTick();
  BSP_LED_Off(LED_GREEN);
  TD1_SequenceStart();
  while (1)
  {

    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
    Button_Poll();
    LED_Poll();
  }
  /* USER CODE END 3 */
}

/**
  * @brief System Clock Configuration
  * @retval None
  */
void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

  /** Configure the main internal regulator output voltage
  */
  HAL_PWREx_ControlVoltageScaling(PWR_REGULATOR_VOLTAGE_SCALE1_BOOST);

  /** Initializes the RCC Oscillators according to the specified parameters
  * in the RCC_OscInitTypeDef structure.
  */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSI;
  RCC_OscInitStruct.HSIState = RCC_HSI_ON;
  RCC_OscInitStruct.HSICalibrationValue = RCC_HSICALIBRATION_DEFAULT;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSI;
  RCC_OscInitStruct.PLL.PLLM = RCC_PLLM_DIV4;
  RCC_OscInitStruct.PLL.PLLN = 85;
  RCC_OscInitStruct.PLL.PLLP = RCC_PLLP_DIV2;
  RCC_OscInitStruct.PLL.PLLQ = RCC_PLLQ_DIV2;
  RCC_OscInitStruct.PLL.PLLR = RCC_PLLR_DIV2;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    Error_Handler();
  }

  /** Initializes the CPU, AHB and APB buses clocks
  */
  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_SYSCLK
                              |RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV1;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;

  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_4) != HAL_OK)
  {
    Error_Handler();
  }
}

/**
  * @brief HRTIM1 Initialization Function
  * @param None
  * @retval None
  */
static void MX_HRTIM1_Init(void)
{

  /* USER CODE BEGIN HRTIM1_Init 0 */

  /* USER CODE END HRTIM1_Init 0 */

  HRTIM_TimeBaseCfgTypeDef pTimeBaseCfg = {0};
  HRTIM_TimerCtlTypeDef pTimerCtl = {0};
  HRTIM_TimerCfgTypeDef pTimerCfg = {0};
  HRTIM_CompareCfgTypeDef pCompareCfg = {0};
  HRTIM_OutputCfgTypeDef pOutputCfg = {0};

  /* USER CODE BEGIN HRTIM1_Init 1 */

  /* USER CODE END HRTIM1_Init 1 */
  hhrtim1.Instance = HRTIM1;
  hhrtim1.Init.HRTIMInterruptResquests = HRTIM_IT_NONE;
  hhrtim1.Init.SyncOptions = HRTIM_SYNCOPTION_NONE;
  if (HAL_HRTIM_Init(&hhrtim1) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_HRTIM_DLLCalibrationStart(&hhrtim1, HRTIM_CALIBRATIONRATE_3) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_HRTIM_PollForDLLCalibration(&hhrtim1, 10) != HAL_OK)
  {
    Error_Handler();
  }
  pTimeBaseCfg.Period = 34000;
  pTimeBaseCfg.RepetitionCounter = 0x00;
  pTimeBaseCfg.PrescalerRatio = HRTIM_PRESCALERRATIO_MUL2;
  pTimeBaseCfg.Mode = HRTIM_MODE_CONTINUOUS;
  if (HAL_HRTIM_TimeBaseConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D, &pTimeBaseCfg) != HAL_OK)
  {
    Error_Handler();
  }
  pTimerCtl.UpDownMode = HRTIM_TIMERUPDOWNMODE_UP;
  pTimerCtl.GreaterCMP1 = HRTIM_TIMERGTCMP1_EQUAL;
  pTimerCtl.DualChannelDacEnable = HRTIM_TIMER_DCDE_DISABLED;
  if (HAL_HRTIM_WaveformTimerControl(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D, &pTimerCtl) != HAL_OK)
  {
    Error_Handler();
  }
  pTimerCfg.InterruptRequests = HRTIM_TIM_IT_REP;
  pTimerCfg.DMARequests = HRTIM_TIM_DMA_NONE;
  pTimerCfg.DMASrcAddress = 0x0000;
  pTimerCfg.DMADstAddress = 0x0000;
  pTimerCfg.DMASize = 0x1;
  pTimerCfg.HalfModeEnable = HRTIM_HALFMODE_DISABLED;
  pTimerCfg.InterleavedMode = HRTIM_INTERLEAVED_MODE_DISABLED;
  pTimerCfg.StartOnSync = HRTIM_SYNCSTART_DISABLED;
  pTimerCfg.ResetOnSync = HRTIM_SYNCRESET_DISABLED;
  pTimerCfg.DACSynchro = HRTIM_DACSYNC_NONE;
  pTimerCfg.PreloadEnable = HRTIM_PRELOAD_ENABLED;
  pTimerCfg.UpdateGating = HRTIM_UPDATEGATING_INDEPENDENT;
  pTimerCfg.BurstMode = HRTIM_TIMERBURSTMODE_MAINTAINCLOCK;
  pTimerCfg.RepetitionUpdate = HRTIM_UPDATEONREPETITION_ENABLED;
  pTimerCfg.PushPull = HRTIM_TIMPUSHPULLMODE_DISABLED;
  pTimerCfg.FaultEnable = HRTIM_TIMFAULTENABLE_NONE;
  pTimerCfg.FaultLock = HRTIM_TIMFAULTLOCK_READWRITE;
  pTimerCfg.DeadTimeInsertion = HRTIM_TIMDEADTIMEINSERTION_DISABLED;
  pTimerCfg.DelayedProtectionMode = HRTIM_TIMER_D_E_DELAYEDPROTECTION_DISABLED;
  pTimerCfg.UpdateTrigger = HRTIM_TIMUPDATETRIGGER_NONE;
  pTimerCfg.ResetTrigger = HRTIM_TIMRESETTRIGGER_NONE;
  pTimerCfg.ResetUpdate = HRTIM_TIMUPDATEONRESET_DISABLED;
  pTimerCfg.ReSyncUpdate = HRTIM_TIMERESYNC_UPDATE_UNCONDITIONAL;
  if (HAL_HRTIM_WaveformTimerConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D, &pTimerCfg) != HAL_OK)
  {
    Error_Handler();
  }
  pCompareCfg.CompareValue = 3400;
  if (HAL_HRTIM_WaveformCompareConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D, HRTIM_COMPAREUNIT_1, &pCompareCfg) != HAL_OK)
  {
    Error_Handler();
  }
  pOutputCfg.Polarity = HRTIM_OUTPUTPOLARITY_HIGH;
  pOutputCfg.SetSource = HRTIM_OUTPUTSET_TIMPER;
  pOutputCfg.ResetSource = HRTIM_OUTPUTRESET_TIMCMP1;
  pOutputCfg.IdleMode = HRTIM_OUTPUTIDLEMODE_NONE;
  pOutputCfg.IdleLevel = HRTIM_OUTPUTIDLELEVEL_INACTIVE;
  pOutputCfg.FaultLevel = HRTIM_OUTPUTFAULTLEVEL_NONE;
  pOutputCfg.ChopperModeEnable = HRTIM_OUTPUTCHOPPERMODE_DISABLED;
  pOutputCfg.BurstModeEntryDelayed = HRTIM_OUTPUTBURSTMODEENTRY_REGULAR;
  if (HAL_HRTIM_WaveformOutputConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D, HRTIM_OUTPUT_TD1, &pOutputCfg) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN HRTIM1_Init 2 */
  /* TD2 uses CMP3 so zero duty can independently suppress its pulses. */
  pCompareCfg.CompareValue = 0U;
  if (HAL_HRTIM_WaveformCompareConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D,
                                     HRTIM_COMPAREUNIT_3, &pCompareCfg) != HAL_OK)
  {
    Error_Handler();
  }
  pOutputCfg.ResetSource = HRTIM_OUTPUTRESET_TIMCMP3;
  if (HAL_HRTIM_WaveformOutputConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D,
                                    HRTIM_OUTPUT_TD2, &pOutputCfg) != HAL_OK)
  {
    Error_Handler();
  }

  /* TF1 has its own timer, with the same period, prescaler and preload.
   * Only Timer D interrupts; its callback queues all three outputs.
   */
  if (HAL_HRTIM_TimeBaseConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_F,
                              &pTimeBaseCfg) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_HRTIM_WaveformTimerControl(&hhrtim1, HRTIM_TIMERINDEX_TIMER_F,
                                    &pTimerCtl) != HAL_OK)
  {
    Error_Handler();
  }
  pTimerCfg.InterruptRequests = HRTIM_TIM_IT_NONE;
  pTimerCfg.DelayedProtectionMode = HRTIM_TIMER_F_DELAYEDPROTECTION_DISABLED;
  if (HAL_HRTIM_WaveformTimerConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_F,
                                   &pTimerCfg) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_HRTIM_WaveformCompareConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_F,
                                     HRTIM_COMPAREUNIT_1, &pCompareCfg) != HAL_OK)
  {
    Error_Handler();
  }
  pOutputCfg.ResetSource = HRTIM_OUTPUTRESET_TIMCMP1;
  if (HAL_HRTIM_WaveformOutputConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_F,
                                    HRTIM_OUTPUT_TF1, &pOutputCfg) != HAL_OK)
  {
    Error_Handler();
  }

  /* USER CODE END HRTIM1_Init 2 */
  HAL_HRTIM_MspPostInit(&hhrtim1);

}

/**
  * @brief GPIO Initialization Function
  * @param None
  * @retval None
  */
static void MX_GPIO_Init(void)
{
  /* USER CODE BEGIN MX_GPIO_Init_1 */

  /* USER CODE END MX_GPIO_Init_1 */

  /* GPIO Ports Clock Enable */
  __HAL_RCC_GPIOC_CLK_ENABLE();
  __HAL_RCC_GPIOF_CLK_ENABLE();
  __HAL_RCC_GPIOB_CLK_ENABLE();
  __HAL_RCC_GPIOA_CLK_ENABLE();

  /* USER CODE BEGIN MX_GPIO_Init_2 */

  /* USER CODE END MX_GPIO_Init_2 */
}

/* USER CODE BEGIN 4 */

/* LD2 (PA5) stays off during startup. Normal mode uses 50% duty blinking
 * at 1/2/4 Hz. Restart with the LED on whenever the PWM selection changes.
 * Only the main loop drives the LED; HRTIM timing and interrupts are untouched.
 */
static void LED_Poll(void)
{
  if (app_mode != APP_MODE_NORMAL)
  {
    return;
  }

  uint32_t half_period = led_half_period_ms[normal_frequency_index];
  uint32_t now = HAL_GetTick();
  if (half_period != led_active_half_period_ms)
  {
    led_active_half_period_ms = half_period;
    led_last_toggle_at = now;
    BSP_LED_On(LED_GREEN);
    return;
  }

  /* Unsigned elapsed time remains valid across HAL tick wraparound. */
  if ((uint32_t)(now - led_last_toggle_at) >= half_period)
  {
    led_last_toggle_at = now;
    BSP_LED_Toggle(LED_GREEN);
  }
}

/* Cyclic startup sequence, stopped only by the PC13 user button.
 * Timers D/F: 340 MHz equivalent clock, PER = 34000 (100 us).
 * TD1 uses D/CMP1, TD2 uses D/CMP3, TF1 uses F/CMP1; all set on period.
 * TD1 -> blank period -> TD2 -> blank period -> TF1 -> blank -> TD1.
 * Each group is three full periods at 10%, 20%, 30% (300 us).
 * The 100 us gap starts after the third full period, not its falling edge.
 * Zero compare suppresses inactive channels; the cycle takes 1.2 ms.
 */
static void TD1_SequenceStart(void)
{
  /* Load an inactive waveform while the counter/output are still stopped. */
  for (uint32_t channel = 0U;
       channel < sizeof(sequence_channels) / sizeof(sequence_channels[0]);
       ++channel)
  {
    __HAL_HRTIM_SETCOMPARE(&hhrtim1, sequence_channels[channel].timer_index,
                          sequence_channels[channel].compare_unit, 0U);
  }
  active_channel = 0U;
  startup_abort_requested = 0U;
  app_mode = APP_MODE_STARTUP;
  if (HAL_HRTIM_SoftwareUpdate(&hhrtim1,
                               HRTIM_TIMERUPDATE_D | HRTIM_TIMERUPDATE_F) != HAL_OK)
  {
    Error_Handler();
  }
  __DSB();
  __HAL_HRTIM_SETCOUNTER(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D, 0U);
  __HAL_HRTIM_SETCOUNTER(&hhrtim1, HRTIM_TIMERINDEX_TIMER_F, 0U);

  /* First rollover loads 10% and starts pulse 1 after a blank 100 us period. */
  __HAL_HRTIM_SETCOMPARE(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D,
                        HRTIM_COMPAREUNIT_1, 3400U);
  td1_sequence_state = TD1_SEQUENCE_QUEUE_20_PERCENT;
  __HAL_HRTIM_TIMER_CLEAR_IT(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D,
                            HRTIM_TIM_IT_REP);
  HAL_NVIC_ClearPendingIRQ(HRTIM1_TIMD_IRQn);

  if (HAL_HRTIM_WaveformOutputStart(&hhrtim1,
                                   HRTIM_OUTPUT_TD1 | HRTIM_OUTPUT_TD2 |
                                   HRTIM_OUTPUT_TF1) != HAL_OK)
  {
    Error_Handler();
  }
  /* One MCR write starts both counters on the same clock edge. */
  if (HAL_HRTIM_WaveformCountStart_IT(&hhrtim1,
                                     HRTIM_TIMERID_TIMER_D |
                                     HRTIM_TIMERID_TIMER_F) != HAL_OK)
  {
    Error_Handler();
  }
}

/* PC13 is active high. Both transitions must be stable for 20 ms.
 * Emit once on release of a short press (at most 1 second); long holds do
 * nothing. Release-to-rearm prevents the startup press also cycling Normal.
 * No delays and no timing-critical work is done while the button is held.
 */
static void Button_Poll(void)
{
  uint32_t now = HAL_GetTick();
  uint32_t raw = (BSP_PB_GetState(BUTTON_USER) != 0) ? 1U : 0U;

  if (raw != button_raw)
  {
    button_raw = raw;
    button_changed_at = now;
  }
  if ((raw == button_stable) ||
      ((uint32_t)(now - button_changed_at) < BUTTON_DEBOUNCE_MS))
  {
    return;
  }
  button_stable = raw;
  if (raw != 0U)
  {
    button_pressed_at = button_changed_at;
    button_press_armed = 1U;
  }
  else
  {
    if ((button_press_armed != 0U) &&
        ((uint32_t)(button_changed_at - button_pressed_at) <=
         BUTTON_SHORT_PRESS_MAX_MS))
    {
      Button_OnShortPress();
    }
    button_press_armed = 0U;
  }
}

/* Called only for a debounced short press.
 * Only active_channel can have a nonzero compare preload, including when
 * the next channel is queued during a gap. Clear that preload, not the
 * active register, so any pulse in progress finishes normally.
 */
static void Button_OnShortPress(void)
{
  if (app_mode == APP_MODE_NORMAL)
  {
    normal_frequency_index = (normal_frequency_index + 1U) %
        (sizeof(normal_period_ticks) / sizeof(normal_period_ticks[0]));
    NormalMode_QueueWaveform(normal_frequency_index);
    return;
  }

  uint32_t primask = __get_PRIMASK();
  __disable_irq();
  if ((app_mode == APP_MODE_STARTUP) &&
      (td1_sequence_state != TD1_SEQUENCE_IDLE) &&
      (startup_abort_requested == 0U))
  {
    __HAL_HRTIM_SETCOMPARE(&hhrtim1,
                          sequence_channels[active_channel].timer_index,
                          sequence_channels[active_channel].compare_unit, 0U);
    __DSB();
    startup_abort_requested = 1U;
    /* Discard any repetition interrupt predating the preload write.
     * The next fresh boundary will have applied zero before entering Normal.
     * IRQ masking prevents the sequence ISR from queuing another pulse.
     */
    __HAL_HRTIM_TIMER_CLEAR_IT(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D,
                              HRTIM_TIM_IT_REP);
    HAL_NVIC_ClearPendingIRQ(HRTIM1_TIMD_IRQn);
  }
  __set_PRIMASK(primask);
}

/* Normal mode uses a common period with 170-tick (500 ns) pulses:
 * TD1: period -> D/CMP1; TD2: D/CMP2 -> D/CMP3;
 * TF1: F/CMP2 -> F/CMP1. TF2 remains unconfigured/disabled.
 * To add TF2 later, use F/CMP4 at 3*delta and F/CMP3 at 3*delta+width
 * (use the period reset event if its falling edge equals the period).
 * The requested delta is never overwritten by the clamped value.
 * Integer division floors the maximum delta so all FOUR slots fit.
 */
static void NormalMode_QueueWaveform(uint32_t frequency_index)
{
  uint32_t period = normal_period_ticks[frequency_index];
  uint32_t delta = NORMAL_REQUESTED_DELTA_US * HRTIM_TICKS_PER_US;
  uint32_t max_delta = (period - NORMAL_PULSE_TICKS) /
             (NORMAL_PHASE_SLOTS - 1U);
  if (delta > max_delta)
  {
  delta = max_delta;
  }

  /* RM0440 table 244: period, compare AND SET/RST registers are preloaded.
   * D/F have aligned counters and identical periods. Inhibit both updates
   * in one common-register write so even a rollover during these writes
   * keeps the entire old waveform. Re-enable together; the NEXT common
   * rollover applies the complete new waveform. Never force a live update.
   */
  SET_BIT(hhrtim1.Instance->sCommonRegs.CR1,
      HRTIM_CR1_TDUDIS | HRTIM_CR1_TFUDIS);
  __DSB();
  __HAL_HRTIM_SETPERIOD(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D, period);
  __HAL_HRTIM_SETPERIOD(&hhrtim1, HRTIM_TIMERINDEX_TIMER_F, period);
  __HAL_HRTIM_SETCOMPARE(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D,
            HRTIM_COMPAREUNIT_1, NORMAL_PULSE_TICKS);
  __HAL_HRTIM_SETCOMPARE(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D,
            HRTIM_COMPAREUNIT_2, delta);
  __HAL_HRTIM_SETCOMPARE(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D,
            HRTIM_COMPAREUNIT_3, delta + NORMAL_PULSE_TICKS);
  __HAL_HRTIM_SETCOMPARE(&hhrtim1, HRTIM_TIMERINDEX_TIMER_F,
            HRTIM_COMPAREUNIT_2, 2U * delta);
  __HAL_HRTIM_SETCOMPARE(&hhrtim1, HRTIM_TIMERINDEX_TIMER_F,
            HRTIM_COMPAREUNIT_1, 2U * delta + NORMAL_PULSE_TICKS);

  hhrtim1.Instance->sTimerxRegs[HRTIM_TIMERINDEX_TIMER_D].SETx1R =
    HRTIM_OUTPUTSET_TIMPER;
  hhrtim1.Instance->sTimerxRegs[HRTIM_TIMERINDEX_TIMER_D].RSTx1R =
    HRTIM_OUTPUTRESET_TIMCMP1;
  hhrtim1.Instance->sTimerxRegs[HRTIM_TIMERINDEX_TIMER_D].SETx2R =
    HRTIM_OUTPUTSET_TIMCMP2;
  hhrtim1.Instance->sTimerxRegs[HRTIM_TIMERINDEX_TIMER_D].RSTx2R =
    HRTIM_OUTPUTRESET_TIMCMP3;
  hhrtim1.Instance->sTimerxRegs[HRTIM_TIMERINDEX_TIMER_F].SETx1R =
    HRTIM_OUTPUTSET_TIMCMP2;
  hhrtim1.Instance->sTimerxRegs[HRTIM_TIMERINDEX_TIMER_F].RSTx1R =
    HRTIM_OUTPUTRESET_TIMCMP1;
  __DSB();
  CLEAR_BIT(hhrtim1.Instance->sCommonRegs.CR1,
      HRTIM_CR1_TDUDIS | HRTIM_CR1_TFUDIS);
}

/* Enter after startup has reached zero at a period boundary. Queue Normal
 * at 10 kHz for the next boundary, leaving one blank handover period.
 * Normal mode is entirely hardware-driven; no 100 kHz PWM ISR is needed.
 */
static void NormalMode_Enter(void)
{
  __HAL_HRTIM_TIMER_DISABLE_IT(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D,
                              HRTIM_TIM_IT_REP);
  td1_sequence_state = TD1_SEQUENCE_IDLE;
  normal_frequency_index = 0U;
  NormalMode_QueueWaveform(normal_frequency_index);
  app_mode = APP_MODE_NORMAL;
}

void HAL_HRTIM_RepetitionEventCallback(HRTIM_HandleTypeDef *hhrtim,
                                      uint32_t TimerIdx)
{
  if ((hhrtim != &hhrtim1) || (TimerIdx != HRTIM_TIMERINDEX_TIMER_D) ||
      (app_mode != APP_MODE_STARTUP))
  {
    return;
  }

  if (startup_abort_requested != 0U)
  {
    NormalMode_Enter();
    return;
  }

  /* Hardware has already applied the preload at this boundary.
   * Queue the NEXT period; do not force an update or delay in this callback.
   * Each write must complete before the next boundary (100 us later).
   */
  switch (td1_sequence_state)
  {
    case TD1_SEQUENCE_QUEUE_20_PERCENT:
      __HAL_HRTIM_SETCOMPARE(hhrtim, sequence_channels[active_channel].timer_index,
                            sequence_channels[active_channel].compare_unit, 6800U);
      td1_sequence_state = TD1_SEQUENCE_QUEUE_30_PERCENT;
      break;

    case TD1_SEQUENCE_QUEUE_30_PERCENT:
      __HAL_HRTIM_SETCOMPARE(hhrtim, sequence_channels[active_channel].timer_index,
                            sequence_channels[active_channel].compare_unit, 10200U);
      td1_sequence_state = TD1_SEQUENCE_QUEUE_OFF;
      break;

    case TD1_SEQUENCE_QUEUE_OFF:
      __HAL_HRTIM_SETCOMPARE(hhrtim, sequence_channels[active_channel].timer_index,
                            sequence_channels[active_channel].compare_unit, 0U);
      td1_sequence_state = TD1_SEQUENCE_WAIT_OFF;
      break;

    case TD1_SEQUENCE_WAIT_OFF:
      /* The third full period has ended; hardware suppressed pulse 4.
       * Leave the output enabled at zero duty to keep actively driving low.
       * Queue the next group one boundary early to account for preload.
       */
      td1_gap_remaining = TD1_GAP_PERIODS;
      td1_sequence_state = TD1_SEQUENCE_GAP;
      /* Fall through: count the first blank period now. With a one-period
       * gap, preload the next channel immediately for the next boundary.
       */

    case TD1_SEQUENCE_GAP:
      if (--td1_gap_remaining == 0U)
      {
        /* Wrap through the configured outputs until the user presses PC13. */
        active_channel = (active_channel + 1U) %
             (sizeof(sequence_channels) / sizeof(sequence_channels[0]));
        __HAL_HRTIM_SETCOMPARE(hhrtim, sequence_channels[active_channel].timer_index,
                              sequence_channels[active_channel].compare_unit, 3400U);
        td1_sequence_state = TD1_SEQUENCE_QUEUE_20_PERCENT;
      }
      break;

    default:
      break;
  }
}

/* USER CODE END 4 */

/**
  * @brief  This function is executed in case of error occurrence.
  * @retval None
  */
void Error_Handler(void)
{
  /* USER CODE BEGIN Error_Handler_Debug */
  /* User can add his own implementation to report the HAL error return state */
  __disable_irq();
  while (1)
  {
  }
  /* USER CODE END Error_Handler_Debug */
}
#ifdef USE_FULL_ASSERT
/**
  * @brief  Reports the name of the source file and the source line number
  *         where the assert_param error has occurred.
  * @param  file: pointer to the source file name
  * @param  line: assert_param error line source number
  * @retval None
  */
void assert_failed(uint8_t *file, uint32_t line)
{
  /* USER CODE BEGIN 6 */
  /* User can add his own implementation to report the file name and line number,
     ex: printf("Wrong parameters value: file %s on line %d\r\n", file, line) */
  /* USER CODE END 6 */
}
#endif /* USE_FULL_ASSERT */
