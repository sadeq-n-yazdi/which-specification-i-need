package main

/*
#cgo LDFLAGS: -framework IOKit -framework CoreFoundation
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/ps/IOPowerSources.h>
#include <IOKit/ps/IOPSKeys.h>
#include <mach/mach.h>
#include <mach/mach_host.h>
#include <sys/sysctl.h>
#include <sys/resource.h>
#include <libproc.h>
#include <stdint.h>
#include <stdlib.h>

static mach_port_t host_port = MACH_PORT_NULL;

static int read_vm_statistics(vm_statistics64_data_t *out) {
	if (host_port == MACH_PORT_NULL) {
		host_port = mach_host_self();
	}
	mach_msg_type_number_t count = HOST_VM_INFO64_COUNT;
	return host_statistics64(host_port, HOST_VM_INFO64, (host_info64_t)out, &count);
}

static int read_swap_usage(struct xsw_usage *out) {
	size_t size = sizeof(*out);
	return sysctlbyname("vm.swapusage", out, &size, NULL, 0);
}

// Reads an integer sysctl that the kernel may expose as 4 or 8 bytes.
static int read_integer_sysctl(const char *name, int64_t *out) {
	int64_t value = 0;
	size_t size = sizeof(value);
	if (sysctlbyname(name, &value, &size, NULL, 0) != 0) {
		return -1;
	}
	if (size == sizeof(int32_t)) {
		value = (int64_t)(int32_t)value;
	}
	*out = value;
	return 0;
}

// Returns the physical footprint (the "Memory" column of Activity Monitor), falling back to the resident size.
static int read_process_memory(int pid, uint64_t *out) {
	struct rusage_info_v2 usage;
	if (proc_pid_rusage(pid, RUSAGE_INFO_V2, (rusage_info_t *)&usage) == 0) {
		*out = usage.ri_phys_footprint;
		return 0;
	}
	struct proc_taskinfo task;
	if (proc_pidinfo(pid, PROC_PIDTASKINFO, 0, &task, sizeof(task)) == sizeof(task)) {
		*out = task.pti_resident_size;
		return 0;
	}
	return -1;
}

// Reads a timeval sysctl such as kern.boottime, kern.sleeptime or kern.waketime.
static int read_time_sysctl(const char *name, int64_t *seconds) {
	struct timeval value;
	size_t size = sizeof(value);
	if (sysctlbyname(name, &value, &size, NULL, 0) != 0) {
		return -1;
	}
	*seconds = value.tv_sec;
	return 0;
}

// Sets *on_battery to 1 on battery, 0 on AC power, and *percent to the internal battery charge (-1 if unknown).
static int read_power(int *on_battery, int *percent) {
	*on_battery = -1;
	*percent = -1;
	CFTypeRef snapshot = IOPSCopyPowerSourcesInfo();
	if (snapshot == NULL) {
		return -1;
	}
	CFStringRef source_type = IOPSGetProvidingPowerSourceType(snapshot);
	if (source_type != NULL) {
		*on_battery = CFStringCompare(source_type, CFSTR(kIOPMBatteryPowerKey), 0) == kCFCompareEqualTo;
	}
	CFArrayRef sources = IOPSCopyPowerSourcesList(snapshot);
	if (sources != NULL) {
		for (CFIndex i = 0; i < CFArrayGetCount(sources); i++) {
			CFDictionaryRef description = IOPSGetPowerSourceDescription(snapshot, CFArrayGetValueAtIndex(sources, i));
			if (description == NULL) {
				continue;
			}
			CFNumberRef current = CFDictionaryGetValue(description, CFSTR(kIOPSCurrentCapacityKey));
			CFNumberRef maximum = CFDictionaryGetValue(description, CFSTR(kIOPSMaxCapacityKey));
			int current_value = 0, maximum_value = 0;
			if (current != NULL && maximum != NULL && CFNumberGetValue(current, kCFNumberIntType, &current_value) &&
			    CFNumberGetValue(maximum, kCFNumberIntType, &maximum_value) && maximum_value > 0) {
				*percent = current_value * 100 / maximum_value;
				break;
			}
		}
		CFRelease(sources);
	}
	CFRelease(snapshot);
	return 0;
}
*/
import "C"

import (
	"errors"
	"sort"
	"time"
	"unsafe"
)

type collector struct {
	pageSize       uint64
	physicalBytes  uint64
	pidBuffer      []C.int
	nameBuffer     *C.char
	nameBufferSize C.uint32_t
}

func newCollector() (*collector, error) {
	var pageSize, physicalBytes C.int64_t
	if C.read_integer_sysctl(C.CString("hw.pagesize"), &pageSize) != 0 {
		return nil, errors.New("cannot read hw.pagesize")
	}
	if C.read_integer_sysctl(C.CString("hw.memsize"), &physicalBytes) != 0 {
		return nil, errors.New("cannot read hw.memsize")
	}
	nameBufferSize := C.uint32_t(C.PROC_PIDPATHINFO_MAXSIZE)
	return &collector{
		pageSize:       uint64(pageSize),
		physicalBytes:  uint64(physicalBytes),
		nameBuffer:     (*C.char)(C.malloc(C.size_t(nameBufferSize))),
		nameBufferSize: nameBufferSize,
	}, nil
}

func (c *collector) pagesToMB(pages C.natural_t) uint64 {
	return uint64(pages) * c.pageSize / megabyte
}

func (c *collector) system() (systemSample, error) {
	var statistics C.vm_statistics64_data_t
	if C.read_vm_statistics(&statistics) != C.KERN_SUCCESS {
		return systemSample{}, errors.New("host_statistics64 failed")
	}
	var swap C.struct_xsw_usage
	if C.read_swap_usage(&swap) != 0 {
		return systemSample{}, errors.New("cannot read vm.swapusage")
	}
	var pressure C.int64_t
	C.read_integer_sysctl(pressureSysctlName, &pressure)
	var loadAverages [1]C.double
	C.getloadavg(&loadAverages[0], 1)

	powerSource, batteryPercent := readPower()
	appPages := statistics.internal_page_count - statistics.purgeable_count
	return systemSample{
		// Round(0) drops the monotonic clock reading, which pauses during sleep and would hide sleep gaps.
		timestamp:            time.Now().Round(0),
		pressureLevel:        pressureName(int64(pressure)),
		physicalMB:           c.physicalBytes / megabyte,
		freeMB:               c.pagesToMB(statistics.free_count),
		activeMB:             c.pagesToMB(statistics.active_count),
		inactiveMB:           c.pagesToMB(statistics.inactive_count),
		wiredMB:              c.pagesToMB(statistics.wire_count),
		appMB:                c.pagesToMB(appPages),
		cachedFilesMB:        c.pagesToMB(statistics.external_page_count),
		compressorOccupiedMB: c.pagesToMB(statistics.compressor_page_count),
		compressorStoredMB:   uint64(statistics.total_uncompressed_pages_in_compressor) * c.pageSize / megabyte,
		swapTotalMB:          uint64(swap.xsu_total) / megabyte,
		swapUsedMB:           uint64(swap.xsu_used) / megabyte,
		loadAverage:          float64(loadAverages[0]),
		uptime:               time.Since(bootTime()).Truncate(time.Second),
		powerSource:          powerSource,
		batteryPercent:       batteryPercent,
		counters: vmCounters{
			swapins:        uint64(statistics.swapins),
			swapouts:       uint64(statistics.swapouts),
			pageins:        uint64(statistics.pageins),
			pageouts:       uint64(statistics.pageouts),
			compressions:   uint64(statistics.compressions),
			decompressions: uint64(statistics.decompressions),
		},
	}, nil
}

var pressureSysctlName = C.CString("kern.memorystatus_vm_pressure_level")

func pressureName(level int64) string {
	switch level {
	case 1:
		return "normal"
	case 2:
		return "warning"
	case 4:
		return "critical"
	}
	return "unknown"
}

// topProcesses groups processes by name (so the many Chrome helpers count as one application) and returns the
// largest groups by memory.
func (c *collector) topProcesses(limit int) []processGroup {
	count := int(C.proc_listallpids(nil, 0))
	if count <= 0 {
		return nil
	}
	if cap(c.pidBuffer) < count+64 {
		c.pidBuffer = make([]C.int, count+64)
	}
	buffer := c.pidBuffer[:cap(c.pidBuffer)]
	count = int(C.proc_listallpids(unsafe.Pointer(&buffer[0]), C.int(len(buffer))*C.int(unsafe.Sizeof(buffer[0]))))

	groups := map[string]*processGroup{}
	for _, pid := range buffer[:max(count, 0)] {
		var memory C.uint64_t
		if pid == 0 || C.read_process_memory(pid, &memory) != 0 {
			continue
		}
		if C.proc_name(pid, unsafe.Pointer(c.nameBuffer), c.nameBufferSize) <= 0 {
			continue
		}
		name := C.GoString(c.nameBuffer)
		group, found := groups[name]
		if !found {
			group = &processGroup{name: name}
			groups[name] = group
		}
		group.count++
		group.memoryMB += uint64(memory)
	}

	result := make([]processGroup, 0, len(groups))
	for _, group := range groups {
		group.memoryMB /= megabyte
		result = append(result, *group)
	}
	sort.Slice(result, func(i, j int) bool { return result[i].memoryMB > result[j].memoryMB })
	return result[:min(limit, len(result))]
}

// bootTime is re-read on each call: a changed value between samples proves the laptop was rebooted.
func bootTime() time.Time {
	return timeSysctl(bootTimeName)
}

// lastSleep returns when the laptop last went to sleep and last woke up (zero values if it has not slept since boot).
func lastSleep() (sleep time.Time, wake time.Time) {
	return timeSysctl(sleepTimeName), timeSysctl(wakeTimeName)
}

var (
	bootTimeName  = C.CString("kern.boottime")
	sleepTimeName = C.CString("kern.sleeptime")
	wakeTimeName  = C.CString("kern.waketime")
)

func timeSysctl(name *C.char) time.Time {
	var seconds C.int64_t
	if C.read_time_sysctl(name, &seconds) != 0 || seconds == 0 {
		return time.Time{}
	}
	return time.Unix(int64(seconds), 0)
}

// readPower returns "battery", "ac" or "unknown" and the battery charge in percent (-1 when there is no battery).
func readPower() (string, int) {
	var onBattery, percent C.int
	C.read_power(&onBattery, &percent)
	switch onBattery {
	case 1:
		return "battery", int(percent)
	case 0:
		return "ac", int(percent)
	}
	return "unknown", int(percent)
}
