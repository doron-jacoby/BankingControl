import unittest
from decimal import Decimal

from finance.providers import ProviderError, normalize_demo_record


class DemoNormalizationTests(unittest.TestCase):
    def payload(self) -> dict[str, object]:
        return {
            "id": "synthetic-1",
            "account_id": "synthetic-account",
            "date": "2026-09-01T12:00:00+00:00",
            "amount": "-12.340000000000000001",
            "currency": "ILS",
            "description": "Synthetic purchase",
        }

    def test_decimal_precision_and_sign_are_preserved(self) -> None:
        record = normalize_demo_record(self.payload())
        self.assertEqual(record.amount, Decimal("-12.340000000000000001"))

    def test_no_secret_payload_is_retained(self) -> None:
        payload = self.payload() | {"token": "private", "card_number": "private"}
        self.assertEqual(normalize_demo_record(payload).raw_metadata, {})

    def test_float_and_nonfinite_amounts_are_rejected(self) -> None:
        for amount in (12.3, "NaN", "Infinity", "invalid"):
            with self.subTest(amount=amount), self.assertRaises(ProviderError):
                normalize_demo_record(self.payload() | {"amount": amount})

    def test_naive_dates_and_unknown_statuses_are_rejected(self) -> None:
        for changed in ({"date": "2026-09-01"}, {"status": "unknown"}):
            with self.assertRaises(ProviderError):
                normalize_demo_record(self.payload() | changed)

    def test_pending_link_and_missing_id(self) -> None:
        record = normalize_demo_record(
            self.payload() | {"id": None, "pending_id": "old-id"}
        )
        self.assertIsNone(record.provider_transaction_id)
        self.assertEqual(record.raw_metadata, {"pending_transaction_id": "old-id"})
