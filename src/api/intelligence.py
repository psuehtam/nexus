import json
import os
import secrets
import threading
from datetime import date
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, insert, update, func, or_
from sqlalchemy.exc import IntegrityError
from src import db, intelligence as service, intelligence_schema as s
from src.intelligence_models import MODELS, Import, Source, Forecast, Vehicle, BatteryAsset, Installation, Powertrain, Category
from src.connectors.public import now

router=APIRouter(prefix='/api/v2',tags=['Battery Intelligence'])
write_lock=threading.Lock()
PUBLIC={'vehicle-specs','battery-models','evidence'}
ROLES={'public_user','analyst','data_engineer','partner','authorized_vehicle_lookup','admin'}

def actor(x_admin_key:str|None=Header(None),x_api_key:str|None=Header(None)):
    admin=os.getenv('ADMIN_API_KEY','')
    if admin and x_admin_key and secrets.compare_digest(admin,x_admin_key):return {'actor':'local-admin','role':'admin'}
    try:tokens=json.loads(os.getenv('I9_ROLE_TOKENS','[]'))
    except ValueError:tokens=[]
    for item in tokens:
        if x_api_key and item.get('key') and secrets.compare_digest(x_api_key,item['key']) and item.get('role') in ROLES:
            return {'actor':item.get('name','configured-user'),'role':item['role']}
    return {'actor':'anonymous','role':'public_user'}

def permit(a,roles):
    if a['role'] not in roles:raise HTTPException(401 if a['role']=='public_user' else 403,'Acesso restrito: credencial com permissão necessária')

def get_one(c,table,key):
    row=c.execute(select(table).where(table.c.id==key)).mappings().first()
    if row is None:raise HTTPException(404,'Registro não encontrado')
    return dict(row)

def validate_references(c,table,data):
    for col in table.c:
        if data.get(col.name) is not None:
            for fk in col.foreign_keys:get_one(c,fk.column.table,data[col.name])

@router.get('/session')
def session(a=Depends(actor)):return {'actor':a['actor'],'role':a['role'],'roles':sorted(ROLES)}

@router.get('/scheduler')
def scheduler_status():
    from src.scheduler import status
    return status()

@router.get('/sources')
def sources():
    with db.engine.connect() as c:
        rows=[dict(x) for x in c.execute(select(s.source_snapshot).where(s.source_snapshot.c.access_level=='public').order_by(s.source_snapshot.c.retrieved_at.desc())).mappings()]
        for r in rows:
            r['quantity']=c.execute(select(func.sum(s.aggregate.c.quantity)).where(s.aggregate.c.source_snapshot_id==r['id'])).scalar() or 0
    return rows

@router.get('/admin/sources')
def admin_sources(a=Depends(actor)):
    permit(a,{'admin','data_engineer'})
    with db.engine.connect() as c:return [dict(r) for r in c.execute(select(s.source_snapshot).order_by(s.source_snapshot.c.retrieved_at.desc())).mappings()]

@router.get('/sources/{key}/changelog')
def changelog(key:str):
    with db.engine.connect() as c:
        src=get_one(c,s.source_snapshot,key)
        if src['access_level']!='public':raise HTTPException(404,'Fonte pública não encontrada')
        prev=c.execute(select(s.source_snapshot).where(s.source_snapshot.c.provider==src['provider'],s.source_snapshot.c.dataset_name==src['dataset_name'],s.source_snapshot.c.metric==src['metric'],s.source_snapshot.c.scope==src['scope'],s.source_snapshot.c.reference_period<src['reference_period'],s.source_snapshot.c.access_level=='public').order_by(s.source_snapshot.c.reference_period.desc())).mappings().first() if src['reference_period'] else None
        if not prev:return {'available':False,'note':'Sem período anterior com mesma medida e cobertura.'}
        def grouped(sid):
            result={}
            for r in c.execute(select(s.aggregate).where(s.aggregate.c.source_snapshot_id==sid)).mappings():
                identity=tuple(r[k] for k in ('country','state','municipality','vehicle_category','manufacturer','model','trim','model_year','manufacture_year','powertrain_type'))
                result[identity]=result.get(identity,0)+r['quantity']
            return result
        before=grouped(prev['id']);after=grouped(key)
        changes=[{'dimensions':dict(zip(('country','state','municipality','vehicle_category','manufacturer','model','trim','model_year','manufacture_year','powertrain_type'),k)),'previous':before.get(k,0),'current':after.get(k,0),'delta':after.get(k,0)-before.get(k,0)} for k in before.keys()|after.keys() if after.get(k,0)!=before.get(k,0)]
    return {'available':True,'previous_reference':prev['reference_period'],'reference':src['reference_period'],'delta':sum(after.values())-sum(before.values()),'changed_groups':len(changes),'items':sorted(changes,key=lambda x:abs(x['delta']),reverse=True)[:100],'note':'Até 100 mudanças exibidas; variação de estoque não equivale a emplacamentos.'}

def refuse_abve_stock(provider, metric):
    if str(provider or '').strip().upper()=='ABVE' and metric=='fleet_stock':
        raise HTTPException(422,'ABVE não entra como estoque da Frota. Use registrations ou cumulative_registrations; o recorte SENATRAN permanece separado.')

@router.post('/sources',status_code=201)
def create_source(body:Source,a=Depends(actor)):
    permit(a,{'admin','data_engineer'})
    refuse_abve_stock(body.provider, body.metric)
    sid=service.uid()
    with db.engine.begin() as c:
        c.execute(insert(s.source_snapshot).values(id=sid,**body.model_dump(),retrieved_at=now(),transformation_version=service.VERSION,ingestion_status='registered',record_count=0,metadata={}))
        service.audit(c,a,'register','source_snapshot',sid)
    return {'id':sid}

@router.post('/imports/aggregates')
def import_data(body:Import,a=Depends(actor)):
    permit(a,{'admin','data_engineer'})
    refuse_abve_stock(body.source.provider, body.source.metric)
    try:
        with write_lock:return service.import_observations(body,a)
    except ValueError as error:
        raise HTTPException(422,str(error))

@router.post('/sync/legacy')
def sync_legacy(a=Depends(actor)):
    permit(a,{'admin','data_engineer'})
    with write_lock:return service.migrate()

class TabularImport(BaseModel):
    source_snapshot_id:str
    format:str=Field(pattern=r'^(csv|xlsx)$')
    content_base64:str=Field(max_length=14_000_000)

@router.post('/imports/tabular')
def import_tabular(body:TabularImport,a=Depends(actor)):
    permit(a,{'admin','data_engineer'})
    import base64,hashlib
    from src.connectors.tabular import parse_tabular
    with db.engine.connect() as c:src=get_one(c,s.source_snapshot,body.source_snapshot_id)
    try:
        raw=base64.b64decode(body.content_base64,validate=True)
        rows=parse_tabular(raw,body.format)
        source=Source.model_validate({k:src[k] for k in Source.model_fields})
        imported=Import(source=source,observations=rows)
    except Exception as e:
        raise HTTPException(422,'Arquivo inválido: '+str(e)[:500])
    digest=hashlib.sha256(raw).hexdigest();path=db.ROOT/'data/raw/imports'/f'{digest}.{body.format}'
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    refuse_abve_stock(imported.source.provider, imported.source.metric)
    try:
        with write_lock:return service.import_observations(imported,a,digest,str(path.relative_to(db.ROOT)))
    except ValueError as error:
        raise HTTPException(422,str(error))

@router.post('/imports/abve-reviewed')
def abve_reviewed(a=Depends(actor)):
    permit(a,{'admin','data_engineer'})
    from src.connectors.abve_reviewed import ingest_reviewed
    with write_lock:return ingest_reviewed(a)

@router.get('/observations')
def observations(source_snapshot_id:str|None=None,reference_period:str|None=None,vehicle_category:Category|None=None,powertrain_type:Powertrain|None=None,state:str|None=None,municipality:str|None=None,manufacturer:str|None=None,model:str|None=None,limit:int=Query(50,ge=1,le=500),offset:int=Query(0,ge=0)):
    q=select(s.aggregate).join(s.source_snapshot).where(s.source_snapshot.c.access_level=='public')
    for key,value in locals().copy().items():
        if key in s.aggregate.c and value is not None:
            q=q.where(s.aggregate.c[key].ilike('%'+value+'%') if key in ('municipality','manufacturer','model') else s.aggregate.c[key]==value)
    with db.engine.connect() as c:
        total=c.execute(select(func.count()).select_from(q.subquery())).scalar()
        quantity=c.execute(select(func.sum(q.subquery().c.quantity))).scalar() or 0
        rows=[dict(x) for x in c.execute(q.order_by(s.aggregate.c.id).limit(limit).offset(offset)).mappings()]
        for row in rows:
            src=get_one(c,s.source_snapshot,row['source_snapshot_id'])
            bm=get_one(c,s.battery_model,row['battery_model_id']) if row['battery_model_id'] else None
            row['quality']=service.quality(row,bm,src)
            row['data_confidence']=row['quality']['total']
    return {'items':rows,'total':total,'quantity':quantity,'note':'Não somar fontes, medidas, meses ou níveis geográficos sobrepostos.'}

@router.get('/observation-filters')
def observation_filters(source_snapshot_id:str|None=None,state:str|None=None):
    """Values present in the selected fleet slice, used by the public filter UI."""
    base=select(s.aggregate).join(s.source_snapshot).where(s.source_snapshot.c.access_level=='public')
    if source_snapshot_id:base=base.where(s.aggregate.c.source_snapshot_id==source_snapshot_id)
    if state:base=base.where(s.aggregate.c.state==state)
    sub=base.subquery()
    def values(c,column,limit=6000):
        return [x for x in c.execute(select(column).select_from(sub).where(column.is_not(None),column!='').distinct().order_by(column).limit(limit)).scalars()]
    with db.engine.connect() as c:
        return {'states':values(c,sub.c.state,100),'municipalities':values(c,sub.c.municipality),'manufacturers':values(c,sub.c.manufacturer,1000),'models':values(c,sub.c.model,3000),'powertrains':values(c,sub.c.powertrain_type,100),'categories':values(c,sub.c.vehicle_category,100)}

@router.get('/market')
def market():
    result=sources()
    stock=[r for r in result if r['provider']=='SENATRAN' and r['metric']=='fleet_stock']
    benchmark=[r for r in result if r['provider']=='ABVE' and r['metric']=='cumulative_registrations']
    left=max(stock,key=lambda r:r['reference_period']) if stock else None
    right=max(benchmark,key=lambda r:r['reference_period']) if benchmark else None
    with db.engine.connect() as c:
        individual=c.execute(select(func.count()).select_from(s.vehicle_asset).where(s.vehicle_asset.c.identity_status.in_(['identified','verified']))).scalar()
        slots=c.execute(select(func.count()).select_from(s.vehicle_asset).where(s.vehicle_asset.c.identity_status=='aggregate_derived')).scalar()
        techs=[]
        for src in result:
            if src['provider']=='ABVE' and src['metric']=='registrations' and 'tecnologia' in (src.get('dataset_name') or '').lower():
                techs=[{'name':r['powertrain_type'],'vehicles':r['quantity']} for r in c.execute(select(s.aggregate).where(s.aggregate.c.source_snapshot_id==src['id'])).mappings()]
                break
    return {'sources':result,'senatran':left,'abve':right,'reconciliation':service.reconciliation(left,right) if left and right else None,'identified_assets':individual,'aggregate_slots':slots,'powertrains':['BEV','PHEV','HEV','HEV_FLEX','MHEV_12V','MHEV_48V','FCEV'],'technologies':techs,'note':'ABVE é mercado (emplacamentos acumulados). SENATRAN família é estoque identificável. Não somar e não usar o KPI ABVE como meta da Frota.'}

@router.get('/reconciliation')
def reconcile(left:str,right:str):
    data={r['id']:r for r in sources()}
    if left not in data or right not in data:raise HTTPException(404,'Fonte pública não encontrada')
    return {'left':data[left],'right':data[right],**service.reconciliation(data[left],data[right])}

@router.post('/forecast/battery-availability')
def forecast(body:Forecast):
    result=service.availability(body)
    if body.battery_model:
        with db.engine.connect() as c:
            model=get_one(c,s.battery_model,body.battery_model)
            source=get_one(c,s.source_snapshot,model['source_snapshot_id'])
            if source['access_level']!='public':raise HTTPException(404,'Modelo público não encontrado')
        result['technical_context']={'battery_model_id':model['id'],'confidence':model['confidence'],'expected_cycle_life':model['expected_cycle_life'],'note':'Sem uso medido, especificações e garantia não alteram automaticamente o cenário de idade.'}
    return result

@router.post('/forecast/battery-availability/save',status_code=201)
def save_forecast(body:Forecast,a=Depends(actor)):
    permit(a,{'admin','analyst','data_engineer'})
    result=forecast(body);key=service.uid()
    with db.engine.begin() as c:
        c.execute(insert(s.forecast_run).values(id=key,method=result['method'],created_at=now(),confidence=result['confidence'],parameters=body.model_dump(mode='json'),result=result))
        service.audit(c,a,'forecast','forecast',key)
    return {'id':key,**result}

@router.get('/opportunities')
def opportunities(source_snapshot_id:str,start_year:int=Query(date.today().year,ge=1900,le=2150),end_year:int=Query(date.today().year+2,ge=1900,le=2150),vehicle_category:Category|None=None,powertrain_type:Powertrain|None=None,state:str|None=None,municipality:str|None=None,chemistry:str|None=None,battery_manufacturer:str|None=None,earliest_age:int=8,most_likely_age:int=10,latest_age:int=12,residual_fraction:float|None=Query(None,ge=0,le=1)):
    if start_year>end_year:raise HTTPException(422,'Janela invertida')
    try:Forecast(earliest_age=earliest_age,most_likely_age=most_likely_age,latest_age=latest_age)
    except ValidationError:raise HTTPException(422,'Idades inválidas ou fora de ordem')
    with db.engine.connect() as c:
        src=get_one(c,s.source_snapshot,source_snapshot_id)
        if src['access_level']!='public' or src['metric'] not in ('fleet_stock','registrations'):raise HTTPException(422,'Escolha uma fonte pública de estoque ou coorte de emplacamentos')
        q=select(s.aggregate).where(s.aggregate.c.source_snapshot_id==source_snapshot_id)
        for key,value in [('vehicle_category',vehicle_category),('powertrain_type',powertrain_type),('state',state),('municipality',municipality)]:
            if value:q=q.where(s.aggregate.c[key]==value)
        bm={r['id']:dict(r) for r in c.execute(select(s.battery_model)).mappings()}
        rows=[dict(r) for r in c.execute(q).mappings()]
        if chemistry:rows=[r for r in rows if bm.get(r['battery_model_id'],{}).get('chemistry')==chemistry]
        if battery_manufacturer:rows=[r for r in rows if bm.get(r['battery_model_id'],{}).get('manufacturer')==battery_manufacturer]
    return {'source':src,**service.opportunity(rows,bm,start_year,end_year,earliest_age,most_likely_age,latest_age,residual_fraction)}

@router.get('/registry/{entity}')
def list_entities(entity:str,limit:int=Query(50,ge=1,le=500),offset:int=Query(0,ge=0),vehicle_category:Category|None=None,powertrain_type:Powertrain|None=None,identity_status:str|None=None,state:str|None=None,manufacturer:str|None=None,model:str|None=None,a=Depends(actor)):
    if entity not in s.TABLES:raise HTTPException(404,'Cadastro desconhecido')
    if entity not in PUBLIC:permit(a,{'admin','authorized_vehicle_lookup'})
    table=s.TABLES[entity];q=select(table)
    if entity in PUBLIC:q=q.join(s.source_snapshot,table.c.source_snapshot_id==s.source_snapshot.c.id).where(s.source_snapshot.c.access_level=='public')
    for k,v in [('vehicle_category',vehicle_category),('powertrain_type',powertrain_type),('identity_status',identity_status),('state',state),('manufacturer',manufacturer),('model',model)]:
        if k in table.c and v:q=q.where(table.c[k]==v)
    with db.engine.begin() as c:
        total=c.execute(select(func.count()).select_from(q.subquery())).scalar()
        rows=[dict(r) for r in c.execute(q.order_by(table.c.id).limit(limit).offset(offset)).mappings()]
        if entity in ('vehicles','battery-assets'):
            for row in rows:
                row['pending']=record_pending(c,entity,row)
        if entity=='battery-models':
            for row in rows:
                specs=c.execute(select(s.vehicle_spec.c.manufacturer,s.vehicle_spec.c.model,s.vehicle_spec.c.trim,s.vehicle_spec.c.model_year_start,s.compatibility.c.status).join(s.compatibility,s.compatibility.c.vehicle_spec_id==s.vehicle_spec.c.id).join(s.source_snapshot,s.compatibility.c.source_snapshot_id==s.source_snapshot.c.id).where(s.compatibility.c.battery_model_id==row['id'],s.source_snapshot.c.access_level=='public')).mappings()
                row['declared_applications']='; '.join(f"{r['manufacturer']} {r['model']} {r['trim'] or ''} {r['model_year_start'] or ''} · {r['status']}" for r in specs)
        if entity not in PUBLIC:service.audit(c,a,'list',entity,None,{'returned':len(rows)})
    return {'items':rows,'total':total}

def record_pending(c,entity,row):
    pending=[]
    if entity=='vehicles':
        if row['identity_status'] not in ('identified','verified'):pending.append({'code':'identity','label':'Identificação pendente'})
        current=c.execute(select(s.installation.c.id).where(s.installation.c.vehicle_id==row['id'],s.installation.c.removed_at.is_(None))).first()
        if not current:pending.append({'code':'installation','label':'Sem bateria vinculada'})
    else:
        if not any(row.get(k) for k in ('battery_identifier','manufacturer_serial_number','passport_identifier')):pending.append({'code':'identifier','label':'Código da bateria pendente'})
        passport=passport_data(c,row['id'])
        if passport['filled']<passport['total']:pending.append({'code':'passport','label':f"{passport['total']-passport['filled']} campos do passaporte pendentes"})
    return pending

class RecordSearch(BaseModel):
    term:str=Field(min_length=1,max_length=100)
    limit:int=Field(25,ge=1,le=50)

class BatteryIdentity(BaseModel):
    source_snapshot_id:str
    battery_identifier:str|None=Field(None,min_length=1,max_length=200)
    manufacturer_serial_number:str|None=Field(None,min_length=1,max_length=200)
    passport_identifier:str|None=Field(None,min_length=1,max_length=200)

@router.post('/battery-assets/{key}/identity')
def update_battery_identity(key:str,body:BatteryIdentity,a=Depends(actor)):
    permit(a,{'admin'})
    try:
        with write_lock,db.engine.begin() as c:
            get_one(c,s.battery_asset,key)
            src=get_one(c,s.source_snapshot,body.source_snapshot_id)
            if src['access_level']!='restricted':raise HTTPException(422,'A identificação exige fonte restrita autorizada')
            data=body.model_dump(exclude_unset=True)
            c.execute(update(s.battery_asset).where(s.battery_asset.c.id==key).values(**data))
            service.audit(c,a,'update_identity','battery-assets',key,{'source_snapshot_id':body.source_snapshot_id})
    except IntegrityError:raise HTTPException(409,'Identificador do passaporte já cadastrado')
    return {'id':key,'status':'updated'}

@router.post('/records/search')
def search_records(body:RecordSearch,a=Depends(actor)):
    permit(a,{'admin','authorized_vehicle_lookup'})
    term=body.term.strip()
    if not term:raise HTTPException(422,'Informe um termo para buscar')
    pattern='%'+term.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
    items=[];total=0
    with db.engine.begin() as c:
        for entity,table,fields in [('vehicles',s.vehicle_asset,('id','plate','vin','renavam','manufacturer','model')),('battery-assets',s.battery_asset,('id','battery_identifier','manufacturer_serial_number','passport_identifier'))]:
            condition=or_(*(table.c[field].ilike(pattern,escape='\\') for field in fields))
            total+=c.execute(select(func.count()).select_from(table).where(condition)).scalar()
            remaining=body.limit-len(items)
            if remaining>0:
                for record in c.execute(select(table).where(condition).order_by(table.c.id).limit(remaining)).mappings():
                    row=dict(record);items.append({'kind':entity,'record':row,'pending':record_pending(c,entity,row)})
        service.audit(c,a,'search','individual-records',None,{'returned':len(items)})
    return {'items':items,'total':total,'limit':body.limit}

@router.get('/registry/{entity}/{key}')
def detail(entity:str,key:str,a=Depends(actor)):
    if entity not in s.TABLES:raise HTTPException(404,'Cadastro desconhecido')
    if entity not in PUBLIC:permit(a,{'admin','authorized_vehicle_lookup'})
    with db.engine.begin() as c:
        row=get_one(c,s.TABLES[entity],key)
        src=get_one(c,s.source_snapshot,row['source_snapshot_id'])
        if entity in PUBLIC and src['access_level']!='public':raise HTTPException(404,'Registro público não encontrado')
        result={'record':row,'source':src}
        if entity in ('vehicles','battery-assets'):result['pending']=record_pending(c,entity,row)
        if entity in ('battery-models','vehicle-specs'):
            field=s.compatibility.c.battery_model_id if entity=='battery-models' else s.compatibility.c.vehicle_spec_id
            result['evidence']=[dict(r) for r in c.execute(select(s.compatibility).join(s.source_snapshot,s.compatibility.c.source_snapshot_id==s.source_snapshot.c.id).where(field==key,s.source_snapshot.c.access_level=='public')).mappings()]
            result['quality']=service.quality({'confidence':row.get('confidence')},row if entity=='battery-models' else None,src)
        if entity=='vehicles':
            result['installations']=[dict(x) for x in c.execute(select(s.installation).where(s.installation.c.vehicle_id==key)).mappings()]
            for installation in result['installations']:
                installation['battery']=get_one(c,s.battery_asset,installation['battery_id'])
            result['quality']=service.quality(row,source=src)
        if entity=='battery-assets':
            result['passport']=passport_data(c,key)
            result['lifecycle']=[dict(x) for x in c.execute(select(s.lifecycle).where(s.lifecycle.c.battery_id==key).order_by(s.lifecycle.c.observed_at)).mappings()]
            result['installations']=[dict(x) for x in c.execute(select(s.installation).where(s.installation.c.battery_id==key)).mappings()]
            for installation in result['installations']:
                installation['vehicle']=get_one(c,s.vehicle_asset,installation['vehicle_id'])
            result['model']=get_one(c,s.battery_model,row['battery_model_id']) if row['battery_model_id'] else None
            result['quality']=service.quality(row,result['model'],src,result['lifecycle'][-1] if result['lifecycle'] else None)
        if entity not in PUBLIC:service.audit(c,a,'read',entity,key)
    return result

@router.post('/registry/{entity}',status_code=201)
def create_entity(entity:str,body:dict,a=Depends(actor)):
    if entity not in MODELS:raise HTTPException(404,'Cadastro desconhecido')
    permit(a,{'admin','data_engineer'} if entity in PUBLIC else {'admin'})
    try:data=MODELS[entity].model_validate(body).model_dump(mode='json')
    except ValidationError as e:raise HTTPException(422,str(e))
    key=service.uid();table=s.TABLES[entity]
    try:
        with write_lock,db.engine.begin() as c:
            validate_references(c,table,data)
            src=get_one(c,s.source_snapshot,data['source_snapshot_id'])
            if entity not in PUBLIC and src['access_level']!='restricted':raise HTTPException(422,'Ativos individuais exigem fonte restrita autorizada')
            if entity=='vehicles':data.update(first_seen_at=now(),last_seen_at=now())
            if entity=='evidence':
                if data['status']=='accepted':permit(a,{'admin'});data.update(verified_by=a['actor'],verified_at=now())
            if entity=='installations':
                matches=c.execute(select(s.installation).where(or_(s.installation.c.vehicle_id==data['vehicle_id'],s.installation.c.battery_id==data['battery_id']))).mappings()
                for r in matches:
                    if (r['battery_id']==data['battery_id'] or (r.get('position') or 'traction-1')==data['position']) and data['installed_at']<(r['removed_at'] or '9999-12-31') and r['installed_at']<(data['removed_at'] or '9999-12-31'):raise HTTPException(409,'Instalação sobreposta; registre a remoção anterior')
                if data['removed_at'] is None:c.execute(update(s.battery_asset).where(s.battery_asset.c.id==data['battery_id']).values(current_status='automotive_first_life'))
            if entity=='lifecycle':
                previous=c.execute(select(s.lifecycle).where(s.lifecycle.c.battery_id==data['battery_id']).order_by(s.lifecycle.c.observed_at.desc())).mappings().first()
                if previous and data['observed_at']<=previous['observed_at']:raise HTTPException(409,'Observação deve ser posterior à última registrada')
                for k in ('cycle_count','energy_throughput_kwh','odometer_km'):
                    if previous and previous[k] is not None and data[k] is not None and data[k]<previous[k]:raise HTTPException(422,'Contador acumulado não pode diminuir')
                changes={k:data[k] for k in ('soh_percent','soce_percent','cycle_count','energy_throughput_kwh','current_status') if data.get(k) is not None}
                changes.update(last_observed_at=data['observed_at'])
                if data['temperature_c'] is not None:changes['last_known_temperature']=data['temperature_c']
                if data['soc_percent'] is not None:changes['last_known_soc']=data['soc_percent']
                c.execute(update(s.battery_asset).where(s.battery_asset.c.id==data['battery_id']).values(**changes))
            c.execute(insert(table).values(id=key,**data));service.audit(c,a,'create',entity,key)
    except IntegrityError:raise HTTPException(409,'Identificador duplicado ou relação inválida')
    return {'id':key,'status':'created'}

class Remove(BaseModel):
    removed_at:date
    source_snapshot_id:str

class GuidedRegistration(BaseModel):
    vehicle:Vehicle
    battery:BatteryAsset|None=None
    battery_id:str|None=None
    installed_at:date|None=None
    position:str=Field('traction-1',min_length=1,max_length=50)

@router.post('/vehicles/register-guided',status_code=201)
def register_guided(body:GuidedRegistration,a=Depends(actor)):
    permit(a,{'admin'})
    if body.battery and body.battery_id:raise HTTPException(422,'Escolha uma bateria existente ou uma nova')
    if bool(body.battery or body.battery_id)!=bool(body.installed_at):
        raise HTTPException(422,'O vínculo exige uma bateria e a data da instalação')
    vid=service.uid();bid=body.battery_id
    try:
        with write_lock,db.engine.begin() as c:
            vehicle=body.vehicle.model_dump(mode='json')
            source=get_one(c,s.source_snapshot,vehicle['source_snapshot_id'])
            if source['access_level']!='restricted':raise HTTPException(422,'Selecione uma fonte restrita autorizada')
            validate_references(c,s.vehicle_asset,vehicle)
            if body.battery:
                battery=body.battery.model_dump(mode='json')
                battery_source=get_one(c,s.source_snapshot,battery['source_snapshot_id'])
                if battery_source['access_level']!='restricted':raise HTTPException(422,'A bateria exige fonte restrita autorizada')
                validate_references(c,s.battery_asset,battery)
                bid=service.uid()
                c.execute(insert(s.battery_asset).values(id=bid,**battery))
                service.audit(c,a,'create','battery-assets',bid)
            elif bid:
                get_one(c,s.battery_asset,bid)
            if bid:
                overlap=c.execute(select(s.installation.c.id).where(s.installation.c.battery_id==bid,or_(s.installation.c.removed_at.is_(None),s.installation.c.removed_at>str(body.installed_at)))).first()
                if overlap:raise HTTPException(409,'A bateria tem instalação atual ou posterior à data informada; registre a remoção antes de vinculá-la')
            c.execute(insert(s.vehicle_asset).values(id=vid,**vehicle,first_seen_at=now(),last_seen_at=now()))
            service.audit(c,a,'create','vehicles',vid)
            if bid:
                installation=Installation(vehicle_id=vid,battery_id=bid,source_snapshot_id=vehicle['source_snapshot_id'],installed_at=body.installed_at,position=body.position).model_dump(mode='json')
                c.execute(insert(s.installation).values(id=service.uid(),**installation))
                c.execute(update(s.battery_asset).where(s.battery_asset.c.id==bid).values(current_status='automotive_first_life'))
                service.audit(c,a,'create_guided_installation','vehicles',vid,{'battery_id':bid})
    except IntegrityError:raise HTTPException(409,'Identificador duplicado; nenhum cadastro desta operação foi salvo')
    return {'id':vid,'battery_id':bid,'status':'created'}

class EvidenceReview(BaseModel):
    status:str=Field(pattern=r'^(accepted|rejected)$')
    notes:str=Field(min_length=10,max_length=2000)

@router.post('/evidence/{key}/review')
def review_evidence(key:str,body:EvidenceReview,a=Depends(actor)):
    permit(a,{'admin'})
    with write_lock,db.engine.begin() as c:
        row=get_one(c,s.compatibility,key)
        if row['status']!='candidate':raise HTTPException(409,'Evidência já revisada; cadastre nova evidência para corrigir')
        c.execute(update(s.compatibility).where(s.compatibility.c.id==key).values(status=body.status,notes=body.notes,verified_by=a['actor'],verified_at=now()))
        service.audit(c,a,'review','evidence',key,body.model_dump())
    return {'id':key,'status':body.status,'note':'A revisão não reescreve snapshots nem comprova instalação física.'}

class PublishEvidence(BaseModel):
    source_snapshot_id:str

@router.post('/evidence/{key}/publish')
def publish_evidence(key:str,body:PublishEvidence,a=Depends(actor)):
    permit(a,{'admin'})
    from src.matching import normalize
    import hashlib
    with write_lock,db.engine.begin() as c:
        evidence=get_one(c,s.compatibility,key);spec=get_one(c,s.vehicle_spec,evidence['vehicle_spec_id'])
        src=get_one(c,s.source_snapshot,body.source_snapshot_id)
        evidence_src=get_one(c,s.source_snapshot,evidence['source_snapshot_id'])
        model=get_one(c,s.battery_model,evidence['battery_model_id'])
        model_src=get_one(c,s.source_snapshot,model['source_snapshot_id'])
        if evidence['status']!='accepted':raise HTTPException(422,'Evidência precisa ser aceita antes da publicação')
        if any(x['access_level']!='public' for x in (src,evidence_src,model_src)):raise HTTPException(422,'Publicação exige fontes públicas')
        if not spec['model_year_start'] or not spec['model_year_end'] or not spec['trim']:raise HTTPException(422,'Informe versão e intervalo de anos explícitos na especificação')
        new_id=service.stable('derived:'+src['id']+':'+key)
        if c.execute(select(s.source_snapshot.c.id).where(s.source_snapshot.c.id==new_id)).first():return {'id':new_id,'status':'unchanged'}
        rows=[dict(r) for r in c.execute(select(s.aggregate).where(s.aggregate.c.source_snapshot_id==src['id'])).mappings()]
        matched=0
        for row in rows:
            year=row.get('manufacture_year') or row['model_year']
            compatible=year is not None and spec['model_year_start']<=year<=spec['model_year_end'] and all(normalize(row.get(k) or '')==normalize(spec.get(sk) or '') for k,sk in [('manufacturer','manufacturer'),('model','model'),('trim','trim')])
            if evidence['valid_from'] and year is not None:compatible=compatible and year>=int(evidence['valid_from'][:4])
            if evidence['valid_to'] and year is not None:compatible=compatible and year<=int(evidence['valid_to'][:4])
            compatible=compatible and spec['powertrain']==row['powertrain_type'] and spec['powertrain']!='UNKNOWN' and spec['country_market']==row['country']
            if compatible:
                row.update(battery_model_id=model['id'],vehicle_spec_id=spec['id'],confidence=evidence['confidence']);matched+=1
            row.update(id=service.uid(),source_snapshot_id=new_id)
        if not matched:raise HTTPException(422,'Nenhuma correspondência exata de fabricante, modelo, versão e ano. Revise a especificação; não foi aplicado matching aproximado.')
        copied={k:v for k,v in src.items() if k!='id'}
        copied.update(source_version=(src.get('source_version') or src['reference_period'])+'+review',transformation_version=service.VERSION,retrieved_at=now(),metadata={'parent_snapshot_id':src['id'],'evidence_id':key,'matched_groups':matched,'raw_file_hash':src['raw_file_hash']},raw_file_hash=hashlib.sha256((src['raw_file_hash']+':'+key).encode()).hexdigest(),method='review',notes='Versão derivada; hash desta transformação, hash RAW preservado em metadata. Quantidades inalteradas.')
        c.execute(insert(s.source_snapshot).values(id=new_id,**copied))
        for i in range(0,len(rows),1000):c.execute(insert(s.aggregate),rows[i:i+1000])
        service.audit(c,a,'publish','source_snapshot',new_id,{'parent':src['id'],'matched':matched})
    return {'id':new_id,'matched_groups':matched,'status':'success'}

@router.post('/installations/{key}/remove')
def remove_installation(key:str,body:Remove,a=Depends(actor)):
    permit(a,{'admin'})
    with write_lock,db.engine.begin() as c:
        row=get_one(c,s.installation,key);src=get_one(c,s.source_snapshot,body.source_snapshot_id)
        if src['access_level']!='restricted':raise HTTPException(422,'Exige fonte restrita')
        if row['removed_at']:raise HTTPException(409,'Remoção já registrada')
        if str(body.removed_at)<row['installed_at']:raise HTTPException(422,'Remoção anterior à instalação')
        c.execute(update(s.installation).where(s.installation.c.id==key).values(removed_at=str(body.removed_at)))
        c.execute(update(s.battery_asset).where(s.battery_asset.c.id==row['battery_id']).values(current_status='removed'))
        service.audit(c,a,'remove','installations',key,body.model_dump(mode='json'))
    return {'status':'removed'}

class Materialize(BaseModel):
    count:int=Field(ge=1,le=1000)

@router.post('/observations/{key}/materialize')
def materialize(key:str,body:Materialize,a=Depends(actor)):
    permit(a,{'admin'})
    with write_lock,db.engine.begin() as c:
        row=get_one(c,s.aggregate,key)
        used=c.execute(select(func.count()).select_from(s.vehicle_asset).where(s.vehicle_asset.c.aggregate_id==key)).scalar()
        if used+body.count>row['quantity']:raise HTTPException(409,'Quantidade excede o agregado de origem')
        ids=[]
        for slot in range(used,used+body.count):
            vid=service.uid();ids.append(vid)
            c.execute(insert(s.vehicle_asset).values(id=vid,aggregate_id=key,slot_number=slot,source_snapshot_id=row['source_snapshot_id'],manufacturer=row['manufacturer'],model=row['model'],trim=row['trim'],model_year=row['model_year'],manufacture_year=row.get('manufacture_year'),vehicle_category=row['vehicle_category'],powertrain_type=row['powertrain_type'],state=row['state'],municipality=row['municipality'],identity_status='aggregate_derived',identity_confidence='unknown',first_seen_at=now(),last_seen_at=now()))
        service.audit(c,a,'materialize','observations',key,{'count':body.count})
    return {'ids':ids,'identity_status':'aggregate_derived','note':'Chaves internas; VIN e serial permanecem NULL. Não adicionar estes slots à quantidade agregada.'}

@router.post('/vehicles/{key}/identify')
def identify(key:str,body:Vehicle,a=Depends(actor)):
    permit(a,{'admin'})
    if body.identity_status not in ('identified','partially_identified','verified'):raise HTTPException(422,'Status de identificação necessário')
    try:
        with write_lock,db.engine.begin() as c:
            get_one(c,s.vehicle_asset,key);src=get_one(c,s.source_snapshot,body.source_snapshot_id)
            if src['access_level']!='restricted':raise HTTPException(422,'Identificação exige fonte restrita autorizada')
            validate_references(c,s.vehicle_asset,body.model_dump())
            c.execute(update(s.vehicle_asset).where(s.vehicle_asset.c.id==key).values(**body.model_dump(mode='json',exclude_unset=True),last_seen_at=now()))
            service.audit(c,a,'identify','vehicles',key,{'source_snapshot_id':body.source_snapshot_id})
    except IntegrityError:raise HTTPException(409,'Identificador já associado a outro veículo')
    return {'id':key,'status':body.identity_status}

class Lookup(BaseModel):
    identifier:str=Field(min_length=7,max_length=30)

@router.post('/vehicles/lookup')
def lookup(body:Lookup,a=Depends(actor)):
    permit(a,{'admin','authorized_vehicle_lookup'})
    with db.engine.begin() as c:
        rows=[dict(r) for r in c.execute(select(s.vehicle_asset).where(or_(s.vehicle_asset.c.vin==body.identifier,s.vehicle_asset.c.renavam==body.identifier,s.vehicle_asset.c.plate==body.identifier))).mappings()]
        service.audit(c,a,'lookup','vehicles',None,{'returned':len(rows)})
    return rows

@router.get('/audit')
def audit(a=Depends(actor)):
    permit(a,{'admin'})
    with db.engine.connect() as c:return [dict(r) for r in c.execute(select(s.audit).order_by(s.audit.c.created_at.desc()).limit(200)).mappings()]

@router.get('/contracts')
def contracts(a=Depends(actor)):
    permit(a,{'admin','data_engineer'})
    return {**{k:m.model_json_schema() for k,m in MODELS.items()},'sources':Source.model_json_schema()}


def passport_data(c, key):
    from src.battery_passport import FIELDS
    record=c.execute(select(s.passport).where(s.passport.c.battery_id==key)).mappings().first()
    battery=get_one(c,s.battery_asset,key)
    model=get_one(c,s.battery_model,battery['battery_model_id']) if battery['battery_model_id'] else {}
    values={}
    for fid, value in {'1':battery.get('passport_identifier') or battery.get('battery_identifier'), '2':battery.get('passport_issuer'),'3':model.get('manufacturer'),'7':battery.get('manufacturer_serial_number'),'9':battery.get('manufacturing_date'),'11':model.get('nominal_capacity_kwh'),'12':model.get('chemistry'),'25':model.get('rated_capacity_ah'),'26':model.get('min_voltage_v'),'27':model.get('nominal_voltage_v'),'28':model.get('max_voltage_v'),'31':model.get('expected_cycle_life'),'35':model.get('warranty_years'),'61':battery.get('soce_percent'),'62':battery.get('soh_percent'),'67':battery.get('current_status') if battery.get('current_status')!='unknown' else None,'68':battery.get('cycle_count'),'70':battery.get('last_known_temperature'),'71':battery.get('last_known_soc')}.items():
        if value is not None:values[fid]=str(value)
    inherited=dict(values)
    saved=dict(record['values'] if record else {})
    dynamic={'61','62','67','68','70','71'}
    for field in dynamic:
        if field in inherited:saved.pop(field,None)
    values.update(saved)
    return {'fields':FIELDS,'values':values,'overrides':saved,'inherited':inherited,'filled':sum(v is not None and v != '' for v in values.values()),'total':len(FIELDS),'updated_at':record['updated_at'] if record else None}

class PassportUpdate(BaseModel):
    values:dict[str,str|None]

@router.post('/battery-assets/{key}/passport')
def save_passport(key:str,body:PassportUpdate,a=Depends(actor)):
    permit(a,{'admin'})
    from src.battery_passport import FIELDS
    allowed={f['id'] for f in FIELDS}
    if set(body.values)-allowed or any(v is not None and len(v)>10000 for v in body.values.values()):
        raise HTTPException(422,'Campo desconhecido ou valor acima de 10.000 caracteres')
    with write_lock,db.engine.begin() as c:
        get_one(c,s.battery_asset,key)
        inherited=passport_data(c,key)['inherited']
        dynamic={'61','62','67','68','70','71'}
        if any(k in dynamic and k in inherited for k in body.values):
            raise HTTPException(422,'Dados dinâmicos medidos devem ser atualizados no histórico da bateria')
        record=c.execute(select(s.passport).where(s.passport.c.battery_id==key)).mappings().first()
        values={**(record['values'] if record else {}),**body.values}
        data={'values':values,'updated_at':now()}
        if record:c.execute(update(s.passport).where(s.passport.c.battery_id==key).values(**data))
        else:c.execute(insert(s.passport).values(id=service.uid(),battery_id=key,**data))
        service.audit(c,a,'update_passport','battery-assets',key,{'fields':list(body.values)})
        return passport_data(c,key)
