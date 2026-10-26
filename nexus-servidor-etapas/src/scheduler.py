"""Optional daily local worker. Persistent daily claims prevent duplicate starts."""
import logging
import os
import threading
from datetime import datetime,timedelta,timezone
from sqlalchemy import select,insert,update
from sqlalchemy.exc import IntegrityError
from src import db,intelligence_schema as s
from src.connectors.public import now

log=logging.getLogger('i9')
BRASILIA=timezone(timedelta(hours=-3))
def enabled():return os.getenv('I9_SCHEDULER_ENABLED','false').lower()=='true'

def tick(at=None):
    current=(at or datetime.now(BRASILIA)).astimezone(BRASILIA)
    if current.hour<2:return {'status':'not_due'}
    key='senatran:'+current.date().isoformat()
    try:
        with db.engine.begin() as c:c.execute(insert(s.scheduler_tick).values(id=key,started_at=now(),status='running'))
    except IntegrityError:return {'status':'already_checked'}
    from src.etl import run_monthly
    try:
        result=run_monthly()
        with db.engine.begin() as c:c.execute(update(s.scheduler_tick).where(s.scheduler_tick.c.id==key).values(status='success',finished_at=now(),run_id=result.get('run_id')))
        return result
    except Exception as e:
        log.exception('Falha no agendador diário')
        with db.engine.begin() as c:c.execute(update(s.scheduler_tick).where(s.scheduler_tick.c.id==key).values(status='error',finished_at=now(),error=str(e)[:1000]))
        return {'status':'error'}

def start():
    stop=threading.Event()
    if enabled():
        def work():
            while not stop.wait(30):
                try:tick()
                except Exception:
                    log.exception('Loop do agendador interrompido; nova tentativa no próximo ciclo')
        threading.Thread(target=work,name='i9-daily-refresh',daemon=True).start()
    return stop

def status():
    with db.engine.connect() as c:
        last=c.execute(select(s.scheduler_tick).order_by(s.scheduler_tick.c.started_at.desc())).mappings().first()
    current=datetime.now(BRASILIA)
    today_key='senatran:'+current.date().isoformat()
    next_due=current.replace(hour=2,minute=0,second=0,microsecond=0)
    if last and last['id']==today_key:next_due+=timedelta(days=1)
    elif next_due<current:next_due=current
    return {'enabled':enabled(),'schedule':'Diariamente às 02:00, horário de Brasília; ao abrir depois desse horário, verifica o dia pendente.','next_check':next_due.isoformat() if enabled() else None,'last_check':dict(last) if last else None,'requires':'API e computador ligados. Não é serviço do Windows. Se falhar, use atualização manual ou aguarde o próximo dia. Não ative o DAG Airflow e este agendador ao mesmo tempo.'}
