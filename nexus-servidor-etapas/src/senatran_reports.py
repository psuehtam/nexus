"""Import official workbooks without inferring vehicle identities from aggregates."""
import hashlib
import json
import os
import zipfile
from itertools import islice
from pathlib import Path
from openpyxl import load_workbook
from sqlalchemy import select
from fastapi import HTTPException
from src import db, intelligence_schema as s, pipeline as p


def import_report(path, payload, lease):
    path = Path(path)
    with path.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    with zipfile.ZipFile(path) as archive:
        if 'xl/workbook.xml' not in archive.namelist() or sum(x.file_size for x in archive.infolist()) > 8_000_000_000:
            raise p.PermanentError('Planilha oficial inválida ou acima do limite de 8 GB descompactados.')
    source_id = p.stable('official-report:' + payload['reference'] + ':' + payload['report_id'] + ':' + checksum)
    with db.engine.connect() as c:
        prior = c.execute(select(s.source_snapshot).where(s.source_snapshot.c.id == source_id)).mappings().first()
        if prior and prior['ingestion_status'] == 'success' and Path(prior['metadata']['rows_path']).is_file():
            return {'source_snapshot_id': source_id, 'rows': prior['record_count']}
    folder = db.ROOT / 'data/processed/senatran'
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / (source_id + '.jsonl')
    temporary = folder / (source_id + f'.{lease["job_id"]}.{lease["token"]}.part')
    count = 0
    scanned = 0
    fuel_report = 'COMBUSTIVEL' in p.normalize(payload['report_name'])
    sheets = []
    try:
        with path.open('rb') as stream, temporary.open('w', encoding='utf-8') as output:
            workbook = load_workbook(stream, read_only=True, data_only=True)
            try:
                for sheet in workbook.worksheets:
                    start = count
                    fuel_index = None
                    for row_number, values in enumerate(sheet.iter_rows(values_only=True), 1):
                        if not any(value is not None for value in values):
                            continue
                        scanned += 1
                        if fuel_report and fuel_index is None:
                            names = [p.normalize(str(value or '')) for value in values]
                            fuel_index = names.index('COMBUSTIVEL VEICULO') if 'COMBUSTIVEL VEICULO' in names else None
                            if fuel_index is None:
                                continue
                        elif not fuel_report or fuel_index is None or fuel_index >= len(values):
                            continue
                        elif 'ELETRIC' not in p.normalize(str(values[fuel_index] or '')) and 'HIBRIDO' not in p.normalize(str(values[fuel_index] or '')):
                            continue
                        output.write(json.dumps({'sheet': sheet.title, 'row': row_number, 'values': values}, ensure_ascii=False, default=str) + '\n')
                        count += 1
                        if count > 5_000_000:
                            raise p.PermanentError('Planilha acima do limite de cinco milhões de linhas.')
                        if count % 10000 == 0:
                            with p.job_transaction(lease, 'report-parse') as c:
                                p._commit_block(c, lease, 'report-progress', count // 10000, {'scanned_rows': count})
                    sheets.append({'name': sheet.title, 'rows': count - start})
            finally:
                workbook.close()
        if not scanned:
            raise p.PermanentError('A planilha oficial não contém dados.')
        # Publish only after complete parsing and a valid lease. Existing versions stay readable.
        with p.job_transaction(lease, 'report-publish') as c:
            os.replace(temporary, destination)
            p._upsert_nothing(c, s.source_snapshot, dict(id=source_id, provider='SENATRAN',
                dataset_name='official-report:' + payload['report_id'], reference_period=payload['reference'],
                retrieved_at=p.timestamp(), method='official_workbook', raw_file_hash=checksum,
                raw_storage_path=None, source_url=payload['url'], ingestion_status='success',
                record_count=count, access_level='public', transformation_version='workbook-1',
                metadata={'name': payload['report_name'], 'rows_path': str(destination), 'sheets': sheets,
                          'scanned_rows':scanned,'scope':'electrified_fuel_only' if fuel_report else 'metadata_only',
                          'report_id': payload['report_id'], 'pipeline_job_id': lease['job_id']}))
        return {'source_snapshot_id': source_id, 'rows': count, 'report_name': payload['report_name']}
    finally:
        temporary.unlink(missing_ok=True)
        resolved=path.resolve()
        if resolved.is_relative_to((db.ROOT/'data/raw/pipeline').resolve()):resolved.unlink(missing_ok=True)


def reports(reference=None):
    with db.engine.connect() as c:
        query = select(s.source_snapshot).where(s.source_snapshot.c.method == 'official_workbook', s.source_snapshot.c.ingestion_status == 'success')
        if reference:
            query = query.where(s.source_snapshot.c.reference_period == reference)
        sources = c.execute(query.order_by(s.source_snapshot.c.retrieved_at.desc(), s.source_snapshot.c.id)).mappings()
        items = {}
        for row in sources:
            key = (row['reference_period'], row['dataset_name'])
            items.setdefault(key, {'id': row['id'], 'reference': row['reference_period'], 'name': row['metadata']['name'],
                                   'rows': row['record_count'], 'sheets': row['metadata']['sheets'], 'source_url': row['source_url']})
        return {'items': list(items.values())}


def report_rows(source_id, offset=0, limit=50, scope="all"):
    if scope not in {"all", "electrified", "electric"}:
        raise HTTPException(422, "Recorte inválido.")
    with db.engine.connect() as c:
        row = c.execute(select(s.source_snapshot).where(s.source_snapshot.c.id == source_id,
            s.source_snapshot.c.method == 'official_workbook', s.source_snapshot.c.ingestion_status == 'success')).mappings().first()
    if not row:
        raise HTTPException(404, 'Relatório não encontrado.')
    if (row['metadata'] or {}).get('scope') == 'metadata_only':
        return {'items': [], 'total': 0, 'scope': 'metadata_only',
                'note': 'Este relatório não separa elétricos e híbridos dos veículos a combustão.'}
    path = Path(row['metadata']['rows_path']).resolve()
    if not path.is_relative_to((db.ROOT / 'data/processed/senatran').resolve()) or not path.is_file():
        raise HTTPException(409, 'O arquivo importado não está disponível.')
    if scope != 'all':
        if 'COMBUSTIVEL' not in p.normalize(row['metadata']['name']):
            raise HTTPException(422, 'Este relatório não informa combustível; não é possível filtrar eletrificados.')
        items, total, quantity = [], 0, 0
        with path.open(encoding='utf-8') as stream:
            header = json.loads(next(stream))['values']
            normalized = [p.normalize(str(v or '')) for v in header]
            if 'COMBUSTIVEL VEICULO' not in normalized or 'QTD VEICULOS' not in normalized:
                raise HTTPException(422, 'Cabeçalho do relatório de combustível não reconhecido.')
            fuel_index, quantity_index = normalized.index('COMBUSTIVEL VEICULO'), normalized.index('QTD VEICULOS')
            for line in stream:
                item = json.loads(line)
                values = item['values']
                if len(values) <= max(fuel_index, quantity_index):
                    continue
                fuel = p.normalize(str(values[fuel_index] or ''))
                matches = fuel in {'ELETRICO', 'ELETRICO FONTE EXTERNA'} if scope == 'electric' else ('ELETRIC' in fuel or 'HIBRIDO' in fuel)
                if not matches:
                    continue
                if offset <= total < offset + limit:
                    items.append(item)
                total += 1
                if isinstance(values[quantity_index], (int, float)):
                    quantity += values[quantity_index]
        return {'items': items, 'total': total, 'total_quantity': quantity, 'columns': header, 'scope': scope}
    with path.open(encoding='utf-8') as stream:
        items = [json.loads(line) for line in islice(stream, offset, offset + limit)]
    return {'items': items, 'total': row['record_count']}
