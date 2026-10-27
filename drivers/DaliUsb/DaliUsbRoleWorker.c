/* SPDX-License-Identifier: GPL-2.0-only */
#include "DaliUsbRoleWorker.h"

static bool valid_platform_ops(const struct usb_role_ops *ops)
{
    return ops && ops->stop_submissions && ops->sync_storage && ops->cancel_requests &&
        ops->stop_dma && ops->select_role && ops->start;
}

static void remember_stop_error(struct dali_usb_role_worker *worker, int result)
{
    if (result && worker->stop_error == USB_OK) worker->stop_error = result;
}

static int dispatch_stop_submissions(void *context)
{
    struct dali_usb_role_worker *worker = context;
    int result = worker->platform_ops.stop_submissions(worker->platform_ops.context);
    remember_stop_error(worker, result);
    return result;
}

static int dispatch_sync_storage(void *context)
{
    struct dali_usb_role_worker *worker = context;
    int result = worker->platform_ops.sync_storage(worker->platform_ops.context);
    remember_stop_error(worker, result);
    return result;
}

static int dispatch_cancel_requests(void *context)
{
    struct dali_usb_role_worker *worker = context;
    int result = worker->platform_ops.cancel_requests(worker->platform_ops.context);
    remember_stop_error(worker, result);
    return result;
}

static int dispatch_stop_dma(void *context)
{
    struct dali_usb_role_worker *worker = context;
    int result = worker->platform_ops.stop_dma(worker->platform_ops.context);
    worker->dma_stopped = result == USB_OK;
    remember_stop_error(worker, result);
    return result;
}

static int dispatch_vbus(void *context, bool source)
{
    struct dali_usb_role_worker *worker = context;
    if (source) {
        if (worker->typec->mode != DALI_CC_HOST ||
            worker->typec->published.role != USB_ROLE_HOST ||
            worker->vbus->state != DALI_VBUS_OFF) return USB_DISCONNECTED;
        return dali_usb_vbus_enable(worker->vbus);
    }

    if (worker->vbus->state == DALI_VBUS_OFF) return USB_OK;
    /* Generic usb_role_switch() reaches VBUS-off even if sync/cancel failed.
     * Preserve the live role and source until the complete stop proof succeeds. */
    if (worker->stop_error) return worker->stop_error;
    if (!worker->dma_stopped) return USB_BUSY;
    int result = dali_usb_vbus_disable(worker->vbus);
    remember_stop_error(worker, result);
    return result;
}

static int dispatch_select_role(void *context, enum usb_role role)
{
    struct dali_usb_role_worker *worker = context;
    if (role != USB_ROLE_HOST && role != USB_ROLE_DEVICE) return USB_INVALID;
    if (worker->typec->published.role != role ||
        worker->typec->mode != (role == USB_ROLE_HOST ? DALI_CC_HOST : DALI_CC_DEVICE))
        return USB_DISCONNECTED;
    return worker->platform_ops.select_role(worker->platform_ops.context, role);
}

static int dispatch_start(void *context, enum usb_role role)
{
    struct dali_usb_role_worker *worker = context;
    if (worker->typec->published.role != role) return USB_DISCONNECTED;
    if (role == USB_ROLE_HOST && worker->vbus->state != DALI_VBUS_ON) return USB_IO;
    if (role == USB_ROLE_DEVICE && worker->vbus->state != DALI_VBUS_OFF) return USB_IO;
    return worker->platform_ops.start(worker->platform_ops.context, role);
}

static void reset_stop_attempt(struct dali_usb_role_worker *worker)
{
    worker->stop_error = USB_OK;
    worker->dma_stopped = false;
}

static int select_stopped_role(struct dali_usb_role_worker *worker)
{
    if (!worker->dma_stopped || worker->stop_error || worker->vbus->state != DALI_VBUS_OFF)
        return worker->stop_error ? worker->stop_error : USB_BUSY;
    return worker->platform_ops.select_role(worker->platform_ops.context, USB_ROLE_STOPPED);
}

static int stop_active_role(struct dali_usb_role_worker *worker)
{
    if (worker->role.role == USB_ROLE_STOPPED) return USB_OK;
    reset_stop_attempt(worker);
    int result = usb_role_switch(&worker->role, &worker->dispatch_ops, USB_ROLE_STOPPED);
    if (result) return result;
    result = select_stopped_role(worker);
    if (result) return result;
    return USB_OK;
}

static int fault_worker(struct dali_usb_role_worker *worker, int result)
{
    if (worker->role.role != USB_ROLE_STOPPED && !worker->role.faulted) {
        int stopped = stop_active_role(worker);
        if (stopped && !result) result = stopped;
    }
    worker->faulted = true;
    worker->role.faulted = true;
    return result ? result : USB_IO;
}

int dali_usb_role_worker_init(struct dali_usb_role_worker *worker,
    struct dali_usb_typec *typec, struct dali_usb_vbus *vbus,
    const struct usb_role_ops *platform_ops)
{
    if (!worker || !typec || !vbus || !valid_platform_ops(platform_ops) ||
        !typec->initialized || !vbus->initialized || vbus->typec != typec)
        return USB_INVALID;
    if (worker->initialized) return USB_BUSY;
    if (typec->faulted || vbus->faulted || vbus->state == DALI_VBUS_UNKNOWN) return USB_IO;
    if (!typec->started || typec->mode != DALI_CC_DRP ||
        typec->published.role != USB_ROLE_STOPPED || vbus->state != DALI_VBUS_OFF)
        return USB_BUSY;

    struct dali_usb_role_worker candidate = {0};
    candidate.typec = typec;
    candidate.vbus = vbus;
    candidate.platform_ops = *platform_ops;
    candidate.role.role = USB_ROLE_STOPPED;
    candidate.dispatch_ops = (struct usb_role_ops){
        .context = worker,
        .stop_submissions = dispatch_stop_submissions,
        .sync_storage = dispatch_sync_storage,
        .cancel_requests = dispatch_cancel_requests,
        .stop_dma = dispatch_stop_dma,
        .vbus = dispatch_vbus,
        .select_role = dispatch_select_role,
        .start = dispatch_start
    };
    candidate.initialized = true;
    *worker = candidate;
    return USB_OK;
}

static int park_drp(struct dali_usb_role_worker *worker)
{
    int result = stop_active_role(worker);
    if (result) return result;
    if (worker->typec->mode != DALI_CC_DRP)
        result = dali_usb_typec_mode(worker->typec, DALI_CC_DRP);
    return result;
}

int dali_usb_role_worker_poll(struct dali_usb_role_worker *worker, uint64_t now_us,
    struct dali_typec_sample *published)
{
    if (!worker || !worker->initialized || !published) return USB_INVALID;
    if (worker->faulted || worker->role.faulted || worker->typec->faulted || worker->vbus->faulted)
        return USB_IO;

    struct dali_typec_sample sample;
    int result = dali_usb_typec_poll(worker->typec, now_us, &sample);
    if (result == USB_AGAIN || result == USB_BUSY) return result;
    if (result) return fault_worker(worker, result);

    if (sample.role == worker->role.role) {
        if (sample.role == USB_ROLE_HOST) {
            /* Revalidate output and CC on every poll without replaying source writes. */
            result = dali_usb_vbus_enable(worker->vbus);
            if (result) return fault_worker(worker, result);
        } else if (sample.role == USB_ROLE_DEVICE &&
            (worker->typec->mode != DALI_CC_DEVICE || worker->vbus->state != DALI_VBUS_OFF)) {
            return fault_worker(worker, USB_IO);
        }
        *published = sample;
        return USB_OK;
    }

    if (sample.role == USB_ROLE_STOPPED) {
        bool was_active = worker->role.role != USB_ROLE_STOPPED;
        result = stop_active_role(worker);
        if (result) return fault_worker(worker, result);
        if (was_active) {
            result = dali_usb_typec_mode(worker->typec, DALI_CC_DRP);
            if (result) return fault_worker(worker, result);
        }
        *published = sample;
        return USB_OK;
    }

    if (worker->role.role != USB_ROLE_STOPPED) {
        /* The TCPC normally publishes STOPPED first. If an alternate backend
         * violates that contract, stop and re-debounce instead of cross-switching. */
        result = park_drp(worker);
        if (result) return fault_worker(worker, result);
        return USB_AGAIN;
    }
    if (worker->vbus->state != DALI_VBUS_OFF) return fault_worker(worker, USB_IO);

    result = dali_usb_typec_latch_attachment(worker->typec);
    if (result == USB_AGAIN || result == USB_DISCONNECTED) return USB_AGAIN;
    if (result == USB_BUSY) return result;
    if (result) return fault_worker(worker, result);

    reset_stop_attempt(worker);
    result = usb_role_switch(&worker->role, &worker->dispatch_ops, sample.role);
    if (result) {
        /* If a failed start was fully stopped, put the TCPC back in DRP. Never
         * alter termination when DMA/VBUS ownership is still uncertain. */
        if (worker->dma_stopped && !worker->stop_error &&
            worker->vbus->state == DALI_VBUS_OFF) {
            int safe = select_stopped_role(worker);
            if (!safe) safe = dali_usb_typec_mode(worker->typec, DALI_CC_DRP);
            if (safe) worker->stop_error = safe;
        }
        return fault_worker(worker, result);
    }
    *published = worker->typec->published;
    return USB_OK;
}

static int retry_faulted_stop(struct dali_usb_role_worker *worker)
{
    reset_stop_attempt(worker);
    int first = dispatch_stop_submissions(worker);
    int result = dispatch_sync_storage(worker);
    if (!first) first = result;
    result = dispatch_cancel_requests(worker);
    if (!first) first = result;
    result = dispatch_stop_dma(worker);
    if (!first) first = result;
    if (!worker->dma_stopped) return first ? first : USB_BUSY;
    if (worker->stop_error || first) return first ? first : worker->stop_error;
    if (worker->vbus->state != DALI_VBUS_OFF) {
        result = dali_usb_vbus_disable(worker->vbus);
        if (result) return result;
    }
    result = select_stopped_role(worker);
    return result ? result : first;
}

int dali_usb_role_worker_stop(struct dali_usb_role_worker *worker)
{
    if (!worker || !worker->initialized) return USB_INVALID;
    int result;
    if (worker->faulted || worker->role.faulted) {
        result = retry_faulted_stop(worker);
    } else {
        result = stop_active_role(worker);
    }
    if (result) {
        worker->faulted = true;
        worker->role.faulted = true;
        return result;
    }
    if (worker->typec->started) result = dali_usb_typec_stop(worker->typec);
    if (result) {
        worker->faulted = true;
        worker->role.faulted = true;
        return result;
    }
    *worker = (struct dali_usb_role_worker){0};
    return USB_OK;
}
