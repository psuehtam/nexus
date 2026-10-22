import json
from sqlalchemy import select, func
from src import intelligence_schema as s, operational_schema as o
from src.opportunities import simulated_profile, status, _simulated_seed
from test_registry import generate, first, account


def test_simulation_is_complete_stable_and_does_not_write(client, database):
    generate(database, 3)
    _, unit = first(client)
    before = client.get('/api/v3/vehicles/' + unit['id'] + '/battery-dossier').json()
    with database.connect() as c:
        vehicle = dict(c.execute(select(s.vehicle_asset).where(s.vehicle_asset.c.id == unit['id'])).mappings().one())
        model = dict(c.execute(select(o.model).where(o.model.c.id == unit['current_model_id'])).mappings().one())
    a = simulated_profile(vehicle, model, [('PR', 'Curitiba')])
    b = simulated_profile(vehicle, model, [('PR', 'Curitiba')])
    assert a == b
    assert len(a['passport']['values']) == 71
    assert a['passport']['counts']['pending'] == 0
    fields = a['passport']['values']
    assert float(fields['51']['value'].split()[0]) == round(a['capacity_kwh'] * a['soh_percent'] / 100, 2)
    assert float(fields['52']['value'].rstrip('%')) == round(100 - a['soh_percent'], 1)
    assert float(fields['62']['value']) == a['soh_percent']
    assert a['simulation_version']
    assert a['battery_manufacture_year'] <= a['manufacture_year'] or a['battery_origin'] == 'replacement'
    assert a['location_simulated']
    with database.connect() as c:
        assert c.execute(select(func.count()).select_from(s.battery_asset)).scalar_one() == 0
    assert client.get('/api/v3/vehicles/' + unit['id'] + '/battery-dossier').json() == before


def test_simulation_has_a_rare_low_health_tail_without_changing_model_specifications():
    model = {'manufacturer': 'Exemplo', 'model': 'Elétrico', 'powertrain_type': 'BEV',
             'vehicle_category': 'car', 'manufacture_year': 2015}
    seeds = [_simulated_seed({'id': f'demo-{i}', 'manufacture_year': 2015}, model)
             for i in range(10000)]
    low = [seed for seed in seeds if seed['soh'] < 50]
    assert 0 < len(low) < 100
    assert all(seed['health_scenario'] == 'exceptional' for seed in low)
    assert len({seed['capacity'] for seed in seeds}) == 1
    assert len({seed['chemistry'] for seed in seeds}) == 1
    assert all(0 < seed['soh'] <= 100 for seed in seeds)
    assert all(seed['battery_year'] >= seed['vehicle']['manufacture_year'] for seed in seeds)


def test_health_status_uses_limit_and_data_freshness():
    assert status(79, 80)['status'] == 'priority'
    assert status(40, 40)['status'] == 'priority'
    assert status(39, 40)['status'] == 'priority'
    assert status(41, 40)['status'] == 'near'
    assert status(46, 40)['status'] == 'monitor'
    assert status(40, 40)['distance_pct'] == 0
    assert status(39, 40)['distance_pct'] == -1
    assert status(83, 80)['status'] == 'near'
    assert status(90, 80)['status'] == 'monitor'
    assert status(None, 80)['status'] == 'insufficient'
    assert status(78, 80, latest_at='2020-01-01')['status'] == 'stale'


def test_mode_switch_is_reversible_for_multiple_months(client, database):
    generate(database, 2, 'jan', '2026-01')
    generate(database, 3, 'feb', '2026-02')
    headers=account(client)
    before=client.get('/api/v3/opportunities?reference=2026-01').json()
    assert before['mode']=='real' and before['total']==0
    changed=client.put('/api/v3/admin/opportunities/settings',json={'mode':'simulated'},headers=headers)
    assert changed.status_code==200,changed.text
    first_month=client.get('/api/v3/opportunities?reference=2026-01').json()
    second_month=client.get('/api/v3/opportunities?reference=2026-02').json()
    assert first_month['total']==2 and second_month['total']==3
    detail=client.get('/api/v3/opportunities/simulated/'+first_month['items'][0]['id']+'/passport').json()
    assert detail['passport']['counts']['total']==71
    assert client.put('/api/v3/admin/opportunities/settings',json={'mode':'real'},headers=headers).status_code==200
    assert client.get('/api/v3/opportunities?reference=2026-01').json()==before


def test_opportunity_health_limit_accepts_40_and_rejects_out_of_range(client):
    headers = account(client)
    response = client.put('/api/v3/admin/opportunities/settings', json={'soh_limit': 40}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['soh_limit'] == 40
    assert client.get('/api/v3/opportunities').json()['soh_limit'] == 40
    assert client.get('/api/v3/opportunities/overview').json()['soh_limit'] == 40
    for invalid in (-0.1, 100.1):
        assert client.put('/api/v3/admin/opportunities/settings', json={'soh_limit': invalid}, headers=headers).status_code == 422
    assert client.get('/api/v3/opportunities/settings').json()['soh_limit'] == 40


def test_partner_import_preview_and_idempotency(client, database, monkeypatch):
    generate(database, 1)
    _, unit = first(client)
    monkeypatch.setenv('I9_ROLE_TOKENS', json.dumps([{'name':'partner-test','role':'data_engineer','key':'safe-test-token'}]))
    headers = {'X-API-Key':'safe-test-token'}
    body = {'source_url':'https://example.org/battery-report', 'source_record_id':'report-1',
            'vehicle_code':unit['public_identifier'], 'vin':'1HGBH41JXMN109186',
            'state':'PR','municipality':'Curitiba','battery_manufacturer':'Example Cells',
            'battery_serial':'CELL-001','installed_at':'2026-01-01',
            'passport':{'12':{'value':'LFP','status':'confirmed','source':'https://example.org/battery-report'}},
            'measurements':[{'observed_at':'2026-09-01T12:00:00Z','soh_percent':79,'cycle_count':800}]}
    preview = client.post('/api/v3/battery-import/validate',json=body,headers=headers)
    assert preview.status_code == 200,preview.text
    with database.connect() as c:assert c.execute(select(func.count()).select_from(s.battery_asset)).scalar_one()==0
    first_result = client.post('/api/v3/battery-import/records',json=body,headers=headers)
    assert first_result.status_code == 200,first_result.text
    again = client.post('/api/v3/battery-import/records',json=body,headers=headers)
    assert again.status_code == 200 and again.json() == first_result.json()
    with database.connect() as c:
        assert c.execute(select(func.count()).select_from(s.battery_asset)).scalar_one()==1
        assert c.execute(select(func.count()).select_from(s.lifecycle)).scalar_one()==1
    ranked=client.get('/api/v3/opportunities').json()
    assert ranked['items'][0]['status']=='priority'
