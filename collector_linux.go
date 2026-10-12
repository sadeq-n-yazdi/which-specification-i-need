package main

import (
	"bufio"
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Level of the 10 second "some" (or "full") stall average in /proc/pressure/memory, in percent, from which the
// pressure level is reported as warning (some) or critical (full). Linux has no equivalent of the macOS level.
const pressureStallThreshold = 10.0

const kilobyte = 1024

type collector struct {
	pageSize uint64
}

func newCollector() (*collector, error) {
	if _, err := os.Stat("/proc/meminfo"); err != nil {
		return nil, errors.New("cannot read /proc/meminfo")
	}
	return &collector{pageSize: uint64(os.Getpagesize())}, nil
}

func (c *collector) system() (systemSample, error) {
	meminfo, err := readKeyValues("/proc/meminfo")
	if err != nil {
		return systemSample{}, err
	}
	vmstat, err := readKeyValues("/proc/vmstat")
	if err != nil {
		return systemSample{}, err
	}
	// meminfo values are in kB.
	megabytes := func(name string) uint64 { return meminfo[name] * kilobyte / megabyte }

	loadAverage, err := readLoadAverage("/proc/loadavg")
	if err != nil {
		return systemSample{}, err
	}
	compressorOccupied, compressorStored := readZramUsage("/sys/block")
	powerSource, batteryPercent := readPower("/sys/class/power_supply")

	return systemSample{
		// Round(0) drops the monotonic clock reading, which pauses during suspend and would hide sleep gaps.
		timestamp:     time.Now().Round(0),
		pressureLevel: readPressureLevel("/proc/pressure/memory"),
		physicalMB:    megabytes("MemTotal"),
		freeMB:        megabytes("MemFree"),
		activeMB:      megabytes("Active"),
		inactiveMB:    megabytes("Inactive"),
		// Memory the kernel itself holds and cannot give back (the closest match to macOS "wired").
		wiredMB:              (meminfo["SUnreclaim"] + meminfo["KernelStack"] + meminfo["PageTables"]) * kilobyte / megabyte,
		appMB:                megabytes("AnonPages"),
		cachedFilesMB:        (meminfo["Cached"] + meminfo["Buffers"]) * kilobyte / megabyte,
		compressorOccupiedMB: compressorOccupied / megabyte,
		compressorStoredMB:   compressorStored / megabyte,
		swapTotalMB:          megabytes("SwapTotal"),
		swapUsedMB:           (meminfo["SwapTotal"] - meminfo["SwapFree"]) * kilobyte / megabyte,
		loadAverage:          loadAverage,
		uptime:               time.Since(bootTime()).Truncate(time.Second),
		powerSource:          powerSource,
		batteryPercent:       batteryPercent,
		counters: vmCounters{
			swapins:  vmstat["pswpin"],
			swapouts: vmstat["pswpout"],
			// pgpgin and pgpgout count kB moved from and to block devices; the macOS columns count pages.
			pageins:  vmstat["pgpgin"] * kilobyte / c.pageSize,
			pageouts: vmstat["pgpgout"] * kilobyte / c.pageSize,
			// zswap counters; zero when zswap is not in use.
			compressions:   vmstat["zswpout"],
			decompressions: vmstat["zswpin"],
		},
	}, nil
}

// readKeyValues parses "Name: 123 kB" (meminfo) and "name 123" (vmstat) lines into name -> number.
func readKeyValues(path string) (map[string]uint64, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return parseKeyValues(data), nil
}

func parseKeyValues(data []byte) map[string]uint64 {
	values := map[string]uint64{}
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(strings.Replace(line, ":", " ", 1))
		if len(fields) < 2 {
			continue
		}
		if number, err := strconv.ParseUint(fields[1], 10, 64); err == nil {
			values[fields[0]] = number
		}
	}
	return values
}

func readLoadAverage(path string) (float64, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	fields := strings.Fields(string(data))
	if len(fields) == 0 {
		return 0, errors.New("empty " + path)
	}
	return strconv.ParseFloat(fields[0], 64)
}

// readPressureLevel maps the kernel's pressure stall information to "normal", "warning" or "critical", and returns
// "unknown" when the kernel has no PSI (before Linux 4.20, or booted with psi=0).
func readPressureLevel(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return "unknown"
	}
	return pressureLevel(string(data))
}

func pressureLevel(content string) string {
	some, full := -1.0, -1.0
	for _, line := range strings.Split(content, "\n") {
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		for _, field := range fields[1:] {
			if value, found := strings.CutPrefix(field, "avg10="); found {
				average, err := strconv.ParseFloat(value, 64)
				if err != nil {
					continue
				}
				switch fields[0] {
				case "some":
					some = average
				case "full":
					full = average
				}
			}
		}
	}
	switch {
	case some < 0 && full < 0:
		return "unknown"
	case full >= pressureStallThreshold:
		return "critical"
	case some >= pressureStallThreshold:
		return "warning"
	}
	return "normal"
}

// readZramUsage sums zram devices, the compressed swap of many distributions: it returns the RAM they occupy and
// the amount of data they store (both in bytes). Both are zero when there is no zram.
func readZramUsage(blockDirectory string) (occupied uint64, stored uint64) {
	paths, _ := filepath.Glob(filepath.Join(blockDirectory, "zram*", "mm_stat"))
	for _, path := range paths {
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		// Columns: orig_data_size compr_data_size mem_used_total ...
		fields := strings.Fields(string(data))
		if len(fields) < 3 {
			continue
		}
		original, errOriginal := strconv.ParseUint(fields[0], 10, 64)
		used, errUsed := strconv.ParseUint(fields[2], 10, 64)
		if errOriginal == nil && errUsed == nil {
			stored += original
			occupied += used
		}
	}
	return occupied, stored
}

// topProcesses groups processes by name and returns the largest groups by anonymous memory (heap and stack, which is
// the closest cheap match to the macOS physical footprint; mapped files and shared libraries are not counted).
// Kernel threads have no user memory and are skipped.
func (c *collector) topProcesses(limit int) []processGroup {
	directories, err := filepath.Glob("/proc/[0-9]*/status")
	if err != nil {
		return nil
	}
	groups := map[string]*processGroup{}
	for _, path := range directories {
		data, err := os.ReadFile(path)
		if err != nil {
			continue // the process ended
		}
		name, memory, found := parseProcessStatus(data)
		if !found {
			continue
		}
		group, exists := groups[name]
		if !exists {
			group = &processGroup{name: name}
			groups[name] = group
		}
		group.count++
		group.memoryMB += memory
	}

	result := make([]processGroup, 0, len(groups))
	for _, group := range groups {
		group.memoryMB /= megabyte
		result = append(result, *group)
	}
	sort.Slice(result, func(i, j int) bool { return result[i].memoryMB > result[j].memoryMB })
	return result[:min(limit, len(result))]
}

// parseProcessStatus returns the process name and its memory in bytes from /proc/<pid>/status. found is false for
// kernel threads, which have no memory lines.
func parseProcessStatus(data []byte) (name string, bytesUsed uint64, found bool) {
	var anonymousKB, residentKB uint64
	var hasAnonymous, hasResident bool
	scanner := bufio.NewScanner(bytes.NewReader(data))
	for scanner.Scan() {
		key, value, ok := strings.Cut(scanner.Text(), ":")
		if !ok {
			continue
		}
		fields := strings.Fields(value)
		switch key {
		case "Name":
			name = strings.Join(fields, " ")
		case "RssAnon":
			anonymousKB, hasAnonymous = parseFirstNumber(fields)
		case "VmRSS":
			residentKB, hasResident = parseFirstNumber(fields)
		}
	}
	switch {
	case name == "":
		return "", 0, false
	case hasAnonymous:
		return name, anonymousKB * kilobyte, true
	case hasResident: // kernels before 4.5 have no RssAnon
		return name, residentKB * kilobyte, true
	}
	return "", 0, false
}

func parseFirstNumber(fields []string) (uint64, bool) {
	if len(fields) == 0 {
		return 0, false
	}
	number, err := strconv.ParseUint(fields[0], 10, 64)
	return number, err == nil
}

// bootTime is re-read on each call: a changed value between samples proves the machine was rebooted.
func bootTime() time.Time {
	data, err := os.ReadFile("/proc/stat")
	if err != nil {
		return time.Time{}
	}
	for _, line := range strings.Split(string(data), "\n") {
		if value, found := strings.CutPrefix(line, "btime "); found {
			if seconds, err := strconv.ParseInt(strings.TrimSpace(value), 10, 64); err == nil {
				return time.Unix(seconds, 0)
			}
		}
	}
	return time.Time{}
}

// lastSleep has no Linux equivalent that works without root; sleep periods still show up as "gap" events.
func lastSleep() (sleep time.Time, wake time.Time) {
	return time.Time{}, time.Time{}
}

// readPower returns "battery", "ac" or "unknown" and the battery charge in percent (-1 when there is no battery).
func readPower(directory string) (string, int) {
	entries, _ := filepath.Glob(filepath.Join(directory, "*"))
	mainsOnline := false
	hasBattery := false
	percent := -1
	for _, entry := range entries {
		kind := readTrimmed(filepath.Join(entry, "type"))
		switch kind {
		case "Mains", "USB":
			if readTrimmed(filepath.Join(entry, "online")) == "1" {
				mainsOnline = true
			}
		case "Battery":
			if readTrimmed(filepath.Join(entry, "scope")) == "Device" {
				continue // a peripheral such as a wireless mouse
			}
			hasBattery = true
			if capacity, err := strconv.Atoi(readTrimmed(filepath.Join(entry, "capacity"))); err == nil && percent < 0 {
				percent = capacity
			}
		}
	}
	switch {
	case mainsOnline:
		return "ac", percent
	case hasBattery:
		return "battery", percent
	}
	return "unknown", percent
}

func readTrimmed(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}
