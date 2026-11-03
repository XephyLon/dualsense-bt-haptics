/*
 * ds5-haptics-sink: PipeWire sink that streams audio to a DualSense /
 * DualSense Edge's voice-coil haptics over Bluetooth.
 *
 * Protocol: HID output report 0x32 carrying int8 stereo PCM at 3 kHz, CRC-32
 * seeded with the Bluetooth HID header byte 0xA2. Byte layout per the public
 * reverse-engineering work of SAxense (egormanga, https://apps.sdore.me/SAxense)
 * and DS5Dongle (awalol); this is an independent implementation.
 *
 * Audio path: F32 2ch 48 kHz from PipeWire -> 4th-order Butterworth low-pass
 * at 1.2 kHz -> decimate 16:1 -> gain/clip -> int8 -> one report per 32 frames.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <glob.h>
#include <math.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <pipewire/pipewire.h>
#include <spa/param/audio/format-utils.h>
#include <zlib.h>

#define IN_RATE        48000
#define HAP_RATE       3000
#define DECIM          (IN_RATE / HAP_RATE)   /* 16 */
#define FRAME_SAMPLES  32                     /* stereo frames per block */
#define FRAME_BYTES    (FRAME_SAMPLES * 2)
#define REPORT_SIZE    142
#define STATE_SIZE     78
#define RING_FRAMES    4096
#define IDLE_BLOCKS    47                     /* ~500 ms of silence -> stop sending */
#define LPF_HZ         1200.0f

struct biquad { float b0, b1, b2, a1, a2, z1, z2; };

static void biquad_lowpass(struct biquad *q, float fs, float fc, float Q)
{
	float w0 = 2.0f * (float)M_PI * fc / fs;
	float cw = cosf(w0), sw = sinf(w0), alpha = sw / (2.0f * Q);
	float a0 = 1.0f + alpha;
	q->b0 = ((1.0f - cw) / 2.0f) / a0;
	q->b1 = (1.0f - cw) / a0;
	q->b2 = q->b0;
	q->a1 = (-2.0f * cw) / a0;
	q->a2 = (1.0f - alpha) / a0;
	q->z1 = q->z2 = 0.0f;
}

static inline float biquad_run(struct biquad *q, float x)
{
	float y = q->b0 * x + q->z1;
	q->z1 = q->b1 * x - q->a1 * y + q->z2;
	q->z2 = q->b2 * x - q->a2 * y;
	return y;
}

struct data {
	struct pw_main_loop *loop;
	struct pw_stream *stream;
	struct spa_source *timer;

	atomic_int fd;              /* hidraw fd, -1 when disconnected */
	atomic_int write_failed;    /* set by RT thread, handled by main loop */
	char dev_override[256];
	float gain;
	int lpf;                    /* -1 leave alone, 0 off, 1 on */
	int power_reduction;        /* -1 leave alone, 0..7 */
	int verbose;

	struct biquad lp[2][2];     /* [channel][stage] */
	int decim_phase;
	int8_t ring[RING_FRAMES * 2];
	int ring_len;               /* frames */
	int silent_blocks;
	uint8_t seq, counter;

	long sent, idle_skips, errors, process_calls, dropped;
	long max_write_us;
	time_t last_stats;
};

static uint32_t sony_crc(const uint8_t *buf, size_t n)
{
	static const uint8_t seed = 0xA2;
	uint32_t c = crc32(0L, Z_NULL, 0);
	c = crc32(c, &seed, 1);
	return crc32(c, buf, n);
}

static void put_crc(uint8_t *rep, size_t size)
{
	uint32_t c = sony_crc(rep, size - 4);
	rep[size - 4] = c & 0xff;
	rep[size - 3] = (c >> 8) & 0xff;
	rep[size - 2] = (c >> 16) & 0xff;
	rep[size - 1] = (c >> 24) & 0xff;
}

static void build_haptics_report(struct data *d, uint8_t out[REPORT_SIZE], const int8_t *pcm, int frames)
{
	int dbl = frames == 2 * FRAME_SAMPLES;
	memset(out, 0, REPORT_SIZE);
	out[0] = 0x32;
	out[1] = (uint8_t)(d->seq << 4);
	out[2] = 0x91;            /* sub-packet 0x11, sized */
	out[3] = 4;               /* popcount of out[4] */
	out[4] = 0x66;            /* buffer-length valid, mandatory bit, mic off */
	out[5] = 48;
	out[6] = 48;
	out[7] = d->counter;
	out[8] = dbl ? 0xD2 : 0x92;   /* sub-packet 0x12, bit6 = two frames */
	out[9] = FRAME_BYTES;
	memcpy(out + 10, pcm, (size_t)frames * 2);
	put_crc(out, REPORT_SIZE);
	d->seq = (d->seq + 1) & 0x0F;
	d->counter = (uint8_t)(d->counter + (dbl ? 2 : 1));
}

static void send_state_report(struct data *d, int fd)
{
	uint8_t rep[STATE_SIZE] = {0};
	rep[0] = 0x31;
	rep[1] = (uint8_t)(d->seq << 4);
	rep[2] = 0x10;
	if (d->lpf >= 0) {
		rep[4] |= 0x20;                     /* AllowHapticLowPassFilter */
		rep[42] = d->lpf ? 0x01 : 0x00;
	}
	if (d->power_reduction >= 0) {
		rep[4] |= 0x40;                     /* AllowMotorPowerLevel */
		rep[39] = (uint8_t)(d->power_reduction << 4);
	}
	put_crc(rep, STATE_SIZE);
	d->seq = (d->seq + 1) & 0x0F;
	if (write(fd, rep, STATE_SIZE) != STATE_SIZE)
		fprintf(stderr, "ds5-haptics-sink: state report failed: %s\n", strerror(errno));
}

static void send_haptics(struct data *d, const int8_t *pcm, int frames)
{
	uint8_t rep[REPORT_SIZE];
	int fd = atomic_load(&d->fd);
	if (fd < 0)
		return;
	build_haptics_report(d, rep, pcm, frames);
	struct timespec a, b;
	clock_gettime(CLOCK_MONOTONIC, &a);
	ssize_t n = write(fd, rep, REPORT_SIZE);
	clock_gettime(CLOCK_MONOTONIC, &b);
	long us = (b.tv_sec - a.tv_sec) * 1000000L + (b.tv_nsec - a.tv_nsec) / 1000;
	if (us > d->max_write_us)
		d->max_write_us = us;
	if (n == REPORT_SIZE) {
		d->sent++;
	} else if (n < 0 && errno == EAGAIN) {
		d->dropped++;
	} else {
		d->errors++;
		atomic_store(&d->write_failed, 1);
	}
}

/* Drain whole blocks from the ring: two per report when available, else one. */
static void emit_ready_blocks(struct data *d)
{
	while (d->ring_len >= FRAME_SAMPLES) {
		int frames = d->ring_len >= 2 * FRAME_SAMPLES ? 2 * FRAME_SAMPLES : FRAME_SAMPLES;
		int silent = 1;
		for (int i = 0; i < frames * 2 && silent; i++)
			if (d->ring[i] != 0)
				silent = 0;
		d->silent_blocks = silent ? d->silent_blocks + frames / FRAME_SAMPLES : 0;
		if (d->silent_blocks > IDLE_BLOCKS)
			d->idle_skips++;
		else
			send_haptics(d, d->ring, frames);
		d->ring_len -= frames;
		memmove(d->ring, d->ring + frames * 2, (size_t)d->ring_len * 2);
	}
}

static inline int8_t to_int8(float x)
{
	if (x > 127.0f) x = 127.0f;
	if (x < -127.0f) x = -127.0f;
	return (int8_t)lrintf(x);
}

static void on_process(void *userdata)
{
	struct data *d = userdata;
	struct pw_buffer *b = pw_stream_dequeue_buffer(d->stream);
	if (!b)
		return;
	struct spa_buffer *buf = b->buffer;
	const float *in = buf->datas[0].data;
	if (in) {
		uint32_t n = buf->datas[0].chunk->size / (sizeof(float) * 2);
		float g = d->gain * 127.0f;
		d->process_calls++;
		for (uint32_t i = 0; i < n; i++) {
			float l = biquad_run(&d->lp[0][1], biquad_run(&d->lp[0][0], in[2 * i]));
			float r = biquad_run(&d->lp[1][1], biquad_run(&d->lp[1][0], in[2 * i + 1]));
			if (++d->decim_phase == DECIM) {
				d->decim_phase = 0;
				if (d->ring_len < RING_FRAMES) {
					d->ring[2 * d->ring_len] = to_int8(l * g);
					d->ring[2 * d->ring_len + 1] = to_int8(r * g);
					d->ring_len++;
				}
			}
		}
		emit_ready_blocks(d);
	}
	pw_stream_queue_buffer(d->stream, b);
}

static void on_state_changed(void *userdata, enum pw_stream_state old, enum pw_stream_state state, const char *error)
{
	struct data *d = userdata;
	(void)old;
	if (d->verbose || error)
		fprintf(stderr, "ds5-haptics-sink: stream %s%s%s\n", pw_stream_state_as_string(state),
			error ? ": " : "", error ? error : "");
	if (state == PW_STREAM_STATE_ERROR)
		pw_main_loop_quit(d->loop);
}

static const struct pw_stream_events stream_events = {
	PW_VERSION_STREAM_EVENTS,
	.process = on_process,
	.state_changed = on_state_changed,
};

static int find_hidraw(char *out, size_t n)
{
	static const char *patterns[] = {
		"/sys/bus/hid/devices/0005:054C:0CE6.*/hidraw/hidraw*",
		"/sys/bus/hid/devices/0005:054C:0DF2.*/hidraw/hidraw*",
	};
	for (size_t p = 0; p < sizeof(patterns) / sizeof(patterns[0]); p++) {
		glob_t g;
		int rc = glob(patterns[p], 0, NULL, &g);
		if (rc == 0 && g.gl_pathc > 0) {
			const char *base = strrchr(g.gl_pathv[0], '/');
			snprintf(out, n, "/dev%s", base);
			globfree(&g);
			return 0;
		}
		if (rc == 0)
			globfree(&g);
	}
	return -1;
}

static void print_stats(struct data *d)
{
	fprintf(stderr, "ds5-haptics-sink: sent %ld, idle-skipped %ld, dropped %ld, errors %ld, "
		"process calls %ld, max write %ld us\n",
		d->sent, d->idle_skips, d->dropped, d->errors, d->process_calls, d->max_write_us);
}

/* Runs on the main loop once per second: handles disconnects and reconnects. */
static void on_timer(void *userdata, uint64_t expirations)
{
	struct data *d = userdata;
	(void)expirations;

	if (atomic_load(&d->write_failed)) {
		int fd = atomic_exchange(&d->fd, -1);
		if (fd >= 0)
			close(fd);
		atomic_store(&d->write_failed, 0);
		fprintf(stderr, "ds5-haptics-sink: controller write failed, waiting for it to come back\n");
	}
	if (atomic_load(&d->fd) < 0) {
		char path[256];
		if (d->dev_override[0])
			snprintf(path, sizeof(path), "%s", d->dev_override);
		else if (find_hidraw(path, sizeof(path)) < 0)
			goto stats;
		int fd = open(path, O_WRONLY | O_NONBLOCK | O_CLOEXEC);
		if (fd < 0) {
			if (errno != ENOENT)
				fprintf(stderr, "ds5-haptics-sink: open %s: %s\n", path, strerror(errno));
			goto stats;
		}
		if (d->lpf >= 0 || d->power_reduction >= 0)
			send_state_report(d, fd);
		atomic_store(&d->fd, fd);
		fprintf(stderr, "ds5-haptics-sink: connected to %s\n", path);
	}
stats:
	if (d->verbose) {
		time_t now = time(NULL);
		if (now - d->last_stats >= 60) {
			d->last_stats = now;
			print_stats(d);
		}
	}
}

static void do_quit(void *userdata, int signal_number)
{
	struct data *d = userdata;
	(void)signal_number;
	pw_main_loop_quit(d->loop);
}

static void usage(void)
{
	fprintf(stderr,
		"usage: ds5-haptics-sink [--gain F] [--lpf on|off] [--power-reduction 0-7]\n"
		"                        [--device /dev/hidrawN] [--verbose]\n"
		"PipeWire sink \"DualSense Haptics (BT)\": audio routed to it plays on the\n"
		"controller's haptic actuators over Bluetooth. DS5_HAPTICS_GAIN env sets gain.\n");
}

int main(int argc, char **argv)
{
	struct data d = { .fd = -1, .gain = 1.0f, .lpf = -1, .power_reduction = -1 };
	const char *env_gain = getenv("DS5_HAPTICS_GAIN");
	if (env_gain)
		d.gain = strtof(env_gain, NULL);

	static const struct option opts[] = {
		{ "gain", required_argument, 0, 'g' },
		{ "lpf", required_argument, 0, 'l' },
		{ "power-reduction", required_argument, 0, 'p' },
		{ "device", required_argument, 0, 'd' },
		{ "verbose", no_argument, 0, 'v' },
		{ "help", no_argument, 0, 'h' },
		{ 0, 0, 0, 0 },
	};
	int c;
	while ((c = getopt_long(argc, argv, "g:l:p:d:vh", opts, NULL)) != -1) {
		switch (c) {
		case 'g': d.gain = strtof(optarg, NULL); break;
		case 'l': d.lpf = strcmp(optarg, "on") == 0; break;
		case 'p': d.power_reduction = atoi(optarg) & 7; break;
		case 'd': snprintf(d.dev_override, sizeof(d.dev_override), "%s", optarg); break;
		case 'v': d.verbose = 1; break;
		default: usage(); return c == 'h' ? 0 : 2;
		}
	}

	/* 4th-order Butterworth = two 2nd-order sections with Q = 0.5412 and 1.3066 */
	for (int ch = 0; ch < 2; ch++) {
		biquad_lowpass(&d.lp[ch][0], IN_RATE, LPF_HZ, 0.5412f);
		biquad_lowpass(&d.lp[ch][1], IN_RATE, LPF_HZ, 1.3066f);
	}

	pw_init(&argc, &argv);
	d.loop = pw_main_loop_new(NULL);
	struct pw_loop *loop = pw_main_loop_get_loop(d.loop);
	pw_loop_add_signal(loop, SIGINT, do_quit, &d);
	pw_loop_add_signal(loop, SIGTERM, do_quit, &d);

	d.stream = pw_stream_new_simple(loop, "DualSense Haptics (BT)",
		pw_properties_new(
			PW_KEY_MEDIA_TYPE, "Audio",
			PW_KEY_MEDIA_CATEGORY, "Playback",
			PW_KEY_MEDIA_CLASS, "Audio/Sink",
			PW_KEY_NODE_NAME, "ds5_haptics",
			PW_KEY_NODE_DESCRIPTION, "DualSense Haptics (BT)",
			PW_KEY_NODE_LATENCY, "512/48000",        /* one 10.667 ms block per cycle */
			PW_KEY_DEVICE_ICON_NAME, "input-gaming",
			PW_KEY_AUDIO_CHANNELS, "2",
			"audio.position", "FL,FR",
			NULL),
		&stream_events, &d);

	uint8_t buffer[1024];
	struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
	struct spa_audio_info_raw info = SPA_AUDIO_INFO_RAW_INIT(
		.format = SPA_AUDIO_FORMAT_F32,
		.rate = IN_RATE,
		.channels = 2,
		.position = { SPA_AUDIO_CHANNEL_FL, SPA_AUDIO_CHANNEL_FR });
	const struct spa_pod *params[1];
	params[0] = spa_format_audio_raw_build(&b, SPA_PARAM_EnumFormat, &info);

	if (pw_stream_connect(d.stream, PW_DIRECTION_INPUT, PW_ID_ANY,
			PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS | PW_STREAM_FLAG_RT_PROCESS,
			params, 1) < 0) {
		fprintf(stderr, "ds5-haptics-sink: pw_stream_connect failed\n");
		return 1;
	}

	d.timer = pw_loop_add_timer(loop, on_timer, &d);
	struct timespec first = { 0, 1000000 }, every = { 1, 0 };
	pw_loop_update_timer(loop, d.timer, &first, &every, false);

	fprintf(stderr, "ds5-haptics-sink: sink \"DualSense Haptics (BT)\" up, gain %.2f\n", d.gain);
	pw_main_loop_run(d.loop);

	print_stats(&d);
	int fd = atomic_exchange(&d.fd, -1);
	if (fd >= 0) {
		/* a few silent blocks so the coils settle */
		int8_t zero[FRAME_BYTES] = {0};
		for (int i = 0; i < 4; i++) {
			uint8_t rep[REPORT_SIZE];
			build_haptics_report(&d, rep, zero, FRAME_SAMPLES);
			if (write(fd, rep, REPORT_SIZE) < 0) break;
			usleep(10667);
		}
		close(fd);
	}
	pw_stream_destroy(d.stream);
	pw_main_loop_destroy(d.loop);
	pw_deinit();
	return 0;
}
