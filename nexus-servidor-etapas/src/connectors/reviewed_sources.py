"""Load versioned reviewed facts that do not require live downloads."""
import json
from pathlib import Path
from sqlalchemy import select, insert
from src import db
from src.db import fuel_baseline, registrations, uf_population, ROOT

REVIEWED = Path(__file__).resolve().parents[2] / 'data' / 'reviewed'

def read_json(name):
    path = REVIEWED / name
    if not path.exists():
        path = ROOT / 'data' / 'reviewed' / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))

def seed_fuel():
    files = sorted((REVIEWED).glob('senatran-fuel-*.json')) if REVIEWED.exists() else []
    payload = None
    for path in files:
        data = json.loads(path.read_text(encoding='utf-8'))
        if payload is None or data.get('reference','') > payload.get('reference',''):
            payload = data
    if not payload:
        payload = read_json('senatran-fuel-2026-02.json')
    if not payload:
        return 0
    rows = [dict(reference=payload['reference'], uf=None, city=None, fuel_type=item['fuel_type'],
                 propulsion=item['propulsion'], quantity=item['quantity']) for item in payload['national']]
    with db.engine.begin() as c:
        if c.execute(select(fuel_baseline.c.id).where(fuel_baseline.c.reference == payload['reference'])).first():
            return 0
        c.execute(insert(fuel_baseline), rows)
    return len(rows)

def seed_fenabrave():
    payload = read_json('fenabrave-2026-H1.json')
    if not payload:
        return 0
    rows = [dict(reference=payload['reference'], provider=payload['provider'], brand=item.get('brand'),
                 model=item.get('model'), propulsion=item['propulsion'], quantity=item['quantity'],
                 notes=payload.get('notes')) for item in payload['items']]
    with db.engine.begin() as c:
        if c.execute(select(registrations.c.id).where(registrations.c.reference == payload['reference'],
                                                      registrations.c.provider == payload['provider'])).first():
            return 0
        c.execute(insert(registrations), rows)
    return len(rows)

def seed_population():
    payload = read_json('ibge-uf-population-2022.json')
    if not payload:
        return 0
    rows = [dict(uf=uf, population=pop, year=payload['year'], source=payload['source_url'])
            for uf, pop in payload['ufs'].items()]
    with db.engine.begin() as c:
        existing = set(c.execute(select(uf_population.c.uf)).scalars())
        new = [row for row in rows if row['uf'] not in existing]
        if new:
            c.execute(insert(uf_population), new)
        return len(new)

def seed_reviewed():
    result = {
        'fuel': seed_fuel(),
        'fenabrave': seed_fenabrave(),
        'population': seed_population(),
    }
    try:
        from src.connectors.inmetro import ingest_seed
        result['inmetro'] = ingest_seed().get('inserted', 0)
    except Exception:
        result['inmetro'] = 0
    return result
