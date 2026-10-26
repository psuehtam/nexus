"""Reviewed public releases; facts live in versioned JSON under data/reviewed."""
from src.db import ROOT
from src.connectors.public import download
from src.connectors.reviewed_sources import read_json
from src.intelligence_models import Import
from src.intelligence import import_observations

MARKET='https://abve.org.br/com-57-mil-emplacamentos-em-agosto-eletrificados-abrem-a-corrida-para-o-milhao-em-setembro/'
BUSES='https://abve.org.br/onibus-eletricos-tem-crescimento-de-925-no-1o-semestre-de-2026-comparado-a-2025/'

def ingest_reviewed(actor, verify_live=True):
    market = read_json('abve-market-2026-08.json')
    buses = read_json('abve-buses-2026-H1.json')
    if not market or not buses:
        raise ValueError('Arquivos revisados ABVE ausentes em data/reviewed')
    results=[]
    for name, payload, url in [('market', market, MARKET), ('buses', buses, BUSES)]:
        path = ROOT/'data/raw/abve'/f"{name}-{payload['period']}.html"
        digest = None
        if verify_live:
            try:
                digest = download(url, path, max_bytes=10_000_000)
                text = path.read_text(encoding='utf-8')
                if not all(v in text for v in payload.get('markers', [])):
                    raise ValueError('Publicação ABVE mudou: números revisados não encontrados; requer nova revisão')
            except Exception as error:
                if 'mudou' in str(error):
                    raise
                digest = None
        base={'provider':'ABVE','reference_period':payload['period'],'method':'review','source_url':url,'license_or_terms':'Publicação pública ABVE. Exportação do BI exige licença; não usar como fleet_stock nem somar à Frota SENATRAN.','access_level':'public'}
        def publish(dataset,metric,scope,rows):
            body=Import.model_validate({'source':{**base,'dataset_name':dataset,'metric':metric,'scope':scope,'notes':f"Transcrição de fatos revisados em {payload['reviewed_at']}; não é API de dados ABVE nem cobertura individual."},'observations':rows})
            raw_path = str(path.relative_to(ROOT)) if path.exists() else None
            results.append(import_observations(body,actor,digest,raw_path))
        if name=='market':
            publish('Universo de eletrificados leves','cumulative_registrations','Brasil; leves BEV/PHEV/HEV/HEV Flex; acumulado publicado desde 2012',[{'quantity':market['cumulative_electrified'],'source_record_reference':'acumulado-br','vehicle_category':'light','powertrain_type':'ELECTRIFIED'}])
            rows=[{'quantity':item['quantity'],'source_record_reference':item['powertrain_type'],'vehicle_category':'light','powertrain_type':item['powertrain_type']} for item in market['registrations']]
            publish('Emplacamentos leves por tecnologia','registrations','Brasil; agosto; tecnologias separadas, incluindo MHEV explicitamente fora da classificação ABVE',rows)
        else:
            cities=buses['cities']
            assert sum(q for _,_,q in cities)==buses['total']
            publish('Ônibus elétricos por município','registrations','Brasil; emplacamentos de ônibus elétricos no primeiro semestre; modelo, ano e tecnologia detalhada não publicados',[{'quantity':q,'source_record_reference':uf+':'+city,'state':uf,'municipality':city,'vehicle_category':'bus','powertrain_type':'UNKNOWN'} for uf,city,q in cities])
    return results

if __name__=='__main__':print(ingest_reviewed({'actor':'reviewed-publication-import','role':'admin'}))
