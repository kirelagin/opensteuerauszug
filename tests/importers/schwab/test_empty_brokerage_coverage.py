import json
from datetime import date
from decimal import Decimal

import pytest

from opensteuerauszug.config.models import SchwabAccountSettings
from opensteuerauszug.importers.schwab.schwab_importer import SchwabImporter
from opensteuerauszug.importers.schwab.transaction_extractor import TransactionExtractor

ACCOUNT_NUMBER = "SYNTHETIC-123"


def _write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def _brokerage_export(from_date, to_date, transactions):
    return {
        "FromDate": from_date,
        "ToDate": to_date,
        "BrokerageTransactions": transactions,
    }


def _write_positions_checkpoint(path):
    path.write_text(
        '"Positions for account Individual ...123 as of 09:00 AM ET, 2026/05/11"\n'
        '""\n'
        '"Symbol","Description","Qty (Quantity)","Price","Mkt Val (Market Value)","Asset Type"\n'
        '"Cash & Cash Investments","",,"","$900.00",""\n'
        '"SYNTH","Synthetic Holdings",2,"$100.00","$200.00","Equity"\n',
        encoding="utf-8",
    )


def _configured_importer():
    return SchwabImporter(
        period_from=date(2025, 1, 1),
        period_to=date(2025, 12, 31),
        account_settings_list=[
            SchwabAccountSettings(
                account_number=ACCOUNT_NUMBER,
                account_name_alias="synthetic",
                broker_name="schwab",
                canton="ZH",
                full_name="Synthetic Test",
            )
        ],
    )


def _write_populated_2025_export(directory):
    path = directory / "Individual_XXX123_Transactions_20250101-000000.json"
    _write_json(
        path,
        _brokerage_export(
            "01/01/2025",
            "12/31/2025",
            [
                {
                    "Date": "06/15/2025",
                    "Action": "Buy",
                    "Symbol": "SYNTH",
                    "Description": "Synthetic Holdings",
                    "Quantity": "2",
                    "Price": "$100.00",
                    "Amount": "-$200.00",
                }
            ],
        ),
    )
    return path


def test_empty_brokerage_export_extends_coverage_without_events(tmp_path):
    populated_2025 = _write_populated_2025_export(tmp_path)
    empty_2026 = tmp_path / "Individual_XXX123_Transactions_20260101-000000.json"
    _write_json(empty_2026, _brokerage_export("01/01/2026", "05/11/2026", []))
    positions_checkpoint = tmp_path / "positions.csv"
    _write_positions_checkpoint(positions_checkpoint)

    statement = _configured_importer().import_files(
        [str(populated_2025), str(empty_2026), str(positions_checkpoint)]
    )

    assert statement.listOfSecurities is not None
    (depot,) = statement.listOfSecurities.depot
    assert str(depot.depotNumber) == ACCOUNT_NUMBER
    (security,) = depot.security
    assert security.symbol == "SYNTH"
    assert security.payment == []
    assert [stock.quantity for stock in security.stock if stock.mutation] == [Decimal("2")]
    assert [
        stock.quantity
        for stock in security.stock
        if not stock.mutation and stock.referenceDate == date(2026, 1, 1)
    ] == [Decimal("2")]

    assert statement.listOfBankAccounts is not None
    (cash_account,) = statement.listOfBankAccounts.bankAccount
    assert cash_account.taxValue is not None
    assert cash_account.taxValue.balance == Decimal("900.00")
    assert cash_account.payment == []


@pytest.mark.parametrize(
    ("filename", "data"),
    [
        ("Individual_XXX123_Transactions_20260101-000000.json", {"BrokerageTransactions": []}),
        (
            "Individual_XXX123_Transactions_20260101-000000.json",
            _brokerage_export("not-a-date", "05/11/2026", []),
        ),
        (
            "Individual_XXX123_Transactions_20260101-000000.json",
            _brokerage_export("05/11/2026", "01/01/2026", []),
        ),
        (
            "Individual_XXX123_Transactions_20260101-000000.json",
            {"FromDate": "01/01/2026", "ToDate": "05/11/2026", "Unrecognized": []},
        ),
        (
            "EquityAwardsCenter_Transactions_20260101-000000.json",
            {"FromDate": "01/01/2026", "ToDate": "05/11/2026", "Transactions": []},
        ),
        (
            "Individual_without-account_Transactions_20260101-000000.json",
            _brokerage_export("01/01/2026", "05/11/2026", []),
        ),
    ],
)
def test_empty_or_invalid_exports_cannot_create_coverage(filename, data):
    assert TransactionExtractor(filename)._extract_transactions_from_dict(data) is None


@pytest.mark.parametrize(
    "empty_filename", [None, "Individual_XXX999_Transactions_20260101-000000.json"]
)
def test_missing_or_mismatched_empty_export_cannot_cover_positions_checkpoint(
    tmp_path, empty_filename
):
    populated_2025 = _write_populated_2025_export(tmp_path)
    positions_checkpoint = tmp_path / "positions.csv"
    _write_positions_checkpoint(positions_checkpoint)
    filenames = [str(populated_2025), str(positions_checkpoint)]

    if empty_filename is not None:
        mismatched_empty = tmp_path / empty_filename
        _write_json(mismatched_empty, _brokerage_export("01/01/2026", "05/11/2026", []))
        filenames.append(str(mismatched_empty))

    with pytest.raises(ValueError, match="No statement date in the maximal covered range"):
        _configured_importer().import_files(filenames)
