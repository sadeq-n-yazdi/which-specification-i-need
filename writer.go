package main

import (
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// dailyLogWriter appends lines to "<prefix>-YYYY-MM-DD.csv", starting a new file each day. Each line is a single
// unbuffered write, so nothing is lost if the process is killed and no extra fsync pressure is put on the disk.
type dailyLogWriter struct {
	directory   string
	prefix      string
	header      string
	currentDate string
	file        *os.File
}

func (w *dailyLogWriter) write(date string, line string) error {
	if date != w.currentDate {
		if err := w.open(date); err != nil {
			return err
		}
	}
	_, err := w.file.WriteString(line + "\n")
	return err
}

func (w *dailyLogWriter) open(date string) error {
	if err := w.openPath(filepath.Join(w.directory, fmt.Sprintf("%s-%s.csv", w.prefix, date))); err != nil {
		return err
	}
	w.currentDate = date
	return nil
}

func (w *dailyLogWriter) openPath(path string) error {
	w.close()
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_RDWR, 0o644)
	if err != nil {
		return err
	}
	if !hasHeader(file, w.header) {
		file.Close()
		archived := strings.TrimSuffix(path, ".csv") + time.Now().Format("-before-150405") + ".csv"
		if err := os.Rename(path, archived); err != nil {
			return err
		}
		if file, err = os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644); err != nil {
			return err
		}
	}
	if info, err := file.Stat(); err == nil && info.Size() == 0 {
		if _, err := file.WriteString(w.header + "\n"); err != nil {
			file.Close()
			return err
		}
	}
	w.file = file
	return nil
}

// hasHeader reports whether the file is empty or starts with the given header line.
func hasHeader(file *os.File, header string) bool {
	info, err := file.Stat()
	if err != nil || info.Size() == 0 {
		return true
	}
	firstLine := make([]byte, len(header)+1)
	count, _ := file.ReadAt(firstLine, 0)
	return string(firstLine[:count]) == header+"\n"
}

func (w *dailyLogWriter) close() {
	if w.file != nil {
		w.file.Close()
		w.file = nil
	}
}

const eventHeader = "timestamp,event,pid,boot_time,uptime_seconds,details"

// eventLog records the logger's own lifecycle (start, stop, gaps, errors) in events.csv, together with the
// system boot time, so every hour of data can be accounted for. Events are rare, so the file is opened per write.
type eventLog struct {
	directory string
}

func (e *eventLog) record(event string, details string) {
	now := time.Now()
	boot := bootTime()
	line := fmt.Sprintf("%s,%s,%d,%s,%d,%s", now.Format(timestampLayout), event, os.Getpid(),
		boot.Format(timestampLayout), int64(now.Sub(boot).Seconds()), csvField(details))
	log.Printf("%s: %s", event, details)

	writer := &dailyLogWriter{directory: e.directory, prefix: "events", header: eventHeader}
	defer writer.close()
	if err := writer.openPath(filepath.Join(e.directory, "events.csv")); err == nil {
		writer.file.WriteString(line + "\n")
	}
}
