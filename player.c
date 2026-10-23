#include <AudioToolbox/AudioToolbox.h>
#include <CoreAudio/CoreAudio.h>
#include <CoreFoundation/CoreFoundation.h>
#include <errno.h>
#include <math.h>
#include <pthread.h>
#include <pthread/qos.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/stat.h>
#include <unistd.h>

#define RATE 48000
#define FRAME_BYTES 4
#define BLOCK_BYTES 2048 /* 512 stereo frames, matching the hardware period */
#define CAPACITY 48000 /* 250 ms ceiling; transport jitter must not grow without bound */
/* Startup/recovery reserve and steady-state target; set by argv[2] in milliseconds. */
static size_t target_bytes = 80*RATE/1000*FRAME_BYTES;
#define OUT_FRAMES (BLOCK_BYTES/FRAME_BYTES)
#define WINDOW_CALLBACKS 94 /* about one second of 512-frame callbacks */
static unsigned char ring[CAPACITY];
static size_t count, head;
static uint64_t received, dropped, underflows;
static uint64_t recoveries;
static int prefilled;
/* Standing delay is removed by playing slightly fast (interpolated), never by skipping blocks. */
static double read_fraction, speed;
static size_t window_min = (size_t)-1;
static unsigned window_callbacks;
static uint64_t trimmed_frames_x1000;
static uint64_t callbacks;
static double max_input_gap_ms, window_gap_ms, last_input_time;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t ready = PTHREAD_COND_INITIALIZER;
static volatile sig_atomic_t stopping;
static atomic_int ended;
static CFStringRef output_uid;
static AudioDeviceID output_device;
static void stop_signal(int sig) { (void)sig; stopping = 1; }
static double monotonic_seconds(void) {
    struct timespec value;
    clock_gettime(CLOCK_MONOTONIC, &value);
    return value.tv_sec + value.tv_nsec/1e9;
}

static int any_output;
/* Default output device and its name; the name is empty if CoreAudio has none. */
static AudioDeviceID default_output(char *text, size_t length) {
    AudioDeviceID device = 0;
    UInt32 size = sizeof(device);
    AudioObjectPropertyAddress address = {kAudioHardwarePropertyDefaultOutputDevice, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
    AudioObjectGetPropertyData(kAudioObjectSystemObject, &address, 0, NULL, &size, &device);
    CFStringRef name = NULL;
    address.mSelector = kAudioObjectPropertyName;
    size = sizeof(name);
    text[0] = 0;
    if (!AudioObjectGetPropertyData(device, &address, 0, NULL, &size, &name) && name) {
        CFStringGetCString(name, text, length, kCFStringEncodingUTF8);
        CFRelease(name);
    }
    return device;
}

static void describe_device(void) {
    char text[256];
    AudioDeviceID device = default_output(text, sizeof(text));
    UInt32 size;
    AudioObjectPropertyAddress address = {kAudioObjectPropertyName, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
    fprintf(stderr, "output_device=%s device_id=%u\n", text, device);
    output_device=device;
    if (!any_output && !strcasestr(text, "AirPods")) {
        fprintf(stderr, "Select AirPods before starting this player\n"); exit(3);
    }
    address.mSelector=kAudioDevicePropertyDeviceUID;
    size=sizeof(output_uid);
    if (AudioObjectGetPropertyData(device, &address, 0, NULL, &size, &output_uid)) {
        fprintf(stderr, "Cannot identify AirPods audio device\n"); exit(3);
    }
    UInt32 selectors[] = {kAudioDevicePropertyLatency, kAudioDevicePropertySafetyOffset, kAudioDevicePropertyBufferFrameSize};
    const char *labels[] = {"device_latency", "safety_offset", "hardware_buffer"};
    for (unsigned i=0; i<3; i++) {
        UInt32 frames = 0;
        size = sizeof(frames);
        address.mSelector = selectors[i];
        address.mScope = kAudioDevicePropertyScopeOutput;
        OSStatus status = AudioObjectGetPropertyData(device, &address, 0, NULL, &size, &frames);
        fprintf(stderr, "%s_frames=%u milliseconds=%.2f status=%d\n", labels[i], frames, frames*1000.0/RATE, status);
    }
}

static void *reader(void *unused) {
    (void)unused;
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
    unsigned char input[4096];
    size_t residual = 0;
    time_t last = 0;
    while (!stopping) {
        ssize_t n = read(0, input+residual, sizeof(input)-residual);
        if (n <= 0) { if (n < 0 && errno == EINTR) continue; break; }
        size_t available = residual+n;
        size_t length = available & ~(FRAME_BYTES-1);
        pthread_mutex_lock(&lock);
        double input_time=monotonic_seconds();
        double gap_ms = last_input_time ? (input_time-last_input_time)*1000 : 0;
        if (gap_ms > max_input_gap_ms) max_input_gap_ms = gap_ms;
        if (gap_ms > window_gap_ms) window_gap_ms = gap_ms;
        last_input_time=input_time;
        received += length;
        if (count+length > CAPACITY) {
            size_t discard = count+length-CAPACITY;
            head = (head+discard)%CAPACITY;
            count -= discard;
            dropped += discard;
        }
        size_t tail = (head+count)%CAPACITY;
        size_t first = length < CAPACITY-tail ? length : CAPACITY-tail;
        memcpy(ring+tail, input, first);
        memcpy(ring, input+first, length-first);
        count += length;
        pthread_cond_signal(&ready);
        time_t now = time(NULL);
        if (now != last) {
            fprintf(stderr, "t=%.2f callbacks=%llu received=%llu queued_ms=%.2f speed=%.3f%% trimmed_ms=%.1f dropped_ms=%.2f underflows=%llu max_input_gap_ms=%.2f gap_1s_ms=%.1f\n", (double)time(NULL), (unsigned long long)callbacks, (unsigned long long)received, count*1000.0/(RATE*FRAME_BYTES), speed*100, trimmed_frames_x1000/1000.0*1000.0/RATE, dropped*1000.0/(RATE*FRAME_BYTES), (unsigned long long)underflows, max_input_gap_ms, window_gap_ms);
            window_gap_ms=0;
            last=now;
        }
        pthread_mutex_unlock(&lock);
        residual = available-length;
        if (residual) memmove(input, input+length, residual);
    }
    pthread_mutex_lock(&lock);
    ended=1;
    pthread_cond_signal(&ready);
    pthread_mutex_unlock(&lock);
    return NULL;
}

static inline int16_t sample_at(size_t frame, int channel) {
    size_t offset = (head+frame*FRAME_BYTES+channel*2)%CAPACITY;
    int16_t value;
    memcpy(&value, ring+offset, 2);
    return value;
}

static void output(void *user, AudioQueueRef queue, AudioQueueBufferRef buffer) {
    (void)user;
    int16_t *out = buffer->mAudioData;
    pthread_mutex_lock(&lock);
    callbacks++;
    if (!prefilled && count >= target_bytes) prefilled=1;
    double step = 1.0+speed;
    size_t needed = (size_t)(read_fraction+OUT_FRAMES*step)+2;
    if (prefilled && count/FRAME_BYTES >= needed) {
        for (unsigned j=0; j<OUT_FRAMES; j++) {
            double position = read_fraction+j*step;
            size_t index = (size_t)position;
            double weight = position-index;
            for (int c=0; c<2; c++)
                out[j*2+c] = (int16_t)lrint(sample_at(index, c)*(1-weight)+sample_at(index+1, c)*weight);
        }
        double end = read_fraction+OUT_FRAMES*step;
        size_t consumed = (size_t)end;
        read_fraction = end-consumed;
        head = (head+consumed*FRAME_BYTES)%CAPACITY;
        count -= consumed*FRAME_BYTES;
        trimmed_frames_x1000 += (uint64_t)lrint(OUT_FRAMES*speed*1000);
    } else {
        size_t length = prefilled ? count & ~(size_t)(FRAME_BYTES-1) : 0;
        if (length > BLOCK_BYTES) length = BLOCK_BYTES;
        size_t first = length < CAPACITY-head ? length : CAPACITY-head;
        memcpy(out, ring+head, first);
        memcpy((char*)out+first, ring, length-first);
        memset((char*)out+length, 0, BLOCK_BYTES-length);
        head = (head+length)%CAPACITY;
        count -= length;
        read_fraction = 0;
        if (prefilled) { underflows++; recoveries++; prefilled=0; }
    }
    /* Queue depth that never dips below the target for a whole second is pure added delay. */
    if (prefilled && count < window_min) window_min = count;
    if (++window_callbacks >= WINDOW_CALLBACKS) {
        double excess_ms = window_min == (size_t)-1 ? 0 : ((double)window_min-(double)target_bytes)*1000.0/(RATE*FRAME_BYTES);
        speed = excess_ms > 5 ? fmin(0.02, excess_ms*0.0002) : 0;
        window_min = (size_t)-1;
        window_callbacks = 0;
    }
    pthread_mutex_unlock(&lock);
    buffer->mAudioDataByteSize=BLOCK_BYTES;
    if (!stopping) AudioQueueEnqueueBuffer(queue, buffer, 0, NULL);
}

int main(int argc, char **argv) {
    if (argc > 1 && !strcmp(argv[1], "--default-output")) {
        /* Fast presence check for the relay and agent (system_profiler takes about a second). */
        char text[256];
        default_output(text, sizeof(text));
        puts(text);
        return 0;
    }
    float volume = argc > 1 ? atof(argv[1])/100.0f : 0.35f;
    if (volume < 0 || volume > 1) return 2;
    int target_ms = argc > 2 ? atoi(argv[2]) : 80;
    if (target_ms < 20 || target_ms > 200) return 2;
    target_bytes = (size_t)target_ms*RATE/1000*FRAME_BYTES;
    signal(SIGTERM, stop_signal); signal(SIGINT, stop_signal); signal(SIGHUP, stop_signal);
    any_output = argc > 3 && !strcmp(argv[3], "any");
    describe_device();
    AudioStreamBasicDescription format = {
        .mSampleRate=RATE, .mFormatID=kAudioFormatLinearPCM,
        .mFormatFlags=kLinearPCMFormatFlagIsSignedInteger|kLinearPCMFormatFlagIsPacked,
        .mBytesPerPacket=FRAME_BYTES, .mFramesPerPacket=1,
        .mBytesPerFrame=FRAME_BYTES, .mChannelsPerFrame=2, .mBitsPerChannel=16
    };
    AudioQueueRef queue = NULL;
    OSStatus status = AudioQueueNewOutput(&format, output, NULL, NULL, NULL, 0, &queue);
    if (status) { fprintf(stderr, "AudioQueueNewOutput failed: %d\n", status); return 1; }
    status=AudioQueueSetProperty(queue, kAudioQueueProperty_CurrentDevice, &output_uid, sizeof(output_uid));
    CFRelease(output_uid);
    if (status) { fprintf(stderr, "Cannot bind player to AirPods: %d\n", status); return 1; }
    AudioQueueSetParameter(queue, kAudioQueueParam_Volume, volume);
    pthread_t input_thread;
    pthread_create(&input_thread, NULL, reader, NULL);
    AudioQueueBufferRef buffers[3];
    for (unsigned i=0; i<3; i++) {
        status=AudioQueueAllocateBuffer(queue, BLOCK_BYTES, &buffers[i]);
        if (status) { fprintf(stderr, "AudioQueueAllocateBuffer: %d\n", status); return 1; }
        output(NULL, queue, buffers[i]);
    }
    status=AudioQueueStart(queue, NULL);
    if (status) { fprintf(stderr, "AudioQueueStart: %d\n", status); return 1; }
    fprintf(stderr, "PLAYING stereo PCM 48000 Hz/16-bit; hardware blocks=3 x 512 frames; target=%dms, ceiling=250ms\n", target_ms);
    int lost_device=0;
    /* Live volume: the relay rewrites this file (0-100) when the remote's volume keys are pressed. */
    const char *volume_file = getenv("AIRPODS_PLAYER_VOLUME_FILE");
    struct timespec volume_mtime = {0, 0};
    while (!stopping && !ended) {
        usleep(100000);
        struct stat info;
        if (volume_file && !stat(volume_file, &info) &&
            (info.st_mtimespec.tv_sec != volume_mtime.tv_sec || info.st_mtimespec.tv_nsec != volume_mtime.tv_nsec)) {
            volume_mtime = info.st_mtimespec;
            FILE *file = fopen(volume_file, "r");
            float level;
            if (file && fscanf(file, "%f", &level) == 1 && level >= 0 && level <= 100) {
                AudioQueueSetParameter(queue, kAudioQueueParam_Volume, level/100.0f);
                fprintf(stderr, "volume=%.0f\n", level);
            }
            if (file) fclose(file);
        }
        UInt32 alive=0, size=sizeof(alive);
        AudioObjectPropertyAddress address={kAudioDevicePropertyDeviceIsAlive, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
        if (AudioObjectGetPropertyData(output_device, &address, 0, NULL, &size, &alive) || !alive) {
            fprintf(stderr, "Output audio device disconnected\n");
            lost_device=1;
            break;
        }
    }
    stopping=1;
    AudioQueueStop(queue, true);
    AudioQueueDispose(queue, true);
    return lost_device ? 1 : 0;
}
