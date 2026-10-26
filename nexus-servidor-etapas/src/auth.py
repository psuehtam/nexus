"""Cookie authentication. Database constraints and counters survive API restarts."""
from datetime import datetime, timezone
import hashlib
import hmac
import os
import re
import secrets
import time
import unicodedata
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import HTTPException, Request
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from src import db
from src import auth_schema as s

COOKIE_NAME = 'i9_session'
PASSWORD_ITERATIONS = 600_000
SESSION_SECONDS = 8 * 60 * 60
RATE_WINDOW_SECONDS = 15 * 60
# A fixed salt is used only for the nonexistent-account timing check.
_DUMMY_SALT = bytes.fromhex('31a5bc07ef030b55a6e1668d74ba99fb')


def now():
    return datetime.now(timezone.utc).isoformat()


def normalize_username(value):
    value = unicodedata.normalize('NFKC', value).strip().casefold()
    if not re.fullmatch(r'[a-z0-9_.@-]{3,120}', value):
        raise ValueError('Use de 3 a 120 letras, números ou os caracteres . _ @ - no usuário.')
    return value


def validate_password(password):
    if len(password) < 12 or len(password.encode('utf-8')) > 1024:
        raise ValueError('A senha precisa ter pelo menos 12 caracteres e no máximo 1024 bytes.')


def hash_password(password):
    validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, PASSWORD_ITERATIONS)
    return f'pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}'


def verify_password(password, encoded):
    if len(password.encode('utf-8')) > 1024:
        return False
    if encoded is None:
        hashlib.pbkdf2_hmac('sha256', password.encode(), _DUMMY_SALT, PASSWORD_ITERATIONS)
        return False
    try:
        method, iterations, salt, expected = encoded.split('$')
        iterations = int(iterations)
        if method != 'pbkdf2_sha256' or not 100_000 <= iterations <= 2_000_000:
            return False
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), iterations)
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def _event(connection, event, username=None, user_id=None):
    connection.execute(insert(s.auth_events).values(
        id=str(uuid4()), event=event, username=username,
        user_id=user_id, occurred_at=now(),
    ))


def create_admin(username, password, *, reset=False):
    """Explicit CLI provisioning; password rotation also revokes every session."""
    username = normalize_username(username)
    encoded = hash_password(password)
    timestamp = now()
    try:
        with db.engine.begin() as connection:
            existing = connection.execute(select(s.users).where(s.users.c.username == username).with_for_update()).mappings().first()
            if existing:
                if not reset:
                    raise ValueError('Este usuário já existe. Use --reset para trocar sua senha.')
                connection.execute(update(s.users).where(s.users.c.id == existing['id']).values(
                    password_hash=encoded, password_changed_at=timestamp, active=1))
                connection.execute(update(s.sessions).where(s.sessions.c.user_id == existing['id']).values(revoked_at=int(time.time())))
                _event(connection, 'password_reset', username, existing['id'])
                return existing['id']
            uid = str(uuid4())
            connection.execute(insert(s.users).values(id=uid, username=username,
                password_hash=encoded, active=1, created_at=timestamp, password_changed_at=timestamp))
            _event(connection, 'account_created', username, uid)
            return uid
    except IntegrityError as exc:
        raise ValueError('Este usuário já existe.') from exc


def check_origin(request):
    """Reject browser cross-site login and unsafe requests, including login CSRF."""
    origin = request.headers.get('origin')
    if not origin:
        if request.headers.get('sec-fetch-site') == 'cross-site':
            raise HTTPException(403, 'Origem da requisição não autorizada.')
        return
    own = f'{request.url.scheme}://{request.url.netloc}'
    allowed = {own}
    configured = os.getenv('I9_TRUSTED_ORIGINS', '')
    allowed.update(x.strip().rstrip('/') for x in configured.split(',') if x.strip())
    if origin.rstrip('/') not in allowed or urlsplit(origin).scheme not in ('http', 'https'):
        raise HTTPException(403, 'Origem da requisição não autorizada.')


def _reserve_attempt(username, address):
    """Upsert and increment under row locks before doing password verification."""
    timestamp = int(time.time())
    limits = ((f'user:{username}', 8), (f'address:{address}', 40))
    blocked = False
    retry_after = 1
    with db.engine.begin() as connection:
        for raw, maximum in sorted(limits):
            bucket = hashlib.sha256(raw.encode()).hexdigest()
            values = dict(bucket=bucket, window_start=timestamp, attempts=0)
            if connection.dialect.name == 'postgresql':
                from sqlalchemy.dialects.postgresql import insert as upsert
            elif connection.dialect.name == 'sqlite':
                from sqlalchemy.dialects.sqlite import insert as upsert
            else:
                raise RuntimeError('Banco não suportado para autenticação.')
            connection.execute(upsert(s.login_limits).values(**values).on_conflict_do_nothing(index_elements=['bucket']))
            row = connection.execute(select(s.login_limits).where(s.login_limits.c.bucket == bucket).with_for_update()).mappings().one()
            start, attempts = row['window_start'], row['attempts']
            if timestamp >= start + RATE_WINDOW_SECONDS:
                start, attempts = timestamp, 0
            if attempts >= maximum:
                blocked = True
                retry_after = max(retry_after, start + RATE_WINDOW_SECONDS - timestamp)
            connection.execute(update(s.login_limits).where(s.login_limits.c.bucket == bucket).values(
                window_start=start, attempts=min(attempts + 1, maximum)))
        # No token, password or raw IP address is written to the audit trail.
        if blocked:
            _event(connection, 'login_throttled', username)
        connection.execute(delete(s.login_limits).where(s.login_limits.c.window_start < timestamp - 2 * RATE_WINDOW_SECONDS))
        connection.execute(delete(s.sessions).where(s.sessions.c.expires_at < timestamp - 24 * 3600))
    if blocked:
        raise HTTPException(429, 'Muitas tentativas. Aguarde antes de tentar novamente.', headers={'Retry-After': str(retry_after)})


def login(username, password, address):
    try:
        username = normalize_username(username)
    except ValueError:
        username = '<invalid>'
    _reserve_attempt(username, address)
    with db.engine.connect() as connection:
        row = connection.execute(select(s.users).where(s.users.c.username == username)).mappings().first()
    valid = verify_password(password, row['password_hash'] if row else None)
    timestamp = int(time.time())
    if not row or not valid or not row['active']:
        with db.engine.begin() as connection:
            _event(connection, 'login_failed', username)
        raise HTTPException(401, 'Usuário ou senha inválidos.')
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    # Lock and recheck the credential version so a password reset cannot race login.
    with db.engine.begin() as connection:
        current = connection.execute(select(s.users).where(s.users.c.id == row['id']).with_for_update()).mappings().one()
        if not current['active'] or current['password_hash'] != row['password_hash']:
            raise HTTPException(401, 'Usuário ou senha inválidos.')
        connection.execute(insert(s.sessions).values(token_hash=hashlib.sha256(token.encode()).hexdigest(),
            user_id=row['id'], csrf_token=csrf, created_at=timestamp, expires_at=timestamp + SESSION_SECONDS))
        connection.execute(delete(s.login_limits).where(s.login_limits.c.bucket == hashlib.sha256(f'user:{username}'.encode()).hexdigest()))
        _event(connection, 'login_success', username, row['id'])
    return token, {'authenticated': True, 'username': username, 'csrf_token': csrf}


def current_session(request):
    token = request.cookies.get(COOKIE_NAME)
    if not token or len(token) > 256:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    with db.engine.connect() as connection:
        row = connection.execute(select(s.sessions, s.users.c.username, s.users.c.active)
            .join(s.users, s.sessions.c.user_id == s.users.c.id)
            .where(s.sessions.c.token_hash == digest,
                s.sessions.c.expires_at > int(time.time()), s.sessions.c.revoked_at.is_(None), s.users.c.active == 1)).mappings().first()
    return dict(row) if row else None


def require_admin(request: Request):
    """Use as a FastAPI dependency; machine keys require an explicit server opt-in."""
    session = current_session(request)
    if session:
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            check_origin(request)
            provided = request.headers.get('x-csrf-token', '')
            if not hmac.compare_digest(provided, session['csrf_token']):
                raise HTTPException(403, 'Token de proteção ausente ou inválido. Atualize a página.')
        return {'id': session['user_id'], 'username': session['username'], 'role': 'admin'}
    if os.getenv('I9_MACHINE_KEY_ENABLED', 'false').lower() == 'true':
        expected = os.getenv('ADMIN_API_KEY', '')
        provided = request.headers.get('x-admin-key', '')
        if expected and provided and hmac.compare_digest(expected, provided):
            return {'id': 'machine', 'username': 'machine', 'role': 'admin'}
    raise HTTPException(401, 'Entre com sua conta administrativa para alterar os dados.')


def revoke_session(request):
    session = current_session(request)
    if session:
        with db.engine.begin() as connection:
            connection.execute(update(s.sessions).where(s.sessions.c.token_hash == session['token_hash']).values(revoked_at=int(time.time())))
            _event(connection, 'logout', session['username'], session['user_id'])
