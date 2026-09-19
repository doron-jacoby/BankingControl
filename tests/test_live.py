import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from test_financy import CREDS, FakeTransport, account_page, token

from finance.cli import main
from finance.exchange import ExchangeRateError
from finance.financy import FinancyClient, FinancyError
from finance.live import (
    LiveLabelRule,
    LiveTagRule,
    PDFExportError,
    _display_money,
    _general_category,
    _is_fee,
    _is_insurance,
    _is_standing_order,
    _latest_reviewable_month,
    expense_subjects,
    general_category_trend,
    load_label_rules,
    load_snapshot,
    load_tag_rules,
    recurring_merchants,
    report_html,
    resolve_label,
    resolve_rule,
    resolve_tag,
    save_label_rule,
    save_tag_rule,
    simple_report_html,
    summarize,
    sync_snapshot,
    write_report,
    write_report_pdf,
    write_simple_report,
)
from finance.security import FakeSecretStore, SecretError, load_database_key
from finance.storage import open_database

START, END = date(2026, 6, 1), date(2026, 9, 19)


def row(identity: str = "one", **updates: object) -> dict[str, object]:
    return {
        "id": identity,
        "accountId": "synthetic-account",
        "status": "BOOKED",
        "date": {"transactionDate": "2026-08-01"},
        "amount": {"chargedAmount": {"amount": Decimal("-19.90"), "currency": "ILS"}},
        "category": {"main": "FOOD_&_DRINKS", "sub": "GROCERIES"},
        "description": {"description": "PRIVATE DESCRIPTION"},
        "accountNumber": "PRIVATE ACCOUNT",
        "isDuplicate": False,
        **updates,
    }


class LiveTests(unittest.TestCase):
    def test_sender_and_zahav_type_survive_sync_without_raw_description(self) -> None:
        self.client.transaction_rows.return_value = [
            row(
                debtorName="  Example Sender 123456789 ",
                description={
                    "initialClean": 'העברת זה"ב',
                    "additionalInfo": "private data",
                },
                amount={"chargedAmount": {"amount": Decimal("100"), "currency": "ILS"}},
            )
        ]
        self.sync()
        snapshot = load_snapshot(self.path, self.store)
        record = snapshot["records"][0]
        self.assertEqual(record["sender_name"], "Example Sender …")
        self.assertEqual(record["merchant"], record["sender_name"])
        self.assertEqual(record["transfer_type"], "ZAHAV")
        self.assertNotIn("private data", json.dumps(snapshot))
        self.assertIn("Example Sender …", simple_report_html(snapshot))
        self.client.transaction_rows.return_value = [
            row(description={"initialClean": "העברת זה״ב יוצאת"})
        ]
        self.sync()
        self.assertEqual(
            load_snapshot(self.path, self.store)["records"][0]["transfer_type"], "ZAHAV"
        )
        self.client.transaction_rows.return_value = [
            row(description={"initialClean": "העברה רגילה"})
        ]
        self.sync()
        self.assertEqual(
            load_snapshot(self.path, self.store)["records"][0]["transfer_type"], ""
        )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "finance.db"
        self.enterContext(patch("finance.cli.DEFAULT_REPORT_DIR", self.path.parent))
        self.enterContext(
            patch(
                "finance.cli.write_report_pdf",
                side_effect=lambda html: html.with_suffix(".pdf"),
            )
        )
        self.store = FakeSecretStore()
        account = FinancyClient(
            CREDS, FakeTransport([token(), account_page()])
        ).list_accounts()[0]
        self.client = Mock(spec=FinancyClient)
        self.client.list_accounts.return_value = [account]
        self.client.transaction_rows.return_value = [row()]

    def sync(self) -> dict[str, object]:
        return sync_snapshot(self.client, self.path, self.store, START, END)

    def test_date_pagination_uses_no_limit_and_retains_filters(self) -> None:
        transport = FakeTransport(
            [
                token(),
                (200, {"items": [row()], "nextPage": "next+/"}),
                (200, {"items": [row("two")]}),
            ]
        )
        rows = FinancyClient(CREDS, transport).transaction_rows(START, END)
        self.assertEqual(len(rows), 2)
        for call in transport.calls[1:]:
            params = parse_qs(urlsplit(call[1]).query)
            self.assertNotIn("limit", params)
            self.assertEqual(params["dateFrom"], ["2026-06-01"])
            self.assertEqual(params["dateTo"], ["2026-09-19"])
            self.assertEqual(params["includeDuplicates"], ["0"])
        self.assertEqual(
            parse_qs(urlsplit(transport.calls[2][1]).query)["nextPage"], ["next+/"]
        )
        with self.assertRaises(FinancyError):
            FinancyClient(CREDS, transport).transaction_rows(END, START)

    def test_report_destination_defaults_to_documents_and_accepts_pdf_output(
        self,
    ) -> None:
        self.sync()
        destination = self.path.parent / "Documents" / "PersonalFinance"
        with (
            patch("finance.cli.DEFAULT_REPORT_DIR", destination),
            patch("finance.cli.MacOSKeychain", return_value=self.store),
            patch(
                "finance.cli.write_report_pdf",
                return_value=destination / "monthly-overview.pdf",
            ) as export,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            base = ["--data-dir", str(self.path.parent), "report"]
            self.assertEqual(main(base), 0)
            export.assert_called_once_with(destination / "monthly-overview.html")
            self.assertTrue((destination / "monthly-overview.html").is_file())
            custom = self.path.parent / "custom report.pdf"
            self.assertEqual(main(base + ["--output", str(custom)]), 0)
            export.assert_called_with(custom.with_suffix(".html"))
            self.assertTrue(custom.with_suffix(".html").is_file())
            export.side_effect = PDFExportError("Chrome failed")
            self.assertEqual(main(base), 1)

    def test_default_sync_fetches_twelve_full_months_and_report_is_hebrew(self) -> None:
        output = io.StringIO()
        with (
            patch("finance.cli.MacOSKeychain", return_value=self.store),
            patch("finance.cli.Credentials.load", return_value=CREDS),
            patch("finance.cli.FinancyClient", return_value=self.client),
            patch(
                "finance.cli.utc_now", return_value=datetime(2026, 9, 19, tzinfo=UTC)
            ),
            contextlib.redirect_stdout(output),
        ):
            base = ["--data-dir", str(self.path.parent)]
            self.assertEqual(main(base + ["sync"]), 0)
            self.client.transaction_rows.assert_called_once_with(
                date(2025, 9, 1), date(2026, 9, 19)
            )
            self.assertEqual(main(base + ["report"]), 0)
            self.assertEqual(main(base + ["report", "--detailed"]), 0)
        self.assertIn(
            "lang='he'", (self.path.parent / "monthly-overview.html").read_text()
        )
        self.assertIn("lang='en'", (self.path.parent / "analysis.html").read_text())

    def test_import_keeps_only_bounded_merchant_context_for_travel(self) -> None:
        self.client.transaction_rows.return_value = [
            row(
                merchantName="Train 1234567890 " + "x" * 300,
                merchantAddress={"country": "us", "streetName": "PRIVATE STREET"},
                category={"main": "TRANSPORT", "sub": "PUBLIC_TRANSPORT"},
            )
        ]
        self.sync()
        snapshot = load_snapshot(self.path, self.store)
        record = snapshot["records"][0]
        self.assertEqual(record["merchant_country"], "US")
        self.assertLessEqual(len(record["merchant"]), 160)
        self.assertNotIn("1234567890", record["merchant"])
        self.assertNotIn("PRIVATE", json.dumps(snapshot))
        august = general_category_trend(snapshot)["currencies"]["ILS"]["2026-08"]
        self.assertEqual(august["categories"]["טיולים בחו״ל"], Decimal("19.90"))

    def test_recipient_survives_resync_and_appears_in_private_reports(self) -> None:
        self.sync()
        recipient = "IBI <נמען>"
        self.client.transaction_rows.return_value = [
            row(
                creditorName="  IBI   <נמען>  ",
                creditorAccount={"iban": "PRIVATE ACCOUNT"},
                category={"main": "TRANSFER", "sub": "PRIVATE"},
            )
        ]
        info = self.sync()
        snapshot = load_snapshot(self.path, self.store)
        record = snapshot["records"][0]
        self.assertEqual(record["recipient_name"], recipient)
        self.assertEqual(record["merchant"], recipient)
        self.assertEqual(len(snapshot["records"]), 1)
        self.assertNotIn("IBI", json.dumps(info))
        self.assertNotIn(recipient.encode(), self.path.read_bytes())
        self.assertNotIn("PRIVATE ACCOUNT", json.dumps(snapshot))
        self.assertNotIn("PRIVATE DESCRIPTION", json.dumps(snapshot))
        for html in (simple_report_html(snapshot), report_html(snapshot)):
            self.assertIn("IBI &lt;נמען&gt;", html)
            self.assertNotIn(recipient, html)
        self.assertNotIn("העברה לא מזוהה", simple_report_html(snapshot))
        monthly = summarize(snapshot, "2026-08")
        self.assertEqual(monthly["reconciled"]["ILS"]["spend"], Decimal(0))
        self.assertIn(
            "unresolved_transfer", monthly["flagged_for_review"][0]["reasons"]
        )

    def test_recipient_validation_minimization_and_merchant_precedence(self) -> None:
        self.client.transaction_rows.return_value = [
            row(creditorName="נמען 123456789 " + "א" * 200, merchantName="Shop"),
            row("null", creditorName=None),
            row("empty", creditorName="  "),
            row("missing"),
        ]
        self.sync()
        snapshot = load_snapshot(self.path, self.store)
        record, *unnamed = snapshot["records"]
        self.assertEqual(record["merchant"], "Shop")
        self.assertTrue(record["recipient_name"].startswith("נמען … "))
        self.assertEqual(len(record["recipient_name"]), 160)
        self.assertTrue(all(r["recipient_name"] == "" for r in unnamed))
        invalid_names: tuple[object, ...] = (123, False, [], {})
        for invalid in invalid_names:
            self.client.transaction_rows.return_value = [row(creditorName=invalid)]
            with self.assertRaises(FinancyError):
                self.sync()
            self.assertEqual(load_snapshot(self.path, self.store), snapshot)

    def test_school_description_becomes_education_without_retaining_private_text(
        self,
    ) -> None:
        self.client.transaction_rows.return_value = [
            row(
                merchantName=None,
                description={
                    "description": "PRIVATE ACCOUNT 123456789 הכפר הירוק הוראת קבע"
                },
                category={"main": "UNCATEGORIZED", "sub": "UNCATEGORIZED"},
            )
        ]
        self.sync()
        snapshot = load_snapshot(self.path, self.store)
        self.assertEqual(snapshot["records"][0]["merchant"], "הכפר הירוק")
        self.assertNotIn("PRIVATE", json.dumps(snapshot))
        august = general_category_trend(snapshot)["currencies"]["ILS"]["2026-08"]
        self.assertEqual(august["categories"]["חינוך"], Decimal("19.90"))
        self.assertNotIn("דיור", simple_report_html(snapshot))

    def test_car_wash_merchant_is_domestic_transport_despite_restaurant_category(
        self,
    ) -> None:
        self.client.transaction_rows.return_value = [
            row(
                merchantName="תחנת החוף המנהרה",
                category={"main": "FOOD_&_DRINKS", "sub": "RESTAURANT"},
            )
        ]
        self.sync()
        snapshot = load_snapshot(self.path, self.store)
        august = general_category_trend(snapshot)["currencies"]["ILS"]["2026-08"]
        self.assertEqual(august["categories"]["תחבורה בארץ"], Decimal("19.90"))
        self.assertNotIn("מזון", august["categories"])

    def test_cli_uses_frozen_rates_after_resync_and_retains_report_on_failure(
        self,
    ) -> None:
        self.client.transaction_rows.return_value = [
            row(amount={"chargedAmount": {"amount": "-100", "currency": "USD"}})
        ]
        self.sync()
        base = ["--data-dir", str(self.path.parent), "report"]
        output = self.path.parent / "monthly-overview.html"
        with (
            patch("finance.cli.MacOSKeychain", return_value=self.store),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with patch(
                "finance.exchange.fetch_month_end_rate",
                side_effect=ExchangeRateError("Rate unavailable"),
            ):
                output.write_text("Existing report")
                self.assertEqual(main(base), 1)
                self.assertEqual(output.read_text(), "Existing report")
            with patch(
                "finance.exchange.fetch_month_end_rate",
                return_value=(
                    Decimal(3),
                    "2026-08-31",
                    "https://edge.boi.gov.il/example",
                ),
            ) as fetch:
                self.assertEqual(main(base), 0)
                fetch.assert_called_once_with("USD", "2026-08")
            original_report = output.read_bytes()
            self.sync()
            with patch(
                "finance.exchange.fetch_month_end_rate",
                side_effect=AssertionError("Do not fetch a saved rate"),
            ):
                self.assertEqual(main(base), 0)
                self.assertEqual(output.read_bytes(), original_report)
            self.assertIn("ILS 300.00", output.read_text())
            self.assertNotIn("USD 100.00", output.read_text())

    def test_snapshot_is_encrypted_minimized_and_replaced_atomically(self) -> None:
        self.assertEqual(self.sync()["transaction_count"], 1)
        self.assertNotIn(b"synthetic-account", self.path.read_bytes())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        snapshot = load_snapshot(self.path, self.store)
        self.assertNotIn("PRIVATE", json.dumps(snapshot))
        with open_database(load_database_key(self.store), self.path) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM transactions").fetchone()[0], 0
            )
        self.client.transaction_rows.return_value = [row(), row("bad", amount={})]
        with self.assertRaises(FinancyError):
            self.sync()
        self.assertEqual(load_snapshot(self.path, self.store), snapshot)
        self.client.transaction_rows.return_value = [row("new", status="PENDING")]
        self.sync()
        updated = load_snapshot(self.path, self.store)
        self.assertEqual(len(updated["records"]), 1)
        self.assertEqual(updated["records"][0]["id"], "new")
        self.store = FakeSecretStore()
        with self.assertRaises(SecretError):
            self.sync()

    def test_duplicates_date_fallback_and_conflicting_ids(self) -> None:
        self.client.transaction_rows.return_value = [
            row(),
            row(),
            row("duplicate", isDuplicate=True),
            row("outside", date={"transactionDate": "2026-05-31"}),
            row(
                "fallback", date={"transactionDate": None, "bookingDate": "2026-08-02"}
            ),
        ]
        info = self.sync()
        self.assertEqual(info["transaction_count"], 2)
        self.assertEqual(info["duplicates_excluded"], 1)
        self.assertEqual(info["repeated_ids_excluded"], 1)
        self.assertEqual(info["outside_range_excluded"], 1)
        snapshot = load_snapshot(self.path, self.store)
        self.assertEqual(summarize(snapshot, "2026-08")["date_fallback_count"], 1)
        self.client.transaction_rows.return_value = [row(), row(status="PENDING")]
        with self.assertRaises(FinancyError):
            self.sync()
        self.assertEqual(load_snapshot(self.path, self.store), snapshot)

    def test_statuses_currencies_categories_and_credits_stay_separate(self) -> None:
        self.client.transaction_rows.return_value = [
            row(),
            row("pending", status="PENDING"),
            row("unknown", status=None),
            row(
                "credit",
                amount={"chargedAmount": {"amount": Decimal("5"), "currency": "ILS"}},
            ),
            row(
                "usd",
                amount={"chargedAmount": {"amount": Decimal("-3"), "currency": "USD"}},
                changedCategory={"main": "SHOPPING", "sub": "BOOKS_&_GAMES"},
            ),
        ]
        self.sync()
        snapshot = load_snapshot(self.path, self.store)
        report = summarize(snapshot, "2026-08")
        movements = {
            (r["currency"], r["source_status"]): r for r in report["movements"]
        }
        self.assertEqual(movements["ILS", "BOOKED"]["debits"], Decimal("19.90"))
        self.assertEqual(movements["ILS", "BOOKED"]["credits"], Decimal("5"))
        self.assertEqual(movements["USD", "BOOKED"]["debits"], Decimal("3"))
        self.assertEqual(report["review_counts"]["not_booked"], 2)
        self.assertEqual(
            {r["category"] for r in report["booked_categories"]},
            {"FOOD_&_DRINKS", "SHOPPING"},
        )
        self.assertNotIn("total", report)
        self.assertTrue(report["requested_month_complete"])
        self.assertFalse(summarize(snapshot, "2026-09")["requested_month_complete"])
        self.assertFalse(report["coverage_verified"])
        with self.assertRaises(ValueError):
            summarize(snapshot, "2026-05")
        output = self.path.parent / "report.html"
        write_report(snapshot, output)
        html = output.read_text()
        self.assertIn("not reconciled spending", html)
        self.assertNotIn("PRIVATE", html)
        self.assertNotIn("synthetic-account", html)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        poisoned = deepcopy(snapshot)
        poisoned["info"]["captured_at"] = "<script>alert(1)</script>"
        self.assertNotIn("<script>", report_html(poisoned))

    def test_bank_card_separation_and_exact_decimal_totals(self) -> None:
        self.client.transaction_rows.return_value = [
            row(amount={"chargedAmount": {"amount": "-19.90", "currency": "ILS"}})
        ]
        self.sync()
        snapshot = load_snapshot(self.path, self.store)
        original = snapshot["records"][0]
        snapshot["records"] = [
            original | {"amount": "-1000000000000000000000000000000"},
            original | {"amount": "-0.000000000000000000000000000001"},
            original | {"amount": "-7", "account_type": "credit_card"},
        ]
        report = summarize(snapshot, "2026-08")
        movements = {r["account_type"]: r for r in report["movements"]}
        self.assertEqual(
            movements["checking"]["debits"],
            Decimal("1000000000000000000000000000000.000000000000000000000000000001"),
        )
        self.assertEqual(movements["credit_card"]["debits"], Decimal("7"))

    def test_missing_charged_amount_is_counted_without_inventing_a_value(self) -> None:
        self.client.transaction_rows.return_value = [
            row(),
            row(
                "missing",
                amount={
                    "chargedAmount": {"amount": "", "currency": "ILS"},
                    "originalAmount": {"amount": Decimal("-500"), "currency": "USD"},
                },
            ),
        ]
        self.assertEqual(self.sync()["missing_amount_count"], 1)
        snapshot = load_snapshot(self.path, self.store)
        report = summarize(snapshot, "2026-08")
        self.assertEqual(report["review_counts"]["missing_amount"], 1)
        self.assertEqual(report["movements"][0]["debits"], Decimal("19.90"))
        self.assertEqual(report["movements"][0]["missing_amount_count"], 1)
        self.assertIn("1 records have no charged amount", report_html(snapshot))

    def test_malformed_money_dates_accounts_and_duplicate_flags_fail_closed(
        self,
    ) -> None:
        for invalid in [
            row(amount={"chargedAmount": {"amount": True, "currency": "ILS"}}),
            row(amount={"chargedAmount": {"amount": "1_000", "currency": "ILS"}}),
            row(amount={"chargedAmount": {"amount": 1.1, "currency": "ILS"}}),
            row(
                amount={"chargedAmount": {"amount": Decimal("NaN"), "currency": "ILS"}}
            ),
            row(date={"transactionDate": "2026-02-30"}),
            row(date={}),
            row(accountId="unknown"),
            row(isDuplicate="false"),
        ]:
            self.client.transaction_rows.return_value = [invalid]
            with self.assertRaises(FinancyError):
                self.sync()
        self.assertFalse(self.path.exists())

    def test_live_cli_sync_monthly_and_report(self) -> None:
        output = io.StringIO()
        with (
            patch("finance.cli.MacOSKeychain", return_value=self.store),
            patch("finance.cli.Credentials.load", return_value=CREDS),
            patch("finance.cli.FinancyClient", return_value=self.client),
            contextlib.redirect_stdout(output),
        ):
            base = ["--data-dir", str(self.path.parent)]
            self.assertEqual(
                main(base + ["sync", "--from", str(START), "--to", str(END)]), 0
            )
            self.assertEqual(main(base + ["monthly", "2026-08"]), 0)
            self.assertEqual(main(base + ["report"]), 0)
            self.assertEqual(
                main(base + ["monthly", "2026-08", "--timezone", "UTC"]), 2
            )
        self.assertTrue((self.path.parent / "monthly-overview.html").exists())
        self.assertNotIn("PRIVATE", output.getvalue())


class LiveTagTests(unittest.TestCase):
    def test_saved_zahav_scope_and_refund_categories(self) -> None:
        self.save(tag="investment", account_id=self.account_id, transfer_type="ZAHAV")
        self.save(
            tag="expense", category="SHOPPING", amount="25", general_category="קניות"
        )
        rules = self.rules()
        base = {**self.snapshot["records"][0], "transfer_type": "ZAHAV"}
        for amount in ("100", "-100", None):
            record = {**base, "amount": amount}
            self.assertEqual(resolve_tag(record, rules), "investment")
            report = summarize({**self.snapshot, "records": [record]}, "2026-08", rules)
            self.assertEqual(report["reconciled"]["ILS"]["spend"], Decimal(0))
            self.assertEqual(report["flagged_for_review"], [])
        self.assertIsNone(resolve_tag({**base, "account_id": "another-account"}, rules))
        self.assertIsNone(resolve_tag({**base, "transfer_type": ""}, rules))
        refund = {**base, "transfer_type": "", "category": "SHOPPING", "amount": "25"}
        self.assertEqual(
            general_category_trend({**self.snapshot, "records": [refund]}, rules)[
                "currencies"
            ]["ILS"]["2026-08"]["categories"],
            {"קניות": Decimal(-25)},
        )
        self.assertIsNone(resolve_tag({**refund, "amount": "26"}, rules))
        from finance.exchange import shekel_record

        converted = shekel_record(
            {**refund, "currency": "USD"}, {("USD", "2026-08"): Decimal(3)}
        )
        matched_rule = resolve_rule(converted, rules)
        assert matched_rule is not None
        self.assertEqual(matched_rule.general_category, "קניות")
        with self.assertRaises(ValueError):
            self.save(tag="investment", category="TRANSFER", transfer_type="ZAHAV")

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "finance.db"
        self.store = FakeSecretStore()
        account = FinancyClient(
            CREDS, FakeTransport([token(), account_page()])
        ).list_accounts()[0]
        self.account_id = account.provider_account_id
        client = Mock(spec=FinancyClient)
        client.list_accounts.return_value = [account]
        client.transaction_rows.return_value = [
            row("expense"),
            row(
                "self-transfer-out",
                amount={
                    "chargedAmount": {"amount": Decimal("-500"), "currency": "ILS"}
                },
                category={"main": "TRANSFER", "sub": "OWN_ACCOUNT"},
            ),
            row(
                "gift",
                amount={"chargedAmount": {"amount": Decimal("300"), "currency": "ILS"}},
                category={"main": "TRANSFER", "sub": "PRIVATE"},
            ),
            row(
                "esop",
                amount={
                    "chargedAmount": {"amount": Decimal("4000"), "currency": "ILS"}
                },
                category={"main": "SECURITY", "sub": "SALE"},
            ),
            row(
                "unknown-credit",
                amount={"chargedAmount": {"amount": Decimal("50"), "currency": "ILS"}},
            ),
        ]
        sync_snapshot(client, self.path, self.store, START, END)
        self.snapshot = load_snapshot(self.path, self.store)

    def save(self, **kwargs: Any) -> None:
        with open_database(load_database_key(self.store), self.path) as db:
            save_tag_rule(db, LiveTagRule(**kwargs))

    def rules(self) -> list[LiveTagRule]:
        with open_database(load_database_key(self.store), self.path) as db:
            return load_tag_rules(db)

    def test_rejects_unsupported_or_underspecified_rules(self) -> None:
        with open_database(load_database_key(self.store), self.path) as db:
            with self.assertRaises(ValueError):
                save_tag_rule(db, LiveTagRule(tag="bogus", category="TRANSFER"))
            with self.assertRaises(ValueError):
                save_tag_rule(db, LiveTagRule(tag="gift"))
            with self.assertRaises(ValueError):
                save_tag_rule(db, LiveTagRule(tag="gift", record_id="one"))
            with self.assertRaises(ValueError):
                save_tag_rule(db, LiveTagRule(tag="gift", category="lowercase"))
            with self.assertRaises(ValueError):
                save_tag_rule(db, LiveTagRule(tag="gift", account_id="  "))
            with self.assertRaises(ValueError):
                save_tag_rule(
                    db,
                    LiveTagRule(tag="gift", category="TRANSFER", note="x" * 2001),
                )

    def test_reconciled_totals_apply_saved_tags_and_keep_unresolved_visible(
        self,
    ) -> None:
        self.save(
            tag="self_transfer",
            account_id=self.account_id,
            record_id="self-transfer-out",
        )
        self.save(tag="gift", account_id=self.account_id, record_id="gift")
        self.save(tag="income", account_id=self.account_id, record_id="esop")
        report = summarize(self.snapshot, "2026-08", self.rules())
        bucket = report["reconciled"]["ILS"]
        self.assertEqual(bucket["spend"], Decimal("19.90"))
        self.assertEqual(bucket["income"], Decimal("4000"))
        self.assertEqual(bucket["breakdown"]["self_transfer"]["count"], 1)
        self.assertEqual(bucket["breakdown"]["gift"]["count"], 1)
        self.assertEqual(bucket["breakdown"]["income"]["count"], 1)
        self.assertEqual(bucket["breakdown"]["unresolved_credit"]["count"], 1)
        self.assertEqual(report["review_counts"]["unresolved_credit"], 1)

    def test_category_rule_matches_every_record_in_that_category(self) -> None:
        self.save(tag="self_transfer", category="TRANSFER")
        report = summarize(self.snapshot, "2026-08", self.rules())
        bucket = report["reconciled"]["ILS"]
        self.assertEqual(bucket["breakdown"]["self_transfer"]["count"], 2)

    def test_record_rule_outranks_category_rule_by_priority(self) -> None:
        self.save(tag="self_transfer", category="TRANSFER", priority=0)
        self.save(tag="gift", account_id=self.account_id, record_id="gift", priority=10)
        record = next(r for r in self.snapshot["records"] if r["id"] == "gift")
        self.assertEqual(resolve_tag(record, self.rules()), "gift")

    def test_tags_survive_a_resync_of_the_snapshot(self) -> None:
        self.save(tag="gift", account_id=self.account_id, record_id="gift")
        client = Mock(spec=FinancyClient)
        client.list_accounts.return_value = [
            FinancyClient(
                CREDS, FakeTransport([token(), account_page()])
            ).list_accounts()[0]
        ]
        client.transaction_rows.return_value = [row("expense")]
        sync_snapshot(client, self.path, self.store, START, END)
        self.assertEqual(len(self.rules()), 1)

    def test_cli_tag_and_tags_commands_apply_to_monthly_report(self) -> None:
        base = ["--data-dir", str(self.path.parent)]

        def run(args: list[str]) -> tuple[int, Any]:
            output = io.StringIO()
            with (
                patch("finance.cli.MacOSKeychain", return_value=self.store),
                contextlib.redirect_stdout(output),
            ):
                code = main(args)
            return code, json.loads(output.getvalue())

        code, saved = run(
            base
            + [
                "tag",
                "gift",
                "--account-id",
                self.account_id,
                "--record-id",
                "gift",
                "--note",
                "From grandma",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(saved["status"], "saved")

        code, listed = run(base + ["tags"])
        self.assertEqual(code, 0)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["tag"], "gift")

        code, monthly = run(base + ["monthly", "2026-08"])
        self.assertEqual(code, 0)
        self.assertEqual(monthly["reconciled"]["ILS"]["breakdown"]["gift"]["count"], 1)

        code, invalid = run(base + ["tag", "gift", "--record-id", "gift"])
        self.assertEqual(code, 2)
        self.assertEqual(invalid["error"], "validation")

        code, _ = run(["--demo", "--data-dir", str(self.path.parent), "tags"])
        self.assertEqual(code, 2)


class LiveLabelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "finance.db"
        self.enterContext(patch("finance.cli.DEFAULT_REPORT_DIR", self.path.parent))
        self.enterContext(
            patch(
                "finance.cli.write_report_pdf",
                side_effect=lambda html: html.with_suffix(".pdf"),
            )
        )
        self.store = FakeSecretStore()
        account = FinancyClient(
            CREDS, FakeTransport([token(), account_page()])
        ).list_accounts()[0]
        self.account_id = account.provider_account_id
        client = Mock(spec=FinancyClient)
        client.list_accounts.return_value = [account]
        client.transaction_rows.return_value = [
            row(
                "hoa",
                amount={
                    "chargedAmount": {"amount": Decimal("-700"), "currency": "ILS"}
                },
                category={"main": "INCOMES_EXPENSES", "sub": "DIRECT_DEBIT"},
            ),
            row(
                "other-debit",
                amount={
                    "chargedAmount": {"amount": Decimal("-500"), "currency": "ILS"}
                },
                category={"main": "INCOMES_EXPENSES", "sub": "DIRECT_DEBIT"},
            ),
        ]
        sync_snapshot(client, self.path, self.store, START, END)
        self.snapshot = load_snapshot(self.path, self.store)

    def save(self, **kwargs: Any) -> None:
        with open_database(load_database_key(self.store), self.path) as db:
            save_label_rule(db, LiveLabelRule(**kwargs))

    def rules(self) -> list[LiveLabelRule]:
        with open_database(load_database_key(self.store), self.path) as db:
            return load_label_rules(db)

    def test_rejects_underspecified_or_invalid_rules(self) -> None:
        with open_database(load_database_key(self.store), self.path) as db:
            with self.assertRaises(ValueError):
                save_label_rule(db, LiveLabelRule(label=""))
            with self.assertRaises(ValueError):
                save_label_rule(db, LiveLabelRule(label="ועד בית"))
            with self.assertRaises(ValueError):
                save_label_rule(db, LiveLabelRule(label="ועד בית", record_id="hoa"))
            with self.assertRaises(ValueError):
                save_label_rule(
                    db, LiveLabelRule(label="ועד בית", category="lowercase")
                )
            with self.assertRaises(ValueError):
                save_label_rule(db, LiveLabelRule(label="ועד בית", account_id="  "))
            with self.assertRaises(ValueError):
                save_label_rule(db, LiveLabelRule(label="x" * 201, category="TRANSFER"))
            with self.assertRaises(ValueError):
                save_label_rule(
                    db, LiveLabelRule(label="ועד בית", amount="not-a-number")
                )

    def test_label_rule_matches_only_the_specified_amount(self) -> None:
        self.save(
            label="ועד בית",
            account_id=self.account_id,
            category="INCOMES_EXPENSES",
            subcategory="DIRECT_DEBIT",
            amount="-700",
        )
        hoa = next(r for r in self.snapshot["records"] if r["id"] == "hoa")
        other = next(r for r in self.snapshot["records"] if r["id"] == "other-debit")
        self.assertEqual(resolve_label(hoa, self.rules()), "ועד בית")
        self.assertIsNone(resolve_label(other, self.rules()))

    def test_expense_subjects_uses_label_rule_when_merchant_is_missing(self) -> None:
        self.save(
            label="ועד בית",
            account_id=self.account_id,
            category="INCOMES_EXPENSES",
            subcategory="DIRECT_DEBIT",
            amount="-700",
        )
        subjects = expense_subjects(
            self.snapshot["records"], [], lambda r: True, self.rules()
        )
        by_subject = {item["subject"]: item for item in subjects}
        self.assertIn("ועד בית", by_subject)
        self.assertEqual(by_subject["ועד בית"]["total"], Decimal(700))
        self.assertIn("ללא שם · INCOMES_EXPENSES / DIRECT_DEBIT", by_subject)

    def test_cli_label_and_labels_commands_apply_to_report(self) -> None:
        base = ["--data-dir", str(self.path.parent)]

        def run(args: list[str]) -> tuple[int, Any]:
            output = io.StringIO()
            with (
                patch("finance.cli.MacOSKeychain", return_value=self.store),
                contextlib.redirect_stdout(output),
            ):
                code = main(args)
            return code, json.loads(output.getvalue())

        code, saved = run(
            base
            + [
                "label",
                "ועד בית",
                "--account-id",
                self.account_id,
                "--category",
                "INCOMES_EXPENSES",
                "--subcategory",
                "DIRECT_DEBIT",
                "--amount",
                "-700",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(saved["status"], "saved")

        code, listed = run(base + ["labels"])
        self.assertEqual(code, 0)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["label"], "ועד בית")

        code, invalid = run(base + ["label", "x", "--record-id", "hoa"])
        self.assertEqual(code, 2)
        self.assertEqual(invalid["error"], "validation")

        code, report = run(base + ["report"])
        self.assertEqual(code, 0)
        html = Path(report["report"]).read_text()
        self.assertIn("ועד בית", html)
        self.assertIn("ללא שם · INCOMES_EXPENSES / DIRECT_DEBIT", html)


class LiveFlaggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "finance.db"
        self.store = FakeSecretStore()
        account = FinancyClient(
            CREDS, FakeTransport([token(), account_page()])
        ).list_accounts()[0]
        client = Mock(spec=FinancyClient)
        client.list_accounts.return_value = [account]
        client.transaction_rows.return_value = [row()]
        sync_snapshot(client, self.path, self.store, START, END)
        self.snapshot = load_snapshot(self.path, self.store)
        self.base_record = self.snapshot["records"][0]

    def record(self, identity: str, **updates: object) -> dict[str, object]:
        return {**self.base_record, "id": identity, **updates}

    def test_fee_like_label_is_flagged_but_ordinary_spend_is_not(self) -> None:
        self.snapshot["records"] = [
            self.record(
                "fee", category="BANK_FEES", subcategory="MONTHLY_FEE", amount="-25"
            ),
            self.record("groceries", amount="-100"),
        ]
        report = summarize(self.snapshot, "2026-08")
        flagged = {r["id"]: r for r in report["flagged_for_review"]}
        self.assertEqual(flagged["fee"]["reasons"], ["fee_like_label"])
        self.assertNotIn("groceries", flagged)

    def test_outlier_amount_in_category_is_flagged(self) -> None:
        self.snapshot["records"] = [
            self.record("a", amount="-50"),
            self.record("b", amount="-55"),
            self.record("c", amount="-48"),
            self.record("big", amount="-900"),
        ]
        report = summarize(self.snapshot, "2026-08")
        flagged = {r["id"]: r for r in report["flagged_for_review"]}
        self.assertEqual(flagged["big"]["reasons"], ["unusual_amount_for_category"])
        self.assertNotIn("a", flagged)
        self.assertNotIn("b", flagged)
        self.assertNotIn("c", flagged)

    def test_expense_tag_does_not_acknowledge_a_fee(self) -> None:
        self.snapshot["records"] = [
            self.record(
                "fee", category="BANK_FEES", subcategory="MONTHLY_FEE", amount="-25"
            )
        ]
        rule = LiveTagRule(
            tag="expense",
            account_id=self.base_record["account_id"],
            record_id="fee",
        )
        report = summarize(self.snapshot, "2026-08", [rule])
        self.assertEqual(report["flagged_for_review"][0]["reasons"], ["fee_like_label"])

    def test_credits_and_non_booked_debits_remain_visible(self) -> None:
        self.snapshot["records"] = [
            self.record("credit-fee", category="BANK_FEES", amount="25"),
            self.record(
                "pending-fee", category="BANK_FEES", amount="-25", status="PENDING"
            ),
        ]
        report = summarize(self.snapshot, "2026-08")
        reasons = {r["id"]: r["reasons"] for r in report["flagged_for_review"]}
        self.assertIn("unresolved_credit", reasons["credit-fee"])
        self.assertIn("not_booked", reasons["pending-fee"])

    def test_report_html_shows_flagged_section(self) -> None:
        ordinary_html = report_html(self.snapshot)
        self.assertIn("Flagged for review", ordinary_html)
        self.assertIn("Nothing flagged this month.", ordinary_html)
        self.snapshot["records"] = [
            self.record(
                "fee", category="BANK_FEES", subcategory="MONTHLY_FEE", amount="-25"
            )
        ]
        html = report_html(self.snapshot)
        self.assertIn("fee_like_label", html)


class LiveTrendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "finance.db"
        self.enterContext(patch("finance.cli.DEFAULT_REPORT_DIR", self.path.parent))
        self.enterContext(
            patch(
                "finance.cli.write_report_pdf",
                side_effect=lambda html: html.with_suffix(".pdf"),
            )
        )
        self.store = FakeSecretStore()
        account = FinancyClient(
            CREDS, FakeTransport([token(), account_page()])
        ).list_accounts()[0]
        self.account_id = account.provider_account_id
        client = Mock(spec=FinancyClient)
        client.list_accounts.return_value = [account]
        client.transaction_rows.return_value = [
            row(
                "aug-food",
                date={"transactionDate": "2026-08-03"},
                amount={
                    "chargedAmount": {"amount": Decimal("-120"), "currency": "ILS"}
                },
                category={"main": "FOOD_&_DRINKS", "sub": "GROCERIES"},
            ),
            row(
                "aug-transfer",
                date={"transactionDate": "2026-08-05"},
                amount={
                    "chargedAmount": {"amount": Decimal("-500"), "currency": "ILS"}
                },
                category={"main": "TRANSFER", "sub": "OWN_ACCOUNT"},
            ),
            row(
                "aug-fee",
                date={"transactionDate": "2026-08-12"},
                amount={"chargedAmount": {"amount": Decimal("-15"), "currency": "ILS"}},
                category={"main": "BANK_FEES", "sub": "MONTHLY_FEE"},
            ),
            row(
                "jul-fuel",
                date={"transactionDate": "2026-07-10"},
                amount={
                    "chargedAmount": {"amount": Decimal("-300"), "currency": "ILS"}
                },
                category={"main": "TRANSPORT", "sub": "FUEL"},
            ),
        ]
        sync_snapshot(
            client, self.path, self.store, date(2026, 6, 1), date(2026, 9, 19)
        )
        self.snapshot = load_snapshot(self.path, self.store)

    def test_trend_shows_twelve_complete_months_and_marks_missing_history(self) -> None:
        trend = general_category_trend(self.snapshot)
        self.assertEqual(len(trend["months"]), 12)
        self.assertEqual(trend["months"][0], "2025-09")
        self.assertEqual(trend["months"][-1], "2026-08")
        self.assertEqual(trend["coverage"]["2025-09"], "missing")
        self.assertEqual(trend["coverage"]["2026-08"], "full")

    def test_trend_groups_by_general_category(self) -> None:
        trend = general_category_trend(self.snapshot)
        august = trend["currencies"]["ILS"]["2026-08"]
        self.assertEqual(august["categories"]["מזון"], Decimal("120"))
        # Transfers await review; only the fee contributes to bank fees.
        self.assertEqual(august["categories"]["עמלות בנק"], Decimal("15"))
        self.assertEqual(august["total"], Decimal("135"))
        july = trend["currencies"]["ILS"]["2026-07"]
        self.assertEqual(july["categories"]["תחבורה בארץ"], Decimal("300"))

    def test_self_transfer_tag_excludes_it_from_the_trend(self) -> None:
        rule = LiveTagRule(
            tag="self_transfer", account_id=self.account_id, record_id="aug-transfer"
        )
        trend = general_category_trend(self.snapshot, [rule])
        august = trend["currencies"]["ILS"]["2026-08"]
        self.assertEqual(august["categories"]["עמלות בנק"], Decimal("15"))
        self.assertEqual(august["total"], Decimal("135"))

    def test_simple_report_is_hebrew_and_flags_the_previous_month(self) -> None:
        html = simple_report_html(self.snapshot)
        self.assertIn("דוח הוצאות חודשי", html)
        self.assertIn("אוגוסט 2026", html)
        self.assertIn("יולי 2026", html)
        self.assertIn("נראה כמו עמלה או חיוב", html)
        self.assertNotIn("PRIVATE", html)
        self.assertIn("פרטי זיהוי", html)
        self.assertIn("aug-fee", html)

    def test_write_simple_report_is_private_and_atomic(self) -> None:
        output = self.path.parent / "overview.html"
        write_simple_report(self.snapshot, output)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertIn("דוח הוצאות חודשי", output.read_text())

    def test_cli_report_simple_writes_hebrew_overview(self) -> None:
        output = io.StringIO()
        with (
            patch("finance.cli.MacOSKeychain", return_value=self.store),
            contextlib.redirect_stdout(output),
        ):
            code = main(["--data-dir", str(self.path.parent), "report", "--simple"])
        self.assertEqual(code, 0)
        overview = self.path.parent / "monthly-overview.html"
        self.assertTrue(overview.exists())
        self.assertIn("דוח הוצאות חודשי", overview.read_text())
        self.assertNotIn("PRIVATE", output.getvalue())


class ReportRegressionTests(unittest.TestCase):
    def test_pdf_export_is_private_and_preserves_previous_pdf_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            html = Path(directory) / "report with spaces.html"
            html.write_text("<html lang='he'>דוח</html>")
            output = html.with_suffix(".pdf")

            def render(command: list[str], **kwargs: Any) -> None:
                self.assertIn(html.resolve().as_uri(), command)
                target = next(
                    arg.removeprefix("--print-to-pdf=")
                    for arg in command
                    if arg.startswith("--print-to-pdf=")
                )
                Path(target).write_bytes(b"%PDF-1.7\nsynthetic\n%%EOF\n")

            with (
                patch("finance.live.Path.is_file", return_value=True),
                patch("finance.live.subprocess.run", side_effect=render),
            ):
                self.assertEqual(write_report_pdf(html), output)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            before = output.read_bytes()

            def render_without_exiting(command: list[str], **kwargs: Any) -> None:
                render(command, **kwargs)
                raise subprocess.TimeoutExpired(command, 15)

            with (
                patch("finance.live.Path.is_file", return_value=True),
                patch(
                    "finance.live.subprocess.run", side_effect=render_without_exiting
                ),
            ):
                self.assertEqual(write_report_pdf(html), output)
            self.assertEqual(output.read_bytes(), before)
            with (
                patch("finance.live.Path.is_file", return_value=True),
                patch(
                    "finance.live.subprocess.run",
                    side_effect=OSError("Private browser details"),
                ),
                self.assertRaisesRegex(PDFExportError, "previous PDF was preserved"),
            ):
                write_report_pdf(html)
            self.assertEqual(output.read_bytes(), before)
            self.assertEqual(list(Path(directory).glob(".finance-pdf-*")), [])

    def record(self, identity: str = "one", **updates: Any) -> dict[str, Any]:
        return (
            dict(
                id=identity,
                account_id="account-1",
                account_type="checking",
                day="2026-08-10",
                date_source="transactionDate",
                amount="-100",
                currency="ILS",
                status="BOOKED",
                category="FOOD",
                subcategory="GROCERIES",
                merchant="",
                merchant_country="",
            )
            | updates
        )

    def snapshot(self, records: list[dict[str, Any]], **info: str) -> dict[str, Any]:
        return {
            "records": records,
            "info": dict(
                date_from="2025-09-01", date_to="2026-09-19", captured_at="2026-09-19"
            )
            | info,
        }

    def test_review_amount_count_and_monthly_average(self) -> None:
        snapshot = self.snapshot(
            [
                self.record("transfer", category="TRANSFER", amount="-1000"),
                self.record("credit", amount="500"),
                self.record("pending", status="PENDING", amount="-250"),
                self.record("missing", amount=None),
                self.record(
                    "foreign", category="TRANSFER", amount="-100", currency="USD"
                ),
                self.record(
                    "settlement", subcategory="CREDIT_CARD_CHECKING", amount="-9000"
                ),
                self.record("investment", account_type="investment", amount="-8000"),
                self.record("known", category="TRANSFER", amount="-7000"),
                self.record(
                    "july", day="2026-07-20", category="TRANSFER", amount="-250"
                ),
            ],
            date_from="2026-07-15",
        )
        rules = [
            LiveTagRule(tag="self_transfer", account_id="account-1", record_id="known")
        ]
        html = simple_report_html(snapshot, rules, {("USD", "2026-08"): Decimal(3)})
        august = html.split("<td>אוגוסט 2026</td>")[1].split("</tr>")[0]
        self.assertTrue(
            august.endswith(
                "<td><span dir='ltr' title='ILS 2,050.00'>2.0</span> (5)</td>"
            )
        )
        average = html.split("<strong>ממוצע חודשי</strong>")[1].split("</tr>")[0]
        self.assertTrue(
            average.endswith(
                "<td><span dir='ltr' title='ILS 1,150.00'>1.2</span> (3.0)</td>"
            )
        )
        self.assertIn("title='ILS 4,500.00'", average)
        self.assertNotIn("ממוצע חודשי</strong>", simple_report_html(self.snapshot([])))

    def test_card_expense_counts_unless_explicitly_excluded(self) -> None:
        snapshot = self.snapshot(
            [
                self.record("purchase", account_type="credit_card"),
                self.record(
                    "settlement",
                    category="INCOMES_EXPENSES",
                    subcategory="CREDIT_CARD_CHECKING",
                ),
                self.record(
                    "transfer", category="TRANSFER", subcategory="BANK_TRANSFER"
                ),
                self.record("investment", category="TRADING", subcategory="SECURITIES"),
            ]
        )
        self.assertEqual(
            general_category_trend(snapshot)["currencies"]["ILS"]["2026-08"]["total"],
            Decimal(200),
        )
        summary = summarize(snapshot, "2026-08")
        self.assertEqual(summary["reconciled"]["ILS"]["spend"], Decimal(200))
        self.assertEqual(
            {r["id"] for r in summary["flagged_for_review"]},
            {"transfer"},
        )
        # Having card purchases (even with the same amount) does not establish
        # that this settlement is covered. Only an explicit exclusion does.
        exclusion = LiveTagRule(
            tag="self_transfer", account_id="account-1", record_id="settlement"
        )
        self.assertEqual(
            summarize(snapshot, "2026-08", [exclusion])["reconciled"]["ILS"]["spend"],
            Decimal(100),
        )
        snapshot["records"] = snapshot["records"][1:2]
        summary = summarize(snapshot, "2026-08")
        self.assertEqual(summary["reconciled"]["ILS"]["spend"], Decimal(100))
        self.assertEqual(summary["flagged_for_review"], [])
        override = LiveTagRule(
            tag="expense", account_id="account-1", record_id="settlement"
        )
        self.assertEqual(
            summarize(snapshot, "2026-08", [override])["reconciled"]["ILS"]["spend"],
            Decimal(100),
        )

    def test_fx_and_card_fees_are_bank_fees_and_investment_account_is_not_reviewed(
        self,
    ) -> None:
        snapshot = self.snapshot(
            [
                self.record(
                    "fx-fee",
                    currency="USD",
                    amount="-20.25",
                    category="TRADING",
                    subcategory="FOREIGN_EXCHANGE",
                ),
                self.record(
                    "conversion",
                    amount="-2054.02",
                    category="TRADING",
                    subcategory="FOREIGN_EXCHANGE",
                ),
                self.record(
                    "card-fee",
                    account_type="credit_card",
                    amount=None,
                    category="FINANCE",
                    subcategory="FEES",
                ),
                self.record(
                    "pending-investment",
                    account_type="investment",
                    amount="20005.24",
                    status="UNKNOWN",
                    category="UNCATEGORIZED",
                    subcategory="UNCATEGORIZED",
                ),
            ]
        )
        trend = general_category_trend(snapshot)["currencies"]
        self.assertEqual(
            trend["USD"]["2026-08"]["categories"], {"עמלות בנק": Decimal("20.25")}
        )
        self.assertEqual(trend["ILS"]["2026-08"]["total"], Decimal(0))
        flagged = {
            r["id"] for r in summarize(snapshot, "2026-08")["flagged_for_review"]
        }
        # The conversion is a known non-expense; the small FX fee still counts.
        self.assertEqual(flagged, set())

    def test_explicit_refund_reduces_spending_in_both_reports(self) -> None:
        snapshot = self.snapshot(
            [self.record(), self.record("refund", amount="25", category="RETURN")]
        )
        rule = LiveTagRule(tag="expense", account_id="account-1", record_id="refund")
        self.assertEqual(
            general_category_trend(snapshot, [rule])["currencies"]["ILS"]["2026-08"][
                "total"
            ],
            Decimal(75),
        )
        self.assertEqual(
            summarize(snapshot, "2026-08", [rule])["reconciled"]["ILS"]["spend"],
            Decimal(75),
        )
        self.assertEqual(
            summarize(snapshot, "2026-08")["reconciled"]["ILS"]["spend"], Decimal(100)
        )

    def test_unitemized_card_expenses_are_included_throughout_history(self) -> None:
        months = [f"2025-{m:02}" for m in range(9, 13)] + [
            f"2026-{m:02}" for m in range(1, 9)
        ]
        records = []
        for month in months:
            for identity, updates in (
                ("unitemized", {"amount": "-25000.50"}),
                ("pending", {"status": "PENDING"}),
                ("unknown", {"status": "UNKNOWN"}),
                ("missing", {"amount": None}),
                ("credit", {"amount": "100"}),
            ):
                records.append(
                    self.record(
                        month + identity,
                        day=month + "-10",
                        subcategory="CREDIT_CARD_CHECKING",
                        **updates,
                    )
                )
        snapshot = self.snapshot(records)
        trend = general_category_trend(snapshot)
        for month in months:
            with self.subTest(month=month):
                bucket = trend["currencies"]["ILS"][month]
                self.assertEqual(bucket["total"], Decimal("25000.50"))
                self.assertEqual(
                    bucket["categories"], {"אשראי ללא פירוט": Decimal("25000.50")}
                )
                summary = summarize(snapshot, month)["reconciled"]["ILS"]
                self.assertEqual(summary["spend"], bucket["total"])
                self.assertEqual(summary["excluded_count"], 4)
                self.assertEqual(summary["included_count"], 1)
        html = simple_report_html(snapshot)
        self.assertIn("<th>אשראי ללא פירוט</th>", html)
        self.assertEqual(html.count("title='ILS 25,000.50'"), 26)
        # Pending/missing debits need review; the known settlement credit does not.
        self.assertEqual(html.count("</span> (3)</td></tr>"), 12)

    def test_confirmed_movements_and_duplicate_settlements_are_excluded(self) -> None:
        snapshot = self.snapshot(
            [
                self.record("purchase"),
                self.record("settlement", subcategory="CREDIT_CARD_CHECKING"),
                self.record("esop", amount="500", category="TRANSFER"),
                self.record("sale", amount="200", category="TRADING"),
            ]
        )
        rules = [
            LiveTagRule(tag=tag, account_id="account-1", record_id=identity)
            for identity, tag in (
                ("settlement", "self_transfer"),
                ("esop", "income"),
                ("sale", "self_transfer"),
            )
        ]
        before = summarize(snapshot, "2026-08")
        after = summarize(snapshot, "2026-08", rules)
        self.assertEqual(after["flagged_for_review"], [])
        self.assertEqual(before["reconciled"]["ILS"]["spend"], Decimal(200))
        self.assertEqual(after["reconciled"]["ILS"]["spend"], Decimal(100))
        self.assertEqual(after["reconciled"]["ILS"]["income"], Decimal(500))
        row = (
            simple_report_html(snapshot, rules)
            .split("<td>אוגוסט 2026</td>")[1]
            .split("</tr>")[0]
        )
        self.assertTrue(
            row.endswith("<td><span dir='ltr' title='ILS 0.00'>0.0</span> (0)</td>")
        )
        # An explicitly identified non-expense does not need classification again.
        snapshot["records"][3]["status"] = "UNKNOWN"
        row = (
            simple_report_html(snapshot, rules)
            .split("<td>אוגוסט 2026</td>")[1]
            .split("</tr>")[0]
        )
        self.assertTrue(
            row.endswith("<td><span dir='ltr' title='ILS 0.00'>0.0</span> (0)</td>")
        )

    def test_known_exclusions_do_not_inflate_review(self) -> None:
        records = [
            self.record("bank-in", category="TRANSFER", amount="217112"),
            self.record(
                "bit-in", category="TRANSFER", subcategory="BIT_PAYBOX", amount="1078"
            ),
            self.record(
                "pending-in", category="TRANSFER", status="PENDING", amount="80000"
            ),
            self.record("investment", category="TRADING", amount="-50000"),
            self.record("missing-investment", category="TRADING", amount=None),
            self.record(
                "card-credit", subcategory="CREDIT_CARD_CHECKING", amount="2521.61"
            ),
            self.record("known-transfer", status="UNKNOWN", amount="-90000"),
            self.record("known-income", status="PENDING", amount="70000"),
            self.record("known-gift", amount=None),
            self.record(
                "card-debit", subcategory="CREDIT_CARD_CHECKING", amount="-100"
            ),
            self.record("unknown-credit", amount="25"),
            self.record("outgoing", category="TRANSFER", amount="-75"),
            self.record("unidentified-incoming", category="TRANSFER", amount="1000"),
            self.record("missing", amount=None),
        ]
        rules = [
            LiveTagRule(tag=tag, account_id="account-1", record_id=identity)
            for identity, tag in (
                ("known-transfer", "self_transfer"),
                ("known-income", "income"),
                ("known-gift", "gift"),
                ("bank-in", "self_transfer"),
                ("bit-in", "gift"),
                ("pending-in", "self_transfer"),
            )
        ]
        snapshot = self.snapshot(records)
        summary = summarize(snapshot, "2026-08", rules)
        self.assertEqual(summary["reconciled"]["ILS"]["spend"], Decimal(100))
        self.assertEqual(summary["reconciled"]["ILS"]["income"], Decimal(0))
        self.assertEqual(
            summary["reconciled"]["ILS"]["breakdown"]["unresolved_transfer"]["count"], 2
        )
        self.assertEqual(
            {r["id"] for r in summary["flagged_for_review"]},
            {"unknown-credit", "outgoing", "unidentified-incoming", "missing"},
        )
        html = simple_report_html(snapshot, rules)
        for label, count in (
            ("אוגוסט 2026", "4"),
            ("<strong>ממוצע חודשי</strong>", "4.0"),
        ):
            row = html.split(f"<td>{label}</td>")[1].split("</tr>")[0]
            self.assertTrue(
                row.endswith(f"title='ILS 1,100.00'>1.1</span> ({count})</td>")
            )
        # An explicit expense/refund decision still takes priority when booked.
        snapshot = self.snapshot([records[1], records[3], records[5]])
        override = [LiveTagRule(tag="expense", account_id="account-1")]
        self.assertEqual(
            summarize(snapshot, "2026-08", override)["reconciled"]["ILS"]["spend"],
            Decimal("46400.39"),
        )

    def test_insurance_increase_unknown_and_missing_amount_are_reviewed(self) -> None:
        snapshot = self.snapshot(
            [
                self.record(
                    "june",
                    day="2026-06-01",
                    category="INSURANCE",
                    subcategory="INSURANCE",
                ),
                self.record(
                    "july",
                    day="2026-07-01",
                    category="INSURANCE",
                    subcategory="INSURANCE",
                ),
                self.record(
                    "insurance",
                    amount="-1000",
                    category="INSURANCE",
                    subcategory="INSURANCE",
                ),
                self.record(
                    "unknown", category="UNCATEGORIZED", subcategory="UNCATEGORIZED"
                ),
                self.record("missing", amount=None),
                self.record("pending", status="PENDING"),
                self.record(
                    "new-insurance",
                    account_id="account-2",
                    category="INSURANCE",
                    subcategory="INSURANCE",
                ),
            ]
        )
        flags = {
            r["id"]: r["reasons"]
            for r in summarize(snapshot, "2026-08")["flagged_for_review"]
        }
        self.assertIn("historical_increase", flags["insurance"])
        self.assertIn("insurance_review", flags["new-insurance"])
        self.assertIn("unclear_charge", flags["unknown"])
        self.assertIn("missing_amount", flags["missing"])
        self.assertIn("not_booked", flags["pending"])
        self.assertNotIn("june", flags)
        self.assertIn("עלייה לעומת", simple_report_html(snapshot))
        # Classification rules do not hide future increases.
        rule = LiveTagRule(tag="expense", category="INSURANCE")
        self.assertIn(
            "insurance",
            {
                r["id"]
                for r in summarize(snapshot, "2026-08", [rule])["flagged_for_review"]
            },
        )

    def test_history_does_not_compare_different_accounts_currencies_or_merchants(
        self,
    ) -> None:
        snapshot = self.snapshot(
            [
                self.record("past1", day="2026-06-01", merchant="A"),
                self.record("past2", day="2026-07-01", merchant="A"),
                self.record("different-merchant", merchant="B", amount="-1000"),
                self.record(
                    "different-currency", merchant="A", currency="USD", amount="-1000"
                ),
                self.record(
                    "different-account",
                    merchant="A",
                    account_id="account-2",
                    amount="-1000",
                ),
            ]
        )
        self.assertEqual(summarize(snapshot, "2026-08")["flagged_for_review"], [])

    def test_category_boundaries_travel_and_subscription_subject(self) -> None:
        cases = [
            ("HEALTH", "HEALTHCARE", "", "בריאות וביטוח"),
            ("HOME", "RENT", "", "אחר"),
            ("TRANSPORT", "FLIGHTS", "", "טיולים בחו״ל"),
            ("TRANSPORT", "PUBLIC_TRANSPORT", "US", "טיולים בחו״ל"),
            ("TRANSPORT", "CAR_&_FUEL", "IL", "תחבורה בארץ"),
            ("SUBSCRIPTIONS", "INTERNET", "", "אחר"),
            ("SUBSCRIPTIONS", "FITNESS", "", "פנאי"),
            ("HEALTHCARE", "SUBSCRIPTION", "", "בריאות וביטוח"),
            ("SUBSCRIPTIONS", "UNKNOWN", "", "לא מזוהה"),
            ("OTHER", "BARGAIN", "", "אחר"),
            ("OTHER", "OTHER", "", "לא מזוהה"),
            ("UNCATEGORIZED", "UNCATEGORIZED", "", "לא מזוהה"),
            ("UNCATEGORIZED", "FLIGHTS", "", "טיולים בחו״ל"),
        ]
        for category, subcategory, country, expected in cases:
            with self.subTest(
                category=category, subcategory=subcategory, country=country
            ):
                self.assertEqual(
                    _general_category(category, subcategory, country), expected
                )

    def test_bold_non_travel_total_includes_unidentified_and_precedes_travel(
        self,
    ) -> None:
        for currency, displayed_total, displayed_travel in [
            ("ILS", "1.6", "0.5"),
            ("USD", "1,600", "500"),
            ("EUR", "1,600", "500"),
        ]:
            with self.subTest(currency=currency):
                snapshot = self.snapshot(
                    [
                        self.record("food", amount="-1000", currency=currency),
                        self.record(
                            "education",
                            amount="-200",
                            currency=currency,
                            category="OTHER",
                            subcategory="EDUCATION",
                        ),
                        self.record(
                            "unknown",
                            amount="-400",
                            currency=currency,
                            category="UNCATEGORIZED",
                            subcategory="UNCATEGORIZED",
                        ),
                        self.record(
                            "flight",
                            amount="-600",
                            currency=currency,
                            category="TRANSPORT",
                            subcategory="FLIGHTS",
                        ),
                        self.record(
                            "flight-refund",
                            amount="100",
                            currency=currency,
                            category="TRANSPORT",
                            subcategory="FLIGHTS",
                        ),
                        self.record(
                            "settlement",
                            amount="-9000",
                            currency=currency,
                            category="INCOMES_EXPENSES",
                            subcategory="CREDIT_CARD_CHECKING",
                        ),
                    ]
                )
                rules = [
                    LiveTagRule(
                        tag="expense", account_id="account-1", record_id="flight-refund"
                    ),
                    LiveTagRule(
                        tag="self_transfer",
                        account_id="account-1",
                        record_id="settlement",
                    ),
                ]
                month = general_category_trend(snapshot, rules)["currencies"][currency][
                    "2026-08"
                ]
                self.assertEqual(month["total"], Decimal(2100))
                self.assertEqual(month["total_excluding_travel"], Decimal(1600))
                self.assertEqual(month["categories"]["חינוך"], Decimal(200))
                self.assertEqual(month["categories"]["לא מזוהה"], Decimal(400))
                self.assertEqual(month["categories"]["טיולים בחו״ל"], Decimal(500))
                rates = {(currency, "2026-08"): Decimal(1)} if currency != "ILS" else {}
                html = simple_report_html(snapshot, rules, rates)
                displayed_total, displayed_travel = "1.6", "0.5"
                headers = [
                    "<th>אחר</th>",
                    "<th>לא מזוהה</th>",
                    "<th><strong>סה״כ ללא טיולים בחו״ל</strong></th>",
                    "<th>טיולים בחו״ל</th>",
                ]
                positions = [html.index(header) for header in headers]
                self.assertEqual(positions, sorted(positions))
                self.assertIn(
                    f"<td><strong><span dir='ltr' title='ILS 1,600.00'>{displayed_total}</span></strong></td>"
                    f"<td><span dir='ltr' title='ILS 500.00'>{displayed_travel}</span></td>",
                    html,
                )

    def test_month_end_leap_year_and_partial_coverage(self) -> None:
        for end, expected in [
            ("2026-08-31", "2026-08"),
            ("2026-09-19", "2026-08"),
            ("2024-02-29", "2024-02"),
            ("2026-01-01", "2025-12"),
        ]:
            self.assertEqual(_latest_reviewable_month({"date_to": end}), expected)
        snapshot = self.snapshot(
            [self.record()], date_from="2026-08-15", date_to="2026-08-31"
        )
        trend = general_category_trend(snapshot)
        self.assertEqual(trend["coverage"]["2026-08"], "partial")
        self.assertEqual(trend["coverage"]["2026-07"], "missing")
        self.assertIn("הבדיקה לחודש הזה חלקית", simple_report_html(snapshot))
        snapshot = self.snapshot([], date_from="2026-09-01")
        html = simple_report_html(snapshot)
        self.assertIn("אי אפשר לבדוק חיובים", html)
        self.assertNotIn("לא נמצאו חיובים", html)

    def test_currency_formatting_preserves_required_trailing_zero(self) -> None:
        for amount, currency, expected in [
            ("12000", "ILS", "12.0"),
            ("12550", "ILS", "12.6"),
            ("0", "ILS", "0.0"),
            ("1234.6", "USD", "1,235"),
            ("1234.4", "EUR", "1,234"),
        ]:
            self.assertEqual(_display_money(Decimal(amount), currency), expected)
        html = simple_report_html(
            self.snapshot(
                [
                    self.record(amount="-12000"),
                    self.record(
                        "fee", category="FINANCE", subcategory="FEES", amount="-25"
                    ),
                ]
            )
        )
        self.assertIn("אלפי ₪", html)
        self.assertIn(">12.0</span>", html)
        self.assertIn("title='ILS 25.00'>0.0</span>", html)

    def test_private_report_escapes_merchant_and_record_identifiers(self) -> None:
        html = simple_report_html(
            self.snapshot(
                [
                    self.record(
                        "<script>alert(1)</script>",
                        merchant="<img src=x onerror=alert(1)>",
                        category="INSURANCE",
                    )
                ]
            )
        )
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)
        self.assertIn("פרטי זיהוי", html)

    def test_one_shekel_table_combines_foreign_categories_with_month_end_rates(
        self,
    ) -> None:
        snapshot = self.snapshot(
            [
                self.record("ils", amount="-1000"),
                self.record("usd", amount="-100", currency="USD"),
                self.record("eur", amount="-50", currency="EUR"),
                self.record(
                    "flight",
                    amount="-200",
                    currency="USD",
                    category="TRANSPORT",
                    subcategory="FLIGHTS",
                ),
                self.record(
                    "school",
                    amount="-25",
                    currency="USD",
                    category="UNCATEGORIZED",
                    subcategory="UNCATEGORIZED",
                    merchant="הכפר הירוק",
                ),
                self.record(
                    "unknown",
                    amount="-10",
                    currency="USD",
                    category="UNCATEGORIZED",
                    subcategory="UNCATEGORIZED",
                ),
                self.record("refund", amount="20", currency="USD"),
            ]
        )
        rules = [LiveTagRule(tag="expense", account_id="account-1", record_id="refund")]
        rates = {("USD", "2026-08"): Decimal(3), ("EUR", "2026-08"): Decimal(4)}
        before = deepcopy(snapshot)
        html = simple_report_html(snapshot, rules, rates)
        self.assertEqual(snapshot, before)
        self.assertEqual(html.count("<h3>אלפי ₪"), 1)
        self.assertNotIn("<h3>דולר", html)
        self.assertNotIn("<h3>אירו", html)
        self.assertIn("title='ILS 1,545.00'>1.5</span></strong>", html)
        self.assertIn("title='ILS 600.00'>0.6</span>", html)
        self.assertIn("title='ILS 75.00'>0.1</span>", html)
        with self.assertRaises(ExchangeRateError):
            simple_report_html(snapshot, rules)

    def test_fx_changes_do_not_trigger_merchant_price_increases(self) -> None:
        snapshot = self.snapshot(
            [
                self.record("june", day="2026-06-10", currency="USD"),
                self.record("july", day="2026-07-10", currency="USD"),
                self.record("aug", day="2026-08-10", currency="USD"),
            ]
        )
        rates = {
            ("USD", "2026-06"): Decimal(1),
            ("USD", "2026-07"): Decimal(1),
            ("USD", "2026-08"): Decimal(5),
        }
        self.assertNotIn(
            "עלייה לעומת חיובים", simple_report_html(snapshot, rates=rates)
        )

    def test_fee_insurance_and_standing_order_predicates(self) -> None:
        self.assertTrue(
            _is_fee(self.record(category="BANK_FEES", subcategory="MONTHLY_FEE"))
        )
        self.assertTrue(_is_fee(self.record(category="FINANCE", subcategory="FEES")))
        self.assertTrue(_is_fee(self.record(merchant="דמי כרטיס")))
        self.assertFalse(_is_fee(self.record()))
        premium = self.record(
            category="HOUSEHOLD_&_SERVICES", subcategory="INSURANCE_&_FEES"
        )
        self.assertFalse(_is_fee(premium))
        self.assertTrue(_is_insurance(premium))
        self.assertFalse(_is_insurance(self.record()))
        self.assertTrue(_is_standing_order(self.record(subcategory="DIRECT_DEBIT")))
        self.assertFalse(_is_standing_order(self.record()))

    def test_recurring_merchants_need_stable_monthly_charges(self) -> None:
        records = []
        for month in ("06", "07", "08"):
            day = f"2026-{month}-05"
            records.append(
                self.record(
                    f"netflix-{month}", day=day, merchant="Netflix", amount="-49.90"
                )
            )
            records.append(
                self.record(
                    f"shop-{month}",
                    day=day,
                    merchant="Shop",
                    amount={"06": "-100", "07": "-400", "08": "-900"}[month],
                )
            )
            records += [
                self.record(
                    f"coffee-{month}-{i}", day=day, merchant="Cafe", amount="-15"
                )
                for i in range(4)
            ]
        records.append(
            self.record("two-months", day="2026-07-01", merchant="Gym", amount="-199")
        )
        records.append(
            self.record("gym", day="2026-08-01", merchant="Gym", amount="-199")
        )
        records.append(
            self.record(
                "premium-06",
                day="2026-06-02",
                merchant="Insurer",
                category="INSURANCE",
                subcategory="INSURANCE",
            )
        )
        self.assertEqual(recurring_merchants(records), {"Netflix"})

    def test_expense_subjects_groups_by_merchant_and_sorts_most_expensive_first(
        self,
    ) -> None:
        records = [
            self.record("netflix1", merchant="Netflix", amount="-40"),
            self.record("netflix2", merchant="Netflix", amount="-40"),
            self.record("gympass", merchant="GymPass", amount="-199"),
            self.record("spotify", merchant="Spotify", amount="-20"),
            self.record("credit", merchant="Netflix", amount="40"),
            self.record("pending", merchant="Netflix", amount="-40", status="PENDING"),
        ]
        for record in records:
            record["category"], record["subcategory"] = "SUBSCRIPTIONS", "STREAMING"
        records[2]["category"] = records[2]["subcategory"] = "SUBSCRIPTIONS"
        subjects = expense_subjects(records, [], lambda r: True)
        self.assertEqual(
            [(item["subject"], item["count"], item["total"]) for item in subjects],
            [
                ("GymPass", 1, Decimal(199)),
                ("Netflix", 2, Decimal(80)),
                ("Spotify", 1, Decimal(20)),
            ],
        )

    def test_expense_subjects_excludes_self_transfer_gift_and_income_tags(
        self,
    ) -> None:
        records = [
            self.record(
                "transfer",
                merchant="Own account",
                amount="-100",
                category="SUBSCRIPTIONS",
            ),
            self.record(
                "kept", merchant="GymPass", amount="-199", category="SUBSCRIPTIONS"
            ),
        ]
        rule = LiveTagRule(
            tag="self_transfer", account_id="account-1", record_id="transfer"
        )
        subjects = expense_subjects(records, [rule], lambda r: True)
        self.assertEqual([item["subject"] for item in subjects], ["GymPass"])

    def test_report_shows_top_ten_fees_subscriptions_and_standing_orders(self) -> None:
        records = [
            self.record(
                f"fee{i}",
                merchant=f"Bank fee {i}",
                amount=str(-(i + 1)),
                category="BANK_FEES",
                subcategory="MONTHLY_FEE",
            )
            for i in range(12)
        ] + [
            self.record(
                "netflix",
                merchant="Netflix",
                amount="-49.90",
                category="SUBSCRIPTIONS",
                subcategory="STREAMING",
            ),
            self.record(
                "municipal-tax",
                merchant="Municipal tax",
                amount="-350",
                category="INCOMES_EXPENSES",
                subcategory="DIRECT_DEBIT",
            ),
        ]
        html = simple_report_html(self.snapshot(records))
        self.assertIn("עמלות, ביטוחים, מנויים והוראות קבע", html)
        top_section = html[html.index("עמלות, ביטוחים, מנויים והוראות קבע") :]
        self.assertIn("Bank fee 11", top_section)
        # Only the 10 most expensive fees are listed, cheapest excluded.
        self.assertNotIn("Bank fee 0<", top_section)
        self.assertNotIn("Bank fee 1<", top_section)
        self.assertIn("Netflix", top_section)
        self.assertIn("Municipal tax", top_section)
        self.assertIn("350.00 ₪", top_section)

    def test_top_categories_section_reports_missing_and_partial_last_month(
        self,
    ) -> None:
        snapshot = self.snapshot([], date_from="2026-09-01")
        html = simple_report_html(snapshot)
        self.assertIn("אין נתונים לחודש הזה; אי אפשר להציג רשימות", html)
        snapshot = self.snapshot(
            [self.record(category="SUBSCRIPTIONS")],
            date_from="2026-08-15",
            date_to="2026-08-31",
        )
        html = simple_report_html(snapshot)
        top_index = html.index("עמלות, ביטוחים, מנויים והוראות קבע")
        self.assertIn("הבדיקה לחודש הזה חלקית", html[top_index:])
