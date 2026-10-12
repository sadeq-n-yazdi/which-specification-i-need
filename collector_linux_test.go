package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestParseKeyValuesReadsMeminfoAndVmstat(t *testing.T) {
	values := parseKeyValues([]byte("MemTotal:       16384 kB\nHugePages_Total:       0\npswpin 42\nbad line here\n"))
	if values["MemTotal"] != 16384 || values["pswpin"] != 42 || values["HugePages_Total"] != 0 {
		t.Fatalf("unexpected values: %v", values)
	}
}

// The level decides whether a period is reported as under memory pressure, so the thresholds must stay fixed.
func TestPressureLevelFromStallInformation(t *testing.T) {
	cases := map[string]string{
		"some avg10=0.00 avg60=0.00 avg300=0.00 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=1\n":   "normal",
		"some avg10=12.50 avg60=1.00 avg300=0.00 total=1\nfull avg10=0.50 avg60=0.00 avg300=0.00 total=1\n":  "warning",
		"some avg10=30.00 avg60=1.00 avg300=0.00 total=1\nfull avg10=10.00 avg60=0.00 avg300=0.00 total=1\n": "critical",
		"": "unknown",
	}
	for content, expected := range cases {
		if got := pressureLevel(content); got != expected {
			t.Errorf("pressureLevel(%q) = %s, want %s", content, got, expected)
		}
	}
}

func TestParseProcessStatusSkipsKernelThreadsAndPrefersAnonymousMemory(t *testing.T) {
	user := []byte("Name:\tpostgres\nVmRSS:\t   5000 kB\nRssAnon:\t   3000 kB\nRssFile:\t 2000 kB\n")
	name, used, found := parseProcessStatus(user)
	if !found || name != "postgres" || used != 3000*1024 {
		t.Fatalf("got %q %d %v", name, used, found)
	}
	if _, _, found := parseProcessStatus([]byte("Name:\tkworker/0:1\nState:\tI (idle)\n")); found {
		t.Fatal("kernel thread must be skipped")
	}
	old := []byte("Name:\tolddaemon\nVmRSS:\t 100 kB\n")
	if _, used, found := parseProcessStatus(old); !found || used != 100*1024 {
		t.Fatalf("fallback to VmRSS failed: %d %v", used, found)
	}
}

func TestReadZramUsageSumsDevices(t *testing.T) {
	directory := t.TempDir()
	for name, content := range map[string]string{"zram0": "1000 400 500 0 0 0 0 0\n", "zram1": "2000 800 900 0 0 0 0 0\n"} {
		if err := os.MkdirAll(filepath.Join(directory, name), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(directory, name, "mm_stat"), []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	occupied, stored := readZramUsage(directory)
	if occupied != 1400 || stored != 3000 {
		t.Fatalf("occupied=%d stored=%d", occupied, stored)
	}
}

func TestReadPowerPrefersMainsOverBattery(t *testing.T) {
	directory := t.TempDir()
	write := func(device, file, content string) {
		path := filepath.Join(directory, device)
		if err := os.MkdirAll(path, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(path, file), []byte(content+"\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("BAT0", "type", "Battery")
	write("BAT0", "capacity", "81")
	write("AC", "type", "Mains")
	write("AC", "online", "0")
	if source, percent := readPower(directory); source != "battery" || percent != 81 {
		t.Fatalf("got %s %d", source, percent)
	}
	write("AC", "online", "1")
	if source, _ := readPower(directory); source != "ac" {
		t.Fatalf("got %s", source)
	}
	if source, percent := readPower(t.TempDir()); source != "unknown" || percent != -1 {
		t.Fatalf("no power supply: got %s %d", source, percent)
	}
}
