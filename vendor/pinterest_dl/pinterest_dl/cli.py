"""Command-line interface for Pinterest HQ Downloader."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

from pinterest_dl import __version__
from pinterest_dl.service import PinterestService

console = Console()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pinterest-dl",
        description=(
            "Free open-source Pinterest high-quality image & video downloader. "
            "Paste a pin, board, search, or pinimg URL."
        ),
    )
    p.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")

    sub = p.add_subparsers(dest="command")

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("url", help="Pinterest pin/board/search URL or i.pinimg.com link")
        sp.add_argument(
            "-o",
            "--output",
            default="downloads",
            help="Output directory (default: downloads)",
        )
        sp.add_argument(
            "-n",
            "--limit",
            type=int,
            default=None,
            help="Max items (images/videos) to fetch/download",
        )
        sp.add_argument(
            "-q",
            "--quality",
            default="originals",
            choices=["originals", "1200x", "736x", "564x", "474x", "236x"],
            help="Preferred quality (falls back automatically)",
        )
        sp.add_argument(
            "-w",
            "--workers",
            type=int,
            default=6,
            help="Parallel download workers (default: 6)",
        )
        sp.add_argument(
            "--proxy",
            default=None,
            help="Optional HTTP(S) proxy URL",
        )
        sp.add_argument(
            "--no-skip",
            action="store_true",
            help="Re-download even if file already exists",
        )

    scrape_p = sub.add_parser("scrape", help="Scrape metadata/URLs without downloading")
    add_common(scrape_p)
    scrape_p.add_argument("--json", action="store_true", help="Print JSON to stdout")

    dl_p = sub.add_parser("download", help="Scrape and download images/videos (default command)")
    add_common(dl_p)
    dl_p.add_argument("--json", action="store_true", help="Print JSON summary to stdout")
    dl_p.add_argument(
        "--pick-folder",
        action="store_true",
        help="Open a native folder window to choose the save path",
    )

    web_p = sub.add_parser("web", help="Start local web UI")
    web_p.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    web_p.add_argument("--port", type=int, default=8787, help="Bind port (default: 8787)")
    web_p.add_argument(
        "-o",
        "--output",
        default="downloads",
        help="Default download directory",
    )

    # bare url convenience: pinterest-dl <url>
    p.add_argument("url_positional", nargs="?", help=argparse.SUPPRESS)
    p.add_argument("-o", "--output", default="downloads", help=argparse.SUPPRESS)
    p.add_argument("-n", "--limit", type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument("-q", "--quality", default="originals", help=argparse.SUPPRESS)
    p.add_argument("-w", "--workers", type=int, default=6, help=argparse.SUPPRESS)
    p.add_argument("--proxy", default=None, help=argparse.SUPPRESS)
    p.add_argument("--no-skip", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--pick-folder", action="store_true", help=argparse.SUPPRESS)
    return p


def _service_from_args(args: argparse.Namespace) -> PinterestService:
    return PinterestService(
        output_dir=args.output,
        workers=getattr(args, "workers", 6),
        preferred_quality=getattr(args, "quality", "originals"),
        skip_existing=not getattr(args, "no_skip", False),
        proxy=getattr(args, "proxy", None),
    )


def cmd_scrape(args: argparse.Namespace) -> int:
    svc = _service_from_args(args)
    with console.status("[bold cyan]Fetching Pinterest page..."):
        result = svc.scrape(args.url, limit=args.limit)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return 0 if result.items else 2

    videos = sum(1 for i in result.items if i.video_url or i.is_video)
    console.print(
        Panel.fit(
            f"[bold]{result.title}[/bold]\n"
            f"Kind: [cyan]{result.kind.value}[/cyan] · "
            f"Items: [green]{len(result.items)}[/green] · "
            f"Videos: [magenta]{videos}[/magenta]\n"
            f"{result.source_url}",
            title="Scrape result",
        )
    )
    for w in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {w}")

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", style="dim", width=4)
    table.add_column("ID")
    table.add_column("Type", width=6)
    table.add_column("Quality")
    table.add_column("Title")
    table.add_column("URL", overflow="fold")
    for i, item in enumerate(result.items, 1):
        media = "video" if item.video_url or item.is_video else "image"
        url_show = item.video_url or item.image_url
        table.add_row(
            str(i),
            item.id[:24],
            media,
            item.quality if media == "image" else "mp4",
            (item.title or "")[:40],
            url_show,
        )
    console.print(table)
    return 0 if result.items else 2


def cmd_download(args: argparse.Namespace) -> int:
    if getattr(args, "pick_folder", False):
        from pinterest_dl.dialogs import pick_directory

        chosen = pick_directory(
            title="Choose folder to save Pinterest media",
            initial=str(Path(args.output).expanduser().resolve()),
        )
        if not chosen:
            console.print("[yellow]Cancelled — no folder selected.[/yellow]")
            return 130
        args.output = chosen

    svc = _service_from_args(args)
    console.print(f"[bold]Pinterest HQ Downloader[/bold] v{__version__}")
    console.print(f"URL: {args.url}")
    console.print(f"Output: {Path(args.output).resolve()}")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Scraping…", total=None)
        scraped = svc.scrape(args.url, limit=args.limit)
        progress.update(
            task,
            description=(
                f"Found {len(scraped.items)} item(s) "
                f"({sum(1 for i in scraped.items if i.video_url)} video). Downloading…"
            ),
            total=max(len(scraped.items), 1),
            completed=0,
        )

        def on_progress(label: str, done: int, total: int) -> None:
            progress.update(
                task,
                completed=done,
                total=total,
                description=f"[{done}/{total}] {label[:40]}",
            )

        results = svc.download_items(scraped, progress=on_progress)

    ok = sum(1 for r in results if r.ok and not r.skipped)
    skipped = sum(1 for r in results if r.skipped)
    failed = sum(1 for r in results if not r.ok)

    if args.json:
        payload = {
            "scrape": scraped.to_dict(),
            "downloads": [r.to_dict() for r in results],
            "summary": {"ok": ok, "skipped": skipped, "failed": failed},
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for w in scraped.warnings:
            console.print(f"[yellow]warning:[/yellow] {w}")
        table = Table(title="Downloads")
        table.add_column("Status")
        table.add_column("File")
        table.add_column("Type")
        table.add_column("Quality")
        table.add_column("Bytes", justify="right")
        for r in results:
            if r.skipped:
                status = "[blue]skip[/blue]"
            elif r.ok:
                status = "[green]ok[/green]"
            else:
                status = "[red]fail[/red]"
            media = "video" if (r.item.video_url or r.item.is_video) else "image"
            file_cell = str(r.path) if r.path else (r.error or "")
            if r.paths and len(r.paths) > 1:
                file_cell = f"{file_cell} (+{len(r.paths) - 1} more)"
            table.add_row(
                status,
                file_cell,
                media,
                r.item.quality if media == "image" else "mp4",
                str(r.bytes_written),
            )
        console.print(table)
        console.print(
            f"[bold green]done[/bold green]: {ok} downloaded, "
            f"{skipped} skipped, {failed} failed → {Path(args.output).resolve()}"
        )

    return 0 if failed == 0 and (ok + skipped) > 0 else (1 if failed else 2)


def cmd_web(args: argparse.Namespace) -> int:
    import uvicorn

    from pinterest_dl.web.app import create_app

    app = create_app(default_output=args.output)
    console.print(
        Panel.fit(
            f"Open [bold cyan]http://{args.host}:{args.port}[/bold cyan]\n"
            f"Downloads → {Path(args.output).resolve()}\n"
            "On Download HQ a folder window opens so you can pick the save path.\n"
            "Press CTRL+C to stop.",
            title=f"Pinterest HQ Downloader v{__version__}",
        )
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    # Default command = download when first token looks like a URL
    if argv and not argv[0].startswith("-") and argv[0] not in {
        "download",
        "scrape",
        "web",
        "help",
    }:
        if (
            "://" in argv[0]
            or "pinterest." in argv[0]
            or argv[0].startswith("pin.it")
            or "pinimg.com" in argv[0]
        ):
            argv = ["download", *argv]

    args = parser.parse_args(argv)
    if not args.command:
        if getattr(args, "url_positional", None):
            args.command = "download"
            args.url = args.url_positional
        else:
            parser.print_help()
            raise SystemExit(0)

    try:
        if args.command == "scrape":
            code = cmd_scrape(args)
        elif args.command == "download":
            code = cmd_download(args)
        elif args.command == "web":
            code = cmd_web(args)
        else:
            parser.print_help()
            code = 0
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted[/yellow]")
        code = 130
    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]error:[/bold red] {exc}")
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
