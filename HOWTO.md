# HOWTO: collect memory data on your Mac and build a report

macOS (Apple Silicon or Intel) and Linux (x86_64 or arm64, kernel 4.20 or newer). The logger needs no admin rights.

## 1. Get the tool

Either download a release bundle (`mac-performance-logger-<version>-darwin-arm64.tar.gz` for Apple Silicon,
`...-darwin-amd64.tar.gz` for Intel Macs, `...-linux-amd64.tar.gz` or `...-linux-arm64.tar.gz` for Linux) from the repository's Releases page and unpack it, or clone the repository and
install [Go](https://go.dev/dl/) (version in `go.mod`). You also need GNU `make`; on Linux with systemd the service
is installed for your user (`systemctl --user`), without root.

Put the folder somewhere permanent, for example `~/code/mac-performance-logger`: launchd points at it, so moving it
later means running `make install` again.

## 2. Configure

```sh
cp logger.conf.sample logger.conf
vim logger.conf
```

- `start` / `stop`: local time, `YYYY-MM-DD HH:MM`. Choose about two weeks that include normal working days.
- `interval`: `1m` is a good default (about 1 MB of logs per day).
- `log_dir`: the `logs` folder inside this directory, for example `~/code/mac-performance-logger/logs`.
- `top_processes`: how many of the largest applications to record per sample.

## 3. Install and check

```sh
make sample    # prints one sample; confirms it works
make install   # registers the background job (launchd on macOS, systemd user service on Linux)
make status    # state, pid and the last log lines
```

Work as usual, on battery and on AC. Close the lid whenever you normally would; on macOS sleep is recorded, on Linux it shows as a gap.

On a Linux server also run `loginctl enable-linger $USER` once, so the service keeps running when you are logged out.

Other commands: `make restart` after editing `logger.conf`, `make uninstall` to stop and remove the job.

## 4. Build the report

Needs [uv](https://docs.astral.sh/uv/). For PDFs also `pandoc` and `xelatex`.

```sh
uv run --with pandas --with matplotlib python report/make_charts.py
uv run --with pandas --with matplotlib python report/make_report.py --no-pdf
uv run --with pandas --with matplotlib python report/make_working_hours_report.py --no-pdf
```

The scripts detect macOS or Linux and word the report accordingly. Output goes to `report/` (all hours) and `report/working-hours/` (Mon-Fri 08:30-17:30). Drop `--no-pdf` to also
build PDFs.

If the logs were recorded on a machine with a different page size (for example a Mac, 16384 bytes, analysed on an x86 Linux
box, 4096 bytes), prefix the commands with `LOGGER_PAGE_SIZE_BYTES=16384`.

Before sharing the result, edit the machine-specific parts: `APPLICATION_PURPOSES` in `report/make_report.py` and the
working hours in `report/make_working_hours_report.py`. `REPORT_INSTRUCTIONS.md` describes the full method; you can
give it to an assistant to write a report for your own situation.

## 5. Privacy

`logs/` contains application names, times and battery state. Review it before sending it to anyone. Nothing is
uploaded anywhere by the logger.

## Troubleshooting

- `make status` says "not loaded": run `make install` again, then check `logs/logger.log`.
- No files appear: `start` is probably in the future, or `stop` is in the past. Fix `logger.conf`, then `make restart`.
- `make install` says `logger.conf` is missing: run `cp logger.conf.sample logger.conf` first.
- Linux without systemd (Alpine, containers, WSL1): run `bin/mac-performance-logger -config logger.conf` from your own init
  system or `tmux`; `make install` stops with an explanation.
- Samples missing for a period: see `gap` rows (and, on macOS, `sleep` rows) in `logs/events.csv`.
- Root-owned processes (for example security agents) cannot be read and are not listed, but they count in system totals.
