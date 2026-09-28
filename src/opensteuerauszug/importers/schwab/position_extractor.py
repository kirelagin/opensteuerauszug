import csv
import re
from typing import List, Optional, Tuple
from decimal import Decimal, InvalidOperation
from datetime import datetime, date, timedelta
from opensteuerauszug.model.position import Position, CashPosition, SecurityPosition
from opensteuerauszug.model.ech0196 import SecurityStock


class PositionExtractor:
    """
    Extracts position data from Schwab CSV files in the expected format.
    """

    def __init__(self, filename: str):
        self.filename = filename

    def extract_positions(self) -> Optional[Tuple[List[Tuple[Position, SecurityStock]], date, str]]:
        """
        Extracts positions and returns a tuple: (positions, statement_date, depot)
        positions: List of (Position, SecurityStock)
        statement_date: The day after the statement date
        depot: The partial account number
        """
        with open(self.filename, 'r', encoding='utf-8-sig') as f:
            content = f.read()
        return self._extract_positions_from_string(content)

    def _extract_positions_from_string(
        self, content: str
    ) -> Optional[Tuple[List[Tuple[Position, SecurityStock]], date, str]]:
        lines = content.splitlines()
        if not lines or not lines[0].startswith('"Positions for account'):
            # Not the expected format
            return None
        # Extract account number and date from the first line
        m = re.match(
            r'"Positions for account [^ ]+ \.\.\.(\d+) as of [^,]+, (\d{4}/\d{2}/\d{2})"', lines[0]
        )
        if not m:
            return None
        partial_account_number = m.group(1)
        date_str = m.group(2)
        try:
            parsed_date = datetime.strptime(date_str, "%Y/%m/%d").date()
        except Exception:
            return None
        # The convention in the tax statements is that balance values are taken at the START of the day.
        ref_date = parsed_date + timedelta(days=1)
        # Find the header row (should be the third line)
        for i, line in enumerate(lines):
            if line.startswith('"Symbol"'):
                header_idx = i
                break
        else:
            return None
        reader = csv.DictReader(lines[header_idx:], skipinitialspace=True)
        header = reader.fieldnames or []
        required_columns = {"Symbol", "Qty (Quantity)", "Mkt Val (Market Value)"}
        missing_columns = required_columns - set(header)
        if "Security Type" not in header and "Asset Type" not in header:
            missing_columns.add("Asset Type or Security Type")
        if missing_columns:
            raise ValueError(
                "Schwab positions CSV is missing required columns: "
                + ", ".join(sorted(missing_columns))
                + ". Re-export the Positions CSV with these columns enabled."
            )

        positions: List[Tuple[Position, SecurityStock]] = []
        for row in reader:
            # DictReader uses None for missing cells and as the key for surplus cells.
            if None in row or any(not isinstance(value, str) for value in row.values()):
                raise ValueError("Schwab positions CSV contains a malformed row")

            def cell(name: str) -> str:
                return row[name].strip() if name in row else ""

            symbol = cell("Symbol")
            if not symbol and all(not cell(name) for name in header):
                continue
            # Schwab's aggregate total is not a position.
            if symbol == "Positions Total":
                continue

            security_type = cell("Security Type") or cell("Asset Type")
            qty_str = cell("Qty (Quantity)").replace(",", "")
            mkt_val_str = cell("Mkt Val (Market Value)").replace(",", "").replace("$", "")
            description = cell("Description") if "Description" in header else None
            depot = partial_account_number
            if symbol == "Cash & Cash Investments":
                try:
                    amount = Decimal(mkt_val_str)
                    if not amount.is_finite():
                        raise InvalidOperation
                except InvalidOperation as exc:
                    raise ValueError("Schwab positions CSV has a nonnumeric cash value") from exc
                pos = CashPosition(depot=depot, currentCy="USD")
                stock = SecurityStock(
                    referenceDate=ref_date,
                    mutation=False,
                    quotationType="PIECE",
                    quantity=amount,
                    balanceCurrency="USD",
                    balance=amount,
                )
                positions.append((pos, stock))
            elif symbol and " " not in symbol and security_type:
                try:
                    quantity = Decimal(qty_str)
                    if not quantity.is_finite():
                        raise InvalidOperation
                except InvalidOperation as exc:
                    raise ValueError(
                        f"Schwab positions CSV has a nonnumeric quantity for security '{symbol}'. "
                        "Re-export the Positions CSV or provide evidence-backed manual quantities."
                    ) from exc
                pos = SecurityPosition(
                    depot=depot,
                    symbol=symbol,
                    securityType=security_type,
                    description=description,
                    # Would have been nice if Schwab gave us this.
                    # isin=row.get('ISIN')
                )
                stock = SecurityStock(
                    referenceDate=ref_date,
                    mutation=False,
                    quotationType="PIECE",
                    quantity=quantity,
                    balanceCurrency="USD",
                )
                positions.append((pos, stock))
            else:
                raise ValueError("Schwab positions CSV contains an unrecognized position row")

        return positions, ref_date, partial_account_number


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <positions_csv_file>")
        sys.exit(1)
    filename = sys.argv[1]
    extractor = PositionExtractor(filename)
    result = extractor.extract_positions()
    if result is not None:
        import pprint

        pprint.pprint(result)
    else:
        print("File is not a valid Schwab positions CSV or could not extract positions.")
