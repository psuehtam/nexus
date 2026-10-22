from sqlalchemy import insert, select, func, update
from src import intelligence_schema as s
from src import operational_schema as o
from src import fleet_publication as fleet

def source(database, sid, reference, quantity):
    with database.begin() as c:
        c.execute(insert(s.source_snapshot).values(id=sid,provider='SENATRAN',dataset_name='Frota validada',reference_period=reference,retrieved_at=reference+'-28',method='download',raw_file_hash=sid,transformation_version='test',ingestion_status='success',record_count=1,access_level='public',scope='Brasil',metric='fleet_stock'))
        c.execute(insert(s.aggregate).values(id='agg-'+sid,source_snapshot_id=sid,reference_period=reference,country='BR',vehicle_category='light',manufacturer='BYD',model='DOLPHIN',powertrain_type='BEV',quantity=quantity,source_record_reference='1'))

def test_materializes_exact_units_and_public_navigation(client,database,monkeypatch):
    monkeypatch.setenv('ADMIN_API_KEY','secret');source(database,'july','2026-07',90)
    monkeypatch.setenv('I9_MACHINE_KEY_ENABLED','true')
    from src import pipeline
    pipeline.initialize()
    response=client.post('/api/v3/admin/jobs',headers={'X-Admin-Key':'secret'},json={'kind':'materialize','payload':{'source_snapshot_id':'july'},'idempotency_key':'july-generate'})
    assert response.status_code==202
    pipeline.run_once()
    assert pipeline.get_job(response.json()['id'])['status']=='complete'
    models=client.get('/api/v3/models').json();assert models['total']==1 and models['items'][0]['quantity']==90
    group=models['items'][0];page=client.get(f"/api/v3/models/{group['id']}/vehicles?limit=25&offset=75").json()
    assert page['total']==90 and len(page['items'])==15
    assert page['items'][-1]['public_identifier'].endswith('-000090')
    assert all(x['vin'] is None and x['plate'] is None and x['battery_count']==0 for x in page['items'])
    detail=client.get('/api/v3/vehicles/'+page['items'][0]['public_identifier'])
    assert detail.status_code==200 and detail.json()['battery_status']=='Bateria ainda não identificada'

def test_reprocessing_is_idempotent_and_new_month_switches_only_when_complete(client,database,monkeypatch):
    monkeypatch.setenv('ADMIN_API_KEY','secret');source(database,'june','2026-06',4);source(database,'july2','2026-07',6)
    first=fleet.materialize('june',2,True);assert fleet.materialize('june',2,True)['id']==first['id']
    staged=fleet.materialize('july2',2,False);assert staged['materialized_units']==2
    assert client.get('/api/v3/models').json()['publication']['reference_period']=='2026-06'
    finished=fleet.materialize('july2',2,True);assert finished['status']=='complete'
    result=client.get('/api/v3/models').json();assert result['publication']['reference_period']=='2026-07' and result['items'][0]['quantity']==6
    with database.connect() as c:
        assert c.execute(select(func.count()).select_from(s.vehicle_asset)).scalar()==6
        assert c.execute(select(func.count()).select_from(s.fleet_unit_membership).where(s.fleet_unit_membership.c.publication_id==finished['id'])).scalar()==6


def test_superseded_statistical_units_are_hidden_but_identified_vehicle_stays(database):
    source(database,'old-fleet','2026-01',2)
    fleet.materialize('old-fleet',10,True)
    with database.begin() as c:
        old_ids=list(c.execute(select(o.registry.c.vehicle_id).order_by(o.registry.c.sequence)).scalars())
        c.execute(update(s.vehicle_asset).where(s.vehicle_asset.c.id==old_ids[0]).values(
            identity_status='verified',vin='TESTVIN12345678901'))
    source(database,'new-fleet','2026-02',1)
    with database.begin() as c:
        c.execute(update(s.aggregate).where(s.aggregate.c.id=='agg-new-fleet').values(
            manufacturer='Foton',model='IBLUE EV 6T',vehicle_category='truck'))
    fleet.materialize('new-fleet',10,True)
    with database.connect() as c:
        visibility=dict(c.execute(select(o.registry.c.vehicle_id,o.registry.c.visible).where(
            o.registry.c.vehicle_id.in_(old_ids))).all())
    assert visibility[old_ids[0]] is True
    assert visibility[old_ids[1]] is False

def test_failed_publication_keeps_previous_active(database):
    source(database,'old','2026-05',2);old=fleet.materialize('old',10,True);source(database,'new','2026-06',3);new=fleet.prepare('new')
    with database.begin() as c:c.execute(update(s.fleet_publication).where(s.fleet_publication.c.id==new['id']).values(status='failed',error='interrupted'))
    with database.connect() as c:assert fleet.active_publication(c)['id']==old['id']


def test_later_automatic_type_updates_existing_model_but_preserves_reviewed_type(database):
    with database.begin() as c:
        first=fleet.canonical(c,{'manufacturer':'Foton','model':'IBLUE EV 6T',
            'vehicle_category':'unknown','powertrain_type':'BEV'})
        corrected=fleet.canonical(c,{'manufacturer':'Foton','model':'IBLUE EV 6T',
            'vehicle_category':'truck','powertrain_type':'BEV'})
        assert corrected['id']==first['id']
        assert corrected['vehicle_category']=='truck'
        assert corrected['is_heavy_vehicle'] is True
        c.execute(update(o.model).where(o.model.c.id==first['id']).values(
            classification_source='Fabricante confirmou outro tipo'))
        reviewed=fleet.canonical(c,{'manufacturer':'Foton','model':'IBLUE EV 6T',
            'vehicle_category':'van','powertrain_type':'BEV'})
        assert reviewed['vehicle_category']=='truck'
        assert reviewed['is_heavy_vehicle'] is True


def test_legacy_unknown_type_backfill_only_changes_automatic_models(database):
    with database.begin() as c:
        auto=fleet.canonical(c,{'manufacturer':'Toyota','model':'Camry Hybrid',
            'vehicle_category':'unknown','powertrain_type':'HEV'})
        reviewed=fleet.canonical(c,{'manufacturer':'Toyota','model':'RAV4 Hybrid',
            'vehicle_category':'unknown','powertrain_type':'HEV'})
        c.execute(update(o.model).where(o.model.c.id==reviewed['id']).values(
            classification_source='Revisão documentada'))
    fleet.backfill_unknown_automatic_types()
    with database.connect() as c:
        assert c.execute(select(o.model.c.vehicle_category).where(o.model.c.id==auto['id'])).scalar_one()=='car'
        assert c.execute(select(o.model.c.vehicle_category).where(o.model.c.id==reviewed['id'])).scalar_one()=='unknown'

def test_historical_reprocessing_does_not_expose_extra_units(database):
    source(database,'active-july','2026-07',3)
    active=fleet.materialize('active-july',10,True)
    source(database,'historical-june','2026-06',5)

    historical=fleet.materialize('historical-june',10,True)

    with database.connect() as c:
        assert fleet.active_publication(c)['id']==active['id']
        assert historical['stage']=='historical'
        assert c.execute(select(func.count()).select_from(o.registry).where(o.registry.c.visible.is_(True))).scalar()==3
        assert c.execute(select(func.count()).select_from(o.registry)).scalar()==5

def test_public_reads_but_write_requires_credentials(client,database):
    source(database,'public','2026-08',1);fleet.materialize('public',10,True)
    assert client.get('/api/v3/models').status_code==200
    assert client.post('/api/v3/admin/jobs',json={'kind':'materialize','payload':{'source_snapshot_id':'public'},'idempotency_key':'public'}).status_code==401
