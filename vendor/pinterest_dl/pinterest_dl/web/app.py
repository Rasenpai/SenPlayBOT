"""FastAPI web UI for Pinterest HQ Downloader."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from pinterest_dl import __version__
from pinterest_dl.dialogs import pick_directory
from pinterest_dl.service import PinterestService

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))
STATIC_DIR = WEB_DIR / "static"

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


class ScrapeRequest(BaseModel):
    url: str = Field(..., min_length=5)
    limit: int | None = Field(default=50, ge=1, le=500)
    quality: str = "originals"


class DownloadRequest(BaseModel):
    url: str = Field(..., min_length=5)
    limit: int | None = Field(default=50, ge=1, le=500)
    quality: str = "originals"
    output: str | None = None
    workers: int = Field(default=6, ge=1, le=32)
    pick_folder: bool = Field(
        default=True,
        description="When true and output is empty, open a native folder dialog first",
    )


class PickFolderRequest(BaseModel):
    initial: str | None = None
    title: str = "Choose folder to save images"


def create_app(default_output: str = "downloads") -> FastAPI:
    app = FastAPI(
        title="Pinterest HQ Downloader",
        version=__version__,
        description="Free open-source Pinterest high-quality image & video downloader",
    )
    default = str(Path(default_output).expanduser().resolve())
    app.state.default_output = default
    app.state.current_output = default
    # Roots allowed for /api/files/download
    app.state.allowed_roots = {default}

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def _register_root(path: str) -> str:
        resolved = str(Path(path).expanduser().resolve())
        app.state.allowed_roots.add(resolved)
        app.state.current_output = resolved
        return resolved

    def _is_allowed(target: Path) -> bool:
        target = target.resolve()
        for root in app.state.allowed_roots:
            root_p = Path(root).resolve()
            if target == root_p or root_p in target.parents:
                return True
        return False

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "version": __version__,
                "default_output": app.state.current_output,
            },
        )

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/api/pick-folder")
    async def api_pick_folder(body: PickFolderRequest | None = None) -> dict[str, Any]:
        """Open a native OS folder dialog (local desktop use)."""
        body = body or PickFolderRequest()
        initial = body.initial or app.state.current_output
        try:
            # Tk must run on a plain thread; FastAPI may be on async loop
            result: dict[str, Any] = {}
            err: list[BaseException] = []

            def _run() -> None:
                try:
                    result["path"] = pick_directory(title=body.title, initial=initial)
                except BaseException as exc:  # noqa: BLE001
                    err.append(exc)

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=300)
            if t.is_alive():
                raise HTTPException(status_code=408, detail="Folder dialog timed out")
            if err:
                raise HTTPException(status_code=500, detail=str(err[0])) from err[0]
            path = result.get("path")
            if not path:
                return {"cancelled": True, "path": None}
            path = _register_root(path)
            return {"cancelled": False, "path": path}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/scrape")
    async def api_scrape(body: ScrapeRequest) -> dict[str, Any]:
        svc = PinterestService(
            output_dir=app.state.current_output,
            preferred_quality=body.quality,
        )
        try:
            result = svc.scrape(body.url, limit=body.limit)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result.to_dict()

    def _run_job(job_id: str, body: DownloadRequest) -> None:
        out = body.output or app.state.current_output or app.state.default_output
        out = _register_root(out)
        svc = PinterestService(
            output_dir=out,
            workers=body.workers,
            preferred_quality=body.quality,
        )
        with _jobs_lock:
            _jobs[job_id]["status"] = "running"
            _jobs[job_id]["message"] = "Scraping Pinterest…"
            _jobs[job_id]["output"] = out
        try:
            scraped = svc.scrape(body.url, limit=body.limit)
            with _jobs_lock:
                _jobs[job_id]["total"] = len(scraped.items)
                _jobs[job_id]["message"] = f"Downloading {len(scraped.items)} image(s)…"
                _jobs[job_id]["scrape"] = scraped.to_dict()

            def progress(label: str, done: int, total: int) -> None:
                with _jobs_lock:
                    _jobs[job_id]["done"] = done
                    _jobs[job_id]["total"] = total
                    _jobs[job_id]["message"] = f"{done}/{total} {label[:60]}"

            results = svc.download_items(scraped, progress=progress)
            ok = sum(1 for r in results if r.ok and not r.skipped)
            skipped = sum(1 for r in results if r.skipped)
            failed = sum(1 for r in results if not r.ok)
            with _jobs_lock:
                _jobs[job_id]["status"] = "done"
                _jobs[job_id]["message"] = (
                    f"Finished: {ok} downloaded, {skipped} skipped, {failed} failed → {out}"
                )
                _jobs[job_id]["done"] = len(results)
                _jobs[job_id]["total"] = len(results)
                _jobs[job_id]["results"] = [r.to_dict() for r in results]
                _jobs[job_id]["output"] = out
                _jobs[job_id]["finished_at"] = time.time()
        except Exception as exc:  # noqa: BLE001
            with _jobs_lock:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["message"] = str(exc)
                _jobs[job_id]["finished_at"] = time.time()

    @app.post("/api/download")
    async def api_download(body: DownloadRequest) -> dict[str, Any]:
        # Optional server-side pick when client asks and no path given
        if body.pick_folder and not body.output:
            picked: dict[str, Any] = {}
            err: list[BaseException] = []

            def _run() -> None:
                try:
                    picked["path"] = pick_directory(
                        title="Choose folder to save images & videos",
                        initial=app.state.current_output,
                    )
                except BaseException as exc:  # noqa: BLE001
                    err.append(exc)

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=300)
            if err:
                raise HTTPException(status_code=500, detail=str(err[0])) from err[0]
            if not picked.get("path"):
                return {"cancelled": True, "job_id": None}
            body.output = _register_root(picked["path"])

        if body.output:
            body.output = _register_root(body.output)

        job_id = uuid.uuid4().hex
        with _jobs_lock:
            _jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "message": "Queued",
                "done": 0,
                "total": 0,
                "url": body.url,
                "output": body.output or app.state.current_output,
                "created_at": time.time(),
            }
        t = threading.Thread(target=_run_job, args=(job_id, body), daemon=True)
        t.start()
        return {"job_id": job_id, "cancelled": False, "output": body.output}

    @app.get("/api/jobs/{job_id}")
    async def api_job(job_id: str) -> dict[str, Any]:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            return dict(job)

    @app.get("/api/files")
    async def api_files(output: str | None = None) -> dict[str, Any]:
        root = Path(output or app.state.current_output).expanduser().resolve()
        if not _is_allowed(root):
            # allow listing only registered roots
            if str(root) not in app.state.allowed_roots:
                raise HTTPException(status_code=403, detail="Path not allowed")
        if not root.exists():
            return {"output": str(root), "files": []}
        files = []
        for p in sorted(root.rglob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.is_file() and p.suffix.lower() in {
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
                ".gif",
                ".mp4",
                ".webm",
                ".mov",
                ".m4v",
            }:
                files.append(
                    {
                        "name": p.name,
                        "path": str(p),
                        "size": p.stat().st_size,
                        "mtime": p.stat().st_mtime,
                    }
                )
            if len(files) >= 100:
                break
        return {"output": str(root), "files": files}

    @app.get("/api/files/download")
    async def api_file_download(path: str) -> FileResponse:
        target = Path(path).expanduser().resolve()
        if not _is_allowed(target):
            raise HTTPException(status_code=403, detail="Path not allowed")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(target, filename=target.name)

    return app


app = create_app()
