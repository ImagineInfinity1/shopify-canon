import os

# Load .env as early as possible so SESSION_SECRET etc. are set no matter how the app is started
# Use explicit path based on this file's location so it works regardless of CWD or Flask reloader
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    load_dotenv(_env_path, override=True)
except ImportError:
    pass

import logging
import gzip
import json
import hmac
import requests
import re
from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, url_for, session, flash, make_response
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_login import current_user, login_required
import time
import uuid
import threading
import copy
import hashlib
import base64
import zlib
import tempfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from PIL import Image

from gemini_utils import generate_product_metadata, create_gemini_prompt, get_default_prompt, get_prompt_sections, profile_requires_description_facts, _word_safe_truncate, generate_collection_metadata
from gemini_utils import TAG_CASE_STYLES, generate_tag_cleanup_plan
from gemini_utils import choose_products_for_collection, generate_collection_groups
from shopify_graphql import (
    create_product_with_graphql,
    get_collections,
    get_collection_updated_at,
    list_collection_catalogue,
    update_collection_metadata,
    get_inventory_locations,
    get_price_lists,
    get_product_metafield_definitions,
    get_sales_channels,
    format_description_html,
    assign_media_to_product_variants,
    list_products_for_seo_enhancement,
    recommend_products_for_discovery,
    search_products_for_references,
    get_product_catalogue_bulk_operation,
    start_product_catalogue_bulk_operation,
    stream_product_catalogue_bulk_result,
    update_product_seo_metadata,
    update_collection_rules,
    create_smart_collection,
    get_online_store_publication_id,
    publish_collection_to_online_store,
    update_variant_skus_graphql,
    build_collection_link_repair_map,
    repair_internal_collection_links,
    _append_collection_links,
    _get_collection_title_handle_map,
)
from shopify_category_metafields import enrich_product_with_category_metafields, get_category_attribute_options
from shop_helpers import get_current_shop, get_shop_credentials
from psd_utils import process_psd_frames
from compress import compress_image
from shopify_publishing import get_shopify_publishing_settings
from shopify_webhooks import (
    WEBHOOK_TOPICS as SHOPIFY_WEBHOOK_TOPICS,
    callback_url_for as shopify_webhook_callback_url,
    ensure_webhooks as ensure_shopify_webhooks,
    list_webhooks as list_shopify_webhooks,
    verify_webhook as verify_shopify_webhook,
)
from shopify_listing_stats import load_analytics, load_catalogue, merge_listing_stats, normalize_range
from dynamic_mockups import process_image_with_dynamic_mockups, DynamicMockupsAPI, optimize_image_for_upload
from temp_file_service import temp_file_service
from mem_utils import log_mem
from ai_provider_benchmark import OPENROUTER_BENCHMARK_MODELS, benchmark_provider

# Configure logging - console always; file only when writable (Render has read-only project dir)
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
handlers = [console_handler]

# Only add file handler if project dir is writable (fails on Render/read-only FS)
_log_file = os.environ.get("LOG_FILE", "app.log")
if os.environ.get("PORT"):
    # PaaS (e.g. Render): use /tmp so we don't touch read-only project dir
    _log_file = os.path.join(os.environ.get("TMPDIR", "/tmp"), "app.log")
try:
    _fh = logging.FileHandler(_log_file, mode='a', encoding='utf-8')
    _fh.setLevel(logging.DEBUG)
    _fh.setFormatter(formatter)
    handlers.append(_fh)
except (OSError, PermissionError) as e:
    pass  # Console only on Render / read-only FS

logging.basicConfig(level=logging.DEBUG, handlers=handlers)
logger = logging.getLogger(__name__)
logger.debug("Logging initialized")

from extensions import db

app = Flask(__name__)
# Use SESSION_SECRET from .env, or a non-empty fallback (Flask rejects empty secret_key)
app.secret_key = (os.environ.get("SESSION_SECRET") or "").strip() or "dev-secret-key-change-in-production"
app.config['SESSION_COOKIE_SECURE'] = False  # Allow HTTP for development
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['MAX_CONTENT_LENGTH'] = 300 * 1024 * 1024  # 300MB max file size
app.config['MAX_FORM_MEMORY_SIZE'] = None  # Allow large form uploads
app.config['MAX_FORM_PARTS'] = 1000  # Allow multiple file parts
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # Disable caching for uploads
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Database configuration - Use PostgreSQL on Render (persistent); SQLite locally
database_url = os.environ.get("DATABASE_URL", "sqlite:///app.db")
if "postgresql" in database_url.lower() or "postgres" in database_url.lower():
    # Render and other hosts use PostgreSQL for persistent storage (profiles survive restarts)
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://"):]
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    logger.warning("Using PostgreSQL database (persistent)")
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
engine_options = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
}
if "postgresql" in app.config["SQLALCHEMY_DATABASE_URI"].lower():
    engine_options["pool_size"] = 3
    engine_options["max_overflow"] = 5
else:
    engine_options["pool_size"] = 3
    engine_options["max_overflow"] = 5
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_options
db.init_app(app)


@app.teardown_appcontext
def shutdown_session(exception=None):
    """Return DB connections to the pool after each request to prevent pool exhaustion."""
    db.session.remove()


@app.context_processor
def inject_current_shop():
    """Make the current shop available in all templates."""
    try:
        shop = get_current_shop()
        return {'current_shop': shop}
    except Exception:
        return {'current_shop': None}


# Configuration
# Note: Permanent folders are no longer created - all files use temporary storage
UPLOAD_FOLDER = 'uploads'  # Kept for backward compatibility in code, but not created
PROCESSED_FOLDER = 'processed'  # Kept for backward compatibility, but not created
PSD_FRAMES_FOLDER = 'psd_frames'  # Kept for backward compatibility, but not created
FRAME_LIBRARY_FOLDER = os.environ.get(
    'FRAME_LIBRARY_FOLDER',
    os.path.join(temp_file_service.get_temp_dir(), 'frame_library')
)
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'}

SEO_METAFIELD_KEYS = [
    'subject', 'room', 'mood', 'palette', 'audience', 'occasion', 'season',
    'composition', 'display_suggestion', 'material', 'art_movement',
    'art_style', 'artwork_authenticity', 'orientation',
]

MANUAL_METAFIELD_KEYS = ['color', 'frame_style', 'theme'] + SEO_METAFIELD_KEYS

# Scheduled cleanup job for temporary files
_cleanup_started = False

def start_cleanup_scheduler():
    """Start background thread for scheduled cleanup of old temp files.
    Safe to call multiple times (e.g. from gunicorn post_fork hook) — only starts once per process."""
    global _cleanup_started
    if _cleanup_started:
        return
    _cleanup_started = True

    def cleanup_worker():
        import time
        while True:
            try:
                time.sleep(3600)  # Run every hour
                cleanup_count = temp_file_service.cleanup_old_files(max_age_hours=24)
                logger.info(f"Scheduled cleanup completed: {cleanup_count} files removed")
            except Exception as e:
                logger.error(f"Error in scheduled cleanup: {e}")

    cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True)
    cleanup_thread.start()
    logger.info("Scheduled cleanup job started (runs every hour, removes files older than 24h)")

# Start cleanup scheduler on app initialization
start_cleanup_scheduler()

# Processing queue
processing_queue = {}
# Reentrant because some error paths update queue state and then persist it via
# save_queue_to_disk(), which also snapshots under queue_lock. A plain Lock can
# deadlock the task thread while holding the queue lock, causing every status
# poll to hang behind it.
queue_lock = threading.RLock()
# Limit concurrent task processing to prevent overwhelming APIs and memory on free tier
_processing_semaphore = threading.Semaphore(1)  # Only 1 task processes at a time
# Event to wake the queue worker when new tasks are enqueued
_task_ready_event = threading.Event()
# Tasks handled by a "direct runner" thread (local-worker ready-framed and raw
# artwork uploads). The global queue worker must NOT also process these, or it
# spins on the still-"queued" task. Maps task_id -> Thread. Registered under
# queue_lock when the runner is launched; an empty registry after a restart lets
# the worker adopt orphaned tasks so nothing gets stuck.
_direct_runner_threads = {}

def ensure_env_in_thread():
    """Ensure environment variables are available in current thread context.
    This is critical because background threads may not inherit environment variables
    set in the parent process on Windows.
    Note: SHOPIFY_ACCESS_TOKEN/SHOPIFY_STORE_URL are no longer required as env vars
    — they come from the per-user Shop model via OAuth.
    """
    import os
    required_vars = [
        'GEMINI_API_KEY',
    ]
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    if missing_vars:
        logger.error(f"Missing required environment variables in thread: {missing_vars}")
        raise ValueError(f"Missing environment variables: {', '.join(missing_vars)}")

_save_lock = threading.Lock()

def save_queue_to_disk():
    """Save processing queue to disk for persistence (saves to temp directory)"""
    try:
        temp_dir = temp_file_service.get_temp_dir()
        os.makedirs(temp_dir, exist_ok=True)
        queue_file = os.path.join(temp_dir, 'processing_queue.json')

        # Serialize all saves so no two writes can race
        with _save_lock:
            with queue_lock:
                queue_copy = copy.deepcopy(processing_queue)
            # Write atomically via temp file + os.replace
            tmp = queue_file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(queue_copy, f, indent=2, default=str)
            os.replace(tmp, queue_file)
        logger.info(f"Saved {len(queue_copy)} tasks to disk")
    except Exception as e:
        logger.error(f"Failed to save queue to disk: {e}")
        import traceback
        logger.error(traceback.format_exc())

def load_queue_from_disk():
    """Load processing queue from disk to restore state and cleanup old temp files"""
    try:
        # Try to load from temp directory first, then fallback to old location
        temp_dir = temp_file_service.get_temp_dir()
        queue_file = os.path.join(temp_dir, 'processing_queue.json')
        
        if not os.path.exists(queue_file):
            # Fallback to old location for backward compatibility
            queue_file = os.path.join(UPLOAD_FOLDER, 'processing_queue.json')
        
        if os.path.exists(queue_file):
            with open(queue_file, 'r') as f:
                loaded_queue = json.load(f)
            
            # Validate file paths - keep completed/error tasks even if files are gone
            cleaned_tasks = {}
            for task_id, task in loaded_queue.items():
                status = task.get('status', '')

                # Always keep completed/error tasks so they persist in the queue history
                if status in ['completed', 'error']:
                    cleaned_tasks[task_id] = task
                    continue

                # CRITICAL: any task still ACTIVE when the process died is marked
                # as an ERROR on restart — it is NEVER auto-reprocessed.
                #
                # The old behaviour reset 'processing' -> 'queued' and let the
                # queue worker pick it up again. But if that task is what crashed
                # the worker (e.g. a create step that wedged the free-tier box),
                # re-running it just crashes the instance again -> a crash-LOOP:
                # the instance flaps up/down for 30+ minutes, never stabilising,
                # because /tmp persists across crash-restarts so the killer task
                # keeps coming back. The stateless WP worker can't hit this — it
                # simply loses in-flight work on restart. Match that: fail the
                # interrupted task cleanly so the instance comes back healthy.
                # The user re-drops the design to retry (desktop now allows that).
                task['status'] = 'error'
                task['current_step'] = 'Interrupted by a server restart'
                task['error'] = (
                    'Processing was interrupted by a server restart. '
                    'Re-drop the artwork to try again.'
                )
                task.pop('_direct_runner_started', None)
                task.pop('_direct_ready_runner_started', None)
                logger.warning(f"Marked interrupted task {task_id} as 'error' (was '{status}') to avoid crash-loop on restart")
                cleaned_tasks[task_id] = task
            
            with queue_lock:
                processing_queue.clear()
                processing_queue.update(cleaned_tasks)
            
            logger.info(f"Loaded {len(cleaned_tasks)} tasks from disk (skipped {len(loaded_queue) - len(cleaned_tasks)} tasks with missing files)")
        else:
            logger.info("No saved queue found, starting fresh")
    except Exception as e:
        logger.error(f"Failed to load queue from disk: {e}")
        import traceback
        logger.error(traceback.format_exc())
        with queue_lock:
            processing_queue.clear()

# Load existing queue on startup
load_queue_from_disk()

def _queue_worker():
    """Background worker that processes queued tasks sequentially.
    Replaces per-task thread spawning to prevent thread contention and API flooding.
    Wrapped in a top-level try/except so the thread never dies silently."""
    logger.info("Queue worker thread started (pid=%s)", os.getpid())
    while True:
        try:
            # Wait for signal that a new task is queued (with 5s polling fallback)
            _task_ready_event.wait(timeout=5)
            _task_ready_event.clear()

            # Statuses the queue worker handles
            _WORKER_STATUSES = {'queued', 'queued_continue', 'queued_approve', 'queued_ready_framed_approve'}

            while True:
                # Find the oldest task with a worker-handled status (by created_at)
                next_task_id = None
                next_task = None
                task_status = None
                with queue_lock:
                    oldest_time = None
                    for tid, t in processing_queue.items():
                        if t.get('status') in _WORKER_STATUSES:
                            # Skip tasks a direct-runner thread already owns in this
                            # process. Without this the worker re-selects the still
                            # "queued" task in a tight loop and pegs the CPU. After a
                            # restart the registry is empty, so orphaned tasks are
                            # adopted and processed normally below.
                            if (t.get('_direct_ready_runner_started') or t.get('_direct_runner_started')) \
                                    and tid in _direct_runner_threads:
                                continue
                            t_created = t.get('created_at', '')
                            if oldest_time is None or t_created < oldest_time:
                                oldest_time = t_created
                                next_task_id = tid
                                next_task = t
                                task_status = t.get('status')

                if not next_task_id:
                    break  # No more queued tasks, go back to waiting

                logger.info(f"Queue worker picking up task {next_task_id} (status={task_status}, {next_task.get('filename', '?')})")

                _processing_semaphore.acquire()
                logger.info(f"Queue worker acquired semaphore for task {next_task_id}")
                try:
                    if task_status == 'queued':
                        # Original new-task processing.
                        # NOTE: tasks whose direct runner is still live are skipped at
                        # selection time, so reaching here with a direct flag set means
                        # the task is orphaned (its runner died, e.g. after a restart).
                        # Clear the stale flag and adopt it instead of spinning.
                        if next_task.get('raw_artwork_workflow') and not next_task.get('image_paths'):
                            # Raw artwork: hand to a fresh direct runner (which renders
                            # the PSD frames). Clearing the stale flag lets the guarded
                            # starter actually launch.
                            with queue_lock:
                                processing_queue[next_task_id].pop('_direct_runner_started', None)
                            _start_direct_raw_artwork_runner(next_task_id)
                            continue
                        elif next_task.get('ready_framed'):
                            with queue_lock:
                                processing_queue[next_task_id].pop('_direct_ready_runner_started', None)
                            actual_filepath = next_task.get('filepath')
                            image_paths = next_task.get('image_paths', [])
                            if image_paths:
                                actual_filepath = image_paths[0]
                            if not actual_filepath or not os.path.exists(actual_filepath):
                                with queue_lock:
                                    processing_queue[next_task_id]['status'] = 'error'
                                    processing_queue[next_task_id]['error'] = 'File not found'
                                save_queue_to_disk()
                                continue
                            process_ready_framed_task(next_task_id, actual_filepath, next_task['filename'])
                        else:
                            filepath = next_task.get('filepath')
                            if not filepath or not os.path.exists(filepath):
                                with queue_lock:
                                    processing_queue[next_task_id]['status'] = 'error'
                                    processing_queue[next_task_id]['error'] = 'Original file not found'
                                save_queue_to_disk()
                                continue
                            process_image_task(next_task_id, filepath, next_task['filename'])

                    elif task_status == 'queued_continue':
                        # Continue processing after user review (metadata gen + Shopify upload)
                        continue_processing_task(next_task_id)

                    elif task_status == 'queued_approve':
                        # Approve and publish to Shopify
                        approve_args = next_task.get('_approve_args', {})
                        _approve_publish_background(
                            next_task_id,
                            next_task.get('metadata'),
                            next_task.get('filename'),
                            approve_args.get('publishing_settings', {}),
                            next_task.get('compressed_paths', []),
                            approve_args.get('shop_domain'),
                            approve_args.get('shop_token'),
                        )

                    elif task_status == 'queued_ready_framed_approve':
                        # Ready-framed approval processing
                        edited_data = next_task.get('_edited_data', {})
                        continue_ready_framed_processing(next_task_id, edited_data)

                except Exception as e:
                    logger.error(f"Queue worker error processing {next_task_id}: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                    with queue_lock:
                        if next_task_id in processing_queue:
                            processing_queue[next_task_id]['status'] = 'error'
                            processing_queue[next_task_id]['error'] = str(e)
                    save_queue_to_disk()
                finally:
                    _processing_semaphore.release()
                    logger.info(f"Queue worker released semaphore for task {next_task_id}")

                # Cooldown between tasks to prevent API flooding
                time.sleep(2)

        except Exception as e:
            # Top-level catch: log and continue so the worker never dies
            logger.error(f"Queue worker unexpected error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            time.sleep(5)  # Back off before retrying

# --- Lazy worker thread start (survives Gunicorn preload_app + fork) ---
_worker_thread = None
_worker_start_lock = threading.Lock()

def _ensure_worker_running():
    """Start the queue worker thread if it isn't running.
    Called lazily so it works after Gunicorn fork (preload_app kills threads)."""
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    with _worker_start_lock:
        # Double-check after acquiring lock
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _worker_thread = threading.Thread(target=_queue_worker, daemon=True, name="queue-worker")
        _worker_thread.start()
        logger.info("Queue worker thread launched (pid=%s)", os.getpid())


def _run_raw_artwork_task_now(task_id):
    """Run one raw-artwork task in a dedicated background thread.

    Render was leaving these tasks in queued state via the global queue scanner.
    This direct runner still uses the same semaphore and processing functions, but
    it is tied to the explicit /start_processing call so the task begins promptly.
    """
    logger.info("Direct raw artwork runner starting for task %s", task_id)
    _processing_semaphore.acquire()
    try:
        with queue_lock:
            task = processing_queue.get(task_id)
            if not task:
                logger.warning("Direct raw artwork runner could not find task %s", task_id)
                return
            if task.get('status') != 'queued':
                logger.info("Direct raw artwork runner skipping task %s with status %s", task_id, task.get('status'))
                return
            if not task.get('raw_artwork_workflow'):
                logger.info("Direct raw artwork runner skipping non-raw task %s", task_id)
                return
            task['current_step'] = 'Starting PSD frame generation...'
        save_queue_to_disk()

        actual_filepath = process_raw_artwork_framing_task(task_id)
        if not actual_filepath:
            return
        with queue_lock:
            task = processing_queue.get(task_id)
            if not task or task.get('status') == 'error':
                return
            filename = task.get('filename', 'artwork')
        process_ready_framed_task(task_id, actual_filepath, filename)
    except Exception as e:
        logger.error("Direct raw artwork runner failed for task %s: %s", task_id, e)
        import traceback
        logger.error(traceback.format_exc())
        with queue_lock:
            if task_id in processing_queue:
                processing_queue[task_id]['status'] = 'error'
                processing_queue[task_id]['current_step'] = f'Error: {str(e)[:140]}'
                processing_queue[task_id]['error'] = str(e)
        save_queue_to_disk()
    finally:
        with queue_lock:
            _direct_runner_threads.pop(task_id, None)
        _processing_semaphore.release()
        logger.info("Direct raw artwork runner finished for task %s", task_id)


def _start_direct_raw_artwork_runner(task_id):
    thread = None
    with queue_lock:
        task = processing_queue.get(task_id)
        if (
            task
            and task.get('raw_artwork_workflow')
            and task.get('status') == 'queued'
            and not task.get('_direct_runner_started')
        ):
            task['_direct_runner_started'] = True
            task['current_step'] = 'Starting PSD frame generation...'
            # Register the owning thread under the same lock that sets the flag (see
            # _start_direct_ready_framed_runner for why this must be atomic).
            thread = threading.Thread(
                target=_run_raw_artwork_task_now,
                args=(task_id,),
                daemon=True,
                name=f"raw-artwork-task-{task_id[:8]}",
            )
            _direct_runner_threads[task_id] = thread
    if thread is None:
        logger.info("Direct raw artwork runner not started for task %s; task is missing, not queued, or already started", task_id)
        return
    save_queue_to_disk()
    thread.start()
    logger.info("Direct raw artwork runner thread launched for task %s (pid=%s)", task_id, os.getpid())

# Add error handlers for file upload issues
@app.errorhandler(413)
def request_entity_too_large(error):
    """Handle file too large errors"""
    return jsonify({
        'error': 'File too large. Maximum file size is 300MB.',
        'max_size': '300MB'
    }), 413

@app.errorhandler(500)
def internal_server_error(error):
    """Handle internal server errors"""
    logger.error(f"Internal server error: {error}")
    return jsonify({
        'error': 'Internal server error occurred during file processing.',
        'message': 'Please try with a smaller file or contact support.'
    }), 500

# Add a test route to check authentication
@app.route('/auth_test')
def auth_test():
    from flask_login import current_user
    if current_user.is_authenticated:
        return jsonify({'authenticated': True, 'user_id': current_user.id, 'username': current_user.username})
    else:
        return jsonify({'authenticated': False})

# Add a test upload endpoint without @login_required
@app.route('/test_upload', methods=['POST'])
def test_upload():
    from flask_login import current_user
    logger.info(f"Test upload - authenticated: {current_user.is_authenticated}")
    logger.info(f"Test upload - session keys: {list(session.keys())}")
    logger.info(f"Test upload - session id: {session.get('_id')}")
    
    if current_user.is_authenticated:
        logger.info(f"Test upload - user: {current_user.username}")
        return jsonify({'success': True, 'message': 'Authentication working'})
    else:
        logger.info("Test upload - not authenticated")
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401

# Simplified upload endpoint for testing
@app.route('/upload_simple', methods=['POST'])
def upload_simple():
    """Simplified upload for testing without CSRF"""
    from flask_login import current_user
    logger.info(f"Simple upload - authenticated: {current_user.is_authenticated}")
    logger.info(f"Simple upload - files: {list(request.files.keys())}")
    
    if not current_user.is_authenticated:
        return jsonify({'error': 'Not authenticated'}), 401
        
    if 'files' not in request.files:
        return jsonify({'error': 'No files'}), 400
        
    files = request.files.getlist('files')
    logger.info(f"Processing {len(files)} files")
    
    tasks = []
    for file in files:
        if file and file.filename:
            task_id = str(uuid.uuid4())
            tasks.append({'task_id': task_id, 'filename': file.filename})
            logger.info(f"Created task {task_id} for {file.filename}")
    
    return jsonify({'tasks': tasks})

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def _parse_variants_data_from_request():
    variants_data_json = request.form.get('variants_data', '')
    if not variants_data_json:
        return []
    try:
        return json.loads(variants_data_json)
    except json.JSONDecodeError as e:
        logger.warning(f"Invalid variants data JSON: {e}")
        return []


def _looks_like_untouched_variant_defaults(variants):
    """True when a submitted variant set is just the form's starter row.

    Saving a profile from a page where the variant editor was never populated
    replaced a real 8-11 size price list with one placeholder row, and every
    listing published afterwards went live at the wrong price with one size.
    """
    rows = [row for row in (variants or []) if isinstance(row, dict)]
    if len(rows) != 1:
        return False
    row = rows[0]
    title = " ".join(str(row.get('title') or row.get('name') or '').split()).casefold()
    price = str(row.get('price') or '').strip()
    return title == 'a4 (21x30cm)' and price in {'29.99', '29.9', '29'}


def _default_poster_variants():
    return [
        {'title': '20x30 cm', 'price': '6.99', 'inventory_quantity': 999},
        {'title': '30x40 cm', 'price': '11.99', 'inventory_quantity': 999},
        {'title': '40x50 cm', 'price': '12.99', 'inventory_quantity': 999},
        {'title': '50x70 cm', 'price': '13.99', 'inventory_quantity': 999},
        {'title': 'A1 - 59.4 x 84.1 cm', 'price': '14.99', 'inventory_quantity': 999},
        {'title': 'A2 - 42 x 59.4 cm', 'price': '13.49', 'inventory_quantity': 999},
        {'title': 'A3 - 29.7 x 42 cm', 'price': '11.99', 'inventory_quantity': 999},
        {'title': 'A4 - 21 x 29.7 cm', 'price': '6.99', 'inventory_quantity': 999},
    ]


def _default_bulk_variant_rows(product, sku_pattern='', default_location_id=''):
    handle = (product.get('handle') or 'poster').strip() or 'poster'
    base_sku = (sku_pattern or handle).upper().replace('-', '')[:24] or 'POSTER'
    rows = []
    for index, variant in enumerate(_default_poster_variants(), start=1):
        rows.append({
            'title': variant['title'],
            'price': variant['price'],
            'inventory_quantity': variant.get('inventory_quantity', 999),
            'inventory_location_id': default_location_id,
            'apply_inventory': bool(default_location_id),
            'inventory_policy': 'CONTINUE',
            'sku': f"{base_sku}-V{index}",
            'option1_name': 'Size',
            'option1_value': variant['title'],
        })
    return rows


def _has_weak_single_default_variant(product):
    variants = product.get('variants') or []
    if len(variants) != 1:
        return False
    variant = variants[0] or {}
    title = (variant.get('title') or variant.get('option1_value') or '').strip().lower()
    price = str(variant.get('price') or product.get('variant_price') or '').strip()
    return title in ('', 'default title', 'default') or price in ('', '0', '0.00')


def _collect_listing_settings_from_request():
    """Collect the common settings that ready-framed processing expects."""
    settings = {
        'custom_prompt': request.form.get('custom_prompt', '').strip(),
        'variants_data': _parse_variants_data_from_request(),
        'business_name': request.form.get('business_name', '').strip(),
        'product_type': request.form.get('product_type', '').strip(),
        'vendor': request.form.get('vendor', '').strip(),
        'title_manual': request.form.get('title_manual', 'false').lower() == 'true',
        'description_manual': request.form.get('description_manual', 'false').lower() == 'true',
        'category_manual': request.form.get('category_manual', 'false').lower() == 'true',
        'product_type_manual': request.form.get('product_type_manual', 'false').lower() == 'true',
        'tags_manual': request.form.get('tags_manual', 'false').lower() == 'true',
        'collections_manual': request.form.get('collections_manual', 'false').lower() == 'true',
        'collections_enabled': request.form.get('collections_enabled', 'true').lower() == 'true',
        'vendor_manual': request.form.get('vendor_manual', 'false').lower() == 'true',
        'sku_manual': request.form.get('sku_manual', 'false').lower() == 'true',
        'handle_manual': request.form.get('handle_manual', 'false').lower() == 'true',
        'color_manual': request.form.get('color_manual', 'false').lower() == 'true',
        'metafield1_manual': request.form.get('metafield1_manual', 'false').lower() == 'true',
        'metafield2_manual': request.form.get('metafield2_manual', 'false').lower() == 'true',
        'condition_manual': request.form.get('condition_manual', 'false').lower() == 'true',
        'decoration_material_manual': request.form.get('decoration_material_manual', 'false').lower() == 'true',
        'artwork_frame_material_manual': request.form.get('artwork_frame_material_manual', 'false').lower() == 'true',
        'seo_title_manual': request.form.get('seo_title_manual', 'false').lower() == 'true',
        'meta_desc_manual': request.form.get('meta_desc_manual', 'false').lower() == 'true',
        'manual_title': request.form.get('manual_title', '').strip(),
        'manual_description': request.form.get('manual_description', '').strip(),
        'manual_category': request.form.get('manual_category', '').strip(),
        'manual_category_gid': request.form.get('manual_category_gid', '').strip(),
        'manual_tags': request.form.get('manual_tags', '').strip(),
        'manual_collections': request.form.get('manual_collections', '').strip(),
        'manual_sku': request.form.get('manual_sku', '').strip(),
        'manual_handle': request.form.get('manual_handle', '').strip(),
        'manual_color': request.form.get('manual_color', '').strip(),
        'manual_metafield1': request.form.get('manual_metafield1', '').strip(),
        'manual_metafield2': request.form.get('manual_metafield2', '').strip(),
        'manual_condition': request.form.get('manual_condition', '').strip(),
        'manual_decoration_material': request.form.get('manual_decoration_material', '').strip(),
        'manual_artwork_frame_material': request.form.get('manual_artwork_frame_material', '').strip(),
        'manual_seo_title': request.form.get('manual_seo_title', '').strip(),
        'manual_meta_desc': request.form.get('manual_meta_desc', '').strip(),
        'manual_google_product_category': request.form.get('manual_google_product_category', '').strip(),
        'manual_gender': request.form.get('manual_gender', '').strip(),
        'manual_age_group': request.form.get('manual_age_group', '').strip(),
        'manual_gs_condition': request.form.get('manual_gs_condition', '').strip(),
        'manual_custom_product': request.form.get('manual_custom_product', '').strip(),
        'manual_custom_label_0': request.form.get('manual_custom_label_0', '').strip(),
        'manual_custom_label_1': request.form.get('manual_custom_label_1', '').strip(),
        'manual_custom_label_2': request.form.get('manual_custom_label_2', '').strip(),
        'manual_custom_label_3': request.form.get('manual_custom_label_3', '').strip(),
        'manual_custom_label_4': request.form.get('manual_custom_label_4', '').strip(),
        'google_shopping_enabled': request.form.get('google_shopping_enabled', 'true').lower() == 'true',
        'google_category_manual': request.form.get('google_category_manual', 'false').lower() == 'true',
        'gender_manual': request.form.get('gender_manual', 'false').lower() == 'true',
        'age_group_manual': request.form.get('age_group_manual', 'false').lower() == 'true',
        'gs_condition_manual': request.form.get('gs_condition_manual', 'false').lower() == 'true',
        'custom_product_manual': request.form.get('custom_product_manual', 'false').lower() == 'true',
        'custom_label_0_manual': request.form.get('custom_label_0_manual', 'false').lower() == 'true',
        'custom_label_1_manual': request.form.get('custom_label_1_manual', 'false').lower() == 'true',
        'custom_label_2_manual': request.form.get('custom_label_2_manual', 'false').lower() == 'true',
        'custom_label_3_manual': request.form.get('custom_label_3_manual', 'false').lower() == 'true',
        'custom_label_4_manual': request.form.get('custom_label_4_manual', 'false').lower() == 'true',
        'product_status': request.form.get('product_status', 'ACTIVE').strip(),
        'review_before_publish': request.form.get('review_before_publish', 'false').lower() == 'true',
        'inventory_quantity': request.form.get('inventory_quantity', '999').strip(),
        'inventory_policy': request.form.get('inventory_policy', 'continue').strip(),
        'selected_sales_channels': session.get('selected_sales_channels', []),
        'selected_markets': session.get('selected_markets', []),
    }
    for key in SEO_METAFIELD_KEYS:
        settings[f'{key}_manual'] = request.form.get(f'{key}_manual', 'false').lower() == 'true'
        settings[f'manual_{key}'] = request.form.get(f'manual_{key}', '').strip()
    settings['frame_style_manual'] = settings.get('metafield1_manual', False)
    settings['theme_manual'] = settings.get('metafield2_manual', False)
    settings['manual_frame_style'] = settings.get('manual_metafield1', '')
    settings['manual_theme'] = settings.get('manual_metafield2', '')
    return settings

def _upgrade_profile_sections_in_place(sections_json, marker, required_directives):
    """Apply the same in-place upgrade to the editable prompt sections.

    Returns the JSON string to store. Directives land in the first section that
    already carries the version marker, otherwise the first section, so they stay
    visible and editable in the app.
    """
    try:
        sections = json.loads(sections_json) if sections_json else []
    except (TypeError, ValueError):
        return sections_json
    if not isinstance(sections, list) or not sections:
        return sections_json

    target = next(
        (sec for sec in sections
         if isinstance(sec, dict) and re.search(r'PROFILE\s+RULES\s+VERSION', str(sec.get('content') or ''), re.I)),
        None,
    )
    if target is None:
        target = sections[0] if isinstance(sections[0], dict) else None
    if target is None:
        return sections_json

    joined = chr(10).join(str(sec.get('content') or '') for sec in sections if isinstance(sec, dict))
    missing = [
        directive for directive in required_directives
        if not re.search(r'^\s*-?\s*' + re.escape(directive.split(':', 1)[0].strip()) + r'\s*:',
                         joined, flags=re.I | re.M)
    ]
    target['content'] = _upgrade_profile_prompt_in_place(
        str(target.get('content') or ''), marker, missing
    )
    return json.dumps(sections, ensure_ascii=False)


def _upgrade_profile_prompt_in_place(prompt_text, marker, required_directives):
    """Move a saved profile to the current rules version without losing edits.

    Only two things change: the version number, and any required directive line
    that is absent. Everything the user wrote is left exactly as it is.
    """
    text = str(prompt_text or '')
    version = marker.rsplit(':', 1)[-1].strip()
    text = re.sub(
        r'(PROFILE\s+RULES\s+VERSION\s*:\s*)\S+',
        lambda m: m.group(1) + version,
        text,
        flags=re.I,
    )
    additions = []
    for directive in required_directives:
        key = directive.split(':', 1)[0].strip()
        if not re.search(r'^\s*-?\s*' + re.escape(key) + r'\s*:', text, flags=re.I | re.M):
            additions.append('- ' + directive)
    if additions:
        newline = chr(10)
        text = text.rstrip() + newline + newline.join(additions) + newline
    return text


def _profile_to_listing_settings(user_id, profile_name=None):
    """Load a saved Ready Images profile as task settings for non-browser workers."""
    from models import UserProfile

    profile = None
    profile_name = (profile_name or os.environ.get('LOCAL_WORKER_PROFILE_NAME') or '').strip()
    if profile_name:
        profile = UserProfile.query.filter_by(
            user_id=user_id,
            profile_name=profile_name,
            profile_type='ready'
        ).first()
        if not profile:
            profile = UserProfile.query.filter_by(
                user_id=user_id,
                profile_name=profile_name
            ).first()
        if not profile:
            raise ValueError(f'Ready Images profile "{profile_name}" was not found')
    if not profile:
        profile = UserProfile.query.filter_by(
            user_id=user_id,
            profile_type='ready'
        ).order_by(UserProfile.updated_at.desc(), UserProfile.created_at.desc()).first()
    if not profile:
        profile = UserProfile.query.filter_by(user_id=user_id).order_by(
            UserProfile.updated_at.desc(),
            UserProfile.created_at.desc()
        ).first()
    if not profile:
        return {
            'custom_prompt': '',
            'variants_data': _default_poster_variants(),
            'business_name': '',
            'product_type': '',
            'vendor': '',
            'product_status': 'ACTIVE',
            'review_before_publish': False,
            'inventory_quantity': '999',
            'inventory_policy': 'continue',
            'selected_sales_channels': [],
            'selected_markets': [],
            'collections_enabled': True,
            'google_shopping_enabled': True,
        }

    # Upgrade the editable saved profile when it is selected. This does not
    # alter variants, prices, publishing settings, or any other profile data.
    # It avoids relying on a Render Blueprint release command having been
    # re-applied to an existing service.
    from migrate_saved_profile_prompt import (
        PROFILE_MARKER, REQUIRED_PROFILE_DIRECTIVES, build_profile_prompt,
    )
    current_prompt = profile.custom_prompt or ''
    if PROFILE_MARKER not in current_prompt:
        if 'PROFILE RULES VERSION' in current_prompt:
            # An older version of this profile. Only add the machine-read
            # directive lines it is missing and move the version number on.
            # Never rebuild the whole prompt: that would silently discard every
            # edit made in the app.
            current_prompt = _upgrade_profile_prompt_in_place(
                current_prompt, PROFILE_MARKER, REQUIRED_PROFILE_DIRECTIVES
            )
            profile.custom_prompt = current_prompt
            # Keep the editable sections in step, or the app would show the old
            # text and saving from the app would drop the directive again.
            profile.custom_prompt_sections = _upgrade_profile_sections_in_place(
                getattr(profile, 'custom_prompt_sections', None),
                PROFILE_MARKER,
                REQUIRED_PROFILE_DIRECTIVES,
            )
        else:
            # No version marker at all - a pre-migration profile, so build it.
            current_prompt, upgraded_sections = build_profile_prompt()
            profile.custom_prompt = current_prompt
            profile.custom_prompt_sections = upgraded_sections
        db.session.commit()
        logger.warning(
            'Upgraded selected Ready Images profile %s to %s',
            profile.profile_name,
            PROFILE_MARKER,
        )

    prompt_fingerprint = hashlib.sha256(current_prompt.encode('utf-8')).hexdigest()[:12]
    required_fact_count = len(re.findall(
        r'^\s*-?\s*REQUIRED\s+DESCRIPTION\s+FACT\s*:',
        current_prompt,
        flags=re.I | re.M,
    ))

    variants_data = _parse_json_list(profile.variants_data)
    if not variants_data:
        variants_data = _default_poster_variants()
        logger.warning(
            "Profile %s has no variants_data; using default poster size variants for local-worker listing",
            profile.profile_name,
        )

    settings = {
        'custom_prompt': current_prompt,
        'custom_prompt_sections': _parse_json_list(getattr(profile, 'custom_prompt_sections', None)),
        'variants_data': variants_data,
        'business_name': '',
        'product_type': profile.product_type or '',
        'vendor': profile.product_vendor or '',
        'product_status': (profile.listing_status or 'ACTIVE').upper(),
        'review_before_publish': bool(profile.review_before_publish),
        'inventory_quantity': profile.inventory_quantity or '999',
        'inventory_policy': profile.inventory_policy or 'continue',
        'selected_sales_channels': _parse_json_list(profile.selected_channels),
        'selected_markets': _parse_json_list(profile.selected_markets),
        'profile_name': profile.profile_name,
        'profile_rules_version': PROFILE_MARKER.rsplit(':', 1)[-1].strip(),
        'prompt_fingerprint': prompt_fingerprint,
        'required_description_fact_count': required_fact_count,
        'product_faq_required': bool(re.search(
            r'^\s*-?\s*REQUIRE\s+(?:PRODUCT|PRODUCT[-\s]+SPECIFIC|GENERIC|STORE[-\s]+WIDE)\s+FAQ\s*:\s*(yes|true|1)\s*$',
            current_prompt,
            flags=re.I | re.M,
        )),
    }

    bool_fields = [
        'title_manual', 'description_manual', 'category_manual',
        'product_type_manual', 'tags_manual', 'collections_manual',
        'collections_enabled', 'vendor_manual', 'sku_manual',
        'handle_manual', 'color_manual', 'frame_style_manual',
        'theme_manual', 'condition_manual', 'decoration_material_manual',
        'artwork_frame_material_manual', 'seo_title_manual', 'meta_desc_manual',
        'google_shopping_enabled', 'google_category_manual', 'gender_manual',
        'age_group_manual', 'gs_condition_manual', 'custom_product_manual',
        'custom_label_0_manual', 'custom_label_1_manual',
        'custom_label_2_manual', 'custom_label_3_manual', 'custom_label_4_manual',
    ] + [f'{key}_manual' for key in SEO_METAFIELD_KEYS]
    text_fields = [
        'manual_title', 'manual_description', 'manual_category',
        'manual_category_gid', 'manual_tags', 'manual_collections',
        'manual_sku', 'manual_handle', 'manual_color',
        'manual_frame_style', 'manual_theme', 'manual_condition',
        'manual_decoration_material', 'manual_artwork_frame_material',
        'manual_seo_title', 'manual_meta_desc',
        'manual_google_product_category', 'manual_gender',
        'manual_age_group', 'manual_gs_condition', 'manual_custom_product',
        'manual_custom_label_0', 'manual_custom_label_1',
        'manual_custom_label_2', 'manual_custom_label_3',
        'manual_custom_label_4',
    ] + [f'manual_{key}' for key in SEO_METAFIELD_KEYS]
    for field in bool_fields:
        settings[field] = bool(getattr(profile, field, False))
    for field in text_fields:
        settings[field] = getattr(profile, field, None) or ''

    settings['metafield1_manual'] = settings['frame_style_manual']
    settings['metafield2_manual'] = settings['theme_manual']
    settings['manual_metafield1'] = settings['manual_frame_style']
    settings['manual_metafield2'] = settings['manual_theme']
    return settings


def _record_effective_prompt_audit(task_id, task, custom_prompt):
    """Record the exact editable prompt contract used by a listing job."""
    prompt_text = str(custom_prompt or '')
    version_match = re.search(r'PROFILE\s+RULES\s+VERSION\s*:\s*([^\s]+)', prompt_text, flags=re.I)
    rules_version = version_match.group(1) if version_match else ''
    fact_count = len(re.findall(
        r'^\s*-?\s*REQUIRED\s+DESCRIPTION\s+FACT\s*:',
        prompt_text,
        flags=re.I | re.M,
    ))
    faq_required = bool(re.search(
        r'^\s*-?\s*REQUIRE\s+(?:PRODUCT|PRODUCT[-\s]+SPECIFIC|GENERIC|STORE[-\s]+WIDE)\s+FAQ\s*:\s*(yes|true|1)\s*$',
        prompt_text,
        flags=re.I | re.M,
    ))
    fingerprint = hashlib.sha256(prompt_text.encode('utf-8')).hexdigest()[:12] if prompt_text else ''
    if task.get('profile_name') and not rules_version:
        raise ValueError(
            f'Selected profile "{task.get("profile_name")}" has no profile rules version; listing was not published.'
        )
    audit = {
        'profile_rules_version': rules_version,
        'prompt_fingerprint': fingerprint,
        'required_description_fact_count': fact_count,
        'product_faq_required': faq_required,
    }
    task.update(audit)
    with queue_lock:
        if task_id in processing_queue:
            processing_queue[task_id].update(audit)
    logger.warning(
        'Effective prompt audit task=%s profile=%s version=%s fingerprint=%s required_facts=%s faq_required=%s',
        task_id,
        task.get('profile_name') or '(unsaved)',
        rules_version or '(none)',
        fingerprint or '(none)',
        fact_count,
        faq_required,
    )
    return audit


def _local_worker_auth_error():
    configured_token = (os.environ.get('LOCAL_WORKER_TOKEN') or '').strip()
    if not configured_token:
        return jsonify({'error': 'LOCAL_WORKER_TOKEN is not configured on the server'}), 503
    auth_header = request.headers.get('Authorization', '')
    submitted_token = ''
    if auth_header.lower().startswith('bearer '):
        submitted_token = auth_header.split(' ', 1)[1].strip()
    else:
        submitted_token = (request.form.get('token') or '').strip()
    if not submitted_token or not hmac.compare_digest(submitted_token, configured_token):
        return jsonify({'error': 'Unauthorized local worker request'}), 401
    return None


def _resolve_local_worker_user_id():
    from models import User

    configured_user_id = (os.environ.get('LOCAL_WORKER_USER_ID') or '').strip()
    if configured_user_id:
        return configured_user_id
    users = User.query.limit(2).all()
    if len(users) == 1:
        return users[0].id
    return None


def _get_active_shop_for_user(user_id):
    from models import Shop
    return Shop.query.filter_by(user_id=user_id, is_active=True).first()


def _category_enrichment_image(task, fallback_image):
    """Use the AI analysis image for category attributes when available."""
    if isinstance(task, dict):
        analysis_image = task.get('primary_compressed_path') or task.get('compressed_path')
        if analysis_image and os.path.exists(analysis_image):
            return analysis_image
    return fallback_image


def _preselected_category_gid(task):
    """
    Return a category GID known before AI runs.

    Category attribute options can only be fed into the single AI image analysis
    if the category is already known from saved/manual settings.
    """
    if not isinstance(task, dict):
        return ''

    sources = []
    fresh_settings = task.get('fresh_settings')
    if isinstance(fresh_settings, dict):
        sources.append(fresh_settings)
    sources.append(task)

    for source in sources:
        gid = (source.get('manual_category_gid') or '').strip()
        if gid:
            return gid

        if source.get('category_manual') and source.get('manual_category'):
            resolved_gid = resolve_category_name_to_gid(source.get('manual_category', '').strip())
            if resolved_gid:
                return resolved_gid

    return ''


def _category_attribute_options_for_task(task):
    """
    Fetch Shopify's allowed category attribute values before the AI call.
    Returns an empty dict on any failure so listing creation remains best-effort.
    """
    category_gid = _preselected_category_gid(task)
    if not category_gid:
        return {}

    try:
        options = get_category_attribute_options(
            category_gid,
            shop_domain=task.get('shop_domain') if isinstance(task, dict) else None,
            access_token=task.get('shop_access_token') if isinstance(task, dict) else None,
        )
        if options:
            logger.info("Loaded %s category attribute option groups for AI prompt", len(options))
        return options or {}
    except Exception as exc:
        logger.warning("Could not load category attribute options for %s: %s", category_gid, exc)
        return {}


def _metadata_retry_backoff_seconds(attempt):
    """Seconds to wait before retry number ``attempt`` (1-based, after a failure).

    Spreads five attempts over roughly three minutes so a temporary outage or a
    rate-limit burst recovers on its own instead of stopping the batch. Capped so
    a permanently broken setup still surfaces quickly.
    """
    schedule = [10, 20, 40, 75]
    try:
        override = os.environ.get("GEMINI_METADATA_RETRY_DELAYS", "").strip()
        if override:
            schedule = [max(0, int(part)) for part in override.split(",") if part.strip()]
    except ValueError:
        pass
    if not schedule:
        return 0
    return schedule[min(attempt, len(schedule)) - 1]


def _category_attribute_picks(metadata):
    if not isinstance(metadata, dict):
        return {}

    picks = metadata.get('category_attribute_picks')
    if not isinstance(picks, dict):
        picks = {}
    else:
        picks = copy.deepcopy(picks)

    metafields = metadata.get('metafields') if isinstance(metadata.get('metafields'), dict) else {}
    # Broad movements. A specific movement is always preferred, but "Contemporary"
    # and "Modernism" are real Shopify taxonomy values, so a product whose only
    # honest answer is a broad one still gets shopify.art-movement filled rather
    # than left blank. Values that are not in the store's allowed list are
    # dropped later by the metaobject match, so nothing invented can get through.
    generic_art_movements = {'contemporary', 'modern', 'modernism', 'illustrative', 'general'}

    for attr_name in list(picks.keys()):
        if str(attr_name).strip().casefold() == 'art movement':
            values = [
                str(value).strip()
                for value in (picks.get(attr_name) if isinstance(picks.get(attr_name), list) else [picks.get(attr_name)])
                if str(value or '').strip()
            ]
            if not values:
                picks.pop(attr_name, None)
                continue
            specific = [v for v in values if v.casefold() not in generic_art_movements]
            broad = [v for v in values if v.casefold() in generic_art_movements]
            picks[attr_name] = specific + broad

    def add_pick(attr_name, value):
        if value is None:
            return
        if isinstance(value, list):
            values = [str(v).strip() for v in value if str(v).strip()]
        else:
            values = [str(value).strip()] if str(value).strip() else []
        if not values:
            return
        existing = picks.get(attr_name)
        if existing is None:
            picks[attr_name] = values
        elif isinstance(existing, list):
            for item in values:
                if item not in existing:
                    existing.append(item)
        elif str(existing).strip():
            merged = [str(existing).strip()]
            for item in values:
                if item not in merged:
                    merged.append(item)
            picks[attr_name] = merged

    def split_pick_values(value):
        if value is None:
            return []
        if isinstance(value, list):
            values = []
            for item in value:
                values.extend(split_pick_values(item))
            return values
        return [
            ' '.join(part.split()).strip()
            for part in re.split(r',|/|\band\b', str(value), flags=re.I)
            if ' '.join(part.split()).strip()
        ]

    def color_pick_values(value):
        if value is None:
            return []
        if isinstance(value, list):
            raw_parts = []
            for item in value:
                raw_parts.extend(color_pick_values(item))
            return raw_parts
        text = str(value or '').replace('&', ',').replace(';', ',')
        parts = []
        for item in re.split(r',|/|\band\b', text, flags=re.I):
            clean = ' '.join(item.split()).strip(' .')
            if clean:
                parts.append(clean)
        expanded = []
        seen = set()
        fallback_map = {
            'teal': ['Teal', 'Blue', 'Green'],
            'deep teal': ['Teal', 'Blue', 'Green'],
            'turquoise': ['Turquoise', 'Blue', 'Green'],
            'mustard': ['Yellow'],
            'mustard yellow': ['Yellow'],
            'golden yellow': ['Yellow', 'Gold'],
            'forest green': ['Green'],
            'dark green': ['Green'],
            'light green': ['Green'],
            'red orange': ['Red', 'Orange'],
            'orange red': ['Orange', 'Red'],
            'cream': ['Cream', 'Beige', 'White'],
            'ivory': ['Ivory', 'Cream', 'White'],
            'gold': ['Gold', 'Yellow'],
            'navy': ['Navy', 'Blue'],
            'charcoal': ['Charcoal', 'Gray', 'Black'],
            'grey': ['Grey', 'Gray'],
            'multicolor': ['Multicolor', 'Multi-color'],
            'multi-color': ['Multi-color', 'Multicolor'],
        }
        canonical_colors = {
            'black', 'blue', 'brown', 'gold', 'gray', 'grey', 'green', 'orange',
            'pink', 'purple', 'red', 'silver', 'white', 'yellow', 'beige', 'cream'
        }
        for part in parts:
            candidates = []
            words = {word.casefold() for word in re.findall(r'[A-Za-z]+', part)}
            for color in canonical_colors:
                if color in words:
                    candidates.append('Gray' if color == 'grey' else color[:1].upper() + color[1:])
            candidates.extend(fallback_map.get(part.casefold(), []))
            candidates.append(part[:1].upper() + part[1:])
            for candidate in candidates:
                key = candidate.casefold()
                if key not in seen:
                    seen.add(key)
                    expanded.append(candidate)
        if len(expanded) > 1 and not any(item.casefold() in {'multicolor', 'multi-color'} for item in expanded):
            expanded.append('Multicolor')
        return expanded[:8]

    # Mirror generic AI metafields into Shopify category attributes. The setter
    # later checks these against the store's allowed metaobject choices, so
    # unsupported values are skipped safely.
    color_values = color_pick_values(metafields.get('palette') or metafields.get('color') or metadata.get('custom_label_2'))
    if color_values:
        add_pick('Color', color_values)
    else:
        add_pick('Color', metafields.get('color'))
    add_pick('Material', metafields.get('material') or metafields.get('decoration_material'))
    # Backfill from the custom metafield so the Shopify standard metafield is
    # never blank while custom.art_movement holds a value.
    art_movement = str(metafields.get('art_movement') or '').strip()
    if art_movement:
        add_pick('Art movement', art_movement)
    add_pick('Art style', split_pick_values(metafields.get('art_style')))
    add_pick('Artwork authenticity', metafields.get('artwork_authenticity'))
    add_pick('Frame style', metafields.get('frame_style'))
    add_pick('Orientation', metafields.get('orientation'))
    add_pick('Theme', split_pick_values(metafields.get('theme')))

    category_text = ' '.join(
        str(metadata.get(key, ''))
        for key in ('category', 'product_type', 'google_product_category')
    ).casefold()
    is_wall_art_print = any(term in category_text for term in ('poster', 'print', 'wall art', 'artwork'))
    if is_wall_art_print:
        add_pick('Material', 'Paper')
        add_pick('Artwork authenticity', 'Reproduction')
        add_pick('Frame style', 'Unframed')

    return picks


def _apply_measured_orientation(metadata, image_path, task=None):
    """Set orientation from the artwork's real pixel dimensions.

    The model guesses this from what it sees and gets it wrong on tall images
    with a lot of white space, which filed portrait prints as Square and left
    the Shopify orientation metafield empty. Measuring the file is exact.
    Skipped when the analysed image is a framed room mockup (its shape is the
    mockup's, not the artwork's) or when the user set orientation manually.
    """
    if not isinstance(metadata, dict) or not image_path:
        return metadata
    task = task or {}
    if task.get('analysis_is_framed'):
        return metadata
    if task.get('orientation_manual') and task.get('manual_orientation'):
        return metadata
    try:
        from gemini_utils import _orientation_from_image
        measured = _orientation_from_image(image_path)
    except Exception as exc:
        logger.warning('Could not measure orientation from %s: %s', image_path, exc)
        return metadata
    if not measured:
        return metadata
    metafields = metadata.get('metafields')
    if not isinstance(metafields, dict):
        metafields = {}
        metadata['metafields'] = metafields
    previous = str(metafields.get('orientation') or '').strip()
    if previous.casefold() != measured.casefold():
        logger.warning('Orientation corrected from %r to %r using the image dimensions', previous, measured)
    metafields['orientation'] = measured
    return metadata


def _apply_manual_metafield_overrides(metadata, task):
    """Apply profile/form metafield overrides before Shopify creation/enrichment."""
    if not isinstance(metadata, dict):
        return
    metafields = metadata.get('metafields')
    if not isinstance(metafields, dict):
        metafields = {}
        metadata['metafields'] = metafields

    for key in MANUAL_METAFIELD_KEYS:
        toggle_key = f'{key}_manual'
        value_key = f'manual_{key}'
        if task.get(toggle_key) and task.get(value_key):
            value = str(task.get(value_key) or '').strip()
            if value:
                metafields[key] = value

    # Keep older UI aliases wired to the explicit metafields.
    if task.get('metafield1_manual') and task.get('manual_metafield1'):
        metafields['frame_style'] = str(task.get('manual_metafield1') or '').strip()
    if task.get('metafield2_manual') and task.get('manual_metafield2'):
        metafields['theme'] = str(task.get('manual_metafield2') or '').strip()

    # Feed fields benefit from the same semantic data unless explicitly overridden.
    if task.get('palette_manual') and task.get('manual_palette') and not task.get('custom_label_2_manual'):
        metadata['custom_label_2'] = str(task.get('manual_palette') or '').strip()
    if task.get('subject_manual') and task.get('manual_subject') and not task.get('custom_label_3_manual'):
        metadata['custom_label_3'] = str(task.get('manual_subject') or '').strip()
    if task.get('audience_manual') and task.get('manual_audience') and not task.get('custom_label_4_manual'):
        metadata['custom_label_4'] = str(task.get('manual_audience') or '').strip()


def _run_ready_framed_task_now(task_id):
    """Run a ready-framed task immediately for non-browser automation."""
    logger.info("Direct ready-framed runner starting for task %s", task_id)
    _processing_semaphore.acquire()
    try:
        with queue_lock:
            task = processing_queue.get(task_id)
            if not task:
                logger.warning("Direct ready-framed runner could not find task %s", task_id)
                return
            if task.get('status') != 'queued':
                logger.info("Direct ready-framed runner skipping task %s with status %s", task_id, task.get('status'))
                return
            if not task.get('ready_framed') or task.get('raw_artwork_workflow'):
                logger.info("Direct ready-framed runner skipping incompatible task %s", task_id)
                return
            image_paths = task.get('image_paths') or []
            actual_filepath = image_paths[0] if image_paths else task.get('filepath')
            filename = task.get('filename') or 'framed-artwork.jpg'
            task['current_step'] = 'Starting local-worker listing task...'
        save_queue_to_disk()

        if not actual_filepath or not os.path.exists(actual_filepath):
            raise FileNotFoundError(f'Ready-framed file not found at {actual_filepath}')
        process_ready_framed_task(task_id, actual_filepath, filename)
    except Exception as e:
        logger.error("Direct ready-framed runner failed for task %s: %s", task_id, e)
        import traceback
        logger.error(traceback.format_exc())
        with queue_lock:
            if task_id in processing_queue:
                processing_queue[task_id]['status'] = 'error'
                processing_queue[task_id]['current_step'] = f'Error: {str(e)[:140]}'
                processing_queue[task_id]['error'] = str(e)
        save_queue_to_disk()
    finally:
        with queue_lock:
            _direct_runner_threads.pop(task_id, None)
        _processing_semaphore.release()
        logger.info("Direct ready-framed runner finished for task %s", task_id)


def _start_direct_ready_framed_runner(task_id):
    thread = None
    with queue_lock:
        task = processing_queue.get(task_id)
        if (
            task
            and task.get('ready_framed')
            and not task.get('raw_artwork_workflow')
            and task.get('status') == 'queued'
            and not task.get('_direct_ready_runner_started')
        ):
            task['_direct_ready_runner_started'] = True
            task['current_step'] = 'Starting local-worker listing task...'
            # Register the owning thread under the same lock that sets the flag, so
            # the queue worker never sees a flagged-but-unregistered task (which it
            # would otherwise adopt and double-process).
            thread = threading.Thread(
                target=_run_ready_framed_task_now,
                args=(task_id,),
                daemon=True,
                name=f"ready-worker-task-{task_id[:8]}",
            )
            _direct_runner_threads[task_id] = thread
    if thread is None:
        logger.info("Direct ready-framed runner not started for task %s; task is missing, not queued, or already started", task_id)
        return
    save_queue_to_disk()
    thread.start()
    logger.info("Direct ready-framed runner thread launched for task %s (pid=%s)", task_id, os.getpid())


def _serialize_frame_template(frame):
    return {
        'id': frame.id,
        'name': frame.display_name,
        'original_filename': frame.original_filename,
        'sort_order': frame.sort_order,
        'active': frame.active,
        'smart_layer_name': frame.smart_layer_name,
        'exists': os.path.exists(frame.file_path),
    }

@app.route('/api/frame_templates', methods=['GET'])
@login_required
def api_frame_templates():
    from models import UserFrameTemplate
    frames = UserFrameTemplate.query.filter_by(
        user_id=current_user.id,
        active=True
    ).order_by(UserFrameTemplate.sort_order.asc(), UserFrameTemplate.id.asc()).all()
    existing_frames = []
    missing_count = 0
    for frame in frames:
        if os.path.exists(frame.file_path):
            existing_frames.append(frame)
        else:
            missing_count += 1
            frame.active = False
    if missing_count:
        db.session.commit()
        logger.warning(
            "Deactivated %s missing PSD frame template(s) for user %s. Configure FRAME_LIBRARY_FOLDER on persistent storage to keep frames across deploys/restarts.",
            missing_count,
            current_user.id,
        )
    return jsonify({
        'success': True,
        'frames': [_serialize_frame_template(frame) for frame in existing_frames],
        'missing_frame_count': missing_count,
    })

@app.route('/upload_frame_template', methods=['POST'])
@login_required
def upload_frame_template():
    from models import UserFrameTemplate
    from smart_mockup_engine import validate_psd_template

    files = request.files.getlist('files') or request.files.getlist('file') or request.files.getlist('mockup_files')
    if not files:
        return jsonify({'success': False, 'error': 'No PSD/PSB files provided'}), 400

    user_dir = os.path.join(FRAME_LIBRARY_FOLDER, str(current_user.id))
    os.makedirs(user_dir, exist_ok=True)
    uploaded = []
    last_frame = UserFrameTemplate.query.filter_by(user_id=current_user.id).order_by(
        UserFrameTemplate.sort_order.desc()
    ).first()
    next_order = (last_frame.sort_order if last_frame else 0) + 1

    for file in files:
        if not file or not file.filename:
            continue
        if not file.filename.lower().endswith(('.psd', '.psb')):
            logger.warning(f"Skipping non-PSD frame upload: {file.filename}")
            continue

        original_filename = secure_filename(file.filename)
        stored_filename = f"{uuid.uuid4().hex}_{original_filename}"
        file_path = os.path.join(user_dir, stored_filename)
        file.save(file_path)

        try:
            validate_psd_template(file_path, smart_layer_name='1')
        except Exception as e:
            try:
                os.unlink(file_path)
            except OSError:
                pass
            return jsonify({
                'success': False,
                'error': f"{original_filename} is not valid: {e}"
            }), 400

        frame = UserFrameTemplate(
            user_id=current_user.id,
            display_name=os.path.splitext(original_filename)[0],
            original_filename=original_filename,
            stored_filename=stored_filename,
            file_path=file_path,
            smart_layer_name='1',
            sort_order=next_order,
            active=True,
        )
        db.session.add(frame)
        db.session.flush()
        uploaded.append(_serialize_frame_template(frame))
        next_order += 1

    if not uploaded:
        return jsonify({'success': False, 'error': 'No valid PSD/PSB files uploaded'}), 400

    db.session.commit()
    return jsonify({'success': True, 'frames': uploaded})

@app.route('/reorder_frame_templates', methods=['POST'])
@login_required
def reorder_frame_templates():
    from models import UserFrameTemplate
    data = request.get_json(silent=True) or {}
    frame_ids = data.get('frame_ids', [])
    if not isinstance(frame_ids, list):
        return jsonify({'success': False, 'error': 'frame_ids must be a list'}), 400

    frames = UserFrameTemplate.query.filter(
        UserFrameTemplate.user_id == current_user.id,
        UserFrameTemplate.id.in_(frame_ids)
    ).all()
    frames_by_id = {frame.id: frame for frame in frames}
    for index, frame_id in enumerate(frame_ids, start=1):
        frame = frames_by_id.get(int(frame_id))
        if frame:
            frame.sort_order = index
    db.session.commit()
    return jsonify({'success': True})

@app.route('/delete_frame_template/<int:frame_id>', methods=['DELETE'])
@login_required
def delete_frame_template(frame_id):
    from models import UserFrameTemplate
    frame = UserFrameTemplate.query.filter_by(id=frame_id, user_id=current_user.id).first()
    if not frame:
        return jsonify({'success': False, 'error': 'Frame not found'}), 404
    frame.active = False
    db.session.commit()
    return jsonify({'success': True})

@app.route('/upload_raw_artwork', methods=['POST'])
@login_required
def upload_raw_artwork():
    from models import UserFrameTemplate

    files = request.files.getlist('files') or request.files.getlist('file')
    if not files:
        return jsonify({'error': 'No artwork files provided'}), 400

    frames = UserFrameTemplate.query.filter_by(
        user_id=current_user.id,
        active=True
    ).order_by(UserFrameTemplate.sort_order.asc(), UserFrameTemplate.id.asc()).all()
    missing_frames = [frame for frame in frames if not os.path.exists(frame.file_path)]
    if missing_frames:
        for frame in missing_frames:
            frame.active = False
        db.session.commit()
    frames = [frame for frame in frames if os.path.exists(frame.file_path)]
    if not frames:
        return jsonify({
            'error': 'No usable PSD frames found. The saved frame records exist, but the PSD files are missing on the server. Upload the PSD frames again, or configure FRAME_LIBRARY_FOLDER on a persistent Render disk.'
        }), 400

    listing_settings = _collect_listing_settings_from_request()
    tasks = []
    frame_templates = [
        {
            'id': frame.id,
            'display_name': frame.display_name,
            'file_path': frame.file_path,
            'smart_layer_name': frame.smart_layer_name or '1',
        }
        for frame in frames
    ]

    for file in files:
        if not file or not file.filename or not allowed_file(file.filename):
            continue

        task_id = str(uuid.uuid4())
        original_filename = secure_filename(file.filename)
        ext = os.path.splitext(original_filename)[1] or '.jpg'
        raw_path = temp_file_service.create_temp_file(
            prefix='raw_artwork',
            suffix=ext,
            task_id=task_id
        )
        file.save(raw_path)

        display_filename = original_filename
        task_payload = {
            'id': task_id,
            'task_id': task_id,
            'filename': display_filename,
            'filepath': raw_path,
            'raw_artwork_path': raw_path,
            'image_paths': [],
            'filenames': [],
            'analysis_image_path': raw_path,
            'status': 'uploaded',
            'current_step': f'Queued for PSD framing ({len(frame_templates)} mockup frame(s))',
            'created_at': datetime.now().isoformat(),
            'ready_framed': True,
            'raw_artwork_workflow': True,
            'raw_frame_templates': frame_templates,
            'template_names': [frame['display_name'] for frame in frame_templates],
            'temp_files': [raw_path],
        }
        task_payload.update(listing_settings)

        with queue_lock:
            processing_queue[task_id] = task_payload
        tasks.append({
            'task_id': task_id,
            'filename': display_filename,
            'image_count': len(frame_templates),
        })

    if not tasks:
        return jsonify({'error': 'No valid artwork files uploaded'}), 400

    save_queue_to_disk()
    return jsonify({'tasks': tasks})


def process_raw_artwork_framing_task(task_id):
    """Render queued raw artwork into saved PSD frames before ready-framed processing."""
    from smart_mockup_engine import render_artwork_with_frame, slug

    ensure_env_in_thread()

    with app.app_context():
        try:
            with queue_lock:
                task = processing_queue.get(task_id)
                if not task:
                    return None
                raw_path = task.get('raw_artwork_path') or task.get('analysis_image_path') or task.get('filepath')
                frames = task.get('raw_frame_templates') or []
                original_filename = task.get('filename') or 'artwork'
                task['status'] = 'processing'
                task['current_step'] = f'Creating {len(frames)} framed mockup image(s)...'

            if not raw_path or not os.path.exists(raw_path):
                raise Exception('Raw artwork file not found')
            if not frames:
                raise Exception('No PSD frame templates saved on task')

            framed_paths = []
            framed_filenames = []
            artwork_slug = slug(os.path.splitext(original_filename)[0])

            for index, frame in enumerate(frames, start=1):
                psd_path = frame.get('file_path')
                display_name = frame.get('display_name') or f'frame-{index}'
                if not psd_path or not os.path.exists(psd_path):
                    raise Exception(f'PSD frame file missing: {display_name}')

                with queue_lock:
                    if task_id in processing_queue:
                        processing_queue[task_id]['current_step'] = f'Creating mockup {index}/{len(frames)}: {display_name}'

                output_path = temp_file_service.create_temp_file(
                    prefix='framed_mockup',
                    suffix='.jpg',
                    task_id=task_id
                )
                render_artwork_with_frame(
                    artwork_path=raw_path,
                    psd_path=psd_path,
                    output_jpg_path=output_path,
                    temp_dir=temp_file_service.create_temp_directory(prefix='smart_mockup', task_id=task_id),
                    smart_layer_name=frame.get('smart_layer_name') or '1',
                    fit_mode='stretch',
                )
                framed_paths.append(output_path)
                framed_filenames.append(f"{slug(display_name)}__{artwork_slug}.jpg")

            if not framed_paths:
                raise Exception('No framed mockup images were created')

            with queue_lock:
                if task_id in processing_queue:
                    task = processing_queue[task_id]
                    task['filepath'] = framed_paths[0]
                    task['image_paths'] = framed_paths
                    task['filenames'] = framed_filenames
                    task['current_step'] = f'Framed and ready for AI processing ({len(framed_paths)} mockup image(s))'
                    task.setdefault('temp_files', [])
                    task['temp_files'].extend(framed_paths)
            save_queue_to_disk()
            return framed_paths[0]

        except Exception as e:
            logger.error(f"Raw artwork queued framing failed for {task_id}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            with queue_lock:
                if task_id in processing_queue:
                    processing_queue[task_id]['status'] = 'error'
                    processing_queue[task_id]['current_step'] = f'Framing failed: {str(e)[:140]}'
                    processing_queue[task_id]['error'] = str(e)
            save_queue_to_disk()
            return None

def process_image_task(task_id, filepath, filename):
    """Background task to process a single image"""
    # CRITICAL: Ensure environment variables are available in this thread
    ensure_env_in_thread()

    # Semaphore handled by queue worker (caller)
    logger.info(f"Mockup task {task_id} starting processing")

    temp_files_to_cleanup = []

    try:
        with queue_lock:
            task = processing_queue.get(task_id)
            if not task:
                return
            task['status'] = 'processing'
            task['current_step'] = 'Compressing image...'
            if 'temp_files' not in task:
                task['temp_files'] = []
        
        # Step 1: Compress the original image (to temp directory)
        logger.info(f"Starting compression for {filename}")
        compressed_path = compress_image(filepath, output_folder=None, task_id=task_id)
        temp_files_to_cleanup.append(compressed_path)
        
        # Check if compression was successful
        if not compressed_path:
            raise Exception("Failed to compress image")
        
        with queue_lock:
            task['current_step'] = 'Processing PSD frames...'
            task['compressed_path'] = compressed_path
            task['temp_files'].append(compressed_path)
            save_queue_to_disk()
        
        # Step 2: Process PSD frames (output to temp)
        logger.info(f"Processing PSD frames for {filename}")
        frame_paths = process_psd_frames(compressed_path, output_folder=None, task_id=task_id)
        temp_files_to_cleanup.extend(frame_paths)
        
        if not frame_paths:
            raise Exception("Failed to process PSD frames")
        
        with queue_lock:
            task['current_step'] = 'Generating AI metadata...'
            task['temp_files'].extend(frame_paths)
        
        # Step 3: Store frame paths for user preview  
        with queue_lock:
            task['current_step'] = 'Framing complete - Ready for review'
            task['status'] = 'framed'
            # Store full paths for serving, but use basename for display
            task['frame_paths'] = frame_paths
            task['frame_paths_display'] = [os.path.basename(path) for path in frame_paths]
        
        logger.info(f"Framing completed for {filename} - awaiting user review")
        
    except Exception as e:
        logger.error(f"Error processing {filename}: {str(e)}")
        with queue_lock:
            task = processing_queue.get(task_id)
            if task:
                task['status'] = 'error'
                task['error'] = str(e)
        save_queue_to_disk()
        # Cleanup temp files on error
        for temp_file in temp_files_to_cleanup:
            try:
                if os.path.exists(temp_file):
                    os.unlink(temp_file)
            except:
                pass
    finally:
        logger.info(f"Mockup task {task_id} finished processing")

def create_local_mockup_fallback(image_path, template_name):
    """
    Create a professional mockup using local image processing as fallback
    when Dynamic Mockups API times out
    """
    try:
        # Optimize the image first 
        optimized_image = optimize_image_for_upload(image_path, max_size_mb=1.0)
        
        with Image.open(optimized_image) as img:
            # Convert to RGB if needed
            if img.mode in ('RGBA', 'LA', 'P'):
                img = img.convert('RGB')
            
            # Create professional poster frame effect
            width, height = img.size
            
            # Calculate frame proportions
            frame_thickness = max(width, height) // 20  # 5% of larger dimension
            shadow_offset = frame_thickness // 3
            
            # Create canvas with frame and shadow space
            canvas_width = width + (frame_thickness * 2) + shadow_offset
            canvas_height = height + (frame_thickness * 2) + shadow_offset
            
            # Create white background
            canvas = Image.new('RGB', (canvas_width, canvas_height), 'white')
            
            # Create shadow effect
            from PIL import ImageDraw, ImageFilter
            shadow = Image.new('RGBA', (canvas_width, canvas_height), (0, 0, 0, 0))
            shadow_draw = ImageDraw.Draw(shadow)
            
            # Draw shadow rectangle
            shadow_rect = [
                frame_thickness + shadow_offset,
                frame_thickness + shadow_offset, 
                frame_thickness + width + shadow_offset,
                frame_thickness + height + shadow_offset
            ]
            shadow_draw.rectangle(shadow_rect, fill=(0, 0, 0, 40))
            
            # Blur the shadow
            shadow = shadow.filter(ImageFilter.GaussianBlur(radius=shadow_offset//2))
            
            # Paste shadow onto canvas
            canvas.paste(shadow, (0, 0), shadow)
            
            # Create frame (white border)
            frame_img = Image.new('RGB', (width + frame_thickness*2, height + frame_thickness*2), 'white')
            
            # Add inner shadow to frame for depth
            frame_draw = ImageDraw.Draw(frame_img)
            inner_shadow_width = 2
            for i in range(inner_shadow_width):
                gray_level = 240 - (i * 10)
                color = (gray_level, gray_level, gray_level)
                frame_draw.rectangle([
                    frame_thickness - 1 - i, 
                    frame_thickness - 1 - i,
                    frame_thickness + width + i,
                    frame_thickness + height + i
                ], outline=color)
            
            # Paste the original image onto the frame
            frame_img.paste(img, (frame_thickness, frame_thickness))
            
            # Paste framed image onto canvas
            canvas.paste(frame_img, (0, 0))
            
            # Save the final mockup
            mockup_filename = f"fallback_mockup_{uuid.uuid4().hex[:8]}.jpg"
            mockup_path = os.path.join(PROCESSED_FOLDER, mockup_filename)
            
            canvas.save(mockup_path, 'JPEG', quality=92, optimize=True)
            
            return {
                'success': True,
                'mockup_url': f"/processed/{mockup_filename}",
                'mockup_path': mockup_path,
                'mockup_filename': mockup_filename,
                'method': 'local_fallback',
                'template_name': template_name
            }
            
    except Exception as e:
        logger.error(f"Local mockup fallback failed: {e}")
        return {
            'success': False,
            'error': f"Local mockup creation failed: {str(e)}"
        }

def get_frames_list():
    """Get list of active frames from filesystem in correct processing order"""
    try:
        frames = []
        
        # First, get uploaded PSDs (sorted by modification time, newest first)
        uploads_dir = UPLOAD_FOLDER
        if os.path.exists(uploads_dir):
            upload_files = []
            for filename in os.listdir(uploads_dir):
                if filename.lower().endswith(('.psd', '.psb')):
                    filepath = os.path.join(uploads_dir, filename)
                    try:
                        file_stats = os.stat(filepath)
                        size_mb = round(file_stats.st_size / (1024 * 1024), 2)
                        upload_files.append({
                            'name': filename.replace('.psd', '').replace('.psb', '').replace('_', ' '),
                            'filename': filename,
                            'path': filepath,
                            'type': 'Uploaded PSD',
                            'size': f"{size_mb}MB",
                            'mtime': file_stats.st_mtime
                        })
                    except OSError:
                        continue
            
            # Sort uploaded files by modification time (newest first for display order)
            upload_files.sort(key=lambda x: x['mtime'], reverse=True)
            for frame in upload_files:
                del frame['mtime']  # Remove mtime before adding to final list
                frames.append(frame)
        
        # Then, get preset frames
        psd_frames_dir = PSD_FRAMES_FOLDER
        if os.path.exists(psd_frames_dir):
            for filename in os.listdir(psd_frames_dir):
                if filename.lower().endswith(('.psd', '.psb')):
                    filepath = os.path.join(psd_frames_dir, filename)
                    try:
                        file_stats = os.stat(filepath)
                        size_mb = round(file_stats.st_size / (1024 * 1024), 2)
                        frames.append({
                            'name': filename.replace('.psd', '').replace('.psb', '').replace('_', ' '),
                            'filename': filename,
                            'path': filepath.replace('\\', '/'),  # Normalize to forward slashes
                            'type': 'Preset Frame',
                            'size': f"{size_mb}MB"
                        })
                    except OSError:
                        continue
        
        return frames
    except Exception as e:
        logger.error(f"Error getting frames list: {e}")
        return []

@app.route('/api/get_active_frames')
@login_required
def api_get_active_frames():
    """API endpoint to get current active frames data for real-time updates"""
    try:
        frames_data = get_frames_list()
        return jsonify({
            'success': True,
            'frames': frames_data,
            'total_frames': len(frames_data)
        })
    except Exception as e:
        logger.error(f"Error getting active frames: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'frames': []
        }), 500


@app.route('/api/get_psd_templates')
def api_get_psd_templates():
    """Get PSD templates for Photopea processing (no login required for client-side processing)"""
    try:
        frames_data = get_frames_list()
        # Filter only PSD files
        psd_templates = [f for f in frames_data if f.get('path', '').lower().endswith(('.psd', '.psb'))]
        logger.info(f"Found {len(psd_templates)} PSD templates for Photopea processing")
        return jsonify({
            'success': True,
            'frames': psd_templates,
            'total_frames': len(psd_templates)
        })
    except Exception as e:
        logger.error(f"Error getting PSD templates: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'frames': []
        }), 500

# --- Shopify Taxonomy Categories (cached in-memory for 24h) ---
_taxonomy_cache = {'data': None, 'fetched_at': 0}

@app.route('/api/taxonomy_categories')
def api_taxonomy_categories():
    """Return Shopify product taxonomy categories as JSON.
    Fetches from Shopify's public GitHub repo and caches 24h in-memory."""
    import time
    now = time.time()
    if _taxonomy_cache['data'] and (now - _taxonomy_cache['fetched_at']) < 86400:
        return jsonify(_taxonomy_cache['data'])

    categories = []
    try:
        url = 'https://raw.githubusercontent.com/Shopify/product-taxonomy/main/dist/en/categories.txt'
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        for line in resp.text.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            # Format: "gid://shopify/TaxonomyCategory/xx-yy-zz : Full > Path > Name"
            if ' : ' in line:
                gid, name = line.split(' : ', 1)
                categories.append({'gid': gid.strip(), 'name': name.strip()})
        logger.info(f"Fetched {len(categories)} taxonomy categories from GitHub")
    except Exception as e:
        logger.warning(f"Failed to fetch taxonomy from GitHub ({e}), using fallback")

    if not categories:
        # Hardcoded fallback subset
        categories = [
            {'gid': 'gid://shopify/TaxonomyCategory/hg-3-4', 'name': 'Home & Garden > Decor > Artwork'},
            {'gid': 'gid://shopify/TaxonomyCategory/hg-3-4-7', 'name': 'Home & Garden > Decor > Artwork > Posters, Prints, & Visual Artwork'},
            {'gid': 'gid://shopify/TaxonomyCategory/ae-1-6', 'name': 'Arts & Entertainment > Hobbies & Creative Arts > Arts & Crafts'},
            {'gid': 'gid://shopify/TaxonomyCategory/ae-1-1', 'name': 'Arts & Entertainment > Event Tickets'},
            {'gid': 'gid://shopify/TaxonomyCategory/hg-3-11', 'name': 'Home & Garden > Decor > Wall Art & Coverings'},
        ]

    _taxonomy_cache['data'] = categories
    _taxonomy_cache['fetched_at'] = now
    return jsonify(categories)


@app.route('/api/default_prompt')
def api_default_prompt():
    """Return the default AI prompt used for generating listing details."""
    try:
        prompt = get_default_prompt()
        sections = get_prompt_sections()
        # Only return editable sections (skip fully locked ones like output_format).
        # For sections with locked_content, return only the editable 'content' part.
        sections_list = [
            {'id': key, 'title': sec['title'], 'content': sec['content']}
            for key, sec in sections.items()
            if not sec.get('locked')
        ]
        return jsonify({'success': True, 'prompt': prompt, 'sections': sections_list})
    except Exception as e:
        logger.error(f"Error getting default prompt: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/baseline_prompt')
def api_baseline_prompt():
    """Return the store baseline prompt: app defaults plus the store profile rules.

    This is what Reset restores. Resetting to the bare app defaults silently drops
    the store's required description facts, FAQ questions and collection rules,
    which then stop being enforced on every listing.
    """
    try:
        from migrate_saved_profile_prompt import build_profile_prompt
        prompt, sections_json = build_profile_prompt()
        return jsonify({
            'success': True,
            'prompt': prompt,
            'sections': [
                {'id': item.get('id'), 'content': item.get('content', '')}
                for item in json.loads(sections_json)
            ],
        })
    except Exception as e:
        logger.error(f"Error getting baseline prompt: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def _ensure_taxonomy_loaded():
    """Ensure the taxonomy cache is populated (for server-side lookups)."""
    import time
    now = time.time()
    if _taxonomy_cache['data'] and (now - _taxonomy_cache['fetched_at']) < 86400:
        return
    try:
        url = 'https://raw.githubusercontent.com/Shopify/product-taxonomy/main/dist/en/categories.txt'
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        categories = []
        for line in resp.text.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if ' : ' in line:
                gid, name = line.split(' : ', 1)
                categories.append({'gid': gid.strip(), 'name': name.strip()})
        if categories:
            _taxonomy_cache['data'] = categories
            _taxonomy_cache['fetched_at'] = now
            logger.info(f"Taxonomy cache populated with {len(categories)} categories")
    except Exception as e:
        logger.warning(f"Failed to populate taxonomy cache: {e}")
    if not _taxonomy_cache.get('data'):
        _taxonomy_cache['data'] = [
            {'gid': 'gid://shopify/TaxonomyCategory/hg-3-4', 'name': 'Home & Garden > Decor > Artwork'},
            {'gid': 'gid://shopify/TaxonomyCategory/hg-3-4-2', 'name': 'Home & Garden > Decor > Artwork > Posters, Prints, & Visual Artwork'},
            {'gid': 'gid://shopify/TaxonomyCategory/ae-1-6', 'name': 'Arts & Entertainment > Hobbies & Creative Arts > Arts & Crafts'},
            {'gid': 'gid://shopify/TaxonomyCategory/hg-3-11', 'name': 'Home & Garden > Decor > Wall Art & Coverings'},
        ]
        _taxonomy_cache['fetched_at'] = now


def resolve_category_name_to_gid(category_name):
    """Look up a category name (e.g. 'Home & Garden > Decor > Artwork') in the taxonomy cache and return its GID.
    Returns empty string if not found."""
    if not category_name:
        return ''
    _ensure_taxonomy_loaded()
    data = _taxonomy_cache.get('data') or []
    cat_lower = category_name.strip().lower()
    # Strip "Manual: " prefix if present
    if cat_lower.startswith('manual:'):
        cat_lower = cat_lower[7:].strip()
    # Exact match first
    for item in data:
        if item['name'].lower() == cat_lower:
            return item['gid']
    # Partial match: check if a taxonomy name ends with the given name (leaf match)
    for item in data:
        if item['name'].lower().endswith(cat_lower):
            return item['gid']
    return ''


@app.route('/landing')
def landing():
    return render_template('landing.html')

@app.route('/')
@login_required
def index():
    # Generate a CSRF token for AJAX requests
    if 'csrf_token' not in session:
        session['csrf_token'] = str(uuid.uuid4())
    # Get frames data to inject into page; pass current_user so nav shows logged-in state
    frames_data = get_frames_list()
    default_prompt = get_default_prompt()
    prompt_sections = get_prompt_sections()
    return render_template('index.html', csrf_token=session['csrf_token'], frames_data=frames_data, user=current_user, template_version='4', default_ai_prompt=default_prompt, prompt_sections=prompt_sections)

@app.route('/test')
def test_upload_page():
    if not current_user.is_authenticated:
        return redirect(url_for('login'))
    return send_from_directory('.', 'test_upload.html')

@app.route('/test_direct')
def test_direct_upload():
    return send_from_directory('.', 'test_direct_upload.html')

@app.route('/debug/gemini')
def debug_gemini():
    """Diagnostic endpoint: test Gemini API connectivity and report errors"""
    import traceback as _tb
    results = {}

    # 1. Check API key
    api_key = os.environ.get("GEMINI_API_KEY", "")
    results['api_key_set'] = bool(api_key)
    results['api_key_prefix'] = api_key[:8] + "..." if len(api_key) > 8 else "(empty)"

    # 2. Try to init client
    try:
        from google import genai as _genai
        from google.genai import types as _types
        results['sdk_version'] = getattr(_genai, '__version__', 'unknown')
        client = _genai.Client(api_key=api_key)
        results['client_init'] = 'ok'
    except Exception as e:
        results['client_init'] = f'FAILED: {type(e).__name__}: {e}'
        return jsonify(results), 500

    # 3. Simple text call
    try:
        resp = client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents="Reply with exactly: {\"status\": \"ok\"}",
            config=_types.GenerateContentConfig(
                response_mime_type="application/json",
                max_output_tokens=256
            )
        )
        results['simple_call'] = 'ok'
        results['simple_response'] = resp.text[:200] if resp.text else '(empty)'
    except Exception as e:
        results['simple_call'] = f'FAILED: {type(e).__name__}: {e}'
        results['simple_traceback'] = _tb.format_exc()[-500:]

    return jsonify(results)

@app.route('/uploads/<filename>')
def serve_uploaded_file(filename):
    """Serve uploaded files"""
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route('/processed/<filename>')
def serve_processed_file(filename):
    """Serve processed/compressed files for faster loading"""
    return send_from_directory(PROCESSED_FOLDER, filename)

@app.route('/upload', methods=['POST'])
def upload_files():
    logger.info(f"Upload request received. Form data: {request.form}")
    logger.info(f"Files in request: {list(request.files.keys())}")
    
    # Check for both 'files' and 'file' keys to handle different frontend configurations
    files = []
    if 'files' in request.files:
        files = request.files.getlist('files')
        logger.info(f"Found {len(files)} files in 'files' key")
    elif 'file' in request.files:
        files = request.files.getlist('file')
        logger.info(f"Found {len(files)} files in 'file' key")
    else:
        logger.error(f"No files found. Available keys: {list(request.files.keys())}")
        return jsonify({'error': 'No files provided'}), 400
    logger.info(f"Number of files: {len(files)}")
    
    # Capture form data for processing
    custom_prompt = request.form.get('custom_prompt', '').strip()
    collection_instructions = request.form.get('collection_instructions', '').strip()
    variants_data_json = request.form.get('variants_data', '')
    
    # Parse variants data if provided
    variants_data = []
    if variants_data_json:
        try:
            variants_data = json.loads(variants_data_json)
            logger.info(f"Received variants data: {len(variants_data)} variants")
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid variants data JSON: {e}")
            variants_data = []
    
    # Collect all valid files and save to temp paths (one listing per upload batch)
    image_paths = []
    filenames = []
    
    for file in files:
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            logger.info(f"Processing file: {filename}")
            file_ext = os.path.splitext(filename)[1] or '.tmp'
            # Use a single task_id for the whole batch (created below)
            temp_filepath = temp_file_service.create_temp_file(
                prefix='upload',
                suffix=file_ext,
                task_id='batch'
            )
            try:
                file.save(temp_filepath)
                logger.info(f"File saved to temporary location: {temp_filepath}")
                image_paths.append(temp_filepath)
                filenames.append(filename)
            except Exception as e:
                logger.error(f"Error saving file {filename}: {str(e)}")
                try:
                    if os.path.exists(temp_filepath):
                        os.unlink(temp_filepath)
                except Exception:
                    pass
        else:
            if file and file.filename:
                logger.warning(f"File {file.filename} not allowed")
    
    if not image_paths:
        return jsonify({'error': 'No valid files to upload', 'tasks': []}), 400
    
    # One task for the entire batch (one listing; only first image is analyzed)
    task_id = str(uuid.uuid4())
    primary_filename = filenames[0]
    display_filename = primary_filename if len(filenames) == 1 else f"{primary_filename} + {len(filenames) - 1} more"
    primary_image_path = image_paths[0]
    
    with queue_lock:
        processing_queue[task_id] = {
            'id': task_id,
            'filename': display_filename,
            'filepath': primary_image_path,
            'image_paths': image_paths,
            'filenames': filenames,
            'status': 'uploaded',
            'current_step': f'Ready for processing ({len(image_paths)} image(s))',
            'created_at': datetime.now().isoformat(),
            'custom_prompt': custom_prompt,
            'collection_instructions': collection_instructions,
            'variants_data': variants_data,
            'ready_framed': True,  # Use ready-framed flow: compress all, analyze first only, one product
            'temp_files': image_paths.copy(),
        }
    
    save_queue_to_disk()
    logger.info(f"Created single listing task {task_id} with {len(image_paths)} image(s)")
    
    return jsonify({
        'tasks': [{
            'task_id': task_id,
            'filename': display_filename,
            'image_count': len(image_paths),
        }]
    })

@app.route('/status/<task_id>')
def get_status(task_id):
    with queue_lock:
        task = processing_queue.get(task_id)
    
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    
    return jsonify(task)

@app.route('/status')
def get_all_status():
    raw_tasks_to_start = []
    with queue_lock:
        for task_id, task in processing_queue.items():
            if (
                task.get('raw_artwork_workflow')
                and task.get('status') == 'queued'
                and not task.get('_direct_runner_started')
            ):
                raw_tasks_to_start.append(task_id)
        tasks = list(processing_queue.values())

    for task_id in raw_tasks_to_start:
        _start_direct_raw_artwork_runner(task_id)
    
    logger.debug(f"Status endpoint returning {len(tasks)} tasks")
    return jsonify({'tasks': tasks})

@app.route('/clear_task/<task_id>', methods=['DELETE'])
def clear_task(task_id):
    """Clear/remove a specific task from the processing queue"""
    try:
        logger.info(f"Clearing task: {task_id}")
        
        # Remove from in-memory queue
        with queue_lock:
            if task_id in processing_queue:
                task = processing_queue.pop(task_id)
                logger.info(f"Removed task {task_id} from memory queue: {task.get('filename', 'unknown')}")
            else:
                logger.warning(f"Task {task_id} not found in memory queue")
        
        # Save queue to disk after removal
        save_queue_to_disk()
        
        # TODO: Clean up any associated files if needed
        # This could include uploaded files, compressed files, processed frames, etc.
        
        return jsonify({'success': True, 'message': f'Task {task_id} cleared successfully'})
        
    except Exception as e:
        logger.error(f"Error clearing task {task_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/serve_file/<filename>')
def serve_file(filename):
    """Serve uploaded files for Dynamic Mockups API access"""
    try:
        # Check if file exists in uploads directory
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(filepath):
            return send_from_directory(UPLOAD_FOLDER, filename)
        
        # If not in uploads, check processed folder
        filepath = os.path.join(PROCESSED_FOLDER, filename)
        if os.path.exists(filepath):
            return send_from_directory(PROCESSED_FOLDER, filename)
        
        # File not found
        return jsonify({'error': 'File not found'}), 404
        
    except Exception as e:
        logger.error(f"Error serving file {filename}: {e}")
        return jsonify({'error': 'Failed to serve file'}), 500


@app.route('/serve_upload/<path:filepath>')
def serve_upload(filepath):
    """Serve uploaded files - now serves from temp directory"""
    """Serve files from uploads directory by path (for Photopea processing)"""
    try:
        # Decode the path
        from urllib.parse import unquote
        filepath = unquote(filepath)
        
        # Security: Only allow files from uploads directory
        if '..' in filepath or filepath.startswith('/'):
            return jsonify({'error': 'Invalid path'}), 403
        
        # Check various locations including temp directory
        locations_to_check = [
            temp_file_service.get_temp_dir(),  # Check temp directory first
            UPLOAD_FOLDER,
            PROCESSED_FOLDER,
            PSD_FRAMES_FOLDER,
            '.'  # Current directory
        ]
        
        for base_dir in locations_to_check:
            if not base_dir:
                continue
            full_path = os.path.join(base_dir, filepath) if not os.path.isabs(filepath) else filepath
            if os.path.exists(full_path) and os.path.isfile(full_path):
                directory = os.path.dirname(full_path)
                filename = os.path.basename(full_path)
                return send_from_directory(directory if directory else '.', filename)
        
        # Also try the path directly if it's an absolute path that exists (for temp files)
        if os.path.exists(filepath) and os.path.isfile(filepath):
            directory = os.path.dirname(filepath)
            filename = os.path.basename(filepath)
            return send_from_directory(directory if directory else '.', filename)
        
        logger.error(f"File not found: {filepath}")
        return jsonify({'error': 'File not found'}), 404
        
    except Exception as e:
        logger.error(f"Error serving upload {filepath}: {e}")
        return jsonify({'error': 'Failed to serve file'}), 500


@app.route('/save_processed_mockup', methods=['POST'])
def save_processed_mockup():
    """Save a processed mockup image from Photopea client-side processing"""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'}), 400
        
        file = request.files['file']
        if not file or not file.filename:
            return jsonify({'success': False, 'error': 'Empty file'}), 400
        
        # Get metadata
        original_image = request.form.get('original_image', 'unknown')
        template_used = request.form.get('template_used', 'unknown')
        
        # Generate unique filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        original_name = secure_filename(file.filename)
        filename = f"{timestamp}_{original_name}"
        
        # Save to processed folder
        filepath = os.path.join(PROCESSED_FOLDER, filename)
        os.makedirs(PROCESSED_FOLDER, exist_ok=True)
        file.save(filepath)
        
        logger.info(f"Saved processed mockup: {filename} (original: {original_image}, template: {template_used})")
        
        # Create a task for this processed mockup so it appears in the queue
        task_id = str(uuid.uuid4())
        
        with queue_lock:
            processing_queue[task_id] = {
                'task_id': task_id,
                'filename': filename,
                'original_filename': original_image,
                'template_used': template_used,
                'image_path': filepath,
                'status': 'pending',
                'current_step': 'Mockup ready for AI processing',
                'ready_framed': True,  # Skip framing since it's already processed
                'photopea_processed': True,
                'created_at': datetime.now().isoformat()
            }
            save_queue_to_disk()
        
        return jsonify({
            'success': True,
            'filename': filename,
            'path': filepath,
            'task_id': task_id,
            'message': 'Mockup saved and added to processing queue'
        })
        
    except Exception as e:
        logger.error(f"Error saving processed mockup: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/process_mockup_server', methods=['POST'])
def process_mockup_server():
    """
    Server-side mockup processing fallback.
    Uses psd-tools and Pillow when Photopea iframe doesn't work.
    """
    try:
        # Get artwork file
        if 'artwork' not in request.files:
            return jsonify({'success': False, 'error': 'No artwork file provided'}), 400
        
        artwork_file = request.files['artwork']
        if not artwork_file or not artwork_file.filename:
            return jsonify({'success': False, 'error': 'Empty artwork file'}), 400
        
        # Get template path from form data
        template_path = request.form.get('template_path')
        if not template_path:
            return jsonify({'success': False, 'error': 'No template path provided'}), 400
        
        # Import the mockup processor
        try:
            from mockup_processor import create_mockup, PSD_TOOLS_AVAILABLE
        except ImportError as e:
            logger.error(f"Failed to import mockup_processor: {e}")
            return jsonify({
                'success': False, 
                'error': 'Mockup processor not available. Install psd-tools: pip install psd-tools'
            }), 500
        
        # Save artwork temporarily
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        artwork_filename = secure_filename(artwork_file.filename)
        temp_artwork_path = os.path.join(UPLOAD_FOLDER, f"temp_{timestamp}_{artwork_filename}")
        artwork_file.save(temp_artwork_path)
        
        # Resolve template path
        template_full_path = None
        for base_dir in [PSD_FRAMES_FOLDER, UPLOAD_FOLDER, '.']:
            potential_path = os.path.join(base_dir, template_path)
            if os.path.exists(potential_path):
                template_full_path = potential_path
                break
        
        if not template_full_path:
            # Try direct path
            if os.path.exists(template_path):
                template_full_path = template_path
            else:
                os.remove(temp_artwork_path)
                return jsonify({'success': False, 'error': f'Template not found: {template_path}'}), 404
        
        logger.info(f"Server-side mockup: artwork={artwork_filename}, template={template_full_path}")
        
        # Generate output filename
        template_name = os.path.splitext(os.path.basename(template_full_path))[0]
        artwork_name = os.path.splitext(artwork_filename)[0]
        output_filename = f"{timestamp}_{artwork_name}_{template_name}_mockup.png"
        output_path = os.path.join(PROCESSED_FOLDER, output_filename)
        os.makedirs(PROCESSED_FOLDER, exist_ok=True)
        
        try:
            # Process the mockup
            create_mockup(template_full_path, temp_artwork_path, output_path)
            
            # Clean up temp artwork
            os.remove(temp_artwork_path)
            
            # Create a task for this processed mockup
            task_id = str(uuid.uuid4())
            
            with queue_lock:
                processing_queue[task_id] = {
                    'task_id': task_id,
                    'filename': output_filename,
                    'original_filename': artwork_filename,
                    'template_used': os.path.basename(template_full_path),
                    'image_path': output_path,
                    'status': 'pending',
                    'current_step': 'Mockup ready for AI processing',
                    'ready_framed': True,
                    'server_processed': True,
                    'created_at': datetime.now().isoformat()
                }
                save_queue_to_disk()
            
            logger.info(f"Server-side mockup created: {output_filename}")
            
            return jsonify({
                'success': True,
                'filename': output_filename,
                'path': output_path,
                'task_id': task_id,
                'message': 'Mockup created server-side and added to queue',
                'psd_tools_available': PSD_TOOLS_AVAILABLE
            })
            
        except Exception as process_error:
            # Clean up on error
            if os.path.exists(temp_artwork_path):
                os.remove(temp_artwork_path)
            raise process_error
        
    except Exception as e:
        logger.error(f"Server-side mockup error: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/upload_mockup', methods=['POST'])
def upload_mockup():
    """Handle custom mockup PSD file uploads"""
    if 'mockup_files' not in request.files:
        return jsonify({'error': 'No mockup files provided'}), 400
    
    files = request.files.getlist('mockup_files')
    uploaded_mockups = []
    
    for file in files:
        if file and file.filename and file.filename.lower().endswith('.psd'):
            filename = secure_filename(file.filename)
            unique_filename = f"mockup_{filename}"
            filepath = os.path.join(PSD_FRAMES_FOLDER, unique_filename)
            
            try:
                file.save(filepath)
                uploaded_mockups.append({
                    'filename': filename,
                    'filepath': filepath
                })
                logger.info(f"Mockup uploaded: {filename}")
            except Exception as e:
                logger.error(f"Error saving mockup {filename}: {str(e)}")
                continue
    
    if uploaded_mockups:
        return jsonify({
            'message': 'Mockup template uploaded successfully',
            'mockups': uploaded_mockups,
            'filename': uploaded_mockups[0]['filename']  # For UI display
        })
    else:
        return jsonify({'error': 'No valid mockup files uploaded'}), 400


@app.route('/api/local_worker/ai_benchmark', methods=['POST'])
def api_local_worker_ai_benchmark():
    """Run read-only listing metadata trials; this route never calls Shopify."""
    auth_error = _local_worker_auth_error()
    if auth_error:
        return auth_error
    files = request.files.getlist('files')
    if not files or len(files) > 3:
        return jsonify({'error': 'Provide between one and three sample images'}), 400
    supported_providers = {'gemini', 'gemini-production', 'qwen', *OPENROUTER_BENCHMARK_MODELS.keys()}
    providers = [
        value.strip().lower()
        for value in (request.form.get('providers') or 'gemini,qwen').split(',')
        if value.strip().lower() in supported_providers
    ]
    if not providers:
        return jsonify({'error': 'No supported providers requested'}), 400
    output = []
    temp_paths = []
    try:
        for image_file in files:
            safe_name = secure_filename(image_file.filename or 'benchmark.jpg')
            suffix = os.path.splitext(safe_name)[1].lower()
            if suffix not in {'.jpg', '.jpeg', '.png', '.webp'}:
                return jsonify({'error': f'Unsupported image type: {safe_name}'}), 400
            temp_handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            temp_handle.close()
            image_file.save(temp_handle.name)
            temp_paths.append(temp_handle.name)
            if os.path.getsize(temp_handle.name) > 8 * 1024 * 1024:
                return jsonify({'error': f'Benchmark image exceeds 8 MB: {safe_name}'}), 400
            image_result = {'filename': safe_name, 'providers': []}
            for provider in providers:
                try:
                    image_result['providers'].append(benchmark_provider(provider, temp_handle.name))
                except Exception as provider_error:
                    image_result['providers'].append({
                        'provider': provider,
                        'error': str(provider_error),
                    })
            output.append(image_result)
        return jsonify({
            'success': True,
            'shopify_writes': 0,
            'results': output,
        })
    finally:
        for temp_path in temp_paths:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


@app.route('/api/local_worker/upload_framed', methods=['POST'])
def api_local_worker_upload_framed():
    """Accept locally-rendered framed JPEGs and enqueue the normal Ready Images pipeline."""
    auth_error = _local_worker_auth_error()
    if auth_error:
        return auth_error

    user_id = _resolve_local_worker_user_id()
    if not user_id:
        return jsonify({
            'error': 'LOCAL_WORKER_USER_ID is not configured and the server has multiple/no users'
        }), 400

    shop = _get_active_shop_for_user(user_id)
    if not shop:
        return jsonify({'error': f'No active Shopify shop is connected for user {user_id}'}), 400

    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': 'No framed JPEG files provided'}), 400

    profile_name = (request.form.get('profile_name') or '').strip()
    client_job_id = secure_filename((request.form.get('client_job_id') or '').strip())
    if client_job_id:
        with queue_lock:
            existing = next(
                (
                    copy.deepcopy(task)
                    for task in processing_queue.values()
                    if task.get('client_job_id') == client_job_id
                ),
                None,
            )
        if existing:
            logger.info(
                "Local worker duplicate upload for client_job_id=%s; returning existing task %s",
                client_job_id,
                existing.get('id') or existing.get('task_id'),
            )
            return jsonify({
                'success': True,
                'duplicate': True,
                'task_id': existing.get('id') or existing.get('task_id'),
                'filename': existing.get('filename'),
                'image_count': len(existing.get('image_paths') or []),
                'status': existing.get('status'),
                'profile_name': existing.get('profile_name') or profile_name,
            })

    try:
        settings = _profile_to_listing_settings(user_id, profile_name)
    except ValueError as profile_error:
        return jsonify({'error': str(profile_error)}), 400
    if not settings.get('custom_prompt'):
        return jsonify({'error': 'The selected Ready Images profile has no custom prompt'}), 400
    if not settings.get('selected_sales_channels'):
        settings['auto_publish_all_channels'] = True
    if 'review_before_publish' in request.form:
        settings['review_before_publish'] = request.form.get('review_before_publish', 'false').lower() == 'true'

    task_id = str(uuid.uuid4())
    image_paths = []
    filenames = []
    temp_files = []

    for file in files:
        if not file or not file.filename or not allowed_file(file.filename):
            continue
        filename = secure_filename(file.filename)
        file_ext = os.path.splitext(filename)[1] or '.jpg'
        temp_filepath = temp_file_service.create_temp_file(
            prefix='local_worker_framed',
            suffix=file_ext,
            task_id=task_id
        )
        try:
            file.save(temp_filepath)
            image_paths.append(temp_filepath)
            filenames.append(filename)
            temp_files.append(temp_filepath)
        except Exception as e:
            logger.error("Error saving local-worker framed file %s: %s", filename, e)
            try:
                if os.path.exists(temp_filepath):
                    os.unlink(temp_filepath)
            except OSError:
                pass

    if not image_paths:
        return jsonify({'error': 'No valid framed image files uploaded'}), 400

    analysis_path = None
    analysis_file = request.files.get('analysis_file')
    if analysis_file and analysis_file.filename and allowed_file(analysis_file.filename):
        analysis_filename = secure_filename(analysis_file.filename)
        analysis_ext = os.path.splitext(analysis_filename)[1] or '.jpg'
        analysis_path = temp_file_service.create_temp_file(
            prefix='local_worker_analysis',
            suffix=analysis_ext,
            task_id=task_id
        )
        try:
            analysis_file.save(analysis_path)
            temp_files.append(analysis_path)
        except Exception as e:
            logger.warning("Could not save local-worker analysis image %s: %s", analysis_filename, e)
            analysis_path = None

    source_filename = secure_filename(
        request.form.get('source_filename') or (analysis_file.filename if analysis_file else '') or filenames[0]
    )
    primary_filename = source_filename or filenames[0]
    auto_start = request.form.get('auto_start', 'true').lower() != 'false'

    task_payload = {
        'id': task_id,
        'filename': primary_filename,
        'filepath': image_paths[0],
        'image_paths': image_paths,
        'filenames': filenames,
        'status': 'queued' if auto_start else 'uploaded',
        'current_step': 'Queued from local framing worker...' if auto_start else f'Ready for processing ({len(image_paths)} image(s))',
        'created_at': datetime.now().isoformat(),
        'ready_framed': True,
        'local_worker_upload': True,
        # Pre-framed mode: the analysis image itself is a framed room mockup,
        # so the AI must be told to describe only the poster artwork in it.
        'analysis_is_framed': request.form.get('analysis_is_framed', 'false').lower() == 'true',
        'client_job_id': client_job_id,
        'temp_files': temp_files,
        'shop_domain': shop.shop_domain,
        'shop_access_token': shop.access_token,
        **settings,
    }
    if analysis_path:
        task_payload['analysis_image_path'] = analysis_path

    with queue_lock:
        processing_queue[task_id] = task_payload
    save_queue_to_disk()

    if auto_start:
        _start_direct_ready_framed_runner(task_id)

    logger.info(
        "Local worker created task %s for %s with %s framed images using profile %s",
        task_id,
        primary_filename,
        len(image_paths),
        task_payload.get('profile_name') or profile_name or '(default)',
    )
    return jsonify({
        'success': True,
        'task_id': task_id,
        'filename': primary_filename,
        'image_count': len(image_paths),
        'status': task_payload['status'],
        'profile_name': task_payload.get('profile_name') or profile_name,
        'profile_rules_version': task_payload.get('profile_rules_version'),
        'prompt_fingerprint': task_payload.get('prompt_fingerprint'),
        'required_description_fact_count': task_payload.get('required_description_fact_count', 0),
    })


@app.route('/api/local_worker/task_status/<task_id>', methods=['GET'])
def api_local_worker_task_status(task_id):
    auth_error = _local_worker_auth_error()
    if auth_error:
        return auth_error

    with queue_lock:
        task = copy.deepcopy(processing_queue.get(task_id))
    if not task:
        load_queue_from_disk()
        with queue_lock:
            task = copy.deepcopy(processing_queue.get(task_id))
    if not task:
        return jsonify({'error': 'Task not found'}), 404

    return jsonify({
        'success': True,
        'task_id': task_id,
        'status': task.get('status'),
        'current_step': task.get('current_step'),
        'error': task.get('error'),
        'product_id': task.get('product_id'),
        'product_url': task.get('product_url'),
        'filename': task.get('filename'),
        'profile_name': task.get('profile_name'),
        'profile_rules_version': task.get('profile_rules_version'),
        'prompt_fingerprint': task.get('prompt_fingerprint'),
        'required_description_fact_count': task.get('required_description_fact_count', 0),
        'product_faq_required': task.get('product_faq_required', False),
        'faq_metafield_verified': task.get('faq_metafield_verified'),
    })


@app.route('/api/local_worker/health', methods=['GET'])
def api_local_worker_health():
    auth_error = _local_worker_auth_error()
    if auth_error:
        return auth_error

    from models import UserProfile

    user_id = _resolve_local_worker_user_id()
    if not user_id:
        return jsonify({
            'success': False,
            'connected': False,
            'error': 'LOCAL_WORKER_USER_ID is not configured and the server has multiple/no users'
        }), 400

    shop = _get_active_shop_for_user(user_id)
    profiles = UserProfile.query.filter_by(
        user_id=user_id,
        profile_type='ready'
    ).order_by(UserProfile.profile_name.asc()).all()
    if not profiles:
        profiles = UserProfile.query.filter_by(user_id=user_id).order_by(UserProfile.profile_name.asc()).all()

    configured_profile = (os.environ.get('LOCAL_WORKER_PROFILE_NAME') or '').strip()
    return jsonify({
        'success': True,
        'connected': bool(shop),
        'user_id': user_id,
        'shop_domain': shop.shop_domain if shop else None,
        'profiles': [profile.profile_name for profile in profiles],
        'configured_profile': configured_profile,
        'gemini_configured': bool(os.environ.get("GEMINI_API_KEY")),
    })


@app.route('/upload_ready', methods=['POST'])
def upload_ready():
    """Handle ready-framed image uploads (skip framing step)"""
    logger.info(f"=== UPLOAD_READY ENDPOINT HIT ===")
    
    # Temporary: Skip auth check to get upload working
    # if not current_user.is_authenticated:
    #     return jsonify({'error': 'Authentication required'}), 401
    
    # Check for both 'files' and 'file' keys to handle different frontend configurations
    files = []
    if 'files' in request.files:
        files = request.files.getlist('files')
        logger.info(f"Found {len(files)} files in 'files' key")
    elif 'file' in request.files:
        files = request.files.getlist('file')
        logger.info(f"Found {len(files)} files in 'file' key")
    else:
        logger.error(f"No files found. Available keys: {list(request.files.keys())}")
        return jsonify({'error': 'No files provided'}), 400
    custom_prompt = request.form.get('custom_prompt', '').strip()
    variants_data_json = request.form.get('variants_data', '')
    
    # Collect Product Organization data
    business_name = request.form.get('business_name', '').strip()
    product_type = request.form.get('product_type', '').strip()
    vendor = request.form.get('vendor', '').strip()
    
    # Collect toggle states for AI vs manual
    title_manual = request.form.get('title_manual', 'false').lower() == 'true'
    description_manual = request.form.get('description_manual', 'false').lower() == 'true'
    category_manual = request.form.get('category_manual', 'false').lower() == 'true'
    product_type_manual = request.form.get('product_type_manual', 'false').lower() == 'true'
    tags_manual = request.form.get('tags_manual', 'false').lower() == 'true'
    collections_manual = request.form.get('collections_manual', 'false').lower() == 'true'
    collections_enabled = request.form.get('collections_enabled', 'true').lower() == 'true'
    vendor_manual = request.form.get('vendor_manual', 'false').lower() == 'true'
    sku_manual = request.form.get('sku_manual', 'false').lower() == 'true'
    handle_manual = request.form.get('handle_manual', 'false').lower() == 'true'
    color_manual = request.form.get('color_manual', 'false').lower() == 'true'
    metafield1_manual = request.form.get('metafield1_manual', 'false').lower() == 'true'
    metafield2_manual = request.form.get('metafield2_manual', 'false').lower() == 'true'
    condition_manual = request.form.get('condition_manual', 'false').lower() == 'true'
    decoration_material_manual = request.form.get('decoration_material_manual', 'false').lower() == 'true'
    artwork_frame_material_manual = request.form.get('artwork_frame_material_manual', 'false').lower() == 'true'
    seo_title_manual = request.form.get('seo_title_manual', 'false').lower() == 'true'
    meta_desc_manual = request.form.get('meta_desc_manual', 'false').lower() == 'true'
    
    # Collect manual values (only used if toggles are true)
    manual_title = request.form.get('manual_title', '').strip()
    manual_description = request.form.get('manual_description', '').strip()
    manual_category = request.form.get('manual_category', '').strip()
    manual_category_gid = request.form.get('manual_category_gid', '').strip()
    manual_tags = request.form.get('manual_tags', '').strip()
    manual_collections = request.form.get('manual_collections', '').strip()
    manual_sku = request.form.get('manual_sku', '').strip()
    manual_handle = request.form.get('manual_handle', '').strip()
    manual_color = request.form.get('manual_color', '').strip()
    manual_metafield1 = request.form.get('manual_metafield1', '').strip()
    manual_metafield2 = request.form.get('manual_metafield2', '').strip()
    manual_condition = request.form.get('manual_condition', '').strip()
    manual_decoration_material = request.form.get('manual_decoration_material', '').strip()
    manual_artwork_frame_material = request.form.get('manual_artwork_frame_material', '').strip()
    manual_seo_title = request.form.get('manual_seo_title', '').strip()
    manual_meta_desc = request.form.get('manual_meta_desc', '').strip()
    manual_google_product_category = request.form.get('manual_google_product_category', '').strip()
    manual_gender = request.form.get('manual_gender', '').strip()
    manual_age_group = request.form.get('manual_age_group', '').strip()
    manual_gs_condition = request.form.get('manual_gs_condition', '').strip()
    manual_custom_product = request.form.get('manual_custom_product', '').strip()
    manual_custom_label_0 = request.form.get('manual_custom_label_0', '').strip()
    manual_custom_label_1 = request.form.get('manual_custom_label_1', '').strip()
    manual_custom_label_2 = request.form.get('manual_custom_label_2', '').strip()
    manual_custom_label_3 = request.form.get('manual_custom_label_3', '').strip()
    manual_custom_label_4 = request.form.get('manual_custom_label_4', '').strip()
    google_shopping_enabled = request.form.get('google_shopping_enabled', 'true').lower() == 'true'
    google_category_manual = request.form.get('google_category_manual', 'false').lower() == 'true'
    gender_manual = request.form.get('gender_manual', 'false').lower() == 'true'
    age_group_manual = request.form.get('age_group_manual', 'false').lower() == 'true'
    gs_condition_manual = request.form.get('gs_condition_manual', 'false').lower() == 'true'
    custom_product_manual = request.form.get('custom_product_manual', 'false').lower() == 'true'
    custom_label_0_manual = request.form.get('custom_label_0_manual', 'false').lower() == 'true'
    custom_label_1_manual = request.form.get('custom_label_1_manual', 'false').lower() == 'true'
    custom_label_2_manual = request.form.get('custom_label_2_manual', 'false').lower() == 'true'
    custom_label_3_manual = request.form.get('custom_label_3_manual', 'false').lower() == 'true'
    custom_label_4_manual = request.form.get('custom_label_4_manual', 'false').lower() == 'true'
    manual_seo_metafields = {}
    for key in SEO_METAFIELD_KEYS:
        manual_seo_metafields[f'{key}_manual'] = request.form.get(f'{key}_manual', 'false').lower() == 'true'
        manual_seo_metafields[f'manual_{key}'] = request.form.get(f'manual_{key}', '').strip()
    
    # Collect product settings
    product_status = request.form.get('product_status', 'ACTIVE').strip()
    review_before_publish = request.form.get('review_before_publish', 'false').lower() == 'true'
    inventory_quantity = request.form.get('inventory_quantity', '999').strip()
    inventory_policy = request.form.get('inventory_policy', 'continue').strip()
    
    logger.info(f"Product Organization data - Business: '{business_name}', Type: '{product_type}', Vendor: '{vendor}'")
    logger.info(f"Toggle states - Title: {title_manual}, Description: {description_manual}, Category: {category_manual}, Product Type: {product_type_manual}, Tags: {tags_manual}, Collections: {collections_manual}")
    logger.warning(f"🔧 CRITICAL DEBUG - Manual category value: '{manual_category}' with toggle: {category_manual}")
    
    # Parse variants data if provided
    variants_data = []
    logger.info(f"🔍 UPLOAD: Raw variants_data_json: {variants_data_json}")
    if variants_data_json:
        try:
            variants_data = json.loads(variants_data_json)
            logger.info(f"✅ UPLOAD: Successfully parsed {len(variants_data)} variants: {variants_data}")
            for i, variant in enumerate(variants_data):
                logger.info(f"  Variant {i+1}: {variant}")
        except json.JSONDecodeError as e:
            logger.error(f"❌ UPLOAD: Invalid variants data JSON: {e}")
            variants_data = []
    else:
        logger.error("❌ UPLOAD: No variants_data_json provided - FRONTEND COLLECTION COMPLETELY BROKEN!")
        logger.error("❌ VARIANTS WILL BE MISSING FROM SHOPIFY PRODUCT!")
    
    logger.info(f"Ready-framed upload: {len(files)} files for single listing")
    
    # Create task first to get task_id for temp file tracking
    task_id = str(uuid.uuid4())
    
    # Save all files to temporary locations
    image_paths = []
    filenames = []
    
    for file in files:
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            logger.info(f"Processing ready-framed file: {filename}")
            
            # Get file extension for temp file
            file_ext = os.path.splitext(filename)[1] or '.tmp'
            
            # Save to temporary location instead of permanent folder
            temp_filepath = temp_file_service.create_temp_file(
                prefix='ready_framed',
                suffix=file_ext,
                task_id=task_id
            )
            
            try:
                file.save(temp_filepath)
                logger.info(f"Ready-framed file saved to temporary location: {temp_filepath}")
                image_paths.append(temp_filepath)
                filenames.append(filename)
            except Exception as e:
                logger.error(f"Error saving ready-framed file {filename}: {str(e)}")
                # Cleanup temp file on error
                try:
                    if os.path.exists(temp_filepath):
                        os.unlink(temp_filepath)
                except:
                    pass
                continue
        else:
            if file and file.filename:
                logger.warning(f"Ready-framed file {file.filename} not allowed")
    
    if not image_paths:
        return jsonify({'error': 'No valid files uploaded'}), 400
    
    # Get publishing settings from session before creating task
    selected_channels = session.get('selected_sales_channels', [])
    selected_markets = session.get('selected_markets', [])
    logger.warning(f"📡 STORING TO TASK - Channels: {len(selected_channels)}, Markets: {len(selected_markets)}")
    
    # Use first filename for display, but store all paths
    primary_filename = filenames[0] if filenames else "Multiple Images"
    primary_image_path = image_paths[0] if image_paths else None
    
    with queue_lock:
        processing_queue[task_id] = {
            'id': task_id,
            'filename': primary_filename,  # Primary filename for display
            'filepath': primary_image_path,  # Primary image path (temporary)
            'image_paths': image_paths,  # List of all image paths in order (temporary)
            'filenames': filenames,  # List of all filenames in order
            'status': 'uploaded',
            'current_step': f'Ready for processing ({len(image_paths)} image(s))',
            'created_at': datetime.now().isoformat(),
            'ready_framed': True,  # Flag to skip framing
            'temp_files': image_paths.copy(),  # Track temp files for cleanup
            'custom_prompt': custom_prompt,  # Store custom prompt
            'variants_data': variants_data,  # Store user-defined variants
            'business_name': business_name,  # Store business name
            'product_type': product_type,  # Store product type
            'vendor': vendor,  # Store vendor
            # Store toggle states for AI vs manual
            'title_manual': title_manual,
            'description_manual': description_manual,
            'category_manual': category_manual,
            'product_type_manual': product_type_manual,
            'tags_manual': tags_manual,
            'collections_manual': collections_manual,
            'collections_enabled': collections_enabled,
            'vendor_manual': vendor_manual,
            'sku_manual': sku_manual,
            'handle_manual': handle_manual,
            'color_manual': color_manual,
            'metafield1_manual': metafield1_manual,
            'metafield2_manual': metafield2_manual,
            'condition_manual': condition_manual,
            'decoration_material_manual': decoration_material_manual,
            'artwork_frame_material_manual': artwork_frame_material_manual,
            'seo_title_manual': seo_title_manual,
            'meta_desc_manual': meta_desc_manual,
            # Store manual values (only used if toggles are true)
            'manual_title': manual_title,
            'manual_description': manual_description,
            'manual_category': manual_category,
            'manual_category_gid': manual_category_gid,
            'manual_tags': manual_tags,
            'manual_collections': manual_collections,
            'manual_sku': manual_sku,
            'manual_handle': manual_handle,
            'manual_color': manual_color,
            'manual_metafield1': manual_metafield1,
            'manual_metafield2': manual_metafield2,
            'manual_condition': manual_condition,
            'manual_decoration_material': manual_decoration_material,
            'manual_artwork_frame_material': manual_artwork_frame_material,
            'manual_seo_title': manual_seo_title,
            'manual_meta_desc': manual_meta_desc,
            'manual_google_product_category': manual_google_product_category,
            'manual_gender': manual_gender,
            'manual_age_group': manual_age_group,
            'manual_gs_condition': manual_gs_condition,
            'manual_custom_product': manual_custom_product,
            'manual_custom_label_0': manual_custom_label_0,
            'manual_custom_label_1': manual_custom_label_1,
            'manual_custom_label_2': manual_custom_label_2,
            'manual_custom_label_3': manual_custom_label_3,
            'manual_custom_label_4': manual_custom_label_4,
            'google_shopping_enabled': google_shopping_enabled,
            'google_category_manual': google_category_manual,
            'gender_manual': gender_manual,
            'age_group_manual': age_group_manual,
            'gs_condition_manual': gs_condition_manual,
            'custom_product_manual': custom_product_manual,
            'custom_label_0_manual': custom_label_0_manual,
            'custom_label_1_manual': custom_label_1_manual,
            'custom_label_2_manual': custom_label_2_manual,
            'custom_label_3_manual': custom_label_3_manual,
            'custom_label_4_manual': custom_label_4_manual,
            **manual_seo_metafields,
            # Store product settings
            'product_status': product_status,
            'review_before_publish': review_before_publish,
            'inventory_quantity': inventory_quantity,
            'inventory_policy': inventory_policy,
            # Store publishing settings from session
            'selected_sales_channels': selected_channels,
            'selected_markets': selected_markets
        }
    
    logger.info(f"Created single listing task {task_id} with {len(image_paths)} images")
    
    # Save queue to disk synchronously — _save_lock serializes writes so this is safe and fast
    save_queue_to_disk()
    
    try:
        response = jsonify({
            'tasks': [{
                'task_id': task_id,
                'filename': primary_filename,
                'image_count': len(image_paths)
            }]
        })
        response.headers['Content-Type'] = 'application/json'
        logger.info(f"Returning upload_ready 200 for task_id={task_id}")
        return response
    except Exception as e:
        logger.error(f"Error creating response: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'error': f'Failed to create response: {str(e)}'}), 500

# AI Prompt management endpoints
@app.route('/save_ai_prompt', methods=['POST'])
@login_required
def save_ai_prompt():
    from models import UserAIPrompt
    data = request.get_json()
    workflow_type = data.get('workflow_type')
    prompt_content = data.get('prompt_content', '').strip()
    
    if not workflow_type or workflow_type not in ['mockup', 'pre-framed']:
        return jsonify({'error': 'Invalid workflow type'}), 400
    
    if not prompt_content:
        return jsonify({'error': 'Prompt content cannot be empty'}), 400
    
    # Check if user already has a prompt for this workflow
    existing_prompt = UserAIPrompt.query.filter_by(
        user_id=current_user.id,
        workflow_type=workflow_type
    ).first()
    
    if existing_prompt:
        existing_prompt.prompt_content = prompt_content
        existing_prompt.updated_at = datetime.now()
    else:
        new_prompt = UserAIPrompt()
        new_prompt.user_id = current_user.id
        new_prompt.workflow_type = workflow_type
        new_prompt.prompt_content = prompt_content
        db.session.add(new_prompt)
    
    db.session.commit()
    return jsonify({'message': 'AI prompt saved successfully'})

@app.route('/load_ai_prompt/<workflow_type>')
@login_required
def load_ai_prompt(workflow_type):
    from models import UserAIPrompt
    if workflow_type not in ['mockup', 'pre-framed']:
        return jsonify({'error': 'Invalid workflow type'}), 400
    
    prompt = UserAIPrompt.query.filter_by(
        user_id=current_user.id,
        workflow_type=workflow_type
    ).first()
    
    if prompt:
        return jsonify({'prompt_content': prompt.prompt_content})
    else:
        return jsonify({'error': 'No saved prompt found'}), 404

@app.route('/save_variant_preset', methods=['POST'])
@login_required
def save_variant_preset():
    from models import UserVariantPreset
    data = request.get_json()
    preset_name = data.get('preset_name')
    variants_data = data.get('variants_data')
    
    if not preset_name or not variants_data:
        return jsonify({'error': 'Preset name and variants data are required'}), 400
    
    # Check if preset with this name already exists for this user
    existing_preset = UserVariantPreset.query.filter_by(
        user_id=current_user.id,
        preset_name=preset_name
    ).first()
    
    if existing_preset:
        existing_preset.variants_data = json.dumps(variants_data)
        existing_preset.updated_at = datetime.now()
    else:
        new_preset = UserVariantPreset()
        new_preset.user_id = current_user.id
        new_preset.preset_name = preset_name
        new_preset.variants_data = json.dumps(variants_data)
        db.session.add(new_preset)
    
    db.session.commit()
    return jsonify({'message': 'Variant preset saved successfully'})

@app.route('/load_variant_preset/<int:preset_id>')
@login_required
def load_variant_preset(preset_id):
    from models import UserVariantPreset
    preset = UserVariantPreset.query.filter_by(
        id=preset_id,
        user_id=current_user.id
    ).first()
    
    if preset:
        return jsonify({'variants_data': json.loads(preset.variants_data)})
    else:
        return jsonify({'error': 'Preset not found'}), 404

@app.route('/get_variant_presets')
@login_required
def get_variant_presets():
    from models import UserVariantPreset
    presets = UserVariantPreset.query.filter_by(user_id=current_user.id).order_by(UserVariantPreset.created_at.desc()).all()
    
    presets_data = []
    for preset in presets:
        presets_data.append({
            'id': preset.id,
            'preset_name': preset.preset_name,
            'created_at': preset.created_at.isoformat()
        })
    
    return jsonify({'presets': presets_data})

@app.route('/delete_variant_preset/<int:preset_id>', methods=['DELETE'])
@login_required
def delete_variant_preset(preset_id):
    from models import UserVariantPreset
    preset = UserVariantPreset.query.filter_by(
        id=preset_id,
        user_id=current_user.id
    ).first()
    
    if not preset:
        return jsonify({'error': 'Preset not found'}), 404
    
    db.session.delete(preset)
    db.session.commit()
    return jsonify({'message': 'Variant preset deleted successfully'})


# AI Instruction Preset endpoints
@app.route('/save_ai_instruction_preset', methods=['POST'])
@login_required
def save_ai_instruction_preset():
    from models import UserAIInstructionPreset
    data = request.get_json()
    preset_name = data.get('preset_name')
    instructions = data.get('instructions')
    
    if not preset_name or not instructions:
        return jsonify({'error': 'Preset name and instructions are required'}), 400
    
    # Check if preset with this name already exists for this user
    existing_preset = UserAIInstructionPreset.query.filter_by(
        user_id=current_user.id,
        preset_name=preset_name
    ).first()
    
    if existing_preset:
        existing_preset.instructions = instructions
        existing_preset.updated_at = datetime.now()
    else:
        new_preset = UserAIInstructionPreset()
        new_preset.user_id = current_user.id
        new_preset.preset_name = preset_name
        new_preset.instructions = instructions
        db.session.add(new_preset)
    
    db.session.commit()
    return jsonify({'message': 'AI instruction preset saved successfully'})


@app.route('/load_ai_instruction_preset/<int:preset_id>')
@login_required
def load_ai_instruction_preset(preset_id):
    from models import UserAIInstructionPreset
    preset = UserAIInstructionPreset.query.filter_by(
        id=preset_id,
        user_id=current_user.id
    ).first()
    
    if preset:
        return jsonify({'instructions': preset.instructions})
    else:
        return jsonify({'error': 'Preset not found'}), 404


@app.route('/get_ai_instruction_presets')
@login_required
def get_ai_instruction_presets():
    from models import UserAIInstructionPreset
    presets = UserAIInstructionPreset.query.filter_by(user_id=current_user.id).order_by(UserAIInstructionPreset.created_at.desc()).all()
    
    presets_data = []
    for preset in presets:
        presets_data.append({
            'id': preset.id,
            'preset_name': preset.preset_name,
            'created_at': preset.created_at.isoformat()
        })
    
    return jsonify({'presets': presets_data})


@app.route('/delete_ai_instruction_preset/<int:preset_id>', methods=['DELETE'])
@login_required
def delete_ai_instruction_preset(preset_id):
    from models import UserAIInstructionPreset
    preset = UserAIInstructionPreset.query.filter_by(
        id=preset_id,
        user_id=current_user.id
    ).first()
    
    if not preset:
        return jsonify({'error': 'Preset not found'}), 404
    
    db.session.delete(preset)
    db.session.commit()
    return jsonify({'message': 'AI instruction preset deleted successfully'})


@app.route('/load_collections')
def load_collections():
    try:
        from flask_login import current_user
        # Use hardcoded user for now
        user_id = "081f126d"  # AJS123's user ID
            
        logger.debug(f"Loading collections for user: {user_id}")
        from models import UserCollectionPreset
        collections = UserCollectionPreset.query.filter_by(user_id=user_id).all()
        collection_names = [c.collection_name for c in collections]
        logger.debug(f"Found {len(collection_names)} collections: {collection_names}")
        return jsonify({'collections': collection_names})
    except Exception as e:
        logger.error(f"Error loading collections: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/get_shopify_collections')
@login_required
def get_shopify_collections_endpoint():
    """Endpoint to fetch live collections from Shopify store"""
    try:
        from shopify_utils import get_shopify_collections
        # Resolve credentials from current user's shop
        _shop_domain = None
        _shop_token = None
        try:
            shop = get_current_shop()
            if shop:
                _shop_domain = shop.shop_domain
                _shop_token = shop.access_token
        except Exception:
            pass
        if not _shop_domain or not _shop_token:
            logger.warning("No Shopify store connected for collections")
            return jsonify({'collections': [], 'no_shop': True, 'error': 'No Shopify store connected. Connect your store in Settings.'})
        collections = get_shopify_collections(shop_domain=_shop_domain, access_token=_shop_token)
        logger.info(f"Retrieved {len(collections)} Shopify collections for sync")
        return jsonify({'collections': collections})
    except Exception as e:
        logger.error(f"Error fetching Shopify collections: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/add_collection', methods=['POST'])
def add_collection():
    try:
        from flask_login import current_user
        # Use hardcoded user for now
        user_id = "081f126d"  # AJS123's user ID
            
        logger.debug(f"Adding collection for user: {user_id}")
        from models import UserCollectionPreset
        data = request.get_json()
        collection_name = data.get('collection_name', '').strip()
        
        if not collection_name:
            return jsonify({'error': 'Collection name cannot be empty'}), 400
        
        # Check if collection already exists
        existing_collection = UserCollectionPreset.query.filter_by(
            user_id=user_id,
            collection_name=collection_name
        ).first()
        
        if existing_collection:
            return jsonify({'error': 'Collection already exists'}), 400
        
        # Add new collection
        new_collection = UserCollectionPreset()
        new_collection.user_id = user_id
        new_collection.collection_name = collection_name
        db.session.add(new_collection)
        db.session.commit()
        
        logger.debug(f"Successfully added collection: {collection_name}")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error adding collection: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/remove_collection', methods=['POST'])
def remove_collection():
    try:
        from flask_login import current_user
        # Use hardcoded user for now
        user_id = "081f126d"  # AJS123's user ID
            
        logger.debug(f"Removing collection for user: {user_id}")
        from models import UserCollectionPreset
        data = request.get_json()
        collection_name = data.get('collection_name', '').strip()
        
        if not collection_name:
            return jsonify({'error': 'Collection name cannot be empty'}), 400
        
        # Find and remove collection
        collection = UserCollectionPreset.query.filter_by(
            user_id=user_id,
            collection_name=collection_name
        ).first()
        
        if not collection:
            return jsonify({'error': 'Collection not found'}), 404
        
        db.session.delete(collection)
        db.session.commit()
        
        logger.debug(f"Successfully removed collection: {collection_name}")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error removing collection: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/start_processing/<task_id>', methods=['POST'])
def start_processing(task_id):
    """Start processing a task manually with current settings"""
    logger.info(f"Starting processing for task ID: {task_id}")
    
    # Get fresh settings from the request if sent
    fresh_settings = None
    if request.is_json:
        fresh_settings = request.get_json()
        logger.info(f"Received fresh settings for task {task_id}: variants={len(fresh_settings.get('variants_data', []))}")
    
    with queue_lock:
        logger.info(f"Available tasks in queue: {list(processing_queue.keys())}")
        task = processing_queue.get(task_id)
        
        # Update task with fresh settings if provided
        if fresh_settings and task:
            task['fresh_settings'] = fresh_settings
            # Also update publishing settings directly in task
            if 'selected_sales_channels' in fresh_settings:
                task['selected_sales_channels'] = fresh_settings['selected_sales_channels']
                logger.info(f"Updated publishing channels: {fresh_settings['selected_sales_channels']}")
            if 'selected_markets' in fresh_settings:
                task['selected_markets'] = fresh_settings['selected_markets']
                logger.info(f"Updated catalogs: {fresh_settings['selected_markets']}")
            # Update ALL manual override settings from fresh_settings into task
            manual_toggle_keys = [
                'title_manual', 'description_manual', 'category_manual', 
                'product_type_manual', 'tags_manual', 'collections_manual',
                'vendor_manual', 'sku_manual', 'handle_manual', 'color_manual',
                'frame_style_manual', 'theme_manual', 'metafield1_manual', 'metafield2_manual',
                'condition_manual', 'decoration_material_manual', 'artwork_frame_material_manual',
                'seo_title_manual', 'meta_desc_manual',
                'collections_enabled',
                'google_shopping_enabled', 'google_category_manual',
                'gender_manual', 'age_group_manual', 'gs_condition_manual', 'custom_product_manual',
                'custom_label_0_manual', 'custom_label_1_manual',
                'custom_label_2_manual', 'custom_label_3_manual', 'custom_label_4_manual'
            ] + [f'{key}_manual' for key in SEO_METAFIELD_KEYS]
            manual_value_keys = [
                'manual_title', 'manual_description', 'manual_category', 'manual_category_gid',
                'manual_tags', 'manual_collections', 'manual_sku', 'manual_handle',
                'manual_color', 'manual_frame_style', 'manual_theme',
                'manual_metafield1', 'manual_metafield2',
                'manual_condition', 'manual_decoration_material', 'manual_artwork_frame_material',
                'manual_seo_title', 'manual_meta_desc',
                'manual_google_product_category', 'manual_gender', 'manual_age_group',
                'manual_gs_condition', 'manual_custom_product',
                'manual_custom_label_0', 'manual_custom_label_1',
                'manual_custom_label_2', 'manual_custom_label_3', 'manual_custom_label_4',
                'business_name', 'product_type', 'vendor', 'product_status'
            ] + [f'manual_{key}' for key in SEO_METAFIELD_KEYS]
            for key in manual_toggle_keys + manual_value_keys:
                if key in fresh_settings:
                    task[key] = fresh_settings[key]
            # Map metafield aliases so both naming conventions work
            if 'metafield1_manual' in fresh_settings and 'frame_style_manual' not in fresh_settings:
                task['frame_style_manual'] = fresh_settings['metafield1_manual']
            if 'metafield2_manual' in fresh_settings and 'theme_manual' not in fresh_settings:
                task['theme_manual'] = fresh_settings['metafield2_manual']
            if 'manual_metafield1' in fresh_settings and 'manual_frame_style' not in fresh_settings:
                task['manual_frame_style'] = fresh_settings['manual_metafield1']
            if 'manual_metafield2' in fresh_settings and 'manual_theme' not in fresh_settings:
                task['manual_theme'] = fresh_settings['manual_metafield2']
            logger.info(f"Updated all manual override settings from fresh_settings")
            # CRITICAL: Update variants_data directly in task if provided
            if 'variants_data' in fresh_settings and fresh_settings['variants_data']:
                task['variants_data'] = fresh_settings['variants_data']
                logger.warning(f"🎯 Updated variants_data from fresh_settings: {len(fresh_settings['variants_data'])} variants")
            logger.info(f"Updated task {task_id} with fresh settings")
        
    if not task:
        logger.error(f"Task {task_id} not found in queue. Available tasks: {list(processing_queue.keys())}")
        return jsonify({'error': 'Task not found'}), 404
    
    # Check task status - allow reprocessing for awaiting_review tasks
    if task['status'] not in ['uploaded', 'awaiting_review']:
        return jsonify({'error': f'Task status is {task["status"]}, cannot start processing'}), 400
    
    # Pre-flight checks for required API keys
    if not os.environ.get("GEMINI_API_KEY"):
        logger.error("❌ GEMINI_API_KEY not set - cannot process!")
        return jsonify({'error': 'GEMINI_API_KEY not configured. Please check server environment.'}), 500
    
    shop = get_current_shop()
    if not shop:
        logger.error("❌ No Shopify store connected - cannot create products!")
        return jsonify({'error': 'No Shopify store connected. Please connect your store first.', 'no_shop': True}), 400
    
    # Store shop credentials in task so background threads can use them
    with queue_lock:
        if task_id in processing_queue:
            processing_queue[task_id]['shop_domain'] = shop.shop_domain
            processing_queue[task_id]['shop_access_token'] = shop.access_token
    
    logger.info(f"✅ API keys verified, enqueueing task {task_id} for processing")

    # Validate files exist before enqueueing
    if task.get('raw_artwork_workflow') and not task.get('image_paths'):
        raw_path = task.get('raw_artwork_path') or task.get('analysis_image_path') or task.get('filepath')
        if not raw_path or not os.path.exists(raw_path):
            logger.error(f"❌ Raw artwork file not found: {raw_path}")
            return jsonify({'error': f'Raw artwork file not found at {raw_path}'}), 404
    elif task.get('ready_framed'):
        actual_filepath = task.get('filepath')
        image_paths = task.get('image_paths', [])
        if image_paths:
            actual_filepath = image_paths[0]
        elif not actual_filepath:
            return jsonify({'error': 'Ready-framed file not found'}), 404
        if not os.path.exists(actual_filepath):
            logger.error(f"❌ File not found: {actual_filepath}")
            return jsonify({'error': f'Ready-framed file not found at {actual_filepath}'}), 404
    else:
        filepath = task.get('filepath')
        if not filepath or not os.path.exists(filepath):
            return jsonify({'error': 'Original file not found'}), 404

    # Enqueue the task for the background worker (no thread spawning)
    with queue_lock:
        processing_queue[task_id]['status'] = 'queued'
        processing_queue[task_id]['current_step'] = 'Waiting in queue...'
        processing_queue[task_id].pop('_direct_runner_started', None)
        is_raw_artwork_task = bool(processing_queue[task_id].get('raw_artwork_workflow'))
    save_queue_to_disk()

    if is_raw_artwork_task:
        _start_direct_raw_artwork_runner(task_id)
    else:
        # Ensure the worker thread is alive (handles Gunicorn fork, crashes, etc.)
        _ensure_worker_running()
        # Wake the queue worker
        _task_ready_event.set()

    logger.info(f"✅ Task {task_id} enqueued for sequential processing")
    return jsonify({'message': 'Processing started', 'task_id': task_id})

@app.route('/continue_processing/<task_id>', methods=['POST'])
def continue_processing(task_id):
    """Continue processing a task to create Shopify product"""
    
    # 🎯 CRITICAL: Extract variant data from request
    data = request.get_json() or {}
    variants = data.get('variants', [])
    logger.warning(f"🎯 CONTINUE_PROCESSING RECEIVED VARIANTS: {variants}")
    
    with queue_lock:
        task = processing_queue.get(task_id)
        
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        
        if task['status'] not in ['framed', 'awaiting_approval']:
            return jsonify({'error': 'Task not ready for continuation'}), 400
        
        # Store variant data in task for processing
        if variants:
            processing_queue[task_id]['user_variants'] = variants
            logger.warning(f"🎯 STORED VARIANTS IN TASK: {variants}")
    
    # Enqueue for the queue worker instead of spawning an independent thread
    with queue_lock:
        processing_queue[task_id]['status'] = 'queued_continue'
    save_queue_to_disk()
    _ensure_worker_running()
    _task_ready_event.set()

    return jsonify({'message': 'Processing continued'})

def continue_processing_task(task_id):
    """Continue processing after user review.
    Called by the queue worker which already holds the semaphore."""
    # CRITICAL: Ensure environment variables are available in this thread
    ensure_env_in_thread()

    # Semaphore handled by queue worker (caller)
    logger.info(f"Continue task {task_id} starting (continue flow)")

    try:
        with queue_lock:
            task = processing_queue.get(task_id)
            if not task:
                return
            
            processing_queue[task_id]['status'] = 'processing'
            processing_queue[task_id]['current_step'] = 'Creating Shopify product...'
        
        filename = task['filename']
        
        # Check if this is a ready-framed task with metadata already generated
        if task.get('metadata') and task.get('compressed_path'):
            # Ready-framed workflow - metadata already exists
            metadata = task['metadata']
            compressed_path = task['compressed_path']
            frame_paths = [compressed_path]  # Use the compressed ready-framed image
            
        else:
            # Regular workflow - need to generate metadata
            # Find the compressed image - check if we have a stored path first
            compressed_path = None
            if task.get('compressed_path') and os.path.exists(task['compressed_path']):
                compressed_path = task['compressed_path']
                logger.info(f"Using stored compressed_path: {compressed_path}")
            else:
                # Try to find compressed image - use unique_filename if available, otherwise search flexibly
                search_name = task.get('unique_filename', filename)
                base_name = search_name.split('.')[0]  # Remove extension
                
                # Search for compressed files - match any file containing the base name and ending with _compressed.jpg
                compressed_files = [f for f in os.listdir(PROCESSED_FOLDER) 
                                  if base_name in f and f.endswith('_compressed.jpg')]
                
                if not compressed_files:
                    # Fallback: search for any file with the original filename base
                    original_base = filename.split('.')[0]
                    compressed_files = [f for f in os.listdir(PROCESSED_FOLDER) 
                                      if original_base in f and f.endswith('_compressed.jpg')]
                
                if not compressed_files:
                    logger.error(f"Could not find compressed image. Searched for: {base_name} or {filename.split('.')[0]}")
                    logger.error(f"Available files in processed folder: {[f for f in os.listdir(PROCESSED_FOLDER) if '_compressed.jpg' in f][:10]}")
                    raise Exception(f"Compressed image not found for {filename}")
                
                compressed_path = os.path.join(PROCESSED_FOLDER, compressed_files[0])
                logger.info(f"Found compressed image: {compressed_path}")
            
            # Get frame paths
            frame_paths = [os.path.join(PROCESSED_FOLDER, fname) for fname in task.get('frame_paths', [])]
            
            # Generate metadata with AI
            with queue_lock:
                processing_queue[task_id]['current_step'] = 'Analyzing image with AI...'
            
            logger.info(f"Generating metadata for {filename}")
            logger.info(f"📋 API Key check: GEMINI_API_KEY={'SET' if os.environ.get('GEMINI_API_KEY') else 'NOT SET'}")
            logger.info(f"📋 Image path: {compressed_path}")
            logger.info(f"📋 Image exists: {os.path.exists(compressed_path) if compressed_path else False}")
            
            # CRITICAL: Validate API key before starting AI processing
            import os
            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                error_msg = "GEMINI_API_KEY is not set. Cannot generate AI metadata. Please ensure the server was started with run_server.py or set the environment variable."
                logger.error(f"❌ {error_msg}")
                raise Exception(error_msg)
            logger.info(f"✅ API key validated: {api_key[:20]}...{api_key[-10:]}")
            logger.info(f"⏱️  Starting AI metadata generation (expected duration: 10-30 seconds)...")
            
            # Get all parameters from task data
            custom_prompt = task.get('custom_prompt', '')
            _record_effective_prompt_audit(task_id, task, custom_prompt)
            
            # Get collections from Shopify store via GraphQL for AI to choose from (unless user turned collections off)
            if not task.get('collections_enabled', True):
                available_collections = []
                logger.info("Collections disabled by user - AI will not assign any collections")
            else:
                try:
                    available_collections = get_collections(shop_domain=task.get('shop_domain'), access_token=task.get('shop_access_token'))
                    logger.info(f"Loading {len(available_collections)} Shopify store collections: {available_collections}")
                except Exception as e:
                    logger.error(f"Failed to fetch Shopify collections: {e}. Continuing with empty collections list.")
                    available_collections = []
            
            category_attribute_options = _category_attribute_options_for_task(task)
            # Retry generation before giving up (see ready-framed path for rationale).
            gen_attempts = max(1, int(os.environ.get("GEMINI_METADATA_RETRIES", "5")))
            metadata = None
            for _attempt in range(1, gen_attempts + 1):
                try:
                    metadata = _run_bounded(
                        generate_product_metadata,
                        int(os.environ.get("GEMINI_METADATA_TASK_TIMEOUT_S", "70")),
                        compressed_path,
                        custom_prompt,
                        available_collections,
                        category_attribute_options=category_attribute_options,
                    )
                except FuturesTimeoutError:
                    logger.error("Gemini metadata generation timed out for %s (attempt %d/%d)", filename, _attempt, gen_attempts)
                    metadata = None
                except Exception as gemini_err:
                    logger.warning("Gemini metadata generation failed for %s (attempt %d/%d): %s", filename, _attempt, gen_attempts, gemini_err)
                    metadata = None
                if metadata:
                    break
                if _attempt < gen_attempts:
                    delay = _metadata_retry_backoff_seconds(_attempt)
                    logger.info(
                        "Retrying metadata generation for %s in %ss (attempt %d/%d)",
                        filename, delay, _attempt + 1, gen_attempts,
                    )
                    if delay:
                        time.sleep(delay)
            if not metadata:
                # No fallback listings, ever. A job that cannot produce complete
                # AI metadata fails here so it is seen and fixed, rather than
                # quietly publishing a placeholder product.
                raise Exception(
                    f"AI metadata generation failed after {gen_attempts} attempts for {filename}. "
                    "Nothing was published - processing stopped so this can be checked."
                )
            if not task.get('collections_enabled', True):
                metadata['collections'] = []
            else:
                metadata['collections'] = _resolve_product_collections(
                    metadata, available_collections, log_prefix="Queue collections",
                    mandatory_collections=_mandatory_collections_from_prompt(custom_prompt),
                )
            # CRITICAL: Check if metadata generation failed immediately
            if not metadata:
                error_msg = "Failed to generate metadata from AI. Please check your GEMINI_API_KEY and ensure the API is accessible."
                logger.error(f"❌ {error_msg}")
                logger.error(f"❌ Image path: {compressed_path}")
                logger.error(f"❌ File exists: {os.path.exists(compressed_path) if compressed_path else False}")
                raise Exception(error_msg)
            
            with queue_lock:
                processing_queue[task_id]['current_step'] = 'Creating Shopify product...'
        
        # Create Shopify product using GraphQL
        logger.info(f"Creating Shopify product via GraphQL for {filename}")
        
        # Get shop credentials from task data (stored when processing started)
        _shop_domain = task.get('shop_domain')
        _shop_token = task.get('shop_access_token')
        if not _shop_domain or not _shop_token:
            error_msg = "No Shopify store connected. Please connect your store first."
            logger.error(error_msg)
            raise Exception(error_msg)
        
        # Get publishing settings from task (include SKU for variant creation)
        publishing_settings = None
        if task:
            _fs = task.get('fresh_settings') or {}
            publishing_settings = {
                'selected_channels': task.get('selected_sales_channels', []),
                'selected_markets': task.get('selected_markets', []),
                'sku_manual': task.get('sku_manual', _fs.get('sku_manual', False)),
                'manual_sku': (task.get('manual_sku') or _fs.get('manual_sku') or '').strip(),
                'auto_publish_all_channels': task.get('auto_publish_all_channels', False),
            }
        
        # 🎯 CRITICAL: Add stored variants to metadata before GraphQL
        # Check multiple sources for variants
        user_variants = task.get('user_variants')
        if user_variants:
            metadata['variants'] = user_variants
            logger.warning(f"🎯 CONTINUE_PROCESSING ADDED VARIANTS FROM user_variants: {user_variants}")
        elif task.get('variants_data'):
            metadata['variants'] = task['variants_data']
            logger.warning(f"🎯 CONTINUE_PROCESSING ADDED VARIANTS FROM variants_data: {task['variants_data']}")
        elif task.get('fresh_settings', {}).get('variants_data'):
            metadata['variants'] = task['fresh_settings']['variants_data']
            logger.warning(f"🎯 CONTINUE_PROCESSING ADDED VARIANTS FROM fresh_settings: {task['fresh_settings']['variants_data']}")
        else:
            logger.warning(f"⚠️ CONTINUE_PROCESSING: No variants found in task")
        
        # Hard timeout so queue never sticks on "Creating Shopify product..." indefinitely
        product_data = None
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    create_product_with_graphql,
                    metadata,
                    filename,
                    publishing_settings,
                    compressed_path,
                    _shop_domain,
                    _shop_token
                )
                product_data = future.result(timeout=120)
        except FuturesTimeoutError:
            logger.error("Product creation timed out after 120 seconds")
            with queue_lock:
                if task_id in processing_queue:
                    processing_queue[task_id]['status'] = 'error'
                    processing_queue[task_id]['error'] = 'Product creation timed out after 120 seconds'
            raise Exception("Product creation timed out after 120 seconds")
        
        if not product_data:
            raise Exception("Failed to create Shopify product - check your Shopify store connection")

        # Enrich with category metafields (best-effort, never fails product creation)
        try:
            category_gid = metadata.get('category_gid', '')
            if category_gid and product_data.get('gid'):
                with queue_lock:
                    processing_queue[task_id]['current_step'] = 'Setting category attributes...'
                enrich_product_with_category_metafields(
                    product_gid=product_data['gid'],
                    category_gid=category_gid,
                    image_path=_category_enrichment_image(task, compressed_path),
                    category_attribute_picks=_category_attribute_picks(metadata),
                    shop_domain=_shop_domain,
                    access_token=_shop_token
                )
        except Exception as enrich_err:
            logger.warning(f"Category metafield enrichment failed (non-fatal): {enrich_err}")

        # Success
        with queue_lock:
            processing_queue[task_id]['status'] = 'completed'
            processing_queue[task_id]['current_step'] = 'Complete!'
            processing_queue[task_id]['product_id'] = product_data.get('id')
            processing_queue[task_id]['product_url'] = product_data.get('admin_url')
            processing_queue[task_id]['faq_metafield_verified'] = product_data.get('faq_metafield_verified')

        logger.info(f"Successfully processed {filename} - Product ID: {product_data.get('id')}")
        
        # Cleanup temporary files after successful Shopify upload
        try:
            cleanup_count = temp_file_service.cleanup_after_task(task_id)
            logger.info(f"Cleaned up {cleanup_count} temporary files for task {task_id} after Shopify upload")
        except Exception as cleanup_error:
            logger.warning(f"Error cleaning up temp files for task {task_id}: {cleanup_error}")
        
    except Exception as e:
        error_message = str(e)
        logger.error(f"Error continuing processing for task {task_id}: {error_message}")
        logger.error(f"Error type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        with queue_lock:
            if task_id in processing_queue:
                processing_queue[task_id]['status'] = 'error'
                processing_queue[task_id]['current_step'] = f'Error: {error_message}'
                processing_queue[task_id]['error'] = error_message
                save_queue_to_disk()  # Persist error state
        
        # Cleanup temp files on error as well
        try:
            cleanup_count = temp_file_service.cleanup_after_task(task_id)
            logger.info(f"Cleaned up {cleanup_count} temporary files for task {task_id} after error")
        except Exception as cleanup_error:
            logger.warning(f"Error cleaning up temp files after error: {cleanup_error}")

def _run_bounded(fn, timeout_s, *args, **kwargs):
    """Run fn(*args, **kwargs) with a hard timeout WITHOUT blocking on shutdown.

    The common pattern `with ThreadPoolExecutor() as ex: fut.result(timeout=N)`
    is a trap: when the call overruns, result() raises TimeoutError but the
    `with`-exit then calls executor.shutdown(wait=True), which blocks until the
    runaway call actually finishes. On the single-worker free tier that wedges
    the whole instance (the desktop sees status polls time out, then Render
    restarts the box ~13 min). shutdown(wait=False, cancel_futures=True) lets
    the timeout actually free the worker; the orphaned thread finishes its own
    (already timeout-bounded) HTTP calls and dies on its own.
    """
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn, *args, **kwargs)
    try:
        return fut.result(timeout=timeout_s)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def process_ready_framed_task(task_id, image_path, filename):
    """Process ready-framed images (skip framing, go direct to AI metadata and Shopify)"""
    # CRITICAL: Ensure environment variables are available in this thread
    ensure_env_in_thread()

    # Semaphore handled by queue worker (caller)
    logger.info(f"Task {task_id} starting ready-framed processing")
    log_mem("task-start")

    task = None
    with app.app_context():  # Add Flask application context for database access
        try:
            with queue_lock:
                task = processing_queue.get(task_id)
                if not task:
                    return
                processing_queue[task_id]['status'] = 'processing'
                
                # Get all image paths (multiple images support)
                image_paths = task.get('image_paths', [image_path] if image_path else [])
                if not image_paths:
                    raise Exception("No images found in task")
                
                processing_queue[task_id]['current_step'] = f'Compressing {len(image_paths)} image(s)...'
            
            pipeline_started_at = time.monotonic()
            stage_started_at = pipeline_started_at

            def _finish_stage(stage_name):
                nonlocal stage_started_at
                elapsed = round(time.monotonic() - stage_started_at, 2)
                with queue_lock:
                    if task_id in processing_queue:
                        processing_queue[task_id].setdefault('timings', {})[stage_name] = elapsed
                logger.info("Task %s timing: %s=%.2fs", task_id, stage_name, elapsed)
                stage_started_at = time.monotonic()

            # Step 1: Prepare Shopify product images.
            # Raw-artwork framing tasks can provide analysis_image_path so AI analyzes
            # the original artwork while Shopify receives only framed mockup images.
            worker_images_are_ready = task.get('local_worker_upload') and os.environ.get(
                "LOCAL_WORKER_SKIP_SERVER_RECOMPRESS", "true"
            ).lower() in {"1", "true", "yes", "on"}
            if worker_images_are_ready:
                compressed_paths = [path for path in image_paths if path and os.path.exists(path)]
                logger.info("Using %s worker-optimised image(s) without a second JPEG encode", len(compressed_paths))
            else:
                logger.info(f"Compressing {len(image_paths)} ready-framed image(s)")
                compressed_paths = []
                for i, img_path in enumerate(image_paths):
                    logger.info(f"Compressing image {i+1}/{len(image_paths)}: {os.path.basename(img_path)}")
                    compressed_path = compress_image(img_path, output_folder=None, task_id=task_id, quality=82, fast=True)
                    if compressed_path:
                        compressed_paths.append(compressed_path)
                        with queue_lock:
                            if 'temp_files' not in task:
                                task['temp_files'] = []
                            task['temp_files'].append(compressed_path)
                    else:
                        logger.warning(f"Failed to compress image {i+1}, skipping")
            
            if not compressed_paths:
                raise Exception("Failed to compress any images")
            
            analysis_image_path = task.get('analysis_image_path')
            if analysis_image_path and os.path.exists(analysis_image_path) and worker_images_are_ready:
                primary_compressed_path = analysis_image_path
                logger.info("Using worker-optimised analysis image without a second JPEG encode: %s", os.path.basename(analysis_image_path))
            elif analysis_image_path and os.path.exists(analysis_image_path):
                logger.info(f"Compressing original artwork for AI analysis: {os.path.basename(analysis_image_path)}")
                primary_compressed_path = compress_image(analysis_image_path, output_folder=None, task_id=task_id, quality=82, fast=True)
                if not primary_compressed_path:
                    raise Exception("Failed to compress original artwork for AI analysis")
                with queue_lock:
                    if 'temp_files' not in task:
                        task['temp_files'] = []
                    task['temp_files'].append(primary_compressed_path)
                logger.info(f"AI will analyze original raw artwork: {os.path.basename(primary_compressed_path)}")
            else:
                # Use image #1 in the user's order for AI analysis (first in image_paths / compressed_paths)
                primary_compressed_path = compressed_paths[0]
                logger.info(f"AI will analyze image #1 in order: {os.path.basename(primary_compressed_path)}")

            _finish_stage('image_preparation')
            
            # Store all compressed paths in task
            with queue_lock:
                task['compressed_paths'] = compressed_paths
                task['primary_compressed_path'] = primary_compressed_path
            
            # The AI analysis is mandatory. The old LOCAL_WORKER_USE_GEMINI escape
            # hatch published desktop uploads from deterministic placeholder
            # metadata, which is exactly the silent-quality-drop this must not do.
            with queue_lock:
                processing_queue[task_id]['current_step'] = 'Analyzing primary image with AI...'
            
            # Step 2: Generate metadata with AI (image #1 in the user's chosen order only)
            logger.info(f"Generating metadata for ready-framed {filename} (using image #1 for AI analysis)")
            
            # Use fresh settings if available, otherwise fall back to upload-time settings
            fresh_settings = task.get('fresh_settings', {})
            if fresh_settings:
                logger.info(f"Using fresh settings for task {task_id} with {len(fresh_settings.get('variants_data', []))} variants")
                custom_prompt = (
                    task.get('custom_prompt', '')
                    if task.get('profile_name')
                    else fresh_settings.get('custom_prompt', task.get('custom_prompt', ''))
                )
                
                # Update task with fresh settings for Shopify product creation
                # Helper to resolve metafield aliases (frontend uses metafield1/2, backend uses frame_style/theme)
                def _get_fs(key, alt_key=None, default=None):
                    """Get from fresh_settings with optional alias fallback, then task fallback"""
                    val = fresh_settings.get(key)
                    if val is None and alt_key:
                        val = fresh_settings.get(alt_key)
                    if val is None:
                        val = task.get(key)
                    if val is None and alt_key:
                        val = task.get(alt_key)
                    return val if val is not None else default
                
                # Build the update dict outside the lock (reads from task are safe here
                # because only the queue worker mutates task during processing)
                _update = {
                    'custom_prompt': custom_prompt,
                    'variants_data': fresh_settings.get('variants_data', task.get('variants_data', [])),
                    'business_name': fresh_settings.get('business_name', task.get('business_name', '')),
                    'product_type': fresh_settings.get('product_type', task.get('product_type', '')),
                    'vendor': fresh_settings.get('vendor', task.get('vendor', '')),
                    # Toggle states - core fields
                    'title_manual': _get_fs('title_manual', default=False),
                    'description_manual': _get_fs('description_manual', default=False),
                    'category_manual': _get_fs('category_manual', default=False),
                    'product_type_manual': _get_fs('product_type_manual', default=False),
                    'tags_manual': _get_fs('tags_manual', default=False),
                    'collections_manual': _get_fs('collections_manual', default=False),
                    'collections_enabled': _get_fs('collections_enabled', default=True),
                    'vendor_manual': _get_fs('vendor_manual', default=False),
                    # Toggle states - extended fields
                    'sku_manual': _get_fs('sku_manual', default=False),
                    'handle_manual': _get_fs('handle_manual', default=False),
                    'color_manual': _get_fs('color_manual', default=False),
                    # Metafield toggles - accept both naming conventions
                    'frame_style_manual': _get_fs('frame_style_manual', 'metafield1_manual', default=False),
                    'theme_manual': _get_fs('theme_manual', 'metafield2_manual', default=False),
                    # Category metafield toggles
                    'condition_manual': _get_fs('condition_manual', default=False),
                    'decoration_material_manual': _get_fs('decoration_material_manual', default=False),
                    'artwork_frame_material_manual': _get_fs('artwork_frame_material_manual', default=False),
                    # SEO toggles
                    'seo_title_manual': _get_fs('seo_title_manual', default=False),
                    'meta_desc_manual': _get_fs('meta_desc_manual', default=False),
                    # Manual values - core fields
                    'manual_title': _get_fs('manual_title', default=''),
                    'manual_description': _get_fs('manual_description', default=''),
                    'manual_category': _get_fs('manual_category', default=''),
                    'manual_category_gid': _get_fs('manual_category_gid', default=''),
                    'manual_tags': _get_fs('manual_tags', default=''),
                    'manual_collections': _get_fs('manual_collections', default=''),
                    # Manual values - extended fields
                    'manual_sku': _get_fs('manual_sku', default=''),
                    'manual_handle': _get_fs('manual_handle', default=''),
                    'manual_color': _get_fs('manual_color', default=''),
                    # Category metafield values
                    'manual_condition': _get_fs('manual_condition', default=''),
                    'manual_decoration_material': _get_fs('manual_decoration_material', default=''),
                    'manual_artwork_frame_material': _get_fs('manual_artwork_frame_material', default=''),
                    # Metafield values - accept both naming conventions
                    'manual_frame_style': _get_fs('manual_frame_style', 'manual_metafield1', default=''),
                    'manual_theme': _get_fs('manual_theme', 'manual_metafield2', default=''),
                    # SEO manual values
                    'manual_seo_title': _get_fs('manual_seo_title', default=''),
                    'manual_meta_desc': _get_fs('manual_meta_desc', default=''),
                    # Google Shopping fields (for feeds / CSV / Shopify metafields)
                    'manual_google_product_category': _get_fs('manual_google_product_category', default=''),
                    'manual_gender': _get_fs('manual_gender', default=''),
                    'manual_age_group': _get_fs('manual_age_group', default=''),
                    'manual_gs_condition': _get_fs('manual_gs_condition', default=''),
                    'manual_custom_product': _get_fs('manual_custom_product', default=''),
                    'manual_custom_label_0': _get_fs('manual_custom_label_0', default=''),
                    'manual_custom_label_1': _get_fs('manual_custom_label_1', default=''),
                    'manual_custom_label_2': _get_fs('manual_custom_label_2', default=''),
                    'manual_custom_label_3': _get_fs('manual_custom_label_3', default=''),
                    'manual_custom_label_4': _get_fs('manual_custom_label_4', default=''),
                    # Google Shopping toggle states
                    'google_shopping_enabled': _get_fs('google_shopping_enabled', default=True),
                    'google_category_manual': _get_fs('google_category_manual', default=False),
                    'gender_manual': _get_fs('gender_manual', default=False),
                    'age_group_manual': _get_fs('age_group_manual', default=False),
                    'gs_condition_manual': _get_fs('gs_condition_manual', default=False),
                    'custom_product_manual': _get_fs('custom_product_manual', default=False),
                    'custom_label_0_manual': _get_fs('custom_label_0_manual', default=False),
                    'custom_label_1_manual': _get_fs('custom_label_1_manual', default=False),
                    'custom_label_2_manual': _get_fs('custom_label_2_manual', default=False),
                    'custom_label_3_manual': _get_fs('custom_label_3_manual', default=False),
                    'custom_label_4_manual': _get_fs('custom_label_4_manual', default=False),
                    **{f'{key}_manual': _get_fs(f'{key}_manual', default=False) for key in SEO_METAFIELD_KEYS},
                    # Product settings
                    'product_status': fresh_settings.get('product_status', task.get('product_status', 'ACTIVE')),
                    'review_before_publish': fresh_settings.get('review_before_publish', task.get('review_before_publish', False)),
                    'inventory_quantity': fresh_settings.get('inventory_quantity', task.get('inventory_quantity', '999')),
                    'inventory_policy': fresh_settings.get('inventory_policy', task.get('inventory_policy', 'continue')),
                    # Variant images: apply main listing image to all variants (Ready Framed)
                    'use_main_image_per_variant': _get_fs('use_main_image_per_variant', default=True)
                }
                for key in SEO_METAFIELD_KEYS:
                    _update[f'manual_{key}'] = _get_fs(f'manual_{key}', default='')
                # Apply update atomically under lock so status-polling routes
                # don't read a partially-updated task dict
                with queue_lock:
                    task.update(_update)
            else:
                logger.info(f"Using upload-time settings for task {task_id}")
                custom_prompt = task.get('custom_prompt', '')

            _record_effective_prompt_audit(task_id, task, custom_prompt)
            
            # CRITICAL: Validate API key before starting AI processing.
            # Desktop/local-worker uploads use Gemini vision by default; fallback
            # metadata is only used if AI is explicitly disabled or times out.
            if not os.environ.get("GEMINI_API_KEY"):
                error_msg = "GEMINI_API_KEY is not set. Cannot generate AI metadata. Please ensure the server was started with run_server.py or set the environment variable."
                logger.error(f"❌ {error_msg}")
                raise Exception(error_msg)
            
            # Get collections from Shopify store via GraphQL for AI to choose from (unless user turned collections off)
            # Desktop/local-worker uploads get the SAME automatic collection
            # assignment as browser uploads. (An earlier speed tweak skipped the
            # collection fetch for local-worker jobs, which silently published
            # every desktop listing with zero collections.) The store collection
            # list is cached per instance, so this costs one call per batch.
            if not task.get('collections_enabled', True):
                available_collections = []
                logger.info("Collections disabled by user - AI will not assign any collections")
            else:
                try:
                    available_collections = get_collections(shop_domain=task.get('shop_domain'), access_token=task.get('shop_access_token'))
                    logger.info(f"Loading {len(available_collections)} Shopify store collections: {available_collections}")
                except Exception as e:
                    logger.error(f"Failed to fetch Shopify collections: {e}. Continuing with empty collections list.")
                    available_collections = []
            
            if task.get('analysis_is_framed'):
                # The analysis image is a finished framed mockup (poster hanging in a
                # styled room). All metadata must describe the poster DESIGN only.
                custom_prompt = (
                    "IMPORTANT CONTEXT: The image provided is a product mockup photo — a framed "
                    "poster displayed in a styled room scene. Analyze ONLY the poster artwork "
                    "visible inside the frame. Base the title, description, tags, colors, theme "
                    "and all other metadata purely on that poster design. Completely IGNORE the "
                    "frame, wall, furniture, plants, lighting, shadows and every other part of "
                    "the room around the poster — never mention or describe them.\n\n"
                    + (custom_prompt or "")
                )
                logger.info("Task %s: analysis image is a framed mockup; prompt scoped to poster artwork only", task_id)

            logger.warning(f"READY-FRAMED: Preparing metadata for: {primary_compressed_path}")
            log_mem("before-gemini")
            category_attribute_options = _category_attribute_options_for_task(task)
            metadata_timeout_s = int(os.environ.get("GEMINI_METADATA_TASK_TIMEOUT_S", "70"))
            # Retry generation: a single Gemini miss (e.g. a description that omits
            # one required profile fact, common on abstract pieces) is usually
            # fixed by re-prompting. Retrying here turns most one-off failures into
            # successful listings instead of silently dropping the design.
            gen_attempts = max(1, int(os.environ.get("GEMINI_METADATA_RETRIES", "5")))
            metadata = None
            for _attempt in range(1, gen_attempts + 1):
                try:
                    metadata = _run_bounded(
                        generate_product_metadata,
                        metadata_timeout_s,
                        primary_compressed_path,
                        custom_prompt,
                        available_collections,
                        category_attribute_options=category_attribute_options,
                    )
                except FuturesTimeoutError:
                    logger.error("Gemini metadata generation timed out after %s seconds (attempt %d/%d)", metadata_timeout_s, _attempt, gen_attempts)
                    metadata = None
                except Exception as gemini_err:
                    logger.warning("Gemini metadata generation failed (attempt %d/%d): %s", _attempt, gen_attempts, gemini_err)
                    metadata = None
                if metadata:
                    break
                if _attempt < gen_attempts:
                    delay = _metadata_retry_backoff_seconds(_attempt)
                    logger.info(
                        "Retrying metadata generation for %s in %ss (attempt %d/%d)",
                        filename, delay, _attempt + 1, gen_attempts,
                    )
                    if delay:
                        time.sleep(delay)
            log_mem("after-gemini")
            if not metadata:
                # No fallback listings, ever. See the queue path above.
                raise Exception(
                    f"AI metadata generation failed after {gen_attempts} attempts for {filename}. "
                    "Nothing was published - processing stopped so this can be checked."
                )
            _finish_stage('metadata_generation')
            if metadata and not task.get('collections_enabled', True):
                metadata['collections'] = []
                metadata['_collections_disabled'] = True

            # CRITICAL: Check if metadata generation failed immediately
            if not metadata:
                error_msg = "Failed to generate metadata from AI. Please check your GEMINI_API_KEY and ensure the API is accessible."
                logger.error(f"❌ {error_msg}")
                logger.error(f"❌ Image path: {primary_compressed_path}")
                logger.error(f"❌ File exists: {os.path.exists(primary_compressed_path) if primary_compressed_path else False}")
                raise Exception(error_msg)
            
            logger.warning(f"🔍 READY-FRAMED: METADATA RESULT: {metadata.get('category', 'NO CATEGORY')}")

            # Override Google Shopping fields based on toggle states (for feeds / CSV)
            if not task.get('google_shopping_enabled', True):
                # Google Shopping is turned off - clear ALL Google Shopping fields
                metadata['google_product_category'] = ''
                metadata['gender'] = ''
                metadata['age_group'] = ''
                metadata['condition'] = ''
                metadata['custom_product'] = ''
                for i in range(5):
                    metadata[f'custom_label_{i}'] = ''
                logger.info("Google Shopping disabled by user - cleared all Google Shopping values")
            else:
                # Apply manual overrides only when toggle is set to manual AND value is non-empty
                if task.get('google_category_manual') and task.get('manual_google_product_category'):
                    metadata['google_product_category'] = task['manual_google_product_category']
                if task.get('gender_manual') and task.get('manual_gender'):
                    metadata['gender'] = task['manual_gender']
                if task.get('age_group_manual') and task.get('manual_age_group'):
                    metadata['age_group'] = task['manual_age_group']
                if task.get('gs_condition_manual') and task.get('manual_gs_condition'):
                    metadata['condition'] = task['manual_gs_condition']
                if task.get('custom_product_manual') and task.get('manual_custom_product'):
                    metadata['custom_product'] = task['manual_custom_product']
                for i in range(5):
                    toggle_key = f'custom_label_{i}_manual'
                    value_key = f'manual_custom_label_{i}'
                    if task.get(toggle_key) and task.get(value_key):
                        metadata[f'custom_label_{i}'] = task[value_key]

            _apply_measured_orientation(metadata, primary_compressed_path, task)
            _apply_manual_metafield_overrides(metadata, task)

            # CRITICAL: Override AI category with manual category if specified
            if task.get('category_manual', False) and task.get('manual_category'):
                manual_cat = task['manual_category'].strip()
                logger.warning(f"🔧 PREVIEW OVERRIDE: Replacing AI category '{metadata.get('category')}' with manual category '{manual_cat}'")
                metadata['category'] = manual_cat
                if task.get('manual_category_gid'):
                    metadata['category_gid'] = task['manual_category_gid']
                    logger.warning(f"🔧 PREVIEW OVERRIDE: Using taxonomy GID '{metadata['category_gid']}'")
                else:
                    # Try to resolve category name to GID via taxonomy lookup
                    resolved_gid = resolve_category_name_to_gid(manual_cat)
                    if resolved_gid:
                        metadata['category_gid'] = resolved_gid
                        logger.warning(f"🔧 PREVIEW OVERRIDE: Resolved taxonomy GID '{resolved_gid}' from name")

            # Also resolve AI-generated category to GID if no manual override
            if not metadata.get('category_gid') and metadata.get('category'):
                resolved_gid = resolve_category_name_to_gid(metadata['category'])
                if resolved_gid:
                    metadata['category_gid'] = resolved_gid
                    logger.warning(f"🔧 AI CATEGORY: Resolved taxonomy GID '{resolved_gid}' from AI category '{metadata['category']}'")

            # CRITICAL: Override AI tags with manual tags if specified
            if task.get('tags_manual', False) and task.get('manual_tags'):
                manual_tags = task['manual_tags'].strip()
                if manual_tags:
                    # Convert comma-separated string to list
                    metadata['tags'] = [tag.strip() for tag in manual_tags.split(',') if tag.strip()]
                    logger.warning(f"🔧 TAGS OVERRIDE: Replacing AI tags with manual tags: {metadata['tags']}")
                else:
                    metadata['tags'] = []
                    logger.warning(f"🔧 TAGS OVERRIDE: Using empty tags list")
            
            # CRITICAL: Override AI collections with manual collections if specified
            if task.get('collections_manual', False) and task.get('manual_collections'):
                manual_collections = task['manual_collections'].strip()
                if manual_collections:
                    # Convert comma-separated string to list
                    metadata['collections'] = [col.strip() for col in manual_collections.split(',') if col.strip()]
                    logger.warning(f"🔧 COLLECTIONS OVERRIDE: Replacing AI collections with manual collections: {metadata['collections']}")
                else:
                    metadata['collections'] = []
                    logger.warning(f"🔧 COLLECTIONS OVERRIDE: Using empty collections list")
            
            # Manual collections are the user's explicit choice - never widen them
            # with auto-matches. Only AI/auto mode goes through resolution.
            _manual_collections_set = bool(
                task.get('collections_manual', False) and task.get('manual_collections')
            )
            if task.get('collections_enabled', True) and not _manual_collections_set:
                metadata['collections'] = _resolve_product_collections(
                    metadata, available_collections, log_prefix="Ready-framed collections",
                    mandatory_collections=_mandatory_collections_from_prompt(custom_prompt),
                )

            discovery_enabled = os.environ.get(
                "LOCAL_WORKER_ENABLE_DISCOVERY", "true"
            ).lower() in {"1", "true", "yes", "on"}
            try:
                _shop_domain_for_recs = task.get('shop_domain')
                _shop_token_for_recs = task.get('shop_access_token')
                if discovery_enabled and _shop_domain_for_recs and _shop_token_for_recs:
                    metadata['discovery_recommendations'] = _run_bounded(
                        recommend_products_for_discovery,
                        25,
                        metadata,
                        shop_domain=_shop_domain_for_recs,
                        access_token=_shop_token_for_recs,
                    )
                    logger.info("Discovery recommendations selected: %s", metadata.get('discovery_recommendations'))
                elif task.get('local_worker_upload'):
                    logger.info("Deferred discovery recommendations for fast local-worker publishing")
            except Exception as exc:
                logger.warning("Ready-framed discovery recommendation lookup failed: %s", exc)

            _finish_stage('metadata_postprocessing')

            # Check if review is enabled - if so, stop here and wait for approval
            review_required = task.get('review_before_publish', False)
            
            if review_required:
                # Step 3: Review mode - store metadata and wait for user approval
                with queue_lock:
                    processing_queue[task_id]['status'] = 'awaiting_review'
                    processing_queue[task_id]['current_step'] = 'Ready for review - awaiting user approval'
                    processing_queue[task_id]['metadata'] = metadata
                    processing_queue[task_id]['compressed_path'] = primary_compressed_path

                logger.info(f"Ready-framed processing awaiting user review for {filename}")
            else:
                # Step 3: Create Shopify product immediately for ready-framed workflow (no review)
                with queue_lock:
                    processing_queue[task_id]['current_step'] = 'Creating Shopify product...'
                
                # Create Shopify product with user configuration
                logger.info(f"Creating Shopify product for ready-framed {filename}")
                
                # Get shop credentials from task data (stored when processing started)
                _shop_domain = task.get('shop_domain')
                _shop_token = task.get('shop_access_token')
                if not _shop_domain or not _shop_token:
                    error_msg = "No Shopify store connected. Please connect your store first."
                    logger.error(error_msg)
                    raise Exception(error_msg)
                
                # Use all compressed images for product creation
                compressed_paths = task.get('compressed_paths', [primary_compressed_path])
                
                # Create product with current task settings using GraphQL
                # Get publishing settings from task (include SKU for variant creation)
                publishing_settings = None
                if task:
                    _fs = task.get('fresh_settings') or {}
                    publishing_settings = {
                        'selected_channels': task.get('selected_sales_channels', []),
                        'selected_markets': task.get('selected_markets', []),
                        'sku_manual': task.get('sku_manual', _fs.get('sku_manual', False)),
                        'manual_sku': (task.get('manual_sku') or _fs.get('manual_sku') or '').strip(),
                        'inventory_policy': task.get('inventory_policy', _fs.get('inventory_policy', 'continue')),
                        'auto_publish_all_channels': task.get('auto_publish_all_channels', False),
                    }
                
                # 🎯 CRITICAL: Add stored variants to metadata for ready-framed workflow
                # Check multiple sources for variants: fresh_settings first, then task-level variants_data
                stored_variants = None
                fresh_settings = task.get('fresh_settings', {})
                
                if fresh_settings and fresh_settings.get('variants_data'):
                    stored_variants = fresh_settings['variants_data']
                    logger.warning(f"🎯 READY-FRAMED: Using variants from fresh_settings: {len(stored_variants)} variants")
                elif task.get('variants_data'):
                    stored_variants = task.get('variants_data')
                    logger.warning(f"🎯 READY-FRAMED: Using variants from task.variants_data: {len(stored_variants)} variants")
                else:
                    logger.error(f"❌ READY-FRAMED: NO VARIANTS FOUND!")
                    logger.error(f"❌ task.variants_data: {task.get('variants_data')}")
                    logger.error(f"❌ fresh_settings: {fresh_settings}")
                    logger.error(f"❌ fresh_settings.variants_data: {fresh_settings.get('variants_data') if fresh_settings else 'N/A'}")
                
                if stored_variants and len(stored_variants) > 0:
                    metadata['variants'] = stored_variants
                    logger.warning(f"🎯 READY-FRAMED ADDED VARIANTS TO METADATA: {stored_variants}")
                    for i, v in enumerate(stored_variants):
                        logger.warning(f"   Variant {i+1}: {v}")
                else:
                    logger.error(f"❌ READY-FRAMED: stored_variants is empty/None - defaults will be used!")
                
                # Create product with first image, then add all images (hard timeout so queue never sticks)
                product_data = None
                use_main_image_per_variant = task.get('use_main_image_per_variant', True)
                log_mem("before-create")
                try:
                    # Hard timeout that actually frees the worker on overrun
                    # (see _run_bounded: the stock `with ThreadPoolExecutor`
                    # blocks on shutdown(wait=True) and wedges the instance).
                    product_data = _run_bounded(
                        create_product_with_graphql,
                        180,
                        metadata,
                        filename,
                        publishing_settings,
                        compressed_paths,
                        _shop_domain,
                        _shop_token,
                        use_main_image_per_variant=use_main_image_per_variant
                    )
                except FuturesTimeoutError:
                    logger.error("Product creation timed out after 180 seconds")
                    with queue_lock:
                        if task_id in processing_queue:
                            processing_queue[task_id]['status'] = 'error'
                            processing_queue[task_id]['current_step'] = 'Error: Product creation timed out'
                            processing_queue[task_id]['error'] = 'Product creation timed out after 180 seconds'
                    raise Exception("Product creation timed out after 180 seconds")
                
                log_mem("after-create")
                _finish_stage('shopify_product_creation')
                if not product_data:
                    raise Exception("Failed to create Shopify product")

                # Enrich with category metafields (best-effort, never fails product creation)
                try:
                    category_gid = metadata.get('category_gid', '')
                    if category_gid and product_data.get('gid'):
                        with queue_lock:
                            processing_queue[task_id]['current_step'] = 'Setting category attributes...'
                        enrich_product_with_category_metafields(
                            product_gid=product_data['gid'],
                            category_gid=category_gid,
                            image_path=_category_enrichment_image(task, compressed_paths),
                            category_attribute_picks=_category_attribute_picks(metadata),
                            shop_domain=_shop_domain,
                            access_token=_shop_token
                        )
                except Exception as enrich_err:
                    logger.warning(f"Category metafield enrichment failed (non-fatal): {enrich_err}")

                _finish_stage('category_enrichment')

                # Success
                with queue_lock:
                    processing_queue[task_id]['status'] = 'completed'
                    processing_queue[task_id]['current_step'] = 'Complete!'
                    processing_queue[task_id]['metadata'] = metadata
                    processing_queue[task_id]['compressed_path'] = primary_compressed_path  # For backward compatibility
                    processing_queue[task_id]['compressed_paths'] = compressed_paths  # Store all compressed paths
                    processing_queue[task_id]['product_id'] = product_data.get('id')
                    processing_queue[task_id]['product_url'] = product_data.get('admin_url')
                    processing_queue[task_id]['faq_metafield_verified'] = product_data.get('faq_metafield_verified')
                    processing_queue[task_id]['timings']['total'] = round(time.monotonic() - pipeline_started_at, 2)
                save_queue_to_disk()

                logger.info(f"Ready-framed processing completed for {filename}")
            
        except Exception as e:
            error_message = str(e)
            logger.error(f"Error processing ready-framed {filename}: {error_message}")
            logger.error(f"Error type: {type(e).__name__}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            with queue_lock:
                if task_id in processing_queue:
                    processing_queue[task_id]['status'] = 'error'
                    processing_queue[task_id]['current_step'] = f'Error: {error_message}'
                    processing_queue[task_id]['error'] = error_message
                    save_queue_to_disk()  # Persist error state
        finally:
            if task and task.get('local_worker_upload'):
                try:
                    cleanup_count = temp_file_service.cleanup_after_task(task_id)
                    logger.info(
                        "Cleaned up %s local-worker upload file(s) immediately after task %s",
                        cleanup_count,
                        task_id,
                    )
                except Exception as cleanup_error:
                    logger.warning("Could not clean local-worker uploads for task %s: %s", task_id, cleanup_error)
            logger.info(f"Task {task_id} finished ready-framed processing")

# Product preset management endpoints
@app.route('/save_product_preset', methods=['POST'])
@login_required
def save_product_preset():
    try:
        from flask_login import current_user
        from models import UserProductPreset
        
        # Use hardcoded user for now
        user_id = "081f126d"  # AJS123's user ID
        
        data = request.get_json()
        business_name = data.get('business_name', '').strip()
        product_type = data.get('product_type', '').strip()
        vendor = data.get('vendor', '').strip()
        platform = data.get('platform', 'Shopify').strip()
        
        # Find existing preset or create new one (one preset per user)
        preset = UserProductPreset.query.filter_by(user_id=user_id).first()
        
        if not preset:
            preset = UserProductPreset()
            preset.user_id = user_id
            db.session.add(preset)
        
        # Update preset values
        preset.business_name = business_name
        preset.product_type = product_type
        preset.vendor = vendor
        preset.platform = platform
        preset.updated_at = datetime.now()
        
        db.session.commit()
        
        logger.debug(f"Successfully saved product preset for user: {user_id}")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error saving product preset: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/load_product_preset', methods=['GET'])
@login_required
def load_product_preset():
    try:
        from flask_login import current_user
        from models import UserProductPreset
        
        # Use hardcoded user for now
        user_id = "081f126d"  # AJS123's user ID
        
        preset = UserProductPreset.query.filter_by(user_id=user_id).first()
        
        if not preset:
            return jsonify({'error': 'No saved preset found'}), 404
        
        return jsonify({
            'business_name': preset.business_name or '',
            'product_type': preset.product_type or '',
            'vendor': preset.vendor or '',
            'platform': preset.platform or 'Shopify'
        })
    except Exception as e:
        logger.error(f"Error loading product preset: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/get_publishing_settings', methods=['GET'])
@login_required
def get_publishing_settings():
    try:
        logger.info("Fetching publishing settings from Shopify...")
        # Resolve credentials from current user's shop to avoid "No Shopify store connected" errors
        _shop_domain = None
        _shop_token = None
        try:
            shop = get_current_shop()
            if shop:
                _shop_domain = shop.shop_domain
                _shop_token = shop.access_token
                logger.info(f"📡 Using shop credentials for publishing settings: {_shop_domain}")
        except Exception as cred_err:
            logger.warning(f"📡 Could not resolve shop credentials: {cred_err}")
        
        if not _shop_domain or not _shop_token:
            logger.warning("No Shopify store connected - returning fallback with no_shop flag")
            fallback = {
                'publications': {'edges': []},
                'markets': {'edges': []},
                'no_shop': True,
                'error': 'No Shopify store connected. Connect your store in Settings.'
            }
            return jsonify(fallback)
        
        settings = get_sales_channels(shop_domain=_shop_domain, access_token=_shop_token)
        
        if settings:
            pub_count = 0
            market_count = 0
            if isinstance(settings, dict):
                pub_data = settings.get('publications', {})
                market_data = settings.get('markets', {})
                if isinstance(pub_data, dict):
                    pub_count = len(pub_data.get('edges', []))
                if isinstance(market_data, dict):
                    market_count = len(market_data.get('edges', []))
            logger.info(f"Successfully loaded {pub_count} publishing channels and {market_count} catalogs from Shopify")
            return jsonify(settings)
        else:
            logger.warning("No publishing settings returned from Shopify API, using fallback data")
            # Return fallback data with proper structure
            fallback_data = {
                'publications': {
                    'edges': [
                        {'node': {'id': 'gid://shopify/Publication/web', 'name': 'Online Store', 'supportsFuturePublishing': True, 'app': {'title': 'Online Store'}}},
                        {'node': {'id': 'gid://shopify/Publication/pos', 'name': 'Point of Sale', 'supportsFuturePublishing': True, 'app': {'title': 'POS'}}},
                        {'node': {'id': 'gid://shopify/Publication/facebook', 'name': 'Facebook & Instagram', 'supportsFuturePublishing': True, 'app': {'title': 'Facebook'}}},
                        {'node': {'id': 'gid://shopify/Publication/google', 'name': 'Google & YouTube', 'supportsFuturePublishing': True, 'app': {'title': 'Google'}}}
                    ]
                },
                'markets': {
                    'edges': [
                        {'node': {'id': 'gid://shopify/Market/1', 'name': 'United Kingdom', 'primary': True, 'enabled': True}},
                        {'node': {'id': 'gid://shopify/Market/2', 'name': 'International', 'primary': False, 'enabled': True}}
                    ]
                }
            }
            logger.info("Returning fallback publishing data")
            return jsonify(fallback_data)
    except Exception as e:
        logger.error(f"Error getting publishing settings: {e}")
        # Return fallback data even on error
        fallback_data = {
            'publications': {
                'edges': [
                    {'node': {'id': 'gid://shopify/Publication/web', 'name': 'Online Store', 'supportsFuturePublishing': True, 'app': {'title': 'Online Store'}}},
                    {'node': {'id': 'gid://shopify/Publication/pos', 'name': 'Point of Sale', 'supportsFuturePublishing': True, 'app': {'title': 'POS'}}}
                ]
            },
            'markets': {
                'edges': [
                    {'node': {'id': 'gid://shopify/Market/1', 'name': 'United Kingdom', 'primary': True, 'enabled': True}}
                ]
            }
        }
        return jsonify(fallback_data)

@app.route('/save_publishing_selections', methods=['POST'])
@login_required
def save_publishing_selections():
    """Save user's publishing channel selections for use during product creation"""
    try:
        data = request.get_json()
        if data:
            selected_channels = data.get('selected_channels', [])
            selected_markets = data.get('selected_markets', [])
        else:
            selected_channels = []
            selected_markets = []
        
        # Store in session for access during product creation
        session['selected_sales_channels'] = selected_channels
        session['selected_markets'] = selected_markets
        
        logger.info(f"📡 SAVED PUBLISHING SELECTIONS - Channels: {len(selected_channels)}, Markets: {len(selected_markets)}")
        
        return jsonify({
            'success': True,
            'channels_saved': len(selected_channels),
            'markets_saved': len(selected_markets)
        })
    except Exception as e:
        logger.error(f"Error saving publishing selections: {e}")
        return jsonify({'error': str(e)}), 500

# Instruction Presets API endpoints
@app.route('/save_instruction_preset', methods=['POST'])
@login_required
def save_instruction_preset():
    """Save a custom AI instruction preset"""
    try:
        data = request.get_json()
        preset_name = data.get('preset_name', '').strip()
        instruction_content = data.get('instruction_content', '').strip()
        
        if not preset_name or not instruction_content:
            return jsonify({'error': 'Preset name and instruction content are required'}), 400
        
        user_id = current_user.id
        
        # Check if preset name already exists for this user
        existing_preset = models.InstructionPreset.query.filter_by(
            user_id=user_id, 
            preset_name=preset_name
        ).first()
        
        if existing_preset:
            # Update existing preset
            existing_preset.instruction_content = instruction_content
            existing_preset.updated_at = datetime.now()
            db.session.commit()
            logger.info(f"Updated instruction preset '{preset_name}' for user {user_id}")
        else:
            # Create new preset
            preset = models.InstructionPreset()
            preset.user_id = user_id
            preset.preset_name = preset_name
            preset.instruction_content = instruction_content
            db.session.add(preset)
            db.session.commit()
            logger.info(f"Created new instruction preset '{preset_name}' for user {user_id}")
        
        return jsonify({'success': True, 'message': f'Preset "{preset_name}" saved successfully'})
        
    except Exception as e:
        logger.error(f"Error saving instruction preset: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/get_instruction_presets', methods=['GET'])
@login_required
def get_instruction_presets():
    """Get all instruction presets for the current user"""
    try:
        user_id = current_user.id
        presets = models.InstructionPreset.query.filter_by(user_id=user_id).order_by(models.InstructionPreset.created_at.desc()).all()
        
        preset_list = []
        for preset in presets:
            preset_list.append({
                'id': preset.id,
                'preset_name': preset.preset_name,
                'instruction_content': preset.instruction_content,
                'created_at': preset.created_at.isoformat(),
                'updated_at': preset.updated_at.isoformat()
            })
        
        return jsonify({'presets': preset_list})
        
    except Exception as e:
        logger.error(f"Error loading instruction presets: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/load_instruction_preset/<int:preset_id>', methods=['GET'])
@login_required
def load_instruction_preset(preset_id):
    """Load a specific instruction preset"""
    try:
        user_id = current_user.id
        preset = models.InstructionPreset.query.filter_by(id=preset_id, user_id=user_id).first()
        
        if not preset:
            return jsonify({'error': 'Preset not found'}), 404
        
        return jsonify({
            'id': preset.id,
            'preset_name': preset.preset_name,
            'instruction_content': preset.instruction_content
        })
        
    except Exception as e:
        logger.error(f"Error loading instruction preset: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/delete_instruction_preset/<int:preset_id>', methods=['DELETE'])
@login_required
def delete_instruction_preset(preset_id):
    """Delete an instruction preset"""
    try:
        user_id = current_user.id
        preset = models.InstructionPreset.query.filter_by(id=preset_id, user_id=user_id).first()
        
        if not preset:
            return jsonify({'error': 'Preset not found'}), 404
        
        preset_name = preset.preset_name
        db.session.delete(preset)
        db.session.commit()
        
        logger.info(f"Deleted instruction preset '{preset_name}' for user {user_id}")
        return jsonify({'success': True, 'message': f'Preset "{preset_name}" deleted successfully'})
        
    except Exception as e:
        logger.error(f"Error deleting instruction preset: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/approve_and_publish', methods=['POST'])
@login_required
def approve_and_publish():
    """Approve reviewed metadata and publish to Shopify (non-blocking)"""
    try:
        data = request.get_json()
        task_id = data.get('task_id')
        variants = data.get('variants', [])
        
        logger.info(f"APPROVE_AND_PUBLISH called for task {task_id} with {len(variants)} variants")
        
        if not task_id:
            return jsonify({'error': 'Task ID is required'}), 400
        
        with queue_lock:
            if task_id not in processing_queue:
                return jsonify({'error': 'Task not found'}), 404
            
            task = processing_queue[task_id]
            if task['status'] != 'awaiting_review':
                return jsonify({'error': 'Task is not awaiting review'}), 400
            
            # Update status to processing
            processing_queue[task_id]['status'] = 'processing'
            processing_queue[task_id]['current_step'] = 'Creating Shopify product...'
        
        # Validate required data before starting background thread
        compressed_paths = task.get('compressed_paths', [])
        if not compressed_paths:
            single_path = task.get('compressed_path')
            if single_path:
                compressed_paths = [single_path]
        metadata = task.get('metadata')
        filename = task.get('filename')
        
        if not all([compressed_paths, metadata, filename]):
            with queue_lock:
                processing_queue[task_id]['status'] = 'error'
                processing_queue[task_id]['error'] = 'Missing required task data'
            return jsonify({'error': 'Missing required task data'}), 400
        
        # Get shop credentials (we're in request context here, resolve now)
        _shop_domain = task.get('shop_domain')
        _shop_token = task.get('shop_access_token')
        if not _shop_domain or not _shop_token:
            shop = get_current_shop()
            if shop:
                _shop_domain = shop.shop_domain
                _shop_token = shop.access_token
                # Store in task so background thread can use them
                with queue_lock:
                    processing_queue[task_id]['shop_domain'] = _shop_domain
                    processing_queue[task_id]['shop_access_token'] = _shop_token
            else:
                with queue_lock:
                    processing_queue[task_id]['status'] = 'error'
                    processing_queue[task_id]['error'] = 'No Shopify store connected'
                return jsonify({'error': 'No Shopify store connected. Please connect your store first.', 'no_shop': True}), 400
        
        # Prepare publishing settings (include SKU so GraphQL can apply to product/variants)
        # Prefer request body (current form at approve time) over task/fresh_settings
        _fs = task.get('fresh_settings') or {}
        _data = data or {}
        sku_manual = _data.get('sku_manual') if ('sku_manual' in _data and _data.get('sku_manual') is not None) else (task.get('sku_manual', _fs.get('sku_manual', False)))
        if isinstance(sku_manual, str):
            sku_manual = sku_manual.lower() in ('true', '1', 'yes')
        manual_sku = (_data.get('manual_sku') or task.get('manual_sku') or _fs.get('manual_sku') or '').strip()
        if isinstance(manual_sku, str):
            manual_sku = manual_sku.strip()
        publishing_settings = {
            'selected_channels': task.get('selected_sales_channels', []),
            'selected_markets': task.get('selected_markets', []),
            'sku_manual': bool(sku_manual),
            'manual_sku': manual_sku or '',
            'auto_publish_all_channels': task.get('auto_publish_all_channels', False),
        }
        logger.info(f"APPROVE: publishing_settings sku_manual={publishing_settings['sku_manual']!r} manual_sku={publishing_settings['manual_sku']!r}")
        
        # Add variants to metadata from multiple sources
        if variants:
            metadata['variants'] = variants
            logger.info(f"APPROVE: Using variants from request: {len(variants)}")
        elif task.get('user_variants'):
            metadata['variants'] = task['user_variants']
            logger.info(f"APPROVE: Using variants from task.user_variants: {len(task['user_variants'])}")
        elif task.get('variants_data'):
            metadata['variants'] = task['variants_data']
            logger.info(f"APPROVE: Using variants from task.variants_data: {len(task['variants_data'])}")
        elif task.get('fresh_settings', {}).get('variants_data'):
            metadata['variants'] = task['fresh_settings']['variants_data']
            logger.info(f"APPROVE: Using variants from fresh_settings")
        else:
            logger.warning(f"APPROVE: No variants found in any source")
        
        # ENFORCE collections_enabled: clear collections if user turned them off
        if not task.get('collections_enabled', True):
            metadata['collections'] = []
            logger.info(f"APPROVE: collections_enabled=False — cleared collections")
        
        # Store updated metadata back into task for the background thread
        with queue_lock:
            processing_queue[task_id]['metadata'] = metadata
            processing_queue[task_id]['compressed_paths'] = compressed_paths
        
        # Enqueue for the queue worker instead of spawning an independent thread
        with queue_lock:
            processing_queue[task_id]['status'] = 'queued_approve'
            processing_queue[task_id]['_approve_args'] = {
                'publishing_settings': publishing_settings,
                'shop_domain': _shop_domain,
                'shop_token': _shop_token,
            }
        save_queue_to_disk()
        _ensure_worker_running()
        _task_ready_event.set()

        logger.info(f"APPROVE: Task {task_id} enqueued for approval processing")
        return jsonify({'success': True, 'message': 'Product creation started', 'task_id': task_id})
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error in approve_and_publish: {error_msg}")
        import traceback
        logger.error(traceback.format_exc())
        with queue_lock:
            if task_id in processing_queue:
                processing_queue[task_id]['status'] = 'error'
                processing_queue[task_id]['error'] = error_msg
        return jsonify({'error': error_msg}), 500


def _approve_publish_background(task_id, metadata, filename, publishing_settings, compressed_paths, shop_domain, shop_token):
    """Approve and publish to Shopify.
    Called by the queue worker which already holds the semaphore."""
    ensure_env_in_thread()

    # Semaphore handled by queue worker (caller)
    logger.info(f"Approve task {task_id} starting")

    with app.app_context():
        try:
            logger.info(f"APPROVE BG: Starting product creation for task {task_id}")
            with queue_lock:
                task = processing_queue.get(task_id)
            use_main_image_per_variant = task.get('use_main_image_per_variant', True) if task else True
            
            product_data = create_product_with_graphql(
                metadata,
                filename,
                publishing_settings,
                compressed_paths,
                shop_domain,
                shop_token,
                use_main_image_per_variant=use_main_image_per_variant
            )
            
            if not product_data:
                logger.error(f"APPROVE BG: Product creation returned None for task {task_id}")
                with queue_lock:
                    if task_id in processing_queue:
                        processing_queue[task_id]['status'] = 'error'
                        processing_queue[task_id]['error'] = 'Failed to create Shopify product'
                return
            
            logger.info(f"APPROVE BG: Product created successfully: {product_data.get('admin_url')}")
            
            # Enrich with category metafields (best-effort)
            try:
                category_gid = metadata.get('category_gid', '')
                if category_gid and product_data.get('gid'):
                    with queue_lock:
                        if task_id in processing_queue:
                            processing_queue[task_id]['current_step'] = 'Setting category attributes...'
                    primary_image = compressed_paths[0] if compressed_paths else None
                    enrich_product_with_category_metafields(
                        product_gid=product_data['gid'],
                        category_gid=category_gid,
                        image_path=_category_enrichment_image(task, primary_image),
                        category_attribute_picks=_category_attribute_picks(metadata),
                        shop_domain=shop_domain,
                        access_token=shop_token
                    )
            except Exception as enrich_err:
                logger.warning(f"Category metafield enrichment failed (non-fatal): {enrich_err}")
            
            # Mark task as completed
            with queue_lock:
                if task_id in processing_queue:
                    processing_queue[task_id]['status'] = 'completed'
                    processing_queue[task_id]['current_step'] = 'Complete!'
                    processing_queue[task_id]['product_id'] = product_data.get('id')
                    processing_queue[task_id]['product_url'] = product_data.get('admin_url')
                    save_queue_to_disk()
            
            logger.info(f"APPROVE BG: Task {task_id} completed successfully")
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"APPROVE BG: Error for task {task_id}: {error_msg}")
            import traceback
            logger.error(traceback.format_exc())
            with queue_lock:
                if task_id in processing_queue:
                    processing_queue[task_id]['status'] = 'error'
                    processing_queue[task_id]['error'] = error_msg
                    processing_queue[task_id]['current_step'] = f'Error: {error_msg[:100]}'
                    save_queue_to_disk()

@app.route('/update_task_metadata', methods=['POST'])
@login_required
def update_task_metadata():
    """Update metadata field for a task awaiting review"""
    try:
        data = request.get_json()
        task_id = data.get('task_id')
        field_name = data.get('field_name')
        new_value = data.get('new_value')
        
        if not all([task_id, field_name, new_value is not None]):
            return jsonify({'error': 'Task ID, field name, and new value are required'}), 400
        
        with queue_lock:
            if task_id not in processing_queue:
                return jsonify({'error': 'Task not found'}), 404
            
            task = processing_queue[task_id]
            if task['status'] != 'awaiting_review':
                return jsonify({'error': 'Task is not awaiting review'}), 400
            
            if 'metadata' not in task:
                return jsonify({'error': 'No metadata found for task'}), 400
            
            # Update the metadata field
            if field_name == 'tags' and isinstance(new_value, str):
                # Handle tags as comma-separated string
                task['metadata']['tags'] = [tag.strip() for tag in new_value.split(',') if tag.strip()]
            elif field_name == 'collections' and isinstance(new_value, str):
                # Handle collections as comma-separated string
                task['metadata']['collections'] = [col.strip() for col in new_value.split(',') if col.strip()]
            elif field_name.startswith('metafield_'):
                # Handle metafield updates
                if 'metafields' not in task['metadata']:
                    task['metadata']['metafields'] = {}
                metafield_key = field_name.replace('metafield_', '')
                task['metadata']['metafields'][metafield_key] = new_value
            elif field_name == 'sku':
                # Handle SKU - store in task for variant creation
                task['manual_sku'] = new_value
            elif field_name == 'vendor':
                # Handle vendor
                task['vendor'] = new_value
            elif field_name == 'seo_title':
                # Handle SEO title
                task['metadata']['seo_title'] = new_value
            else:
                # Handle other fields as direct string values
                task['metadata'][field_name] = new_value
        
        logger.info(f"Updated {field_name} for task {task_id}")
        return jsonify({'success': True})
        
    except Exception as e:
        logger.error(f"Error updating task metadata: {e}")
        return jsonify({'error': str(e)}), 500

def _get_user_profiles_columns(conn):
    """Return set of column names for user_profiles. Works with SQLite and PostgreSQL."""
    from sqlalchemy import text
    drv = db.engine.url.drivername
    if drv == 'sqlite':
        r = conn.execute(text("PRAGMA table_info(user_profiles)"))
        return {row[1] for row in r}
    if 'postgresql' in drv or 'postgres' in drv:
        r = conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'user_profiles'"
        ))
        return {row[0] for row in r}
    return set()

def _ensure_profile_seo_columns():
    """Add SEO columns to user_profiles if missing (one-time migration for existing DBs)."""
    try:
        from sqlalchemy import text
        with db.engine.begin() as conn:
            cols = _get_user_profiles_columns(conn)
            drv = db.engine.url.drivername
            is_pg = 'postgresql' in drv or 'postgres' in drv
            # (column_name, sql_sqlite, sql_postgres)
            additions = [
                ('seo_title_manual', 'ALTER TABLE user_profiles ADD COLUMN seo_title_manual BOOLEAN DEFAULT 0',
                 'ALTER TABLE user_profiles ADD COLUMN seo_title_manual BOOLEAN DEFAULT false'),
                ('meta_desc_manual', 'ALTER TABLE user_profiles ADD COLUMN meta_desc_manual BOOLEAN DEFAULT 0',
                 'ALTER TABLE user_profiles ADD COLUMN meta_desc_manual BOOLEAN DEFAULT false'),
                ('manual_seo_title', 'ALTER TABLE user_profiles ADD COLUMN manual_seo_title VARCHAR(255)',
                 'ALTER TABLE user_profiles ADD COLUMN manual_seo_title VARCHAR(255)'),
                ('manual_meta_desc', 'ALTER TABLE user_profiles ADD COLUMN manual_meta_desc TEXT',
                 'ALTER TABLE user_profiles ADD COLUMN manual_meta_desc TEXT'),
            ]
            for col, sql_sqlite, sql_pg in additions:
                if col not in cols:
                    conn.execute(text(sql_pg if is_pg else sql_sqlite))
    except Exception as e:
        logger.warning(f"Profile SEO columns migration check failed (may already be applied): {e}")

def _ensure_profile_category_gid_column():
    """Add manual_category_gid column to user_profiles if missing."""
    try:
        from sqlalchemy import text
        with db.engine.begin() as conn:
            cols = _get_user_profiles_columns(conn)
            if 'manual_category_gid' not in cols:
                conn.execute(text('ALTER TABLE user_profiles ADD COLUMN manual_category_gid VARCHAR(200)'))
    except Exception as e:
        logger.warning(f"Profile category GID column migration check failed (may already be applied): {e}")

def _ensure_profile_type_columns():
    """Add profile_type, csv_custom_prompt, csv_field_settings, csv_use_main_image_per_variant columns if missing."""
    try:
        from sqlalchemy import text
        with db.engine.begin() as conn:
            cols = _get_user_profiles_columns(conn)
            drv = db.engine.url.drivername
            is_pg = 'postgresql' in drv or 'postgres' in drv
            additions = [
                ('profile_type', "ALTER TABLE user_profiles ADD COLUMN profile_type VARCHAR(20) DEFAULT 'ready'",
                 "ALTER TABLE user_profiles ADD COLUMN profile_type VARCHAR(20) DEFAULT 'ready'"),
                ('csv_custom_prompt', 'ALTER TABLE user_profiles ADD COLUMN csv_custom_prompt TEXT',
                 'ALTER TABLE user_profiles ADD COLUMN csv_custom_prompt TEXT'),
                ('csv_field_settings', 'ALTER TABLE user_profiles ADD COLUMN csv_field_settings TEXT',
                 'ALTER TABLE user_profiles ADD COLUMN csv_field_settings TEXT'),
                ('csv_use_main_image_per_variant', 'ALTER TABLE user_profiles ADD COLUMN csv_use_main_image_per_variant BOOLEAN DEFAULT 0',
                 'ALTER TABLE user_profiles ADD COLUMN csv_use_main_image_per_variant BOOLEAN DEFAULT false'),
            ]
            for col, sql_sqlite, sql_pg in additions:
                if col not in cols:
                    conn.execute(text(sql_pg if is_pg else sql_sqlite))
    except Exception as e:
        logger.warning(f"Profile type columns migration check failed (may already be applied): {e}")

def _ensure_profile_collections_enabled():
    """Add collections_enabled column to user_profiles if missing."""
    try:
        from sqlalchemy import text
        with db.engine.begin() as conn:
            cols = _get_user_profiles_columns(conn)
            if 'collections_enabled' not in cols:
                drv = db.engine.url.drivername
                is_pg = 'postgresql' in drv or 'postgres' in drv
                sql = "ALTER TABLE user_profiles ADD COLUMN collections_enabled BOOLEAN DEFAULT true" if is_pg else "ALTER TABLE user_profiles ADD COLUMN collections_enabled BOOLEAN DEFAULT 1"
                conn.execute(text(sql))
    except Exception as e:
        logger.warning(f"Profile collections_enabled column migration check failed (may already be applied): {e}")

def _ensure_profile_all_columns():
    """Add ALL missing columns to user_profiles table for both SQLite and PostgreSQL.
    This is a comprehensive migration that ensures the DB matches the UserProfile model."""
    try:
        from sqlalchemy import text
        drv = db.engine.url.drivername or ""
        is_pg = 'postgresql' in drv or 'postgres' in drv
        is_sqlite = drv == 'sqlite'
        
        with db.engine.begin() as conn:
            cols = _get_user_profiles_columns(conn)
            
            # Define ALL columns from UserProfile model with their SQL types
            # Format: (column_name, sqlite_type, postgres_type)
            all_columns = [
                # Core profile columns
                ('profile_type', "VARCHAR(20) DEFAULT 'ready'", "VARCHAR(20) DEFAULT 'ready'"),
                ('custom_prompt', "TEXT", "TEXT"),
                ('product_vendor', "VARCHAR(100)", "VARCHAR(100)"),
                ('product_type', "VARCHAR(100)", "VARCHAR(100)"),
                ('listing_status', "VARCHAR(20)", "VARCHAR(20)"),
                ('review_before_publish', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('inventory_quantity', "VARCHAR(10)", "VARCHAR(10)"),
                ('inventory_policy', "VARCHAR(20)", "VARCHAR(20)"),
                
                # Manual toggles (all BOOLEAN)
                ('title_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('description_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('category_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('product_type_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('tags_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('collections_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('collections_enabled', "BOOLEAN DEFAULT 1", "BOOLEAN DEFAULT true"),
                ('vendor_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('sku_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('handle_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('color_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('frame_style_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('theme_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('condition_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('decoration_material_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('artwork_frame_material_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('subject_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('room_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('mood_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('palette_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('audience_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('occasion_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('season_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('composition_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('display_suggestion_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('material_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('art_movement_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('art_style_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('artwork_authenticity_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('orientation_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('seo_title_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('meta_desc_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('google_shopping_enabled', "BOOLEAN DEFAULT 1", "BOOLEAN DEFAULT true"),
                ('google_category_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('gender_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('age_group_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('gs_condition_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('custom_product_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('custom_label_0_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('custom_label_1_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('custom_label_2_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('custom_label_3_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                ('custom_label_4_manual', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
                
                # Manual values
                ('manual_title', "TEXT", "TEXT"),
                ('manual_description', "TEXT", "TEXT"),
                ('manual_category', "VARCHAR(100)", "VARCHAR(100)"),
                ('manual_category_gid', "VARCHAR(200)", "VARCHAR(200)"),
                ('manual_tags', "TEXT", "TEXT"),
                ('manual_collections', "TEXT", "TEXT"),
                ('manual_sku', "VARCHAR(100)", "VARCHAR(100)"),
                ('manual_handle', "VARCHAR(100)", "VARCHAR(100)"),
                ('manual_color', "VARCHAR(50)", "VARCHAR(50)"),
                ('manual_frame_style', "VARCHAR(50)", "VARCHAR(50)"),
                ('manual_theme', "VARCHAR(50)", "VARCHAR(50)"),
                ('manual_condition', "VARCHAR(100)", "VARCHAR(100)"),
                ('manual_decoration_material', "VARCHAR(100)", "VARCHAR(100)"),
                ('manual_artwork_frame_material', "VARCHAR(100)", "VARCHAR(100)"),
                ('manual_subject', "VARCHAR(150)", "VARCHAR(150)"),
                ('manual_room', "VARCHAR(150)", "VARCHAR(150)"),
                ('manual_mood', "VARCHAR(150)", "VARCHAR(150)"),
                ('manual_palette', "VARCHAR(150)", "VARCHAR(150)"),
                ('manual_audience', "VARCHAR(150)", "VARCHAR(150)"),
                ('manual_occasion', "VARCHAR(150)", "VARCHAR(150)"),
                ('manual_season', "VARCHAR(150)", "VARCHAR(150)"),
                ('manual_composition', "VARCHAR(255)", "VARCHAR(255)"),
                ('manual_display_suggestion', "TEXT", "TEXT"),
                ('manual_material', "VARCHAR(150)", "VARCHAR(150)"),
                ('manual_art_movement', "VARCHAR(150)", "VARCHAR(150)"),
                ('manual_art_style', "VARCHAR(150)", "VARCHAR(150)"),
                ('manual_artwork_authenticity', "VARCHAR(150)", "VARCHAR(150)"),
                ('manual_orientation', "VARCHAR(100)", "VARCHAR(100)"),
                ('manual_seo_title', "VARCHAR(255)", "VARCHAR(255)"),
                ('manual_meta_desc', "TEXT", "TEXT"),
                ('manual_google_product_category', "VARCHAR(255)", "VARCHAR(255)"),
                ('manual_gender', "VARCHAR(50)", "VARCHAR(50)"),
                ('manual_age_group', "VARCHAR(50)", "VARCHAR(50)"),
                ('manual_gs_condition', "VARCHAR(50)", "VARCHAR(50)"),
                ('manual_custom_product', "VARCHAR(20)", "VARCHAR(20)"),
                ('manual_custom_label_0', "VARCHAR(100)", "VARCHAR(100)"),
                ('manual_custom_label_1', "VARCHAR(100)", "VARCHAR(100)"),
                ('manual_custom_label_2', "VARCHAR(100)", "VARCHAR(100)"),
                ('manual_custom_label_3', "VARCHAR(100)", "VARCHAR(100)"),
                ('manual_custom_label_4', "VARCHAR(100)", "VARCHAR(100)"),
                
                # JSON/data columns
                ('custom_prompt_sections', "TEXT", "TEXT"),
                ('variants_data', "TEXT", "TEXT"),
                ('selected_channels', "TEXT", "TEXT"),
                ('selected_markets', "TEXT", "TEXT"),
                
                # CSV-specific columns
                ('csv_custom_prompt', "TEXT", "TEXT"),
                ('csv_field_settings', "TEXT", "TEXT"),
                ('csv_use_main_image_per_variant', "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT false"),
            ]
            
            for col_name, sqlite_type, pg_type in all_columns:
                if col_name not in cols:
                    try:
                        type_sql = pg_type if is_pg else sqlite_type
                        sql = f"ALTER TABLE user_profiles ADD COLUMN {col_name} {type_sql}"
                        conn.execute(text(sql))
                        logger.warning(f"Added column {col_name} to user_profiles")
                    except Exception as col_err:
                        logger.warning(f"Could not add column {col_name}: {col_err}")
                        
    except Exception as e:
        logger.error(f"Comprehensive profile migration failed: {e}")
        import traceback
        logger.error(traceback.format_exc())


@app.route('/save_profile', methods=['POST'])
@login_required
def save_profile():
    """Save a configuration profile"""
    try:
        from models import UserProfile
        from sqlalchemy.exc import IntegrityError
        import json
        
        # Ensure profile table has all columns (SQLite and PostgreSQL). Runs at startup too; this catches any missed columns.
        _ensure_profile_all_columns()
        
        data = request.get_json(silent=True) or {}
        profile_name = (data.get('profile_name') or '').strip()
        profile_type = (data.get('profile_type') or 'ready').strip()
        
        if profile_type not in ('ready', 'csv', 'raw'):
            profile_type = 'ready'
        
        if not profile_name:
            return jsonify({'error': 'Profile name is required'}), 400

        # Guard against a save that would wipe a real variant/price list with the
        # form's untouched starter row. Requires an explicit acknowledgement.
        if profile_type != 'csv' and not data.get('confirm_variant_reset'):
            incoming_variants = data.get('variants_data', [])
            existing = UserProfile.query.filter_by(
                user_id=current_user.id, profile_name=profile_name, profile_type=profile_type
            ).first()
            existing_variants = _parse_json_list(existing.variants_data) if existing else []
            if (
                len(existing_variants) > 1
                and _looks_like_untouched_variant_defaults(incoming_variants)
            ):
                return jsonify({
                    'error': (
                        'This save would replace the %d sizes and prices saved on "%s" with a single '
                        'default row (A4 (21x30cm) at 29.99). Load the profile first so the variant '
                        'editor is filled in, then save again.'
                    ) % (len(existing_variants), profile_name),
                    'variant_reset_warning': True,
                    'existing_variant_count': len(existing_variants),
                }), 409
        
        def _apply_profile_data(profile, data, pt):
            """Apply request data to a profile (shared by main path and IntegrityError retry)."""
            if pt == 'csv':
                profile.csv_custom_prompt = data.get('csv_custom_prompt', '')
                csv_field_settings = data.get('csv_field_settings', {})
                profile.csv_field_settings = json.dumps(csv_field_settings)
                profile.csv_use_main_image_per_variant = data.get('csv_use_main_image_per_variant', False)
                profile.collections_enabled = data.get('collections_enabled', True)
                profile.custom_prompt = data.get('custom_prompt', '')
                profile.product_vendor = data.get('product_vendor', '')
                profile.manual_sku = data.get('manual_sku', '')
                profile.listing_status = data.get('listing_status', '')
            else:
                profile.custom_prompt = data.get('custom_prompt', '')
                # Save per-section prompt data as JSON for lossless roundtrip
                sections = data.get('custom_prompt_sections')
                if sections and isinstance(sections, list):
                    profile.custom_prompt_sections = json.dumps(sections)
                else:
                    profile.custom_prompt_sections = None
                profile.product_vendor = data.get('product_vendor', '')
                profile.product_type = data.get('product_type', '')
                profile.listing_status = data.get('listing_status', 'active')
                profile.review_before_publish = data.get('review_before_publish', False)
                profile.inventory_quantity = data.get('inventory_quantity', '999')
                profile.inventory_policy = data.get('inventory_policy', 'continue')
                profile.title_manual = data.get('title_manual', False)
                profile.description_manual = data.get('description_manual', False)
                profile.category_manual = data.get('category_manual', False)
                profile.product_type_manual = data.get('product_type_manual', False)
                profile.tags_manual = data.get('tags_manual', False)
                profile.collections_manual = data.get('collections_manual', False)
                profile.collections_enabled = data.get('collections_enabled', True)
                profile.vendor_manual = data.get('vendor_manual', False)
                profile.sku_manual = data.get('sku_manual', False)
                profile.handle_manual = data.get('handle_manual', False)
                profile.color_manual = data.get('color_manual', False)
                profile.frame_style_manual = data.get('frame_style_manual', data.get('metafield1_manual', False))
                profile.theme_manual = data.get('theme_manual', data.get('metafield2_manual', False))
                profile.condition_manual = data.get('condition_manual', False)
                profile.decoration_material_manual = data.get('decoration_material_manual', False)
                profile.artwork_frame_material_manual = data.get('artwork_frame_material_manual', False)
                profile.seo_title_manual = data.get('seo_title_manual', False)
                profile.meta_desc_manual = data.get('meta_desc_manual', False)
                profile.google_shopping_enabled = data.get('google_shopping_enabled', True)
                profile.google_category_manual = data.get('google_category_manual', False)
                profile.gender_manual = data.get('gender_manual', False)
                profile.age_group_manual = data.get('age_group_manual', False)
                profile.gs_condition_manual = data.get('gs_condition_manual', False)
                profile.custom_product_manual = data.get('custom_product_manual', False)
                profile.custom_label_0_manual = data.get('custom_label_0_manual', False)
                profile.custom_label_1_manual = data.get('custom_label_1_manual', False)
                profile.custom_label_2_manual = data.get('custom_label_2_manual', False)
                profile.custom_label_3_manual = data.get('custom_label_3_manual', False)
                profile.custom_label_4_manual = data.get('custom_label_4_manual', False)
                for key in SEO_METAFIELD_KEYS:
                    setattr(profile, f'{key}_manual', data.get(f'{key}_manual', False))
                profile.manual_title = data.get('manual_title', '')
                profile.manual_description = data.get('manual_description', '')
                profile.manual_category = data.get('manual_category', '')
                profile.manual_category_gid = data.get('manual_category_gid', '')
                profile.manual_tags = data.get('manual_tags', '')
                profile.manual_collections = data.get('manual_collections', '')
                profile.manual_sku = data.get('manual_sku', '')
                profile.manual_handle = data.get('manual_handle', '')
                profile.manual_color = data.get('manual_color', '')
                profile.manual_frame_style = data.get('manual_frame_style', data.get('manual_metafield1', ''))
                profile.manual_theme = data.get('manual_theme', data.get('manual_metafield2', ''))
                profile.manual_condition = data.get('manual_condition', '')
                profile.manual_decoration_material = data.get('manual_decoration_material', '')
                profile.manual_artwork_frame_material = data.get('manual_artwork_frame_material', '')
                profile.manual_seo_title = data.get('manual_seo_title', '')
                profile.manual_meta_desc = data.get('manual_meta_desc', '')
                profile.manual_google_product_category = data.get('manual_google_product_category', '')
                profile.manual_gender = data.get('manual_gender', '')
                profile.manual_age_group = data.get('manual_age_group', '')
                profile.manual_gs_condition = data.get('manual_gs_condition', '')
                profile.manual_custom_product = data.get('manual_custom_product', '')
                profile.manual_custom_label_0 = data.get('manual_custom_label_0', '')
                profile.manual_custom_label_1 = data.get('manual_custom_label_1', '')
                profile.manual_custom_label_2 = data.get('manual_custom_label_2', '')
                profile.manual_custom_label_3 = data.get('manual_custom_label_3', '')
                profile.manual_custom_label_4 = data.get('manual_custom_label_4', '')
                for key in SEO_METAFIELD_KEYS:
                    setattr(profile, f'manual_{key}', data.get(f'manual_{key}', ''))
                variants_data = data.get('variants_data', [])
                logger.info("save_profile variants_data count: %s", len(variants_data))
                profile.variants_data = json.dumps(variants_data)
                selected_channels = data.get('selected_channels', [])
                selected_markets = data.get('selected_markets', [])
                profile.selected_channels = json.dumps(selected_channels)
                profile.selected_markets = json.dumps(selected_markets)

        # Check if profile name already exists for this user AND profile_type
        existing_profile = UserProfile.query.filter_by(user_id=current_user.id, profile_name=profile_name, profile_type=profile_type).first()
        
        if existing_profile:
            # Update existing profile
            profile = existing_profile
        else:
            # Create new profile
            profile = UserProfile()
            profile.user_id = current_user.id
            profile.profile_name = profile_name
            profile.profile_type = profile_type
        
        _apply_profile_data(profile, data, profile_type)
        
        if not existing_profile:
            db.session.add(profile)
        db.session.commit()
        db.session.refresh(profile)
        saved_variants = []
        if profile_type != 'csv':
            requested_variants = data.get('variants_data', [])
            saved_variants = _parse_json_list(profile.variants_data)
            if saved_variants != requested_variants:
                raise RuntimeError('Variant profile verification failed; saved data did not match the submitted variants')
        
        logger.warning(f"🎯 SAVE_PROFILE: Successfully saved {profile_type} profile '{profile_name}' for user {current_user.id}")
        return jsonify({'success': True, 'message': f'Profile "{profile_name}" saved successfully', 'variants_data': saved_variants})
        
    except IntegrityError:
        db.session.rollback()
        existing_profile = UserProfile.query.filter_by(user_id=current_user.id, profile_name=profile_name, profile_type=profile_type).first()
        if not existing_profile:
            raise
        profile = existing_profile
        _apply_profile_data(profile, data, profile_type)
        db.session.commit()
        db.session.refresh(profile)
        saved_variants = []
        if profile_type != 'csv':
            requested_variants = data.get('variants_data', [])
            saved_variants = _parse_json_list(profile.variants_data)
            if saved_variants != requested_variants:
                raise RuntimeError('Variant profile verification failed; saved data did not match the submitted variants')
        logger.warning(f"🎯 SAVE_PROFILE: Successfully saved {profile_type} profile '{profile_name}' for user {current_user.id} (retry after IntegrityError)")
        return jsonify({'success': True, 'message': f'Profile "{profile_name}" saved successfully', 'variants_data': saved_variants})
    except Exception as e:
        import traceback
        logger.error(f"Error saving profile: {e}")
        logger.error(traceback.format_exc())
        db.session.rollback()
        # Return a short message for client; avoid exposing internal details
        msg = str(e)
        if 'no such column' in msg.lower() or 'operationalerror' in type(e).__name__.lower():
            msg = "Database schema is out of date. Please run: python migrate_profile_seo_columns.py"
        return jsonify({'error': msg}), 500

def _parse_json_list(value):
    """Safely parse a JSON list from DB (return [] for None, empty, or invalid)."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            out = json.loads(s)
            return out if isinstance(out, list) else []
        except (TypeError, ValueError):
            return []
    return []


@app.route('/load_profile/<profile_name>')
@login_required
def load_profile(profile_name):
    """Load a configuration profile"""
    try:
        from models import UserProfile
        import json
        
        profile_type = request.args.get('profile_type', 'ready')
        if profile_type not in ('ready', 'csv', 'raw'):
            profile_type = 'ready'
        
        # Try to find profile with profile_type filter first
        profile = UserProfile.query.filter_by(user_id=current_user.id, profile_name=profile_name, profile_type=profile_type).first()
        
        # Fallback: try without profile_type for legacy profiles (before migration).
        # Raw artwork profiles are new and should not accidentally load a Ready Images profile.
        if not profile and profile_type != 'raw':
            profile = UserProfile.query.filter_by(user_id=current_user.id, profile_name=profile_name).first()
        
        if not profile:
            return jsonify({'error': 'Profile not found'}), 404
        
        actual_type = getattr(profile, 'profile_type', 'ready') or 'ready'
        
        if actual_type == 'csv':
            # Return CSV-specific profile data
            csv_field_settings = {}
            try:
                raw = getattr(profile, 'csv_field_settings', None) or ''
                if raw:
                    csv_field_settings = json.loads(raw)
            except (TypeError, ValueError):
                csv_field_settings = {}
            
            profile_data = {
                'profile_type': 'csv',
                'csv_custom_prompt': getattr(profile, 'csv_custom_prompt', None) or '',
                'csv_field_settings': csv_field_settings,
                'csv_use_main_image_per_variant': getattr(profile, 'csv_use_main_image_per_variant', False) or False,
                'custom_prompt': profile.custom_prompt or '',
                'product_vendor': profile.product_vendor or '',
                'manual_sku': profile.manual_sku or '',
                'listing_status': profile.listing_status or '',
            }
        else:
            # Parse saved per-section prompt data if available
            _prompt_sections = None
            try:
                _raw_sections = getattr(profile, 'custom_prompt_sections', None)
                if _raw_sections:
                    _prompt_sections = json.loads(_raw_sections)
            except (TypeError, ValueError):
                _prompt_sections = None

            profile_data = {
                'profile_type': actual_type if actual_type in ('ready', 'raw') else 'ready',
                'custom_prompt': profile.custom_prompt or '',
                'custom_prompt_sections': _prompt_sections,
                'product_vendor': profile.product_vendor or '',
                'product_type': profile.product_type or '',
                'listing_status': profile.listing_status or 'active',
                'review_before_publish': profile.review_before_publish or False,
                'inventory_quantity': profile.inventory_quantity or '999',
                'inventory_policy': profile.inventory_policy or 'continue',
                
                # Manual toggles
                'title_manual': profile.title_manual or False,
                'description_manual': profile.description_manual or False,
                'category_manual': profile.category_manual or False,
                'product_type_manual': profile.product_type_manual or False,
                'tags_manual': profile.tags_manual or False,
                'collections_manual': profile.collections_manual or False,
                'collections_enabled': getattr(profile, 'collections_enabled', True),
                'vendor_manual': getattr(profile, 'vendor_manual', False) or False,
                'sku_manual': profile.sku_manual or False,
                'handle_manual': profile.handle_manual or False,
                'color_manual': profile.color_manual or False,
                'frame_style_manual': profile.frame_style_manual or False,
                'theme_manual': profile.theme_manual or False,
                'condition_manual': getattr(profile, 'condition_manual', False) or False,
                'decoration_material_manual': getattr(profile, 'decoration_material_manual', False) or False,
                'artwork_frame_material_manual': getattr(profile, 'artwork_frame_material_manual', False) or False,
                'seo_title_manual': getattr(profile, 'seo_title_manual', False) or False,
                'meta_desc_manual': getattr(profile, 'meta_desc_manual', False) or False,
                'google_shopping_enabled': getattr(profile, 'google_shopping_enabled', True),
                'google_category_manual': getattr(profile, 'google_category_manual', False) or False,
                'gender_manual': getattr(profile, 'gender_manual', False) or False,
                'age_group_manual': getattr(profile, 'age_group_manual', False) or False,
                'gs_condition_manual': getattr(profile, 'gs_condition_manual', False) or False,
                'custom_product_manual': getattr(profile, 'custom_product_manual', False) or False,
                'custom_label_0_manual': getattr(profile, 'custom_label_0_manual', False) or False,
                'custom_label_1_manual': getattr(profile, 'custom_label_1_manual', False) or False,
                'custom_label_2_manual': getattr(profile, 'custom_label_2_manual', False) or False,
                'custom_label_3_manual': getattr(profile, 'custom_label_3_manual', False) or False,
                'custom_label_4_manual': getattr(profile, 'custom_label_4_manual', False) or False,
                **{f'{key}_manual': getattr(profile, f'{key}_manual', False) or False for key in SEO_METAFIELD_KEYS},
                
                # Manual values
                'manual_title': profile.manual_title or '',
                'manual_description': profile.manual_description or '',
                'manual_category': profile.manual_category or '',
                'manual_category_gid': getattr(profile, 'manual_category_gid', None) or '',
                'manual_tags': profile.manual_tags or '',
                'manual_collections': profile.manual_collections or '',
                'manual_sku': profile.manual_sku or '',
                'manual_handle': profile.manual_handle or '',
                'manual_color': profile.manual_color or '',
                'manual_frame_style': profile.manual_frame_style or '',
                'manual_theme': profile.manual_theme or '',
                'manual_condition': getattr(profile, 'manual_condition', None) or '',
                'manual_decoration_material': getattr(profile, 'manual_decoration_material', None) or '',
                'manual_artwork_frame_material': getattr(profile, 'manual_artwork_frame_material', None) or '',
                'manual_seo_title': getattr(profile, 'manual_seo_title', None) or '',
                'manual_meta_desc': getattr(profile, 'manual_meta_desc', None) or '',
                'manual_google_product_category': getattr(profile, 'manual_google_product_category', None) or '',
                'manual_gender': getattr(profile, 'manual_gender', None) or '',
                'manual_age_group': getattr(profile, 'manual_age_group', None) or '',
                'manual_gs_condition': getattr(profile, 'manual_gs_condition', None) or '',
                'manual_custom_product': getattr(profile, 'manual_custom_product', None) or '',
                'manual_custom_label_0': getattr(profile, 'manual_custom_label_0', None) or '',
                'manual_custom_label_1': getattr(profile, 'manual_custom_label_1', None) or '',
                'manual_custom_label_2': getattr(profile, 'manual_custom_label_2', None) or '',
                'manual_custom_label_3': getattr(profile, 'manual_custom_label_3', None) or '',
                'manual_custom_label_4': getattr(profile, 'manual_custom_label_4', None) or '',
                **{f'manual_{key}': getattr(profile, f'manual_{key}', None) or '' for key in SEO_METAFIELD_KEYS},
                # Aliases for frontend (metafield1/2 UI)
                'metafield1_manual': profile.frame_style_manual or False,
                'metafield2_manual': profile.theme_manual or False,
                'manual_metafield1': profile.manual_frame_style or '',
                'manual_metafield2': profile.manual_theme or '',
                
                # Variants and publishing (parse JSON safely)
                'variants_data': _parse_json_list(profile.variants_data),
                'selected_channels': _parse_json_list(profile.selected_channels),
                'selected_markets': _parse_json_list(profile.selected_markets)
            }
            logger.info("load_profile variants_data count: %s", len(profile_data.get('variants_data', [])))
        
        return jsonify({'success': True, 'data': profile_data})
        
    except Exception as e:
        logger.error(f"Error loading profile: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/delete_profile/<profile_name>', methods=['DELETE'])
@login_required
def delete_profile(profile_name):
    """Delete a configuration profile"""
    try:
        from models import UserProfile
        
        profile_name = (profile_name or '').strip()
        profile_type = request.args.get('profile_type', 'ready')
        if profile_type not in ('ready', 'csv', 'raw'):
            profile_type = 'ready'
        
        profile = UserProfile.query.filter_by(user_id=current_user.id, profile_name=profile_name, profile_type=profile_type).first()
        
        # Fallback for legacy profiles without profile_type. Keep raw profiles isolated.
        if not profile and profile_type != 'raw':
            profile = UserProfile.query.filter_by(user_id=current_user.id, profile_name=profile_name).first()
        
        if not profile:
            return jsonify({'error': 'Profile not found'}), 404
        
        db.session.delete(profile)
        db.session.commit()
        
        return jsonify({'success': True, 'message': f'Profile "{profile_name}" deleted successfully'})
        
    except Exception as e:
        logger.error(f"Error deleting profile: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/get_profiles')
@login_required
def get_profiles():
    """Get all configuration profiles for the current user, filtered by profile_type"""
    try:
        from models import UserProfile
        
        if "sqlite" in (db.engine.url.drivername or ""):
            _ensure_profile_type_columns()
        
        profile_type = request.args.get('profile_type', 'ready')
        if profile_type not in ('ready', 'csv', 'raw'):
            profile_type = 'ready'
        
        logger.warning(f"🎯 GET_PROFILES: User ID: {current_user.id}, type: {profile_type}")
        
        # Try to filter by profile_type; also include legacy profiles (NULL profile_type) for 'ready'
        try:
            profiles = UserProfile.query.filter_by(user_id=current_user.id, profile_type=profile_type).order_by(UserProfile.profile_name).all()
            # For ready type, also include legacy profiles that have NULL/empty profile_type
            if profile_type == 'ready':
                from sqlalchemy import or_
                profiles = UserProfile.query.filter(
                    UserProfile.user_id == current_user.id,
                    or_(UserProfile.profile_type == 'ready', UserProfile.profile_type == None, UserProfile.profile_type == '')
                ).order_by(UserProfile.profile_name).all()
        except Exception:
            # Fallback if profile_type column doesn't exist yet
            profiles = UserProfile.query.filter_by(user_id=current_user.id).order_by(UserProfile.profile_name).all()
        
        logger.warning(f"🎯 GET_PROFILES: Found {len(profiles)} profiles")
        
        profile_list = [{'name': p.profile_name, 'created_at': p.created_at.isoformat()} for p in profiles]
        logger.warning(f"🎯 GET_PROFILES: Profile list: {profile_list}")
        
        return jsonify({'success': True, 'profiles': profile_list})
        
    except Exception as e:
        logger.error(f"Error getting profiles: {e}")
        return jsonify({'error': str(e)}), 500

@app.errorhandler(404)
def not_found(error):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal server error: {error}")
    return render_template('500.html'), 500

# Photopea Integration Endpoints
@app.route('/photopea_callback', methods=['POST', 'OPTIONS'])
def photopea_callback():
    """Handle processed files from Photopea API with CORS support"""
    
    # Handle preflight CORS request
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response
    
    try:
        from photopea_utils import validate_photopea_response
        import uuid
    except ImportError as e:
        logger.error(f"Import error: {e}")
        return jsonify({"message": "Photopea integration not available"}), 500
    
    try:
        # Get the raw binary data
        raw_data = request.get_data()
        
        logger.info(f"Received Photopea callback with {len(raw_data)} bytes")
        
        # Generate a unique filename for this processed mockup
        unique_id = str(uuid.uuid4())[:8]
        expected_filename = f"photopea_mockup_{unique_id}.png"
        
        # Validate the response
        validation_result = validate_photopea_response(raw_data, expected_filename)
        
        if validation_result['success']:
            # Save the processed image
            output_path = os.path.join(PROCESSED_FOLDER, expected_filename)
            
            with open(output_path, 'wb') as f:
                f.write(validation_result['image_data'])
            
            logger.info(f"Saved Photopea mockup: {output_path}")
            
            # Store the result in our processing queue for retrieval
            with queue_lock:
                # Find the task that's waiting for this result
                for task_id, task_data in processing_queue.items():
                    if task_data.get('waiting_for_photopea', False):
                        task_data['photopea_result'] = output_path
                        task_data['waiting_for_photopea'] = False
                        task_data['current_step'] = 'PSD mockup completed'
                        task_data['frame_paths'] = [output_path]  # Use the mockup as frame
                        break
            
            # Return success response to Photopea with CORS headers
            response_data = {
                "message": "Mockup processed successfully",
                "script": 'app.echoToOE("Mockup saved successfully! You can now close this window.");'
            }
            
            response = make_response(jsonify(response_data))
            response.headers['Access-Control-Allow-Origin'] = '*'
            return response
        else:
            logger.error(f"Failed to validate Photopea response: {validation_result.get('error')}")
            response = make_response(jsonify({"message": "Error processing mockup"}), 400)
            response.headers['Access-Control-Allow-Origin'] = '*'
            return response
            
    except Exception as e:
        logger.error(f"Error in Photopea callback: {e}")
        response = make_response(jsonify({"message": "Internal error processing mockup"}), 500)
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response

@app.route('/create_photopea_mockup', methods=['POST'])
@login_required
def create_photopea_mockup():
    """Create a PSD mockup using Photopea API"""
    try:
        from photopea_utils import create_psd_mockup, get_available_psd_frames
    except ImportError as e:
        logger.error(f"Import error: {e}")
        return jsonify({'success': False, 'error': 'Photopea integration not available'})
    
    try:
        data = request.get_json()
        image_path = data.get('image_path')
        frame_name = data.get('frame_name')
        task_id = data.get('task_id')
        
        if not image_path or not os.path.exists(image_path):
            return jsonify({'success': False, 'error': 'Image file not found'})
        
        # Get available frames
        available_frames = get_available_psd_frames()
        
        if not available_frames:
            return jsonify({'success': False, 'error': 'No PSD frames available'})
        
        # Find the requested frame or use the first one
        selected_frame = None
        if frame_name:
            selected_frame = next((f for f in available_frames if f['name'] == frame_name), None)
        
        if not selected_frame:
            selected_frame = available_frames[0]  # Use first available frame
            logger.info(f"Using default frame: {selected_frame['name']}")
        
        # Create the mockup configuration
        mockup_result = create_psd_mockup(
            image_path=image_path,
            psd_template_path=selected_frame['path'],
            output_dir=PROCESSED_FOLDER
        )
        
        if mockup_result:
            # Mark the task as waiting for Photopea result
            if task_id:
                with queue_lock:
                    if task_id in processing_queue:
                        processing_queue[task_id]['waiting_for_photopea'] = True
                        processing_queue[task_id]['current_step'] = 'Creating PSD mockup...'
                        processing_queue[task_id]['status'] = 'creating_mockup'
            
            # Return the Photopea URL for the client to open
            return jsonify({
                'success': True,
                'photopea_url': mockup_result['photopea_url'],
                'frame_name': selected_frame['name'],
                'message': f'Photopea editor will open with {selected_frame["name"]} frame. Click File > Save when ready.'
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to create mockup configuration'})
            
    except Exception as e:
        logger.error(f"Error creating Photopea mockup: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/get_psd_frames')
@login_required
def get_psd_frames():
    """Get list of available PSD frame templates"""
    try:
        from photopea_utils import get_available_psd_frames
    except ImportError as e:
        logger.error(f"Import error: {e}")
        return jsonify({'success': False, 'error': 'Photopea integration not available'})
    
    try:
        frames = get_available_psd_frames()
        return jsonify({
            'success': True,
            'frames': frames,
            'count': len(frames)
        })
    except Exception as e:
        logger.error(f"Error getting PSD frames: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/upload_psd_template', methods=['POST'])
def upload_psd_template():
    """Upload PSD mockup templates with smart layers (login not required for Photopea processing)"""
    try:
        # Log the content length for debugging
        content_length = request.content_length
        max_size = 300 * 1024 * 1024  # 300MB
        
        logger.info(f"Upload request content length: {content_length}")
        
        # Early check for file size before any processing
        if content_length and content_length > max_size:
            logger.warning(f"Request too large: {content_length / 1024 / 1024:.1f}MB")
            return jsonify({
                'error': f'File too large ({content_length / 1024 / 1024:.1f}MB). Maximum size is 300MB.'
            }), 413
        
        # Safe way to check for files without triggering full form parsing
        try:
            # Check if we have any files without fully parsing the form
            has_files = bool(request.files)
            logger.info(f"Files detected in request: {has_files}")
        except Exception as form_error:
            logger.error(f"Error checking for files: {form_error}")
            return jsonify({'error': 'Failed to process upload request due to size or format issues'}), 400
        
        # Use temporary storage for PSD templates
        uploaded_templates = []
        
        try:
            # Get files with memory-efficient approach
            files = []
            
            # Try multiple field names safely
            for field_name in ['file', 'files']:
                try:
                    field_files = request.files.getlist(field_name)
                    if field_files:
                        files = field_files
                        logger.info(f"Found {len(files)} files using field '{field_name}'")
                        break
                except Exception as field_error:
                    logger.warning(f"Error accessing field '{field_name}': {field_error}")
                    continue
            
            # If no files found with standard names, try all available fields
            if not files:
                try:
                    for field_name in request.files.keys():
                        field_files = request.files.getlist(field_name)
                        if field_files:
                            files = field_files
                            logger.info(f"Found {len(files)} files using field '{field_name}'")
                            break
                except Exception as keys_error:
                    logger.error(f"Error iterating through file fields: {keys_error}")
            
            # Debug: Log all file information
            for i, file in enumerate(files):
                if file:
                    logger.info(f"File {i}: name='{file.filename}', size=unknown")
                else:
                    logger.info(f"File {i}: None/empty file")
            
            for file in files:
                if not file or not file.filename:
                    logger.info("Skipping empty file")
                    continue
                    
                filename = secure_filename(file.filename)
                logger.info(f"Processing file: {filename}")
                
                # Validate PSD file
                if not filename.lower().endswith('.psd'):
                    logger.warning(f"Skipping non-PSD file: {filename}")
                    continue
                
                logger.info(f"Validated PSD file: {filename}")
                
                # Save to temporary location (PSD templates kept until processing completes)
                temp_filepath = temp_file_service.create_temp_file(
                    prefix='psd_template',
                    suffix='.psd',
                    task_id=None  # Not tied to specific task, cleaned up by scheduled cleanup
                )
                
                # Save file with streaming to handle large files
                try:
                    logger.info(f"Saving PSD template to temporary location: {temp_filepath}")
                    
                    # Use much larger chunks for faster upload (1MB chunks)
                    chunk_size = 1024 * 1024  # 1MB chunks for optimal speed
                    with open(temp_filepath, 'wb') as f:
                        file.seek(0)  # Reset file pointer
                        while True:
                            chunk = file.read(chunk_size)
                            if not chunk:
                                break
                            f.write(chunk)
                    
                    file_size = os.path.getsize(temp_filepath)
                    logger.info(f"Successfully uploaded PSD template to temp: {filename} ({file_size / 1024 / 1024:.1f}MB)")
                    
                    # Verify file was saved correctly
                    if os.path.exists(temp_filepath) and file_size > 0:
                        logger.info(f"File verification passed: {temp_filepath}")
                    else:
                        logger.error(f"File verification failed: {temp_filepath}")
                        # Cleanup on verification failure
                        try:
                            if os.path.exists(temp_filepath):
                                os.unlink(temp_filepath)
                        except:
                            pass
                        continue
                    
                    filename_base, ext = os.path.splitext(filename)
                    uploaded_templates.append({
                        'filename': filename,
                        'unique_filename': os.path.basename(temp_filepath),
                        'filepath': temp_filepath,
                        'name': filename_base,
                        'size': f"{file_size / 1024 / 1024:.1f}MB"
                    })
                except Exception as save_error:
                    logger.error(f"Error saving file {filename}: {save_error}")
                    # Clean up partial file if it exists
                    if os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                            logger.info(f"Cleaned up partial file: {filepath}")
                        except Exception as cleanup_error:
                            logger.error(f"Error cleaning up partial file: {cleanup_error}")
                    continue
            
        except Exception as file_processing_error:
            logger.error(f"Error processing uploaded files: {file_processing_error}")
            # Clean up any partially uploaded files
            try:
                for template in uploaded_templates:
                    filepath = template.get('filepath')
                    if filepath and os.path.exists(filepath):
                        os.remove(filepath)
                        logger.info(f"Cleaned up partial upload: {filepath}")
            except Exception as cleanup_error:
                logger.error(f"Error during cleanup: {cleanup_error}")
            
            return jsonify({
                'error': 'Failed to process uploaded files. The file may be too large or corrupted.',
                'details': str(file_processing_error),
                'suggestion': 'Try compressing your PSD file or uploading a smaller file.'
            }), 400
        
        if not uploaded_templates:
            logger.warning("No valid PSD files were uploaded")
            return jsonify({'error': 'No valid PSD files uploaded'}), 400
        
        logger.info(f"Successfully processed {len(uploaded_templates)} PSD templates")
        
        # Force refresh of frames list by calling the frame loading function
        try:
            from photopea_utils import get_available_psd_templates
            all_templates = get_available_psd_templates()
            logger.info(f"Total available templates after upload: {len(all_templates)}")
        except Exception as frame_error:
            logger.error(f"Error refreshing frame list: {frame_error}")
        
        return jsonify({
            'success': True,
            'message': f'Uploaded {len(uploaded_templates)} PSD template(s)',
            'templates': uploaded_templates,
            'total_frames': len(uploaded_templates)
        })
        
    except Exception as e:
        logger.error(f"Critical error in upload_psd_template: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({
            'error': 'Upload failed due to server error.',
            'details': str(e)
        }), 500

@app.route('/get_uploaded_templates')
@login_required
def get_uploaded_templates():
    """Get list of uploaded PSD templates"""
    try:
        from photopea_utils import get_available_psd_templates
        templates = get_available_psd_templates()
        return jsonify({
            'success': True,
            'templates': templates
        })
    except Exception as e:
        logger.error(f"Error getting uploaded templates: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/remove_psd_template', methods=['POST'])
@login_required
def remove_psd_template():
    """Remove a PSD template"""
    try:
        data = request.get_json()
        filename = data.get('filename')
        
        if not filename:
            return jsonify({'error': 'Filename is required'}), 400
        
        # Remove from uploads directory
        filepath = os.path.join('uploads', filename)
        if os.path.exists(filepath):
            os.remove(filepath)
            logger.info(f"Removed PSD template: {filename}")
            return jsonify({'success': True, 'message': 'Template removed successfully'})
        else:
            return jsonify({'error': 'Template file not found'}), 404
            
    except Exception as e:
        logger.error(f"Error removing PSD template: {e}")
        return jsonify({'error': str(e)}), 500

# Removed duplicate get_active_frames function - using api_get_active_frames instead

@app.route('/reorder_frames', methods=['POST'])
@login_required
def reorder_frames():
    """Reorder the processing sequence of frames by creating ordered prefix files"""
    try:
        data = request.get_json()
        from_index = data.get('from_index')
        to_index = data.get('to_index')
        
        if from_index is None or to_index is None:
            return jsonify({'error': 'Both from_index and to_index are required'}), 400
        
        # Get current frames list exactly as displayed
        frames_data = get_frames_list()
        
        if from_index >= len(frames_data) or to_index >= len(frames_data) or from_index == to_index:
            return jsonify({'error': 'Invalid frame index'}), 400
        
        # Only allow reordering uploaded frames, not preset frames
        frame_to_move = frames_data[from_index]
        if frame_to_move['type'] not in ['Uploaded PSD', 'Uploaded Template']:
            return jsonify({'error': 'Can only reorder uploaded PSD files'}), 400
        
        # Reorder the frames list
        frames_data.insert(to_index, frames_data.pop(from_index))
        
        # Rename files to reflect the new order using timestamp ordering
        import time
        base_time = time.time()
        
        for i, frame in enumerate(frames_data):
            if frame['type'] in ['Uploaded PSD', 'Uploaded Template']:
                old_path = frame['path']
                filename = frame['filename']
                
                # Create new timestamp for ordering
                new_timestamp = base_time + (i * 10)  # 10 second intervals
                os.utime(old_path, (new_timestamp, new_timestamp))
        
        logger.info(f"Frame reorder completed: {from_index} -> {to_index}")
        return jsonify({'success': True, 'message': 'Frame order updated'})
        
    except Exception as e:
        logger.error(f"Error reordering frames: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/remove_active_frame', methods=['POST'])
def remove_active_frame():
    """Remove an uploaded frame/template (delete file)"""
    try:
        data = request.get_json()
        frame_path = data.get('frame_path')
        
        if not frame_path:
            return jsonify({'error': 'Frame path is required'}), 400
        
        # Security: prevent path traversal
        if '..' in frame_path:
            return jsonify({'error': 'Invalid path'}), 400
        
        # Normalize path separators for cross-platform compatibility
        normalized_path = frame_path.replace('\\', '/')
        
        # Get absolute paths for allowed directories
        allowed_dirs = {
            'uploads': os.path.abspath(UPLOAD_FOLDER).replace('\\', '/'),
            'psd_frames': os.path.abspath(PSD_FRAMES_FOLDER).replace('\\', '/')
        }
        
        # Get absolute path of the file to delete
        if os.path.isabs(frame_path):
            abs_file_path = os.path.abspath(frame_path).replace('\\', '/')
        else:
            abs_file_path = os.path.abspath(frame_path).replace('\\', '/')
        
        # Check if file is within any allowed directory
        is_allowed = False
        matched_dir = None
        
        for dir_name, dir_path in allowed_dirs.items():
            # Check if the file path starts with the directory path
            if abs_file_path.startswith(dir_path):
                # Verify it's actually a file (not trying to delete the directory itself)
                rel_path = abs_file_path[len(dir_path):].lstrip('/')
                if rel_path and not rel_path.startswith('..'):
                    is_allowed = True
                    matched_dir = dir_name
                    break
        
        # Also check relative paths (for backward compatibility)
        if not is_allowed:
            allowed_prefixes = ['uploads/', 'psd_frames/']
            is_allowed = any(normalized_path.startswith(prefix) for prefix in allowed_prefixes)
        
        # Log for debugging
        logger.info(f"Delete request - original: {frame_path}, normalized: {normalized_path}, absolute: {abs_file_path}, allowed: {is_allowed}")
        
        if is_allowed:
            # Use the original frame_path for file operations (handles both relative and absolute)
            if os.path.exists(frame_path):
                os.remove(frame_path)
                logger.info(f"Deleted frame/template: {frame_path} (matched dir: {matched_dir})")
                return jsonify({'success': True, 'message': 'Frame deleted successfully'})
            else:
                return jsonify({'error': 'Frame file not found'}), 404
        else:
            return jsonify({'error': f'Cannot delete files outside allowed directories. Path: {normalized_path}'}), 400
        
    except Exception as e:
        logger.error(f"Error deleting frame: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/disable_preset_frame', methods=['POST'])
def disable_preset_frame():
    """Disable a preset frame from processing"""
    try:
        data = request.get_json()
        frame_path = data.get('frame_path')
        
        if not frame_path:
            return jsonify({'error': 'Frame path is required'}), 400
        
        # For preset frames, move to a disabled folder
        if frame_path.startswith('psd_frames/'):
            disabled_dir = 'psd_frames_disabled'
            os.makedirs(disabled_dir, exist_ok=True)
            
            filename = os.path.basename(frame_path)
            disabled_path = os.path.join(disabled_dir, filename)
            
            if os.path.exists(frame_path):
                # Move to disabled folder
                os.rename(frame_path, disabled_path)
                logger.info(f"Disabled preset frame: {frame_path} -> {disabled_path}")
                return jsonify({'success': True, 'message': 'Preset frame disabled successfully'})
            else:
                return jsonify({'error': 'Frame file not found'}), 404
        else:
            return jsonify({'error': 'This operation is only for preset frames'}), 400
        
    except Exception as e:
        logger.error(f"Error disabling preset frame: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/start_mockup_workflow', methods=['POST'])
@login_required 
def start_mockup_workflow():
    """Start the PSD mockup workflow with uploaded template and image using Dynamic Mockups API"""
    
    try:
        data = request.get_json()
        task_id = data.get('task_id')
        template_filename = data.get('template_filename')  # Selected uploaded PSD template
        
        if not task_id:
            return jsonify({'error': 'Task ID is required'}), 400
        
        if not template_filename:
            return jsonify({'error': 'PSD template selection is required'}), 400
        
        # FIX: Try to get task from memory first, if not found, reconstruct from uploaded files
        with queue_lock:
            task = processing_queue.get(task_id)
        
        if not task:
            # Task not in memory - reconstruct from uploaded files
            logger.info(f"Task {task_id} not found in memory, reconstructing from uploaded files")
            
            # Find the uploaded image file that matches this task_id pattern
            uploads_dir = "uploads"
            uploaded_files = []
            if os.path.exists(uploads_dir):
                for filename in os.listdir(uploads_dir):
                    if filename.endswith('.jpg') or filename.endswith('.png'):
                        # Check if this could be our uploaded file
                        file_path = os.path.join(uploads_dir, filename)
                        uploaded_files.append(file_path)
            
            if not uploaded_files:
                return jsonify({'error': 'No uploaded image files found. Please upload an image first.'}), 404
                
            # Use the most recent uploaded image file
            image_path = max(uploaded_files, key=os.path.getctime)
            logger.info(f"Using most recent uploaded image: {image_path}")
            
            # Recreate task in memory
            with queue_lock:
                processing_queue[task_id] = {
                    'id': task_id,
                    'filename': os.path.basename(image_path),
                    'filepath': image_path,
                    'status': 'uploaded',
                    'current_step': 'Ready for mockup processing'
                }
                task = processing_queue[task_id]
            
        if task['status'] != 'uploaded':
            return jsonify({'error': f'Task status is {task["status"]}, cannot start mockup workflow'}), 400
        
        # Get the uploaded image path
        image_path = task.get('filepath')
        if not image_path or not os.path.exists(image_path):
            return jsonify({'error': 'Uploaded image file not found'}), 404
        
        # Find the selected PSD template in uploads
        try:
            from dynamic_mockups import get_available_dynamic_mockups
        except ImportError as e:
            logger.error(f"Import error: {e}")
            return jsonify({'error': 'Dynamic Mockups integration not available'}), 500
        
        available_templates = get_available_dynamic_mockups()
        selected_template = next((t for t in available_templates if t['filename'] == template_filename), None)
        
        if not selected_template:
            return jsonify({'error': f'PSD template "{template_filename}" not found in uploads'}), 404
        
        # Update task status
        with queue_lock:
            processing_queue[task_id]['status'] = 'creating_mockup'
            processing_queue[task_id]['current_step'] = 'Processing with Dynamic Mockups API...'
            processing_queue[task_id]['selected_template'] = template_filename
        
        # Use Dynamic Mockups API for proper smart object replacement  
        try:
            
            logger.info(f"Starting Dynamic Mockups processing for task {task_id}")
            
            # Extract template name for API
            template_name = os.path.splitext(template_filename)[0].replace('_', ' ')
            
            # Use Dynamic Mockups API - no file size limits, optimized for performance
            result = process_image_with_dynamic_mockups(
                image_path=image_path,
                psd_template_path=selected_template['path'], 
                template_name=template_name
            )
            
            if result['success']:
                # Download the generated mockup from Dynamic Mockups
                mockup_url = result['mockup_url']
                output_filename = f"dynamic_mockup_{uuid.uuid4().hex[:8]}.png"
                output_path = os.path.join(PROCESSED_FOLDER, output_filename)
                
                logger.info(f"Downloading mockup from: {mockup_url}")
                
                # Download the mockup image
                response = requests.get(mockup_url)
                response.raise_for_status()
                
                with open(output_path, 'wb') as f:
                    f.write(response.content)
                
                # Update task with completed mockup
                with queue_lock:
                    processing_queue[task_id]['status'] = 'mockup_complete'
                    processing_queue[task_id]['current_step'] = 'Professional mockup created with Dynamic Mockups'
                    processing_queue[task_id]['mockup_path'] = output_path
                    processing_queue[task_id]['mockup_filename'] = output_filename
                    processing_queue[task_id]['dynamic_mockup_info'] = result
                
                logger.info(f"Dynamic Mockups processing completed: {output_path}")
                
                return jsonify({
                    'success': True,
                    'automatic_processing': True,
                    'mockup_path': f"/processed/{output_filename}",
                    'mockup_filename': output_filename,
                    'template_name': template_name,
                    'task_id': task_id,
                    'message': f'Professional mockup created using Dynamic Mockups with {template_name}!',
                    'smart_objects': result.get('smart_objects', []),
                    'mockup_uuid': result.get('mockup_uuid')
                })
                
            else:
                logger.error(f"Dynamic Mockups processing failed: {result['error']}")
                
                # Update task with error status
                with queue_lock:
                    processing_queue[task_id]['status'] = 'error'
                    processing_queue[task_id]['current_step'] = f'Dynamic Mockups failed: {result["error"]}'
                    processing_queue[task_id]['error'] = result['error']
                
                return jsonify({
                    'success': False,
                    'error': f'Dynamic Mockups failed: {result["error"]}',
                    'task_id': task_id
                }), 500
            
        except Exception as processing_error:
            logger.error(f"Dynamic Mockups processing error: {processing_error}")
            
            # Update task with error status
            with queue_lock:
                processing_queue[task_id]['status'] = 'error'
                processing_queue[task_id]['current_step'] = f'Processing failed: {str(processing_error)}'
                processing_queue[task_id]['error'] = str(processing_error)
            
            return jsonify({
                'success': False,
                'error': f'Mockup processing failed: {str(processing_error)}',
                'task_id': task_id
            }), 500
            
    except Exception as e:
        logger.error(f"Error starting mockup workflow: {e}")
        return jsonify({'error': str(e)}), 500

def _calculate_transparency_ratio(image):
    """Calculate the ratio of transparent pixels in an RGBA image"""
    if image.mode != 'RGBA':
        return 0
    
    alpha_channel = image.split()[-1]
    transparent_pixels = sum(1 for pixel in alpha_channel.getdata() if pixel < 128)
    total_pixels = image.width * image.height
    
    return transparent_pixels / total_pixels if total_pixels > 0 else 0

def _create_simple_frame_mockup(user_image, output_path):
    """Create a simple framed mockup as fallback"""
    width, height = user_image.size
    
    # Add padding for frame effect
    frame_width = 100
    new_width = width + 2 * frame_width
    new_height = height + 2 * frame_width
    
    # Create frame background
    frame_image = Image.new('RGB', (new_width, new_height), (240, 240, 240))
    
    # Add inner border
    inner_border = Image.new('RGB', (width + 20, height + 20), (200, 200, 200))
    frame_image.paste(inner_border, (frame_width - 10, frame_width - 10))
    
    # Paste user image
    frame_image.paste(user_image, (frame_width, frame_width))
    
    # Save result
    frame_image.save(output_path, 'PNG', quality=95)

@app.route('/queue_test')
def queue_test():
    """DRASTIC MEASURE: Minimal queue test page"""
    with queue_lock:
        tasks = processing_queue
    ready_tasks = [t for t in tasks.values() if t.get('ready_framed') == True]
    
    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Queue Test - {len(ready_tasks)} Ready-Framed Tasks</title>
        <style>
            .card {{ border: 2px solid #007bff; margin: 10px; padding: 15px; background: #f8f9fa; }}
            .badge {{ background: #28a745; color: white; padding: 5px 10px; border-radius: 4px; font-weight: bold; }}
            body {{ font-family: Arial, sans-serif; margin: 20px; }}
            h1 {{ color: #007bff; }}
        </style>
    </head>
    <body>
        <h1>DIRECT SERVER QUEUE TEST</h1>
        <p><strong>Total tasks on server:</strong> {len(tasks)}</p>
        <p><strong>Ready-framed tasks:</strong> {len(ready_tasks)}</p>
        
        <div id="queue-display">
    '''
    
    if ready_tasks:
        for task in ready_tasks:
            task_id = task.get('task_id', 'Unknown')
            html += f'''
            <div class="card" id="task-{task_id}">
                <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                    <div>
                        <h3>{task.get('filename', 'Unknown')}</h3>
                        <p>Status: <span class="badge">{task.get('status', 'uploaded')}</span></p>
                        <p>Step: {task.get('current_step', 'Ready for processing')}</p>
                        <p style="font-size: 0.8em; color: #666;">{task_id}</p>
                    </div>
                    <button onclick="deleteTask('{task_id}')" 
                            style="background: #dc3545; color: white; border: none; padding: 8px 12px; border-radius: 4px; cursor: pointer;">
                        🗑️ Delete
                    </button>
                </div>
            </div>
            '''
    else:
        html += '<div class="card"><h3>No ready-framed tasks found</h3></div>'
    
    html += '''
        </div>
        <script>
            console.log('🔥 DRASTIC QUEUE TEST LOADED');
            console.log('Ready tasks found:', ''' + str(len(ready_tasks)) + ''');
            
            function deleteTask(taskId) {
                console.log('🗑️ Deleting task:', taskId);
                
                fetch(`/clear_task/${taskId}`, {
                    method: 'DELETE'
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        console.log('✅ Task deleted successfully');
                        // Remove the card from display
                        const taskCard = document.getElementById(`task-${taskId}`);
                        if (taskCard) {
                            taskCard.remove();
                        }
                        // Reload page to show updated count
                        setTimeout(() => location.reload(), 500);
                    } else {
                        console.error('❌ Delete failed:', data.error);
                        alert('Delete failed: ' + (data.error || 'Unknown error'));
                    }
                })
                .catch(error => {
                    console.error('❌ Delete request failed:', error);
                    alert('Delete failed: ' + error.message);
                });
            }
        </script>
    </body>
    </html>
    '''
    
    return html

@app.route('/process_ready_framed/<task_id>', methods=['POST'])
def handle_ready_framed_approval(task_id):
    """Handle approval and processing of ready-framed tasks"""
    try:
        data = request.get_json()
        action = data.get('action')
        edited_data = data.get('edited_data', {})
        
        if action == 'approve':
            # Get the task from the queue
            with queue_lock:
                task = processing_queue.get(task_id)
                if not task:
                    logger.error(f"🔥 APPROVAL FAILED: Task {task_id} not found in processing_queue")
                    logger.info(f"🔍 Available tasks: {list(processing_queue.keys())}")
                    return jsonify({'success': False, 'error': 'Task not found'}), 404
                
                # 🎯 CRITICAL: Extract variants from edited_data and store in task
                variants = edited_data.get('variants', [])
                logger.warning(f"🎯 APPROVAL RECEIVED VARIANTS: {variants}")
                task['variants_data'] = variants  # Store for processing function
                
                # Update task with edited data
                task.update({
                    'ai_metadata': edited_data,
                    'status': 'processing'
                })
            
            # Enqueue for the queue worker instead of spawning an independent thread
            actual_filepath = task.get('filepath')
            if not actual_filepath:
                return jsonify({'success': False, 'error': 'File path not found'}), 400

            with queue_lock:
                processing_queue[task_id]['status'] = 'queued_ready_framed_approve'
                processing_queue[task_id]['_edited_data'] = edited_data
            save_queue_to_disk()
            _ensure_worker_running()
            _task_ready_event.set()

            return jsonify({'success': True, 'message': 'Product creation started'})
        else:
            return jsonify({'success': False, 'error': 'Invalid action'}), 400
            
    except Exception as e:
        logger.error(f"Error handling ready-framed approval: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

def continue_ready_framed_processing(task_id, edited_data):
    """Continue processing after user approval with edited data.
    Called by the queue worker which already holds the semaphore."""
    # CRITICAL: Ensure environment variables are available in this thread
    ensure_env_in_thread()

    # Semaphore handled by queue worker (caller)
    logger.info(f"Continue-ready task {task_id} starting")

    with app.app_context():
        try:
            with queue_lock:
                task = processing_queue.get(task_id)
                if not task:
                    return
                processing_queue[task_id]['current_step'] = 'Creating Shopify product...'
            
            # Use the edited data for Shopify product creation
            # Get all compressed image paths (multiple images support)
            compressed_paths = task.get('compressed_paths', [])
            if not compressed_paths:
                # Fallback to single image for backward compatibility
                filepath = task.get('filepath') or task.get('compressed_path')
                if filepath and not os.path.isabs(filepath):
                    filepath = os.path.abspath(filepath)
                if filepath and os.path.exists(filepath):
                    compressed_paths = [filepath]
            
            logger.info(f"Using {len(compressed_paths)} image(s) for Shopify: {compressed_paths}")
            logger.info(f"File exists check: {[os.path.exists(p) for p in compressed_paths] if compressed_paths else 'No paths'}")
            
            # Create Shopify product using edited metadata via GraphQL
            logger.warning(f"🚀 Creating Shopify product via GraphQL with merged AI+user data for task {task_id}")
            
            # 🎯 DEBUG: Log complete edited_data to see what frontend sent
            logger.warning(f"🎯 RECEIVED EDITED_DATA: {edited_data}")
            logger.warning(f"🔍 VARIANTS CHECK - variants: {edited_data.get('variants')}, variants_data: {edited_data.get('variants_data')}")
            
            # 🎯 CRITICAL FIX: Merge AI metadata with user edits properly
            # Start with AI-generated metadata (preserves title, metafields, etc.)
            original_metadata = task.get('metadata', {})
            logger.warning(f"🔍 ORIGINAL AI METADATA keys: {list(original_metadata.keys())}")
            logger.warning(f"🔍 ORIGINAL AI TITLE: '{original_metadata.get('title', 'NOT_FOUND')}'")
            logger.warning(f"🔍 ORIGINAL AI METAFIELDS: {bool(original_metadata.get('metafields'))}")
            
            # Create merged metadata starting with AI data
            merged_metadata = original_metadata.copy()
            
            # Apply user edits on top of AI data (only override specific fields)
            for key, value in edited_data.items():
                if key in ['title', 'tags', 'collections', 'short_description', 'description', 'faq', 'product_type', 'vendor', 'category', 'variants', 'seo_title', 'seo_url_handle', 'meta_description']:
                    # An older/cached review UI may submit blank values for
                    # fields it did not render. Preserve generated listing
                    # content instead of silently deleting it at publish time.
                    if key == 'short_description' and not str(value or '').strip():
                        continue
                    if key == 'faq' and not value:
                        continue
                    # These fields can be overridden by user
                    merged_metadata[key] = value
                    logger.warning(f"🔧 USER OVERRIDE: {key} = {value}")
            
            # Merge approval-card metafields into merged_metadata (category + product metafields)
            edited_mf = edited_data.get('metafields')
            if edited_mf and isinstance(edited_mf, dict) and len(edited_mf) > 0:
                if 'metafields' not in merged_metadata or not isinstance(merged_metadata.get('metafields'), dict):
                    merged_metadata['metafields'] = {}
                for k, v in edited_mf.items():
                    if v is not None and str(v).strip():
                        merged_metadata['metafields'][k] = str(v).strip()
                        logger.warning(f"🔧 APPROVAL METAFIELD: {k} = {merged_metadata['metafields'][k]}")
            
            # 🎯 CRITICAL: Add variants to merged metadata for GraphQL processing
            # Priority: 1. edited_data.variants (from frontend approval), 2. task.variants_data (from upload)
            variants_to_use = None
            
            # First check edited_data (from frontend approval)
            if edited_data.get('variants') and len(edited_data.get('variants', [])) > 0:
                variants_to_use = edited_data['variants']
                logger.warning(f"🎯 USING VARIANTS FROM EDITED_DATA (frontend approval): {variants_to_use}")
            # Then check task.variants_data (from upload)
            elif task.get('variants_data') and len(task.get('variants_data', [])) > 0:
                variants_to_use = task['variants_data']
                logger.warning(f"🎯 USING VARIANTS FROM TASK (upload time): {variants_to_use}")
            else:
                logger.error(f"❌ NO VARIANTS FOUND IN edited_data OR task!")
                logger.error(f"   edited_data.variants: {edited_data.get('variants')}")
                logger.error(f"   task.variants_data: {task.get('variants_data')}")
            
            if variants_to_use:
                merged_metadata['variants'] = variants_to_use
                logger.warning(f"🎯 FINAL VARIANTS ADDED TO MERGED_METADATA: {len(variants_to_use)} variants")
            
            # Log final merged result
            logger.warning(f"🎯 FINAL MERGED METADATA TITLE: '{merged_metadata.get('title', 'NOT_FOUND')}'")
            logger.warning(f"🎯 FINAL MERGED METADATA METAFIELDS: {bool(merged_metadata.get('metafields'))}")
            logger.warning(f"🎯 FINAL MERGED METADATA keys: {list(merged_metadata.keys())}")
            
            # Ensure metafields dict exists so overrides and defaults can run
            if 'metafields' not in merged_metadata or not isinstance(merged_metadata.get('metafields'), dict):
                merged_metadata['metafields'] = {}
            
            # CRITICAL FIX: Apply manual overrides to merged_metadata based on task settings
            logger.warning(f"🔍 BEFORE OVERRIDES - Tags: {merged_metadata.get('tags', 'NOT_SET')}, Collections: {merged_metadata.get('collections', 'NOT_SET')}")
            if task:
                logger.warning(f"🔍 TASK SETTINGS - tags_manual: {task.get('tags_manual', False)}, collections_manual: {task.get('collections_manual', False)}")
                logger.warning(f"🔍 MANUAL VALUES - manual_tags: '{task.get('manual_tags', '')}', manual_collections: '{task.get('manual_collections', '')}'")
                
                # Apply manual tags override if specified
                if task.get('tags_manual', False) and task.get('manual_tags'):
                    manual_tags = task['manual_tags'].strip()
                    if manual_tags:
                        merged_metadata['tags'] = [tag.strip() for tag in manual_tags.split(',') if tag.strip()]
                        logger.warning(f"🔧 TAGS OVERRIDE APPLIED: {merged_metadata['tags']}")
                    else:
                        merged_metadata['tags'] = []
                        logger.warning(f"🔧 TAGS OVERRIDE: Using empty tags list")
                # Note: If not manual override, AI tags are already preserved in merged_metadata
                
                # Apply manual collections override if specified
                if task.get('collections_manual', False) and task.get('manual_collections'):
                    manual_collections = task['manual_collections'].strip()
                    if manual_collections:
                        merged_metadata['collections'] = [col.strip() for col in manual_collections.split(',') if col.strip()]
                        logger.warning(f"🔧 COLLECTIONS OVERRIDE APPLIED: {merged_metadata['collections']}")
                    else:
                        merged_metadata['collections'] = []
                        logger.warning(f"🔧 COLLECTIONS OVERRIDE: Using empty collections list")
                # Note: If not manual override, AI collections are already preserved in merged_metadata
                
                # ENFORCE collections_enabled: clear collections if user turned them off (overrides everything above)
                if not task.get('collections_enabled', True):
                    merged_metadata['collections'] = []
                    logger.warning(f"🔧 EDIT: collections_enabled=False — cleared collections before publish")
                
                # Apply manual category override if specified (so product category is dictatable from form)
                if task.get('category_manual', False) and task.get('manual_category'):
                    manual_cat = task['manual_category'].strip()
                    merged_metadata['category'] = manual_cat
                    logger.warning(f"🔧 CATEGORY OVERRIDE APPLIED: '{manual_cat}'")
                    if task.get('manual_category_gid'):
                        merged_metadata['category_gid'] = task['manual_category_gid']
                        logger.warning(f"🔧 CATEGORY GID OVERRIDE APPLIED: '{merged_metadata['category_gid']}'")
                    else:
                        resolved_gid = resolve_category_name_to_gid(manual_cat)
                        if resolved_gid:
                            merged_metadata['category_gid'] = resolved_gid
                            logger.warning(f"🔧 CATEGORY GID RESOLVED FROM NAME: '{resolved_gid}'")

                # Also resolve AI-generated category to GID if no manual override
                if not merged_metadata.get('category_gid') and merged_metadata.get('category'):
                    resolved_gid = resolve_category_name_to_gid(merged_metadata['category'])
                    if resolved_gid:
                        merged_metadata['category_gid'] = resolved_gid
                        logger.warning(f"🔧 AI CATEGORY GID RESOLVED: '{resolved_gid}' from '{merged_metadata['category']}'")

                # Apply manual product metafields (Theme, Frame Style, Color) so they are sent to Shopify
                if 'metafields' not in merged_metadata or not isinstance(merged_metadata.get('metafields'), dict):
                    merged_metadata['metafields'] = {}
                mf = merged_metadata['metafields']
                if task.get('color_manual') and task.get('manual_color'):
                    mf['color'] = task['manual_color'].strip()
                    logger.warning(f"🔧 METAFIELD OVERRIDE: color = {mf['color']}")
                elif not mf.get('color') and original_metadata.get('metafields', {}).get('color'):
                    mf['color'] = original_metadata['metafields']['color']
                if task.get('frame_style_manual') and task.get('manual_frame_style'):
                    mf['frame_style'] = task['manual_frame_style'].strip()
                    logger.warning(f"🔧 METAFIELD OVERRIDE: frame_style = {mf['frame_style']}")
                elif not mf.get('frame_style') and original_metadata.get('metafields', {}).get('frame_style'):
                    mf['frame_style'] = original_metadata['metafields']['frame_style']
                if task.get('theme_manual') and task.get('manual_theme'):
                    mf['theme'] = task['manual_theme'].strip()
                    logger.warning(f"🔧 METAFIELD OVERRIDE: theme = {mf['theme']}")
                elif not mf.get('theme') and original_metadata.get('metafields', {}).get('theme'):
                    mf['theme'] = original_metadata['metafields']['theme']
                # Category metafields (condition, decoration_material, artwork_frame_material)
                if task.get('condition_manual') and task.get('manual_condition'):
                    mf['condition'] = task['manual_condition'].strip()
                elif not mf.get('condition') and original_metadata.get('metafields', {}).get('condition'):
                    mf['condition'] = original_metadata['metafields']['condition']
                if task.get('decoration_material_manual') and task.get('manual_decoration_material'):
                    mf['decoration_material'] = task['manual_decoration_material'].strip()
                elif not mf.get('decoration_material') and original_metadata.get('metafields', {}).get('decoration_material'):
                    mf['decoration_material'] = original_metadata['metafields']['decoration_material']
                if task.get('artwork_frame_material_manual') and task.get('manual_artwork_frame_material'):
                    mf['artwork_frame_material'] = task['manual_artwork_frame_material'].strip()
                elif not mf.get('artwork_frame_material') and original_metadata.get('metafields', {}).get('artwork_frame_material'):
                    mf['artwork_frame_material'] = original_metadata['metafields']['artwork_frame_material']
                # Ensure AI-generated metafields (from original_metadata) are preserved if not overridden
                for k, v in (original_metadata.get('metafields') or {}).items():
                    if k not in mf and v and str(v).strip():
                        mf[k] = v
                _apply_manual_metafield_overrides(merged_metadata, task)
            
            # Guarantee category metafield defaults when missing so they are always sent to Shopify
            mf_final = merged_metadata.get('metafields') or {}
            if not mf_final.get('condition') or not str(mf_final.get('condition', '')).strip():
                mf_final['condition'] = 'New'
            if not mf_final.get('decoration_material') or not str(mf_final.get('decoration_material', '')).strip():
                mf_final['decoration_material'] = 'Mixed Materials'
            if not mf_final.get('artwork_frame_material') or not str(mf_final.get('artwork_frame_material', '')).strip():
                mf_final['artwork_frame_material'] = 'Unframed'
            
            logger.warning(f"🔍 AFTER OVERRIDES - Tags: {merged_metadata.get('tags', 'NOT_SET')}, Collections: {merged_metadata.get('collections', 'NOT_SET')}")
            
            # Get publishing settings - use task data (user's explicit selections)
            publishing_settings = None
            if task:
                task_channels = task.get('selected_sales_channels', [])
                task_markets = task.get('selected_markets', [])
                
                # Use whatever the user selected - even if empty (they may have unchecked everything intentionally)
                logger.info(f"📡 USING TASK PUBLISHING SETTINGS - Publishing: {len(task_channels)}, Catalogs: {len(task_markets)}")
                publishing_settings = {
                    'selected_channels': task_channels,
                    'selected_markets': task_markets,
                    'auto_publish_all_channels': task.get('auto_publish_all_channels', False),
                }
            
            # Get shop credentials from task data (stored when processing started)
            _shop_domain = task.get('shop_domain')
            _shop_token = task.get('shop_access_token')
            if not _shop_domain or not _shop_token:
                error_msg = "No Shopify store connected. Please connect your store first."
                logger.error(error_msg)
                with queue_lock:
                    if task_id in processing_queue:
                        processing_queue[task_id]['status'] = 'error'
                        processing_queue[task_id]['error'] = error_msg
                return
            
            # 🎯 CRITICAL FIX: Use merged_metadata instead of edited_data to preserve AI title and metafields
            # Pass all compressed image paths for multiple image upload
            # Hard timeout so queue never sticks on "Creating Shopify product..." indefinitely
            use_main_image_per_variant = task.get('use_main_image_per_variant', True)
            product_data = None
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        create_product_with_graphql,
                        merged_metadata,
                        task.get('filename'),
                        publishing_settings,
                        compressed_paths,
                        _shop_domain,
                        _shop_token,
                        use_main_image_per_variant=use_main_image_per_variant
                    )
                    product_data = future.result(timeout=120)
            except FuturesTimeoutError:
                logger.error("Product creation timed out after 120 seconds")
                with queue_lock:
                    if task_id in processing_queue:
                        processing_queue[task_id]['status'] = 'error'
                        processing_queue[task_id]['error'] = 'Product creation timed out after 120 seconds'
                return
            
            logger.warning(f"🎯 USED MERGED_METADATA FOR PRODUCT CREATION - Title: '{merged_metadata.get('title')}', Metafields: {bool(merged_metadata.get('metafields'))}")
            
            if not product_data:
                raise Exception("Failed to create Shopify product")

            # Enrich with category metafields (best-effort, never fails product creation)
            try:
                category_gid = merged_metadata.get('category_gid', '')
                if category_gid and product_data.get('gid'):
                    with queue_lock:
                        processing_queue[task_id]['current_step'] = 'Setting category attributes...'
                    enrich_product_with_category_metafields(
                        product_gid=product_data['gid'],
                        category_gid=category_gid,
                        image_path=_category_enrichment_image(task, compressed_paths),
                        category_attribute_picks=_category_attribute_picks(merged_metadata),
                        shop_domain=_shop_domain,
                        access_token=_shop_token
                    )
            except Exception as enrich_err:
                logger.warning(f"Category metafield enrichment failed (non-fatal): {enrich_err}")

            # Success
            with queue_lock:
                processing_queue[task_id]['status'] = 'completed'
                processing_queue[task_id]['current_step'] = 'Complete!'
                processing_queue[task_id]['product_id'] = product_data.get('id')
                processing_queue[task_id]['product_url'] = product_data.get('admin_url')
                processing_queue[task_id]['faq_metafield_verified'] = product_data.get('faq_metafield_verified')
            save_queue_to_disk()

            logger.warning(f"✅ SUCCESS: Created Shopify product for task {task_id} - Product ID: {product_data.get('id')}")
            logger.warning(f"✅ PRODUCT TITLE SHOULD BE: '{merged_metadata.get('title', 'NOT_FOUND')}'")
            logger.warning(f"✅ METAFIELDS INCLUDED: {bool(merged_metadata.get('metafields'))}")
            if merged_metadata.get('metafields'):
                logger.warning(f"✅ METAFIELDS SENT: {list(merged_metadata['metafields'].keys())}")
            
            # Cleanup temporary files after successful Shopify upload
            try:
                cleanup_count = temp_file_service.cleanup_after_task(task_id)
                logger.info(f"Cleaned up {cleanup_count} temporary files for task {task_id} after Shopify upload")
            except Exception as cleanup_error:
                logger.warning(f"Error cleaning up temp files for task {task_id}: {cleanup_error}")
            
        except Exception as e:
            logger.error(f"Error in continue_ready_framed_processing for task {task_id}: {str(e)}")
            with queue_lock:
                if task_id in processing_queue:
                    processing_queue[task_id]['status'] = 'error'
                    processing_queue[task_id]['error'] = str(e)
            
            # Cleanup temp files on error as well
            try:
                cleanup_count = temp_file_service.cleanup_after_task(task_id)
                logger.info(f"Cleaned up {cleanup_count} temporary files for task {task_id} after error")
            except Exception as cleanup_error:
                logger.warning(f"Error cleaning up temp files after error: {cleanup_error}")

# Bulk CSV Import Endpoints

@app.route('/bulk_upload', methods=['POST'])
@login_required
def bulk_upload():
    """Handle bulk image upload for CSV generation"""
    try:
        if 'files' not in request.files:
            return jsonify({'success': False, 'error': 'No files provided'}), 400
        
        files = request.files.getlist('files')
        if not files or files[0].filename == '':
            return jsonify({'success': False, 'error': 'No files selected'}), 400
        
        vendor = request.form.get('vendor', 'My Store')
        product_type = request.form.get('product_type', 'Poster')
        custom_prompt = request.form.get('custom_prompt', '')
        
        uploaded_images = []
        
        for file in files:
            if file and file.filename and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                # Add timestamp to prevent conflicts
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_")
                unique_filename = f"{timestamp}{filename}"
                filepath = os.path.join(UPLOAD_FOLDER, unique_filename)
                
                file.save(filepath)
                logger.info(f"Bulk upload saved: {filepath}")
                
                uploaded_images.append({
                    'filename': filename,
                    'unique_filename': unique_filename,
                    'path': filepath
                })
        
        if not uploaded_images:
            return jsonify({'success': False, 'error': 'No valid image files uploaded'}), 400
        
        return jsonify({
            'success': True, 
            'uploaded_images': uploaded_images,
            'count': len(uploaded_images)
        })
        
    except Exception as e:
        logger.error(f"Bulk upload error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/bulk_process_image', methods=['POST'])
@login_required
def bulk_process_image():
    """Process individual image for bulk CSV generation"""
    try:
        data = request.get_json()
        if not data or 'image_path' not in data:
            return jsonify({'success': False, 'error': 'Image path required'}), 400
        
        image_path = data['image_path']
        vendor = data.get('vendor', 'My Store')
        product_type = data.get('product_type', 'Poster')
        custom_prompt = data.get('custom_prompt', '')
        field_settings = data.get('field_settings', {})
        collections_enabled = data.get('collections_enabled', True)
        
        if not os.path.exists(image_path):
            return jsonify({'success': False, 'error': 'Image file not found'}), 400
        
        # CRITICAL: Validate API key before starting AI processing
        import os
        if not os.environ.get("GEMINI_API_KEY"):
            error_msg = "GEMINI_API_KEY is not set. Cannot generate AI metadata. Please ensure the server was started with run_server.py or set the environment variable."
            logger.error(f"❌ {error_msg}")
            return jsonify({'success': False, 'error': error_msg}), 500
        
        # Generate AI metadata using existing function
        logger.info(f"Generating AI metadata for bulk processing: {image_path}")
        
        # Fetch Shopify collections so AI can suggest them (unless user turned collections off)
        if not collections_enabled:
            available_collections = []
            logger.info("Bulk: collections disabled by user - no collections will be assigned")
        else:
            try:
                available_collections = get_collections()
                logger.info(f"Bulk: loaded {len(available_collections)} Shopify collections")
            except Exception as e:
                logger.error(f"Bulk: failed to fetch Shopify collections: {e}. Continuing with empty list.")
                available_collections = []
        
        # Use existing Gemini function to generate metadata
        metadata = generate_product_metadata(image_path, custom_prompt, available_collections)
        if not collections_enabled:
            if metadata:
                metadata['collections'] = []
        elif metadata:
            metadata['collections'] = _resolve_product_collections(
                metadata, available_collections, log_prefix="Bulk-CSV collections",
                mandatory_collections=_mandatory_collections_from_prompt(custom_prompt),
            )
        
        # CRITICAL: Check if metadata generation failed immediately
        if not metadata:
            error_msg = "Failed to generate metadata from AI. Please check your GEMINI_API_KEY and ensure the API is accessible."
            logger.error(f"❌ {error_msg}")
            logger.error(f"❌ Image path: {image_path}")
            return jsonify({'success': False, 'error': error_msg}), 500
        
        # Create image URL for CSV
        filename = os.path.basename(image_path)
        image_url = f"{request.url_root}serve_file/{filename}"
        
        # Helper: resolve field value using field_settings
        def resolve_field(field_id, ai_value, fallback=''):
            """Apply field_settings mode: auto=AI value, manual=manual_value, do_not_edit=empty."""
            fs = field_settings.get(field_id, {})
            mode = fs.get('mode', 'auto')
            if mode == 'manual':
                return fs.get('manual_value', fallback)
            elif mode == 'do_not_edit':
                return ''
            else:  # auto
                return ai_value if ai_value is not None else fallback
        
        # Format tags: ensure list is comma-separated string
        raw_tags = metadata.get('tags', [])
        if isinstance(raw_tags, list):
            tags_str = ', '.join(str(t) for t in raw_tags)
        else:
            tags_str = str(raw_tags)
        
        # Format collections: ensure list is comma-separated string
        raw_collections = metadata.get('collections', [])
        if isinstance(raw_collections, list):
            collections_str = ', '.join(str(c) for c in raw_collections)
        else:
            collections_str = str(raw_collections)
        
        # Prepare product data in CSV format structure
        product_data = {
            'handle': metadata.get('seo_url_handle', '').lower().replace(' ', '-'),
            'title': metadata.get('title', ''),
            'body_html': format_description_html(metadata.get('description', '')),
            'vendor': vendor,
            'product_category': metadata.get('category', 'Home & Garden > Decor > Artwork > Posters, Prints, & Visual Artwork'),
            'type': metadata.get('product_type', product_type),
            'tags': resolve_field('tags', tags_str),
            'published': True,
            'image_src': image_url,
            'image_alt_text': metadata.get('alt_text', ''),
            'seo_title': resolve_field('seo_title', metadata.get('seo_title', '')),
            'seo_description': metadata.get('meta_description', ''),
            'collections': resolve_field('collections', collections_str),
            'google_product_category': resolve_field('google_product_category', metadata.get('google_product_category', '')),
            'gender': resolve_field('gender', metadata.get('gender', 'Unisex')),
            'age_group': resolve_field('age_group', metadata.get('age_group', 'Adult')),
            'condition': resolve_field('condition', metadata.get('condition', 'New')),
            'custom_product': resolve_field('custom_product', metadata.get('custom_product', 'TRUE')),
            'custom_label_0': resolve_field('custom_label_0', metadata.get('custom_label_0', '')),
            'custom_label_1': resolve_field('custom_label_1', metadata.get('custom_label_1', '')),
            'custom_label_2': resolve_field('custom_label_2', metadata.get('custom_label_2', '')),
            'custom_label_3': resolve_field('custom_label_3', metadata.get('custom_label_3', '')),
            'custom_label_4': resolve_field('custom_label_4', metadata.get('custom_label_4', '')),
            'metafields': metadata.get('metafields', {}),
            'filename': filename,
            'original_path': image_path
        }
        
        return jsonify({
            'success': True,
            'product_data': product_data
        })
        
    except Exception as e:
        logger.error(f"Bulk process image error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/generate_bulk_csv', methods=['POST'])
@login_required
def generate_bulk_csv():
    """Generate CSV file for Shopify bulk import"""
    try:
        data = request.get_json()
        if not data or 'products' not in data:
            return jsonify({'success': False, 'error': 'Products data required'}), 400
        
        products = data['products']
        if not products:
            return jsonify({'success': False, 'error': 'No products to export'}), 400
        
        field_settings = data.get('field_settings', {})
        
        # Generate CSV content
        csv_content = generate_shopify_csv(products, field_settings)
        
        # Create response
        response = make_response(csv_content)
        response.headers['Content-Type'] = 'text/csv'
        response.headers['Content-Disposition'] = 'attachment; filename=shopify_bulk_import.csv'
        
        return response
        
    except Exception as e:
        logger.error(f"Generate bulk CSV error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

def _ensure_string(val):
    """Ensure a value is a string for CSV output (convert lists to comma-separated)."""
    if isinstance(val, list):
        return ', '.join(str(v) for v in val)
    if val is None:
        return ''
    return str(val)

def generate_shopify_csv(products, field_settings=None):
    """Generate CSV content matching Shopify's import format"""
    import csv
    import io
    
    if field_settings is None:
        field_settings = {}
    
    # Define the CSV header based on the sample provided (with Collection column added)
    header = [
        'Handle', 'Title', 'Body (HTML)', 'Vendor', 'Product Category', 'Type', 'Tags', 'Collection',
        'Published', 'Option1 Name', 'Option1 Value', 'Option1 Linked To', 'Option2 Name', 
        'Option2 Value', 'Option2 Linked To', 'Option3 Name', 'Option3 Value', 'Option3 Linked To',
        'Variant SKU', 'Variant Grams', 'Variant Inventory Tracker', 'Variant Inventory Qty',
        'Variant Inventory Policy', 'Variant Fulfillment Service', 'Variant Price', 
        'Variant Compare At Price', 'Variant Requires Shipping', 'Variant Taxable', 
        'Variant Barcode', 'Image Src', 'Image Position', 'Image Alt Text', 'Gift Card',
        'SEO Title', 'SEO Description', 'Google Shopping / Google Product Category',
        'Google Shopping / Gender', 'Google Shopping / Age Group', 'Google Shopping / MPN',
        'Google Shopping / Condition', 'Google Shopping / Custom Product',
        'Google Shopping / Custom Label 0', 'Google Shopping / Custom Label 1',
        'Google Shopping / Custom Label 2', 'Google Shopping / Custom Label 3',
        'Google Shopping / Custom Label 4', 'Color (product.metafields.custom.color)',
        'Frame Style (product.metafields.custom.frame_style)', 
        'Theme (product.metafields.custom.theme)', 
        'Google: Custom Product (product.metafields.mm-google-shopping.custom_product)',
        'Art movement (product.metafields.shopify.art-movement)',
        'Art style (product.metafields.shopify.art-style)',
        'Artwork authenticity (product.metafields.shopify.artwork-authenticity)',
        'Color (product.metafields.shopify.color-pattern)',
        'Frame style (product.metafields.shopify.frame-style)',
        'Material (product.metafields.shopify.material)',
        'Orientation (product.metafields.shopify.orientation)',
        'Theme (product.metafields.shopify.theme)',
        'Complementary products (product.metafields.shopify--discovery--product_recommendation.complementary_products)',
        'Related products (product.metafields.shopify--discovery--product_recommendation.related_products)',
        'Related products settings (product.metafields.shopify--discovery--product_recommendation.related_products_display)',
        'Search product boosts (product.metafields.shopify--discovery--product_search_boost.queries)',
        'Variant Image', 'Variant Weight Unit', 'Variant Tax Code', 'Cost per item', 'Status'
    ]
    
    # Build a column index map for quick lookup
    col = {name: idx for idx, name in enumerate(header)}
    
    # Size variants with pricing (matching the example)
    size_variants = [
        {'name': '20x30 cm', 'price': '6.99'},
        {'name': '30x40 cm', 'price': '11.99'},
        {'name': '40x50 cm', 'price': '12.99'},
        {'name': '50x70 cm', 'price': '13.99'},
        {'name': 'A1 - 59.4 x 84.1 cm', 'price': '14.99'},
        {'name': 'A2 - 42 x 59.4 cm', 'price': '13.49'},
        {'name': 'A3 - 29.7 x 42 cm', 'price': '11.99'},
        {'name': 'A4 - 21 x 29.7 cm', 'price': '6.99'}
    ]
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Write header
    writer.writerow(header)
    
    # Write product rows
    for product in products:
        metafields = product.get('metafields', {})
        
        # Extract metafield values
        color = metafields.get('color', '')
        frame_style = metafields.get('frame_style', 'unframed')
        theme = metafields.get('theme', '')
        
        # Write rows for each size variant
        for i, variant in enumerate(size_variants):
            row = [''] * len(header)
            
            # Only fill product details in first row
            if i == 0:
                row[col['Handle']] = product.get('handle', '')
                row[col['Title']] = product.get('title', '')
                row[col['Body (HTML)']] = product.get('body_html', '')
                row[col['Vendor']] = product.get('vendor', 'My Store')
                row[col['Product Category']] = product.get('product_category', 'Home & Garden > Decor > Artwork > Posters, Prints, & Visual Artwork')
                row[col['Type']] = product.get('product_type') or product.get('type') or 'Poster'
                row[col['Tags']] = _ensure_string(product.get('tags', ''))
                row[col['Collection']] = _ensure_string(product.get('collections', ''))
                row[col['Published']] = 'true' if product.get('published', True) else 'false'
                row[col['Image Src']] = product.get('image_src', '')
                row[col['Image Position']] = str(i + 1)
                row[col['Image Alt Text']] = product.get('image_alt_text', '')
                row[col['Gift Card']] = 'false'
                row[col['SEO Title']] = _ensure_string(product.get('seo_title', ''))
                row[col['SEO Description']] = product.get('seo_description', '')
                # Google Shopping fields
                row[col['Google Shopping / Google Product Category']] = _ensure_string(product.get('google_product_category', ''))
                row[col['Google Shopping / Gender']] = _ensure_string(product.get('gender', ''))
                row[col['Google Shopping / Age Group']] = _ensure_string(product.get('age_group', ''))
                row[col['Google Shopping / Condition']] = _ensure_string(product.get('condition', ''))
                row[col['Google Shopping / Custom Product']] = _ensure_string(product.get('custom_product', ''))
                row[col['Google Shopping / Custom Label 0']] = _ensure_string(product.get('custom_label_0', ''))
                row[col['Google Shopping / Custom Label 1']] = _ensure_string(product.get('custom_label_1', ''))
                row[col['Google Shopping / Custom Label 2']] = _ensure_string(product.get('custom_label_2', ''))
                row[col['Google Shopping / Custom Label 3']] = _ensure_string(product.get('custom_label_3', ''))
                row[col['Google Shopping / Custom Label 4']] = _ensure_string(product.get('custom_label_4', ''))
                # Metafields
                row[col['Color (product.metafields.custom.color)']] = color
                row[col['Frame Style (product.metafields.custom.frame_style)']] = frame_style
                row[col['Theme (product.metafields.custom.theme)']] = theme
                row[col['Google: Custom Product (product.metafields.mm-google-shopping.custom_product)']] = _ensure_string(product.get('custom_product', ''))
                row[col['Art movement (product.metafields.shopify.art-movement)']] = _ensure_string(metafields.get('art_movement', ''))
                row[col['Art style (product.metafields.shopify.art-style)']] = _ensure_string(metafields.get('art_style', theme))
                row[col['Artwork authenticity (product.metafields.shopify.artwork-authenticity)']] = _ensure_string(metafields.get('artwork_authenticity', 'Reproduction'))
                row[col['Color (product.metafields.shopify.color-pattern)']] = color
                row[col['Frame style (product.metafields.shopify.frame-style)']] = frame_style
                row[col['Material (product.metafields.shopify.material)']] = _ensure_string(metafields.get('material', metafields.get('decoration_material', 'Paper')))
                row[col['Orientation (product.metafields.shopify.orientation)']] = _ensure_string(metafields.get('orientation', ''))
                row[col['Theme (product.metafields.shopify.theme)']] = theme
                row[col['Status']] = 'active'
            else:
                # For subsequent rows, only add image if different variants had different images
                row[col['Image Src']] = product.get('image_src', '')
                row[col['Image Position']] = str(i + 1)
                row[col['Image Alt Text']] = '""'
            
            # Size option details
            row[col['Option1 Name']] = 'Size'
            row[col['Option1 Value']] = variant['name']
            
            # Variant details
            row[col['Variant SKU']] = ''
            row[col['Variant Grams']] = '10.0'
            row[col['Variant Inventory Tracker']] = 'shopify'
            row[col['Variant Inventory Qty']] = '999'
            row[col['Variant Inventory Policy']] = 'deny'
            row[col['Variant Fulfillment Service']] = 'manual'
            row[col['Variant Price']] = variant['price']
            row[col['Variant Compare At Price']] = ''
            row[col['Variant Requires Shipping']] = 'true'
            row[col['Variant Taxable']] = 'true'
            row[col['Variant Barcode']] = ''
            row[col['Variant Weight Unit']] = 'g'
            
            writer.writerow(row)
    
    return output.getvalue()

# CSV Enhancement Endpoints

@app.route('/parse_shopify_csv', methods=['POST'])
@login_required
def parse_shopify_csv():
    """Parse uploaded Shopify CSV and extract products with image URLs"""
    import csv
    import io
    
    try:
        if 'csv_file' not in request.files:
            return jsonify({'success': False, 'error': 'No CSV file provided'}), 400
        
        csv_file = request.files['csv_file']
        if not csv_file.filename or csv_file.filename == '':
            return jsonify({'success': False, 'error': 'No file selected'}), 400
        
        filename = csv_file.filename
        if not filename.lower().endswith('.csv'):
            return jsonify({'success': False, 'error': 'File must be a CSV'}), 400
        
        content = csv_file.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))
        
        original_rows = list(csv.DictReader(io.StringIO(content)))
        headers = reader.fieldnames
        
        products = []
        seen_handles = set()
        
        for row_index, row in enumerate(original_rows):
            handle = row.get('Handle', '').strip()
            
            if not handle or handle in seen_handles:
                continue
            
            seen_handles.add(handle)
            
            products.append(_shopify_csv_row_to_product(row, row_index))
        
        logger.info(f"Parsed CSV: {len(products)} products found")
        
        return jsonify({
            'success': True,
            'products': products,
            'original_csv': {
                'headers': headers,
                'rows': original_rows
            }
        })
        
    except Exception as e:
        logger.error(f"Parse CSV error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/shopify_products_for_enhancement', methods=['GET'])
@login_required
def shopify_products_for_enhancement():
    """Fetch live Shopify products for the bulk SEO enhancement workflow."""
    try:
        shop = get_current_shop()
        if not shop:
            return jsonify({'success': False, 'error': 'No Shopify store connected. Connect a store first.'}), 400

        try:
            limit = int(request.args.get('limit', 250))
        except (TypeError, ValueError):
            limit = 250
        query_parts = []
        query_filter = (request.args.get('query') or '').strip()
        status_filter = (request.args.get('status') or '').strip().upper()
        vendor_filter = (request.args.get('vendor') or '').strip()
        tag_filter = (request.args.get('tag') or '').strip()
        title_filter = (request.args.get('title') or '').strip()
        if query_filter:
            query_parts.append(query_filter)
        if status_filter:
            query_parts.append(f"status:{status_filter.lower()}")
        if vendor_filter:
            query_parts.append(f'vendor:"{vendor_filter}"')
        if tag_filter:
            query_parts.append(f'tag:"{tag_filter}"')
        if title_filter:
            query_parts.append(f'title:"{title_filter}"')
        query_filter = " ".join(query_parts).strip()
        after_cursor = (request.args.get('after') or '').strip() or None
        sort_key = (request.args.get('sort_key') or 'UPDATED_AT').strip().upper()
        reverse = (request.args.get('reverse', 'true') or 'true').lower() == 'true'

        page = list_products_for_seo_enhancement(
            limit=limit,
            query_filter=query_filter,
            shop_domain=shop.shop_domain,
            access_token=shop.access_token,
            after_cursor=after_cursor,
            sort_key=sort_key,
            reverse=reverse,
            include_page_info=True,
        )
        products = page.get('products', [])
        # Catalogue-level resources do not change between cursor pages.
        # Fetch them once instead of repeating these calls for all 26 pages.
        if after_cursor is None:
            metafield_definitions = get_product_metafield_definitions(
                shop_domain=shop.shop_domain,
                access_token=shop.access_token,
            )
            inventory_locations = get_inventory_locations(
                shop_domain=shop.shop_domain,
                access_token=shop.access_token,
            )
            price_lists = get_price_lists(
                shop_domain=shop.shop_domain,
                access_token=shop.access_token,
            )
        else:
            metafield_definitions = []
            inventory_locations = []
            price_lists = []

        return jsonify({
            'success': True,
            'source': 'shopify_api',
            'products': products,
            'count': len(products),
            'page_info': page.get('page_info', {}),
            'effective_query': query_filter,
            'sort_key': page.get('sort_key', sort_key),
            'reverse': page.get('reverse', reverse),
            'metafield_definitions': metafield_definitions,
            'inventory_locations': inventory_locations,
            'price_lists': price_lists,
        })
    except Exception as e:
        logger.error(f"Fetch Shopify products for enhancement error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

shopify_catalogue_bulk_jobs = {}
shopify_catalogue_bulk_jobs_lock = threading.Lock()


def _encode_catalogue_payload(product):
    raw = json.dumps(product, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return base64.b64encode(zlib.compress(raw, 6)).decode('ascii')


def _decode_catalogue_payload(value):
    return json.loads(zlib.decompress(base64.b64decode(value.encode('ascii'))).decode('utf-8'))


def _catalogue_resources_json(metafield_definitions=None, inventory_locations=None, price_lists=None,
                              collections=None, collection_error=None):
    return json.dumps({
        'metafield_definitions': metafield_definitions or [],
        'inventory_locations': inventory_locations or [],
        'price_lists': price_lists or [],
        'collections': collections or [],
        'collection_error': collection_error,
    }, ensure_ascii=False, separators=(',', ':'))


def _persist_full_catalogue_cache(shop_id, user_id, path, metafield_definitions, inventory_locations, price_lists,
                                  collections=None, collection_error=None):
    """Build a new cache generation, then atomically make it active."""
    from models import ShopifyCachedProduct, ShopifyCatalogueCacheState

    generation = str(uuid.uuid4())
    count = 0
    newest_updated_at = ''
    batch = []
    with open(path, 'rb') as handle:
        for position, raw in enumerate(handle):
            if not raw:
                continue
            product = json.loads(raw.decode('utf-8'))
            product_gid = str(product.get('id') or '')
            if not product_gid:
                continue
            updated_at = str(product.get('updated_at') or '')
            newest_updated_at = max(newest_updated_at, updated_at)
            batch.append(ShopifyCachedProduct(
                shop_id=shop_id,
                generation=generation,
                product_gid=product_gid,
                handle=str(product.get('handle') or ''),
                shopify_updated_at=updated_at,
                position=position,
                payload_compressed=_encode_catalogue_payload(product),
            ))
            count += 1
            if len(batch) >= 500:
                db.session.bulk_save_objects(batch)
                db.session.commit()
                batch = []
    if batch:
        db.session.bulk_save_objects(batch)
        db.session.commit()

    state = ShopifyCatalogueCacheState.query.filter_by(shop_id=shop_id).first()
    old_generation = state.active_generation if state else None
    now = datetime.utcnow()
    if not state:
        state = ShopifyCatalogueCacheState(shop_id=shop_id, user_id=user_id, active_generation=generation)
        db.session.add(state)
    state.user_id = user_id
    state.active_generation = generation
    state.product_count = count
    state.sync_status = 'ready'
    state.last_error = None
    state.last_full_sync_at = now
    state.last_incremental_sync_at = now
    state.last_shopify_updated_at = newest_updated_at or None
    state.resources_json = _catalogue_resources_json(
        metafield_definitions, inventory_locations, price_lists, collections, collection_error
    )
    db.session.commit()
    if old_generation and old_generation != generation:
        ShopifyCachedProduct.query.filter_by(shop_id=shop_id, generation=old_generation).delete(synchronize_session=False)
        db.session.commit()
    # Serialize while the ORM instance is still attached to this app context.
    # Background workers cannot safely dereference an expired SQLAlchemy model
    # after the context/session is removed.
    return _catalogue_cache_public(state)


def _catalogue_cache_public(state):
    if not state:
        return {'available': False, 'count': 0, 'status': 'empty'}
    return {
        'available': bool(state.active_generation and state.product_count >= 0),
        'count': int(state.product_count or 0),
        'status': state.sync_status or 'ready',
        'last_error': state.last_error,
        'last_full_sync_at': state.last_full_sync_at.isoformat() if state.last_full_sync_at else None,
        'last_incremental_sync_at': state.last_incremental_sync_at.isoformat() if state.last_incremental_sync_at else None,
        'last_shopify_updated_at': state.last_shopify_updated_at,
    }


def _cleanup_shopify_catalogue_bulk_jobs(max_age_seconds=6 * 60 * 60):
    """Release old in-memory indexes and temporary catalogue exports."""
    cutoff = time.time() - max_age_seconds
    stale_paths = []
    with shopify_catalogue_bulk_jobs_lock:
        stale_ids = [
            job_id for job_id, job in shopify_catalogue_bulk_jobs.items()
            if job.get('status') in ('ready', 'error') and job.get('created_ts', time.time()) < cutoff
        ]
        for job_id in stale_ids:
            job = shopify_catalogue_bulk_jobs.pop(job_id, {})
            if job.get('path'):
                stale_paths.append(job['path'])
    for path in stale_paths:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as exc:
            logger.warning("Could not remove stale Shopify catalogue export %s: %s", path, exc)


def _catalogue_query_parts(values):
    parts = []
    raw = (values.get('query') or '').strip()
    if raw:
        parts.append(raw)
    status = (values.get('status') or '').strip().lower()
    vendor = (values.get('vendor') or '').strip()
    tag = (values.get('tag') or '').strip()
    title = (values.get('title') or '').strip()
    if status:
        parts.append(f"status:{status}")
    if vendor:
        parts.append(f'vendor:"{vendor}"')
    if tag:
        parts.append(f'tag:"{tag}"')
    if title:
        parts.append(f'title:"{title}"')
    return " ".join(parts)


def _run_shopify_catalogue_bulk_job(job_id, shop_id, shop_domain, access_token):
    try:
        while True:
            with shopify_catalogue_bulk_jobs_lock:
                job = shopify_catalogue_bulk_jobs.get(job_id)
                if not job:
                    return
                operation_id = job['operation_id']
            operation = get_product_catalogue_bulk_operation(
                operation_id,
                shop_domain=shop_domain,
                access_token=access_token,
            )
            status = str(operation.get('status') or '').upper()
            with shopify_catalogue_bulk_jobs_lock:
                job = shopify_catalogue_bulk_jobs.get(job_id)
                if not job:
                    return
                job['status'] = status.lower()
                job['object_count'] = int(operation.get('objectCount') or 0)
                job['updated_at'] = datetime.now().isoformat()
            if status == 'COMPLETED':
                result_url = operation.get('url')
                if not result_url:
                    raise RuntimeError('Shopify completed the export without a result file')
                output_path = os.path.join(temp_file_service.get_temp_dir(), f'shopify-catalogue-{job_id}.jsonl')
                with shopify_catalogue_bulk_jobs_lock:
                    shopify_catalogue_bulk_jobs[job_id]['status'] = 'processing'
                offsets = stream_product_catalogue_bulk_result(result_url, output_path)
                metafield_definitions = get_product_metafield_definitions(
                    shop_domain=shop_domain, access_token=access_token
                )
                inventory_locations = get_inventory_locations(
                    shop_domain=shop_domain, access_token=access_token
                )
                price_lists = get_price_lists(
                    shop_domain=shop_domain, access_token=access_token
                )
                collection_error = None
                try:
                    collections = list_collection_catalogue(
                        shop_domain=shop_domain, access_token=access_token
                    )
                except Exception as collection_exc:
                    collections = []
                    collection_error = str(collection_exc)
                    logger.warning("Collection catalogue audit could not be refreshed: %s", collection_exc)
                with app.app_context():
                    cached_state = _persist_full_catalogue_cache(
                        shop_id,
                        job.get('user_id'),
                        output_path,
                        metafield_definitions,
                        inventory_locations,
                        price_lists,
                        collections,
                        collection_error,
                    )
                with shopify_catalogue_bulk_jobs_lock:
                    job = shopify_catalogue_bulk_jobs.get(job_id)
                    if job:
                        job.update({
                            'status': 'ready',
                            'count': len(offsets),
                            'offsets': offsets,
                            'path': output_path,
                            'metafield_definitions': metafield_definitions,
                            'inventory_locations': inventory_locations,
                            'price_lists': price_lists,
                            'collections': collections,
                            'collection_error': collection_error,
                            'cache': cached_state,
                            'updated_at': datetime.now().isoformat(),
                        })
                return
            if status in ('FAILED', 'CANCELED', 'EXPIRED'):
                raise RuntimeError(
                    f"Shopify catalogue export {status.lower()}: {operation.get('errorCode') or 'unknown error'}"
                )
            time.sleep(4)
    except Exception as exc:
        logger.error("Shopify catalogue bulk job %s failed: %s", job_id, exc)
        try:
            from models import ShopifyCatalogueCacheState
            with app.app_context():
                state = ShopifyCatalogueCacheState.query.filter_by(shop_id=shop_id).first()
                if state:
                    state.sync_status = 'error'
                    state.last_error = str(exc)
                    db.session.commit()
        except Exception as cache_exc:
            logger.warning('Could not record catalogue cache failure: %s', cache_exc)
        with shopify_catalogue_bulk_jobs_lock:
            job = shopify_catalogue_bulk_jobs.get(job_id)
            if job:
                job['status'] = 'error'
                job['error'] = str(exc)
                job['updated_at'] = datetime.now().isoformat()


@app.route('/api/shopify_bulk_catalogue/start', methods=['POST'])
@login_required
def start_shopify_bulk_catalogue():
    try:
        _cleanup_shopify_catalogue_bulk_jobs()
        shop = get_current_shop()
        if not shop:
            return jsonify({'success': False, 'error': 'No Shopify store connected.'}), 400
        values = request.get_json(silent=True) or {}
        query_filter = _catalogue_query_parts(values)
        sort_key = (values.get('sort_key') or 'UPDATED_AT').strip().upper()
        reverse = str(values.get('reverse', 'true')).lower() == 'true'
        operation = start_product_catalogue_bulk_operation(
            query_filter=query_filter,
            sort_key=sort_key,
            reverse=reverse,
            shop_domain=shop.shop_domain,
            access_token=shop.access_token,
        )
        from models import ShopifyCatalogueCacheState
        existing_state = ShopifyCatalogueCacheState.query.filter_by(shop_id=shop.id).first()
        if existing_state:
            existing_state.sync_status = 'full_syncing'
            existing_state.last_error = None
            db.session.commit()
        job_id = str(uuid.uuid4())
        with shopify_catalogue_bulk_jobs_lock:
            shopify_catalogue_bulk_jobs[job_id] = {
                'job_id': job_id,
                'kind': 'full',
                'operation_id': operation['id'],
                'user_id': current_user.id,
                'shop_id': shop.id,
                'shop_domain': shop.shop_domain,
                'status': str(operation.get('status') or 'created').lower(),
                'object_count': 0,
                'count': 0,
                'error': None,
                'created_ts': time.time(),
                'created_at': datetime.now().isoformat(),
                'updated_at': datetime.now().isoformat(),
                'effective_query': query_filter,
                'sort_key': sort_key,
                'reverse': reverse,
            }
        threading.Thread(
            target=_run_shopify_catalogue_bulk_job,
            args=(job_id, shop.id, shop.shop_domain, shop.access_token),
            daemon=True,
            name=f"shopify-catalogue-{job_id[:8]}",
        ).start()
        return jsonify({'success': True, 'job_id': job_id, 'status': operation.get('status')})
    except Exception as exc:
        logger.error("Start Shopify catalogue bulk export failed: %s", exc)
        return jsonify({'success': False, 'error': str(exc)}), 500


def _current_catalogue_bulk_job(job_id):
    with shopify_catalogue_bulk_jobs_lock:
        job = copy.deepcopy(shopify_catalogue_bulk_jobs.get(job_id))
    if not job or job.get('user_id') != current_user.id:
        return None
    shop = get_current_shop()
    if not shop or str(job.get('shop_id')) != str(shop.id):
        return None
    return job


@app.route('/api/shopify_bulk_catalogue/<job_id>/status', methods=['GET'])
@login_required
def shopify_bulk_catalogue_status(job_id):
    job = _current_catalogue_bulk_job(job_id)
    if not job:
        return jsonify({'success': False, 'error': 'Catalogue import job not found.'}), 404
    return jsonify({
        'success': True,
        'status': job.get('status'),
        'object_count': job.get('object_count', 0),
        'count': job.get('count', 0),
        'error': job.get('error'),
        'effective_query': job.get('effective_query', ''),
        'sort_key': job.get('sort_key'),
        'reverse': job.get('reverse'),
        'kind': job.get('kind', 'full'),
        'changed_count': job.get('changed_count'),
        'cache': job.get('cache'),
    })


@app.route('/api/shopify_bulk_catalogue/<job_id>/products', methods=['GET'])
@login_required
def shopify_bulk_catalogue_products(job_id):
    job = _current_catalogue_bulk_job(job_id)
    if not job:
        return jsonify({'success': False, 'error': 'Catalogue import job not found.'}), 404
    if job.get('status') != 'ready':
        return jsonify({'success': False, 'error': 'Catalogue import is not ready.'}), 409

    try:
        offset = max(0, int(request.args.get('offset', 0)))
        limit = max(1, min(int(request.args.get('limit', 500)), 500))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid offset or limit.'}), 400
    offsets = job.get('offsets') or []
    path = job.get('path')
    if not path or not os.path.isfile(path):
        return jsonify({'success': False, 'error': 'Catalogue result file is no longer available. Please import again.'}), 410

    products = []
    selected_offsets = offsets[offset:offset + limit]
    with open(path, 'rb') as handle:
        for byte_offset in selected_offsets:
            handle.seek(byte_offset)
            raw = handle.readline()
            if raw:
                products.append(json.loads(raw.decode('utf-8')))
    total = len(offsets)
    return jsonify({
        'success': True,
        'products': products,
        'offset': offset,
        'next_offset': offset + len(products),
        'total': total,
        'has_more': offset + len(products) < total,
        'metafield_definitions': (job.get('metafield_definitions') or []) if offset == 0 else [],
        'inventory_locations': (job.get('inventory_locations') or []) if offset == 0 else [],
        'price_lists': (job.get('price_lists') or []) if offset == 0 else [],
        'collections': (job.get('collections') or []) if offset == 0 else [],
        'collection_error': job.get('collection_error') if offset == 0 else None,
    })


@app.route('/api/shopify_bulk_catalogue/cache/status', methods=['GET'])
@login_required
def shopify_bulk_catalogue_cache_status():
    from models import ShopifyCatalogueCacheState
    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected.'}), 400
    state = ShopifyCatalogueCacheState.query.filter_by(shop_id=shop.id, user_id=current_user.id).first()
    return jsonify({'success': True, 'cache': _catalogue_cache_public(state)})


@app.route('/api/shopify_bulk_catalogue/cache/products', methods=['GET'])
@login_required
def shopify_bulk_catalogue_cache_products():
    from models import ShopifyCachedProduct, ShopifyCatalogueCacheState
    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected.'}), 400
    state = ShopifyCatalogueCacheState.query.filter_by(shop_id=shop.id, user_id=current_user.id).first()
    if not state or not state.active_generation:
        return jsonify({'success': False, 'error': 'No cached catalogue is available yet.'}), 404
    try:
        offset = max(0, int(request.args.get('offset', 0)))
        limit = max(1, min(int(request.args.get('limit', 500)), 500))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid offset or limit.'}), 400
    rows = (ShopifyCachedProduct.query
            .filter_by(shop_id=shop.id, generation=state.active_generation)
            .order_by(ShopifyCachedProduct.position.asc())
            .offset(offset).limit(limit).all())
    products = [_decode_catalogue_payload(row.payload_compressed) for row in rows]
    resources = json.loads(state.resources_json or '{}')
    total = int(state.product_count or 0)
    return jsonify({
        'success': True,
        'products': products,
        'offset': offset,
        'next_offset': offset + len(products),
        'total': total,
        'has_more': offset + len(products) < total,
        'cache': _catalogue_cache_public(state),
        'metafield_definitions': resources.get('metafield_definitions', []) if offset == 0 else [],
        'inventory_locations': resources.get('inventory_locations', []) if offset == 0 else [],
        'price_lists': resources.get('price_lists', []) if offset == 0 else [],
        'collections': resources.get('collections', []) if offset == 0 else [],
        'collection_error': resources.get('collection_error') if offset == 0 else None,
    })


def _create_collection_edit_snapshot(shop, originals, retention_days=90):
    """Store collection before-states in the existing shop-bound recovery ledger."""
    from datetime import timedelta
    from models import BulkEditSnapshot

    originals = [copy.deepcopy(item) for item in (originals or []) if item]
    if not originals:
        return None
    payload = {
        'version': 2,
        'resource_type': 'collection',
        'shop_domain': shop.shop_domain,
        'created_at': datetime.now().isoformat(),
        'retention_days': retention_days,
        'collections': originals,
    }
    encoded = base64.b64encode(
        zlib.compress(json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8'), level=9)
    ).decode('ascii')
    now = datetime.now()
    snapshot = BulkEditSnapshot(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        shop_id=shop.id,
        shop_domain=shop.shop_domain,
        item_count=len(originals),
        payload_compressed=encoded,
        status='saved',
        created_at=now,
        expires_at=now + timedelta(days=retention_days),
    )
    BulkEditSnapshot.query.filter(BulkEditSnapshot.expires_at < now).delete(synchronize_session=False)
    db.session.add(snapshot)
    db.session.commit()
    return snapshot.id


@app.route('/api/shopify_collections/apply', methods=['POST'])
@login_required
def apply_shopify_collection_changes():
    """Apply reviewed collection proposals with confirmation, conflict checks, and recovery."""
    from models import ShopifyCatalogueCacheState

    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected.'}), 400
    data = request.get_json(silent=True) or {}
    edits = data.get('edits') or []
    originals = data.get('originals') or []
    if not edits or len(edits) > 50:
        return jsonify({'success': False, 'error': 'Submit between 1 and 50 reviewed collection changes.'}), 400
    if len(originals) != len(edits):
        return jsonify({'success': False, 'error': 'A complete collection before-state is required.'}), 400
    expected_confirmation = f"APPLY {len(edits)} COLLECTION" + ('S' if len(edits) != 1 else '')
    if data.get('confirmation') != expected_confirmation:
        return jsonify({'success': False, 'error': f'Type {expected_confirmation} exactly to confirm.'}), 400

    for edit in edits:
        changes = edit.get('changes') or {}
        if 'title' in changes and not str(changes.get('title') or '').strip():
            return jsonify({'success': False, 'error': 'Collection titles cannot be blank.'}), 400
        if 'handle' in changes and not str(changes.get('handle') or '').strip():
            return jsonify({'success': False, 'error': 'Collection handles cannot be blank.'}), 400

    snapshot_id = _create_collection_edit_snapshot(shop, originals, retention_days=90)
    results = []
    success_count = 0
    for edit in edits:
        collection_id = edit.get('collection_id')
        expected_updated_at = edit.get('original_updated_at')
        try:
            live_updated_at = get_collection_updated_at(
                collection_id, shop_domain=shop.shop_domain, access_token=shop.access_token
            )
            if expected_updated_at and live_updated_at and expected_updated_at != live_updated_at:
                results.append({
                    'success': False,
                    'collection_id': collection_id,
                    'error': 'Collection changed in Shopify after this proposal was created. Refresh and review again.',
                })
                continue
            result = update_collection_metadata(
                collection_id,
                edit.get('changes') or {},
                shop_domain=shop.shop_domain,
                access_token=shop.access_token,
            )
        except Exception as exc:
            result = {'success': False, 'error': str(exc)}
        result['collection_id'] = collection_id
        results.append(result)
        if result.get('success'):
            success_count += 1
        time.sleep(0.2)

    cache_warning = None
    if success_count:
        state = ShopifyCatalogueCacheState.query.filter_by(shop_id=shop.id, user_id=current_user.id).first()
        if state:
            resources = json.loads(state.resources_json or '{}')
            try:
                resources['collections'] = list_collection_catalogue(
                    shop_domain=shop.shop_domain, access_token=shop.access_token
                )
                resources['collection_error'] = None
                state.resources_json = json.dumps(resources, ensure_ascii=False, separators=(',', ':'))
                state.last_incremental_sync_at = datetime.utcnow()
                db.session.commit()
            except Exception as refresh_exc:
                cache_warning = str(refresh_exc)
                logger.warning('Collection write succeeded but cache refresh failed: %s', refresh_exc)

    return jsonify({
        'success': success_count == len(edits),
        'success_count': success_count,
        'failed_count': len(edits) - success_count,
        'snapshot_id': snapshot_id,
        'cache_warning': cache_warning,
        'results': results,
    })



def _scan_products_for_broken_internal_links(shop, limit=10000, mode='both'):
    """Find products whose internal collection links are broken or missing.

    Read-only, no AI. Returns (items, unresolved_handles). Each item carries the
    corrected description so the caller can review before anything is written.
    ``mode`` is 'repair' (fix dead links), 'add' (backfill products that have no
    link) or 'both'.
    """
    handle_map = _get_collection_title_handle_map(
        shop_domain=shop.shop_domain, access_token=shop.access_token
    )
    if not handle_map:
        raise RuntimeError('Could not load the store collections needed to repair links.')
    repair_map = build_collection_link_repair_map(handle_map)

    items = []
    unresolved = []
    cursor = None
    scanned = 0
    while scanned < limit:
        page = list_products_for_seo_enhancement(
            limit=250,
            shop_domain=shop.shop_domain,
            access_token=shop.access_token,
            after_cursor=cursor,
            sort_key='UPDATED_AT',
            reverse=False,
            include_page_info=True,
        )
        products = page.get('products') or []
        if not products:
            break
        scanned += len(products)
        want_repair = mode in {'repair', 'both'}
        want_add = mode in {'add', 'both'}
        for product in products:
            body = product.get('body_html') or ''
            if '/collections/' in body:
                if not want_repair:
                    continue
                fixed_html, fixes, unresolved_handles = repair_internal_collection_links(
                    body, handle_map, repair_map
                )
                unresolved.extend(unresolved_handles)
                if not fixes:
                    continue
                items.append({
                    'kind': 'repair',
                    'product_id': product.get('id'),
                    'handle': product.get('handle'),
                    'title': product.get('title'),
                    'fixes': fixes,
                    'original_body_html': body,
                    'fixed_body_html': fixed_html,
                })
                continue

            # No internal link at all (published before the link sentence
            # existed). Add one from the collections the product is already in,
            # using their real handles. Nothing is invented.
            if not want_add or not body.strip():
                continue
            product_collections = [
                name.strip()
                for name in str(product.get('collections') or '').split(',')
                if name.strip() and name.strip() in handle_map
            ]
            if not product_collections:
                continue
            linked_html = _append_collection_links(body, product_collections, handle_map)
            if linked_html == body:
                continue
            items.append({
                'kind': 'add',
                'product_id': product.get('id'),
                'handle': product.get('handle'),
                'title': product.get('title'),
                'fixes': [{'from': '', 'to': handle_map[name], 'text': name}
                          for name in product_collections[:2]],
                'original_body_html': body,
                'fixed_body_html': linked_html,
            })
        page_info = page.get('page_info') or {}
        if not page_info.get('has_next_page'):
            break
        cursor = page_info.get('end_cursor')
    return items, sorted(set(unresolved))


@app.route('/api/shopify_products/internal_link_scan', methods=['POST'])
@login_required
def scan_product_internal_links():
    """Report products whose description links point at collection URLs that 404."""
    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected.'}), 400
    mode = (request.get_json(silent=True) or {}).get('mode') or 'both'
    if mode not in {'repair', 'add', 'both'}:
        return jsonify({'success': False, 'error': 'mode must be repair, add or both.'}), 400
    try:
        items, unresolved = _scan_products_for_broken_internal_links(shop, mode=mode)
    except Exception as exc:
        logger.error('Internal link scan failed: %s', exc)
        return jsonify({'success': False, 'error': str(exc)}), 500

    return jsonify({
        'success': True,
        'mode': mode,
        'affected_products': len(items),
        'to_repair': sum(1 for item in items if item.get('kind') == 'repair'),
        'to_add': sum(1 for item in items if item.get('kind') == 'add'),
        'broken_links': sum(len(item['fixes']) for item in items if item.get('kind') == 'repair'),
        'unresolved_handles': unresolved,
        'sample': [
            {
                'kind': item.get('kind'),
                'handle': item['handle'],
                'title': item['title'],
                'fixes': item['fixes'],
            }
            for item in items[:20]
        ],
    })


# ---------------------------------------------------------------------------
# Long-running catalogue maintenance jobs (link repair, SKU replace).
#
# These walk the whole catalogue and write to Shopify one product at a time, so
# they cannot run inside a single HTTP request: the web server would time the
# request out long before a few thousand products were done. They run on a
# background thread instead and report progress, which is why there is no
# 200-product cap any more.
# ---------------------------------------------------------------------------
maintenance_jobs = {}
maintenance_jobs_lock = threading.Lock()


class _MaintenanceShop:
    """Thread-safe snapshot of the shop credentials a job needs."""

    def __init__(self, shop):
        self.id = shop.id
        self.shop_domain = shop.shop_domain
        self.access_token = shop.access_token


def _maintenance_job_update(job_id, **fields):
    with maintenance_jobs_lock:
        job = maintenance_jobs.get(job_id)
        if job:
            job.update(fields)


def _prune_maintenance_jobs():
    cutoff = time.time() - 3600
    with maintenance_jobs_lock:
        for job_id in [
            key for key, job in maintenance_jobs.items()
            if job.get('finished_at') and job['finished_at'] < cutoff
        ]:
            maintenance_jobs.pop(job_id, None)


def _start_maintenance_job(kind, shop, user_id, runner, message=''):
    _prune_maintenance_jobs()
    job_id = str(uuid.uuid4())
    with maintenance_jobs_lock:
        maintenance_jobs[job_id] = {
            'job_id': job_id,
            'kind': kind,
            'user_id': user_id,
            'shop_id': shop.id,
            'status': 'starting',
            'message': message,
            'done': 0,
            'total': 0,
            'failed': 0,
            'failures': [],
            'snapshot_ids': [],
            'started_at': time.time(),
            'finished_at': None,
            'error': None,
        }

    def _wrapped():
        try:
            with app.app_context():
                runner(job_id)
        except Exception as exc:
            logger.error('Maintenance job %s (%s) failed: %s', job_id, kind, exc)
            _maintenance_job_update(
                job_id, status='failed', error=str(exc), finished_at=time.time()
            )
        else:
            with maintenance_jobs_lock:
                job = maintenance_jobs.get(job_id)
                if job and job.get('status') not in ('failed', 'complete'):
                    job['status'] = 'complete'
                    job['finished_at'] = time.time()

    threading.Thread(
        target=_wrapped, daemon=True, name='maintenance-%s-%s' % (kind, job_id[:8])
    ).start()
    return job_id


# ---------------------------------------------------------------------------
# AI proposals for many collections at once (the collection equivalent of the
# product bulk editor). Nothing is written here: the job only produces
# proposals, which go through the existing reviewed apply endpoint.
# ---------------------------------------------------------------------------
def _cached_collection_records(shop, user_id):
    from models import ShopifyCatalogueCacheState

    state = ShopifyCatalogueCacheState.query.filter_by(shop_id=shop.id, user_id=user_id).first()
    if not state:
        return []
    resources = json.loads(state.resources_json or '{}')
    return resources.get('collections') or []


def _sample_titles_for_collection(collection, shop, limit=12):
    """A few real product titles from inside the collection, for context."""
    handle = collection.get('handle') or ''
    if not handle:
        return []
    try:
        page = list_products_for_seo_enhancement(
            limit=limit,
            query_filter="collection:'%s'" % handle.replace("'", ""),
            shop_domain=shop.shop_domain,
            access_token=shop.access_token,
        )
    except Exception as exc:
        logger.warning('Could not read sample products for %s: %s', handle, exc)
        return []
    products = page if isinstance(page, list) else (page.get('products') or [])
    return [str(product.get('title') or '') for product in products if product.get('title')]


def _run_collection_ai_job(shop, user_id, collection_ids, custom_prompt, model_name):
    def runner(job_id):
        _maintenance_job_update(job_id, status='scanning', message='Reading collections...')
        records = {str(item.get('id')): item for item in _cached_collection_records(shop, user_id)}
        chosen = [records[str(cid)] for cid in collection_ids if str(cid) in records]
        missing = [cid for cid in collection_ids if str(cid) not in records]
        if missing:
            raise RuntimeError(
                'These collections are not in the cached list; refresh the collection cache first: %s'
                % ', '.join(str(m) for m in missing[:5])
            )
        _maintenance_job_update(
            job_id, status='running', total=len(chosen),
            message='Writing copy for %d collections...' % len(chosen),
        )

        proposals = []
        failures = []
        done = 0
        usage_total = {'calls': 0, 'prompt_tokens': 0, 'output_tokens': 0,
                       'thinking_tokens': 0, 'total_tokens': 0}
        for collection in chosen:
            try:
                sample_titles = _sample_titles_for_collection(collection, shop)
                result = generate_collection_metadata(
                    collection, custom_prompt=custom_prompt, model_name=model_name,
                    sample_products=sample_titles,
                )
                usage = result.pop('ai_usage', {}) or {}
                for key in usage_total:
                    usage_total[key] += int(usage.get(key) or 0)
                proposals.append({
                    'collection_id': collection.get('id'),
                    'title': collection.get('title') or '',
                    'handle': collection.get('handle') or '',
                    'original_updated_at': collection.get('updated_at') or '',
                    'before': {
                        'description_html': collection.get('description_html') or '',
                        'seo_title': collection.get('seo_title') or '',
                        'seo_description': collection.get('seo_description') or '',
                    },
                    'after': result,
                    'sample_products': sample_titles[:5],
                })
                done += 1
            except Exception as exc:
                # No fallback text is ever written: a collection that fails is
                # reported and left exactly as it was.
                failures.append({'handle': collection.get('handle') or collection.get('id'),
                                 'error': str(exc)})
            _maintenance_job_update(
                job_id, done=done, failed=len(failures), failures=failures[:20],
                proposals=proposals, ai_usage=usage_total,
                message='Written %d of %d collections.' % (done, len(chosen)),
            )

        _maintenance_job_update(
            job_id, status='complete', finished_at=time.time(), proposals=proposals,
            ai_usage=usage_total,
            message='Proposals ready for %d of %d collections.' % (done, len(chosen)),
        )

    return runner


@app.route('/api/shopify_collections/ai_proposals', methods=['POST'])
@login_required
def start_collection_ai_proposals():
    """Start an AI pass over several collections. Produces proposals only."""
    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected.'}), 400
    data = request.get_json(silent=True) or {}
    collection_ids = [str(value).strip() for value in (data.get('collection_ids') or []) if str(value).strip()]
    if not collection_ids:
        return jsonify({'success': False, 'error': 'Tick the collections you want to improve first.'}), 400
    if len(collection_ids) > 50:
        return jsonify({'success': False, 'error': 'Do at most 50 collections at a time.'}), 400
    if data.get('ai_cost_acknowledged') is not True:
        return jsonify({'success': False, 'error': 'Approve the estimated AI cost before starting.'}), 400
    model_name = str(data.get('model_name') or 'gemini-3.1-flash-lite').strip()
    if model_name not in {'gemini-3.1-flash-lite', 'gemini-3.5-flash'}:
        return jsonify({'success': False, 'error': 'Unsupported listing AI model.'}), 400
    if not os.environ.get('GEMINI_API_KEY'):
        return jsonify({'success': False, 'error': 'GEMINI_API_KEY is not set on the server.'}), 400

    job_id = _start_maintenance_job(
        'collection_ai', shop, current_user.id,
        _run_collection_ai_job(
            _MaintenanceShop(shop), current_user.id, collection_ids,
            data.get('custom_prompt') or '', model_name,
        ),
        message='Starting...',
    )
    return jsonify({'success': True, 'job_id': job_id})


# ---------------------------------------------------------------------------
# Compress text responses.
#
# The app page is about 1 MB of HTML, CSS and JavaScript. Sending it raw is the
# single largest use of outbound bandwidth, which Render bills for. Gzip cuts it
# by roughly 85% and costs nothing but a few milliseconds of CPU.
# ---------------------------------------------------------------------------
_COMPRESSIBLE_TYPES = (
    'text/html', 'text/css', 'text/plain', 'text/xml',
    'application/json', 'application/javascript', 'application/xml',
    'image/svg+xml',
)
_COMPRESS_MIN_BYTES = 1024


@app.after_request
def _compress_text_responses(response):
    try:
        if response.direct_passthrough or response.status_code >= 300:
            return response
        if 'gzip' not in (request.headers.get('Accept-Encoding') or '').lower():
            return response
        if response.headers.get('Content-Encoding'):
            return response
        content_type = (response.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        if content_type not in _COMPRESSIBLE_TYPES:
            return response
        data = response.get_data()
        if len(data) < _COMPRESS_MIN_BYTES:
            return response
        compressed = gzip.compress(data, compresslevel=6)
        if len(compressed) >= len(data):
            return response
        response.set_data(compressed)
        response.headers['Content-Encoding'] = 'gzip'
        response.headers['Content-Length'] = str(len(compressed))
        response.headers.add('Vary', 'Accept-Encoding')
    except Exception as exc:
        # Compression is an optimisation; never let it break a response.
        logger.warning('Response compression skipped: %s', exc)
    return response


@app.route('/api/shopify_products/maintenance_job/<job_id>', methods=['GET'])
@login_required
def maintenance_job_status(job_id):
    with maintenance_jobs_lock:
        job = copy.deepcopy(maintenance_jobs.get(job_id))
    if not job or job.get('user_id') != current_user.id:
        return jsonify({'success': False, 'error': 'Job not found.'}), 404
    job.pop('user_id', None)
    job['success'] = True
    return jsonify(job)


def _run_internal_link_repair_job(shop, user_id, mode, only, limit):
    def runner(job_id):
        _maintenance_job_update(job_id, status='scanning', message='Scanning the catalogue...')
        items, unresolved = _scan_products_for_broken_internal_links(shop, mode=mode)
        if only:
            items = [
                item for item in items
                if item.get('handle') in only or str(item.get('product_id')) in only
            ]
        if limit:
            items = items[:limit]
        _maintenance_job_update(
            job_id,
            status='running',
            total=len(items),
            unresolved_handles=unresolved,
            message='Updating %d products...' % len(items),
        )
        if not items:
            _maintenance_job_update(
                job_id, status='complete', finished_at=time.time(),
                message='Nothing to change: every internal collection link is already correct.',
            )
            return

        done = 0
        failures = []
        snapshot_ids = []
        # Snapshot in chunks so one enormous row is never written, and so a run
        # that stops half way still has recovery data for what it did touch.
        for chunk_start in range(0, len(items), 200):
            chunk = items[chunk_start:chunk_start + 200]
            snapshot_id = _create_bulk_edit_snapshot(
                shop,
                [
                    {
                        'id': item['product_id'],
                        'handle': item['handle'],
                        'title': item['title'],
                        'body_html': item['original_body_html'],
                    }
                    for item in chunk
                ],
                job_id=job_id,
                retention_days=90,
                user_id=user_id,
            )
            if snapshot_id:
                snapshot_ids.append(snapshot_id)
            _maintenance_job_update(job_id, snapshot_ids=list(snapshot_ids))

            for item in chunk:
                try:
                    result = update_product_seo_metadata(
                        item['product_id'],
                        {'body_html': item['fixed_body_html']},
                        shop_domain=shop.shop_domain,
                        access_token=shop.access_token,
                    )
                except Exception as exc:
                    result = {'success': False, 'error': str(exc)}
                if result.get('success'):
                    done += 1
                else:
                    failures.append({'handle': item['handle'], 'error': result.get('error')})
                _maintenance_job_update(
                    job_id,
                    done=done,
                    failed=len(failures),
                    failures=failures[:20],
                    message='Updated %d of %d products.' % (done, len(items)),
                )
                time.sleep(0.2)

        _maintenance_job_update(
            job_id,
            status='complete',
            finished_at=time.time(),
            message='Updated %d of %d products.' % (done, len(items)),
        )

    return runner


@app.route('/api/shopify_products/internal_link_repair', methods=['POST'])
@login_required
def repair_product_internal_links():
    """Repoint broken internal collection links after explicit confirmation.

    Only the href value changes. 90-day recovery snapshots of every original
    description are written before each chunk of products is touched. The work
    runs on a background thread so the whole catalogue can be done in one go.
    """
    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected.'}), 400

    data = request.get_json(silent=True) or {}
    mode = data.get('mode') or 'both'
    if mode not in ('repair', 'add', 'both'):
        return jsonify({'success': False, 'error': 'mode must be repair, add or both.'}), 400
    # Optional narrowing: run on a chosen selection, or on the first N only, so a
    # change can be proved on one product before the whole catalogue is touched.
    only = set(str(value).strip() for value in (data.get('only') or []) if str(value).strip())
    try:
        limit = int(data.get('limit') or 0)
    except (TypeError, ValueError):
        limit = 0
    limit = max(0, limit)

    planned = int(data.get('planned_products') or 0)
    expected = 'REPAIR %d PRODUCT%s' % (planned, '' if planned == 1 else 'S')
    if not planned or data.get('confirmation') != expected:
        return jsonify({
            'success': False,
            'needs_confirmation': True,
            'expected_confirmation': expected,
            'error': 'Type %s exactly to confirm.' % expected,
        }), 400

    job_id = _start_maintenance_job(
        'internal_links', shop, current_user.id,
        _run_internal_link_repair_job(_MaintenanceShop(shop), current_user.id, mode, only, limit),
        message='Starting...',
    )
    return jsonify({'success': True, 'job_id': job_id})


# ---------------------------------------------------------------------------
# Tag cleanup
#
# Tags grow wild: the same idea ends up written three ways, half in capitals.
# This reads every tag in the catalogue, asks the AI for a tidy spelling, shows
# the whole plan for review, and only then rewrites products - and the rules of
# any automated collection that was built on a tag that moved.
# ---------------------------------------------------------------------------
def _split_tags(value):
    """Split a Shopify tag string into a clean, order-preserving list."""
    if isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        raw = str(value or '').split(',')
    seen = set()
    tags = []
    for item in raw:
        tag = item.strip()
        if not tag:
            continue
        if tag.lower() in seen:
            continue
        seen.add(tag.lower())
        tags.append(tag)
    return tags


def _collect_catalogue_tags(shop):
    """Return every tag in the catalogue with how many products carry it."""
    counts = {}
    products = 0
    for product in _iter_maintenance_products(shop, sort_key='UPDATED_AT', reverse=False):
        products += 1
        for tag in _split_tags(product.get('tags')):
            entry = counts.setdefault(tag, {'tag': tag, 'count': 0})
            entry['count'] += 1
    return {
        'tags': sorted(counts.values(), key=lambda row: (-row['count'], row['tag'].lower())),
        'products_scanned': products,
    }


def _tag_collection_usage(shop):
    """Map each tag to the automated collections whose rules depend on it."""
    usage = {}
    catalogue = list_collection_catalogue(
        shop_domain=shop.shop_domain, access_token=shop.access_token) or []
    for collection in catalogue:
        rule_set = collection.get('rule_set') or {}
        for rule in rule_set.get('rules') or []:
            if str(rule.get('column') or '').upper() != 'TAG':
                continue
            condition = str(rule.get('condition') or '').strip()
            if not condition:
                continue
            usage.setdefault(condition.lower(), []).append({
                'id': collection.get('id'),
                'title': collection.get('title'),
                'handle': collection.get('handle'),
                'rule_set': rule_set,
            })
    return usage


@app.route('/api/shopify_tags/scan', methods=['POST'])
@login_required
def scan_shopify_tags():
    """List every tag, how often it is used, and what depends on it."""
    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected.'}), 400
    maintenance_shop = _MaintenanceShop(shop)
    try:
        found = _collect_catalogue_tags(maintenance_shop)
        usage = _tag_collection_usage(maintenance_shop)
    except Exception as exc:
        logger.error('Tag scan failed: %s', exc)
        return jsonify({'success': False, 'error': str(exc)}), 500

    tags = []
    for row in found['tags']:
        collections = usage.get(row['tag'].lower()) or []
        tags.append({
            'tag': row['tag'],
            'count': row['count'],
            'collections': [{'title': item['title'], 'handle': item['handle']} for item in collections],
        })
    return jsonify({
        'success': True,
        'tags': tags,
        'tag_count': len(tags),
        'products_scanned': found['products_scanned'],
        'collection_linked': sum(1 for row in tags if row['collections']),
    })


def _run_tag_plan_job(shop, user_id, case_style, merge_mode, custom_prompt, model_name, only_tags):
    """Ask the AI for a tidy spelling of every tag. Writes nothing."""
    def runner(job_id):
        _maintenance_job_update(job_id, status='scanning', message='Reading every tag...')
        found = _collect_catalogue_tags(shop)
        usage = _tag_collection_usage(shop)
        tags = found['tags']
        if only_tags:
            wanted = {str(tag).strip().lower() for tag in only_tags if str(tag).strip()}
            tags = [row for row in tags if row['tag'].lower() in wanted]
        if not tags:
            _maintenance_job_update(
                job_id, status='complete', finished_at=time.time(),
                message='No tags found to tidy.', result={'renames': []},
            )
            return

        # Sorted alphabetically and sent in blocks, so tags that are near
        # duplicates of each other land in the same block and can be merged.
        ordered = sorted(tags, key=lambda row: row['tag'].lower())
        chunk_size = 300
        chunks = [ordered[index:index + chunk_size] for index in range(0, len(ordered), chunk_size)]
        _maintenance_job_update(
            job_id, status='running', total=len(chunks),
            message='Planning %d tags...' % len(ordered),
        )

        renames = []
        usage_totals = {'calls': 0, 'prompt_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}
        for index, chunk in enumerate(chunks):
            # No fallback: one bad block stops the run rather than producing a
            # half-finished plan that looks complete.
            plan = generate_tag_cleanup_plan(
                chunk,
                case_style=case_style,
                merge_mode=merge_mode,
                custom_prompt=custom_prompt,
                model_name=model_name,
            )
            renames.extend(plan.get('renames') or [])
            for key in usage_totals:
                usage_totals[key] += int((plan.get('ai_usage') or {}).get(key) or 0)
            _maintenance_job_update(
                job_id, done=index + 1,
                message='Planned %d of %d blocks of tags.' % (index + 1, len(chunks)),
            )

        counts = {row['tag']: row['count'] for row in tags}
        proposals = []
        for rename in renames:
            collections = usage.get(rename['from'].lower()) or []
            proposals.append({
                'from': rename['from'],
                'to': rename['to'],
                'reason': rename.get('reason') or '',
                'products': counts.get(rename['from'], 0),
                'collections': [
                    {'title': item['title'], 'handle': item['handle']} for item in collections
                ],
            })
        proposals.sort(key=lambda row: (-row['products'], row['from'].lower()))

        merges = {}
        for row in proposals:
            merges.setdefault(row['to'].lower(), []).append(row['from'])
        _maintenance_job_update(
            job_id, status='complete', finished_at=time.time(),
            message='%d tags would change.' % len(proposals),
            result={
                'renames': proposals,
                'tags_reviewed': len(tags),
                'merge_count': sum(1 for group in merges.values() if len(group) > 1),
                'ai_usage': usage_totals,
            },
        )

    return runner


@app.route('/api/shopify_tags/plan', methods=['POST'])
@login_required
def plan_shopify_tag_cleanup():
    """Start the AI planning pass. Nothing is written to Shopify."""
    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected.'}), 400
    data = request.get_json(silent=True) or {}
    case_style = str(data.get('case_style') or 'sentence').strip().lower()
    if case_style not in TAG_CASE_STYLES:
        return jsonify({'success': False, 'error': 'Unknown capitalisation style.'}), 400
    merge_mode = str(data.get('merge_mode') or 'synonyms').strip().lower()
    if merge_mode not in {'exact', 'synonyms', 'full'}:
        return jsonify({'success': False, 'error': 'Unknown merge setting.'}), 400
    only_tags = data.get('only') or []

    job_id = _start_maintenance_job(
        'tag_plan', shop, current_user.id,
        _run_tag_plan_job(
            _MaintenanceShop(shop), current_user.id, case_style, merge_mode,
            str(data.get('custom_prompt') or ''), data.get('model') or None, only_tags,
        ),
        message='Starting...',
    )
    return jsonify({'success': True, 'job_id': job_id})


def _apply_tag_map_to_list(tags, tag_map):
    """Return the new tag list for one product, keeping the original order."""
    result = []
    seen = set()
    for tag in tags:
        new_tag = tag_map.get(tag.lower(), tag)
        if new_tag.lower() in seen:
            # A merge can bring two tags to the same name; the product keeps one.
            continue
        seen.add(new_tag.lower())
        result.append(new_tag)
    return result


def _run_tag_apply_job(shop, user_id, renames):
    """Rewrite tags across the catalogue, then repair the collections that used them."""
    def runner(job_id):
        tag_map = {}
        for rename in renames:
            source = str((rename or {}).get('from') or '').strip()
            target = str((rename or {}).get('to') or '').strip()
            if source and target and source.lower() != target.lower():
                tag_map[source.lower()] = target
            elif source and target:
                # Capitals-only change: still a real change worth making.
                tag_map[source.lower()] = target

        _maintenance_job_update(job_id, status='scanning', message='Finding products to update...')
        pending = []
        for product in _iter_maintenance_products(shop, sort_key='UPDATED_AT', reverse=False):
            current = _split_tags(product.get('tags'))
            if not current:
                continue
            updated = _apply_tag_map_to_list(current, tag_map)
            if updated == current:
                continue
            pending.append({
                'id': product.get('id'),
                'handle': product.get('handle'),
                'title': product.get('title'),
                'original_tags': ', '.join(current),
                'new_tags': updated,
            })

        _maintenance_job_update(
            job_id, status='running', total=len(pending),
            message='Updating %d products...' % len(pending),
        )

        done = 0
        failures = []
        snapshot_ids = []
        for chunk_start in range(0, len(pending), 200):
            chunk = pending[chunk_start:chunk_start + 200]
            snapshot_id = _create_bulk_edit_snapshot(
                shop,
                [
                    {
                        'id': item['id'],
                        'handle': item['handle'],
                        'title': item['title'],
                        'tags': item['original_tags'],
                    }
                    for item in chunk
                ],
                job_id=job_id,
                retention_days=90,
                user_id=user_id,
            )
            if snapshot_id:
                snapshot_ids.append(snapshot_id)
            _maintenance_job_update(job_id, snapshot_ids=list(snapshot_ids))

            for item in chunk:
                try:
                    result = update_product_seo_metadata(
                        item['id'],
                        {'tags': item['new_tags']},
                        shop_domain=shop.shop_domain,
                        access_token=shop.access_token,
                    )
                except Exception as exc:
                    result = {'success': False, 'error': str(exc)}
                if result.get('success'):
                    done += 1
                else:
                    failures.append({'handle': item['handle'], 'error': result.get('error')})
                _maintenance_job_update(
                    job_id, done=done, failed=len(failures), failures=failures[:20],
                    message='Updated %d of %d products.' % (done, len(pending)),
                )
                time.sleep(0.2)

        # A smart collection built on a renamed tag would quietly empty itself,
        # so its rule moves with the tag.
        collections_fixed = []
        collection_failures = []
        try:
            usage = _tag_collection_usage(shop)
        except Exception as exc:
            usage = {}
            collection_failures.append({'handle': '', 'error': 'Could not read collections: %s' % exc})

        repaired = {}
        for old_tag, new_tag in tag_map.items():
            for collection in usage.get(old_tag) or []:
                entry = repaired.setdefault(collection['id'], {
                    'id': collection['id'],
                    'title': collection['title'],
                    'handle': collection['handle'],
                    'rule_set': copy.deepcopy(collection['rule_set']),
                    'changes': [],
                })
                for rule in entry['rule_set'].get('rules') or []:
                    if str(rule.get('column') or '').upper() != 'TAG':
                        continue
                    if str(rule.get('condition') or '').strip().lower() != old_tag:
                        continue
                    rule['condition'] = new_tag
                    entry['changes'].append({'from': old_tag, 'to': new_tag})

        for entry in repaired.values():
            if not entry['changes']:
                continue
            try:
                result = update_collection_rules(
                    entry['id'], entry['rule_set'],
                    shop_domain=shop.shop_domain, access_token=shop.access_token,
                )
            except Exception as exc:
                result = {'success': False, 'error': str(exc)}
            if result.get('success'):
                collections_fixed.append({'title': entry['title'], 'handle': entry['handle'],
                                          'changes': entry['changes']})
            else:
                collection_failures.append({'handle': entry['handle'], 'error': result.get('error')})

        message = 'Updated %d of %d products.' % (done, len(pending))
        if collections_fixed:
            message += ' Repaired %d collection rule%s.' % (
                len(collections_fixed), '' if len(collections_fixed) == 1 else 's')
        if collection_failures:
            message += ' %d collection rule%s could not be updated.' % (
                len(collection_failures), '' if len(collection_failures) == 1 else 's')
        _maintenance_job_update(
            job_id, status='complete', finished_at=time.time(), message=message,
            result={
                'products_updated': done,
                'collections_fixed': collections_fixed,
                'collection_failures': collection_failures,
            },
        )

    return runner


@app.route('/api/shopify_tags/apply', methods=['POST'])
@login_required
def apply_shopify_tag_cleanup():
    """Apply an approved tag plan after an explicit typed confirmation."""
    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected.'}), 400
    data = request.get_json(silent=True) or {}
    renames = [item for item in (data.get('renames') or []) if isinstance(item, dict)]
    clean = []
    for item in renames:
        source = str(item.get('from') or '').strip()
        target = str(item.get('to') or '').strip()
        if not source or not target or source == target:
            continue
        clean.append({'from': source, 'to': target})
    if not clean:
        return jsonify({'success': False, 'error': 'No tag changes to apply.'}), 400

    expected = 'CHANGE %d TAG%s' % (len(clean), '' if len(clean) == 1 else 'S')
    if data.get('confirmation') != expected:
        return jsonify({
            'success': False,
            'needs_confirmation': True,
            'expected_confirmation': expected,
            'error': 'Type %s exactly to confirm.' % expected,
        }), 400

    job_id = _start_maintenance_job(
        'tag_apply', shop, current_user.id,
        _run_tag_apply_job(_MaintenanceShop(shop), current_user.id, clean),
        message='Starting...',
    )
    return jsonify({'success': True, 'job_id': job_id})


# ---------------------------------------------------------------------------
# Tags into collection pages
#
# A tag has no page of its own on Shopify: no title, no description, no SEO
# fields. An automated collection does. So the tags get grouped into real
# collection pages driven by tag rules, and products join by carrying a tag -
# which is also how existing listings are sorted into a new page afterwards.
# ---------------------------------------------------------------------------
def _collection_tag_for_title(title):
    """The tag that drives a generated collection, derived from its title."""
    cleaned = re.sub(r'[^A-Za-z0-9 ]+', ' ', str(title or '')).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    if not cleaned:
        raise ValueError('A collection needs a title before it can have a tag.')
    return cleaned[:1].upper() + cleaned[1:].lower()


def _existing_collection_titles(shop):
    try:
        catalogue = list_collection_catalogue(
            shop_domain=shop.shop_domain, access_token=shop.access_token) or []
    except Exception as exc:
        logger.warning('Could not read collections while planning: %s', exc)
        return [], {}
    titles = [str(item.get('title') or '').strip() for item in catalogue if item.get('title')]
    by_title = {title.lower(): item for title, item in
                zip(titles, catalogue) if title}
    return titles, by_title


def _run_collection_group_plan_job(shop, user_id, minimum_products, custom_prompt, model_name):
    """Group the store's tags into proposed collection pages. Writes nothing."""
    def runner(job_id):
        _maintenance_job_update(job_id, status='scanning', message='Reading every tag...')
        found = _collect_catalogue_tags(shop)
        tags = found['tags']
        if not tags:
            _maintenance_job_update(
                job_id, status='complete', finished_at=time.time(),
                message='No tags found, so there is nothing to group.',
                result={'collections': []},
            )
            return

        titles, by_title = _existing_collection_titles(shop)
        _maintenance_job_update(
            job_id, status='running', total=1,
            message='Grouping %d tags into collections...' % len(tags),
        )
        plan = generate_collection_groups(
            tags,
            minimum_products=minimum_products,
            custom_prompt=custom_prompt,
            existing_collections=titles,
            model_name=model_name,
        )
        proposals = []
        for collection in plan.get('collections') or []:
            existing = by_title.get(collection['title'].lower())
            collection['exists'] = bool(existing)
            collection['existing_handle'] = (existing or {}).get('handle') or ''
            collection['existing_id'] = (existing or {}).get('id') or ''
            collection['collection_tag'] = _collection_tag_for_title(collection['title'])
            proposals.append(collection)
        proposals.sort(key=lambda row: -row.get('estimated_products', 0))

        _maintenance_job_update(
            job_id, status='complete', finished_at=time.time(),
            message='%d collections proposed.' % len(proposals),
            result={
                'collections': proposals,
                'tags_reviewed': len(tags),
                'ai_usage': plan.get('ai_usage') or {},
            },
        )

    return runner


@app.route('/api/shopify_collections/tag_group_plan', methods=['POST'])
@login_required
def plan_collections_from_tags():
    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected.'}), 400
    data = request.get_json(silent=True) or {}
    try:
        minimum_products = max(1, int(data.get('minimum_products') or 8))
    except (TypeError, ValueError):
        minimum_products = 8
    job_id = _start_maintenance_job(
        'collection_group_plan', shop, current_user.id,
        _run_collection_group_plan_job(
            _MaintenanceShop(shop), current_user.id, minimum_products,
            str(data.get('custom_prompt') or ''), data.get('model') or None,
        ),
        message='Starting...',
    )
    return jsonify({'success': True, 'job_id': job_id})


def _run_collection_group_apply_job(shop, user_id, collections, publish):
    """Create or refresh the approved collection pages."""
    def runner(job_id):
        _maintenance_job_update(
            job_id, status='running', total=len(collections),
            message='Building %d collections...' % len(collections),
        )
        _, by_title = _existing_collection_titles(shop)
        publication_id = ''
        if publish:
            try:
                publication_id = get_online_store_publication_id(
                    shop_domain=shop.shop_domain, access_token=shop.access_token)
            except Exception as exc:
                logger.warning('Could not read the Online Store channel: %s', exc)

        done = 0
        failures = []
        created = []
        for collection in collections:
            title = str(collection.get('title') or '').strip()
            tags = [str(tag).strip() for tag in collection.get('tags') or [] if str(tag).strip()]
            collection_tag = str(collection.get('collection_tag') or '').strip()
            if collection_tag and collection_tag not in tags:
                # The page also answers to its own tag, so listings sorted into
                # it later join by carrying that one tag.
                tags.append(collection_tag)
            existing = by_title.get(title.lower())
            payload = {
                'title': title,
                'tags': tags,
                'description_html': collection.get('description_html') or '',
                'seo_title': collection.get('seo_title') or '',
                'seo_description': collection.get('seo_description') or '',
                'image_url': collection.get('image_url') or '',
                'image_alt': collection.get('image_alt') or title,
            }
            try:
                if existing and existing.get('id'):
                    result = update_collection_metadata(
                        existing['id'],
                        {
                            'description_html': payload['description_html'],
                            'seo_title': payload['seo_title'],
                            'seo_description': payload['seo_description'],
                        },
                        shop_domain=shop.shop_domain, access_token=shop.access_token,
                    )
                    if result.get('success') and existing.get('rule_set'):
                        rule_set = copy.deepcopy(existing['rule_set'])
                        known = {str(rule.get('condition') or '').strip().lower()
                                 for rule in rule_set.get('rules') or []
                                 if str(rule.get('column') or '').upper() == 'TAG'}
                        for tag in tags:
                            if tag.lower() not in known:
                                rule_set.setdefault('rules', []).append(
                                    {'column': 'TAG', 'relation': 'EQUALS', 'condition': tag})
                        rule_set['appliedDisjunctively'] = True
                        result = update_collection_rules(
                            existing['id'], rule_set,
                            shop_domain=shop.shop_domain, access_token=shop.access_token,
                        )
                    collection_id = existing['id']
                    handle = existing.get('handle') or ''
                else:
                    result = create_smart_collection(
                        payload, shop_domain=shop.shop_domain, access_token=shop.access_token)
                    collection_id = ((result.get('collection') or {}).get('id') or '')
                    handle = ((result.get('collection') or {}).get('handle') or '')
            except Exception as exc:
                result = {'success': False, 'error': str(exc)}
                collection_id = ''
                handle = ''

            if result.get('success'):
                done += 1
                if publish and publication_id and collection_id:
                    try:
                        publish_collection_to_online_store(
                            collection_id, publication_id,
                            shop_domain=shop.shop_domain, access_token=shop.access_token)
                    except Exception as exc:
                        logger.warning('Could not publish %s: %s', title, exc)
                created.append({
                    'title': title,
                    'handle': handle,
                    'id': collection_id,
                    'collection_tag': collection_tag,
                    'existed': bool(existing),
                })
            else:
                failures.append({'handle': title, 'error': result.get('error')})
            _maintenance_job_update(
                job_id, done=done, failed=len(failures), failures=failures[:20],
                message='Built %d of %d collections.' % (done, len(collections)),
            )
            time.sleep(0.2)

        try:
            shopify_graphql._store_config_cache.clear()
        except Exception:
            pass
        _maintenance_job_update(
            job_id, status='complete', finished_at=time.time(),
            message='Built %d of %d collections.' % (done, len(collections)),
            result={'collections': created},
        )

    return runner


@app.route('/api/shopify_collections/tag_group_apply', methods=['POST'])
@login_required
def apply_collections_from_tags():
    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected.'}), 400
    data = request.get_json(silent=True) or {}
    collections = [item for item in (data.get('collections') or []) if isinstance(item, dict)]
    clean = []
    for item in collections:
        title = str(item.get('title') or '').strip()
        tags = [str(tag).strip() for tag in item.get('tags') or [] if str(tag).strip()]
        if not title or not tags:
            continue
        entry = dict(item)
        entry['title'] = title
        entry['tags'] = tags
        entry['collection_tag'] = str(item.get('collection_tag') or '').strip() or _collection_tag_for_title(title)
        clean.append(entry)
    if not clean:
        return jsonify({'success': False, 'error': 'No collections to build.'}), 400

    expected = 'BUILD %d COLLECTION%s' % (len(clean), '' if len(clean) == 1 else 'S')
    if data.get('confirmation') != expected:
        return jsonify({
            'success': False,
            'needs_confirmation': True,
            'expected_confirmation': expected,
            'error': 'Type %s exactly to confirm.' % expected,
        }), 400

    job_id = _start_maintenance_job(
        'collection_group_apply', shop, current_user.id,
        _run_collection_group_apply_job(
            _MaintenanceShop(shop), current_user.id, clean, bool(data.get('publish', True))),
        message='Starting...',
    )
    return jsonify({'success': True, 'job_id': job_id})


def _product_colour_hint(product):
    """The colour recorded on a product, if the store keeps one."""
    for key in ('shopify.color-pattern', 'custom.color', 'mm-google-shopping.color'):
        value = ((product.get('metafields') or {}).get(key) or {}).get('value')
        if value:
            return str(value)
    return ''


def _run_collection_fill_job(shop, user_id, collections, custom_prompt, model_name):
    """Read the catalogue and tag the listings that belong in each collection."""
    def runner(job_id):
        _maintenance_job_update(job_id, status='scanning', message='Reading the catalogue...')
        catalogue = []
        for product in _iter_maintenance_products(shop, sort_key='UPDATED_AT', reverse=False):
            body = re.sub(r'<[^>]+>', ' ', str(product.get('body_html') or ''))
            catalogue.append({
                'id': product.get('id'),
                'handle': product.get('handle'),
                'title': product.get('title'),
                'summary': re.sub(r'\s+', ' ', body).strip()[:280],
                'colour': _product_colour_hint(product),
                'tags': _split_tags(product.get('tags')),
            })
        if not catalogue:
            _maintenance_job_update(
                job_id, status='complete', finished_at=time.time(),
                message='No products found.', result={'assignments': []},
            )
            return

        _maintenance_job_update(
            job_id, status='running', total=len(collections),
            message='Sorting %d products into %d collections...' % (len(catalogue), len(collections)),
        )

        by_handle = {item['handle']: item for item in catalogue if item.get('handle')}
        additions = {}
        assignments = []
        done = 0
        for collection in collections:
            title = str(collection.get('title') or '').strip()
            collection_tag = str(collection.get('collection_tag') or '').strip() or _collection_tag_for_title(title)
            summary = re.sub(r'<[^>]+>', ' ', str(collection.get('description_html') or ''))
            summary = re.sub(r'\s+', ' ', summary).strip()[:400]

            chosen = []
            # The catalogue goes in blocks so a big store does not have to fit
            # into one request. No fallback: a failed block stops the job.
            for start in range(0, len(catalogue), 120):
                block = catalogue[start:start + 120]
                result = choose_products_for_collection(
                    title, summary, block,
                    custom_prompt=custom_prompt, model_name=model_name,
                )
                chosen.extend(result.get('handles') or [])

            added = []
            for handle in chosen:
                product = by_handle.get(handle)
                if not product:
                    continue
                if any(tag.lower() == collection_tag.lower() for tag in product['tags']):
                    continue
                additions.setdefault(handle, []).append(collection_tag)
                added.append(handle)
            assignments.append({
                'title': title,
                'collection_tag': collection_tag,
                'matched': len(chosen),
                'to_add': len(added),
            })
            done += 1
            _maintenance_job_update(
                job_id, done=done,
                message='Sorted %d of %d collections.' % (done, len(collections)),
            )

        pending = []
        for handle, tags_to_add in additions.items():
            product = by_handle.get(handle)
            if not product:
                continue
            new_tags = list(product['tags'])
            for tag in tags_to_add:
                if not any(existing.lower() == tag.lower() for existing in new_tags):
                    new_tags.append(tag)
            pending.append({
                'id': product['id'],
                'handle': handle,
                'title': product['title'],
                'original_tags': ', '.join(product['tags']),
                'new_tags': new_tags,
            })

        _maintenance_job_update(
            job_id, total=len(pending), done=0,
            message='Tagging %d products...' % len(pending),
        )

        written = 0
        failures = []
        snapshot_ids = []
        for chunk_start in range(0, len(pending), 200):
            chunk = pending[chunk_start:chunk_start + 200]
            snapshot_id = _create_bulk_edit_snapshot(
                shop,
                [
                    {'id': item['id'], 'handle': item['handle'], 'title': item['title'],
                     'tags': item['original_tags']}
                    for item in chunk
                ],
                job_id=job_id, retention_days=90, user_id=user_id,
            )
            if snapshot_id:
                snapshot_ids.append(snapshot_id)
            _maintenance_job_update(job_id, snapshot_ids=list(snapshot_ids))
            for item in chunk:
                try:
                    result = update_product_seo_metadata(
                        item['id'], {'tags': item['new_tags']},
                        shop_domain=shop.shop_domain, access_token=shop.access_token,
                    )
                except Exception as exc:
                    result = {'success': False, 'error': str(exc)}
                if result.get('success'):
                    written += 1
                else:
                    failures.append({'handle': item['handle'], 'error': result.get('error')})
                _maintenance_job_update(
                    job_id, done=written, failed=len(failures), failures=failures[:20],
                    message='Tagged %d of %d products.' % (written, len(pending)),
                )
                time.sleep(0.2)

        _maintenance_job_update(
            job_id, status='complete', finished_at=time.time(),
            message='Tagged %d products across %d collections.' % (written, len(collections)),
            result={'assignments': assignments, 'products_tagged': written},
        )

    return runner


@app.route('/api/shopify_collections/fill_from_catalogue', methods=['POST'])
@login_required
def fill_collections_from_catalogue():
    """Sort existing listings into collections by tagging the ones that fit."""
    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected.'}), 400
    data = request.get_json(silent=True) or {}
    collections = [item for item in (data.get('collections') or []) if isinstance(item, dict)]
    clean = []
    for item in collections:
        title = str(item.get('title') or '').strip()
        if not title:
            continue
        entry = dict(item)
        entry['title'] = title
        entry['collection_tag'] = str(item.get('collection_tag') or '').strip() or _collection_tag_for_title(title)
        clean.append(entry)
    if not clean:
        return jsonify({'success': False, 'error': 'No collections to fill.'}), 400

    expected = 'FILL %d COLLECTION%s' % (len(clean), '' if len(clean) == 1 else 'S')
    if data.get('confirmation') != expected:
        return jsonify({
            'success': False,
            'needs_confirmation': True,
            'expected_confirmation': expected,
            'error': 'Type %s exactly to confirm.' % expected,
        }), 400

    job_id = _start_maintenance_job(
        'collection_fill', shop, current_user.id,
        _run_collection_fill_job(
            _MaintenanceShop(shop), current_user.id, clean,
            str(data.get('custom_prompt') or ''), data.get('model') or None),
        message='Starting...',
    )
    return jsonify({'success': True, 'job_id': job_id})


# ---------------------------------------------------------------------------
# SKU find and replace
# ---------------------------------------------------------------------------
def _iter_maintenance_products(shop, sort_key='CREATED_AT', reverse=True, max_products=0):
    """Yield catalogue products newest-first (or oldest-first), page by page."""
    cursor = None
    seen = 0
    while True:
        page = list_products_for_seo_enhancement(
            limit=250,
            shop_domain=shop.shop_domain,
            access_token=shop.access_token,
            after_cursor=cursor,
            sort_key=sort_key,
            reverse=reverse,
            include_page_info=True,
        )
        products = page.get('products') or []
        if not products:
            return
        for product in products:
            yield product
            seen += 1
            if max_products and seen >= max_products:
                return
        page_info = page.get('page_info') or {}
        if not page_info.get('has_next_page'):
            return
        cursor = page_info.get('end_cursor')


def _plan_sku_replacements(shop, find_text, replace_text, scope, only=None, recent_count=0):
    """Work out exactly which variant SKUs would change, and to what.

    Nothing is guessed: a variant is only listed when its current SKU really
    contains find_text, and the new value is that same SKU with the matched
    text swapped out.
    """
    only = set(str(value).strip() for value in (only or []) if str(value).strip())
    pattern = re.compile(re.escape(find_text), re.IGNORECASE)
    max_products = recent_count if scope == 'recent' else 0
    items = []
    for product in _iter_maintenance_products(
        shop, sort_key='CREATED_AT', reverse=True, max_products=max_products
    ):
        if scope == 'selected':
            if not (product.get('handle') in only or str(product.get('id')) in only):
                continue
        changes = []
        for variant in (product.get('variants') or []):
            sku = variant.get('sku') or ''
            if not sku or not variant.get('id') or not pattern.search(sku):
                continue
            new_sku = pattern.sub(replace_text, sku)
            if new_sku == sku:
                continue
            changes.append({
                'id': variant['id'],
                'variant_title': variant.get('title') or '',
                'before': sku,
                'sku': new_sku,
            })
        if changes:
            items.append({
                'product_id': product.get('id'),
                'handle': product.get('handle'),
                'title': product.get('title'),
                'variants': product.get('variants') or [],
                'changes': changes,
            })
    return items


def _sku_scope_from_request(data):
    scope = (data.get('scope') or 'recent').strip()
    if scope not in ('recent', 'selected', 'all'):
        raise ValueError('scope must be recent, selected or all.')
    find_text = str(data.get('find') or '').strip()
    if not find_text:
        raise ValueError('Enter the wrong SKU text to look for.')
    replace_text = str(data.get('replace') or '').strip()
    only = data.get('only') or []
    if scope == 'selected' and not only:
        raise ValueError('Tick the products you want to change first.')
    try:
        recent_count = int(data.get('recent_count') or 0)
    except (TypeError, ValueError):
        recent_count = 0
    if scope == 'recent' and recent_count <= 0:
        raise ValueError('Enter how many of the most recent products to check.')
    return find_text, replace_text, scope, only, max(0, recent_count)


@app.route('/api/shopify_products/sku_replace_scan', methods=['POST'])
@login_required
def scan_product_sku_replacements():
    """Preview a SKU find-and-replace. Nothing is written to Shopify."""
    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected.'}), 400
    try:
        find_text, replace_text, scope, only, recent_count = _sku_scope_from_request(
            request.get_json(silent=True) or {}
        )
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    try:
        items = _plan_sku_replacements(
            _MaintenanceShop(shop), find_text, replace_text, scope, only, recent_count
        )
    except Exception as exc:
        logger.error('SKU replace scan failed: %s', exc)
        return jsonify({'success': False, 'error': str(exc)}), 500

    sample = []
    for item in items[:20]:
        for change in item['changes'][:3]:
            sample.append({
                'handle': item['handle'],
                'title': item['title'],
                'variant_title': change['variant_title'],
                'before': change['before'],
                'after': change['sku'],
            })
    return jsonify({
        'success': True,
        'affected_products': len(items),
        'affected_variants': sum(len(item['changes']) for item in items),
        'find': find_text,
        'replace': replace_text,
        'scope': scope,
        'sample': sample,
    })


def _run_sku_replace_job(shop, user_id, find_text, replace_text, scope, only, recent_count):
    def runner(job_id):
        _maintenance_job_update(job_id, status='scanning', message='Checking SKUs...')
        items = _plan_sku_replacements(shop, find_text, replace_text, scope, only, recent_count)
        _maintenance_job_update(
            job_id, status='running', total=len(items),
            message='Updating %d products...' % len(items),
        )
        if not items:
            _maintenance_job_update(
                job_id, status='complete', finished_at=time.time(),
                message='No SKU contains that text, so nothing was changed.',
            )
            return

        done = 0
        failures = []
        snapshot_ids = []
        for chunk_start in range(0, len(items), 200):
            chunk = items[chunk_start:chunk_start + 200]
            snapshot_id = _create_bulk_edit_snapshot(
                shop,
                [
                    {
                        'id': item['product_id'],
                        'handle': item['handle'],
                        'title': item['title'],
                        'variants': item['variants'],
                    }
                    for item in chunk
                ],
                job_id=job_id,
                retention_days=90,
                user_id=user_id,
            )
            if snapshot_id:
                snapshot_ids.append(snapshot_id)
            _maintenance_job_update(job_id, snapshot_ids=list(snapshot_ids))

            for item in chunk:
                result = update_variant_skus_graphql(
                    item['product_id'],
                    [{'id': change['id'], 'sku': change['sku']} for change in item['changes']],
                    shop_domain=shop.shop_domain,
                    access_token=shop.access_token,
                )
                if result.get('success'):
                    done += 1
                else:
                    failures.append({'handle': item['handle'], 'error': result.get('error')})
                _maintenance_job_update(
                    job_id, done=done, failed=len(failures), failures=failures[:20],
                    message='Updated %d of %d products.' % (done, len(items)),
                )
                time.sleep(0.2)

        _maintenance_job_update(
            job_id, status='complete', finished_at=time.time(),
            message='Updated %d of %d products.' % (done, len(items)),
        )

    return runner


@app.route('/api/shopify_products/sku_replace', methods=['POST'])
@login_required
def replace_product_skus():
    """Swap wrong text inside variant SKUs after an explicit typed confirmation."""
    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected.'}), 400
    data = request.get_json(silent=True) or {}
    try:
        find_text, replace_text, scope, only, recent_count = _sku_scope_from_request(data)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400

    planned = int(data.get('planned_products') or 0)
    expected = 'CHANGE SKU ON %d PRODUCT%s' % (planned, '' if planned == 1 else 'S')
    if not planned or data.get('confirmation') != expected:
        return jsonify({
            'success': False,
            'needs_confirmation': True,
            'expected_confirmation': expected,
            'error': 'Type %s exactly to confirm.' % expected,
        }), 400

    job_id = _start_maintenance_job(
        'sku_replace', shop, current_user.id,
        _run_sku_replace_job(
            _MaintenanceShop(shop), current_user.id,
            find_text, replace_text, scope, only, recent_count,
        ),
        message='Starting...',
    )
    return jsonify({'success': True, 'job_id': job_id})





# ---------------------------------------------------------------------------
# Shopify webhooks: the cached catalogue updates itself the moment Shopify
# changes, instead of waiting for someone to press "Update changed products".
# ---------------------------------------------------------------------------
webhook_activity = {}
webhook_activity_lock = threading.Lock()


def _record_webhook_activity(shop_domain, topic, detail=''):
    with webhook_activity_lock:
        entry = webhook_activity.setdefault(shop_domain, {'received': 0, 'applied': 0, 'failed': 0})
        entry['received'] += 1
        entry['last_topic'] = topic
        entry['last_at'] = datetime.now().isoformat()
        if detail:
            entry['last_detail'] = detail


def _record_webhook_result(shop_domain, ok, detail=''):
    with webhook_activity_lock:
        entry = webhook_activity.setdefault(shop_domain, {'received': 0, 'applied': 0, 'failed': 0})
        entry['applied' if ok else 'failed'] += 1
        if detail:
            entry['last_detail'] = detail


def _refresh_cached_product_from_shopify(shop_id, user_id, shop_domain, access_token, product_gid):
    """Re-read one product from Shopify and rewrite its cached row.

    The webhook body is deliberately ignored as a source of truth: it is in a
    different shape from the catalogue cache, and re-reading is the only way to
    be certain the cache matches what Shopify actually holds.
    """
    from models import ShopifyCachedProduct, ShopifyCatalogueCacheState

    state = ShopifyCatalogueCacheState.query.filter_by(shop_id=shop_id, user_id=user_id).first()
    if not state:
        return 'no cache yet'

    numeric_id = str(product_gid).rsplit('/', 1)[-1]
    page = list_products_for_seo_enhancement(
        limit=1,
        query_filter='id:%s' % numeric_id,
        shop_domain=shop_domain,
        access_token=access_token,
    )
    products = page if isinstance(page, list) else (page.get('products') or [])
    if not products:
        return 'product not found in Shopify'

    product = products[0]
    row = ShopifyCachedProduct.query.filter_by(
        shop_id=shop_id, generation=state.active_generation, product_gid=str(product.get('id') or ''),
    ).first()
    if not row:
        max_position = (db.session.query(db.func.max(ShopifyCachedProduct.position))
                        .filter(ShopifyCachedProduct.shop_id == shop_id,
                                ShopifyCachedProduct.generation == state.active_generation)
                        .scalar() or -1)
        row = ShopifyCachedProduct(
            shop_id=shop_id, generation=state.active_generation,
            product_gid=str(product.get('id') or ''), position=max_position + 1,
        )
        db.session.add(row)
        state.product_count = int(state.product_count or 0) + 1
    row.handle = str(product.get('handle') or '')
    row.shopify_updated_at = str(product.get('updated_at') or '')
    row.payload_compressed = _encode_catalogue_payload(product)
    row.cached_at = datetime.utcnow()
    if row.shopify_updated_at > (state.last_shopify_updated_at or ''):
        state.last_shopify_updated_at = row.shopify_updated_at
    db.session.commit()
    return 'cached %s' % (row.handle or numeric_id)


def _delete_cached_product(shop_id, user_id, product_gid):
    from models import ShopifyCachedProduct, ShopifyCatalogueCacheState

    state = ShopifyCatalogueCacheState.query.filter_by(shop_id=shop_id, user_id=user_id).first()
    if not state:
        return 'no cache yet'
    numeric_id = str(product_gid).rsplit('/', 1)[-1]
    removed = ShopifyCachedProduct.query.filter(
        ShopifyCachedProduct.shop_id == shop_id,
        ShopifyCachedProduct.generation == state.active_generation,
        ShopifyCachedProduct.product_gid.like('%' + numeric_id),
    ).delete(synchronize_session=False)
    if removed:
        state.product_count = max(0, int(state.product_count or 0) - removed)
    db.session.commit()
    return 'removed %d cached row(s)' % removed


def _refresh_cached_collections(shop_id, user_id, shop_domain, access_token):
    """Re-read the collection audit list after any collection changes."""
    from models import ShopifyCatalogueCacheState
    import shopify_graphql as _sg

    # The collection title/handle directory is cached in-process for speed; a
    # new or renamed collection must not stay invisible to the AI pickers.
    with _sg._store_config_cache_lock:
        for key in [k for k in _sg._store_config_cache if 'collection' in str(k).lower()]:
            _sg._store_config_cache.pop(key, None)

    state = ShopifyCatalogueCacheState.query.filter_by(shop_id=shop_id, user_id=user_id).first()
    if not state:
        return 'collection cache cleared'
    resources = json.loads(state.resources_json or '{}')
    resources['collections'] = list_collection_catalogue(
        shop_domain=shop_domain, access_token=access_token
    )
    resources['collection_error'] = None
    state.resources_json = json.dumps(resources, ensure_ascii=False, separators=(',', ':'))
    db.session.commit()
    return 'refreshed %d collections' % len(resources['collections'])


def _handle_shopify_webhook(shop_domain, topic, payload):
    """Apply one verified webhook. Runs on a background thread."""
    from models import Shop

    with app.app_context():
        shop = Shop.query.filter_by(shop_domain=shop_domain, is_active=True).first()
        if not shop:
            logger.info('Webhook for %s ignored: store is not connected here.', shop_domain)
            return
        object_id = payload.get('admin_graphql_api_id') or payload.get('id')
        try:
            if topic in ('products/create', 'products/update'):
                detail = _refresh_cached_product_from_shopify(
                    shop.id, shop.user_id, shop.shop_domain, shop.access_token, object_id
                )
            elif topic == 'products/delete':
                detail = _delete_cached_product(shop.id, shop.user_id, object_id)
            elif topic.startswith('collections/'):
                detail = _refresh_cached_collections(
                    shop.id, shop.user_id, shop.shop_domain, shop.access_token
                )
            else:
                detail = 'topic ignored'
            _record_webhook_result(shop_domain, True, detail)
            logger.info('Webhook %s for %s: %s', topic, shop_domain, detail)
        except Exception as exc:
            db.session.rollback()
            _record_webhook_result(shop_domain, False, str(exc))
            logger.error('Webhook %s for %s failed: %s', topic, shop_domain, exc)


@app.route('/webhooks/shopify', methods=['POST'])
def shopify_webhook_endpoint():
    """Receive a Shopify webhook, verify it, and answer immediately.

    Shopify gives a webhook five seconds to be acknowledged and retries when it
    is not, so the actual work is handed to a background thread.
    """
    raw_body = request.get_data()
    if not verify_shopify_webhook(raw_body, request.headers.get('X-Shopify-Hmac-Sha256')):
        # An unverified body is not from Shopify and is never acted on.
        logger.warning('Rejected an unverified Shopify webhook.')
        return ('', 401)

    topic = (request.headers.get('X-Shopify-Topic') or '').strip().lower()
    shop_domain = (request.headers.get('X-Shopify-Shop-Domain') or '').strip().lower()
    try:
        payload = json.loads(raw_body.decode('utf-8') or '{}')
    except ValueError:
        payload = {}
    _record_webhook_activity(shop_domain, topic)
    threading.Thread(
        target=_handle_shopify_webhook,
        args=(shop_domain, topic, payload),
        daemon=True,
        name='webhook-%s' % topic.replace('/', '-'),
    ).start()
    return ('', 200)


@app.route('/api/shopify_webhooks/status', methods=['GET'])
@login_required
def shopify_webhooks_status():
    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected.'}), 400
    expected = shopify_webhook_callback_url(os.environ.get('APP_URL') or request.url_root)
    try:
        subscriptions = list_shopify_webhooks(
            shop_domain=shop.shop_domain, access_token=shop.access_token
        )
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500
    active = [s for s in subscriptions if s.get('callback_url') == expected]
    missing = [topic for topic in SHOPIFY_WEBHOOK_TOPICS
               if topic not in {s.get('topic') for s in active}]
    with webhook_activity_lock:
        activity = copy.deepcopy(webhook_activity.get(shop.shop_domain) or {})
    return jsonify({
        'success': True,
        'callback_url': expected,
        'active_topics': sorted({s.get('topic') for s in active}),
        'missing_topics': missing,
        'other_urls': sorted({s.get('callback_url') for s in subscriptions
                              if s.get('callback_url') != expected}),
        'activity': activity,
    })


@app.route('/api/shopify_webhooks/register', methods=['POST'])
@login_required
def shopify_webhooks_register():
    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected.'}), 400
    if not os.environ.get('SHOPIFY_API_SECRET', '').strip():
        return jsonify({
            'success': False,
            'error': 'SHOPIFY_API_SECRET is not set on the server, so webhooks could not be verified. '
                     'Set it before registering.',
        }), 400
    callback_url = shopify_webhook_callback_url(os.environ.get('APP_URL') or request.url_root)
    if callback_url.startswith('http://'):
        return jsonify({
            'success': False,
            'error': 'Shopify only sends webhooks to https URLs. Set APP_URL to the public https address.',
        }), 400
    try:
        result = ensure_shopify_webhooks(
            callback_url, shop_domain=shop.shop_domain, access_token=shop.access_token
        )
    except Exception as exc:
        logger.error('Webhook registration failed: %s', exc)
        return jsonify({'success': False, 'error': str(exc)}), 500
    return jsonify(result)



def _run_shopify_catalogue_incremental_job(job_id, shop_id, user_id, shop_domain, access_token):
    from models import ShopifyCachedProduct, ShopifyCatalogueCacheState
    try:
        with app.app_context():
            state = ShopifyCatalogueCacheState.query.filter_by(shop_id=shop_id, user_id=user_id).first()
            if not state:
                raise RuntimeError('Run a full catalogue sync before using incremental refresh.')
            since = state.last_shopify_updated_at or (state.last_full_sync_at.isoformat() + 'Z' if state.last_full_sync_at else '')
            query_filter = "updated_at:>='%s'" % since if since else ''
            page = list_products_for_seo_enhancement(
                limit=10000,
                query_filter=query_filter,
                shop_domain=shop_domain,
                access_token=access_token,
                sort_key='UPDATED_AT',
                reverse=False,
                include_page_info=True,
            )
            if (page.get('page_info') or {}).get('has_next_page'):
                raise RuntimeError('More than 10,000 products changed; use Full rebuild for a safe reconciliation.')
            products = page.get('products') or []
            max_position = (db.session.query(db.func.max(ShopifyCachedProduct.position))
                            .filter(ShopifyCachedProduct.shop_id == shop_id,
                                    ShopifyCachedProduct.generation == state.active_generation)
                            .scalar() or -1)
            newest_updated_at = state.last_shopify_updated_at or ''
            added = 0
            for product in products:
                product_gid = str(product.get('id') or '')
                if not product_gid:
                    continue
                row = ShopifyCachedProduct.query.filter_by(
                    shop_id=shop_id, generation=state.active_generation, product_gid=product_gid
                ).first()
                if not row:
                    max_position += 1
                    row = ShopifyCachedProduct(
                        shop_id=shop_id, generation=state.active_generation,
                        product_gid=product_gid, position=max_position,
                    )
                    db.session.add(row)
                    added += 1
                row.handle = str(product.get('handle') or '')
                row.shopify_updated_at = str(product.get('updated_at') or '')
                row.payload_compressed = _encode_catalogue_payload(product)
                row.cached_at = datetime.utcnow()
                newest_updated_at = max(newest_updated_at, row.shopify_updated_at)
            state.product_count = int(state.product_count or 0) + added
            resources = json.loads(state.resources_json or '{}')
            try:
                resources['collections'] = list_collection_catalogue(
                    shop_domain=shop_domain, access_token=access_token
                )
                resources['collection_error'] = None
            except Exception as collection_exc:
                resources['collection_error'] = str(collection_exc)
                logger.warning('Incremental collection audit refresh failed: %s', collection_exc)
            state.resources_json = json.dumps(resources, ensure_ascii=False, separators=(',', ':'))
            state.sync_status = 'ready'
            state.last_error = None
            state.last_incremental_sync_at = datetime.utcnow()
            state.last_shopify_updated_at = newest_updated_at or state.last_shopify_updated_at
            db.session.commit()
            with shopify_catalogue_bulk_jobs_lock:
                job = shopify_catalogue_bulk_jobs.get(job_id)
                if job:
                    job.update({'status': 'ready', 'count': state.product_count,
                                'changed_count': len(products), 'cache': _catalogue_cache_public(state),
                                'updated_at': datetime.now().isoformat()})
    except Exception as exc:
        logger.error('Incremental catalogue refresh %s failed: %s', job_id, exc)
        with app.app_context():
            state = ShopifyCatalogueCacheState.query.filter_by(shop_id=shop_id, user_id=user_id).first()
            if state:
                state.sync_status = 'error'
                state.last_error = str(exc)
                db.session.commit()
        with shopify_catalogue_bulk_jobs_lock:
            job = shopify_catalogue_bulk_jobs.get(job_id)
            if job:
                job.update({'status': 'error', 'error': str(exc), 'updated_at': datetime.now().isoformat()})


@app.route('/api/shopify_bulk_catalogue/cache/incremental', methods=['POST'])
@login_required
def start_shopify_catalogue_incremental():
    from models import ShopifyCatalogueCacheState
    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected.'}), 400
    state = ShopifyCatalogueCacheState.query.filter_by(shop_id=shop.id, user_id=current_user.id).first()
    if not state:
        return jsonify({'success': False, 'error': 'Run a full catalogue sync first.'}), 409
    state.sync_status = 'incremental_syncing'
    state.last_error = None
    db.session.commit()
    job_id = str(uuid.uuid4())
    with shopify_catalogue_bulk_jobs_lock:
        shopify_catalogue_bulk_jobs[job_id] = {
            'job_id': job_id, 'kind': 'incremental', 'user_id': current_user.id, 'shop_id': shop.id,
            'shop_domain': shop.shop_domain, 'status': 'incremental_syncing',
            'count': state.product_count, 'object_count': 0, 'created_ts': time.time(),
            'created_at': datetime.now().isoformat(), 'updated_at': datetime.now().isoformat(),
        }
    threading.Thread(
        target=_run_shopify_catalogue_incremental_job,
        args=(job_id, shop.id, current_user.id, shop.shop_domain, shop.access_token),
        daemon=True,
        name=f'shopify-incremental-{job_id[:8]}',
    ).start()
    return jsonify({'success': True, 'job_id': job_id, 'status': 'incremental_syncing'})


listing_stats_cache = {}
listing_stats_cache_lock = threading.Lock()
LISTING_STATS_CATALOGUE_TTL = 10 * 60
LISTING_STATS_ANALYTICS_TTL = 30 * 60


def _listing_stats_catalogue_key(shop):
    return f"catalogue:{shop.id}:{shop.shop_domain}"


def _listing_stats_range_key(shop, selected_range):
    return f"analytics:{shop.id}:{shop.shop_domain}:{selected_range}"


def _listing_stats_status_payload(entry):
    entry = entry or {}
    return {
        'analytics_status': entry.get('status', 'idle'),
        'analytics_error': entry.get('error'),
        'analytics_updated_at': entry.get('updated_at'),
    }


def _run_listing_stats_analytics(shop_id, shop_domain, access_token, selected_range, products):
    key = f"analytics:{shop_id}:{shop_domain}:{selected_range}"
    try:
        sales_rows, traffic_rows = load_analytics(
            shop_domain, access_token, selected_range
        )
        listings = merge_listing_stats(products, sales_rows, traffic_rows)
        with listing_stats_cache_lock:
            listing_stats_cache[key] = {
                'status': 'ready',
                'listings': listings,
                'updated_at': datetime.now().isoformat(),
                'cached_at': time.time(),
                'error': None,
            }
    except Exception as exc:
        logger.warning("Listing stats analytics background job failed for %s: %s", shop_domain, exc)
        with listing_stats_cache_lock:
            listing_stats_cache[key] = {
                'status': 'error',
                'listings': None,
                'updated_at': datetime.now().isoformat(),
                'cached_at': time.time(),
                'error': str(exc),
            }


@app.route('/api/shopify_listing_stats_v2', methods=['GET'])
@login_required
def shopify_listing_stats_v2():
    """Return the catalogue immediately and hydrate analytics in the background."""
    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected. Connect a store first.'}), 400

    selected_range = normalize_range((request.args.get('range') or '').strip())
    refresh = (request.args.get('refresh') or '').lower() in ('1', 'true', 'yes')
    status_only = (request.args.get('status_only') or '').lower() in ('1', 'true', 'yes')
    catalogue_key = _listing_stats_catalogue_key(shop)
    analytics_key = _listing_stats_range_key(shop, selected_range)
    now = time.time()

    granted_scopes = {
        scope.strip()
        for scope in str(shop.scope or '').replace(',', ' ').split()
        if scope.strip()
    }
    reports_known_missing = (
        bool(granted_scopes)
        and 'custom_app' not in granted_scopes
        and 'read_reports' not in granted_scopes
    )

    with listing_stats_cache_lock:
        analytics_entry = copy.deepcopy(listing_stats_cache.get(analytics_key) or {})
        if refresh and analytics_entry.get('status') != 'running':
            listing_stats_cache.pop(analytics_key, None)
            analytics_entry = {}

    if status_only:
        return jsonify({
            'success': True,
            'range': selected_range,
            'needs_reports_reconnect': reports_known_missing,
            **_listing_stats_status_payload(analytics_entry),
        })

    with listing_stats_cache_lock:
        catalogue_entry = copy.deepcopy(listing_stats_cache.get(catalogue_key) or {})
    catalogue_stale = (
        not catalogue_entry.get('products')
        or now - float(catalogue_entry.get('cached_at') or 0) > LISTING_STATS_CATALOGUE_TTL
    )
    if refresh or catalogue_stale:
        try:
            currency, products = load_catalogue(shop.shop_domain, shop.access_token)
        except Exception as exc:
            logger.error("Listing stats catalogue failed: %s", exc)
            return jsonify({'success': False, 'error': str(exc)}), 502
        catalogue_entry = {
            'currency': currency,
            'products': products,
            'cached_at': now,
            'updated_at': datetime.now().isoformat(),
        }
        with listing_stats_cache_lock:
            listing_stats_cache[catalogue_key] = copy.deepcopy(catalogue_entry)
    else:
        currency = catalogue_entry.get('currency') or 'GBP'
        products = catalogue_entry.get('products') or []

    with listing_stats_cache_lock:
        analytics_entry = copy.deepcopy(listing_stats_cache.get(analytics_key) or {})
    analytics_stale = (
        analytics_entry.get('status') == 'ready'
        and now - float(analytics_entry.get('cached_at') or 0) > LISTING_STATS_ANALYTICS_TTL
    )
    if analytics_stale:
        analytics_entry = {}

    if reports_known_missing:
        analytics_entry = {'status': 'permission_required', 'error': None}
    elif analytics_entry.get('status') not in ('running', 'ready'):
        analytics_entry = {
            'status': 'running',
            'error': None,
            'updated_at': datetime.now().isoformat(),
            'cached_at': now,
        }
        with listing_stats_cache_lock:
            listing_stats_cache[analytics_key] = copy.deepcopy(analytics_entry)
        thread = threading.Thread(
            target=_run_listing_stats_analytics,
            args=(shop.id, shop.shop_domain, shop.access_token, selected_range, copy.deepcopy(products)),
            daemon=True,
            name=f"listing-stats-{shop.id}-{selected_range}",
        )
        thread.start()

    listings = analytics_entry.get('listings')
    if not listings:
        listings = [
            dict(product, visits=None, orders=None, units_sold=None, net_sales=None, conversion_rate=None)
            for product in products
        ]

    return jsonify({
        'success': True,
        'range': selected_range,
        'shop': {
            'id': shop.id,
            'name': shop.shop_name or shop.shop_domain,
            'domain': shop.shop_domain,
            'currency': currency,
        },
        'listings': listings,
        'catalogue_updated_at': catalogue_entry.get('updated_at'),
        'needs_reports_reconnect': reports_known_missing,
        **_listing_stats_status_payload(analytics_entry),
        'traffic_definition': (
            'Human online-store sessions whose first page was this product. '
            'Shopify does not expose an Etsy-style lifetime view counter on each product.'
        ),
    })


@app.route('/api/shopify_listing_stats', methods=['GET'])
@login_required
def shopify_listing_stats():
    """Return a read-only product performance spreadsheet for the active shop."""
    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected. Connect a store first.'}), 400

    selected_range = normalize_range((request.args.get('range') or '').strip())
    granted_scopes = {
        scope.strip()
        for scope in str(shop.scope or '').replace(',', ' ').split()
        if scope.strip()
    }
    # Env-token custom apps do not expose their granted scopes in our DB, so
    # attempt the report query and surface Shopify's answer. OAuth connections
    # avoid a guaranteed permission error until the user reconnects once.
    reports_known_missing = (
        bool(granted_scopes)
        and 'custom_app' not in granted_scopes
        and 'read_reports' not in granted_scopes
    )

    try:
        currency, products = load_catalogue(shop.shop_domain, shop.access_token)
    except Exception as exc:
        logger.error(f"Listing stats catalogue failed: {exc}")
        return jsonify({'success': False, 'error': str(exc)}), 502

    analytics_error = None
    listings = [
        dict(product, visits=None, orders=None, units_sold=None, net_sales=None, conversion_rate=None)
        for product in products
    ]
    if not reports_known_missing:
        try:
            sales_rows, traffic_rows = load_analytics(
                shop.shop_domain, shop.access_token, selected_range
            )
            listings = merge_listing_stats(products, sales_rows, traffic_rows)
        except Exception as exc:
            analytics_error = str(exc)
            logger.warning(f"Listing stats analytics unavailable for {shop.shop_domain}: {exc}")

    return jsonify({
        'success': True,
        'range': selected_range,
        'shop': {
            'id': shop.id,
            'name': shop.shop_name or shop.shop_domain,
            'domain': shop.shop_domain,
            'currency': currency,
        },
        'listings': listings,
        'needs_reports_reconnect': reports_known_missing,
        'analytics_error': analytics_error,
        'traffic_definition': (
            'Human online-store sessions whose first page was this product. '
            'Shopify does not expose an Etsy-style lifetime view counter on each product.'
        ),
    })

@app.route('/api/shopify_assign_main_media_to_variants', methods=['POST'])
@login_required
def shopify_assign_main_media_to_variants():
    """Attach each selected product's main media item to its variants."""
    try:
        shop = get_current_shop()
        if not shop:
            return jsonify({'success': False, 'error': 'No Shopify store connected. Connect a store first.'}), 400

        data = request.get_json() or {}
        products = data.get('products') or []
        if not products:
            return jsonify({'success': False, 'error': 'No products selected'}), 400

        results = []
        success_count = 0
        failed_count = 0
        for product in products:
            product_id = product.get('id') or product.get('product_id')
            media = product.get('media') or []
            variants = product.get('variants') or []
            main_media_id = product.get('main_media_id') or ((media[0] or {}).get('id') if media else '')
            variant_ids = [v.get('id') for v in variants if v.get('id')]
            result = assign_media_to_product_variants(
                product_id,
                main_media_id,
                variant_ids,
                shop_domain=shop.shop_domain,
                access_token=shop.access_token,
            )
            result['title'] = product.get('title') or product.get('handle') or product_id or 'Unknown product'
            result['product_id'] = product_id
            results.append(result)
            if result.get('success'):
                success_count += 1
            else:
                failed_count += 1
            time.sleep(0.15)

        return jsonify({
            'success': failed_count == 0,
            'success_count': success_count,
            'failed_count': failed_count,
            'results': results,
        })
    except Exception as e:
        logger.error(f"Assign main media to variants error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/shopify_product_reference_search', methods=['GET'])
@login_required
def shopify_product_reference_search():
    """Search Shopify products for reference metafield selectors."""
    try:
        shop = get_current_shop()
        if not shop:
            return jsonify({'success': False, 'error': 'No Shopify store connected. Connect a store first.'}), 400

        search = (request.args.get('q') or '').strip()
        try:
            limit = int(request.args.get('limit', 20))
        except (TypeError, ValueError):
            limit = 20
        products = search_products_for_references(
            search=search,
            limit=limit,
            shop_domain=shop.shop_domain,
            access_token=shop.access_token,
        )
        return jsonify({'success': True, 'products': products})
    except Exception as e:
        logger.error(f"Shopify product reference search error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

def is_safe_image_url(url):
    """Validate image URL to prevent SSRF attacks - blocks internal/private IPs"""
    from urllib.parse import urlparse
    import ipaddress
    import socket
    
    try:
        parsed = urlparse(url)
        
        if parsed.scheme not in ('http', 'https'):
            return False
        
        hostname = parsed.hostname
        if not hostname:
            return False
        
        blocked_hostnames = ['localhost', 'localhost.localdomain', '127.0.0.1', '0.0.0.0', '::1']
        if hostname.lower() in blocked_hostnames:
            return False
        
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
                return False
        except ValueError:
            pass
        
        private_prefixes = ['10.', '192.168.', '172.16.', '172.17.', '172.18.', '172.19.', 
                           '172.20.', '172.21.', '172.22.', '172.23.', '172.24.', '172.25.',
                           '172.26.', '172.27.', '172.28.', '172.29.', '172.30.', '172.31.', '169.254.']
        for prefix in private_prefixes:
            if hostname.startswith(prefix):
                return False
        
        return True
        
    except Exception:
        return False

csv_enhance_jobs = {}
csv_enhance_jobs_lock = threading.Lock()


def _csv_enhance_jobs_path():
    temp_dir = temp_file_service.get_temp_dir()
    os.makedirs(temp_dir, exist_ok=True)
    return os.path.join(temp_dir, 'csv_enhance_jobs.json')


def _save_csv_enhance_jobs():
    try:
        path = _csv_enhance_jobs_path()
        with csv_enhance_jobs_lock:
            payload = copy.deepcopy(csv_enhance_jobs)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as e:
        logger.error(f"Failed to save CSV enhance jobs: {e}")


def _load_csv_enhance_jobs():
    try:
        path = _csv_enhance_jobs_path()
        if not os.path.exists(path):
            return
        with open(path, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
        for job in loaded.values():
            if job.get('status') in ('processing', 'applying'):
                job['status'] = 'paused'
                job['current_step'] = 'Paused after server restart. Resume the job to continue.'
            job.pop('_thread_running', None)
        with csv_enhance_jobs_lock:
            csv_enhance_jobs.clear()
            csv_enhance_jobs.update(loaded)
        logger.info(f"Loaded {len(loaded)} CSV enhance jobs from disk")
    except Exception as e:
        logger.error(f"Failed to load CSV enhance jobs: {e}")


def _csv_job_public(job):
    public_job = copy.deepcopy(job)
    public_job.pop('_thread_running', None)
    return public_job


def _append_csv_job_log(job, message, level='info'):
    logs = job.setdefault('logs', [])
    logs.append({
        'time': datetime.now().isoformat(),
        'level': level,
        'message': message,
    })
    if len(logs) > 120:
        del logs[:len(logs) - 120]


def _bulk_job_shop_mismatch(job, shop):
    """Prevent a resumable job from being applied to a different active shop."""
    bound_shop_id = job.get('shop_id')
    return bool(bound_shop_id and (not shop or str(bound_shop_id) != str(shop.id)))


def _bulk_restore_payload(product):
    """Convert an imported catalogue row into the shape accepted by the updater."""
    product = copy.deepcopy(product or {})
    variants = product.get('variants') or []
    media = product.get('media') or []
    restored = {
        'product_id': product.get('id') or product.get('product_id') or '',
        'handle': product.get('handle') or '',
        'status': product.get('status') or '',
        'title': product.get('title') or '',
        'body_html': product.get('body_html') or product.get('descriptionHtml') or '',
        'tags': product.get('tags') or '',
        'vendor': product.get('vendor') or '',
        'product_type': product.get('product_type') or product.get('type') or '',
        'product_category': product.get('product_category') or '',
        'category_gid': product.get('category_gid') or '',
        'seo_title': product.get('seo_title') or '',
        'seo_description': product.get('seo_description') or '',
        'image_alt_text': product.get('image_alt_text') or '',
        'collections': product.get('collections') or '',
        'variants_json': json.dumps(variants, ensure_ascii=False),
        'media_json': json.dumps(media, ensure_ascii=False),
        'price_lists_json': product.get('price_lists_json') or '[]',
        'variant_mode': 'merge',
        'original_variant_ids': [v.get('id') for v in variants if v.get('id')],
    }
    for identifier, metafield in (product.get('metafields') or {}).items():
        metafield = metafield or {}
        namespace = metafield.get('namespace') or str(identifier).split('.')[0]
        key = metafield.get('key') or '.'.join(str(identifier).split('.')[1:])
        if namespace and key:
            restored[f'mf__{namespace}__{key}'] = metafield.get('value') or ''
            restored[f'mf_type__{namespace}__{key}'] = metafield.get('type') or 'single_line_text_field'
    return restored


def _create_bulk_edit_snapshot(shop, originals, job_id=None, retention_days=90, user_id=None):
    """Persist a compressed, shop-bound before-state before any Shopify write."""
    import base64
    import zlib
    from datetime import timedelta
    from models import BulkEditSnapshot

    originals = [copy.deepcopy(item) for item in (originals or []) if item]
    if not originals:
        return None
    payload = {
        'version': 1,
        'shop_domain': shop.shop_domain,
        'created_at': datetime.now().isoformat(),
        'retention_days': retention_days,
        'products': originals,
        'restore_payloads': [_bulk_restore_payload(item) for item in originals],
    }
    encoded = base64.b64encode(
        zlib.compress(json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8'), level=9)
    ).decode('ascii')
    now = datetime.now()
    snapshot = BulkEditSnapshot(
        id=str(uuid.uuid4()),
        job_id=job_id,
        user_id=user_id if user_id is not None else current_user.id,
        shop_id=shop.id,
        shop_domain=shop.shop_domain,
        item_count=len(originals),
        payload_compressed=encoded,
        status='saved',
        created_at=now,
        expires_at=now + timedelta(days=retention_days),
    )
    BulkEditSnapshot.query.filter(BulkEditSnapshot.expires_at < now).delete(synchronize_session=False)
    db.session.add(snapshot)
    db.session.commit()
    return snapshot.id


def _decode_bulk_edit_snapshot(snapshot):
    import base64
    import zlib
    return json.loads(zlib.decompress(base64.b64decode(snapshot.payload_compressed)).decode('utf-8'))


def _resolve_enhanced_category(enhanced):
    """Resolve an edited category name to the Shopify taxonomy GID required by productUpdate."""
    category_name = (enhanced.get('product_category') or '').strip()
    if category_name:
        enhanced['category_gid'] = resolve_category_name_to_gid(category_name) or enhanced.get('category_gid', '')
    return enhanced


def _apply_category_attribute_metafields(enhanced, shop_domain=None, access_token=None):
    """Best-effort write of Shopify standard category metafields from AI picks."""
    product_id = enhanced.get('product_id')
    category_gid = enhanced.get('category_gid')
    picks = enhanced.get('category_attribute_picks')
    if not product_id or not category_gid or not isinstance(picks, dict) or not picks:
        return
    try:
        enrich_product_with_category_metafields(
            product_gid=product_id,
            category_gid=category_gid,
            category_attribute_picks=picks,
            shop_domain=shop_domain,
            access_token=access_token,
        )
    except Exception as exc:
        logger.warning("Bulk category metafield apply failed for %s: %s", product_id, exc)


def _shopify_csv_row_to_product(row, row_index):
    """Normalize a Shopify CSV product row while preserving every source column."""
    return {
        'row_index': row_index,
        'handle': row.get('Handle', '').strip(),
        'title': row.get('Title', ''),
        'body_html': row.get('Body (HTML)', ''),
        'vendor': row.get('Vendor', ''),
        'product_category': row.get('Product Category', ''),
        'type': row.get('Type', ''),
        'tags': row.get('Tags', ''),
        'status': row.get('Status', ''),
        'collections': row.get('Collection', ''),
        'image_url': row.get('Image Src', '').strip(),
        'image_alt_text': row.get('Image Alt Text', ''),
        'seo_title': row.get('SEO Title', ''),
        'seo_description': row.get('SEO Description', ''),
        'google_product_category': row.get('Google Shopping / Google Product Category', ''),
        'gender': row.get('Google Shopping / Gender', ''),
        'age_group': row.get('Google Shopping / Age Group', ''),
        'condition': row.get('Google Shopping / Condition', ''),
        'custom_product': row.get('Google Shopping / Custom Product', ''),
        'custom_label_0': row.get('Google Shopping / Custom Label 0', ''),
        'custom_label_1': row.get('Google Shopping / Custom Label 1', ''),
        'custom_label_2': row.get('Google Shopping / Custom Label 2', ''),
        'custom_label_3': row.get('Google Shopping / Custom Label 3', ''),
        'custom_label_4': row.get('Google Shopping / Custom Label 4', ''),
        'metafield_color': row.get('Metafield: custom.color [single_line_text_field]', '') or row.get('Color (product.metafields.custom.color)', ''),
        'metafield_theme': row.get('Metafield: custom.theme [single_line_text_field]', '') or row.get('Theme (product.metafields.custom.theme)', ''),
        'metafield_frame_style': row.get('Metafield: custom.frame_style [single_line_text_field]', ''),
        'metafield_condition': row.get('Metafield: custom.condition [single_line_text_field]', ''),
        'metafield_decoration_material': row.get('Metafield: custom.decoration_material [single_line_text_field]', ''),
        'metafield_artwork_frame_material': row.get('Metafield: custom.artwork_frame_material [single_line_text_field]', ''),
        'source_columns': row,
    }


def _generic_metafields_from_product(product):
    """Return dynamic metafield edit keys from a Shopify API product payload."""
    generic = {}
    for mf in (product.get('metafields') or {}).values():
        namespace = mf.get('namespace') or ''
        key = mf.get('key') or ''
        if namespace and key:
            generic[f"mf__{namespace}__{key}"] = mf.get('value') or ''
            generic[f"mf_type__{namespace}__{key}"] = mf.get('type') or 'single_line_text_field'
    return generic


def _bulk_product_type(product):
    """Return a non-empty Shopify product type for CSV/API enhancement rows."""
    return (
        product.get('product_type')
        or product.get('type')
        or product.get('productType')
        or 'Poster'
    )


def _set_bulk_source_column(source_columns, header, value):
    if header in source_columns and value is not None:
        source_columns[header] = _ensure_string(value)


def _apply_ai_metafield_defaults(enhanced, ai_metafields):
    """Populate safe text/boolean metafield edit keys from AI metadata.

    Shopify taxonomy attributes and product references can be constrained by
    Shopify definitions, so those still go through category/product selectors.
    """
    color = _ensure_string(ai_metafields.get('color', '')).strip()
    theme = _ensure_string(ai_metafields.get('theme', '')).strip()
    frame_style = _ensure_string(ai_metafields.get('frame_style', '')).strip()
    custom_product = _ensure_string(enhanced.get('custom_product', '')).strip() or 'TRUE'
    seo_title = _ensure_string(enhanced.get('seo_title', '')).strip()
    seo_description = _ensure_string(enhanced.get('seo_description', '')).strip()

    safe_metafields = {
        ('custom', 'color', 'single_line_text_field'): color,
        ('custom', 'theme', 'single_line_text_field'): theme,
        ('custom', 'frame_style', 'single_line_text_field'): frame_style,
        ('mm-google-shopping', 'custom_product', 'boolean'): 'true' if custom_product.lower() in {'1', 'true', 'yes'} else 'false',
        ('global', 'title_tag', 'single_line_text_field'): seo_title,
        ('global', 'description_tag', 'multi_line_text_field'): seo_description,
    }

    # The Google Shopping metafields were being left empty on every listing.
    # The values already exist on the row - the AI produced them and they go
    # into the CSV columns - they just were never copied into the matching
    # metafields, so the review screen showed a blank box next to a blank
    # current value and nothing ever changed. Only values the AI actually
    # returned are copied; nothing is invented to fill a gap.
    google_shopping_fields = [
        ('google_product_category', enhanced.get('google_product_category', '')),
        ('gender', enhanced.get('gender', '')),
        ('age_group', enhanced.get('age_group', '')),
        ('condition', enhanced.get('condition', '')),
        ('custom_label_0', enhanced.get('custom_label_0', '')),
        ('custom_label_1', enhanced.get('custom_label_1', '')),
        ('custom_label_2', enhanced.get('custom_label_2', '')),
        ('custom_label_3', enhanced.get('custom_label_3', '')),
        ('custom_label_4', enhanced.get('custom_label_4', '')),
        ('color', color),
        ('material', _ensure_string(ai_metafields.get('material', '')).strip()),
    ]
    for key, value in google_shopping_fields:
        value = _ensure_string(value).strip()
        if value:
            safe_metafields[('mm-google-shopping', key, 'single_line_text_field')] = value
    for (namespace, key, metafield_type), value in safe_metafields.items():
        if value in (None, ''):
            continue
        edit_key = f"mf__{namespace}__{key}"
        type_key = f"mf_type__{namespace}__{key}"
        if not _ensure_string(enhanced.get(edit_key, '')).strip():
            enhanced[edit_key] = value
        enhanced[type_key] = enhanced.get(type_key) or metafield_type


def _apply_discovery_recommendation_fields(enhanced, recommendations):
    recommendations = recommendations or {}
    related = [pid for pid in recommendations.get('related_products') or [] if pid]
    complementary = [pid for pid in recommendations.get('complementary_products') or [] if pid]
    boosts = [term for term in recommendations.get('search_boosts') or [] if term]

    if related:
        enhanced['mf__shopify--discovery--product_recommendation__related_products'] = json.dumps(related)
        enhanced['mf_type__shopify--discovery--product_recommendation__related_products'] = 'list.product_reference'
    if complementary:
        enhanced['mf__shopify--discovery--product_recommendation__complementary_products'] = json.dumps(complementary)
        enhanced['mf_type__shopify--discovery--product_recommendation__complementary_products'] = 'list.product_reference'
    if related or complementary:
        enhanced['mf__shopify--discovery--product_recommendation__related_products_display'] = 'true'
        enhanced['mf_type__shopify--discovery--product_recommendation__related_products_display'] = 'boolean'
    if boosts:
        enhanced['mf__shopify--discovery--product_search_boost__queries'] = json.dumps(boosts)
        enhanced['mf_type__shopify--discovery--product_search_boost__queries'] = 'list.single_line_text_field'


def _apply_discovery_source_columns(source_columns, enhanced):
    source_values = {
        'Complementary products (product.metafields.shopify--discovery--product_recommendation.complementary_products)': enhanced.get('mf__shopify--discovery--product_recommendation__complementary_products', ''),
        'Related products (product.metafields.shopify--discovery--product_recommendation.related_products)': enhanced.get('mf__shopify--discovery--product_recommendation__related_products', ''),
        'Related products settings (product.metafields.shopify--discovery--product_recommendation.related_products_display)': enhanced.get('mf__shopify--discovery--product_recommendation__related_products_display', ''),
        'Search product boosts (product.metafields.shopify--discovery--product_search_boost.queries)': enhanced.get('mf__shopify--discovery--product_search_boost__queries', ''),
    }
    for header, value in source_values.items():
        _set_bulk_source_column(source_columns, header, value)


def _filter_to_real_collections(selected_collections, available_collections):
    if not available_collections:
        return selected_collections or []
    selected = selected_collections or []
    if isinstance(selected, str):
        selected = [item.strip() for item in selected.split(',') if item.strip()]

    def _norm(name):
        # Match on alphanumerics only so "Japanese Art", "japanese-art" and
        # "Japanese  Art!" all resolve to the same real collection. This never
        # produces a wrong match — only near-miss formatting differences.
        return re.sub(r'[^a-z0-9]+', '', str(name or '').lower())

    real_by_lower = {}
    real_by_norm = {}
    real_by_segment = {}
    for name in available_collections:
        clean = str(name).strip()
        if not clean:
            continue
        real_by_lower.setdefault(clean.lower(), clean)
        real_by_norm.setdefault(_norm(clean), clean)
        # Store titles are often two labels in one, e.g.
        # "Animal Prints | Animal Art". The AI naturally names one half of that,
        # so match each segment too - otherwise a perfectly good pick is thrown
        # away and the product falls back to keyword auto-matching.
        for segment in re.split(r'[|–—/>+]| - ', clean):
            segment_key = _norm(segment)
            if len(segment_key) >= 4:
                real_by_segment.setdefault(segment_key, clean)

    matched = []
    seen = set()
    for item in selected:
        key = str(item).strip().lower()
        if not key:
            continue
        real = (
            real_by_lower.get(key)
            or real_by_norm.get(_norm(item))
            or real_by_segment.get(_norm(item))
        )
        if real and real not in seen:
            seen.add(real)
            matched.append(real)
    return matched


# Generic words that appear in most poster collection titles / product tags and
# therefore carry no signal for matching a product to a specific collection.
_GENERIC_COLLECTION_TOKENS = {
    "wall", "art", "arts", "print", "prints", "poster", "posters", "decor",
    "home", "artwork", "artworks", "collection", "collections", "the", "and",
    "for", "with", "new", "all", "best", "sellers", "seller", "shop", "store",
    "arrivals", "sale", "featured", "products", "product", "gift", "gifts",
    "piece", "pieces", "picture", "pictures", "design", "designs", "style",
    "styles", "unframed", "framed", "paper", "canvas",
}


# Concrete colour words (>=4 chars, so they survive tokenisation). Used to
# recognise collections defined purely by colour so a single incidental palette
# colour can't force-assign a whole-artwork colour-scheme collection (e.g. a
# brown/beige piece that merely contains white joining "Black and White Art").
_COLOR_TOKENS = {
    "black", "white", "grey", "gray", "blue", "green", "yellow", "orange",
    "purple", "pink", "brown", "beige", "cream", "gold", "silver", "teal",
    "navy", "maroon", "violet", "indigo", "turquoise", "ivory", "charcoal",
    "bronze", "copper", "coral", "mint", "lavender", "burgundy", "mustard",
    "olive", "peach", "salmon", "magenta", "cyan", "aqua", "sepia", "taupe",
    "khaki", "crimson", "scarlet", "azure", "emerald", "amber", "ruby",
    "sapphire",
}


def _keyword_tokens(*values):
    tokens = set()
    for value in values:
        if isinstance(value, (list, tuple, set)):
            tokens |= _keyword_tokens(*value)
            continue
        for tok in re.findall(r'[a-z0-9]+', str(value or '').lower()):
            if len(tok) >= 4 and tok not in _GENERIC_COLLECTION_TOKENS:
                tokens.add(tok)
    return tokens


_NEUTRAL_COLOR_TOKENS = {"black", "white", "grey", "gray"}


def _colour_only_collection_ok(name, palette_colors):
    """False if *name* is a collection defined *entirely* by colour(s) whose
    scheme the artwork's palette does not satisfy.

    Two regimes, because colour collections mean different things:
      * Neutral-only scheme (e.g. "Black and White", "Grey") means an *absence*
        of chroma — it matches only when EVERY palette colour is one of the
        collection's neutrals (greys always allowed). Blocks a vibrant
        orange/green piece with incidental black linework from being filed as
        B&W.
      * Chromatic colour (e.g. "Blue Art") means that colour is *present* — it
        matches when the named colour appears in the palette, regardless of the
        other colours (so a navy/blue piece still matches "Blue").
    Concept collections and colour+concept names always pass.
    """
    ctoks = _keyword_tokens(name)
    if not ctoks:
        return True
    color_in_name = ctoks & _COLOR_TOKENS
    if not color_in_name or color_in_name != ctoks:
        return True  # not a colour-only collection
    if not palette_colors:
        return False  # scheme can't be confirmed — don't force-assign
    if color_in_name <= _NEUTRAL_COLOR_TOKENS:
        return palette_colors.issubset(color_in_name | _NEUTRAL_COLOR_TOKENS)
    return bool(color_in_name & palette_colors)


def _auto_match_collections(metadata, available_collections, max_n=3):
    """Deterministically map a product to real store collections by keyword
    overlap, so assignment (and internal links) never depend solely on the AI
    returning collection names. Universal and subject-agnostic: it only matches
    on meaningful shared tokens, so generic 'Wall Art'-style collections that
    share nothing specific are not force-assigned.
    """
    if not available_collections:
        return []
    mf = metadata.get('metafields') if isinstance(metadata.get('metafields'), dict) else {}
    product_tokens = _keyword_tokens(
        metadata.get('title'), metadata.get('product_type'), metadata.get('tags'),
        mf.get('theme'), mf.get('subject'), mf.get('art_style'), mf.get('art_movement'),
        mf.get('palette'), mf.get('composition'), mf.get('color'), mf.get('room'),
        mf.get('mood'),
        metadata.get('custom_label_0'), metadata.get('custom_label_3'),
    )
    if not product_tokens:
        return []
    palette_colors = _keyword_tokens(mf.get('palette'), mf.get('color')) & _COLOR_TOKENS
    scored = []
    for name in available_collections:
        clean = str(name).strip()
        if not clean:
            continue
        overlap = _keyword_tokens(clean) & product_tokens
        if not overlap:
            continue
        if not _colour_only_collection_ok(clean, palette_colors):
            continue
        scored.append((len(overlap), clean))
    scored.sort(key=lambda item: (-item[0], item[1].lower()))
    return [name for _, name in scored[:max_n]]


def _mandatory_collections_from_prompt(prompt_text):
    """Collections a profile says every product must join.

    Driven entirely by an editable profile line, so it stays universal:
        - ALWAYS INCLUDE COLLECTION: View All Posters
    Repeat the line for more than one. Nothing is hardcoded per store.
    """
    return [
        " ".join(match.group(1).split())
        for match in re.finditer(
            r"^\s*-?\s*ALWAYS\s+INCLUDE\s+COLLECTION\s*:\s*(.+?)\s*$",
            str(prompt_text or ""),
            flags=re.I | re.M,
        )
        if match.group(1).strip()
    ]


def _resolve_product_collections(metadata, available_collections, log_prefix="Collections", target=6,
                                 mandatory_collections=None):
    """Resolve a product's real store collections.

    Combines the AI's picks (filtered to real collections) with deterministic
    keyword auto-matches, so a product is assigned to *all* clearly relevant
    collections rather than however few the model happened to name. Auto-match
    only ever adds collections that share meaningful keywords, so nothing
    irrelevant is force-assigned. Capped at ``target`` to avoid dilution."""
    ai_picks = metadata.get('collections', [])
    real = _filter_to_real_collections(ai_picks, available_collections)
    # A profile's must-join collections lead the list, so a catch-all like
    # "View All Posters" cannot be squeezed out by the per-product cap or
    # missed because the model forgot to name it.
    required = _filter_to_real_collections(mandatory_collections or [], available_collections)
    real = required + [name for name in real if name not in required]
    # Auto-match tops up the AI's picks; it never out-numbers them, so a profile
    # rule like "always the catch-all + one subject + one style + one colour"
    # survives intact instead of being squeezed out by keyword matches.
    auto = _auto_match_collections(metadata, available_collections, max_n=4)

    # Sanity-check colour-only collections against the palette for EVERY source,
    # including AI picks (the model can wrongly file anything with black linework
    # under "Black and White").
    mf = metadata.get('metafields') if isinstance(metadata.get('metafields'), dict) else {}
    palette_colors = _keyword_tokens(mf.get('palette'), mf.get('color')) & _COLOR_TOKENS

    combined = []
    seen = set()
    for name in list(real) + list(auto):
        key = str(name).strip().lower()
        if key and key not in seen and _colour_only_collection_ok(name, palette_colors):
            seen.add(key)
            combined.append(name)
    final = combined[:target]

    if real and auto and len(final) > len(real):
        source = "ai+auto"
    elif real:
        source = "ai"
    elif auto:
        source = "auto-match"
    else:
        source = "none"
    logger.warning(
        "%s: available=%d, ai_picked=%s, final(%s)=%s",
        log_prefix, len(available_collections or []), ai_picks, source, final,
    )
    return final


def _apply_ai_source_column_defaults(source_columns, enhanced):
    """Fill matching Shopify CSV/source headers when they exist."""
    ai_source_defaults = {
        'Google: Custom Product (product.metafields.mm-google-shopping.custom_product)': enhanced.get('custom_product', ''),
        'Color (product.metafields.custom.color)': enhanced.get('metafield_color', ''),
        'Frame Style (product.metafields.custom.frame_style)': enhanced.get('metafield_frame_style', ''),
        'Theme (product.metafields.custom.theme)': enhanced.get('metafield_theme', ''),
        'Art movement (product.metafields.shopify.art-movement)': enhanced.get('metafield_art_movement', ''),
        'Art style (product.metafields.shopify.art-style)': enhanced.get('metafield_art_style', ''),
        'Artwork authenticity (product.metafields.shopify.artwork-authenticity)': enhanced.get('metafield_artwork_authenticity', ''),
        'Color (product.metafields.shopify.color-pattern)': enhanced.get('metafield_color', ''),
        'Frame style (product.metafields.shopify.frame-style)': enhanced.get('metafield_frame_style', ''),
        'Material (product.metafields.shopify.material)': enhanced.get('metafield_material', ''),
        'Orientation (product.metafields.shopify.orientation)': enhanced.get('metafield_orientation', ''),
        'Theme (product.metafields.shopify.theme)': enhanced.get('metafield_theme', ''),
        'Metafield: custom.color [single_line_text_field]': enhanced.get('metafield_color', ''),
        'Metafield: custom.theme [single_line_text_field]': enhanced.get('metafield_theme', ''),
        'Metafield: custom.frame_style [single_line_text_field]': enhanced.get('metafield_frame_style', ''),
        'global.title_tag': enhanced.get('seo_title', ''),
        'global.description_tag': enhanced.get('seo_description', ''),
        'Metafield: global.title_tag [single_line_text_field]': enhanced.get('seo_title', ''),
        'Metafield: global.description_tag [multi_line_text_field]': enhanced.get('seo_description', ''),
    }
    for header, value in ai_source_defaults.items():
        _set_bulk_source_column(source_columns, header, value)


def _enhance_product_for_bulk_job(product, custom_prompt, field_settings, vendor, sku_pattern,
                                  collections_enabled=True, available_collections=None,
                                  shop_domain=None, access_token=None, model_name=None):
    """Enhance one product for CSV/API bulk SEO jobs."""
    import tempfile

    image_url = product.get('image_url', '')
    if not image_url:
        raise ValueError('No image URL in product')
    if not is_safe_image_url(image_url):
        raise ValueError('Image URL not from allowed domain')
    if not os.environ.get("GEMINI_API_KEY"):
        raise ValueError("GEMINI_API_KEY is not set. Cannot generate AI metadata.")

    try:
        response = requests.get(image_url, timeout=30)
        response.raise_for_status()
    except Exception as e:
        raise ValueError(f'Failed to download image: {str(e)}')

    with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp_file:
        tmp_file.write(response.content)
        tmp_path = tmp_file.name

    try:
        collections_for_prompt = available_collections if collections_enabled else []
        category_gid_for_prompt = product.get('category_gid') or resolve_category_name_to_gid(product.get('product_category', ''))
        category_attribute_options = {}
        if category_gid_for_prompt:
            try:
                category_attribute_options = get_category_attribute_options(
                    category_gid_for_prompt,
                    shop_domain=shop_domain,
                    access_token=access_token,
                )
            except Exception as exc:
                logger.warning("Bulk category attribute lookup failed for %s: %s", category_gid_for_prompt, exc)
        metadata = generate_product_metadata(
            tmp_path,
            custom_prompt,
            collections_for_prompt,
            category_attribute_options=category_attribute_options,
            model_name=model_name,
        )
        if not metadata:
            raise ValueError("Failed to generate metadata from AI.")
        metadata['collections'] = _resolve_product_collections(
            metadata, collections_for_prompt, log_prefix="Bulk-job collections",
            mandatory_collections=_mandatory_collections_from_prompt(custom_prompt),
        )
        if not collections_enabled:
            metadata['collections'] = []

        def resolve_enhance_field(field_id, ai_value, original_key=None, fallback=''):
            fs = field_settings.get(field_id, {})
            mode = fs.get('mode', 'auto')
            if mode == 'manual':
                return fs.get('manual_value', fallback)
            if mode == 'clear':
                return ''
            if mode == 'if_empty':
                existing = product.get(original_key, fallback) if original_key else fallback
                return existing if str(existing or '').strip() else (ai_value if ai_value is not None else fallback)
            if mode == 'do_not_edit':
                if original_key:
                    return product.get(original_key, fallback)
                return fallback
            return ai_value if ai_value is not None else fallback

        raw_tags = metadata.get('tags', [])
        ai_tags_str = ', '.join(str(t) for t in raw_tags) if isinstance(raw_tags, list) else str(raw_tags)
        raw_collections = metadata.get('collections', [])
        ai_collections_str = ', '.join(str(c) for c in raw_collections) if isinstance(raw_collections, list) else str(raw_collections)
        ai_metafields = metadata.get('metafields', {})

        variant_sku = ''
        if sku_pattern:
            handle = product.get('handle', '')
            variant_sku = f"{sku_pattern}-{handle[:20].upper().replace('-', '')}" if handle else sku_pattern

        ai_title = metadata.get('title', product.get('title', ''))
        product_category = metadata.get('category', 'Home & Garden > Decor > Artwork > Posters, Prints, & Visual Artwork')
        category_gid = resolve_category_name_to_gid(product_category) or category_gid_for_prompt or product.get('category_gid', '')

        source_columns = dict(product.get('source_columns') or {})
        recommendations = {}
        if shop_domain and access_token:
            try:
                recommendations = recommend_products_for_discovery(
                    metadata,
                    current_product_id=product.get('id', ''),
                    shop_domain=shop_domain,
                    access_token=access_token,
                )
            except Exception as exc:
                logger.warning("Bulk discovery recommendation lookup failed: %s", exc)
        variants_for_apply = copy.deepcopy(product.get('variants') or [])
        if _has_weak_single_default_variant(product):
            variants_for_apply = _default_bulk_variant_rows(
                product,
                sku_pattern=sku_pattern,
                default_location_id=product.get('variant_inventory_location_id', ''),
            )
        else:
            for index, variant in enumerate(variants_for_apply):
                if variant_sku and not variant.get('sku'):
                    variant['sku'] = f"{variant_sku}-V{index + 1}" if len(variants_for_apply) > 1 else variant_sku
                if variant.get('inventory_quantity') in (None, '') and product.get('variant_inventory_location_id'):
                    variant['inventory_quantity'] = 999
                    variant['inventory_location_id'] = product.get('variant_inventory_location_id')
                    variant['apply_inventory'] = True
                if not variant.get('inventory_policy'):
                    variant['inventory_policy'] = 'CONTINUE'
        media_for_apply = copy.deepcopy(product.get('media') or [])
        ai_alt_text = metadata.get('alt_text', '')
        if media_for_apply and ai_alt_text:
            media_for_apply[0]['alt'] = ai_alt_text
        enhanced = {
            'product_id': product.get('id', ''),
            'status': product.get('status', ''),
            'handle': product.get('handle', ''),
            'title': ai_title,
            'body_html': format_description_html(metadata.get('description', product.get('body_html', ''))),
            'tags': resolve_enhance_field('tags', ai_tags_str, 'tags'),
            'vendor': vendor or product.get('vendor', 'Listing Cannon'),
            'product_type': _bulk_product_type(product),
            'product_category': product_category,
            'category_gid': category_gid,
            'seo_title': resolve_enhance_field('seo_title', metadata.get('seo_title', _word_safe_truncate(ai_title, 70) if ai_title else ''), 'seo_title'),
            'seo_description': metadata.get('meta_description', ''),
            'image_alt_text': metadata.get('alt_text', ''),
            'collections': resolve_enhance_field('collections', ai_collections_str),
            'variant_id': product.get('variant_id', '') or ((product.get('variants') or [{}])[0] or {}).get('id', ''),
            'variant_inventory_item_id': product.get('variant_inventory_item_id', '') or ((product.get('variants') or [{}])[0] or {}).get('inventory_item_id', ''),
            'variant_inventory_location_id': product.get('variant_inventory_location_id', ''),
            'google_product_category': resolve_enhance_field('google_product_category', metadata.get('google_product_category', '')),
            'gender': resolve_enhance_field('gender', metadata.get('gender', 'Unisex')),
            'age_group': resolve_enhance_field('age_group', metadata.get('age_group', 'Adult')),
            'condition': resolve_enhance_field('condition', metadata.get('condition', 'New')),
            'custom_product': resolve_enhance_field('custom_product', metadata.get('custom_product', 'TRUE')),
            'custom_label_0': resolve_enhance_field('custom_label_0', metadata.get('custom_label_0', '')),
            'custom_label_1': resolve_enhance_field('custom_label_1', metadata.get('custom_label_1', '')),
            'custom_label_2': resolve_enhance_field('custom_label_2', metadata.get('custom_label_2', '')),
            'custom_label_3': resolve_enhance_field('custom_label_3', metadata.get('custom_label_3', '')),
            'custom_label_4': resolve_enhance_field('custom_label_4', metadata.get('custom_label_4', '')),
            'variant_sku': variant_sku,
            'variant_barcode': product.get('variant_barcode', ''),
            'variant_price': product.get('variant_price', ''),
            'variant_compare_at_price': product.get('variant_compare_at_price', ''),
            'variant_inventory_quantity': product.get('variant_inventory_quantity', ''),
            'variant_inventory_policy': product.get('variant_inventory_policy', ''),
            'variant_taxable': product.get('variant_taxable', ''),
            'variant_weight': product.get('variant_weight', ''),
            'variant_weight_unit': product.get('variant_weight_unit', ''),
            'metafield_color': ai_metafields.get('color', ''),
            'metafield_theme': ai_metafields.get('theme', ''),
            'metafield_frame_style': ai_metafields.get('frame_style', ''),
            'metafield_condition': ai_metafields.get('condition', 'New'),
            'metafield_decoration_material': ai_metafields.get('decoration_material', ''),
            'metafield_artwork_frame_material': ai_metafields.get('artwork_frame_material', ''),
            'metafield_art_movement': ai_metafields.get('art_movement', ''),
            'metafield_art_style': ai_metafields.get('art_style', ''),
            'metafield_artwork_authenticity': ai_metafields.get('artwork_authenticity', ''),
            'variant_mode': 'merge',
            'original_variant_ids': [v.get('id') for v in variants_for_apply if v.get('id')],
            'metafield_material': ai_metafields.get('material', ''),
            'metafield_orientation': ai_metafields.get('orientation', ''),
            'category_attribute_picks': metadata.get('category_attribute_picks', {}),
            'variants_json': json.dumps(variants_for_apply, indent=2),
            'media_json': json.dumps(media_for_apply, indent=2),
            'price_lists_json': product.get('price_lists_json', '[]'),
            'source_columns': source_columns,
            'ai_usage': metadata.get('_ai_usage') or {},
        }
        source_overlay = {
            'Title': enhanced['title'],
            'Body (HTML)': enhanced['body_html'],
            'Tags': enhanced['tags'],
            'Vendor': enhanced['vendor'],
            'Product Category': enhanced['product_category'],
            'Type': enhanced['product_type'],
            'SEO Title': enhanced['seo_title'],
            'SEO Description': enhanced['seo_description'],
            'Image Alt Text': enhanced['image_alt_text'],
            'Google Shopping / Google Product Category': enhanced['google_product_category'],
            'Google Shopping / Gender': enhanced['gender'],
            'Google Shopping / Age Group': enhanced['age_group'],
            'Google Shopping / Condition': enhanced['condition'],
            'Google Shopping / Custom Product': enhanced['custom_product'],
            'Google Shopping / Custom Label 0': enhanced['custom_label_0'],
            'Google Shopping / Custom Label 1': enhanced['custom_label_1'],
            'Google Shopping / Custom Label 2': enhanced['custom_label_2'],
            'Google Shopping / Custom Label 3': enhanced['custom_label_3'],
            'Google Shopping / Custom Label 4': enhanced['custom_label_4'],
            'Variant SKU': enhanced['variant_sku'],
            'Art movement (product.metafields.shopify.art-movement)': enhanced['metafield_art_movement'],
            'Art style (product.metafields.shopify.art-style)': enhanced['metafield_art_style'],
            'Artwork authenticity (product.metafields.shopify.artwork-authenticity)': enhanced['metafield_artwork_authenticity'],
            'Material (product.metafields.shopify.material)': enhanced['metafield_material'],
            'Orientation (product.metafields.shopify.orientation)': enhanced['metafield_orientation'],
        }
        for header, value in source_overlay.items():
            _set_bulk_source_column(source_columns, header, value)
        _apply_ai_source_column_defaults(source_columns, enhanced)
        for key, value in _generic_metafields_from_product(product).items():
            enhanced.setdefault(key, value)
        _apply_ai_metafield_defaults(enhanced, ai_metafields)
        _apply_discovery_recommendation_fields(enhanced, recommendations)
        _apply_discovery_source_columns(source_columns, enhanced)
        return enhanced
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# Fields that identify or structurally describe the product rather than
# describing it in words. A column-scoped edit must never rewrite these.
_ENHANCEMENT_STRUCTURAL_KEYS = {
    'product_id', 'handle', 'status', 'variant_mode', 'original_variant_ids',
    'variants_json', 'media_json', 'price_lists_json', 'source_columns',
    'variant_id', 'variant_inventory_item_id', 'ai_usage', 'recommendations',
}


def _restrict_enhancement_to_fields(enhanced, product, only_fields):
    """Keep the AI's rewrite for the chosen fields only.

    A column-scoped edit asks the AI to improve one column. The model still
    returns a whole listing, so every other field is put back to exactly what
    the product already has. Nothing outside the chosen column can change.

    Returns True when the chosen fields actually differ from what is there now.
    """
    only_fields = [str(field).strip() for field in (only_fields or []) if str(field).strip()]
    if not enhanced or not only_fields:
        return True

    keep = set(only_fields)
    # A category name and its taxonomy id are one value in two fields.
    if 'product_category' in keep:
        keep.add('category_gid')

    for key in list(enhanced.keys()):
        if key in keep or key in _ENHANCEMENT_STRUCTURAL_KEYS:
            continue
        if key in product:
            enhanced[key] = product[key]

    changed = False
    for field in only_fields:
        before = str(product.get(field, '') or '').strip()
        after = str(enhanced.get(field, '') or '').strip()
        if before != after:
            changed = True
            break
    enhanced['_only_fields'] = only_fields
    enhanced['_no_change'] = not changed
    return changed


def _run_csv_enhance_job(job_id, shop_domain=None, access_token=None):
    try:
        with csv_enhance_jobs_lock:
            job = csv_enhance_jobs.get(job_id)
            if not job:
                return
            if job.get('_thread_running'):
                return
            job['_thread_running'] = True
            job['status'] = 'processing'
            job['current_step'] = 'Starting AI enhancement...'
            job['updated_at'] = datetime.now().isoformat()

        settings = job.get('settings', {})
        available_collections = []
        if settings.get('collections_enabled', True):
            try:
                available_collections = get_collections(shop_domain=shop_domain, access_token=access_token)
            except Exception as e:
                logger.error(f"Bulk job {job_id}: failed to fetch collections: {e}")

        while True:
            with csv_enhance_jobs_lock:
                job = csv_enhance_jobs.get(job_id)
                if not job or job.get('status') != 'processing':
                    break
                products = job.get('products', [])
                index = int(job.get('current_index', 0))
                if index >= len(products):
                    job['status'] = 'enhanced'
                    job['current_step'] = 'AI enhancement complete.'
                    job['updated_at'] = datetime.now().isoformat()
                    _append_csv_job_log(job, 'AI enhancement complete.', 'success')
                    break
                product = products[index]
                job['current_step'] = f"Enhancing {index + 1} of {len(products)}: {product.get('title') or product.get('handle') or 'Untitled'}"
                job['updated_at'] = datetime.now().isoformat()

            error = None
            enhanced = None
            max_attempts = max(1, min(int(settings.get('max_attempts') or 2), 3))
            for attempt in range(1, max_attempts + 1):
                try:
                    enhanced = _enhance_product_for_bulk_job(
                        product,
                        settings.get('custom_prompt', ''),
                        settings.get('field_settings', {}),
                        settings.get('vendor', 'Listing Cannon'),
                        settings.get('sku_pattern', ''),
                        settings.get('collections_enabled', True),
                        available_collections,
                        shop_domain=shop_domain,
                        access_token=access_token,
                        model_name=settings.get('model_name'),
                    )
                    _restrict_enhancement_to_fields(enhanced, product, settings.get('only_fields'))
                    break
                except Exception as e:
                    error = str(e)
                    logger.warning(f"Bulk job {job_id}: product {index + 1} attempt {attempt} failed: {error}")
                    time.sleep(min(2 * attempt, 5))

            with csv_enhance_jobs_lock:
                job = csv_enhance_jobs.get(job_id)
                if not job:
                    break
                if enhanced:
                    usage = enhanced.get('ai_usage') or {}
                    totals = job.setdefault('ai_usage', {'calls': 0, 'prompt_tokens': 0, 'output_tokens': 0,
                                                         'thinking_tokens': 0, 'total_tokens': 0})
                    for usage_key in ('calls', 'prompt_tokens', 'output_tokens', 'thinking_tokens', 'total_tokens'):
                        totals[usage_key] = int(totals.get(usage_key) or 0) + int(usage.get(usage_key) or 0)
                    job.setdefault('enhancements', []).append({
                        'originalIndex': product.get('originalIndex', index),
                        'enhanced': enhanced,
                    })
                    _append_csv_job_log(job, f"Enhanced {enhanced.get('title') or product.get('title') or product.get('handle')}", 'success')
                else:
                    job.setdefault('failures', []).append({
                        'index': index,
                        'title': product.get('title') or product.get('handle') or 'Untitled',
                        'error': error or 'Unknown error',
                        'product': product,
                    })
                    _append_csv_job_log(job, f"Failed {product.get('title') or product.get('handle')}: {error}", 'error')
                job['current_index'] = index + 1
                job['updated_at'] = datetime.now().isoformat()
                job['_thread_running'] = False
            _save_csv_enhance_jobs()

            with csv_enhance_jobs_lock:
                job = csv_enhance_jobs.get(job_id)
                if job:
                    job['_thread_running'] = True

        with csv_enhance_jobs_lock:
            job = csv_enhance_jobs.get(job_id)
            if job:
                job['_thread_running'] = False
                job['updated_at'] = datetime.now().isoformat()
        _save_csv_enhance_jobs()
    except Exception as e:
        logger.error(f"Bulk CSV enhance job {job_id} crashed: {e}")
        with csv_enhance_jobs_lock:
            job = csv_enhance_jobs.get(job_id)
            if job:
                job['_thread_running'] = False
                job['status'] = 'error'
                job['current_step'] = str(e)
                _append_csv_job_log(job, f"Job crashed: {e}", 'error')
        _save_csv_enhance_jobs()


def _run_csv_apply_job(job_id, shop_domain, access_token):
    try:
        with csv_enhance_jobs_lock:
            job = csv_enhance_jobs.get(job_id)
            if not job:
                return
            if job.get('_thread_running'):
                return
            job['_thread_running'] = True
            job['status'] = 'applying'
            job['current_step'] = 'Applying enhanced data to Shopify...'
            job.setdefault('apply_results', [])
            job['updated_at'] = datetime.now().isoformat()

        while True:
            with csv_enhance_jobs_lock:
                job = csv_enhance_jobs.get(job_id)
                if not job or job.get('status') != 'applying':
                    break
                enhancements = job.get('enhancements', [])
                index = int(job.get('apply_index', 0))
                if index >= len(enhancements):
                    job['status'] = 'applied'
                    job['current_step'] = 'Shopify apply complete.'
                    job['updated_at'] = datetime.now().isoformat()
                    _append_csv_job_log(job, 'Shopify apply complete.', 'success')
                    break
                enhanced = _resolve_enhanced_category(enhancements[index].get('enhanced') or {})
                product_id = enhanced.get('product_id')
                # A column-scoped run only writes the products the AI actually
                # changed, so untouched listings are never re-saved.
                if enhanced.get('_no_change'):
                    job.setdefault('apply_results', []).append({
                        'success': True,
                        'skipped': True,
                        'title': enhanced.get('title') or enhanced.get('handle') or product_id,
                        'product_id': product_id,
                        'error': '',
                    })
                    job['apply_index'] = index + 1
                    job['updated_at'] = datetime.now().isoformat()
                    continue
                job['current_step'] = f"Applying {index + 1} of {len(enhancements)}: {enhanced.get('title') or enhanced.get('handle') or product_id}"

            if product_id:
                result = update_product_seo_metadata(product_id, enhanced, shop_domain=shop_domain, access_token=access_token)
                if result.get('success'):
                    _apply_category_attribute_metafields(
                        enhanced,
                        shop_domain=shop_domain,
                        access_token=access_token,
                    )
            else:
                result = {'success': False, 'error': 'Missing Shopify product id'}

            with csv_enhance_jobs_lock:
                job = csv_enhance_jobs.get(job_id)
                if not job:
                    break
                result['title'] = enhanced.get('title') or enhanced.get('handle') or product_id or 'Unknown product'
                result['product_id'] = product_id
                job.setdefault('apply_results', []).append(result)
                job['apply_index'] = index + 1
                job['updated_at'] = datetime.now().isoformat()
                if result.get('success'):
                    _append_csv_job_log(job, f"Applied {result['title']} to Shopify.", 'success')
                else:
                    _append_csv_job_log(job, f"Apply failed for {result['title']}: {result.get('error')}", 'error')
            _save_csv_enhance_jobs()
            time.sleep(0.2)

        with csv_enhance_jobs_lock:
            job = csv_enhance_jobs.get(job_id)
            if job:
                job['_thread_running'] = False
                job['updated_at'] = datetime.now().isoformat()
        _save_csv_enhance_jobs()
    except Exception as e:
        logger.error(f"Bulk CSV apply job {job_id} crashed: {e}")
        with csv_enhance_jobs_lock:
            job = csv_enhance_jobs.get(job_id)
            if job:
                job['_thread_running'] = False
                job['status'] = 'error'
                job['current_step'] = str(e)
                _append_csv_job_log(job, f"Apply job crashed: {e}", 'error')
        _save_csv_enhance_jobs()


_load_csv_enhance_jobs()


@app.route('/api/bulk_seo_jobs', methods=['POST'])
@login_required
def create_bulk_seo_job():
    """Create a resumable server-side AI enhancement job."""
    try:
        data = request.get_json() or {}
        products = data.get('products') or []
        if not products:
            return jsonify({'success': False, 'error': 'No products selected'}), 400
        if data.get('ai_cost_acknowledged') is not True:
            return jsonify({'success': False, 'error': 'Confirm the AI model and estimated usage before starting.'}), 400

        model_name = str(data.get('model_name') or 'gemini-3.1-flash-lite').strip()
        if model_name not in {'gemini-3.1-flash-lite', 'gemini-3.5-flash'}:
            return jsonify({'success': False, 'error': 'Unsupported listing AI model.'}), 400

        shop = get_current_shop()
        source = data.get('source', 'csv')
        if source == 'shopify_api' and not shop:
            return jsonify({'success': False, 'error': 'No Shopify store connected. Connect a store first.'}), 400
        shop_domain = shop.shop_domain if shop else None
        access_token = shop.access_token if shop else None

        job_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        job = {
            'job_id': job_id,
            'user_id': current_user.id,
            'source': source,
            'shop_id': shop.id if shop else None,
            'shop_domain': shop.shop_domain if shop else None,
            'status': 'queued',
            'products': products,
            'settings': {
                'custom_prompt': data.get('custom_prompt', ''),
                'vendor': data.get('vendor', 'Listing Cannon'),
                'sku_pattern': data.get('sku_pattern', ''),
                'field_settings': data.get('field_settings', {}),
                # Column-scoped edit: when set, only these fields may change.
                'only_fields': [str(field).strip() for field in (data.get('only_fields') or [])
                                if str(field).strip()],
                'collections_enabled': data.get('collections_enabled', True),
                'model_name': model_name,
                'max_attempts': max(1, min(int(data.get('max_attempts') or 2), 3)),
            },
            'current_index': 0,
            'apply_index': 0,
            'enhancements': [],
            'failures': [],
            'apply_results': [],
            'logs': [],
            'ai_usage': {'calls': 0, 'prompt_tokens': 0, 'output_tokens': 0,
                         'thinking_tokens': 0, 'total_tokens': 0},
            'current_step': 'Queued.',
            'created_at': now,
            'updated_at': now,
        }
        _append_csv_job_log(job, f"Queued {len(products)} products using {model_name}; one vision call per attempt.", 'info')
        with csv_enhance_jobs_lock:
            csv_enhance_jobs[job_id] = job
        _save_csv_enhance_jobs()

        thread = threading.Thread(
            target=_run_csv_enhance_job,
            args=(job_id, shop_domain, access_token),
            daemon=True,
            name=f"csv-enhance-{job_id[:8]}",
        )
        thread.start()

        return jsonify({'success': True, 'job': _csv_job_public(job)})
    except Exception as e:
        logger.error(f"Create bulk SEO job error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bulk_seo_jobs/<job_id>', methods=['GET'])
@login_required
def get_bulk_seo_job(job_id):
    with csv_enhance_jobs_lock:
        job = csv_enhance_jobs.get(job_id)
        if not job or job.get('user_id') != current_user.id:
            return jsonify({'success': False, 'error': 'Job not found'}), 404
        return jsonify({'success': True, 'job': _csv_job_public(job)})




@app.route('/api/bulk_edit_snapshots', methods=['GET'])
@login_required
def list_bulk_edit_snapshots():
    from models import BulkEditSnapshot
    shop = get_current_shop()
    if not shop:
        return jsonify({'success': False, 'error': 'No Shopify store connected.'}), 400
    snapshots = (
        BulkEditSnapshot.query
        .filter_by(user_id=current_user.id, shop_id=shop.id)
        .filter(BulkEditSnapshot.expires_at >= datetime.now())
        .order_by(BulkEditSnapshot.created_at.desc())
        .limit(50)
        .all()
    )
    return jsonify({
        'success': True,
        'retention_days': 90,
        'snapshots': [{
            'id': snapshot.id,
            'job_id': snapshot.job_id,
            'item_count': snapshot.item_count,
            'status': snapshot.status,
            'created_at': snapshot.created_at.isoformat(),
            'expires_at': snapshot.expires_at.isoformat(),
            'download_url': url_for('download_bulk_edit_snapshot', snapshot_id=snapshot.id),
        } for snapshot in snapshots],
    })


@app.route('/api/bulk_edit_snapshots/<snapshot_id>/download', methods=['GET'])
@login_required
def download_bulk_edit_snapshot(snapshot_id):
    from models import BulkEditSnapshot
    shop = get_current_shop()
    snapshot = BulkEditSnapshot.query.filter_by(
        id=snapshot_id,
        user_id=current_user.id,
        shop_id=shop.id if shop else None,
    ).first()
    if not snapshot:
        return jsonify({'success': False, 'error': 'Recovery snapshot not found.'}), 404
    payload = _decode_bulk_edit_snapshot(snapshot)
    response = make_response(json.dumps(payload, ensure_ascii=False, indent=2))
    response.headers['Content-Type'] = 'application/json; charset=utf-8'
    response.headers['Content-Disposition'] = f'attachment; filename=listing-cannon-recovery-{snapshot.id}.json'
    return response
@app.route('/api/bulk_seo_jobs/<job_id>/resume', methods=['POST'])
@login_required
def resume_bulk_seo_job(job_id):
    try:
        shop = get_current_shop()
        shop_domain = shop.shop_domain if shop else None
        access_token = shop.access_token if shop else None
        with csv_enhance_jobs_lock:
            job = csv_enhance_jobs.get(job_id)
            if not job or job.get('user_id') != current_user.id:
                return jsonify({'success': False, 'error': 'Job not found'}), 404
            if _bulk_job_shop_mismatch(job, shop):
                return jsonify({'success': False, 'error': 'This job belongs to a different Shopify store. Switch back to that store first.'}), 409
            if job.get('status') not in ('queued', 'paused', 'error'):
                return jsonify({'success': False, 'error': f"Job cannot resume from {job.get('status')}"}), 400
            job['status'] = 'queued'
            job['current_step'] = 'Resuming...'
        thread = threading.Thread(
            target=_run_csv_enhance_job,
            args=(job_id, shop_domain, access_token),
            daemon=True,
            name=f"csv-enhance-{job_id[:8]}",
        )
        thread.start()
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Resume bulk SEO job error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bulk_seo_jobs/<job_id>/apply', methods=['POST'])
@login_required
def apply_bulk_seo_job(job_id):
    try:
        shop = get_current_shop()
        if not shop:
            return jsonify({'success': False, 'error': 'No Shopify store connected. Connect a store first.'}), 400
        data = request.get_json(silent=True) or {}
        edited_enhancements = data.get('enhancements')
        with csv_enhance_jobs_lock:
            job = csv_enhance_jobs.get(job_id)
            if not job or job.get('user_id') != current_user.id:
                return jsonify({'success': False, 'error': 'Job not found'}), 404
            if _bulk_job_shop_mismatch(job, shop):
                return jsonify({'success': False, 'error': 'This job belongs to a different Shopify store. Switch back to that store first.'}), 409
            # Bind jobs made before this safeguard was introduced on first use.
            if not job.get('shop_id'):
                job['shop_id'] = shop.id
                job['shop_domain'] = shop.shop_domain
            if isinstance(edited_enhancements, list) and edited_enhancements:
                job['enhancements'] = edited_enhancements
            if not job.get('enhancements'):
                return jsonify({'success': False, 'error': 'No enhanced products to apply'}), 400
            enhancement_count = len(job.get('enhancements') or [])
            expected_confirmation = f"APPLY {enhancement_count}"
            if data.get('confirmation') != expected_confirmation:
                return jsonify({
                    'success': False,
                    'error': f"Confirmation required. Type {expected_confirmation} exactly.",
                }), 400
            replacements = [
                item for item in job.get('enhancements') or []
                if str((item.get('enhanced') or {}).get('variant_mode') or '').lower() == 'replace'
            ]
            if replacements and data.get('variant_replacement_confirmation') != 'REPLACE VARIANTS':
                return jsonify({
                    'success': False,
                    'error': 'Variant replacement requires the additional confirmation REPLACE VARIANTS.',
                }), 400
            if not job.get('snapshot_id'):
                products = job.get('products') or []
                originals = []
                for item in job.get('enhancements') or []:
                    original_index = item.get('originalIndex')
                    target_id = (item.get('enhanced') or {}).get('product_id')
                    original = next(
                        (p for p in products if (p.get('id') or p.get('product_id')) == target_id),
                        None,
                    )
                    if original is None:
                        original = next(
                            (p for p in products if str(p.get('originalIndex')) == str(original_index)),
                            None,
                        )
                    if original:
                        originals.append(original)
                job['snapshot_id'] = _create_bulk_edit_snapshot(
                    shop, originals, job_id=job_id, retention_days=90
                )
            if job.get('status') not in ('enhanced', 'applied', 'error'):
                return jsonify({'success': False, 'error': f"Job cannot apply from {job.get('status')}"}), 400
            if job.get('status') == 'applied':
                job['apply_index'] = 0
                job['apply_results'] = []
        _save_csv_enhance_jobs()
        thread = threading.Thread(
            target=_run_csv_apply_job,
            args=(job_id, shop.shop_domain, shop.access_token),
            daemon=True,
            name=f"csv-apply-{job_id[:8]}",
        )
        thread.start()
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Apply bulk SEO job error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/enhance_csv_product', methods=['POST'])
@login_required
def enhance_csv_product():
    """Analyze product image and generate AI-enhanced metadata"""
    import requests
    import tempfile
    
    try:
        data = request.get_json()
        if not data or 'product' not in data:
            return jsonify({'success': False, 'error': 'Product data required'}), 400
        
        product = data['product']
        custom_prompt = data.get('custom_prompt', '')
        field_settings = data.get('field_settings', {})
        image_url = product.get('image_url', '')
        
        if not image_url:
            return jsonify({'success': False, 'error': 'No image URL in product'}), 400
        
        if not is_safe_image_url(image_url):
            logger.warning(f"Blocked unsafe image URL: {image_url[:100]}")
            return jsonify({'success': False, 'error': 'Image URL not from allowed domain'}), 400
        
        logger.info(f"Downloading image for enhancement: {image_url[:100]}...")
        
        try:
            response = requests.get(image_url, timeout=30)
            response.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to download image: {e}")
            return jsonify({'success': False, 'error': f'Failed to download image: {str(e)}'}), 400
        
        with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp_file:
            tmp_file.write(response.content)
            tmp_path = tmp_file.name
        
        try:
            # CRITICAL: Validate API key before starting AI processing
            import os
            if not os.environ.get("GEMINI_API_KEY"):
                error_msg = "GEMINI_API_KEY is not set. Cannot generate AI metadata. Please ensure the server was started with run_server.py or set the environment variable."
                logger.error(f"❌ {error_msg}")
                return jsonify({'success': False, 'error': error_msg}), 500
            
            # Fetch Shopify collections so AI can suggest them (unless user turned collections off)
            collections_enabled = data.get('collections_enabled', True)
            if not collections_enabled:
                available_collections = []
                logger.info("Enhance: collections disabled by user - no collections will be assigned")
            else:
                try:
                    available_collections = get_collections()
                    logger.info(f"Enhance: loaded {len(available_collections)} Shopify collections")
                except Exception as e:
                    logger.error(f"Enhance: failed to fetch Shopify collections: {e}. Continuing with empty list.")
                    available_collections = []
            
            category_gid_for_prompt = product.get('category_gid') or resolve_category_name_to_gid(product.get('product_category', ''))
            category_attribute_options = {}
            if category_gid_for_prompt:
                try:
                    shop = get_current_shop()
                    category_attribute_options = get_category_attribute_options(
                        category_gid_for_prompt,
                        shop_domain=shop.shop_domain if shop else None,
                        access_token=shop.access_token if shop else None,
                    )
                except Exception as exc:
                    logger.warning("Enhance category attribute lookup failed for %s: %s", category_gid_for_prompt, exc)
            metadata = generate_product_metadata(
                tmp_path,
                custom_prompt,
                available_collections,
                category_attribute_options=category_attribute_options,
            )
            metadata['collections'] = _resolve_product_collections(
                metadata, available_collections, log_prefix="Enhance collections",
                mandatory_collections=_mandatory_collections_from_prompt(custom_prompt),
            )
            if not collections_enabled:
                metadata['collections'] = []
            
            # CRITICAL: Check if metadata generation failed immediately
            if not metadata:
                error_msg = "Failed to generate metadata from AI. Please check your GEMINI_API_KEY and ensure the API is accessible."
                logger.error(f"❌ {error_msg}")
                logger.error(f"❌ Image path: {tmp_path}")
                return jsonify({'success': False, 'error': error_msg}), 500
            
            # Get vendor from request (use form value or default)
            vendor = data.get('vendor', 'Listing Cannon')
            
            # Get SKU pattern from request
            sku_pattern = data.get('sku_pattern', '')
            
            # Generate variant SKU if pattern provided
            variant_sku = ''
            if sku_pattern:
                # Use the handle or title to create unique SKU
                handle = product.get('handle', '')
                if handle:
                    variant_sku = f"{sku_pattern}-{handle[:20].upper().replace('-', '')}"
                else:
                    variant_sku = sku_pattern
            
            # Get the AI-generated title for SEO title base
            ai_title = metadata.get('title', product.get('title', ''))
            
            # Helper: resolve field value using field_settings for enhance flow
            # "auto" = use AI value, "manual" = use manual_value, "do_not_edit" = keep original CSV value
            def resolve_enhance_field(field_id, ai_value, original_key=None, fallback=''):
                fs = field_settings.get(field_id, {})
                mode = fs.get('mode', 'auto')
                if mode == 'manual':
                    return fs.get('manual_value', fallback)
                elif mode == 'clear':
                    return ''
                elif mode == 'if_empty':
                    existing = product.get(original_key, fallback) if original_key else fallback
                    return existing if str(existing or '').strip() else (ai_value if ai_value is not None else fallback)
                elif mode == 'do_not_edit':
                    # Keep original value from the uploaded CSV product row
                    if original_key:
                        return product.get(original_key, fallback)
                    return fallback
                else:  # auto
                    return ai_value if ai_value is not None else fallback
            
            # Format tags: ensure list is comma-separated string
            raw_tags = metadata.get('tags', [])
            if isinstance(raw_tags, list):
                ai_tags_str = ', '.join(str(t) for t in raw_tags)
            else:
                ai_tags_str = str(raw_tags)
            
            # Format collections: ensure list is comma-separated string
            raw_collections = metadata.get('collections', [])
            if isinstance(raw_collections, list):
                ai_collections_str = ', '.join(str(c) for c in raw_collections)
            else:
                ai_collections_str = str(raw_collections)
            
            # Extract product metafields from AI response (Gemini always generates these)
            ai_metafields = metadata.get('metafields', {})
            
            product_category = metadata.get('category', 'Home & Garden > Decor > Artwork > Posters, Prints, & Visual Artwork')
            category_gid = resolve_category_name_to_gid(product_category) or category_gid_for_prompt or product.get('category_gid', '')

            source_columns = dict(product.get('source_columns') or {})
            recommendations = {}
            try:
                shop = get_current_shop()
                if shop:
                    recommendations = recommend_products_for_discovery(
                        metadata,
                        current_product_id=product.get('id', ''),
                        shop_domain=shop.shop_domain,
                        access_token=shop.access_token,
                    )
            except Exception as exc:
                logger.warning("Discovery recommendation lookup failed: %s", exc)
            variants_for_apply = copy.deepcopy(product.get('variants') or [])
            if _has_weak_single_default_variant(product):
                variants_for_apply = _default_bulk_variant_rows(
                    product,
                    sku_pattern=sku_pattern,
                    default_location_id=product.get('variant_inventory_location_id', ''),
                )
            elif variant_sku:
                for index, variant in enumerate(variants_for_apply):
                    if not variant.get('sku'):
                        variant['sku'] = f"{variant_sku}-V{index + 1}" if len(variants_for_apply) > 1 else variant_sku
            media_for_apply = copy.deepcopy(product.get('media') or [])
            ai_alt_text = metadata.get('alt_text', '')
            if media_for_apply and ai_alt_text:
                media_for_apply[0]['alt'] = ai_alt_text
            enhanced_data = {
                'product_id': product.get('id', ''),
                'status': product.get('status', ''),
                'handle': product.get('handle', ''),
                'title': ai_title,
                'body_html': format_description_html(metadata.get('description', product.get('body_html', ''))),
                'tags': resolve_enhance_field('tags', ai_tags_str, 'tags'),
                'vendor': vendor,
                'product_type': _bulk_product_type(product),
                'product_category': product_category,
                'category_gid': category_gid,
                'seo_title': resolve_enhance_field('seo_title', metadata.get('seo_title', _word_safe_truncate(ai_title, 70) if ai_title else ''), 'seo_title'),
                'seo_description': metadata.get('meta_description', ''),
                'image_alt_text': metadata.get('alt_text', ''),
                'collections': resolve_enhance_field('collections', ai_collections_str),
                'variant_id': ((product.get('variants') or [{}])[0] or {}).get('id', ''),
                'google_product_category': resolve_enhance_field('google_product_category', metadata.get('google_product_category', '')),
                'gender': resolve_enhance_field('gender', metadata.get('gender', 'Unisex')),
                'age_group': resolve_enhance_field('age_group', metadata.get('age_group', 'Adult')),
                'condition': resolve_enhance_field('condition', metadata.get('condition', 'New')),
                'custom_product': resolve_enhance_field('custom_product', metadata.get('custom_product', 'TRUE')),
                'custom_label_0': resolve_enhance_field('custom_label_0', metadata.get('custom_label_0', '')),
                'custom_label_1': resolve_enhance_field('custom_label_1', metadata.get('custom_label_1', '')),
                'custom_label_2': resolve_enhance_field('custom_label_2', metadata.get('custom_label_2', '')),
                'custom_label_3': resolve_enhance_field('custom_label_3', metadata.get('custom_label_3', '')),
                'custom_label_4': resolve_enhance_field('custom_label_4', metadata.get('custom_label_4', '')),
                'variant_sku': variant_sku,
                'variant_barcode': product.get('variant_barcode', ''),
                'variant_price': product.get('variant_price', ''),
                'variant_compare_at_price': product.get('variant_compare_at_price', ''),
                'variant_inventory_quantity': product.get('variant_inventory_quantity', ''),
                'variant_inventory_policy': product.get('variant_inventory_policy', ''),
                'variant_taxable': product.get('variant_taxable', ''),
                'variant_weight': product.get('variant_weight', ''),
                'variant_weight_unit': product.get('variant_weight_unit', ''),
                # Product metafields (AI-generated from image analysis)
                'metafield_color': ai_metafields.get('color', ''),
                'metafield_theme': ai_metafields.get('theme', ''),
                'metafield_frame_style': ai_metafields.get('frame_style', ''),
                'metafield_condition': ai_metafields.get('condition', 'New'),
                'metafield_decoration_material': ai_metafields.get('decoration_material', ''),
                'metafield_artwork_frame_material': ai_metafields.get('artwork_frame_material', ''),
                'variant_mode': 'merge',
                'original_variant_ids': [v.get('id') for v in variants_for_apply if v.get('id')],
                'metafield_art_movement': ai_metafields.get('art_movement', ''),
                'metafield_art_style': ai_metafields.get('art_style', ''),
                'metafield_artwork_authenticity': ai_metafields.get('artwork_authenticity', ''),
                'metafield_material': ai_metafields.get('material', ''),
                'metafield_orientation': ai_metafields.get('orientation', ''),
                'category_attribute_picks': metadata.get('category_attribute_picks', {}),
                'variants_json': json.dumps(variants_for_apply, indent=2),
                'media_json': json.dumps(media_for_apply, indent=2),
                'price_lists_json': product.get('price_lists_json', '[]'),
                'source_columns': source_columns,
            }
            source_overlay = {
                'Title': enhanced_data['title'],
                'Body (HTML)': enhanced_data['body_html'],
                'Tags': enhanced_data['tags'],
                'Vendor': enhanced_data['vendor'],
                'Product Category': enhanced_data['product_category'],
                'Type': enhanced_data['product_type'],
                'SEO Title': enhanced_data['seo_title'],
                'SEO Description': enhanced_data['seo_description'],
                'Image Alt Text': enhanced_data['image_alt_text'],
                'Google Shopping / Google Product Category': enhanced_data['google_product_category'],
                'Google Shopping / Gender': enhanced_data['gender'],
                'Google Shopping / Age Group': enhanced_data['age_group'],
                'Google Shopping / Condition': enhanced_data['condition'],
                'Google Shopping / Custom Product': enhanced_data['custom_product'],
                'Google Shopping / Custom Label 0': enhanced_data['custom_label_0'],
                'Google Shopping / Custom Label 1': enhanced_data['custom_label_1'],
                'Google Shopping / Custom Label 2': enhanced_data['custom_label_2'],
                'Google Shopping / Custom Label 3': enhanced_data['custom_label_3'],
                'Google Shopping / Custom Label 4': enhanced_data['custom_label_4'],
                'Variant SKU': enhanced_data['variant_sku'],
                'Art movement (product.metafields.shopify.art-movement)': enhanced_data['metafield_art_movement'],
                'Art style (product.metafields.shopify.art-style)': enhanced_data['metafield_art_style'],
                'Artwork authenticity (product.metafields.shopify.artwork-authenticity)': enhanced_data['metafield_artwork_authenticity'],
                'Material (product.metafields.shopify.material)': enhanced_data['metafield_material'],
                'Orientation (product.metafields.shopify.orientation)': enhanced_data['metafield_orientation'],
            }
            for header, value in source_overlay.items():
                _set_bulk_source_column(source_columns, header, value)
            _apply_ai_source_column_defaults(source_columns, enhanced_data)
            for key, value in _generic_metafields_from_product(product).items():
                enhanced_data.setdefault(key, value)
            _apply_ai_metafield_defaults(enhanced_data, ai_metafields)
            _apply_discovery_recommendation_fields(enhanced_data, recommendations)
            _apply_discovery_source_columns(source_columns, enhanced_data)
            
            logger.info(f"CSV enhance metafields: color={ai_metafields.get('color')}, theme={ai_metafields.get('theme')}, "
                        f"frame_style={ai_metafields.get('frame_style')}, condition={ai_metafields.get('condition')}, "
                        f"decoration_material={ai_metafields.get('decoration_material')}, "
                        f"artwork_frame_material={ai_metafields.get('artwork_frame_material')}")
            
            logger.info(f"Enhanced product: {enhanced_data['title']}")
            
            return jsonify({
                'success': True,
                'enhanced_data': enhanced_data
            })
            
        finally:
            import os
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        
    except Exception as e:
        logger.error(f"Enhance product error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/apply_enhanced_shopify_products', methods=['POST'])
@login_required
def apply_enhanced_shopify_products():
    """Apply enhanced product SEO metadata directly to Shopify."""
    try:
        shop = get_current_shop()
        if not shop:
            return jsonify({'success': False, 'error': 'No Shopify store connected. Connect a store first.'}), 400

        data = request.get_json() or {}
        enhancements = data.get('enhancements', [])
        if not enhancements:
            return jsonify({'success': False, 'error': 'No enhanced products to apply'}), 400

        expected_confirmation = f"APPLY {len(enhancements)}"
        if data.get('confirmation') != expected_confirmation:
            return jsonify({
                'success': False,
                'error': f"Confirmation required. Type {expected_confirmation} exactly.",
            }), 400
        replacements = [
            item for item in enhancements
            if str((item.get('enhanced') or {}).get('variant_mode') or '').lower() == 'replace'
        ]
        if replacements and data.get('variant_replacement_confirmation') != 'REPLACE VARIANTS':
            return jsonify({
                'success': False,
                'error': 'Variant replacement requires the additional confirmation REPLACE VARIANTS.',
            }), 400
        originals = data.get('originals') or []
        if len(originals) != len(enhancements):
            return jsonify({
                'success': False,
                'error': 'A complete before-state is required before live Shopify changes can start.',
            }), 400
        snapshot_id = _create_bulk_edit_snapshot(shop, originals, retention_days=90)
        results = []
        success_count = 0
        failed_count = 0

        for item in enhancements:
            enhanced = _resolve_enhanced_category(item.get('enhanced') or {})
            product_id = enhanced.get('product_id') or item.get('product_id')
            if not product_id:
                failed_count += 1
                results.append({
                    'success': False,
                    'title': enhanced.get('title') or enhanced.get('handle') or 'Unknown product',
                    'error': 'Missing Shopify product id',
                })
                continue

            result = update_product_seo_metadata(
                product_id,
                enhanced,
                shop_domain=shop.shop_domain,
                access_token=shop.access_token,
            )
            if result.get('success'):
                _apply_category_attribute_metafields(
                    enhanced,
                    shop_domain=shop.shop_domain,
                    access_token=shop.access_token,
                )
            result['title'] = enhanced.get('title') or enhanced.get('handle') or product_id
            result['product_id'] = product_id
            results.append(result)
            if result.get('success'):
                success_count += 1
            else:
                failed_count += 1

            # Keep direct writes gentle on Shopify and the Render process.
            time.sleep(0.2)

        return jsonify({
            'success': failed_count == 0,
            'success_count': success_count,
            'snapshot_id': snapshot_id,
            'failed_count': failed_count,
            'results': results,
        })
    except Exception as e:
        logger.error(f"Apply enhanced Shopify products error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/generate_enhanced_csv', methods=['POST'])
@login_required
def generate_enhanced_csv():
    """Generate enhanced CSV by merging AI metadata with original CSV.
    
    Supports optional variant override mode: when variant_override_enabled is True,
    all existing variant rows are stripped and replaced with user-defined variants.
    This effectively turns the CSV enhancement into a bulk repricing tool.
    """
    import csv
    import io
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        original_csv = data.get('original_csv', {})
        enhancements = data.get('enhancements', [])
        field_settings = data.get('field_settings', {})
        use_main_image_per_variant = data.get('use_main_image_per_variant', False)
        
        # Variant override parameters
        variant_override_enabled = data.get('variant_override_enabled', False)
        variant_option_name = data.get('variant_option_name', 'Size').strip() or 'Size'
        variant_definitions = data.get('variant_definitions', [])  # [{name, price, quantity}]
        variant_inventory_policy = data.get('variant_inventory_policy', 'deny')
        
        headers = list(original_csv.get('headers', []))
        rows = original_csv.get('rows', [])
        
        if not headers or not rows:
            return jsonify({'success': False, 'error': 'Invalid CSV data'}), 400
        
        if variant_override_enabled:
            logger.info(f"Variant override enabled: {len(variant_definitions)} variant(s), option='{variant_option_name}', policy='{variant_inventory_policy}'")
        
        # Build a map of handle -> main listing image (Image Src from the first row per handle)
        # Used when use_main_image_per_variant is True to copy the main image to every variant row
        main_image_by_handle = {}
        if use_main_image_per_variant:
            for row in rows:
                handle = row.get('Handle', '').strip()
                if handle and handle not in main_image_by_handle:
                    image_src = row.get('Image Src', '').strip()
                    if image_src:
                        main_image_by_handle[handle] = image_src
            logger.info(f"Main image per variant enabled - found images for {len(main_image_by_handle)} products")
        
        # Ensure all columns we want to write exist in the headers
        # Map of enhanced_data keys -> CSV column names
        extra_columns = {
            'Collection': 'collections',
            'Google Shopping / Google Product Category': 'google_product_category',
            'Google Shopping / Gender': 'gender',
            'Google Shopping / Age Group': 'age_group',
            'Google Shopping / Condition': 'condition',
            'Google Shopping / Custom Product': 'custom_product',
            'Google Shopping / Custom Label 0': 'custom_label_0',
            'Google Shopping / Custom Label 1': 'custom_label_1',
            'Google Shopping / Custom Label 2': 'custom_label_2',
            'Google Shopping / Custom Label 3': 'custom_label_3',
            'Google Shopping / Custom Label 4': 'custom_label_4',
            # Product metafields (Shopify CSV import format)
            'Metafield: custom.color [single_line_text_field]': 'metafield_color',
            'Metafield: custom.theme [single_line_text_field]': 'metafield_theme',
            'Metafield: custom.frame_style [single_line_text_field]': 'metafield_frame_style',
            'Metafield: custom.condition [single_line_text_field]': 'metafield_condition',
            'Metafield: custom.decoration_material [single_line_text_field]': 'metafield_decoration_material',
            'Metafield: custom.artwork_frame_material [single_line_text_field]': 'metafield_artwork_frame_material',
            'Google: Custom Product (product.metafields.mm-google-shopping.custom_product)': 'custom_product',
            'Color (product.metafields.custom.color)': 'metafield_color',
            'Frame Style (product.metafields.custom.frame_style)': 'metafield_frame_style',
            'Theme (product.metafields.custom.theme)': 'metafield_theme',
            'Art movement (product.metafields.shopify.art-movement)': 'metafield_art_movement',
            'Art style (product.metafields.shopify.art-style)': 'metafield_art_style',
            'Artwork authenticity (product.metafields.shopify.artwork-authenticity)': 'metafield_artwork_authenticity',
            'Color (product.metafields.shopify.color-pattern)': 'metafield_color',
            'Frame style (product.metafields.shopify.frame-style)': 'metafield_frame_style',
            'Material (product.metafields.shopify.material)': 'metafield_material',
            'Orientation (product.metafields.shopify.orientation)': 'metafield_orientation',
            'Theme (product.metafields.shopify.theme)': 'metafield_theme',
            'Metafield: global.title_tag [single_line_text_field]': 'seo_title',
            'Metafield: global.description_tag [multi_line_text_field]': 'seo_description',
            'Complementary products (product.metafields.shopify--discovery--product_recommendation.complementary_products)': 'mf__shopify--discovery--product_recommendation__complementary_products',
            'Related products (product.metafields.shopify--discovery--product_recommendation.related_products)': 'mf__shopify--discovery--product_recommendation__related_products',
            'Related products settings (product.metafields.shopify--discovery--product_recommendation.related_products_display)': 'mf__shopify--discovery--product_recommendation__related_products_display',
            'Search product boosts (product.metafields.shopify--discovery--product_search_boost.queries)': 'mf__shopify--discovery--product_search_boost__queries',
        }
        for col_name in extra_columns:
            if col_name not in headers:
                headers.append(col_name)
        
        # When variant override is enabled, ensure variant-related columns exist in headers
        if variant_override_enabled and variant_definitions:
            variant_columns = [
                'Option1 Name', 'Option1 Value',
                'Variant SKU', 'Variant Price', 'Variant Inventory Qty',
                'Variant Inventory Policy', 'Variant Fulfillment Service',
                'Variant Requires Shipping', 'Variant Taxable',
            ]
            for vc in variant_columns:
                if vc not in headers:
                    headers.append(vc)
        
        enhancement_map = {}
        for item in enhancements:
            orig_index = item.get('originalIndex')
            enhanced = item.get('enhanced', {})
            handle = enhanced.get('handle', '')
            if handle:
                enhancement_map[handle] = enhanced
        
        # Standard column mapping: enhanced_data key -> CSV header name
        column_map = [
            ('title', 'Title'),
            ('body_html', 'Body (HTML)'),
            ('tags', 'Tags'),
            ('product_category', 'Product Category'),
            ('product_type', 'Type'),
            ('seo_title', 'SEO Title'),
            ('seo_description', 'SEO Description'),
            ('vendor', 'Vendor'),
            ('collections', 'Collection'),
            ('google_product_category', 'Google Shopping / Google Product Category'),
            ('gender', 'Google Shopping / Gender'),
            ('age_group', 'Google Shopping / Age Group'),
            ('condition', 'Google Shopping / Condition'),
            ('custom_product', 'Google Shopping / Custom Product'),
            ('custom_label_0', 'Google Shopping / Custom Label 0'),
            ('custom_label_1', 'Google Shopping / Custom Label 1'),
            ('custom_label_2', 'Google Shopping / Custom Label 2'),
            ('custom_label_3', 'Google Shopping / Custom Label 3'),
            ('custom_label_4', 'Google Shopping / Custom Label 4'),
            # Product metafields
            ('metafield_color', 'Metafield: custom.color [single_line_text_field]'),
            ('metafield_theme', 'Metafield: custom.theme [single_line_text_field]'),
            ('metafield_frame_style', 'Metafield: custom.frame_style [single_line_text_field]'),
            ('metafield_condition', 'Metafield: custom.condition [single_line_text_field]'),
            ('metafield_decoration_material', 'Metafield: custom.decoration_material [single_line_text_field]'),
            ('metafield_artwork_frame_material', 'Metafield: custom.artwork_frame_material [single_line_text_field]'),
            ('custom_product', 'Google: Custom Product (product.metafields.mm-google-shopping.custom_product)'),
            ('metafield_color', 'Color (product.metafields.custom.color)'),
            ('metafield_frame_style', 'Frame Style (product.metafields.custom.frame_style)'),
            ('metafield_theme', 'Theme (product.metafields.custom.theme)'),
            ('metafield_art_movement', 'Art movement (product.metafields.shopify.art-movement)'),
            ('metafield_art_style', 'Art style (product.metafields.shopify.art-style)'),
            ('metafield_artwork_authenticity', 'Artwork authenticity (product.metafields.shopify.artwork-authenticity)'),
            ('metafield_color', 'Color (product.metafields.shopify.color-pattern)'),
            ('metafield_frame_style', 'Frame style (product.metafields.shopify.frame-style)'),
            ('metafield_material', 'Material (product.metafields.shopify.material)'),
            ('metafield_orientation', 'Orientation (product.metafields.shopify.orientation)'),
            ('metafield_theme', 'Theme (product.metafields.shopify.theme)'),
            ('seo_title', 'Metafield: global.title_tag [single_line_text_field]'),
            ('seo_description', 'Metafield: global.description_tag [multi_line_text_field]'),
            ('mf__shopify--discovery--product_recommendation__complementary_products', 'Complementary products (product.metafields.shopify--discovery--product_recommendation.complementary_products)'),
            ('mf__shopify--discovery--product_recommendation__related_products', 'Related products (product.metafields.shopify--discovery--product_recommendation.related_products)'),
            ('mf__shopify--discovery--product_recommendation__related_products_display', 'Related products settings (product.metafields.shopify--discovery--product_recommendation.related_products_display)'),
            ('mf__shopify--discovery--product_search_boost__queries', 'Search product boosts (product.metafields.shopify--discovery--product_search_boost.queries)'),
        ]
        
        enhanced_rows = []
        current_handle = None
        current_variant_counter = 0
        
        for row in rows:
            handle = row.get('Handle', '').strip()
            
            # --- Variant override mode: skip all original variant rows ---
            # Variant rows have an empty Handle; they belong to the previous product.
            # We will inject replacement variants after processing the product row.
            if variant_override_enabled and variant_definitions and not handle:
                continue  # skip original variant row
            
            new_row = dict(row)
            # Ensure all headers have a value (even empty) so DictWriter doesn't error
            for h in headers:
                if h not in new_row:
                    new_row[h] = ''
            
            if handle:
                current_handle = handle
                current_variant_counter = 0
            else:
                current_variant_counter += 1
            
            if current_handle and current_handle in enhancement_map:
                enhanced = enhancement_map[current_handle]
                
                if handle:
                    source_columns = enhanced.get('source_columns') or {}
                    if isinstance(source_columns, dict):
                        for csv_col, val in source_columns.items():
                            if csv_col not in headers:
                                headers.append(csv_col)
                            new_row[csv_col] = _ensure_string(val)
                    # Apply enhanced values to product row (first row for this handle)
                    for data_key, csv_col in column_map:
                        if csv_col in headers:
                            val = enhanced.get(data_key, '')
                            if val:
                                new_row[csv_col] = _ensure_string(val)
                
                if 'Image Alt Text' in headers and enhanced.get('image_alt_text'):
                    new_row['Image Alt Text'] = enhanced['image_alt_text']
                
                # Apply variant SKU to all variant rows for this product
                if 'Variant SKU' in headers and enhanced.get('variant_sku'):
                    base_sku = enhanced['variant_sku']
                    variant_suffix = f"-V{current_variant_counter}" if current_variant_counter > 0 else ""
                    new_row['Variant SKU'] = f"{base_sku}{variant_suffix}"
            
            # Set main listing image on every variant row when option is enabled
            if use_main_image_per_variant and current_handle and current_handle in main_image_by_handle:
                if 'Image Src' in headers:
                    new_row['Image Src'] = main_image_by_handle[current_handle]
            
            # --- Variant override: set Option1 on the product row (first variant) ---
            if variant_override_enabled and variant_definitions and handle:
                first_variant = variant_definitions[0]
                new_row['Option1 Name'] = variant_option_name
                new_row['Option1 Value'] = first_variant.get('name', '')
                new_row['Variant Price'] = str(first_variant.get('price', '0'))
                if 'Variant Inventory Qty' in headers:
                    new_row['Variant Inventory Qty'] = str(first_variant.get('quantity', 0))
                if 'Variant Inventory Policy' in headers:
                    new_row['Variant Inventory Policy'] = variant_inventory_policy
                if 'Variant Fulfillment Service' in headers:
                    new_row['Variant Fulfillment Service'] = 'manual'
                if 'Variant Requires Shipping' in headers:
                    new_row['Variant Requires Shipping'] = 'TRUE'
                if 'Variant Taxable' in headers:
                    new_row['Variant Taxable'] = 'TRUE'
                # SKU for first variant
                if 'Variant SKU' in headers:
                    enhanced = enhancement_map.get(current_handle, {})
                    base_sku = enhanced.get('variant_sku', '')
                    new_row['Variant SKU'] = base_sku if base_sku else ''
            
            enhanced_rows.append(new_row)
            
            # --- Variant override: inject additional variant rows after the product row ---
            if variant_override_enabled and variant_definitions and handle and len(variant_definitions) > 1:
                enhanced = enhancement_map.get(current_handle, {})
                base_sku = enhanced.get('variant_sku', '')
                product_image = main_image_by_handle.get(current_handle, '') if use_main_image_per_variant else ''
                
                for v_idx, v_def in enumerate(variant_definitions[1:], start=1):
                    variant_row = {h: '' for h in headers}
                    # Handle is empty for variant rows (Shopify CSV convention)
                    variant_row['Handle'] = ''
                    variant_row['Option1 Name'] = variant_option_name
                    variant_row['Option1 Value'] = v_def.get('name', '')
                    variant_row['Variant Price'] = str(v_def.get('price', '0'))
                    if 'Variant Inventory Qty' in headers:
                        variant_row['Variant Inventory Qty'] = str(v_def.get('quantity', 0))
                    if 'Variant Inventory Policy' in headers:
                        variant_row['Variant Inventory Policy'] = variant_inventory_policy
                    if 'Variant Fulfillment Service' in headers:
                        variant_row['Variant Fulfillment Service'] = 'manual'
                    if 'Variant Requires Shipping' in headers:
                        variant_row['Variant Requires Shipping'] = 'TRUE'
                    if 'Variant Taxable' in headers:
                        variant_row['Variant Taxable'] = 'TRUE'
                    # SKU with variant suffix
                    if 'Variant SKU' in headers and base_sku:
                        variant_row['Variant SKU'] = f"{base_sku}-V{v_idx}"
                    # Copy main image to variant row if option is enabled
                    if use_main_image_per_variant and product_image:
                        variant_row['Image Src'] = product_image
                    
                    enhanced_rows.append(variant_row)
        
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=headers)
        writer.writeheader()
        writer.writerows(enhanced_rows)
        
        csv_content = output.getvalue()
        
        response = make_response(csv_content)
        response.headers['Content-Type'] = 'text/csv; charset=utf-8'
        response.headers['Content-Disposition'] = 'attachment; filename=shopify_enhanced_import.csv'
        
        if variant_override_enabled:
            logger.info(f"Generated enhanced CSV with variant override: {len(enhanced_rows)} rows ({len(variant_definitions)} variants per product)")
        else:
            logger.info(f"Generated enhanced CSV with {len(enhanced_rows)} rows")
        
        return response
        
    except Exception as e:
        logger.error(f"Generate enhanced CSV error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

# Initialize database and auth after app is fully loaded (avoids circular import)
with app.app_context():
    import models  # noqa: F401
    db.create_all()
    # Run comprehensive profile table migration (adds ALL missing columns)
    _ensure_profile_all_columns()
import auth  # noqa: E402
auth.init_app(app)  # Attach login_manager to this app instance so current_user works

# Register Shopify OAuth routes (multi-tenant store connection)
from shopify_oauth import init_oauth_routes  # noqa: E402
init_oauth_routes(app)

# Start the queue worker thread now that the app is fully initialized.
# This also handles the local-dev case; for Gunicorn, _ensure_worker_running()
# is called again in start_processing() in case the fork killed this thread.
_ensure_worker_running()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
