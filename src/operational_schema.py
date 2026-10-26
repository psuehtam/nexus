"""Permanent registry, independent from monthly statistical publications."""
from sqlalchemy import Table, Column, String, Integer, Boolean, JSON, Text, ForeignKey, UniqueConstraint, CheckConstraint, Index
from src.db import meta

def ident(): return Column('id', String, primary_key=True)
def ref(name, target, **kw): return Column(name, ForeignKey(target), **kw)

model = Table('registry_model', meta, ident(),
    Column('model_key', String, nullable=False, unique=True),
    Column('manufacturer', String, nullable=False), Column('model', String, nullable=False),
    Column('vehicle_category', String, nullable=False), Column('powertrain_type', String, nullable=False),
    Column('vehicle_subtype', String), Column('segment', String), Column('body_type', String),
    Column('is_electric', Boolean, nullable=False, default=False),
    Column('is_heavy_vehicle', Boolean, nullable=False, default=False),
    Column('model_year_start', Integer), Column('model_year_end', Integer),
    Column('classification_source', Text), Column('classification_confidence', String),
    Column('classification_status', String, nullable=False, default='approved'),
    Column('allocated_count', Integer, nullable=False, default=0),
    Column('created_at', String, nullable=False))
registry = Table('registry_vehicle', meta,
    ref('vehicle_id','vehicle_asset.id',primary_key=True),
    ref('origin_model_id','registry_model.id',nullable=False),
    ref('current_model_id','registry_model.id',nullable=False),
    Column('sequence', Integer, nullable=False), Column('public_code', String, nullable=False, unique=True),
    Column('visible', Boolean, nullable=False, default=False),
    Column('origin', String, nullable=False, default='statistical_placeholder'),
    ref('created_publication_id','fleet_publication.id'),
    Column('revision', Integer, nullable=False, default=0),
    UniqueConstraint('origin_model_id','sequence'), CheckConstraint('sequence > 0'))
Index('ix_registry_model_visible', registry.c.current_model_id, registry.c.visible, registry.c.sequence)
group_model = Table('registry_publication_group',meta,
    ref('group_id','fleet_model_group.id',primary_key=True),ref('model_id','registry_model.id',nullable=False))
publication_state = Table('registry_publication_state',meta,
    ref('publication_id','fleet_publication.id',primary_key=True),
    Column('generation',Integer,nullable=False),Column('block_index',Integer,nullable=False,default=0),
    Column('created_at',String,nullable=False))
block = Table('registry_generation_block',meta, ident(),
    ref('publication_id','fleet_publication.id',nullable=False),Column('block_index',Integer,nullable=False),
    Column('count',Integer,nullable=False),Column('completed_at',String,nullable=False),
    UniqueConstraint('publication_id','block_index'))
alias = Table('registry_alias',meta,ident(),Column('entity_type',String,nullable=False),
    Column('namespace',String,nullable=False),Column('old_id',String,nullable=False),Column('canonical_id',String,nullable=False),
    UniqueConstraint('entity_type','namespace','old_id'))
semantic_alias = Table('registry_model_semantic_alias',meta,ident(),
    ref('model_id','registry_model.id',nullable=False),Column('source',String,nullable=False),
    Column('alias_raw',String,nullable=False),Column('alias_normalized',String,nullable=False),
    Column('match_type',String,nullable=False),Column('confidence',String,nullable=False),
    Column('evidence',Text,nullable=False),Column('approved',Boolean,nullable=False,default=False),
    Column('version',Integer,nullable=False,default=1),Column('actor',String,nullable=False),
    Column('created_at',String,nullable=False),
    UniqueConstraint('source','alias_normalized','version'))
Index('ix_registry_semantic_alias_current',semantic_alias.c.source,semantic_alias.c.alias_normalized,semantic_alias.c.approved)
heavy_stat = Table('heavy_vehicle_statistic',meta,ident(),
    Column('source_snapshot_id',String,nullable=False),Column('reference_period',String,nullable=False),
    Column('dataset',String,nullable=False),Column('state',String),Column('municipality',String),
    Column('vehicle_type',String),Column('species',String),Column('axles',Integer),
    Column('quantity',Integer,nullable=False),Column('source_record_reference',String,nullable=False),
    UniqueConstraint('source_snapshot_id','source_record_reference'),CheckConstraint('quantity >= 0'))
Index('ix_heavy_stat_filters',heavy_stat.c.reference_period,heavy_stat.c.vehicle_type,heavy_stat.c.state)
Index('ix_heavy_stat_geography',heavy_stat.c.reference_period,heavy_stat.c.state,heavy_stat.c.municipality)
schema = Table('passport_schema_version',meta,Column('version',String,primary_key=True),
    Column('fields',JSON,nullable=False),Column('created_at',String,nullable=False))
dossier = Table('passport_dossier',meta,ident(),Column('owner_type',String,nullable=False),
    Column('owner_id',String,nullable=False),Column('code',String,nullable=False,unique=True),
    ref('schema_version','passport_schema_version.version',nullable=False),Column('revision',Integer,nullable=False,default=0),
    Column('values',JSON,nullable=False),Column('updated_at',String,nullable=False),
    UniqueConstraint('owner_type','owner_id'),CheckConstraint("owner_type IN ('vehicle','battery')"))
dossier_revision = Table('passport_dossier_revision',meta,ident(),ref('dossier_id','passport_dossier.id',nullable=False),
    Column('revision',Integer,nullable=False),Column('schema_version',String,nullable=False),
    Column('values',JSON,nullable=False),Column('actor',String,nullable=False),Column('created_at',String,nullable=False),
    UniqueConstraint('dossier_id','revision'))
identifier = Table('registry_identifier',meta,ident(),Column('entity_type',String,nullable=False),
    Column('entity_id',String,nullable=False),Column('kind',String,nullable=False),Column('issuer',String,nullable=False),
    Column('normalized',String,nullable=False),Column('active',Boolean,nullable=False,default=True),
    Column('evidence',String,nullable=False),Column('created_at',String,nullable=False))
Index('uq_registry_identifier_active',identifier.c.entity_type,identifier.c.kind,identifier.c.issuer,identifier.c.normalized,
      unique=True,postgresql_where=identifier.c.active.is_(True),sqlite_where=identifier.c.active.is_(True))
current_install = Table('registry_current_installation',meta,
    ref('battery_id','battery_asset.id',primary_key=True),ref('installation_id','vehicle_battery_installation.id',nullable=False,unique=True),
    ref('vehicle_id','vehicle_asset.id',nullable=False),Column('position',String,nullable=False),
    UniqueConstraint('vehicle_id','position'))
installation_evidence = Table('registry_installation_evidence',meta,
    ref('installation_id','vehicle_battery_installation.id',primary_key=True),
    Column('observed_at',String,nullable=False),Column('evidence',String,nullable=False),
    Column('removal_observed_at',String),Column('removal_evidence',String),Column('closed',Boolean,nullable=False,default=False))
command = Table('registry_command',meta,Column('key',String,primary_key=True),Column('body_hash',String,nullable=False),
    Column('result',JSON,nullable=False),Column('created_at',String,nullable=False))
policy = Table('registry_alert_policy',meta,ident(),Column('national_pct',Integer,nullable=False),
    Column('model_pct',Integer,nullable=False),Column('model_absolute',Integer,nullable=False),
    Column('actor',String,nullable=False),Column('reason',String,nullable=False),Column('created_at',String,nullable=False))
alert = Table('registry_publication_alert',meta,ident(),ref('publication_id','fleet_publication.id',nullable=False),
    ref('policy_id','registry_alert_policy.id',nullable=False),Column('code',String,nullable=False),
    Column('details',JSON,nullable=False),Column('created_at',String,nullable=False))
setting = Table('registry_setting',meta,Column('key',String,primary_key=True),Column('value',JSON,nullable=False),Column('updated_at',String,nullable=False))
