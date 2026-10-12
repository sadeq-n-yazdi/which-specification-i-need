package main

import (
	"os"
	"path/filepath"
	"testing"
)

// When the column layout changes mid-day, old rows must not end up under a new header, or the report's CSV
// parsing fails or silently shifts columns.
func TestDailyLogWriterSetsAsideFileWithOldHeader(t *testing.T) {
	directory := t.TempDir()
	path := filepath.Join(directory, "system-2026-09-26.csv")
	if err := os.WriteFile(path, []byte("old,header\n1,2\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	writer := &dailyLogWriter{directory: directory, prefix: "system", header: "new,header,extra"}
	if err := writer.write("2026-09-26", "1,2,3"); err != nil {
		t.Fatal(err)
	}
	writer.close()

	content, _ := os.ReadFile(path)
	if string(content) != "new,header,extra\n1,2,3\n" {
		t.Errorf("current file = %q", content)
	}
	archived, _ := filepath.Glob(filepath.Join(directory, "system-2026-09-26-before-*.csv"))
	if len(archived) != 1 {
		t.Fatalf("expected the old file to be kept, found %v", archived)
	}
	if old, _ := os.ReadFile(archived[0]); string(old) != "old,header\n1,2\n" {
		t.Errorf("archived file = %q", old)
	}
}

// Appending after a restart with the same layout must continue the file, not rotate it.
func TestDailyLogWriterAppendsWithSameHeader(t *testing.T) {
	directory := t.TempDir()
	for _, line := range []string{"1", "2"} {
		writer := &dailyLogWriter{directory: directory, prefix: "system", header: "value"}
		if err := writer.write("2026-09-26", line); err != nil {
			t.Fatal(err)
		}
		writer.close()
	}
	content, _ := os.ReadFile(filepath.Join(directory, "system-2026-09-26.csv"))
	if string(content) != "value\n1\n2\n" {
		t.Errorf("content = %q", content)
	}
}
