"""Additive v2 schema. Public observations and restricted identities never share rows."""
from sqlalchemy import Table, Column, String, Integer, Float, Text, JSON, ForeignKey, UniqueConstraint, CheckConstraint, Index
from src.db import meta

def table(name, fields, *constraints):
    return Table(name, meta, Column('id', String, primary_key=True), *fields, *constraints)

def strings(*names): return [Column(n, String) for n in names]
def numbers(*names): return [Column(n, Float) for n in names]
def integers(*names): return [Column(n, Integer) for n in names]
def fk(name, target, nullable=True): return Column(name, ForeignKey(target+'.id'), nullable=nullable)

source_snapshot = table('source_snapshot', [*strings('provider','dataset_name','reference_period','retrieved_at','method','raw_file_hash','raw_storage_path','source_version','transformation_version','license_or_terms','ingestion_status','source_url','scope','metric','notes'),Column('record_count',Integer), Column('access_level',String,nullable=False,default='public'), Column('metadata',JSON)], UniqueConstraint('provider','dataset_name','reference_period','raw_file_hash'))
vehicle_spec = table('vehicle_spec', [*strings('manufacturer','model','trim','powertrain','country_market','vehicle_category','vehicle_subtype','segment','body_type'), *integers('model_year_start','model_year_end','battery_pack_count','passenger_capacity'), *numbers('dc_charge_power_kw','ac_charge_power_kw','charging_time_minutes','motor_power_kw','torque_nm','range_km','gross_vehicle_weight_kg','payload_kg','length_mm','width_mm','height_mm','wheelbase_mm'), Column('field_sources',JSON), fk('source_snapshot_id','source_snapshot',False)])
battery_model = table('battery_model', [*strings('manufacturer','model_name','chemistry','technology','part_number','confidence'), *numbers('nominal_capacity_kwh','usable_capacity_kwh','nominal_voltage_v','min_voltage_v','max_voltage_v','rated_capacity_ah','expected_cycle_life','automotive_eol_threshold_percent','warranty_years','warranty_km'), *integers('module_count','cell_count'), fk('source_snapshot_id','source_snapshot',False), Column('field_sources',JSON)])
compatibility = table('vehicle_spec_battery_compatibility', [fk('vehicle_spec_id','vehicle_spec',False),fk('battery_model_id','battery_model',False),fk('source_snapshot_id','source_snapshot',False),*strings('valid_from','valid_to','confidence','evidence_type','verified_by','verified_at','status','notes')])
aggregate = table('vehicle_aggregate_observation', [fk('source_snapshot_id','source_snapshot',False),fk('vehicle_spec_id','vehicle_spec'),fk('battery_model_id','battery_model'),*strings('reference_period','country','state','municipality','vehicle_category','manufacturer','model','trim','powertrain_type','fuel_type','source_record_reference','confidence'), Column('model_year',Integer),Column('manufacture_year',Integer),Column('quantity',Integer,nullable=False),Column('abve_electrified_classification',Integer)], CheckConstraint('quantity >= 0'), UniqueConstraint('source_snapshot_id','source_record_reference'))
vehicle_asset = table('vehicle_asset', [*strings('vin','renavam','plate','manufacturer','model','trim','vehicle_category','powertrain_type','registration_date','state','municipality','operator','identity_status','identity_confidence','first_seen_at','last_seen_at'),*integers('model_year','manufacture_year'),fk('source_snapshot_id','source_snapshot',False),fk('aggregate_id','vehicle_aggregate_observation'),fk('vehicle_spec_id','vehicle_spec'),Column('slot_number',Integer)], UniqueConstraint('vin'),UniqueConstraint('renavam'),UniqueConstraint('plate'),UniqueConstraint('aggregate_id','slot_number'))
battery_asset = table('battery_asset', [fk('battery_model_id','battery_model'),fk('source_snapshot_id','source_snapshot',False),fk('parent_battery_id','battery_asset'),*strings('passport_identifier','manufacturer_serial_number','manufacturing_date','first_service_date','current_status','confidence','last_observed_at','battery_identifier_type','battery_identifier','passport_schema','passport_version','passport_url_reference','passport_issuer','passport_created_at','passport_last_updated'),*numbers('soh_percent','soce_percent','energy_throughput_kwh','last_known_temperature','last_known_soc'),Column('cycle_count',Integer)],UniqueConstraint('passport_identifier'))
installation = table('vehicle_battery_installation',[fk('vehicle_id','vehicle_asset',False),fk('battery_id','battery_asset',False),fk('source_snapshot_id','source_snapshot',False),*strings('installed_at','removed_at','installation_type','confidence','position')])
lifecycle = table('battery_lifecycle_observation',[fk('battery_id','battery_asset',False),fk('source_snapshot_id','source_snapshot',False),*strings('observed_at','event_type','notes','current_status'),*numbers('soh_percent','soce_percent','soc_percent','odometer_km','energy_throughput_kwh','temperature_c'),*integers('cycle_count','fast_charge_count')])
audit = table('intelligence_audit',[*strings('actor','role','action','entity_type','entity_id','created_at'),Column('details',JSON)])
forecast_run = table('battery_availability_forecast',[*strings('method','created_at','confidence'),Column('parameters',JSON),Column('result',JSON)])
scheduler_tick = table('scheduler_tick',[*strings('started_at','finished_at','status','error','run_id')])
Index('idx_aggregate_filter',aggregate.c.reference_period,aggregate.c.vehicle_category,aggregate.c.powertrain_type,aggregate.c.state)
Index('idx_aggregate_snapshot_state_city',aggregate.c.source_snapshot_id,aggregate.c.state,aggregate.c.municipality)
Index('idx_aggregate_snapshot_quantity',aggregate.c.source_snapshot_id,aggregate.c.quantity.desc(),aggregate.c.id)
Index('idx_install_vehicle',installation.c.vehicle_id,installation.c.installed_at)
Index('idx_lifecycle_battery',lifecycle.c.battery_id,lifecycle.c.observed_at)

TABLES={'vehicle-specs':vehicle_spec,'battery-models':battery_model,'evidence':compatibility,'vehicles':vehicle_asset,'battery-assets':battery_asset,'installations':installation,'lifecycle':lifecycle}

# Odometers belong to an installation/vehicle, not to the lifetime of a pack.
lifecycle.append_column(fk('installation_id', 'vehicle_battery_installation'))
lifecycle.append_column(fk('vehicle_id', 'vehicle_asset'))
warranty = Table('battery_warranty', meta,
    Column('battery_id', ForeignKey('battery_asset.id'), primary_key=True),
    Column('revision', Integer, nullable=False), Column('data', JSON, nullable=False),
    Column('updated_at', String, nullable=False))

passport = table("battery_passport_record", [fk("battery_id", "battery_asset", False), Column("values", JSON, nullable=False), Column("updated_at", String)], UniqueConstraint("battery_id"))

# Operational fleet publications are staged separately from the immutable source
# observations.  The singleton pointer is changed only after every membership has
# been created, so readers never see half of a month.
fleet_publication = table('fleet_publication', [
    fk('source_snapshot_id','source_snapshot',False),
    *strings('reference_period','status','stage','started_at','finished_at','error'),
    *integers('planned_units','materialized_units'),
], UniqueConstraint('source_snapshot_id'))
fleet_active = table('fleet_active_publication', [
    fk('publication_id','fleet_publication',False), Column('updated_at',String)
])
fleet_model_group = table('fleet_model_group', [
    fk('publication_id','fleet_publication',False),
    *strings('model_key','manufacturer','model','vehicle_category','powertrain_type'),
    Column('quantity',Integer,nullable=False),
], UniqueConstraint('publication_id','model_key'))
fleet_unit_membership = table('fleet_unit_membership', [
    fk('publication_id','fleet_publication',False), fk('model_group_id','fleet_model_group',False),
    fk('vehicle_id','vehicle_asset',False), Column('sequence',Integer,nullable=False),
    Column('public_identifier',String,nullable=False),
], UniqueConstraint('publication_id','model_group_id','sequence'), UniqueConstraint('publication_id','vehicle_id'))
Index('idx_fleet_group_publication',fleet_model_group.c.publication_id,fleet_model_group.c.manufacturer,fleet_model_group.c.model)
Index('idx_fleet_membership_group',fleet_unit_membership.c.model_group_id,fleet_unit_membership.c.sequence)
Index('idx_fleet_public_identifier',fleet_unit_membership.c.public_identifier)
