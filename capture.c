#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <time.h>
#include <ucontext.h>
#include <unistd.h>

static volatile sig_atomic_t stop_requested;
static void stop_signal(int sig) { (void)sig; stop_requested = 1; }
static void fault(int sig, siginfo_t *info, void *context) {
    ucontext_t *uc = context;
    dprintf(2, "FAULT signal=%d address=%p pc=%08lx lr=%08lx\n", sig, info->si_addr,
            (unsigned long)uc->uc_mcontext.arm_pc, (unsigned long)uc->uc_mcontext.arm_lr);
    _exit(128 + sig);
}
static void *symbol(void *lib, const char *name) {
    void *value = dlsym(lib, name);
    if (!value) { dprintf(2, "missing %s: %s\n", name, dlerror()); exit(1); }
    dprintf(2, "symbol %s=%p\n", name, value);
    return value;
}
int main(int argc, char **argv) {
    int audio_fd = dup(1);
    dup2(2, 1);
    struct sigaction action = {.sa_sigaction=fault, .sa_flags=SA_SIGINFO};
    sigaction(SIGSEGV, &action, NULL);
    sigaction(SIGBUS, &action, NULL);
    alarm(10);
    if (!dlopen("librt.so.1", RTLD_NOW | RTLD_GLOBAL) ||
        !dlopen("libpthread.so.0", RTLD_NOW | RTLD_GLOBAL)) {
        dprintf(2, "runtime libraries: %s\n", dlerror()); return 1;
    }
    void *lib = dlopen("/usr/lib/libkadaptor.so.0.1.0", RTLD_NOW | RTLD_GLOBAL);
    if (!lib) { dprintf(2, "dlopen: %s\n", dlerror()); return 1; }
    dprintf(2, "phase MsOS_Init\n");
    unsigned char (*os_init)(void) = symbol(lib, "MsOS_Init");
    dprintf(2, "MsOS_Init=%u\n", os_init());
    dprintf(2, "phase UtopiaInit\n");
    unsigned (*utopia_init)(void) = symbol(lib, "UtopiaInit");
    dprintf(2, "UtopiaInit=%u\n", utopia_init());
    dprintf(2, "phase MDrv_MMIO_Init\n");
    unsigned char (*mmio_init)(void) = symbol(lib, "MDrv_MMIO_Init");
    dprintf(2, "MDrv_MMIO_Init=%u\n", mmio_init());
    dprintf(2, "phase audio mapping\n");
    void (*audio_mapping)(void) = symbol(lib, "MDrv_AUDIO_InitMMIO");
    audio_mapping();
    dprintf(2, "phase DSP base\n");
    uint32_t (*dsp_base)(unsigned char) = symbol(lib, "MApi_AUDIO_GetDspMadBaseAddr");
    dprintf(2, "DSP base=%08x\n", dsp_base(1));
    uint16_t (*read_reg)(unsigned) = symbol(lib, "HAL_AUDIO_ReadReg");
    dprintf(2, "capture thresholds: %04x %04x\n", read_reg(0x2dd6), read_reg(0x2d3a));
    if (argc > 1 && (!strcmp(argv[1], "--capture") || !strcmp(argv[1], "--stream"))) {
        unsigned char channel = argc > 2 ? atoi(argv[2]) : 1;
        unsigned source = argc > 3 ? atoi(argv[3]) : 8;
        double seconds = argc > 4 ? atof(argv[4]) : 2;
        int stream = !strcmp(argv[1], "--stream");
        if (channel > 1) return 2;
        /* Optional UDP target: sequence-numbered, timestamped 10 ms datagrams with a resend history. */
        int udp = -1;
        struct sockaddr_in target = {.sin_family=AF_INET};
        if (stream && argc > 6) {
            if (inet_pton(AF_INET, argv[5], &target.sin_addr) != 1) return 2;
            target.sin_port = htons(atoi(argv[6]));
            udp = socket(AF_INET, SOCK_DGRAM, 0);
            /* Best effort by default: this driver's voice queue (EF) held packets up to 5 s and dropped 10%. */
            int tos = argc > 7 ? (int)strtol(argv[7], NULL, 0) : 0, priority = tos >> 5;
            setsockopt(udp, IPPROTO_IP, IP_TOS, &tos, sizeof(tos));
            setsockopt(udp, SOL_SOCKET, SO_PRIORITY, &priority, sizeof(priority));
            if (udp < 0 || connect(udp, (struct sockaddr *)&target, sizeof(target))) { dprintf(2, "udp setup failed: %s\n", strerror(errno)); return 2; }
            dprintf(2, "udp target %s:%s\n", argv[5], argv[6]);
        }
        uint32_t sequence = 0;
        /* A UDP stream never writes to the SSH channel, so a dropped session is seen only as a new parent. */
        pid_t session = getppid();
        unsigned char packet[8+1920]; /* seq, send time in microseconds (CLOCK_MONOTONIC, low 32 bits), PCM */
        /* Last 1.28 s of packets, kept so the receiver can ask for exactly the ones it missed. */
        enum { HISTORY = 128 };
        static unsigned char history[HISTORY][8+1920];
        static uint32_t history_seq[HISTORY];
        uint64_t resent = 0, nack_requests = 0;
        if (read_reg(channel ? 0x2d3a : 0x2dd6)) {
            dprintf(2, "capture channel is in use\n"); return 3;
        }
        unsigned char (*capture_init)(unsigned char, unsigned) = symbol(lib, "MApi_AUDIO_PCMCapture_Init");
        unsigned char (*capture_start)(unsigned char) = symbol(lib, "MApi_AUDIO_PCMCapture_Start");
        unsigned char (*capture_read)(unsigned char, void *, unsigned) = symbol(lib, "MApi_AUDIO_PCMCapture_Read");
        unsigned char (*capture_stop)(unsigned char) = symbol(lib, "MApi_AUDIO_PCMCapture_Stop");
        dprintf(2, "capture init channel=%u source=%u\n", channel, source);
        if (!capture_init(channel, source)) { dprintf(2, "capture init failed\n"); return 4; }
        signal(SIGINT, stop_signal); signal(SIGTERM, stop_signal); signal(SIGHUP, stop_signal);
        signal(SIGALRM, stop_signal); signal(SIGPIPE, SIG_IGN); signal(SIGCHLD, SIG_IGN);
        if (!capture_start(channel)) { capture_stop(channel); dprintf(2, "capture start failed\n"); return 5; }
        alarm(0);
        int16_t buffer[960];
        unsigned chunks = 0, attempts = 0, peak = 0;
        uint64_t squares = 0;
        struct timespec begin, now;
        clock_gettime(CLOCK_MONOTONIC, &begin);
        /* Per-second timing of the capture itself, to separate DSP burstiness from transport. */
        double last_ok = 0, max_read_gap = 0, max_write = 0, window_start = 0;
        unsigned window_chunks = 0;
        do {
            attempts++;
            if (capture_read(channel, buffer, sizeof(buffer))) {
                for (unsigned i=0; i<960; i++) {
                    int value = buffer[i]; unsigned magnitude = value < 0 ? -value : value;
                    if (magnitude > peak) peak = magnitude;
                    squares += (int64_t)value * value;
                }
                clock_gettime(CLOCK_MONOTONIC, &now);
                double t = now.tv_sec + now.tv_nsec/1e9;
                if (last_ok && t-last_ok > max_read_gap) max_read_gap = t-last_ok;
                last_ok = t;
                if (udp >= 0) {
                    struct timespec sent;
                    clock_gettime(CLOCK_MONOTONIC, &sent);
                    uint32_t sent_us = (uint32_t)(sent.tv_sec*1000000ULL + sent.tv_nsec/1000);
                    memcpy(packet, &sequence, 4);
                    memcpy(packet+4, &sent_us, 4);
                    memcpy(packet+8, buffer, sizeof(buffer));
                    memcpy(history[sequence%HISTORY], packet, sizeof(packet));
                    history_seq[sequence%HISTORY] = sequence;
                    sequence++;
                    send(udp, packet, sizeof(packet), 0);
                } else if (write(audio_fd, buffer, sizeof(buffer)) != sizeof(buffer)) break;
                clock_gettime(CLOCK_MONOTONIC, &now);
                double w = now.tv_sec + now.tv_nsec/1e9 - t;
                if (w > max_write) max_write = w;
                chunks++; window_chunks++;
                if (!window_start) window_start = t;
                if (t-window_start >= 1) {
                    dprintf(2, "timing chunks=%u max_read_gap_ms=%.1f max_write_ms=%.1f attempts=%u nacks=%llu resent=%llu\n", window_chunks, max_read_gap*1000, max_write*1000, attempts, (unsigned long long)nack_requests, (unsigned long long)resent);
                    window_start = t; window_chunks = 0; max_read_gap = 0; max_write = 0;
                }
            } else usleep(5000);
            if (udp >= 0) {
                /* NACK datagram: "NACK" then up to 64 little-endian sequence numbers. */
                unsigned char nack[4+64*4];
                ssize_t got;
                while ((got = recv(udp, nack, sizeof(nack), MSG_DONTWAIT)) >= 4) {
                    if (!memcmp(nack, "PING", 4)) {
                        /* Echo immediately: the Mac measures the router-to-TV leg with these. */
                        memcpy(nack, "PONG", 4);
                        send(udp, nack, got, 0);
                        continue;
                    }
                    if (!memcmp(nack, "RASC", 4)) {
                        /* Refresh the association when the Mac sees the downlink degrading. */
                        dprintf(2, "reassociate requested\n");
                        if (fork() == 0) {
                            execl("/usr/sbin/wpa_cli", "wpa_cli", "-p", "/var/run/wpa_supplicant", "-i", "wlan0", "reassociate", (char *)NULL);
                            _exit(127);
                        }
                        continue;
                    }
                    if (got < 8 || memcmp(nack, "NACK", 4)) continue;
                    nack_requests++;
                    for (ssize_t at = 4; at+4 <= got; at += 4) {
                        uint32_t wanted;
                        memcpy(&wanted, nack+at, 4);
                        if (history_seq[wanted%HISTORY] == wanted && sequence-wanted <= HISTORY) {
                            send(udp, history[wanted%HISTORY], sizeof(packet), 0);
                            resent++;
                        }
                    }
                }
            }
            clock_gettime(CLOCK_MONOTONIC, &now);
        } while (!stop_requested && getppid() == session && (stream || (now.tv_sec - begin.tv_sec) + (now.tv_nsec - begin.tv_nsec)/1e9 < seconds));
        dprintf(2, "capture stop=%u\n", capture_stop(channel));
        dprintf(2, "RESULT chunks=%u bytes=%u attempts=%u peak=%u mean_square=%.2f final_threshold=%u\n", chunks, chunks*(unsigned)sizeof(buffer), attempts, peak, chunks ? squares/(double)(chunks*960) : 0, read_reg(channel ? 0x2d3a : 0x2dd6));
    }
    return 0;
}
