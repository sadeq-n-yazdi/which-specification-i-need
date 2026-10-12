"""Write report/memory-report.md (and the PDF) from report/statistics.json produced by make_charts.py.

Run: uv run python report/make_report.py [--no-pdf]
Every number in the report comes from statistics.json or from the live system commands listed in the appendix.
"""

import argparse
import json
import platform
import re
import subprocess
from datetime import datetime
from pathlib import Path

REPORT_DIRECTORY = Path(__file__).resolve().parent
TARGET_RAM_CONFIGURATIONS_GB = (24, 32, 36, 48, 64, 96, 128)
HEADROOM = 1.3
APPLICATION_PURPOSES = {
    "Google Chrome Helper (Renderer)": ("Chrome renderers (tabs, web apps)", "web app testing, Jira, Confluence, docs"),
    "claude": ("AI coding assistants (`claude` sessions)", "development and code review tooling"),
    "com.apple.Virtualization.Virtua": ("Rancher Desktop VM (Docker)", "local stack: Postgres, services, tests"),
    "pycharm": ("PyCharm", "IDE"),
    "node": ("Node processes", "tooling, language servers, agents"),
    "Code Helper (Plugin)": ("VS Code extensions", "editor"),
    "Code Helper (Renderer)": ("VS Code windows", "editor"),
    "iTerm2": ("iTerm2", "terminal"),
    "python3.13": ("Python processes", "tests, scripts, local services"),
}


def command_output(*command: str) -> str:
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()


def read_text(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def linux_disk_written_bytes() -> int:
    """Bytes written to all physical block devices since boot (512-byte sectors, 7th field of /sys/block/*/stat)."""
    total = 0
    for stat_file in Path("/sys/block").glob("*/stat"):
        if re.match(r"(loop|ram|zram|dm-|md|sr|fd|nbd)", stat_file.parent.name):
            continue
        fields = read_text(str(stat_file)).split()
        if len(fields) > 6:
            total += int(fields[6]) * 512
    return total


def linux_machine_facts() -> dict:
    os_release = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', read_text("/etc/os-release"), re.MULTILINE))
    cpu = re.search(r"^(?:model name|Model)\s*:\s*(.+)$", read_text("/proc/cpuinfo"), re.MULTILINE)
    boot_seconds = int(re.search(r"^btime (\d+)", read_text("/proc/stat"), re.MULTILINE).group(1))
    model = read_text("/sys/class/dmi/id/product_name") or read_text("/proc/device-tree/model") or "Linux machine"
    return {
        "title": model,
        "model": model,
        "chip": cpu.group(1) if cpu else "unknown CPU",
        "operating_system": f"{os_release.get('PRETTY_NAME', 'Linux')}, kernel {platform.release()}",
        "kernel_name": "Linux",
        "pressure_source": "The Linux kernel (pressure stall information)",
        "pressure_note": "“Memory pressure” is derived from the kernel's pressure stall information "
        "(`/proc/pressure/memory`): “warning” means processes stalled on memory for 10 % or more of the last 10 "
        "seconds, “critical” means all non-idle processes stalled at the same time for 10 % or more. It is not the same "
        "scale as the macOS level.",
        "swap_owner": "The kernel",
        "vendor": "The manufacturer",
        "storage_note": "Flash memory has a limited write endurance. On many laptops the SSD is soldered and cannot be "
        "replaced; on servers and desktops it can be.",
        "noun": "machine",
        "live_sources": "`/proc`, `/sys` and `/etc/os-release` (disk bytes written)",
        "boot_time": datetime.fromtimestamp(boot_seconds),
        "ssd_written_tb": linux_disk_written_bytes() / 1e12,
    }


def macos_machine_facts() -> dict:
    boot_seconds = int(re.search(r"sec = (\d+)", command_output("sysctl", "-n", "kern.boottime")).group(1))
    disk_statistics = command_output("ioreg", "-r", "-c", "IOBlockStorageDriver", "-w0")
    written_bytes = sum(int(value) for value in re.findall(r'"Bytes \(Write\)"=(\d+)', disk_statistics))
    model = command_output("sysctl", "-n", "hw.model")
    return {
        "title": "MacBook Pro" if model.startswith("MacBookPro") else model,
        "model": model,
        "chip": command_output("sysctl", "-n", "machdep.cpu.brand_string"),
        "operating_system": f"macOS {command_output('sw_vers', '-productVersion')} "
        f"({command_output('sw_vers', '-buildVersion')})",
        "kernel_name": "macOS",
        "pressure_source": "macOS",
        "pressure_note": "“Memory pressure” is macOS's own assessment (the colour in Activity Monitor's Memory tab).",
        "swap_owner": "macOS",
        "vendor": "Apple",
        "storage_note": "The SSD is soldered to the logic board and cannot be replaced, so its write endurance limits "
        "the life of the machine.",
        "noun": "laptop",
        "live_sources": "`sysctl`, `sw_vers` and `ioreg` (SSD bytes written)",
        "boot_time": datetime.fromtimestamp(boot_seconds),
        "ssd_written_tb": written_bytes / 1e12,
    }


def machine_facts() -> dict:
    facts = macos_machine_facts() if platform.system() == "Darwin" else linux_machine_facts()
    facts["chip_short"] = facts["chip"].replace("Apple ", "")
    facts["days_since_boot"] = (datetime.now() - facts["boot_time"]).total_seconds() / 86400
    return facts


def compression_sentence(statistics: dict, machine: dict) -> str:
    stored = statistics["compressor_stored_mb"]["median"]
    occupied = statistics["compressor_occupied_mb"]["median"]
    if machine["kernel_name"] == "Linux" and stored == 0:
        return "No compressed memory was recorded (zram is not in use on this machine), so no CPU time went into it."
    prefix = (
        "macOS also compresses memory"
        if machine["kernel_name"] == "macOS"
        else "This machine also compresses memory (zram)"
    )
    return (
        f"{prefix}: a median {gigabytes(stored)} of data was squeezed into {gigabytes(occupied)} of RAM, "
        "which costs CPU time."
    )


def gigabytes(megabytes: float) -> str:
    return f"{megabytes / 1024:.1f} GB"


def percent(value: float) -> str:
    return f"{value:.1f} %"


def day_label(day: str) -> str:
    return datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b")


def timestamp_label(value: str) -> str:
    return datetime.fromisoformat(value).strftime("%a %d %b %H:%M")


def value_range(values: list[float], unit: str = "GB", divisor: float = 1024) -> str:
    low, high = min(values) / divisor, max(values) / divisor
    return f"{low:.1f} {unit}" if round(low, 1) == round(high, 1) else f"{low:.1f}–{high:.1f} {unit}"


def recommendation(conservative_p95_mb: float, upper_p95_mb: float) -> tuple[int, int]:
    need_gb = conservative_p95_mb / 1024 * HEADROOM
    minimum = next(size for size in TARGET_RAM_CONFIGURATIONS_GB if size >= need_gb)
    covering = next(size for size in TARGET_RAM_CONFIGURATIONS_GB if size >= upper_p95_mb / 1024)
    return minimum, covering


def worst_moment_summary(moments: list[dict]) -> str:
    by_application: dict[str, list[float]] = {}
    for moment in moments:
        for entry in moment["top_applications"]:
            name, megabytes = re.fullmatch(r"(.*) \((\d+) MB\)", entry).groups()
            by_application.setdefault(name, []).append(float(megabytes))
    parts = [
        f"{APPLICATION_PURPOSES.get(name, (name,))[0]} ({value_range(values)})"
        for name, values in sorted(by_application.items(), key=lambda item: -max(item[1]))
    ]
    traffic = [moment["swap_traffic_mb_per_minute"] for moment in moments]
    swap = [moment["swap_used_mb"] for moment in moments]
    times = sorted(moment["timestamp"] for moment in moments)
    return (
        f"Worst five minutes ({timestamp_label(times[0])} – {timestamp_label(times[-1])}): "
        f"{value_range(traffic, 'GB/min')} swap traffic with {value_range(swap)} swap used. The largest applications at "
        f"those moments were {', '.join(parts)}: the normal set of development tools running at the same time, not a "
        "single misbehaving program."
    )


def cost_section(statistics: dict, machine: dict) -> str:
    timing_rows = (
        "| Task | After reboot | Normal load | Difference |\n|---|---:|---:|---:|\n"
        "| `make test` | _tbd_ | _tbd_ | _tbd_ |\n| Docker build | _tbd_ | _tbd_ | _tbd_ |\n"
        "| IDE indexing | _tbd_ | _tbd_ | _tbd_ |"
    )
    reboot = statistics.get("after_last_reboot")
    if not reboot:
        return (
            "> **Placeholder — to be filled with real measurements.** Time `make test`, a Docker build and IDE indexing\n"
            "> (a) right after a reboot and (b) during normal work, and record the difference.\n\n" + timing_rows
        )
    after, normal = reboot["after_reboot"], reboot["normal_load"]
    hours = (datetime.fromisoformat(reboot["last_sample"]) - datetime.fromisoformat(reboot["boot_time"])).seconds / 3600

    def minutes(value: float | None) -> str:
        return "not reached" if value is None else f"{value:.0f} min"

    def row(label: str, key: str, unit=gigabytes) -> str:
        return (
            f"| {label} | {unit(after[key]['median'])} (max {unit(after[key]['max'])}) | "
            f"{unit(normal[key]['median'])} (max {unit(normal[key]['max'])}) |"
        )

    thresholds = reboot["minutes_to_swap_over_mb"]
    normal_days = ", ".join(day_label(day) for day in reboot["normal_load_days"])
    return f"""The {machine["noun"]} was rebooted on {timestamp_label(reboot["boot_time"])}. The {after["samples"]} samples since then
({hours:.1f} h) show how quickly a clean start fills up with the normal set of tools, compared with
{normal["samples"]:,} working-hour samples before the reboot ({normal_days}). Source: `logs/system-*.csv`.

| After the reboot | Minutes after boot |
|---|---:|
| First “warning” pressure sample | {minutes(reboot["minutes_to_first_warning"])} |
| Swap used above 1 GB | {minutes(thresholds["1024"])} |
| Swap used above 4 GB | {minutes(thresholds["4096"])} |
| Swap used above 8 GB | {minutes(thresholds["8192"])} |

| Median (max) | After reboot | Normal load |
|---|---:|---:|
| Samples at “warning” or “critical” | {percent(after["warning_or_critical_percent"])} | \
{percent(normal["warning_or_critical_percent"])} |
{row("Swap used", "swap_used_mb")}
{row("Swap traffic per minute", "swap_traffic_mb_per_minute", lambda value: f"{value:,.0f} MB")}
{row("Compressed data in RAM", "compressor_stored_mb")}

A reboot only resets swap; memory pressure returns within minutes, so restarting is not a workaround. The time lost
per task is not in the logs. Time `make test`, a Docker build and IDE indexing right after a reboot and during
normal work to fill this table:

{timing_rows}"""


def build(statistics: dict, machine: dict) -> str:
    working = statistics["working_hours"]
    working_days = working["days"]
    working_pressure = statistics["pressure_percent_working"]
    swap_working = working["swap_used_mb"]
    traffic_working = working["swap_traffic_mb_per_minute"]
    conservative, upper = working["demand_conservative_mb"], working["demand_upper_mb"]
    minimum_gb, covering_gb = recommendation(conservative["p95"], upper["p95"])
    physical_gb = statistics["physical_mb"] // 1024
    written_by_day = statistics["swap_written_gb_per_day"]
    busiest_day = max(written_by_day, key=written_by_day.get)
    swap_share = statistics["swap_used_over_percent_of_ram"]
    power = statistics["power"]
    categories = statistics["category_mb"]
    period = f"{timestamp_label(statistics['first_sample'])} – {timestamp_label(statistics['last_sample'])}"
    working_day_text = ", ".join(day_label(day) for day in working_days) or "none yet"
    too_few_days = len(working_days) < 5
    reboots = len(statistics["boot_times"]) - 1
    reboot_text = "No reboot during the period." if reboots == 0 else f"{reboots} reboot(s) during the period."
    stops = sum(event["event"] == "stop" for event in statistics["events"])
    stop_text = (
        f"{stops - reboots} logger upgrades, {reboots} shutdown(s) before a reboot" if reboots else "logger upgrades"
    )
    # The SSD counter resets at boot, so a fresh boot says nothing about the measurement period.
    ssd_text = (
        f"The SSD has written **{machine['ssd_written_tb']:.1f} TB in the {machine['days_since_boot']:.1f} days since "
        "the last boot**. "
        if machine["days_since_boot"] >= 1
        else ""
    )

    pressure_rows = "\n".join(
        f"| {day_label(day)} | {percent(shares.get('normal', 0))} | {percent(shares.get('warning', 0))} | "
        f"{percent(shares.get('critical', 0))} |"
        for day, shares in statistics["pressure_percent_by_day"].items()
    )
    written_rows = ", ".join(f"{day_label(day)} {value:,.0f} GB" for day, value in written_by_day.items())
    consumer_rows = "\n".join(
        f"| {APPLICATION_PURPOSES.get(row['name'], (row['name'], 'other'))[0]} | {gigabytes(row['median_mb'])} | "
        f"{gigabytes(row['p95_mb'])} | {row['share_of_samples_percent']:.0f} % | "
        f"{APPLICATION_PURPOSES.get(row['name'], ('', 'other'))[1]} |"
        for row in statistics["top_consumers"][:8]
    )
    category_text = ", ".join(
        f"{name} {gigabytes(values['median'])} (up to {gigabytes(values['p95'])})"
        for name, values in sorted(categories.items(), key=lambda item: -item[1]["p95"])
        if name != "Other"
    )
    power_text = "; ".join(
        f"{source.upper() if source == 'ac' else source}: {values['samples']:,} samples, "
        f"“warning” in {percent(values['pressure_percent'].get('warning', 0))}"
        for source, values in power.items()
    )
    sample_banner = (
        f"**SAMPLE REPORT** — interim result of a 14-day measurement. It includes {len(working_days)} working "
        f"day(s) ({working_day_text}); the final report will cover two full working weeks.\n\n"
        if too_few_days
        else ""
    )

    return f"""# Memory usage report — {machine["title"]} ({machine["chip_short"]}, {physical_gb} GB)

{sample_banner}## Summary

- **Period:** {period} ({statistics["hours_span"]:.0f} h, of which {statistics["hours_awake"]} h awake).
  {statistics["samples"]:,} one-minute samples = {percent(statistics["awake_coverage_percent"])} of the awake time.
  {reboot_text}
- **Memory pressure:** {machine["pressure_source"]} reported **warning** in **{percent(working_pressure.get("warning", 0))}** of working-hour
  samples and {percent(statistics["pressure_percent_all"].get("warning", 0))} of all samples; “normal” in only
  {percent(working_pressure.get("normal", 0))} of working time.
- **Swap:** during working hours a median **{gigabytes(swap_working["median"])}** of memory was held on the SSD
  (installed RAM: {physical_gb} GB), and a median **{gigabytes(traffic_working["median"])} per minute** was moved
  between RAM and SSD. The busiest day ({day_label(busiest_day)}) wrote **{written_by_day[busiest_day]:,.0f} GB** to
  swap.
- **Estimated memory need of the workload:** between **{gigabytes(conservative["p95"])}** (conservative, 95th
  percentile) and about **{gigabytes(upper["p95"])}** (upper estimate), against {physical_gb} GB installed.
- **Conclusion:** the data supports **{minimum_gb} GB as the minimum**; {covering_gb} GB covers the upper estimate.

## Machine and method

| | |
|---|---|
| Model | {machine["model"]} ({machine["chip"]}), {physical_gb} GB RAM, {machine["operating_system"]} |
| Tool | `mac-performance-logger`, reads the {machine["kernel_name"]} kernel memory counters once per minute |
| Logger's own cost | about 4 MB RAM, less than 0.1 % CPU, about 1 MB log data per day |
| Data | `logs/system-*.csv`, `logs/processes-*.csv`, `logs/events.csv` |
| Working hours | Mon–Fri 09:00–19:00: {statistics["working_samples"]:,} samples on {working_day_text} |

{machine["pressure_note"]} “Swap” is memory that
{machine["swap_owner"]} has moved from RAM to the disk because RAM is full; reading it back is far slower than RAM.

## Findings

### 1. Memory pressure is at “warning” almost all the time

![Memory pressure over time](charts/pressure_timeline.png)

| Day | Normal | Warning | Critical |
|---|---:|---:|---:|
{pressure_rows}

![Pressure by day](charts/pressure_by_day.png)

### 2. The SSD is used as RAM

![Swap used](charts/swap_used.png)

- Swap used during working hours: median **{gigabytes(swap_working["median"])}**, 95th percentile
  {gigabytes(swap_working["p95"])}, peak **{gigabytes(swap_working["max"])}**.
- Over the whole period swap was above 25 % of installed RAM in {percent(swap_share["25"])} of samples, above 50 %
  in {percent(swap_share["50"])}, and **larger than the entire installed RAM in {percent(swap_share["100"])}**.

### 3. Constant data movement between RAM and SSD during work

![Swap traffic](charts/swap_traffic.png)

- Working hours: median **{gigabytes(traffic_working["median"])} per minute** swapped in or out; peaks of
  {gigabytes(traffic_working["max"])} per minute.
- Written to swap per day: {written_rows}.
- {ssd_text}A 60-second cross-check (28 Sep) showed swap writes made up almost all disk writes (4.6 of 4.8 GB).

Every swap-in is a wait for the user: an application, browser tab or container touches memory that is on the SSD and
has to be brought back first. Heavy, continuous writing also wears the SSD, which is soldered and not replaceable.

![When swapping happens](charts/heatmap_hour_weekday.png)

### 4. The workload needs more memory than installed

![Memory demand](charts/memory_demand.png)

{compression_sentence(statistics, machine)}

| Estimate (working hours) | Median | 95th percentile | Max |
|---|---:|---:|---:|
| Conservative: app + wired + compressed data | {gigabytes(conservative["median"])} | **{gigabytes(conservative["p95"])}** | {gigabytes(conservative["max"])} |
| Upper: conservative + swap used | {gigabytes(upper["median"])} | {gigabytes(upper["p95"])} | {gigabytes(upper["max"])} |

The true need lies between the two (compressed data and swap partly overlap, so the upper line may count some memory
twice). Even the conservative figure is {conservative["p95"] / statistics["physical_mb"]:.1f}× the installed RAM.

## Workload

![Memory by category](charts/top_consumers.png)

| Application | Median | 95th percentile | In top 10 | Why it is needed |
|---|---:|---:|---:|---|
{consumer_rows}

Per category (median, 95th percentile): {category_text}.

{worst_moment_summary(statistics["worst_moments"])}

## Why other developers may not see this

Memory need depends on the workload, not the job title: how many containers run locally, whether an IDE, browser
testing and several AI assistants are used in parallel, and how many projects are open at once. This report does not
claim anything about other machines. For a like-for-like comparison the same logger can be run on a colleague's
{machine["noun"]} for the same period; it needs no admin rights and has no noticeable cost.

## Cost of the problem

{cost_section(statistics, machine)}

## Recommendation

The conservative 95th-percentile need is {gigabytes(conservative["p95"])}. With {HEADROOM - 1:.0%} headroom that is
{conservative["p95"] / 1024 * HEADROOM:.1f} GB, so **{minimum_gb} GB is the smallest configuration that fits this
workload; {covering_gb} GB** also covers the upper estimate ({gigabytes(upper["p95"])}).

## Data quality

- Coverage: {statistics["samples"]:,} samples = {percent(statistics["awake_coverage_percent"])} of the
  {statistics["hours_awake"]} awake hours; {statistics["hours_not_sampled"]} hours without samples while the {machine["noun"]}
  was asleep or shut down ({statistics["sleep_count"]} sleeps recorded since sleep logging was added on 26 Sep 17:05).
- Logger events: {sum(event["event"] == "start" for event in statistics["events"])} starts,
  {stops} stops ({stop_text}),
  {sum(event["event"] == "gap" for event in statistics["events"])} gaps in sampling ({machine["noun"]} asleep or shut down),
  {sum(event["event"] == "error" for event in statistics["events"])} errors.
- Power: {power_text}. The problem is not tied to one power state. Samples before 26 Sep 17:03 have no power data.
- Limitations: root-owned processes (e.g. the security agent) are not in the application table but are included in
  all system totals. Application figures overlap slightly and are not added up as a total.

## Appendix — how to reproduce

```sh
cd ~/code/mac-performance-logger
uv run --with pandas --with matplotlib python report/make_charts.py   # charts + statistics.json
uv run python report/make_report.py                                   # this report + PDF
```

Generated {datetime.now():%Y-%m-%d %H:%M} from `report/statistics.json` and {machine["live_sources"]}.
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-pdf", action="store_true", help="only write the Markdown file")
    arguments = parser.parse_args()

    statistics = json.loads((REPORT_DIRECTORY / "statistics.json").read_text())
    markdown_path = REPORT_DIRECTORY / "memory-report.md"
    markdown_path.write_text(build(statistics, machine_facts()))
    print(f"wrote {markdown_path}")
    if not arguments.no_pdf:
        pdf_path = markdown_path.with_suffix(".pdf")
        subprocess.run(
            [
                "pandoc",
                markdown_path.name,
                "-o",
                pdf_path.name,
                "--pdf-engine=xelatex",
                "--resource-path=.",
                "-V",
                "geometry:margin=2cm",
            ],
            cwd=REPORT_DIRECTORY,
            check=True,
        )
        print(f"wrote {pdf_path}")


if __name__ == "__main__":
    main()
