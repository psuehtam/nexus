"""Same-origin administrator login; public GET/session supports the SPA shell."""
import os
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from src import auth

router = APIRouter(prefix='/api/v3/auth', tags=['Acesso administrativo'])


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=1024)


@router.get('/session')
def session(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    current = auth.current_session(request)
    if not current:
        from sqlalchemy import select,func
        from src import db,auth_schema
        with db.engine.connect() as c:setup_required=not c.execute(select(func.count()).select_from(auth_schema.users)).scalar_one()
        return {'authenticated': False, 'username': None, 'csrf_token': None,'setup_required':setup_required}
    return {'authenticated': True, 'username': current['username'], 'csrf_token': current['csrf_token']}


@router.post('/login')
def login(payload: LoginPayload, request: Request, response: Response):
    auth.check_origin(request)
    token, result = auth.login(payload.username, payload.password,
        request.client.host if request.client else 'unknown')
    secure = request.url.scheme == 'https' or os.getenv('I9_COOKIE_SECURE', 'false').lower() == 'true'
    response.set_cookie(auth.COOKIE_NAME, token, max_age=auth.SESSION_SECONDS,
        httponly=True, secure=secure, samesite='strict', path='/')
    response.headers['Cache-Control'] = 'no-store'
    return result


@router.post('/logout', status_code=204)
def logout(request: Request, response: Response, _user=Depends(auth.require_admin)):
    auth.revoke_session(request)
    response.delete_cookie(auth.COOKIE_NAME, path='/', httponly=True, samesite='strict')
    response.headers['Cache-Control'] = 'no-store'
