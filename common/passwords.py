"""パスワードのハッシュ化(8.3.4.1、AU-09)。

Python標準の scrypt を使う。入力されたままの文字列では保存しない。
空欄のパスワード(閲覧者の初期値)も、同じ方法でハッシュ化する。
"""

import base64
import hashlib
import hmac
import secrets

from common.constants import SCRYPT_DKLEN, SCRYPT_N, SCRYPT_P, SCRYPT_R, SCRYPT_SALT_BYTES

_SCHEME = "scrypt"
# scrypt の計算に使うメモリの上限(バイト)。N と r から必要な量を求め、余裕を持たせる
_MAXMEM = 128 * SCRYPT_N * SCRYPT_R * 2


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=dklen)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    """パスワードをハッシュ化した文字列を返す。

    形式:scrypt$<N>$<r>$<p>$<ソルト>$<ハッシュ値>(ソルトとハッシュ値は Base64)
    ソルト(パスワードごとに変える乱数)を付けるため、同じパスワードでも毎回違う文字列になる。
    """
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN)
    return f"{_SCHEME}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, password_hash: str) -> bool:
    """パスワードが、保存したハッシュ値と一致するかを確かめる。形式が壊れている場合は False。"""
    try:
        scheme, n_text, r_text, p_text, salt_b64, digest_b64 = password_hash.split("$")
        if scheme != _SCHEME:
            return False
        n, r, p = int(n_text), int(r_text), int(p_text)
        if (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            # 想定外の条件のハッシュ値は、計算量が大きくなるおそれがあるため受け付けない
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(digest_b64, validate=True)
    except ValueError:
        return False
    actual = _scrypt(password, salt, n, r, p, len(expected))
    return hmac.compare_digest(actual, expected)
