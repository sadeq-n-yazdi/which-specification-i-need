LABEL := local.mac-performance-logger
BINARY := $(CURDIR)/bin/mac-performance-logger
CONFIG := $(CURDIR)/logger.conf
LOG_DIRECTORY := $(CURDIR)/logs
OPERATING_SYSTEM := $(shell uname -s)
PLIST := $(HOME)/Library/LaunchAgents/$(LABEL).plist
DOMAIN := gui/$(shell id -u)
UNIT := mac-performance-logger.service
UNIT_FILE := $(HOME)/.config/systemd/user/$(UNIT)

.PHONY: build test sample install uninstall restart status

build:
	@if [ -f main.go ]; then go build -trimpath -ldflags "-s -w" -o $(BINARY) . ; else echo "using prebuilt $(BINARY)"; fi

test:
	go vet ./...
	go test ./...

sample: build
	$(BINARY) -once

ifeq ($(OPERATING_SYSTEM),Darwin)
install: build
	mkdir -p $(LOG_DIRECTORY) $(dir $(PLIST))
	sed -e 's|__LABEL__|$(LABEL)|' -e 's|__BINARY__|$(BINARY)|' -e 's|__CONFIG__|$(CONFIG)|' \
		-e 's|__LOG_DIRECTORY__|$(LOG_DIRECTORY)|' launchd.plist.template > $(PLIST)
	-launchctl bootout $(DOMAIN)/$(LABEL) 2>/dev/null
	@sleep 2
	launchctl bootstrap $(DOMAIN) $(PLIST)
	@echo "Installed. Check with: make status"

uninstall:
	-launchctl bootout $(DOMAIN)/$(LABEL)
	rm -f $(PLIST)

# Re-read logger.conf after editing it.
restart:
	launchctl kickstart -k $(DOMAIN)/$(LABEL)

status:
	@launchctl print $(DOMAIN)/$(LABEL) | grep -E 'state|pid|last exit' || echo "not loaded"
	@tail -n 5 $(LOG_DIRECTORY)/logger.log 2>/dev/null || true
else
# Linux: a systemd user service. Run `loginctl enable-linger $USER` once to keep it running without a login session.
install: build
	mkdir -p $(LOG_DIRECTORY) $(dir $(UNIT_FILE))
	sed -e 's|__BINARY__|$(BINARY)|' -e 's|__CONFIG__|$(CONFIG)|' -e 's|__LOG_DIRECTORY__|$(LOG_DIRECTORY)|' \
		systemd.service.template > $(UNIT_FILE)
	systemctl --user daemon-reload
	systemctl --user enable $(UNIT)
	systemctl --user restart $(UNIT)
	@echo "Installed. Check with: make status"

uninstall:
	-systemctl --user disable --now $(UNIT)
	rm -f $(UNIT_FILE)
	-systemctl --user daemon-reload

# Re-read logger.conf after editing it.
restart:
	systemctl --user restart $(UNIT)

status:
	@systemctl --user show $(UNIT) -p ActiveState -p MainPID -p ExecMainStatus || echo "not loaded"
	@tail -n 5 $(LOG_DIRECTORY)/logger.log 2>/dev/null || true
endif
