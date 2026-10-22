"""Drain durable work only while requested jobs exist; no periodic discovery."""
import logging
import threading
from sqlalchemy import select
from src import db, pipeline, pipeline_schema as schema

log = logging.getLogger(__name__)
_lock = threading.Lock()
_thread = None
_enabled = False
_stop = threading.Event()
_wake = threading.Event()


def start(check_catalog=True):
    global _enabled
    pipeline.initialize()
    from src.senatran_months import retire_catalog_retries, request_refresh
    retire_catalog_retries()
    _stop.clear()
    _enabled = True
    if check_catalog:
        request_refresh(startup=True)
    try:
        pipeline.enqueue_classifier_refresh()
    except Exception:
        log.exception('Falha ao agendar atualização da classificação automática')
    pipeline.record_worker_health('idle')
    notify()  # Also recover unfinished work after an interruption.


def notify():
    global _thread
    if not _enabled or _stop.is_set():
        return
    with _lock:
        _wake.set()
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_drain, name='i9-requested-work', daemon=True)
            _thread.start()


def _drain():
    global _thread
    try:
        while not _stop.is_set():
            _wake.clear()
            try:
                result = pipeline.run_once()
                if result:
                    continue
                with db.engine.connect() as c:
                    pending = c.execute(select(schema.job.c.id).where(schema.job.c.status.in_(('queued', 'running'))).limit(1)).first()
                with _lock:
                    if not pending and not _wake.is_set():
                        _thread = None
                        pipeline.record_worker_health('idle')
                        return
                # Only wait when an actual job is waiting for retry/lease expiry.
                _wake.wait(5)
            except Exception:
                log.exception('Falha ao processar trabalho solicitado')
                _wake.wait(15)
    finally:
        with _lock:
            if _thread is threading.current_thread():
                _thread = None


def stop():
    global _enabled
    _enabled = False
    _stop.set()
    _wake.set()
    thread = _thread
    if thread:
        thread.join(timeout=5)
