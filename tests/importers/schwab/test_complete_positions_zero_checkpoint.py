import json
from datetime import date
from decimal import Decimal

import pytest

from opensteuerauszug.config.models import SchwabAccountSettings
from opensteuerauszug.core.position_reconciler import PositionReconciler
from opensteuerauszug.importers.schwab.schwab_importer import SchwabImporter

ACCOUNT_NUMBER = "SYNTHETIC-123"


def _write_json(path, from_date, to_date, transactions):
    path.write_text(
        json.dumps(
            {
                "FromDate": from_date,
                "ToDate": to_date,
                "BrokerageTransactions": transactions,
            }
        ),
        encoding="utf-8",
    )


def _write_complete_positions_csv(path, as_of_date="2026/01/10"):
    path.write_text(
        f'"Positions for account Individual ...123 as of 09:00 AM ET, {as_of_date}"\n'
        '""\n'
        '"Symbol","Description","Qty (Quantity)","Price","Mkt Val (Market Value)","Asset Type"\n'
        '"Cash & Cash Investments","",,"","$1,340.00",""\n'
        '"HELD","Still Held",2,"$100.00","$200.00","Equity"\n',
        encoding="utf-8",
    )


def _importer():
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
        strict_consistency=True,
    )


def _write_contiguous_exports(directory, end_2026="01/10/2026"):
    transactions_2025 = directory / "Individual_XXX123_Transactions_20250101-000000.json"
    _write_json(
        transactions_2025,
        "01/01/2025",
        "12/31/2025",
        [
            {
                "Date": "06/15/2025",
                "Action": "Buy",
                "Symbol": "CLOSED2025",
                "Description": "Closed During 2025",
                "Quantity": "2",
                "Price": "$50.00",
                "Amount": "-$100.00",
            },
            {
                "Date": "06/17/2025",
                "Action": "Sale",
                "Symbol": "CLOSED2025",
                "Description": "Closed During 2025",
                "Quantity": "2",
                "Price": "$60.00",
                "Amount": "$120.00",
            },
            {
                "Date": "06/20/2025",
                "Action": "Dividend",
                "Symbol": "CLOSED",
                "Description": "Closed Synthetic Holding",
                "Quantity": "",
                "Price": "",
                "Amount": "$10.00",
            },
        ],
    )
    transactions_2026 = directory / "Individual_XXX123_Transactions_20260101-000000.json"
    _write_json(
        transactions_2026,
        "01/01/2026",
        end_2026,
        [
            {
                "Date": "01/01/2026",
                "Action": "Sale",
                "Symbol": "CLOSED",
                "Description": "Closed Synthetic Holding",
                "Quantity": "5",
                "Price": "$120.00",
                "Amount": "$600.00",
            },
            {
                "Date": "01/02/2026",
                "Action": "Dividend",
                "Symbol": "CLOSED",
                "Description": "Closed Synthetic Holding",
                "Quantity": "",
                "Price": "",
                "Amount": "$10.00",
            },
        ],
    )
    return transactions_2025, transactions_2026


def test_complete_positions_snapshot_reconciles_omitted_closed_symbol_backwards(
    tmp_path, monkeypatch
):
    transactions_2025, transactions_2026 = _write_contiguous_exports(tmp_path)
    positions = tmp_path / "positions.csv"
    _write_complete_positions_csv(positions)
    checkpoint_stocks = []
    original_init = PositionReconciler.__init__

    def capture_checkpoint_stocks(self, initial_stocks, identifier="UnknownPosition"):
        if identifier == "123-CLOSED":
            checkpoint_stocks.extend(
                stock
                for stock in initial_stocks
                if stock.name == "Complete Schwab positions snapshot: omitted symbol"
            )
        original_init(self, initial_stocks, identifier)

    monkeypatch.setattr(PositionReconciler, "__init__", capture_checkpoint_stocks)

    statement = _importer().import_files(
        [str(transactions_2025), str(transactions_2026), str(positions)]
    )

    assert len(checkpoint_stocks) == 1
    assert statement.listOfSecurities is not None
    (depot,) = statement.listOfSecurities.depot
    (security,) = [security for security in depot.security if security.symbol == "CLOSED"]
    assert [
        stock.quantity
        for stock in security.stock
        if not stock.mutation and stock.referenceDate == date(2025, 1, 1)
    ] == [Decimal("5")]
    assert [
        stock.quantity
        for stock in security.stock
        if not stock.mutation and stock.referenceDate == date(2026, 1, 1)
    ] == [Decimal("5")]
    assert all(
        stock.referenceDate <= date(2025, 12, 31) for stock in security.stock if stock.mutation
    )
    assert all(payment.paymentDate <= date(2025, 12, 31) for payment in security.payment)
    assert [payment.amount for payment in security.payment] == [Decimal("10.00")]
    (closed_during_2025,) = [
        security for security in depot.security if security.symbol == "CLOSED2025"
    ]
    assert [
        stock.quantity
        for stock in closed_during_2025.stock
        if not stock.mutation and stock.referenceDate == date(2025, 1, 1)
    ] == [Decimal("0")]
    assert [
        stock.quantity
        for stock in closed_during_2025.stock
        if not stock.mutation and stock.referenceDate == date(2026, 1, 1)
    ] == [Decimal("0")]

    assert statement.listOfBankAccounts is not None
    (cash_account,) = statement.listOfBankAccounts.bankAccount
    assert cash_account.taxValue is not None
    assert cash_account.taxValue.balance == Decimal("730.00")
    assert all(payment.paymentDate <= date(2025, 12, 31) for payment in cash_account.payment)


def test_complete_positions_snapshot_requires_continuous_coverage_to_its_date(tmp_path):
    transactions_2025, transactions_2026 = _write_contiguous_exports(
        tmp_path, end_2026="01/08/2026"
    )
    positions = tmp_path / "positions.csv"
    _write_complete_positions_csv(positions)

    with pytest.raises(ValueError):
        _importer().import_files([str(transactions_2025), str(transactions_2026), str(positions)])


def _write_unbalanced_share_exports(directory):
    transactions_2025 = directory / "Individual_XXX123_Transactions_20250101-000000.json"
    _write_json(
        transactions_2025,
        "01/01/2025",
        "12/31/2025",
        [
            {
                "Date": "06/15/2025",
                "Action": "Journaled Shares",
                "Symbol": "CLOSED",
                "Description": "Closed Synthetic Holding",
                "Quantity": "5",
            }
        ],
    )
    transactions_2026 = directory / "Individual_XXX123_Transactions_20260101-000000.json"
    _write_json(
        transactions_2026,
        "01/01/2026",
        "01/10/2026",
        [
            {
                "Date": "01/01/2026",
                "Action": "Journaled Shares",
                "Symbol": "CLOSED",
                "Description": "Closed Synthetic Holding",
                "Quantity": "-6",
            }
        ],
    )
    return transactions_2025, transactions_2026


@pytest.mark.parametrize("checkpoint_kind", ["malformed_primary", "fallback"])
def test_noncomplete_checkpoint_cannot_infer_an_omitted_symbol_zero(tmp_path, checkpoint_kind):
    transactions_2025, transactions_2026 = _write_unbalanced_share_exports(tmp_path)
    if checkpoint_kind == "malformed_primary":
        checkpoint = tmp_path / "positions.csv"
        checkpoint.write_text(
            '"Positions for account Individual ...123 as of 09:00 AM ET, 2026/01/10"\n'
            '""\n'
            '"Symbol","Description","Qty (Quantity)","Price","Mkt Val (Market Value)","Asset Type"\n'
            '"CLOSED","Closed Synthetic Holding","not-a-number","$100.00","$0.00","Equity"\n',
            encoding="utf-8",
        )
    else:
        checkpoint = tmp_path / "manual.csv"
        checkpoint.write_text(
            "Depot,Date,Symbol,Quantity\n123,2026-01-11,HELD,2\n", encoding="utf-8"
        )

    error_message = (
        "nonnumeric quantity"
        if checkpoint_kind == "malformed_primary"
        else "Mutation-only consistency check failed"
    )
    with pytest.raises(ValueError, match=error_message):
        _importer().import_files([str(transactions_2025), str(transactions_2026), str(checkpoint)])


def test_complete_positions_snapshot_from_another_account_cannot_supply_a_zero(tmp_path):
    transactions_2025, transactions_2026 = _write_unbalanced_share_exports(tmp_path)
    checkpoint = tmp_path / "other-account-positions.csv"
    checkpoint.write_text(
        '"Positions for account Individual ...999 as of 09:00 AM ET, 2026/01/10"\n'
        '""\n'
        '"Symbol","Description","Qty (Quantity)","Price","Mkt Val (Market Value)","Asset Type"\n'
        '"HELD","Still Held",2,"$100.00","$200.00","Equity"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="No statement date in the maximal covered range"):
        _importer().import_files([str(transactions_2025), str(transactions_2026), str(checkpoint)])
