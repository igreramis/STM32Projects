#include "pwm_breaker.h"
#include "main.h"

/* RM0440 tables 199/200/84:
 * PA1 -> COMP1 INP; DAC3_CH1 -> COMP1 INM; COMP1 -> HRTIM internal FLT4.
 * DAC/COMP use CMSIS registers because their HAL drivers are not in this project.
 */
#define BREAKER_DAC_CODE ((PWM_BREAKER_THRESHOLD_MV * 4095U + \
                           PWM_BREAKER_VREF_MV / 2U) / PWM_BREAKER_VREF_MV)

_Static_assert(PWM_BREAKER_VREF_MV > PWM_BREAKER_THRESHOLD_MV,
               "Breaker threshold must be below VREF+");
_Static_assert(BREAKER_DAC_CODE <= 4095U, "Invalid 12-bit DAC code");
_Static_assert(PWM_BREAKER_OUTPUTS ==
               (HRTIM_ODSR_TD1ODS | HRTIM_ODSR_TD2ODS | HRTIM_ODSR_TF1ODS),
               "Output-enable and disabled-status masks must match");

static HRTIM_HandleTypeDef *breaker_hrtim;
static volatile uint32_t outputs_requested;

void PWM_Breaker_Init(HRTIM_HandleTypeDef *hrtim)
{
  GPIO_InitTypeDef gpio = {0};
  HRTIM_FaultCfgTypeDef fault = {0};

  breaker_hrtim = hrtim;
  outputs_requested = 0U;
  __HAL_RCC_GPIOA_CLK_ENABLE();
  __HAL_RCC_SYSCFG_CLK_ENABLE();
  __HAL_RCC_DAC3_CLK_ENABLE();

  gpio.Pin = GPIO_PIN_1;
  gpio.Mode = GPIO_MODE_ANALOG;
  gpio.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(GPIOA, &gpio);

  /* Configure safe levels BEFORE enabling fault acceptance on either timer.
   * FAULT=10 means inactive (low for our active-high outputs).
   * Prepare TF2's safe level as well, but do not enable that output.
   */
  MODIFY_REG(hrtim->Instance->sTimerxRegs[HRTIM_TIMERINDEX_TIMER_D].OUTxR,
             HRTIM_OUTR_FAULT1 | HRTIM_OUTR_FAULT2,
             HRTIM_OUTR_FAULT1_1 | HRTIM_OUTR_FAULT2_1);
  MODIFY_REG(hrtim->Instance->sTimerxRegs[HRTIM_TIMERINDEX_TIMER_F].OUTxR,
             HRTIM_OUTR_FAULT1 | HRTIM_OUTR_FAULT2,
             HRTIM_OUTR_FAULT1_1 | HRTIM_OUTR_FAULT2_1);
  /* Counters are stopped: transfer any preloaded output configuration now. */
  if (HAL_HRTIM_SoftwareUpdate(hrtim,
                               HRTIM_TIMERUPDATE_D | HRTIM_TIMERUPDATE_F) != HAL_OK)
  {
    Error_Handler();
  }

  /* MODE1=011: normal, internal-only, unbuffered. HFSEL=10 for 170 MHz AHB.
   * Dedicated DAC3 instance; no trigger/DMA/sample-and-hold is used.
   */
  __HAL_RCC_DAC3_FORCE_RESET();
  __HAL_RCC_DAC3_RELEASE_RESET();
  DAC3->MCR = DAC_MCR_MODE1_0 | DAC_MCR_MODE1_1 | DAC_MCR_HFSEL_1;
  DAC3->DHR12R1 = BREAKER_DAC_CODE;
  DAC3->CR = DAC_CR_EN1;
  HAL_Delay(1U); /* Startup only: allow the fixed reference to settle. */

  /* INPSEL=0: PA1; INMSEL=100: DAC3_CH1. Non-inverted, no hysteresis or
   * blanking: rising/falling thresholds both nominally 1.7 V.
   */
  COMP1->CSR = COMP_CSR_INMSEL_2;
  SET_BIT(COMP1->CSR, COMP_CSR_EN);
  HAL_Delay(1U); /* Startup only: comparator stabilization before PWM. */

  fault.Source = HRTIM_FAULTSOURCE_INTERNAL;
  fault.Polarity = HRTIM_FAULTPOLARITY_HIGH;
  fault.Filter = HRTIM_FAULTFILTER_NONE;
  fault.Lock = HRTIM_FAULTLOCK_READWRITE;
  if (HAL_HRTIM_FaultConfig(hrtim, HRTIM_FAULT_4, &fault) != HAL_OK)
  {
    Error_Handler();
  }
  SET_BIT(hrtim->Instance->sTimerxRegs[HRTIM_TIMERINDEX_TIMER_D].FLTxR,
          HRTIM_TIMFAULTENABLE_FAULT4);
  SET_BIT(hrtim->Instance->sTimerxRegs[HRTIM_TIMERINDEX_TIMER_F].FLTxR,
          HRTIM_TIMFAULTENABLE_FAULT4);
  /* Reset defaults leave fault blanking and event counting disabled. */
  HAL_HRTIM_FaultModeCtl(hrtim, HRTIM_FAULT_4, HRTIM_FAULTMODECTL_ENABLED);

  /* COMP1 is EXTI21 (RM0440 table 101); only recovery needs an IRQ.
   * Shutdown is asynchronous hardware, including while interrupts are masked.
   */
  CLEAR_BIT(EXTI->IMR1, EXTI_IMR1_IM21);
  CLEAR_BIT(EXTI->RTSR1, EXTI_RTSR1_RT21);
  SET_BIT(EXTI->FTSR1, EXTI_FTSR1_FT21);
  EXTI->PR1 = EXTI_PR1_PIF21;
  HAL_NVIC_ClearPendingIRQ(COMP1_2_3_IRQn);
  HAL_NVIC_SetPriority(COMP1_2_3_IRQn, 0U, 0U);
  SET_BIT(EXTI->IMR1, EXTI_IMR1_IM21);
  HAL_NVIC_EnableIRQ(COMP1_2_3_IRQn);
}

void PWM_Breaker_EnableOutputs(void)
{
  uint32_t primask = __get_PRIMASK();
  __disable_irq();
  outputs_requested = 1U;
  /* Always request initial output ownership, even with an already-high input.
   * The enabled hardware fault overrides OENR and holds all outputs low.
   */
  breaker_hrtim->Instance->sCommonRegs.OENR = PWM_BREAKER_OUTPUTS;
  __DSB();
  __set_PRIMASK(primask);
}

void PWM_Breaker_Poll(void)
{
  if ((outputs_requested == 0U) || ((COMP1->CSR & COMP_CSR_VALUE) != 0U))
  {
    return;
  }

  /* ODSR is latched: even a fault shorter than the software response is not
   * lost. This fallback also handles an edge occurring during initialization.
   * This module owns re-enabling; any future intentional stop must first
   * disarm recovery rather than simply writing ODISR elsewhere.
   */
  if ((breaker_hrtim->Instance->sCommonRegs.ODSR & PWM_BREAKER_OUTPUTS) != 0U)
  {
    breaker_hrtim->Instance->sCommonRegs.ICR = HRTIM_ICR_FLT4C;
    /* No HAL handle locking in the ISR. One write re-enables all channels.
     * A new high comparator level still wins in hardware, including a race
     * after the CSR read. Counters/compares and the startup state keep running.
     * Immediate recovery can expose the remainder of an in-progress pulse;
     * it deliberately does not wait for a new PWM period.
     */
    breaker_hrtim->Instance->sCommonRegs.OENR = PWM_BREAKER_OUTPUTS;
    __DSB();
  }
}

void PWM_Breaker_COMP_IRQHandler(void)
{
  if ((EXTI->PR1 & EXTI_PR1_PIF21) != 0U)
  {
    EXTI->PR1 = EXTI_PR1_PIF21;
    PWM_Breaker_Poll();
  }
}