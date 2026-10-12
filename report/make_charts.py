"""Build the charts and summary statistics for the memory report from the logs/ directory.

Run: uv run --with pandas --with matplotlib python report/make_charts.py [--from YYYY-MM-DD] [--to YYYY-MM-DD]
Writes report/charts/*.png and report/statistics.json; reads nothing outside logs/.
"""

import argparse
import glob
import json
import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

PROJECT_DIRECTORY = Path(__file__).resolve().parent.parent
LOG_DIRECTORY = PROJECT_DIRECTORY / "logs"
REPORT_DIRECTORY = PROJECT_DIRECTORY / "report"
CHART_DIRECTORY = REPORT_DIRECTORY / "charts"
COUNTER_COLUMNS = ["swapins", "swapouts", "pageins", "pageouts", "compressions", "decompressions"]
PRESSURE_COLORS = {"normal": "#4caf50", "warning": "#f5b800", "critical": "#e53935"}
WORK_START_HOUR, WORK_END_HOUR = 9, 19
SOURCE_NOTE = "Source: mac-performance-logger, logs/system-*.csv"
CATEGORIES = [
    ("Browser", ("Google Chrome", "Safari", "firefox", "Arc")),
    ("Containers / VM", ("com.apple.Virtualization", "limactl", "qemu", "docker", "Rancher")),
    ("IDE / editor", ("pycharm", "PyCharm", "Code", "idea", "vim")),
    ("AI tools", ("claude", "node")),
    ("Terminal / shell", ("iTerm", "Terminal", "zsh", "fish", "tmux")),
    ("Database", ("postgres",)),
]


def page_size_bytes() -> int:
    return int(subprocess.run(["sysctl", "-n", "hw.pagesize"], capture_output=True, text=True, check=True).stdout)


def read_logs(prefix: str) -> pd.DataFrame:
    # Includes *-before-HHMMSS.csv files written before a column layout change; their missing columns become NaN.
    files = sorted(glob.glob(str(LOG_DIRECTORY / f"{prefix}-*.csv")))
    frame = pd.concat([pd.read_csv(file) for file in files], ignore_index=True)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    return frame.sort_values("timestamp").reset_index(drop=True)


def is_working_time(timestamps: pd.Series) -> pd.Series:
    return (timestamps.dt.weekday < 5) & timestamps.dt.hour.between(WORK_START_HOUR, WORK_END_HOUR - 1)


def category_of(name: str) -> str:
    for category, markers in CATEGORIES:
        if any(marker.lower() in name.lower() for marker in markers):
            return category
    return "Other"


def sleep_periods(events: pd.DataFrame) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    periods = []
    for details in events.loc[events["event"] == "sleep", "details"]:
        parts = dict(part.strip().split(" ", 1) for part in details.split(",") if " " in part.strip())
        periods.append((pd.Timestamp(parts["slept"]), pd.Timestamp(parts["woke"])))
    return periods


def shade(axis, system: pd.DataFrame, sleeps) -> None:
    for start, end in sleeps:
        axis.axvspan(start, end, color="grey", alpha=0.35, linewidth=0)
    on_battery = system["power_source"].eq("battery")
    for _, block in system[on_battery].groupby((~on_battery).cumsum()[on_battery]):
        axis.axvspan(block["timestamp"].iloc[0], block["timestamp"].iloc[-1], color="#90caf9", alpha=0.25, linewidth=0)


def finish(figure, axis, title: str, ylabel: str, filename: str, caption: str = SOURCE_NOTE) -> None:
    axis.set_title(title, fontsize=15, loc="left")
    axis.set_ylabel(ylabel, fontsize=12)
    axis.grid(alpha=0.3)
    figure.text(0.01, 0.01, caption, fontsize=9, color="#555555")
    figure.tight_layout(rect=(0, 0.03, 1, 1))
    figure.savefig(CHART_DIRECTORY / filename, dpi=100)
    plt.close(figure)


def new_figure(height: float = 6):
    return plt.subplots(figsize=(16, height))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="start", help="first day to include, YYYY-MM-DD")
    parser.add_argument("--to", dest="end", help="last day to include, YYYY-MM-DD")
    arguments = parser.parse_args()
    CHART_DIRECTORY.mkdir(parents=True, exist_ok=True)

    system = read_logs("system")
    processes = read_logs("processes")
    events = pd.read_csv(LOG_DIRECTORY / "events.csv", parse_dates=["timestamp"])
    if arguments.start:
        system, processes = (frame[frame["timestamp"] >= arguments.start] for frame in (system, processes))
    if arguments.end:
        end = pd.Timestamp(arguments.end) + pd.Timedelta(days=1)
        system, processes = (frame[frame["timestamp"] < end] for frame in (system, processes))

    page_size = page_size_bytes()
    physical_mb = int(system["physical_mb"].iloc[-1])
    interval_minutes = system["timestamp"].diff().dt.total_seconds().div(60).clip(lower=1 / 60)
    swap_pages = system["swapins"] + system["swapouts"]
    system["swap_traffic_mb_per_minute"] = swap_pages * page_size / 2**20 / interval_minutes
    system["estimated_demand_mb"] = (
        system["app_mb"] + system["wired_mb"] + system["compressor_stored_mb"] + system["swap_used_mb"]
    )
    system["working"] = is_working_time(system["timestamp"])
    processes["category"] = processes["name"].map(category_of)
    sleeps = sleep_periods(events)

    # Statistics.
    def pressure_share(frame: pd.DataFrame) -> dict:
        return (frame["pressure"].value_counts(normalize=True) * 100).round(1).to_dict()

    def percentiles(series: pd.Series) -> dict:
        series = series.dropna()
        return {
            "median": round(float(series.median()), 1),
            "p95": round(float(series.quantile(0.95)), 1),
            "max": round(float(series.max()), 1),
        }

    first, last = system["timestamp"].min(), system["timestamp"].max()
    asleep_seconds = sum(
        (min(end, last) - max(start, first)).total_seconds() for start, end in sleeps if end > first and start < last
    )
    # Sleep events are missing before the logger recorded them, so time without samples is taken from the samples
    # themselves: any gap longer than three intervals counts as not awake.
    sample_interval = system["timestamp"].diff().median()
    gaps = system["timestamp"].diff()
    not_sampled_seconds = gaps[gaps > 3 * sample_interval].sum().total_seconds()
    awake_minutes = ((last - first).total_seconds() - not_sampled_seconds) / 60
    expected_samples = awake_minutes / (sample_interval.total_seconds() / 60)
    swap_written_gb = (system.groupby(system["timestamp"].dt.date)["swapouts"].sum() * page_size / 2**30).round(2)
    top_samples = system.nlargest(5, "swap_traffic_mb_per_minute")
    worst_moments = []
    for _, row in top_samples.iterrows():
        at_sample = processes[processes["timestamp"] == row["timestamp"]].nsmallest(5, "rank")
        worst_moments.append(
            {
                "timestamp": row["timestamp"].isoformat(),
                "pressure": row["pressure"],
                "swap_used_mb": int(row["swap_used_mb"]),
                "swap_traffic_mb_per_minute": round(float(row["swap_traffic_mb_per_minute"]), 1),
                "top_applications": [f"{item.name} ({item.memory_mb} MB)" for item in at_sample.itertuples()],
            }
        )
    consumers = (
        processes.groupby("name")["memory_mb"]
        .agg(["median", lambda values: values.quantile(0.95), "count"])
        .set_axis(["median_mb", "p95_mb", "samples_in_top"], axis=1)
        .sort_values("median_mb", ascending=False)
        .head(12)
    )
    samples = processes["timestamp"].nunique()
    consumers["share_of_samples_percent"] = (consumers["samples_in_top"] / samples * 100).round(1)
    category_per_sample = processes.groupby(["timestamp", "category"])["memory_mb"].sum().unstack(fill_value=0)
    power_split = {
        source: {
            "samples": int(len(frame)),
            "pressure_percent": pressure_share(frame),
            "swap_traffic_mb_per_minute": percentiles(frame["swap_traffic_mb_per_minute"]),
        }
        for source, frame in system.groupby("power_source")
    }

    statistics = {
        "first_sample": first.isoformat(),
        "last_sample": last.isoformat(),
        "samples": int(len(system)),
        "physical_mb": physical_mb,
        "page_size_bytes": page_size,
        "hours_span": round((last - first).total_seconds() / 3600, 1),
        "hours_asleep": round(asleep_seconds / 3600, 1),
        "hours_not_sampled": round(not_sampled_seconds / 3600, 1),
        "hours_awake": round(awake_minutes / 60, 1),
        "awake_coverage_percent": round(min(100.0, len(system) / expected_samples * 100), 1),
        "working_samples": int(system["working"].sum()),
        "pressure_percent_all": pressure_share(system),
        "pressure_percent_working": pressure_share(system[system["working"]]),
        "pressure_percent_by_day": {
            str(day): pressure_share(frame) for day, frame in system.groupby(system["timestamp"].dt.date)
        },
        "swap_used_mb": percentiles(system["swap_used_mb"]),
        "swap_used_over_percent_of_ram": {
            str(share): round(float((system["swap_used_mb"] > physical_mb * share / 100).mean() * 100), 1)
            for share in (25, 50, 100)
        },
        "swap_traffic_mb_per_minute": percentiles(system["swap_traffic_mb_per_minute"]),
        "swap_written_gb_per_day": {str(day): value for day, value in swap_written_gb.items()},
        "compressor_stored_mb": percentiles(system["compressor_stored_mb"]),
        "compressor_occupied_mb": percentiles(system["compressor_occupied_mb"]),
        "compressions_per_minute": percentiles((system["compressions"] + system["decompressions"]) / interval_minutes),
        "estimated_demand_mb": percentiles(system["estimated_demand_mb"]),
        "top_consumers": consumers.round(0).reset_index().to_dict(orient="records"),
        "category_mb": {
            category: percentiles(category_per_sample[category]) for category in category_per_sample.columns
        },
        "power": power_split,
        "worst_moments": worst_moments,
        "events": events[events["event"].isin(["start", "stop", "gap", "error"])]
        .assign(timestamp=lambda frame: frame["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%S"))[
            ["timestamp", "event", "details"]
        ]
        .to_dict(orient="records"),
        "sleep_count": len(sleeps),
        "boot_times": sorted(events["boot_time"].unique().tolist()),
    }
    working = system[system["working"]]
    conservative_demand = working["app_mb"] + working["wired_mb"] + working["compressor_stored_mb"]
    statistics["working_hours"] = {
        "days": sorted({str(day) for day in working["timestamp"].dt.date}),
        "swap_used_mb": percentiles(working["swap_used_mb"]),
        "swap_traffic_mb_per_minute": percentiles(working["swap_traffic_mb_per_minute"]),
        "demand_conservative_mb": percentiles(conservative_demand),
        "demand_upper_mb": percentiles(conservative_demand + working["swap_used_mb"]),
        "pressure_percent_by_day": {
            str(day): pressure_share(frame) for day, frame in working.groupby(working["timestamp"].dt.date)
        },
        "swap_written_gb_by_day": {
            str(day): round(float(frame["swapouts"].sum() * page_size / 2**30), 1)
            for day, frame in working.groupby(working["timestamp"].dt.date)
        },
    }
    if len(statistics["boot_times"]) > 1:
        # Compares the fresh system after the last reboot with normal working load before it.
        boot = pd.Timestamp(statistics["boot_times"][-1])
        after = system[system["timestamp"] >= boot]
        before = working[working["timestamp"] < boot]

        def minutes_until(condition: pd.Series) -> float | None:
            hits = after.loc[condition, "timestamp"]
            return round((hits.iloc[0] - boot).total_seconds() / 60, 1) if len(hits) else None

        def state(frame: pd.DataFrame) -> dict:
            return {
                "samples": int(len(frame)),
                "warning_or_critical_percent": round(float(frame["pressure"].ne("normal").mean() * 100), 1),
                "swap_used_mb": percentiles(frame["swap_used_mb"]),
                "swap_traffic_mb_per_minute": percentiles(frame["swap_traffic_mb_per_minute"]),
                "compressor_stored_mb": percentiles(frame["compressor_stored_mb"]),
            }

        statistics["after_last_reboot"] = {
            "boot_time": boot.isoformat(),
            "last_sample": after["timestamp"].max().isoformat(),
            "minutes_to_first_warning": minutes_until(after["pressure"].ne("normal")),
            "minutes_to_swap_over_mb": {
                str(threshold): minutes_until(after["swap_used_mb"] > threshold) for threshold in (1024, 4096, 8192)
            },
            "after_reboot": state(after),
            "normal_load": state(before),
            "normal_load_days": sorted({str(day) for day in before["timestamp"].dt.date}),
        }
    (REPORT_DIRECTORY / "statistics.json").write_text(json.dumps(statistics, indent=2, default=str))

    # Charts. Gaps longer than 3 intervals are broken so lines are not drawn across sleep.
    broken = system.copy()
    broken.loc[
        broken["timestamp"].diff() > pd.Timedelta(minutes=3),
        ["swap_used_mb", "swap_traffic_mb_per_minute", "estimated_demand_mb"],
    ] = float("nan")

    figure, axis = new_figure(3.5)
    for pressure, color in PRESSURE_COLORS.items():
        rows = system[system["pressure"] == pressure]
        axis.bar(rows["timestamp"], 1, width=pd.Timedelta(minutes=1.2), color=color, label=pressure, linewidth=0)
    shade(axis, system, sleeps)
    axis.set_yticks([])
    axis.legend(loc="upper left", ncols=3)
    finish(figure, axis, "Memory pressure over time (grey = asleep, blue = on battery)", "", "pressure_timeline.png")

    figure, axis = new_figure()
    axis.plot(broken["timestamp"], broken["swap_used_mb"] / 1024, color="#6a1b9a", label="Swap used")
    axis.axhline(physical_mb / 1024, color="black", linestyle="--", label=f"Installed RAM ({physical_mb // 1024} GB)")
    shade(axis, system, sleeps)
    axis.legend(loc="upper left")
    finish(figure, axis, "Swap used compared with installed RAM", "GB", "swap_used.png")

    figure, axis = new_figure()
    axis.plot(broken["timestamp"], broken["swap_traffic_mb_per_minute"], color="#c62828", linewidth=0.8)
    for day in pd.date_range(first.normalize(), last.normalize()):
        if day.weekday() < 5:
            work_start = max(first, day + pd.Timedelta(hours=WORK_START_HOUR))
            work_end = min(last, day + pd.Timedelta(hours=WORK_END_HOUR))
            if work_start < work_end:
                axis.axvspan(work_start, work_end, color="#fff3c4", alpha=0.6, linewidth=0)
    shade(axis, system, sleeps)
    finish(
        figure,
        axis,
        "Swap traffic (data moved between RAM and SSD); yellow = working hours",
        "MB per minute",
        "swap_traffic.png",
        SOURCE_NOTE + f"; swapins + swapouts x {page_size} byte pages",
    )

    figure, axis = new_figure()
    axis.stackplot(
        system["timestamp"],
        system["app_mb"] / 1024,
        system["wired_mb"] / 1024,
        system["compressor_occupied_mb"] / 1024,
        labels=["App memory", "Wired", "Compressed"],
        colors=["#42a5f5", "#ffa726", "#ab47bc"],
        alpha=0.85,
    )
    axis.plot(
        broken["timestamp"],
        broken["estimated_demand_mb"] / 1024,
        color="#c62828",
        label="Estimated demand (app + wired + compressed data + swap)",
    )
    axis.axhline(physical_mb / 1024, color="black", linestyle="--", label="Installed RAM")
    shade(axis, system, sleeps)
    axis.legend(loc="upper left", fontsize=10)
    finish(
        figure,
        axis,
        "Memory use and estimated demand",
        "GB",
        "memory_demand.png",
        SOURCE_NOTE + "; demand is an estimate, see report",
    )

    figure, axis = new_figure(5)
    by_day = pd.DataFrame(statistics["pressure_percent_by_day"]).T.fillna(0)
    by_day = by_day.reindex(columns=[name for name in PRESSURE_COLORS if name in by_day.columns])
    by_day.plot.bar(stacked=True, ax=axis, color=[PRESSURE_COLORS[name] for name in by_day.columns], rot=0)
    finish(figure, axis, "Share of awake time per memory pressure level, by day", "% of samples", "pressure_by_day.png")

    figure, axis = new_figure(5)
    category_table = pd.DataFrame(statistics["category_mb"]).T.sort_values("median")
    axis.barh(category_table.index, category_table["p95"] / 1024, color="#90caf9", label="95th percentile")
    axis.barh(category_table.index, category_table["median"] / 1024, color="#1565c0", label="Median")
    axis.legend(loc="lower right")
    axis.set_xlabel("GB", fontsize=12)
    finish(
        figure,
        axis,
        "Memory by application category (top 10 applications per sample)",
        "",
        "top_consumers.png",
        "Source: logs/processes-*.csv; root-owned processes (e.g. security software) excluded",
    )

    figure, axis = new_figure(5)
    heatmap = system.pivot_table(
        index=system["timestamp"].dt.day_name(),
        columns=system["timestamp"].dt.hour,
        values="swap_traffic_mb_per_minute",
        aggfunc="median",
    )
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    heatmap = heatmap.reindex([day for day in weekdays if day in heatmap.index]).reindex(columns=range(24))
    image = axis.imshow(heatmap, aspect="auto", cmap="Reds")
    axis.set_yticks(range(len(heatmap.index)), heatmap.index)
    axis.set_xticks(range(24))
    axis.set_xlabel("Hour of day", fontsize=12)
    figure.colorbar(image, ax=axis, label="Median swap MB per minute")
    finish(figure, axis, "When swapping happens (blank = no data)", "", "heatmap_hour_weekday.png")

    print(json.dumps({key: statistics[key] for key in ("first_sample", "last_sample", "samples")}, default=str))


if __name__ == "__main__":
    main()
