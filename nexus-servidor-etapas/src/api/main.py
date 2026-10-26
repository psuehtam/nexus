import logging
import csv
import io
import json
import os
import secrets
import threading
from functools import lru_cache
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Literal

from fastapi import FastAPI, Depends, HTTPException, Header, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, func, insert, update
from src.db import engine, init_db, fleet, locations, batteries, snapshots, sources, runs, models, reviews, probes, unclassified, fuel_baseline, registrations, uf_population
from src.catalog import seed_catalog
from src.connectors.public import now, probe, CATALOG, IBGE, OPENEV, GITHUB
from src.etl import run_monthly, start_run, lock

log = logging.getLogger('i9')

@asynccontextmanager
async def lifespan(app):
    import anyio.to_thread
    anyio.to_thread.current_default_thread_limiter().total_tokens=8
    init_db();fail_orphan_runs();seed_catalog()
    from src import on_demand
    requested_work = os.getenv('I9_ON_DEMAND_WORKER', 'false').lower() == 'true'
    if requested_work:
        on_demand.start(check_catalog=os.getenv('I9_CHECK_ON_START', 'true').lower() == 'true')
    try:
        yield
    finally:
        if requested_work:
            on_demand.stop()

app = FastAPI(title='NEXUS • Inteligência de baterias e veículos', version='2.0.0',lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=os.getenv('CORS_ORIGINS','http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000').split(','),allow_credentials=True,allow_methods=['GET','POST','PUT'],allow_headers=['Content-Type','X-Admin-Key','X-API-Key','X-CSRF-Token','Idempotency-Key'])

@app.middleware('http')
async def legacy_read_only(request, call_next):
    from fastapi.responses import JSONResponse
    if request.method not in ('GET','HEAD','OPTIONS') and request.url.path.startswith(('/api/v1/','/api/v2/')) and os.getenv('I9_LEGACY_WRITES','false').lower()!='true':
        return JSONResponse({'detail':'Escritas antigas desativadas. Utilize a administração v3.'},status_code=410)
    return await call_next(request)

class Filters(BaseModel):
    reference: str | None = Field(None, pattern=r'^20\d{2}-(0[1-9]|1[0-2])$')
    uf: str | None = Field(None, pattern=r'^[A-Z]{2}$')
    city: str | None = None
    brand: str | None = None
    model: str | None = None
    version: str | None = None
    vehicle_year: int | None = Field(None, ge=1990, le=date.today().year+1)
    chemistry: str | None = None
    capacity_min: float | None = Field(None, ge=0,le=500)
    capacity_max: float | None = Field(None, ge=0,le=500)
    warranty_end: int | None = Field(None,ge=1990,le=2100)
    target_year: int | None = Field(None,ge=1990,le=2100)
    confidence: Literal['A','B','C','D'] | None = None
    source: str | None = None
    propulsion: Literal['BEV','PHEV','HEV','HEV_FLEX','FCEV'] | None = None
    scenario: int = Field(8,ge=8,le=12)

    @field_validator('scenario')
    @classmethod
    def valid_scenario(cls,v):
        if v not in (8,10,12):raise ValueError('Cenários permitidos: 8, 10 e 12')
        return v

def get_filters(reference: str|None=Query(None,pattern=r'^20\d{2}-(0[1-9]|1[0-2])$'),
                uf:str|None=Query(None,pattern=r'^[A-Z]{2}$'), city:str|None=None,brand:str|None=None,
                model:str|None=None,version:str|None=None,vehicle_year:int|None=Query(None,ge=1990,le=date.today().year+1),
                chemistry:str|None=None,capacity_min:float|None=Query(None,ge=0,le=500),capacity_max:float|None=Query(None,ge=0,le=500),
                warranty_end:int|None=Query(None,ge=1990,le=2100),target_year:int|None=Query(None,ge=1990,le=2100),
                confidence:Literal['A','B','C','D']|None=None,source:str|None=None,propulsion:Literal['BEV','PHEV','HEV','HEV_FLEX','FCEV']|None=None,scenario:int=Query(8,ge=8,le=12)):
    if scenario not in (8,10,12):raise HTTPException(422,'Cenários permitidos: 8, 10 e 12')
    return Filters(**locals())

F = Annotated[Filters, Depends(get_filters)]

def admin(x_admin_key: Annotated[str|None, Header()] = None):
    expected = os.getenv('ADMIN_API_KEY')
    if not expected: raise HTTPException(503,'Administração não configurada: defina ADMIN_API_KEY no servidor')
    if not x_admin_key or not secrets.compare_digest(x_admin_key,expected):
        raise HTTPException(401,'Chave administrativa inválida')

def resolve(f):
    if f.capacity_min is not None and f.capacity_max is not None and f.capacity_min > f.capacity_max:
        raise HTTPException(422,'Capacidade mínima maior que a máxima')
    if f.reference: return f.reference
    with engine.connect() as c: return c.execute(select(func.max(snapshots.c.reference))).scalar()

def statement(f, reference=None):
    ref = reference or resolve(f)
    q=select(fleet,locations.c.uf,locations.c.city,batteries.c.capacity_kwh,batteries.c.usable_kwh,
        batteries.c.chemistry,batteries.c.technology,batteries.c.warranty_years,batteries.c.warranty_km,
        batteries.c.warranty_notes,batteries.c.provenance).select_from(fleet.join(locations).outerjoin(batteries)).where(fleet.c.reference==ref)
    for name,col in {'uf':locations.c.uf,'city':locations.c.city,'brand':fleet.c.brand,'model':fleet.c.model,'version':fleet.c.version,
                     'vehicle_year':fleet.c.vehicle_year,'chemistry':batteries.c.chemistry,'confidence':fleet.c.confidence,'source':fleet.c.source_id,'propulsion':fleet.c.propulsion}.items():
        v=getattr(f,name)
        if v is not None: q=q.where(col==v)
    if f.capacity_min is not None:q=q.where(batteries.c.capacity_kwh>=f.capacity_min)
    if f.capacity_max is not None:q=q.where(batteries.c.capacity_kwh<=f.capacity_max)
    if f.warranty_end is not None:q=q.where(fleet.c.vehicle_year+batteries.c.warranty_years==f.warranty_end)
    if f.target_year is not None:q=q.where(fleet.c.vehicle_year+f.scenario==f.target_year)
    return q

_records_lock=threading.Lock()

@lru_cache(maxsize=4)
def _cached_records(database,reference,serialized_filters):
    f=Filters.model_validate_json(serialized_filters)
    if reference is None:return []
    query=statement(f,reference)
    with database.connect() as c:return [dict(r) for r in c.execute(query).mappings()]

def records(f, reference=None):
    reference=reference or resolve(f)
    # Snapshots and their technical versions are immutable. Resolve the month
    # before caching so a newly published snapshot is immediately visible.
    with _records_lock:
        return _cached_records(engine,reference,f.model_dump_json(exclude={'reference'}))

def enrich(row):
    row=dict(row)
    known=row['confidence'] in ('A','B') and row['capacity_kwh'] is not None
    row['energy_mwh']=round(row['quantity']*row['capacity_kwh']/1000,4) if known else None
    row['warranty_end']=row['vehicle_year']+row['warranty_years'] if known and row['warranty_years'] and row['vehicle_year'] is not None else None
    return row

@app.get('/health')
def health():
    with engine.connect() as c:c.execute(select(1))
    return {'status':'ok','database':engine.dialect.name}

@app.get('/api/v1/fleet')
def get_fleet(f:F,limit:int=Query(50,ge=1,le=1000),offset:int=Query(0,ge=0)):
    q=statement(f)
    with engine.connect() as c:
        total=c.execute(select(func.count()).select_from(q.subquery())).scalar()
        data=[enrich(r) for r in c.execute(q.order_by(fleet.c.quantity.desc(),fleet.c.id).limit(limit).offset(offset)).mappings()]
    return {'reference':resolve(f),'total':total,'limit':limit,'offset':offset,'items':data}

def grouped(rows,key):
    result=defaultdict(lambda:{'vehicles':0,'energy_mwh':0,'known_vehicles':0})
    for raw in rows:
        r=enrich(raw); g=result[str(r.get(key) or 'Não identificado')];g['vehicles']+=r['quantity']
        if r['energy_mwh'] is not None:g['energy_mwh']+=r['energy_mwh'];g['known_vehicles']+=r['quantity']
    return [dict(name=k,**v,coverage_pct=round(v['known_vehicles']/v['vehicles']*100,1) if v['vehicles'] else 0) for k,v in sorted(result.items(),key=lambda x:-x[1]['vehicles'])]

@app.get('/api/v1/summary')
def summary(f:F):
    rows=records(f);total=sum(r['quantity'] for r in rows);known=sum(r['quantity'] for r in rows if r['capacity_kwh'] is not None and r['confidence'] in ('A','B'))
    reference=resolve(f)
    with engine.connect() as c:
        snap=c.execute(select(snapshots).where(snapshots.c.reference==reference)).mappings().first()
        pops={r.uf:r.population for r in c.execute(select(uf_population)).mappings()}
    by_uf=[]
    for item in grouped(rows,'uf'):
        pop=pops.get(item['name'])
        by_uf.append({**item,'population':pop,'vehicles_per_100k':round(item['vehicles']/pop*100000,1) if pop else None})
    by_propulsion=grouped(rows,'propulsion')
    catalog_totals={item['name']:item['vehicles'] for item in by_propulsion}
    from src.connectors.fuel import coverage as fuel_coverage
    official=fuel_coverage(catalog_totals)
    energy_by_propulsion=[{'name':item['name'],'vehicles':item['vehicles'],'energy_mwh':round(item['energy_mwh'],3) if item['known_vehicles'] else None,
                           'known_vehicles':item['known_vehicles'],'coverage_pct':item['coverage_pct']} for item in by_propulsion]
    return {'reference':resolve(f),'vehicles':total,'brands':len({r['brand'] for r in rows}), 'models':len({(r['brand'],r['model']) for r in rows}),
        'versions':len({(r['brand'],r['model'],r['version']) for r in rows}), 'energy_mwh':round(sum(enrich(r)['energy_mwh'] or 0 for r in rows),3) if known else None,
        'known_vehicles':known,'unknown_battery_vehicles':total-known,'coverage_pct':round(known/total*100,1) if total else 0,'by_brand':grouped(rows,'brand'),'by_model':grouped(rows,'model'),
        'by_uf':by_uf,'by_city':grouped(rows,'city'),'by_chemistry':grouped(rows,'chemistry'),'by_propulsion':by_propulsion,
        'energy_by_propulsion':energy_by_propulsion,
        'official_coverage':official,
        'official_ruler_pct':official.get('coverage_pct'),'official_ruler_total':official.get('official_total'),
        'scope':snap['scope'] if snap else 'Nenhum snapshot publicado. Atualize a base em Fontes e qualidade.', 'is_complete_national_coverage':False}

@app.get('/api/v1/forecast')
def forecast(f:F):
    rows=records(f);groups=defaultdict(lambda:{'vehicles':0,'energy_mwh':0,'known_vehicles':0})
    warranty=defaultdict(int);missing_warranty=0;missing_year=0
    for raw in rows:
        r=enrich(raw)
        if r['vehicle_year'] is None:
            missing_year+=r['quantity'];missing_warranty+=r['quantity'];continue
        g=groups[r['vehicle_year']+f.scenario];g['vehicles']+=r['quantity']
        if r['energy_mwh'] is not None:g['energy_mwh']+=r['energy_mwh'];g['known_vehicles']+=r['quantity']
        if r['warranty_end']:warranty[r['warranty_end']]+=r['quantity']
        else:missing_warranty+=r['quantity']
    return {'reference':resolve(f),'scenario':f.scenario,'label':'Janela estimada de segunda vida; não é data de falha ou descarte',
        'items':[dict(year=k,**{**v,'energy_mwh':round(v['energy_mwh'],3) if v['known_vehicles'] else None}) for k,v in sorted(groups.items())],
        'warranty':[{'year':k,'vehicles':v} for k,v in sorted(warranty.items())],'missing_warranty_vehicles':missing_warranty,'missing_year_vehicles':missing_year}

@app.get('/api/v1/monthly-change')
def monthly_change(f:F):
    ref=resolve(f)
    if not ref:return {'reference':None,'previous_reference':None,'available':False,'items':[]}
    y,m=map(int,ref.split('-'));prev=f'{y-1}-12' if m==1 else f'{y}-{m-1:02}'
    with engine.connect() as c:exists=c.execute(select(snapshots.c.reference).where(snapshots.c.reference==prev)).first()
    if not exists:return {'reference':ref,'previous_reference':prev,'available':False,'items':[],'reason':'Snapshot do mês anterior não disponível'}
    def aggregate(rows):
        result=defaultdict(int)
        for r in rows:result[(r['brand'],r['model'],r['city'],r['uf'])]+=r['quantity']
        return result
    a,b=aggregate(records(f)),aggregate(records(f,prev))
    items=[dict(brand=k[0],model=k[1],city=k[2],uf=k[3],current=a[k],previous=b[k],delta=a[k]-b[k],new=a[k]>0 and b[k]==0,disappeared=a[k]==0 and b[k]>0,atypical=b[k]>0 and abs(a[k]-b[k])/b[k]>.30) for k in set(a)|set(b)]
    cur,old=sum(a.values()),sum(b.values())
    return {'reference':ref,'previous_reference':prev,'available':True,'current':cur,'previous':old,'delta':cur-old,'change_pct':round((cur-old)/old*100,2) if old else None,'items':sorted(items,key=lambda x:abs(x['delta']),reverse=True)}

@app.get('/api/v1/monthly-series')
def monthly_series(f:F):
    with engine.connect() as c:
        refs=list(c.execute(select(snapshots.c.reference).order_by(snapshots.c.reference)).scalars())
        sales=[dict(r) for r in c.execute(select(registrations)).mappings()]
    items=[]
    for ref in refs:
        rows=records(f,ref)
        items.append({'reference':ref,'vehicles':sum(r['quantity'] for r in rows),'known_vehicles':sum(r['quantity'] for r in rows if r['capacity_kwh'] is not None and r['confidence'] in ('A','B'))})
    return {'items':items,'registrations':sales,'note':'A série é estoque do recorte. Emplacamentos Fenabrave não devem ser somados ao estoque.'}

@app.get('/api/v1/models')
def get_models():
    with engine.connect() as c:return [dict(r) for r in c.execute(select(models)).mappings()]

@app.get('/api/v1/batteries')
def get_batteries():
    with engine.connect() as c:return [dict(r) for r in c.execute(select(batteries,models.c.brand,models.c.model,models.c.version,models.c.year_start,models.c.year_end,models.c.propulsion,models.c.category).join(models)).mappings()]

@app.get('/api/v1/filters')
def filters(reference:str|None=Query(None,pattern=r'^20\d{2}-(0[1-9]|1[0-2])$'),uf:str|None=Query(None,pattern=r'^[A-Z]{2}$'),brand:str|None=None,model:str|None=None):
    f=Filters(reference=reference,uf=uf,brand=brand,model=model)
    with engine.connect() as c:
        refs=list(c.execute(select(snapshots.c.reference).order_by(snapshots.c.reference.desc())).scalars())
    rows=records(f)
    return {'references':refs,**{k:sorted({r[k] for r in rows if r.get(k) is not None}) for k in ['uf','city','brand','model','version','vehicle_year','chemistry','confidence','source_id','propulsion']}}

@app.get('/api/v1/sources')
def get_sources():
    with engine.connect() as c:
        data=[dict(r) for r in c.execute(select(sources)).mappings()]
        imports=[dict(r) for r in c.execute(select(runs).where(runs.c.status=='SUCESSO')).mappings()]
    for r in data:
        ref=r.get('reference');r['months_late']=((date.today().year-int(ref[:4]))*12+date.today().month-int(ref[5:])) if ref else None
        r['downloads']=[]
        if r['id']=='senatran':
            r['portal_url']='https://www.gov.br/transportes/pt-br/assuntos/transito/conteudo-Senatran/frota-de-veiculos-2026'
            seen=set()
            for item in imports:
                url=(item.get('details') or {}).get('download_url')
                if url and url not in seen:
                    r['downloads'].append({'label':f"ZIP usado · {item['reference']}", 'url':url})
                    seen.add(url)
        elif r['id']=='openev':
            url=(r.get('details') or {}).get('download_url')
            if url:r['downloads'].append({'label':'JSON importado · '+str(r['details'].get('release','')), 'url':url})
        elif r['id'] in ('inmetro','byd'):
            r['downloads'].append({'label':'PDF usado na revisão técnica','url':r['url']})
    data.append({'id':'ibge','name':'IBGE — municípios e mapa','kind':'oficial','url':IBGE,
        'collected_at':None,'months_late':None,'details':{'role':'Códigos e nomes de municípios; limites das UFs para o mapa. Não fornece quantidades da frota.'},
        'downloads':[{'label':'API de municípios','url':IBGE},{'label':'Mapa das UFs · GeoJSON','url':'https://servicodados.ibge.gov.br/api/v3/malhas/paises/BR?formato=application/vnd.geo%2Bjson&qualidade=minima&intrarregiao=UF'}]})
    return data

@app.get('/api/v1/etl/runs/{run_id}')
def get_run(run_id:str):
    with engine.connect() as c:
        row=c.execute(select(runs).where(runs.c.id==run_id)).mappings().first()
    if not row:raise HTTPException(404,'Execução não encontrada')
    return dict(row)

@app.get('/api/v1/data-quality')
def data_quality(f:F):
    rows=records(f)
    pending=defaultdict(lambda:{'vehicles':0,'records':0})
    for r in rows:
        if r['battery_id'] is None:
            g=pending[(r['raw_name'],r['vehicle_year'],r['confidence'])];g['vehicles']+=r['quantity'];g['records']+=1
    reference=resolve(f)
    with engine.connect() as c:
        run_log=[dict(r) for r in c.execute(select(runs).order_by(runs.c.started_at.desc()).limit(20)).mappings()]
        checks=[dict(r) for r in c.execute(select(probes).order_by(probes.c.id.desc()).limit(20)).mappings()]
        skipped=[dict(r) for r in c.execute(select(unclassified).where(unclassified.c.reference==reference).order_by(unclassified.c.vehicles.desc()).limit(500)).mappings()] if reference else []
        hint_vehicles=c.execute(select(func.coalesce(func.sum(unclassified.c.vehicles),0)).where(unclassified.c.reference==reference,unclassified.c.electrified_hint==1)).scalar() if reference else 0
        other_vehicles=c.execute(select(func.coalesce(func.sum(unclassified.c.vehicles),0)).where(unclassified.c.reference==reference,unclassified.c.electrified_hint==0)).scalar() if reference else 0
    from src.connectors.technical_review import shortlist
    return {'reference':reference,'by_confidence':grouped(rows,'confidence'),'pending':[dict(raw_name=k[0],vehicle_year=k[1],confidence=k[2],**v) for k,v in sorted(pending.items(),key=lambda x:-x[1]['vehicles'])],
        'unclassified':skipped,'unclassified_hint_vehicles':int(hint_vehicles or 0),'unclassified_other_vehicles':int(other_vehicles or 0),
        'unclassified_note':'Priorize nomes com indício de eletrificação. Nomes ambíguos (ex.: TOYOTA COROLLA sem HEV) não entram no catálogo. Ampliar FAMILIES só vale no próximo mês de ETL; este snapshot não ganha linhas novas.',
        'technical_shortlist':shortlist(),
        'missing_location_vehicles':sum(r['quantity'] for r in rows if r['uf'] is None),'missing_year_vehicles':sum(r['quantity'] for r in rows if r['vehicle_year'] is None),'missing_battery_vehicles':sum(v['vehicles'] for v in pending.values()),'missing_warranty_vehicles':sum(r['quantity'] for r in rows if r['warranty_years'] is None),'runs':run_log,'api_checks':checks}

@app.get('/api/v1/export/{report}')
def export(report:Literal['fleet','forecast','monthly-change','data-quality','batteries'],f:F,format:Literal['csv','xlsx']='csv'):
    if report=='fleet': data=[enrich(r) for r in records(f)]
    elif report=='forecast': data=forecast(f)['items']
    elif report=='monthly-change':data=monthly_change(f)['items']
    elif report=='batteries':data=get_batteries()
    else:data=data_quality(f)['pending']
    # Neutralize spreadsheet formulas in every export format.
    def safe(v):
        if isinstance(v,(dict,list)):v=json.dumps(v,ensure_ascii=False)
        if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')):return "'"+v
        return v
    data=[{k:safe(v) for k,v in r.items()} for r in data]
    headers=list(data[0]) if data else ['sem_registros']
    if format=='csv':
        output=io.StringIO();w=csv.DictWriter(output,fieldnames=headers,delimiter=';');w.writeheader();w.writerows(data)
        body=output.getvalue().encode('utf-8-sig');media='text/csv; charset=utf-8'
    else:
        from openpyxl import Workbook
        wb=Workbook();ws=wb.active;ws.title='Dados';ws.append(headers)
        for r in data:ws.append([r.get(k) for k in headers])
        ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
        output=io.BytesIO();wb.save(output);body=output.getvalue();media='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    return Response(body,media_type=media,headers={'Content-Disposition':f'attachment; filename="nexus-{report}-{resolve(f) or "sem-dados"}.{format}"'})

class RunRequest(BaseModel):
    reference:str|None=Field(None,pattern=r'^20\d{2}-(0[1-9]|1[0-2])$')

def fail_orphan_runs():
    with engine.begin() as c:
        c.execute(update(runs).where(runs.c.status=='EXECUTANDO').values(status='ERRO',error='Execução interrompida pela reinicialização da API. Confira os logs e o snapshot antes de reexecutar.',finished_at=now(),stage='Processamento interrompido'))

def background_run(ref,run_id):
    try:run_monthly(ref,run_id)
    except Exception:
        log.exception('ETL em segundo plano falhou (run_id=%s)', run_id)

@app.post('/api/v1/admin/etl/run',dependencies=[Depends(admin)],status_code=202)
def etl_run(body:RunRequest,bg:BackgroundTasks):
    if lock.locked():raise HTTPException(409,'Já há uma atualização em execução')
    if os.getenv('ALLOW_SOURCE_DOWNLOAD','false').lower()!='true':raise HTTPException(409,'Download da fonte não habilitado no servidor')
    run_id=start_run(body.reference);bg.add_task(background_run,body.reference,run_id)
    return {'run_id':run_id,'status':'EXECUTANDO'}

@app.post('/api/v1/admin/probe',dependencies=[Depends(admin)])
def run_probe():
    from concurrent.futures import ThreadPoolExecutor
    targets=[('SENATRAN catálogo',CATALOG),('IBGE municípios',IBGE),('OpenEV GitHub',GITHUB)]
    with ThreadPoolExecutor(max_workers=4) as pool:return list(pool.map(lambda x:probe(*x),targets))

@app.post('/api/v1/admin/technical/download',dependencies=[Depends(admin)])
def technical_download():
    from src.connectors.technical_download import import_release
    try:
        return import_release()
    except Exception as e:
        raise HTTPException(502, f'Não foi possível importar a base técnica: {e}')

@app.get('/api/v1/technical-candidates')
def list_technical_candidates():
    from src.db import technical_candidates
    from src.connectors.technical_review import shortlist
    with engine.connect() as c:
        rows=[dict(r) for r in c.execute(select(technical_candidates).order_by(technical_candidates.c.brand,technical_candidates.c.model)).mappings()]
    return {'items':rows,'shortlist':shortlist()}

@app.get('/api/v1/fuel-baseline')
def get_fuel_baseline():
    with engine.connect() as c:
        rows=[dict(r) for r in c.execute(select(fuel_baseline)).mappings()]
    return rows

@app.get('/api/v1/registrations')
def get_registrations():
    with engine.connect() as c:
        return [dict(r) for r in c.execute(select(registrations)).mappings()]

@app.post('/api/v1/admin/inmetro/seed',dependencies=[Depends(admin)])
def inmetro_seed():
    from src.connectors.inmetro import ingest_seed
    return ingest_seed()

@app.post('/api/v1/admin/population/sync',dependencies=[Depends(admin)])
def population_sync():
    from src.connectors.population import sync_population
    return sync_population()

class ReviewRequest(BaseModel):
    raw_name:str=Field(min_length=1,max_length=150)
    vehicle_year:int=Field(ge=1990,le=date.today().year+1)
    model_id:str
    approver:str=Field(min_length=3,max_length=100)
    notes:str=Field(min_length=10,max_length=2000)

@app.post('/api/v1/admin/matches',dependencies=[Depends(admin)])
def approve_match(body:ReviewRequest):
    from src.matching import normalize
    from sqlalchemy import update
    with engine.begin() as c:
        model=c.execute(select(models).where(models.c.id==body.model_id)).mappings().first()
        if not model:raise HTTPException(404,'Modelo não encontrado')
        if not model['year_start'] <= body.vehicle_year <= model['year_end']:raise HTTPException(422,'Ano fora da vigência técnica')
        aliases=list(model['aliases']);name=normalize(body.raw_name)
        if name not in aliases:aliases.append(name)
        c.execute(update(models).where(models.c.id==body.model_id).values(aliases=aliases))
        c.execute(insert(reviews).values(**body.model_dump(),score=1.0,method='revisao_manual',status='APROVADO',created_at=now()))
    return {'status':'APROVADO','note':'Alias aplicado às próximas cargas; snapshots publicados permanecem imutáveis.'}

from src.api.intelligence import router as intelligence_router
app.include_router(intelligence_router)
from src.api.fleet import router as fleet_router
app.include_router(fleet_router)
from src.api.battery_import import router as battery_import_router
app.include_router(battery_import_router)
from src.api.auth import router as auth_router
app.include_router(auth_router)

# A compiled SPA is served without a Node or development server in production.
from pathlib import Path
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from src import pipeline_schema
_static = Path(__file__).resolve().parents[2] / 'dashboard' / 'dist'
if (_static / 'assets').is_dir():
    app.mount('/assets', StaticFiles(directory=_static / 'assets'), name='assets')

@app.get('/{path:path}', include_in_schema=False)
def frontend(path: str):
    if path.startswith(('api/','health','docs','openapi')):
        raise HTTPException(404,'Rota não encontrada')
    candidate=(_static/path).resolve()
    if candidate.is_relative_to(_static.resolve()) and candidate.is_file():
        return FileResponse(candidate)
    if (_static/'index.html').is_file():
        return FileResponse(_static/'index.html',headers={'Cache-Control':'no-cache'})
    raise HTTPException(503,'Interface ainda não compilada. Execute o build do dashboard.')
