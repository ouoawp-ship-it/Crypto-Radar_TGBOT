from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

try:
    from PIL import Image, ImageChops, ImageEnhance
except ImportError as exc:  # pragma: no cover - depends on the local visual QA environment
    raise SystemExit(
        "Mercu visual comparison requires Pillow. Run: "
        "python -m pip install -r requirements-visual.txt"
    ) from exc


DEFAULT_ROUTES = ("radar", "info", "funds")
DEFAULT_VIEWPORTS = ((1440, 900), (1920, 1080))


@dataclass(frozen=True)
class DiffResult:
    route: str
    viewport: str
    target: str
    actual: str
    diff: str
    status: str
    width: int | None = None
    height: int | None = None
    changed_pixels: int | None = None
    total_pixels: int | None = None
    changed_pixel_ratio: float | None = None
    mean_absolute_error: float | None = None
    root_mean_square_error: float | None = None
    max_channel_error: int | None = None
    reason: str = ""


def _parse_viewport(value: str) -> tuple[int, int]:
    raw = value.lower().strip()
    try:
        width_text, height_text = raw.split("x", 1)
        width, height = int(width_text), int(height_text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("viewport must use WIDTHxHEIGHT, for example 1440x900") from exc
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("viewport dimensions must be positive")
    return width, height


def _image_metrics(target: Image.Image, actual: Image.Image, pixel_threshold: int) -> tuple[int, float, float, float, int]:
    target_rgb = target.convert("RGB")
    actual_rgb = actual.convert("RGB")
    difference = ImageChops.difference(target_rgb, actual_rgb)
    total_pixels = target_rgb.width * target_rgb.height
    channels = difference.split()
    max_channel = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
    pixel_histogram = max_channel.histogram()
    changed_pixels = sum(pixel_histogram[pixel_threshold + 1 :])
    channel_histograms = [channel.histogram() for channel in channels]
    absolute_sum = sum(value * count for histogram in channel_histograms for value, count in enumerate(histogram))
    squared_sum = sum(value * value * count for histogram in channel_histograms for value, count in enumerate(histogram))
    max_error = max((value for histogram in channel_histograms for value, count in enumerate(histogram) if count), default=0)
    channel_count = total_pixels * 3
    changed_ratio = changed_pixels / total_pixels if total_pixels else 0.0
    mean_absolute_error = absolute_sum / channel_count / 255 if channel_count else 0.0
    root_mean_square_error = math.sqrt(squared_sum / channel_count) / 255 if channel_count else 0.0
    return changed_pixels, changed_ratio, mean_absolute_error, root_mean_square_error, max_error


def _write_heatmap(target: Image.Image, actual: Image.Image, destination: Path) -> None:
    difference = ImageChops.difference(target.convert("RGB"), actual.convert("RGB"))
    amplified = ImageEnhance.Contrast(difference).enhance(3.0)
    red = amplified.convert("L")
    heatmap = Image.merge("RGB", (red, Image.new("L", red.size, 0), Image.new("L", red.size, 0)))
    overlay = Image.blend(actual.convert("RGB"), heatmap, 0.65)
    destination.parent.mkdir(parents=True, exist_ok=True)
    overlay.save(destination)


def compare_pair(
    *,
    route: str,
    viewport: tuple[int, int],
    target_path: Path,
    actual_path: Path,
    diff_path: Path,
    pixel_threshold: int,
    max_changed_ratio: float,
    max_mean_error: float,
) -> DiffResult:
    viewport_label = f"{viewport[0]}x{viewport[1]}"
    shared = {
        "route": route,
        "viewport": viewport_label,
        "target": str(target_path),
        "actual": str(actual_path),
        "diff": str(diff_path),
    }
    if not target_path.is_file():
        return DiffResult(**shared, status="missing", reason="Mercu target screenshot is missing")
    if not actual_path.is_file():
        return DiffResult(**shared, status="missing", reason="Paoxx actual screenshot is missing")

    with Image.open(target_path) as target_image, Image.open(actual_path) as actual_image:
        target = target_image.convert("RGB")
        actual = actual_image.convert("RGB")
        if target.size != viewport:
            return DiffResult(
                **shared,
                status="size_mismatch",
                width=target.width,
                height=target.height,
                reason=f"Mercu target is {target.width}x{target.height}, expected {viewport_label}",
            )
        if actual.size != viewport:
            return DiffResult(
                **shared,
                status="size_mismatch",
                width=actual.width,
                height=actual.height,
                reason=f"Paoxx actual is {actual.width}x{actual.height}, expected {viewport_label}",
            )
        changed, changed_ratio, mae, rmse, max_error = _image_metrics(target, actual, pixel_threshold)
        _write_heatmap(target, actual, diff_path)

    passed = changed_ratio <= max_changed_ratio and mae <= max_mean_error
    return DiffResult(
        **shared,
        status="passed" if passed else "failed",
        width=viewport[0],
        height=viewport[1],
        changed_pixels=changed,
        total_pixels=viewport[0] * viewport[1],
        changed_pixel_ratio=round(changed_ratio, 8),
        mean_absolute_error=round(mae, 8),
        root_mean_square_error=round(rmse, 8),
        max_channel_error=max_error,
        reason="" if passed else "pixel difference exceeds the configured acceptance gate",
    )


def _pairs(
    routes: Iterable[str],
    viewports: Iterable[tuple[int, int]],
    target_dir: Path,
    actual_dir: Path,
    output_dir: Path,
) -> Iterable[tuple[str, tuple[int, int], Path, Path, Path]]:
    for route in routes:
        for width, height in viewports:
            label = f"{width}x{height}"
            yield (
                route,
                (width, height),
                target_dir / f"mercu-{route}-{label}.png",
                actual_dir / f"{route}-{label}-chromium.png",
                output_dir / f"diff-{route}-{label}.png",
            )


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Compare Mercu login-state targets with Paoxx workstation screenshots.")
    parser.add_argument("--target-dir", type=Path, default=root / "frontend/e2e/mercu-targets")
    parser.add_argument("--actual-dir", type=Path, default=root / "frontend/e2e/public-workflow.spec.ts-snapshots")
    parser.add_argument("--output-dir", type=Path, default=root / "frontend/e2e/mercu-diffs")
    parser.add_argument("--route", action="append", choices=DEFAULT_ROUTES, dest="routes")
    parser.add_argument("--viewport", action="append", type=_parse_viewport, dest="viewports")
    parser.add_argument("--pixel-threshold", type=int, default=0, help="Per-channel pixel tolerance (0-255). Default: exact.")
    parser.add_argument("--max-changed-ratio", type=float, default=0.0, help="Allowed changed-pixel ratio. Default: 0.")
    parser.add_argument("--max-mean-error", type=float, default=0.0, help="Allowed normalized mean absolute error. Default: 0.")
    args = parser.parse_args()

    if not 0 <= args.pixel_threshold <= 255:
        parser.error("--pixel-threshold must be between 0 and 255")
    if not 0 <= args.max_changed_ratio <= 1:
        parser.error("--max-changed-ratio must be between 0 and 1")
    if not 0 <= args.max_mean_error <= 1:
        parser.error("--max-mean-error must be between 0 and 1")

    routes = tuple(args.routes or DEFAULT_ROUTES)
    viewports = tuple(args.viewports or DEFAULT_VIEWPORTS)
    results = [
        compare_pair(
            route=route,
            viewport=viewport,
            target_path=target,
            actual_path=actual,
            diff_path=diff,
            pixel_threshold=args.pixel_threshold,
            max_changed_ratio=args.max_changed_ratio,
            max_mean_error=args.max_mean_error,
        )
        for route, viewport, target, actual, diff in _pairs(
            routes, viewports, args.target_dir, args.actual_dir, args.output_dir
        )
    ]
    report = {
        "gate": {
            "pixel_threshold": args.pixel_threshold,
            "max_changed_ratio": args.max_changed_ratio,
            "max_mean_error": args.max_mean_error,
        },
        "passed": all(result.status == "passed" for result in results),
        "results": [asdict(result) for result in results],
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report: {report_path}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
