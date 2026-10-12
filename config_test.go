package main

import (
	"strings"
	"testing"
	"time"
)

func validValues() map[string]string {
	return map[string]string{"start": "2026-09-28 09:00", "stop": "2026-10-10 19:00", "interval": "60s"}
}

// A typo in the config must fail at startup; otherwise the logger silently runs with defaults for two weeks.
func TestParseConfigRejectsUnknownKey(t *testing.T) {
	values := validValues()
	values["intervall"] = "30s"
	if _, err := parseConfig(values); err == nil || !strings.Contains(err.Error(), "intervall") {
		t.Fatalf("expected error naming the unknown key, got %v", err)
	}
}

func TestParseConfigUsesLocalTimeAndDefaults(t *testing.T) {
	settings, err := parseConfig(validValues())
	if err != nil {
		t.Fatal(err)
	}
	expectedStart := time.Date(2026, 9, 28, 9, 0, 0, 0, time.Local)
	if !settings.start.Equal(expectedStart) {
		t.Errorf("start = %v, want %v (times are wall-clock times of this laptop)", settings.start, expectedStart)
	}
	if settings.interval != time.Minute || settings.topProcesses != 10 || settings.logDirectory != "logs" {
		t.Errorf("unexpected defaults: %+v", settings)
	}
}

func TestParseConfigRejectsInvalidSchedules(t *testing.T) {
	cases := map[string]map[string]string{
		"stop before start":  {"stop": "2026-09-01 09:00"},
		"missing stop":       {"stop": ""},
		"too short interval": {"interval": "1s"},
		"bad time format":    {"start": "28/09/2026"},
	}
	for name, overrides := range cases {
		values := validValues()
		for key, value := range overrides {
			if value == "" {
				delete(values, key)
			} else {
				values[key] = value
			}
		}
		if _, err := parseConfig(values); err == nil {
			t.Errorf("%s: expected an error", name)
		}
	}
}

// Counters are cumulative since boot; the report needs swap activity per interval, not a growing total.
func TestFormatSystemWritesCounterDeltas(t *testing.T) {
	sample := systemSample{timestamp: time.Date(2026, 9, 28, 9, 0, 0, 0, time.Local), pressureLevel: "warning",
		counters: vmCounters{swapins: 150, swapouts: 220, pageins: 10, pageouts: 5, compressions: 40, decompressions: 30}}
	previous := &vmCounters{swapins: 100, swapouts: 200, pageins: 10, pageouts: 1, compressions: 20, decompressions: 25}

	if line := formatSystem(sample, previous); !strings.HasSuffix(line, ",50,20,0,4,20,5") {
		t.Errorf("unexpected deltas in %q", line)
	}
	if line := formatSystem(sample, nil); !strings.HasSuffix(line, ",,,,,,") {
		t.Errorf("first sample must leave deltas empty, got %q", line)
	}
}

func TestCsvFieldQuotesSeparators(t *testing.T) {
	if got := csvField(`Helper, "beta"`); got != `"Helper, ""beta"""` {
		t.Errorf("got %s", got)
	}
}
