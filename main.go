// mac-performance-logger samples macOS memory pressure, swap activity and the largest applications at a fixed
// interval between a configured start and stop time, writing daily CSV files.
package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

const (
	systemHeader = "timestamp,pressure,physical_mb,free_mb,active_mb,inactive_mb,wired_mb,app_mb,cached_files_mb," +
		"compressor_occupied_mb,compressor_stored_mb,swap_total_mb,swap_used_mb,load_average_1m,uptime_seconds,power_source,battery_percent," +
		"swapins,swapouts,pageins,pageouts,compressions,decompressions"
	processHeader   = "timestamp,rank,name,process_count,memory_mb"
	timestampLayout = "2006-01-02T15:04:05"
	dateLayout      = "2006-01-02"
	// Upper bound for one sleep, so the wall clock is re-checked after the laptop wakes up.
	maximumSleep = time.Minute
)

func main() {
	configPath := flag.String("config", "logger.conf", "path to the configuration file")
	once := flag.Bool("once", false, "print one sample to stdout and exit, ignoring start and stop")
	flag.Parse()
	log.SetFlags(log.LstdFlags)

	collector, err := newCollector()
	if err != nil {
		log.Fatal(err)
	}
	if *once {
		printOnce(collector)
		return
	}

	settings, err := loadConfig(*configPath)
	if err != nil {
		log.Fatalf("config: %v", err)
	}
	if err := os.MkdirAll(settings.logDirectory, 0o755); err != nil {
		log.Fatal(err)
	}

	stopSignal := make(chan os.Signal, 1)
	signal.Notify(stopSignal, syscall.SIGTERM, syscall.SIGINT)
	if err := run(settings, collector, stopSignal); err != nil {
		log.Fatal(err)
	}
}

func run(settings config, collector *collector, stopSignal <-chan os.Signal) error {
	events := &eventLog{directory: settings.logDirectory}
	schedule := fmt.Sprintf("interval=%s start=%s stop=%s", settings.interval,
		settings.start.Format(timestampLayout), settings.stop.Format(timestampLayout))
	if time.Now().After(settings.stop) {
		events.record("stop", "stop time already passed; "+schedule)
		return nil
	}
	events.record("start", schedule)
	log.Printf("logging %s into %s", schedule, settings.logDirectory)

	systemLog := &dailyLogWriter{directory: settings.logDirectory, prefix: "system", header: systemHeader}
	processLog := &dailyLogWriter{directory: settings.logDirectory, prefix: "processes", header: processHeader}
	defer systemLog.close()
	defer processLog.close()

	var previous *vmCounters
	var lastSampleTime time.Time
	lastPowerSource := ""
	_, lastWake := lastSleep()
	if sleep, wake := lastSleep(); !wake.IsZero() {
		events.record("last_sleep_before_start", sleepDetails(sleep, wake))
	}
	nextSample := settings.start
	for {
		now := time.Now()
		if now.After(settings.stop) {
			events.record("stop", "stop time reached")
			return nil
		}
		if now.Before(nextSample) {
			select {
			case received := <-stopSignal:
				events.record("stop", "signal "+received.String())
				return nil
			case <-time.After(min(nextSample.Sub(now), maximumSleep)):
			}
			continue
		}

		sample, err := collector.system()
		if err != nil {
			events.record("error", "sample skipped: "+err.Error())
		} else {
			// A missing stretch of samples (sleep, freeze, suspended process) is recorded so it is not mistaken
			// for a quiet period.
			if !lastSampleTime.IsZero() && sample.timestamp.Sub(lastSampleTime) > 2*settings.interval {
				events.record("gap", fmt.Sprintf("no samples for %s", sample.timestamp.Sub(lastSampleTime).Round(time.Second)))
			}
			lastSampleTime = sample.timestamp
			// The kernel keeps only the latest sleep; several short sleeps within one interval appear as the last one.
			if sleep, wake := lastSleep(); wake.After(lastWake) {
				events.record("sleep", sleepDetails(sleep, wake))
				lastWake = wake
			}
			if sample.powerSource != lastPowerSource {
				events.record("power", fmt.Sprintf("power source %s, battery %s%%", sample.powerSource,
					batteryField(sample.batteryPercent)))
				lastPowerSource = sample.powerSource
			}
			date := sample.timestamp.Format(dateLayout)
			if err := systemLog.write(date, formatSystem(sample, previous)); err != nil {
				events.record("error", err.Error())
				return err
			}
			for rank, group := range collector.topProcesses(settings.topProcesses) {
				line := fmt.Sprintf("%s,%d,%s,%d,%d", sample.timestamp.Format(timestampLayout), rank+1,
					csvField(group.name), group.count, group.memoryMB)
				if err := processLog.write(date, line); err != nil {
					events.record("error", err.Error())
					return err
				}
			}
			previous = &sample.counters
		}

		// Skip slots missed while the laptop was asleep instead of sampling repeatedly to catch up.
		for !nextSample.After(time.Now()) {
			nextSample = nextSample.Add(settings.interval)
		}
	}
}

// formatSystem writes counter columns as the change since the previous sample; they stay empty for the first one.
func formatSystem(sample systemSample, previous *vmCounters) string {
	deltas := ",,,,,"
	if previous != nil {
		current := sample.counters
		deltas = fmt.Sprintf("%d,%d,%d,%d,%d,%d", current.swapins-previous.swapins,
			current.swapouts-previous.swapouts, current.pageins-previous.pageins, current.pageouts-previous.pageouts,
			current.compressions-previous.compressions, current.decompressions-previous.decompressions)
	}
	return fmt.Sprintf("%s,%s,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%.2f,%d,%s,%s,%s",
		sample.timestamp.Format(timestampLayout), sample.pressureLevel, sample.physicalMB, sample.freeMB,
		sample.activeMB, sample.inactiveMB, sample.wiredMB, sample.appMB, sample.cachedFilesMB,
		sample.compressorOccupiedMB, sample.compressorStoredMB, sample.swapTotalMB, sample.swapUsedMB,
		sample.loadAverage, int64(sample.uptime.Seconds()), sample.powerSource,
		batteryField(sample.batteryPercent), deltas)
}

func sleepDetails(sleep time.Time, wake time.Time) string {
	return fmt.Sprintf("slept %s, woke %s, duration %s", sleep.Format(timestampLayout), wake.Format(timestampLayout),
		wake.Sub(sleep).Round(time.Second))
}

func batteryField(percent int) string {
	if percent < 0 {
		return ""
	}
	return fmt.Sprint(percent)
}

func csvField(value string) string {
	if !strings.ContainsAny(value, ",\"\n") {
		return value
	}
	return `"` + strings.ReplaceAll(value, `"`, `""`) + `"`
}

func printOnce(collector *collector) {
	sample, err := collector.system()
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println(systemHeader)
	fmt.Println(formatSystem(sample, nil))
	fmt.Println()
	fmt.Println(processHeader)
	for rank, group := range collector.topProcesses(15) {
		fmt.Printf("%s,%d,%s,%d,%d\n", sample.timestamp.Format(timestampLayout), rank+1, csvField(group.name),
			group.count, group.memoryMB)
	}
}
