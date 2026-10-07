// mphalport.h — minimal HAL declarations for the NanoSoC port.
// stdin_rx_chr / stdout_tx_strn live in nanosoc_uart.c.

#include <stdint.h>

static inline mp_uint_t mp_hal_ticks_ms(void) {
    return 0;
}
static inline mp_uint_t mp_hal_ticks_us(void) {
    return 0;
}
static inline mp_uint_t mp_hal_ticks_cpu(void) {
    return 0;
}
static inline void mp_hal_set_interrupt_char(char c) {
    (void)c;
}
static inline void mp_hal_delay_ms(mp_uint_t ms) {
    // crude busy-wait; the REPL does not depend on accurate timing
    volatile uint32_t n = ms * 5000u;
    while (n--) {
    }
}
static inline void mp_hal_delay_us(mp_uint_t us) {
    volatile uint32_t n = us * 5u;
    while (n--) {
    }
}
