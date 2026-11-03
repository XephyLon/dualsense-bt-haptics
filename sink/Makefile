CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -std=gnu17
PKGS     = libpipewire-0.3 zlib
CFLAGS  += $(shell pkg-config --cflags $(PKGS))
LDLIBS   = $(shell pkg-config --libs $(PKGS)) -lm

TARGET   = ds5-haptics-sink
PREFIX  ?= $(HOME)/.local
UNIT_DIR = $(HOME)/.config/systemd/user

$(TARGET): ds5_haptics_sink.c
	$(CC) $(CFLAGS) -o $@ $< $(LDLIBS)

install: $(TARGET)
	install -Dm755 $(TARGET) $(PREFIX)/bin/$(TARGET)

service: install
	install -Dm644 ../systemd/ds5-haptics-sink.service $(UNIT_DIR)/ds5-haptics-sink.service
	systemctl --user daemon-reload
	systemctl --user enable --now ds5-haptics-sink.service

service-uninstall:
	-systemctl --user disable --now ds5-haptics-sink.service
	rm -f $(UNIT_DIR)/ds5-haptics-sink.service
	systemctl --user daemon-reload

clean:
	rm -f $(TARGET)

.PHONY: install service service-uninstall clean
