#!/usr/bin/env python3
"""Compare transcoded video files against a source using VMAF quality analysis."""

import argparse
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

from datetime import timedelta

from rich.console import Console
from rich.progress import BarColumn, Progress, ProgressColumn, TaskProgressColumn, TextColumn
from rich.table import Table
from rich.text import Text

VMAF_SCORE_RE = re.compile(r"VMAF score:\s*([\d.]+)")
DURATION_RE   = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
TIME_RE       = re.compile(r"\btime=(\d+):(\d+):(\d+(?:\.\d+)?)")
DEFAULT_EXTENSIONS = ("mkv", "mp4", "webm", "mov")


def _parse_ts(h: str, m: str, s: str) -> float:
    """Convert HH:MM:SS.xx timestamp components to total seconds."""
    return int(h) * 3600 + int(m) * 60 + float(s)


class ParallelTimeRemainingColumn(ProgressColumn):
    """ETA based on wall-clock elapsed/completed, accurate for parallel jobs."""

    max_refresh = 0.5

    def render(self, task) -> Text:
        if task.finished and task.finished_time is not None:
            delta = timedelta(seconds=int(task.finished_time))
            return Text(str(delta), style="progress.elapsed")
        if (
            task.total is None
            or task.total == 0
            or task.completed is None
            or task.completed <= 0
            or task.elapsed is None
        ):
            return Text("--:--", style="progress.remaining")

        remaining = task.total - task.completed
        if remaining <= 0:
            return Text("0:00:00", style="progress.remaining")

        # Average time per unit * remaining units (works correctly with parallel jobs)
        eta_seconds = (task.elapsed / task.completed) * remaining
        minutes, seconds = divmod(int(eta_seconds), 60)
        hours, minutes = divmod(minutes, 60)
        formatted = f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return Text(formatted, style="progress.remaining")


def format_size(size_bytes: int) -> str:
    """Format bytes as human-readable size (KB/MB/GB)."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def find_video_files(directory: Path, extensions: tuple) -> list:
    """Recursively find video files with given extensions."""
    files = []
    for ext in extensions:
        files.extend(directory.rglob(f"*.{ext}"))
    return sorted(files)


def get_codec(path: Path) -> str:
    """Get video codec from file using ffprobe. Returns e.g. h264, h265, av1."""
    CODEC_ALIAS = {"hevc": "h265"}
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            codec = result.stdout.strip().lower()
            return CODEC_ALIAS.get(codec, codec)
    except Exception:
        pass
    return "?"


def get_duration(path: Path) -> Optional[float]:
    """Get video duration in seconds using ffprobe container metadata (fast, no decode)."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            val = result.stdout.strip()
            if val.lower() not in ("n/a", ""):
                return float(val)
    except Exception:
        pass
    return None


def run_vmaf(
    source: Path,
    transcoded: Path,
    threads: int = 0,
    progress_callback: Optional[Callable[[float, float], None]] = None,
) -> Optional[float]:
    """Run ffmpeg libvmaf and return the VMAF score, or None on failure.

    progress_callback(current_sec, total_sec) is called for each ffmpeg
    progress line so the caller can update a progress bar in real time.
    threads=0 lets ffmpeg choose automatically.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(source),
        "-i", str(transcoded),
        "-lavfi", "libvmaf",
        "-f", "null",
        "-",
    ]
    if threads > 0:
        cmd += ["-threads", str(threads)]
    try:
        proc = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
        )
        vmaf_score = None
        total_duration: Optional[float] = None
        for line in proc.stderr:
            # Capture total duration from the first Input block
            if total_duration is None:
                m = DURATION_RE.search(line)
                if m:
                    total_duration = _parse_ts(*m.groups())

            # Report per-progress-line updates back to the caller
            if progress_callback is not None and total_duration:
                m = TIME_RE.search(line)
                if m:
                    current = _parse_ts(*m.groups())
                    progress_callback(current, total_duration)

            match = VMAF_SCORE_RE.search(line)
            if match:
                vmaf_score = float(match.group(1))

        proc.wait()
        return vmaf_score if proc.returncode == 0 else None
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare transcoded videos against a source using VMAF quality analysis."
    )
    parser.add_argument(
        "source",
        type=Path,
        help="Path to the base/source video file",
    )
    parser.add_argument(
        "transcoded_path",
        type=Path,
        help="Directory containing transcoded files (searched recursively)",
    )
    parser.add_argument(
        "--extensions",
        default="mkv,mp4,webm,mov",
        help="Comma-separated video extensions to search (default: mkv,mp4,webm,mov)",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable progress bar (e.g. for piping)",
    )
    parser.add_argument(
        "--jobs",
        "-j",
        type=int,
        default=4,
        metavar="N",
        help="Number of parallel VMAF jobs (default: 4)",
    )
    parser.add_argument(
        "--sort",
        choices=["name", "ratio", "saved", "score"],
        default="ratio",
        help="Sort table by: name, ratio (compression), saved (data saved), score (VMAF) (default: ratio)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        metavar="FILE",
        help="Write results to file (plain text) in addition to printing to the terminal",
    )
    args = parser.parse_args()

    console = Console()

    if args.jobs < 1:
        console.print("[red]Error: --jobs must be at least 1[/red]")
        return 1

    # Validate source
    if not args.source.is_file():
        console.print(f"[red]Error: Source file not found: {args.source}[/red]")
        return 1

    # Check ffmpeg
    if not shutil.which("ffmpeg"):
        console.print("[red]Error: ffmpeg not found in PATH. Install ffmpeg with libvmaf support.[/red]")
        return 1

    # Find transcoded files
    extensions = tuple(e.strip().lstrip(".").lower() for e in args.extensions.split(","))
    transcoded_files = find_video_files(args.transcoded_path, extensions)

    if not transcoded_files:
        console.print(
            f"[red]Error: No video files found in {args.transcoded_path} "
            f"(extensions: {', '.join(extensions)})[/red]"
        )
        return 1

    source_size = args.source.stat().st_size
    total_files = len(transcoded_files)
    results = []

    # Limit per-job thread count to avoid CPU oversubscription across parallel jobs
    jobs = min(args.jobs, total_files)
    cpu_count = os.cpu_count() or 1
    threads_per_job = max(1, cpu_count // jobs)

    # Pre-fetch durations for all transcoded files in parallel (fast ffprobe, no decode)
    if not args.no_progress:
        console.print("[dim]Scanning file durations…[/dim]")
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        durations: dict[Path, Optional[float]] = dict(
            executor.map(lambda p: (p, get_duration(p)), transcoded_files)
        )

    known_durations = [d for d in durations.values() if d is not None]
    total_duration = sum(known_durations) if known_durations else None
    use_duration_mode = total_duration is not None and total_duration > 0

    progress_columns = (
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        ParallelTimeRemainingColumn(),
    )

    with Progress(*progress_columns, console=console, disable=args.no_progress) as progress:
        if use_duration_mode:
            overall_task = progress.add_task(
                f"Computing VMAF (0/{total_files} files)",
                total=total_duration,
            )
        else:
            overall_task = progress.add_task(
                f"Computing VMAF (0/{total_files} files, {total_files} left)",
                total=total_files,
            )

        def process_file(transcoded: Path) -> dict:
            """Run VMAF on one file, reporting real-time progress, and return its result dict."""
            file_duration = durations.get(transcoded)
            last_reported = [0.0]

            # Per-file transient sub-task
            file_task = progress.add_task(
                f"  [dim]{transcoded.name}[/dim]",
                total=file_duration if file_duration else 100,
                visible=not args.no_progress,
            )

            def on_progress(current_sec: float, _total_sec: float) -> None:
                delta = current_sec - last_reported[0]
                if delta <= 0:
                    return
                last_reported[0] = current_sec
                if use_duration_mode:
                    progress.advance(overall_task, delta)
                progress.update(file_task, completed=current_sec)

            vmaf_score = run_vmaf(
                args.source,
                transcoded,
                threads=threads_per_job,
                progress_callback=on_progress if not args.no_progress else None,
            )

            # Advance overall task for any unaccounted duration (e.g. no progress events)
            if use_duration_mode and file_duration is not None:
                remaining = file_duration - last_reported[0]
                if remaining > 0:
                    progress.advance(overall_task, remaining)
            elif not use_duration_mode:
                progress.advance(overall_task, 1)

            progress.remove_task(file_task)

            transcoded_size = transcoded.stat().st_size
            compression_ratio = source_size / transcoded_size if transcoded_size > 0 else 0
            data_saved = source_size - transcoded_size
            codec = get_codec(transcoded)

            result = {
                "filename": transcoded.name,
                "codec": codec,
                "compression_ratio": compression_ratio,
                "file_size": transcoded_size,
                "data_saved": data_saved,
                "vmaf_score": vmaf_score,
            }

            completed_count = len(results) + 1
            score_str = f"{vmaf_score:.2f}" if vmaf_score is not None else "ERROR"
            progress.console.log(
                f"[green]✓[/green] {transcoded.name}  "
                f"[bold]VMAF:[/bold] {score_str}  "
                f"({completed_count}/{total_files})"
            )

            return result

        with ThreadPoolExecutor(max_workers=jobs) as executor:
            futures = {executor.submit(process_file, t): t for t in transcoded_files}
            for future in as_completed(futures):
                results.append(future.result())
                completed = len(results)
                left = total_files - completed
                if use_duration_mode:
                    progress.update(
                        overall_task,
                        description=f"Computing VMAF ({completed}/{total_files} files)",
                    )
                else:
                    progress.update(
                        overall_task,
                        description=f"Computing VMAF ({completed}/{total_files} files, {left} left)",
                    )

    # Sort results; put failures (None) at end for numeric sorts
    sort_key = args.sort
    if sort_key == "name":
        results.sort(key=lambda r: (r["filename"].lower(),))
    elif sort_key == "ratio":
        results.sort(key=lambda r: (
            r["compression_ratio"] is None or r["compression_ratio"] <= 0,
            -(r["compression_ratio"] or 0),
        ))
    elif sort_key == "saved":
        results.sort(key=lambda r: (False, -r["data_saved"]))
    else:  # score
        results.sort(key=lambda r: (r["vmaf_score"] is None, -(r["vmaf_score"] or 0)))

    # VMAF scale explanation
    console.print()
    console.print(
        "VMAF Scale (0–100): 100 = identical to source | 93+ = perceptually transparent | "
        "80–93 = good | 60–80 = fair | <60 = poor"
    )
    console.print()

    # Output table
    table = Table(show_header=True, header_style="bold")
    table.add_column("Filename", style="cyan")
    table.add_column("Codec", justify="center")
    table.add_column("Compression Ratio", justify="right")
    table.add_column("File Size", justify="right")
    table.add_column("Data Saved", justify="right")
    table.add_column("VMAF Score", justify="right")

    for r in results:
        vmaf_str = f"{r['vmaf_score']:.2f}" if r["vmaf_score"] is not None else "ERROR"
        table.add_row(
            r["filename"],
            r["codec"],
            f"{r['compression_ratio']:.2f}x",
            format_size(r["file_size"]),
            format_size(r["data_saved"]),
            vmaf_str,
        )

    console.print(table)

    # Write to output file if requested
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            out_console = Console(file=f, no_color=True, force_terminal=False)
            out_console.print()
            out_console.print(
                "VMAF Scale (0–100): 100 = identical to source | 93+ = perceptually transparent | "
                "80–93 = good | 60–80 = fair | <60 = poor"
            )
            out_console.print()
            out_console.print(table)

    return 0


if __name__ == "__main__":
    sys.exit(main())
