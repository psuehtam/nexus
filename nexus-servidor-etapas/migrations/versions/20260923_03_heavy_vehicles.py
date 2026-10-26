"""Add heavy electrified catalog metadata and independent statistics."""
from alembic import op
from sqlalchemy import Column,String,Integer,Float,Boolean,JSON,Text,inspect

revision='20260923_03'
down_revision='20260917_02'
branch_labels=None
depends_on=None

def add_missing(table, columns):
    existing={item['name'] for item in inspect(op.get_bind()).get_columns(table)}
    for column in columns:
        if column.name not in existing:op.add_column(table,column)

def upgrade():
    add_missing('registry_model',[
        Column('vehicle_subtype',String),Column('segment',String),Column('body_type',String),
        Column('is_electric',Boolean,nullable=False,server_default='false'),Column('is_heavy_vehicle',Boolean,nullable=False,server_default='false'),
        Column('model_year_start',Integer),Column('model_year_end',Integer),Column('classification_source',Text),
        Column('classification_confidence',String),Column('classification_status',String,nullable=False,server_default='approved')])
    add_missing('vehicle_spec',[
        Column('vehicle_subtype',String),Column('segment',String),Column('body_type',String),
        Column('battery_pack_count',Integer),Column('passenger_capacity',Integer),
        *[Column(name,Float) for name in ('dc_charge_power_kw','ac_charge_power_kw','charging_time_minutes','motor_power_kw','torque_nm','range_km','gross_vehicle_weight_kg','payload_kg','length_mm','width_mm','height_mm','wheelbase_mm')],
        Column('field_sources',JSON)])
    for table in ('pipeline_rule','pipeline_classified_name'):
        add_missing(table,[Column('vehicle_subtype',String),Column('segment',String),Column('body_type',String)])
    op.execute('''CREATE TABLE IF NOT EXISTS registry_model_semantic_alias (
        id VARCHAR PRIMARY KEY, model_id VARCHAR NOT NULL REFERENCES registry_model(id), source VARCHAR NOT NULL,
        alias_raw VARCHAR NOT NULL, alias_normalized VARCHAR NOT NULL, match_type VARCHAR NOT NULL,
        confidence VARCHAR NOT NULL, evidence TEXT NOT NULL, approved BOOLEAN NOT NULL DEFAULT FALSE,
        version INTEGER NOT NULL DEFAULT 1, actor VARCHAR NOT NULL, created_at VARCHAR NOT NULL,
        UNIQUE(source,alias_normalized,version))''')
    op.execute('CREATE INDEX IF NOT EXISTS ix_registry_semantic_alias_current ON registry_model_semantic_alias(source,alias_normalized,approved)')
    op.execute('''CREATE TABLE IF NOT EXISTS heavy_vehicle_statistic (
        id VARCHAR PRIMARY KEY, source_snapshot_id VARCHAR NOT NULL, reference_period VARCHAR NOT NULL,
        dataset VARCHAR NOT NULL, state VARCHAR, municipality VARCHAR, vehicle_type VARCHAR, species VARCHAR,
        axles INTEGER, quantity INTEGER NOT NULL CHECK (quantity >= 0), source_record_reference VARCHAR NOT NULL,
        UNIQUE(source_snapshot_id,source_record_reference))''')
    op.execute('CREATE INDEX IF NOT EXISTS ix_heavy_stat_filters ON heavy_vehicle_statistic(reference_period,vehicle_type,state)')
    op.execute("UPDATE registry_model SET is_electric = powertrain_type IN ('BEV','PHEV','HEV','HEV_FLEX','MHEV','MHEV_12V','MHEV_48V','FCEV')")
    op.execute("UPDATE registry_model SET is_heavy_vehicle = vehicle_category IN ('bus','microbus','truck','tractor_truck','van','utility','chassis_platform','heavy')")

def downgrade():
    raise RuntimeError('Migração aditiva: preserve modelos, aliases e estatísticas.')
