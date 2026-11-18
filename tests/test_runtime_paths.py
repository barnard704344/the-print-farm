import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from src.runtime_paths import obico_state_directory

ROOT = Path(__file__).resolve().parents[1]


class ObicoRuntimePathTests(unittest.TestCase):
    def test_default_and_custom_database_share_their_persistent_directory(self):
        self.assertEqual(obico_state_directory({}, "/opt/farm"), Path("/opt/farm/data/obico"))
        self.assertEqual(obico_state_directory({"db_path":"state/queue.db"}, "/opt/farm"), Path("/opt/farm/state/obico"))
        self.assertEqual(obico_state_directory({"db_path":"/srv/farm/queue.db"}, "/opt/farm"), Path("/srv/farm/obico"))

    def test_override_is_resolved_relative_to_this_installation(self):
        self.assertEqual(obico_state_directory({"obico_state_dir":"private/obico"}, "/opt/site-one"), Path("/opt/site-one/private/obico"))
        self.assertEqual(obico_state_directory({"obico_state_dir":"/srv/site-two/obico"}, "/opt/site-one"), Path("/srv/site-two/obico"))

    def test_installer_prepares_and_validates_runtime_directory(self):
        script = (ROOT / "setup.sh").read_text()
        section = script[script.index('info "Verifying service-user access to configured runtime paths..."'):]
        python = section.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("uploads", "data", "logs"):
                (root/name).mkdir()
            config_path = root/"config.yaml"
            config = {"queue": {"db_path":"data/queue.db", "upload_dir":"uploads"}}
            config_path.write_text(yaml.safe_dump(config))
            env = dict(os.environ, FARM_CONFIG_PATH=str(config_path), FARM_REPO_DIR=str(root))
            result = subprocess.run([sys.executable,"-c",python],cwd=ROOT,env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue((root/"data/obico").is_dir())
            self.assertEqual((root/"data/obico").stat().st_mode & 0o777,0o700)
            config["queue"]["obico_state_dir"] = "custom/identities"
            config_path.write_text(yaml.safe_dump(config))
            result = subprocess.run([sys.executable,"-c",python],cwd=ROOT,env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue((root/"custom/identities").is_dir())
            blocked = root/"not-a-directory"
            blocked.write_text("preserve this file")
            config["queue"]["obico_state_dir"] = str(blocked)
            config_path.write_text(yaml.safe_dump(config))
            result = subprocess.run([sys.executable,"-c",python],cwd=ROOT,env=env,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn("Cannot prepare queue.obico_state_dir",result.stderr)
            self.assertEqual(blocked.read_text(),"preserve this file")


if __name__ == "__main__":
    unittest.main()
