"""Utility helpers for hass_baidu_dueros."""

import logging
import re
import time

from base64 import b64decode, b64encode

import jwt
from Crypto.Cipher import AES
from packaging import version

_LOGGER = logging.getLogger(__name__)

ENTITY_KEY = ''

_TOKEN_PATTERN = re.compile(r'("(?:accessToken|token)"\s*:\s*")([^"]+)"', re.I)


def mask_tokens(text):
    """日志脱敏：仅保留 token 前 6 位。"""
    if not text:
        return text
    return _TOKEN_PATTERN.sub(lambda m: f'{m.group(1)}{m.group(2)[:6]}..."', text)


class AESCipher:
    def __init__(self, key):
        self.key = key
        self.mode = AES.MODE_CBC

    def encrypt(self, raw):
        block_size = 16
        pad = lambda s: s + (block_size - len(s) % block_size) * chr(block_size - len(s) % block_size).encode('utf8')
        raw = pad(raw)
        cipher = AES.new(self.key, self.mode, b'0000000000000000')
        return b64encode(cipher.encrypt(raw)).decode('utf8')

    def decrypt(self, enc):
        unpad = lambda s: s[:-ord(s[len(s) - 1:])]
        enc = b64decode(enc)
        cipher = AES.new(self.key, self.mode, b'0000000000000000')
        return unpad(cipher.decrypt(enc)).decode('utf8')


def decrypt_device_id(device_id):
    """Decrypt an appliance id, return None when it cannot be decoded."""
    try:
        if not ENTITY_KEY:
            return device_id
        device_id = device_id.replace('-', '+').replace('_', '/')
        device_id += '=' * (-len(device_id) % 4)
        return AESCipher(ENTITY_KEY.encode('utf-8')).decrypt(device_id)
    except Exception:
        return None


def encrypt_device_id(device_id):
    """Encrypt an appliance id (plain passthrough when no key configured)."""
    if not ENTITY_KEY:
        new_device_id = device_id
    else:
        new_device_id = AESCipher(ENTITY_KEY.encode('utf-8')).encrypt(device_id.encode('utf8'))
        new_device_id = new_device_id.replace('+', '-').replace('/', '_').replace('=', '')
    return new_device_id


def get_platform_from_command(command):
    if 'DuerOS' in command:
        return 'dueros'
    return 'unknown'


def get_token_from_command(command):
    result = re.search(r'(?:accessToken|token)[\'"\s:]+(.*?)[\'"\s]+(,|\})', command, re.M | re.I)
    return result.group(1) if result else None


def update_token_expiration(access_token, hass, expiration):
    """Update the refresh token access-token expiration so new tokens last longer."""
    try:
        if version.parse(jwt.__version__) < version.parse("2.0.0"):
            unverif_claims = jwt.decode(access_token, verify=False)
        else:
            unverif_claims = jwt.decode(
                access_token, algorithms=["HS256"], options={"verify_signature": False}
            )
    except jwt.InvalidTokenError:
        _LOGGER.debug("[util] access_token is invalid, expiration not updated")
        return False

    refresh_token = hass.auth.async_get_refresh_token(unverif_claims.get('iss'))
    if refresh_token is None:
        _LOGGER.debug("[util] refresh_token not found for access_token")
        return False

    if refresh_token.access_token_expiration != expiration:
        _LOGGER.debug("[util] set new access token expiration for refresh_token[%s]", refresh_token.id)
        refresh_token.access_token_expiration = expiration
    return True


def is_access_token_expired(token):
    """Return True when the token is well formed but expired."""
    if not token:
        return False
    try:
        claims = jwt.decode(token, algorithms=["HS256"], options={"verify_signature": False})
    except jwt.InvalidTokenError:
        return False
    exp = claims.get('exp')
    return bool(exp) and int(exp) <= int(time.time())
