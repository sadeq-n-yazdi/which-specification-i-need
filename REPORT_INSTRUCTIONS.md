# Instructions: build the memory evidence report

You are turning the data in `logs/` into a Markdown report with charts. The report supports a request to upgrade
the RAM of the machine that was measured (a Mac or a Linux laptop, desktop or server; give its model and installed RAM). A manager objected that other developers use similar machines
without problems, so the report must be **factual, reproducible and free of exaggeration**. Every number in it must
come from the logs, and each claim must say which file and column it comes from.

## Inputs

All files are CSV with a header row; times are the laptop's local time, `YYYY-MM-DDTHH:MM:SS`.

| File | One row per | Key columns |
|------|-------------|-------------|
| `logs/system-YYYY-MM-DD.csv` | sample (every `interval`, see `logger.conf`) | `pressure` (normal/warning/critical/unknown), `physical_mb`, `app_mb`, `wired_mb`, `compressor_occupied_mb`, `compressor_stored_mb`, `swap_used_mb`, `free_mb`, `load_average_1m`, `uptime_seconds`, `power_source` (ac/battery), `battery_percent`, and `swapins`, `swapouts`, `pageins`, `pageouts`, `compressions`, `decompressions` |
| `logs/processes-YYYY-MM-DD.csv` | sample × top-N application | `rank`, `name`, `process_count`, `memory_mb` (physical footprint, processes grouped by name) |
| `logs/events.csv` | logger lifecycle event | `event` (start, stop, gap, sleep and last_sleep_before_start on macOS only, power, error), `pid`, `boot_time`, `uptime_seconds`, `details` |

Facts about the data you must respect:

- The counter columns (`swapins` … `decompressions`) are **pages per interval** (the delta since the previous sample),
  not totals. They are empty on the first row after every logger start; treat empty as missing, not zero. Get the page
  size of the machine that wrote the logs to convert pages to MB: `sysctl -n hw.pagesize` on macOS (16384 on Apple
  Silicon), `getconf PAGESIZE` on Linux (4096 on most x86_64 and arm64 distributions, 16384 or 65536 on some arm64
  kernels). The scripts use the page size of the machine they run on; set `LOGGER_PAGE_SIZE_BYTES` if the logs come
  from another one.
- `memory_mb` values of different applications overlap slightly (shared memory), so never add them up and present
  the total as "RAM used". Use `system-*.csv` for totals.
- Root-owned processes (e.g. security software) are not in `processes-*.csv` on macOS; they are still counted in the
  system totals. Say so in the report. On Linux every process is listed (kernel threads are skipped).
- Linux columns differ from macOS, see "Linux data" below. Never compare `pressure` levels, `compressor_*` or
  `wired_mb` between a Mac and a Linux machine as if they were the same measurement.
- Wall-clock gaps are explained by `gap` events; on macOS `sleep` events give the exact sleep and wake time. Do not
  interpolate across them. The kernel only keeps the latest sleep, so several short sleeps inside one interval show
  as one; cross-check with `pmset -g log | grep -E " (Sleep|Wake) "` if exact counts matter. Linux has no `sleep`
  events: a suspended machine appears as a `gap` only (cross-check with `journalctl -b | grep -E "PM: suspend (entry|exit)"`),
  and a server that never sleeps has none.
- Files named `*-before-HHMMSS.csv` hold rows written before a column layout change on that day. Load them too; their
  missing columns (e.g. power) are unknown, not zero.

## Step 1 — Validate the data before analysing it

Write the findings of this step into a "Data quality" section:

1. Coverage: first and last sample, number of samples, expected samples (`duration / interval`), percentage covered.
2. List every `gap`, `error`, `start` and `stop` from `events.csv`. A `start` without a preceding `stop` means the
   logger crashed or the laptop lost power; say which.
3. Reboots: distinct `boot_time` values in `events.csv`, and places where `uptime_seconds` in `system-*.csv` drops.
4. Sleep: total time asleep per day from `sleep` events, and samples on battery versus AC.
5. Split samples into working hours (Mon–Fri 09:00–19:00 unless the user says otherwise) and the rest. The main
   analysis uses working hours only; idle nights with the lid closed would dilute the result.

Report samples as covered only when the laptop was awake; time asleep is not missing data.
If coverage in working hours is below 80 %, stop and tell the user before writing conclusions.

## Step 2 — Analysis

Compute, for working hours:

1. **Memory pressure**: percentage of samples at normal / warning / critical, overall and per day.
2. **Swap**: `swap_used_mb` median, 95th percentile and max; how often swap used exceeds 25 %, 50 % and 100 % of
   `physical_mb`.
3. **Swap traffic**: swapins + swapouts per interval converted to MB/minute; median, p95, max, and total GB written
   to swap per working day (a proxy for SSD wear and for waiting time).
4. **Compression**: `compressor_stored_mb` versus `compressor_occupied_mb` (how much data is squeezed into how much RAM),
   and compressions + decompressions per minute as a CPU cost signal.
5. **Estimated memory demand**: `app_mb + wired_mb + compressor_stored_mb + swap_used_mb` (a rough "memory the
   workload wants") per sample; median and p95, compared with `physical_mb`. State that this is an estimate.
6. **Largest consumers**: for each application in `processes-*.csv`, median and p95 `memory_mb` during working
   hours and how often it is in the top 10. Group into categories: browser, containers/VM
   (`com.apple.Virtualization.*`, `limactl`, `qemu*`, docker, `dockerd`, `containerd`), IDEs (PyCharm, Code), AI tools
   (claude, node started by agents), terminals, databases (`postgres`, `mysqld`, `redis`), other. Linux truncates
   process names to 15 characters.
7. **Power**: pressure and swap traffic on battery versus AC, to show the problem is not tied to one power state.
   Skip this on machines whose `power_source` is `unknown` (servers, desktops).
8. **Worst moments**: the 5 intervals with the highest swap traffic; for each show time, pressure, swap used and the
   top 5 applications at that sample.

Do not invent causes. When you link a symptom to an application, show the numbers side by side.

## Step 3 — Charts

Write one script, `report/make_charts.py`, run it with `uv run --with pandas --with matplotlib python
report/make_charts.py`, and save PNGs (width 1600 px, readable fonts, titled, axes with units, source note in the
caption) to `report/charts/`:

1. `pressure_timeline.png` — pressure level over time as a coloured band (green/yellow/red), gaps left blank.
2. `swap_used.png` — `swap_used_mb` over time, with a horizontal line at `physical_mb`.
3. `swap_traffic.png` — swap MB/minute over time (working hours shaded).
4. `memory_demand.png` — stacked area of `app_mb`, `wired_mb`, `compressor_occupied_mb` against the physical RAM line,
   plus the estimated demand line from Step 2.5.
   Shade sleep periods grey and battery periods lightly on the timeline charts (1–4).
5. `pressure_by_day.png` — stacked bar per day of % normal / warning / critical.
6. `top_consumers.png` — horizontal bar of median and p95 memory per application category.
7. `heatmap_hour_weekday.png` — median swap traffic by hour of day × weekday.

Keep the script deterministic and re-runnable; it reads everything from `logs/` and writes nothing outside `report/`.
The existing scripts detect the operating system (`platform.system()`) and word the text accordingly.

## Step 4 — The report

Write `report/memory-report.md`, at most about 3 pages, for a non-specialist manager:

1. **Summary** (5 bullet points max): measurement period, coverage, % of working time under memory pressure, typical
   and peak swap use against installed RAM, estimated demand, and the conclusion.
2. **Machine and method**: model, RAM, operating system version (`sw_vers` on macOS, `/etc/os-release` and `uname -r` on
   Linux), what was measured, how often, and the logger's own
   cost (about 4 MB RAM, negligible CPU).
3. **Findings**: one short paragraph plus one chart per finding from Step 2.
4. **Workload**: which applications need the memory and why they are required for the work (containers for the
   local stack, Postgres, IDE, browser for testing, AI tools). Keep it factual.
5. **Why other developers may not see this**: explain in neutral terms that memory need depends on the workload
   (number of containers, parallel projects, AI agents), not on the job title, and that the logger can be run on
   their machines for a like-for-like comparison. Do not claim anything about other machines without their data.
6. **Cost of the problem**: if the user gives benchmark results (for example `make test` time right after a reboot
   and under normal load), include them in a table. Otherwise leave a clearly marked placeholder. Do not make up
   timings or money figures.
7. **Recommendation**: the RAM size the p95 estimated demand supports, with some headroom (e.g. p95 demand × 1.3,
   rounded up to an available configuration).
8. **Data quality** from Step 1, and an appendix listing the files and the exact commands used.

Embed charts with relative links (`![Swap used](charts/swap_used.png)`). Convert to PDF if asked, for example with
`pandoc report/memory-report.md -o report/memory-report.pdf --pdf-engine=xelatex --resource-path=report`.

## Checks before handing over

- Every number in the report can be regenerated by re-running the script.
- No value is summed across `processes-*.csv` applications as if it were total RAM.
- Missing data and root-owned processes are stated, not hidden.
- The tone is factual; there are no claims beyond what the data shows.

## Linux data

The Linux collector fills the same columns from different sources (full table in `README.md`). Keep these points when
reporting on Linux data:

- `pressure` comes from `/proc/pressure/memory` (PSI): `warning` = `some avg10` of 10 or more, `critical` =
  `full avg10` of 10 or more, `unknown` when the kernel has no PSI. Describe it as "time stalled on memory", not as the
  macOS level.
- `wired_mb` is unreclaimable kernel memory, `app_mb` is anonymous memory, `cached_files_mb` is page cache plus
  buffers. The "estimated memory demand" formula stays `app_mb + wired_mb + compressor_stored_mb + swap_used_mb`, but
  page cache is reclaimable and is not demand; say so.
- `compressor_*`, `compressions` and `decompressions` are zero unless zram or zswap is in use; do not report zero as a
  measured "no compression cost" without saying why.
- `pageins` and `pageouts` count block-device traffic (including ordinary file reads and writes), not only swap, so do
  not read them as memory trouble. Use `swapins` and `swapouts`.
- `processes-*.csv` `memory_mb` is anonymous memory per process name (`RssAnon`), grouped by name; kernel threads are not listed.
- Servers have no battery and may never sleep: leave out the power and sleep sections instead of printing empty ones.
- Reports mention "SSD"; on a machine with spinning disks or network storage, say "disk".
