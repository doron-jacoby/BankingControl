"""Encrypted current snapshot and provisional source-labelled movement reports.

No pending/final mapping or settlement matching is inferred. This snapshot is
kept apart from the reconciled expense ledger until that contract is verified.
"""

import calendar
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from html import escape
from pathlib import Path
from statistics import median
from typing import Any

from sqlcipher3 import dbapi2 as sqlcipher

from finance.exchange import shekel_record
from finance.financy import FinancyClient, FinancyError, required_text
from finance.models import (
    Account,
    new_id,
    require_aware,
    utc_now,
    validate_currency,
    validate_money,
)
from finance.repository import dictionaries
from finance.security import (
    DATABASE_KEY,
    SERVICE,
    SecretStore,
    create_database_key,
    load_database_key,
)
from finance.storage import open_database
from finance.sync import sync_lock

LIMITATIONS = (
    "Provisional movements, not reconciled spending. Source BOOKED labels are "
    "not verified final-ledger semantics. Untagged transfers, card settlements, "
    "investment movements and credits are held for review, not matched by amount. "
    "Only explicit expense tags override those exclusions; positive expense "
    "adjustments reduce spending. Requested dates do not prove bank coverage. "
    "Category and anomaly rules are local hints, not verified findings."
)
TAGS = {"self_transfer", "gift", "income", "expense"}
LABEL_PATTERN = re.compile(r"[A-Z][A-Z_& -]{0,79}")
FEE_KEYWORDS = {
    "FEE",
    "FEES",
    "CHARGE",
    "CHARGES",
    "COMMISSION",
    "PENALTY",
    "INTEREST",
    "OVERDRAFT",
}
HEBREW_FEE_WORDS = ("עמלה", "עמלת", "עמלות", "דמי כרטיס", "דמי ניהול")
OTHER_CATEGORY_LABEL = "אחר"
UNIDENTIFIED_CATEGORY_LABEL = "לא מזוהה"
# Whole tokens only. Subcategory takes precedence over the broad source category.
GENERAL_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "מזון",
        (
            "FOOD",
            "GROCERIES",
            "GROCERY",
            "RESTAURANT",
            "RESTAURANTS",
            "DINING",
            "CAFE",
            "COFFEE",
            "SUPERMARKET",
            "BAR",
            "BARS",
            "SNACKS",
        ),
    ),
    (
        "קניות",
        (
            "SHOPPING",
            "SHOP",
            "RETAIL",
            "CLOTHES",
            "CLOTHING",
            "ELECTRONICS",
            "ELECTRONIC",
            "MARKETPLACE",
            "BOOKS",
        ),
    ),
    (
        "תחבורה בארץ",
        (
            "TRANSPORT",
            "TRANSPORTATION",
            "FUEL",
            "PARKING",
            "TAXI",
            "CAR",
            "TOLL",
            "TRAIN",
            "BUS",
            "PUBLIC",
        ),
    ),
    (
        "חינוך",
        (
            "EDUCATION",
            "SCHOOL",
            "SCHOOLS",
            "TUITION",
            "KINDERGARTEN",
            "NURSERY",
            "UNIVERSITY",
            "COLLEGE",
        ),
    ),
    (
        "בריאות וביטוח",
        (
            "HEALTH",
            "HEALTHCARE",
            "MEDICAL",
            "PHARMACY",
            "INSURANCE",
            "DOCTOR",
            "DENTAL",
            "CLINIC",
            "EYECARE",
            "BEAUTY",
        ),
    ),
    (
        "פנאי",
        (
            "ENTERTAINMENT",
            "STREAMING",
            "LEISURE",
            "SPORT",
            "SPORTS",
            "FITNESS",
            "HOBBY",
            "GAME",
            "GAMES",
            "CINEMA",
            "MUSIC",
            "CULTURE",
            "EVENTS",
        ),
    ),
    (
        "טיולים בחו״ל",
        (
            "FLIGHT",
            "FLIGHTS",
            "AIRLINE",
            "AIRLINES",
            "AIRFARE",
            "ABROAD",
            "OVERSEAS",
            "VACATION",
            "TRAVEL",
        ),
    ),
)
HEBREW_MONTHS = {
    1: "ינואר",
    2: "פברואר",
    3: "מרץ",
    4: "אפריל",
    5: "מאי",
    6: "יוני",
    7: "יולי",
    8: "אוגוסט",
    9: "ספטמבר",
    10: "אוקטובר",
    11: "נובמבר",
    12: "דצמבר",
}
REASON_LABELS_HE = {
    "fee_like_label": "נראה כמו עמלה או חיוב",
    "unusual_amount_for_category": "סכום חריג ביחס לקטגוריה בחודש הזה",
    "historical_increase": "עלייה לעומת חיובים קודמים מאותו סוג",
    "insurance_review": "ביטוח — לבדוק ספק, כיסוי וסכום",
    "unclear_charge": "חיוב ללא סיווג ברור",
    "unresolved_transfer": "העברה לא מזוהה — לא נכללה בהוצאות",
    "unresolved_settlement": "חיוב כרטיס בעו״ש — לא נכלל כדי למנוע ספירה כפולה",
    "unresolved_investment": "תנועת השקעה או המרת מטבע — לא נכללה בהוצאות",
    "unresolved_credit": "זיכוי לא מזוהה — לבדוק אם זה החזר הוצאה",
    "missing_amount": "סכום חסר — לא נכלל בהוצאות",
    "not_booked": "חיוב ממתין או סטטוס לא ידוע — לא נכלל בהוצאות",
}
ACCOUNT_TYPE_LABELS_HE = {
    "checking": "עו״ש",
    "credit_card": "כרטיס אשראי",
    "savings": "חיסכון",
    "loan": "הלוואה",
    "investment": "השקעות",
}


@dataclass(frozen=True)
class LiveRecord:
    id: str
    account_id: str
    account_type: str
    day: str
    date_source: str
    amount: str | None
    currency: str
    status: str
    category: str
    subcategory: str
    merchant: str = ""
    merchant_country: str = ""


def label(value: object, fallback: str = "UNCLASSIFIED") -> str:
    return (
        value if isinstance(value, str) and LABEL_PATTERN.fullmatch(value) else fallback
    )


@dataclass(frozen=True, kw_only=True)
class LiveTagRule:
    """A user-confirmed reconciliation decision for provisional live records.

    Financy exposes no counterparty details, so self-transfers and gifts cannot
    be inferred from amounts or categories alone; the user identifies them from
    their own bank/Financy records and this rule remembers that decision so it
    can be reapplied to history and future syncs without repeating the review.
    """

    tag: str
    account_id: str | None = None
    record_id: str | None = None
    category: str | None = None
    subcategory: str | None = None
    note: str = ""
    priority: int = 0
    enabled: bool = True
    internal_id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        require_aware(self.created_at)
        require_aware(self.updated_at)


def save_tag_rule(db: sqlcipher.Connection, rule: LiveTagRule) -> None:
    if rule.tag not in TAGS:
        raise ValueError("Unsupported reconciliation tag")
    if not (rule.record_id or rule.category or rule.subcategory or rule.account_id):
        raise ValueError(
            "A rule must match on a record, account, category or subcategory"
        )
    if rule.record_id is not None and rule.account_id is None:
        raise ValueError("A record-specific rule requires its account ID")
    for value in (rule.category, rule.subcategory):
        if value is not None and not LABEL_PATTERN.fullmatch(value):
            raise ValueError(
                "Category and subcategory must match Financy's label format"
            )
    for value in (rule.account_id, rule.record_id):
        if value is not None and (not value.strip() or len(value) > 8192):
            raise ValueError("Account and record IDs must be non-empty and bounded")
    if len(rule.note) > 2000:
        raise ValueError("Note is too long")
    db.execute(
        "CREATE TABLE IF NOT EXISTS live_tags ("
        "internal_id TEXT PRIMARY KEY, account_id TEXT, record_id TEXT, "
        "category TEXT, subcategory TEXT, tag TEXT NOT NULL, note TEXT NOT NULL, "
        "priority INTEGER NOT NULL, enabled INTEGER NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    db.execute(
        "INSERT INTO live_tags VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(internal_id) DO UPDATE SET account_id=excluded.account_id, "
        "record_id=excluded.record_id, category=excluded.category, "
        "subcategory=excluded.subcategory, tag=excluded.tag, note=excluded.note, "
        "priority=excluded.priority, enabled=excluded.enabled, "
        "updated_at=excluded.updated_at",
        (
            rule.internal_id,
            rule.account_id,
            rule.record_id,
            rule.category,
            rule.subcategory,
            rule.tag,
            rule.note,
            rule.priority,
            rule.enabled,
            rule.created_at.isoformat(),
            rule.updated_at.isoformat(),
        ),
    )


def load_tag_rules(db: sqlcipher.Connection) -> list[LiveTagRule]:
    db.execute(
        "CREATE TABLE IF NOT EXISTS live_tags ("
        "internal_id TEXT PRIMARY KEY, account_id TEXT, record_id TEXT, "
        "category TEXT, subcategory TEXT, tag TEXT NOT NULL, note TEXT NOT NULL, "
        "priority INTEGER NOT NULL, enabled INTEGER NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    result = []
    for row in dictionaries(
        db,
        "SELECT * FROM live_tags WHERE enabled=1 ORDER BY priority DESC, internal_id",
    ):
        for name in ("created_at", "updated_at"):
            row[name] = datetime.fromisoformat(row[name])
        row["enabled"] = bool(row["enabled"])
        result.append(LiveTagRule(**row))
    return result


def resolve_tag(record: dict[str, Any], rules: Sequence[LiveTagRule]) -> str | None:
    """First matching rule wins; rules are pre-sorted by priority, then ID."""
    for rule in rules:
        if (
            (rule.record_id is None or rule.record_id == record["id"])
            and (rule.account_id is None or rule.account_id == record["account_id"])
            and (rule.category is None or rule.category == record["category"])
            and (rule.subcategory is None or rule.subcategory == record["subcategory"])
        ):
            return rule.tag
    return None


def _school_merchant(value: str) -> bool:
    normalized = " ".join(re.findall(r"[\w]+", value.upper()))
    return any(
        name in normalized
        for name in (
            "הכפר הירוק",
            "כפר הירוק",
            "HAKFAR HAYAROK",
            "KFAR HAYAROK",
            "KFAR YAROK",
        )
    )


def _car_wash_merchant(value: str) -> bool:
    # Financy files this under FOOD_&_DRINKS/RESTAURANT; it is actually a car wash.
    normalized = " ".join(re.findall(r"[\w]+", value.upper()))
    return any(name in normalized for name in ("תחנת החוף המנהרה",))


def normalize(row: dict[str, Any], account_map: dict[str, Account]) -> LiveRecord:
    """Keep only report fields and IDs; discard descriptions and account numbers."""
    try:
        account_id = required_text(row, "accountId")
        account = account_map[account_id]
        charged = row["amount"]["chargedAmount"]
        value = charged["amount"]
        if value is None or value == "":
            amount = None
        elif isinstance(value, bool) or not isinstance(value, int | Decimal | str):
            raise ValueError
        else:
            if isinstance(value, str) and (
                len(value) > 512
                or not re.fullmatch(
                    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value
                )
            ):
                raise ValueError
            amount = Decimal(value)
        currency = required_text(charged, "currency")
        if amount is None:
            validate_currency(currency)
        else:
            validate_money(amount, currency)
        dates = row["date"]
        date_source = next(
            name
            for name in ("transactionDate", "bookingDate", "valueDate")
            if dates.get(name)
        )
        raw_day = dates[date_source]
        if not isinstance(raw_day, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", raw_day
        ):
            raise ValueError
        day = date.fromisoformat(raw_day)
        category = row.get("changedCategory") or row.get("category") or {}
        merchant = row.get("merchantName") or ""
        description = row.get("description")
        if isinstance(description, dict) and any(
            isinstance(value, str) and _school_merchant(value)
            for value in description.values()
        ):
            # User-confirmed school: retain its canonical name, not the raw
            # bank description (which can contain account or personal details).
            merchant = "הכפר הירוק"
        country = (row.get("merchantAddress") or {}).get("country") or ""
        if not isinstance(merchant, str) or not isinstance(country, str):
            raise ValueError
        # Retain only useful merchant context, never the street/address or description.
        merchant = re.sub(r"\d{6,}", "…", " ".join(merchant.split()))[:160]
        country = " ".join(country.upper().split())[:80]
        return LiveRecord(
            id=required_text(row, "id"),
            account_id=account_id,
            account_type=account.account_type,
            day=day.isoformat(),
            date_source=date_source,
            amount=str(amount) if amount is not None else None,
            currency=currency,
            status=label(row.get("status"), "UNKNOWN"),
            category=label(category.get("main"), "UNCATEGORIZED"),
            subcategory=label(category.get("sub"), "UNCATEGORIZED"),
            merchant=merchant,
            merchant_country=country,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        StopIteration,
        InvalidOperation,
    ):
        raise FinancyError(
            "validation",
            detail="Transaction fields invalid; previous snapshot retained",
        ) from None


def sync_snapshot(
    client: FinancyClient,
    path: Path,
    store: SecretStore,
    start: date,
    end: date,
) -> dict[str, Any]:
    if start > end:
        raise ValueError("Date range must be ordered")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with sync_lock(path.with_suffix(".sync.lock")) as acquired:
        if not acquired:
            return {"status": "already_running"}
        discovered = client.list_accounts()
        account_map = {a.provider_account_id: a for a in discovered}
        if len(account_map) != len(discovered):
            raise FinancyError("validation", detail="Duplicate account IDs")
        records: dict[tuple[str, str], LiveRecord] = {}
        duplicates = outside_range = repeated = 0
        for row in client.transaction_rows(start, end):
            duplicate = row.get("isDuplicate", False)
            if not isinstance(duplicate, bool):
                raise FinancyError("validation", detail="Invalid isDuplicate field")
            if duplicate:
                duplicates += 1
                continue
            record = normalize(row, account_map)
            identity = (record.account_id, record.id)
            if identity in records:
                if records[identity] != record:
                    raise FinancyError(
                        "validation", detail="Conflicting transaction IDs"
                    )
                repeated += 1
                continue
            records[identity] = record
        selected = []
        for record in records.values():
            if start.isoformat() <= record.day <= end.isoformat():
                selected.append(asdict(record))
            else:
                outside_range += 1
        info = {
            "status": "completed",
            "analysis": "provisional",
            "captured_at": utc_now().isoformat(),
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "account_count": len(discovered),
            "accounts_with_transactions": len({r["account_id"] for r in selected}),
            "transaction_count": len(selected),
            "duplicates_excluded": duplicates,
            "repeated_ids_excluded": repeated,
            "outside_range_excluded": outside_range,
            "source_status_counts": dict(Counter(r["status"] for r in selected)),
            "date_source_counts": dict(Counter(r["date_source"] for r in selected)),
            "missing_amount_count": sum(r["amount"] is None for r in selected),
        }
        # A full bounded snapshot avoids stale pending rows and changing IDs
        # accumulating across syncs. It is not a historical coverage guarantee.
        if path.exists() or store.get_password(SERVICE, DATABASE_KEY) is not None:
            key = load_database_key(store)
        else:
            key = create_database_key(store)
        with open_database(key, path, create=not path.exists()) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS live_snapshot ("
                "id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO live_snapshot VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (json.dumps({"info": info, "records": selected}),),
            )
        return info


def load_snapshot(path: Path, store: SecretStore) -> dict[str, Any]:
    with open_database(load_database_key(store), path) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_snapshot'"
        ).fetchone():
            raise ValueError("Run finance sync first")
        row = db.execute("SELECT payload FROM live_snapshot WHERE id=1").fetchone()
        if row is None:
            raise ValueError("Run finance sync first")
        result: dict[str, Any] = json.loads(row[0])
        return result


def _reconcile_reason(
    record: dict[str, Any], amount: Decimal | None, tag: str | None
) -> tuple[str, bool]:
    if record["status"] != "BOOKED":
        return "not_booked", False
    if amount is None:
        return "missing_amount", False
    if tag == "self_transfer":
        return "self_transfer", False
    if tag == "gift":
        return "gift", False
    if tag == "income":
        return "income", True
    if tag == "expense":
        return "expense_tagged", True
    if amount >= 0:
        return "unresolved_credit", False
    if record["subcategory"] == "CREDIT_CARD_CHECKING":
        return "unresolved_settlement", False
    if record["category"] == "TRANSFER":
        return "unresolved_transfer", False
    if record["category"] in {
        "TRADING",
        "SECURITY",
        "SECURITIES",
        "INVESTMENT",
        "INVESTMENTS",
        "DEPOSIT",
    } or record["account_type"] in {"investment", "savings"}:
        return "unresolved_investment", False
    if amount < 0:
        return "expense", True
    return "unresolved_credit", False


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[A-Z]+", text.upper()))


def _record_tokens(record: dict[str, Any]) -> set[str]:
    return _tokens(f"{record['category']} {record['subcategory']}")


def _is_insurance(record: dict[str, Any]) -> bool:
    return "INSURANCE" in _record_tokens(record)


def _is_fee(record: dict[str, Any]) -> bool:
    # Financy files insurance premiums under INSURANCE_&_FEES; those are not fees.
    if _is_insurance(record):
        return False
    merchant = record.get("merchant") or ""
    return bool(_record_tokens(record) & FEE_KEYWORDS) or any(
        word in merchant for word in HEBREW_FEE_WORDS
    )


def _is_standing_order(record: dict[str, Any]) -> bool:
    return bool(record["subcategory"] == "DIRECT_DEBIT")


def recurring_merchants(
    records: Sequence[dict[str, Any]], rules: Sequence[LiveTagRule] = ()
) -> set[str]:
    """Merchants charged about once a month, at a stable price, in 3+ months.

    Financy has no subscription label, so this is a heuristic: variable
    shopping and frequent small purchases (coffee) do not qualify.
    """
    charges: dict[str, list[tuple[str, Decimal]]] = {}
    for record in records:
        merchant = record.get("merchant") or ""
        if not merchant or record["amount"] is None:
            continue
        if _is_fee(record) or _is_insurance(record) or _is_standing_order(record):
            continue
        amount = Decimal(record["amount"])
        reason, included = _reconcile_reason(record, amount, resolve_tag(record, rules))
        if not included or reason == "income" or amount >= 0:
            continue
        charges.setdefault(merchant, []).append((record["day"][:7], -amount))
    result = set()
    with localcontext() as context:
        context.prec = 400 + len(str(len(records)))
        for merchant, items in charges.items():
            months = {month for month, _ in items}
            if len(months) < 3 or len(items) > len(months) * Decimal("1.5"):
                continue
            typical = median(amount for _, amount in items)
            stable = sum(
                abs(amount - typical) <= typical * Decimal("0.2") for _, amount in items
            )
            if stable >= len(items) * Decimal("0.8"):
                result.add(merchant)
    return result


def _flag_debits(
    records: list[dict[str, Any]],
    rules: Sequence[LiveTagRule],
    history: Sequence[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Review candidates, including unresolved credits and incomplete charges.

    Insurance stays visible even without enough history to detect a price change.
    A category-wide tag is a classification, not approval of future charges.
    """
    groups: dict[tuple[str, ...], list[Decimal]] = {}
    previous: dict[tuple[str, ...], list[Decimal]] = {}

    def key(record: dict[str, Any]) -> tuple[str, ...]:
        return (
            record["account_id"],
            record["currency"],
            record["category"],
            record["subcategory"],
            record.get("merchant", ""),
        )

    # ponytail: without a merchant, historical comparisons use account/subcategory;
    # retain insurance and unknown items for manual review rather than claim certainty.
    for source, target in ((records, groups), (history, previous)):
        for record in source:
            if record["status"] == "BOOKED" and record["amount"] is not None:
                debit_amount = Decimal(record["amount"])
                if debit_amount < 0 and resolve_tag(record, rules) not in {
                    "self_transfer",
                    "gift",
                    "income",
                }:
                    target.setdefault(key(record), []).append(debit_amount.copy_abs())
    flagged = []
    with localcontext() as context:
        context.prec = 400 + len(str(len(records) + len(history)))
        for record in records:
            tag = resolve_tag(record, rules)
            if tag in {"self_transfer", "gift", "income"}:
                continue
            amount = Decimal(record["amount"]) if record["amount"] is not None else None
            reason, included = _reconcile_reason(record, amount, tag)
            reasons = []
            if not included:
                reasons.append(reason)
            if amount is not None and amount < 0:
                tokens = _tokens(f"{record['category']} {record['subcategory']}")
                if tokens & FEE_KEYWORDS:
                    reasons.append("fee_like_label")
                if "INSURANCE" in tokens:
                    reasons.append("insurance_review")
                if (
                    _general_category(
                        record["category"],
                        record["subcategory"],
                        record.get("merchant_country", ""),
                        record.get("merchant", ""),
                    )
                    == UNIDENTIFIED_CATEGORY_LABEL
                ):
                    reasons.append("unclear_charge")
                baseline = previous.get(key(record), [])
                if len(baseline) >= 2 and -amount > median(baseline) * Decimal("1.25"):
                    reasons.append("historical_increase")
                peers = groups.get(key(record), [])
                if len(peers) >= 3 and -amount > median(peers) * 3:
                    reasons.append("unusual_amount_for_category")
            if reasons:
                flagged.append({**record, "amount": amount, "reasons": reasons})
    return sorted(
        flagged, key=lambda item: abs(item["amount"] or Decimal(0)), reverse=True
    )


def _general_category(
    category: str, subcategory: str, merchant_country: str = "", merchant: str = ""
) -> str:
    if _school_merchant(merchant):
        return "חינוך"
    if _car_wash_merchant(merchant):
        return "תחבורה בארץ"
    tokens = _tokens(f"{category} {subcategory}")
    travel_label, travel_words = GENERAL_CATEGORIES[-1]
    transport_words = GENERAL_CATEGORIES[2][1]
    # Currency alone says nothing about where a purchase happened.
    abroad = bool(merchant_country) and merchant_country.upper() not in {
        "IL",
        "ISR",
        "ISRAEL",
        "ישראל",
        "UNKNOWN",
        "N/A",
        "ZZ",
        "ZZZ",
    }
    if tokens.intersection(travel_words) or (
        abroad and tokens.intersection((*transport_words, "HOTEL", "HOTELS", "LODGING"))
    ):
        return travel_label
    for source in (subcategory, category):
        source_tokens = _tokens(source)
        for category_label, keywords in GENERAL_CATEGORIES[:-1]:
            if source_tokens.intersection(keywords):
                return category_label
    if tokens.intersection({"UNCATEGORIZED", "UNCLASSIFIED", "UNKNOWN"}) or (
        category in {"OTHER", "FINANCE", "INCOMES_EXPENSES", "SUBSCRIPTIONS"}
        and subcategory in {"OTHER", "FINANCE_OTHER", "DIRECT_DEBIT", "SUBSCRIPTIONS"}
    ):
        return UNIDENTIFIED_CATEGORY_LABEL
    return OTHER_CATEGORY_LABEL


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _previous_month_key(day: date) -> str:
    year, month = _shift_month(day.year, day.month, -1)
    return f"{year:04d}-{month:02d}"


def _latest_reviewable_month(info: dict[str, Any]) -> str:
    """Last completed calendar month, anchored to the snapshot's end date."""
    end = date.fromisoformat(info["date_to"])
    if end.day == calendar.monthrange(end.year, end.month)[1]:
        return end.strftime("%Y-%m")
    return _previous_month_key(end)


def _hebrew_month_label(month_key: str) -> str:
    year, month = month_key.split("-")
    return f"{HEBREW_MONTHS[int(month)]} {year}"


def general_category_trend(
    snapshot: dict[str, Any], rules: Sequence[LiveTagRule] = (), months: int = 12
) -> dict[str, Any]:
    """Identified spending for twelve completed months with explicit coverage.

    Missing months remain on the axis; the renderer never presents them as zero.
    """
    info = snapshot["info"]
    end = date.fromisoformat(_latest_reviewable_month(info) + "-01")
    start = date.fromisoformat(info["date_from"])
    fetched_end = date.fromisoformat(info["date_to"])
    keys = []
    coverage = {}
    for offset in reversed(range(months)):
        year, month = _shift_month(end.year, end.month, -offset)
        first = date(year, month, 1)
        last = first.replace(day=calendar.monthrange(year, month)[1])
        key = first.strftime("%Y-%m")
        keys.append(key)
        coverage[key] = (
            "missing"
            if last < start or first > fetched_end
            else ("full" if start <= first and fetched_end >= last else "partial")
        )
    currencies = sorted({r["currency"] for r in snapshot["records"]})
    totals: dict[str, dict[str, dict[str, Any]]] = {
        currency: {key: {"total": Decimal(0), "categories": {}} for key in keys}
        for currency in currencies
    }
    for key in keys:
        records = [r for r in snapshot["records"] if r["day"].startswith(key + "-")]
        # Mirrors summarize()'s precision sizing so wide-range amounts still add
        # exactly, without rounding at Decimal's ambient default context.
        with localcontext() as context:
            context.prec = 400 + len(str(len(records)))
            for record in records:
                amount = (
                    Decimal(record["amount"]) if record["amount"] is not None else None
                )
                reason, included = _reconcile_reason(
                    record, amount, resolve_tag(record, rules)
                )
                if not included or reason == "income":
                    continue
                assert amount is not None
                bucket = _general_category(
                    record["category"],
                    record["subcategory"],
                    record.get("merchant_country", ""),
                    record.get("merchant", ""),
                )
                month_entry = totals[record["currency"]][key]
                month_entry["categories"][bucket] = (
                    month_entry["categories"].get(bucket, Decimal(0)) - amount
                )
                month_entry["total"] -= amount
            for currency in currencies:
                month_entry = totals[currency][key]
                month_entry["total_excluding_travel"] = month_entry["total"] - (
                    month_entry["categories"].get(GENERAL_CATEGORIES[-1][0], Decimal(0))
                )
    return {"months": keys, "currencies": totals, "coverage": coverage}


def _included_debit(
    record: dict[str, Any], rules: Sequence[LiveTagRule]
) -> Decimal | None:
    """The record's spent amount if it is a BOOKED, untagged-transfer debit."""
    if record["status"] != "BOOKED" or record["amount"] is None:
        return None
    amount = Decimal(record["amount"])
    if amount >= 0:
        return None
    if resolve_tag(record, rules) in {"self_transfer", "gift", "income"}:
        return None
    return amount


def expense_subjects(
    records: Sequence[dict[str, Any]],
    rules: Sequence[LiveTagRule],
    predicate: Callable[[dict[str, Any]], bool],
) -> list[dict[str, Any]]:
    """Debits matching predicate, grouped by merchant and sorted most expensive first."""
    groups: dict[str, dict[str, Any]] = {}
    with localcontext() as context:
        context.prec = 400 + len(str(len(records)))
        for record in records:
            amount = _included_debit(record, rules)
            if amount is None or not predicate(record):
                continue
            subject = (
                record.get("merchant")
                or f"ללא שם · {record['category']} / {record['subcategory']}"
            )
            bucket = groups.setdefault(
                subject,
                {
                    "subject": subject,
                    "count": 0,
                    "total": Decimal(0),
                    "days": [],
                    "account_ids": set(),
                },
            )
            bucket["count"] += 1
            bucket["total"] -= amount
            bucket["days"].append(record["day"])
            bucket["account_ids"].add(record["account_id"])
    return sorted(groups.values(), key=lambda item: item["total"], reverse=True)


def _predicate_monthly_totals(
    records: Sequence[dict[str, Any]],
    months: Sequence[str],
    rules: Sequence[LiveTagRule],
    predicate: Callable[[dict[str, Any]], bool],
) -> dict[str, Decimal]:
    totals = {key: Decimal(0) for key in months}
    with localcontext() as context:
        context.prec = 400 + len(str(len(records)))
        for record in records:
            key = record["day"][:7]
            if key not in totals:
                continue
            amount = _included_debit(record, rules)
            if amount is None or not predicate(record):
                continue
            totals[key] -= amount
    return totals


def summarize(
    snapshot: dict[str, Any], month: str, rules: Sequence[LiveTagRule] = ()
) -> dict[str, Any]:
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("Expected YYYY-MM")
    first = date.fromisoformat(month + "-01")
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    info = snapshot["info"]
    start, end = (
        date.fromisoformat(info["date_from"]),
        date.fromisoformat(info["date_to"]),
    )
    if last < start or first > end:
        raise ValueError("Month outside imported snapshot")
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    categories: dict[tuple[str, ...], dict[str, Any]] = {}
    reconciled: dict[str, dict[str, Any]] = {}
    records = [r for r in snapshot["records"] if r["day"].startswith(month + "-")]
    # validate_money bounds individual precision/exponents; 400 digits plus carry
    # covers their entire possible range, without rounding Decimal additions.
    with localcontext() as context:
        context.prec = 400 + len(str(len(records)))
        for record in records:
            amount = Decimal(record["amount"]) if record["amount"] is not None else None
            reconciled_bucket = reconciled.setdefault(
                record["currency"],
                {
                    "spend": Decimal(0),
                    "income": Decimal(0),
                    "included_count": 0,
                    "excluded_count": 0,
                    "breakdown": {},
                },
            )
            reason, included = _reconcile_reason(
                record, amount, resolve_tag(record, rules)
            )
            entry = reconciled_bucket["breakdown"].setdefault(
                reason, {"count": 0, "signed_amount": Decimal(0)}
            )
            entry["count"] += 1
            if amount is not None:
                entry["signed_amount"] += amount
            if included:
                # _reconcile_reason only marks a record included once its amount
                # is known: "missing_amount" and "not_booked" are both excluded.
                assert amount is not None
                reconciled_bucket["included_count"] += 1
                if reason == "income":
                    reconciled_bucket["income"] += amount
                else:
                    reconciled_bucket["spend"] -= amount
            else:
                reconciled_bucket["excluded_count"] += 1
            identity = (record["account_type"], record["currency"], record["status"])
            bucket = groups.setdefault(
                identity,
                {
                    "account_type": identity[0],
                    "currency": identity[1],
                    "source_status": identity[2],
                    "count": 0,
                    "missing_amount_count": 0,
                    "debits": Decimal(0),
                    "credits": Decimal(0),
                },
            )
            bucket["count"] += 1
            if amount is None:
                bucket["missing_amount_count"] += 1
            else:
                bucket["debits" if amount < 0 else "credits"] += abs(amount)
            if record["status"] == "BOOKED":
                category_key = (
                    identity[0],
                    identity[1],
                    record["category"],
                    record["subcategory"],
                )
                category = categories.setdefault(
                    category_key,
                    {
                        "account_type": identity[0],
                        "currency": identity[1],
                        "category": category_key[2],
                        "subcategory": category_key[3],
                        "count": 0,
                        "missing_amount_count": 0,
                        "debits": Decimal(0),
                        "credits": Decimal(0),
                    },
                )
                category["count"] += 1
                if amount is None:
                    category["missing_amount_count"] += 1
                else:
                    category["debits" if amount < 0 else "credits"] += abs(amount)
        flagged_for_review = _flag_debits(
            records, rules, [r for r in snapshot["records"] if r["day"] < month + "-01"]
        )
    return {
        "month": month,
        "analysis": "provisional",
        "limitations": LIMITATIONS,
        "requested_month_complete": start <= first and end >= last,
        "coverage_verified": False,
        "captured_at": info["captured_at"],
        "transaction_count": len(records),
        "date_fallback_count": sum(
            r["date_source"] != "transactionDate" for r in records
        ),
        "review_counts": {
            "missing_amount": sum(r["amount"] is None for r in records),
            "not_booked": sum(r["status"] != "BOOKED" for r in records),
            "uncategorized": sum(r["category"] == "UNCATEGORIZED" for r in records),
            "card_settlement_label": sum(
                r["subcategory"] == "CREDIT_CARD_CHECKING" for r in records
            ),
            "transfer_label": sum(r["category"] == "TRANSFER" for r in records),
            "return_label": sum(r["category"] == "RETURN" for r in records),
            "unresolved_credit": sum(
                bucket["breakdown"].get("unresolved_credit", {"count": 0})["count"]
                for bucket in reconciled.values()
            ),
        },
        "movements": [groups[k] for k in sorted(groups)],
        "booked_categories": sorted(
            categories.values(), key=lambda r: r["debits"], reverse=True
        ),
        "reconciled": {
            currency: reconciled[currency] for currency in sorted(reconciled)
        },
        "flagged_for_review": flagged_for_review,
    }


def report_html(snapshot: dict[str, Any], rules: Sequence[LiveTagRule] = ()) -> str:
    def table(rows: list[dict[str, Any]], columns: dict[str, str]) -> str:
        def cell(value: Any) -> str:
            return escape(
                format(value, ",.2f") if isinstance(value, Decimal) else str(value)
            )

        return (
            "<div class='scroll'><table><thead><tr>"
            + "".join(f"<th>{escape(title)}</th>" for title in columns.values())
            + "</tr></thead><tbody>"
            + "".join(
                "<tr>"
                + "".join(f"<td>{cell(row[key])}</td>" for key in columns)
                + "</tr>"
                for row in rows
            )
            + "</tbody></table></div>"
        )

    info = snapshot["info"]
    current = date.fromisoformat(info["date_from"]).replace(day=1)
    end = date.fromisoformat(info["date_to"])
    sections = []
    while current <= end:
        month = current.strftime("%Y-%m")
        report = summarize(snapshot, month, rules)
        reconciled_rows = [
            {"currency": currency, **bucket}
            for currency, bucket in report["reconciled"].items()
        ]
        breakdown_rows = [
            {"currency": currency, "reason": reason, **entry}
            for currency, bucket in report["reconciled"].items()
            for reason, entry in bucket["breakdown"].items()
        ]
        flagged_rows = [
            {**row, "reasons": ", ".join(row["reasons"])}
            for row in report["flagged_for_review"]
        ]
        coverage = (
            "Full requested month"
            if report["requested_month_complete"]
            else "Partial month"
        )
        booked = [r for r in report["movements"] if r["source_status"] == "BOOKED"]
        other = [r for r in report["movements"] if r["source_status"] != "BOOKED"]
        columns = {
            "account_type": "Account type",
            "currency": "Currency",
            "count": "Records",
            "debits": "Known debits",
            "credits": "Known credits",
            "missing_amount_count": "Missing amount",
        }
        sections.append(
            f"<section><h2>{month} <small>{coverage}</small></h2>"
            f"<p>{report['transaction_count']} records · {report['date_fallback_count']} use booking/value date.</p>"
            "<h3>Identified spending and income (including saved decisions)</h3>"
            + table(
                reconciled_rows,
                {
                    "currency": "Currency",
                    "spend": "Identified spending",
                    "income": "Tagged income",
                    "included_count": "Included",
                    "excluded_count": "Excluded",
                },
            )
            + "<h3>Spend by category (BOOKED movements)</h3>"
            + table(
                report["booked_categories"],
                {"category": "Category", "subcategory": "Subcategory", **columns},
            )
            + "<h3>Flagged for review — worth a look, not a finding</h3>"
            + (
                table(
                    flagged_rows,
                    {
                        "day": "Date",
                        "account_type": "Account type",
                        "currency": "Currency",
                        "category": "Category",
                        "subcategory": "Subcategory",
                        "amount": "Amount",
                        "reasons": "Why flagged",
                        "id": "Record ID",
                    },
                )
                if flagged_rows
                else "<p>Nothing flagged this month.</p>"
            )
            + "<details><summary>Movements labelled BOOKED</summary>"
            + table(booked, columns)
            + "</details><details><summary>Pending and other statuses — separate from BOOKED</summary>"
            + (
                table(other, {"source_status": "Source status", **columns})
                if other
                else "<p>None in this snapshot.</p>"
            )
            + "</details><details><summary>Reconciliation breakdown by reason</summary>"
            + table(
                breakdown_rows,
                {
                    "currency": "Currency",
                    "reason": "Reason",
                    "count": "Records",
                    "signed_amount": "Signed amount",
                },
            )
            + "</details><p>Review labels (counts can overlap): "
            + escape(
                " · ".join(
                    f"{k.replace('_', ' ')}: {v}"
                    for k, v in report["review_counts"].items()
                )
            )
            + "</p></section>"
        )
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    return (
        "<!doctype html><html lang='en'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Personal finance — provisional analysis</title><style>"
        "body{font:16px system-ui,sans-serif;background:#f4f6f8;color:#172536;max-width:1100px;margin:40px auto;padding:0 20px}"
        "section,header{background:white;border:1px solid #dae1e8;border-radius:12px;padding:24px;margin-bottom:20px}"
        "h1{margin-top:0}h3,summary{font-size:16px}small{font-size:14px;color:#637080;font-weight:400}"
        ".note{border-left:4px solid #c38314;padding:12px;background:#fff8e9;line-height:1.6}"
        "table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:10px;text-align:right;border-bottom:1px solid #e7edf2}"
        "th:first-child,td:first-child{text-align:left}.scroll{overflow-x:auto}summary{cursor:pointer;margin:20px 0}"
        "p{line-height:1.6}@media print{body{background:white;margin:0}details{display:block}section{break-inside:avoid}}"
        "</style><header><h1>Personal finance</h1><p>Provisional transaction analysis · "
        + escape(info["date_from"] + " to " + info["date_to"])
        + f"</p><p>{info['account_count']} accounts · {info['accounts_with_transactions']} with movements · {info['transaction_count']} records</p>"
        + "<p>Snapshot captured: "
        + escape(info["captured_at"])
        + "</p>"
        + "<p class='note'>"
        + escape(LIMITATIONS)
        + "</p>"
        + "<p>Dates use transaction date, then booking date, then value date. Amounts use charged currency. "
        + f"{info['missing_amount_count']} records have no charged amount and are omitted from monetary sums. "
        + "Original amounts are not substituted for missing charged amounts. "
        "No bank/card grand total or currency conversion is calculated. "
        "A sync replaces this snapshot with the requested window. Display amounts are rounded to two decimals; calculations retain exact decimals.</p>"
        + f"<p>Excluded: {info['duplicates_excluded']} provider duplicates, {info['repeated_ids_excluded']} repeated IDs, "
        + f"{info['outside_range_excluded']} records outside the selected date basis.</p></header>"
        + "".join(sections)
        + "</html>"
    )


def _write_atomic_html(content: str, output: Path) -> None:
    descriptor, temporary = tempfile.mkstemp(
        prefix=".finance-report-", dir=output.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_report(
    snapshot: dict[str, Any], output: Path, rules: Sequence[LiveTagRule] = ()
) -> None:
    # Explicit local export: aggregate HTML is plaintext, records remain encrypted.
    _write_atomic_html(report_html(snapshot, rules), output)


def _display_money(value: Decimal, currency: str) -> str:
    with localcontext() as context:
        context.prec = 410
        if currency == "ILS":
            return format(value / 1000, ",.1f")
        return format(value, ",.0f" if currency in {"USD", "EUR"} else ",.2f")


def simple_report_html(
    snapshot: dict[str, Any],
    rules: Sequence[LiveTagRule] = (),
    rates: dict[tuple[str, str], Decimal] | None = None,
) -> str:
    """Twelve completed months in shekels, using saved month-end exchange rates."""
    info = snapshot["info"]
    rate_values = rates or {}
    source_snapshot = snapshot
    month_keys = set(general_category_trend(snapshot, rules)["months"])
    snapshot = {
        **snapshot,
        "records": [
            shekel_record(record, rate_values)
            for record in snapshot["records"]
            if record["day"][:7] in month_keys
        ],
    }
    trend = general_category_trend(snapshot, rules)
    travel_label = GENERAL_CATEGORIES[-1][0]
    labels = [label for label, _ in GENERAL_CATEGORIES[:-1]] + [
        OTHER_CATEGORY_LABEL,
        UNIDENTIFIED_CATEGORY_LABEL,
    ]
    account_labels = {
        identity: f"חשבון {index}"
        for index, identity in enumerate(
            sorted({r["account_id"] for r in snapshot["records"]}), 1
        )
    }

    def money(value: Decimal | None, currency: str) -> str:
        if value is None:
            return "לא ידוע"
        # Exact amounts remain available for investigating small fees rounded to 0.0k.
        return f"<span dir='ltr' title='{escape(currency)} {value:,.2f}'>{_display_money(value, currency)}</span>"

    def units(currency: str) -> str:
        return {
            "ILS": "אלפי ₪ · ספרה אחת אחרי הנקודה",
            "USD": "דולר · ללא ספרות אחרי הנקודה",
            "EUR": "אירו · ללא ספרות אחרי הנקודה",
        }.get(currency, currency)

    trend_sections = []
    for currency, months in trend["currencies"].items():
        rows = []
        for key in trend["months"]:
            coverage = trend["coverage"][key]
            records = [
                r
                for r in snapshot["records"]
                if r["currency"] == currency and r["day"].startswith(key + "-")
            ]
            month_label = escape(_hebrew_month_label(key))
            if coverage == "missing" or not records:
                message = (
                    "לא יובאו נתונים"
                    if coverage == "missing"
                    else "אין תנועות במידע שהתקבל"
                )
                rows.append(
                    f"<tr><td>{month_label}</td><td colspan='{len(labels) + 3}'>{message}</td></tr>"
                )
                continue
            if coverage == "partial":
                month_label += " <small>(חלקי)</small>"
            unresolved = 0
            for record in records:
                amount = (
                    Decimal(record["amount"]) if record["amount"] is not None else None
                )
                reason, _ = _reconcile_reason(
                    record, amount, resolve_tag(record, rules)
                )
                unresolved += reason.startswith("unresolved_") or reason in {
                    "missing_amount",
                    "not_booked",
                }
            rows.append(
                f"<tr><td>{month_label}</td>"
                + "".join(
                    f"<td>{money(months[key]['categories'].get(label, Decimal(0)), currency)}</td>"
                    for label in labels
                )
                + f"<td><strong>{money(months[key]['total_excluding_travel'], currency)}</strong></td>"
                + f"<td>{money(months[key]['categories'].get(travel_label, Decimal(0)), currency)}</td>"
                + f"<td>{unresolved if unresolved else '—'}</td></tr>"
            )
        trend_sections.append(
            f"<h3>{escape(units(currency))}</h3><div class='scroll'><table><thead><tr><th>חודש</th>"
            + "".join(f"<th>{escape(label)}</th>" for label in labels)
            + f"<th><strong>סה״כ ללא טיולים בחו״ל</strong></th><th>{escape(travel_label)}</th>"
            + "<th>תנועות שלא נכללו ודורשות בירור</th></tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table></div>"
        )
    if not trend_sections:
        trend_sections = ["<p>אין נתונים להצגת מגמה חודשית.</p>"]

    last_month = _latest_reviewable_month(info)
    last_month_records = [
        r for r in snapshot["records"] if r["day"].startswith(last_month + "-")
    ]
    # Detect changes in original currencies so exchange-rate movements are not
    # mistaken for merchant price increases. Convert only the displayed amounts.
    original_records = [
        r for r in source_snapshot["records"] if r["day"].startswith(last_month + "-")
    ]
    original_history = [
        r for r in source_snapshot["records"] if r["day"] < last_month + "-01"
    ]
    flagged = _flag_debits(original_records, rules, original_history)
    flagged = [
        {**r, "amount": Decimal(r["amount"]) if r["amount"] is not None else None}
        for item in flagged
        for r in [shekel_record(item, rate_values)]
    ]
    if flagged:
        flagged_html = (
            "<div class='scroll'><table><thead><tr><th>תאריך</th><th>חשבון</th><th>בית עסק / נושא</th>"
            "<th>יחידות</th><th>סכום</th><th>למה לבדוק</th></tr></thead><tbody>"
            + "".join(
                "<tr>"
                f"<td dir='ltr'>{escape(item['day'])}</td>"
                f"<td>{account_labels[item['account_id']]} · {escape(ACCOUNT_TYPE_LABELS_HE.get(item['account_type'], item['account_type']))}</td>"
                f"<td>{escape(item.get('merchant') or _general_category(item['category'], item['subcategory'], item.get('merchant_country', ''), item.get('merchant', '')))}"
                "<details><summary>פרטי זיהוי</summary>"
                f"<p dir='ltr'>{escape(item['category'])} / {escape(item['subcategory'])}<br>"
                f"{escape(item['id'])}<br>{escape(item['account_id'])}</p></details></td>"
                f"<td>{escape(units(item['currency']).split(' · ')[0])}</td>"
                f"<td>{money(item['amount'].copy_negate() if item['amount'] is not None else None, item['currency'])}</td>"
                f"<td>{escape(' · '.join(REASON_LABELS_HE[r] for r in item['reasons']))}</td></tr>"
                for item in flagged
            )
            + "</tbody></table></div>"
        )
    elif trend["coverage"][last_month] == "missing" or not last_month_records:
        flagged_html = "<p>אין נתונים לחודש הזה; אי אפשר לבדוק חיובים.</p>"
    else:
        flagged_html = "<p>לא סומנו תנועות לפי הבדיקות הזמינות. אין בכך אישור שכל החיובים תקינים.</p>"
    coverage_note = ""
    if any(value != "full" for value in trend["coverage"].values()):
        coverage_note = "<p class='note'>המידע אינו מכסה שנה מלאה. חודשים חסרים מסומנים, וחודש חלקי אינו סיכום של חודש שלם.</p>"
    if trend["coverage"][last_month] == "partial":
        flagged_html = (
            "<p class='note'>הבדיקה לחודש הזה חלקית: לא יובאו כל ימי החודש.</p>"
            + flagged_html
        )

    def full_money(value: Decimal) -> str:
        return f"<span dir='ltr' title='₪ {value:,.4f}'>{value:,.2f} ₪</span>"

    # Original currencies, so exchange-rate moves don't break price stability.
    recurring = recurring_merchants(
        [r for r in source_snapshot["records"] if r["day"][:7] in month_keys], rules
    )

    def is_subscription(record: dict[str, Any]) -> bool:
        if _is_fee(record) or _is_insurance(record) or _is_standing_order(record):
            return False
        return (
            record["category"] == "SUBSCRIPTIONS"
            or record.get("merchant", "") in recurring
        )

    top_category_defs: tuple[tuple[str, Callable[[dict[str, Any]], bool]], ...] = (
        ("עמלות", _is_fee),
        ("ביטוחים", _is_insurance),
        ("מנויים וחיובים חוזרים", is_subscription),
        ("הוראות קבע", _is_standing_order),
    )
    top_monthly_totals = {
        top_label: _predicate_monthly_totals(
            snapshot["records"], trend["months"], rules, predicate
        )
        for top_label, predicate in top_category_defs
    }
    top_trend_rows = []
    for key in trend["months"]:
        coverage = trend["coverage"][key]
        month_label = escape(_hebrew_month_label(key))
        if coverage == "missing":
            top_trend_rows.append(
                f"<tr><td>{month_label}</td>"
                f"<td colspan='{len(top_category_defs)}'>לא יובאו נתונים</td></tr>"
            )
            continue
        if coverage == "partial":
            month_label += " <small>(חלקי)</small>"
        top_trend_rows.append(
            f"<tr><td>{month_label}</td>"
            + "".join(
                f"<td>{full_money(top_monthly_totals[top_label][key])}</td>"
                for top_label, _ in top_category_defs
            )
            + "</tr>"
        )
    if trend["coverage"][last_month] == "missing" or not last_month_records:
        top_lists_html = "<p>אין נתונים לחודש הזה; אי אפשר להציג רשימות.</p>"
    else:
        top_list_sections = []
        for top_label, predicate in top_category_defs:
            subjects = expense_subjects(last_month_records, rules, predicate)
            top_ten = subjects[:10]
            if top_ten:
                list_table = (
                    "<div class='scroll'><table><thead><tr><th>בית עסק / נושא</th>"
                    "<th>מספר חיובים</th><th>סכום</th><th>תאריכים</th><th>חשבון</th>"
                    "</tr></thead><tbody>"
                    + "".join(
                        f"<tr><td>{escape(item['subject'])}</td>"
                        f"<td>{item['count']}</td>"
                        f"<td>{full_money(item['total'])}</td>"
                        f"<td dir='ltr'>{escape(', '.join(sorted(day[8:] + '/' + day[5:7] for day in item['days'])))}</td>"
                        f"<td>{escape(', '.join(sorted(account_labels[a] for a in item['account_ids'])))}</td></tr>"
                        for item in top_ten
                    )
                    + "</tbody></table></div>"
                )
            else:
                list_table = "<p>אין חיובים מסוג זה בחודש הזה.</p>"
            top_list_sections.append(
                f"<h3>{escape(top_label)}</h3>"
                f"<p>סה״כ בחודש: {full_money(top_monthly_totals[top_label][last_month])} · "
                f"{len(subjects)} בתי עסק/נושאים שונים.</p>" + list_table
            )
        top_lists_html = "".join(top_list_sections)
        if trend["coverage"][last_month] == "partial":
            top_lists_html = (
                "<p class='note'>הבדיקה לחודש הזה חלקית: לא יובאו כל ימי החודש.</p>"
                + top_lists_html
            )
    top_categories_section = (
        "<section><h2>עמלות, ביטוחים, מנויים והוראות קבע</h2>"
        "<p>מגמה חודשית, ועשרת הפריטים היקרים ביותר מכל סוג בחודש האחרון שהסתיים "
        f"({escape(_hebrew_month_label(last_month))}), מסודרים מהיקר לזול. "
        "הסכומים בסעיף זה בשקלים מלאים, לא באלפים.</p>"
        "<p class='note'>Financy לא מסמן מנויים, ולכן ״מנויים וחיובים חוזרים״ הם הערכה: "
        "בית עסק שחויב בערך פעם בחודש, בסכום יציב, ב-3 חודשים לפחות. "
        "להוראות קבע Financy לא מוסר שם מוטב, ולכן הן מזוהות לפי תאריך וחשבון.</p>"
        "<div class='scroll'><table><thead><tr><th>חודש</th>"
        + "".join(f"<th>{escape(top_label)}</th>" for top_label, _ in top_category_defs)
        + "</tr></thead><tbody>"
        + "".join(top_trend_rows)
        + "</tbody></table></div>"
        + top_lists_html
        + "</section>"
    )
    return (
        "<!doctype html><html lang='he' dir='rtl'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>דוח הוצאות חודשי</title><style>"
        "body{font:16px system-ui,sans-serif;background:#f4f6f8;color:#172536;max-width:1400px;margin:32px auto;padding:0 20px}"
        "section,header{background:white;border:1px solid #dae1e8;border-radius:12px;padding:24px;margin-bottom:20px}"
        "h1{margin-top:0}h3{font-size:16px}table{border-collapse:collapse;width:100%;font-size:14px}"
        "th,td{padding:10px;text-align:right;border-bottom:1px solid #e7edf2}th{background:#f7f9fb}"
        "td:first-child{white-space:nowrap}.scroll{overflow-x:auto}small{color:#637080}"
        ".note{border-right:4px solid #c38314;padding:12px;background:#fff8e9;line-height:1.6;font-size:14px}"
        "summary{cursor:pointer;font-size:12px}p{line-height:1.6}"
        "@media print{body{background:white;margin:0;font-size:12px}section{padding:8px}.scroll{overflow:visible}th,td{padding:5px;font-size:10px}}"
        "</style><header><h1>דוח הוצאות חודשי</h1>"
        f"<p>12 חודשים שהסתיימו ב{escape(_hebrew_month_label(last_month))}. נתונים שנמשכו: {escape(info['date_from'])} עד {escape(info['date_to'])}.</p>"
        "<p>הסכום המודגש אינו כולל טיולים בחו״ל. הוצאות ללא קטגוריה מופיעות ב״לא מזוהה״ ונכללות בסכום; תנועות שטרם הוגדרו כהוצאה אינן כלולות.</p>"
        + coverage_note
        + "</header><section><h2>הוצאות לפי חודש וקטגוריה</h2>"
        + "".join(trend_sections)
        + "<details><summary>איך לקרוא את הסכומים</summary><p>חיוב הכרטיס בעו״ש, העברות, תנועות השקעה וזיכויים לא מזוהים ממתינים לבירור ואינם נספרים כהוצאה. "
        "חיובים ממתינים וסכומים חסרים אינם כלולים. סימון אישי כהוצאה גובר על סיווג זה; זיכוי שסומן כהוצאה מפחית את הסכום. "
        "הסכומים בטבלה מעוגלים לתצוגה בלבד; הסכום המדויק מופיע בהצבעה על מספר. "
        "מטבע זר מומר לפי השער היציג האחרון שפרסם בנק ישראל עד סוף חודש העסקה. "
        "השער נשמר ואינו מתעדכן בהפקות חוזרות. ימי המשיכה אינם מבטיחים שהבנק מסר היסטוריה מלאה.</p></details></section>"
        f"<section><h2>חיובים ותנועות לבירור — {escape(_hebrew_month_label(last_month))}</h2>"
        "<p>עמלות, ביטוחים, שינויים לעומת העבר ותנועות לא מזוהות. הסימון הוא סיבה לבדיקה, לא קביעה שנפלה טעות.</p>"
        + flagged_html
        + "</section>"
        + top_categories_section
        + "</html>"
    )


def write_simple_report(
    snapshot: dict[str, Any],
    output: Path,
    rules: Sequence[LiveTagRule] = (),
    rates: dict[tuple[str, str], Decimal] | None = None,
) -> None:
    _write_atomic_html(simple_report_html(snapshot, rules, rates), output)
