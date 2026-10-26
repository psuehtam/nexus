"""Versioned release downloads, stored separately from approved battery rules."""
import hashlib
import json
import re
import threading
from sqlalchemy import select, insert, update
from src.db import ROOT, engine, technical_candidates, sources, init_db
from src.connectors.public import get_json, download, now

lock = threading.Lock()

def import_release():
    with lock:
        init_db()
        release = get_json('https://api.github.com/repos/open-ev-data/open-ev-data-dataset/releases/latest')
        tag = release['tag_name']
        if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag):
            raise ValueError('Versão OpenEV fora do formato esperado')
        asset = next(a for a in release['assets'] if a['name'] == f'open-ev-data-{tag}.json')
        url = asset['browser_download_url']
        if not url.startswith('https://github.com/open-ev-data/open-ev-data-dataset/releases/download/'):
            raise ValueError('Origem inesperada do arquivo')
        path = ROOT / 'data/raw/openev' / f'{tag}.json'
        digest = download(url, path, max_bytes=30_000_000)
        payload = json.loads(path.read_text(encoding='utf-8'))
        records = payload['vehicles']
        if not isinstance(records, list) or payload['vehicle_count'] != len(records):
            raise ValueError('Contagem do arquivo OpenEV inconsistente')
        selected = [r for r in records if 'BR' in r.get('markets', [])]
        collected = now()
        added = 0
        with engine.begin() as c:
            existing = set(c.execute(select(technical_candidates.c.id)).scalars())
            for r in selected:
                identity = hashlib.sha256((tag+json.dumps(r,sort_keys=True)).encode()).hexdigest()
                if identity in existing:
                    continue
                c.execute(insert(technical_candidates).values(id=identity,release=tag,
                    brand=r['make']['name'],model=r['model']['name'],year=r['year'],raw=r,
                    source_url=url,collected_at=collected,status='PENDENTE_REVISAO'))
                existing.add(identity)
                added += 1
            details = {'role': f'{len(selected)} registros BR baixados; aguardam revisão técnica por versão.',
                'release':tag,'total_records':len(records),'br_records':len(selected),
                'file_bytes':path.stat().st_size,'sha256':digest,'download_url':url}
            c.execute(update(sources).where(sources.c.id=='openev').values(collected_at=collected,details=details))
        result = {**details,'inserted':added,'checked_at':collected}
        (ROOT/'docs/openev-download.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        from src.intelligence import migrate_candidates
        migrate_candidates()
        return result

if __name__ == '__main__':
    print(json.dumps(import_release(),ensure_ascii=False))
