"""Persistent administrator credentials, sessions and login throttling."""
from sqlalchemy import Column, ForeignKey, Integer, String, Table, Text
from src.db import meta

users = Table(
    'admin_user', meta,
    Column('id', String, primary_key=True),
    Column('username', String(120), nullable=False, unique=True),
    Column('password_hash', Text, nullable=False),
    Column('active', Integer, nullable=False, default=1),
    Column('created_at', String, nullable=False),
    Column('password_changed_at', String, nullable=False),
)

sessions = Table(
    'admin_session', meta,
    Column('token_hash', String(64), primary_key=True),
    Column('user_id', ForeignKey('admin_user.id'), nullable=False, index=True),
    Column('csrf_token', String(64), nullable=False),
    Column('created_at', Integer, nullable=False),
    Column('expires_at', Integer, nullable=False, index=True),
    Column('revoked_at', Integer),
)

login_limits = Table(
    'admin_login_limit', meta,
    Column('bucket', String(64), primary_key=True),
    Column('window_start', Integer, nullable=False),
    Column('attempts', Integer, nullable=False),
)

auth_events = Table(
    'admin_auth_event', meta,
    Column('id', String, primary_key=True),
    Column('user_id', String),
    Column('username', String(120)),
    Column('event', String(40), nullable=False),
    Column('occurred_at', String, nullable=False),
)
