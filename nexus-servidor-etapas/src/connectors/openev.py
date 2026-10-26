"""API-based technical enrichment candidates, never automatic Brazil warranty."""
import json
import base64
from src.db import ROOT
from src.connectors.public import get_json,GITHUB,now

def fetch_candidates(make='byd',model='dolphin',year=2025):
    commit=get_json('https://api.github.com/repos/open-ev-data/open-ev-data-dataset/commits/main')['sha']
    tree=get_json(f'https://api.github.com/repos/open-ev-data/open-ev-data-dataset/git/trees/{commit}?recursive=1')
    base=f'https://raw.githubusercontent.com/open-ev-data/open-ev-data-dataset/{commit}/'
    prefix=f'src/{make}/{model}/{year}/'
    paths=[x['path'] for x in tree['tree'] if x['path'].startswith(prefix) and x['path'].endswith('.json')]
    candidates=[]
    for path in paths:
        # GitHub Contents API avoids depending on the separate raw CDN host.
        payload=get_json(f'https://api.github.com/repos/open-ev-data/open-ev-data-dataset/contents/{path}?ref={commit}')
        if payload.get('encoding')!='base64':raise ValueError('Contrato inesperado da API GitHub Contents')
        record=json.loads(base64.b64decode(payload['content']))
        if 'BR' not in record.get('markets',[]):continue
        candidates.append({'raw':record,'source_url':base+path,'collected_at':now(),'commit':commit,
                           'status':'PENDENTE_REVISAO','note':'Confirmar versão e garantia brasileira na montadora antes de vincular.'})
    destination=ROOT/'data/staging'/f'openev-{make}-{model}-{year}.json'
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(candidates,ensure_ascii=False,indent=2),encoding='utf-8')
    return candidates

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--make',default='byd');p.add_argument('--model',default='dolphin');p.add_argument('--year',type=int,default=2025)
    args=p.parse_args();print(f'{len(fetch_candidates(args.make,args.model,args.year))} candidatos salvos para revisão')
