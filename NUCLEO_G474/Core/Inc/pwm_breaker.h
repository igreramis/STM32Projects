#ifndef PWM_BREAKER_H
#define PWM_BREAKER_H

#include "stm32g4xx_hal.h"

/* Set this to the measured board VREF+ voltage for threshold accuracy. */
#define PWM_BREAKER_VREF_MV 3300U
#define PWM_BREAKER_THRESHOLD_MV 1700U
#define PWM_BREAKER_OUTPUTS (HRTIM_OUTPUT_TD1 | HRTIM_OUTPUT_TD2 | HRTIM_OUTPUT_TF1)

/* Init once after HRTIM configuration, before any counter/output starts. */
void PWM_Breaker_Init(HRTIM_HandleTypeDef *hrtim);
/* Owns output enabling from this point until reset, including fault recovery. */
void PWM_Breaker_EnableOutputs(void);
void PWM_Breaker_Poll(void);
void PWM_Breaker_COMP_IRQHandler(void);

#endif /* PWM_BREAKER_H */