#pragma once
#include <stdlib.h>
#include <string.h>
typedef struct { size_t size; int ready; unsigned char data[]; } *QueueHandle_t;
static inline QueueHandle_t xQueueCreate(int count, size_t size) {
    (void)count; QueueHandle_t q=calloc(1,sizeof(*q)+size); q->size=size; return q;
}
static inline void xQueueOverwrite(QueueHandle_t q, const void *data) { memcpy(q->data,data,q->size); q->ready=1; }
static inline int xQueueReceive(QueueHandle_t q, void *out, int timeout) {
    (void)timeout; if(!q->ready) return 0; memcpy(out,q->data,q->size); q->ready=0; return 1;
}
