"""HTTP views for HASS Baidu DuerOS OAuth2 and service endpoints."""

import asyncio
import json
import logging
import time
import traceback

from datetime import timedelta
from urllib import parse
from urllib.parse import urlencode, urlparse

import aiohttp
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from . import util as havcs_util
from .const import (
    CLIENT_PLATFORM_DICT,
    DATA_HAVCS_CONFIG,
    DATA_HAVCS_HANDLER,
    INTEGRATION,
)

_LOGGER = logging.getLogger(__name__)
LOGGER_NAME = 'http'

STORAGE_VERSION = 1

_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_WINDOW = 60.0

# Minimal inline HTML login form (replaces html/login.html)
_LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>小度技能授权登录</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;background:#f5f5f5}
.card{background:#fff;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.1);padding:32px;width:320px;max-width:90%}
h2{text-align:center;color:#333;margin:0 0 24px}
input{width:100%;padding:10px 12px;margin:8px 0;border:1px solid #ddd;border-radius:4px;box-sizing:border-box;font-size:14px}
button{width:100%;padding:10px;background:#03a9f4;color:#fff;border:none;border-radius:4px;font-size:16px;cursor:pointer;margin-top:16px}
button:hover{background:#0288d1}
.msg{text-align:center;color:#d32f2f;margin-top:12px;font-size:14px;min-height:20px}
</style>
</head>
<body>
<div class="card">
<h2>小度技能授权</h2>
<form id="loginForm" method="POST">
<input type="text" id="username" name="username" placeholder="HomeAssistant 用户名" required autocomplete="username">
<input type="password" id="password" name="password" placeholder="HomeAssistant 密码" required autocomplete="current-password">
<button type="submit">授权登录</button>
<div class="msg" id="msg"></div>
</form>
</div>
<script>
document.getElementById('loginForm').addEventListener('submit',function(e){
e.preventDefault();
var f=new FormData(this);
var params=new URLSearchParams(window.location.search);
f.append('client_id',params.get('client_id')||'');
f.append('redirect_uri',params.get('redirect_uri')||'');
f.append('state',params.get('state')||'');
fetch(window.location.href,{method:'POST',body:f})
.then(r=>r.json())
.then(d=>{
if(d.code==='ok'&&d.data&&d.data.location){window.location.href=d.data.location}
else{document.getElementById('msg').textContent=d.Msg||'登录失败'}
})
.catch(()=>{document.getElementById('msg').textContent='网络错误'});
});
</script>
</body>
</html>"""


class HavcsServiceView(HomeAssistantView):
    """View to handle skill service requests."""

    url = f'/{INTEGRATION}/service'
    name = f'{INTEGRATION}:service'
    requires_auth = False

    def __init__(self, hass):
        self._hass = hass

    async def post(self, request):
        """Handle skill requests from Xiaodu."""
        start_time = time.time()
        try:
            data = await request.text()
            _LOGGER.debug("[%s] raw message: %s", LOGGER_NAME, havcs_util.mask_tokens(data))
            platform = havcs_util.get_platform_from_command(data)
            auth_value = havcs_util.get_token_from_command(data)
            _LOGGER.debug("[%s] get access_token >>> %s <<<", LOGGER_NAME, f"{auth_value[:6]}..." if auth_value else None)
            refresh_token = self._hass.auth.async_validate_access_token(auth_value)
            if refresh_token:
                _LOGGER.debug("[%s] validate access_token, refresh_token id = %s", LOGGER_NAME, refresh_token.id)
            else:
                _LOGGER.debug("[%s] validate access_token failed", LOGGER_NAME)

            handler = self._hass.data.get(INTEGRATION, {}).get(DATA_HAVCS_HANDLER, {}).get(platform)
            if handler is None:
                _LOGGER.error("[%s] no handler for platform %s", LOGGER_NAME, platform)
                return self.json({})

            response = await handler.handleRequest(
                json.loads(data),
                refresh_token,
                token_expired=havcs_util.is_access_token_expired(auth_value),
            )
        except Exception:
            _LOGGER.error("[%s] handle fail: %s", LOGGER_NAME, traceback.format_exc())
            response = {}
        finally:
            _LOGGER.debug("[%s] -------- http task finish, running time: %ss --------", LOGGER_NAME, round(time.time() - start_time, 3))
        return self.json(response)


class HavcsAuthorizeView(HomeAssistantView):
    """OAuth2 authorization endpoint."""

    url = f'/{INTEGRATION}/auth/authorize'
    name = f'{INTEGRATION}:auth:authorize'
    requires_auth = False

    def __init__(self, hass):
        self._hass = hass
        self._login_attempts = {}

    @property
    def _ha_url(self):
        config = self._hass.data.get(INTEGRATION, {}).get(DATA_HAVCS_CONFIG) or {}
        return config.get('http', {}).get('ha_url') or ''

    def _clients(self):
        return self._hass.data.get(INTEGRATION, {}).get(DATA_HAVCS_CONFIG, {}).get('http', {}).get('clients', {})

    async def head(self, request):
        return web.Response(status=200)

    async def get(self, request):
        """Show inline login form."""
        client_id = request.query.get('client_id')
        if client_id in self._clients():
            return web.Response(body=_LOGIN_HTML, content_type='text/html')
        return web.Response(body='401 Unauthorized', status=401)

    async def post(self, request):
        """Handle login form submission."""
        remote_addr = request.remote or 'unknown'
        now = time.monotonic()
        attempt = self._login_attempts.setdefault(remote_addr, {'count': 0, 'first_time': None, 'last_time': None})
        if attempt['last_time'] is not None and now - attempt['last_time'] > _LOGIN_WINDOW:
            attempt['count'] = 0
            attempt['first_time'] = None
        if attempt['count'] >= _MAX_LOGIN_ATTEMPTS and attempt['first_time'] is not None and now - attempt['first_time'] < _LOGIN_WINDOW:
            _LOGGER.warning("[%s][auth] too many login attempts from %s", LOGGER_NAME, remote_addr)
            return web.Response(body='403 Too Many Attempts', status=403)
        self._prune_attempts(now)

        req = await request.post()
        client_id = req.get('client_id') or request.query.get('client_id')
        redirect_uri = req.get('redirect_uri') or request.query.get('redirect_uri')
        state = req.get('state') or request.query.get('state')
        username = req.get('username')
        password = req.get('password')
        if not all((client_id, redirect_uri, username, password)):
            return web.Response(body='400 Bad Request', status=400)
        if client_id not in self._clients():
            return web.Response(body='400 Bad Request', status=400)

        parts = urlparse(redirect_uri)
        redirect_uri_host = f"{parts.scheme}://{parts.netloc}"
        if redirect_uri_host not in CLIENT_PLATFORM_DICT.values():
            _LOGGER.error("[%s][auth] unsupported redirect_uri: %s", LOGGER_NAME, redirect_uri)
            return web.Response(body='400 Bad Request', status=400)
        if not self._ha_url:
            _LOGGER.error("[%s][auth] integration config is not loaded", LOGGER_NAME)
            return web.Response(body='503 Service Unavailable', status=503)

        try:
            session = async_get_clientsession(self._hass)
            # 每次授权使用独立的 login flow，避免跨用户共享/复用失效 flow
            async with asyncio.timeout(5):
                response = await session.post(self._ha_url + '/auth/login_flow', json={
                    "client_id": redirect_uri_host,
                    "handler": ["homeassistant", None],
                    "redirect_uri": redirect_uri,
                    "type": "authorize",
                })
            flow = await response.json()
            flow_id = flow.get('flow_id') if isinstance(flow, dict) else None
            if not flow_id:
                _LOGGER.error("[%s][auth] create login flow failed: %s", LOGGER_NAME, flow)
                return self.json({'code': 'error', 'Msg': '创建 HA 登录流程失败'})

            async with asyncio.timeout(5):
                response = await session.post(self._ha_url + '/auth/login_flow/' + flow_id, json={
                    "client_id": redirect_uri_host,
                    'username': username,
                    'password': password,
                })
            result = await response.json()
        except (asyncio.TimeoutError, aiohttp.ClientError) as ex:
            _LOGGER.error("[%s][auth] timeout: %r", LOGGER_NAME, ex)
            self._record_failure(attempt, now)
            return self.json({'code': 'error', 'Msg': '连接 HA 登录服务超时'})
        except Exception:
            _LOGGER.error("[%s][auth] %s", LOGGER_NAME, traceback.format_exc())
            self._record_failure(attempt, now)
            return self.json({'code': 'error', 'Msg': 'HA 登录服务异常'})

        code = result.get('result') if isinstance(result, dict) else None
        if not code:
            self._record_failure(attempt, now)
            step_id = result.get('step_id') if isinstance(result, dict) else None
            if step_id and step_id != 'init':
                _LOGGER.warning("[%s][auth] additional login step required: %s", LOGGER_NAME, step_id)
                message = '该 HA 账号启用了两步验证（MFA），请改用未启用 MFA 的账号授权'
            else:
                message = 'HA 用户名或密码错误'
            return self.json({'code': 'error', 'Msg': message})

        attempt['count'] = 0
        attempt['first_time'] = None
        attempt['last_time'] = now
        data = {'code': code}
        if state:
            data['state'] = state
        query_string = urlencode(data)
        separator = '&' if parts.query else ''
        redirect_uri_full = f"{parts.scheme}://{parts.netloc}{parts.path}?{query_string}{separator}{parts.query}"
        return self.json({'code': 'ok', 'Msg': '成功授权', 'data': {'location': redirect_uri_full}})

    @staticmethod
    def _record_failure(attempt, now):
        attempt['count'] += 1
        attempt['first_time'] = attempt['first_time'] or now
        attempt['last_time'] = now

    def _prune_attempts(self, now):
        if len(self._login_attempts) <= 128:
            return
        stale = [addr for addr, item in self._login_attempts.items()
                 if item['last_time'] is None or now - item['last_time'] > _LOGIN_WINDOW * 10]
        for addr in stale:
            self._login_attempts.pop(addr, None)


class HavcsTokenView(HomeAssistantView):
    """OAuth2 token endpoint."""

    url = f'/{INTEGRATION}/auth/token'
    name = f'{INTEGRATION}:auth:token'
    requires_auth = False

    def __init__(self, hass, expiration):
        self._hass = hass
        self._expiration = expiration
        self._store = Store(hass, STORAGE_VERSION, f"{INTEGRATION}_client_ids")
        self._client_ids = {}

    @property
    def _token_url(self):
        config = self._hass.data.get(INTEGRATION, {}).get(DATA_HAVCS_CONFIG) or {}
        ha_url = config.get('http', {}).get('ha_url') or ''
        return f"{ha_url}/auth/token" if ha_url else ''

    def _clients(self):
        return self._hass.data.get(INTEGRATION, {}).get(DATA_HAVCS_CONFIG, {}).get('http', {}).get('clients', {})

    async def async_setup_client_ids(self):
        """恢复 refresh_token → client_id 映射（HA 要求刷新时 client_id 与签发时完全一致）。"""
        self._client_ids = await self._store.async_load() or {}

    async def _async_latch_client_id(self, refresh_token, client_id):
        if not refresh_token or not client_id or self._client_ids.get(refresh_token) == client_id:
            return
        self._client_ids[refresh_token] = client_id
        await self._store.async_save(self._client_ids)

    @staticmethod
    def _normalize_client_id(client_id, redirect_uri):
        """把 小度 传来的 client_id 归一化为 HA 可校验的 URL 形式。"""
        if client_id and client_id.startswith('https://'):
            return client_id
        parts = urlparse(redirect_uri) if redirect_uri else None
        host = f"{parts.scheme}://{parts.netloc}" if parts and parts.scheme and parts.netloc else ''
        if host in CLIENT_PLATFORM_DICT.values():
            return host
        for platform in sorted(CLIENT_PLATFORM_DICT, key=len, reverse=True):
            if client_id and client_id.startswith(platform):
                return CLIENT_PLATFORM_DICT[platform]
        return CLIENT_PLATFORM_DICT['dueros']

    def _forward_client_id(self, client_id, redirect_uri, refresh_token=None):
        """确定转发给 HA 的 client_id。

        刷新时必须沿用签发该 refresh_token 时使用的 client_id，否则 HA 返回 invalid_request。
        """
        if refresh_token and self._client_ids.get(refresh_token):
            return self._client_ids[refresh_token]
        return self._normalize_client_id(client_id, redirect_uri)

    async def get(self, request):
        return web.Response(body='404 Not Found', status=404)

    async def post(self, request):
        """Handle token exchange."""
        body_data = await request.text()
        try:
            data = json.loads(body_data)
        except json.decoder.JSONDecodeError:
            query_string = body_data if body_data else request.query_string
            data = {k: v[0] for k, v in parse.parse_qs(query_string).items()}
        except Exception:
            _LOGGER.error("[%s][auth] handle request: %s", LOGGER_NAME, traceback.format_exc())
            return web.Response(status=400)
        if not isinstance(data, dict):
            return web.Response(status=400)

        grant_type = data.get('grant_type')
        client_id = data.get('client_id')
        client_secret = data.get('client_secret')
        redirect_uri = data.get('redirect_uri')
        refresh_token = data.get('refresh_token')
        clients = self._clients()

        if not self._token_url:
            _LOGGER.error("[%s][auth] integration config is not loaded", LOGGER_NAME)
            return web.Response(body='503 Service Unavailable', status=503)

        if grant_type == 'authorization_code':
            if not all((client_id, client_secret, data.get('code'))):
                _LOGGER.error("[%s][auth] invalid authorization_code request (client_id=%s, has_code=%s, has_secret=%s)",
                              LOGGER_NAME, client_id, bool(data.get('code')), bool(client_secret))
                return web.Response(body='400 Bad Request', status=400)
            stored_secret = clients.get(client_id)
            if stored_secret is None or client_secret != stored_secret:
                _LOGGER.error("[%s][auth] invalid client (client_id=%s)", LOGGER_NAME, client_id)
                return web.Response(body='401 Unauthorized', status=401)
            data['client_id'] = self._forward_client_id(client_id, redirect_uri)
        elif grant_type == 'refresh_token':
            if not refresh_token:
                _LOGGER.error("[%s][auth] refresh request without refresh_token", LOGGER_NAME)
                return web.Response(body='400 Bad Request', status=400)
            if client_id:
                stored_secret = clients.get(client_id)
                if stored_secret is None or (client_secret and client_secret != stored_secret):
                    _LOGGER.error("[%s][auth] invalid client on refresh (client_id=%s)", LOGGER_NAME, client_id)
                    return web.Response(body='401 Unauthorized', status=401)
            data['client_id'] = self._forward_client_id(client_id, redirect_uri, refresh_token)
        else:
            _LOGGER.error("[%s][auth] unsupported grant_type: %s", LOGGER_NAME, grant_type)
            return web.Response(body='400 Unsupported Grant Type', status=400)

        _LOGGER.debug("[%s][auth] %s → HA: client_id=%s (incoming=%s, redirect_uri=%s)",
                      LOGGER_NAME, grant_type, data.get('client_id'), client_id, bool(redirect_uri))

        session = async_get_clientsession(self._hass)
        try:
            async with asyncio.timeout(5):
                response = await session.post(self._token_url, data=data)
        except (asyncio.TimeoutError, aiohttp.ClientError):
            _LOGGER.error("[%s][auth] fail to get token: timeout", LOGGER_NAME)
            return web.Response(status=500)
        except Exception:
            _LOGGER.error("[%s][auth] fail to get token: %s", LOGGER_NAME, traceback.format_exc())
            return web.Response(status=500)

        try:
            result = await response.json()
        except Exception:
            _LOGGER.error("[%s][auth] invalid token response (status=%s)", LOGGER_NAME, response.status)
            return web.Response(status=response.status if response.status >= 400 else 500)
        if not isinstance(result, dict) or not result.get('access_token'):
            self._log_token_failure(grant_type, data.get('client_id'), result, response.status)
            return web.Response(status=response.status if response.status >= 400 else 400)

        if grant_type == 'authorization_code':
            extended = havcs_util.update_token_expiration(result['access_token'], self._hass, self._expiration)
            await self._async_latch_client_id(result.get('refresh_token'), data['client_id'])
            if extended:
                refreshed = await self._async_refresh(session, data.get('client_id'), result.get('refresh_token'))
                if refreshed and refreshed.get('access_token'):
                    result['access_token'] = refreshed['access_token']
                else:
                    extended = False
            if extended:
                result['expires_in'] = int(self._expiration.total_seconds())
            else:
                # 无法延长有效期时如实返回 HA 给出的有效期
                result['expires_in'] = int(result.get('expires_in', 1800))
            return self.json(result)

        await self._async_latch_client_id(refresh_token, data['client_id'])
        result['refresh_token'] = refresh_token
        return self.json(result)

    @staticmethod
    def _log_token_failure(grant_type, client_id, result, status):
        """记录 HA 拒绝原因（区分 client_id 不一致 / client_id 非法 / token 无效）。"""
        error = result.get('error') if isinstance(result, dict) else result
        description = result.get('error_description') if isinstance(result, dict) else None
        _LOGGER.error("[%s][auth] token exchange failed (%s): client_id=%s, error=%s, description=%s",
                      LOGGER_NAME, grant_type, client_id, error, description)
        if error == 'invalid_request':
            _LOGGER.error(
                "[%s][auth] HA 拒绝原因通常是 client_id 与签发 refresh_token 时不一致或格式非法；"
                "如反复出现，请在小度 APP 重新绑定设备以重新签发 token", LOGGER_NAME)

    async def _async_refresh(self, session, client_id, refresh_token):
        """用 refresh_token 重新签发 access token（使新有效期生效）。"""
        if not refresh_token:
            return None
        payload = {
            'client_id': client_id,
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
        }
        try:
            async with asyncio.timeout(5):
                response = await session.post(self._token_url, data=payload)
            return await response.json()
        except Exception:
            _LOGGER.error("[%s][auth] refresh after code exchange failed: %s", LOGGER_NAME, traceback.format_exc())
            return None


class HavcsHttpManager:
    """Manager for HAVCS HTTP views."""

    def __init__(self, hass, ha_url, client_id, client_secret, expiration_hours=24):
        self._hass = hass
        self._ha_url = ha_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._expiration = timedelta(hours=expiration_hours)

    def register_views(self):
        """注册视图；已注册过的实例复用，避免重复注册与状态残留。"""
        cache = self._hass.data.setdefault(INTEGRATION, {}).setdefault('http_views', {})
        views = {
            'service': HavcsServiceView(self._hass),
            'authorize': HavcsAuthorizeView(self._hass),
            'token': HavcsTokenView(self._hass, self._expiration),
        }
        for key, view in views.items():
            if key in cache:
                _LOGGER.debug("[%s] view %s already registered, reuse existing instance", LOGGER_NAME, key)
            else:
                self._hass.http.register_view(view)
                cache[key] = view
            if key == 'token':
                self._hass.async_create_task(cache[key].async_setup_client_ids())
