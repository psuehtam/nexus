"""Durable ingestion state. All mutable work is fenced by a database lease."""
from sqlalchemy import (Table, Column, String, Integer, BigInteger, Text, JSON,
                        ForeignKey, UniqueConstraint, Index, CheckConstraint)
from src.db import meta


def table(name, *fields, constraints=()):
    return Table(name, meta, Column('id', String, primary_key=True), *fields, *constraints)


epoch = Table('pipeline_epoch', meta, Column('id', String, primary_key=True),
              Column('value', String, nullable=False), Column('updated_at', String, nullable=False))
intent = Table('pipeline_publication_intent', meta,
               Column('reference', String, primary_key=True),
               Column('generation', Integer, nullable=False), Column('job_id', String, nullable=False))
job = table('pipeline_job',
    Column('kind', String, nullable=False), Column('payload', JSON, nullable=False),
    Column('idempotency_key', String, nullable=False, unique=True),
    Column('request_hash', String, nullable=False), Column('status', String, nullable=False),
    Column('stage', String, nullable=False), Column('created_at', String, nullable=False),
    Column('updated_at', String, nullable=False), Column('available_at', String, nullable=False),
    Column('owner', String), Column('lease_until', String), Column('token', Integer, nullable=False, default=0),
    Column('epoch', String), Column('attempt_count', Integer, nullable=False, default=0),
    Column('generation', Integer), Column('error', Text), Column('result', JSON),
    Column('progress', JSON, nullable=False), Column('block_size', Integer, nullable=False))
Index('idx_pipeline_jobs_claim', job.c.status, job.c.available_at, job.c.lease_until)
attempt = table('pipeline_attempt',
    Column('job_id', ForeignKey('pipeline_job.id'), nullable=False),
    Column('number', Integer, nullable=False), Column('owner', String, nullable=False),
    Column('token', Integer, nullable=False), Column('epoch', String, nullable=False),
    Column('started_at', String, nullable=False), Column('finished_at', String),
    Column('status', String, nullable=False), Column('error', Text),
    constraints=(UniqueConstraint('job_id', 'number'),))
block = table('pipeline_block',
    Column('job_id', ForeignKey('pipeline_job.id'), nullable=False),
    Column('stage', String, nullable=False), Column('block_index', Integer, nullable=False),
    Column('result', JSON, nullable=False), Column('finished_at', String, nullable=False),
    constraints=(UniqueConstraint('job_id', 'stage', 'block_index'),))
artifact = table('pipeline_artifact',
    Column('provider', String, nullable=False), Column('dataset', String, nullable=False),
    Column('reference', String, nullable=False), Column('raw_sha256', String, nullable=False),
    Column('parser_version', String, nullable=False), Column('normalization_version', String, nullable=False),
    Column('path', Text, nullable=False), Column('url', Text),
    Column('collected_at', String, nullable=False), Column('bytes', BigInteger, nullable=False),
    Column('uncompressed_bytes', BigInteger), Column('rows', BigInteger, nullable=False, default=0),
    Column('status', String, nullable=False), Column('semantic_content_id', String),
    Column('parse_rules', JSON), Column('parse_stats', JSON),
    constraints=(UniqueConstraint('provider', 'dataset', 'reference', 'raw_sha256',
                                 'parser_version', 'normalization_version'),))
filtered_summary = Table('pipeline_filtered_summary', meta,
    Column('artifact_id', ForeignKey('pipeline_artifact.id'), primary_key=True),
    Column('block_index', Integer, primary_key=True), Column('raw_name', String, primary_key=True),
    Column('status', String, nullable=False), Column('quantity', BigInteger, nullable=False),
    Column('row_count', BigInteger, nullable=False), Column('electrified_hint', Integer, nullable=False))
observation = table('pipeline_observation',
    Column('artifact_id', ForeignKey('pipeline_artifact.id'), nullable=False),
    Column('ordinal', BigInteger, nullable=False), Column('member', Text, nullable=False),
    Column('line_number', BigInteger, nullable=False), Column('canonical', Text, nullable=False),
    Column('row_sha256', String, nullable=False), Column('state', String), Column('city', String),
    Column('raw_name', String, nullable=False), Column('manufacture_year', Integer),
    Column('category', String), Column('quantity', BigInteger, nullable=False),
    constraints=(UniqueConstraint('artifact_id', 'ordinal'), CheckConstraint('quantity >= 0'),))
Index('idx_pipeline_observation_hash', observation.c.artifact_id, observation.c.row_sha256)
Index('idx_pipeline_observation_name', observation.c.artifact_id, observation.c.raw_name)
content = table('pipeline_content',
    Column('provider', String, nullable=False), Column('dataset', String, nullable=False),
    Column('reference', String, nullable=False), Column('normalization_version', String, nullable=False),
    Column('semantic_sha256', String, nullable=False), Column('artifact_id', ForeignKey('pipeline_artifact.id'), nullable=False),
    Column('rows', BigInteger, nullable=False),
    constraints=(UniqueConstraint('provider', 'dataset', 'reference', 'normalization_version', 'semantic_sha256'),))
classification = table('pipeline_classification',
    Column('content_id', ForeignKey('pipeline_content.id'), nullable=False),
    Column('rules_version', String, nullable=False), Column('rules_snapshot', JSON, nullable=False),
    Column('transformation_version', String, nullable=False), Column('source_snapshot_id', String, nullable=False),
    Column('status', String, nullable=False), Column('recognized_quantity', BigInteger, nullable=False, default=0),
    Column('pending_quantity', BigInteger, nullable=False, default=0),
    constraints=(UniqueConstraint('content_id', 'rules_version', 'transformation_version'),))
classified_name = table('pipeline_classified_name',
    Column('classification_id', ForeignKey('pipeline_classification.id'), nullable=False),
    Column('raw_name', String, nullable=False), Column('status', String, nullable=False),
    Column('manufacturer', String), Column('model', String), Column('powertrain', String),
    Column('category', String), Column('vehicle_subtype',String),Column('segment',String),Column('body_type',String),Column('evidence', Text),
    constraints=(UniqueConstraint('classification_id', 'raw_name'),))
rule = table('pipeline_rule',
    Column('raw_name', String, nullable=False), Column('version', Integer, nullable=False),
    Column('manufacturer', String), Column('model', String), Column('powertrain', String),
    Column('category', String), Column('vehicle_subtype',String),Column('segment',String),Column('body_type',String),Column('decision', String, nullable=False),
    Column('evidence', Text, nullable=False), Column('actor', String, nullable=False),
    Column('created_at', String, nullable=False),
    constraints=(UniqueConstraint('raw_name', 'version'),))
policy = table('pipeline_alert_policy', Column('created_at', String, nullable=False),
               Column('actor', String, nullable=False), Column('reason', Text, nullable=False),
               Column('values', JSON, nullable=False))
evaluation = table('pipeline_alert_evaluation',
    Column('classification_id', ForeignKey('pipeline_classification.id'), nullable=False),
    Column('policy_id', ForeignKey('pipeline_alert_policy.id'), nullable=False),
    Column('previous_source_id', String, nullable=False), Column('created_at', String, nullable=False),
    Column('metrics', JSON, nullable=False), Column('alerts', JSON, nullable=False),
    constraints=(UniqueConstraint('classification_id', 'policy_id', 'previous_source_id'),))
