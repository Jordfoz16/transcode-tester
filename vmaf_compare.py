#!/usr/bin/env python3
"""Compare transcoded video files against a source using VMAF quality analysis."""

import argparse
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from datetime import timedelta

from rich.console import Console
from rich.progress import BarColumn, Progress, ProgressColumn, TaskProgressColumn, TextColumn
from rich.table import Table
from rich.text import Text

VMAF_SCORE_RE = re.compile(r"VMAF score:\s*([\d.]+)")
DEFAULT_EXTENSIONS = ("mkv", "mp4", "webm", "mov")


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

        # Average time per item * remaining items (works correctly with parallel jobs)
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


def run_vmaf(source: Path, transcoded: Path) -> Optional[float]:
    """Run ffmpeg libvmaf and return the VMAF score, or None on failure."""
    cmd = [
        "ffmpeg",
        "-y",  # Overwrite output
        "-i", str(source),
        "-i", str(transcoded),
        "-lavfi", "libvmaf",
        "-f", "null",
        "-",
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
        )
        vmaf_score = None
        for line in proc.stderr:
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
        default=1,
        metavar="N",
        help="Number of parallel VMAF jobs (default: 1)",
    )
    parser.add_argument(
        "--sort",
        choices=["name", "ratio", "saved", "score"],
        default="ratio",
        help="Sort table by: name, ratio (compression), saved (data saved), score (VMAF) (default: ratio)",
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
    results = []

    def process_file(transcoded: Path) -> dict:
        """Run VMAF on one file and return its result dict."""
        vmaf_score = run_vmaf(args.source, transcoded)
        transcoded_size = transcoded.stat().st_size
        compression_ratio = source_size / transcoded_size if transcoded_size > 0 else 0
        data_saved = source_size - transcoded_size
        codec = get_codec(transcoded)
        return {
            "filename": transcoded.name,
            "codec": codec,
            "compression_ratio": compression_ratio,
            "file_size": transcoded_size,
            "data_saved": data_saved,
            "vmaf_score": vmaf_score,
        }

    total_files = len(transcoded_files)
    progress_columns = (
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        ParallelTimeRemainingColumn(),
    )

    jobs = min(args.jobs, total_files)
    with Progress(*progress_columns, console=console, disable=args.no_progress) as progress:
        task = progress.add_task(
            f"Computing VMAF (0/{total_files} files, {total_files} left)",
            total=total_files,
        )

        with ThreadPoolExecutor(max_workers=jobs) as executor:
            futures = {executor.submit(process_file, t): t for t in transcoded_files}
            for future in as_completed(futures):
                results.append(future.result())
                completed = len(results)
                left = total_files - completed
                progress.update(
                    task,
                    advance=1,
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
