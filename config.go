package main

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const minimumInterval = 5 * time.Second

var timeLayouts = []string{"2006-01-02 15:04", "2006-01-02 15:04:05", time.RFC3339}

type config struct {
	start        time.Time
	stop         time.Time
	interval     time.Duration
	logDirectory string
	topProcesses int
}

// loadConfig parses a "key = value" file; blank lines and lines starting with '#' are ignored.
func loadConfig(path string) (config, error) {
	file, err := os.Open(path)
	if err != nil {
		return config{}, err
	}
	defer file.Close()

	values := map[string]string{}
	scanner := bufio.NewScanner(file)
	for lineNumber := 1; scanner.Scan(); lineNumber++ {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, found := strings.Cut(line, "=")
		if !found {
			return config{}, fmt.Errorf("%s:%d: expected 'key = value'", path, lineNumber)
		}
		values[strings.TrimSpace(key)] = strings.TrimSpace(value)
	}
	if err := scanner.Err(); err != nil {
		return config{}, err
	}
	return parseConfig(values)
}

func parseConfig(values map[string]string) (config, error) {
	result := config{interval: time.Minute, logDirectory: "logs", topProcesses: 10}
	var err error

	for key, value := range values {
		switch key {
		case "start":
			result.start, err = parseTime(value)
		case "stop":
			result.stop, err = parseTime(value)
		case "interval":
			result.interval, err = time.ParseDuration(value)
		case "log_dir":
			result.logDirectory, err = expandHome(value)
		case "top_processes":
			result.topProcesses, err = strconv.Atoi(value)
		default:
			err = fmt.Errorf("unknown key")
		}
		if err != nil {
			return config{}, fmt.Errorf("%s: %w", key, err)
		}
	}

	switch {
	case result.start.IsZero() || result.stop.IsZero():
		return config{}, fmt.Errorf("both start and stop are required")
	case !result.stop.After(result.start):
		return config{}, fmt.Errorf("stop must be after start")
	case result.interval < minimumInterval:
		return config{}, fmt.Errorf("interval must be at least %s", minimumInterval)
	case result.topProcesses < 0:
		return config{}, fmt.Errorf("top_processes must not be negative")
	}
	return result, nil
}

func parseTime(value string) (time.Time, error) {
	for _, layout := range timeLayouts {
		if parsed, err := time.ParseInLocation(layout, value, time.Local); err == nil {
			return parsed, nil
		}
	}
	return time.Time{}, fmt.Errorf("cannot parse %q, use YYYY-MM-DD HH:MM", value)
}

func expandHome(path string) (string, error) {
	if path != "~" && !strings.HasPrefix(path, "~/") {
		return path, nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, strings.TrimPrefix(path, "~")), nil
}
