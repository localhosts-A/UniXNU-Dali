// SPDX-License-Identifier: BSD-2-Clause-Patent
#pragma once
// Only the explicit read-only diagnostic may consume this recovery path.
// The single-CPU worker retains context while waiting. No automatic reset.
extern "C" void dali_ufs_wait_for_manual_recovery(const char *token, bool (*quiesce)(void *), void *context);
// Acquire the token-bound platform checkpoint published by DaliBootLog only
// after single-CPU/GIC/timer readiness and successful persistent logging.
extern "C" bool dali_ufs_platform_ready(const char *token);
