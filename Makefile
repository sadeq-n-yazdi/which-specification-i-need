# Works with GNU make 3.81 or newer (the version shipped with macOS) on macOS and Linux.
LABEL := local.mac-performance-logger
BINARY := $(CURDIR)/bin/mac-performance-logger
CONFIG := $(CURDIR)/logger.conf
LOG_DIRECTORY := $(CURDIR)/logs
OPERATING_SYSTEM := $(shell uname -s)
PLIST := $(HOME)/Library/LaunchAgents/$(LABEL).plist
DOMAIN := gui/$(shell id -u)
UNIT := mac-performance-logger.service
UNIT_FILE := $(HOME)/.config/systemd/user/$(UNIT)

.PHONY: build test sample install uninstall restart status check-config

# Compiles from source when the source is present; a downloaded release bundle only contains the binary.
build:
	@if [ -f main.go ]; then \
		command -v go >/dev/null 2>&1 || { echo "Go is not installed (https://go.dev/dl/); needed to build from source"; exit 1; }; \
		go build -trimpath -ldflags "-s -w" -o "$(BINARY)" . ; \
	elif [ -x "$(BINARY)" ]; then \
		echo "using prebuilt $(BINARY)"; \
	else \
		echo "no source (main.go) and no binary at $(BINARY)"; exit 1; \
	fi

test:
	go vet ./...
	go test ./...

sample: build
	"$(BINARY)" -once

check-config:
	@[ -f "$(CONFIG)" ] || { echo "$(CONFIG) is missing: run 'cp logger.conf.sample logger.conf' and edit it first"; exit 1; }

ifeq ($(OPERATING_SYSTEM),Darwin)
install: build check-config
	mkdir -p "$(LOG_DIRECTORY)" "$(dir $(PLIST))"
	sed -e 's|__LABEL__|$(LABEL)|' -e 's|__BINARY__|$(BINARY)|' -e 's|__CONFIG__|$(CONFIG)|' \
		-e 's|__LOG_DIRECTORY__|$(LOG_DIRECTORY)|' launchd.plist.template > "$(PLIST)"
	-launchctl bootout $(DOMAIN)/$(LABEL) 2>/dev/null
	@sleep 2
	launchctl bootstrap $(DOMAIN) "$(PLIST)"
	@echo "Installed. Check with: make status"

uninstall:
	-launchctl bootout $(DOMAIN)/$(LABEL)
	rm -f "$(PLIST)"

# Re-read logger.conf after editing it.
restart:
	launchctl kickstart -k $(DOMAIN)/$(LABEL)

status:
	@launchctl print $(DOMAIN)/$(LABEL) | grep -E 'state|pid|last exit' || echo "not loaded"
	@tail -n 5 "$(LOG_DIRECTORY)/logger.log" 2>/dev/null || true
else ifeq ($(OPERATING_SYSTEM),Linux)
# A systemd user service. On a server run `loginctl enable-linger $USER` once to keep it running without a login.
install: build check-config
	@command -v systemctl >/dev/null 2>&1 || { echo "systemctl not found: this Linux has no systemd; run bin/mac-performance-logger -config logger.conf from your own init system"; exit 1; }
	mkdir -p "$(LOG_DIRECTORY)" "$(dir $(UNIT_FILE))"
	sed -e 's|__BINARY__|$(BINARY)|' -e 's|__CONFIG__|$(CONFIG)|' -e 's|__LOG_DIRECTORY__|$(LOG_DIRECTORY)|' \
		systemd.service.template > "$(UNIT_FILE)"
	systemctl --user daemon-reload
	systemctl --user enable $(UNIT)
	systemctl --user restart $(UNIT)
	@echo "Installed. Check with: make status"

uninstall:
	-systemctl --user disable --now $(UNIT)
	rm -f "$(UNIT_FILE)"
	-systemctl --user daemon-reload

# Re-read logger.conf after editing it.
restart:
	systemctl --user restart $(UNIT)

status:
	@systemctl --user show $(UNIT) -p ActiveState -p MainPID -p ExecMainStatus || echo "not loaded"
	@tail -n 5 "$(LOG_DIRECTORY)/logger.log" 2>/dev/null || true
else
install uninstall restart status:
	@echo "Unsupported operating system $(OPERATING_SYSTEM): only macOS and Linux are supported"; exit 1
endif
