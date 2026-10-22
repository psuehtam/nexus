import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select,insert,func,event
from src import fleet_publication as fleet, operational_schema as o,intelligence_schema as s,auth,pipeline
from test_pipeline_postgres import postgres

def source(database,key,reference,quantity):
    with database.begin() as c:
        c.execute(insert(s.source_snapshot).values(id=key,provider='SENATRAN',dataset_name='fixture',reference_period=reference,
            retrieved_at=reference+'-28',ingestion_status='success',metric='fleet_stock',access_level='public',metadata={}))
        c.execute(insert(s.aggregate).values(id='agg-'+key,source_snapshot_id=key,quantity=quantity,manufacturer='BYD',model='DOLPHIN',powertrain_type='BEV',vehicle_category='light',source_record_reference='1'))

def generate(database,quantity=2,key='jan',reference='2026-01'):
    source(database,key,reference,quantity)
    return fleet.materialize(key,17,True)

def account(client):
    auth.create_admin('operator','senha-de-teste-segura')
    response=client.post('/api/v3/auth/login',json={'username':'operator','password':'senha-de-teste-segura'})
    assert response.status_code==200
    return {'X-CSRF-Token':response.json()['csrf_token']}

def first(client):
    model=client.get('/api/v3/models').json()['items'][0]
    unit=client.get('/api/v3/models/'+model['id']+'/vehicles').json()['items'][0]
    return model,unit

def test_permanent_registry_fall_rise_and_out_of_order(client,database):
    original=generate(database,90)
    model,_=first(client)
    last=client.get('/api/v3/models/'+model['id']+'/vehicles?offset=75&limit=25').json()['items'][-1]
    for key,ref,qty in [('feb','2026-02',95),('mar','2026-03',85),('apr','2026-04',92)]:generate(database,qty,key,ref)
    result=client.get('/api/v3/models').json()
    assert result['items'][0]['quantity']==92 and result['items'][0]['units_created']==95
    assert result['items'][0]['id']==model['id']
    assert client.get('/api/v3/vehicles/'+last['public_identifier']).status_code==200
    generate(database,91,'old-revision','2026-01')
    assert client.get('/api/v3/models').json()['publication']['reference_period']=='2026-04'
    pinned=client.get('/api/v3/models?publication_id='+original['id']).json()
    assert pinned['items'][0]['quantity']==90


def test_heavy_bus_uses_same_model_vehicle_battery_flow(client,database):
    with database.begin() as c:
        c.execute(insert(s.source_snapshot).values(id='heavy-month',provider='SENATRAN',dataset_name='fixture',reference_period='2026-01',
            retrieved_at='2026-01-28',ingestion_status='success',metric='fleet_stock',access_level='public',metadata={}))
        c.execute(insert(s.aggregate).values(id='heavy-aggregate',source_snapshot_id='heavy-month',quantity=90,manufacturer='Mercedes-Benz',
            model='eO500U',powertrain_type='BEV',vehicle_category='bus',source_record_reference='heavy-1'))
    fleet.materialize('heavy-month',1000,True)
    models=client.get('/api/v3/models?is_heavy_vehicle=true&vehicle_type=bus').json()
    assert models['total']==1 and models['items'][0]['quantity']==90
    assert models['items'][0]['vehicle_type']=='bus'
    # O parâmetro antigo continua válido durante a migração dos clientes v3.
    assert client.get('/api/v3/models?vehicle_category=bus').json()['total']==1
    units=client.get('/api/v3/models/'+models['items'][0]['id']+'/vehicles?limit=100').json()
    assert units['total']==90 and len(units['items'])==90
    unit=units['items'][0]
    assert not unit.get('plate') and not unit.get('vin') and unit['battery_count']==0
    dossier=client.get('/api/v3/vehicles/'+unit['public_identifier']+'/battery-dossier').json()
    assert dossier['counts']['total']==71 and dossier['counts']['pending']==71

def test_draft_dossier_no_fake_battery_revision_and_csrf(client,database):
    generate(database);_,unit=first(client);path='/api/v3/vehicles/'+unit['id']+'/battery-dossier'
    p=client.get(path).json();assert p['counts']['total']==71 and p['counts']['pending']==71
    body={'revision':0,'schema_version':'1','values':{'12':{'value':'LFP','status':'draft','source':'informação preliminar'}}}
    assert client.put(path,json=body).status_code==401
    headers=account(client)
    assert client.put(path,json=body).status_code==403
    result=client.put(path,json=body,headers=headers)
    assert result.status_code==200,result.text
    assert result.json()['counts']['draft']==1 and result.json()['counts']['confirmed']==0
    assert client.put(path,json=body,headers=headers).status_code==409
    assert len(client.get(path).json()['history'])==1
    with database.connect() as c:assert c.execute(select(func.count()).select_from(s.battery_asset)).scalar()==0


def test_report_links_to_national_registry_and_source_totals(client,database):
    generate(database,90)
    report=client.get('/api/v3/observations').json()
    assert report['total_quantity']==90
    assert report['items'][0]['model_id']==client.get('/api/v3/models').json()['items'][0]['id']
    assert client.get('/api/v3/observations?uf=PR').json()['total_quantity']==0


def test_report_separates_manufacture_years_and_keeps_totals(client,database):
    source(database,'grouped','2026-01',60)
    with database.begin() as c:
        c.execute(s.aggregate.update().where(s.aggregate.c.id=='agg-grouped').values(
            state='PR',municipality='CURITIBA',manufacture_year=2026))
        c.execute(insert(s.aggregate).values(id='agg-grouped-older',source_snapshot_id='grouped',quantity=30,
            manufacturer='BYD',model='DOLPHIN',powertrain_type='BEV',vehicle_category='light',
            state='PR',municipality='CURITIBA',manufacture_year=2025,source_record_reference='2'))
    fleet.materialize('grouped',17,True)
    report=client.get('/api/v3/observations?uf=PR').json()
    assert report['total']==2
    assert {(r['manufacture_year'],r['quantity']) for r in report['items']}=={(2026,60),(2025,30)}
    assert all(r['vehicle_category']=='unknown' for r in report['items'])
    assert report['total_quantity']==90
    assert client.get('/api/v3/observations?uf=PR&manufacture_year=2025').json()['total_quantity']==30
    filters=client.get('/api/v3/observation-filters').json()
    assert filters['states']==['PR']
    assert filters['state_quantities']=={'PR':90}
    assert filters['vehicle_types']==['unknown']
    assert client.get('/api/v3/observations?vehicle_type=unknown').json()['total_quantity']==90


def test_legacy_allocation_does_not_duplicate_current_model(client,database):
    generate(database,2)
    from src import intelligence
    with database.begin() as c:
        c.execute(insert(s.vehicle_asset).values(id='preserved-legacy',source_snapshot_id='jan',manufacturer='BYD',model='DOLPHIN',vehicle_category='light',powertrain_type='BEV',identity_status='aggregate_derived',state='PR',municipality='Curitiba'))
    fleet.migrate_legacy_registry()
    with database.begin() as c:
        original=c.execute(select(o.registry.c.current_model_id).where(o.registry.c.vehicle_id=='preserved-legacy')).scalar_one()
        canonical=fleet.canonical(c,{'manufacturer':'BYD','model':'DOLPHIN','vehicle_category':'unknown','powertrain_type':'BEV'})
        assert canonical['id']==original
        assert c.execute(select(s.vehicle_asset.c.state).where(s.vehicle_asset.c.id=='preserved-legacy')).scalar_one() is None
    assert client.get('/api/v3/models').json()['items'][0]['units_created']==3
    results=client.get('/api/v3/search?q=DOLPHIN').json()['models']
    assert len(results)==1


def test_remote_schedule_paused_without_disabling_local_reads(client,database,monkeypatch):
    generate(database,2)
    monkeypatch.setenv('I9_UPDATES_PAUSED','true')
    assert pipeline.schedule_due()['paused'] is True
    headers=account(client)
    assert client.post('/api/v3/admin/jobs',json={'kind':'discover','payload':{},'idempotency_key':'paused-check'},headers=headers).status_code==409
    assert client.get('/api/v3/admin/status',headers=headers).json()['updates_paused'] is True
    assert client.get('/api/v3/models').json()['total']==1

def test_multiple_packs_removal_reinstall_and_late_measurement(client,database):
    generate(database);_,unit=first(client);headers=account(client);base='/api/v3/vehicles/'+unit['id']
    data={'revision':0,'vin':'9BWZZZ377VT004251','evidence':'Inspeção documental','verified':True}
    assert client.put(base,json=data,headers=headers).status_code==200
    packs=[]
    for position in ('pack-1','pack-2'):
        response=client.post(base+'/batteries',json={'serial':position,'manufacturer':'OEM','evidence':'Etiqueta conferida','position':position},headers={**headers,'Idempotency-Key':position})
        assert response.status_code==200,response.text
        packs.append(response.json())
        repeat=client.post(base+'/batteries',json={'serial':position,'manufacturer':'OEM','evidence':'Etiqueta conferida','position':position},headers={**headers,'Idempotency-Key':position})
        assert repeat.json()==response.json()
    detail=client.get(base).json();assert sum(x['is_current'] for x in detail['installations'])==2
    bid=packs[0]['battery_id'];eventpath='/api/v3/batteries/'+bid+'/events'
    for dt,soh in [('2026-02-01',90),('2026-01-01',95)]:
        assert client.post(eventpath,json={'observed_at':dt,'soh_percent':soh,'evidence':'Laudo de medição'},headers=headers).status_code==200
    assert client.get('/api/v3/batteries/'+bid).json()['battery']['soh_percent']==90
    iid=packs[0]['installation_id']
    assert client.post('/api/v3/installations/'+iid+'/remove',json={'removed_at':None,'evidence':'Remoção observada; data desconhecida'},headers=headers).status_code==200
    closed=next(x for x in client.get(base).json()['installations'] if x['id']==iid)
    assert closed['removed_at'] is None and closed['is_current'] is False
    response=client.post(base+'/batteries',json={'battery_id':bid,'evidence':'Reinstalação observada','position':'pack-1'},headers=headers)
    assert response.status_code==200,response.text

def test_legacy_writes_disabled_and_staging_hidden(client,database,monkeypatch):
    monkeypatch.delenv('I9_LEGACY_WRITES',raising=False)
    assert client.post('/api/v2/sources',json={}).status_code==410
    source(database,'new','2026-01',90)
    partial=fleet.materialize('new',17,False)
    assert partial['materialized_units']==17
    assert client.get('/api/v3/models').json()['items']==[]

def test_concurrent_generation_same_model_postgres(postgres):
    source(postgres,'month','2026-01',90);pub=fleet.prepare('month')
    barrier=threading.Barrier(2,timeout=10)
    def run():
        barrier.wait();return fleet.process(pub['id'],90)
    with ThreadPoolExecutor(2) as pool:
        a,b=pool.submit(run),pool.submit(run)
        assert a.result(30)['status']=='complete'
        assert b.result(30)['status']=='complete'
    with postgres.connect() as c:
        assert c.execute(select(func.count()).select_from(o.registry)).scalar()==90
        assert c.execute(select(func.count()).select_from(o.block)).scalar()==1

def test_generation_transaction_failure_rolls_back(postgres):
    source(postgres,'month','2026-01',90);pub=fleet.prepare('month')
    def fail(conn,cursor,statement,parameters,context,many):
        if 'INSERT INTO registry_generation_block' in statement:raise RuntimeError('power lost before checkpoint')
    event.listen(postgres,'before_cursor_execute',fail)
    try:
        with pytest.raises(RuntimeError):fleet.process(pub['id'],90)
    finally:event.remove(postgres,'before_cursor_execute',fail)
    with postgres.connect() as c:assert c.execute(select(func.count()).select_from(o.registry)).scalar()==0
    assert fleet.process(pub['id'],90)['status']=='complete'

def test_superseded_first_publication_cannot_activate(postgres):
    from src import pipeline_schema as p
    source(postgres,'stale','2026-01',2)
    with postgres.begin() as c:
        c.execute(s.source_snapshot.update().where(s.source_snapshot.c.id=='stale').values(metadata={'pipeline_generation':1}))
        c.execute(insert(p.intent).values(reference='2026-01',generation=2,job_id='new'))
    result=fleet.materialize('stale',1000,True)
    assert result['status']=='superseded'
    with postgres.connect() as c:
        assert fleet.active_publication(c) is None
        assert c.execute(select(func.count()).select_from(o.registry).where(o.registry.c.visible.is_(True))).scalar()==0
