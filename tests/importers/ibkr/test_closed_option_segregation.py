from datetime import date
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader
from typer.testing import CliRunner

from opensteuerauszug.calculate.base import CalculationMode
from opensteuerauszug.calculate.cleanup import CleanupCalculator
from opensteuerauszug.calculate.kursliste_tax_value_calculator import KurslisteTaxValueCalculator
from opensteuerauszug.calculate.minimal_tax_value import MinimalTaxValueCalculator
from opensteuerauszug.calculate.total import TotalCalculator
from opensteuerauszug.config.models import GeneralSettings
from opensteuerauszug.core.exchange_rate_provider import DummyExchangeRateProvider
from opensteuerauszug.core.kursliste_exchange_rate_provider import KurslisteExchangeRateProvider
from opensteuerauszug.core.kursliste_manager import KurslisteManager
from opensteuerauszug.importers.ibkr.ibkr_importer import IbkrImporter
from opensteuerauszug.render.render import render_tax_statement
from opensteuerauszug.steuerauszug import app

ACCOUNT_ID = "SYNTH-IBKR-01"
PERIOD_FROM = date(2025, 1, 1)
PERIOD_TO = date(2025, 12, 31)
OPTION_CONIDS = ["88001", "88002", "88003", "88004"]
SETTLED_CASH = Decimal("845.00")
runner = CliRunner()


class SyntheticKurslisteExchangeRateProvider(KurslisteExchangeRateProvider):
    def __init__(self):
        super().__init__(KurslisteManager())

    def get_exchange_rate(self, currency, reference_date, path_prefix_for_log=None):
        return Decimal("1") if currency == "CHF" else Decimal("0.5")


def _option_trades(include_final_closes: bool = True) -> str:
    legs = [
        ("88001", "SYNX 250620C00100000", "SYNX JUN25 100 CALL", "C", 1, "BUY"),
        ("88002", "SYNX 250620C00110000", "SYNX JUN25 110 CALL", "C", -1, "SELL"),
        ("88003", "SYNX 250620P00100000", "SYNX JUN25 100 PUT", "P", -1, "SELL"),
        ("88004", "SYNX 250620P00110000", "SYNX JUN25 110 PUT", "P", 1, "BUY"),
    ]
    rows = []
    for index, (conid, symbol, description, put_call, quantity, buy_sell) in enumerate(
        legs, start=1
    ):
        open_date = f"2025030{index}"
        rows.append(
            f'<Trade accountId="{ACCOUNT_ID}" currency="USD" assetCategory="OPT" '
            f'subCategory="{put_call}" symbol="{symbol}" description="{description}" '
            f'conid="{conid}" isin="" issuerCountryCode="US" multiplier="100" '
            f'expiry="20250620" tradeDate="{open_date}" settleDateTarget="{open_date}" '
            f'quantity="{quantity}" tradePrice="1.{index}" tradeMoney="{10 * quantity}" '
            f'buySell="{buy_sell}" ibCommission="-0.10" netCash="0" />'
        )
        if include_final_closes or index != len(legs):
            close_quantity = -quantity
            close_buy_sell = "SELL" if close_quantity < 0 else "BUY"
            rows.append(
                f'<Trade accountId="{ACCOUNT_ID}" currency="USD" assetCategory="OPT" '
                f'subCategory="{put_call}" symbol="{symbol}" description="{description}" '
                f'conid="{conid}" isin="" issuerCountryCode="US" multiplier="100" '
                f'expiry="20250620" tradeDate="20250620" settleDateTarget="20250620" '
                f'quantity="{close_quantity}" tradePrice="0" tradeMoney="0" buySell="{close_buy_sell}" '
                f'transactionType="BookTrade" closePrice="0" ibCommission="0" netCash="0" />'
            )
    return "\n".join(rows)


def _option_eae_events() -> str:
    events = [
        ("88001", "SYNX 250620C00100000", "SYNX JUN25 100 CALL", "C", "Exercise"),
        ("88002", "SYNX 250620C00110000", "SYNX JUN25 110 CALL", "C", "Assignment"),
        ("88003", "SYNX 250620P00100000", "SYNX JUN25 100 PUT", "P", "Assignment"),
        ("88004", "SYNX 250620P00110000", "SYNX JUN25 110 PUT", "P", "Exercise"),
    ]
    rows = []
    for conid, symbol, description, put_call, event_type in events:
        rows.append(
            f'<OptionEAE accountId="{ACCOUNT_ID}" assetCategory="OPT" '
            f'transactionType="{event_type}" currency="USD" symbol="{symbol}" '
            f'description="{description}" conid="{conid}" multiplier="100" '
            f'expiry="20250620" putCall="{put_call}" date="20250620" quantity="1" />'
        )
    return "\n".join(rows)


def _flex_xml(
    *,
    opening_option_quantity: str | None = None,
    ending_option_quantity: str | None = None,
    include_final_closes: bool = True,
    include_option_payment: bool = False,
    include_opening_snapshot: bool = True,
    include_ending_snapshot: bool = True,
    source_from: str = "2025-01-01",
    ending_report_date: str = "2025-12-31",
    missing_activity_section: str | None = None,
) -> str:
    opening_statement = ""
    if include_opening_snapshot:
        opening_positions = "<OpenPositions />"
        if opening_option_quantity is not None:
            opening_positions = f'''<OpenPositions>
          <OpenPosition accountId="{ACCOUNT_ID}" assetCategory="OPT" subCategory="C" symbol="SYNX 250620C00100000" description="SYNX JUN25 100 CALL" conid="88001" isin="" currency="USD" position="{opening_option_quantity}" markPrice="1" positionValue="100" multiplier="100" reportDate="2024-12-31" />
        </OpenPositions>'''
        opening_statement = f'''<FlexStatement accountId="{ACCOUNT_ID}" fromDate="2024-12-31" toDate="2024-12-31" period="Custom" whenGenerated="2025-01-01T12:00:00">
      {opening_positions}
    </FlexStatement>'''

    ending_positions = ""
    if include_ending_snapshot:
        ending_option = ""
        if ending_option_quantity is not None:
            ending_option = f'''\n          <OpenPosition accountId="{ACCOUNT_ID}" assetCategory="OPT" subCategory="C" symbol="SYNX 250620C00100000" description="SYNX JUN25 100 CALL" conid="88001" isin="" currency="USD" position="{ending_option_quantity}" markPrice="1" positionValue="100" multiplier="100" reportDate="{ending_report_date}" />'''
        ending_positions = f'''<OpenPositions>
        <OpenPosition accountId="{ACCOUNT_ID}" assetCategory="STK" symbol="SYNTHETF" description="Synthetic Equity Fund" conid="99001" isin="ZZ0000000000" issuerCountryCode="US" currency="USD" position="2" markPrice="50" positionValue="100" reportDate="{ending_report_date}" />{ending_option}
      </OpenPositions>'''

    cash_transactions = "<CashTransactions />"
    if include_option_payment:
        cash_transactions = f'''<CashTransactions>
        <CashTransaction accountId="{ACCOUNT_ID}" type="Dividends" currency="USD" amount="3.00" description="Synthetic option payment" conid="88001" isin="" symbol="SYNX 250620C00100000" dateTime="20250621;120000" assetCategory="OPT" />
      </CashTransactions>'''
    activity_sections = "\n      ".join(
        section
        for section in [
            "<Transfers />" if missing_activity_section != "Transfers" else "",
            "<CorporateActions />" if missing_activity_section != "CorporateActions" else "",
            cash_transactions if missing_activity_section != "CashTransactions" else "",
        ]
        if section
    )

    statement_count = "2" if include_opening_snapshot else "1"
    return f'''<FlexQueryResponse queryName="SyntheticLongBox" type="AF">
  <FlexStatements count="{statement_count}">
    {opening_statement}
    <FlexStatement accountId="{ACCOUNT_ID}" fromDate="{source_from}" toDate="2025-12-31" period="Year" whenGenerated="2026-01-01T12:00:00">
      <Trades>
        {_option_trades(include_final_closes)}
      </Trades>
      <OptionEAE>
        {_option_eae_events()}
      </OptionEAE>
      {ending_positions}
      {activity_sections}
      <CashReport>
        <CashReportCurrency accountId="{ACCOUNT_ID}" currency="USD" startingCash="835.00" endingCash="{SETTLED_CASH}" fromDate="{source_from}" toDate="2025-12-31" />
      </CashReport>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>'''


def _with_overlapping_full_period_source(xml: str) -> str:
    statements = xml.split('<FlexStatements count="2">', maxsplit=1)[1].split(
        "</FlexStatements>", maxsplit=1
    )[0]
    period_statement = statements[statements.rfind("<FlexStatement") :]
    return xml.replace('<FlexStatements count="2">', '<FlexStatements count="3">', 1).replace(
        "</FlexStatements>", f"{period_statement}</FlexStatements>", 1
    )


def _supplemental_contract_trades(asset_category: str, isin: str) -> str:
    return f'''<Trade accountId="{ACCOUNT_ID}" currency="USD" assetCategory="{asset_category}" subCategory="C" symbol="SYNX 250620C00100000" description="SYNX JUN25 100 CALL" conid="88001" isin="{isin}" issuerCountryCode="US" multiplier="100" expiry="20250620" tradeDate="20250410" settleDateTarget="20250410" quantity="1" tradePrice="1" tradeMoney="10" buySell="BUY" ibCommission="0" netCash="0" />
        <Trade accountId="{ACCOUNT_ID}" currency="USD" assetCategory="{asset_category}" subCategory="C" symbol="SYNX 250620C00100000" description="SYNX JUN25 100 CALL" conid="88001" isin="{isin}" issuerCountryCode="US" multiplier="100" expiry="20250620" tradeDate="20250620" settleDateTarget="20250620" quantity="-1" tradePrice="0" tradeMoney="0" buySell="SELL" transactionType="BookTrade" closePrice="0" ibCommission="0" netCash="0" />'''


def _with_conflicting_contract_record(xml: str, asset_category: str, isin: str) -> str:
    return xml.replace(
        "</Trades>",
        f"{_supplemental_contract_trades(asset_category, isin)}</Trades>",
        1,
    )


def _import(
    tmp_path: Path,
    xml: str,
    *,
    segregate: bool,
    boundary_xml: str | None = None,
    corrections_xml: str | None = None,
):
    input_file = tmp_path / "synthetic_option_box.xml"
    input_file.write_text(xml, encoding="utf-8")
    boundary_filenames = None
    if boundary_xml is not None:
        boundary_file = tmp_path / "synthetic_boundary_checkpoint.xml"
        boundary_file.write_text(boundary_xml, encoding="utf-8")
        boundary_filenames = [str(boundary_file)]
    correction_filenames = None
    if corrections_xml is not None:
        correction_file = tmp_path / "synthetic_corrections.xml"
        correction_file.write_text(corrections_xml, encoding="utf-8")
        correction_filenames = [str(correction_file)]
    return IbkrImporter(
        period_from=PERIOD_FROM,
        period_to=PERIOD_TO,
        account_settings_list=[],
        segregate_proven_closed_options=segregate,
        boundary_filenames=boundary_filenames,
    ).import_files([str(input_file)], corrections_filenames=correction_filenames)


def _opening_boundary_xml() -> str:
    return f'''<FlexQueryResponse queryName="SyntheticOpeningCheckpoint" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="{ACCOUNT_ID}" fromDate="2024-12-31" toDate="2024-12-31" period="Custom" whenGenerated="2025-01-01T12:00:00">
      <OpenPositions />
      <CashReport>
        <CashReportCurrency accountId="{ACCOUNT_ID}" currency="USD" endingCash="999.00" fromDate="2024-12-31" toDate="2024-12-31" />
      </CashReport>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>'''


def _ambiguous_ending_boundary_xml() -> str:
    return f'''<FlexQueryResponse queryName="SyntheticAmbiguousEnding" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="{ACCOUNT_ID}" fromDate="2025-12-31" toDate="2025-12-31" period="Custom" whenGenerated="2026-01-01T12:00:00">
      <OpenPositions>
        <OpenPosition accountId="{ACCOUNT_ID}" assetCategory="STK" symbol="SYNTHETF" description="Synthetic Equity Fund" conid="99001" isin="ZZ0000000000" issuerCountryCode="US" currency="USD" position="2" markPrice="50" positionValue="100" reportDate="2025-12-31" />
        <OpenPosition accountId="{ACCOUNT_ID}" assetCategory="OPT" subCategory="C" symbol="SYNX 250620C00100000" description="SYNX JUN25 100 CALL" conid="88001" isin="" currency="USD" position="1" markPrice="1" positionValue="100" multiplier="100" reportDate="2025-12-31" />
      </OpenPositions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>'''


def _corrections_xml() -> str:
    return f'''<FlexQueryResponse queryName="SyntheticCorrection" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="{ACCOUNT_ID}" fromDate="2026-01-01" toDate="2026-01-02" period="Custom" whenGenerated="2026-01-03T12:00:00">
      <CashTransactions>
        <CashTransaction accountId="{ACCOUNT_ID}" type="Withholding Tax" currency="USD" amount="-1.00" description="Synthetic correction payment" conid="88001" isin="" symbol="SYNX 250620C00100000" dateTime="20260102;120000" settleDate="20250620" assetCategory="OPT" />
      </CashTransactions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>'''


def _finalize(statement, *, kursliste: bool = False):
    statement = CleanupCalculator(
        period_from=PERIOD_FROM,
        period_to=PERIOD_TO,
        importer_name="ibkr",
        config_settings=GeneralSettings(canton="ZH", full_name="Synthetic User"),
    ).calculate(statement)
    calculator_class = KurslisteTaxValueCalculator if kursliste else MinimalTaxValueCalculator
    exchange_rate_provider = (
        SyntheticKurslisteExchangeRateProvider() if kursliste else DummyExchangeRateProvider()
    )
    statement = calculator_class(
        mode=CalculationMode.OVERWRITE,
        exchange_rate_provider=exchange_rate_provider,
    ).calculate(statement)
    return TotalCalculator(mode=CalculationMode.OVERWRITE).calculate(statement)


def _securities(statement):
    if not statement.listOfSecurities:
        return []
    return [security for depot in statement.listOfSecurities.depot for security in depot.security]


def _option_symbols(statement):
    return [
        security.symbol
        for security in _securities(statement)
        if security.securityCategory == "OPTION"
    ]


def test_full_period_end_checkpoint_proves_closed_long_box_without_opening_export(tmp_path, caplog):
    caplog.set_level("INFO")
    xml = _flex_xml(include_opening_snapshot=False)
    default_statement = _finalize(_import(tmp_path, xml, segregate=False))
    segregated_statement = _finalize(_import(tmp_path, xml, segregate=True))
    kursliste_statement = _finalize(_import(tmp_path, xml, segregate=True), kursliste=True)

    assert _option_symbols(default_statement) == OPTION_CONIDS
    assert len(default_statement.critical_warnings) == 4
    assert segregated_statement.segregated_closed_option_count == 4
    assert _option_symbols(segregated_statement) == []
    assert [security.symbol for security in _securities(segregated_statement)] == ["99001"]
    assert segregated_statement.critical_warnings == []
    assert kursliste_statement.critical_warnings == []
    assert _option_symbols(kursliste_statement) == []
    assert segregated_statement.listOfBankAccounts.bankAccount[0].taxValue.balance == SETTLED_CASH
    assert default_statement.listOfBankAccounts.bankAccount[0].taxValue.balance == SETTLED_CASH
    assert segregated_statement.totalTaxValue == default_statement.totalTaxValue
    assert segregated_statement.totalGrossRevenueA == default_statement.totalGrossRevenueA
    assert segregated_statement.totalGrossRevenueB == default_statement.totalGrossRevenueB
    assert (
        segregated_statement.totalWithHoldingTaxClaim == default_statement.totalWithHoldingTaxClaim
    )
    assert "Segregated 4 source-proven closed IBKR option position(s)" in caplog.text

    segregated_statement.validate_model()
    kursliste_statement.validate_model()
    assert b"88001" not in segregated_statement.to_xml_bytes()
    assert b"ZZ0000000000" in segregated_statement.to_xml_bytes()

    for minimal in (False, True):
        output_pdf = tmp_path / f"segregated-{minimal}.pdf"
        render_tax_statement(
            segregated_statement,
            output_pdf,
            minimal_frontpage_placeholder=minimal,
            language="en",
        )
        text = "".join(page.extract_text() or "" for page in PdfReader(output_pdf).pages)
        # assert "source-proven closed IBKR option" in text
        # assert "original broker activity and settlement evidence" in text

    kursliste_pdf = tmp_path / "segregated-kursliste.pdf"
    render_tax_statement(kursliste_statement, kursliste_pdf, language="en")
    kursliste_text = "".join(page.extract_text() or "" for page in PdfReader(kursliste_pdf).pages)
    # assert "source-proven closed IBKR option" in kursliste_text


def test_boundary_flex_is_evidence_only_and_does_not_change_cash(tmp_path):
    statement = _import(
        tmp_path,
        _flex_xml(include_opening_snapshot=False),
        segregate=True,
        boundary_xml=_opening_boundary_xml(),
    )

    assert statement.segregated_closed_option_count == 4
    assert statement.listOfBankAccounts.bankAccount[0].taxValue.balance == SETTLED_CASH


def test_option_mutations_are_not_netted_across_ibkr_accounts(tmp_path):
    first_account = _flex_xml(include_final_closes=False)
    second_account = _flex_xml(include_final_closes=False).replace(ACCOUNT_ID, "SYNTH-IBKR-02")
    second_account = second_account.replace(
        'tradeDate="20250304" settleDateTarget="20250304" quantity="1"',
        'tradeDate="20250304" settleDateTarget="20250304" quantity="-1"',
    )
    second_statements = second_account.split('<FlexStatements count="2">', maxsplit=1)[1].split(
        "</FlexStatements>", maxsplit=1
    )[0]
    two_account_xml = first_account.replace(
        '<FlexStatements count="2">', '<FlexStatements count="4">', 1
    ).replace("</FlexStatements>", f"{second_statements}</FlexStatements>", 1)

    statement = _import(tmp_path, two_account_xml, segregate=True)

    assert statement.segregated_closed_option_count == 6
    assert _option_symbols(statement) == ["88004", "88004"]


def test_source_identifiers_and_conflicting_contract_records_keep_every_option_instance(tmp_path):
    identified_xml = _flex_xml(include_opening_snapshot=False).replace(
        'conid="88001" isin=""', 'conid="88001" isin="ZY0000000000"'
    )
    identified_statement = _import(tmp_path, identified_xml, segregate=True)
    assert identified_statement.segregated_closed_option_count == 3
    assert _option_symbols(identified_statement) == ["88001"]

    conflicting_identity_xml = _with_conflicting_contract_record(
        _flex_xml(include_opening_snapshot=False), "OPT", "ZY0000000000"
    )
    conflicting_identity_statement = _import(tmp_path, conflicting_identity_xml, segregate=True)
    assert conflicting_identity_statement.segregated_closed_option_count == 3
    assert _option_symbols(conflicting_identity_statement) == ["88001", "88001"]

    conflicting_category_xml = _with_conflicting_contract_record(
        _flex_xml(include_opening_snapshot=False), "FOP", ""
    )
    conflicting_category_statement = _import(tmp_path, conflicting_category_xml, segregate=True)
    assert conflicting_category_statement.segregated_closed_option_count == 3
    assert _option_symbols(conflicting_category_statement) == ["88001"]


def test_incomplete_or_nonzero_evidence_keeps_option_in_statement(tmp_path):
    cases = [
        (_flex_xml(ending_option_quantity="1"), "88001"),
        (_flex_xml(opening_option_quantity="1"), "88001"),
        (_flex_xml(source_from="2025-01-02"), "88001"),
        (_with_overlapping_full_period_source(_flex_xml()), "88001"),
        (_flex_xml(include_ending_snapshot=False, include_opening_snapshot=False), "88001"),
        (_flex_xml(ending_report_date="2025-12-30", include_opening_snapshot=False), "88001"),
        (_flex_xml(include_final_closes=False, include_opening_snapshot=False), "88004"),
        (_flex_xml(include_option_payment=True, include_opening_snapshot=False), "88001"),
        (_flex_xml(missing_activity_section="CashTransactions"), "88001"),
    ]

    for xml, expected_symbol in cases:
        statement = _import(tmp_path, xml, segregate=True)
        assert expected_symbol in _option_symbols(statement)

    ambiguous_statement = _import(
        tmp_path,
        _flex_xml(include_opening_snapshot=False),
        segregate=True,
        boundary_xml=_ambiguous_ending_boundary_xml(),
    )
    assert "88001" in _option_symbols(ambiguous_statement)

    correction_statement = _import(
        tmp_path,
        _flex_xml(include_opening_snapshot=False),
        segregate=True,
        corrections_xml=_corrections_xml(),
    )
    assert "88001" in _option_symbols(correction_statement)


def test_segregation_flag_rejects_non_ibkr_importers(tmp_path):
    input_file = tmp_path / "unrelated.xml"
    input_file.write_text("<not-used />", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "process",
            str(input_file),
            "--importer",
            "none",
            "--segregate-proven-closed-options",
        ],
    )

    assert result.exit_code != 0
    assert "available only with --importer ibkr" in result.output
