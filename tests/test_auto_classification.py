"""Automatic coverage must never turn short non-vehicle codes into EVs."""
import pytest
from sqlalchemy import select

from src import auto_classification as auto, pipeline, pipeline_schema as p


@pytest.mark.parametrize(('name', 'powertrain'), [
    ('AUDI Q5 P HIBRIDO', 'PHEV'),
    ('BMW I3 BEV 120AH', 'BEV'),
    ('FORD FUSION HYBRID', 'HYBRID'),
    ('GWM HAVAL H6 PREM PHEV', 'PHEV'),
    ('KIA SPORTAGE MHEV LX', 'MHEV'),
    ('BYD ATTO 2 GL DM FLEX', 'PHEV'),
    ('VOLVO FM ELECTRIC 6X4T', 'BEV'),
    ('HYD NEXO FUEL CELL EV', 'FCEV'),
    ('CIVIC HYBRID', 'HEV'),
    ('RENAULT KOLEOS E TECH', 'HEV'),
])
def test_explicit_vehicle_names_are_classified(name, powertrain):
    assert auto.classify(name)['powertrain'] == powertrain


@pytest.mark.parametrize('name', [
    'JIANGSU DM 1000 W P09',
    'BUSSCAR URBPLUSS HIBRIDO',
    'LMABORGHINI HURACAN EV', 'TOYOTA COROLLA 2.0',
])
def test_ambiguous_codes_and_combustion_names_are_excluded(name):
    assert auto.classify(name) is None


@pytest.mark.parametrize('name', [
    'GUZZI CALIFORNIA EV', 'R DM REBOQUES BAU 1E',
    'IVECO ETECH RODOCAR', 'BRAMONT EVADER EV 1000',
])
def test_known_false_positive_names_are_discarded_without_review(name):
    assert auto.classify(name)['status'] == 'excluded'


def test_remaining_ambiguous_names_explain_why_they_stay_out():
    assert 'combustíveis diferentes' in auto.pending_reason('BORAM SC 20 EV')[0]
    assert 'elétrico e a gasolina' in auto.pending_reason('JIANGSU DM 500 W GY')[0]


@pytest.mark.parametrize(('name', 'category'), [
    ('FOTON IBLUE EV 6T', 'truck'),
    ('MPOLO TORINO ELETRICO U', 'bus'),
    ('VOLVO FM ELECTRIC 6X2T', 'tractor_truck'),
    ('VOLVO FM ELECTRIC 4X2R', 'truck'),
    ('TOYOTA CCROSS XRX HYBRID', 'car'),
    ('YAMAHA FLUO 125 HYBRID', 'motorcycle'),
    ('BYD SHARK', 'utility'),
    ('BYD SHARK GS DM', 'utility'),
    ('GMC HUMMER EV EDITION1', 'utility'),
    ('KIA CARNIVAL HEV EX', 'van'),
    ('DENZA D9 GS DM', 'van'),
    ('VW ID BUZZ', 'van'),
])
def test_model_family_has_vehicle_category(name, category):
    assert auto.category_for(name)[0] == category


def test_pickup_does_not_count_as_heavy_truck():
    from src.vehicle_taxonomy import is_heavy
    assert not is_heavy(auto.category_for('BYD SHARK')[0])
    assert is_heavy(auto.category_for('FOTON IBLUE EV 6T')[0])


def test_catalog_model_and_explicit_heavy_keep_their_types():
    frozen={'automatic_classifier': auto.VERSION,
            'prefixes':[('BYD DOLPHIN','BYD','Dolphin','BEV')], 'overrides':[]}
    assert pipeline._classify('BYD DOLPHIN', frozen)['category'] == 'car'
    assert pipeline._classify('FOTON IBLUE EV 6T', frozen)['category'] == 'truck'
    assert pipeline._classify('MPOLO TORINO ELETRICO U', frozen)['category'] == 'bus'
    assert pipeline._classify('BUSSCAR URBPLUSS HIBRIDO', frozen)['status'] == 'pending'
    assert pipeline._classify('RENAULT KOLEOS E TECH', frozen)['powertrain'] == 'HEV'


def test_explicit_powertrain_overrides_broad_family_but_not_admin_decision():
    frozen={'automatic_classifier':auto.VERSION,
            'prefixes':[('BYD SONG PLUS','BYD','Song Plus','PHEV')],
            'overrides':[]}
    assert pipeline._classify('BYD SONG PLUS EV', frozen)['powertrain'] == 'BEV'
    frozen['overrides']=[{'raw_name':'BYD SONG PLUS EV','decision':'exclude','evidence':'source'}]
    assert pipeline._classify('BYD SONG PLUS EV', frozen)['status'] == 'excluded'


def test_import_keeps_year_city_and_quantity_for_explicit_hybrid(database, monkeypatch, tmp_path):
    from src import db
    monkeypatch.setattr(db,'ROOT',tmp_path)
    pipeline.initialize()
    path=tmp_path/'senatran.csv'
    path.write_text('UF;MUNICIPIO;MARCA MODELO;ANO FABRICACAO;QUANTIDADE\n'
                    'SP;Sao Paulo;I/FORD FUSION HYBRID;2019;3\n'
                    'SP;Sao Paulo;I/TOYOTA COROLLA 2.0;2019;9\n',encoding='utf-8')
    pipeline.enqueue('import_file',{'path':str(path),'reference':'2026-01'},'automatic-example')
    lease=pipeline.claim('test')
    artifact=pipeline.register_artifact(path,'2026-01',lease)
    content=pipeline.parse_artifact(artifact['id'],lease)
    source_id,_=pipeline.classify_content(content,lease)
    from src import intelligence_schema as s
    with database.connect() as c:
        rows=list(c.execute(select(s.aggregate).where(s.aggregate.c.source_snapshot_id==source_id)).mappings())
        assert len(rows)==1
        assert (rows[0]['model'],rows[0]['manufacture_year'],rows[0]['municipality'],rows[0]['quantity']) == ('FUSION HYBRID',2019,'Sao Paulo',3)
        discarded=c.execute(select(p.filtered_summary).where(p.filtered_summary.c.artifact_id==artifact['id'])).mappings().one()
        assert discarded['raw_name']=='TOYOTA COROLLA 2 0'
        assert discarded['quantity']==0


def test_import_assigns_heavy_and_light_types_without_source_type_column(database, monkeypatch, tmp_path):
    from src import db, intelligence_schema as s
    monkeypatch.setattr(db,'ROOT',tmp_path)
    pipeline.initialize()
    path=tmp_path/'types.csv'
    path.write_text('UF;MUNICIPIO;MARCA MODELO;ANO FABRICACAO;QUANTIDADE\n'
                    'SP;Sao Paulo;I/FOTON IBLUE EV 6T;2024;3\n'
                    'SP;Sao Paulo;I/MPOLO TORINO ELETRICO U;2024;2\n'
                    'SP;Sao Paulo;I/TOYOTA CCROSS XRX HYBRID;2024;5\n', encoding='utf-8')
    pipeline.enqueue('import_file',{'path':str(path),'reference':'2026-01'},'type-example')
    lease=pipeline.claim('test')
    artifact=pipeline.register_artifact(path,'2026-01',lease)
    content=pipeline.parse_artifact(artifact['id'],lease)
    source_id,_=pipeline.classify_content(content,lease)
    with database.connect() as c:
        rows={row['model']:row for row in c.execute(select(s.aggregate).where(
            s.aggregate.c.source_snapshot_id==source_id)).mappings()}
    assert rows['IBLUE EV 6T']['vehicle_category']=='truck'
    assert rows['TORINO ELETRICO U']['vehicle_category']=='bus'
    assert rows['CCROSS XRX HYBRID']['vehicle_category']=='car'


def test_published_month_is_queued_once_for_automatic_reimport(database, monkeypatch):
    from src import db, intelligence_schema as s
    from sqlalchemy import insert
    monkeypatch.setenv('ALLOW_SOURCE_DOWNLOAD','true')
    pipeline.initialize()
    with database.begin() as c:
        c.execute(insert(s.source_snapshot).values(id='old-source',provider='SENATRAN',
            metric='fleet_stock',source_url='https://gov.br/senatran/marca-modelo.zip',
            reference_period='2026-09',metadata={'automatic_classifier':'legacy'}))
        c.execute(insert(s.fleet_publication).values(id='old-pub',source_snapshot_id='old-source',
            reference_period='2026-09',status='complete',finished_at='2026-10-01'))
    first=pipeline.enqueue_classifier_refresh()
    assert len(first)==1
    assert pipeline.get_job(first[0])['payload']['reference']=='2026-09'
    assert pipeline.enqueue_classifier_refresh()==[]
