"""Pipeline invariants: real persisted blocks, source equivalence and fencing."""
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update, func
from src import db, pipeline as q, pipeline_schema as p, intelligence_schema as s, operational_schema as o


@pytest.fixture
def queue(database, monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'ROOT', tmp_path)
    q.initialize()
    return database


HEADER = 'UF;MUNICIPIO;MARCA MODELO;ANO FABRICACAO;QUANTIDADE\n'
ROWS = ['PR;Curitiba;I/BYD DOLPHIN GS;2026;90\n',
        'SP;São Paulo;I/TOYOTA COROLLA;2025;100\n']


def source_file(tmp_path, name, rows=ROWS):
    path = tmp_path / name
    path.write_text(HEADER + ''.join(rows), encoding='utf-8')
    return path


def parse(queue, path, reference='2026-01', key=None):
    job = q.enqueue('import_file', {'path': str(path), 'reference': reference}, key or path.name + reference)
    lease = q.claim('test')
    assert lease['job_id'] == job['id']
    artifact = q.register_artifact(path, reference, lease)
    content = q.parse_artifact(artifact['id'], lease)
    return job, lease, artifact, content


def test_semantic_hash_ignores_order_zip_metadata_but_preserves_multiplicity(queue, tmp_path):
    a = source_file(tmp_path, 'a.csv')
    _, lease, artifact1, content1 = parse(queue, a)
    q.finish(lease, {'parsed': True})
    zipped = tmp_path / 'reordered.zip'
    with zipfile.ZipFile(zipped, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('reordered.csv', HEADER + ''.join(reversed(ROWS)))
    _, lease, artifact2, content2 = parse(queue, zipped)
    assert artifact1['raw_sha256'] != artifact2['raw_sha256']
    assert content1 == content2
    q.finish(lease, {'parsed': True})
    b = source_file(tmp_path, 'duplicated.csv', ROWS + [ROWS[0]])
    _, lease, _, content3 = parse(queue, b)
    assert content3 != content1
    q.finish(lease, {'parsed': True})
    _, lease, _, content4 = parse(queue, a, '2026-02')
    assert content4 != content1


def test_same_rows_get_new_content_when_classifier_rules_change(queue, tmp_path, monkeypatch):
    from src import auto_classification
    path=source_file(tmp_path, 'classifier.csv')
    _, first_lease, _, first=parse(queue,path)
    q.finish(first_lease, {'parsed':True})
    monkeypatch.setattr(auto_classification,'VERSION','test-classifier-next')
    _, second_lease, artifact, second=parse(queue,path,key='classifier-next')
    assert second != first
    with queue.connect() as c:
        assert c.execute(select(p.content.c.artifact_id).where(p.content.c.id==second)).scalar_one()==artifact['id']
    q.finish(second_lease, {'parsed':True})


def test_idempotency_rejects_reused_key_different_payload(queue):
    first = q.enqueue('discover', {'start': '2026-01'}, 'one')
    assert q.enqueue('discover', {'start': '2026-01'}, 'one')['id'] == first['id']
    with pytest.raises(q.Conflict):
        q.enqueue('discover', {'start': '2026-02'}, 'one')


def test_targeted_claim_does_not_take_another_month(queue):
    january = q.enqueue('discover', {'start': '2026-01'}, 'target-january')
    february = q.enqueue('discover', {'start': '2026-02'}, 'target-february')
    lease = q.claim('targeted-worker', job_id=february['id'])
    assert lease['job_id'] == february['id']
    assert q.get_job(january['id'])['status'] == 'queued'


def test_expired_worker_cannot_commit_after_reclaim(queue):
    q.enqueue('discover', {}, 'lease')
    first = q.claim('first')
    with queue.begin() as c:
        c.execute(update(p.job).where(p.job.c.id == first['job_id']).values(
            lease_until=q.timestamp(datetime.now(timezone.utc) - timedelta(seconds=1))))
    second = q.claim('second')
    assert second['token'] == first['token'] + 1
    with pytest.raises(q.LeaseLost):
        with q.job_transaction(first) as c:
            q._commit_block(c, first, 'test', 0, {'bad': True})
    q.finish(second, {'ok': True})
    assert q.get_job(second['job_id'])['attempts'][0]['status'] == 'expired'


def test_restore_epoch_fences_all_pre_restore_tokens(queue):
    q.enqueue('discover', {}, 'restore')
    before = q.claim('old')
    q.reset_epoch()
    with pytest.raises(q.LeaseLost):
        q.heartbeat(before)
    after = q.claim('new')
    assert after['epoch'] != before['epoch']
    q.finish(after, {'ok': True})


def test_failure_before_commit_rolls_back_rows_and_checkpoint(queue, tmp_path, monkeypatch):
    path = source_file(tmp_path, 'rollback.csv')
    q.enqueue('import_file', {'path': str(path), 'reference': '2026-01'}, 'rollback')
    lease = q.claim('test')
    artifact = q.register_artifact(path, '2026-01', lease)
    original = q._commit_block
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('crash before transaction commit')
    monkeypatch.setattr(q, '_commit_block', interrupted)
    with pytest.raises(RuntimeError):
        q.parse_artifact(artifact['id'], lease)
    with queue.connect() as c:
        assert c.execute(select(func.count()).select_from(p.observation)).scalar_one() == 0
        assert c.execute(select(func.count()).select_from(p.block)).scalar_one() == 0
    monkeypatch.setattr(q, '_commit_block', original)
    q.parse_artifact(artifact['id'], lease)
    with queue.connect() as c:
        assert c.execute(select(func.count()).select_from(p.observation)).scalar_one() == 1


def test_parse_retries_after_committed_block_without_duplicate(queue, tmp_path, monkeypatch):
    path = source_file(tmp_path, 'resume.csv', [ROWS[0]] * 1001)
    q.enqueue('import_file', {'path': str(path), 'reference': '2026-01'}, 'resume')
    lease = q.claim('test')
    artifact = q.register_artifact(path, '2026-01', lease)
    original = q._persist_filtered_chunk
    def response_lost(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('committed, response lost')
    monkeypatch.setattr(q, '_persist_filtered_chunk', response_lost)
    with pytest.raises(RuntimeError):
        q.parse_artifact(artifact['id'], lease)
    monkeypatch.setattr(q, '_persist_filtered_chunk', original)
    q.parse_artifact(artifact['id'], lease)
    with queue.connect() as c:
        assert c.execute(select(func.count()).select_from(p.observation)).scalar_one() == 1001
        assert c.execute(select(func.count()).select_from(p.block)).scalar_one() == 1


def test_classification_uses_known_model_family_category(queue, tmp_path):
    path = source_file(tmp_path, 'classification.csv')
    _, lease, _, content = parse(queue, path)
    source_id, equivalent = q.classify_content(content, lease)
    assert not equivalent
    with queue.connect() as c:
        source = c.execute(select(s.source_snapshot).where(s.source_snapshot.c.id == source_id)).mappings().one()
        rows = c.execute(select(s.aggregate).where(s.aggregate.c.source_snapshot_id == source_id)).mappings().all()
        assert source['metadata']['recognized_quantity'] == 90
        assert source['metadata']['pending_quantity'] == 0
        assert len(rows) == 1
        assert rows[0]['vehicle_category'] == 'car'
    assert q.pending_reviews() == []  # No electrification hint, so not surfaced.


def test_reclassification_rereads_preserved_original_and_freezes_rules(queue, tmp_path):
    path = source_file(tmp_path, 'rules.csv')
    _, lease, _, content = parse(queue, path)
    first, _ = q.classify_content(content, lease)
    q.finish(lease, {})
    q.approve_rule('I/TOYOTA COROLLA', 'exclude', 'Nome mistura motorizações; decisão explícita de exclusão', 'test')
    q.enqueue('reclassify', {'content_id': content, 'reference': '2026-01'}, 'reclassification')
    lease = q.claim('test')
    artifact = q.register_artifact(path, '2026-01', lease)
    revised_content = q.parse_artifact(artifact['id'], lease)
    second, _ = q.classify_content(revised_content, lease)
    assert first != second
    with queue.connect() as c:
        assert c.execute(select(func.count()).select_from(p.artifact)).scalar_one() == 2
        assert c.execute(select(func.count()).select_from(p.observation)).scalar_one() == 2
        assert c.execute(select(s.source_snapshot.c.metadata).where(s.source_snapshot.c.id == second)).scalar_one()['pending_quantity'] == 0


def test_discovery_records_missing_and_ambiguous_months(queue):
    resources = [
        {'name': 'Marca modelo janeiro 2026', 'url': 'https://www.gov.br/a/janeiro-2026.zip'},
        {'name': 'Marca modelo fevereiro 2026', 'url': 'https://www.gov.br/a/fevereiro-2026.zip'},
        {'name': 'Marca modelo fevereiro 2026', 'url': 'https://www.gov.br/b/fevereiro-2026.zip'},
        {'name': 'Combustível março 2026', 'url': 'https://www.gov.br/c/marco-2026.zip'},
    ]
    result = q.discover(resources, end='2026-03', discovery_key='fixture')
    assert len(result['queued_jobs']) == 1
    assert result['ambiguous_resources'][0]['reference'] == '2026-02'
    assert result['missing_references'] == ['2026-03']
    assert q.discover(resources, end='2026-03', discovery_key='fixture')['queued_jobs'] == []


def test_discovery_does_not_queue_duplicate_work_for_same_month(queue):
    resources=[{'name':'Marca modelo janeiro 2026','url':'https://www.gov.br/a/janeiro-2026.zip'}]
    first=q.discover(resources,end='2026-01',discovery_key='first')
    second=q.discover(resources,end='2026-01',discovery_key='second')
    assert len(first['queued_jobs'])==1
    assert second['queued_jobs']==[]


def test_scheduler_only_expects_closed_months(queue):
    from datetime import datetime,timezone
    at=datetime(2026,9,24,12,tzinfo=timezone.utc)
    result=q.schedule_due(at)
    daily=q.get_job(result['daily_job'])
    weekly=q.get_job(result['weekly_job'])
    assert daily['payload']['end']=='2026-08'
    assert weekly['payload']['end']=='2026-08'
    assert q.schedule_due(at)==result


def test_reference_generations_allocated_at_enqueue_not_completion(queue):
    first = q.enqueue('import_file', {'path': 'old.csv', 'reference': '2026-01'}, 'old')
    second = q.enqueue('import_file', {'path': 'new.csv', 'reference': '2026-01'}, 'new')
    assert second['generation'] == first['generation'] + 1
    assert q.enqueue('import_file', {'path': 'old.csv', 'reference': '2026-01'}, 'old')['generation'] == first['generation']


def test_csv_invalid_quantity_and_missing_columns_block_publication(queue, tmp_path):
    path = source_file(tmp_path, 'invalid.csv', ['PR;Curitiba;I/BYD DOLPHIN;2026;-1\n'])
    with pytest.raises(q.PermanentError, match='Quantidade'):
        list(q.iter_normalized(path, '2026-01'))
    path.write_text('UF;QUANTIDADE\nPR;4\n', encoding='utf-8')
    with pytest.raises(q.PermanentError, match='Colunas'):
        list(q.iter_normalized(path, '2026-01'))


def test_download_resume_uses_persisted_ranges(queue,tmp_path,monkeypatch):
    import httpx,hashlib
    monkeypatch.setenv('ALLOW_SOURCE_DOWNLOAD','true')
    content=b'A'*(8*1024*1024)+b'last bytes'
    requests=[];fail=[True];client=httpx.Client
    def serve(request):
        start,end=map(int,request.headers['Range'].split('=')[1].split('-'))
        requests.append(start)
        if start and fail[0]:
            fail[0]=False
            raise httpx.ConnectError('temporary failure')
        end=min(end,len(content)-1)
        return httpx.Response(206,headers={'etag':'version-1','content-range':f'bytes {start}-{end}/{len(content)}'},content=content[start:end+1])
    monkeypatch.setattr(q.httpx,'Client',lambda **kwargs:client(transport=httpx.MockTransport(serve),**kwargs))
    job=q.enqueue('import_remote',{'reference':'2026-01','url':'https://dados.gov.br/source.zip'},'download-resume')
    lease=q.claim('download-test')
    with pytest.raises(httpx.ConnectError):q._download(job,lease)
    path=q._download(job,lease)
    assert requests.count(0)==1
    assert hashlib.sha256(path.read_bytes()).digest()==hashlib.sha256(content).digest()


def test_small_heavy_spreadsheet_without_validator_uses_one_bounded_response(queue,tmp_path,monkeypatch):
    import httpx,hashlib
    monkeypatch.setenv('ALLOW_SOURCE_DOWNLOAD','true')
    content=b'X'*(9*1024*1024)
    requests=[];client=httpx.Client
    def serve(request):
        start,end=map(int,request.headers['Range'].split('=')[1].split('-'))
        requests.append((start,end))
        end=min(end,len(content)-1)
        return httpx.Response(206,headers={'content-range':f'bytes {start}-{end}/{len(content)}'},content=content[start:end+1])
    monkeypatch.setattr(q.httpx,'Client',lambda **kwargs:client(transport=httpx.MockTransport(serve),**kwargs))
    job=q.enqueue('heavy_stats_remote',{'reference':'2026-01','url':'https://dados.gov.br/source.xlsx'},'heavy-single-response')
    lease=q.claim('download-heavy-test')
    path=q._download(job,lease)
    assert requests==[(0,16*1024*1024-1)]
    assert hashlib.sha256(path.read_bytes()).digest()==hashlib.sha256(content).digest()


def test_only_recognized_rows_stored_and_approved_model_recovered(queue,tmp_path):
    path=source_file(tmp_path,'filtered.csv',[ROWS[0],'PR;Curitiba;MARCA TESTE EV;2026;5\n'])
    _,lease,artifact,content=parse(queue,path)
    q.classify_content(content,lease);q.finish(lease,{})
    with queue.connect() as c:
        assert c.execute(select(func.count()).select_from(p.observation)).scalar_one()==1
        assert c.execute(select(func.sum(p.filtered_summary.c.quantity))).scalar_one()==0
    q.approve_rule('MARCA TESTE EV','include','Evidência de teste','tester','Marca','Teste EV','BEV','bus')
    q.enqueue('reclassify',{'content_id':content,'reference':'2026-01'},'recover-model')
    result=q.run_once()
    assert result['status']=='complete',result['error']
    with queue.connect() as c:
        assert c.execute(select(func.sum(s.aggregate.c.quantity)).where(s.aggregate.c.source_snapshot_id==result['result']['source_snapshot_id'])).scalar_one()==95
        assert c.execute(select(func.count()).select_from(p.artifact)).scalar_one()==2


def test_heavy_approval_creates_versioned_semantic_alias(queue):
    rule=q.approve_rule('M.BENZ/EO500U','include','Ficha oficial Mercedes-Benz','tester',
        'Mercedes-Benz','eO500U','BEV','bus','urban_bus','Ônibus urbano','chassis',0)
    assert rule['category']=='bus' and rule['segment']=='Ônibus urbano'
    with queue.connect() as c:
        model=c.execute(select(o.model)).mappings().one()
        alias=c.execute(select(o.semantic_alias)).mappings().one()
        assert model['is_heavy_vehicle'] is True and model['is_electric'] is True
        assert alias['model_id']==model['id'] and alias['approved'] is True


def test_type_species_axles_is_independent_and_resumable(queue,tmp_path):
    path=tmp_path/'type-species.csv'
    path.write_text('UF;MUNICIPIO;TIPO VEICULO;ESPECIE;EIXOS;QUANTIDADE\nPR;Curitiba;ONIBUS;PASSAGEIRO;2;8\nPR;Curitiba;AUTOMOVEL;PASSAGEIRO;2;100\n',encoding='utf-8')
    q.enqueue('heavy_stats_file',{'path':str(path),'reference':'2026-01'},'heavy-file')
    result=q.run_once('heavy-test')
    assert result['status']=='complete',result.get('error')
    with queue.connect() as c:
        rows=c.execute(select(o.heavy_stat)).mappings().all()
        assert rows==[]  # This report cannot distinguish electric from combustion buses.
        assert c.execute(select(func.count()).select_from(s.aggregate)).scalar_one()==0


def test_preserved_original_corruption_blocks_reclassification(queue,tmp_path):
    from pathlib import Path
    _,lease,artifact,content=parse(queue,source_file(tmp_path,'original.csv'))
    q.classify_content(content,lease);q.finish(lease,{})
    Path(artifact['path']).write_bytes(b'changed')
    q.enqueue('reclassify',{'content_id':content,'reference':'2026-01'},'corrupt-original')
    result=q.run_once()
    assert result['status']=='failed'
    assert 'Original alterado' in result['error']
