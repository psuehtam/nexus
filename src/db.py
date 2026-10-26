"""Portable relational storage; PostgreSQL in production, SQLite for local preview."""
import os
from pathlib import Path
from sqlalchemy import (create_engine, MetaData, Table, Column, Integer, String, Float,
                        Text, JSON, ForeignKey, UniqueConstraint, CheckConstraint, event, Index, func)

ROOT = Path(__file__).resolve().parents[1]
# Load local configuration consistently for CLI and API; explicit environment wins.
if (ROOT/'.env').exists():
    for line in (ROOT/'.env').read_text(encoding='utf-8').splitlines():
        if line and not line.startswith('#') and '=' in line:
            key,value=line.split('=',1)
            if key in ('DATABASE_URL','ADMIN_API_KEY','ALLOW_SOURCE_DOWNLOAD','CORS_ORIGINS','I9_ROLE_TOKENS','I9_SCHEDULER_ENABLED') or key.startswith('I9_'):
                os.environ.setdefault(key,value)
(ROOT / 'data').mkdir(exist_ok=True)
engine = create_engine(os.getenv('DATABASE_URL', f'sqlite:///{ROOT / "data/i9.db"}'),
                       connect_args={'check_same_thread': False} if os.getenv('DATABASE_URL', 'sqlite').startswith('sqlite') else {})
if engine.dialect.name == 'sqlite':
    @event.listens_for(engine, 'connect')
    def sqlite_config(conn, _):
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=30000')
meta = MetaData()
sources = Table('fonte_dado', meta, Column('id', String, primary_key=True),
    Column('name', String, nullable=False), Column('url', Text), Column('kind', String),
    Column('collected_at', String), Column('reference', String), Column('details', JSON))
models = Table('modelo_veiculo', meta, Column('id', String, primary_key=True),
    Column('brand', String, nullable=False), Column('model', String, nullable=False),
    Column('version', String, nullable=False), Column('year_start', Integer), Column('year_end', Integer),
    Column('classification_source', Text), Column('aliases', JSON), Column('category', String, default='BEV'),
    Column('propulsion', String, default='BEV'))
batteries = Table('bateria', meta, Column('id', String, primary_key=True),
    Column('model_id', ForeignKey('modelo_veiculo.id'), nullable=False),
    Column('capacity_kwh', Float), Column('usable_kwh', Float), Column('chemistry', String),
    Column('technology', String), Column('voltage', Float), Column('pack_manufacturer', String),
    Column('part_number', String),
    Column('warranty_years', Integer), Column('warranty_km', Integer), Column('warranty_notes', Text),
    Column('provenance', JSON, nullable=False), Column('confidence', String),
    CheckConstraint('capacity_kwh > 0 AND capacity_kwh <= 500'))
locations = Table('localidade', meta, Column('id', String, primary_key=True),
    Column('uf', String, nullable=True), Column('city', String, nullable=False), Column('normalized', String, nullable=False))
runs = Table('etl_execucao', meta, Column('id', String, primary_key=True),
    Column('reference', String), Column('started_at', String), Column('finished_at', String),
    Column('status', String), Column('stage', String), Column('raw_rows', Integer, default=0),
    Column('bev_rows', Integer, default=0), Column('matched_rows', Integer, default=0),
    Column('error', Text), Column('details', JSON))
snapshots = Table('snapshot', meta, Column('reference', String, primary_key=True),
    Column('run_id', ForeignKey('etl_execucao.id')), Column('source_id', ForeignKey('fonte_dado.id')),
    Column('sha256', String, nullable=False), Column('published_at', String), Column('scope', Text))
fleet = Table('frota_snapshot', meta, Column('id', Integer, primary_key=True),
    Column('reference', ForeignKey('snapshot.reference'), nullable=False, index=True),
    Column('location_id', ForeignKey('localidade.id'), nullable=False, index=True),
    Column('raw_name', String, nullable=False), Column('brand', String, nullable=False),
    Column('model', String, nullable=False), Column('version', String, nullable=False),
    Column('vehicle_year', Integer, nullable=True, index=True), Column('quantity', Integer, nullable=False),
    Column('propulsion', String),
    Column('battery_id', ForeignKey('bateria.id')), Column('confidence', String, nullable=False),
    Column('match_method', String), Column('source_id', ForeignKey('fonte_dado.id')),
    UniqueConstraint('reference', 'location_id', 'raw_name', 'vehicle_year'),
    CheckConstraint('quantity >= 0'))
Index('uq_fleet_identity_including_unknown_year',fleet.c.reference,fleet.c.location_id,fleet.c.raw_name,func.coalesce(fleet.c.vehicle_year,-1),unique=True)
reviews = Table('match_modelo', meta, Column('id', Integer, primary_key=True),
    Column('raw_name', String), Column('vehicle_year', Integer), Column('model_id', String),
    Column('score', Float), Column('method', String), Column('status', String),
    Column('approver', String), Column('notes', Text), Column('created_at', String))
probes = Table('api_probe', meta, Column('id', Integer, primary_key=True),
    Column('name', String), Column('url', Text), Column('checked_at', String),
    Column('status_code', Integer), Column('duration_ms', Integer), Column('ok', Integer),
    Column('details', JSON))

technical_candidates = Table('candidato_tecnico', meta,
    Column('id', String, primary_key=True), Column('release', String, nullable=False),
    Column('brand', String), Column('model', String), Column('year', Integer),
    Column('raw', JSON, nullable=False), Column('source_url', Text),
    Column('collected_at', String), Column('status', String))
unclassified = Table('modelo_nao_classificado', meta, Column('id', Integer, primary_key=True),
    Column('reference', String, nullable=False, index=True), Column('raw_name', String, nullable=False),
    Column('vehicles', Integer, nullable=False), Column('electrified_hint', Integer, default=0),
    UniqueConstraint('reference', 'raw_name'))
fuel_baseline = Table('frota_combustivel', meta, Column('id', Integer, primary_key=True),
    Column('reference', String, nullable=False, index=True), Column('uf', String), Column('city', String),
    Column('fuel_type', String, nullable=False), Column('propulsion', String, nullable=False),
    Column('quantity', Integer, nullable=False),
    UniqueConstraint('reference', 'uf', 'city', 'fuel_type'))
registrations = Table('emplacamento_mensal', meta, Column('id', Integer, primary_key=True),
    Column('reference', String, nullable=False, index=True), Column('provider', String, nullable=False),
    Column('brand', String), Column('model', String), Column('propulsion', String, nullable=False),
    Column('quantity', Integer, nullable=False), Column('notes', Text),
    UniqueConstraint('reference', 'provider', 'brand', 'model', 'propulsion'))
uf_population = Table('uf_populacao', meta, Column('uf', String, primary_key=True),
    Column('population', Integer, nullable=False), Column('year', Integer), Column('source', Text))

def init_db():
    from src import intelligence_schema, operational_schema, auth_schema, pipeline_schema
    meta.create_all(engine)
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    def cols(table):
        return {x['name'] for x in inspector.get_columns(table)} if table in tables else set()
    observation_cols = cols('vehicle_aggregate_observation')
    installation_cols = cols('vehicle_battery_installation')
    model_cols = cols('modelo_veiculo')
    fleet_cols = cols('frota_snapshot')
    battery_cols = cols('bateria')
    battery_model_cols = cols('battery_model')
    del inspector
    if 'manufacture_year' not in observation_cols:
        with engine.begin() as c:
            c.execute(text('ALTER TABLE vehicle_aggregate_observation ADD COLUMN manufacture_year INTEGER'))
            c.execute(text("UPDATE vehicle_aggregate_observation SET manufacture_year=model_year, model_year=NULL WHERE source_snapshot_id IN (SELECT id FROM source_snapshot WHERE provider='SENATRAN')"))
    if 'position' not in installation_cols:
        with engine.begin() as c:c.execute(text("ALTER TABLE vehicle_battery_installation ADD COLUMN position VARCHAR DEFAULT 'traction-1'"))
    if 'propulsion' not in model_cols:
        with engine.begin() as c:c.execute(text("ALTER TABLE modelo_veiculo ADD COLUMN propulsion VARCHAR DEFAULT 'BEV'"))
    if 'propulsion' not in fleet_cols:
        with engine.begin() as c:c.execute(text('ALTER TABLE frota_snapshot ADD COLUMN propulsion VARCHAR'))
    if 'part_number' not in battery_cols:
        with engine.begin() as c:c.execute(text('ALTER TABLE bateria ADD COLUMN part_number VARCHAR'))
    if 'part_number' not in battery_model_cols:
        with engine.begin() as c:c.execute(text('ALTER TABLE battery_model ADD COLUMN part_number VARCHAR'))
