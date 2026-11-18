import unittest
from unittest.mock import Mock, patch

from src.obico_client import ObicoClient


class ObicoClientTests(unittest.TestCase):
    def registration_client(self):
        client = ObicoClient("https://obico.test", "user", "secret", 0)
        client._session = Mock()
        return client

    def test_registers_installed_name_in_monitoring_mode_and_reuses_on_retry(self):
        client = self.registration_client()
        record = {"id": 6, "name": "Work-BBL-37", "auth_token": "private", "agent_name": "the_print_farm", "agent_version": "1.0+device-key"}
        listing = Mock(status_code=200)
        listing.json.return_value = []
        created = Mock(status_code=201)
        created.json.return_value = record
        client._session.get.return_value = listing
        client._session.post.return_value = created
        self.assertEqual(client.ensure_printer("Work-BBL-37", "device-key"), record)
        self.assertEqual(client._session.post.call_args.kwargs["json"], {
            "name": "Work-BBL-37", "action_on_failure": "NONE", "watching_enabled": True,
            "agent_name": "the_print_farm", "agent_version": "1.0+device-key",
        })
        listing.json.return_value = [record]
        self.assertEqual(client.ensure_printer("Work-BBL-37", "device-key"), record)
        client._session.post.assert_called_once()

    def test_registration_searches_every_page_without_creating_duplicate(self):
        client = self.registration_client()
        first, second = Mock(status_code=200), Mock(status_code=200)
        first.json.return_value = {"results": [{"name": "other"}], "next": "https://obico.test/api/v1/printers/?page=2"}
        record = {"id": 42, "name": "Lab-Far-End", "auth_token": "token", "agent_name": "the_print_farm", "agent_version": "1.0+device-key"}
        second.json.return_value = {"results": [record], "next": None}
        client._session.get.side_effect = [first, second]
        self.assertEqual(client.ensure_printer("Lab-Far-End", "device-key"), record)
        client._session.post.assert_not_called()

    def test_registration_rejects_ambiguous_registration_records(self):
        client = self.registration_client()
        listing = Mock(status_code=200)
        client._session.get.return_value = listing
        listing.json.return_value = [{"name": "P1", "agent_name":"the_print_farm", "agent_version":"1.0+device-key"}]*2
        with self.assertRaisesRegex(ValueError, "Duplicate registration"):
            client.ensure_printer("P1", "device-key")
        client._session.post.assert_not_called()

    def test_registration_does_not_follow_cross_origin_pagination(self):
        client = self.registration_client()
        listing = Mock(status_code=200)
        listing.json.return_value = {"results": [], "next": "https://elsewhere.test/api/v1/printers/"}
        client._session.get.return_value = listing
        with self.assertRaisesRegex(ValueError, "invalid printer-list"):
            client.ensure_printer("P1", "device-key")
        client._session.get.assert_called_once()
        client._session.post.assert_not_called()

    def test_same_name_on_another_installation_gets_an_independent_record(self):
        client = self.registration_client()
        listing = Mock(status_code=200)
        listing.json.return_value = [{"id": 1, "name": "P1S-1", "auth_token": "other-site-token",
                                     "agent_name": "the_print_farm", "agent_version": "1.0+other-site-key"}]
        client._session.get.return_value = listing
        created = Mock(status_code=201)
        created.json.return_value = {"id": 2, "name": "P1S-1", "auth_token": "own-token"}
        client._session.post.return_value = created
        self.assertEqual(client.ensure_printer("P1S-1", "own-site-key")["id"], 2)
        client._session.post.assert_called_once()

    def test_older_server_registration_handles_linking_code_without_user_steps(self):
        client = self.registration_client()
        listing, code, verified, updated = (Mock(status_code=200) for _ in range(4))
        listing.json.return_value = []
        code.json.return_value = {"code": "123456", "printer": None, "verified_at": None}
        record = {"id": 9, "name": "School P1S", "auth_token": "private-token"}
        verified.json.return_value = {"printer": record}
        updated.json.return_value = record
        client._session.get.side_effect = [listing, code, updated]
        client._session.post.return_value = Mock(status_code=405)
        ack = Mock(status_code=200)
        ack.json.return_value = {"result": "ok"}
        client._session.patch.return_value = ack
        with patch("src.obico_client.requests.post", return_value=verified) as agent_post:
            self.assertEqual(client.ensure_printer("School P1S", "device-key"), record)
        verify = agent_post.call_args
        self.assertTrue(verify.args[0].endswith('/api/v1/octo/verify/'))
        self.assertEqual(verify.kwargs["params"], {"code": "123456"})
        self.assertNotIn("cookies", verify.kwargs)
        self.assertNotIn("auth", verify.kwargs)
        client._session.post.assert_called_once()  # signed-in session is never used to verify
        applied = client._session.patch.call_args.kwargs["json"]
        self.assertEqual(applied["name"], "School P1S")
        self.assertEqual(applied["action_on_failure"], "NONE")
        self.assertEqual(applied["agent_version"], "1.0+device-key")

    def test_legacy_registration_never_relinks_or_rotates_an_existing_printer_token(self):
        client = self.registration_client()
        listing, code = Mock(status_code=200), Mock(status_code=200)
        listing.json.return_value = []
        code.json.return_value = {"code":"123456", "printer":{"id":3}, "verified_at":None}
        client._session.get.side_effect = [listing, code]
        client._session.post.return_value = Mock(status_code=405)
        with self.assertRaisesRegex(ValueError, "new unassigned"):
            client.ensure_printer("P1S", "device-key")
        client._session.post.assert_called_once()  # no verification of the bound code
        client._session.patch.assert_not_called()

    def test_nullable_obico_fields_are_supported(self):
        client = ObicoClient("http://obico.test", "user", "secret", 3)
        response = Mock(status_code=200)
        response.json.return_value = {
            "status": None,
            "pic": None,
            "current_print": None,
            "watching_enabled": True,
        }
        client._session = Mock()
        client._session.get.return_value = response

        status = client.fetch_status()

        self.assertTrue(status["connected"])
        self.assertTrue(status["watching"])
        self.assertEqual(status["state"], "")
        self.assertEqual(status["snapshot_url"], "")

    def test_missing_printer_id_is_reported(self):
        client = ObicoClient("http://obico.test", "user", "secret", 2)
        client._session = Mock()
        client._session.get.return_value = Mock(status_code=404)

        status = client.fetch_status()

        self.assertFalse(status["connected"])
        self.assertEqual(status["error"], "Obico printer ID 2 was not found")

    def test_request_failure_marks_cached_data_unavailable(self):
        client = ObicoClient("http://obico.test", "user", "secret", 3)
        client._last_data = {"connected": True, "state": "Printing"}
        client._session = Mock()
        client._session.get.side_effect = RuntimeError("network unavailable")

        status = client.fetch_status()

        self.assertFalse(status["connected"])
        self.assertEqual(status["state"], "Printing")
        self.assertIn("network unavailable", status["error"])


if __name__ == "__main__":
    unittest.main()
