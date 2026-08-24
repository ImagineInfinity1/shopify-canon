from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Callable

import requests
from PIL import Image, ImageOps


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from smart_mockup_engine import render_artwork_with_frame, slug  # noqa: E402


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
PSD_EXTENSIONS = {".psd", ".psb"}
APP_DIR_NAME = "Listing Cannon PSD Framer"
SETTINGS_FILE_NAME = "settings.json"
LogFunc = Callable[[str], None]
HTTP_SESSION = requests.Session()
_last_render_contact_at = 0.0


def default_app_data_root() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_DIR_NAME


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_settings_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        if key and value is not None and key not in os.environ:
            os.environ[str(key)] = str(value).strip()


def config(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def ensure_dirs(root: Path) -> dict[str, Path]:
    dirs = {
        "frames": root / "frames",
        "inbox": root / "inbox",
        "processing": root / "processing",
        "completed": root / "completed",
        "failed": root / "failed",
        "output": root / "output",
        "work": root / "work",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([str(path.resolve()), str(root.resolve())]) == str(root.resolve())
    except (OSError, ValueError):
        return False


def _remove_generated_path(path: Path, allowed_root: Path, log: LogFunc = print) -> None:
    if not path or not path.exists():
        return
    if not _path_is_within(path, allowed_root) or path.resolve() == allowed_root.resolve():
        raise RuntimeError(f"Refusing to remove path outside generated worker data: {path}")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    log(f"Removed generated worker artifact: {path.name}")


def cleanup_listing_artifacts(
    processing_path: Path,
    rendered_paths: list[Path],
    analysis_path: Path,
    dirs: dict[str, Path],
    log: LogFunc = print,
) -> None:
    """Delete only worker-created copies; external source artwork is never in these roots."""
    output_parents = {
        path.parent for path in (rendered_paths or [])
        if path and _path_is_within(path, dirs['output'])
    }
    for output_parent in output_parents:
        _remove_generated_path(output_parent, dirs['output'], log=log)
        work_dir = dirs['work'] / output_parent.name
        _remove_generated_path(work_dir, dirs['work'], log=log)
    if analysis_path and _path_is_within(analysis_path, dirs['work']):
        _remove_generated_path(analysis_path, dirs['work'], log=log)
    if processing_path and _path_is_within(processing_path, dirs['processing']):
        _remove_generated_path(processing_path, dirs['processing'], log=log)


def stable_file(path: Path, stable_seconds: float) -> bool:
    try:
        first = path.stat().st_size
        time.sleep(stable_seconds)
        second = path.stat().st_size
        return first == second and second > 0
    except OSError:
        return False


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 10000):
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find unique path for {path}")


def sorted_frames(frames_dir: Path) -> list[Path]:
    return sorted(
        [p for p in frames_dir.iterdir() if p.is_file() and p.suffix.lower() in PSD_EXTENSIONS],
        key=lambda p: p.name.lower(),
    )


def pending_artworks(inbox_dir: Path) -> list[Path]:
    return sorted(
        [p for p in inbox_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS],
        key=lambda p: p.stat().st_mtime,
    )


def render_all_frames(artwork_path: Path, frame_paths: list[Path], dirs: dict[str, Path], log: LogFunc = print) -> list[Path]:
    smart_layer_name = config("SMART_LAYER_NAME", "1")
    # "stretch" resizes the artwork to the smart layer's exact dimensions so
    # nothing is ever lost. "cover" CROPS overflow (it was shaving the top and
    # bottom off posters) and "contain" letterboxes — stretch is the default,
    # and a stale FIT_MODE=cover left in .env/settings must not resurrect the
    # cropping bug, so cover is ignored unless FIT_MODE_ALLOW_COVER=1.
    fit_mode = (config("FIT_MODE", "stretch") or "stretch").lower()
    if fit_mode == "cover" and config("FIT_MODE_ALLOW_COVER", "") != "1":
        log("FIT_MODE=cover ignored (crops artwork) — using stretch instead.")
        fit_mode = "stretch"

    artwork_slug = slug(artwork_path.stem)
    output_dir = unique_path(dirs["output"] / artwork_slug)
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = dirs["work"] / output_dir.name
    temp_dir.mkdir(parents=True, exist_ok=True)

    rendered_paths: list[Path] = []
    try:
        for index, frame_path in enumerate(frame_paths, start=1):
            output_path = output_dir / f"{index:02d}_{slug(frame_path.stem)}__{artwork_slug}.jpg"
            log(f"Rendering {artwork_path.name} into frame {index}/{len(frame_paths)}: {frame_path.name}")
            render_artwork_with_frame(
                artwork_path=artwork_path,
                psd_path=frame_path,
                output_jpg_path=output_path,
                temp_dir=temp_dir,
                smart_layer_name=smart_layer_name,
                fit_mode=fit_mode,
            )
            rendered_paths.append(output_path)
    except Exception as exc:
        _remove_generated_path(output_dir, dirs['output'], log=log)
        _remove_generated_path(temp_dir, dirs['work'], log=log)
        raise RuntimeError(f"Frame render failed for {frame_path.name}: {exc}") from exc
    return rendered_paths


def prepare_analysis_upload(artwork_path: Path, dirs: dict[str, Path], log: LogFunc = print) -> Path:
    """Create a bounded JPEG for Render/Gemini analysis instead of uploading huge originals."""
    max_pixels = int(config("ANALYSIS_MAX_PIXELS", "6000000") or "6000000")
    max_side = int(config("ANALYSIS_MAX_SIDE", "3000") or "3000")
    quality = int(config("ANALYSIS_JPEG_QUALITY", "88") or "88")
    analysis_dir = dirs["work"] / "analysis_uploads"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    # unique_path: pre-framed groups can stage identically-named files (e.g.
    # design-a/1.jpg, design-b/1.jpg) and the next group's analysis is prepared
    # WHILE the previous one may still be uploading — never overwrite in place.
    output_path = unique_path(analysis_dir / f"{slug(artwork_path.stem)}__analysis.jpg")

    with Image.open(artwork_path) as image:
        image.draft("RGB", (max_side, max_side))
        image = ImageOps.exif_transpose(image)
        source_pixels = image.width * image.height
        if source_pixels > max_pixels:
            scale = (max_pixels / float(source_pixels)) ** 0.5
            target_size = (
                max(1, int(image.width * scale)),
                max(1, int(image.height * scale)),
            )
            image.thumbnail(target_size, Image.Resampling.LANCZOS)
        if image.width > max_side or image.height > max_side:
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        try:
            image.convert("RGB").save(output_path, "JPEG", quality=quality, optimize=True)
        except Exception:
            if output_path.exists():
                output_path.unlink()
            raise

    log(
        f"Prepared analysis upload: {output_path.name} "
        f"({output_path.stat().st_size / (1024 * 1024):.2f} MB)"
    )
    return output_path


def prepare_ready_group(
    source_paths: list[Path],
    dirs: dict[str, Path],
    analysis_index: int = 1,
    log: LogFunc = print,
) -> tuple[Path, list[Path], Path]:
    """Stage a group of ALREADY-FRAMED images as one listing (no PSD render).

    Copies the images (in the given order, with an order-preserving prefix)
    into a group folder under processing/, and builds the AI analysis upload
    from image #analysis_index (1-based). Returns (group_dir, image_paths,
    analysis_path) shaped like render_stage's output. publish_stage deletes
    every staged copy after the final result; external sources are untouched."""
    if not source_paths:
        raise RuntimeError("Pre-framed group is empty")
    # Include the parent folder in the group name: designs often live in
    # per-design subfolders with generic filenames (1.jpg, 2.jpg), and the
    # folder name is what identifies the design in logs/completed/failed.
    first = source_paths[0]
    group_name = slug(f"{first.parent.name}-{first.stem}") or slug(first.stem) or "group"
    group_dir = unique_path(dirs["processing"] / group_name)
    group_dir.mkdir(parents=True, exist_ok=True)
    try:
        staged_paths: list[Path] = []
        for index, source in enumerate(source_paths, start=1):
            dest = group_dir / f"{index:02d}_{slug(source.stem)}{source.suffix.lower()}"
            shutil.copy2(source, dest)
            staged_paths.append(dest)
        pick = min(max(1, analysis_index), len(staged_paths)) - 1
        log(
            f"Pre-framed group {group_dir.name}: {len(staged_paths)} image(s); "
            f"analyzing image #{pick + 1} ({staged_paths[pick].name})"
        )
        analysis_path = prepare_analysis_upload(staged_paths[pick], dirs, log=log)
        return group_dir, staged_paths, analysis_path
    except Exception as exc:
        _remove_generated_path(group_dir, dirs['processing'], log=log)
        log(f"FAILED {group_dir.name}: {exc}")
        raise


def _request_delay(attempt: int) -> float:
    base_delay = float(config("RETRY_BASE_SECONDS", "8") or "8")
    return min(60.0, base_delay * attempt)


def _request_timeout(connect_default: str = "30", read_default: str = "300") -> tuple[float, float]:
    connect = float(config("CONNECT_TIMEOUT_SECONDS", connect_default) or connect_default)
    read = float(config("READ_TIMEOUT_SECONDS", read_default) or read_default)
    return (connect, read)


def client_job_id(artwork_path: Path, rendered_paths: list[Path], analysis_path: Path) -> str:
    digest = hashlib.sha256()
    for path in [artwork_path, analysis_path, *rendered_paths]:
        stat = path.stat()
        digest.update(str(path.name).encode("utf-8", errors="ignore"))
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(str(int(stat.st_mtime)).encode("ascii"))
    return digest.hexdigest()[:32]


def wake_render(base_url: str, log: LogFunc = print) -> None:
    """Wake a sleeping Render service before sending the large multipart upload."""
    global _last_render_contact_at
    if time.monotonic() - _last_render_contact_at < 300:
        return
    attempts = int(config("WAKE_RETRIES", "12") or "12")
    for attempt in range(1, attempts + 1):
        try:
            response = HTTP_SESSION.get(base_url + "/", timeout=_request_timeout("25", "60"), allow_redirects=True)
            if response.status_code < 500:
                _last_render_contact_at = time.monotonic()
                if attempt > 1:
                    log("Render is awake.")
                return
            log(f"Render wake attempt {attempt}/{attempts}: HTTP {response.status_code}")
        except requests.RequestException as exc:
            log(f"Render wake attempt {attempt}/{attempts}: {exc}")
        if attempt < attempts:
            time.sleep(_request_delay(attempt))


def upload_to_listing_cannon(
    artwork_path: Path,
    rendered_paths: list[Path],
    analysis_path: Path,
    log: LogFunc = print,
    profile_name: str | None = None,
    extra_form: dict[str, str] | None = None,
) -> dict:
    base_url = config("LISTING_CANNON_URL", "https://listing-cannon.onrender.com").rstrip("/")
    token = config("LOCAL_WORKER_TOKEN")
    profile_name = (profile_name or config("PROFILE_NAME")).strip()
    review_before_publish = config("REVIEW_BEFORE_PUBLISH", "false").lower()
    upload_retries = int(config("UPLOAD_RETRIES", "10") or "10")
    job_id = client_job_id(artwork_path, rendered_paths, analysis_path)
    if not token:
        raise RuntimeError("Worker token is missing. Open Settings in the desktop app and enter the Render worker token.")

    data = {
        "client_job_id": job_id,
        "source_filename": artwork_path.name,
        "profile_name": profile_name,
        "auto_start": "true",
        "review_before_publish": "true" if review_before_publish in {"1", "true", "yes"} else "false",
    }
    if extra_form:
        data.update(extra_form)
    headers = {"Authorization": f"Bearer {token}"}

    wake_render(base_url, log=log)

    last_error = None
    for attempt in range(1, upload_retries + 1):
        try:
            with ExitStack() as stack:
                files = []
                for path in rendered_paths:
                    files.append(("files", (path.name, stack.enter_context(path.open("rb")), "image/jpeg")))
                files.append((
                    "analysis_file",
                    (analysis_path.name, stack.enter_context(analysis_path.open("rb")), "image/jpeg"),
                ))
                response = HTTP_SESSION.post(
                    f"{base_url}/api/local_worker/upload_framed",
                    headers=headers,
                    data=data,
                    files=files,
                    timeout=_request_timeout("30", "420"),
                )

            try:
                payload = response.json()
            except ValueError:
                payload = {"error": response.text[:500] or f"HTTP {response.status_code}"}
            if response.ok:
                global _last_render_contact_at
                _last_render_contact_at = time.monotonic()
                if payload.get("duplicate"):
                    log(f"Render already accepted this upload as task {payload.get('task_id')}; resuming status polling.")
                profile_used = payload.get("profile_name") or "default"
                version = payload.get("profile_rules_version") or "unversioned"
                fingerprint = payload.get("prompt_fingerprint") or "unknown"
                fact_count = payload.get("required_description_fact_count", 0)
                log(
                    f"Render profile: {profile_used}; rules {version}; "
                    f"prompt {fingerprint}; required description facts {fact_count}."
                )
                return payload
            last_error = payload.get("error") or f"Upload failed with HTTP {response.status_code}"
            if response.status_code not in {502, 503, 504, 521, 522, 523, 524}:
                raise RuntimeError(last_error)
            log(f"Upload attempt {attempt}/{upload_retries} failed: {last_error}")
        except requests.RequestException as exc:
            last_error = str(exc)
            log(f"Upload attempt {attempt}/{upload_retries} failed: {last_error}")
        if attempt < upload_retries:
            time.sleep(_request_delay(attempt))
            wake_render(base_url, log=log)
    raise RuntimeError(last_error or "Upload failed")


def wait_for_listing_result(task_id: str, log: LogFunc = print) -> dict:
    if config("WAIT_FOR_COMPLETION", "true").lower() not in {"1", "true", "yes"}:
        return {"status": "accepted", "task_id": task_id}

    base_url = config("LISTING_CANNON_URL", "https://listing-cannon.onrender.com").rstrip("/")
    token = config("LOCAL_WORKER_TOKEN")
    poll_seconds = float(config("STATUS_POLL_SECONDS", "3") or "3")
    # Allows for the server-side AI retry schedule (5 attempts spread over a few
    # minutes) plus image upload and product creation.
    timeout_seconds = float(config("LISTING_TIMEOUT_SECONDS", "1500") or "1500")
    headers = {"Authorization": f"Bearer {token}"}
    deadline = time.monotonic() + timeout_seconds
    last_step = None
    # A newly accepted task should be visible almost immediately. If it stays
    # missing, Render has usually replaced the instance and lost its /tmp queue.
    not_found_until = time.monotonic() + min(45.0, timeout_seconds)
    not_found_count = 0

    while time.monotonic() < deadline:
        try:
            response = HTTP_SESSION.get(
                f"{base_url}/api/local_worker/task_status/{task_id}",
                headers=headers,
                timeout=_request_timeout("25", "60"),
            )
        except requests.RequestException as exc:
            log(f"Status check failed, retrying: {exc}")
            time.sleep(poll_seconds)
            continue
        try:
            payload = response.json()
        except ValueError:
            payload = {"error": response.text[:500] or f"HTTP {response.status_code}"}
        if not response.ok:
            error_message = payload.get("error") or f"Status check failed with HTTP {response.status_code}"
            if response.status_code in {502, 503, 504, 521, 522, 523, 524}:
                log(f"Render temporarily unavailable during status check ({response.status_code}); retrying...")
                time.sleep(poll_seconds)
                continue
            if response.status_code == 404 and "Task not found" in error_message and time.monotonic() < not_found_until:
                not_found_count += 1
                if not_found_count == 1 or not_found_count % 6 == 0:
                    log("Task status not visible yet on Render; retrying...")
                time.sleep(poll_seconds)
                continue
            if response.status_code == 404 and "Task not found" in error_message:
                raise RuntimeError(
                    "Render lost the accepted task during an instance restart or deployment."
                )
            raise RuntimeError(payload.get("error") or f"Status check failed with HTTP {response.status_code}")

        status = payload.get("status")
        step = payload.get("current_step")
        if step and step != last_step:
            log(f"Task {task_id}: {status} - {step}")
            last_step = step
        if status == "completed":
            global _last_render_contact_at
            _last_render_contact_at = time.monotonic()
            faq_verified = payload.get("faq_metafield_verified")
            if payload.get("product_faq_required") and faq_verified is not True:
                log("WARNING: Shopify FAQ metafield was not verified after product creation.")
            elif faq_verified is True:
                log("Shopify FAQ metafield verified.")
            return payload
        if status == "error":
            raise RuntimeError(payload.get("error") or "Listing task failed on Render")
        if status == "awaiting_review":
            raise RuntimeError("Listing task is awaiting review. Disable review in the profile or set REVIEW_BEFORE_PUBLISH=false.")
        time.sleep(poll_seconds)

    raise TimeoutError(f"Timed out waiting for Listing Cannon task {task_id}")


def render_stage(
    artwork_path: Path,
    frame_paths: list[Path],
    dirs: dict[str, Path],
    log: LogFunc = print,
) -> tuple[Path, list[Path], Path]:
    """Local-only half of process_one: move to processing/ and render all frames.

    Returns (processing_path, rendered_paths, analysis_path). On failure all
    worker-created copies are removed and the external source remains untouched.
    Split out so the desktop app can pre-render the NEXT artwork while waiting
    on Render for the current one."""
    processing_path = unique_path(dirs["processing"] / artwork_path.name)
    shutil.move(str(artwork_path), processing_path)
    rendered_paths = []
    analysis_path = None
    try:
        rendered_paths = render_all_frames(processing_path, frame_paths, dirs, log=log)
        analysis_path = prepare_analysis_upload(processing_path, dirs, log=log)
        return processing_path, rendered_paths, analysis_path
    except Exception as exc:
        cleanup_listing_artifacts(processing_path, rendered_paths, analysis_path, dirs, log=log)
        log(f"FAILED {processing_path.name}: {exc}")
        raise


def publish_stage(
    processing_path: Path,
    rendered_paths: list[Path],
    analysis_path: Path,
    dirs: dict[str, Path],
    log: LogFunc = print,
    profile_name: str | None = None,
    extra_form: dict[str, str] | None = None,
) -> bool:
    """Network half of process_one: upload framed images and wait for the listing."""
    try:
        result = None
        final_result = None
        for submit_attempt in range(1, 3):
            result = upload_to_listing_cannon(
                processing_path,
                rendered_paths,
                analysis_path,
                log=log,
                profile_name=profile_name,
                extra_form=extra_form,
            )
            try:
                final_result = wait_for_listing_result(result["task_id"], log=log)
                break
            except RuntimeError as status_error:
                interrupted = any(
                    marker in str(status_error).casefold()
                    for marker in ("lost the accepted task", "server restart", "task not found")
                )
                if not interrupted or submit_attempt >= 2:
                    raise
                log("Render restarted after accepting the upload. Re-uploading the same listing once...")
                wake_render(
                    config("LISTING_CANNON_URL", "https://listing-cannon.onrender.com").rstrip("/"),
                    log=log,
                )
        if final_result is None:
            raise RuntimeError("Listing did not return a final result after re-upload.")
        try:
            cleanup_listing_artifacts(processing_path, rendered_paths, analysis_path, dirs, log=log)
        except Exception as cleanup_error:
            log(f"WARNING: listing succeeded but local artifact cleanup needs attention: {cleanup_error}")
        log(
            f"Completed {processing_path.name}: task {result.get('task_id')} "
            f"({result.get('image_count')} framed image(s)), product {final_result.get('product_url') or final_result.get('product_id') or 'created'}"
        )
        return True
    except Exception as exc:
        try:
            cleanup_listing_artifacts(processing_path, rendered_paths, analysis_path, dirs, log=log)
        except Exception as cleanup_error:
            log(f"WARNING: local artifact cleanup needs attention: {cleanup_error}")
        log(f"FAILED {processing_path.name}: {exc}")
        return False


def process_one(
    artwork_path: Path,
    frame_paths: list[Path],
    dirs: dict[str, Path],
    log: LogFunc = print,
    profile_name: str | None = None,
) -> bool:
    try:
        processing_path, rendered_paths, analysis_path = render_stage(
            artwork_path, frame_paths, dirs, log=log
        )
    except Exception:
        return False
    return publish_stage(
        processing_path,
        rendered_paths,
        analysis_path,
        dirs,
        log=log,
        profile_name=profile_name,
    )


def run(root: Path, once: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    load_settings_file(root / SETTINGS_FILE_NAME)
    load_env_file(root / ".env")
    dirs = ensure_dirs(root)
    poll_seconds = float(config("POLL_SECONDS", "5") or "5")
    stable_seconds = float(config("STABILITY_SECONDS", "2") or "2")

    print(f"Listing Cannon local worker")
    print(f"Frames: {dirs['frames']}")
    print(f"Inbox:  {dirs['inbox']}")
    print("PSD order is filename order. Prefix frames like 01_, 02_, 03_ to control Shopify image order.")

    while True:
        frame_paths = sorted_frames(dirs["frames"])
        if not frame_paths:
            print(f"No PSD/PSB frames in {dirs['frames']}. Waiting...")
            if once:
                return
            time.sleep(poll_seconds)
            continue

        artworks = pending_artworks(dirs["inbox"])
        if not artworks:
            if once:
                return
            time.sleep(poll_seconds)
            continue

        for artwork_path in artworks:
            if not stable_file(artwork_path, stable_seconds):
                continue
            if not process_one(artwork_path, frame_paths, dirs):
                # Stop the whole run on the first failure. Carrying on would
                # quietly leave a gap in the batch, and whatever broke would
                # most likely break the next artwork too.
                print("")
                print("=" * 70)
                print("PROCESSING STOPPED - a listing failed and nothing was published for it.")
                print(f"Artwork: {artwork_path.name}")
                print("Fix the cause above, then start the worker again.")
                print("=" * 70)
                return

        if once:
            return


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch a folder, frame artwork locally, and submit Shopify listing tasks.")
    parser.add_argument("--root", default=str(default_app_data_root()), help="Worker folder containing frames/inbox/output folders.")
    parser.add_argument("--once", action="store_true", help="Process currently pending files once, then exit.")
    args = parser.parse_args()
    run(Path(args.root).resolve(), args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
