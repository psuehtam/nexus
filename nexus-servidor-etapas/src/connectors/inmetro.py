"""INMETRO PBE Veicular: labeled electrified specs, never installed-pack identity."""
from pathlib import Path
from sqlalchemy import select, insert
from src import db, intelligence_schema as s
from src.intelligence import stable, VERSION, uid
from src.connectors.public import now
from src.connectors.reviewed_sources import read_json
from src.catalog import PBE
from src.matching import normalize

def _upsert_spec(c, item, sid):
    key = stable('pbe:' + '|'.join(str(item.get(k) or '') for k in ('manufacturer', 'model', 'trim', 'powertrain', 'model_year_start')))
    if c.execute(select(s.vehicle_spec.c.id).where(s.vehicle_spec.c.id == key)).first():
        return 0
    c.execute(insert(s.vehicle_spec).values(id=key, manufacturer=item['manufacturer'], model=item['model'],
        trim=item.get('trim'), powertrain=item.get('powertrain') or 'UNKNOWN', country_market='BR',
        vehicle_category='light', model_year_start=item.get('model_year_start'), model_year_end=item.get('model_year_end'),
        source_snapshot_id=sid))
    capacity = item.get('nominal_capacity_kwh')
    if capacity:
        bid = stable('pbe-battery:' + key)
        c.execute(insert(s.battery_model).values(id=bid, manufacturer=item['manufacturer'], model_name=item.get('trim'),
            chemistry=item.get('chemistry'), part_number=item.get('part_number'), nominal_capacity_kwh=capacity,
            confidence='medium', source_snapshot_id=sid,
            field_sources={'nominal_capacity_kwh': item.get('source_url') or PBE}))
        c.execute(insert(s.compatibility).values(id=stable('pbe-evidence:' + key), vehicle_spec_id=key, battery_model_id=bid,
            source_snapshot_id=sid, confidence='medium', evidence_type='technical', status='candidate',
            notes='Candidato PBE/INMETRO. Revisar versão e ano antes de aceitar como pack instalado.'))
    return 1

def ingest_seed():
    payload = read_json('inmetro-pbe-seed.json')
    if not payload:
        return {'inserted': 0}
    sid = stable('pbe-seed:' + payload['reviewed_at'])
    inserted = 0
    with db.engine.begin() as c:
        if not c.execute(select(s.source_snapshot.c.id).where(s.source_snapshot.c.id == sid)).first():
            c.execute(insert(s.source_snapshot).values(id=sid, provider='INMETRO', dataset_name='PBE Veicular (semente revisada)',
                reference_period=None, retrieved_at=now(), method='review', source_url=payload['source_url'],
                source_version=payload['reviewed_at'], transformation_version=VERSION,
                license_or_terms='Tabela pública PBE; consultar termos INMETRO', ingestion_status='partial',
                record_count=len(payload['vehicles']), scope=payload.get('notes'), metric='technical', access_level='public', metadata={}))
        for item in payload['vehicles']:
            inserted += _upsert_spec(c, item, sid)
    return {'id': sid, 'inserted': inserted}

def ingest_table(path):
    import pandas as pd
    path = Path(path)
    frame = pd.read_excel(path, dtype=str) if path.suffix.lower() in ('.xlsx', '.xls') else pd.read_csv(path, dtype=str)
    renamed = {col: {'MARCA': 'manufacturer', 'FABRICANTE': 'manufacturer', 'MODELO': 'model', 'VERSAO': 'trim',
                     'ANO': 'model_year_start', 'TECNOLOGIA': 'powertrain', 'CAPACIDADE': 'nominal_capacity_kwh',
                     'CAPACIDADE BATERIA': 'nominal_capacity_kwh', 'QUIMICA': 'chemistry'}.get(normalize(col), col)
               for col in frame.columns}
    frame = frame.rename(columns=renamed)
    sid = stable('pbe-file:' + path.name)
    rows = frame.to_dict('records')
    inserted = 0
    with db.engine.begin() as c:
        if not c.execute(select(s.source_snapshot.c.id).where(s.source_snapshot.c.id == sid)).first():
            c.execute(insert(s.source_snapshot).values(id=sid, provider='INMETRO', dataset_name='PBE Veicular (planilha)',
                reference_period=None, retrieved_at=now(), method='import', source_url=PBE, source_version=path.name,
                transformation_version=VERSION, license_or_terms='Tabela pública PBE', ingestion_status='partial',
                record_count=len(rows), scope='Importação da tabela PBE; conferir unidades e versões', metric='technical',
                access_level='public', metadata={'file': path.name}))
        for rec in rows:
            if not rec.get('manufacturer') or not rec.get('model'):
                continue
            item = {k: rec.get(k) for k in ('manufacturer', 'model', 'trim', 'powertrain', 'chemistry', 'part_number')}
            year = rec.get('model_year_start')
            if year and str(year).isdigit():
                item['model_year_start'] = item['model_year_end'] = int(year)
            cap = rec.get('nominal_capacity_kwh')
            if cap:
                try:
                    item['nominal_capacity_kwh'] = float(str(cap).replace(',', '.'))
                except ValueError:
                    pass
            item['source_url'] = PBE
            inserted += _upsert_spec(c, item, sid)
    return {'id': sid, 'inserted': inserted}
