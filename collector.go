package main

import "time"

const megabyte = 1024 * 1024

// vmCounters holds the cumulative kernel counters that are reported as per-interval deltas.
type vmCounters struct {
	swapins, swapouts, pageins, pageouts, compressions, decompressions uint64
}

type systemSample struct {
	timestamp                                time.Time
	pressureLevel                            string
	physicalMB, freeMB, activeMB, inactiveMB uint64
	wiredMB, appMB, cachedFilesMB            uint64
	compressorOccupiedMB, compressorStoredMB uint64
	swapTotalMB, swapUsedMB                  uint64
	loadAverage                              float64
	uptime                                   time.Duration
	powerSource                              string
	batteryPercent                           int
	counters                                 vmCounters
}

type processGroup struct {
	name     string
	count    int
	memoryMB uint64
}
