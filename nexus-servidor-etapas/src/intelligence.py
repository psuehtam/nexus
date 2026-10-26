"""Data services: additive migration, provenance, explicit assumptions and reconciliation."""
import hashlib
import json
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from sqlalchemy import select, insert, func
from src import db
from src import intelligence_schema as s
from src.intelligence_models import Forecast, Import
from src.connectors.public import now

VERSION='intelligence-v2.0'
def uid():return str(uuid.uuid4())
def stable(value):return str(uuid.uuid5(uuid.NAMESPACE_URL,value))
def audit(c,actor,action,entity,entity_id,details=None):
    c.execute(insert(s.audit).values(id=uid(),actor=actor['actor'],role=actor['role'],action=action,entity_type=entity,entity_id=entity_id,created_at=now(),details=details or {}))

def migrate():
    """Idempotent mirror; old snapshots and their totals are never rewritten."""
    db.init_db()
    with db.engine.begin() as c:
        existing=set(c.execute(select(s.source_snapshot.c.id)).scalars())
        legacy_sources={x['id']:dict(x) for x in c.execute(select(db.sources)).mappings()}
        for key,src in legacy_sources.items():
            sid=stable('legacy-source:'+key)
            if sid not in existing:
                c.execute(insert(s.source_snapshot).values(id=sid,provider=src['name'],dataset_name='Catálogo técnico legado',reference_period=src.get('reference'),retrieved_at=src['collected_at'],method='review',source_url=src['url'],source_version='legacy-reviewed',transformation_version=VERSION,license_or_terms='Consultar termos na origem',ingestion_status='partial',record_count=0,scope=(src.get('details') or {}).get('role','Catálogo de fontes'),metric='technical',access_level='public',metadata=src.get('details')))
        for m in c.execute(select(db.models)).mappings():
            mid=stable('spec:'+m['id'])
            if not c.execute(select(s.vehicle_spec.c.id).where(s.vehicle_spec.c.id==mid)).first():
                c.execute(insert(s.vehicle_spec).values(id=mid,manufacturer=m['brand'],model=m['model'],trim=m['version'],model_year_start=m['year_start'],model_year_end=m['year_end'],powertrain=m.get('propulsion') or m['category'],country_market='BR',vehicle_category='light',source_snapshot_id=stable('legacy-source:inmetro')))
        for b in c.execute(select(db.batteries)).mappings():
            bid=stable('battery-model:'+b['id'])
            if not c.execute(select(s.battery_model.c.id).where(s.battery_model.c.id==bid)).first():
                c.execute(insert(s.battery_model).values(id=bid,manufacturer=b['pack_manufacturer'],model_name=None,chemistry=b['chemistry'],technology=b['technology'],part_number=b.get('part_number'),nominal_capacity_kwh=b['capacity_kwh'],usable_capacity_kwh=b['usable_kwh'],nominal_voltage_v=b['voltage'],warranty_years=b['warranty_years'],warranty_km=b['warranty_km'],confidence='high',source_snapshot_id=stable('legacy-source:byd'),field_sources=b['provenance']))
                c.execute(insert(s.compatibility).values(id=stable('evidence:'+b['id']),vehicle_spec_id=stable('spec:'+b['model_id']),battery_model_id=bid,source_snapshot_id=stable('legacy-source:byd'),confidence='high',evidence_type='oem',verified_by='legacy-reviewed-rule',verified_at=now(),status='accepted',notes='Associação de especificação, não identificação de pack individual.'))
        for snap in c.execute(select(db.snapshots)).mappings():
            sid=stable('senatran:'+snap['reference']+':'+snap['sha256'])
            if sid in existing:continue
            run=c.execute(select(db.runs).where(db.runs.c.id==snap['run_id'])).mappings().first()
            details=(run['details'] or {}) if run else {}
            query=select(db.fleet,db.locations.c.uf,db.locations.c.city).join(db.locations,db.fleet.c.location_id==db.locations.c.id).where(db.fleet.c.reference==snap['reference'])
            rows=list(c.execute(query).mappings())
            c.execute(insert(s.source_snapshot).values(id=sid,provider='SENATRAN',dataset_name='Frota eletrificada revisada',reference_period=snap['reference'],retrieved_at=snap['published_at'],method='download',raw_file_hash=snap['sha256'],raw_storage_path='data/raw/'+snap['reference']+'/senatran/',source_url=details.get('download_url') or legacy_sources['senatran']['url'],source_version=snap['reference'],transformation_version=VERSION,license_or_terms='Dados públicos; consultar termos SENATRAN',ingestion_status='success',record_count=len(rows),scope=snap['scope'],metric='fleet_stock',access_level='public',metadata={'legacy_run_id':snap['run_id']}))
            converted=[]
            for row in rows:
                converted.append(dict(id=stable('observation:'+str(row['id'])+':'+snap['sha256']),source_snapshot_id=sid,reference_period=snap['reference'],country='BR',state=row['uf'],municipality=None if row['city']=='Não informado' else row['city'],vehicle_category='light',manufacturer=row['brand'],model=row['model'],trim=row['version'],model_year=None,manufacture_year=row['vehicle_year'],powertrain_type=row.get('propulsion') or 'BEV',fuel_type=None,quantity=row['quantity'],source_record_reference=str(row['id']),confidence='high' if row['battery_id'] else 'unknown',battery_model_id=stable('battery-model:'+row['battery_id']) if row['battery_id'] else None,vehicle_spec_id=None,abve_electrified_classification=1))
            for i in range(0,len(converted),1000):c.execute(insert(s.aggregate),converted[i:i+1000])
    migrate_candidates()
    return {'status':'success','version':VERSION}

def migrate_candidates():
    with db.engine.begin() as c:
        groups=defaultdict(list)
        for row in c.execute(select(db.technical_candidates)).mappings():groups[row['release']].append(dict(row))
        for release,rows in groups.items():
            sid=stable('openev-release:'+release)
            if c.execute(select(s.source_snapshot.c.id).where(s.source_snapshot.c.id==sid)).first():continue
            path=db.ROOT/'data/raw/openev'/f'{release}.json'
            digest=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            c.execute(insert(s.source_snapshot).values(id=sid,provider='OpenEV',dataset_name='Candidatos técnicos BR',reference_period=None,retrieved_at=rows[0]['collected_at'],method='download',raw_file_hash=digest,raw_storage_path=str(path.relative_to(db.ROOT)) if path.exists() else None,source_version=release,transformation_version=VERSION,license_or_terms='CDLA-Permissive-2.0; confirmar aplicabilidade por versão BR',ingestion_status='partial',record_count=len(rows),source_url=rows[0]['source_url'],scope='Mercado BR declarado pela fonte; especificações não aprovadas',metric='technical',access_level='public',metadata={}))
            for row in rows:
                raw=row['raw'];b=raw.get('battery') or {};vid=stable('openev-spec:'+row['id']);bid=stable('openev-battery:'+row['id'])
                c.execute(insert(s.vehicle_spec).values(id=vid,manufacturer=row['brand'],model=row['model'],trim=(raw.get('trim') or {}).get('name'),model_year_start=row['year'],model_year_end=row['year'],powertrain='UNKNOWN',country_market='BR',vehicle_category='bus' if raw.get('vehicle_type')=='bus' else 'light' if raw.get('vehicle_type')=='passenger_car' else 'unknown',source_snapshot_id=sid))
                c.execute(insert(s.battery_model).values(id=bid,model_name=b.get('model_name'),manufacturer=None,chemistry=b.get('chemistry'),nominal_capacity_kwh=b.get('pack_capacity_kwh_gross'),usable_capacity_kwh=b.get('pack_capacity_kwh_net'),confidence='low',source_snapshot_id=sid,field_sources={k:row['source_url'] for k in ('chemistry','nominal_capacity_kwh','usable_capacity_kwh')}))
                c.execute(insert(s.compatibility).values(id=stable('openev-evidence:'+row['id']),vehicle_spec_id=vid,battery_model_id=bid,source_snapshot_id=sid,confidence='low',evidence_type='technical',status='candidate',notes='Candidato OpenEV. Revisar ano, versão, mercado e fonte OEM antes de aceitar; garantia global não copiada.'))

def import_observations(body:Import,actor,raw_hash=None,raw_path=None):
    if body.source.provider.strip().upper()=='ABVE' and body.source.metric=='fleet_stock':
        raise ValueError('ABVE não entra como estoque da Frota. Importe como registrations ou cumulative_registrations (mercado), nunca fleet_stock.')
    raw=body.model_dump(mode='json')
    encoded=json.dumps(raw,ensure_ascii=False,sort_keys=True).encode()
    digest=raw_hash or hashlib.sha256(encoded).hexdigest()
    sid=stable('import:'+body.source.provider+':'+body.source.dataset_name+':'+body.source.reference_period+':'+digest)
    with db.engine.begin() as c:
        if c.execute(select(s.source_snapshot.c.id).where(s.source_snapshot.c.id==sid)).first():return {'id':sid,'status':'unchanged','inserted':0}
        if raw_path is None:
            path=db.ROOT/'data/raw/imports'/f'{sid}.json';path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(encoded);raw_path=str(path.relative_to(db.ROOT))
        c.execute(insert(s.source_snapshot).values(id=sid,**body.source.model_dump(),raw_file_hash=digest,raw_storage_path=raw_path,retrieved_at=now(),source_version=body.source.reference_period,transformation_version=VERSION,ingestion_status='success',record_count=len(body.observations),metadata={}))
        for row in body.observations:
            c.execute(insert(s.aggregate).values(id=uid(),source_snapshot_id=sid,reference_period=body.source.reference_period,**row.model_dump(),abve_electrified_classification=None if row.powertrain_type=='UNKNOWN' else int(row.powertrain_type in ('BEV','PHEV','HEV','HEV_FLEX','ELECTRIFIED'))))
        audit(c,actor,'import','source_snapshot',sid,{'records':len(body.observations),'sha256':digest})
    return {'id':sid,'status':'success','inserted':len(body.observations)}

def availability(body:Forecast):
    base=body.registration_date.year if body.registration_date else body.vehicle_year
    return {'earliest_year':base+body.earliest_age if base else None,'most_likely_year':base+body.most_likely_age if base else None,'latest_year':base+body.latest_age if base else None,'confidence':'low' if base else 'unknown','method':'calendar_age_prior_v1','missing_features':['state_of_health','cycle_count','usage_profile']+([] if base else ['vehicle_year']),'assumptions':body.model_dump(mode='json'),'note':'Cenário de planejamento configurável, não previsão calibrada nem data de falha. Garantia não define vida útil.'}

def quality(row,battery=None,source=None,lifecycle=None):
    identity=100 if row.get('identity_status')=='verified' else 75 if row.get('identity_status')=='identified' else 35 if row.get('identity_status')=='partially_identified' else 0
    mapping=100 if battery and row.get('confidence') in ('high','A') else 50 if battery else 0
    source_score=100 if source and source.get('raw_file_hash') and source.get('source_url') else 50 if source else 0
    fresh=0
    if source and source.get('retrieved_at'):
        elapsed=max(0,(date.today()-date.fromisoformat(source['retrieved_at'][:10])).days)
        fresh=max(0,100-elapsed//3)
    life=round(sum((lifecycle or {}).get(k) is not None for k in ('soh_percent','soce_percent','cycle_count','energy_throughput_kwh'))/4*100)
    parts={'identity_score':identity,'battery_mapping_score':mapping,'source_score':source_score,'freshness_score':fresh,'lifecycle_score':life}
    return {**parts,'total':round(sum(parts.values())/5),'method':'completeness_v1_equal_weights','note':'Completude e rastreabilidade; não é probabilidade de veracidade ou disponibilidade.'}

def reconciliation(left,right):
    fields=('reference_period','scope','metric','dataset_name')
    reasons=[k for k in fields if left.get(k)!=right.get(k)]
    comparable=not reasons
    return {'comparable':comparable,'differences':reasons,'delta':right['quantity']-left['quantity'] if comparable else None,'note':'Só comparar numericamente após alinhar período, medida e cobertura.' if reasons else 'Mesma medida, período e cobertura declarados; conferir metodologias das fontes.'}

def opportunity(rows,battery_models,start,end,early=8,mode=10,late=12,residual=None):
    regions=defaultdict(lambda:{'vehicles':0,'known_battery_vehicles':0,'potential_mwh':0.0,'weighted_scenario_mwh':0.0})
    by_powertrain=defaultdict(lambda:{'vehicles':0,'known_battery_vehicles':0,'potential_mwh':0.0,'weighted_scenario_mwh':0.0})
    missing_year=0
    for row in rows:
        year=row.get('manufacture_year') or row.get('model_year')
        if year is None:missing_year+=row['quantity'];continue
        # Discrete triangular weights are scenario assumptions, never fitted probabilities.
        weights={age:1+min(age-early,late-age) for age in range(early,late+1)}
        if early<mode<late:
            weights={age:(age-early+1)/(mode-early+1) if age<=mode else (late-age+1)/(late-mode+1) for age in weights}
        weight=sum(v for age,v in weights.items() if start<=year+age<=end)/sum(weights.values())
        if weight==0:continue
        powertrain=row.get('powertrain_type') or 'UNKNOWN'
        key=(row.get('state') or 'Não informado',row.get('municipality') or 'Não informado',powertrain)
        r=regions[key];r['vehicles']+=row['quantity']
        pt=by_powertrain[powertrain];pt['vehicles']+=row['quantity']
        b=battery_models.get(row.get('battery_model_id'))
        if b and b.get('nominal_capacity_kwh'):
            energy=row['quantity']*b['nominal_capacity_kwh']/1000
            r['known_battery_vehicles']+=row['quantity'];r['potential_mwh']+=energy;r['weighted_scenario_mwh']+=energy*weight
            pt['known_battery_vehicles']+=row['quantity'];pt['potential_mwh']+=energy;pt['weighted_scenario_mwh']+=energy*weight
    output=[]
    for (state,city,powertrain),r in regions.items():
        coverage=r['known_battery_vehicles']/r['vehicles'] if r['vehicles'] else 0
        output.append({'state':state,'municipality':city,'powertrain_type':powertrain,**r,'coverage_pct':round(coverage*100,1),'opportunity_score':round(r['weighted_scenario_mwh']*coverage,3),'recoverable_scenario_mwh':r['weighted_scenario_mwh']*residual if residual is not None else None,'confidence':'low'})
    powertrains=[]
    for name,r in sorted(by_powertrain.items(),key=lambda x:-x[1]['potential_mwh']):
        coverage=r['known_battery_vehicles']/r['vehicles'] if r['vehicles'] else 0
        powertrains.append({'powertrain_type':name,**r,'coverage_pct':round(coverage*100,1)})
    return {'items':sorted(output,key=lambda r:r['opportunity_score'],reverse=True),'by_powertrain':powertrains,'missing_year_vehicles':missing_year,'method':'calendar_triangular_scenario_v1','note':'Pontuação = MWh ponderados no cenário × cobertura técnica, separado por propulsão (BEV/PHEV/HEV não se somam num único MWh sem rótulo). Não é probabilidade calibrada nem packs confirmados.','assumptions':{'start_year':start,'end_year':end,'earliest_age':early,'most_likely_age':mode,'latest_age':late,'residual_fraction':residual}}
