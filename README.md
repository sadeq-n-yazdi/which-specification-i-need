# mac-performance-logger

Small background logger that records macOS memory pressure, swap activity and the largest applications at a fixed
interval between a configured start and stop time. It reads kernel counters directly (no `vm_stat`/`ps` child
processes), uses about 4 MB of RAM and near-zero CPU, and appends one short CSV line per sample. launchd runs it at
interactive priority (no CPU, disk or timer throttling), so samples are not dropped when the machine is overloaded.

## Usage

```sh
make test        # vet and unit tests
make sample      # print one sample to stdout
vim logger.conf  # set start, stop, interval
make install     # build and run in the background via launchd (also starts at every login)
make status      # is it running, last log lines
make restart     # after editing logger.conf
make uninstall   # stop and remove the launchd job
```

## Output (`logs/`)

- `system-YYYY-MM-DD.csv` — one row per sample: pressure (`normal`/`warning`/`critical`), memory breakdown in MB,
  swap used, load average, system uptime, power source (`ac`/`battery`) and battery %, and swapins/swapouts/pageins/pageouts/compressions/decompressions **since the previous
  sample** (empty on the first row).
- `processes-YYYY-MM-DD.csv` — top N applications by physical footprint, grouped by process name.
- `events.csv` — the logger's own lifecycle: `start`, `stop` (with reason), `gap` (no samples for more than two
  intervals), `sleep` (sleep and wake time and duration), `last_sleep_before_start`, `power` (plugged in/unplugged)
  and `error`, each with pid, system boot time and uptime. A changed boot time means
  the laptop was rebooted in between.
- `logger.log` — the same messages as plain text, plus anything printed before the log directory exists.

Processes owned by root (e.g. security agents) cannot be read without root and are not listed.

If the column layout of a file changes (after an upgrade), the existing file for that day is kept as
`<name>-before-HHMMSS.csv` and a new file with the new header is started.

## Quick start for a new machine

Needs macOS, [Go](https://go.dev/dl/) (version in `go.mod`) and [uv](https://docs.astral.sh/uv/) for the reports.

1. Unpack the folder somewhere permanent (launchd points at it), e.g. `~/code/mac-performance-logger`.
2. Copy `logger.conf.sample` to `logger.conf` and edit it: set `start` and `stop` (local time, about two weeks apart) and make `log_dir` point at the
   `logs` folder inside this directory.
3. Run `make sample` (one sample is printed), then `make install` and `make status`.
4. Work normally on battery and on AC. The logger stops itself at `stop`. Use `make restart` after editing the
   config and `make uninstall` to remove it.

## Creating the report

```sh
uv run --with pandas --with matplotlib python report/make_charts.py
uv run --with pandas --with matplotlib python report/make_report.py --no-pdf
uv run --with pandas --with matplotlib python report/make_working_hours_report.py --no-pdf
```

Reports are written to `report/` and `report/working-hours/`. Drop `--no-pdf` to also build PDFs (needs `pandoc` and
`xelatex`). The application descriptions in `report/make_report.py` (`APPLICATION_PURPOSES`) and the working hours in
`report/make_working_hours_report.py` (Mon–Fri 08:30–17:30) are written for the original author's machine; adjust them
before sharing results. The `logs/` files contain application names, so review them before sending them to anyone.
