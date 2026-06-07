from datetime import datetime
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from .db import Base


class ItemCategory(Base):
    __tablename__ = "item_categories"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    notes = Column(Text, nullable=True)


class Item(Base):
    __tablename__ = "items"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(100), unique=True, nullable=True)
    name = Column(String(200), nullable=False)
    unit = Column(String(50), nullable=True, default="علبة")
    item_kind = Column(String(20), nullable=False, default="general")  # general | private
    purchase_price = Column(Numeric(12, 2), nullable=False, default=0)
    sale_price = Column(Numeric(12, 2), nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    notes = Column(Text, nullable=True)
    category_id = Column(Integer, ForeignKey("item_categories.id"), nullable=True)

    units = relationship("ItemUnit", back_populates="item", cascade="all, delete-orphan")


class ItemUnit(Base):
    __tablename__ = "item_units"
    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    name = Column(String(100), nullable=False)
    factor = Column(Numeric(12, 4), nullable=False, default=1)
    notes = Column(Text, nullable=True)

    item = relationship("Item", back_populates="units")


class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    contact_name = Column(String(200), nullable=True)
    contact_phone = Column(String(50), nullable=True)
    phone = Column(String(50), nullable=True)
    phone2 = Column(String(50), nullable=True)
    fax = Column(String(50), nullable=True)
    address = Column(String(300), nullable=True)
    governorate = Column(String(100), nullable=True)
    city = Column(String(100), nullable=True)
    region = Column(String(100), nullable=True)
    email = Column(String(200), nullable=True)
    website = Column(String(200), nullable=True)
    notes = Column(Text, nullable=True)


class SupplierAdjustment(Base):
    __tablename__ = "supplier_adjustments"
    id = Column(Integer, primary_key=True, index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    date = Column(Date, nullable=False)
    adjustment_type = Column(String(20), nullable=False)  # discount | addition
    amount = Column(Numeric(12, 2), nullable=False, default=0)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    supplier = relationship("Supplier")


class SupplierTransaction(Base):
    __tablename__ = "supplier_transactions"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(DateTime, nullable=False, default=datetime.now)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    type = Column(String(50), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False, default=0)
    prev_balance_snapshot = Column(Numeric(12, 2), nullable=True)
    new_balance_snapshot = Column(Numeric(12, 2), nullable=True)
    notes = Column(Text, nullable=True)
    source_type = Column(String(50), nullable=True)
    source_id = Column(Integer, nullable=True)

    supplier = relationship("Supplier")


class Doctor(Base):
    __tablename__ = "doctors"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    phone = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)


class Representative(Base):
    __tablename__ = "representatives"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    code = Column(String(50), nullable=True)
    phone = Column(String(50), nullable=True)
    home_phone = Column(String(50), nullable=True)
    mobile = Column(String(50), nullable=True)
    address = Column(String(300), nullable=True)
    governorate = Column(String(100), nullable=True)
    city = Column(String(100), nullable=True)
    region = Column(String(100), nullable=True)
    birth_date = Column(Date, nullable=True)
    gender = Column(String(20), nullable=True)
    national_id = Column(String(50), nullable=True)
    job_title = Column(String(100), nullable=True)
    supervisor = Column(String(200), nullable=True)
    hire_date = Column(Date, nullable=True)
    base_salary = Column(Numeric(12, 2), nullable=False, default=0)
    hourly_rate = Column(Numeric(12, 2), nullable=False, default=0)
    insurance_no = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)


location_reps = Table(
    "location_reps",
    Base.metadata,
    Column("location_id", Integer, ForeignKey("locations.id"), primary_key=True),
    Column("rep_id", Integer, ForeignKey("representatives.id"), primary_key=True),
)


class EmployeeAddition(Base):
    __tablename__ = "employee_additions"
    id = Column(Integer, primary_key=True, index=True)
    rep_id = Column(Integer, ForeignKey("representatives.id"), nullable=False)
    date = Column(Date, nullable=False)
    month = Column(Integer, nullable=False, default=0)
    year = Column(Integer, nullable=False, default=0)
    reason = Column(String(200), nullable=True)
    amount = Column(Numeric(12, 2), nullable=False, default=0)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    rep = relationship("Representative")


class EmployeeDeduction(Base):
    __tablename__ = "employee_deductions"
    id = Column(Integer, primary_key=True, index=True)
    rep_id = Column(Integer, ForeignKey("representatives.id"), nullable=False)
    date = Column(Date, nullable=False)
    month = Column(Integer, nullable=False, default=0)
    year = Column(Integer, nullable=False, default=0)
    reason = Column(String(200), nullable=True)
    amount = Column(Numeric(12, 2), nullable=False, default=0)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    rep = relationship("Representative")


class EmployeeSalary(Base):
    __tablename__ = "employee_salaries"
    id = Column(Integer, primary_key=True, index=True)
    rep_id = Column(Integer, ForeignKey("representatives.id"), nullable=False)
    month = Column(Integer, nullable=False)
    year = Column(Integer, nullable=False)
    date = Column(Date, nullable=False)
    amount = Column(Numeric(12, 2), nullable=False, default=0)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    rep = relationship("Representative")


class CashAccount(Base):
    __tablename__ = "cash_accounts"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    rep_id = Column(Integer, ForeignKey("representatives.id"), nullable=True)
    is_main = Column(Boolean, nullable=False, default=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    rep = relationship("Representative")


class CashTransaction(Base):
    __tablename__ = "cash_transactions"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(DateTime, nullable=False)
    type = Column(String(50), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False, default=0)
    from_account_id = Column(Integer, ForeignKey("cash_accounts.id"), nullable=True)
    to_account_id = Column(Integer, ForeignKey("cash_accounts.id"), nullable=True)
    notes = Column(Text, nullable=True)
    source_type = Column(String(50), nullable=True)
    source_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    from_account = relationship("CashAccount", foreign_keys=[from_account_id])
    to_account = relationship("CashAccount", foreign_keys=[to_account_id])


class OtherExpense(Base):
    __tablename__ = "other_expenses"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(DateTime, nullable=False)
    title = Column(String(200), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False, default=0)
    notes = Column(Text, nullable=True)
    source_type = Column(String(50), nullable=True)
    source_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class SalesInvoicePayment(Base):
    __tablename__ = "sales_invoice_payments"
    id = Column(Integer, primary_key=True, index=True)
    sale_id = Column(Integer, ForeignKey("transfers.id"), nullable=False)
    rep_id = Column(Integer, ForeignKey("representatives.id"), nullable=True)
    cash_account_id = Column(Integer, ForeignKey("cash_accounts.id"), nullable=True)
    date = Column(DateTime, nullable=False)
    amount = Column(Numeric(12, 2), nullable=False, default=0)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    sale = relationship("Transfer")
    rep = relationship("Representative")
    cash_account = relationship("CashAccount")


class PurchaseInvoicePayment(Base):
    __tablename__ = "purchase_invoice_payments"
    id = Column(Integer, primary_key=True, index=True)
    purchase_id = Column(Integer, ForeignKey("purchases.id"), nullable=False)
    cash_account_id = Column(Integer, ForeignKey("cash_accounts.id"), nullable=True)
    date = Column(DateTime, nullable=False)
    amount = Column(Numeric(12, 2), nullable=False, default=0)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    purchase = relationship("Purchase")
    cash_account = relationship("CashAccount")

class Location(Base):
    __tablename__ = "locations"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    type = Column(String(50), nullable=False)  # pharmacy | warehouse | sub_warehouse
    address = Column(String(300), nullable=True)
    phone = Column(String(50), nullable=True)
    region = Column(String(100), nullable=True)
    notes = Column(Text, nullable=True)
    rep_id = Column(Integer, ForeignKey("representatives.id"), nullable=True)

    rep = relationship("Representative")
    reps = relationship("Representative", secondary=location_reps, back_populates="locations")


Representative.locations = relationship("Location", secondary=location_reps, back_populates="reps")


class LocationTransaction(Base):
    __tablename__ = "location_transactions"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(DateTime, nullable=False, default=datetime.now)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    type = Column(String(50), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False, default=0)
    receipt_no = Column(String(100), nullable=True)
    prev_balance_snapshot = Column(Numeric(12, 2), nullable=True)
    new_balance_snapshot = Column(Numeric(12, 2), nullable=True)
    notes = Column(Text, nullable=True)
    source_type = Column(String(50), nullable=True)
    source_id = Column(Integer, nullable=True)

    location = relationship("Location")


class ItemLot(Base):
    __tablename__ = "item_lots"
    __table_args__ = (UniqueConstraint("item_id", "lot_code", name="uq_item_lot"),)
    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    lot_code = Column(String(100), nullable=False)
    expiry_date = Column(Date, nullable=True)
    purchase_price = Column(Numeric(12, 2), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    item = relationship("Item")


class InventoryMove(Base):
    __tablename__ = "inventory_moves"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(DateTime, nullable=False)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    lot_id = Column(Integer, ForeignKey("item_lots.id"), nullable=True)
    qty_in = Column(Numeric(12, 2), nullable=False, default=0)
    qty_out = Column(Numeric(12, 2), nullable=False, default=0)
    source_type = Column(String(50), nullable=False)
    source_id = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)

    location = relationship("Location")
    item = relationship("Item")
    lot = relationship("ItemLot")


class Purchase(Base):
    __tablename__ = "purchases"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(DateTime, nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=True)
    invoice_no = Column(String(100), nullable=True)
    notes = Column(Text, nullable=True)
    subtotal = Column(Numeric(12, 2), nullable=False, default=0)
    shipping_cost = Column(Numeric(12, 2), nullable=False, default=0)
    total = Column(Numeric(12, 2), nullable=False, default=0)
    kind = Column(String(20), nullable=False, default="purchase")  # purchase | purchase_return

    supplier = relationship("Supplier")
    location = relationship("Location")
    items = relationship("PurchaseItem", cascade="all, delete-orphan")


class PurchaseItem(Base):
    __tablename__ = "purchase_items"
    id = Column(Integer, primary_key=True, index=True)
    purchase_id = Column(Integer, ForeignKey("purchases.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    lot_id = Column(Integer, ForeignKey("item_lots.id"), nullable=True)
    qty = Column(Numeric(12, 2), nullable=False, default=0)
    bonus_qty = Column(Numeric(12, 2), nullable=False, default=0)
    tax_amount = Column(Numeric(12, 2), nullable=False, default=0)
    unit_price = Column(Numeric(12, 2), nullable=False, default=0)
    discount_base = Column(Numeric(12, 2), nullable=False, default=0)
    discount_extra = Column(Numeric(12, 2), nullable=False, default=0)
    total = Column(Numeric(12, 2), nullable=False, default=0)
    line_total = Column(Numeric(12, 2), nullable=False, default=0)

    item = relationship("Item")
    lot = relationship("ItemLot")


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(DateTime, nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    notes = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="open")
    total = Column(Numeric(12, 2), nullable=False, default=0)

    supplier = relationship("Supplier")
    location = relationship("Location")
    lines = relationship("PurchaseOrderLine", cascade="all, delete-orphan")


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_lines"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    lot_code = Column(String(100), nullable=True)
    qty = Column(Numeric(12, 2), nullable=False, default=0)
    bonus_qty = Column(Numeric(12, 2), nullable=False, default=0)
    tax_amount = Column(Numeric(12, 2), nullable=False, default=0)
    unit_price = Column(Numeric(12, 2), nullable=False, default=0)
    discount_base = Column(Numeric(12, 2), nullable=False, default=0)
    discount_extra = Column(Numeric(12, 2), nullable=False, default=0)
    line_total = Column(Numeric(12, 2), nullable=False, default=0)

    item = relationship("Item")


class Transfer(Base):
    __tablename__ = "transfers"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(DateTime, nullable=False)
    kind = Column(String(20), nullable=False, default="transfer")  # transfer | sale | sale_return
    price_category = Column(String(50), nullable=True)
    from_location_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    to_location_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    rep_id = Column(Integer, ForeignKey("representatives.id"), nullable=True)
    notes = Column(Text, nullable=True)
    total = Column(Numeric(12, 2), nullable=True)

    from_location = relationship("Location", foreign_keys=[from_location_id])
    to_location = relationship("Location", foreign_keys=[to_location_id])
    rep = relationship("Representative")
    lines = relationship("TransferLine", cascade="all, delete-orphan")


class TransferLine(Base):
    __tablename__ = "transfer_lines"
    id = Column(Integer, primary_key=True, index=True)
    transfer_id = Column(Integer, ForeignKey("transfers.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    requested_qty = Column(Numeric(12, 2), nullable=False, default=0)
    unit_price = Column(Numeric(12, 2), nullable=False, default=0)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=True)
    commission_amount = Column(Numeric(12, 2), nullable=False, default=0)
    bonus_amount = Column(Numeric(12, 2), nullable=False, default=0)
    discount_amount = Column(Numeric(12, 2), nullable=False, default=0)
    line_total = Column(Numeric(12, 2), nullable=False, default=0)

    item = relationship("Item")
    doctor = relationship("Doctor")
    allocations = relationship("TransferAllocation", cascade="all, delete-orphan")


class TransferAllocation(Base):
    __tablename__ = "transfer_allocations"
    id = Column(Integer, primary_key=True, index=True)
    transfer_line_id = Column(Integer, ForeignKey("transfer_lines.id"), nullable=False)
    lot_id = Column(Integer, ForeignKey("item_lots.id"), nullable=False)
    qty = Column(Numeric(12, 2), nullable=False, default=0)
    lot_code_snapshot = Column(String(100), nullable=False)

    lot = relationship("ItemLot")


class DoctorCommissionRule(Base):
    __tablename__ = "doctor_commission_rules"
    __table_args__ = (
        UniqueConstraint("doctor_id", "pharmacy_location_id", "item_id", name="uq_rule"),
    )
    id = Column(Integer, primary_key=True, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False)
    pharmacy_location_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    commission_type = Column(String(50), nullable=False)
    commission_value = Column(Numeric(12, 2), nullable=False, default=0)
    active = Column(Boolean, nullable=False, default=True)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)

    doctor = relationship("Doctor")
    pharmacy_location = relationship("Location")
    item = relationship("Item")


class DoctorTransaction(Base):
    __tablename__ = "doctor_transactions"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(DateTime, nullable=False)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False)
    pharmacy_location_id = Column(Integer, ForeignKey("locations.id"), nullable=True)
    transfer_id = Column(Integer, ForeignKey("transfers.id"), nullable=True)
    type = Column(String(50), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False, default=0)
    notes = Column(Text, nullable=True)

    doctor = relationship("Doctor")
    pharmacy_location = relationship("Location")
    transfer = relationship("Transfer")


class Stocktake(Base):
    __tablename__ = "stocktakes"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(DateTime, nullable=False)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    notes = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="draft")

    location = relationship("Location")
    lines = relationship("StocktakeLine", cascade="all, delete-orphan")


class StocktakeLine(Base):
    __tablename__ = "stocktake_lines"
    id = Column(Integer, primary_key=True, index=True)
    stocktake_id = Column(Integer, ForeignKey("stocktakes.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    lot_id = Column(Integer, ForeignKey("item_lots.id"), nullable=True)
    counted_qty = Column(Numeric(12, 2), nullable=False, default=0)
    system_qty = Column(Numeric(12, 2), nullable=False, default=0)
    diff_qty = Column(Numeric(12, 2), nullable=False, default=0)

    item = relationship("Item")
    lot = relationship("ItemLot")


class StockOpening(Base):
    __tablename__ = "stock_openings"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(DateTime, nullable=False)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    notes = Column(Text, nullable=True)

    location = relationship("Location")
    lines = relationship("StockOpeningLine", cascade="all, delete-orphan")


class StockOpeningLine(Base):
    __tablename__ = "stock_opening_lines"
    id = Column(Integer, primary_key=True, index=True)
    opening_id = Column(Integer, ForeignKey("stock_openings.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    lot_id = Column(Integer, ForeignKey("item_lots.id"), nullable=True)
    lot_code_snapshot = Column(String(100), nullable=True)
    qty = Column(Numeric(12, 2), nullable=False, default=0)
    unit_price = Column(Numeric(12, 2), nullable=False, default=0)
    discount_percent = Column(Numeric(5, 2), nullable=False, default=0)
    discount_amount = Column(Numeric(12, 2), nullable=False, default=0)
    line_total = Column(Numeric(12, 2), nullable=False, default=0)

    item = relationship("Item")
    lot = relationship("ItemLot")


class SalesOrder(Base):
    __tablename__ = "sales_orders"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(DateTime, nullable=False)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    notes = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="open")
    total = Column(Numeric(12, 2), nullable=False, default=0)

    location = relationship("Location")
    lines = relationship("SalesOrderLine", cascade="all, delete-orphan")


class SalesOrderLine(Base):
    __tablename__ = "sales_order_lines"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("sales_orders.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    qty = Column(Numeric(12, 2), nullable=False, default=0)
    unit_price = Column(Numeric(12, 2), nullable=False, default=0)
    total = Column(Numeric(12, 2), nullable=False, default=0)

    item = relationship("Item")


class DamageNote(Base):
    __tablename__ = "damage_notes"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(DateTime, nullable=False)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    notes = Column(Text, nullable=True)

    location = relationship("Location")
    lines = relationship("DamageLine", cascade="all, delete-orphan")


class DamageLine(Base):
    __tablename__ = "damage_lines"
    id = Column(Integer, primary_key=True, index=True)
    damage_id = Column(Integer, ForeignKey("damage_notes.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    lot_id = Column(Integer, ForeignKey("item_lots.id"), nullable=True)
    lot_code_snapshot = Column(String(100), nullable=True)
    qty = Column(Numeric(12, 2), nullable=False, default=0)

    item = relationship("Item")
    lot = relationship("ItemLot")

