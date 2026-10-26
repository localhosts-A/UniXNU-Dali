#include "DaliUsbCdcLog.h"

#include <assert.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>

static pthread_mutex_t g_gate_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_gate_condition = PTHREAD_COND_INITIALIZER;
static bool g_block_writer;
static bool g_writer_entered;
static bool g_release_writer;
static size_t g_accept_limit = SIZE_MAX;
static struct usb_mtu3_device *g_last_device;
static uint8_t g_last_bytes[64];
static size_t g_last_length;

/* Test-only replacement for the portable ring writer. */
size_t usb_mtu3_device_log(struct usb_mtu3_device *device, const void *bytes, size_t length)
{
    pthread_mutex_lock(&g_gate_mutex);
    if (g_block_writer) {
        g_writer_entered = true;
        pthread_cond_broadcast(&g_gate_condition);
        while (!g_release_writer) pthread_cond_wait(&g_gate_condition, &g_gate_mutex);
    }
    g_last_device = device;
    g_last_length = length < g_accept_limit ? length : g_accept_limit;
    if (g_last_length > sizeof(g_last_bytes)) g_last_length = sizeof(g_last_bytes);
    memcpy(g_last_bytes, bytes, g_last_length);
    pthread_mutex_unlock(&g_gate_mutex);
    return g_last_length;
}

struct writer_args { const void *bytes; size_t length; size_t accepted; };

static void *write_thread(void *context)
{
    struct writer_args *args = context;
    args->accepted = dali_usb_cdc_log_write(args->bytes, args->length);
    return NULL;
}

int main(void)
{
    struct usb_cdc cdc = {0};
    struct usb_mtu3_device device = {.cdc = &cdc, .initialized = true};
    struct usb_mtu3_device uninitialized = {.cdc = &cdc};
    const uint8_t message[] = "early log";

    assert(dali_usb_cdc_log_write(message, sizeof(message) - 1) == 0);
    assert(dali_usb_cdc_log_dropped() == sizeof(message) - 1);
    assert(dali_usb_cdc_log_bind(&uninitialized) == USB_INVALID);
    assert(dali_usb_cdc_log_bind(NULL) == USB_INVALID);
    assert(dali_usb_cdc_log_bind(&device) == USB_OK);
    assert(dali_usb_cdc_log_bind(&device) == USB_OK);
    assert(dali_usb_cdc_log_write(message, sizeof(message) - 1) == sizeof(message) - 1);
    assert(g_last_device == &device && g_last_length == sizeof(message) - 1);
    assert(memcmp(g_last_bytes, message, sizeof(message) - 1) == 0);

    g_accept_limit = 3;
    assert(dali_usb_cdc_log_write(message, sizeof(message) - 1) == 3);
    assert(dali_usb_cdc_log_unbind(&uninitialized) == USB_INVALID);
    assert(dali_usb_cdc_log_unbind(&device) == USB_OK);
    assert(dali_usb_cdc_log_write(message, sizeof(message) - 1) == 0);
    assert(dali_usb_cdc_log_dropped() == (sizeof(message) - 1) * 2);

    g_accept_limit = SIZE_MAX;
    assert(dali_usb_cdc_log_bind(&device) == USB_OK);
    pthread_mutex_lock(&g_gate_mutex);
    g_block_writer = true;
    g_writer_entered = false;
    g_release_writer = false;
    pthread_mutex_unlock(&g_gate_mutex);

    struct writer_args args = {message, sizeof(message) - 1, 0};
    pthread_t writer;
    assert(pthread_create(&writer, NULL, write_thread, &args) == 0);
    pthread_mutex_lock(&g_gate_mutex);
    while (!g_writer_entered) pthread_cond_wait(&g_gate_condition, &g_gate_mutex);
    pthread_mutex_unlock(&g_gate_mutex);

    /* The provider cannot stop or free the gadget until the in-flight producer
     * has returned and a retry proves the transient reference drained. */
    assert(dali_usb_cdc_log_unbind(&device) == USB_BUSY);
    assert(dali_usb_cdc_log_write(message, sizeof(message) - 1) == 0);
    pthread_mutex_lock(&g_gate_mutex);
    g_release_writer = true;
    pthread_cond_broadcast(&g_gate_condition);
    pthread_mutex_unlock(&g_gate_mutex);
    assert(pthread_join(writer, NULL) == 0);
    assert(args.accepted == sizeof(message) - 1);
    assert(dali_usb_cdc_log_unbind(&device) == USB_OK);
    puts("PASS: nonblocking Dali CDC log sink and stop-safe unbind");
    return 0;
}
