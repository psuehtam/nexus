"""Evidence-based warranty and health summaries; no replacement-date prediction."""
import calendar
from datetime import date, datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select, insert, update
from fastapi import HTTPException
from src import intelligence_schema as s, registry_service as registry


def add_months(start, months):
    year, month = divmod(start.year * 12 + start.month - 1 + months, 12)
    return date(year, month + 1, min(start.day, calendar.monthrange(year, month + 1)[1]))


def validate(data):
    start = date.fromisoformat(data['start_date']) if data.get('start_date') else None
    end = date.fromisoformat(data['end_date']) if data.get('end_date') else None
    months = data.get('duration_months')
    try:
        calculated = add_months(start, months) if start and months is not None else None
    except ValueError as exc:
        raise HTTPException(422, 'Data de término fora do intervalo suportado.') from exc
    if end and start and end < start:
        raise HTTPException(422, 'O termino deve ser posterior ou igual ao inicio.')
    if end and calculated and end != calculated:
        raise HTTPException(422, 'Data de termino e duracao informadas divergem.')
    if data.get('km_basis') == 'since_start' and data.get('baseline_km') is None:
        raise HTTPException(422, 'Informe o hodometro inicial para contar quilometros desde o inicio.')
    return end or calculated


def save(c, battery_id, payload, user):
    registry.get(c, s.battery_asset, battery_id, lock=True)
    prior = c.execute(select(s.warranty).where(s.warranty.c.battery_id == battery_id)).mappings().first()
    if payload['revision'] != (prior['revision'] if prior else 0):
        raise HTTPException(409, 'Garantia alterada por outra pessoa. Recarregue antes de salvar.')
    data = {k: v for k, v in payload.items() if k != 'revision'}
    validate(data)
    if data.get('installation_id'):
        installation = registry.get(c, s.installation, data['installation_id'])
        if installation['battery_id'] != battery_id:
            raise HTTPException(422, 'A instalacao nao pertence a esta bateria.')
    source_id = registry.evidence_source(c, data['evidence'], user)
    data['source_snapshot_id'] = source_id
    values = dict(revision=payload['revision'] + 1, data=data, updated_at=datetime.now(ZoneInfo('America/Sao_Paulo')).isoformat())
    if prior:
        c.execute(update(s.warranty).where(s.warranty.c.battery_id == battery_id).values(**values))
    else:
        c.execute(insert(s.warranty).values(battery_id=battery_id, **values))
    registry.audit(c, user, 'battery_warranty', 'battery', battery_id, dict(prior) if prior else None, values)
    return {'revision': values['revision']}


def summary(c, battery_id, today=None):
    today = today or datetime.now(ZoneInfo('America/Sao_Paulo')).date()
    battery = registry.get(c, s.battery_asset, battery_id)
    model = registry.get(c, s.battery_model, battery['battery_model_id']) if battery['battery_model_id'] else {}
    record = c.execute(select(s.warranty).where(s.warranty.c.battery_id == battery_id)).mappings().first()
    data = dict(record['data']) if record else {}
    end = validate(data)
    days = (end - today).days if end else None
    term = 'unknown' if days is None else 'expired' if days < 0 else 'expiring' if days <= 90 else 'within_term'
    if data.get('start_date') and data['start_date'] > today.isoformat(): term = 'not_started'
    observations = [dict(r) for r in c.execute(select(s.lifecycle).where(s.lifecycle.c.battery_id == battery_id)
                    .order_by(s.lifecycle.c.observed_at.desc(), s.lifecycle.c.id.desc())).mappings()]
    def last(field):
        return next((x for x in observations if x.get(field) is not None), None)
    soh, cycles = last('soh_percent'), last('cycle_count')
    reading = next((x for x in observations if x.get('odometer_km') is not None and data.get('installation_id') and x.get('installation_id') == data['installation_id']), None)
    current_installations = set(c.execute(select(s.installation.c.id).where(s.installation.c.battery_id == battery_id, s.installation.c.removed_at.is_(None))).scalars())
    km_remaining = None
    if data.get('km_limit') is not None and reading and data.get('km_basis') and data.get('installation_id') in current_installations:
        used = reading['odometer_km'] - (data.get('baseline_km') or 0) if data['km_basis'] == 'since_start' else reading['odometer_km']
        if used >= 0: km_remaining = data['km_limit'] - used
    km_status = 'unknown' if km_remaining is None else 'limit_reached' if km_remaining <= 0 else 'below_limit_at_measurement'
    threshold = model.get('automotive_eol_threshold_percent')
    expected = model.get('expected_cycle_life')
    health = {'soh_percent': soh['soh_percent'] if soh else None, 'soh_observed_at': soh['observed_at'] if soh else None,
              'cycle_count': cycles['cycle_count'] if cycles else None, 'cycles_observed_at': cycles['observed_at'] if cycles else None,
              'soh_threshold': threshold, 'expected_cycles': expected, 'field_sources': model.get('field_sources') or {},
              'soh_limit_reached': bool(soh and threshold is not None and soh['soh_percent'] <= threshold),
              'cycle_limit_reached': bool(cycles and expected is not None and cycles['cycle_count'] >= expected)}
    missing = []
    if end is None: missing.append('Data de termino ou inicio e duracao')
    if km_remaining is None: missing.append('Limite, base de contagem e leitura da instalacao atual para avaliar quilometragem')
    return {'warranty': {'revision': record['revision'] if record else 0, 'data': data, 'end_date': end.isoformat() if end else None,
                         'days_remaining': days, 'term_status': term, 'km_remaining': km_remaining, 'km_status': km_status,
                         'km_observed_at': reading['observed_at'] if reading else None, 'missing': missing,
                         'suggestion': {'years': model.get('warranty_years'), 'km': model.get('warranty_km'), 'sources': model.get('field_sources') or {}}},
            'health': health}


def validate_odometer(c, battery_id, payload, observed):
    if payload.get('odometer_km') is None: return {}
    if not payload.get('installation_id'):
        raise HTTPException(422, 'Selecione a instalacao para registrar o hodometro.')
    installation = registry.get(c, s.installation, payload['installation_id'])
    if installation['battery_id'] != battery_id:
        raise HTTPException(422, 'Instalacao de outra bateria.')
    if not installation.get('installed_at'):
        raise HTTPException(422, 'Data da instalacao necessaria para validar a leitura.')
    instant = datetime.fromisoformat(observed.replace('Z', '+00:00'))
    def moment(value):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.replace(tzinfo=instant.tzinfo) if parsed.tzinfo is None else parsed
    if instant < moment(installation['installed_at']) or (installation.get('removed_at') and instant >= moment(installation['removed_at'])):
        raise HTTPException(422, 'Leitura fora do periodo da instalacao.')
    readings = c.execute(select(s.lifecycle).where(s.lifecycle.c.installation_id == installation['id'], s.lifecycle.c.odometer_km.is_not(None))).mappings()
    for previous in readings:
        prior_time = moment(previous['observed_at'])
        if (prior_time <= instant and previous['odometer_km'] > payload['odometer_km']) or (prior_time >= instant and previous['odometer_km'] < payload['odometer_km']):
            raise HTTPException(422, 'Hodometro inconsistente com as leituras desta instalacao.')
    return {'odometer_km': payload['odometer_km'], 'installation_id': installation['id'], 'vehicle_id': installation['vehicle_id']}
