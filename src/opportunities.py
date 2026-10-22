"""Read-only real and simulated battery opportunity views."""
import hashlib
import heapq
import gzip
import json
import math
from threading import Lock, Thread
from bisect import bisect_left
from collections import Counter
from datetime import date
from functools import lru_cache

from sqlalchemy import and_, func, select

from src import db, intelligence_schema as s, operational_schema as o
from src.battery_passport import FIELDS


def _fraction(key):
    return int(hashlib.sha256(str(key).encode()).hexdigest()[:12], 16) / 0xFFFFFFFFFFFF


@lru_cache(maxsize=20000)
def _family_fraction(model_key):
    return _fraction('family:' + model_key)


def _unit_fractions(vid):
    digest = hashlib.sha256(str(vid).encode()).hexdigest()
    return tuple(int(digest[start:start + 12], 16) / 0xFFFFFFFFFFFF for start in (0, 12, 24, 36, 48))


# Demonstration assumptions, not observed prevalence in the SENATRAN fleet.
# Keep the exceptional tail separate from gradual degradation.
SIMULATION_VERSION = 'battery-health-demo-4'
DEMO_HEALTH_PARAMETERS = {
    'BEV': (0.015, 0.027, 0.0015),
    'PHEV': (0.018, 0.034, 0.0018),
    'HEV': (0.013, 0.032, 0.0012),
    'MHEV': (0.012, 0.031, 0.0012),
    'HEAVY': (0.017, 0.036, 0.0020),
}


def _simulated_seed(vehicle, model, locations=()):
    """Cheap shared calculation; the 71 passport fields are made only on detail."""
    vid = vehicle['id']
    year_fraction, unit, location_fraction, event_fraction, severity_fraction = _unit_fractions(vid)
    year_simulated = not bool(vehicle.get('manufacture_year'))
    simulated_year = vehicle.get('manufacture_year') or (2015 + int(year_fraction * 11))
    vehicle = {**vehicle, 'manufacture_year': simulated_year}
    model_key = '|'.join(str(model.get(k) or vehicle.get(k) or '') for k in
                         ('manufacturer', 'model', 'trim', 'manufacture_year', 'powertrain_type', 'vehicle_category'))
    family = _family_fraction(model_key)
    vehicle_age = max(0, date.today().year - (vehicle.get('manufacture_year') or 2020))
    category = model.get('vehicle_category') or 'unknown'
    powertrain = model.get('powertrain_type') or 'UNKNOWN'
    if powertrain.startswith('MHEV'):
        capacity = round(0.4 + family * 0.8, 2)
    elif powertrain.startswith('HEV') or powertrain == 'HYBRID':
        capacity = round(1.0 + family * 1.8, 2)
    elif powertrain == 'PHEV':
        capacity = round(12 + family * 28, 1)
    else:
        capacity = round((90 if category in ('bus', 'truck') else 30) + family * (240 if category in ('bus', 'truck') else 75), 1)
    chemistry = 'LFP' if family < 0.52 else 'NMC'
    health_group = ('HEAVY' if category in ('bus', 'microbus', 'truck', 'tractor_truck')
                    else 'MHEV' if powertrain.startswith('MHEV')
                    else 'HEV' if powertrain.startswith('HEV') or powertrain == 'HYBRID'
                    else 'PHEV' if powertrain == 'PHEV' else 'BEV')
    low_rate, high_rate, exceptional_share = DEMO_HEALTH_PARAMETERS[health_group]
    # A small simulated replacement chance means an old car can carry a younger pack.
    replacement = vehicle_age >= 3 and event_fraction < min(0.03, vehicle_age * 0.0015)
    age = max(0, min(vehicle_age, 1 + int(severity_fraction * vehicle_age))) if replacement else vehicle_age
    battery_year = date.today().year - age
    rate = low_rate + family * (high_rate - low_rate)
    usage = 0.7 + 0.7 * unit
    cycles = int(age * (75 + 210 * unit) * (1.6 if health_group == 'HEAVY' else 1))
    gradual_damage = rate * age * usage
    # An exceptional cell/pack event is a rare, explicit demonstration scenario.
    exceptional = age > 0 and not replacement and event_fraction >= 0.03 and event_fraction < 0.03 + exceptional_share
    shock = (0.55 + 0.45 * severity_fraction) if exceptional else 0.0
    soh = round(100 * math.exp(-(gradual_damage + shock)), 1)
    annual_loss = round(100 * (1 - math.exp(-rate * usage)), 2)
    city = vehicle.get('municipality')
    state = vehicle.get('state')
    simulated_location = False
    if not city and locations:
        if isinstance(locations, dict):
            marker = location_fraction * locations['total']
            index = min(len(locations['rows']) - 1, bisect_left(locations['cumulative'], marker))
            state, city = locations['rows'][index]
        else:
            index = min(len(locations) - 1, int(location_fraction * len(locations)))
            state, city = locations[index][:2]
        simulated_location = True
    return {'vid': vid, 'vehicle': vehicle, 'model_key': model_key, 'family': family,
            'unit': unit, 'age': age, 'battery_year': battery_year,
            'battery_origin': 'replacement' if replacement else 'original',
            'health_scenario': 'exceptional' if exceptional else 'replacement' if replacement else 'gradual',
            'capacity': capacity, 'chemistry': chemistry,
            'annual_loss': annual_loss, 'degradation_rate': rate * usage,
            'soh': soh, 'cycles': cycles, 'state': state,
            'city': city, 'simulated_location': simulated_location,
            'year_simulated': year_simulated, 'powertrain': powertrain}


def simulated_profile(vehicle, model, locations=()):
    """Stable, non-persisted demonstration profile for one registry vehicle."""
    seed = _simulated_seed(vehicle, model, locations)
    vid, vehicle, model_key = seed['vid'], seed['vehicle'], seed['model_key']
    family, unit, age = seed['family'], seed['unit'], seed['age']
    capacity, chemistry, annual_loss = seed['capacity'], seed['chemistry'], seed['annual_loss']
    soh, cycles, powertrain = seed['soh'], seed['cycles'], seed['powertrain']
    state, city, simulated_location = seed['state'], seed['city'], seed['simulated_location']
    year_simulated = seed['year_simulated']
    simulated_year = vehicle['manufacture_year']
    battery_year = seed['battery_year']
    power_health = round(100 * (max(soh, 0.1) / 100) ** 0.75, 1)
    values = {}
    dynamic = {'62': str(soh), '67': 'Em uso no veículo', '68': str(cycles),
               '70': str(round(20 + 18 * unit, 1)), '71': str(round(20 + 60 * unit, 1))}
    if powertrain in ('BEV', 'PHEV'):
        dynamic['61'] = str(round(soh * (0.97 + family * 0.03), 1))
    voltage = round(300 + family * 450)
    power = round(capacity * (1.8 + unit), 1)
    resistance = round(30 + 35 * unit, 1)
    efficiency = round(92 + 4 * family, 1)
    known = {'1': 'SIM-' + vid[:18], '2': 'NEXUS — simulação', '3': model.get('manufacturer') or 'Fabricante simulado',
             '4': 'Endereço simulado — Brasil', '5': 'Contato digital simulado', '6': 'EV' if powertrain=='BEV' else 'Bateria de veículo híbrido',
             '7': 'PACK-' + hashlib.sha256(vid.encode()).hexdigest()[:12].upper(), '8': 'Planta simulada BR-01',
             '9': str(battery_year) + '-06', '10': str(round(capacity * 6.5, 1)) + ' kg',
             '11': str(capacity) + ' kWh', '12': chemistry, '13': 'Eletrólito inflamável — cenário',
             '14': 'Agente apropriado conforme protocolo simulado de emergência',
             '15': 'Lítio, grafite' + (', níquel, cobalto' if chemistry=='NMC' else ', ferro, fosfato'),
             '16': chemistry + ' / grafite / eletrólito orgânico', '17': str(round(capacity*75))+' kg CO₂e — simulado',
             '18': 'Classe simulada B', '19': 'Declaração simulada de fornecimento responsável',
             '20': '4%', '21': '3%', '22': '5%' if chemistry=='NMC' else '0%', '23': '0%', '24': '0%',
             '25': str(round(capacity*1000/voltage,1))+' Ah', '26': str(round(voltage*0.8))+' V',
             '27': str(voltage)+' V', '28': str(round(voltage*1.15))+' V', '29': str(round(power*1000))+' W',
             '30': f'0 a {round(power*1000)} W, 0–40 °C', '31': str(round(1200+family*2000))+' ciclos',
             '32': 'Teste simulado a 25 °C e 1 C', '33': '80% de SOH — cenário',
             '34': '-20 a 45 °C', '35': '8 anos', '36': str(efficiency)+'%',
             '37': str(round(efficiency-1.2,1))+'%', '38': str(resistance)+' mΩ',
             '39': '1 C', '43': 'Entrega a reciclador autorizado — orientação simulada',
             '44': 'Manual de uso simulado: evitar dano físico e calor excessivo',
             '45': chemistry+' / ânodo grafite / eletrólito orgânico',
             '46': 'MOD-'+hashlib.sha256(model_key.encode()).hexdigest()[:8].upper(),
             '47': 'Contato de reposição simulado', '48': 'Diagrama e sequência de desmontagem simulados',
             '49': 'Desenergizar e isolar o pack antes da intervenção — cenário',
             '50': 'Laudo simulado; não é certificado de conformidade',
             '51': str(round(capacity*soh/100,2))+' kWh', '52': str(round(100-soh,1))+'%',
             '53': str(round(power*power_health*10))+' W', '54': str(round(100-power_health,1))+'%',
             '55': str(round(resistance*(1+(100-soh)/100),1))+' mΩ',
             '56': str(round(100-soh,1))+'%', '57': str(round(efficiency-(100-soh)*0.08,1))+'%',
             '58': str(round((100-soh)*0.08,1))+' ponto percentual',
             '59': str(max(0,round(1200+family*2000)-cycles))+' ciclos restantes — cenário',
             '60': str(round(max(0, math.log(max(soh, 0.1)/80)/max(seed['degradation_rate'],0.001)),1))+' anos até 80% — cenário',
             '63': str(power_health)+'%', '64': str(round((efficiency-(100-soh)*0.08)/efficiency*100,1))+'%',
             '65': 'Autodescarga simulada de '+str(round(1+unit,1))+'% ao mês',
             '66': str(round(resistance*(1+(100-soh)/100),1))+' mΩ',
             '69': 'Nenhum evento adverso simulado',
             '70': str(round(20+18*unit,1))+' °C — última amostra simulada',
             '71': str(round(20+60*unit,1))+'% — última amostra simulada'}
    known.update(dynamic)
    for field in FIELDS:
        fid = field['id']
        if fid in known:
            values[fid] = {'value': known[fid], 'status': 'confirmed', 'source': 'SIMULAÇÃO — cenário NEXUS', 'reason': ''}
        elif fid in {'40', '41', '42', '61'}:
            values[fid] = {'value': None, 'status': 'not_applicable', 'source': 'SIMULAÇÃO — cenário NEXUS',
                           'reason': ('SOCE não aplicável a esta categoria de híbrido nesta demonstração.' if fid == '61'
                                      else 'Documento regulatório não usado nesta demonstração brasileira.')}
        else:
            values[fid] = {'value': 'Parâmetro simulado: ' + field['label'].lower(), 'status': 'confirmed',
                           'source': 'SIMULAÇÃO — cenário NEXUS', 'reason': ''}
    return {'id': vid, 'vehicle_id': vid, 'battery_id': 'sim-battery-' + vid,
            'manufacture_year': simulated_year, 'year_simulated': year_simulated,
            'battery_manufacture_year': battery_year, 'battery_origin': seed['battery_origin'],
            'health_scenario': seed['health_scenario'], 'simulation_version': SIMULATION_VERSION,
            'capacity_kwh': capacity, 'chemistry': chemistry, 'soh_percent': soh, 'cycle_count': cycles,
            'annual_degradation_pct': round(annual_loss, 2), 'state': state, 'municipality': city,
            'location_simulated': simulated_location, 'passport': {'fields': FIELDS, 'values': values,
            'counts': {'confirmed': sum(v['status'] == 'confirmed' for v in values.values()),
                       'not_applicable': sum(v['status'] == 'not_applicable' for v in values.values()),
                       'pending': 0, 'draft': 0, 'total': len(FIELDS)}},
            'source': 'SIMULAÇÃO — parâmetros demonstrativos por categoria; não é medição nem prevalência da SENATRAN'}


def status(soh, threshold, previous=None, latest_at=None):
    if soh is None:
        return {'status': 'insufficient', 'reason': 'Saúde da bateria não medida', 'distance_pct': None,
                'trend_pct': None, 'threshold_pct': threshold}
    distance = round(soh - threshold, 1)
    trend = round(soh - previous, 1) if previous is not None else None
    if latest_at and (date.today() - date.fromisoformat(str(latest_at)[:10])).days > 365:
        label = 'stale'
    else:
        label = 'priority' if distance <= 0 else 'near' if distance <= 5 else 'monitor'
    return {'status': label, 'reason': f'SOH {soh:.1f}% − limite {threshold:.1f}% = {distance:+.1f} pontos',
            'distance_pct': distance, 'trend_pct': trend, 'threshold_pct': threshold}


def real_rows(c, reference=None, state=None, municipality=None, category=None, powertrain=None, model=None, limit=50, offset=0, threshold=80, status_filter=None):
    # Identified vehicles only. Statistical placeholders never become real opportunities.
    q = select(s.vehicle_asset, o.registry.c.public_code, o.model.c.model.label('registry_model'),
               o.model.c.manufacturer.label('registry_manufacturer'), s.battery_asset.c.id.label('battery_id'),
               s.battery_asset.c.soh_percent, s.battery_asset.c.last_observed_at, s.battery_asset.c.cycle_count).select_from(
        s.vehicle_asset.join(o.registry, o.registry.c.vehicle_id == s.vehicle_asset.c.id)
        .join(o.model, o.model.c.id == o.registry.c.current_model_id)
        .join(o.current_install, o.current_install.c.vehicle_id == s.vehicle_asset.c.id)
        .join(s.battery_asset, s.battery_asset.c.id == o.current_install.c.battery_id)).where(
            o.registry.c.visible.is_(True), s.vehicle_asset.c.identity_status.in_(['identified', 'verified']))
    if reference:
        q = q.join(s.fleet_unit_membership, s.fleet_unit_membership.c.vehicle_id == s.vehicle_asset.c.id).join(
            s.fleet_publication, s.fleet_publication.c.id == s.fleet_unit_membership.c.publication_id).where(
            s.fleet_publication.c.reference_period == reference)
    for col, val in ((s.vehicle_asset.c.state, state), (s.vehicle_asset.c.municipality, municipality),
                     (o.model.c.vehicle_category, category), (o.model.c.powertrain_type, powertrain)):
        if val: q = q.where(col == val)
    if model: q = q.where(o.model.c.model.ilike('%' + model + '%'))
    output = []
    for row in c.execute(q).mappings():
        item = dict(row)
        latest = c.execute(select(s.lifecycle.c.soh_percent, s.lifecycle.c.observed_at).where(
            s.lifecycle.c.battery_id == item['battery_id'], s.lifecycle.c.soh_percent.is_not(None))
            .order_by(s.lifecycle.c.observed_at.desc()).limit(2)).all()
        soh = latest[0][0] if latest else item.get('soh_percent')
        observed = latest[0][1] if latest else item.get('last_observed_at')
        previous = latest[1][0] if len(latest) > 1 else None
        item.update(status(soh, threshold, previous, observed))
        item['soh_percent'] = soh
        item['observed_at'] = observed
        output.append(item)
    order = {'priority': 0, 'near': 1, 'monitor': 2, 'stale': 3, 'insufficient': 4}
    output.sort(key=lambda x: (order[x['status']], x['distance_pct'] if x['distance_pct'] is not None else 999, x['id']))
    if status_filter:output=[item for item in output if item['status']==status_filter]
    return {'items': output[offset:offset + limit], 'total': len(output), 'limit': limit, 'offset': offset}


_simulated_cache = {}
_simulated_cache_lock = Lock()


def _store_snapshot(path, rows):
    """Disposable compact simulation index, separate from real records."""
    models, model_ids, places, place_ids, packed = [], {}, [], {}, []
    for row in rows:
        model = (row['registry_manufacturer'], row['registry_model'], row['registry_category'], row['registry_powertrain'])
        place = (row['state'], row['municipality'], row['location_simulated'])
        if model not in model_ids:
            model_ids[model] = len(models); models.append(model)
        if place not in place_ids:
            place_ids[place] = len(places); places.append(place)
        packed.append((row['id'], row['public_code'], model_ids[model], place_ids[place],
                       row['manufacture_year'], row['year_simulated'], row['soh_percent'],
                       row['capacity_kwh'], row['chemistry'], row['health_scenario']))
    temp_path = path.with_suffix('.part')
    try:
        with gzip.open(temp_path, 'wt', encoding='utf-8', compresslevel=1) as cached:
            json.dump({'models': models, 'places': places, 'rows': packed}, cached,
                      ensure_ascii=False, separators=(',', ':'))
        temp_path.replace(path)
        for old in path.parent.glob('opportunities-*.json.gz'):
            if old != path: old.unlink(missing_ok=True)
    finally:
        temp_path.unlink(missing_ok=True)


def _load_snapshot(path):
    with gzip.open(path, 'rt', encoding='utf-8') as cached:
        packed = json.load(cached)
    today = date.today().isoformat()
    rows = []
    for vid, code, model_index, place_index, year, year_simulated, soh, capacity, chemistry, health_scenario in packed['rows']:
        manufacturer, model, category, powertrain = packed['models'][model_index]
        state, city, location_simulated = packed['places'][place_index]
        rows.append({'id': vid, 'vehicle_id': vid, 'battery_id': 'sim-battery-' + vid,
                     'public_code': code, 'registry_manufacturer': manufacturer,
                     'registry_model': model, 'registry_category': category,
                     'registry_powertrain': powertrain, 'state': state,
                     'municipality': city, 'location_simulated': location_simulated,
                     'manufacture_year': year, 'year_simulated': year_simulated,
                     'soh_percent': soh, 'capacity_kwh': capacity, 'chemistry': chemistry,
                     'health_scenario': health_scenario,
                     'observed_at': today})
    return rows


def _simulated_snapshot(c, reference):
    """Cache synthetic summaries briefly in RAM; never persist them in the database."""
    q = select(s.vehicle_asset.c.id, s.vehicle_asset.c.manufacture_year,
               s.vehicle_asset.c.state, s.vehicle_asset.c.municipality,
               o.registry.c.public_code, o.model.c.model.label('registry_model'),
               o.model.c.manufacturer.label('registry_manufacturer'), o.model.c.vehicle_category.label('registry_category'),
               o.model.c.powertrain_type.label('registry_powertrain')).select_from(
        s.vehicle_asset.join(o.registry, o.registry.c.vehicle_id == s.vehicle_asset.c.id)
        .join(o.model, o.model.c.id == o.registry.c.current_model_id)).where(o.registry.c.visible.is_(True))
    if reference:
        q = q.join(s.fleet_unit_membership, s.fleet_unit_membership.c.vehicle_id == s.vehicle_asset.c.id).join(
            s.fleet_publication, s.fleet_publication.c.id == s.fleet_unit_membership.c.publication_id).where(
            s.fleet_publication.c.reference_period == reference)
    q = q.distinct()
    signature = c.execute(select(func.count(), func.coalesce(func.sum(o.registry.c.revision), 0))
        .where(o.registry.c.visible.is_(True))).one()
    publication_version = c.execute(select(func.max(s.fleet_publication.c.finished_at))).scalar()
    cache_key = (str(c.engine.url), id(c.engine), reference, tuple(signature), publication_version, date.today().isoformat())
    with _simulated_cache_lock:
        if cache_key in _simulated_cache:
            return _simulated_cache[cache_key]
        cache_path = None
        if c.engine.dialect.name == 'postgresql':
            cache_dir = db.ROOT / 'data' / 'cache'
            cache_dir.mkdir(parents=True, exist_ok=True)
            filename = hashlib.sha256(repr((reference, tuple(signature), publication_version,
                                            date.today().isoformat(), SIMULATION_VERSION)).encode()).hexdigest()[:24]
            cache_path = cache_dir / ('opportunities-' + filename + '.json.gz')
            if cache_path.is_file():
                try:
                    rows = _load_snapshot(cache_path)
                    _simulated_cache.clear()
                    _simulated_cache[cache_key] = rows
                    return rows
                except (OSError, ValueError, json.JSONDecodeError):
                    cache_path.unlink(missing_ok=True)
        locations = location_weights(c)
        rows = []
        for row in c.execute(q).mappings():
            item = dict(row)
            seed = _simulated_seed(item, {'manufacturer': item['registry_manufacturer'], 'model': item['registry_model'],
                'powertrain_type': item['registry_powertrain'], 'vehicle_category': item['registry_category']}, locations)
            item.update(vehicle_id=item['id'], battery_id='sim-battery-' + item['id'],
                        manufacture_year=seed['vehicle']['manufacture_year'], year_simulated=seed['year_simulated'],
                        state=seed['state'], municipality=seed['city'], location_simulated=seed['simulated_location'],
                        soh_percent=seed['soh'], capacity_kwh=seed['capacity'], chemistry=seed['chemistry'],
                        health_scenario=seed['health_scenario'])
            item['observed_at'] = date.today().isoformat()
            rows.append(item)
        _simulated_cache.clear()
        _simulated_cache[cache_key] = rows
        if cache_path:
            Thread(target=_store_snapshot, args=(cache_path, rows), daemon=True).start()
        return rows


def _filtered_simulation(c, reference, state, municipality, category, powertrain, model):
    model_term = (model or '').casefold()
    return (item for item in _simulated_snapshot(c, reference)
            if (not state or item['state'] == state)
            and (not municipality or (item['municipality'] or '').casefold() == municipality.casefold())
            and (not category or item['registry_category'] == category)
            and (not powertrain or item['registry_powertrain'] == powertrain)
            and (not model_term or model_term in (item['registry_model'] or '').casefold()))


def simulated_rows(c, reference=None, state=None, municipality=None, category=None, powertrain=None, model=None, limit=50, offset=0, threshold=80, status_filter=None):
    count = 0
    order = {'priority': 0, 'near': 1, 'monitor': 2}
    def candidates():
        nonlocal count
        for source in _filtered_simulation(c, reference, state, municipality, category, powertrain, model):
            item = {**source, **status(source['soh_percent'], threshold)}
            if status_filter and item['status'] != status_filter: continue
            count += 1
            yield item
    output = heapq.nsmallest(offset + limit, candidates(),
        key=lambda x: (order[x['status']], x['distance_pct'], x['id']))
    return {'items': output[offset:offset + limit], 'total': count, 'limit': limit, 'offset': offset}


def overview(c, mode='real', reference=None, state=None, municipality=None, category=None, powertrain=None, model=None, threshold=80):
    if mode == 'simulated':
        sources = _filtered_simulation(c, reference, state, municipality, category, powertrain, model)
    else:
        sources = real_rows(c, reference, state, municipality, category, powertrain, model,
                            limit=1000000, threshold=threshold)['items']
    counts = Counter()
    places = Counter()
    models = Counter()
    for item in sources:
        label = status(item['soh_percent'], threshold, latest_at=item.get('observed_at'))['status'] if mode == 'simulated' else item['status']
        counts[label] += 1
        if label in ('priority', 'near') and item.get('state') and item.get('municipality'):
            places[(item['state'], item['municipality'])] += 1
        if label == 'priority':
            models[(item.get('registry_manufacturer') or item.get('manufacturer'),
                    item.get('registry_model') or item.get('model'),
                    item.get('registry_powertrain') or item.get('powertrain_type'))] += 1
    return {'total': sum(counts.values()), 'priority': counts['priority'], 'near': counts['near'],
            'monitor': counts['monitor'], 'pending': counts['stale'] + counts['insufficient'],
            'top_places': [{'state': uf, 'municipality': city, 'candidates': quantity}
                           for (uf, city), quantity in places.most_common(5)],
            'focus_models': [{'manufacturer': manufacturer, 'model': model,
                              'powertrain_type': powertrain, 'candidates': quantity}
                             for (manufacturer, model, powertrain), quantity in models.most_common(4)],
            'mode': mode}


def location_weights(c):
    """Municipal distribution from the latest aggregate reference, for simulation only."""
    latest = c.execute(select(func.max(s.aggregate.c.reference_period))).scalar()
    if not latest:
        return []
    weighted = [tuple(row) for row in c.execute(select(s.aggregate.c.state, s.aggregate.c.municipality,
        func.sum(s.aggregate.c.quantity)).where(s.aggregate.c.reference_period == latest,
        s.aggregate.c.state.is_not(None), s.aggregate.c.municipality.is_not(None))
        .group_by(s.aggregate.c.state, s.aggregate.c.municipality)
        .order_by(s.aggregate.c.state, s.aggregate.c.municipality))]
    rows, cumulative, total = [], [], 0
    for state, city, quantity in weighted:
        if not quantity or quantity <= 0: continue
        total += quantity
        rows.append((state, city))
        cumulative.append(total)
    return {'rows': rows, 'cumulative': cumulative, 'total': total} if total else []
