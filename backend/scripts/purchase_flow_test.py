import argparse
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from app.models import Base, Item, ItemLot, Location, Purchase, PurchaseItem, PurchaseInvoicePayment, Supplier, SupplierAdjustment
from sqlalchemy import func


def get_supplier_balance(session, supplier_id, up_to=None, exclude_purchase_id=None):
    if not supplier_id:
        return Decimal("0")

    purchases_q = session.query(func.coalesce(func.sum(Purchase.total), 0)).filter(
        Purchase.supplier_id == supplier_id,
        Purchase.kind == "purchase",
    )
    returns_q = session.query(func.coalesce(func.sum(Purchase.total), 0)).filter(
        Purchase.supplier_id == supplier_id,
        Purchase.kind == "purchase_return",
    )
    if up_to:
        purchases_q = purchases_q.filter(Purchase.date <= up_to)
        returns_q = returns_q.filter(Purchase.date <= up_to)
    if exclude_purchase_id:
        purchases_q = purchases_q.filter(Purchase.id != exclude_purchase_id)

    purchases_total = Decimal(str(purchases_q.scalar() or 0))
    returns_total = Decimal(str(returns_q.scalar() or 0))

    payments_q = (
        session.query(func.coalesce(func.sum(PurchaseInvoicePayment.amount), 0))
        .join(Purchase)
        .filter(Purchase.supplier_id == supplier_id)
    )
    if up_to:
        payments_q = payments_q.filter(PurchaseInvoicePayment.date <= up_to)
    payments_total = Decimal(str(payments_q.scalar() or 0))

    adjustments_q = session.query(
        SupplierAdjustment.adjustment_type,
        func.coalesce(func.sum(SupplierAdjustment.amount), 0),
    ).filter(SupplierAdjustment.supplier_id == supplier_id)
    if up_to:
        adjustments_q = adjustments_q.filter(SupplierAdjustment.date <= up_to.date())
    adjustments = adjustments_q.group_by(SupplierAdjustment.adjustment_type).all()
    additions = Decimal("0")
    discounts = Decimal("0")
    for adj_type, amount in adjustments:
        amount_val = Decimal(str(amount or 0))
        if adj_type in {"addition", "opening_balance"}:
            additions += amount_val
        elif adj_type == "discount":
            discounts += amount_val

    return purchases_total - returns_total - payments_total + additions - discounts


def make_engine(db_path: Path):
    return create_engine(f"sqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False})


def parse_args():
    parser = argparse.ArgumentParser(description="Manual purchase flow test (isolated DB).")
    parser.add_argument("--db", default=":memory:", help="Path to test sqlite db or ':memory:'.")
    parser.add_argument("--reset", action="store_true", help="Delete test db if exists.")
    return parser.parse_args()


def add_purchase(session, supplier, location, item, lot_code, date, total):
    lot = ItemLot(item_id=item.id, lot_code=lot_code, purchase_price=Decimal("0"))
    session.add(lot)
    session.flush()

    purchase = Purchase(
        date=date,
        supplier_id=supplier.id,
        location_id=location.id,
        invoice_no="",
        notes="test",
        shipping_cost=Decimal("0"),
        kind="purchase",
        subtotal=Decimal(str(total)),
        total=Decimal(str(total)),
    )
    session.add(purchase)
    session.flush()

    session.add(
        PurchaseItem(
            purchase_id=purchase.id,
            item_id=item.id,
            lot_id=lot.id,
            qty=Decimal("1"),
            bonus_qty=Decimal("0"),
            tax_amount=Decimal("0"),
            unit_price=Decimal(str(total)),
            discount_base=Decimal("0"),
            discount_extra=Decimal("0"),
            line_total=Decimal(str(total)),
            total=Decimal(str(total)),
        )
    )
    return purchase


def main():
    args = parse_args()
    if args.db == ":memory:":
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        db_path = None
    else:
        db_path = Path(args.db)
        if db_path.exists():
            if args.reset:
                db_path.unlink()
            else:
                raise SystemExit(f"Test DB already exists: {db_path}. Use --reset to recreate.")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = make_engine(db_path)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with Session() as session:
        supplier_a = Supplier(name="Supplier A")
        supplier_b = Supplier(name="Supplier B")
        session.add_all([supplier_a, supplier_b])

        location = Location(name="Main Warehouse", type="warehouse")
        item = Item(name="Test Item", unit="box", purchase_price=Decimal("0"), sale_price=Decimal("0"))
        session.add_all([location, item])
        session.flush()

        # Opening balances
        session.add(
            SupplierAdjustment(
                supplier_id=supplier_a.id,
                date=datetime(2026, 1, 1).date(),
                adjustment_type="opening_balance",
                amount=Decimal("0"),
                notes="opening",
            )
        )
        session.add(
            SupplierAdjustment(
                supplier_id=supplier_b.id,
                date=datetime(2026, 1, 1).date(),
                adjustment_type="opening_balance",
                amount=Decimal("20"),
                notes="opening",
            )
        )
        session.commit()

        # Supplier A: purchase 100, pay 50, then purchase 100
        p1_date = datetime(2026, 1, 2)
        p1 = add_purchase(session, supplier_a, location, item, "BATCH-A-1", p1_date, 100)
        session.commit()

        session.add(
            PurchaseInvoicePayment(
                purchase_id=p1.id,
                cash_account_id=None,
                date=datetime(2026, 1, 3),
                amount=Decimal("50"),
                notes="payment",
            )
        )
        session.commit()

        p2_date = datetime(2026, 1, 4)
        p2 = add_purchase(session, supplier_a, location, item, "BATCH-A-2", p2_date, 100)
        session.commit()

        # Supplier B: purchase 100 (opening 20)
        p3_date = datetime(2026, 1, 2)
        p3 = add_purchase(session, supplier_b, location, item, "BATCH-B-1", p3_date, 100)
        session.commit()

        # Compute balances using same logic as printing
        prev_a_p1 = get_supplier_balance(session, supplier_a.id, up_to=p1_date, exclude_purchase_id=p1.id)
        prev_a_p2 = get_supplier_balance(session, supplier_a.id, up_to=p2_date, exclude_purchase_id=p2.id)
        prev_b_p3 = get_supplier_balance(session, supplier_b.id, up_to=p3_date, exclude_purchase_id=p3.id)

        print("Supplier A - Purchase 1")
        print("  prev balance:", prev_a_p1, "invoice:", p1.total, "new balance:", prev_a_p1 + Decimal(str(p1.total)))
        print("Supplier A - Purchase 2 (after payment 50)")
        print("  prev balance:", prev_a_p2, "invoice:", p2.total, "new balance:", prev_a_p2 + Decimal(str(p2.total)))
        print("Supplier B - Purchase 1 (opening 20)")
        print("  prev balance:", prev_b_p3, "invoice:", p3.total, "new balance:", prev_b_p3 + Decimal(str(p3.total)))

    if db_path:
        print(f"Test DB saved at: {db_path}")


if __name__ == "__main__":
    main()
