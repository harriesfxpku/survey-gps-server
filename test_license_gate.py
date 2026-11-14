"""Tes gerbang lisensi server Survei. Jalankan: python -m unittest test_license_gate -v"""
import hashlib, hmac, json, os, tempfile, threading, unittest, urllib.error, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from simpati_license_gate import SimpatiLicenseGate, is_mutation_allowed_when_restricted

TOKEN = "integration-token-123"


def server_response(token, nonce, restricted=False, module=True, status="ACTIVE"):
    """Meniru endpoint /api/integrasi/lisensi/status di SIMPATI 555."""
    body = {"status": status, "restricted": restricted, "survei_module_allowed": module, "nonce": nonce}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    body["signature"] = hmac.new(token.encode(), canonical, hashlib.sha256).hexdigest()
    return body


def nonce_of(url):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["nonce"][0]


class Clock:
    def __init__(self): self.t = 1_800_000_000.0
    def __call__(self): return self.t


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state.json")
        self.clock = Clock()

    def gate(self, fetcher, **kw):
        return SimpatiLicenseGate("http://555", TOKEN, enforce=True, ttl_seconds=300,
                                  offline_grace_days=7, state_file=self.state,
                                  fetcher=fetcher, clock=self.clock, **kw)

    def ok(self, **kw):
        return lambda url, tok, to: server_response(TOKEN, nonce_of(url), **kw)

    def test_active_license_allows(self):
        self.assertEqual(self.gate(self.ok()).evaluate(), (False, ""))

    def test_restricted_blocks(self):
        r, why = self.gate(self.ok(restricted=True, status="RESTRICTED")).evaluate()
        self.assertTrue(r); self.assertIn("tidak aktif", why)

    def test_module_not_licensed_blocks(self):
        self.assertTrue(self.gate(self.ok(module=False)).evaluate()[0])

    def test_unconfigured_blocks_when_enforced(self):
        g = SimpatiLicenseGate("", "", enforce=True, state_file=self.state)
        self.assertTrue(g.evaluate()[0])

    def test_dev_mode_disables(self):
        g = SimpatiLicenseGate("", "", enforce=False, state_file=self.state)
        self.assertEqual(g.evaluate(), (False, ""))

    def test_forged_signature_blocks(self):
        def f(url, tok, to):
            b = server_response("token-lain", nonce_of(url)); return b
        self.assertTrue(self.gate(f).evaluate()[0])

    def test_replayed_response_blocks(self):
        stale = server_response(TOKEN, "nonce-lama-0001")
        self.assertTrue(self.gate(lambda u, t, to: dict(stale)).evaluate()[0])

    def test_first_boot_555_down_blocks(self):
        def down(u, t, to): raise ConnectionError("down")
        r, why = self.gate(down).evaluate()
        self.assertTrue(r); self.assertIn("belum pernah", why)

    def test_offline_grace_then_block(self):
        mode = {"down": False}
        def f(u, t, to):
            if mode["down"]: raise ConnectionError("down")
            return server_response(TOKEN, nonce_of(u))
        g = self.gate(f)
        self.assertFalse(g.evaluate()[0])
        mode["down"] = True
        self.clock.t += 400;           self.assertFalse(g.evaluate()[0])      # dalam grace
        self.clock.t += 6 * 86400;     self.assertFalse(g.evaluate()[0])
        self.clock.t += 2 * 86400;     self.assertTrue(g.evaluate()[0])       # grace habis

    def test_restart_does_not_reset_offline_grace(self):
        mode = {"down": False}
        def f(u, t, to):
            if mode["down"]: raise ConnectionError("down")
            return server_response(TOKEN, nonce_of(u))
        self.assertFalse(self.gate(f).evaluate()[0])
        mode["down"] = True
        self.clock.t += 400; self.gate(f).evaluate()                      # mulai offline, state disimpan
        self.clock.t += 8 * 86400
        self.assertTrue(self.gate(f).evaluate()[0])                       # proses baru, tetap diblokir

    def test_tampered_state_file_is_ignored(self):
        self.gate(self.ok()).evaluate()
        with open(self.state, encoding="utf-8") as f:
            env = json.load(f)
        d = json.loads(env["data"])
        d["last_good"]["ts"] = 9_999_999_999.0          # diubah, MAC tidak diperbarui
        d["max_seen"] = 0.0
        env["data"] = json.dumps(d, sort_keys=True)
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump(env, f)
        def down(u, t, to): raise ConnectionError("down")
        r, why = self.gate(down).evaluate()
        self.assertTrue(r); self.assertIn("belum pernah", why)   # state diabaikan

    def test_clock_rollback_does_not_extend_grace(self):
        mode = {"down": False}
        def f(u, t, to):
            if mode["down"]: raise ConnectionError("down")
            return server_response(TOKEN, nonce_of(u))
        g = self.gate(f); g.evaluate(); mode["down"] = True
        self.clock.t += 400; g.evaluate()
        self.clock.t += 9 * 86400; self.assertTrue(g.evaluate()[0])
        self.clock.t -= 9 * 86400; self.assertTrue(g.evaluate()[0])       # jam dimundurkan: tetap diblokir

    def test_license_renewal_is_picked_up_after_ttl(self):
        st = {"restricted": True}
        g = self.gate(lambda u, t, to: server_response(TOKEN, nonce_of(u), restricted=st["restricted"]))
        self.assertTrue(g.evaluate()[0])
        st["restricted"] = False; self.clock.t += 301
        self.assertFalse(g.evaluate()[0])

    def test_real_http_404_is_rejection_not_offline(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(s): s.send_response(404); s.end_headers()
            def log_message(s, *a): pass
        srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        g = SimpatiLicenseGate(f"http://127.0.0.1:{srv.server_port}", TOKEN, enforce=True,
                               state_file=self.state, clock=self.clock)
        r, why = g.evaluate()
        self.assertTrue(r); self.assertIn("404", why)

    def test_real_http_success(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(s):
                if s.headers.get("X-SIMPATI-Token") != TOKEN:
                    s.send_response(401); s.end_headers(); return
                body = json.dumps(server_response(TOKEN, nonce_of(s.path))).encode()
                s.send_response(200); s.send_header("Content-Type", "application/json"); s.end_headers(); s.wfile.write(body)
            def log_message(s, *a): pass
        srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        g = SimpatiLicenseGate(f"http://127.0.0.1:{srv.server_port}", TOKEN, enforce=True,
                               state_file=self.state, clock=self.clock)
        self.assertEqual(g.evaluate(), (False, ""))


class AllowlistTests(unittest.TestCase):
    def test_reads_always_allowed(self):
        self.assertTrue(is_mutation_allowed_when_restricted("GET", "/surveys"))

    def test_login_and_deactivation_allowed(self):
        for m, p in [("POST", "/auth/login"), ("POST", "/dps/login/"), ("POST", "/admin-survei/password"),
                     ("POST", "/api/integrasi/simpati/anggota/abc/nonaktifkan"),
                     ("POST", "/admin/akun-petugas/12/nonaktifkan")]:
            self.assertTrue(is_mutation_allowed_when_restricted(m, p), p)

    def test_simpati_555_m2m_sync_allowed(self):
        # 555 (otoritas lisensi) sudah menggerbang aksi ini sendiri; lihat simpati_license_gate.py.
        for p in ("/api/integrasi/simpati/admin-survei", "/api/integrasi/simpati/admin-survei/",
                  "/api/integrasi/simpati/anggota"):
            self.assertTrue(is_mutation_allowed_when_restricted("POST", p), p)
        self.assertFalse(is_mutation_allowed_when_restricted("POST", "/api/integrasi/simpati/lainnya"))

    def test_data_mutations_blocked(self):
        for m, p in [("POST", "/surveys"), ("POST", "/surveys/9/media"), ("DELETE", "/admin/surveys/3"),
                     ("POST", "/dps/surveys/1/verification"), ("POST", "/surveys/../auth/login/x")]:
            self.assertFalse(is_mutation_allowed_when_restricted(m, p), p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
