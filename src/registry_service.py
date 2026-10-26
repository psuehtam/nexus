"""Transactional vehicle, dossier and physical battery operations."""
import hashlib
import json
import re
from datetime import datetime, timezone
from sqlalchemy import select, insert, update, delete, func, or_
from fastapi import HTTPException
from src import db, intelligence_schema as s, operational_schema as o, intelligence
from src.battery_passport import FIELDS
from src.connectors.public import now
from src.fleet_publication import insert_once

SCHEMA_VERSION='1'

def actor(user): return {'actor':user.get('username',user.get('actor','system')),'role':user.get('role','admin')}

def audit(c,user,action,entity,key,before=None,after=None):
    intelligence.audit(c,actor(user),action,entity,key,{'before':before,'after':after})

def get(c,table,key,column=None,lock=False):
    query=select(table).where((column if column is not None else table.c.id)==key)
    if lock:query=query.with_for_update()
    row=c.execute(query).mappings().first()
    if not row:raise HTTPException(404,'Registro não encontrado')
    return dict(row)

def resolve_model(c,key):
    row=c.execute(select(o.model).where(o.model.c.id==key)).mappings().first()
    if not row:
        target=c.execute(select(o.alias.c.canonical_id).where(o.alias.c.entity_type=='model',o.alias.c.old_id==key)).scalar()
        if target:row=c.execute(select(o.model).where(o.model.c.id==target)).mappings().first()
    if not row:raise HTTPException(404,'Modelo não encontrado')
    return dict(row)

def resolve_vehicle(c,key,lock=False):
    query=select(o.registry).where(or_(o.registry.c.vehicle_id==key,o.registry.c.public_code==key),o.registry.c.visible.is_(True))
    if lock:query=query.with_for_update()
    row=c.execute(query).mappings().first()
    if not row:
        target=c.execute(select(o.alias.c.canonical_id).where(o.alias.c.entity_type=='vehicle',o.alias.c.old_id==key)).scalar()
        if target:
            query=select(o.registry).where(o.registry.c.vehicle_id==target,o.registry.c.visible.is_(True))
            row=c.execute(query.with_for_update() if lock else query).mappings().first()
    if not row:raise HTTPException(404,'Cadastro individual não encontrado')
    return dict(row)

def passport(c,owner_type,owner_id,history=True):
    record=c.execute(select(o.dossier).where(o.dossier.c.owner_type==owner_type,o.dossier.c.owner_id==owner_id)).mappings().first()
    if record:record=dict(record)
    else:record=dict(id=intelligence.stable(f'dossier:{owner_type}:{owner_id}'),owner_type=owner_type,owner_id=owner_id,
        code=('FIC-' if owner_type=='vehicle' else 'PASS-')+owner_id.replace('-','').upper(),schema_version=SCHEMA_VERSION,revision=0,values={},updated_at=None)
    definition=c.execute(select(o.schema.c.fields).where(o.schema.c.version==record['schema_version'])).scalar()
    fields=definition or FIELDS
    counts={k:0 for k in ('pending','draft','confirmed','not_applicable')}
    for f in fields:counts[(record['values'].get(f['id']) or {}).get('status','pending')]+=1
    record.update(fields=fields,counts={**counts,'total':len(fields)},filled=counts['draft']+counts['confirmed'],total=len(fields))
    record['history']=[dict(r) for r in c.execute(select(o.dossier_revision.c.revision,o.dossier_revision.c.schema_version,
        o.dossier_revision.c.actor,o.dossier_revision.c.created_at).where(o.dossier_revision.c.dossier_id==record['id']).order_by(o.dossier_revision.c.revision.desc()).limit(100)).mappings()] if history else []
    # Reference model specifications are deliberately excluded from filled values.
    return record

def save_passport(c,owner_type,owner_id,payload,user):
    if owner_type=='vehicle':get(c,o.registry,owner_id,o.registry.c.vehicle_id,True)
    else:get(c,s.battery_asset,owner_id,lock=True)
    old=passport(c,owner_type,owner_id,False)
    if payload['revision']!=old['revision'] or payload['schema_version']!=old['schema_version']:
        raise HTTPException(409,'A ficha foi atualizada. Recarregue antes de salvar.')
    allowed={x['id'] for x in old['fields']}
    values=dict(old['values'])
    for key,value in payload['values'].items():
        if key not in allowed:raise HTTPException(422,'Campo desconhecido nesta versão')
        status=value.get('status','draft');text=value.get('value');source=(value.get('source') or '').strip();reason=(value.get('reason') or '').strip()
        if status not in ('pending','draft','confirmed','not_applicable'):raise HTTPException(422,'Situação de campo inválida')
        if text is not None and not isinstance(text,(str,int,float,bool)):raise HTTPException(422,'Valor de campo inválido')
        if text is not None and len(str(text))>10000:raise HTTPException(422,'Valor muito longo')
        if status in ('draft','confirmed') and (text is None or not str(text).strip()):raise HTTPException(422,'Informe o valor ou deixe pendente')
        if status=='confirmed' and not source:raise HTTPException(422,'A confirmação exige fonte/evidência')
        if status=='not_applicable' and not reason:raise HTTPException(422,'Não aplicável exige justificativa')
        if status=='pending':values.pop(key,None)
        else:values[key]=dict(value=text if status!='not_applicable' else None,status=status,source=source,reason=reason,
            actor=actor(user)['actor'],updated_at=now())
    insert_once(c,o.schema,dict(version=SCHEMA_VERSION,fields=FIELDS,created_at=now()),['version'])
    revision=old['revision']+1
    data=dict(schema_version=old['schema_version'],revision=revision,values=values,updated_at=now())
    if old['revision']:
        result=c.execute(update(o.dossier).where(o.dossier.c.id==old['id'],o.dossier.c.revision==old['revision']).values(**data))
        if result.rowcount!=1:raise HTTPException(409,'Edição concorrente da ficha')
    else:c.execute(insert(o.dossier).values(id=old['id'],owner_type=owner_type,owner_id=owner_id,code=old['code'],**data))
    c.execute(insert(o.dossier_revision).values(id=intelligence.uid(),dossier_id=old['id'],revision=revision,schema_version=old['schema_version'],
        values=values,actor=actor(user)['actor'],created_at=now()))
    audit(c,user,'passport_update',owner_type,owner_id,old['values'],values)
    return passport(c,owner_type,owner_id)

def evidence_source(c,evidence,user):
    evidence=(evidence or '').strip()
    if len(evidence)<3:raise HTTPException(422,'Informe a origem/evidência dos dados')
    sid=intelligence.uid()
    c.execute(insert(s.source_snapshot).values(id=sid,provider='Cadastro administrativo',dataset_name='Informação individual',
        retrieved_at=now(),method='manual',ingestion_status='success',metric='individual',access_level='public',notes=evidence,
        metadata={'actor':actor(user)['actor']}))
    return sid

def normalize_id(kind,value):
    value=re.sub(r'[^A-Z0-9]','',str(value).upper())
    if kind=='vin' and not re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}',value):raise HTTPException(422,'Chassi deve ter 17 caracteres válidos')
    if kind=='renavam' and not re.fullmatch(r'\d{9,11}',value):raise HTTPException(422,'RENAVAM deve conter de 9 a 11 dígitos')
    if kind=='plate' and not re.fullmatch(r'[A-Z]{3}\d[A-Z0-9]\d{2}',value):raise HTTPException(422,'Placa inválida')
    return value

def claim_identifier(c,entity_type,entity_id,kind,issuer,value,evidence):
    current=c.execute(select(o.identifier).where(o.identifier.c.entity_type==entity_type,o.identifier.c.kind==kind,
        o.identifier.c.issuer==issuer,o.identifier.c.normalized==value,o.identifier.c.active.is_(True))).mappings().first()
    if current:
        if current['entity_id']!=entity_id:raise HTTPException(409,'Identificador já atribuído a outro cadastro')
        return
    c.execute(insert(o.identifier).values(id=intelligence.uid(),entity_type=entity_type,entity_id=entity_id,kind=kind,issuer=issuer,
        normalized=value,active=True,evidence=evidence,created_at=now()))

def identify(c,key,payload,user):
    reg=resolve_vehicle(c,key,True);old=get(c,s.vehicle_asset,reg['vehicle_id'],lock=True)
    if payload['revision']!=reg['revision']:raise HTTPException(409,'Cadastro alterado. Recarregue a página.')
    evidence=payload.get('evidence','');sid=evidence_source(c,evidence,user)
    changes={}
    for kind in ('vin','renavam','plate'):
        if kind not in payload:continue
        value=normalize_id(kind,payload[kind]) if payload[kind] else None
        if value:
            existing=c.execute(select(s.vehicle_asset.c.id).where(s.vehicle_asset.c[kind]==value,s.vehicle_asset.c.id!=old['id'])).first()
            if existing:raise HTTPException(409,'Identificador já usado por outro veículo')
            claim_identifier(c,'vehicle',old['id'],kind,'BR',value,evidence)
        if old.get(kind) and old[kind]!=value:
            c.execute(update(o.identifier).where(o.identifier.c.entity_type=='vehicle',o.identifier.c.entity_id==old['id'],o.identifier.c.kind==kind,
                o.identifier.c.normalized==old[kind],o.identifier.c.active.is_(True)).values(active=False))
        changes[kind]=value
    for key in ('state','municipality'):
        if key in payload:changes[key]=(payload[key] or '').strip() or None
    if changes.get('state') and not re.fullmatch(r'[A-Z]{2}',changes['state']):raise HTTPException(422,'UF inválida')
    merged={**old,**changes}
    has_identity=bool(merged.get('vin') or merged.get('renavam') or merged.get('plate'))
    verified=bool(payload.get('verified'))
    if verified and not (merged.get('vin') or merged.get('renavam')):raise HTTPException(422,'Verificação exige chassi ou RENAVAM e evidência')
    changes.update(identity_status='verified' if verified else 'informed' if has_identity else 'pending',identity_confidence='verified' if verified else 'pending',last_seen_at=now())
    c.execute(update(s.vehicle_asset).where(s.vehicle_asset.c.id==old['id']).values(**changes))
    c.execute(update(o.registry).where(o.registry.c.vehicle_id==old['id']).values(revision=reg['revision']+1))
    audit(c,user,'vehicle_identification','vehicle',old['id'],old,{**changes,'evidence_source':sid})
    return old['id']

def iso(value,required=False):
    if not value:
        if required:raise HTTPException(422,'Data da medição é obrigatória')
        return None
    try:
        dt=datetime.fromisoformat(value.replace('Z','+00:00'))
        if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
        if dt>datetime.now(timezone.utc):raise ValueError('future')
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError,TypeError):raise HTTPException(422,'Data inválida ou futura')

def installations(c,vehicle_id=None,battery_id=None):
    query=select(s.installation).order_by(s.installation.c.installed_at.desc(),s.installation.c.id)
    if vehicle_id:query=query.where(s.installation.c.vehicle_id==vehicle_id)
    if battery_id:query=query.where(s.installation.c.battery_id==battery_id)
    result=[]
    for row in c.execute(query).mappings():
        item=dict(row);ev=c.execute(select(o.installation_evidence).where(o.installation_evidence.c.installation_id==item['id'])).mappings().first()
        item['is_current']=not ev['closed'] if ev else item['removed_at'] is None
        if ev:item.update(evidence=ev['evidence'],observed_at=ev['observed_at'],removal_observed_at=ev['removal_observed_at'])
        item['battery']=get(c,s.battery_asset,item['battery_id'])
        if battery_id:
            item['vehicle']=get(c,s.vehicle_asset,item['vehicle_id'])
            item['public_identifier']=c.execute(select(o.registry.c.public_code).where(o.registry.c.vehicle_id==item['vehicle_id'])).scalar()
        result.append(item)
    return result

def link_battery(c,key,payload,user):
    reg=resolve_vehicle(c,key,True);vehicle=get(c,s.vehicle_asset,reg['vehicle_id'],lock=True)
    if vehicle['identity_status']!='verified':raise HTTPException(422,'Verifique a identificação do veículo antes de confirmar vínculo físico. Use a ficha para rascunhos.')
    sid=evidence_source(c,payload.get('evidence'),user)
    battery_id=payload.get('battery_id')
    if battery_id:bat=get(c,s.battery_asset,battery_id,lock=True)
    else:
        serial=(payload.get('serial') or '').strip();manufacturer=(payload.get('manufacturer') or '').strip()
        if not serial or not manufacturer:raise HTTPException(422,'Informe fabricante e serial para distinguir a bateria física')
        battery_id=intelligence.uid()
        claim_identifier(c,'battery',battery_id,'serial',manufacturer.upper(),serial.upper(),payload['evidence'])
        bat=dict(id=battery_id,source_snapshot_id=sid,manufacturer_serial_number=serial,battery_identifier='BAT-'+battery_id.replace('-','').upper(),
            battery_identifier_type='internal',current_status='in_use',confidence='verified')
        c.execute(insert(s.battery_asset).values(**bat))
    if c.execute(select(o.current_install).where(o.current_install.c.battery_id==battery_id)).first():raise HTTPException(409,'Bateria já possui vínculo atual')
    # Also reject legacy current installations not yet mirrored.
    if any(x['is_current'] for x in installations(c,battery_id=battery_id)):raise HTTPException(409,'Bateria já instalada')
    position=(payload.get('position') or 'pack-1').strip()
    if not position or len(position)>80:raise HTTPException(422,'Informe uma posição/rótulo válido')
    if any(x['is_current'] and (x['position'] or 'pack-1')==position for x in installations(c,vehicle_id=vehicle['id'])):raise HTTPException(409,'Já existe bateria nessa posição')
    installed=iso(payload.get('installed_at'))
    history=installations(c,battery_id=battery_id)
    if installed and any(not x.get('removed_at') or x['removed_at']>installed for x in history):raise HTTPException(409,'Data conflita com vínculos anteriores; revise o histórico')
    iid=intelligence.uid()
    c.execute(insert(s.installation).values(id=iid,vehicle_id=vehicle['id'],battery_id=battery_id,source_snapshot_id=sid,
        installed_at=installed,removed_at=None,installation_type='traction',confidence='verified',position=position))
    c.execute(insert(o.current_install).values(battery_id=battery_id,installation_id=iid,vehicle_id=vehicle['id'],position=position))
    c.execute(insert(o.installation_evidence).values(installation_id=iid,observed_at=now(),evidence=payload['evidence'],closed=False))
    transfer=payload.get('transfer_fields') or []
    if transfer:
        draft=passport(c,'vehicle',vehicle['id'],False)
        if draft['revision']!=payload.get('dossier_revision'):raise HTTPException(409,'Ficha preliminar mudou; confira antes de transferir')
        if set(transfer)-set(draft['values']):raise HTTPException(422,'Campo selecionado não preenchido')
        physical=passport(c,'battery',battery_id,False)
        changes={k:{**draft['values'][k],'status':'draft','source':f"Ficha {draft['code']} revisão {draft['revision']}: {draft['values'][k].get('source','')}"} for k in transfer}
        save_passport(c,'battery',battery_id,dict(revision=physical['revision'],schema_version=physical['schema_version'],values=changes),user)
    audit(c,user,'battery_link','installation',iid,None,dict(vehicle_id=vehicle['id'],battery_id=battery_id,evidence=payload['evidence']))
    return {'battery_id':battery_id,'installation_id':iid}

def remove_battery(c,iid,payload,user):
    old=get(c,s.installation,iid,lock=True);get(c,s.battery_asset,old['battery_id'],lock=True)
    evidence_source(c,payload.get('evidence'),user)
    evidence=c.execute(select(o.installation_evidence).where(o.installation_evidence.c.installation_id==iid)).mappings().first()
    if (evidence and evidence['closed']) or old['removed_at']:raise HTTPException(409,'Vínculo já encerrado')
    removed=iso(payload.get('removed_at'))
    if removed and old['installed_at'] and removed<old['installed_at']:raise HTTPException(422,'Remoção anterior à instalação')
    c.execute(update(s.installation).where(s.installation.c.id==iid).values(removed_at=removed))
    values=dict(closed=True,removal_observed_at=now(),removal_evidence=payload['evidence'])
    if evidence:c.execute(update(o.installation_evidence).where(o.installation_evidence.c.installation_id==iid).values(**values))
    else:c.execute(insert(o.installation_evidence).values(installation_id=iid,observed_at=now(),evidence='Vínculo legado',**values))
    c.execute(delete(o.current_install).where(o.current_install.c.installation_id==iid))
    audit(c,user,'battery_remove','installation',iid,old,{**values,'removed_at':removed})
    return {'status':'removed'}

def add_event(c,battery_id,payload,user):
    battery=get(c,s.battery_asset,battery_id,lock=True)
    source=evidence_source(c,payload.get('evidence'),user)
    observed=iso(payload.get('observed_at'),True)
    values={k:payload[k] for k in ('soh_percent','soc_percent','cycle_count','temperature_c') if payload.get(k) is not None}
    for k in ('soh_percent','soc_percent'):
        if k in values and not 0<=values[k]<=100:raise HTTPException(422,'Percentual deve estar entre 0 e 100')
    if 'cycle_count' in values and (values['cycle_count']<0 or int(values['cycle_count'])!=values['cycle_count']):raise HTTPException(422,'Ciclos devem ser inteiros não negativos')
    from src.battery_status import validate_odometer
    values.update(validate_odometer(c, battery_id, payload, observed))
    event=dict(id=intelligence.uid(),battery_id=battery_id,source_snapshot_id=source,observed_at=observed,event_type=payload.get('event_type') or 'measurement',notes=payload.get('notes'),**values)
    c.execute(insert(s.lifecycle).values(**event))
    changes={}
    for event_key,cache_key in [('soh_percent','soh_percent'),('soc_percent','last_known_soc'),('temperature_c','last_known_temperature'),('cycle_count','cycle_count')]:
        latest=c.execute(select(s.lifecycle.c[event_key]).where(s.lifecycle.c.battery_id==battery_id,s.lifecycle.c[event_key].is_not(None))
            .order_by(s.lifecycle.c.observed_at.desc(),s.lifecycle.c.id.desc()).limit(1)).scalar()
        if latest is not None:changes[cache_key]=latest
    changes['last_observed_at']=c.execute(select(func.max(s.lifecycle.c.observed_at)).where(s.lifecycle.c.battery_id==battery_id)).scalar()
    c.execute(update(s.battery_asset).where(s.battery_asset.c.id==battery_id).values(**changes))
    audit(c,user,'battery_event','battery',battery_id,None,event)
    return event
