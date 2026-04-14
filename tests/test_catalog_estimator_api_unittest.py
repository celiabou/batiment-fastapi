from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import sys
import tempfile
from contextlib import ExitStack
from unittest import mock
import unittest
from pathlib import Path
from types import SimpleNamespace

from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request


ROOT = Path("/Users/keythinkerscelia/PycharmProjects/PythonProject/batiment-fastapi-repo")


def _load_app_module():
    path = ROOT / "app.py"
    sys.path.insert(0, str(ROOT))
    try:
        spec = importlib.util.spec_from_file_location("batiment_fastapi_app", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


class CatalogEstimatorApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_app_module()

    def test_catalog_estimate_endpoint_success(self):
        response = self.module.catalog_estimate(
            {
                "lines": [
                    {"code": "renovation_complete", "quantity": 50},
                    {"code": "tableau_electrique"},
                    {"code": "depose_cuisine", "quantity": 1},
                ],
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.body),
            {
                "lines": [
                    {
                        "code": "renovation_complete",
                        "quantity": 50,
                        "unit": "m2",
                        "unit_price_min": 1200,
                        "unit_price_max": 2500,
                        "line_total_min": 60000,
                        "line_total_max": 125000,
                    },
                    {
                        "code": "tableau_electrique",
                        "quantity": None,
                        "unit": "forfait",
                        "unit_price_min": 800,
                        "unit_price_max": 2000,
                        "line_total_min": 800,
                        "line_total_max": 2000,
                    },
                    {
                        "code": "depose_cuisine",
                        "quantity": 1,
                        "unit": "forfait",
                        "unit_price_min": 350,
                        "unit_price_max": 1200,
                        "line_total_min": 350,
                        "line_total_max": 1200,
                    },
                ],
                "total_min_ht": 61150,
                "total_max_ht": 128200,
            },
        )

    def test_catalog_estimate_endpoint_rejects_unknown_code(self):
        response = self.module.catalog_estimate({"lines": [{"code": "tableau_elec", "quantity": 1}]})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body), {"error": "Invalid service code or quantity"})

    def test_catalog_estimate_endpoint_rejects_missing_quantity_for_m2(self):
        response = self.module.catalog_estimate({"lines": [{"code": "carrelage"}]})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body), {"error": "Invalid service code or quantity"})

    def test_devis_intelligent_accepts_uploaded_documents(self):
        class DummySession:
            def add(self, _obj):
                return None

            def commit(self):
                return None

            def refresh(self, obj):
                obj.id = 123

            def close(self):
                return None

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/devis-intelligent",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "query_string": b"",
                "scheme": "http",
                "server": ("127.0.0.1", 8081),
            }
        )
        project_photo = UploadFile(
            file=io.BytesIO(b"fake-image-bytes"),
            filename="photo.jpg",
            headers=Headers({"content-type": "image/jpeg"}),
        )
        upload_root = self.module.STATIC_DIR / "estimate"
        upload_root.mkdir(parents=True, exist_ok=True)

        with ExitStack() as stack:
            tmpdir = stack.enter_context(
                tempfile.TemporaryDirectory(dir=str(upload_root))
            )
            stack.enter_context(mock.patch.object(self.module, "_get_current_user", return_value=None))
            stack.enter_context(mock.patch.object(self.module, "_smtp_settings", return_value={}))
            stack.enter_context(mock.patch.object(self.module, "_smtp_ready", return_value=False))
            stack.enter_context(mock.patch.object(self.module, "INTERNAL_REPORT_EMAIL", ""))
            stack.enter_context(mock.patch.object(self.module, "SessionLocal", return_value=DummySession()))
            stack.enter_context(mock.patch.object(self.module, "ESTIMATE_UPLOAD_DIR", Path(tmpdir)))
            stack.enter_context(
                mock.patch.object(
                    self.module,
                    "HandoffRequest",
                    side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
                )
            )

            response = asyncio.run(
                self.module.devis_intelligent(
                    request=request,
                    project_type="bien_professionnel",
                    style="contemporain",
                    scope="rafraichissement",
                    timeline="urgent",
                    finishing_level="",
                    work_item_key="",
                    work_quantity="",
                    work_unit="",
                    city="Paris",
                    surface="50",
                    rooms="7",
                    budget="100000",
                    notes="Mur porteur plomberie sur mesure",
                    name="Client Test",
                    phone="0600000000",
                    email="client@example.com",
                    project_photos=[project_photo],
                    project_videos=[],
                    project_dpe=None,
                    project_plans=[],
                    visitor_id="",
                    visitor_landing="",
                    visitor_referrer="",
                    visitor_utm="",
                )
            )

        payload = response if isinstance(response, dict) else json.loads(response.body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["delivery"]["client_email_sent"], False)
        self.assertEqual(payload["delivery"]["internal_email_sent"], False)
        self.assertEqual(payload["quote"]["low"], 12500)
        self.assertEqual(payload["quote"]["high"], 37500)
        self.assertEqual(payload["quote"]["pricing_context"], "Catalogue Eurobat • Rénovation légère • 50 m2")

    def test_devis_intelligent_sends_pdf_attachment_to_client(self):
        class DummySession:
            def add(self, _obj):
                return None

            def commit(self):
                return None

            def refresh(self, obj):
                obj.id = 456

            def close(self):
                return None

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/devis-intelligent",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "query_string": b"",
                "scheme": "http",
                "server": ("127.0.0.1", 8081),
            }
        )
        sent_messages = []

        def fake_send_email_message(**kwargs):
            sent_messages.append(kwargs)
            return True, None

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.module, "_get_current_user", return_value=None))
            stack.enter_context(
                mock.patch.object(
                    self.module,
                    "_smtp_settings",
                    return_value={
                        "host": "smtp.example.com",
                        "port": 587,
                        "starttls": True,
                        "user": "user@example.com",
                        "password": "secret",
                        "from_email": "noreply@example.com",
                        "from_name": "Eurobat",
                    },
                )
            )
            stack.enter_context(mock.patch.object(self.module, "_smtp_ready", return_value=True))
            stack.enter_context(mock.patch.object(self.module, "INTERNAL_REPORT_EMAIL", "internal@example.com"))
            stack.enter_context(mock.patch.object(self.module, "SessionLocal", return_value=DummySession()))
            stack.enter_context(
                mock.patch.object(
                    self.module,
                    "HandoffRequest",
                    side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
                )
            )
            stack.enter_context(mock.patch.object(self.module, "_send_email_message", side_effect=fake_send_email_message))

            response = asyncio.run(
                self.module.devis_intelligent(
                    request=request,
                    project_type="appartement",
                    style="dubai",
                    scope="renovation_complete",
                    timeline="3_mois",
                    finishing_level="standard",
                    work_item_key="renovation_complete",
                    work_quantity="80",
                    work_unit="m2",
                    city="Champigny sur marne",
                    surface="80",
                    rooms="3",
                    budget="20000",
                    notes="APPRT",
                    name="LAURA CHRIS",
                    phone="0769410395",
                    email="Boudrahemcelia@gmail.com",
                    project_photos=[],
                    project_videos=[],
                    project_dpe=None,
                    project_plans=[],
                    visitor_id="",
                    visitor_landing="",
                    visitor_referrer="",
                    visitor_utm="",
                )
            )

        payload = response if isinstance(response, dict) else json.loads(response.body)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(sent_messages), 2)
        client_message = sent_messages[0]
        self.assertEqual(client_message["to_email"], "Boudrahemcelia@gmail.com")
        self.assertEqual(len(client_message["attachments"]), 1)
        attachment = client_message["attachments"][0]
        self.assertEqual(attachment["mime_type"], "application/pdf")
        self.assertTrue(str(attachment["filename"]).endswith(".pdf"))
        self.assertTrue(bytes(attachment["content"]).startswith(b"%PDF-1.4"))
        internal_message = sent_messages[1]
        self.assertFalse(internal_message.get("attachments"))


if __name__ == "__main__":
    unittest.main()
