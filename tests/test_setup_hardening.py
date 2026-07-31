import importlib.machinery
import importlib.util
import os
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HELPER_PATH = os.path.join(ROOT, "scripts", "the-print-farm-helper")
SETUP_PATH = os.path.join(ROOT, "setup.sh")


def load_helper():
    loader = importlib.machinery.SourceFileLoader("print_farm_helper", HELPER_PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


helper = load_helper()


class PrivilegedHelperTests(unittest.TestCase):
    def test_update_installs_and_checks_declared_dependencies(self):
        with open(HELPER_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('"pip", "install", "-r"', source)
        self.assertIn('"pip", "check"', source)
        self.assertIn('requirements = Path(repo) / "requirements.txt"', source)

    def test_update_reconciles_in_a_transient_privileged_unit(self):
        with open(HELPER_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('_command_path("systemd-run")', source)
        self.assertIn('"--service-type=exec"', source)
        self.assertIn('f"--unit=the-print-farm-update-{os.getpid()}"', source)

    def test_restart_is_queued_without_waiting_on_its_own_service(self):
        with open(HELPER_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('"--no-block",\n            "restart",', source)

    def test_printer_names_and_ports_are_strictly_validated(self):
        self.assertEqual(helper.validate_printer_name("P1S-1"), "P1S-1")
        self.assertEqual(helper.validate_port("5001"), 5001)
        for name in ("", "../escape", "bad\nname", "-starts-with-symbol"):
            with self.subTest(name=name), self.assertRaises(helper.HelperError):
                helper.validate_printer_name(name)
        for port in ("not-a-port", 0, 80, 65536):
            with self.subTest(port=port), self.assertRaises(helper.HelperError):
                helper.validate_port(port)

    def test_site_ids_do_not_collide_after_sanitising(self):
        first = helper.site_id("Printer One")
        second = helper.site_id("Printer-One")
        third = helper.site_id("printer one")
        self.assertEqual(len({first, second, third}), 3)
        self.assertRegex(first, r"^printer-[a-z0-9-]+-[0-9a-f]{10}$")

    def test_orca_vhost_encodes_name_and_has_no_wildcard_cors(self):
        content = helper.render_orca_vhost("Printer One", 5001, 5000)
        self.assertIn("http://127.0.0.1:5000/Printer%20One/api", content)
        self.assertIn("ProxyPreserveHost On", content)
        self.assertNotIn("Access-Control-Allow-Origin", content)
        self.assertNotIn("\nListen ", content)

    def test_managed_listen_lines_are_reconciled_without_duplicates(self):
        original = "Listen 80\nListen 5001\nListen 5001\nListen 5002\nListen 7443\n"
        content = helper.reconcile_listen_lines(
            original,
            existing_ports={5001, 5002},
            desired_ports={5001, 5003},
            other_ports={5002},
        )
        self.assertEqual(content.count("Listen 5001\n"), 1)
        self.assertEqual(content.count("Listen 5002\n"), 1)
        self.assertEqual(content.count("Listen 5003\n"), 1)
        self.assertEqual(content.count("Listen 7443\n"), 1)

    def test_proxy_block_can_be_inserted_and_updated_idempotently(self):
        original = "<VirtualHost *:80>\n    DocumentRoot /var/www/html\n</VirtualHost>\n"
        installed = helper.update_proxy_config(original, 5000)
        self.assertIn(helper.PROXY_BEGIN, installed)
        managed = installed[
            installed.index(helper.PROXY_BEGIN):
            installed.index(helper.PROXY_END) + len(helper.PROXY_END)
        ]
        self.assertIn(
            'ProxyPassMatch "^/api/(version|connection|printer|files/local)$"',
            managed,
        )
        self.assertIn("ProxyPass /the-print-farm ", managed)
        self.assertNotIn("ProxyPass /api ", managed)
        self.assertNotIn("LimitRequestBody", managed)
        self.assertNotIn(r"^/[^/]+/api/", installed)
        self.assertEqual(helper.update_proxy_config(installed, 5000), installed)

        updated = helper.update_proxy_config(installed, 5100)
        self.assertIn("127.0.0.1:5100", updated)
        self.assertNotIn("127.0.0.1:5000", updated)

    def test_legacy_proxy_migration_preserves_unrelated_directives(self):
        original = """<VirtualHost *:80>
    ServerName farm.example
    # the-print-farm proxy
    # Allow large file uploads (10GB)
    LimitRequestBody 10737418240
    ProxyPass /api http://127.0.0.1:5000/api
    ProxyPassReverse /api http://127.0.0.1:5000/api
    Header always set X-Unrelated yes
</VirtualHost>
"""
        migrated = helper.update_proxy_config(original, 5000)
        self.assertIn("Header always set X-Unrelated yes", migrated)
        self.assertNotIn("LimitRequestBody", migrated)
        self.assertNotIn("10737418240", migrated)
        self.assertEqual(migrated.count(helper.PROXY_BEGIN), 1)

    def test_ambiguous_vhost_is_rejected(self):
        content = (
            "<VirtualHost *:80>\n</VirtualHost>\n"
            "<VirtualHost *:443>\n</VirtualHost>\n"
        )
        with self.assertRaises(helper.HelperError):
            helper.update_proxy_config(content, 5000)

    def test_duplicate_managed_proxy_blocks_are_rejected(self):
        block = helper.render_proxy_block(5000)
        content = f"<VirtualHost *:80>\n{block}\n{block}\n</VirtualHost>\n"
        with self.assertRaises(helper.HelperError):
            helper.update_proxy_config(content, 5000)


class SetupScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SETUP_PATH, "r", encoding="utf-8") as handle:
            cls.script = handle.read()

    def test_strict_shell_and_private_umask_are_enabled(self):
        self.assertIn("set -Eeuo pipefail", self.script)
        self.assertIn("umask 077", self.script)
        self.assertIn("the-print-farm-setup.lock", self.script)
        self.assertIn("SERVICE_OPTION_COUNT <= 1", self.script)
        self.assertIn('mv -Tf "${CONFIG_LINK_DIR}/config.yaml" "$REPO_CONFIG_PATH"', self.script)
        self.assertIn("Restart refused while print activity exists", self.script)
        self.assertIn('safe_statuses = {"IDLE", "FINISH", "FAILED"}', self.script)
        self.assertIn("configured_names - set(states)", self.script)
        self.assertIn("printer state is not confirmed safe", self.script)
        self.assertIn("--force-restart must be used together with --restart", self.script)
        self.assertIn('ad["ca_certs_file"]', self.script)
        self.assertIn("Python 3.10 or newer is required", self.script)
        self.assertIn("configured runtime paths", self.script.lower())
        self.assertIn("must be a real directory, not a symlink", self.script)
        self.assertIn("migrate_api_keys(config)", self.script)
        self.assertIn('"orca_api_key": os.environ["FARM_SETUP_ORCA_API_KEY"]', self.script)
        self.assertIn('"admin_api_key": os.environ["FARM_SETUP_ADMIN_API_KEY"]', self.script)
        self.assertIn("Share this key with users who connect OrcaSlicer", self.script)
        self.assertIn(
            'install -r "${SCRIPT_DIR}/requirements.txt"',
            self.script,
        )
        self.assertIn('FARM_SETUP_TRANSIENT_UNIT', self.script)
        self.assertIn('--unit="the-print-farm-setup-${BASHPID}"', self.script)
        self.assertIn('! runuser -u root -- true', self.script)

    def test_setup_does_not_grant_generic_privileged_commands(self):
        forbidden = (
            "NOPASSWD: /usr/bin/tee",
            "NOPASSWD: /bin/rm",
            "NOPASSWD: /bin/cat",
            "NOPASSWD: /usr/sbin/a2ensite",
            "NOPASSWD: /usr/sbin/a2dissite",
        )
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, self.script)
        self.assertIn(
            "NOPASSWD: ${HELPER_PATH}",
            self.script,
        )

    def test_setup_does_not_advertise_the_loopback_backend(self):
        self.assertNotIn("(Direct):", self.script)
        self.assertNotIn("systemctl restart apache2", self.script)
        self.assertIn('"$HELPER_PATH" configure-proxy', self.script)
        self.assertIn('socket.create_connection(("127.0.0.1", port)', self.script)


if __name__ == "__main__":
    unittest.main()
