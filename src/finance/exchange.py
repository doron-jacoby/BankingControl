"""Bank of Israel month-end USD/EUR rates, frozen in the encrypted database."""

import calendar
import csv
import http.client
import io
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from sqlcipher3 import dbapi2 as sqlcipher

from finance.models import utc_now

RATE_SOURCE = "https://www.boi.org.il/information/bank-paymnts/guide/api-guide/"
RATE_HOST = "edge.boi.gov.il"
RATE_PATH = "/FusionEdgeServer/sdmx/v2/data/dataflow/BOI.STATISTICS/EXR/1.0/"


class ExchangeRateError(ValueError):
    """A missing/invalid rate must not silently produce a partial shekel total."""


def fetch_month_end_rate(currency: str, month: str) -> tuple[Decimal, str, str]:
    if currency not in {"USD", "EUR"}:
        raise ExchangeRateError(
            f"No verified ILS rate source configured for {currency}."
        )
    first = date.fromisoformat(month + "-01")
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    if last >= utc_now().astimezone(ZoneInfo("Asia/Jerusalem")).date():
        raise ExchangeRateError(
            "Only completed months can have frozen month-end rates."
        )
    route = (
        RATE_PATH
        + f"RER_{currency}_ILS?"
        + urlencode(
            {
                "startPeriod": first.isoformat(),
                "endPeriod": last.isoformat(),
                "format": "csv",
            }
        )
    )
    connection = http.client.HTTPSConnection(RATE_HOST, timeout=30)
    try:
        connection.request(
            "GET",
            route,
            headers={"Accept": "text/csv", "User-Agent": "PersonalFinanceMonitor/0.1"},
        )
        response = connection.getresponse()
        data = response.read(1024 * 1024 + 1)
        if response.status != 200 or len(data) > 1024 * 1024:
            raise ExchangeRateError(
                f"Bank of Israel rates unavailable for {currency} {month}."
            )
        rate, observed_on = parse_month_end_rate(
            data.decode("utf-8-sig"), currency, month
        )
        return rate, observed_on, f"https://{RATE_HOST}{route}"
    except (OSError, http.client.HTTPException, UnicodeError):
        raise ExchangeRateError(
            "Could not read Bank of Israel rates; existing report retained."
        ) from None
    finally:
        connection.close()


def parse_month_end_rate(
    payload: str, currency: str, month: str
) -> tuple[Decimal, str]:
    """Select the last actual daily observation in the requested calendar month."""
    observations: dict[str, Decimal] = {}
    try:
        for row in csv.DictReader(io.StringIO(payload)):
            if (
                row["SERIES_CODE"],
                row["BASE_CURRENCY"],
                row["COUNTER_CURRENCY"],
                row["UNIT_MEASURE"],
                row["UNIT_MULT"],
                row["FREQ"],
                row["DATA_TYPE"],
            ) != (f"RER_{currency}_ILS", currency, "ILS", "ILS", "0", "D", "OF00"):
                raise ValueError
            observed_on = date.fromisoformat(row["TIME_PERIOD"]).isoformat()
            rate = Decimal(row["OBS_VALUE"])
            if (
                not observed_on.startswith(month + "-")
                or not rate.is_finite()
                or not 0 < rate < 1000
            ):
                raise ValueError
            if len(rate.as_tuple().digits) > 30 or int(rate.as_tuple().exponent) < -30:
                raise ValueError
            if observed_on in observations and observations[observed_on] != rate:
                raise ValueError
            observations[observed_on] = rate
        if not observations:
            raise ValueError
    except (KeyError, TypeError, ValueError, InvalidOperation, csv.Error):
        raise ExchangeRateError(
            f"Invalid or missing Bank of Israel rate for {currency} {month}."
        ) from None
    observed_on = max(observations)
    return observations[observed_on], observed_on


def ensure_month_end_rates(
    db: sqlcipher.Connection, pairs: set[tuple[str, str]]
) -> dict[tuple[str, str], Decimal]:
    """Fetch each missing (currency, month) once; never update an existing rate."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS month_end_rates ("
        "currency TEXT NOT NULL, month TEXT NOT NULL, rate TEXT NOT NULL, "
        "observed_on TEXT NOT NULL, source_url TEXT NOT NULL, captured_at TEXT NOT NULL, "
        "PRIMARY KEY(currency, month))"
    )
    result = {}
    for currency, month in sorted(pairs):
        row = db.execute(
            "SELECT rate FROM month_end_rates WHERE currency=? AND month=?",
            (currency, month),
        ).fetchone()
        if row is None:
            rate, observed_on, source_url = fetch_month_end_rate(currency, month)
            db.execute(
                "INSERT OR IGNORE INTO month_end_rates VALUES (?, ?, ?, ?, ?, ?)",
                (
                    currency,
                    month,
                    str(rate),
                    observed_on,
                    source_url,
                    utc_now().isoformat(),
                ),
            )
            # A concurrent import can win the insert; always use the persisted value.
            row = db.execute(
                "SELECT rate FROM month_end_rates WHERE currency=? AND month=?",
                (currency, month),
            ).fetchone()
        result[currency, month] = Decimal(row[0])
    return result


def shekel_record(
    record: dict[str, Any], rates: dict[tuple[str, str], Decimal]
) -> dict[str, Any]:
    if record["currency"] == "ILS":
        return record
    amount = record["amount"]
    if amount is not None:
        rate = rates.get((record["currency"], record["day"][:7]))
        if rate is None or not rate.is_finite() or rate <= 0:
            raise ExchangeRateError(
                f"Missing saved month-end rate for {record['currency']} {record['day'][:7]}."
            )
        with localcontext() as context:
            context.prec = 440
            amount = str(Decimal(amount) * rate)
    return {**record, "currency": "ILS", "amount": amount}
