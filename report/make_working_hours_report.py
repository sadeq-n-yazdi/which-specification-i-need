"""Build the working-hours-only memory and SSD report: Mon-Fri 08:30-17:30 (local time), nothing else.

Run: uv run --with pandas --with matplotlib python report/make_working_hours_report.py [--no-pdf] [--from] [--to]
Reads logs/ only. Writes report/working-hours/{charts/*.png, statistics.json, memory-report-working-hours.md/.pdf}.
Reuses helpers from make_charts.py and make_report.py; the all-hours reports are not touched.
"""

import argparse
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_charts import (  # noqa: E402
    LOG_DIRECTORY,
    PRESSURE_COLORS,
    category_of,
    page_size_bytes,
    read_logs,
    sleep_periods,
)
from make_report import (  # noqa: E402
    APPLICATION_PURPOSES,
    HEADROOM,
    day_label,
    gigabytes,
    machine_facts,
    percent,
    recommendation,
    timestamp_label,
    worst_moment_summary,
)

OUTPUT_DIRECTORY = Path(__file__).resolve().parent / "working-hours"
CHART_DIRECTORY = OUTPUT_DIRECTORY / "charts"
WORK_START_MINUTE = 8 * 60 + 30
WORK_END_MINUTE = 17 * 60 + 30
WINDOW_MINUTES = WORK_END_MINUTE - WORK_START_MINUTE
WINDOW_TEXT = "Mon–Fri 08:30–17:30"
WORKING_DAYS_PER_YEAR = 250  # assumption used only for the SSD projection
MINIMUM_COVERAGE_PERCENT = 80
SOURCE_NOTE = f"Source: mac-performance-logger, logs/system-*.csv; {WINDOW_TEXT} only"
GIGABYTE = 2**30
MEGABYTE = 2**20


def minute_of_day(timestamps: pd.Series) -> pd.Series:
    return timestamps.dt.hour * 60 + timestamps.dt.minute


def percentiles(series: pd.Series) -> dict:
    series = series.dropna()
    return {
        "median": round(float(series.median()), 1),
        "p95": round(float(series.quantile(0.95)), 1),
        "max": round(float(series.max()), 1),
    }


def pressure_share(frame: pd.DataFrame) -> dict:
    return (frame["pressure"].value_counts(normalize=True) * 100).round(1).to_dict()


def parse_duration_minutes(text: str) -> float:
    hours, minutes, seconds = (
        int(value or 0) for value in re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text).groups()
    )
    return hours * 60 + minutes + seconds / 60


def window_bounds(day: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    return day + pd.Timedelta(minutes=WORK_START_MINUTE), day + pd.Timedelta(minutes=WORK_END_MINUTE)


def overlap_minutes(start: pd.Timestamp, end: pd.Timestamp, day: pd.Timestamp) -> float:
    window_start, window_end = window_bounds(day)
    return max(0.0, (min(end, window_end) - max(start, window_start)).total_seconds() / 60)


def save_chart(figure, title: str, caption: str, filename: str, axis=None, ylabel: str = "") -> None:
    if axis is not None:
        axis.set_title(title, fontsize=15, loc="left")
        axis.set_ylabel(ylabel, fontsize=12)
        axis.grid(alpha=0.3)
    else:
        figure.suptitle(title, fontsize=15, x=0.01, ha="left")
    figure.text(0.01, 0.01, caption, fontsize=9, color="#555555")
    figure.tight_layout(rect=(0, 0.03, 1, 0.97 if axis is None else 1))
    figure.savefig(CHART_DIRECTORY / filename, dpi=100)
    plt.close(figure)


def day_panels(
    working: pd.DataFrame,
    column: str,
    scale: float,
    ylabel: str,
    title: str,
    filename: str,
    color: str,
    reference: tuple[float, str] | None = None,
) -> None:
    days = sorted(working["day"].unique())
    columns = 5
    rows = math.ceil(len(days) / columns)
    figure, axes = plt.subplots(rows, columns, figsize=(16, 3.2 * rows + 0.8), sharex=True, sharey=True, squeeze=False)
    for axis, day in zip(axes.flat, days):
        frame = working[working["day"] == day]
        values = (frame[column] / scale).where(frame["gap_minutes"] <= 3)  # break the line across gaps
        axis.plot(frame["minute_of_day"] / 60, values, color=color, linewidth=0.9)
        if reference:
            axis.axhline(reference[0], color="black", linestyle="--", linewidth=1, label=reference[1])
        axis.set_title(day_label(str(day)), fontsize=11, loc="left")
        axis.set_xlim(WORK_START_MINUTE / 60, WORK_END_MINUTE / 60)
        axis.set_xticks([8.5 + step for step in range(0, 10, 3)], [f"{int(8.5 + step)}:30" for step in range(0, 10, 3)])
        axis.grid(alpha=0.3)
    for axis in list(axes.flat)[len(days) :]:
        axis.axis("off")
    for axis in axes[:, 0]:
        axis.set_ylabel(ylabel, fontsize=11)
    if reference:
        axes.flat[0].legend(loc="upper left", fontsize=9)
    save_chart(figure, title, SOURCE_NOTE + "; x axis = time of day", filename)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-pdf", action="store_true", help="only write the Markdown file")
    parser.add_argument("--from", dest="start", help="first day to include, YYYY-MM-DD")
    parser.add_argument("--to", dest="end", help="last day to include, YYYY-MM-DD")
    arguments = parser.parse_args()
    CHART_DIRECTORY.mkdir(parents=True, exist_ok=True)

    system = read_logs("system")
    processes = read_logs("processes")
    events = pd.read_csv(LOG_DIRECTORY / "events.csv", parse_dates=["timestamp"])
    # Events stay complete: the start before the period gives each sample its boot time.
    if arguments.start:
        system, processes = (frame[frame["timestamp"] >= arguments.start] for frame in (system, processes))
    if arguments.end:
        end = pd.Timestamp(arguments.end) + pd.Timedelta(days=1)
        system, processes = (frame[frame["timestamp"] < end] for frame in (system, processes))
    system = system.reset_index(drop=True)
    page_size = page_size_bytes()
    physical_mb = int(system["physical_mb"].iloc[-1])

    system["gap_minutes"] = system["timestamp"].diff().dt.total_seconds() / 60
    interval_minutes = system["gap_minutes"].clip(lower=1 / 60)
    system["swap_traffic_mb_per_minute"] = (
        (system["swapins"] + system["swapouts"]) * page_size / MEGABYTE / interval_minutes
    )
    system["swap_write_mb_per_minute"] = system["swapouts"] * page_size / MEGABYTE / interval_minutes
    system["swap_written_gb"] = system["swapouts"] * page_size / GIGABYTE
    system["swap_read_gb"] = system["swapins"] * page_size / GIGABYTE
    system["conservative_demand_mb"] = system["app_mb"] + system["wired_mb"] + system["compressor_stored_mb"]
    system["upper_demand_mb"] = system["conservative_demand_mb"] + system["swap_used_mb"]
    system["day"] = system["timestamp"].dt.date
    system["minute_of_day"] = minute_of_day(system["timestamp"])
    in_window = (
        (system["timestamp"].dt.weekday < 5)
        & (system["minute_of_day"] >= WORK_START_MINUTE)
        & (system["minute_of_day"] < WORK_END_MINUTE)
    )
    starts = events.loc[events["event"] == "start", ["timestamp", "boot_time"]]
    system = pd.merge_asof(system, starts, on="timestamp", direction="backward")
    system["boot_time"] = system["boot_time"].fillna(starts["boot_time"].iloc[0])
    working = system[in_window].copy()
    working_timestamps = set(working["timestamp"])
    days = sorted(working["day"].unique())

    # Step 1: data quality inside the window only.
    sleeps = sleep_periods(events)
    quality_rows = []
    for day in days:
        day_start = pd.Timestamp(day)
        asleep = sum(overlap_minutes(slept, woke, day_start) for slept, woke in sleeps)
        samples = int((working["day"] == day).sum())
        awake_expected = WINDOW_MINUTES - asleep
        quality_rows.append(
            {
                "day": str(day),
                "samples": samples,
                "expected": WINDOW_MINUTES,
                "asleep_minutes": round(asleep, 1),
                "other_missing_minutes": round(max(0.0, awake_expected - samples), 1),
                "awake_coverage_percent": round(min(100.0, samples / awake_expected * 100), 1),
            }
        )
    total_samples = len(working)
    total_asleep = sum(row["asleep_minutes"] for row in quality_rows)
    awake_coverage = min(100.0, total_samples / (WINDOW_MINUTES * len(days) - total_asleep) * 100)
    if awake_coverage < MINIMUM_COVERAGE_PERCENT:
        sys.exit(f"Working-hour coverage is only {awake_coverage:.1f} % (< {MINIMUM_COVERAGE_PERCENT} %): stopping.")
    window_events = []
    for _, event in events[events["event"].isin(["start", "stop", "gap", "error"])].iterrows():
        if event["event"] == "gap":
            gap_start = event["timestamp"] - pd.Timedelta(
                minutes=parse_duration_minutes(event["details"].split("for ")[1])
            )
            minutes_in_window = sum(overlap_minutes(gap_start, event["timestamp"], pd.Timestamp(day)) for day in days)
        else:
            minutes_in_window = 0.0
            day = pd.Timestamp(event["timestamp"].date())
            start, end = window_bounds(day)
            if day.date() in days and start <= event["timestamp"] < end:
                minutes_in_window = 1.0
        if minutes_in_window > 0:
            window_events.append(
                {
                    "timestamp": event["timestamp"].strftime("%Y-%m-%dT%H:%M:%S"),
                    "event": event["event"],
                    "details": event["details"],
                    "minutes_in_window": round(minutes_in_window, 1),
                }
            )

    # Step 2: analysis.
    by_day = working.groupby("day")
    boot_periods = []
    for boot_time, frame in working.groupby("boot_time"):
        window_days = len(frame) / WINDOW_MINUTES
        written = float(frame["swap_written_gb"].sum())
        boot_periods.append(
            {
                "boot_time": boot_time,
                "days": sorted({str(day) for day in frame["day"]}),
                "window_days_equivalent": round(window_days, 2),
                "swap_written_gb": round(written, 1),
                "swap_written_gb_per_window_day": round(written / window_days, 1),
                "swap_used_median_mb": round(float(frame["swap_used_mb"].median()), 0),
                "warning_or_critical_percent": round(float(frame["pressure"].ne("normal").mean() * 100), 1),
            }
        )
    written_per_day = by_day["swap_written_gb"].sum()
    mean_written_per_day = float(written_per_day.mean())
    ssd_by_day = {
        str(day): {
            "swap_written_gb": round(float(frame["swap_written_gb"].sum()), 1),
            "swap_read_gb": round(float(frame["swap_read_gb"].sum()), 1),
            "minutes_over_1gb_per_minute": int((frame["swap_write_mb_per_minute"] >= 1024).sum()),
            "average_write_mb_per_second": round(
                float(frame["swap_written_gb"].sum() * 1024 / (WINDOW_MINUTES * 60)), 1
            ),
        }
        for day, frame in by_day
    }
    machine = machine_facts()
    boot = machine["boot_time"]
    since_boot = system[system["timestamp"] >= boot]
    disk_written_bytes = machine["ssd_written_tb"] * 1e12
    swap_since_boot_bytes = float(since_boot["swapouts"].sum()) * page_size
    category_per_sample = (
        processes[processes["timestamp"].isin(working_timestamps)]
        .assign(category=lambda frame: frame["name"].map(category_of))
        .groupby(["timestamp", "category"])["memory_mb"]
        .sum()
        .unstack(fill_value=0)
    )
    working_processes = processes[processes["timestamp"].isin(working_timestamps)]
    consumers = (
        working_processes.groupby("name")["memory_mb"]
        .agg(["median", lambda values: values.quantile(0.95), "count"])
        .set_axis(["median_mb", "p95_mb", "samples_in_top"], axis=1)
        .sort_values("median_mb", ascending=False)
        .head(12)
    )
    consumers = consumers[consumers["samples_in_top"] >= 0.05 * working_processes["timestamp"].nunique()]
    consumers["share_of_samples_percent"] = (
        consumers["samples_in_top"] / working_processes["timestamp"].nunique() * 100
    ).round(1)
    worst_moments = []
    for _, row in working.nlargest(5, "swap_traffic_mb_per_minute").iterrows():
        at_sample = processes[processes["timestamp"] == row["timestamp"]].nsmallest(5, "rank")
        worst_moments.append(
            {
                "timestamp": row["timestamp"].isoformat(),
                "pressure": row["pressure"],
                "swap_used_mb": int(row["swap_used_mb"]),
                "swap_traffic_mb_per_minute": round(float(row["swap_traffic_mb_per_minute"]), 1),
                "interval_minutes": round(float(row["gap_minutes"]), 1),
                "top_applications": [f"{item.name} ({item.memory_mb} MB)" for item in at_sample.itertuples()],
            }
        )
    statistics = {
        "days": [str(day) for day in days],
        "physical_mb": physical_mb,
        "page_size_bytes": page_size,
        "samples": total_samples,
        "awake_coverage_percent": round(awake_coverage, 1),
        "quality_by_day": quality_rows,
        "window_events": window_events,
        "boot_times": sorted(working["boot_time"].unique().tolist()),
        "pressure_percent": pressure_share(working),
        "pressure_percent_by_day": {str(day): pressure_share(frame) for day, frame in by_day},
        "swap_used_mb": percentiles(working["swap_used_mb"]),
        "swap_used_over_percent_of_ram": {
            str(share): round(float((working["swap_used_mb"] > physical_mb * share / 100).mean() * 100), 1)
            for share in (25, 50, 100)
        },
        "swap_traffic_mb_per_minute": percentiles(working["swap_traffic_mb_per_minute"]),
        "compressor_stored_mb": percentiles(working["compressor_stored_mb"]),
        "compressor_occupied_mb": percentiles(working["compressor_occupied_mb"]),
        "compression_mb_per_minute": percentiles(
            (working["compressions"] + working["decompressions"]) * page_size / MEGABYTE / interval_minutes[in_window]
        ),
        "demand_conservative_mb": percentiles(working["conservative_demand_mb"]),
        "demand_upper_mb": percentiles(working["upper_demand_mb"]),
        "demand_by_day": {
            str(day): {
                "conservative_median_mb": round(float(frame["conservative_demand_mb"].median()), 0),
                "conservative_p95_mb": round(float(frame["conservative_demand_mb"].quantile(0.95)), 0),
            }
            for day, frame in by_day
        },
        "category_mb": {
            category: percentiles(category_per_sample[category]) for category in category_per_sample.columns
        },
        "top_consumers": consumers.round(0).reset_index().to_dict(orient="records"),
        "power": {
            source: {
                "samples": int(len(frame)),
                "pressure_percent": pressure_share(frame),
                "swap_traffic_mb_per_minute": percentiles(frame["swap_traffic_mb_per_minute"]),
            }
            for source, frame in working.dropna(subset=["power_source"]).groupby("power_source")
        },
        "worst_moments": worst_moments,
        "ssd": {
            "by_day": ssd_by_day,
            "total_swap_written_gb": round(float(working["swap_written_gb"].sum()), 1),
            "mean_swap_written_gb_per_day": round(mean_written_per_day, 1),
            "boot_periods": boot_periods,
            "since_boot_boot_time": boot.isoformat(),
        },
    }
    # The SSD counter resets at boot; it can only be compared with the logs when samples exist since that boot.
    if len(since_boot):
        statistics["ssd"] |= {
            "since_boot_swap_written_gb": round(swap_since_boot_bytes / GIGABYTE, 1),
            "since_boot_disk_written_gb": round(disk_written_bytes / GIGABYTE, 1),
            "since_boot_swap_share_of_disk_writes_percent": round(swap_since_boot_bytes / disk_written_bytes * 100, 1),
            "since_boot_last_sample": since_boot["timestamp"].max().isoformat(),
        }
    (OUTPUT_DIRECTORY / "statistics.json").write_text(json.dumps(statistics, indent=2, default=str))

    # Step 3: charts.
    figure, axis = plt.subplots(figsize=(16, 5))
    pressure_by_day = pd.DataFrame(statistics["pressure_percent_by_day"]).T.fillna(0)
    pressure_by_day = pressure_by_day.reindex(
        columns=[name for name in PRESSURE_COLORS if name in pressure_by_day.columns]
    )
    pressure_by_day.index = [day_label(day) for day in pressure_by_day.index]
    pressure_by_day.plot.bar(
        stacked=True, ax=axis, color=[PRESSURE_COLORS[name] for name in pressure_by_day.columns], rot=0
    )
    save_chart(
        figure,
        f"Memory pressure per working day ({WINDOW_TEXT})",
        SOURCE_NOTE,
        "pressure_by_day.png",
        axis,
        "% of samples",
    )

    day_panels(
        working,
        "swap_used_mb",
        1024,
        "Swap used (GB)",
        "Swap used per working day, compared with installed RAM",
        "swap_used_by_day.png",
        "#6a1b9a",
        (physical_mb / 1024, f"Installed RAM ({physical_mb // 1024} GB)"),
    )
    day_panels(
        working,
        "swap_write_mb_per_minute",
        1,
        "MB written to swap per minute",
        "Data written to the SSD as swap, per working day",
        "swap_writes_by_day.png",
        "#c62828",
    )

    figure, axis = plt.subplots(figsize=(16, 5))
    demand = pd.DataFrame(statistics["demand_by_day"]).T
    positions = range(len(demand))
    axis.bar(
        [p - 0.2 for p in positions],
        demand["conservative_median_mb"] / 1024,
        width=0.4,
        color="#1565c0",
        label="Median",
    )
    axis.bar(
        [p + 0.2 for p in positions],
        demand["conservative_p95_mb"] / 1024,
        width=0.4,
        color="#90caf9",
        label="95th percentile",
    )
    axis.axhline(physical_mb / 1024, color="black", linestyle="--", label=f"Installed RAM ({physical_mb // 1024} GB)")
    axis.set_xticks(list(positions), [day_label(day) for day in demand.index])
    axis.legend(loc="upper right")
    save_chart(
        figure,
        "Estimated memory demand per working day (app + wired + compressed data)",
        SOURCE_NOTE + "; demand is an estimate, see report",
        "memory_demand_by_day.png",
        axis,
        "GB",
    )

    figure, axis = plt.subplots(figsize=(16, 5))
    boot_colors = dict(zip(statistics["boot_times"], ["#c62828", "#ef6c00", "#1565c0", "#2e7d32"]))
    for day, row in ssd_by_day.items():
        boot_of_day = working[working["day"].astype(str) == day]["boot_time"].mode().iloc[0]
        axis.bar(day_label(day), row["swap_written_gb"], color=boot_colors[boot_of_day])
        axis.text(
            day_label(day),
            row["swap_written_gb"],
            f"{row['swap_written_gb']:,.0f}",
            ha="center",
            va="bottom",
            fontsize=10,
        )
    handles = [plt.Rectangle((0, 0), 1, 1, color=color) for color in boot_colors.values()]
    axis.legend(
        handles, [f"since boot of {timestamp_label(boot_time)}" for boot_time in boot_colors], loc="upper right"
    )
    save_chart(
        figure,
        "Data written to the SSD as swap during working hours, per day",
        SOURCE_NOTE + "; swapouts x page size; colour = boot (reboot) the day mostly belongs to",
        "swap_written_per_day.png",
        axis,
        "GB written",
    )

    figure, axis = plt.subplots(figsize=(16, 5))
    heat = working.assign(
        slot=(working["minute_of_day"] - WORK_START_MINUTE) // 30, weekday=working["timestamp"].dt.weekday
    )
    heatmap = heat.pivot_table(index="weekday", columns="slot", values="swap_traffic_mb_per_minute", aggfunc="median")
    heatmap = heatmap.reindex(index=range(5), columns=range(WINDOW_MINUTES // 30))
    image = axis.imshow(heatmap, aspect="auto", cmap="Reds")
    axis.set_yticks(range(5), ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])
    labels = [
        (datetime(2000, 1, 1) + timedelta(minutes=WORK_START_MINUTE + 30 * slot)).strftime("%H:%M")
        for slot in heatmap.columns
    ]
    axis.set_xticks(range(len(labels)), labels, rotation=45)
    figure.colorbar(image, ax=axis, label="Median swap MB per minute (in + out)")
    save_chart(
        figure,
        "When swapping happens (30-minute slots, blank = no data)",
        SOURCE_NOTE,
        "heatmap_slot_weekday.png",
        axis,
    )

    figure, axis = plt.subplots(figsize=(16, 5))
    category_table = pd.DataFrame(statistics["category_mb"]).T.sort_values("median")
    axis.barh(category_table.index, category_table["p95"] / 1024, color="#90caf9", label="95th percentile")
    axis.barh(category_table.index, category_table["median"] / 1024, color="#1565c0", label="Median")
    axis.legend(loc="lower right")
    axis.set_xlabel("GB", fontsize=12)
    save_chart(
        figure,
        "Memory by application category (top applications per sample)",
        "Source: logs/processes-*.csv; working hours only; root-owned processes excluded",
        "top_consumers.png",
        axis,
    )

    # Step 4: report.
    build_report(statistics, machine, page_size)
    if not arguments.no_pdf:
        subprocess.run(
            [
                "pandoc",
                "memory-report-working-hours.md",
                "-o",
                "memory-report-working-hours.pdf",
                "--pdf-engine=xelatex",
                "--resource-path=.",
                "-V",
                "geometry:margin=2cm",
            ],
            cwd=OUTPUT_DIRECTORY,
            check=True,
        )
    print(
        json.dumps(
            {"days": statistics["days"], "samples": total_samples, "awake_coverage_percent": round(awake_coverage, 1)}
        )
    )


def build_report(statistics: dict, machine: dict, page_size: int) -> None:
    physical_gb = statistics["physical_mb"] // 1024
    days = statistics["days"]
    pressure = statistics["pressure_percent"]
    swap = statistics["swap_used_mb"]
    traffic = statistics["swap_traffic_mb_per_minute"]
    conservative, upper = statistics["demand_conservative_mb"], statistics["demand_upper_mb"]
    minimum_gb, covering_gb = recommendation(conservative["p95"], upper["p95"])
    ssd = statistics["ssd"]
    share = statistics["swap_used_over_percent_of_ram"]
    boot_periods = ssd["boot_periods"]
    day_range = f"{day_label(days[0])} – {day_label(days[-1])}"
    busiest_day = max(ssd["by_day"], key=lambda day: ssd["by_day"][day]["swap_written_gb"])
    mean_per_day = ssd["mean_swap_written_gb_per_day"]
    daily_gb = {day: row["swap_written_gb"] for day, row in ssd["by_day"].items()}
    lightest_gb, heaviest_gb = min(daily_gb.values()), max(daily_gb.values())
    heavy_days = sum(value > 400 for value in daily_gb.values())
    last_boot = boot_periods[-1]
    later_mean = last_boot["swap_written_gb_per_window_day"]

    def terabytes_per_year(gigabytes_per_day: float) -> float:
        return gigabytes_per_day * WORKING_DAYS_PER_YEAR / 1024

    def years_per_100_terabytes(gigabytes_per_day: float) -> str:
        per_year = terabytes_per_year(gigabytes_per_day)
        return f"{100 / per_year:.1f}" if per_year else "–"

    pressure_rows = "\n".join(
        f"| {day_label(day)} | {percent(shares.get('normal', 0))} | {percent(shares.get('warning', 0))} | "
        f"{percent(shares.get('critical', 0))} |"
        for day, shares in statistics["pressure_percent_by_day"].items()
    )
    quality_rows = "\n".join(
        f"| {day_label(row['day'])} | {row['samples']} | {row['asleep_minutes']:.0f} | {row['other_missing_minutes']:.0f} | "
        f"{percent(row['awake_coverage_percent'])} |"
        for row in statistics["quality_by_day"]
    )
    event_rows = (
        "\n".join(
            f"| {timestamp_label(event['timestamp'])} | {event['event']} | {event['details'][:60]} | {event['minutes_in_window']:.0f} |"
            for event in statistics["window_events"]
        )
        or "| – | – | none inside the working window | – |"
    )
    ssd_rows = "\n".join(
        f"| {day_label(day)} | {row['swap_written_gb']:,.0f} GB | {row['swap_read_gb']:,.0f} GB | "
        f"{row['average_write_mb_per_second']:,.0f} MB/s | {row['minutes_over_1gb_per_minute']} |"
        for day, row in ssd["by_day"].items()
    )
    lifetime_rows = "\n".join(
        f"| Boot of {timestamp_label(period['boot_time'])} ({day_label(period['days'][0])} – {day_label(period['days'][-1])}) | "
        f"{period['window_days_equivalent']:.1f} | {period['swap_written_gb_per_window_day']:,.0f} GB | "
        f"{terabytes_per_year(period['swap_written_gb_per_window_day']):,.0f} TB | "
        f"{years_per_100_terabytes(period['swap_written_gb_per_window_day'])} years |"
        for period in boot_periods
    )
    consumer_rows = "\n".join(
        f"| {APPLICATION_PURPOSES.get(row['name'], (row['name'], 'other'))[0]} | {gigabytes(row['median_mb'])} | "
        f"{gigabytes(row['p95_mb'])} | {row['share_of_samples_percent']:.0f} % | "
        f"{APPLICATION_PURPOSES.get(row['name'], ('', 'other'))[1]} |"
        for row in statistics["top_consumers"][:8]
    )
    category_text = ", ".join(
        f"{name} {gigabytes(values['median'])} (up to {gigabytes(values['p95'])})"
        for name, values in sorted(statistics["category_mb"].items(), key=lambda item: -item[1]["p95"])
        if name != "Other"
    )
    power_text = "; ".join(
        f"{source.upper() if source == 'ac' else source}: {values['samples']:,} samples, “warning” in "
        f"{percent(values['pressure_percent'].get('warning', 0))}, median {values['swap_traffic_mb_per_minute']['median']:,.0f} MB/min"
        for source, values in statistics["power"].items()
    )
    worst_rows = "\n".join(
        f"| {timestamp_label(moment['timestamp'])} | {moment['pressure']} | {gigabytes(moment['swap_used_mb'])} | "
        f"{gigabytes(moment['swap_traffic_mb_per_minute'])}/min over {moment['interval_minutes']:.0f} min | "
        f"{', '.join(moment['top_applications'][:3])} |"
        for moment in statistics["worst_moments"]
    )
    cross_check = (
        f"The {machine['noun']} was booted on {timestamp_label(ssd['since_boot_boot_time'])}, after the last logged "
        "sample, so the SSD's own write counter (which resets at boot) cannot be compared with the logs."
        if "since_boot_last_sample" not in ssd
        else f"Since the last boot ({timestamp_label(ssd['since_boot_boot_time'])}, until the last logged sample "
        f"{timestamp_label(ssd['since_boot_last_sample'])}) the logs record **{ssd['since_boot_swap_written_gb'] / 1024:.2f} TB "
        f"written to swap**, while the SSD's own counter (`ioreg`, “Bytes (Write)”, read when this report was generated) "
        f"shows **{ssd['since_boot_disk_written_gb'] / 1024:.2f} TB written in total**. Swap is therefore "
        f"**{percent(ssd['since_boot_swap_share_of_disk_writes_percent'])}** of everything written to the disk since boot "
        "(all hours, not only working hours; the two readings differ by the minutes between the last sample and now)."
    )
    swap_share_text = (
        f"; swap is {percent(ssd['since_boot_swap_share_of_disk_writes_percent'])} of all disk writes since the last boot"
        if "since_boot_swap_share_of_disk_writes_percent" in ssd
        else ""
    )
    reboot_split_text = (
        f", with {heavy_days} very heavy days before the reboots and about {later_mean:,.0f} GB per day since the last boot"
        if len(boot_periods) > 1
        else ""
    )
    markdown = f"""# Memory and SSD usage during working hours — {machine["title"]} ({machine["chip_short"]}, {physical_gb} GB)

*Only {WINDOW_TEXT} (local time) is analysed: {len(days)} working days, {statistics["samples"]:,} one-minute samples.
Evenings, nights and weekends are excluded.*

## Summary

- **Period:** {day_range} ({len(days)} working days); {statistics["samples"]:,} samples = {percent(statistics["awake_coverage_percent"])} of the awake
  time in the window. {len(boot_periods) - 1} reboot(s) happened during these days.
- **Memory pressure:** {machine["pressure_source"]} reported **warning** in **{percent(pressure.get("warning", 0))}** and “normal” in only
  {percent(pressure.get("normal", 0))} of working-hour samples.
- **Swap:** median **{gigabytes(swap["median"])}** on the SSD (95th percentile {gigabytes(swap["p95"])}), against
  {physical_gb} GB installed RAM; median **{gigabytes(traffic["median"])}/min** moved between RAM and SSD.
- **SSD:** **{mean_per_day:,.0f} GB per working day** written to swap on average (range
  {lightest_gb:,.0f}–{heaviest_gb:,.0f} GB{reboot_split_text}){swap_share_text}.
- **Estimated memory need:** **{gigabytes(conservative["p95"])}** (conservative, 95th percentile) to
  about **{gigabytes(upper["p95"])}** (upper estimate). Conclusion: **{minimum_gb} GB is the minimum**; {covering_gb} GB
  covers the upper estimate.

## Machine and method

| | |
|---|---|
| Model | {machine["model"]} ({machine["chip"]}), {physical_gb} GB RAM, {machine["operating_system"]} |
| Tool | `mac-performance-logger`, reads {machine["kernel_name"]} kernel memory counters once per minute (about 4 MB RAM, negligible CPU) |
| Window | {WINDOW_TEXT}, 08:30 included, 17:30 excluded; 540 expected samples per day |
| Counters | swap in/out are pages per interval × {page_size} bytes; rates divide by the real time since the previous sample |

{machine["pressure_note"]} “Swap” is memory that {machine["swap_owner"]} has moved from
RAM to the disk because RAM is full.

## Findings

### 1. Memory pressure during work

| Day | Normal | Warning | Critical |
|---|---:|---:|---:|
{pressure_rows}

![Pressure by day](charts/pressure_by_day.png)

### 2. The SSD is used as RAM

Swap used: median **{gigabytes(swap["median"])}**, 95th percentile {gigabytes(swap["p95"])}, peak
**{gigabytes(swap["max"])}**. It was above 25 % of installed RAM in {percent(share["25"])} of working samples, above 50 %
in {percent(share["50"])}, and larger than the whole installed RAM in **{percent(share["100"])}**.

![Swap used per day](charts/swap_used_by_day.png)

### 3. Constant data movement between RAM and SSD

Swap traffic (in + out): median {gigabytes(traffic["median"])}/min, 95th percentile {gigabytes(traffic["p95"])}/min,
peak {gigabytes(traffic["max"])}/min. Every swap-in is a wait: an application touches memory that is on the SSD and
must wait for it to be read back. Compression adds CPU cost: a median {gigabytes(statistics["compressor_stored_mb"]["median"])}
of data was squeezed into {gigabytes(statistics["compressor_occupied_mb"]["median"])} of RAM, while
a median {gigabytes(statistics["compression_mb_per_minute"]["median"])} per minute was compressed or decompressed.

![When swapping happens](charts/heatmap_slot_weekday.png)

### 4. The workload needs more memory than installed

Estimate, working hours (an estimate, not a measurement):

| | Median | 95th percentile | Max |
|---|---:|---:|---:|
| Conservative: app + wired + compressed data | {gigabytes(conservative["median"])} | **{gigabytes(conservative["p95"])}** | {gigabytes(conservative["max"])} |
| Upper: conservative + swap used | {gigabytes(upper["median"])} | {gigabytes(upper["p95"])} | {gigabytes(upper["max"])} |

Compressed data and swap partly overlap, so the true need lies between the two. Even the conservative 95th percentile is
{conservative["p95"] / statistics["physical_mb"]:.1f}× the installed RAM.

![Estimated demand per day](charts/memory_demand_by_day.png)

## SSD usage caused by swap, and what it means for the {machine["noun"]}'s lifetime

{machine["storage_note"]}
Flash memory wears with every byte **written**; reads (swap-ins) do not wear it. Swap-outs are writes.

![Swap written per day](charts/swap_written_per_day.png)

| Day | Written to swap | Read back from swap | Average write rate over the 9 h | Minutes with 1 GB/min or more written |
|---|---:|---:|---:|---:|
{ssd_rows}

- During the {len(days)} working days **{ssd["total_swap_written_gb"] / 1024:.1f} TB** was written to swap inside the window
  ({mean_per_day:,.0f} GB per day on average; heaviest day {day_label(busiest_day)}, {ssd["by_day"][busiest_day]["swap_written_gb"]:,.0f} GB).
- {cross_check}
- The write volume was not constant: the heaviest day wrote {heaviest_gb / lightest_gb:.0f}× the lightest. Days with more
  than 400 GB all fall before the reboot of {timestamp_label(boot_periods[1]["boot_time"]) if len(boot_periods) > 1 else "–"};
  the logs show the volume, not the reason. The table below splits the days by boot period.

![Swap writes per day](charts/swap_writes_by_day.png)

### Effect on lifetime

The logs measure how much is written; they **cannot** tell how much the SSD can take. {machine["vendor"]} does not publish an endurance
rating (TBW) for this SSD, and the real wear indicator (“Percentage Used” in the drive's SMART data) is not read by the
logger. Two facts limit what can be concluded:

1. The data written to flash is at least the data the operating system writes (write amplification is unknown and
   normally above 1), so the figures below are a **lower bound** for wear.
2. Only working hours are counted here; swap writes at other times add to it.

The table converts the measured rate into a yearly volume (assuming {WORKING_DAYS_PER_YEAR} working days per year, same
workload) and into how long it takes to use up each **100 TB of endurance rating**. Multiply or divide by the actual rating
once it is known; no rating is assumed.

| Measured period | Working-day equivalents | Swap written per working day | Per year at that rate | Years to consume 100 TB of rating |
|---|---:|---:|---:|---:|
{lifetime_rows}
| All {len(days)} days together | {len(days)} | {mean_per_day:,.0f} GB | {terabytes_per_year(mean_per_day):,.0f} TB | {years_per_100_terabytes(mean_per_day)} years |

How to read it: at the measured rates the swap alone writes the amounts in the “per year” column; the shorter the “years to
consume 100 TB” figure, the faster the drive would reach a given rating. To replace the estimate with a measurement, read the SSD's own “Percentage Used” and “Data Units
Written” values (SMART, e.g. `smartctl -a` from smartmontools) now and again in a few
months.

## Workload

| Application | Median | 95th percentile | In top 10 | Why it is needed |
|---|---:|---:|---:|---|
{consumer_rows}

Per category (median, 95th percentile): {category_text}. Application figures overlap slightly and are never added up as
total RAM. Root-owned processes (e.g. the security agent) are not in this table but are counted in all system totals.

![Memory by category](charts/top_consumers.png)

Five highest swap-traffic intervals in the window:

| Time | Pressure | Swap used | Swap traffic | Largest applications at that sample |
|---|---|---:|---:|---|
{worst_rows}

{worst_moment_summary(statistics["worst_moments"])}

Power: {power_text}. The problem is not tied to one power state.

## Why other developers may not see this

Memory need depends on the workload, not the job title: how many containers run locally, whether an IDE, browser testing
and several AI assistants run in parallel, and how many projects are open. This report makes no claim about other
machines. The same logger can be run on a colleague's {machine["noun"]} for the same period for a like-for-like comparison.

## Cost of the problem

> **Placeholder — to be filled with real measurements.** Time `make test`, a Docker build and IDE indexing (a) right after
> a reboot and (b) during normal work.

| Task | After reboot | Normal load | Difference |
|---|---:|---:|---:|
| `make test` | _tbd_ | _tbd_ | _tbd_ |
| Docker build | _tbd_ | _tbd_ | _tbd_ |
| IDE indexing | _tbd_ | _tbd_ | _tbd_ |

## Recommendation

The conservative 95th-percentile need is {gigabytes(conservative["p95"])}; with {HEADROOM - 1:.0%} headroom that is
{conservative["p95"] / 1024 * HEADROOM:.1f} GB, so **{minimum_gb} GB is the smallest configuration that fits this
workload; {covering_gb} GB** also covers the upper estimate ({gigabytes(upper["p95"])}). The swap writing in the SSD
section exists because the estimated demand is above the installed RAM (findings 2 and 4).

## Data quality

Coverage is counted inside the window only. Time asleep (from `sleep` events in `logs/events.csv`, recorded since
26 Sep 17:05) is not missing data.

| Day | Samples (of 540) | Asleep (min) | Other missing (min) | Coverage of awake time |
|---|---:|---:|---:|---:|
{quality_rows}

Logger events inside the window (`logs/events.csv`; “gap” = no samples, minutes counted inside the window only):

| Time | Event | Details | Minutes in window |
|---|---|---|---:|
{event_rows}

The counter columns are empty on the first row after each logger start and are treated as missing. Readings after a gap
cover the whole gap, so their per-minute rate is diluted, while their volume is included in the written-GB totals.
Samples before 26 Sep 17:03 have no power data (not used here).

## Appendix — how to reproduce

```sh
cd ~/code/mac-performance-logger
uv run --with pandas --with matplotlib python report/make_working_hours_report.py
```

Inputs: `logs/system-*.csv`, `logs/processes-*.csv`, `logs/events.csv`; live sources: the page size of this machine and
{machine["live_sources"]}. Outputs: `report/working-hours/`. Generated {datetime.now():%Y-%m-%d %H:%M}.
"""
    (OUTPUT_DIRECTORY / "memory-report-working-hours.md").write_text(markdown)


if __name__ == "__main__":
    main()
