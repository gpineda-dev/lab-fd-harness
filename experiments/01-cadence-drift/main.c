/*
 * main.c - Native C benchmark under fd-harness supervisor.
 * Demonstrates sub-millisecond and high-frequency (100Hz+) pacing with zero forks.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <getopt.h>

static double get_time_sec(clockid_t clk_id) {
    struct timespec ts;
    clock_gettime(clk_id, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static void print_timestamp(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    struct tm tm_buf;
    localtime_r(&ts.tv_sec, &tm_buf);
    printf("%02d:%02d:%02d.%06ld",
           tm_buf.tm_hour, tm_buf.tm_min, tm_buf.tm_sec,
           ts.tv_nsec / 1000);
}

static void read_harness_response(char *buf, size_t max_len) {
    if (fgets(buf, (int)max_len, stdin) == NULL) {
        buf[0] = '\0';
    }
}

int main(int argc, char *argv[]) {
    // Unbuffered stdout for immediate IPC delivery to supervisor
    setvbuf(stdout, NULL, _IONBF, 0);

    char strategy[64] = "clock";
    double interval = 0.01;      // 10ms default (100 Hz!)
    double work_dur = 0.0;       // 0s default
    int cycles = 10;
    char align[64] = "";
    int opt;

    static struct option long_options[] = {
        {"strategy", required_argument, 0, 's'},
        {"interval", required_argument, 0, 'i'},
        {"work",     required_argument, 0, 'w'},
        {"cycles",   required_argument, 0, 'n'},
        {"align",    required_argument, 0, 'a'},
        {"help",     no_argument,       0, 'h'},
        {0, 0, 0, 0}
    };

    while ((opt = getopt_long(argc, argv, "s:i:w:n:a:h", long_options, NULL)) != -1) {
        switch (opt) {
            case 's':
                strncpy(strategy, optarg, sizeof(strategy) - 1);
                break;
            case 'i':
                interval = atof(optarg);
                break;
            case 'w':
                work_dur = atof(optarg);
                break;
            case 'n':
                cycles = atoi(optarg);
                break;
            case 'a':
                strncpy(align, optarg, sizeof(align) - 1);
                break;
            case 'h':
            default:
                fprintf(stderr, "Usage: %s [-s clock|timer] [-i sec] [-w sec] [-n cycles] [-a align]\n", argv[0]);
                return 1;
        }
    }

    // Normalize strategy aliases
    if (strcmp(strategy, "harnessed-clock") == 0) strcpy(strategy, "clock");
    if (strcmp(strategy, "harnessed-timer") == 0) strcpy(strategy, "timer");

    printf("=== Native C Cadence Drift Benchmark ===\n");
    printf("Strategy:      harnessed-%s\n", strategy);
    printf("Interval:      %.4fs (%.1f Hz)\n", interval, 1.0 / interval);
    printf("Work duration: %.4fs\n", work_dur);
    printf("Cycles:        %d\n", cycles);
    printf("Started at:    ");
    print_timestamp();
    printf("\n-------------------------------------------------------------\n");

    char resp[256];

    // Optional pre-benchmark grid alignment
    if (strlen(align) > 0) {
        printf("# @harness.shift to=\"%s\"\n", align);
        read_harness_response(resp, sizeof(resp));
    }

    printf("@@@ CLAP:START @@@\n");
    double t0 = get_time_sec(CLOCK_MONOTONIC);

    if (strcmp(strategy, "clock") == 0) {
        // Mode 1: Harnessed Clock (Isochronous Metronome)
        printf("# @harness.clock:init id=bench interval=%.4f cycles=%d policy=skip\n", interval, cycles);

        while (1) {
            printf("# @harness.clock:wait id=bench\n");
            read_harness_response(resp, sizeof(resp));

            char tag[32], id[32], status[32];
            int cycle = 0, skipped = 0;
            double lag_ms = 0.0, mono_ts = 0.0;

            int parsed = sscanf(resp, "%31s %31s %d %d %lf %lf %31s",
                                tag, id, &cycle, &skipped, &lag_ms, &mono_ts, status);

            if (parsed >= 7 && strcmp(status, "done") == 0) {
                break;
            }

            printf("[Cycle %d] ", cycle);
            print_timestamp();
            printf(" (lag: %.3fms)\n", lag_ms);

            if (work_dur > 0.0) {
                struct timespec req;
                req.tv_sec = (time_t)work_dur;
                req.tv_nsec = (long)((work_dur - req.tv_sec) * 1e9);
                nanosleep(&req, NULL);
            }
        }
    } else {
        // Mode 2: Harnessed Timer (Stateless Relative/Delta Sleep)
        for (int i = 0; i < cycles; i++) {
            printf("[Cycle %d] ", i);
            print_timestamp();
            printf("\n");

            if (work_dur > 0.0) {
                struct timespec req;
                req.tv_sec = (time_t)work_dur;
                req.tv_nsec = (long)((work_dur - req.tv_sec) * 1e9);
                nanosleep(&req, NULL);
            }

            if (i < cycles - 1) {
                double target = t0 + (i + 1) * interval;
                double now = get_time_sec(CLOCK_MONOTONIC);
                double remaining = target - now;

                if (remaining > 0.0) {
                    printf("# @harness.sleep duration=%.6f\n", remaining);
                    read_harness_response(resp, sizeof(resp));
                }
            }
        }
    }

    printf("@@@ CLAP:END @@@\n");
    double t_end = get_time_sec(CLOCK_MONOTONIC);
    double actual_dur = t_end - t0;
    double theoretical_dur = (cycles - 1) * interval + work_dur;
    double drift_ms = (actual_dur - theoretical_dur) * 1000.0;

    printf("-------------------------------------------------------------\n");
    printf("Finished at:            ");
    print_timestamp();
    printf("\n");
    printf("Theoretical duration:   %.4fs\n", theoretical_dur);
    printf("Actual duration:        %.4fs\n", actual_dur);
    printf("Net cumulative drift:   %+0.3f ms\n", drift_ms);
    printf("=============================================================\n");

    return 0;
}
