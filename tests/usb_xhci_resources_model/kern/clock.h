/* Test-only monotonic clock seam for the production XNU runtime model. */
#pragma once

#include <stdint.h>

inline uint64_t gIOKitTestUptimeNanoseconds = 0;

inline void clock_get_uptime(uint64_t *absolute_time)
{
    if (absolute_time) *absolute_time = gIOKitTestUptimeNanoseconds;
}

inline void absolutetime_to_nanoseconds(uint64_t absolute_time, uint64_t *nanoseconds)
{
    if (nanoseconds) *nanoseconds = absolute_time;
}
