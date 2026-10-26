"""Stream SENATRAN data, retain only reviewed BEVs, publish atomically."""
import csv
import json
import re
import zipfile
from collections import defaultdict
from pathlib import Path
from sqlalchemy import select, insert, update, func
import pandas as pd
from src.db import engine, locations, models, batteries, fleet, snapshots, runs, sources, unclassified, ROOT
from src.matching import normalize, match
from src.catalog import classify, electrified_hint, PBE
from src.connectors.public import now, get_json, IBGE

MONTHS = dict(zip(['janeiro','fevereiro','marco','abril','maio','junho','julho','agosto','setembro','outubro','novembro','dezembro'], range(1,13)))
UF_NAMES=dict(zip(['ACRE','ALAGOAS','AMAPA','AMAZONAS','BAHIA','CEARA','DISTRITO FEDERAL','ESPIRITO SANTO','GOIAS','MARANHAO','MATO GROSSO','MATO GROSSO DO SUL','MINAS GERAIS','PARA','PARAIBA','PARANA','PERNAMBUCO','PIAUI','RIO DE JANEIRO','RIO GRANDE DO NORTE','RIO GRANDE DO SUL','RONDONIA','RORAIMA','SANTA CATARINA','SAO PAULO','SERGIPE','TOCANTINS'],['AC','AL','AP','AM','BA','CE','DF','ES','GO','MA','MT','MS','MG','PA','PB','PR','PE','PI','RJ','RN','RS','RO','RR','SC','SP','SE','TO']))

def parse_quantity(value):
    value=str(value).strip()
    if not re.fullmatch(r'\d+(?:\.0+)?',value):raise ValueError(f'Quantidade inválida: {value!r}')
    return int(value.split('.')[0])

def reference_of(resource):
    text = normalize(resource.get('url', '')).lower()
    for name, month in MONTHS.items():
        m = re.search(r'\b' + name + r' (20\d{2})\b', text)
        if m: return f'{m[1]}-{month:02}'
    return None

def sync_locations():
    payload = get_json(IBGE)
    if not isinstance(payload, list) or len(payload) < 5000:
        raise ValueError('Cadastro IBGE incompleto')
    raw = ROOT / 'data/raw/ibge-municipios.json'
    raw.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    rows = [dict(id=str(x['municipio-id']), uf=x['UF-sigla'], city=x['municipio-nome'], normalized=normalize(x['municipio-nome'])) for x in payload]
    rows.append(dict(id='unknown',uf=None,city='Não informado',normalized='SEM INFORMACAO'))
    with engine.begin() as c:
        existing = set(c.execute(select(locations.c.id)).scalars())
        new = [r for r in rows if r['id'] not in existing]
        if new: c.execute(insert(locations), new)
    return len(payload)

def iter_frames(path):
    """No ZIP extraction and no full uncompressed dataset in memory."""
    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as z:
            members = [m for m in z.infolist() if m.filename.lower().endswith(('.csv', '.txt')) and not m.is_dir()]
            if not members: raise ValueError('ZIP sem CSV/TXT; formato requer conector específico')
            if sum(m.file_size for m in members) > 8_000_000_000: raise ValueError('ZIP excede limite descompactado de 8 GB')
            for m in members:
                with z.open(m) as stream:
                    yield from csv_frames(stream)
    else:
        with path.open('rb') as stream: yield from csv_frames(stream)

def csv_frames(stream):
    sample = stream.read(65536)
    stream.seek(0)
    try: sample.decode('utf-8-sig'); encoding='utf-8-sig'
    except UnicodeDecodeError: encoding='latin-1'
    delimiter = csv.Sniffer().sniff(sample.decode(encoding), delimiters=';,\t|').delimiter
    yield from pd.read_csv(stream, sep=delimiter, encoding=encoding, dtype=str, chunksize=100000, keep_default_na=False)

def columns(frame):
    aliases = {'UF':'uf', 'MUNICIPIO':'city', 'MUNICIPIO NOME':'city', 'MARCA MODELO':'raw_name',
               'MARCA MODELO VEICULO':'raw_name', 'ANO FABRICACAO':'vehicle_year', 'ANO DE FABRICACAO':'vehicle_year',
               'ANO FABRICACAO CRV':'vehicle_year', 'QUANTIDADE':'quantity', 'QTD VEICULOS':'quantity',
               'ANO FABRICACAO VEICULO CRV':'vehicle_year',
               'QTD':'quantity', 'QTDE':'quantity', 'TOTAL':'quantity'}
    renamed = {col: aliases.get(normalize(col), col) for col in frame.columns}
    frame = frame.rename(columns=renamed)
    missing = {'uf','city','raw_name','vehicle_year','quantity'} - set(frame.columns)
    if missing: raise ValueError(f'Colunas obrigatórias ausentes: {sorted(missing)}. Recebidas: {list(frame.columns)}')
    return frame

def process_file(path, reference, sha256, run_id):
    with engine.connect() as c:
        if c.execute(select(snapshots).where(snapshots.c.reference==reference)).first():
            raise ValueError('Referência já publicada; snapshot imutável')
        loc = {(r.uf, r.normalized): r.id for r in c.execute(select(locations))}
        by_id={r.id:r.uf for r in c.execute(select(locations))}
        cat = [dict(r) for r in c.execute(select(models)).mappings()]
        battery = {r.model_id:r.id for r in c.execute(select(batteries))}
    if not loc: raise ValueError('Cadastro IBGE não carregado')
    for uf,name,code in json.loads((Path(__file__).parent/'locality_aliases.json').read_text(encoding='utf-8'))['aliases']:
        if by_id.get(code)==uf:loc[(uf,name)]=code
    aggregated = defaultdict(int)
    identities = {}
    skipped = defaultdict(int)
    raw_count = excluded = 0
    seen = set()
    max_year = int(reference[:4]) + 1
    for frame in iter_frames(Path(path)):
        frame = columns(frame)
        raw_count += len(frame)
        # Classify unique names once per chunk, not each of millions of rows.
        classes = {name:classify(name) for name in frame['raw_name'].unique()}
        chosen = frame[frame['raw_name'].map(lambda x: classes[x] is not None)]
        leftover = frame[frame['raw_name'].map(lambda x: classes[x] is None)]
        excluded += len(leftover)
        for row in leftover[['raw_name','quantity']].itertuples(index=False, name=None):
            try:
                skipped[normalize(row[0])] += parse_quantity(row[1])
            except ValueError:
                continue
        # Full source-row identity: identical technical duplicates are ignored;
        # distinct model-year groups may legitimately aggregate by manufacture year.
        keep = []
        for row in chosen.itertuples(index=False, name=None):
            if row not in seen:
                seen.add(row);keep.append(True)
            else:keep.append(False)
        chosen=chosen.loc[keep]
        for row in chosen[['uf','city','raw_name','vehicle_year','quantity']].itertuples(index=False, name=None):
            uf, city, name, year, qty = row
            year=year.strip()
            if normalize(year) in ('SEM INFORMACAO','NAO INFORMADO',''):
                year=None
            elif re.fullmatch(r'\d{4}',year) and 1990 <= int(year) <= max_year:
                year=int(year)
            else:raise ValueError(f'Ano inválido em registro eletrificado: {year!r}')
            quantity=parse_quantity(qty)
            uf=UF_NAMES.get(normalize(uf),normalize(uf))
            if uf=='SEM INFORMACAO' and normalize(city)=='SEM INFORMACAO':
                locality='unknown' if 'unknown' in by_id else None
            else:locality = loc.get((uf, normalize(city)))
            if not locality: raise ValueError(f'Município/UF inválido: {city}/{uf}')
            key = (locality, normalize(name), year)
            aggregated[key] += quantity
            identities[key] = classes[name]
        with engine.begin() as c:
            c.execute(update(runs).where(runs.c.id==run_id).values(raw_rows=raw_count, bev_rows=len(aggregated), stage='Tratando e validando registros'))
    if not aggregated: raise ValueError('Nenhum veículo eletrificado reconhecido. Publicação bloqueada para evitar snapshot vazio enganoso.')
    rows = []
    match_cache={}
    for (location, name, year), qty in aggregated.items():
        brand, model, version, propulsion = identities[(location,name,year)]
        if (name,year) not in match_cache:match_cache[(name,year)]=match(name,year,cat)
        result = match_cache[(name,year)]
        b = battery.get(result['model']['id']) if result['model'] else None
        rows.append(dict(reference=reference,location_id=location,raw_name=name,brand=brand,model=model,version=version,
                         vehicle_year=year,quantity=qty,propulsion=propulsion,battery_id=b,confidence=result['confidence'] if b else result['confidence'] if not result['model'] else 'D',
                         match_method=result['method'],source_id='senatran'))
    pending=[{'reference':reference,'raw_name':name,'vehicles':qty,'electrified_hint':int(electrified_hint(name))}
             for name,qty in sorted(skipped.items(), key=lambda x:-x[1])]
    scope = 'Recorte de famílias eletrificadas revisadas (BEV, PHEV, HEV e equivalentes). Modelos fora do catálogo ficam na fila de revisão; o total oficial por combustível é a régua de cobertura.'
    with engine.begin() as c:
        c.execute(insert(snapshots).values(reference=reference, run_id=run_id, source_id='senatran', sha256=sha256, published_at=now(), scope=scope))
        for i in range(0,len(rows),2000): c.execute(insert(fleet), rows[i:i+2000])
        for i in range(0,len(pending),2000):
            if pending: c.execute(insert(unclassified), pending[i:i+2000])
        latest=c.execute(select(func.max(snapshots.c.reference))).scalar()
        c.execute(update(sources).where(sources.c.id=='senatran').values(collected_at=now(),reference=latest))
        c.execute(update(runs).where(runs.c.id==run_id).values(status='SUCESSO',stage='Publicado',finished_at=now(),raw_rows=raw_count,bev_rows=len(rows),matched_rows=sum(r['battery_id'] is not None for r in rows),details={'excluded_unclassified_or_non_electrified_rows':excluded,'unclassified_groups':len(pending),'scope':scope,'classification_source':PBE,'sha256':sha256}))
    return {'reference':reference,'rows':len(rows),'vehicles':sum(r['quantity'] for r in rows),'scope':scope}
