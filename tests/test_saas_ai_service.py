from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base
from saas_ai.constants import PRODUCT_ARCHITECTURE_3D, PRODUCT_DEVIS_INTELLIGENT, PRODUCT_EUROBAT_CAPTURE
from saas_ai.service import SaaSAIService


class SaaSAIServiceTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        Base.metadata.create_all(bind=self.engine)
        self.service = SaaSAIService(session_factory=self.Session)

    def test_create_tenant_and_start_trial(self):
        tenant = self.service.create_tenant(company_name="Eurobat", contact_email="ops@eurobat.fr")
        payload = self.service.start_trial(tenant_id=tenant["id"], trial_days=60)

        self.assertEqual(payload["tenant"]["company_name"], "Eurobat")
        self.assertEqual(len(payload["subscriptions"]), 3)

        products = {sub["product_code"] for sub in payload["subscriptions"]}
        self.assertEqual(
            products,
            {PRODUCT_EUROBAT_CAPTURE, PRODUCT_DEVIS_INTELLIGENT, PRODUCT_ARCHITECTURE_3D},
        )
        self.assertTrue(all(sub["is_entitled"] for sub in payload["subscriptions"]))

    def test_training_requires_active_subscription(self):
        tenant = self.service.create_tenant(company_name="Eurobat")

        with self.assertRaises(PermissionError):
            self.service.request_training_job(
                tenant_id=tenant["id"],
                product_code=PRODUCT_EUROBAT_CAPTURE,
                objective="Ameliorer la qualification de leads IDF",
            )

    def test_training_flow_and_model_version_increment(self):
        tenant = self.service.create_tenant(company_name="Eurobat")
        self.service.start_trial(tenant_id=tenant["id"], product_codes=[PRODUCT_EUROBAT_CAPTURE], trial_days=60)

        queue_payload = self.service.request_training_job(
            tenant_id=tenant["id"],
            product_code=PRODUCT_EUROBAT_CAPTURE,
            objective="Optimiser le scoring chantiers",
            dataset_uri="s3://bucket/eurobat/scoring.csv",
            requested_by="celia.b@keythinkers.fr",
        )

        job_id = queue_payload["job"]["id"]
        model_before = queue_payload["model_profile"]["model_version"]
        self.assertEqual(queue_payload["job"]["status"], "queued")

        running_payload = self.service.start_training_job(job_id)
        self.assertEqual(running_payload["job"]["status"], "running")

        completed_payload = self.service.complete_training_job(job_id, metrics={"f1": 0.87})
        self.assertEqual(completed_payload["job"]["status"], "completed")
        self.assertNotEqual(completed_payload["model_profile"]["model_version"], model_before)


if __name__ == "__main__":
    unittest.main()
