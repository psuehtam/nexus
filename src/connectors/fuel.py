"""SENATRAN fuel-type fleet as an official coverage baseline."""
from collections import defaultdict
from pathlib import Path
from sqlalchemy import select, insert, delete
from src import db
from src.db import fuel_baseline
from src.matching import normalize
from src.transform.ingest import parse_quantity, UF_NAMES, csv_frames, iter_frames

FUEL_PROPULSION = {
    'ELETRICO FONTE EXTERNA': 'BEV',
    'ELETRICO': 'BEV',
    'HIBRIDO PLUG IN': 'PHEV',
    'HIBRIDO PLUGIN': 'PHEV',
    'GASOLINA ELETRICO': 'HEV',
    'ELETRICO FONTE INTERNA': 'HEV',
    'HIBRIDO': 'HEV',
    'GASOLINA ALCOOL ELETRICO': 'HEV_FLEX',
    'DIESEL ELETRICO': 'HEV',
    'ETANOL ELETRICO': 'HEV',
    'HIBRIDO GAS NATURAL VEICULAR': 'HEV',
    'CELULA COMBUSTIVEL': 'FCEV',
}

def map_propulsion(fuel):
    key = normalize(fuel)
    return FUEL_PROPULSION.get(key)

def ingest_fuel_file(path, reference):
    path = Path(path)
    aggregated = defaultdict(int)
    labels = {}
    frames = csv_frames(path.open('rb')) if path.suffix.lower() in ('.csv', '.txt') else iter_frames(path)
    if path.suffix.lower() in ('.xlsx', '.xls'):
        import pandas as pd
        frames = [pd.read_excel(path, dtype=str)]
    for frame in frames:
        renamed = {col: {'UF': 'uf', 'MUNICIPIO': 'city', 'MUNICIPIO NOME': 'city', 'COMBUSTIVEL': 'fuel',
                         'COMBUSTIVEL VEICULO': 'fuel', 'QUANTIDADE': 'quantity', 'QTD': 'quantity',
                         'QTD VEICULOS': 'quantity', 'TOTAL': 'quantity'}.get(normalize(col), col)
                   for col in frame.columns}
        frame = frame.rename(columns=renamed)
        missing = {'fuel', 'quantity'} - set(frame.columns)
        if missing:
            raise ValueError(f'Planilha de combustível sem colunas {sorted(missing)}')
        for rec in frame.to_dict('records'):
            propulsion = map_propulsion(rec['fuel'])
            if not propulsion:
                continue
            uf = rec.get('uf')
            city = rec.get('city')
            uf = UF_NAMES.get(normalize(uf), normalize(uf)) if uf not in (None, '') else None
            city = None if city in (None, '') else str(city)
            key = (uf, city, propulsion, normalize(rec['fuel']))
            aggregated[key] += parse_quantity(rec['quantity'])
            labels[key] = rec['fuel']
    if not aggregated:
        raise ValueError('Nenhum combustível eletrificado encontrado na planilha')
    rows = [dict(reference=reference, uf=uf, city=city, fuel_type=labels[key], propulsion=propulsion, quantity=qty)
            for key, qty in aggregated.items() for uf, city, propulsion, _ in [key]]
    with db.engine.begin() as c:
        c.execute(delete(fuel_baseline).where(fuel_baseline.c.reference == reference))
        for i in range(0, len(rows), 2000):
            c.execute(insert(fuel_baseline), rows[i:i + 2000])
    return {'reference': reference, 'rows': len(rows), 'vehicles': sum(r['quantity'] for r in rows)}

def coverage(catalog_by_propulsion, reference=None):
    from sqlalchemy import func, select as sel
    with db.engine.connect() as c:
        q = sel(fuel_baseline.c.propulsion, func.sum(fuel_baseline.c.quantity))
        if reference:
            q = q.where(fuel_baseline.c.reference == reference)
        q = q.where(fuel_baseline.c.uf.is_(None)).group_by(fuel_baseline.c.propulsion)
        official = {prop: int(qty) for prop, qty in c.execute(q)}
        if not official:
            q = sel(fuel_baseline.c.propulsion, func.sum(fuel_baseline.c.quantity)).group_by(fuel_baseline.c.propulsion)
            if reference:
                q = q.where(fuel_baseline.c.reference == reference)
            official = {prop: int(qty) for prop, qty in c.execute(q)}
            latest = c.execute(sel(func.max(fuel_baseline.c.reference))).scalar()
        else:
            latest = reference or c.execute(sel(func.max(fuel_baseline.c.reference))).scalar()
    items = []
    keys = sorted(set(official) | set(catalog_by_propulsion))
    for prop in keys:
        catalog = catalog_by_propulsion.get(prop, 0)
        base = official.get(prop, 0)
        items.append({'propulsion': prop, 'catalog_vehicles': catalog, 'official_vehicles': base,
                      'gap': None if not base else base - catalog,
                      'coverage_pct': round(catalog / base * 100, 1) if base else None})
    official_total = sum(official.values())
    catalog_total = sum(catalog_by_propulsion.values())
    return {'reference': latest, 'items': items, 'official_total': official_total, 'catalog_total': catalog_total,
            'coverage_pct': round(catalog_total / official_total * 100, 1) if official_total else None,
            'note': 'Régua SENATRAN por combustível (estoque oficial), não o acumulado ABVE. Sem marca/modelo; a diferença indica famílias ainda fora do catálogo, não erro da SENATRAN. Não somar com o universo ABVE.'}
