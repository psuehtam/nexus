"""Monthly statistics and permanent vehicle records; transactional generation."""
import hashlib
import re
from sqlalchemy import select, insert, update, func, and_, or_
from src import db, intelligence_schema as s, operational_schema as o
from src import intelligence
from src.vehicle_taxonomy import is_electrified,is_heavy
from src.connectors.public import now

ACTIVE_ID = 'active'

def _clean(value):
    return re.sub(r'\s+', ' ', str(value or '').strip()).upper()

def model_key(row):
    raw = '|'.join(_clean(row.get(k)) for k in ('manufacturer','model','vehicle_category','powertrain_type'))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]

def public_identifier(key, sequence):
    return f'VEI-{hashlib.sha256(key.encode()).hexdigest()[:8].upper()}-{sequence:06d}'

def insert_once(c, table, values, keys):
    if c.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert as ins
    else:
        from sqlalchemy.dialects.sqlite import insert as ins
    c.execute(ins(table).values(**values).on_conflict_do_nothing(index_elements=keys))

def canonical(c, row):
    key = model_key(row)
    exact=c.execute(select(o.model).where(o.model.c.model_key==key)).mappings().first()
    if exact:return exact
    candidates=c.execute(select(o.model).where(~o.model.c.model_key.like('legacy:%'),func.upper(o.model.c.manufacturer)==_clean(row.get('manufacturer')),func.upper(o.model.c.model)==_clean(row.get('model')),o.model.c.powertrain_type==row.get('powertrain_type'))).mappings().all()
    if len(candidates)==1:
        current=candidates[0]
        # A previous automatic publication may have known the motorization but
        # not the vehicle type. Keep an explicit human classification, while
        # allowing later model evidence to correct system-created categories.
        category=(current['vehicle_category'] if current.get('classification_source') else
                  row.get('vehicle_category') or current['vehicle_category'])
        values={'is_electric':is_electrified(row.get('powertrain_type')),
                'is_heavy_vehicle':is_heavy(category)}
        if not current.get('classification_source'):
            values['vehicle_category']=category
        c.execute(update(o.model).where(o.model.c.id==current['id']).values(**values))
        return dict(c.execute(select(o.model).where(o.model.c.id==current['id'])).mappings().one())
    mid = intelligence.stable('registry-model:' + key)
    insert_once(c, o.model, dict(id=mid,model_key=key,manufacturer=row.get('manufacturer') or 'Não identificado',
        model=row.get('model') or 'Não identificado',vehicle_category=row.get('vehicle_category') or 'unknown',
        powertrain_type=row.get('powertrain_type') or 'UNKNOWN',is_electric=is_electrified(row.get('powertrain_type')),
        is_heavy_vehicle=is_heavy(row.get('vehicle_category')),classification_status='approved',allocated_count=0,created_at=now()), ['model_key'])
    return c.execute(select(o.model).where(o.model.c.model_key==key).with_for_update()).mappings().one()

def latest_source(c):
    return c.execute(select(s.source_snapshot).where(s.source_snapshot.c.provider=='SENATRAN',
        s.source_snapshot.c.metric=='fleet_stock',s.source_snapshot.c.ingestion_status=='success',
        s.source_snapshot.c.access_level=='public').order_by(s.source_snapshot.c.reference_period.desc(),
        s.source_snapshot.c.retrieved_at.desc(),s.source_snapshot.c.id)).mappings().first()

def prepare(source_snapshot_id=None):
    with db.engine.begin() as c:
        source = c.execute(select(s.source_snapshot).where(s.source_snapshot.c.id==source_snapshot_id)).mappings().first() if source_snapshot_id else latest_source(c)
        if not source or source['provider']!='SENATRAN' or source['metric']!='fleet_stock' or source['ingestion_status']!='success' or source['access_level']!='public':
            raise ValueError('A publicação exige fonte SENATRAN pública e validada')
        # Source row serializes repeated preparation before any child records exist.
        source = c.execute(select(s.source_snapshot).where(s.source_snapshot.c.id==source['id']).with_for_update()).mappings().one()
        pid=intelligence.stable('fleet-publication:'+source['id'])
        old=c.execute(select(s.fleet_publication).where(s.fleet_publication.c.id==pid)).mappings().first()
        if old:
            generation=(source.get('metadata') or {}).get('pipeline_generation')
            state=c.execute(select(o.publication_state).where(o.publication_state.c.publication_id==pid)).mappings().one()
            if generation is not None and generation>state['generation'] and old['status']!='complete':
                c.execute(update(o.publication_state).where(o.publication_state.c.publication_id==pid).values(generation=generation))
                c.execute(update(s.fleet_publication).where(s.fleet_publication.c.id==pid).values(status='pending',stage='units',finished_at=None))
                return dict(c.execute(select(s.fleet_publication).where(s.fleet_publication.c.id==pid)).mappings().one())
            return dict(old)
        rows=list(c.execute(select(s.aggregate.c.manufacturer,s.aggregate.c.model,s.aggregate.c.vehicle_category,
            s.aggregate.c.powertrain_type,func.sum(s.aggregate.c.quantity).label('quantity')).where(
            s.aggregate.c.source_snapshot_id==source['id']).group_by(s.aggregate.c.manufacturer,s.aggregate.c.model,
            s.aggregate.c.vehicle_category,s.aggregate.c.powertrain_type)).mappings())
        if not rows or sum(r['quantity'] for r in rows)<=0:raise ValueError('Publicação vazia exige revisão')
        c.execute(insert(s.fleet_publication).values(id=pid,source_snapshot_id=source['id'],reference_period=source['reference_period'],
            status='pending',stage='units',planned_units=sum(r['quantity'] for r in rows),materialized_units=0))
        generation=(source.get('metadata') or {}).get('pipeline_generation')
        if generation is None:
            generation=(c.execute(select(func.max(o.publication_state.c.generation))).scalar() or 0)+1
        c.execute(insert(o.publication_state).values(publication_id=pid,generation=generation,block_index=0,created_at=now()))
        for row in sorted(rows,key=model_key):
            m=canonical(c,row);gid=intelligence.stable(f'fleet-group:{pid}:{m["model_key"]}')
            c.execute(insert(s.fleet_model_group).values(id=gid,publication_id=pid,model_key=m['model_key'],manufacturer=m['manufacturer'],
                model=m['model'],vehicle_category=m['vehicle_category'],powertrain_type=m['powertrain_type'],quantity=int(row['quantity'])))
            c.execute(insert(o.group_model).values(group_id=gid,model_id=m['id']))
            insert_once(c,o.alias,dict(id=intelligence.uid(),entity_type='model',namespace='legacy',old_id=gid,canonical_id=m['id']),['entity_type','namespace','old_id'])
        return dict(c.execute(select(s.fleet_publication).where(s.fleet_publication.c.id==pid)).mappings().one())

def _alerts(c, pub, previous):
    pol=c.execute(select(o.policy).order_by(o.policy.c.created_at.desc(),o.policy.c.id.desc())).mappings().first()
    if not pol:
        values=dict(id='default-v1',national_pct=20,model_pct=50,model_absolute=100,actor='system',reason='Política inicial aprovada',created_at=now())
        insert_once(c,o.policy,values,['id']);pol=values
    findings=[]
    if previous and previous['reference_period']!=pub['reference_period']:
        old=previous['planned_units'];new=pub['planned_units']
        if old and abs(new-old)/old*100>pol['national_pct']:findings.append(('national_variation',dict(before=old,after=new)))
        a={r['model_key']:r for r in c.execute(select(s.fleet_model_group).where(s.fleet_model_group.c.publication_id==previous['id'])).mappings()}
        b={r['model_key']:r for r in c.execute(select(s.fleet_model_group).where(s.fleet_model_group.c.publication_id==pub['id'])).mappings()}
        for key,prior in a.items():
            current=b.get(key);qty=current['quantity'] if current else 0
            if not current:findings.append(('model_missing',dict(model=prior['model'],before=prior['quantity'],after=0)))
            elif prior['quantity'] and abs(qty-prior['quantity'])>=pol['model_absolute'] and abs(qty-prior['quantity'])/prior['quantity']*100>pol['model_pct']:
                findings.append(('model_variation',dict(model=prior['model'],before=prior['quantity'],after=qty)))
        y,m=map(int,previous['reference_period'].split('-')); yy,mm=map(int,pub['reference_period'].split('-'))
        if (yy-y)*12+mm-m>1:findings.append(('missing_months',dict(before=previous['reference_period'],after=pub['reference_period'])))
    for code,details in findings:
        c.execute(insert(o.alert).values(id=intelligence.uid(),publication_id=pub['id'],policy_id=pol['id'],code=code,
            details={**details,'previous_publication_id':previous['id'] if previous else None},created_at=now()))

def process(publication_id,batch_size=1000,lease=None):
    with db.engine.begin() as c:
        if lease:
            from src.pipeline import lock_lease
            lock_lease(c,lease)
        pub=c.execute(select(s.fleet_publication).where(s.fleet_publication.c.id==publication_id).with_for_update()).mappings().first()
        if not pub:raise ValueError('Publicação não encontrada')
        if pub['status'] in ('complete','superseded'):return dict(pub)
        state=c.execute(select(o.publication_state).where(o.publication_state.c.publication_id==publication_id)).mappings().one()
        c.execute(update(s.fleet_publication).where(s.fleet_publication.c.id==publication_id).values(status='running',started_at=pub['started_at'] or now(),error=None))
        remaining=max(1,min(int(batch_size),1000));made=0
        groups=c.execute(select(s.fleet_model_group,o.group_model.c.model_id).join(o.group_model,o.group_model.c.group_id==s.fleet_model_group.c.id)
            .where(s.fleet_model_group.c.publication_id==publication_id).order_by(o.group_model.c.model_id)).mappings().all()
        for g in groups:
            count=c.execute(select(func.max(s.fleet_unit_membership.c.sequence)).where(s.fleet_unit_membership.c.model_group_id==g['id'])).scalar() or 0
            end=min(g['quantity'],count+remaining)
            if count==end:continue
            m=c.execute(select(o.model).where(o.model.c.id==g['model_id']).with_for_update()).mappings().one()
            existing={r['sequence']:dict(r) for r in c.execute(select(o.registry).where(o.registry.c.origin_model_id==m['id'],o.registry.c.sequence>count,o.registry.c.sequence<=end)).mappings()}
            needed=[(seq,intelligence.stable(f'fleet-unit:{g["model_key"]}:{seq}')) for seq in range(count+1,end+1) if seq not in existing]
            asset_ids=[vid for seq,vid in needed]
            known=set();old_codes={}
            for start in range(0,len(asset_ids),400):
                ids=asset_ids[start:start+400]
                known.update(c.execute(select(s.vehicle_asset.c.id).where(s.vehicle_asset.c.id.in_(ids))).scalars())
                legacy_ids=[vid for vid in ids if vid in known]
                if legacy_ids:old_codes.update({r.vehicle_id:r.public_identifier for r in c.execute(select(s.fleet_unit_membership.c.vehicle_id,s.fleet_unit_membership.c.public_identifier).where(s.fleet_unit_membership.c.vehicle_id.in_(legacy_ids)))})
            assets=[];registries=[];memberships=[];timestamp=now()
            for seq,vid in needed:
                if vid not in known:assets.append(dict(id=vid,manufacturer=m['manufacturer'],model=m['model'],vehicle_category=m['vehicle_category'],powertrain_type=m['powertrain_type'],identity_status='pending',identity_confidence='pending',source_snapshot_id=pub['source_snapshot_id'],first_seen_at=timestamp,last_seen_at=timestamp))
                code=old_codes.get(vid) or f'VEI-{m["id"].replace("-","")[:12].upper()}-{seq:06d}'
                row=dict(vehicle_id=vid,origin_model_id=m['id'],current_model_id=m['id'],sequence=seq,public_code=code,visible=False,origin='statistical_placeholder',created_publication_id=publication_id,revision=0)
                registries.append(row);existing[seq]=row
            if assets:c.execute(insert(s.vehicle_asset),assets)
            if registries:c.execute(insert(o.registry),registries)
            for seq in range(count+1,end+1):
                reg=existing[seq]
                memberships.append(dict(id=intelligence.stable(f'membership:{publication_id}:{g["model_key"]}:{seq}'),publication_id=publication_id,model_group_id=g['id'],vehicle_id=reg['vehicle_id'],sequence=seq,public_identifier=reg['public_code']))
            if memberships:c.execute(insert(s.fleet_unit_membership),memberships)
            c.execute(update(o.model).where(o.model.c.id==m['id']).values(allocated_count=max(m['allocated_count'],end)))
            made+=end-count;remaining-=end-count
            if not remaining:break
        total=(pub['materialized_units'] or 0)+made
        if total>pub['planned_units']:raise ValueError('Contagem excedeu a quantidade planejada')
        if made:
            c.execute(insert(o.block).values(id=intelligence.uid(),publication_id=publication_id,block_index=state['block_index'],count=made,completed_at=now()))
            c.execute(update(o.publication_state).where(o.publication_state.c.publication_id==publication_id).values(block_index=state['block_index']+1))
        values=dict(materialized_units=total)
        if total==pub['planned_units']:
            verified=c.execute(select(func.count()).select_from(s.fleet_unit_membership).where(s.fleet_unit_membership.c.publication_id==publication_id)).scalar()
            if verified!=total:raise ValueError('Checkpoint e contagem final divergentes')
            for g in groups:
                actual=c.execute(select(func.count()).select_from(s.fleet_unit_membership).where(s.fleet_unit_membership.c.model_group_id==g['id'])).scalar()
                if actual!=g['quantity']:raise ValueError('Contagem por modelo inconsistente')
            insert_once(c,o.setting,dict(key='publication_gate',value={},updated_at=now()),['key'])
            c.execute(select(o.setting).where(o.setting.c.key=='publication_gate').with_for_update()).one()
            source=c.execute(select(s.source_snapshot).where(s.source_snapshot.c.id==pub['source_snapshot_id'])).mappings().one()
            selected=True
            pipeline_generation=(source.get('metadata') or {}).get('pipeline_generation')
            if pipeline_generation is not None:
                from src import pipeline_schema as ps
                wanted=c.execute(select(ps.intent.c.generation).where(ps.intent.c.reference==pub['reference_period']).with_for_update()).scalar_one_or_none()
                selected=(pipeline_generation==wanted)
            previous=active_publication(c)
            prev_gen=c.execute(select(o.publication_state.c.generation).where(o.publication_state.c.publication_id==previous['id'])).scalar() if previous else 0
            eligible=selected and (not previous or pub['reference_period']>previous['reference_period'] or (pub['reference_period']==previous['reference_period'] and (pipeline_generation is not None or state['generation']>=(prev_gen or 0))))
            # Historical reprocessing must never lock or change the public
            # registry. Only the publication that wins the active pointer can
            # expose newly prepared units.
            if eligible:
                c.execute(update(o.registry).where(o.registry.c.vehicle_id.in_(select(s.fleet_unit_membership.c.vehicle_id).where(s.fleet_unit_membership.c.publication_id==publication_id))).values(visible=True))
                if previous:
                    current_models=select(o.group_model.c.model_id).join(s.fleet_model_group,
                        o.group_model.c.group_id==s.fleet_model_group.c.id).where(
                        s.fleet_model_group.c.publication_id==publication_id)
                    retired_models=select(o.group_model.c.model_id).join(s.fleet_model_group,
                        o.group_model.c.group_id==s.fleet_model_group.c.id).where(
                        s.fleet_model_group.c.publication_id==previous['id'],
                        ~o.group_model.c.model_id.in_(current_models))
                    identified_ids=select(s.vehicle_asset.c.id).where(or_(
                        s.vehicle_asset.c.identity_status.in_(['informed','verified','identified']),
                        s.vehicle_asset.c.vin.is_not(None),s.vehicle_asset.c.renavam.is_not(None),
                        s.vehicle_asset.c.plate.is_not(None)))
                    installed_ids=select(o.current_install.c.vehicle_id)
                    c.execute(update(o.registry).where(o.registry.c.origin=='statistical_placeholder',
                        o.registry.c.visible.is_(True),o.registry.c.current_model_id.in_(retired_models),
                        ~o.registry.c.vehicle_id.in_(identified_ids),
                        ~o.registry.c.vehicle_id.in_(installed_ids)).values(visible=False))
            if eligible:
                insert_once(c,s.fleet_active,dict(id=ACTIVE_ID,publication_id=publication_id,updated_at=now()),['id'])
                c.execute(update(s.fleet_active).where(s.fleet_active.c.id==ACTIVE_ID).values(publication_id=publication_id,updated_at=now()))
            if not (source.get('metadata') or {}).get('pipeline_generation'):_alerts(c,pub,previous)
            values.update(status='complete' if selected else 'superseded',stage='published' if eligible else 'historical' if selected else 'superseded',finished_at=now())
        c.execute(update(s.fleet_publication).where(s.fleet_publication.c.id==publication_id).values(**values))
        return dict(c.execute(select(s.fleet_publication).where(s.fleet_publication.c.id==publication_id)).mappings().one())

def materialize(source_snapshot_id=None,batch_size=1000,complete=True,lease=None):
    pub=prepare(source_snapshot_id)
    while pub['status'] not in ('complete','superseded'):
        pub=process(pub['id'],batch_size,lease)
        if not complete:break
    return pub

def active_publication(c):
    return c.execute(select(s.fleet_publication).join(s.fleet_active,s.fleet_active.c.publication_id==s.fleet_publication.c.id).where(s.fleet_active.c.id==ACTIVE_ID)).mappings().first()


def backfill_unknown_automatic_types():
    """Resolve legacy model types without changing reviewed classifications."""
    from src.auto_classification import category_for
    with db.engine.begin() as c:
        rows=c.execute(select(o.model.c.id,o.model.c.manufacturer,o.model.c.model).where(
            o.model.c.vehicle_category=='unknown',o.model.c.classification_source.is_(None))).mappings().all()
        for row in rows:
            category,_=category_for(row['manufacturer']+' '+row['model'])
            if category=='unknown':continue
            c.execute(update(o.model).where(o.model.c.id==row['id']).values(
                vehicle_category=category,is_heavy_vehicle=is_heavy(category)))
            vehicle_ids=select(o.registry.c.vehicle_id).where(o.registry.c.current_model_id==row['id'])
            c.execute(update(s.vehicle_asset).where(s.vehicle_asset.c.id.in_(vehicle_ids),
                or_(s.vehicle_asset.c.vehicle_category.is_(None),
                    s.vehicle_asset.c.vehicle_category.in_(['unknown','light','heavy','LMT']))).values(
                vehicle_category=category))

def migrate_legacy_registry():
    """Explicit, resumable migration, preserving asset UUIDs and old public codes."""
    with db.engine.begin() as c:
        groups=[dict(x) for x in c.execute(select(s.fleet_model_group).order_by(s.fleet_model_group.c.publication_id,s.fleet_model_group.c.id)).mappings()]
        for g in groups:
            m=canonical(c,g)
            insert_once(c,o.group_model,dict(group_id=g['id'],model_id=m['id']),['group_id'])
            insert_once(c,o.alias,dict(id=intelligence.uid(),entity_type='model',namespace='legacy',old_id=g['id'],canonical_id=m['id']),['entity_type','namespace','old_id'])
        pubs=c.execute(select(s.fleet_publication).order_by(s.fleet_publication.c.reference_period)).mappings().all()
        for index,pub in enumerate(pubs):insert_once(c,o.publication_state,dict(publication_id=pub['id'],generation=index+1,block_index=0,created_at=now()),['publication_id'])
    for g in groups:
        last=0
        while True:
            with db.engine.begin() as c:
                mid=c.execute(select(o.group_model.c.model_id).where(o.group_model.c.group_id==g['id'])).scalar_one()
                pub=c.execute(select(s.fleet_publication).where(s.fleet_publication.c.id==g['publication_id'])).mappings().one()
                rows=c.execute(select(s.fleet_unit_membership).where(s.fleet_unit_membership.c.model_group_id==g['id'],s.fleet_unit_membership.c.sequence>last).order_by(s.fleet_unit_membership.c.sequence).limit(1000)).mappings().all()
                if not rows:break
                ids=[r['vehicle_id'] for r in rows]
                known=set(c.execute(select(o.registry.c.vehicle_id).where(o.registry.c.vehicle_id.in_(ids))).scalars())
                values=[dict(vehicle_id=r['vehicle_id'],origin_model_id=mid,current_model_id=mid,sequence=r['sequence'],public_code=r['public_identifier'],visible=pub['status']=='complete',origin='statistical_placeholder',created_publication_id=pub['id'],revision=0) for r in rows if r['vehicle_id'] not in known]
                if values:c.execute(insert(o.registry),values)
                if pub['status']=='complete':c.execute(update(o.registry).where(o.registry.c.vehicle_id.in_(ids)).values(visible=True))
                last=rows[-1]['sequence']
                old=c.execute(select(o.model.c.allocated_count).where(o.model.c.id==mid)).scalar()
                c.execute(update(o.model).where(o.model.c.id==mid).values(allocated_count=max(old,last)))
    preserved=0
    while True:
        with db.engine.begin() as c:
            rows=c.execute(select(s.vehicle_asset).where(~select(o.registry.c.vehicle_id).where(o.registry.c.vehicle_id==s.vehicle_asset.c.id).exists()).order_by(s.vehicle_asset.c.id).limit(1000)).mappings().all()
            if not rows:break
            for row in rows:
                m=canonical(c,row)
                # Legacy allocations must never consume the SENATRAN sequence.
                origin_id=intelligence.stable('legacy-allocation:'+m['id'])
                insert_once(c,o.model,dict(id=origin_id,model_key='legacy:'+m['id'],manufacturer=m['manufacturer'],model=m['model'],vehicle_category=m['vehicle_category'],powertrain_type=m['powertrain_type'],allocated_count=0,created_at=now()),['model_key'])
                allocation=c.execute(select(o.model).where(o.model.c.id==origin_id).with_for_update()).mappings().one()
                sequence=allocation['allocated_count']+1
                c.execute(insert(o.registry).values(vehicle_id=row['id'],origin_model_id=origin_id,current_model_id=m['id'],sequence=sequence,public_code='LEG-'+row['id'].replace('-','').upper(),visible=True,origin='legacy',revision=0))
                c.execute(update(o.model).where(o.model.c.id==origin_id).values(allocated_count=sequence))
                if row['identity_status']=='aggregate_derived' and not any(row.get(k) for k in ('plate','vin','renavam')):
                    inferred={k:row.get(k) for k in ('state','municipality','manufacture_year')}
                    intelligence.audit(c,{'actor':'migration','role':'admin'},'preserve_aggregate_reference','vehicle',row['id'],{'statistical_reference':inferred,'reason':'Origem agregada não comprova localização ou ano de uma unidade.'})
                    c.execute(update(s.vehicle_asset).where(s.vehicle_asset.c.id==row['id']).values(state=None,municipality=None,manufacture_year=None,identity_status='pending'))
                preserved+=1
    return {'groups':len(groups),'legacy_preserved':preserved,'status':'complete'}
