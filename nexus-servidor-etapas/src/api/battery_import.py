"""Documented, idempotent entry point for partner battery measurements."""
import hashlib
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import insert, select

from src import db, intelligence_schema as s, operational_schema as o, registry_service as registry
from src.api.intelligence import actor, permit
from src.battery_passport import FIELDS
from src.connectors.public import now

router = APIRouter(prefix='/api/v3/battery-import', tags=['Importação de baterias'])
FIELD_IDS = {f['id'] for f in FIELDS}


class PassportField(BaseModel):
    value: str | None = None
    status: str = 'confirmed'
    source: str
    reason: str = ''


class Measurement(BaseModel):
    observed_at: datetime
    soh_percent: float = Field(ge=0, le=100)
    cycle_count: int | None = Field(None, ge=0)
    soc_percent: float | None = Field(None, ge=0, le=100)
    temperature_c: float | None = Field(None, ge=-100, le=300)

    @field_validator('observed_at')
    @classmethod
    def timezone(cls, value):
        if value.tzinfo is None: raise ValueError('A medição precisa de fuso horário')
        return value


class BatteryImport(BaseModel):
    source_url: str = Field(pattern=r'^https://', max_length=2000)
    source_record_id: str = Field(min_length=1, max_length=200)
    vehicle_code: str
    vin: str = Field(pattern=r'^[A-HJ-NPR-Z0-9]{17}$')
    state: str | None = Field(None, pattern=r'^[A-Z]{2}$')
    municipality: str | None = None
    battery_manufacturer: str = Field(min_length=2)
    battery_serial: str = Field(min_length=2)
    installed_at: str | None = None
    passport: dict[str, PassportField] = Field(default_factory=dict)
    measurements: list[Measurement] = Field(default_factory=list)

    @field_validator('passport')
    @classmethod
    def fields(cls, value):
        if set(value) - FIELD_IDS: raise ValueError('Passaporte contém campo desconhecido')
        for field in value.values():
            if field.status not in ('confirmed', 'draft', 'not_applicable'): raise ValueError('Estado de campo inválido')
            if field.status == 'not_applicable' and not field.reason.strip(): raise ValueError('Não aplicável exige motivo')
            if field.status != 'not_applicable' and not (field.value or '').strip(): raise ValueError('Campo exige valor')
            if not field.source.strip(): raise ValueError('Campo exige fonte')
        return value


def _check(c, body):
    reg = registry.resolve_vehicle(c, body.vehicle_code)
    vehicle = registry.get(c, s.vehicle_asset, reg['vehicle_id'])
    if vehicle.get('vin') and vehicle['vin'] != body.vin: raise HTTPException(409, 'Chassi não corresponde ao veículo')
    owner = c.execute(select(s.vehicle_asset.c.id).where(s.vehicle_asset.c.vin == body.vin)).scalar()
    if owner and owner != vehicle['id']: raise HTTPException(409, 'Chassi pertence a outro veículo')
    battery = c.execute(select(s.battery_asset).where(s.battery_asset.c.manufacturer_serial_number == body.battery_serial)).mappings().first()
    if battery:
        current = c.execute(select(o.current_install).where(o.current_install.c.battery_id == battery['id'])).mappings().first()
        if current and current['vehicle_id'] != vehicle['id']: raise HTTPException(409, 'Bateria vinculada a outro veículo')
    return reg, vehicle, dict(battery) if battery else None


@router.post('/validate')
def validate(body: BatteryImport, identity=Depends(actor)):
    permit(identity, {'admin', 'data_engineer'})
    with db.engine.connect() as c:
        reg, vehicle, battery = _check(c, body)
    return {'valid': True, 'vehicle_id': vehicle['id'], 'battery_id': battery['id'] if battery else None,
            'new_battery': battery is None, 'passport_fields': len(body.passport), 'measurements': len(body.measurements)}


@router.post('/records')
def import_record(body: BatteryImport, identity=Depends(actor)):
    permit(identity, {'admin', 'data_engineer'})
    encoded = json.dumps(body.model_dump(mode='json'), sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    key = 'battery-import:' + body.source_url + ':' + body.source_record_id
    user = {'username': identity['actor'], 'role': identity['role']}
    with db.engine.begin() as c:
        previous = c.execute(select(o.command).where(o.command.c.key == key)).mappings().first()
        if previous:
            if previous['body_hash'] != digest: raise HTTPException(409, 'Mesmo identificador de origem com conteúdo diferente; envie uma revisão identificada')
            return previous['result']
        reg, vehicle, battery = _check(c, body)
        evidence = body.source_url + ' #' + body.source_record_id
        if vehicle.get('vin') != body.vin or vehicle.get('identity_status') != 'verified' or body.state or body.municipality:
            identity_data = {'revision': reg['revision'], 'vin': body.vin, 'verified': True, 'evidence': evidence}
            if body.state is not None: identity_data['state'] = body.state
            if body.municipality is not None: identity_data['municipality'] = body.municipality
            registry.identify(c, reg['public_code'], identity_data, user)
        if battery is None:
            linked = registry.link_battery(c, reg['public_code'], {'serial': body.battery_serial,
                'manufacturer': body.battery_manufacturer, 'installed_at': body.installed_at,
                'evidence': evidence}, user)
            battery_id = linked['battery_id']
        else:
            battery_id = battery['id']
            current = c.execute(select(o.current_install).where(o.current_install.c.battery_id == battery_id)).mappings().first()
            if not current: registry.link_battery(c, reg['public_code'], {'battery_id': battery_id,
                'installed_at': body.installed_at, 'evidence': evidence}, user)
        if body.passport:
            existing = registry.passport(c, 'battery', battery_id, False)
            changes = {fid: field.model_dump() for fid, field in body.passport.items()
                       if existing['values'].get(fid, {}).get('value') != field.value or existing['values'].get(fid, {}).get('status') != field.status}
            if changes: registry.save_passport(c, 'battery', battery_id, {'revision': existing['revision'],
                'schema_version': existing['schema_version'], 'values': changes}, user)
        for measurement in sorted(body.measurements, key=lambda m: m.observed_at):
            observed = measurement.observed_at.isoformat()
            existing = c.execute(select(s.lifecycle.c.id).where(s.lifecycle.c.battery_id == battery_id,
                s.lifecycle.c.observed_at == registry.iso(observed))).scalar()
            if existing: raise HTTPException(409, 'Já existe medição nesta data; envie uma revisão identificada')
            registry.add_event(c, battery_id, {'observed_at': observed, 'event_type': 'measurement',
                'soh_percent': measurement.soh_percent, 'cycle_count': measurement.cycle_count,
                'soc_percent': measurement.soc_percent, 'temperature_c': measurement.temperature_c,
                'evidence': evidence}, user)
        result = {'vehicle_id': vehicle['id'], 'battery_id': battery_id, 'passport_fields': len(body.passport),
                  'measurements': len(body.measurements), 'source_record_id': body.source_record_id}
        c.execute(insert(o.command).values(key=key, body_hash=digest, result=result, created_at=now()))
        return result
