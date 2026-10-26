"""Catalog discovery never downloads datasets. Imports require an explicit request."""
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from uuid import uuid4
import httpx
from sqlalchemy import select, update
from fastapi import HTTPException
from src import db, pipeline as p, pipeline_schema as ps, operational_schema as o, intelligence_schema as s
from src.fleet_publication import insert_once


def closed_month():
    return (datetime.now(ZoneInfo('America/Sao_Paulo')).replace(day=1) - timedelta(days=1)).strftime('%Y-%m')


def setting(c, key):
    return c.execute(select(o.setting.c.value).where(o.setting.c.key == key)).scalar() or {}


def save(c, key, value):
    insert_once(c, o.setting, dict(key=key, value=value, updated_at=p.timestamp()), ['key'])
    c.execute(update(o.setting).where(o.setting.c.key == key).values(value=value, updated_at=p.timestamp()))


def refresh_catalog(resources=None):
    try:
        resources = p.catalog_resources('2026-01', closed_month()) if resources is None else resources
        months = {}
        reports = {}
        for resource in resources:
            ref = p._resource_reference(resource)
            if not ref or ref > closed_month() or not resource.get('url'):
                continue
            if resource.get('report_only'):
                reports.setdefault(ref, {})[resource['url']] = {'name': resource['name'], 'url': resource['url']}
                continue
            label = p.normalize(' '.join(str(resource.get(k) or '') for k in ('name', 'description', 'url')))
            kind = 'import_remote' if 'MARCA' in label and 'MODELO' in label else 'heavy_stats_remote' if all(x in label for x in ('TIPO', 'ESPECIE', 'EIX')) else None
            if kind:
                group = months.setdefault(ref, {}).setdefault(kind, {})
                group[resource['url']] = resource
        with db.engine.begin() as c:
            old_catalog = setting(c, 'senatran_catalog')
            previous = old_catalog.get('months', {})
            for ref, groups in months.items():
                for kind, entries in list(groups.items()):
                    resolved = {url: item for url, item in entries.items() if not item.get('published_only')}
                    if resolved:
                        groups[kind] = resolved
                    elif getattr(resources, 'warning', None):
                        cached = {url: item for url, item in previous.get(ref, {}).get(kind, {}).items() if not item.get('published_only')}
                        if cached:
                            groups[kind] = cached
            # A partial source outage must not discard previously valid entries.
            if getattr(resources, 'warning', None):
                for ref, groups in previous.items():
                    months.setdefault(ref, groups)
                for ref, entries in old_catalog.get('reports', {}).items():
                    reports.setdefault(ref, entries)
            value = {'months': months, 'checked_at': p.timestamp(), 'last_error': None,
                     'source_warning': getattr(resources, 'warning', None), 'reports': reports}
            save(c, 'senatran_catalog', value)
        return {'references': sorted(months), 'checked_at': value['checked_at']}
    except Exception as exc:
        with db.engine.begin() as c:
            value = setting(c, 'senatran_catalog')
            value.update(last_error=catalog_error_message(exc), attempted_at=p.timestamp())
            save(c, 'senatran_catalog', value)
        raise


def request_refresh(startup=False):
    # Serialize across browser sessions/processes, including bucket boundaries.
    with db.engine.begin() as c:
        insert_once(c, o.setting, dict(key='senatran_refresh_lock', value={}, updated_at=p.timestamp()), ['key'])
        c.execute(select(o.setting).where(o.setting.c.key == 'senatran_refresh_lock').with_for_update()).first()
        pending = c.execute(select(ps.job).where(ps.job.c.kind == 'catalog_refresh',
            ps.job.c.status.in_(['queued', 'running'])).order_by(ps.job.c.created_at.desc()).limit(1)).mappings().first()
        if pending:
            return dict(pending)
        latest = c.execute(select(ps.job).where(ps.job.c.kind == 'catalog_refresh')
            .order_by(ps.job.c.created_at.desc()).limit(1)).mappings().first()
        if not startup and latest and latest['updated_at'] > p.timestamp(datetime.now(ZoneInfo('UTC')) - timedelta(seconds=60)):
            return dict(latest)
        return p.enqueue('catalog_refresh', {}, 'catalog-refresh:' + uuid4().hex, connection=c)


def retire_catalog_retries():
    """End legacy catalog retry waits; keep history and leave imports untouched."""
    with db.engine.begin() as c:
        c.execute(update(ps.job).where(ps.job.c.kind == 'catalog_refresh', ps.job.c.status == 'queued',
            ps.job.c.attempt_count > 0).values(status='failed', stage='failed', owner=None, lease_until=None, updated_at=p.timestamp()))


def catalog_error_message(error):
    if isinstance(error, httpx.HTTPStatusError):
        return f'O portal da Senatran não respondeu à consulta (HTTP {error.response.status_code}). Tente novamente mais tarde.'
    if isinstance(error, httpx.TransportError):
        return 'Não foi possível conectar ao portal da Senatran. Confira a conexão e tente novamente mais tarde.'
    return str(error)


def request_import(reference, refresh, key, dataset='all'):
    selected = {'all': None, 'primary': 'import_remote', 'complement': 'heavy_stats_remote'}
    if dataset not in selected:
        raise HTTPException(422, 'Relatório inválido.')
    if dataset == 'complement':
        raise HTTPException(422, 'Tipo/espécie/eixos não informa eletrificação e não pode ser guardado no recorte eletrificado.')
    try:
        p.validate_reference(reference)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if reference > closed_month():
        raise HTTPException(422, 'Escolha uma competencia encerrada.')
    if os.getenv('ALLOW_SOURCE_DOWNLOAD', 'false').lower() != 'true':
        raise HTTPException(409, 'Downloads desabilitados. Habilite ALLOW_SOURCE_DOWNLOAD na configuracao.')
    with db.engine.begin() as c:
        # A shared row serializes imports, including two different browser sessions.
        insert_once(c, o.setting, dict(key='senatran_import_lock', value={}, updated_at=p.timestamp()), ['key'])
        c.execute(select(o.setting).where(o.setting.c.key == 'senatran_import_lock').with_for_update()).first()
        logical = {'reference': reference, 'refresh': refresh}
        if dataset != 'all':
            logical['dataset'] = dataset
        command_key = 'senatran-request:' + p.digest(key)
        prior = setting(c, command_key)
        if prior:
            if prior['request'] != logical:
                raise HTTPException(409, 'Chave de idempotencia reutilizada com outra solicitacao.')
            return prior['result']
        catalog = setting(c, 'senatran_catalog')
        groups = catalog.get('months', {}).get(reference, {})
        groups = {kind: {url: resource for url, resource in entries.items() if not resource.get('published_only')}
                  for kind, entries in groups.items()}
        required = selected[dataset]
        if required and not groups.get(required):
            raise HTTPException(409, 'O arquivo deste relatório está indisponível. Atualize a lista.')
        groups = {kind: entries for kind, entries in groups.items() if entries and (dataset == 'all' or kind == required)}
        if dataset == 'all':
            groups = {kind:entries for kind,entries in groups.items() if kind == 'import_remote'}
        if dataset == 'all':
            for url, report in catalog.get('reports', {}).get(reference, {}).items():
                if 'COMBUSTIVEL' in p.normalize(report['name']):
                    groups['report:' + p.digest(url)] = {url: report}
        if not groups:
            raise HTTPException(409, 'Nenhum arquivo disponível para este mês. Atualize a lista.')
        if any(len(items) != 1 for items in groups.values()):
            raise HTTPException(409, 'Mais de um arquivo possivel: revisao da fonte necessaria.')
        jobs = list(c.execute(select(ps.job).where(ps.job.c.kind.in_(['import_remote', 'heavy_stats_remote', 'official_report_remote']))
                    .order_by(ps.job.c.created_at.desc(), ps.job.c.id)).mappings())
        publication_exists = c.execute(select(s.fleet_publication.c.id).where(s.fleet_publication.c.reference_period == reference,
                                        s.fleet_publication.c.status == 'complete').limit(1)).scalar()
        complement_exists = c.execute(select(s.source_snapshot.c.id).where(s.source_snapshot.c.reference_period == reference,
            s.source_snapshot.c.dataset_name == 'tipo-especie-eixos', s.source_snapshot.c.ingestion_status == 'success').limit(1)).scalar()
        ids = []
        for group, items in sorted(groups.items()):
            report_id = group.removeprefix('report:') if group.startswith('report:') else None
            kind = 'official_report_remote' if report_id else group
            same = [j for j in jobs if j['kind'] == kind and j['payload'].get('reference') == reference and j['payload'].get('report_id') == report_id]
            active = next((j for j in same if j['status'] in ('queued', 'running')), None)
            if active:
                ids.append(active['id'])
                continue
            if not refresh and same:
                # Failures stay visible and use the existing checkpoint-aware retry endpoint.
                ids.append(same[0]['id'])
                continue
            if not refresh and not report_id and (publication_exists if kind == 'import_remote' else complement_exists):
                continue
            resource = next(iter(items.values()))
            payload = {'reference': reference, 'url': resource['url'], 'resource_id': str(resource.get('id') or p.digest(resource['url'])),
                       'catalog_fingerprint': fingerprint(resource)}
            if report_id:
                payload.update(report_id=report_id, report_name=resource['name'])
            job = p.enqueue(kind, payload, f'month:{reference}:{group}:{p.digest(key)}', connection=c)
            ids.append(job['id'])
        result = {'reference': reference, 'job_ids': ids}
        save(c, command_key, {'request': logical, 'result': result})
        return result


def fingerprint(resource):
    # No download or HEAD probe; only evidence explicitly supplied by the catalog.
    return p.digest({k: resource.get(k) for k in ('url', 'hash', 'last_modified', 'size')})


def months():
    with db.engine.connect() as c:
        catalog = setting(c, 'senatran_catalog')
        check = c.execute(select(ps.job).where(ps.job.c.kind == 'catalog_refresh')
            .order_by(ps.job.c.created_at.desc()).limit(1)).mappings().first()
        jobs = list(c.execute(select(ps.job).where(ps.job.c.kind.in_(['import_remote', 'heavy_stats_remote', 'official_report_remote']))
                    .order_by(ps.job.c.created_at.desc(), ps.job.c.id)).mappings())
        installed = set(c.execute(select(s.fleet_publication.c.reference_period).where(s.fleet_publication.c.status == 'complete')).scalars())
    items = []
    for ref in sorted(set(catalog.get('months', {})) | set(catalog.get('reports', {})) | installed | {j['payload']['reference'] for j in jobs}, reverse=True):
        groups = catalog.get('months', {}).get(ref, {})
        datasets = []
        for kind in ('import_remote', 'heavy_stats_remote'):
            job = next((dict(j) for j in jobs if j['kind'] == kind and j['payload'].get('reference') == ref), None)
            resources = groups.get(kind, {})
            published = bool(resources)
            publication_url = next((item['publication_url'] for item in resources.values() if item.get('publication_url')), None)
            resources = {url: resource for url, resource in resources.items() if not resource.get('published_only')}
            revision = bool(job and job['status'] == 'complete' and len(resources) == 1 and job['payload'].get('catalog_fingerprint') and
                            fingerprint(next(iter(resources.values()))) != job['payload']['catalog_fingerprint'])
            datasets.append({'kind': kind, 'available': len(resources) == 1, 'ambiguous': len(resources) > 1,
                             'job': job, 'revision_available': revision, 'published': published, 'publication_url': publication_url})
        reports = []
        for url, report in catalog.get('reports', {}).get(ref, {}).items():
            report_id = p.digest(url)
            job = next((dict(j) for j in jobs if j['kind'] == 'official_report_remote' and j['payload'].get('reference') == ref and j['payload'].get('report_id') == report_id), None)
            reports.append({**report, 'kind': report_id, 'available': True, 'job': job})
        items.append({'reference': ref, 'installed': ref in installed, 'datasets': datasets,
                      'reports': reports})
    return {'items': items, 'checked_at': catalog.get('checked_at'), 'last_error': catalog.get('last_error'),
            'attempted_at': catalog.get('attempted_at'), 'refresh_status': check['status'] if check else None,
            'source_warning': catalog.get('source_warning')}
