import os
import threading
import uuid
import time
from sqlalchemy import select, insert, update
from src.db import engine, runs, snapshots, ROOT
from src.catalog import seed_catalog
from src.connectors.public import catalog, download, now
from src.transform.ingest import reference_of, sync_locations, process_file

lock = threading.Lock()

def start_run(reference=None):
    run_id = str(uuid.uuid4())
    with engine.begin() as c:
        c.execute(insert(runs).values(id=run_id, reference=reference, started_at=now(), status='EXECUTANDO', stage='Aguardando processamento', details={}))
    return run_id

def run_monthly(reference=None, run_id=None):
    if not lock.acquire(blocking=False): raise ValueError('Já há uma atualização em execução')
    run_id = run_id or start_run(reference)
    started=time.perf_counter()
    timings={}
    try:
        if os.getenv('ALLOW_SOURCE_DOWNLOAD', 'false').lower() != 'true':
            raise ValueError('Download de fonte não habilitado. Configure ALLOW_SOURCE_DOWNLOAD=true para processar a fonte no servidor.')
        seed_catalog()
        resources = [(reference_of(r), r) for r in catalog()]
        resources = [(ref,r) for ref,r in resources if ref and (reference is None or ref==reference)]
        if not resources: raise ValueError('Nenhum recurso encontrado para a referência solicitada')
        ref, resource = max(resources, key=lambda x:x[0])
        with engine.begin() as c:
            c.execute(update(runs).where(runs.c.id==run_id).values(reference=ref,stage='Consultando fonte'))
            if c.execute(select(snapshots).where(snapshots.c.reference==ref)).first():
                c.execute(update(runs).where(runs.c.id==run_id).values(status='SEM_NOVIDADE',finished_at=now(),stage='Referência já publicada'))
                return {'run_id':run_id, 'status':'SEM_NOVIDADE','reference':ref}
        phase=time.perf_counter()
        sync_locations()
        timings['locations_seconds']=round(time.perf_counter()-phase,3)
        with engine.begin() as c:
            c.execute(update(runs).where(runs.c.id==run_id).values(stage='Baixando fonte no servidor',details={'url':resource['url'],'size':resource.get('size')}))
        path = ROOT / 'data/raw' / ref / 'senatran' / (resource['id'] + '.zip')
        phase=time.perf_counter()
        digest = download(resource['url'],path)
        timings['download_seconds']=round(time.perf_counter()-phase,3)
        phase=time.perf_counter()
        result = process_file(path,ref,digest,run_id)
        timings['processing_seconds']=round(time.perf_counter()-phase,3)
        timings['total_seconds']=round(time.perf_counter()-started,3)
        with engine.begin() as c:
            details=c.execute(select(runs.c.details).where(runs.c.id==run_id)).scalar() or {}
            c.execute(update(runs).where(runs.c.id==run_id).values(details={**details,'timings':timings,'download_url':resource['url'],'file_bytes':path.stat().st_size}))
        from src.intelligence import migrate
        try:
            migrate()
        except Exception as sync_error:
            # The fleet transaction already committed. Preserve its successful status.
            with engine.begin() as c:
                details=c.execute(select(runs.c.details).where(runs.c.id==run_id)).scalar() or {}
                c.execute(update(runs).where(runs.c.id==run_id).values(details={**details,'intelligence_sync_error':str(sync_error)[:1000]}))
        return {'run_id':run_id,**result,'timings':timings}
    except BaseException as e:
        with engine.begin() as c:
            c.execute(update(runs).where(runs.c.id==run_id).values(status='ERRO',finished_at=now(),error=str(e)[:2000],stage='Processamento interrompido'))
        raise
    finally:
        lock.release()

if __name__ == '__main__':
    import argparse
    from src.db import init_db
    p=argparse.ArgumentParser();p.add_argument('--reference');args=p.parse_args()
    init_db();print(run_monthly(args.reference))
