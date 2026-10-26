"""Public API adapters. Catalog metadata is never reported as fleet records."""
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import httpx
from sqlalchemy import insert
from src.db import engine, probes, ROOT

CATALOG = 'https://dados.transportes.gov.br/api/3/action/package_show?id=registro-nacional-de-veiculos-automotores-renavam'
IBGE = 'https://servicodados.ibge.gov.br/api/v1/localidades/municipios?view=nivelado'
OPENEV = 'https://api.open-ev-data.org/v1/vehicles?limit=1'
GITHUB = 'https://api.github.com/repos/open-ev-data/open-ev-data-dataset/git/trees/main?recursive=1'

def now():
    return datetime.now(timezone.utc).isoformat()

def transfer_log(**entry):
    path=ROOT/'data/transfer-log.jsonl'
    with path.open('a',encoding='utf-8') as f:
        f.write(json.dumps({'recorded_at':now(),**entry},ensure_ascii=False)+'\n')

def get_json(url, client=None):
    if client is None:
        with httpx.Client(timeout=30, follow_redirects=True, headers={'User-Agent': 'i9-data/1.0'}) as c:
            return get_json(url, c)
    for attempt in range(3):
        started=time.perf_counter()
        try:
            r = client.get(url)
            r.raise_for_status()
            transfer_log(kind='api_json',url=url,status=r.status_code,payload_bytes=len(r.content),wire_body_bytes=r.num_bytes_downloaded,seconds=round(time.perf_counter()-started,3))
            return r.json()
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt == 2 or ('r' in locals() and r.status_code < 500 and r.status_code != 429):
                raise
            time.sleep(attempt + 1)

def catalog():
    payload = get_json(CATALOG)
    if not payload.get('success') or not isinstance(payload.get('result', {}).get('resources'), list):
        raise ValueError('Resposta do catálogo SENATRAN fora do contrato esperado')
    return payload['result']['resources']

def download(url, destination: Path, max_bytes=300_000_000):
    """Bounded-memory, atomic download. Never replaces a verified raw file."""
    started=time.perf_counter()
    if destination.exists() and destination.stat().st_size:
        digest=file_hash(destination)
        transfer_log(kind='file',url=url,path=str(destination.relative_to(ROOT)),reused=True,file_bytes=destination.stat().st_size,downloaded_bytes=0,seconds=round(time.perf_counter()-started,3),sha256=digest)
        return digest
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(destination.suffix + '.part')
    try:
        with httpx.stream('GET', url, timeout=90, follow_redirects=True) as r:
            r.raise_for_status()
            size = 0
            with temp.open('wb') as f:
                for block in r.iter_bytes(1024 * 1024):
                    size += len(block)
                    if size > max_bytes:
                        raise ValueError('Download excedeu limite configurado')
                    f.write(block)
        temp.replace(destination)
        digest=file_hash(destination)
        transfer_log(kind='file',url=url,path=str(destination.relative_to(ROOT)),reused=False,file_bytes=destination.stat().st_size,downloaded_bytes=r.num_bytes_downloaded,seconds=round(time.perf_counter()-started,3),sha256=digest)
        return digest
    except Exception as e:
        transfer_log(kind='file',url=url,path=str(destination),failed=True,seconds=round(time.perf_counter()-started,3),error=str(e))
        raise
    finally:
        temp.unlink(missing_ok=True)

def file_hash(path):
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def probe(name, url):
    started = time.monotonic()
    result = dict(name=name, url=url, checked_at=now(), status_code=None, ok=0, details={})
    try:
        with httpx.Client(timeout=25, follow_redirects=True) as c:
            r = c.get(url)
        result['status_code'] = r.status_code
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get('success') is False:
            raise ValueError(str(data.get('error')))
        result['ok'] = 1
        result['details'] = {'response_bytes': len(r.content)}
        if name == 'SENATRAN catálogo':
            resources = data['result']['resources']
            result['details'].update(resources=len(resources), datastore_active=sum(bool(x.get('datastore_active')) for x in resources), note='Metadados e links de arquivos; não é API de registros.')
        if name == 'IBGE municípios':
            result['details']['municipalities'] = len(data)
    except Exception as e:
        result['details'] = {'error': str(e)[:800]}
    result['duration_ms'] = round((time.monotonic() - started) * 1000)
    with engine.begin() as c:
        c.execute(insert(probes).values(**result))
    return result
