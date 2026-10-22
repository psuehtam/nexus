"""Public permanent registry and authenticated v3 operations."""
import hashlib
import json
from datetime import date,datetime,timedelta,timezone
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel,Field,ConfigDict
from sqlalchemy import select,func,or_,and_,insert,update,case
from sqlalchemy.exc import IntegrityError
from src import db,intelligence_schema as s,operational_schema as o,fleet_publication as fleet,registry_service as service
from src.auth import require_admin
from src.connectors.public import now
from src import opportunities

router=APIRouter(prefix='/api/v3',tags=['Veículos e baterias'])

@router.get('/opportunities/settings')
def opportunity_settings():
    with db.engine.connect() as c:
        rows={r.key:r.value for r in c.execute(select(o.setting).where(o.setting.c.key.in_(['opportunity_mode','opportunity_soh_limit'])))}
    return {'mode':(rows.get('opportunity_mode') or {}).get('mode','real'),
            'soh_limit':(rows.get('opportunity_soh_limit') or {}).get('value',80)}

class OpportunitySettings(BaseModel):
    mode:str|None=None
    soh_limit:float|None=Field(None,ge=50,le=100)

@router.put('/admin/opportunities/settings')
def set_opportunity_settings(body:OpportunitySettings,user=Depends(require_admin)):
    if body.mode is not None and body.mode not in ('real','simulated'):raise HTTPException(422,'Modo inválido')
    with db.engine.begin() as c:
        for key,value in [('opportunity_mode',{'mode':body.mode} if body.mode else None),
                          ('opportunity_soh_limit',{'value':body.soh_limit} if body.soh_limit is not None else None)]:
            if value is None:continue
            from src.fleet_publication import insert_once
            insert_once(c,o.setting,dict(key=key,value=value,updated_at=now()),['key'])
            c.execute(update(o.setting).where(o.setting.c.key==key).values(value=value,updated_at=now()))
    return opportunity_settings()

@router.get('/opportunities')
def opportunity_list(reference:str|None=None,state:str|None=None,municipality:str|None=None,
                     vehicle_category:str|None=None,powertrain_type:str|None=None,model:str|None=None,
                     status_filter:str|None=Query(None,pattern='^(priority|near|monitor|stale|insufficient)$'),
                     limit:int=Query(50,ge=1,le=200),offset:int=Query(0,ge=0)):
    settings=opportunity_settings()
    with db.engine.connect() as c:
        fn=opportunities.simulated_rows if settings['mode']=='simulated' else opportunities.real_rows
        result=fn(c,reference,state,municipality,vehicle_category,powertrain_type,model,limit,offset,settings['soh_limit'],status_filter)
    return {'mode':settings['mode'],'soh_limit':settings['soh_limit'],**result}

@router.get('/opportunities/overview')
def opportunity_overview(reference:str|None=None,state:str|None=None,municipality:str|None=None,
                         vehicle_category:str|None=None,powertrain_type:str|None=None,model:str|None=None):
    settings=opportunity_settings()
    with db.engine.connect() as c:
        result=opportunities.overview(c,settings['mode'],reference,state,municipality,
                                      vehicle_category,powertrain_type,model,settings['soh_limit'])
    return {'soh_limit':settings['soh_limit'],**result}

@router.get('/opportunities/simulated/{vehicle_id}/passport')
def simulated_opportunity_passport(vehicle_id:str):
    if opportunity_settings()['mode']!='simulated':raise HTTPException(404,'Simulação desativada')
    with db.engine.connect() as c:
        reg=service.resolve_vehicle(c,vehicle_id)
        v=service.get(c,s.vehicle_asset,reg['vehicle_id'])
        m=service.get(c,o.model,reg['current_model_id'])
        locations=opportunities.location_weights(c)
    return opportunities.simulated_profile(v,m,locations)

@router.get('/opportunities/regions')
def opportunity_regions(reference:str|None=None,state:str|None=None,municipality:str|None=None,
                        vehicle_category:str|None=None,powertrain_type:str|None=None,model:str|None=None,
                        limit:int=Query(50,ge=1,le=200),offset:int=Query(0,ge=0)):
    with db.engine.connect() as c:
        source=c.execute(select(s.source_snapshot).where(s.source_snapshot.c.provider=='SENATRAN',
            s.source_snapshot.c.dataset_name.like('municipio-marca-modelo-ano-fabricacao%'),
            s.source_snapshot.c.metric=='fleet_stock',s.source_snapshot.c.ingestion_status=='success',
            *([s.source_snapshot.c.reference_period==reference] if reference else [])
            ).order_by(s.source_snapshot.c.reference_period.desc(),s.source_snapshot.c.retrieved_at.desc()).limit(1)).mappings().first()
        if not source:return {'items':[],'total':0,'reference':reference}
        q=select(s.aggregate.c.state,s.aggregate.c.municipality,s.aggregate.c.manufacturer,s.aggregate.c.model,
            s.aggregate.c.vehicle_category,s.aggregate.c.powertrain_type,s.aggregate.c.manufacture_year,
            s.aggregate.c.quantity).where(s.aggregate.c.source_snapshot_id==source['id'])
        for col,val in ((s.aggregate.c.state,state),(s.aggregate.c.municipality,municipality),
                        (s.aggregate.c.vehicle_category,vehicle_category),(s.aggregate.c.powertrain_type,powertrain_type)):
            if val:q=q.where(col==val)
        if model:q=q.where(s.aggregate.c.model.ilike('%'+model+'%'))
        grouped={}
        for row in c.execute(q).mappings():
            if not row['manufacture_year']:continue
            key=(row['state'],row['municipality'],row['manufacturer'],row['model'],row['vehicle_category'],row['powertrain_type'])
            item=grouped.setdefault(key,dict(zip(('state','municipality','manufacturer','model','vehicle_category','powertrain_type'),key),vehicles=0,older_vehicles=0))
            item['vehicles']+=row['quantity']
            if date.today().year-row['manufacture_year']>=8:item['older_vehicles']+=row['quantity']
        items=sorted(grouped.values(),key=lambda r:(-r['older_vehicles'],-r['vehicles'],str(r['state']),str(r['model'])))
        return {'items':items[offset:offset+limit],'total':len(items),'reference':source['reference_period'],
                'note':'Cenário regional pela idade dos veículos. Não identifica bateria, saúde ou disponibilidade individual.'}

@router.get('/opportunities/places')
def opportunity_places(reference:str|None=None,state:str|None=None,vehicle_category:str|None=None,
                       powertrain_type:str|None=None,model:str|None=None,limit:int=Query(8,ge=1,le=50)):
    """Regional research leads from aggregate fleet age; no individual batteries inferred."""
    with db.engine.connect() as c:
        source=c.execute(select(s.source_snapshot).where(s.source_snapshot.c.provider=='SENATRAN',
            s.source_snapshot.c.dataset_name.like('municipio-marca-modelo-ano-fabricacao%'),
            s.source_snapshot.c.metric=='fleet_stock',s.source_snapshot.c.ingestion_status=='success',
            *([s.source_snapshot.c.reference_period==reference] if reference else [])
        ).order_by(s.source_snapshot.c.reference_period.desc(),s.source_snapshot.c.retrieved_at.desc()).limit(1)).mappings().first()
        if not source:return {'items':[],'reference':reference}
        q=select(s.aggregate.c.state,s.aggregate.c.municipality,
                 func.sum(s.aggregate.c.quantity).label('vehicles')).where(
            s.aggregate.c.source_snapshot_id==source['id'],
            s.aggregate.c.manufacture_year.is_not(None),
            s.aggregate.c.manufacture_year<=date.today().year-8,
            s.aggregate.c.state.is_not(None),s.aggregate.c.municipality.is_not(None))
        for col,val in ((s.aggregate.c.state,state),(s.aggregate.c.vehicle_category,vehicle_category),
                        (s.aggregate.c.powertrain_type,powertrain_type)):
            if val:q=q.where(col==val)
        if model:q=q.where(s.aggregate.c.model.ilike('%'+model+'%'))
        q=q.group_by(s.aggregate.c.state,s.aggregate.c.municipality).order_by(func.sum(s.aggregate.c.quantity).desc()).limit(limit)
        return {'items':[dict(row) for row in c.execute(q).mappings()],
                'reference':source['reference_period'],
                'note':'Veículos com 8 anos ou mais; local de pesquisa, sem bateria individual identificada.'}

LEGACY_SIZE_CATEGORIES=frozenset({'light','heavy','LMT'})

def public_vehicle_type(value):
    """Return the public vehicle type without treating size as a type."""
    return 'unknown' if value in LEGACY_SIZE_CATEGORIES or not value else value

def vehicle_type_expression(column):
    return case((column.in_(LEGACY_SIZE_CATEGORIES),'unknown'),else_=column)

def filter_vehicle_type(query,column,value):
    if not value:return query
    return query.where(column.in_(LEGACY_SIZE_CATEGORIES|{'unknown'})) if value=='unknown' else query.where(column==value)

class Body(BaseModel):
    model_config=ConfigDict(extra='forbid')

class DossierUpdate(Body):
    revision:int=Field(ge=0)
    schema_version:str='1'
    values:dict[str,dict[str,Any]]

class VehicleUpdate(Body):
    revision:int=Field(ge=0)
    vin:str|None=None
    renavam:str|None=None
    plate:str|None=None
    state:str|None=None
    municipality:str|None=None
    evidence:str=Field(min_length=3,max_length=4000)
    verified:bool=False

class LinkBattery(Body):
    battery_id:str|None=None
    serial:str|None=Field(None,max_length=200)
    manufacturer:str|None=Field(None,max_length=200)
    evidence:str=Field(min_length=3,max_length=4000)
    position:str='pack-1'
    installed_at:str|None=None
    transfer_fields:list[str]=[]
    dossier_revision:int|None=None

class Removal(Body):
    removed_at:str|None=None
    evidence:str=Field(min_length=3,max_length=4000)

class Event(Body):
    odometer_km:float|None=Field(None,ge=0,allow_inf_nan=False)
    installation_id:str|None=None
    observed_at:str
    event_type:str=Field('measurement',max_length=80)
    soh_percent:float|None=Field(None,ge=0,le=100)
    soc_percent:float|None=Field(None,ge=0,le=100)
    cycle_count:int|None=Field(None,ge=0)
    temperature_c:float|None=None
    notes:str|None=Field(None,max_length=10000)
    evidence:str=Field(min_length=3,max_length=4000)


def transaction(request,payload,user,fn):
    raw_key=request.headers.get('Idempotency-Key')
    if raw_key and len(raw_key)>200:raise HTTPException(422,'Chave de idempotência muito longa')
    key=hashlib.sha256((user['id']+':'+raw_key).encode()).hexdigest() if raw_key else None
    digest=hashlib.sha256(json.dumps([request.method,request.url.path,payload],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    try:
        with db.engine.begin() as c:
            if key:
                fleet.insert_once(c,o.command,dict(key=key,body_hash=digest,result={},created_at=now()),['key'])
                prior=c.execute(select(o.command).where(o.command.c.key==key).with_for_update()).mappings().one()
                if prior['body_hash']!=digest:raise HTTPException(409,'Chave já usada com outro conteúdo')
                if prior['result']:return prior['result']
            result=fn(c)
            if key:c.execute(update(o.command).where(o.command.c.key==key).values(result=result))
            return result
    except IntegrityError as exc:raise HTTPException(409,'Conflito: identificador, posição ou cadastro já existente. Recarregue e revise.') from exc


def publication(c,publication_id=None,reference=None):
    if publication_id:
        row=c.execute(select(s.fleet_publication).where(s.fleet_publication.c.id==publication_id,s.fleet_publication.c.status=='complete')).mappings().first()
    elif reference:
        row=c.execute(select(s.fleet_publication).join(o.publication_state,o.publication_state.c.publication_id==s.fleet_publication.c.id)
            .where(s.fleet_publication.c.reference_period==reference,s.fleet_publication.c.status=='complete')
            .order_by(o.publication_state.c.generation.desc()).limit(1)).mappings().first()
    else:row=fleet.active_publication(c)
    if not row and (publication_id or reference):raise HTTPException(404,'Competência/publicação indisponível')
    return dict(row) if row else None


def serialize_publication(c,pub):
    if not pub:return None
    src=service.get(c,s.source_snapshot,pub['source_snapshot_id'])
    from src import pipeline_schema as ps
    evaluations=c.execute(select(ps.evaluation.c.alerts).join(ps.classification,ps.evaluation.c.classification_id==ps.classification.c.id).where(ps.classification.c.source_snapshot_id==pub['source_snapshot_id'])).scalars().all()
    alerts=[item for batch in evaluations for item in batch]
    if not evaluations:alerts=[dict(r) for r in c.execute(select(o.alert.c.code,o.alert.c.details).where(o.alert.c.publication_id==pub['id'])).mappings()]
    return {**pub,'coverage':src.get('scope'),'metadata':src.get('metadata') or {},'alerts':alerts}


@router.get('/publications')
def publications():
    with db.engine.connect() as c:
        rows=c.execute(select(s.fleet_publication,o.publication_state.c.generation).join(o.publication_state,o.publication_state.c.publication_id==s.fleet_publication.c.id)
            .where(s.fleet_publication.c.status=='complete').order_by(s.fleet_publication.c.reference_period.desc(),o.publication_state.c.generation.desc())).mappings().all()
        seen=set();items=[]
        for r in rows:
            if r['reference_period'] not in seen:items.append(serialize_publication(c,dict(r)));seen.add(r['reference_period'])
        current=date.today().replace(day=1)
        closed=current-timedelta(days=1)
        expected=[];year,month=2026,1
        while (year,month)<=(closed.year,closed.month):
            expected.append(f'{year:04d}-{month:02d}')
            year,month=(year+1,1) if month==12 else (year,month+1)
        available={item['reference_period'] for item in items}
        return {'items':items,'active':publication(c),'expected_through':closed.strftime('%Y-%m'),
                'missing_references':[ref for ref in expected if ref not in available],
                'official_source':'https://www.gov.br/transportes/pt-br/assuntos/transito/conteudo-Senatran/frota-de-veiculos-2026'}


@router.get('/fleet/status')
def status():
    with db.engine.connect() as c:
        running=c.execute(select(s.fleet_publication).where(s.fleet_publication.c.status.in_(['pending','running','failed'])).order_by(s.fleet_publication.c.reference_period.desc())).mappings().first()
        return {'active':serialize_publication(c,publication(c)),'processing':dict(running) if running else None}


def model_query(pub):
    # Aggregate each registry once instead of repeating scans for every card.
    registered = select(o.registry.c.current_model_id.label('model_id'), func.count().label('n')).where(
        o.registry.c.visible.is_(True)).group_by(o.registry.c.current_model_id).subquery()
    identified = select(o.registry.c.current_model_id.label('model_id'), func.count().label('n')).select_from(
        o.registry.join(s.vehicle_asset, o.registry.c.vehicle_id == s.vehicle_asset.c.id)).where(
        o.registry.c.visible.is_(True), s.vehicle_asset.c.identity_status.in_(['informed', 'verified', 'identified'])
        ).group_by(o.registry.c.current_model_id).subquery()
    batteries = select(o.registry.c.current_model_id.label('model_id'), func.count(func.distinct(o.current_install.c.vehicle_id)).label('n')).select_from(
        o.current_install.join(o.registry, o.registry.c.vehicle_id == o.current_install.c.vehicle_id)).where(
        o.registry.c.visible.is_(True)).group_by(o.registry.c.current_model_id).subquery()
    quantities = select(o.group_model.c.model_id, func.sum(s.fleet_model_group.c.quantity).label('n')).select_from(
        s.fleet_model_group.join(o.group_model, o.group_model.c.group_id == s.fleet_model_group.c.id)).where(
        s.fleet_model_group.c.publication_id == (pub['id'] if pub else '')).group_by(o.group_model.c.model_id).subquery()
    rc, ic, bc = (func.coalesce(table.c.n, 0) for table in (registered, identified, batteries))
    quantity = func.coalesce(quantities.c.n, 0)
    query = select(o.model, quantity.label('quantity'), rc.label('units_created'), ic.label('identified_count'), bc.label('batteries_registered')).select_from(
        o.model.outerjoin(registered, registered.c.model_id == o.model.c.id)
        .outerjoin(identified, identified.c.model_id == o.model.c.id)
        .outerjoin(batteries, batteries.c.model_id == o.model.c.id)
        .outerjoin(quantities, quantities.c.model_id == o.model.c.id)).where(
        or_(rc > 0, quantity > 0), o.model.c.powertrain_type.in_(['BEV','PHEV','HEV','HEV_FLEX','HYBRID','MHEV','MHEV_12V','MHEV_48V','FCEV']))
    return query,rc,ic,bc


@router.get('/models')
def models(manufacturer:str|None=None,model:str|None=None,powertrain_type:str|None=None,vehicle_type:str|None=None,vehicle_category:str|None=None,
           vehicle_subtype:str|None=None,segment:str|None=None,is_heavy_vehicle:bool|None=None,status:str|None=None,
           reference:str|None=None,publication_id:str|None=None,q:str|None=None,limit:int=Query(50,ge=1,le=200),offset:int=Query(0,ge=0)):
    with db.engine.connect() as c:
        pub=publication(c,publication_id,reference);query,rc,ic,bc=model_query(pub)
        for col,v in [(o.model.c.manufacturer,manufacturer),(o.model.c.model,model),(o.model.c.powertrain_type,powertrain_type)]:
            if v:query=query.where(col.ilike('%'+v+'%'))
        query=filter_vehicle_type(query,o.model.c.vehicle_category,vehicle_type or vehicle_category)
        if vehicle_subtype:query=query.where(o.model.c.vehicle_subtype.ilike('%'+vehicle_subtype+'%'))
        if segment:query=query.where(o.model.c.segment.ilike('%'+segment+'%'))
        if is_heavy_vehicle is not None:query=query.where(o.model.c.is_heavy_vehicle.is_(is_heavy_vehicle))
        if q:query=query.where(or_(o.model.c.manufacturer.ilike('%'+q+'%'),o.model.c.model.ilike('%'+q+'%')))
        if status=='with_battery':query=query.where(bc>0)
        elif status=='without_battery':query=query.where(bc==0)
        elif status=='identified':query=query.where(ic>0)
        elif status=='pending':query=query.where(rc>ic)
        total=c.execute(select(func.count()).select_from(query.subquery())).scalar()
        items=[dict(r) for r in c.execute(query.order_by(o.model.c.manufacturer,o.model.c.model,o.model.c.id).limit(limit).offset(offset)).mappings()]
        for r in items:
            r['vehicle_type']=public_vehicle_type(r.get('vehicle_category'))
            r['progress_percent']=min(100,round(100*r['units_created']/r['quantity'],1)) if r['quantity'] else 100
        return {'publication':serialize_publication(c,pub),'items':items,'total':total,'limit':limit,'offset':offset}


def vehicle_row(row):
    r=dict(row);r['public_identifier']=r['public_code'];r['revision']=r.get('revision',0)
    r['registration_status']='Identificação verificada' if r.get('identity_status')=='verified' else 'Identificação informada' if any(r.get(k) for k in ('vin','plate','renavam')) else 'Identificação pendente'
    r['battery_status']='Bateria vinculada' if r.get('battery_count',0) else 'Bateria ainda não identificada'
    return r


def units_query(mid):
    count=select(func.count()).select_from(o.current_install).where(o.current_install.c.vehicle_id==o.registry.c.vehicle_id).correlate(o.registry).scalar_subquery()
    return select(s.vehicle_asset,o.registry,count.label('battery_count')).join(o.registry,o.registry.c.vehicle_id==s.vehicle_asset.c.id).where(o.registry.c.current_model_id==mid,o.registry.c.visible.is_(True)),count


@router.get('/models/{model_id}/vehicles')
def model_vehicles(model_id:str,status:str|None=None,q:str|None=None,state:str|None=None,municipality:str|None=None,reference:str|None=None,publication_id:str|None=None,
                   limit:int=Query(50,ge=1,le=200),offset:int=Query(0,ge=0)):
    with db.engine.connect() as c:
        pub=publication(c,publication_id,reference);m=service.resolve_model(c,model_id);mq,*_=model_query(pub)
        counts=c.execute(mq.where(o.model.c.id==m['id'])).mappings().first()
        query,bc=units_query(m['id'])
        if status=='with_battery':query=query.where(bc>0)
        elif status=='without_battery':query=query.where(bc==0)
        elif status=='identified':query=query.where(or_(s.vehicle_asset.c.vin.is_not(None),s.vehicle_asset.c.plate.is_not(None),s.vehicle_asset.c.renavam.is_not(None)))
        elif status=='pending':query=query.where(s.vehicle_asset.c.vin.is_(None),s.vehicle_asset.c.plate.is_(None),s.vehicle_asset.c.renavam.is_(None))
        if q:query=query.where(or_(o.registry.c.public_code.ilike('%'+q+'%'),s.vehicle_asset.c.vin.ilike('%'+q+'%'),s.vehicle_asset.c.plate.ilike('%'+q+'%')))
        if state:query=query.where(s.vehicle_asset.c.state==state)
        if municipality:query=query.where(s.vehicle_asset.c.municipality.ilike('%'+municipality+'%'))
        total=c.execute(select(func.count()).select_from(query.subquery())).scalar()
        items=[vehicle_row(r) for r in c.execute(query.order_by(o.registry.c.sequence,o.registry.c.vehicle_id).limit(limit).offset(offset)).mappings()]
        return {'publication':serialize_publication(c,pub),'model_group':dict(counts) if counts else m,'items':items,'total':total,'limit':limit,'offset':offset,'scope':'Cadastros nacionais; localização individual somente quando informada.'}


@router.get('/vehicles/{identifier}')
def vehicle(identifier:str):
    with db.engine.connect() as c:
        reg=service.resolve_vehicle(c,identifier);item=service.get(c,s.vehicle_asset,reg['vehicle_id']);m=service.get(c,o.model,reg['current_model_id'])
        links=service.installations(c,vehicle_id=item['id']);row=vehicle_row({**item,**reg,'battery_count':sum(x['is_current'] for x in links)})
        return {'publication':publication(c),'vehicle':row,'membership':{**reg,'public_identifier':reg['public_code']},'model_group':m,'installations':links,
            'dossier':service.passport(c,'vehicle',item['id'],False),'registration_status':row['registration_status'],'battery_status':row['battery_status']}


@router.put('/vehicles/{identifier}')
def update_vehicle(identifier:str,body:VehicleUpdate,request:Request,user=Depends(require_admin)):
    payload=body.model_dump(exclude_unset=True)
    result=transaction(request,payload,user,lambda c:{'id':service.identify(c,identifier,payload,user)})
    return {**vehicle(result['id']),'id':result['id']}


@router.get('/vehicles/{identifier}/battery-dossier')
def vehicle_dossier(identifier:str):
    with db.engine.connect() as c:
        reg=service.resolve_vehicle(c,identifier)
        return service.passport(c,'vehicle',reg['vehicle_id'])


@router.put('/vehicles/{identifier}/battery-dossier')
def update_dossier(identifier:str,body:DossierUpdate,request:Request,user=Depends(require_admin)):
    payload=body.model_dump()
    return transaction(request,payload,user,lambda c:service.save_passport(c,'vehicle',service.resolve_vehicle(c,identifier)['vehicle_id'],payload,user))


@router.get('/batteries/{battery_id}')
def battery(battery_id:str):
    with db.engine.connect() as c:
        b=service.get(c,s.battery_asset,battery_id);p=service.passport(c,'battery',battery_id)
        from src.battery_status import summary
        return {**summary(c,battery_id), 'battery':b,'battery_model':service.get(c,s.battery_model,b['battery_model_id']) if b['battery_model_id'] else None,
            'passport':p,'pending_fields':p['counts']['pending'],'installations':service.installations(c,battery_id=battery_id),
            'events':[dict(r) for r in c.execute(select(s.lifecycle).where(s.lifecycle.c.battery_id==battery_id).order_by(s.lifecycle.c.observed_at.desc(),s.lifecycle.c.id.desc())).mappings()]}


@router.get('/batteries/{battery_id}/passport')
def physical_passport(battery_id:str):
    with db.engine.connect() as c:service.get(c,s.battery_asset,battery_id);return service.passport(c,'battery',battery_id)


@router.put('/batteries/{battery_id}/passport')
def update_passport(battery_id:str,body:DossierUpdate,request:Request,user=Depends(require_admin)):
    payload=body.model_dump()
    return transaction(request,payload,user,lambda c:service.save_passport(c,'battery',battery_id,payload,user))


@router.post('/vehicles/{identifier}/batteries')
def add_battery(identifier:str,body:LinkBattery,request:Request,user=Depends(require_admin)):
    payload=body.model_dump()
    return transaction(request,payload,user,lambda c:service.link_battery(c,identifier,payload,user))


@router.post('/installations/{iid}/remove')
def remove_battery(iid:str,body:Removal,request:Request,user=Depends(require_admin)):
    payload=body.model_dump()
    return transaction(request,payload,user,lambda c:service.remove_battery(c,iid,payload,user))


@router.post('/batteries/{battery_id}/events')
def event(battery_id:str,body:Event,request:Request,user=Depends(require_admin)):
    payload=body.model_dump()
    return transaction(request,payload,user,lambda c:service.add_event(c,battery_id,payload,user))


@router.get('/search')
def search(q:str=Query(min_length=2,max_length=200)):
    with db.engine.connect() as c:
        pattern='%'+q+'%'
        visible=select(o.registry.c.vehicle_id).where(o.registry.c.current_model_id==o.model.c.id,o.registry.c.visible.is_(True)).exists()
        ms=[dict(x) for x in c.execute(select(o.model).where(visible,or_(o.model.c.manufacturer.ilike(pattern),o.model.c.model.ilike(pattern))).order_by(o.model.c.manufacturer,o.model.c.model,o.model.c.id).limit(15)).mappings()]
        vs=[vehicle_row(x) for x in c.execute(select(s.vehicle_asset,o.registry).join(o.registry,o.registry.c.vehicle_id==s.vehicle_asset.c.id).where(o.registry.c.visible.is_(True),or_(o.registry.c.public_code.ilike(pattern),s.vehicle_asset.c.plate.ilike(pattern),s.vehicle_asset.c.vin.ilike(pattern),s.vehicle_asset.c.renavam.ilike(pattern))).limit(25)).mappings()]
        bs=[dict(x) for x in c.execute(select(s.battery_asset).where(or_(s.battery_asset.c.battery_identifier.ilike(pattern),s.battery_asset.c.manufacturer_serial_number.ilike(pattern))).limit(25)).mappings()]
        return {'models':ms,'vehicles':vs,'batteries':bs}


def observations_query(pub,uf=None,city=None,manufacturer=None,model=None,powertrain_type=None,vehicle_category=None,vehicle_type=None,manufacture_year=None):
    query=select(s.aggregate).where(s.aggregate.c.source_snapshot_id==(pub['source_snapshot_id'] if pub else ''))
    for col,v in [(s.aggregate.c.state,uf),(s.aggregate.c.municipality,city),(s.aggregate.c.manufacturer,manufacturer),(s.aggregate.c.model,model),
                  (s.aggregate.c.powertrain_type,powertrain_type)]:
        if v:query=query.where(col==v)
    if manufacture_year is not None:query=query.where(s.aggregate.c.manufacture_year==manufacture_year)
    return filter_vehicle_type(query,s.aggregate.c.vehicle_category,vehicle_type or vehicle_category)


def grouped_observations_query(pub,uf=None,city=None,manufacturer=None,model=None,powertrain_type=None,vehicle_category=None,vehicle_type=None,manufacture_year=None):
    """Combine source rows with the same model, year and geography.

    SENATRAN publishes one line per manufacture year and can publish more than
    one technical line for the same year. Keep years visible in the report,
    while combining duplicate technical lines for the same public dimensions.
    """
    raw=observations_query(pub,uf,city,manufacturer,model,powertrain_type,vehicle_category,vehicle_type,manufacture_year).subquery()
    public_type=vehicle_type_expression(raw.c.vehicle_category).label('vehicle_category')
    dimensions=[raw.c.state,raw.c.municipality,raw.c.manufacturer,raw.c.model,raw.c.trim,raw.c.manufacture_year,
                raw.c.powertrain_type,public_type]
    return select(func.min(raw.c.id).label('id'),*dimensions,
                  func.min(raw.c.reference_period).label('reference_period'),
                  func.sum(raw.c.quantity).label('quantity')).group_by(*dimensions)


@router.get('/observations')
def observations(reference:str|None=None,publication_id:str|None=None,uf:str|None=None,city:str|None=None,manufacturer:str|None=None,model:str|None=None,
                 powertrain_type:str|None=None,vehicle_type:str|None=None,vehicle_category:str|None=None,
                 manufacture_year:int|None=Query(None,ge=0,le=2100),limit:int=Query(50,ge=1,le=200),offset:int=Query(0,ge=0)):
    with db.engine.connect() as c:
        pub=publication(c,publication_id,reference);query=grouped_observations_query(pub,uf,city,manufacturer,model,powertrain_type,vehicle_category,vehicle_type,manufacture_year);sq=query.subquery()
        total,quantity=c.execute(select(func.count(),func.coalesce(func.sum(sq.c.quantity),0)).select_from(sq)).one()
        rows=[dict(r) for r in c.execute(query.order_by(query.selected_columns.quantity.desc(),query.selected_columns.id).limit(limit).offset(offset)).mappings()]
        groups=c.execute(select(s.fleet_model_group,o.group_model.c.model_id).join(o.group_model,o.group_model.c.group_id==s.fleet_model_group.c.id).where(s.fleet_model_group.c.publication_id==(pub['id'] if pub else ''))).mappings().all()
        exact={fleet.model_key(g):g['model_id'] for g in groups}
        for row in rows:
            row['model_id']=exact.get(fleet.model_key(row))
            if not row['model_id']:
                candidates={g['model_id'] for g in groups if all(fleet._clean(g[k])==fleet._clean(row[k]) for k in ('manufacturer','model','powertrain_type'))}
                if len(candidates)==1:row['model_id']=next(iter(candidates))
        serialized=serialize_publication(c,pub)
        return {'publication':serialized,'coverage':serialized.get('coverage') if serialized else None,'reference':pub['reference_period'] if pub else None,'total':total,'quantity':quantity,'total_quantity':quantity,'limit':limit,'offset':offset,'items':rows}


@router.get('/observation-filters')
def observation_filters(reference:str|None=None,publication_id:str|None=None,uf:str|None=None,
                        manufacturer:str|None=None,model:str|None=None,powertrain_type:str|None=None,
                        vehicle_type:str|None=None,vehicle_category:str|None=None):
    with db.engine.connect() as c:
        pub=publication(c,publication_id,reference)
        geography=observations_query(pub,manufacturer=manufacturer,model=model,
            powertrain_type=powertrain_type,vehicle_category=vehicle_category,vehicle_type=vehicle_type).subquery()
        state_rows=c.execute(select(geography.c.state,func.sum(geography.c.quantity))
            .where(geography.c.state.is_not(None)).group_by(geography.c.state).order_by(geography.c.state)).all()
        query=observations_query(pub,uf=uf,manufacturer=manufacturer,model=model,
            powertrain_type=powertrain_type,vehicle_category=vehicle_category,vehicle_type=vehicle_type).subquery()
        facets={key:[r[0] for r in c.execute(select(query.c[col]).where(query.c[col].is_not(None)).distinct().order_by(query.c[col]))] for key,col in
            [('manufacturers','manufacturer'),('models','model'),('powertrains','powertrain_type')]}
        type_column=vehicle_type_expression(query.c.vehicle_category).label('vehicle_type')
        facets['vehicle_types']=[r[0] for r in c.execute(select(type_column).where(query.c.vehicle_category.is_not(None)).distinct().order_by(type_column))]
        facets['categories']=facets['vehicle_types'] # v3 compatibility for existing clients
        facets['states']=[row[0] for row in state_rows]
        facets['state_quantities']={row[0]:row[1] for row in state_rows}
        # More than five thousand municipality options made the report slow and
        # difficult to use. Load that list only after the user chooses a state.
        facets['municipalities']=[r[0] for r in c.execute(select(query.c.municipality).where(query.c.municipality.is_not(None)).distinct().order_by(query.c.municipality))] if uf else []
        return facets


class JobRequest(Body):
    kind:str='discover'
    payload:dict[str,Any]={}
    idempotency_key:str=Field(min_length=1,max_length=200)


@router.get('/admin/jobs')
def jobs(user=Depends(require_admin),limit:int=Query(50,ge=1,le=200),offset:int=Query(0,ge=0)):
    from src import pipeline
    from src import pipeline_schema as ps
    with db.engine.connect() as c:
        total=c.execute(select(func.count()).select_from(ps.job)).scalar()
        rows=[dict(r) for r in c.execute(select(ps.job).order_by(ps.job.c.created_at.desc(),ps.job.c.id).limit(limit).offset(offset)).mappings()]
    for row in rows:
        progress=row.get('progress') or {};row.update(processed_count=progress.get('materialized_units',progress.get('rows')),total_count=progress.get('planned_units'),attempts=row['attempt_count'])
    return {'items':rows,'total':total,'limit':limit,'offset':offset}


@router.post('/admin/jobs',status_code=202)
def enqueue_job(body:JobRequest,user=Depends(require_admin)):
    from src import pipeline
    import os
    if body.kind=='discover' and os.getenv('I9_UPDATES_PAUSED','false').lower()=='true':
        raise HTTPException(409,'Atualizações remotas pausadas para economizar internet. O mês local continua disponível.')
    if body.kind not in ('discover','materialize','reclassify','heavy_stats_remote','heavy_stats_file'):
        raise HTTPException(422,'Tipo de execução não permitido nesta interface')
    try:
        result=pipeline.enqueue(body.kind,body.payload,body.idempotency_key)
        from src.on_demand import notify
        notify()
        return result
    except ValueError as exc:raise HTTPException(409,str(exc))


@router.get('/admin/jobs/{job_id}')
def get_job(job_id:str,user=Depends(require_admin)):
    from src import pipeline
    result=pipeline.get_job(job_id)
    if not result:raise HTTPException(404,'Execução não encontrada')
    return result


@router.post('/admin/jobs/{job_id}/retry',status_code=202)
def retry_job(job_id:str,user=Depends(require_admin)):
    from src import pipeline
    try:
        result=pipeline.retry_job(job_id)
        from src.on_demand import notify
        notify()
        return result
    except ValueError as exc:raise HTTPException(409,str(exc))


@router.get('/admin/status')
def admin_status(user=Depends(require_admin)):
    from src import pipeline
    import os
    with db.engine.connect() as c:
        settings={r['key']:r['value'] for r in c.execute(select(o.setting)).mappings()}
        active=publication(c)
        refs=set(c.execute(select(s.fleet_publication.c.reference_period).where(s.fleet_publication.c.status=='complete')).scalars())
    recent=pipeline.list_jobs(limit=1)
    today=date.today();closed=today.replace(day=1)-timedelta(days=1)
    missing=[f'2026-{m:02d}' for m in range(1,min(closed.month,12)+1) if f'2026-{m:02d}' not in refs]
    return {'database':db.engine.dialect.name,'active':active,'last_attempt':recent[0] if recent else None,'next_check':(settings.get('next_check') or {}).get('scheduled_at'),
        'worker':{'status':(settings.get('worker') or {}).get('state','unknown'),'last_seen_at':(settings.get('worker') or {}).get('checked_at')},
        'cutover':settings.get('cutover',{'status':'not_recorded'}),'missing_references':missing,
        'updates_paused':os.getenv('I9_UPDATES_PAUSED','false').lower()=='true'}


@router.get('/admin/audit')
def audits(user=Depends(require_admin),limit:int=Query(50,ge=1,le=200),offset:int=Query(0,ge=0)):
    with db.engine.connect() as c:return {'items':[dict(r) for r in c.execute(select(s.audit).order_by(s.audit.c.created_at.desc(),s.audit.c.id).limit(limit).offset(offset)).mappings()]}


@router.get('/admin/alert-policy')
def alert_policy(user=Depends(require_admin)):
    from src import pipeline_schema as ps
    with db.engine.connect() as c:
        row=c.execute(select(ps.policy).order_by(ps.policy.c.created_at.desc(),ps.policy.c.id).limit(1)).mappings().first()
        if not row:return {'national_pct':20,'model_pct':50,'model_absolute':100}
        return {'id':row['id'],'national_pct':100*row['values']['national_ratio'],'model_pct':100*row['values']['model_ratio'],'model_absolute':row['values']['model_absolute']}



class Policy(Body):
    national_pct:int=Field(ge=1,le=1000)
    model_pct:int=Field(ge=1,le=1000)
    model_absolute:int=Field(ge=1)
    reason:str=Field(min_length=3,max_length=4000)


@router.get('/admin/classifications')
def classifications(user=Depends(require_admin),limit:int=Query(50,ge=1,le=200),after:str='',offset:int=Query(0,ge=0)):
    from src import pipeline
    return pipeline.pending_reviews(limit=limit,after=after,offset=offset,paged=True)


class Classification(Body):
    raw_name:str=Field(min_length=1,max_length=300)
    decision:str
    evidence:str=Field(min_length=3,max_length=4000)
    manufacturer:str|None=None
    model:str|None=None
    powertrain_type:str|None=None
    vehicle_category:str|None=None
    vehicle_subtype:str|None=Field(None,max_length=100)
    segment:str|None=Field(None,max_length=150)
    body_type:str|None=Field(None,max_length=150)
    expected_version:int=0


@router.post('/admin/classifications')
def classify(body:Classification,user=Depends(require_admin)):
    from src import pipeline
    try:
        result=pipeline.approve_rule(raw_name=body.raw_name,decision=body.decision,evidence=body.evidence,actor=user['username'],
            manufacturer=body.manufacturer,model=body.model,powertrain=body.powertrain_type,category=body.vehicle_category,
            vehicle_subtype=body.vehicle_subtype,segment=body.segment,body_type=body.body_type,expected_version=body.expected_version)
        return result
    except ValueError as exc:raise HTTPException(409,str(exc))


@router.get('/admin/model-aliases')
def model_aliases(model_id:str|None=None,user=Depends(require_admin),limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0)):
    with db.engine.connect() as c:
        query=select(o.semantic_alias)
        if model_id:query=query.where(o.semantic_alias.c.model_id==service.resolve_model(c,model_id)['id'])
        total=c.execute(select(func.count()).select_from(query.subquery())).scalar_one()
        items=[dict(r) for r in c.execute(query.order_by(o.semantic_alias.c.created_at.desc(),o.semantic_alias.c.id).limit(limit).offset(offset)).mappings()]
        return {'items':items,'total':total,'limit':limit,'offset':offset}


@router.get('/heavy-statistics')
def heavy_statistics(reference:str|None=None,vehicle_type:str|None=None,uf:str|None=None,municipality:str|None=None,
                     limit:int=Query(50,ge=1,le=200),offset:int=Query(0,ge=0)):
    with db.engine.connect() as c:
        query=select(o.heavy_stat)
        for column,value in [(o.heavy_stat.c.reference_period,reference),(o.heavy_stat.c.vehicle_type,vehicle_type),
                             (o.heavy_stat.c.state,uf),(o.heavy_stat.c.municipality,municipality)]:
            if value:query=query.where(column==value)
        sq=query.subquery();total,quantity=c.execute(select(func.count(),func.coalesce(func.sum(sq.c.quantity),0)).select_from(sq)).one()
        items=[dict(r) for r in c.execute(query.order_by(o.heavy_stat.c.quantity.desc(),o.heavy_stat.c.id).limit(limit).offset(offset)).mappings()]
        return {'items':items,'total':total,'total_quantity':quantity,'limit':limit,'offset':offset,
                'notice':'Estatística independente por tipo/espécie/eixos; não identifica modelos ou unidades.'}


@router.post('/admin/alert-policy')
def set_policy(body:Policy,request:Request,user=Depends(require_admin)):
    from src import pipeline
    return pipeline.set_alert_policy({'national_ratio':body.national_pct/100,'model_ratio':body.model_pct/100,'model_absolute':body.model_absolute},user['username'],body.reason)


class MonthImport(Body):
    dataset: str = Field('all', pattern='^(all|primary|complement)$')
    reference: str = Field(pattern=r'^20\d{2}-(0[1-9]|1[0-2])$')
    refresh: bool = False
    idempotency_key: str = Field(min_length=1, max_length=200)


@router.get('/admin/senatran/months')
def senatran_months(user=Depends(require_admin)):
    from src.senatran_months import months
    return months()


@router.post('/admin/senatran/refresh', status_code=202)
def senatran_refresh(user=Depends(require_admin)):
    from src.senatran_months import request_refresh
    result = request_refresh()
    from src.on_demand import notify
    notify()
    return result


@router.post('/admin/senatran/import', status_code=202)
def senatran_import(body: MonthImport, user=Depends(require_admin)):
    from src.senatran_months import request_import
    result = request_import(body.reference, body.refresh, user['id'] + ':' + body.idempotency_key, dataset=body.dataset)
    from src.on_demand import notify
    notify()
    return result


class WarrantyUpdate(Body):
    revision: int = Field(ge=0)
    start_date: date | None = None
    end_date: date | None = None
    duration_months: int | None = Field(None, ge=0, le=600)
    km_limit: float | None = Field(None, ge=0, allow_inf_nan=False)
    km_basis: str | None = Field(None, pattern='^(absolute|since_start)$')
    baseline_km: float | None = Field(None, ge=0, allow_inf_nan=False)
    installation_id: str | None = None
    conditions: str | None = Field(None, max_length=10000)
    evidence: str = Field(min_length=3, max_length=4000)


@router.get('/batteries/{battery_id}/warranty')
def warranty_detail(battery_id: str):
    from src.battery_status import summary
    with db.engine.connect() as c:
        return summary(c, battery_id)


@router.put('/batteries/{battery_id}/warranty')
def warranty_update(battery_id: str, body: WarrantyUpdate, request: Request, user=Depends(require_admin)):
    from src.battery_status import save
    payload = body.model_dump(mode='json')
    return transaction(request, payload, user, lambda c: save(c, battery_id, payload, user))


@router.get('/admin/battery-alerts')
def battery_alerts(user=Depends(require_admin), limit:int=Query(50,ge=1,le=200), offset:int=Query(0,ge=0)):
    from src.battery_status import summary
    items = []
    with db.engine.connect() as c:
        for battery in c.execute(select(s.battery_asset).order_by(s.battery_asset.c.id)).mappings():
            status = summary(c, battery['id'])
            reasons = []
            if status['warranty']['term_status'] in ('expired', 'expiring'): reasons.append(status['warranty']['term_status'])
            if status['warranty']['km_status'] == 'limit_reached': reasons.append('km_limit')
            if status['health']['soh_limit_reached']: reasons.append('soh_limit')
            if status['health']['cycle_limit_reached']: reasons.append('cycle_limit')
            if reasons: items.append({'battery_id':battery['id'],'identifier':battery['battery_identifier'] or battery['manufacturer_serial_number'] or battery['id'],'reasons':reasons,**status})
    return {'items':items[offset:offset+limit],'total':len(items),'limit':limit,'offset':offset}


@router.get('/senatran/reports')
def official_reports(reference: str | None = None):
    from src.senatran_reports import reports
    return reports(reference)


@router.get('/senatran/reports/{source_id}/rows')
def official_report_rows(source_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), scope: str = Query('all', pattern='^(all|electrified|electric)$')):
    from src.senatran_reports import report_rows
    return report_rows(source_id, offset, limit, scope)
