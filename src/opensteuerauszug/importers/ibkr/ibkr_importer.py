import os
import logging
from typing import Final, List, Any, Dict, Optional, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from collections import defaultdict

from defusedxml import ElementTree

logger = logging.getLogger(__name__)

from opensteuerauszug.model.position import SecurityPosition
from opensteuerauszug.model.ech0196 import (
    BankAccountPayment,
    Institution,
    ISINType,
    SecurityCategory,
    SecurityPayment,
    SecurityStock,
    TaxStatement,
    Client,
)
from opensteuerauszug.config.models import IbkrAccountSettings
from opensteuerauszug.importers.common import (
    CashAccountEntry,
    CashPositionData,
    PositionHints,
    SecurityNameRegistry,
    SecurityPositionData,
    aggregate_mutations,
    apply_withholding_tax_fields,
    augment_list_of_bank_accounts,
    augment_list_of_securities,
    build_client,
    build_security_payment,
    fold_cash_payments,
    parse_swiss_canton,
    resolve_first_last_name,
    to_decimal,
)
from opensteuerauszug.render.translations import get_text, Language, DEFAULT_LANGUAGE

IBKR_ASSET_CATEGORY_TO_ECH_SECURITY_CATEGORY: Final[Dict[str, SecurityCategory]] = {
    "STK": "SHARE",
    "BOND": "BOND",
    "OPT": "OPTION",
    "FOP": "OPTION",
    "FUT": "OTHER",
    "ETF": "FUND",
    "FUND": "FUND",
}
# Import ibflex components to avoid RuntimeWarning about module loading order
import ibflex
from ibflex.parser import FlexParserError
from ibflex.enums import TradeType


def is_summary_level(entry: object) -> bool:
    """Return True when an entry is marked with levelOfDetail SUMMARY."""
    level_of_detail = getattr(entry, "levelOfDetail", None)
    if level_of_detail is None:
        return False
    level_value = (
        level_of_detail.value if hasattr(level_of_detail, "value") else str(level_of_detail)
    )
    return str(level_value).upper() == "SUMMARY"


def should_skip_pseudo_account_entry(entry: object) -> bool:
    """Skip pseudo rows where accountId='-' or mapped-to-None SUMMARY rows."""
    # ibflex maps accountId="-" to None on some entry types, so
    # we only treat missing accountId rows as pseudo entries when they
    # are marked as SUMMARY.
    entry_account_id = getattr(entry, "accountId", None)
    return entry_account_id == "-" or (entry_account_id is None and is_summary_level(entry))


class IbkrImporter:
    """
    Imports Interactive Brokers account data for a given tax period
    from Flex Query XML files.
    """

    def _get_required_field(
        self, data_object: object, field_name: str, object_description: str
    ) -> Any:
        """Helper to get a required field or raise ValueError if missing."""
        value = getattr(data_object, field_name, None)
        if value is None:
            error_desc = object_description  # Use the passed in description
            if hasattr(data_object, 'symbol'):
                error_desc = (
                    f"{object_description} (Symbol: " f"{getattr(data_object, 'symbol', 'N/A')})"
                )
            elif (
                hasattr(data_object, 'accountId') and 'Account:' not in object_description
            ):  # Avoid double "Account:"
                error_desc = (
                    f"{object_description} (Account: "
                    f"{getattr(data_object, 'accountId', 'N/A')})"
                )
            raise ValueError(f"Missing required field '{field_name}' in {error_desc}.")
        if isinstance(value, str) and not value.strip():
            error_desc = object_description  # Use the passed in description
            if hasattr(data_object, 'symbol'):
                error_desc = (
                    f"{object_description} (Symbol: " f"{getattr(data_object, 'symbol', 'N/A')})"
                )
            elif hasattr(data_object, 'accountId') and 'Account:' not in object_description:
                error_desc = (
                    f"{object_description} (Account: "
                    f"{getattr(data_object, 'accountId', 'N/A')})"
                )
            raise ValueError(f"Empty required field '{field_name}' in {error_desc}.")
        return value

    def _price_apply_multiplier(
        self, data_object: Any, price: Decimal, object_description: str
    ) -> Decimal:
        """Helper to apply multiplier to quantity."""
        if price:
            value = getattr(data_object, "assetCategory", None)
            if value in ["OPT", "FOP"]:
                multiplier = self._to_decimal(
                    self._get_required_field(data_object, "multiplier", object_description),
                    "multiplier",
                    object_description,
                )
                return price * multiplier

        return price

    def _to_decimal(
        self, value: object | None, field_name: str, object_description: str
    ) -> Decimal:
        """Converts a value to Decimal, raising ValueError on failure."""
        return to_decimal(value, field_name, object_description)

    def _normalize_country_code(self, value: object | None) -> str | None:
        if value is None:
            return None
        country = str(value).strip().upper()
        if not country:
            return None
        return country[:2]

    def _as_date(self, value: object | None) -> date | None:
        """Normalize Flex date fields, including compact ``YYYYMMDD`` values."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            value = value.split(";")[0].split("T")[0]
            for date_format in ("%Y-%m-%d", "%Y%m%d"):
                try:
                    return datetime.strptime(value, date_format).date()
                except ValueError:
                    pass
        return None

    def _source_identifiers(self, entry: object) -> tuple[str | None, int | None]:
        """Extract source identifiers without manufacturing model identifiers."""
        isin = getattr(entry, "isin", None)
        security_id = getattr(entry, "securityID", None)
        security_id_type = str(getattr(entry, "securityIDType", "") or "").upper()
        if not isin and security_id_type == "ISIN":
            isin = security_id
        if security_id_type not in {"VALOR", "VALORNUMBER"} or security_id is None:
            return isin or None, None
        try:
            return isin or None, int(str(security_id))
        except ValueError:
            return isin or None, None

    def _maybe_update_security_country(
        self,
        security_country_map: Dict[SecurityPosition, str],
        sec_pos: SecurityPosition,
        country_code: str | None,
        source_label: str,
    ) -> None:
        if not country_code:
            return
        existing = security_country_map.get(sec_pos)
        if existing and existing != country_code:
            logger.warning(
                "Conflicting issuer country code for %s from %s: %s (existing: %s)",
                sec_pos.get_processing_identifier(),
                source_label,
                country_code,
                existing,
            )
            return
        if not existing:
            security_country_map[sec_pos] = country_code

    def __init__(
        self,
        period_from: date,
        period_to: date,
        account_settings_list: List[IbkrAccountSettings],
        render_language: Language = DEFAULT_LANGUAGE,
        segregate_proven_closed_options: bool = False,
        boundary_filenames: Optional[List[str]] = None,
    ):
        """
        Initialize the importer with a tax period.

        Args:
            period_from (date): The start date of the tax period.
            period_to (date): The end date of the tax period.
            account_settings_list: List of IBKR account settings.
            render_language (Language): Language for translations.
            segregate_proven_closed_options: Exclude only options with source-proven
                zero opening and closing checkpoints and no reportable payments.
            boundary_filenames: Optional Flex XML snapshot sources used only to
                establish source checkpoints, never to import activity or cash.
        """
        self.period_from = period_from
        self.period_to = period_to
        self.account_settings_list = account_settings_list
        self.render_language = render_language
        self.segregate_proven_closed_options = segregate_proven_closed_options
        self.boundary_filenames = boundary_filenames or []

        if not self.account_settings_list:
            # Currently no account info is used so we keep stumm.
            logger.debug("IbkrImporter initialized with an empty list of " "account settings.")
        # else:
        # print(
        #     f"IbkrImporter initialized. Primary account (if used): "
        #     f"{self.account_settings_list[0].account_id}"
        # )

    def _aggregate_stocks(self, stocks: List[SecurityStock]) -> List[SecurityStock]:
        """Aggregate buy and sell entries on the same date with equal order id if present without reordering."""
        return aggregate_mutations(stocks)

    def _parse_flex_statements(
        self,
        filenames: Sequence[str],
        *,
        file_label: str,
        log_label: str,
        error_label: str,
    ) -> list[ibflex.FlexStatement]:
        statements: list[ibflex.FlexStatement] = []

        for filename in filenames:
            if not os.path.exists(filename):
                raise FileNotFoundError(f"{file_label} not found: {filename}")
            if not filename.lower().endswith(".xml"):
                logger.warning("Skipping non-XML %s: %s", log_label, filename)
                continue

            try:
                logger.info("Parsing %s: %s", log_label, filename)
                response = ibflex.parser.parse(filename)
                if response and response.FlexStatements:
                    for stmt in response.FlexStatements:
                        if should_skip_pseudo_account_entry(stmt):
                            logger.info(
                                "Skipping FlexStatement with pseudo accountId in %s",
                                filename,
                            )
                            continue
                        logger.info(
                            "Successfully parsed statement for account: %s, Period: %s to %s",
                            stmt.accountId,
                            stmt.fromDate,
                            stmt.toDate,
                        )
                        statements.append(stmt)
                else:
                    logger.warning(
                        "No FlexStatements found in %s or response was empty.",
                        filename,
                    )
            except FlexParserError as e:
                raise ValueError(f"Failed to parse {error_label} {filename} with ibflex: {e}")
            except Exception as e:
                raise RuntimeError(f"An unexpected error occurred while parsing {filename}: {e}")

        return statements

    def _open_positions_snapshot_keys(
        self, filenames: Sequence[str]
    ) -> set[tuple[str, date | None, date | None]]:
        """Return keys for Flex statements that explicitly contain OpenPositions.

        ibflex parses both an omitted OpenPositions XML element and an empty
        OpenPositions XML element as an empty tuple. Only an explicit empty
        element proves that the account had no open positions at that snapshot.
        """
        keys = set()
        for filename in filenames:
            root = ElementTree.parse(filename).getroot()
            for statement in root.iter():
                if statement.tag.rsplit("}", 1)[-1] != "FlexStatement":
                    continue
                if not any(child.tag.rsplit("}", 1)[-1] == "OpenPositions" for child in statement):
                    continue
                keys.add(
                    (
                        statement.get("accountId", ""),
                        self._as_date(statement.get("fromDate")),
                        self._as_date(statement.get("toDate")),
                    )
                )
        return keys

    def _has_complete_nonoverlapping_coverage(self, ranges: Sequence[tuple[date, date]]) -> bool:
        """Return whether source-declared ranges cover the requested period exactly once."""
        relevant_ranges = sorted(
            (max(start, self.period_from), min(end, self.period_to))
            for start, end in ranges
            if start <= self.period_to and end >= self.period_from
        )
        expected_start = self.period_from
        for start, end in relevant_ranges:
            if start != expected_start:
                return False
            expected_start = end + timedelta(days=1)
        return expected_start > self.period_to

    def _activity_coverage_keys(
        self, filenames: Sequence[str]
    ) -> set[tuple[str, date | None, date | None]]:
        """Return ranges that explicitly declare every security activity section.

        A source range can prove that no unreported option event occurred only
        when it declares Trades, Transfers, CorporateActions, and
        CashTransactions. Empty elements are valid evidence; omitted elements
        are not.
        """
        required_sections = {"Trades", "Transfers", "CorporateActions", "CashTransactions"}
        keys = set()
        for filename in filenames:
            root = ElementTree.parse(filename).getroot()
            for statement in root.iter():
                if statement.tag.rsplit("}", 1)[-1] != "FlexStatement":
                    continue
                section_names = {child.tag.rsplit("}", 1)[-1] for child in statement}
                if not required_sections <= section_names:
                    continue
                keys.add(
                    (
                        statement.get("accountId", ""),
                        self._as_date(statement.get("fromDate")),
                        self._as_date(statement.get("toDate")),
                    )
                )
        return keys

    def _find_processed_security_position(
        self,
        processed_security_positions: Dict[SecurityPosition, SecurityPositionData],
        account_id: str,
        security_id: object,
    ) -> SecurityPosition | None:
        for position in processed_security_positions:
            if position.depot == account_id and position.symbol == str(security_id):
                return position
        return None

    def _build_cash_transaction_security_position(
        self,
        account_id: str,
        cash_tx: ibflex.CashTransaction,
        description: str,
    ) -> SecurityPosition:
        security_id = self._get_required_field(cash_tx, 'conid', 'CashTransaction')
        isin_attr = cash_tx.isin
        symbol_attr = cash_tx.symbol
        return SecurityPosition(
            depot=account_id,
            valor=None,
            isin=ISINType(isin_attr) if isin_attr else None,
            symbol=str(security_id),
            description=(f"{description} ({symbol_attr})" if symbol_attr else description),
        )

    def _apply_withholding_tax_fields(
        self,
        payment: SecurityPayment,
        amount: Decimal,
        currency: str,
        tx_type: ibflex.CashAction,
    ) -> None:
        if tx_type != ibflex.CashAction.WHTAX:
            return
        apply_withholding_tax_fields(payment, amount, currency)

    def _build_security_payment(
        self,
        *,
        payment_date: date,
        description: str,
        currency: str,
        amount: Decimal,
        tx_type: ibflex.CashAction,
    ) -> SecurityPayment:
        return build_security_payment(
            payment_date=payment_date,
            description=description,
            currency=currency,
            amount=amount,
            broker_label=tx_type.value,
            is_withholding=tx_type == ibflex.CashAction.WHTAX,
            is_securities_lending=tx_type == ibflex.CashAction.PAYMENTINLIEU,
        )

    def _import_corrections_flex_files(
        self,
        corrections_filenames: Sequence[str],
        processed_security_positions: Dict[SecurityPosition, SecurityPositionData],
    ) -> None:
        corrections_flex_statements = self._parse_flex_statements(
            corrections_filenames,
            file_label="Corrections Flex file",
            log_label="corrections Flex statement",
            error_label="corrections Flex file",
        )

        corrections_count = 0
        for stmt in corrections_flex_statements:
            account_id = self._get_required_field(
                stmt,
                'accountId',
                'FlexStatement (corrections)',
            )
            if not stmt.CashTransactions:
                continue

            for cash_tx in stmt.CashTransactions:
                if should_skip_pseudo_account_entry(cash_tx):
                    continue

                settle_date = getattr(cash_tx, 'settleDate', None)
                if settle_date is None:
                    continue
                if isinstance(settle_date, str):
                    settle_date = datetime.strptime(settle_date, "%Y%m%d").date()
                if settle_date < self.period_from or settle_date > self.period_to:
                    continue

                security_id = cash_tx.conid
                if not security_id:
                    continue

                tx_type = cash_tx.type
                if tx_type is None:
                    continue

                if tx_type != ibflex.CashAction.WHTAX:
                    continue

                description = self._get_required_field(
                    cash_tx,
                    'description',
                    'CashTransaction (corrections)',
                )
                amount = self._to_decimal(
                    self._get_required_field(
                        cash_tx,
                        'amount',
                        'CashTransaction (corrections)',
                    ),
                    'amount',
                    f"CashTransaction (corrections) {description[:30]}",
                )
                currency = self._get_required_field(
                    cash_tx,
                    'currency',
                    'CashTransaction (corrections)',
                )

                sec_pos_key = self._find_processed_security_position(
                    processed_security_positions,
                    account_id,
                    security_id,
                )
                if sec_pos_key is None:
                    logger.warning(
                        "Corrections flex: skipping withholding correction for unknown security conid=%s (%s)",
                        security_id,
                        description,
                    )
                    continue

                processed_security_positions[sec_pos_key]['payments'].append(
                    self._build_security_payment(
                        payment_date=settle_date,
                        description=description,
                        currency=currency,
                        amount=amount,
                        tx_type=tx_type,
                    )
                )
                corrections_count += 1

        if corrections_count:
            logger.info(
                "Imported %d withholding-tax correction(s) from corrections flex file(s).",
                corrections_count,
            )

    def import_files(
        self, filenames: List[str], corrections_filenames: Optional[List[str]] = None
    ) -> TaxStatement:
        """
        Import data from IBKR Flex Query XMLs and return a TaxStatement.

        Args:
            filenames: List of file paths to import (XML).
            corrections_filenames: Optional list of "corrections" flex query XML
                files covering the period after the tax year (e.g. Jan–Mar of
                the following year).  Only CashTransactions whose settleDate
                falls within [period_from, period_to] are imported from these
                files, allowing withholding-tax reversals to be netted against
                original deductions.

        Returns:
            The imported tax statement.
        """
        all_flex_statements = self._parse_flex_statements(
            filenames,
            file_label="IBKR Flex statement file",
            log_label="IBKR Flex statement",
            error_label="IBKR Flex XML file",
        )

        boundary_flex_statements = self._parse_flex_statements(
            self.boundary_filenames,
            file_label="IBKR boundary Flex statement file",
            log_label="IBKR boundary Flex statement",
            error_label="IBKR boundary Flex XML file",
        )

        if not all_flex_statements:
            # This might be an error or just a case of no relevant data.
            # "If data is missing do a hard error" - might need adjustment
            logger.warning(
                "No Flex statements were successfully parsed. " "Returning empty TaxStatement."
            )
            return TaxStatement(
                minorVersion=1,
                periodFrom=self.period_from,
                periodTo=self.period_to,
                taxPeriod=self.period_from.year,
                listOfSecurities=None,
                listOfBankAccounts=None,
            )

        # Key: SecurityPosition or tuple for cash. Value: dict with 'stocks', 'payments'
        processed_security_positions: defaultdict[SecurityPosition, SecurityPositionData] = (
            defaultdict(lambda: {'stocks': [], 'payments': []})
        )

        # Best-name-wins registry for security display names.
        security_name_registry = SecurityNameRegistry()

        processed_cash_positions: defaultdict[tuple, CashPositionData] = defaultdict(
            lambda: {'stocks': [], 'payments': []}
        )
        security_country_map: Dict[SecurityPosition, str] = {}
        open_positions_snapshot_keys = self._open_positions_snapshot_keys(
            [*filenames, *self.boundary_filenames]
        )
        activity_coverage_keys = self._activity_coverage_keys(filenames)
        accounts_with_period_end_open_positions: set[str] = set()
        complete_open_position_snapshots: set[tuple[str, date]] = set()
        snapshot_positions: Dict[tuple[str, date], Dict[str, tuple[Decimal, str]]] = {}
        ambiguous_snapshot_dates: set[tuple[str, date]] = set()
        ambiguous_snapshot_positions: set[tuple[str, date, str]] = set()
        source_coverage_ranges: defaultdict[str, list[tuple[date, date]]] = defaultdict(list)

        # Map to store assetCategory and subCategory for each security
        security_asset_category_map: Dict[SecurityPosition, tuple[str, Optional[str]]] = {}
        security_asset_categories: defaultdict[SecurityPosition, set[str]] = defaultdict(set)
        security_event_kinds: defaultdict[SecurityPosition, set[str]] = defaultdict(set)
        source_contract_identities: defaultdict[
            tuple[str, str], set[tuple[str | None, int | None]]
        ] = defaultdict(set)
        source_contract_categories: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
        rights_issue_positions: set[SecurityPosition] = set()

        def record_source_contract(
            depot: str,
            contract: str,
            isin: str | None,
            valor: int | None,
            asset_category: str | None,
        ) -> None:
            source_contract_identities[(depot, contract)].add((isin, valor))
            if asset_category:
                source_contract_categories[(depot, contract)].add(asset_category)

        for stmt, is_boundary_source in [
            *((stmt, False) for stmt in all_flex_statements),
            *((stmt, True) for stmt in boundary_flex_statements),
        ]:
            account_id = self._get_required_field(stmt, 'accountId', 'FlexStatement')
            source_from = self._as_date(getattr(stmt, "fromDate", None))
            source_to = self._as_date(getattr(stmt, "toDate", None))
            open_positions = getattr(stmt, "OpenPositions", None)
            is_period_end_open_positions_snapshot = (
                (account_id, source_from, source_to) in open_positions_snapshot_keys
                and source_to == self.period_to
                and open_positions is not None
                and all(
                    self._as_date(getattr(open_pos, "reportDate", None)) == self.period_to
                    for open_pos in open_positions
                    if not should_skip_pseudo_account_entry(open_pos)
                )
            )
            if is_period_end_open_positions_snapshot and not is_boundary_source:
                accounts_with_period_end_open_positions.add(account_id)

            # An explicit OpenPositions element is an account-wide checkpoint,
            # including when it is empty. Preserve its per-contract evidence
            # separately from the eCH stock history so option segregation never
            # guesses an absent boundary balance from net trades.
            snapshot_date = source_to
            is_complete_open_positions_snapshot = (
                (account_id, source_from, source_to) in open_positions_snapshot_keys
                and snapshot_date is not None
                and open_positions is not None
                and all(
                    self._as_date(getattr(open_pos, "reportDate", None)) == snapshot_date
                    for open_pos in open_positions
                    if not should_skip_pseudo_account_entry(open_pos)
                )
            )
            if is_complete_open_positions_snapshot:
                complete_open_position_snapshots.add((account_id, snapshot_date))
                positions_at_snapshot: Dict[str, tuple[Decimal, str]] = {}
                for open_pos in open_positions:
                    if should_skip_pseudo_account_entry(open_pos):
                        continue
                    snapshot_conid = str(
                        self._get_required_field(open_pos, "conid", "OpenPosition")
                    )
                    snapshot_category = self._get_required_field(
                        open_pos, "assetCategory", "OpenPosition"
                    )
                    snapshot_quantity = self._to_decimal(
                        self._get_required_field(open_pos, "position", "OpenPosition"),
                        "position",
                        f"OpenPosition {snapshot_conid}",
                    )
                    snapshot_isin, snapshot_valor = self._source_identifiers(open_pos)
                    record_source_contract(
                        account_id,
                        snapshot_conid,
                        snapshot_isin,
                        snapshot_valor,
                        snapshot_category,
                    )
                    if snapshot_conid in positions_at_snapshot:
                        ambiguous_snapshot_positions.add(
                            (account_id, snapshot_date, snapshot_conid)
                        )
                        continue
                    positions_at_snapshot[snapshot_conid] = (
                        snapshot_quantity,
                        snapshot_category,
                    )
                snapshot_key = (account_id, snapshot_date)
                previous_snapshot = snapshot_positions.get(snapshot_key)
                if previous_snapshot is not None and previous_snapshot != positions_at_snapshot:
                    ambiguous_snapshot_dates.add(snapshot_key)
                else:
                    snapshot_positions[snapshot_key] = positions_at_snapshot

            if is_boundary_source:
                continue
            if (
                (account_id, source_from, source_to) in activity_coverage_keys
                and source_from is not None
                and source_to is not None
                and source_from <= source_to
            ):
                source_coverage_ranges[account_id].append((source_from, source_to))

            # account_id_processed = account_id # Keep track for summary
            logger.info(f"Processing statement for account: {account_id}")

            def should_skip_entry(entry: Any, entry_label: str) -> bool:
                if should_skip_pseudo_account_entry(entry):
                    logger.info(
                        "Skipping %s entry with pseudo accountId in account %s",
                        entry_label,
                        account_id,
                    )
                    return True
                return False

            # --- Process Trades ---
            if stmt.Trades:
                for trade in stmt.Trades:
                    if not isinstance(trade, ibflex.Trade):
                        # Skipping summary objects.
                        # It seems tempting to use SymbolSummary but for FX these
                        # are actually for the full report period, so have no fixed date.
                        continue
                    if should_skip_entry(trade, "Trade"):
                        continue
                    trade_date = self._get_required_field(trade, 'tradeDate', 'Trade')
                    settle_date = self._get_required_field(trade, 'settleDateTarget', 'Trade')
                    symbol = self._get_required_field(trade, 'symbol', 'Trade')
                    description = self._get_required_field(trade, 'description', 'Trade')
                    asset_category = self._get_required_field(trade, 'assetCategory', 'Trade')

                    conid = str(self._get_required_field(trade, 'conid', 'Trade'))
                    isin = trade.isin  # Optional field always present on dataclass
                    valor = None  # Flex does not typically provide Valor

                    quantity = self._to_decimal(
                        self._get_required_field(trade, 'quantity', 'Trade'),
                        'quantity',
                        f"Trade {symbol}",
                    )
                    trade_price = self._to_decimal(
                        self._get_required_field(trade, 'tradePrice', 'Trade'),
                        'tradePrice',
                        f"Trade {symbol}",
                    )
                    trade_price = self._price_apply_multiplier(
                        trade, trade_price, f"Trade {symbol}"
                    )

                    trade_money = self._to_decimal(
                        self._get_required_field(trade, 'tradeMoney', 'Trade'),
                        'tradeMoney',
                        f"Trade {symbol}",
                    )
                    currency = self._get_required_field(trade, 'currency', 'Trade')
                    # 'BUY' or 'SELL'
                    buy_sell = self._get_required_field(trade, 'buySell', 'Trade')

                    transaction_type: Optional[TradeType] = getattr(trade, 'transactionType', None)
                    expiry_date = getattr(trade, 'expiry', None)
                    close_price = getattr(trade, 'closePrice', None)

                    ib_commission = self._to_decimal(
                        trade.ibCommission if trade.ibCommission is not None else '0',
                        'ibCommission',
                        f"Trade {symbol}",
                    )

                    if asset_category == "CASH":
                        # FX trades are neutral to the portfolio, so we skip them.
                        logger.debug("Skipped CASH trade {symbol}")
                        continue

                    if asset_category not in ["STK", "OPT", "FUT", "BOND", "ETF", "FUND", "FOP"]:
                        logger.warning(
                            f"Skipping trade for unhandled asset "
                            f"category: {asset_category} (Symbol: {symbol})"
                        )
                        continue

                    sec_pos = SecurityPosition(
                        depot=account_id,
                        valor=valor,
                        isin=ISINType(isin) if isin else None,
                        symbol=conid,
                        description=f"{description} ({symbol})",
                    )

                    # Update name metadata (Priority: 8 for Trades)
                    security_name_registry.update(sec_pos, f"{description} ({symbol})", 8)

                    # Store assetCategory and subCategory
                    sub_category = getattr(trade, 'subCategory', None)
                    if sec_pos not in security_asset_category_map:
                        security_asset_category_map[sec_pos] = (asset_category, sub_category)
                    security_asset_categories[sec_pos].add(asset_category)
                    security_event_kinds[sec_pos].add("trade")
                    source_isin, source_valor = self._source_identifiers(trade)
                    record_source_contract(
                        account_id, conid, source_isin, source_valor, asset_category
                    )

                    trade_country = self._normalize_country_code(
                        getattr(trade, 'issuerCountryCode', None)
                    )
                    self._maybe_update_security_country(
                        security_country_map,
                        sec_pos,
                        trade_country,
                        "Trade",
                    )

                    unit_price = trade_price if trade_price != Decimal(0) else None
                    name = get_text(buy_sell.value.lower(), self.render_language)
                    # Trade price is 0 for expired, assigned or exercised options.
                    if trade_price == Decimal(0) and asset_category in ["OPT", "FOP"]:
                        if transaction_type is None:
                            raise ValueError(
                                f"Transaction type is missing for category {asset_category} with zero price"
                            )
                        if transaction_type == TradeType.BOOKTRADE:
                            if close_price is None:
                                raise ValueError(
                                    f"Close price is missing for category {asset_category} with zero price"
                                )
                            if close_price == Decimal(0) and (
                                expiry_date is None
                                or expiry_date is not None
                                and expiry_date == trade_date
                            ):
                                # For expired options with zero close price, we can assume they expired worthless. However, we need corresponding OptionEAE entry to be sure. But taxwise it does not matter.
                                name = get_text('option_expiration', self.render_language)
                            elif close_price != Decimal(0):
                                name = get_text(
                                    'option_assignment', self.render_language
                                )  # can be assignemnt or exercise, but for that we would need to link the trade to the corresponding OptionEAE entry
                        unit_price = Decimal(0)

                    stock_mutation = SecurityStock(
                        referenceDate=trade_date,
                        mutation=True,
                        quantity=quantity,
                        unitPrice=unit_price,
                        name=name,
                        orderId=trade.ibOrderID,
                        balanceCurrency=currency,
                        quotationType="PIECE",
                    )
                    processed_security_positions[sec_pos]['stocks'].append(stock_mutation)

                    # Cash movements resulting from trades are tracked via the cash transaction section. Only the stock mutation is stored here.

            # --- Process Open Positions (End of Period Snapshot) ---
            if stmt.OpenPositions:
                for open_pos in stmt.OpenPositions:
                    if should_skip_entry(open_pos, "OpenPosition"):
                        continue
                    report_date = self._as_date(
                        self._get_required_field(open_pos, 'reportDate', 'OpenPosition')
                    )
                    if report_date is None:
                        raise ValueError("OpenPosition has an invalid reportDate")
                    symbol = self._get_required_field(open_pos, 'symbol', 'OpenPosition')
                    description = self._get_required_field(open_pos, 'description', 'OpenPosition')
                    asset_category = self._get_required_field(
                        open_pos, 'assetCategory', 'OpenPosition'
                    )

                    conid = str(self._get_required_field(open_pos, 'conid', 'OpenPosition'))
                    isin = open_pos.isin
                    valor = None

                    quantity = self._to_decimal(
                        self._get_required_field(open_pos, 'position', 'OpenPosition'),
                        'position',
                        f"OpenPosition {symbol}",
                    )
                    currency = self._get_required_field(open_pos, 'currency', 'OpenPosition')

                    if asset_category not in ["STK", "OPT", "FUT", "BOND", "ETF", "FUND", "FOP"]:
                        logger.warning(
                            f"Skipping open position for unhandled "
                            f"asset category: {asset_category} "
                            f"(Symbol: {symbol})"
                        )
                        continue

                    sec_pos = SecurityPosition(
                        depot=account_id,
                        valor=valor,
                        isin=ISINType(isin) if isin else None,
                        symbol=conid,
                        description=f"{description} ({symbol})",
                    )

                    # Update name metadata (Priority: 10 for OpenPositions)
                    security_name_registry.update(sec_pos, f"{description} ({symbol})", 10)

                    # Store assetCategory and subCategory
                    sub_category = getattr(open_pos, 'subCategory', None)
                    if sec_pos not in security_asset_category_map:
                        security_asset_category_map[sec_pos] = (asset_category, sub_category)
                    security_asset_categories[sec_pos].add(asset_category)
                    source_isin, source_valor = self._source_identifiers(open_pos)
                    record_source_contract(
                        account_id, conid, source_isin, source_valor, asset_category
                    )

                    position_country = self._normalize_country_code(
                        getattr(open_pos, 'issuerCountryCode', None)
                    )
                    self._maybe_update_security_country(
                        security_country_map,
                        sec_pos,
                        position_country,
                        "OpenPosition",
                    )

                    mark_price = None
                    if getattr(open_pos, 'markPrice', None) is not None:
                        mark_price = self._to_decimal(
                            open_pos.markPrice, 'markPrice', f"OpenPosition {symbol}"
                        )
                        mark_price = self._price_apply_multiplier(
                            open_pos, mark_price, f"OpenPosition {symbol}"
                        )

                    pos_value = None
                    if getattr(open_pos, 'positionValue', None) is not None:
                        pos_value = self._to_decimal(
                            open_pos.positionValue, 'positionValue', f"OpenPosition {symbol}"
                        )

                    balance_stock = SecurityStock(
                        # IBKR's reportDate is an end-of-day balance, while an
                        # eCH stock referenceDate represents the following
                        # start-of-day. Preserve the actual snapshot date rather
                        # than labelling every OpenPosition as period end.
                        referenceDate=report_date + timedelta(days=1),
                        mutation=False,
                        quantity=quantity,
                        balanceCurrency=currency,
                        quotationType="PIECE",
                        unitPrice=mark_price,
                        balance=pos_value,
                    )
                    processed_security_positions[sec_pos]['stocks'].append(balance_stock)

            # --- Process Transfers ---
            if stmt.Transfers:
                for transfer in stmt.Transfers:
                    if should_skip_entry(transfer, "Transfer"):
                        continue
                    asset_category = self._get_required_field(transfer, 'assetCategory', 'Transfer')
                    asset_cat_val = (
                        asset_category.value
                        if hasattr(asset_category, 'value')
                        else str(asset_category)
                    )
                    if str(asset_cat_val).upper() == 'CASH':
                        continue

                    tx_date = transfer.date
                    if tx_date is None:
                        tx_dt = transfer.dateTime
                        if tx_dt is not None:
                            tx_date = tx_dt.date() if hasattr(tx_dt, 'date') else tx_dt
                    if tx_date is None:
                        raise ValueError('Transfer missing date/dateTime')

                    symbol = self._get_required_field(transfer, 'symbol', 'Transfer')
                    description = self._get_required_field(transfer, 'description', 'Transfer')
                    conid = str(self._get_required_field(transfer, 'conid', 'Transfer'))
                    isin = transfer.isin

                    quantity = self._to_decimal(
                        self._get_required_field(transfer, 'quantity', 'Transfer'),
                        'quantity',
                        f"Transfer {symbol}",
                    )

                    direction = transfer.direction
                    direction_val = direction.value.upper() if direction else None
                    is_cancel = ibflex.Code.CANCEL in (transfer.code or ())
                    if direction_val == 'OUT' and quantity > 0 and not is_cancel:
                        raise ValueError(
                            f"Transfer direction OUT but quantity {quantity} positive"
                            f" for {symbol}"
                        )
                    if direction_val == 'IN' and quantity < 0 and not is_cancel:
                        raise ValueError(
                            f"Transfer direction IN but quantity {quantity} negative"
                            f" for {symbol}"
                        )

                    currency = self._get_required_field(transfer, 'currency', 'Transfer')

                    transfer_type = self._get_required_field(transfer, 'type', 'Transfer')
                    transfer_type_val = transfer_type.value
                    account = self._get_required_field(transfer, 'account', 'Transfer')

                    sec_pos = SecurityPosition(
                        depot=account_id,
                        valor=None,
                        isin=ISINType(isin) if isin else None,
                        symbol=conid,
                        description=f"{description} ({symbol})",
                    )

                    # Update name metadata (Priority: 5 for Transfers)
                    security_name_registry.update(sec_pos, f"{description} ({symbol})", 5)

                    stock_mutation = SecurityStock(
                        referenceDate=tx_date,
                        mutation=True,
                        quantity=quantity,
                        name=f"{transfer_type_val} {account}"
                        + (" (Cancelled)" if is_cancel else ""),
                        balanceCurrency=currency,
                        quotationType="PIECE",
                    )

                    processed_security_positions[sec_pos]['stocks'].append(stock_mutation)
                    security_asset_categories[sec_pos].add(str(asset_cat_val))
                    security_event_kinds[sec_pos].add("transfer")
                    source_isin, source_valor = self._source_identifiers(transfer)
                    record_source_contract(
                        account_id, conid, source_isin, source_valor, str(asset_cat_val)
                    )

            # --- Process Corporate Actions ---
            if stmt.CorporateActions:
                for action in stmt.CorporateActions:
                    if should_skip_entry(action, "CorporateAction"):
                        continue
                    # CorporateActions have dates with time stamps, which can be at end of business etc
                    # to avoid this we assume that the reportDate is always the effective date when we see
                    # a difference in the amount of securities.
                    action_date = self._get_required_field(action, "reportDate", "CorporateAction")

                    if hasattr(action_date, "date"):
                        action_date = action_date.date()
                    elif isinstance(action_date, str):
                        date_part = action_date.split(";")[0].split("T")[0]
                        action_date = date.fromisoformat(date_part)

                    symbol = self._get_required_field(action, "symbol", "CorporateAction")
                    description = self._get_required_field(action, "description", "CorporateAction")
                    conid = str(self._get_required_field(action, "conid", "CorporateAction"))
                    isin = action.isin

                    quantity = self._to_decimal(
                        self._get_required_field(action, "quantity", "CorporateAction"),
                        "quantity",
                        f"CorporateAction {symbol}",
                    )
                    currency = self._get_required_field(action, "currency", "CorporateAction")

                    action_description = getattr(action, "actionDescription", None) or description

                    sec_pos = SecurityPosition(
                        depot=account_id,
                        valor=None,
                        isin=ISINType(isin) if isin else None,
                        symbol=conid,
                        description=f"{description} ({symbol})",
                    )

                    # Update name metadata for CorporateActions
                    # Priority logic:
                    # - Issuer available: 4
                    # - Description only (short): 1
                    # - Description only (long): 0 (use symbol fallback via helper logic if priority 0 beats existing)
                    # Actually, if description is long, we prefer symbol.
                    # Let's say:
                    # - Issuer: 4
                    # - Description <= 50 chars: 1
                    # - Description > 50 chars: -1 (Don't use if possible, prefer symbol if nothing else)

                    issuer = getattr(action, "issuer", None)
                    ca_name = f"{description} ({symbol})"
                    ca_priority = 1

                    if issuer:
                        ca_name = f"{issuer} ({symbol})"
                        ca_priority = 4
                    elif len(description) > 50:
                        # Long description and no issuer. Prefer symbol (short name).
                        ca_name = f"{symbol} ({symbol})"
                        ca_priority = 2
                    else:
                        ca_name = f"{description} ({symbol})"
                        ca_priority = 3

                    security_name_registry.update(sec_pos, ca_name, ca_priority)

                    sub_category = getattr(action, "subCategory", None)
                    if sub_category == "RIGHT":
                        rights_issue_positions.add(sec_pos)

                    stock_mutation = SecurityStock(
                        referenceDate=action_date,
                        mutation=True,
                        quantity=quantity,
                        name=action_description,
                        balanceCurrency=currency,
                        quotationType="PIECE",
                    )

                    processed_security_positions[sec_pos]["stocks"].append(stock_mutation)
                    security_event_kinds[sec_pos].add("corporate_action")
                    action_asset_category = getattr(action, "assetCategory", None)
                    source_isin, source_valor = self._source_identifiers(action)
                    record_source_contract(
                        account_id,
                        conid,
                        source_isin,
                        source_valor,
                        str(action_asset_category) if action_asset_category else None,
                    )

            # --- Process Cash Transactions ---
            if stmt.CashTransactions:
                for cash_tx in stmt.CashTransactions:
                    if should_skip_entry(cash_tx, "CashTransaction"):
                        continue
                    tx_date_time = self._get_required_field(cash_tx, 'dateTime', 'CashTransaction')
                    # Ensure tx_date is a date object
                    tx_date = (
                        tx_date_time.date()
                        if hasattr(tx_date_time, 'date')
                        else self._get_required_field(cash_tx, 'tradeDate', 'CashTransaction')
                    )

                    description = self._get_required_field(
                        cash_tx, 'description', 'CashTransaction'
                    )
                    amount = self._to_decimal(
                        self._get_required_field(cash_tx, 'amount', 'CashTransaction'),
                        'amount',
                        f"CashTransaction {description[:30]}",
                    )
                    currency = self._get_required_field(cash_tx, 'currency', 'CashTransaction')

                    security_id = cash_tx.conid
                    tx_type = cash_tx.type
                    if tx_type is None:
                        raise ValueError(f"CashTransaction type is missing for {description}")

                    # Skip fees even if they are associated with a security (e.g., ADR fees)
                    if tx_type in [ibflex.CashAction.FEES, ibflex.CashAction.ADVISORFEES]:
                        logger.warning(f"Fees paid for {description} are ignored for statement.")
                        continue

                    if security_id:
                        tx_type_str = tx_type.value
                        tx_type_str_lower = str(tx_type_str).lower()
                        asset_category = getattr(cash_tx, 'assetCategory', None)
                        # We assert that we do not book general broker/cash interest accidentally under a security.
                        # However, bond interest is fine at import time (assetCategory == "BOND").
                        assert 'interest' not in tx_type_str_lower or asset_category == 'BOND'

                        sec_pos_key = self._find_processed_security_position(
                            processed_security_positions,
                            account_id,
                            security_id,
                        )

                        sym_attr = cash_tx.symbol

                        if sec_pos_key is None:
                            sec_pos_key = self._build_cash_transaction_security_position(
                                account_id,
                                cash_tx,
                                description,
                            )

                        if asset_category:
                            sub_category = getattr(cash_tx, 'subCategory', None)
                            if sec_pos_key not in security_asset_category_map:
                                security_asset_category_map[sec_pos_key] = (
                                    asset_category,
                                    sub_category,
                                )
                            security_asset_categories[sec_pos_key].add(asset_category)
                        source_isin, source_valor = self._source_identifiers(cash_tx)
                        record_source_contract(
                            account_id,
                            str(security_id),
                            source_isin,
                            source_valor,
                            asset_category,
                        )

                        # Update name metadata (Priority: 0 for CashTransactions - lowest)
                        # Use description or symbol if description is generic?
                        # Usually description in CashTx is like "Dividend ...". Not great for security name.
                        # But if it's the only source, it's better than nothing.
                        security_name_registry.update(
                            sec_pos_key,
                            f"{description} ({sym_attr})" if sym_attr else description,
                            0,
                        )

                        sec_payment = self._build_security_payment(
                            payment_date=tx_date,
                            description=description,
                            currency=currency,
                            amount=amount,
                            tx_type=tx_type,
                        )
                        processed_security_positions[sec_pos_key]['payments'].append(sec_payment)
                    else:
                        if tx_type in [ibflex.CashAction.DEPOSITWITHDRAW]:
                            # Not Tax Relant event
                            continue
                        elif tx_type in [ibflex.CashAction.BROKERINTPAID]:
                            # Interst paid due to negative balance: description starting with "<CURRENCY> DEBIT INT FOR"
                            if description.startswith(f"{currency} DEBIT INT FOR"):
                                # Tax relevant event. Fall through to create a bank payment.
                                pass
                            else:
                                # TODO: CREDIT INT is charged on positive balance and would belong to fees (not liabilities).
                                logger.warning(
                                    f"Broker credit interest payment {description} with amount {amount} is not handled, would belong to fees."
                                )
                                continue
                        elif tx_type in [ibflex.CashAction.FEES]:
                            # TODO: Optionally create a costs sections.
                            logger.warning(
                                f"Fees paid for {description} are ignored for statement."
                            )
                            continue
                        elif tx_type in [ibflex.CashAction.ADVISORFEES]:
                            # TODO: Optionally create a costs sections.
                            logger.warning(
                                f"Fees paid for {description} are ignored for statement."
                            )
                            continue
                        elif tx_type in [ibflex.CashAction.BROKERINTRCVD]:
                            # Tax relevant event. Fall through to create a bank payment.
                            pass
                        elif tx_type in [ibflex.CashAction.WHTAX]:
                            # Withholding tax not linked to a security (e.g. yield enhancement).
                            # Tax relevant event. Fall through to create a bank payment.
                            pass
                        else:
                            raise ValueError(
                                f"CashTransaction type {tx_type} is not supported for {description}"
                            )
                        cash_pos_key = (account_id, currency, "MAIN_CASH")

                        bank_payment = BankAccountPayment(
                            paymentDate=tx_date,
                            name=description,
                            amountCurrency=currency,
                            amount=amount,
                        )
                        processed_cash_positions[cash_pos_key]['payments'].append(bank_payment)

        # Corrections are payment evidence for opt-in segregation. Preserve the
        # historical no-flag ordering by importing them later in that path.
        if self.segregate_proven_closed_options and corrections_filenames:
            self._import_corrections_flex_files(
                corrections_filenames,
                processed_security_positions,
            )

        if self.segregate_proven_closed_options:
            opening_snapshot_date = self.period_from - timedelta(days=1)
            ending_snapshot_date = self.period_to
            excluded_positions: list[SecurityPosition] = []
            for sec_pos, data in processed_security_positions.items():
                asset_categories = security_asset_categories[sec_pos]
                event_kinds = security_event_kinds[sec_pos]
                mutations = [stock for stock in data["stocks"] if stock.mutation]
                boundary_balances = [stock for stock in data["stocks"] if not stock.mutation]
                opening_snapshot = snapshot_positions.get(
                    (sec_pos.depot, opening_snapshot_date), {}
                )
                ending_snapshot = snapshot_positions.get((sec_pos.depot, ending_snapshot_date), {})
                opening_evidence = opening_snapshot.get(sec_pos.symbol)
                ending_evidence = ending_snapshot.get(sec_pos.symbol)
                mutation_quantity = sum((stock.quantity for stock in mutations), Decimal("0"))
                source_proven_closing = (
                    Decimal("0") if ending_evidence is None else ending_evidence[0]
                )
                source_proven_opening = (
                    Decimal("0") if opening_evidence is None else opening_evidence[0]
                )
                derived_opening = source_proven_closing - mutation_quantity
                has_opening_checkpoint = (
                    sec_pos.depot,
                    opening_snapshot_date,
                ) in complete_open_position_snapshots
                has_complete_coverage = self._has_complete_nonoverlapping_coverage(
                    source_coverage_ranges[sec_pos.depot]
                )
                contract_key = (sec_pos.depot, sec_pos.symbol)
                contract_identities = source_contract_identities[contract_key]
                has_source_identifier = any(
                    source_isin is not None or source_valor is not None
                    for source_isin, source_valor in contract_identities
                )
                has_contract_collision = (
                    len(contract_identities) > 1
                    or len(source_contract_categories[contract_key]) > 1
                )
                is_ambiguous = (
                    sec_pos.depot,
                    opening_snapshot_date,
                    sec_pos.symbol,
                ) in ambiguous_snapshot_positions or (
                    sec_pos.depot,
                    ending_snapshot_date,
                    sec_pos.symbol,
                ) in ambiguous_snapshot_positions

                if (
                    asset_categories
                    and asset_categories <= {"OPT", "FOP"}
                    and event_kinds == {"trade"}
                    and mutations
                    and all(
                        self.period_from <= stock.referenceDate <= self.period_to
                        for stock in mutations
                    )
                    and mutation_quantity == 0
                    and has_complete_coverage
                    and not has_source_identifier
                    and not has_contract_collision
                    and not data["payments"]
                    and (sec_pos.depot, ending_snapshot_date) in complete_open_position_snapshots
                    and source_proven_closing == 0
                    and (
                        (has_opening_checkpoint and source_proven_opening == 0)
                        or (has_complete_coverage and derived_opening == 0)
                    )
                    and (not has_opening_checkpoint or source_proven_opening == derived_opening)
                    and (sec_pos.depot, opening_snapshot_date) not in ambiguous_snapshot_dates
                    and (sec_pos.depot, ending_snapshot_date) not in ambiguous_snapshot_dates
                    and not is_ambiguous
                    and (opening_evidence is None or opening_evidence[0] == 0)
                    and (ending_evidence is None or ending_evidence[0] == 0)
                    and all(
                        stock.referenceDate
                        in (self.period_from, self.period_to + timedelta(days=1))
                        and stock.quantity == 0
                        and stock.balance in (None, Decimal("0"))
                        for stock in boundary_balances
                    )
                ):
                    excluded_positions.append(sec_pos)
                elif asset_categories and asset_categories <= {"OPT", "FOP"}:
                    logger.info(
                        "Retained IBKR option because closed-option segregation evidence is incomplete: "
                        "depot=%s contract=%s.",
                        sec_pos.depot,
                        sec_pos.symbol,
                    )

            for sec_pos in excluded_positions:
                del processed_security_positions[sec_pos]
                logger.info(
                    "Excluded source-proven closed IBKR option from supported-assets statement: "
                    "depot=%s contract=%s. Retain separate broker activity and settlement evidence.",
                    sec_pos.depot,
                    sec_pos.symbol,
                )

        # A part-year import needs a complete period-end OpenPositions snapshot
        # for every account represented by security data. A stale or omitted
        # snapshot cannot establish either a security's end balance or a zero
        # balance for a security absent from the snapshot. Reject the account
        # before per-security reconciliation can extrapolate from a midyear
        # checkpoint. January-to-December imports retain their existing logic.
        is_annual_period = self.period_from == date(
            self.period_to.year, 1, 1
        ) and self.period_to == date(self.period_to.year, 12, 31)
        if not is_annual_period:
            security_accounts = {sec_pos.depot for sec_pos in processed_security_positions}
            accounts_without_period_end_snapshot = (
                security_accounts - accounts_with_period_end_open_positions
            )
            if accounts_without_period_end_snapshot:
                account_ids = ", ".join(sorted(accounts_without_period_end_snapshot))
                raise ValueError(
                    "Cannot infer part-year boundary balances for account(s) "
                    f"{account_ids}: no verified period-end OpenPositions checkpoint."
                )

        # A security omitted from a verified period-end OpenPositions snapshot
        # is proven to close at zero, so add the explicit eCH closing checkpoint.
        end_plus_one = self.period_to + timedelta(days=1)
        for sec_pos, data in processed_security_positions.items():
            stocks = data["stocks"]
            if any(not stock.mutation for stock in stocks):
                continue

            if is_annual_period:
                continue

            currency = stocks[0].balanceCurrency if stocks else data["payments"][0].amountCurrency
            quotation_type = stocks[0].quotationType if stocks else "PIECE"
            stocks.append(
                SecurityStock(
                    referenceDate=end_plus_one,
                    mutation=False,
                    quotationType=quotation_type,
                    quantity=Decimal("0"),
                    balanceCurrency=currency,
                )
            )

        # --- Process Corrections Flex Files ---
        # Preserve the historical ordering unless opt-in segregation needed
        # correction payments as eligibility evidence above.
        if not self.segregate_proven_closed_options and corrections_filenames:
            self._import_corrections_flex_files(
                corrections_filenames,
                processed_security_positions,
            )

        # --- Assemble the partial TaxStatement and augment it via the shared
        # post-processing stage. The client/institution/canton block below
        # continues to write onto the same object.
        tax_statement = TaxStatement(
            minorVersion=1,
            periodFrom=self.period_from,
            periodTo=self.period_to,
            taxPeriod=self.period_from.year,
        )

        ignore_rights_issues_by_account: Dict[str, bool] = {
            s.account_number: getattr(s, "ignore_rights_issues", False)
            for s in self.account_settings_list
            if getattr(s, "account_number", None)
        }

        def _hints_for(sec_pos: SecurityPosition) -> PositionHints:
            asset_cat, _sub_category = security_asset_category_map.get(sec_pos, ("STK", None))
            sec_category = IBKR_ASSET_CATEGORY_TO_ECH_SECURITY_CATEGORY.get(asset_cat)
            if not sec_category:
                raise ValueError(f"Unknown asset category: {asset_cat}")
            is_rights = sec_pos in rights_issue_positions
            skip_if_zero = is_rights and ignore_rights_issues_by_account.get(sec_pos.depot, False)
            return PositionHints(
                security_category=sec_category,
                country=security_country_map.get(sec_pos, "US"),
                is_rights_issue=is_rights,
                skip_if_zero=skip_if_zero,
            )

        augment_list_of_securities(
            tax_statement,
            processed_security_positions,
            name_registry=security_name_registry,
            hints_for=_hints_for,
        )
        tax_statement.segregated_closed_option_count = (
            len(excluded_positions) if self.segregate_proven_closed_options else 0
        )
        if tax_statement.segregated_closed_option_count:
            logger.info(
                "Segregated %d source-proven closed IBKR option position(s) from the eCH output.",
                tax_statement.segregated_closed_option_count,
            )

        # --- Collect per-account dateOpened / dateClosed + CashReport seeds ---
        account_dates: Dict[str, Dict[str, date | None]] = {}
        for s_stmt in all_flex_statements:
            stmt_account_id = self._get_required_field(s_stmt, 'accountId', 'FlexStatement')
            if s_stmt.AccountInformation:
                acc_info = s_stmt.AccountInformation
                account_dates[stmt_account_id] = {
                    'dateOpened': acc_info.dateOpened,
                    'dateClosed': acc_info.dateClosed,
                }

        seed_entries: List[CashAccountEntry] = []
        for s_stmt in all_flex_statements:
            account_id = s_stmt.accountId
            if not s_stmt.CashReport:
                continue
            for cash_report_currency_obj in s_stmt.CashReport:
                if should_skip_pseudo_account_entry(cash_report_currency_obj):
                    logger.info(
                        "Skipping CashReport entry with pseudo accountId in account %s",
                        account_id,
                    )
                    continue
                curr = cash_report_currency_obj.currency
                if curr is None or curr == "BASE_SUMMARY":
                    continue
                curr = str(curr)

                closing_balance_value: Optional[Decimal] = None
                if cash_report_currency_obj.endingCash is not None:
                    closing_balance_value = self._to_decimal(
                        cash_report_currency_obj.endingCash,
                        'endingCash',
                        f"CashReport {account_id} {curr}",
                    )
                elif (
                    getattr(cash_report_currency_obj, 'balance', None) is not None
                    and getattr(cash_report_currency_obj, 'reportDate', None) == self.period_to
                ):
                    closing_balance_value = self._to_decimal(
                        getattr(cash_report_currency_obj, 'balance'),
                        'balance',
                        f"CashReport {account_id} {curr}",
                    )

                if closing_balance_value is None:
                    continue
                dates_for_account = account_dates.get(account_id, {})
                seed_entries.append(
                    CashAccountEntry(
                        account_id=account_id,
                        currency=curr,
                        closing_balance=closing_balance_value,
                        name=f"{account_id} {curr}",
                        number=f"{account_id}-{curr}",
                        opening_date=dates_for_account.get('dateOpened'),
                        closing_date=dates_for_account.get('dateClosed'),
                    )
                )

        cash_entries = fold_cash_payments(seed_entries, processed_cash_positions)
        augment_list_of_bank_accounts(tax_statement, cash_entries)

        logger.info(
            "Partial TaxStatement created with Trades, OpenPositions, "
            "and basic CashTransactions mapping."
        )

        # Fill in institution
        # Name is sufficient. Avoid setting legal identifiers avoid implying this is
        # officially from the broker.
        tax_statement.institution = Institution(name="Interactive Brokers")

        # --- Create Client object ---
        # TODO: Handle joint accounts
        client_obj: Optional[Client] = None
        if all_flex_statements:
            first_statement = all_flex_statements[0]
            acc_info = getattr(first_statement, 'AccountInformation', None)
            if acc_info:
                canton = parse_swiss_canton(getattr(acc_info, 'stateResidentialAddress', None))
                if canton:
                    tax_statement.canton = canton
                    logger.info(f"Set canton from IBKR stateResidentialAddress: {canton}")

                client_first_name, client_last_name = resolve_first_last_name(
                    first_name=getattr(acc_info, 'firstName', None),
                    last_name=getattr(acc_info, 'lastName', None),
                    full_name=getattr(acc_info, 'name', None),
                    account_holder_name=getattr(acc_info, 'accountHolderName', None),
                )
                client_obj = build_client(
                    client_number=getattr(acc_info, 'accountId', None),
                    first_name=client_first_name,
                    last_name=client_last_name,
                )
        if client_obj:
            tax_statement.client = [client_obj]
        # --- End Client object ---

        return tax_statement


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    logger.info("IbkrImporter module loaded.")
    # Example usage:
    # from .config.models import IbkrAccountSettings
    # settings = IbkrAccountSettings(account_id="U1234567")
    # importer = IbkrImporter(
    #     period_from=date(2023, 1, 1),
    #     period_to=date(2023, 12, 31),
    #     account_settings_list=[settings]
    # )
    #
    # # Create a dummy XML file for testing
    # DUMMY_XML_CONTENT = """
    # <FlexQueryResponse queryName="Test Query" type="AF">
    #   <FlexStatements count="1">
    #     <FlexStatement accountId="U1234567" fromDate="2023-01-01"
    #                    toDate="2023-12-31" period="Year"
    #                    whenGenerated="2024-01-15T10:00:00">
    #       <Trades>
    #         <Trade assetCategory="STK" symbol="AAPL" tradeDate="2023-05-10"
    #                quantity="10" tradePrice="150.00" currency="USD" />
    #       </Trades>
    #       <CashTransactions>
    #         <CashTransaction type="Deposits/Withdrawals"
    #                          dateTime="2023-02-01T00:00:00"
    #                          amount="1000" currency="USD" />
    #       </CashTransactions>
    #       <OpenPositions>
    #         <OpenPosition assetCategory="STK" symbol="MSFT" position="100"
    #                       markPrice="300" currency="USD" />
    #       </OpenPositions>
    #     </FlexStatement>
    #   </FlexStatements>
    # </FlexQueryResponse>
    # """
    # DUMMY_FILE = "dummy_ibkr_flex.xml"
    # with open(DUMMY_FILE, "w") as f:
    #     f.write(DUMMY_XML_CONTENT)
    #
    # try:
    #     print(f"Attempting to import dummy file: {DUMMY_FILE}")
    #     statement = importer.import_files([DUMMY_FILE])
    #     from devtools import debug
    #     debug(statement)
    #     print("Dummy import successful.")
    # except Exception as e:
    #     print(f"Error during example usage: {e}")
    # finally:
    #     if os.path.exists(DUMMY_FILE):
    #         os.remove(DUMMY_FILE)
    logger.info(
        "Example usage in __main__ needs IbkrAccountSettings to be defined "
        "in config.models and 'pip install ibflex devtools'."
    )
