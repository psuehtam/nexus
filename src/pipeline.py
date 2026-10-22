"""Persistent SENATRAN pipeline: bounded input, semantic identity and fenced work.

There are no in-process ownership locks. PostgreSQL row locks, uniqueness and
lease generation protect every committed block. SQLite is supported for unit
tests; deployment and multi-worker execution use PostgreSQL.
"""
from __future__ import annotations
from contextlib import nullcontext

import csv
import hashlib
import io
import json
import os
import re
import shutil
import socket
import threading
import time
import unicodedata
import uuid
import zipfile
from html.parser import HTMLParser
from urllib.parse import urljoin
from contextlib import contextmanager
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select, insert, update, func, and_, or_, literal
from sqlalchemy.exc import IntegrityError, OperationalError
from src import db, pipeline_schema as p, intelligence_schema as s, operational_schema as registry_schema
from src import catalog, auto_classification
from src.vehicle_taxonomy import CANONICAL_CATEGORIES,ELECTRIFIED_POWERTRAINS,is_heavy
from src.matching import normalize

PROVIDER = 'SENATRAN'
DATASET = 'municipio-marca-modelo-ano-fabricacao'
PARSER_VERSION = 'senatran-filtered-2'
NORMALIZATION_VERSION = 'typed-canonical-1'
TRANSFORMATION_VERSION = 'model-observation-1'
BLOCK_SIZE = 1000
LEASE_SECONDS = 120
RETRY_MINUTES = (5, 30, 120)
KINDS = {'catalog_refresh', 'discover', 'import_file', 'import_remote', 'heavy_stats_file', 'heavy_stats_remote', 'official_report_remote', 'reclassify', 'materialize'}
DEFAULT_POLICY = {'national_ratio': .20, 'model_ratio': .50, 'model_absolute': 100}


class LeaseLost(RuntimeError):
    pass


class PermanentError(ValueError):
    pass


class Conflict(ValueError):
    pass


def timestamp(value=None):
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec='microseconds')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def stable(value):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'i9-pipeline:' + value))


def _upsert_nothing(c, table, values):
    # Only the explicitly named primary key is allowed to be idempotent. A
    # different unique constraint conflict must be surfaced to the caller.
    if c.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    return c.execute(dialect_insert(table).values(**values).on_conflict_do_nothing(index_elements=['id']).returning(table.c.id)).scalar_one_or_none() is not None


def initialize():
    db.meta.create_all(db.engine, tables=[t for t in db.meta.sorted_tables if t.name.startswith('pipeline_')])
    with db.engine.begin() as c:
        _upsert_nothing(c, p.epoch, {'id': 'operational', 'value': str(uuid.uuid4()), 'updated_at': timestamp()})
        _upsert_nothing(c, p.policy, {'id': 'initial', 'created_at': timestamp(), 'actor': 'system',
                                   'reason': 'Limites iniciais aprovados no plano', 'values': DEFAULT_POLICY})


def _epoch(c):
    value = c.execute(select(p.epoch.c.value).where(p.epoch.c.id == 'operational')).scalar_one_or_none()
    if not value:
        raise RuntimeError('Pipeline não inicializado; execute a migração antes do trabalhador')
    return value


def desired_generation(c, reference):
    return c.execute(select(p.intent.c.generation).where(p.intent.c.reference == reference)).scalar_one_or_none()


def enqueue(kind, payload, idempotency_key, connection=None):
    if kind not in KINDS or not isinstance(payload, dict) or not idempotency_key:
        raise ValueError('Tipo, payload ou chave de idempotência inválido')
    reference = payload.get('reference')
    if kind in {'import_file', 'import_remote', 'heavy_stats_file', 'heavy_stats_remote', 'official_report_remote', 'reclassify'}:
        validate_reference(reference)
    request_hash = digest({'kind': kind, 'payload': payload})
    jid = stable('job:' + idempotency_key)
    stamp = timestamp()
    with (nullcontext(connection) if connection is not None else db.engine.begin()) as c:
        result = _upsert_nothing(c, p.job, dict(id=jid, kind=kind, payload=payload,
            idempotency_key=idempotency_key, request_hash=request_hash, status='queued', stage='queued',
            created_at=stamp, updated_at=stamp, available_at=stamp, token=0, attempt_count=0,
            progress={}, block_size=BLOCK_SIZE))
        existing = dict(c.execute(select(p.job).where(p.job.c.id == jid).with_for_update()).mappings().one())
        if existing['request_hash'] != request_hash:
            raise Conflict('A chave de idempotência já foi usada com outro conteúdo')
        if result and reference and kind in {'import_file','import_remote','reclassify'}:
            if c.dialect.name == 'postgresql':
                from sqlalchemy.dialects.postgresql import insert as dialect_insert
            else:
                from sqlalchemy.dialects.sqlite import insert as dialect_insert
            c.execute(dialect_insert(p.intent).values(reference=reference, generation=0, job_id=jid)
                      .on_conflict_do_nothing(index_elements=['reference']))
            generation = c.execute(select(p.intent.c.generation).where(p.intent.c.reference == reference)
                                   .with_for_update()).scalar_one() + 1
            c.execute(update(p.intent).where(p.intent.c.reference == reference).values(generation=generation, job_id=jid))
            c.execute(update(p.job).where(p.job.c.id == jid).values(generation=generation))
            existing['generation'] = generation
        return existing


def get_job(job_id):
    with db.engine.connect() as c:
        row = c.execute(select(p.job).where(p.job.c.id == job_id)).mappings().first()
        if not row:
            raise KeyError('Execução não encontrada')
        result = dict(row)
        result['attempts'] = [dict(x) for x in c.execute(select(p.attempt).where(p.attempt.c.job_id == job_id)
                                                      .order_by(p.attempt.c.number)).mappings()]
        result['blocks'] = c.execute(select(func.count()).select_from(p.block).where(p.block.c.job_id == job_id)).scalar_one()
        return result


def list_jobs(limit=50):
    with db.engine.connect() as c:
        return [dict(x) for x in c.execute(select(p.job).order_by(p.job.c.created_at.desc(), p.job.c.id)
                                         .limit(max(1, min(200, int(limit))))).mappings()]


def retry_job(job_id):
    job = get_job(job_id)
    if job['kind'] == 'catalog_refresh':
        if job['status'] not in ('failed', 'paused'):
            raise Conflict('Somente execuções falhas ou pausadas podem ser retomadas')
        from src.senatran_months import request_refresh
        return request_refresh()
    with db.engine.begin() as c:
        row = c.execute(select(p.job).where(p.job.c.id == job_id).with_for_update()).mappings().first()
        if not row:
            raise KeyError('Execução não encontrada')
        if row['status'] not in ('failed','paused'):
            raise Conflict('Somente execuções falhas ou pausadas podem ser retomadas')
        c.execute(update(p.job).where(p.job.c.id == job_id).values(status='queued', available_at=timestamp(),
                  updated_at=timestamp(), owner=None, lease_until=None, error=None))
    return get_job(job_id)


def claim(owner=None, at=None, job_id=None):
    """Reserve the next ready job, optionally restricting the claim to one ID.

    The targeted form is used by the one-month CLI so it cannot accidentally
    process another queued competence while the regular worker is stopped.
    """
    owner = owner or f'{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}'
    current = at or datetime.now(timezone.utc)
    stamp = timestamp(current)
    with db.engine.begin() as c:
        operational_epoch = _epoch(c)
        eligible = or_(and_(p.job.c.status == 'queued', p.job.c.available_at <= stamp),
                       and_(p.job.c.status == 'running', or_(p.job.c.lease_until <= stamp,
                            p.job.c.epoch != operational_epoch)))
        # Serialize heavy remote imports across CLI and worker processes.
        from src import operational_schema as operational
        from src.fleet_publication import insert_once
        insert_once(c, operational.setting, dict(key='remote_import_claim', value={}, updated_at=stamp), ['key'])
        c.execute(select(operational.setting.c.key).where(operational.setting.c.key == 'remote_import_claim').with_for_update()).first()
        remote_kinds = ('import_remote', 'heavy_stats_remote', 'official_report_remote')
        running_remote = c.execute(select(p.job.c.id).where(p.job.c.kind.in_(remote_kinds), p.job.c.status == 'running',
            p.job.c.lease_until > stamp, p.job.c.epoch == operational_epoch).limit(1)).scalar()
        if running_remote:
            eligible = and_(eligible, p.job.c.kind.not_in(remote_kinds))
        where = and_(eligible, p.job.c.id == job_id) if job_id else eligible
        row = c.execute(select(p.job).where(where).order_by(p.job.c.available_at, p.job.c.created_at, p.job.c.id)
                        .limit(1).with_for_update(skip_locked=True)).mappings().first()
        if not row:
            return None
        result = c.execute(update(p.job).where(p.job.c.id == row['id'], p.job.c.token == row['token'], eligible)
            .values(status='running', owner=owner, token=row['token'] + 1, epoch=operational_epoch,
                    lease_until=timestamp(current + timedelta(seconds=LEASE_SECONDS)), updated_at=stamp,
                    attempt_count=row['attempt_count'] + 1, error=None))
        if result.rowcount != 1:
            return None
        if row['status'] == 'running':
            c.execute(update(p.attempt).where(p.attempt.c.job_id == row['id'], p.attempt.c.status == 'running')
                      .values(status='expired', finished_at=stamp, error='Reserva expirada ou época operacional alterada'))
        c.execute(insert(p.attempt).values(id=str(uuid.uuid4()), job_id=row['id'], number=row['attempt_count'] + 1,
            owner=owner, token=row['token'] + 1, epoch=operational_epoch, started_at=stamp, status='running'))
        return {'job_id': row['id'], 'owner': owner, 'token': row['token'] + 1, 'epoch': operational_epoch}


def lock_lease(c, lease, at=None):
    row = c.execute(select(p.job).where(p.job.c.id == lease['job_id']).with_for_update()).mappings().first()
    if (not row or row['status'] != 'running' or row['owner'] != lease['owner'] or
        row['token'] != lease['token'] or row['epoch'] != lease['epoch'] or
        _epoch(c) != lease['epoch'] or not row['lease_until'] or row['lease_until'] <= timestamp(at)):
        raise LeaseLost('Trabalhador perdeu a reserva; a transação foi cancelada')
    return dict(row)


def heartbeat(lease):
    with db.engine.begin() as c:
        lock_lease(c, lease)
        c.execute(update(p.job).where(p.job.c.id == lease['job_id']).values(
            lease_until=timestamp(datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)), updated_at=timestamp()))


@contextmanager
def job_transaction(lease, stage=None):
    with db.engine.begin() as c:
        lock_lease(c, lease)
        if stage:
            c.execute(update(p.job).where(p.job.c.id == lease['job_id']).values(stage=stage, updated_at=timestamp()))
        yield c


def _block_result(c, lease, stage, index):
    return c.execute(select(p.block.c.result).where(p.block.c.job_id == lease['job_id'],
                     p.block.c.stage == stage, p.block.c.block_index == index)).scalar_one_or_none()


def _commit_block(c, lease, stage, index, result):
    c.execute(insert(p.block).values(id=stable(f'{lease["job_id"]}:{stage}:{index}'), job_id=lease['job_id'],
             stage=stage, block_index=index, result=result, finished_at=timestamp()))
    c.execute(update(p.job).where(p.job.c.id == lease['job_id']).values(progress={'stage': stage, **result}, updated_at=timestamp()))


def finish(lease, result):
    with job_transaction(lease) as c:
        c.execute(update(p.job).where(p.job.c.id == lease['job_id']).values(status='complete', stage='complete',
            result=result, owner=None, lease_until=None, error=None, updated_at=timestamp()))
        c.execute(update(p.attempt).where(p.attempt.c.job_id == lease['job_id'], p.attempt.c.token == lease['token'])
                  .values(status='complete', finished_at=timestamp()))


def fail(lease, error):
    with job_transaction(lease) as c:
        row = lock_lease(c, lease)
        retry = isinstance(error, (httpx.TransportError, OperationalError, OSError)) or (
            isinstance(error, httpx.HTTPStatusError) and (error.response.status_code >= 500 or error.response.status_code == 429))
        attempt_number = row['attempt_count']
        delay = RETRY_MINUTES[attempt_number - 1] if retry and attempt_number <= len(RETRY_MINUTES) else None
        message = str(error)[:2000]
        if row['kind'] == 'catalog_refresh':
            from src.senatran_months import catalog_error_message
            delay = None  # Catalog checks are retried explicitly, not on a timer.
            message = catalog_error_message(error)[:2000]
        c.execute(update(p.job).where(p.job.c.id == lease['job_id']).values(status='queued' if delay else 'failed',
            available_at=timestamp(datetime.now(timezone.utc) + timedelta(minutes=delay or 0)),
            owner=None, lease_until=None, error=message, stage='failed' if row['kind'] == 'catalog_refresh' else row['stage'], updated_at=timestamp()))
        c.execute(update(p.attempt).where(p.attempt.c.job_id == lease['job_id'], p.attempt.c.token == lease['token'])
                  .values(status='failed', error=message, finished_at=timestamp()))


def reset_epoch():
    """Call only during restore maintenance, with every previous writer stopped."""
    with db.engine.begin() as c:
        row = c.execute(select(p.epoch).where(p.epoch.c.id == 'operational').with_for_update()).mappings().one()
        value = str(uuid.uuid4())
        c.execute(update(p.epoch).where(p.epoch.c.id == 'operational').values(value=value, updated_at=timestamp()))
        c.execute(update(p.job).where(p.job.c.status == 'running').values(lease_until=timestamp()))
        return {'previous': row['value'], 'current': value}


def validate_reference(reference):
    if not isinstance(reference, str) or not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])', reference):
        raise PermanentError('Competência inválida; use AAAA-MM')
    if reference < '2026-01':
        raise PermanentError('A importação operacional começa em janeiro de 2026')


HEADER_ALIASES = {
    'UF': 'state', 'MUNICIPIO': 'city', 'MUNICIPIO NOME': 'city',
    'MARCA MODELO': 'raw_name', 'MARCA MODELO VEICULO': 'raw_name',
    'ANO FABRICACAO': 'manufacture_year', 'ANO DE FABRICACAO': 'manufacture_year',
    'ANO FABRICACAO CRV': 'manufacture_year', 'ANO FABRICACAO VEICULO CRV': 'manufacture_year',
    'QUANTIDADE': 'quantity', 'QTD VEICULOS': 'quantity', 'QTD': 'quantity', 'QTDE': 'quantity', 'TOTAL': 'quantity',
    'TIPO VEICULO': 'category', 'TIPO DE VEICULO': 'category', 'CATEGORIA': 'category',
}
CATEGORIES = {'AUTOMOVEL': 'car', 'CAMINHONETE': 'utility', 'CAMIONETA': 'utility',
              'MOTOCICLETA': 'motorcycle', 'MOTONETA': 'motorcycle', 'CICLOMOTOR': 'motorcycle',
              'ONIBUS': 'bus', 'MICRO ONIBUS': 'microbus', 'CAMINHAO': 'truck', 'CAMINHAO TRATOR': 'tractor_truck'}


def _normal_text(value):
    return unicodedata.normalize('NFC', value.strip()) if isinstance(value, str) else value


def _normalize_row(row, header, reference):
    values = {header[k]: _normal_text(v) for k, v in row.items()}
    raw_quantity = values['quantity']
    if not isinstance(raw_quantity, str) or not re.fullmatch(r'\d+(?:\.0+)?', raw_quantity):
        raise PermanentError(f'Quantidade inválida: {raw_quantity!r}')
    quantity = int(raw_quantity.split('.')[0])
    if quantity > 9_000_000_000_000_000_000:
        raise PermanentError('Quantidade excede o limite de armazenamento')
    # The typed canonical row keeps every source dimension. Missing CSV cells
    # (None), blank strings and numeric zero remain distinct.
    values['quantity'] = quantity
    raw_name = normalize(values['raw_name'] or '')
    if not raw_name:
        raise PermanentError('Linha sem marca/modelo')
    year_text = values['manufacture_year']
    if normalize(year_text or '') in {'', '0', '0000', 'SEM INFORMACAO', 'NAO INFORMADO', 'NAO INFORMADA'}:
        year = None
    elif isinstance(year_text, str) and re.fullmatch(r'\d{4}', year_text) and 1800 <= int(year_text) <= int(reference[:4]) + 1:
        year = int(year_text)
    else:
        raise PermanentError(f'Ano de fabricação inválido: {year_text!r}')
    from src.transform.ingest import UF_NAMES
    state = UF_NAMES.get(normalize(values['state'] or ''), normalize(values['state'] or '')) or None
    if state and state not in UF_NAMES.values() and state not in {'SEM INFORMACAO', 'NAO INFORMADO'}:
        raise PermanentError(f'UF inválida: {values["state"]!r}')
    if state in {'SEM INFORMACAO', 'NAO INFORMADO'}:
        state = None
    encoded = canonical({'version': NORMALIZATION_VERSION, 'dimensions': values})
    return {'canonical': encoded, 'row_sha256': hashlib.sha256(encoded.encode()).hexdigest(),
            'state': state, 'city': values['city'], 'raw_name': raw_name, 'manufacture_year': year,
            'category': CATEGORIES.get(normalize(values.get('category') or '')), 'quantity': quantity}


def _csv_rows(binary, member, reference):
    sample = binary.read(65536)
    binary.seek(0)
    try:
        sample.decode('utf-8-sig')
        encoding = 'utf-8-sig'
    except UnicodeDecodeError:
        encoding = 'latin-1'
    try:
        delimiter = csv.Sniffer().sniff(sample.decode(encoding), delimiters=';,\t|').delimiter
    except csv.Error as exc:
        raise PermanentError('Separador CSV não reconhecido') from exc
    text = io.TextIOWrapper(binary, encoding=encoding, newline='')
    try:
        reader = csv.DictReader(text, delimiter=delimiter)
        if not reader.fieldnames:
            raise PermanentError('Arquivo sem cabeçalho')
        header = {key: HEADER_ALIASES.get(normalize(key), 'extra:' + normalize(key)) for key in reader.fieldnames}
        if len(set(header.values())) != len(reader.fieldnames):
            raise PermanentError('Colunas duplicadas ou ambíguas no cabeçalho')
        missing = {'state', 'city', 'raw_name', 'manufacture_year', 'quantity'} - set(header.values())
        if missing:
            raise PermanentError(f'Colunas obrigatórias ausentes: {sorted(missing)}')
        for position, row in enumerate(reader, 2):
            if None in row:
                raise PermanentError(f'Colunas excedentes em {member}, linha {position}')
            yield {**_normalize_row(row, header, reference), 'member': member, 'line_number': position}
    finally:
        text.detach()


def iter_normalized(path, reference):
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = [x for x in archive.infolist() if not x.is_dir() and x.filename.lower().endswith(('.csv', '.txt'))]
            if not members:
                raise PermanentError('ZIP sem CSV/TXT; o formato exige conector específico')
            if len({x.filename for x in members}) != len(members):
                raise PermanentError('ZIP contém nomes de membros duplicados')
            if sum(x.file_size for x in members) > 8_000_000_000:
                raise PermanentError('ZIP excede o limite descompactado de 8 GB')
            for member in sorted(members, key=lambda x: x.filename):
                with archive.open(member) as stream:
                    yield from _csv_rows(stream, member.filename, reference)
    else:
        with path.open('rb') as stream:
            yield from _csv_rows(stream, path.name, reference)


def register_artifact(path, reference, lease, url=None):
    validate_reference(reference)
    path = Path(path).resolve()
    if not path.is_file():
        raise PermanentError('Arquivo original não encontrado')
    with path.open('rb') as stream:
        raw_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    # CLI uploads may be edited or removed later. The pipeline always parses an
    # immutable, content-addressed copy under its own raw storage.
    folder = db.ROOT / 'data' / 'raw' / 'pipeline'
    folder.mkdir(parents=True, exist_ok=True)
    preserved = folder / (raw_hash + '.raw')
    if path != preserved.resolve():
        if not preserved.exists():
            temporary = folder / f'{raw_hash}-{lease["job_id"]}-{lease["token"]}.copy'
            copied_hash = hashlib.sha256()
            try:
                with path.open('rb') as source, temporary.open('wb') as target:
                    for chunk in iter(lambda: source.read(1024 * 1024), b''):
                        copied_hash.update(chunk)
                        target.write(chunk)
                if copied_hash.hexdigest() != raw_hash:
                    raise PermanentError('Arquivo alterado durante a preservação; importe novamente')
                temporary.replace(preserved)
            finally:
                temporary.unlink(missing_ok=True)
        path = preserved
    with job_transaction(lease) as c:
        frozen = rules_snapshot(c)
    parser_key = PARSER_VERSION + ':' + digest(frozen)
    artifact_id = stable(canonical([PROVIDER, DATASET, reference, raw_hash, parser_key, NORMALIZATION_VERSION]))
    expanded = path.stat().st_size
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            expanded = sum(x.file_size for x in archive.infolist())
    if expanded > 8_000_000_000:
        raise PermanentError('Arquivo excede o limite de 8 GB descompactados')
    with job_transaction(lease, 'artifact') as c:
        _upsert_nothing(c, p.artifact, dict(id=artifact_id, provider=PROVIDER, dataset=DATASET, reference=reference,
            raw_sha256=raw_hash, parser_version=parser_key, normalization_version=NORMALIZATION_VERSION,parse_rules=frozen,parse_stats={},
            path=str(path), url=url, collected_at=timestamp(), bytes=path.stat().st_size, uncompressed_bytes=expanded,
            rows=0, status='pending'))
        return dict(c.execute(select(p.artifact).where(p.artifact.c.id == artifact_id)).mappings().one())


def parse_artifact(artifact_id, lease):
    with job_transaction(lease, 'parse') as c:
        artifact = dict(c.execute(select(p.artifact).where(p.artifact.c.id == artifact_id)).mappings().one())
        if artifact['semantic_content_id']:
            return artifact['semantic_content_id']
        # Resume by physical row position. Already committed positions are read
        # again from a ZIP but never inserted twice.
        committed = c.execute(select(func.max(p.observation.c.ordinal)).where(p.observation.c.artifact_id == artifact_id)).scalar_one() or 0
        size = lock_lease(c, lease)['block_size']
    _check_disk(int(os.getenv('I9_PIPELINE_MIN_FREE_BYTES','2000000000')))
    with Path(artifact['path']).open('rb') as original:
        if hashlib.file_digest(original,'sha256').hexdigest()!=artifact['raw_sha256']:
            raise PermanentError('O arquivo original preservado foi alterado ou corrompido')
    import pandas as pd
    from src.transform.ingest import csv_frames
    frozen=artifact.get('parse_rules') or rules_snapshot_for_parse()
    @lru_cache(maxsize=20000)
    def decision(name):
        return _classify(name,frozen)['status']
    @lru_cache(maxsize=20000)
    def hint(name):
        return int(catalog.electrified_hint(name))
    def frames():
        if zipfile.is_zipfile(artifact['path']):
            with zipfile.ZipFile(artifact['path']) as archive:
                for member in archive.infolist():
                    if not member.is_dir() and member.filename.lower().endswith(('.csv','.txt')):
                        with archive.open(member) as stream:
                            for frame in csv_frames(stream):yield member.filename,frame
        else:
            with open(artifact['path'],'rb') as stream:
                for frame in csv_frames(stream):yield Path(artifact['path']).name,frame
    scanned=0
    for chunk_index,(member,frame) in enumerate(frames()):
        with job_transaction(lease) as c:
            prior=_block_result(c,lease,'filtered:'+artifact_id,chunk_index)
        if prior:
            scanned+=len(frame)
            continue
        header={key:HEADER_ALIASES.get(normalize(key),'extra:'+normalize(key)) for key in frame.columns}
        if len(set(header.values()))!=len(header):raise PermanentError('Colunas duplicadas no arquivo')
        missing={'state','city','raw_name','manufacture_year','quantity'}-set(header.values())
        if missing:raise PermanentError('Colunas obrigatórias ausentes: '+str(sorted(missing)))
        name_col=next(key for key,value in header.items() if value=='raw_name')
        qty_col=next(key for key,value in header.items() if value=='quantity')
        names={name:normalize(name) for name in frame[name_col].unique()}
        decisions={name:decision(normalized) for name,normalized in names.items()}
        qty=pd.to_numeric(frame[qty_col],errors='coerce')
        if qty.isna().any() or (qty<0).any() or (qty%1!=0).any() or (qty>9_000_000_000_000_000).any():raise PermanentError('Quantidade inválida na fonte')
        chosen=frame[name_col].map(decisions)=='recognized'
        records=[]
        for position,row in frame.loc[chosen].iterrows():
            records.append(dict(**_normalize_row(row.to_dict(),header,artifact['reference']),member=member,line_number=scanned+int(frame.index.get_loc(position))+2))
        rest=pd.DataFrame({'raw_name':frame.loc[~chosen,name_col].map(names),'quantity':qty.loc[~chosen].astype('int64')})
        grouped=rest.groupby('raw_name',sort=True)['quantity'].agg(['sum','count'])
        # Keep only names for coverage diagnostics. Neither quantities nor
        # individual rows of unsupported names are retained.
        summaries=[dict(raw_name=str(name),quantity=0,row_count=0,status='pending',electrified_hint=hint(str(name)))
                   for name,row in grouped.iterrows() if decision(str(name))=='pending']
        scanned+=len(frame)
        _persist_filtered_chunk(artifact_id,records,summaries,scanned,chunk_index,lease)
    if not scanned:raise PermanentError('Arquivo não contém observações')
    with db.engine.connect() as c:
        ordinal=c.execute(select(func.count()).select_from(p.observation).where(p.observation.c.artifact_id==artifact_id)).scalar_one()
    # Hash computation streams the database sort; only one canonical row is
    # retained in Python. Ordering by row hash also avoids locale dependence.
    hasher = hashlib.sha256()
    with db.engine.connect() as c:
        collation = 'C' if c.dialect.name == 'postgresql' else 'BINARY'
        rows = c.execution_options(stream_results=True, yield_per=1000).execute(
            select(p.observation.c.canonical).where(p.observation.c.artifact_id == artifact_id)
            .order_by(p.observation.c.row_sha256, p.observation.c.canonical.collate(collation)))
        count = 0
        for row in rows:
            encoded = row.canonical.encode('utf-8')
            hasher.update(len(encoded).to_bytes(8, 'big'))
            hasher.update(encoded)
            count += 1
    with db.engine.connect() as c:
        for item in c.execute(select(p.filtered_summary.c.raw_name,p.filtered_summary.c.status,func.sum(p.filtered_summary.c.quantity),func.sum(p.filtered_summary.c.row_count)).where(p.filtered_summary.c.artifact_id==artifact_id).group_by(p.filtered_summary.c.raw_name,p.filtered_summary.c.status).order_by(p.filtered_summary.c.raw_name,p.filtered_summary.c.status)):
            hasher.update(canonical([item[0],item[1],int(item[2]),int(item[3])]).encode('utf-8'))
    # Filtering happens before content is stored. Equal retained rows under
    # different classifier rules are not interchangeable: their category and
    # excluded-name decisions can differ even when row bytes do not.
    semantic_hash = digest([hasher.hexdigest(), artifact['parser_version']])
    content_id = stable(canonical([PROVIDER, DATASET, artifact['reference'], NORMALIZATION_VERSION, semantic_hash]))
    with job_transaction(lease, 'parsed') as c:
        if count != ordinal:
            raise PermanentError('Contagem persistida não corresponde à leitura integral')
        _upsert_nothing(c, p.content, dict(id=content_id, provider=PROVIDER, dataset=DATASET,
            reference=artifact['reference'], normalization_version=NORMALIZATION_VERSION, semantic_sha256=semantic_hash,
            artifact_id=artifact_id, rows=scanned))
        c.execute(update(p.artifact).where(p.artifact.c.id == artifact_id).values(status='complete', rows=count, semantic_content_id=content_id))
        return content_id


def rules_snapshot_for_parse():
    with db.engine.connect() as c:return rules_snapshot(c)


def _persist_filtered_chunk(artifact_id, records, summaries, scanned, index, lease):
    _check_disk(int(os.getenv('I9_PIPELINE_MIN_FREE_BYTES','2000000000')))
    with job_transaction(lease,'filter') as c:
        artifact=c.execute(select(p.artifact).where(p.artifact.c.id==artifact_id).with_for_update()).mappings().one()
        stats=artifact.get('parse_stats') or {}
        if stats.get('last_chunk',-1)>=index:
            _commit_block(c,lease,'filtered:'+artifact_id,index,{'scanned_rows':scanned})
            return
        if stats.get('last_chunk',-1)!=index-1:raise PermanentError('Bloco fora de sequência')
        first=artifact['rows'] or 0
        for number,row in enumerate(records,first+1):row.update(id=stable(f'{artifact_id}:{number}'),artifact_id=artifact_id,ordinal=number)
        if records:c.execute(insert(p.observation),records)
        for row in summaries:row.update(artifact_id=artifact_id,block_index=index)
        if summaries:c.execute(insert(p.filtered_summary),summaries)
        c.execute(update(p.artifact).where(p.artifact.c.id==artifact_id).values(rows=first+len(records),status='filtering',parse_stats={'scanned_rows':scanned,'last_chunk':index}))
        _commit_block(c,lease,'filtered:'+artifact_id,index,{'scanned_rows':scanned,'retained_rows':first+len(records)})
        c.execute(update(p.job).where(p.job.c.id==lease['job_id']).values(progress={'scanned_rows':scanned,'retained_rows':first+len(records),'artifact_id':artifact_id}))


def _persist_rows(artifact_id, rows, lease, size):
    _check_disk(int(os.getenv('I9_PIPELINE_MIN_FREE_BYTES', '2000000000')))
    with job_transaction(lease, 'parse') as c:
        # Serializes identical artifacts imported by independently queued jobs.
        c.execute(select(p.artifact.c.id).where(p.artifact.c.id == artifact_id).with_for_update()).one()
        existing = c.execute(select(func.max(p.observation.c.ordinal)).where(p.observation.c.artifact_id == artifact_id)).scalar_one() or 0
        index = (rows[0]['ordinal'] - 1) // size
        if _block_result(c, lease, 'parse:' + artifact_id, index) is not None:
            return
        new_rows = [r for r in rows if r['ordinal'] > existing]
        if new_rows:
            if new_rows[0]['ordinal'] != existing + 1:
                raise PermanentError('Lacuna detectada entre blocos da leitura')
            c.execute(insert(p.observation), new_rows)
        _commit_block(c, lease, 'parse:' + artifact_id, index, {'last_row': rows[-1]['ordinal'], 'artifact_id': artifact_id})
        c.execute(update(p.artifact).where(p.artifact.c.id == artifact_id).values(rows=max(existing, rows[-1]['ordinal']), status='parsing'))


def _check_disk(required):
    # For remote PostgreSQL, the administrator can provide a mounted volume path;
    # otherwise this checks the local raw/runtime volume. DB disk-full errors are
    # separately recoverable and never activate a partially prepared publication.
    folder = Path(os.getenv('I9_DATABASE_VOLUME_PATH', str(db.ROOT)))
    if shutil.disk_usage(folder).free < required:
        raise PermanentError(f'Espaço em disco insuficiente: necessários ao menos {required // 1_000_000} MB livres para preparação segura')


def rules_snapshot(c):
    latest = select(p.rule.c.raw_name, func.max(p.rule.c.version).label('version')).group_by(p.rule.c.raw_name).subquery()
    overrides = [dict(r) for r in c.execute(select(p.rule).join(latest, and_(
        p.rule.c.raw_name == latest.c.raw_name, p.rule.c.version == latest.c.version)).order_by(p.rule.c.raw_name)).mappings()]
    # Built-in rules remain frozen in the classification record across releases.
    # Model-family assertions supply vehicle type when the source omits it.
    return {'prefixes': list(catalog.PREFIXES), 'overrides': overrides, 'category_policy': 'model-family-2',
            'automatic_classifier': auto_classification.VERSION}


def approve_rule(raw_name, decision, evidence, actor, manufacturer=None, model=None,
                 powertrain=None, category=None, vehicle_subtype=None, segment=None, body_type=None, expected_version=0):
    raw_name = normalize(raw_name)
    if decision not in {'include', 'exclude'} or not raw_name or not evidence.strip() or not actor:
        raise ValueError('Regra exige nome, decisão, evidência e responsável')
    if decision == 'include' and (not manufacturer or not model or powertrain not in ELECTRIFIED_POWERTRAINS):
        raise ValueError('Inclusão exige fabricante, modelo e eletrificação reconhecida')
    if category and category not in CANONICAL_CATEGORIES:
        raise ValueError('Categoria inválida')
    with db.engine.begin() as c:
        version = c.execute(select(func.max(p.rule.c.version)).where(p.rule.c.raw_name == raw_name)).scalar_one() or 0
        if version != expected_version:
            raise Conflict('A regra foi alterada; atualize a revisão antes de salvar')
        result = dict(id=str(uuid.uuid4()), raw_name=raw_name, version=version + 1, decision=decision,
            evidence=evidence, actor=actor, manufacturer=manufacturer, model=model, powertrain=powertrain,
            category=category or 'unknown',vehicle_subtype=vehicle_subtype,segment=segment,body_type=body_type,created_at=timestamp())
        try:
            c.execute(insert(p.rule).values(**result))
        except IntegrityError as exc:
            raise Conflict('Outro administrador alterou esta regra') from exc
        c.execute(insert(s.audit).values(id=str(uuid.uuid4()), actor=actor, role='admin', action='classification_rule',
            entity_type='pipeline_rule', entity_id=result['id'], created_at=timestamp(), details=result))
        if decision=='include':
            candidates=c.execute(select(registry_schema.model).where(
                func.upper(registry_schema.model.c.manufacturer)==normalize(manufacturer),
                func.upper(registry_schema.model.c.model)==normalize(model),
                registry_schema.model.c.powertrain_type==powertrain)).mappings().all()
            if len(candidates)>1:raise Conflict('Há mais de um modelo canônico compatível; corrija os aliases antes de aprovar')
            if candidates:
                canonical_model=candidates[0]
                c.execute(update(registry_schema.model).where(registry_schema.model.c.id==canonical_model['id']).values(
                    vehicle_category=category or canonical_model['vehicle_category'],vehicle_subtype=vehicle_subtype,
                    segment=segment,body_type=body_type,is_electric=True,is_heavy_vehicle=is_heavy(category),
                    classification_source=evidence,classification_confidence='high',classification_status='approved'))
            else:
                canonical_key=digest(['approved-model',normalize(manufacturer),normalize(model),powertrain])[:24]
                model_id=stable('registry-model-approved:'+canonical_key)
                _upsert_nothing(c,registry_schema.model,dict(id=model_id,model_key=canonical_key,manufacturer=manufacturer,
                    model=model,vehicle_category=category or 'unknown',powertrain_type=powertrain,vehicle_subtype=vehicle_subtype,
                    segment=segment,body_type=body_type,is_electric=True,is_heavy_vehicle=is_heavy(category),allocated_count=0,
                    classification_source=evidence,classification_confidence='high',classification_status='approved',created_at=timestamp()))
                canonical_model=c.execute(select(registry_schema.model).where(registry_schema.model.c.model_key==canonical_key)).mappings().one()
            c.execute(update(registry_schema.semantic_alias).where(registry_schema.semantic_alias.c.source=='SENATRAN',
                registry_schema.semantic_alias.c.alias_normalized==raw_name,registry_schema.semantic_alias.c.approved.is_(True)).values(approved=False))
            c.execute(insert(registry_schema.semantic_alias).values(id=str(uuid.uuid4()),model_id=canonical_model['id'],source='SENATRAN',
                alias_raw=raw_name,alias_normalized=raw_name,match_type='administrative_review',confidence='high',evidence=evidence,
                approved=True,version=version+1,actor=actor,created_at=timestamp()))
        return result


def _classify(name, frozen):
    for override in frozen['overrides']:
        if override['raw_name'] == name:
            return {key: override.get(key) for key in ('manufacturer', 'model', 'powertrain', 'category','vehicle_subtype','segment','body_type', 'evidence')} | {
                'status': 'recognized' if override['decision'] == 'include' else 'excluded'}
    explicit = (auto_classification.classify(name)
                if frozen.get('automatic_classifier') == auto_classification.VERSION else None)
    # A motorization written in the source name takes precedence over a broad
    # family prefix (for example, a BEV version of a normally PHEV family).
    if explicit:
        for prefix, brand, model, propulsion in frozen['prefixes']:
            if (name == prefix or name.startswith(prefix + ' ')) and propulsion == explicit['powertrain']:
                explicit['manufacturer'], explicit['model'] = brand, model
                break
        return explicit
    for prefix, brand, model, propulsion in frozen['prefixes']:
        if name == prefix or name.startswith(prefix + ' '):
            known_bus=prefix in {'M BENZ EO500U','MERCEDES BENZ EO500U'}
            inferred_category, category_source = auto_classification.category_for(name)
            return {'manufacturer': brand, 'model': model, 'powertrain': propulsion,
                    'category': 'bus' if known_bus else inferred_category,
                    'vehicle_subtype':None,'segment':None,'body_type':None,
                    'evidence': 'https://www.mercedes-benz-trucks.com.br/onibus/eletrico/tecnologia/bateria' if known_bus else (category_source or 'Catálogo revisado local; categoria inferida por família do modelo'), 'status': 'recognized'}
    return {'status': 'pending', 'manufacturer': None, 'model': None, 'powertrain': None,
            'category': None,'vehicle_subtype':None,'segment':None,'body_type':None,'evidence': None}


def classify_content(content_id, lease):
    with job_transaction(lease, 'classify') as c:
        job = lock_lease(c, lease)
        progress = job['progress'] or {}
        classification_id = progress.get('classification_id')
        if not classification_id:
            content_row=c.execute(select(p.content).where(p.content.c.id==content_id)).mappings().one()
            artifact_row=c.execute(select(p.artifact).where(p.artifact.c.id==content_row['artifact_id'])).mappings().one()
            frozen = artifact_row.get('parse_rules') or rules_snapshot(c)
            rules_version = digest(frozen)
            classification_id = stable(canonical([content_id, rules_version, TRANSFORMATION_VERSION]))
            source_id = stable('source:' + classification_id)
            _upsert_nothing(c, p.classification, dict(id=classification_id, content_id=content_id,
                rules_version=rules_version, rules_snapshot=frozen, transformation_version=TRANSFORMATION_VERSION,
                source_snapshot_id=source_id, status='classifying', recognized_quantity=0, pending_quantity=0))
            # A checkpoint independent of current stage preserves the frozen rules
            # if the worker is restarted halfway through aggregation.
            key = 'classification:' + content_id
            persisted = _block_result(c, lease, key, 0)
            if persisted:
                classification_id = persisted['classification_id']
            else:
                _commit_block(c, lease, key, 0, {'classification_id': classification_id})
        classification = dict(c.execute(select(p.classification).where(p.classification.c.id == classification_id)).mappings().one())
        content = dict(c.execute(select(p.content).where(p.content.c.id == content_id)).mappings().one())
        artifact = dict(c.execute(select(p.artifact).where(p.artifact.c.id == content['artifact_id'])).mappings().one())
    if classification['status'] == 'complete':
        return classification['source_snapshot_id'], True
    frozen = classification['rules_snapshot']
    last_name = ''
    index = 0
    while True:
        with db.engine.connect() as c:
            names = c.execute(select(p.observation.c.raw_name).where(p.observation.c.artifact_id == artifact['id'],
                p.observation.c.raw_name > last_name).distinct().order_by(p.observation.c.raw_name).limit(BLOCK_SIZE)).scalars().all()
        if not names:
            break
        with job_transaction(lease, 'classify') as c:
            c.execute(select(p.classification.c.id).where(p.classification.c.id == classification_id).with_for_update()).one()
            if _block_result(c, lease, 'names:' + classification_id, index) is None:
                existing = set(c.execute(select(p.classified_name.c.raw_name).where(
                    p.classified_name.c.classification_id == classification_id, p.classified_name.c.raw_name.in_(names))).scalars())
                records = [dict(id=stable(classification_id + ':' + name), classification_id=classification_id,
                                raw_name=name, **_classify(name, frozen)) for name in names if name not in existing]
                if records:
                    c.execute(insert(p.classified_name), records)
                _commit_block(c, lease, 'names:' + classification_id, index,
                              {'classification_id': classification_id, 'last_name': names[-1]})
        last_name = names[-1]
        index += 1
    return _aggregate_classification(classification, content, artifact, lease), False


def _aggregate_classification(classification, content, artifact, lease):
    cid = classification['id']
    source_id = classification['source_snapshot_id']
    with job_transaction(lease, 'aggregate') as c:
        job = lock_lease(c, lease)
        _upsert_nothing(c, s.source_snapshot, dict(id=source_id, provider=PROVIDER,
            dataset_name=f'{DATASET} / regras {classification["rules_version"]}', reference_period=content['reference'],
            retrieved_at=timestamp(), method='public_monthly_aggregate', raw_file_hash=artifact['raw_sha256'],
            raw_storage_path=artifact['path'], source_version=PARSER_VERSION, transformation_version=TRANSFORMATION_VERSION,
            ingestion_status='staging', source_url=artifact['url'], metric='fleet_stock', access_level='public',
            scope='Veículos eletrificados por catálogo ou motorização explícita no nome SENATRAN; nomes duvidosos excluídos; cadastros sem identidade física',
            metadata={'pipeline_job_id': lease['job_id'], 'pipeline_generation': job['generation'],
                      'semantic_content_id': content['id'], 'semantic_sha256': content['semantic_sha256'],
                      'rules_version': classification['rules_version'], 'normalization_version': NORMALIZATION_VERSION,
                      'automatic_classifier': auto_classification.VERSION}))
    # Joining classified names avoids a Python lookup growing with every source
    # model. Every original row contributes its quantity, including multiplicity.
    o, n = p.observation, p.classified_name
    category = func.coalesce(o.c.category, n.c.category, literal('unknown')).label('category')
    dimensions = [o.c.state, o.c.city, o.c.raw_name, o.c.manufacture_year,
                  n.c.manufacturer, n.c.model, n.c.powertrain, category]
    query = select(*dimensions, func.sum(o.c.quantity).label('quantity')).select_from(
        o.join(n, and_(o.c.raw_name == n.c.raw_name, n.c.classification_id == cid))).where(
        o.c.artifact_id == artifact['id'], n.c.status == 'recognized').group_by(*dimensions).order_by(*dimensions)
    buffer = []
    index = count = recognized = 0
    with db.engine.connect() as read:
        for row in read.execution_options(stream_results=True, yield_per=1000).execute(query).mappings():
            identity = digest([row[k] for k in ('state', 'city', 'raw_name', 'manufacture_year', 'manufacturer', 'model', 'powertrain', 'category')])
            buffer.append(dict(id=stable(source_id + ':' + identity), source_snapshot_id=source_id,
                reference_period=content['reference'], country='BR', state=row['state'], municipality=row['city'],
                vehicle_category=row['category'], manufacturer=row['manufacturer'], model=row['model'],
                trim=None, powertrain_type=row['powertrain'], manufacture_year=row['manufacture_year'],
                quantity=int(row['quantity']), source_record_reference=identity, confidence='reviewed-model-not-identity',
                abve_electrified_classification=1))
            recognized += int(row['quantity'])
            count += 1
            if len(buffer) == BLOCK_SIZE:
                _aggregate_block(cid, source_id, buffer, index, lease)
                index += 1
                buffer = []
    if buffer:
        _aggregate_block(cid, source_id, buffer, index, lease)
    if not count or not recognized:
        raise PermanentError('Nenhum veículo eletrificado reconhecido; publicação vazia bloqueada')
    with job_transaction(lease, 'validated') as c:
        actual = c.execute(select(func.count(), func.sum(s.aggregate.c.quantity)).where(s.aggregate.c.source_snapshot_id == source_id)).one()
        if actual[0] != count or actual[1] != recognized:
            raise PermanentError('Totais preparados divergem da classificação')
        pending = c.execute(select(func.coalesce(func.sum(p.filtered_summary.c.quantity),0)).where(p.filtered_summary.c.artifact_id==artifact['id'],p.filtered_summary.c.status=='pending')).scalar_one()
        pending_names=c.execute(select(func.count(func.distinct(p.filtered_summary.c.raw_name))).where(p.filtered_summary.c.artifact_id==artifact['id'],p.filtered_summary.c.status=='pending')).scalar_one()
        metadata = c.execute(select(s.source_snapshot.c.metadata).where(s.source_snapshot.c.id == source_id)).scalar_one() or {}
        metadata.update({'recognized_quantity': recognized, 'pending_quantity': int(pending), 'pending_names': pending_names,
                         'raw_rows': content['rows'], 'coverage': 'automatic_explicit_partial'})
        c.execute(update(s.source_snapshot).where(s.source_snapshot.c.id == source_id).values(
            ingestion_status='success', record_count=count, metadata=metadata))
        c.execute(update(p.classification).where(p.classification.c.id == cid).values(status='complete',
            recognized_quantity=recognized, pending_quantity=int(pending)))
        evaluate_alerts(c, cid, source_id)
    return source_id


def _aggregate_block(cid, source_id, rows, index, lease):
    with job_transaction(lease, 'aggregate') as c:
        c.execute(select(p.classification.c.id).where(p.classification.c.id == cid).with_for_update()).one()
        if _block_result(c, lease, 'aggregate:' + cid, index) is not None:
            return
        existing = set(c.execute(select(s.aggregate.c.id).where(s.aggregate.c.id.in_([r['id'] for r in rows]))).scalars())
        fresh = [row for row in rows if row['id'] not in existing]
        if fresh:
            c.execute(insert(s.aggregate), fresh)
        _commit_block(c, lease, 'aggregate:' + cid, index, {'classification_id': cid, 'source_id': source_id, 'rows_in_block': len(rows)})


def pending_reviews(classification_id=None, limit=50, after='', offset=0, paged=False):
    with db.engine.connect() as c:
        if not classification_id:
            classification_id=c.execute(select(p.classification.c.id)
                .join(s.fleet_publication,p.classification.c.source_snapshot_id==s.fleet_publication.c.source_snapshot_id)
                .join(s.fleet_active,s.fleet_active.c.publication_id==s.fleet_publication.c.id)
                .where(s.fleet_active.c.id=='active',p.classification.c.status=='complete')
                .limit(1)).scalar_one_or_none()
        if not classification_id:return {'items':[],'total':0,'offset':offset,'limit':limit} if paged else []
        artifact_id=c.execute(select(p.content.c.artifact_id).join(p.classification).where(p.classification.c.id==classification_id)).scalar_one()
        t=p.filtered_summary
        query=select(t.c.raw_name,func.sum(t.c.quantity).label('quantity'),func.max(t.c.electrified_hint).label('electrified_hint')).where(t.c.artifact_id==artifact_id,t.c.status=='pending',t.c.electrified_hint==1,t.c.raw_name>after,
            ~select(p.rule.c.id).where(p.rule.c.raw_name==t.c.raw_name).exists()).group_by(t.c.raw_name)
        total=c.execute(select(func.count()).select_from(query.subquery())).scalar()
        rows=c.execute(query.order_by(func.max(t.c.electrified_hint).desc(),t.c.raw_name).limit(min(200,max(1,limit))).offset(offset)).mappings()
        items=[dict(row,classification_id=classification_id,
            reason=auto_classification.pending_reason(row['raw_name'])[0],
            source=auto_classification.pending_reason(row['raw_name'])[1]) for row in rows]
        return {'items':items,'total':total,'offset':offset,'limit':limit} if paged else items


def enqueue_classifier_refresh():
    """Reimport published months once when explicit-name rules change.

    The old publication remains active until its replacement is fully built.
    Source downloads can be disabled by the installation configuration.
    """
    if os.getenv('ALLOW_SOURCE_DOWNLOAD', 'false').lower() != 'true':
        return []
    with db.engine.connect() as c:
        sources=list(c.execute(select(s.fleet_publication.c.reference_period,
            s.fleet_publication.c.finished_at,s.source_snapshot.c.id,
            s.source_snapshot.c.source_url,s.source_snapshot.c.metadata)
            .join(s.source_snapshot,s.fleet_publication.c.source_snapshot_id==s.source_snapshot.c.id)
            .where(s.fleet_publication.c.status=='complete',s.source_snapshot.c.provider==PROVIDER,
                   s.source_snapshot.c.metric=='fleet_stock')
            .order_by(s.fleet_publication.c.reference_period,s.fleet_publication.c.finished_at.desc())).mappings())
        busy={row['reference'] for row in c.execute(select(p.job.c.payload)
            .where(p.job.c.kind.in_(['import_remote','reclassify']),
                   p.job.c.status.in_(['queued','running']))).scalars()
            if isinstance(row,dict) and (row.get('reference') or '')}
    latest={}
    for row in sources:
        latest.setdefault(row['reference_period'],row)
    queued=[]
    for reference,row in latest.items():
        if reference in busy or not row['source_url']:
            continue
        if (row['metadata'] or {}).get('automatic_classifier')==auto_classification.VERSION:
            continue
        job=enqueue('import_remote',{'reference':reference,'url':row['source_url'],
            'resource_id':'automatic-classification','automatic_classifier':auto_classification.VERSION},
            f'automatic-classification:{auto_classification.VERSION}:{reference}:{row["id"]}')
        if job['status']=='queued':
            queued.append(job['id'])
    return queued


def evaluate_alerts(c, classification_id, source_id):
    source = c.execute(select(s.source_snapshot).where(s.source_snapshot.c.id == source_id)).mappings().one()
    selected_policy = c.execute(select(p.policy).order_by(p.policy.c.created_at.desc(), p.policy.c.id).limit(1)).mappings().one()
    previous = c.execute(select(s.source_snapshot).join(s.fleet_publication,
        s.fleet_publication.c.source_snapshot_id == s.source_snapshot.c.id).where(
        s.fleet_publication.c.status == 'complete', s.source_snapshot.c.provider == PROVIDER,
        s.source_snapshot.c.reference_period < source['reference_period']).order_by(
        s.source_snapshot.c.reference_period.desc(), s.source_snapshot.c.retrieved_at.desc()).limit(1)).mappings().first()
    current_total = c.execute(select(func.sum(s.aggregate.c.quantity)).where(s.aggregate.c.source_snapshot_id == source_id)).scalar_one() or 0
    alerts, metrics = [], {'current_total': int(current_total), 'reference': source['reference_period']}
    if previous:
        previous_total = c.execute(select(func.sum(s.aggregate.c.quantity)).where(s.aggregate.c.source_snapshot_id == previous['id'])).scalar_one() or 0
        metrics.update(previous_total=int(previous_total), previous_reference=previous['reference_period'])
        if previous_total and abs(current_total - previous_total) / previous_total > selected_policy['values']['national_ratio']:
            alerts.append({'kind': 'national_variation', 'current': int(current_total), 'previous': int(previous_total)})
        ref_year, ref_month = map(int, source['reference_period'].split('-'))
        prev_year, prev_month = map(int, previous['reference_period'].split('-'))
        if (ref_year * 12 + ref_month) - (prev_year * 12 + prev_month) > 1:
            alerts.append({'kind': 'missing_months', 'after': previous['reference_period'], 'before': source['reference_period']})
        # Model-level aggregation is bounded by the reviewed catalog, not source rows.
        dims = [s.aggregate.c.manufacturer, s.aggregate.c.model, s.aggregate.c.powertrain_type]
        def model_totals(sid):
            return {tuple(row[:3]): int(row[3]) for row in c.execute(select(*dims, func.sum(s.aggregate.c.quantity))
                    .where(s.aggregate.c.source_snapshot_id == sid).group_by(*dims))}
        before, after = model_totals(previous['id']), model_totals(source_id)
        for key, old in before.items():
            new = after.get(key, 0)
            if new == 0 and old > 0:
                alerts.append({'kind': 'model_disappeared', 'model': list(key), 'previous': old})
            elif old and abs(new - old) >= selected_policy['values']['model_absolute'] and abs(new - old) / old > selected_policy['values']['model_ratio']:
                alerts.append({'kind': 'model_variation', 'model': list(key), 'previous': old, 'current': new})
        old_pending = (previous['metadata'] or {}).get('pending_quantity')
        new_pending = (source['metadata'] or {}).get('pending_quantity', 0)
        if old_pending is not None and new_pending > old_pending:
            alerts.append({'kind': 'pending_increased', 'previous': old_pending, 'current': new_pending})
    result = dict(id=stable(f'alerts:{classification_id}:{selected_policy["id"]}:{previous["id"] if previous else "none"}'),
        classification_id=classification_id, policy_id=selected_policy['id'], previous_source_id=previous['id'] if previous else 'none',
        created_at=timestamp(), metrics=metrics, alerts=alerts)
    _upsert_nothing(c, p.evaluation, result)
    return result


def set_alert_policy(values, actor, reason):
    if set(values) != set(DEFAULT_POLICY) or not actor or not reason.strip():
        raise ValueError('Política exige limites, responsável e motivo')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 for v in values.values()):
        raise ValueError('Limites precisam ser números não negativos')
    result = dict(id=str(uuid.uuid4()), created_at=timestamp(), actor=actor, reason=reason, values=values)
    with db.engine.begin() as c:
        c.execute(insert(p.policy).values(**result))
        c.execute(insert(s.audit).values(id=str(uuid.uuid4()), actor=actor, role='admin', action='alert_policy',
            entity_type='pipeline_alert_policy', entity_id=result['id'], created_at=timestamp(), details=result))
    return result


def _resource_reference(resource):
    from src.transform.ingest import reference_of, MONTHS
    reference = reference_of(resource)
    if reference:
        return reference
    text = normalize(' '.join(str(resource.get(k) or '') for k in ('name', 'description', 'url'))).lower().replace('maro ', 'marco ')
    for name, month in MONTHS.items():
        match = re.search(r'\b' + name + r' (20\d{2})\b', text)
        if match:
            return f'{match[1]}-{month:02d}'
    match = re.search(r'\b(20\d{2}) (0[1-9]|1[0-2])\b', text)
    return f'{match[1]}-{match[2]}' if match else None


class _HeavyLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.publications = []
        self.reports = []
        self.paragraph = None
        self.anchor = None
        self.reference = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'p':
            self.paragraph = ''
        if tag.lower() == 'a':
            href = dict(attrs).get('href') or ''
            self.anchor = [href, '']
            if 'TIPO_ESPECIE_EIXOS' in href.upper():
                self.links.append(href)

    def handle_data(self, data):
        if self.paragraph is not None:
            self.paragraph += data
        if self.anchor is not None:
            self.anchor[1] += data

    def handle_endtag(self, tag):
        if tag.lower() == 'a' and self.anchor is not None:
            href, title = self.anchor
            if href.lower().endswith('.xlsx') and 'TIPO_ESPECIE_EIXOS' not in href.upper():
                self.reports.append((href, title.strip()))
            if self.reference and all(word in normalize(title) for word in ('MARCA', 'MODELO')):
                self.publications.append((self.reference, href))
            self.anchor = None
        if tag.lower() == 'p' and self.paragraph is not None:
            title = normalize(self.paragraph)
            if title.startswith('FROTA NACIONAL'):
                self.reference = _resource_reference({'name': title.replace(' DE ', ' ')})
            self.paragraph = None


def _official_heavy_resources(client, start, end):
    """Discover file 08 on the official yearly pages, independently of CKAN file 07."""
    resources = []
    for year in range(int(start[:4]), int(end[:4]) + 1):
        page = f'https://www.gov.br/transportes/pt-br/assuntos/transito/conteudo-Senatran/frota-de-veiculos-{year}'
        response = client.get(page)
        response.raise_for_status()
        parser = _HeavyLinkParser()
        parser.feed(response.text)
        for href in sorted(set(parser.links)):
            url = urljoin(page, href)
            resources.append({'id': digest(url), 'name': '08 TIPO ESPECIE EIXOS',
                              'description': 'Arquivo estatístico oficial 08', 'url': url})
        for reference, href in sorted(set(parser.publications)):
            resources.append({'id': digest(page + reference), 'name': '07 MARCA MODELO',
                'description': reference, 'url': urljoin(page, href), 'published_only': True, 'publication_url': page})
        from urllib.parse import urlparse
        for href, title in sorted(set(parser.reports)):
            url = urljoin(page, href)
            if urlparse(url).scheme == 'https' and urlparse(url).hostname == 'www.gov.br':
                resources.append({'id': digest(url), 'name': title, 'url': url,
                                  'report_only': True, 'publication_url': page})
    return resources


class CatalogResources(list):
    warning = None


def catalog_resources(start, end):
    from src.connectors.public import CATALOG
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        resources = CatalogResources()
        catalog_error = None
        try:
            response = client.get(CATALOG)
            response.raise_for_status()
            body = response.json()
            if not body.get('success') or not isinstance(body.get('result', {}).get('resources'), list):
                raise PermanentError('Catalogo SENATRAN fora do contrato esperado')
            resources.extend(body['result']['resources'])
        except (httpx.HTTPError, ValueError, PermanentError) as exc:
            catalog_error = exc
        try:
            official = _official_heavy_resources(client, start, end)
            resources.extend(official)
        except (httpx.HTTPError, ValueError):
            if not resources:
                raise
            resources.warning = 'A página de publicações está indisponível; a lista foi obtida pelo catálogo de arquivos.'
        if catalog_error:
            if not resources:
                raise catalog_error
            resources.warning = 'Meses consultados na página oficial gov.br. O catálogo de arquivos de marca/modelo está indisponível; esse relatório ficará pendente, mas os demais podem ser baixados e importados.'
    return resources


def discover(resources=None, start='2026-01', end=None, refresh=False, discovery_key=None):
    """Discover one unambiguous brand/model dataset per month; never sum files."""
    validate_reference(start)
    end = end or datetime.now(ZoneInfo('America/Sao_Paulo')).strftime('%Y-%m')
    validate_reference(end)
    if resources is None:
        resources = catalog_resources(start, end)
    candidates,heavy_candidates = {},{}
    for resource in resources:
        if resource.get('published_only') or resource.get('report_only'):
            continue
        reference = _resource_reference(resource)
        label = normalize(' '.join(str(resource.get(k) or '') for k in ('name', 'description', 'url')))
        if not reference or reference < start or reference > end:
            continue
        url = resource.get('url')
        if url and 'MARCA' in label and 'MODELO' in label:
            candidates.setdefault(reference, {})[url] = resource
        if url and 'TIPO' in label and 'ESPECIE' in label and 'EIX' in label:
            heavy_candidates.setdefault(reference,{})[url]=resource
    with db.engine.connect() as c:
        completed = set(c.execute(select(p.content.c.reference).join(p.classification,
            p.classification.c.content_id == p.content.c.id).where(p.classification.c.status == 'complete')).scalars())
        pending_jobs = {(row['kind'], (row['payload'] or {}).get('reference')) for row in c.execute(
            select(p.job.c.kind,p.job.c.payload).where(p.job.c.status.in_(['queued','running']))).mappings()}
    queued, ambiguous = [], []
    key = discovery_key or datetime.now(ZoneInfo('America/Sao_Paulo')).strftime('%G-W%V')
    for reference, urls in sorted(candidates.items()):
        if len(urls) != 1:
            ambiguous.append({'reference': reference, 'urls': sorted(urls), 'reason': 'Mais de um arquivo possível para o mesmo recorte'})
            continue
        if reference in completed and not refresh:
            continue
        if ('import_remote',reference) in pending_jobs:
            continue
        resource = next(iter(urls.values()))
        item = enqueue('import_remote', {'reference': reference, 'url': resource['url'],
                       'resource_id': str(resource.get('id') or digest(resource['url']))},
                       f'senatran:{reference}:{digest(resource["url"])}:{key}')
        queued.append(item['id'])
    with db.engine.connect() as c:
        heavy_completed=set(c.execute(select(s.source_snapshot.c.reference_period).where(
            s.source_snapshot.c.provider==PROVIDER,s.source_snapshot.c.dataset_name=='tipo-especie-eixos',
            s.source_snapshot.c.ingestion_status=='success')).scalars())
    for reference,urls in sorted(heavy_candidates.items()):
        if len(urls)!=1:
            ambiguous.append({'reference':reference,'urls':sorted(urls),'reason':'Mais de um arquivo tipo/espécie/eixos para o mesmo recorte'})
            continue
        if reference in heavy_completed and not refresh:continue
        if ('heavy_stats_remote',reference) in pending_jobs:continue
        resource=next(iter(urls.values()))
        item=enqueue('heavy_stats_remote',{'reference':reference,'url':resource['url'],
            'resource_id':str(resource.get('id') or digest(resource['url']))},
            f'senatran-heavy:{reference}:{digest(resource["url"])}:{key}')
        queued.append(item['id'])
    year, month = map(int, start.split('-'))
    missing = []
    while f'{year:04d}-{month:02d}' <= end:
        reference = f'{year:04d}-{month:02d}'
        if reference not in candidates and reference not in completed:
            missing.append(reference)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    if not candidates and not completed:
        raise PermanentError('Nenhum arquivo de marca/modelo com competência inequívoca foi descoberto')
    return {'queued_jobs': queued, 'missing_references': missing, 'ambiguous_resources': ambiguous,
            'available_references': sorted(candidates),'heavy_stat_references':sorted(heavy_candidates),'checked_at': timestamp()}


def backfill(start='2026-01', end=None, idempotency_key=None):
    return enqueue('discover', {'start': start, 'end': end},
                   idempotency_key or 'backfill:' + timestamp())


def schedule_due(at=None):
    catalog_job = None
    if os.getenv('I9_CATALOG_AUTO_REFRESH', 'true').lower() == 'true':
        day = (at or datetime.now(timezone.utc)).astimezone(ZoneInfo('America/Sao_Paulo')).date().isoformat()
        catalog_job = enqueue('catalog_refresh', {}, 'catalog-daily:' + day)['id']
    if os.getenv('I9_UPDATES_PAUSED','false').lower()=='true':
        return {'paused':True,'catalog_job':catalog_job,'reason':'Downloads automaticos pausados; consulta do catalogo independente.'}
    current = (at or datetime.now(timezone.utc)).astimezone(ZoneInfo('America/Sao_Paulo'))
    due = current if current.hour >= 2 else current - timedelta(days=1)
    day = due.date().isoformat()
    week = due.strftime('%G-W%V')
    # Monthly fleet files describe a closed month. During September, for
    # example, August is due and September is still in progress.
    closed_month = current.replace(day=1) - timedelta(days=1)
    expected_end = closed_month.strftime('%Y-%m')
    # The key includes the scheduling contract version. Older installations
    # used the current (still open) month in the payload, so reusing that key
    # after an upgrade would correctly be rejected as different content.
    daily_key = f'daily-discovery:closed-v1:{day}:{expected_end}'
    weekly_key = f'weekly-revisions:closed-v1:{week}:{expected_end}'
    daily = enqueue('discover', {'start': '2026-01', 'end': expected_end,
                    'refresh': False, 'discovery_key': week}, daily_key)
    weekly = enqueue('discover', {'start': '2026-01', 'end': expected_end,
                     'refresh': True, 'discovery_key': week}, weekly_key)
    return {'daily_job': daily['id'], 'weekly_job': weekly['id']}


def record_worker_health(state='idle', job_id=None):
    from src import operational_schema as operational
    current = datetime.now(ZoneInfo('America/Sao_Paulo'))
    next_check = current.replace(hour=2, minute=0, second=0, microsecond=0)
    if next_check <= current:
        next_check += timedelta(days=1)
    with db.engine.begin() as c:
        if c.dialect.name == 'postgresql':
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        else:
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        for key, value in [('worker', {'state': state, 'pid': os.getpid(), 'hostname': socket.gethostname(),
                                      'status': state, 'last_seen_at': timestamp(), 'job_id': job_id, 'checked_at': timestamp()}),
                           ('next_check', {'scheduled_at': None if os.getenv('I9_CATALOG_AUTO_REFRESH','true').lower()!='true' and os.getenv('I9_UPDATES_PAUSED','false').lower()=='true' else timestamp(next_check), 'timezone': 'America/Sao_Paulo'})]:
            c.execute(dialect_insert(operational.setting).values(key=key, value=value, updated_at=timestamp())
                      .on_conflict_do_update(index_elements=['key'], set_={'value': value, 'updated_at': timestamp()}))


def _download(job, lease):
    """Bounded HTTP ranges, each persisted with validator and fenced checkpoint."""
    if os.getenv('ALLOW_SOURCE_DOWNLOAD','false').lower()!='true':
        raise PermanentError('Download desabilitado; configure ALLOW_SOURCE_DOWNLOAD=true')
    from urllib.parse import urlparse,urljoin
    url=job['payload']['url']
    def allowed(target):
        parsed=urlparse(target)
        return parsed.scheme=='https' and (parsed.hostname or '').endswith('.gov.br')
    if not allowed(url):raise PermanentError('Download exige domínio oficial gov.br e HTTPS')
    folder=db.ROOT/'data/raw/pipeline';folder.mkdir(parents=True,exist_ok=True)
    assembled=folder/f'{lease["job_id"]}-{lease["token"]}.part'
    maximum=int(os.getenv('I9_MAX_DOWNLOAD_BYTES','500000000'))
    # The official type/species/axles spreadsheets are slightly larger than
    # 8 MiB and gov.br currently omits ETag/Last-Modified. Reading each one in
    # a single bounded response prevents mixing bytes from different versions.
    # Large fleet archives keep the smaller resumable chunks and still require
    # a remote validator between parts.
    chunk_size = maximum if job['kind'] == 'official_report_remote' else (16 if job['kind'] == 'heavy_stats_remote' else 8) * 1024 * 1024
    total=None;validator=None;offset=0;index=0;hasher=hashlib.sha256()
    try:
        with httpx.Client(timeout=90,follow_redirects=False) as client,assembled.open('wb') as target:
            while total is None or offset<total:
                with job_transaction(lease,'download') as c:prior=_block_result(c,lease,'download_part',index)
                if prior:
                    piece=Path(prior['path'])
                    if not piece.is_file():raise PermanentError('Bloco baixado ausente; verifique armazenamento')
                    with piece.open('rb') as stream:
                        if hashlib.file_digest(stream,'sha256').hexdigest()!=prior['sha256']:raise PermanentError('Bloco baixado corrompido')
                    total=prior['total'];validator=prior['validator'];received=prior['bytes']
                else:
                    piece=folder/f'{lease["job_id"]}-{lease["token"]}-{index}.chunk'
                    headers={'Range':f'bytes={offset}-{min(offset+chunk_size,total or maximum)-1}','Accept-Encoding':'identity'}
                    if validator:headers['If-Range']=validator
                    for redirects in range(6):
                        with client.stream('GET',url,headers=headers) as response:
                            if response.is_redirect:
                                url=urljoin(url,response.headers['location'])
                                if not allowed(url):raise PermanentError('Redirecionamento não autorizado')
                                continue
                            response.raise_for_status()
                            content_range=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('content-range',''))
                            current_validator=response.headers.get('etag') or response.headers.get('last-modified')
                            if validator and current_validator!=validator:raise PermanentError('Arquivo remoto mudou durante o download; inicie nova verificação')
                            validator=current_validator
                            if response.status_code==206:
                                if not content_range or int(content_range[1])!=offset:raise PermanentError('Intervalo HTTP inconsistente')
                                expected=int(content_range[2])-offset+1;new_total=int(content_range[3])
                                if expected>chunk_size or expected<=0 or (total is not None and new_total!=total):raise PermanentError('Tamanho de intervalo inconsistente')
                                total=new_total
                                # Some official gov.br spreadsheets omit ETag and
                                # Last-Modified. Accept that only when the entire
                                # file fits in this single response; resumable
                                # multi-part downloads still require a validator.
                                if not validator and not (offset==0 and total<=chunk_size):
                                    raise PermanentError('Origem não fornece versão para download segmentado')
                            elif response.status_code==200 and offset==0:
                                expected=int(response.headers.get('content-length','0'));total=expected
                            else:raise PermanentError('Origem recusou a retomada do download')
                            if not total or total>maximum:raise PermanentError('Tamanho do download inválido ou acima do limite')
                            received=0;part_hash=hashlib.sha256()
                            with piece.open('wb') as output:
                                for chunk in response.iter_bytes(1024*1024):
                                    received+=len(chunk)
                                    if received>expected:raise PermanentError('Resposta excedeu intervalo solicitado')
                                    output.write(chunk);part_hash.update(chunk)
                            if received!=expected:raise OSError('Download incompleto; bloco será repetido')
                            with job_transaction(lease,'download') as c:
                                _commit_block(c,lease,'download_part',index,{'path':str(piece),'sha256':part_hash.hexdigest(),'bytes':received,'total':total,'validator':validator})
                                c.execute(update(p.job).where(p.job.c.id==lease['job_id']).values(progress={'downloaded_bytes':offset+received,'download_bytes':total}))
                            break
                    else:raise PermanentError('Excesso de redirecionamentos')
                with piece.open('rb') as stream:
                    for chunk in iter(lambda:stream.read(1024*1024),b''):
                        target.write(chunk);hasher.update(chunk)
                offset+=received;index+=1
        with job_transaction(lease,'downloaded'):
            path=folder/(hasher.hexdigest()+'.raw')
            if path.exists():
                with path.open('rb') as stream:
                    if hashlib.file_digest(stream,'sha256').hexdigest()!=hasher.hexdigest():raise PermanentError('Original preservado corrompido')
            else:assembled.replace(path)
        return path
    finally:assembled.unlink(missing_ok=True)


def _execute(lease):
    job = get_job(lease['job_id'])
    payload = job['payload']
    if job['kind'] == 'catalog_refresh':
        from src.senatran_months import refresh_catalog
        return refresh_catalog()
    if job['kind'] == 'discover':
        # Older installations may still have scheduled discovery jobs queued.
        # In desktop mode those jobs must only refresh the catalog.
        if os.getenv('I9_ON_DEMAND_WORKER', 'false').lower() == 'true':
            from src.senatran_months import refresh_catalog
            return refresh_catalog()
        return discover(start=payload.get('start') or '2026-01', end=payload.get('end'),
                        refresh=bool(payload.get('refresh')), discovery_key=payload.get('discovery_key'))
    if job['kind'] == 'materialize':
        return _materialize(payload['source_snapshot_id'], lease)
    if job['kind'] in {'heavy_stats_file','heavy_stats_remote'}:
        path=_download(job,lease) if job['kind']=='heavy_stats_remote' else Path(payload['path'])
        from src.heavy_statistics import import_type_species_axles
        return import_type_species_axles(path,payload['reference'],lease,payload.get('url'))
    if job['kind'] == 'official_report_remote':
        from src.senatran_reports import import_report
        return import_report(_download(job, lease), payload, lease)
    if job['kind'] == 'reclassify':
        content_id = payload['content_id']
        with db.engine.connect() as c:
            reference = c.execute(select(p.content.c.reference).where(p.content.c.id == content_id)).scalar_one_or_none()
        if reference != payload['reference']:
            raise PermanentError('Conteúdo não pertence à competência solicitada')
        with db.engine.connect() as c:
            old_artifact=c.execute(select(p.artifact).join(p.content,p.content.c.artifact_id==p.artifact.c.id).where(p.content.c.id==content_id)).mappings().one()
        old_path=Path(old_artifact['path'])
        if old_path.is_file():
            with old_path.open('rb') as original:
                if hashlib.file_digest(original,'sha256').hexdigest()!=old_artifact['raw_sha256']:
                    raise PermanentError('Original alterado; reclassificação bloqueada para preservar a procedência')
            path=old_path
        elif old_artifact.get('url'):
            path=_download({**job,'kind':'import_remote','payload':{'url':old_artifact['url']}},lease)
            with Path(path).open('rb') as original:
                if hashlib.file_digest(original,'sha256').hexdigest()!=old_artifact['raw_sha256']:
                    raise PermanentError('Fonte mudou; reclassificação requer nova revisão do mês')
        else:raise PermanentError('Original indisponível e sem URL; informe uma nova importação')
        artifact=register_artifact(path,reference,lease,old_artifact.get('url'))
        content_id=parse_artifact(artifact['id'],lease)
    else:
        with job_transaction(lease, 'download') as c:
            checkpoint = _block_result(c, lease, 'artifact', 0)
        if checkpoint:
            artifact_id = checkpoint['artifact_id']
        else:
            path = _download(job, lease) if job['kind'] == 'import_remote' else Path(payload['path'])
            artifact = register_artifact(path, payload['reference'], lease, payload.get('url'))
            artifact_id = artifact['id']
            with job_transaction(lease) as c:
                _commit_block(c, lease, 'artifact', 0, {'artifact_id': artifact_id})
        content_id = parse_artifact(artifact_id, lease)
    source_id, equivalent = classify_content(content_id, lease)
    with db.engine.connect() as c:
        completed = c.execute(select(s.fleet_publication.c.status).where(s.fleet_publication.c.source_snapshot_id == source_id)).scalar_one_or_none()
    if equivalent and completed == 'complete':
        return {'source_snapshot_id': source_id, 'content_id': content_id, 'equivalent': True, 'status': 'unchanged'}
    # A failed earlier attempt may have built this identical source under an old
    # generation. Reaffirm it only when the current job is the selected intent.
    with job_transaction(lease) as c:
        if desired_generation(c, payload['reference']) == job['generation']:
            source = c.execute(select(s.source_snapshot).where(s.source_snapshot.c.id == source_id).with_for_update()).mappings().one()
            metadata = dict(source['metadata'] or {})
            metadata.update(pipeline_generation=job['generation'], pipeline_job_id=job['id'])
            c.execute(update(s.source_snapshot).where(s.source_snapshot.c.id == source_id).values(metadata=metadata))
    publication = _materialize(source_id, lease)
    if publication.get('status') == 'complete':
        with db.engine.begin() as c:
            path=c.execute(select(p.artifact.c.path).join(p.content,p.content.c.artifact_id==p.artifact.c.id)
                .where(p.content.c.id==content_id)).scalar_one()
            c.execute(update(s.source_snapshot).where(s.source_snapshot.c.id==source_id).values(raw_storage_path=None))
        target=Path(path).resolve()
        if target.is_relative_to((db.ROOT/'data/raw/pipeline').resolve()):target.unlink(missing_ok=True)
    return {'source_snapshot_id': source_id, 'content_id': content_id, 'equivalent': equivalent, 'publication': publication}


def _materialize(source_id, lease):
    from src.fleet_publication import materialize
    while True:
        publication = materialize(source_id, batch_size=1000, complete=False, lease=lease)
        with job_transaction(lease, 'vehicles') as c:
            c.execute(update(p.job).where(p.job.c.id == lease['job_id']).values(progress={
                'publication_id': publication['id'], 'materialized_units': publication['materialized_units'],
                'planned_units': publication['planned_units']}))
        if publication['status'] in {'complete', 'superseded'}:
            return publication


def run_once(owner=None, job_id=None):
    lease = claim(owner, job_id=job_id)
    if not lease:
        return None
    stopped = threading.Event()
    lost = []
    def renew():
        while not stopped.wait(30):
            try:
                heartbeat(lease)
                record_worker_health('running', lease['job_id'])
            except Exception as exc:
                lost.append(exc)
                return
    renewer = threading.Thread(target=renew, daemon=True, name='i9-pipeline-heartbeat')
    renewer.start()
    try:
        record_worker_health('running', lease['job_id'])
        result = _execute(lease)
        if lost:
            raise LeaseLost(str(lost[0]))
        finish(lease, result)
    except LeaseLost:
        # No attempt to mark another worker's job failed.
        pass
    except Exception as exc:
        try:
            fail(lease, exc)
        except LeaseLost:
            pass
    finally:
        stopped.set()
        renewer.join(timeout=5)
    return get_job(lease['job_id'])
