import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from finance.exchange import (
    ExchangeRateError,
    ensure_month_end_rates,
    fetch_month_end_rate,
    parse_month_end_rate,
)
from finance.storage import open_database

HEADER = "SERIES_CODE,FREQ,BASE_CURRENCY,COUNTER_CURRENCY,UNIT_MEASURE,UNIT_MULT,DATA_TYPE,TIME_PERIOD,OBS_VALUE\n"


def observation(day: str, value: str, currency: str = "USD") -> str:
    return f"RER_{currency}_ILS,D,{currency},ILS,ILS,0,OF00,{day},{value}\n"


class ExchangeTests(unittest.TestCase):
    def test_last_published_day_not_monthly_average_and_order_independent(self) -> None:
        payload = (
            HEADER + observation("2026-01-30", "3.1") + observation("2026-01-29", "3.2")
        )
        self.assertEqual(
            parse_month_end_rate(payload, "USD", "2026-01"),
            (Decimal("3.1"), "2026-01-30"),
        )
        payload = HEADER + observation("2024-02-29", "4.0", "EUR")
        self.assertEqual(
            parse_month_end_rate(payload, "EUR", "2024-02"), (Decimal(4), "2024-02-29")
        )

    def test_invalid_rates_units_currency_or_out_of_month_fail_closed(self) -> None:
        for body in [
            HEADER,
            "<html>Service unavailable</html>",
            HEADER + observation("2026-02-02", "3.2"),
            HEADER + observation("2026-01-30", "NaN"),
            HEADER + observation("2026-01-30", "0"),
            HEADER + observation("2026-01-30", "-1"),
            HEADER + observation("2026-01-30", "3.2", "EUR"),
            HEADER + observation("2026-01-30", "3.2").replace(",ILS,0,", ",ILS,2,"),
            HEADER
            + observation("2026-01-30", "3.2")
            + observation("2026-01-30", "3.3"),
        ]:
            with self.subTest(body=body):
                with self.assertRaises(ExchangeRateError):
                    parse_month_end_rate(body, "USD", "2026-01")

    def test_http_query_is_month_bounded_and_unclosed_month_is_not_frozen(self) -> None:
        response = Mock(status=200)
        response.read.return_value = (
            HEADER + observation("2026-01-30", "3.1")
        ).encode()
        connection = Mock()
        connection.getresponse.return_value = response
        with (
            patch(
                "finance.exchange.http.client.HTTPSConnection", return_value=connection
            ),
            patch(
                "finance.exchange.utc_now",
                return_value=datetime(2026, 2, 5, tzinfo=UTC),
            ),
        ):
            rate, day, url = fetch_month_end_rate("USD", "2026-01")
            self.assertEqual((rate, day), (Decimal("3.1"), "2026-01-30"))
            self.assertIn("startPeriod=2026-01-01", url)
            self.assertIn("endPeriod=2026-01-31", url)
            self.assertEqual(connection.request.call_args.args[0], "GET")
            self.assertNotIn(
                "Authorization", connection.request.call_args.kwargs["headers"]
            )
            with self.assertRaises(ExchangeRateError):
                fetch_month_end_rate("USD", "2026-02")
        connection.close.assert_called_once()

    def test_saved_rates_survive_reopen_and_are_never_replaced_by_new_quotes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "finance.db"
            with (
                open_database("a" * 64, path, create=True) as db,
                patch(
                    "finance.exchange.fetch_month_end_rate",
                    return_value=(
                        Decimal("3.1"),
                        "2026-01-30",
                        "https://edge.boi.gov.il/example",
                    ),
                ) as fetch,
            ):
                self.assertEqual(
                    ensure_month_end_rates(db, {("USD", "2026-01")})["USD", "2026-01"],
                    Decimal("3.1"),
                )
                fetch.assert_called_once()
            with (
                open_database("a" * 64, path) as db,
                patch(
                    "finance.exchange.fetch_month_end_rate",
                    side_effect=AssertionError("Must remain offline"),
                ),
            ):
                self.assertEqual(
                    ensure_month_end_rates(db, {("USD", "2026-01")})["USD", "2026-01"],
                    Decimal("3.1"),
                )
                row = db.execute(
                    "SELECT observed_on, source_url, captured_at FROM month_end_rates"
                ).fetchone()
                self.assertEqual(
                    row[:2], ("2026-01-30", "https://edge.boi.gov.il/example")
                )
                self.assertTrue(row[2])
