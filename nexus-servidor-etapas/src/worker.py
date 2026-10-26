"""Standalone persistent worker; no browser or web-server process required."""
import argparse
import json
import logging
import logging.handlers
import os
import signal
import threading

from src import db, pipeline


def main():
    parser = argparse.ArgumentParser(description='Trabalhador de atualização SENATRAN')
    parser.add_argument('--once', action='store_true', help='Processar no máximo uma execução pronta')
    parser.add_argument('--no-schedule', action='store_true', help='Não criar verificações periódicas')
    parser.add_argument('--discover', action='store_true', help='Enfileirar descoberta desde janeiro de 2026')
    parser.add_argument('--import-file', type=str, help='Importar arquivo local preservado')
    parser.add_argument('--reference', help='Competência AAAA-MM do arquivo local')
    parser.add_argument('--retry', help='Retomar execução falha pelo ID')
    parser.add_argument('--reset-epoch', action='store_true', help='Invalidar reservas após restore, com escritores parados')
    args = parser.parse_args()
    db.init_db()
    pipeline.initialize()
    logs = db.ROOT / 'data' / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(logs / 'worker.log', maxBytes=5_000_000, backupCount=5, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()],
                        format='%(asctime)s %(levelname)s %(message)s')
    if args.reset_epoch:
        print(json.dumps(pipeline.reset_epoch()))
        return
    if args.import_file:
        if not args.reference:
            parser.error('--import-file exige --reference')
        from pathlib import Path
        path = str(Path(args.import_file).resolve())
        job = pipeline.enqueue('import_file', {'path': path, 'reference': args.reference},
                               'cli-file:' + pipeline.digest([path, args.reference, pipeline.timestamp()]))
        print(json.dumps({'queued_job': job['id']}))
    if args.retry:
        pipeline.retry_job(args.retry)
    if args.discover:
        print(json.dumps({'queued_job': pipeline.backfill()['id']}))
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    while not stopped.is_set():
        try:
            pipeline.record_worker_health('idle')
            if not args.no_schedule:
                pipeline.schedule_due()
            result = pipeline.run_once()
            if result:
                logging.info('job=%s kind=%s status=%s error=%s', result['id'], result['kind'], result['status'], result.get('error'))
            if args.once:
                break
            if not result:
                stopped.wait(15)
        except Exception:
            logging.exception('Falha no ciclo; a fila persistida será consultada novamente')
            if args.once:
                raise
            stopped.wait(30)
    pipeline.record_worker_health('stopped')


if __name__ == '__main__':
    main()
