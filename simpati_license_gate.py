"""simpati_license_gate.py - penegakan lisensi untuk server Survei (modul SURVEI_GPS).

Server Survei tidak punya kredensial License Manager sendiri, jadi status lisensi diambil dari
SIMPATI 555 (GET /api/integrasi/lisensi/status) memakai SIMPATI_INTEGRATION_TOKEN. Respons
ditandatangani HMAC (kunci = token, memuat nonce penanya) sehingga tidak bisa dipalsukan/replay.

Kebijakan (fail-closed):
  * SIMPATI_API_URL / token belum dikonfigurasi            -> terbatas
  * 555 melaporkan restricted, atau modul SURVEI_GPS tak berlisensi -> terbatas
  * 555 menolak (401/403/404)                              -> terbatas seketika
  * 555 tak terjangkau                                     -> pakai status baik terakhir selama
                                                              OFFLINE_GRACE_DAYS (default 7), lalu terbatas
  * Belum pernah berhasil sama sekali                      -> terbatas
Env: SURVEY_LICENSE_ENFORCE (default true; false hanya untuk pengembangan lokal),
     SURVEY_LICENSE_TTL_SECONDS (default 300), SURVEY_LICENSE_OFFLINE_GRACE_DAYS (default 7),
     SURVEY_LICENSE_STATE_FILE (default ./license_gate_state.json).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional, Tuple

logger = logging.getLogger("simpati_license_gate")

# Mutasi yang tetap diizinkan walau terbatas: login/logout dan tindakan yang MENGURANGI akses.
ALLOWED_EXACT = {
    "/auth/login", "/auth/logout",
    "/dps/login", "/dps/logout", "/dpc/login", "/dpd/login", "/dpc/logout", "/dpd/logout",
    "/superuser/logout", "/admin-survei/login", "/admin-survei/logout", "/admin-survei/password",
}
ALLOWED_PATTERNS = [
    re.compile(r"^/api/integrasi/simpati/anggota/[^/]+/nonaktifkan$"),
    re.compile(r"^/admin/akun-petugas/[^/]+/nonaktifkan$"),
    re.compile(r"^/admin/officers/[^/]+/deactivate$"),
    re.compile(r"^/admin/kelola-akun/[^/]+/nonaktifkan$"),
]
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def is_mutation_allowed_when_restricted(method: str, path: str) -> bool:
    if method.upper() not in MUTATING_METHODS:
        return True
    path = path.rstrip("/") or "/"
    return path in ALLOWED_EXACT or any(p.match(path) for p in ALLOWED_PATTERNS)


class SimpatiLicenseGate:
    def __init__(
        self,
        api_url: str,
        token: str,
        enforce: Optional[bool] = None,
        ttl_seconds: Optional[float] = None,
        offline_grace_days: Optional[float] = None,
        state_file: Optional[str] = None,
        fetcher: Optional[Callable[[str, str, float], dict]] = None,
        clock: Callable[[], float] = time.time,
    ):
        env = os.environ.get
        self.api_url = (api_url or "").rstrip("/")
        self.token = token or ""
        self.enforce = (env("SURVEY_LICENSE_ENFORCE", "true").strip().lower() in ("1", "true", "yes", "on")
                        if enforce is None else enforce)
        self.ttl = float(env("SURVEY_LICENSE_TTL_SECONDS", "300")) if ttl_seconds is None else ttl_seconds
        self.grace = (float(env("SURVEY_LICENSE_OFFLINE_GRACE_DAYS", "7")) if offline_grace_days is None
                      else offline_grace_days) * 86400
        self.state_file = state_file or env("SURVEY_LICENSE_STATE_FILE", "./license_gate_state.json")
        self._fetch = fetcher or self._http_fetch
        self._clock = clock
        self._refresh_lock = threading.Lock()
        self._last_good: Optional[dict] = None       # {"restricted": bool, "status": str, "ts": float}
        self._offline_since: Optional[float] = None
        self._rejected: Optional[str] = None
        self._max_seen = 0.0
        self._last_check = 0.0
        self._load_state()

    # ---- util ----
    def _now(self) -> float:
        real = self._clock()
        if real > self._max_seen:
            self._max_seen = real
        return max(real, self._max_seen)   # jam yang dimundurkan tidak memperpanjang grace

    def _key(self) -> bytes:
        return hashlib.sha256(b"simpati-survey-gate|" + self.token.encode()).digest()

    def _mac(self, s: str) -> str:
        return hmac.new(self._key(), s.encode(), hashlib.sha256).hexdigest()

    def _load_state(self) -> None:
        if not self.token:
            return
        try:
            with open(self.state_file, encoding="utf-8") as f:
                env = json.load(f)
            if not hmac.compare_digest(self._mac(env["data"]), str(env.get("mac", ""))):
                logger.error("State lisensi %s gagal verifikasi integritas; diabaikan.", self.state_file)
                return
            d = json.loads(env["data"])
            self._last_good, self._offline_since = d.get("last_good"), d.get("offline_since")
            self._max_seen = float(d.get("max_seen") or 0.0)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("Gagal memuat state lisensi: %s", e)

    def _save_state(self) -> None:
        if not self.token:
            return
        try:
            data = json.dumps({"last_good": self._last_good, "offline_since": self._offline_since,
                               "max_seen": self._max_seen}, sort_keys=True)
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump({"data": data, "mac": self._mac(data)}, f)
        except Exception as e:
            logger.debug("Gagal menyimpan state lisensi: %s", e)

    # ---- HTTP & verifikasi ----
    def _http_fetch(self, url: str, token: str, timeout: float) -> dict:
        req = urllib.request.Request(url, headers={"X-SIMPATI-Token": token, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _verify(self, body: dict, nonce: str) -> dict:
        sig = str(body.pop("signature", ""))
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        if not sig or not hmac.compare_digest(self._mac_token(canonical), sig):
            raise ValueError("Tanda tangan status lisensi tidak valid.")
        if body.get("nonce") != nonce:
            raise ValueError("Nonce tidak cocok (replay).")
        return body

    def _mac_token(self, s: str) -> str:
        # Harus sama dengan sisi 555: HMAC-SHA256(kunci = token mentah)
        return hmac.new(self.token.encode(), s.encode(), hashlib.sha256).hexdigest()

    def _refresh(self) -> None:
        nonce = secrets.token_hex(12)
        url = f"{self.api_url}/api/integrasi/lisensi/status?nonce={urllib.parse.quote(nonce)}"
        now = self._now()
        try:
            body = self._verify(dict(self._fetch(url, self.token, 10.0)), nonce)
            restricted = bool(body.get("restricted")) or not bool(body.get("survei_module_allowed"))
            self._last_good = {"restricted": restricted, "status": str(body.get("status")), "ts": now}
            self._offline_since, self._rejected = None, None
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404, 422):       # ditolak: bukan offline, tanpa grace
                self._rejected = f"SIMPATI 555 menolak permintaan status lisensi (HTTP {e.code})."
                logger.error(self._rejected)
            else:
                self._mark_offline(now, e)
        except ValueError as e:                       # respons palsu/replay: tanpa grace
            self._rejected = str(e)
            logger.error("Status lisensi ditolak: %s", e)
        except Exception as e:
            self._mark_offline(now, e)
        self._last_check = now
        self._save_state()

    def _mark_offline(self, now: float, exc: Exception) -> None:
        if self._offline_since is None:
            self._offline_since = now
        self._rejected = None
        logger.warning("SIMPATI 555 tidak terjangkau untuk cek lisensi: %s", exc)

    # ---- API ----
    def evaluate(self) -> Tuple[bool, str]:
        """(restricted, alasan). Aman dipanggil dari banyak thread."""
        if not self.enforce:
            return False, ""
        if not self.api_url or not self.token:
            return True, "SIMPATI_API_URL dan SIMPATI_INTEGRATION_TOKEN wajib dikonfigurasi untuk verifikasi lisensi."
        with self._refresh_lock:
            if self._now() - self._last_check >= self.ttl or self._last_check == 0.0:
                self._refresh()
            now = self._now()
            if self._rejected:
                return True, self._rejected
            if self._offline_since is not None:
                if not self._last_good:
                    return True, "Lisensi belum pernah terverifikasi dan SIMPATI 555 tidak terjangkau."
                if now - self._offline_since > self.grace:
                    return True, "Masa tenggang offline habis; lisensi tidak dapat diverifikasi ke SIMPATI 555."
            if not self._last_good:
                return True, "Lisensi belum terverifikasi."
            if self._last_good["restricted"]:
                return True, "Lisensi SIMPATI tidak aktif atau tidak mencakup modul Survei (SURVEI_GPS)."
            return False, ""
